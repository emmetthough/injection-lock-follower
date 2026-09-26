# Injection lock follower — firmware change summary

Thirteen commits against `injection_follower_v3.ino` as received. Every change compiles clean
against a native stub (`test/check.sh`), and the directional logic is checked numerically
(`test/verify.sh`).

---

## Part 1 — Bugs fixed

### 1.1 / 1.2 — The bump loops never stepped

`bump_slower_up` and `bump_xbeam_up` recomputed `bump` from `slowerADCout` on every pass, but
never updated it inside the loop. Ten iterations wrote the **same DAC code ten times**.

Worse than the missing motion: `start_val = iteration_mean` at the bottom meant the
`iteration_mean < start_val` slope test compared two noisy measurements *of the same physical
point*. It fired on measurement noise roughly half the time, sending the channel into relock.
This was almost certainly the dominant source of spurious relock activity.

Also: the step size is now guarded with `max(1, ...)`, since `bump_thresh * fwhm` rounds to
zero for FWHM below ~10 LSB; and the DAC position is committed on every write, so the software
copy and the hardware can no longer disagree.

### 1.3 — `Slope.x0` was a relative offset used as an absolute DAC code

`getSlope` returned `steps[maxInd]`, a sweep-relative offset, but callers assigned it straight
into `slowerGoodADC` and `recover_*` used that as an absolute starting point. Recovery therefore
began near the bottom of the DAC range and swept *away* from the peak. **Recovery almost
certainly never succeeded before this fix.**

`getSlope` now takes the sweep base and returns an absolute code. A second defect on the
`fb_sign == 0` early-return path — it returned `maxInd`, a raw array index — is also fixed.

### 1.4 / 1.5 — Unguarded division and unclamped DAC write in relock

`relock_*` divided by `fb.m`, which `getSlope` returns as exactly `0.0` whenever the half-height
crossing is not found. `inf` cast to `int` is undefined behaviour. The result was then applied
with a bare `ADCout += dx` — the only DAC writes in the firmware without a `constrain`.

Now: a degenerate fit (`m == 0`, non-finite `m`, or `fwhm == 0`) aborts relock and hands to
recovery; a single correction is clamped to ±2·FWHM; and both writes are clamped.

### 1.6 — Missing braces in `getSlope`

Only `m = ...` was inside `if (dx != 0)`. Behaviour preserved deliberately (a failed fit zeroes
the width rather than leaving a stale one), but stated explicitly instead of by accident.

### Recovery sweeps could walk past the DAC rails

**Not in the original review.** Both loops tested the bound *before* stepping and wrote
unclamped:

```c
while (xbeamADCout < 4095) { xbeamADCout += step_xbeam; analogWrite(...); }
```

At 4090 the test passes, +27 gives 4117, and `analogWrite` masks to 12 bits → 21. The output
slams from near full scale to near zero in one step, while the channel is already failed. The
slower is the mirror image.

Now clamped to land exactly on the rail. Reaching it without finding the peak latches
`atLimit`, raises the failure line, and **parks the DAC back at `goodCode`** rather than
abandoning the laser at a rail.

### 1.7 — `clusterPeaks` scrambled the peak report

It insertion-sorted the caller's array in place, so after clustering `highPeakPos[i]` and
`highPeaks[i]` no longer described the same peak and `sendPeaks()` emitted mismatched
`val=`/`pos=` pairs. Control logic was unaffected; the plot was wrong. Now sorts a local copy.

### 1.8 — Division by zero in `trackPeaksInRegions`

`slower_mean /= slower_mean_N` with no guard. Integer div-by-zero returns 0 on Cortex-M3, so
this silently pushed 0 into the running buffer — indistinguishable from a lost lock. Now skips
the push and records `-1` in `lastMean`.

### 1.10 — `acquireScan` had no timeout

All three waits were unbounded: the trigger-low poll, the rising-edge wait, and the ADC
end-of-conversion spin. A stopped ramp generator hung the board forever with the DAC frozen.

