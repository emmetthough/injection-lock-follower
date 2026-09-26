# Injection lock follower — development guide

This project has three coupled pieces that all evolved together: Arduino firmware, a Python/
FastAPI host running on the Raspberry Pi next to the hardware, and a Dash dashboard for
monitoring and control. This guide lists each generation of each piece, what it added over the
last one, and which generations are meant to run together.

Folder layout:

```
firmware/   — Arduino sketches, oldest to newest
host/       — Raspberry Pi / FastAPI scripts, oldest to newest
dash/       — Dash dashboards, oldest to newest
host_acquisition_variant/       — an earlier, superseded acquisition-server design
dashboard_console_prototype/    — proofs-of-concept for one dashboard feature
```

Filenames are sortable in build order within each folder. Originals are unchanged elsewhere.

## Firmware generations

| File | Adds | Commands it understands |
|---|---|---|
| `firmware_00_plaintext_prototype.cpp` | Peak detection + clustering over a fixed-length scan; plaintext block markers (`BEGIN_RESULTS…END_RESULTS`, `Start_High_Peak_Stats…End_…`) | `D<us>` (set holdoff), `R` (re-init + report) |
| `firmware_01_plaintext_prototype_WIP_broken.cpp` | Continuous background tracking against the clusters found at init, `OK`/`LOST` status per peak | same as above — **has two `void loop()` definitions and won't compile as committed; delete the first one before flashing** |
| `firmware_02_bracket_protocol_no_feedback.cpp` | Switches to a bracket-tagged block protocol (`[START]/[END]` around `BEGIN_peaks`, `BEGIN_clusters`, `BEGIN_tracking`, `BEGIN_stats`) | `I` (init), `D<us>` (set holdoff + re-init), `R` (trace + peak status) |
| `firmware_03_closedloop_v3_initial.cpp` | Closed-loop DAC feedback: `bump_slower_up`/`bump_xbeam_up`, `relock_slower`/`relock_xbeam`, `recover_slower`/`recover_xbeam`; working Spectral Purity Curve (SPC) with a linear slope fit | + `S<start>,<stop>,<step>` (run SPC), `C<var>,<value>` (set tuning param), `F`, `ZS`/`ZX`/`Z` (zero outputs) |
| `firmware_04_closedloop_v3_digilock_AS_RECEIVED.cpp` | Gates every DAC move on an external Digilock-good signal; adds GPIO status/failure output pins; bounds recovery attempts instead of retrying forever; `FB` toggle for enabling/disabling feedback | + `FB` |

A further, unfilmed generation is described only in `firmware_changes_summary.md` and
`feedback_scheme_v4.html`: it unifies the duplicated slower/xbeam functions into one set of
functions operating on a `Channel` struct, and adds the holdoff drift servo, safe-side bias, and
instability back-off (new `[WARN]`/`[HOLDOFF]` output lines, and `HC`/`HF`/`HR`/`HS`/`BR`/`BZ`/
`BZS`/`BZX` commands). If that `.ino` turns up, it belongs here as
`firmware_05_channel_refactor.ino`.

## Host (Raspberry Pi / FastAPI) generations

| File | Adds |
|---|---|
| `host_00a_stream_reader.py` / `host_00b_live_plot.py` | Bare serial client for `firmware_00`/`01`'s continuous status stream; `host_00b` adds a live matplotlib trace + peak scatter plot |
| `host_01a_plaintext_client.py` / `host_01b_plaintext_client_fixed.py` | Full parser for `firmware_01`'s plaintext block protocol, including stats and clusters. `host_01a` has an off-by-one in its peak parser (`parts[3]`/`parts[4]` into a 4-token line) — use `host_01b`, which fixes it |
| `host_02_bracket_protocol_client.py` | Parser for `firmware_02`'s `[START]/[END]` bracket protocol |
| `host_03a_fastapi_v0.py` | First FastAPI wrapper: `/waveform`, `/peaks`, `/clusters`, `/tracking`, `/stats`, `/holdoff/{value}`, `/init`, `/SPC/{start}/{stop}/{step}`, `/C/{var}/{value}` |
| `host_03b_spc_debug_standalone.py` | Same core, no FastAPI — interactive command prompt + matplotlib plot of the SPC fit, for visually sanity-checking the slope before trusting it |
| `host_04_fastapi_threaded_gpio.py` | Thread-safe access to shared state (`data_lock`, `operation_in_progress`); a GPIO thread that reads Digilock lock/fail pins and polls a dashboard for feedback-enable state; `/status`, `/spc`, `/FB` endpoints |
| `host_05_fastapi_v2_draft.py` | Same features, physical `GPIO.BOARD` pin numbers and real dashboard IP instead of dev defaults; adds `/TD` |
| `host_06_fastapi_v2_CURRENT.py` | Passes `[DEBUG]` lines through to the console; resets GPIO outputs low on error; adds `/ZS`, `/ZX`, `/Z` as dedicated endpoints; de-duplicates lock-state pushes to the dashboard |

`host_side_changes.md` (already in this project) lists the fixes still needed to make
`host_06` reliable against the post-refactor firmware described above — start there for what's
left to do on the host side, in the order it suggests.

## Dash generations