Now returns `bool`, with a `micros()` deadline on the trigger waits (`scan_timeout_us`, default
500 ms) and a bounded spin count on EOC. `trackPeaksInRegions` pushes nothing on failure, and
`loop()` skips feedback, so a dead trigger leaves the DAC holding and the command path alive.

### 1.11 — Two competing reference heights

`relock` targeted `fb.y0` (the SPC peak height) while every threshold used `refHeight` (the
init-time measurement). Two setpoints drifting apart in one decision chain. The SPC now
contributes **only the slope**; `y0` is reported but unused.

### 1.12 — Incommensurable standard deviations

`refStd` came from `computePeakStats` — the spread over *individual peaks* — but was compared
against `buf->getStd()`, the spread of *cluster-averaged* heights across scans. The former is
systematically larger, so the `losing_stdThresh` test was effectively dead.

Init now runs a second pass of `STATS_SCANS` tracking iterations and derives both `refHeight`
and `refStd` from the same quantity the servo watches.

---

## Part 2 — Architecture

### 2.1 — The servo ran at the dashboard's refresh rate

`loop()` opened with `SerialUSB.readStringUntil('\n')`, which blocks for the 1000 ms `Stream`
timeout when nothing is pending. **With the host idle the servo ran at ~1 Hz**; when the
dashboard polled it ran at the poll rate. Loop bandwidth was a side effect of the GUI.

Replaced with a non-blocking byte accumulator and `strcmp` dispatch. The servo now runs every
pass, bounded only by `acquireScan` waiting on the ramp trigger — one iteration per scan. The
debug dump is rate-limited to 1 Hz and reports the measured servo rate in Hz.

### 2.3 — The two channels were 400 lines of copy-paste

Eight functions became four taking `Channel&`, plus `serviceChannel()`:

| Was | Now |
|---|---|
| `get_slower_iteration_mean` / `get_xbeam_iteration_mean` | `getIterationMean` |
| `bump_slower_up` / `bump_xbeam_up` | `bumpUp` |
| `relock_slower` / `relock_xbeam` | `relock` |
| `recover_slower` / `recover_xbeam` | `recover` |

The direction asymmetries turned out to be `fb_sign` in disguise and generalise with **no
behaviour change**, verified numerically:

- relock accepted `dx<0` (slower) / `dx>0` (xbeam) → `dx * fbSign < 0`
- recovery swept down / up → `−fbSign`
- recovery start `good+(n+1)·FWHM` / `good+fbSign·(n+1)·FWHM` → the latter

`setDac(Channel&, int)` is now the single point where the DAC is written. Legacy global names
survive as reference aliases, so reporting, SPC and command parsing were untouched.

**`bump_xbeam_up()` was dead code.** Nothing called it. The xbeam's losing-but-not-lost branch
went straight to `relock_xbeam(0)`, skipping the search entirely. Both channels now follow the
flowchart, so the xbeam gains a search stage it has never had.

### Handoff geometry — `fb_sign` correctness

Two problems found by walking the sign combinations by hand.

**The LOST branch of `bumpUp` was provably unreachable.** Exits were tested as success /
passed-maximum / lost, but `start_val` is always at or above the lost threshold, so any reading
low enough to be "lost" is also below `start_val` and was caught first. Cliff-edge losses and
gentle roll-over were handled identically. Lost is now tested first.

**Both handoffs must leave the channel on the gentle flank**, because relock only travels in the
`−fbSign` direction and so can only approach the peak from that side:

- Jump-back was `goodCode − effective_sign·FWHM`, correct only when `effective_sign == −fbSign`.
  Now `goodCode + fbSign·FWHM`.
- The 0.25·FWHM retreat was guarded on `effective_sign > 0` and hardcoded downward. Now guarded
  on `effective_sign != fbSign` and directed `−effective_sign`.

All eight combinations verified to land on the gentle flank.

---

## New features

### Holdoff drift servo

Peaks walk through the acquisition window over hours. Once a peak nears the edge of its search
region, the tracked maximum is a point on its *flank* rather than its top, so the measured
height falls while the lock is fine — **position drift presenting as height loss**, which the
servo then tries to correct with laser current.

- **Error signal:** mean of `maxPos − clusterMean` over all tracked clusters on both channels,
  skipping any flagged lost. Accumulated inside `trackPeaksInRegions`, so it costs no extra
  acquisitions.
- **Calibration (`HC`)** is adaptive: steps the holdoff outward in `holdoff_cal_step` increments
  until displacement reaches `holdoff_cal_frac · SEARCH_WINDOW`, then sweeps symmetrically about
  the start so the least-squares fit is balanced. Aborts if a cluster goes lost mid-sweep;
  always restores the original holdoff.
- **Servo** actuates only when `|error| > holdoff_actuate_frac · SEARCH_WINDOW`, at most once per
  `holdoff_interval_ms`, step clamped to `holdoff_max_step`. Suppressed while either channel is
  failed.
- **Sign:** increasing the holdoff starts the window later, so peaks appear earlier and the
  slope is negative. Correction is `−error / slope`.

The reported slope is `−1/sample_interval`, so inverting it gives your actual ADC sample rate —
a number you currently have no other way to measure.

### Safe-side bias

The maximum of the SPC is not a safe operating point for a channel whose sharp edge sits next to
it. For the slower, the cliff is immediately below the peak, and every path targets the peak, so
after any excursion the servo parks beside the edge and the cycle repeats.

- **`applySafeBias`** — after each successful acquisition, offset by `+fbSign · bias_frac · fwhm`.
  `fbSign` already encodes which side is safe. Verified and reverted if it costs too much
  height, so a wrong `fwhm` cannot walk the channel off the plateau. **On for the slower by
  default.**
- **`continuousBump`** — one safe-side step per locked iteration, kept if the height holds.
  Costs an extra scan per iteration, so **off by default**. Holds off 10 iterations after a
  rejection, otherwise it dithers every iteration at the edge of the band.

### Instability back-off

A per-channel acting threshold `ch.losingThresh` replaces the global `losing_meanThresh`
throughout the control path. `refHeight` is untouched, so lost detection, relock's aim point and
recovery's success test keep their original sensitivity — that separation is the reason not to
lower `refHeight` directly.

- Events counted at top-level relock entry and lost-branch entry (not recursive retries).
- `instab_events` within `instab_window_ms` drops the threshold by `backoff_step`.
- At `backoff_floor` it stops dropping and **latches a fault**. A threshold that decays without
  bound ends in a servo that never acts and reports itself content.
- Creep back is deliberately asymmetric: `backoff_creep` per quiet interval after
  `backoff_quiet_ms`. Symmetric steps would oscillate at the instability period.

With the defaults: one trigger costs 0.02 and takes 40 minutes to undo; reaching the floor needs
39 events with no quiet gaps.

---

## Serial command reference

All commands are newline-terminated. Unknown commands now emit `[WARN]` rather than being
silently ignored. The command buffer is 64 bytes; over-long lines are discarded with a warning.

### Core

| Command | Effect |
|---|---|
| `I` | Re-initialise: find peaks, cluster, measure `refHeight` / `refStd`. This is the "update peaks" setpoint reset. |
| `FB` | Toggle feedback active. |
| `R` | Send binary trace, then peak status. |
| `TD` | Toggle debug output. |
| `ZS` | Zero the slower DAC to midscale, clear its fail and at-limit flags. |
| `ZX` | Same for the xbeam. |
| `Z` | Zero both, clear all flags, drop `initialized` and `SPC_init`. |
| `D<us>` | Set the trigger holdoff **and re-initialise**. |
| `S<start_mA>,<stop_mA>,<step_uA>` | Run a spectral purity curve. Malformed arguments fall back to `-1.5,1.5,25.0`. |
| `C<var>,<value>` | Set a tuning parameter. |

### Holdoff drift servo

| Command | Effect |
|---|---|
| `HC` | Run the adaptive holdoff calibration. Emits a `[START] Holdoff Calibration` block. |
| `HF` | Toggle the holdoff feedback loop. Warns if enabled while uncalibrated. |
| `HR` | Report holdoff status. |
| `HS<us>` | Set the holdoff **without** re-initialising. Use this, not `D`, for anything automated. |