| File | Pairs with | Adds |
|---|---|---|
| `dash_01_main_stats_clusters.py` | `host_03a` | Waveform, stats, and clusters panels; calls `/stats`, `/clusters`, `/waveform`, `/holdoff` |
| `dash_02_adds_spc_C_FB_status.py` | `host_04` | SPC run/plot panel, tuning-parameter (`/C`) controls, feedback toggle (`/FB`), status polling |
| `dash_03_adds_gpio_api_CURRENT.py` | `host_06` | Becomes a small Flask server itself, so the Pi's GPIO thread has something to talk to: `/api/feedback_status`, `/api/blue_lock_status`, `/api/override_status` |

### A superseded acquisition-server design

`host_acquisition_variant/` holds an earlier, separate attempt at the Pi-side server
(`acq_variant_a_raw_waveform_holdoff_only.py`, `acq_variant_b_adds_peaks.py`), paired with
`dash/dash_00_prototype_single_laser_ARCHIVE.py`. This branch was abandoned in favor of the
`host_0X` lineage above — **don't run it against current firmware.** It reads the trace with a
bare `if ser.in_waiting >= 2*N: raw = ser.read(2*N)`, with no check for a marker like
`[START] Trace` first. Against firmware that prints text lines around the trace (all five
firmware generations above do), that can silently misalign the read and hand back a corrupted
waveform with no error. `dash_00`'s single-laser client-side `scipy.signal.find_peaks` reflects
the same era: the Pi wasn't reporting peaks yet, so the dashboard found them itself.

### Console-log feature prototypes

`dashboard_console_prototype/` holds three small scripts that aren't acquisition or dashboard
code, but proofs-of-concept for one feature present in every `dash_0X` file: launching a script
and streaming its live output into a scrolling panel.

| File | What it proves out |
|---|---|
| `console_prototype_dummy_target.py` | A trivial script that prints once a second — something to launch |
| `console_prototype_stdout_streamer.py` | `subprocess.Popen` + a background thread + a `queue.Queue`, to capture that script's stdout without blocking the dashboard |
| `console_prototype_autoscroll_ui.py` | The clientside JS that keeps a log panel scrolled to the bottom unless the user has scrolled up |

Useful as a minimal repro if that feature ever breaks and you want to test the streaming
mechanism on its own, separate from the surrounding Dash layout code.

## Incremental bring-up procedure

Bring the system up in the same order it was built. Each stage isolates one new piece of
hardware or logic, so a failure points at exactly one place to look, and nothing with an active
feedback loop runs until the passive stages behind it check out.

1. **Trace + raw peak detection.** Flash `firmware_00` (or `firmware_01` with its duplicate
   `loop()` removed). Run `host_00b_live_plot.py` and confirm the scope trace and detected peaks
   look right on the bench. No feedback loop exists yet, so nothing can move the laser current —
   the safest possible first step on new hardware.
2. **Clustering + stats.** Run `host_01b_plaintext_client_fixed.py` or
   `host_02_bracket_protocol_client.py` against the matching firmware. Confirm cluster positions
   are stable across repeated `R`/`I` calls before trusting them to drive tracking.
3. **Open-loop SPC.** Flash `firmware_03`, run `host_03b_spc_debug_standalone.py` interactively —
   it plots the fitted slope directly, the fastest way to check `fb.m`/`x0`/`y0` before anything
   closes the loop around them. Feedback stays off.
4. **Closed loop, one channel at a time.** Flash `firmware_04`, run `host_04_fastapi_threaded_gpio.py`.
   Enable `slower` feedback alone first (leave `xbeam` disabled), confirm it survives a manual
   perturbation, then enable `xbeam`. Doing one channel at a time isolates which channel's sign or
   threshold constants are wrong if only one locks.
5. **Dashboard, read-only panels.** Add `dash_01_main_stats_clusters.py` once step 2 is behaving,
   to confirm the waveform/stats/clusters panels render correctly against real data.
6. **Dashboard, active controls.** Move to `dash_02_adds_spc_C_FB_status.py` alongside `host_04`
   once you're ready to drive `/SPC`, `/C`, and `/FB` from the UI instead of a script.
7. **Host hardening.** Move to `host_06_fastapi_v2_CURRENT.py` and `dash_03_adds_gpio_api_CURRENT.py`
   together. Deliberately trigger a channel failure on the bench and confirm the dashboard's
   fail indicator actually flips — the wire protocol hasn't changed since step 4, so this step
   should mostly confirm the GPIO/dashboard link rather than the feedback logic itself.
8. **Apply `host_side_changes.md`** in the order it suggests before moving to the refactored
   firmware: the `[WARN]` filter first, then the `read_until` deadline and interlock reordering,
   then the three new block parsers and endpoints, then persisting `holdoff_slope`, then queue
   bounding and async endpoints, then logging and configuration last.
9. **Bring up the refactored firmware last**, against the host from step 8, using
   `feedback_scheme_v4.html`'s worked examples as acceptance tests: force a mode hop and confirm
   the jump direction matches the table for the perturbed channel; run `HC` and confirm the
   reported slope sign matches "negative for slower, positive for xbeam"; check `BR` reports
   back-off events only after deliberately-induced instability, not spontaneously.

Keep every earlier-generation file around rather than deleting it — each is a clean fallback if
a later stage misbehaves and you need to prove the hardware itself is still fine.