### Safe-side bias / back-off

| Command | Effect |
|---|---|
| `BR` | Report back-off status for both channels. |
| `BZ` | Reset the back-off on both channels. |
| `BZS` | Reset the slower only. |
| `BZX` | Reset the xbeam only. |

### `C` parameters

| Parameter | Default | Meaning |
|---|---|---|
| `unlock_thresh` | 0.95 | Nominal acting threshold (`losing_meanThresh`). |
| `lost_thresh` | 0.25 | Lost threshold. Not affected by back-off. |
| `bump_thresh` | 0.05 | Bump step as a fraction of FWHM. |
| `relock_thresh` | 0.95 | Gain on the relock extrapolation. |
| `std_thresh` | — | Running-std trigger, as a multiple of `refStd`. |
| `slower_sign`, `xbeam_sign` | +1, −1 | `fb_sign`. Points at the gentle flank. |
| `scan_timeout_ms` | 500 | Acquisition watchdog. Raise if the ramp is slower than 2 Hz. |
| `slower_bias`, `xbeam_bias` | 1, 0 | Enable the fixed safe-side offset. |
| `slower_contbump`, `xbeam_contbump` | 0, 0 | Enable the continuous safe-side bump. |
| `bias_frac` | 0.25 | Fixed offset as a fraction of FWHM. |
| `instab_window_ms` | 60000 | Rolling window for counting instability events. |
| `instab_events` | 3 | Events in window that trigger a back-off. |
| `backoff_step` | 0.02 | Threshold drop per trigger. |
| `backoff_floor` | 0.70 | Hard minimum; latches a fault. |
| `backoff_quiet_ms` | 600000 | Quiet time before creeping back. |
| `backoff_creep` | 0.005 | Increase per quiet interval. |
| `holdoff_slope` | 0 | Samples per µs, negative. Set by `HC`, or restored by the host. |
| `holdoff_cal_step` | 10 | µs per calibration step. |
| `holdoff_cal_frac` | 0.5 | Calibrate until displacement reaches this × `SEARCH_WINDOW`. |
| `holdoff_actuate_frac` | 0.5 | Actuate above this × `SEARCH_WINDOW`. |
| `holdoff_max_step` | 20 | µs clamp on one correction. |
| `holdoff_interval_ms` | 60000 | Minimum time between corrections. |

---

## Output blocks

| Block | Emitted by |
|---|---|
| `[START] Initalization` … `[END] Stats` | `I`, `D` |
| `[START] Trace` | `R` |
| `[START] Clusters` … `[END] Clusters` | peak status |
| `[START] Spectral Purity Curve` … `[END] Spectral Purity Curve` | `S` |
| `[START] Holdoff Calibration` … `[END] Holdoff Calibration` | `HC` |
| `[START] Holdoff Status` … `[END] Holdoff Status` | `HF`, `HR`, `HS` |
| `[START] Backoff Status` … `[END] Backoff Status` | `BR`, and `C` on any bias parameter |

Line-oriented diagnostics: `[DEBUG]`, `[WARN]`, `[INFO]`, `[HOLDOFF]`.

---

## Not yet done

| Item | Note |
|---|---|
| 1.13 | Dead code: `initialPeaks`, `foundInitial`, unused `positions` parameter, `stop_xbeam`. |
| 1.9, 1.14, 1.15 | Host-side hang and safety bugs. See the host-side document. |
| 2.2 | Timer-triggered DMA acquisition. Would make the holdoff calibration unnecessary. |
| 2.4 | Explicit `LockState` enum. Mostly mechanical now that `serviceChannel` is unified. |
| 2.6 | `RunningBuffer` heap-allocates on every `getIterationMean` call. |
| 2.7 | The two lasers are discriminated by peak amplitude, which fails when one is losing lock. |
| 2.8–2.10 | Framed binary protocol, command ACKs, parameter persistence. |
| Part 5 | Simulator. The LOST branch and the xbeam probe have still never executed. |
