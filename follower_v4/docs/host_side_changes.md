# Host-side changes required — `injection_monitor_v2.py`

Everything below is needed to make the current firmware usable. Items are ordered by how much
they block you, not by size.

---

## 0. Blocking: `[WARN]` lines are discarded

`serial_read_loop` echoes only lines containing `[DEBUG]`:

```python
if "[DEBUG]" in line:
    print(line)
```

Everything else that isn't a `[START]` block is silently dropped. The firmware now emits
**nineteen distinct `[WARN]` messages**, plus `[INFO]` and `[HOLDOFF]`, and none of them reach
you. Three of the features added this session report their failures *exclusively* through
`[WARN]` — the holdoff calibration in particular is interactive and every one of its failure
modes is a warning.

```python
if any(tag in line for tag in ("[DEBUG]", "[WARN]", "[INFO]", "[HOLDOFF]")):
    print(line)
```

Better: route them to `logging` at the matching level, and push `[WARN]` to the dashboard so
they're visible without tailing a console.

Full list of warning sources:

| Source | Meaning |
|---|---|
| `scan trigger timeout` | Ramp stopped. Feedback halted, DAC holding. |
| `relock: degenerate SPC fit` | `m == 0` or `fwhm == 0`. Channel handed to recovery. |
| `recover: FWHM is zero` | No valid SPC fit; recovery cannot search. |
| `recover: swept to the DAC rail` | At limit. DAC reverted to `goodCode`. |
| `getSlope: no threshold crossing` | SPC peak sat at the edge of the sweep. |
| `holdoff calibration: …` (6 variants) | Calibration failed; slope not stored. |
| `holdoff feedback enabled but uncalibrated` | `HF` without a slope. |
| `<channel> instability detected` | Back-off triggered. |
| `<channel> unstable at the back-off floor` | Latched fault. Physical problem. |
| `unknown command` / `unknown … subcommand` | Dashboard sent something unrecognised. |
| `command exceeded buffer` | Line longer than 64 bytes. |

---

## 1. Blocking: `read_until()` spins forever

```python
def read_until(ser, target):
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line: continue      # timeout returns "" → loops forever
```

With `timeout=1`, a dropped or malformed terminator means this never returns, inside
`serial_read_loop`, with `operation_in_progress` set. Every API endpoint then blocks for its
30 s timeout and the waveform stops updating. It is indistinguishable from a hang.

This is now more likely, not less: the firmware emits more block types, and `HC` can take tens of
seconds, during which the host is waiting on a terminator that may never arrive if the
calibration aborts early.

```python
def read_until(ser, target, timeout=30.0, max_lines=5000):
    deadline = time.monotonic() + timeout
    lines, n = [], 0
    while time.monotonic() < deadline and n < max_lines:
        line = ser.readline().decode(errors="ignore").strip()
        n += 1
        if not line:
            continue
        if target in line:
            return lines
        lines.append(line)
    raise TimeoutError(f"never saw {target!r}")
```

Callers must handle `TimeoutError` and clear `operation_in_progress` in a `finally`.

---

## 2. Blocking: safety logic sits behind network calls

In `gpio_status_thread`, the fail-state response is correct but runs *after* two blocking
`requests` calls with 2 s timeouts, and is skipped entirely if either raises. If the Dash app is
slow or down, fail handling is delayed by up to 4 s per iteration or not run at all.

Move all interlock logic to the top of the loop, unconditional, before any network I/O. Push
dashboard updates onto a separate thread or a fire-and-forget queue.

Also: while the fail line is high, `"ZS\n"` is enqueued **every 500 ms forever** into an
unbounded queue. Latch the response instead.

---

## 3. Command parsing must not silently swallow errors

```python
val = int(re.search(r"val=(\d+)", line).group(1))
```

`re.search` returns `None` on any malformed line → `AttributeError` → caught by a broad
`except` → re-raised as `ValueError` → caught by another broad `except` that prints. A 5%
corruption rate would be invisible.

`interpret()` also falls off the end and returns `None` if no marker matches, so
`key, data = interpret(...)` raises `TypeError`.

And the stats parse indexes lines positionally (`lines[1]`, `lines[3]`), which breaks if a
`[WARN]` ever interleaves — now much more likely.

---

## 4. New: parsers for the new blocks

### `[START] Holdoff Calibration`

```
[START] Holdoff Calibration
step_us,<int>
nsteps,<int>
START Points
<delayus>,<mean_position_error>     ← repeated, 2*nsteps+1 lines
END Points
slope,<float>
[END] Holdoff Calibration
```

`slope,0` means the calibration failed — check the accompanying `[WARN]`. **Store the slope
persistently.** The firmware loses it on reset and has no flash storage, so on reconnect the host
should re-send it with `Choldoff_slope,<value>` rather than forcing a re-calibration.

The points are worth plotting: the fit residual tells you whether the sweep stayed linear, and
`−1/slope` is your actual ADC sample interval in µs.

### `[START] Holdoff Status`

```
[START] Holdoff Status
enabled,<0|1>
slope,<float>
delayus,<int>
cal_step,<int>
deadband,<float>          ← in samples
error,<float|nan>         ← most recent drained average, nan before the first drain
[END] Holdoff Status
```

### `[START] Backoff Status`

```
[START] Backoff Status
nominal,<float>
floor,<float>
slower,<losingThresh>,<eventCount>,<floorLatched>,<biasEnabled>,<contBumpEnabled>
xbeam,<losingThresh>,<eventCount>,<floorLatched>,<biasEnabled>,<contBumpEnabled>
[END] Backoff Status
```

Note this block is emitted **unsolicited** whenever a bias-related `C` parameter is set, not only
in response to `BR`. The reader must handle it arriving without having been requested.

### `[HOLDOFF]` line

```
[HOLDOFF] err=<float> samples, delayus <old> -> <new>
```

Emitted on every actuation. Log these — the sequence over a day is the drift record.

---

## 5. New: endpoints and controls

| Endpoint | Command | Notes |
|---|---|---|
| `POST /holdoff/calibrate` | `HC` | Long-running, tens of seconds. Must not block a threadpool worker. |
| `POST /holdoff/toggle` | `HF` | |
| `GET /holdoff/status` | `HR` | |
| `POST /holdoff/set/{us}` | `HS<us>` | **Use this, not `D`.** |
| `GET /backoff/status` | `BR` | |
| `POST /backoff/reset` | `BZ` | The "kill this threshold" button. |
| `POST /backoff/reset/{ch}` | `BZS` / `BZX` | |

### Important: stop using `D` programmatically

`D<us>` sets the holdoff **and calls `initialize_peak_vals_locations()`**, which runs 130 scans
and resets `refHeight`/`refStd`. Any automated holdoff adjustment through `D` would continuously
reset your setpoint. `HS` exists precisely to avoid this. Keep `D` bound to a manual
"set holdoff and re-init" control only.

### Dashboard additions worth having

- Holdoff: current value, calibration slope, deadband in samples, current position error, an
  enable toggle and a calibrate button.
- Back-off: per channel, the current acting threshold against nominal and floor, drawn as a bar
  so a backed-off channel is obvious at a glance; the event count; the floor-latched flag; and
  the bias / continuous-bump enables.
- A reset button per channel plus one for both.

---

## 6. The waveform path is backwards

```python
@app.get("/waveform")
def get_waveform():
    command_queue.put("R\n")          # ask for a new trace
    snap = snapshot_running_data()    # return the previous one
```

Every displayed trace is one poll stale, and `command_queue` is unbounded, so a stall queues a
burst of `R` commands that all hit the Arduino at once when it recovers.

This matters more now. The servo runs at the scan rate rather than the poll rate, so the trace in
the buffer is at most one scan old — but the queue behaviour is unchanged, and a blocking
recovery sweep (up to ~13 s) will accumulate a large backlog.

- Bound the queue (`queue.Queue(maxsize=16)`).
- Coalesce: never enqueue `R` if one is already pending.
- Better: have the firmware stream traces at a fixed rate when enabled, and push to the dashboard
  over a WebSocket.

---

## 7. Blocking endpoints on the threadpool

`wait_for_operation_clear(timeout=30)` polls inside a synchronous FastAPI endpoint, so it runs in
Starlette's threadpool (40 workers). A handful of stuck requests wedges the whole API, including
`/status`, which is what you'd reach for to diagnose it.

`HC` makes this worse: it is legitimately slow, so a calibration request will hold a worker for
its full duration.

Make the endpoints `async def` and `await asyncio.wait_for(event.wait(), timeout)` against an
`asyncio.Event`, or return a request ID and let the client poll.

---

## 8. Logging and retention

`print()` with no timestamps, levels or persistence. For a system whose purpose is surviving rare
failures, you cannot debug what you didn't record.

Minimum: `logging` with a rotating file handler and ISO timestamps.

Worth doing: log every state transition, DAC write, back-off event and holdoff correction to a
time-series store. The single most useful derived number is **relock events per hour, per
channel** — that is what tells you whether a lock is healthy, and plotting it against room
temperature or time of day is how you find the physical cause rather than tuning thresholds by
hand.

---

## 9. Configuration and lifecycle

- `N = 500` exists in both the firmware and the Python. A mismatch silently corrupts every trace.
  Put shared constants in one file that both read.
- `PORT`, `BAUD`, `DASH_URL` and seven pin numbers are module-level constants. Move to
  `pydantic-settings` with environment overrides.
- `@app.on_event("startup")` is deprecated; use `lifespan`.
- `GPIO.cleanup()` is never called.
- `ser` is opened at import time with no retry, so if the Arduino isn't enumerated at boot the
  service crashes and stays down.
- `serial_read_loop` catches `SerialException`, sleeps 1 s, then **returns** — the thread dies and
  is never restarted. A USB glitch permanently kills acquisition until someone restarts the
  service. Wrap in a supervisory loop with reconnect.

---

## Suggested order

1. `[WARN]` filter (item 0) — one line, unblocks everything else.
2. `read_until` deadline (item 1) and the interlock reordering (item 2).
3. Parsers for the three new blocks (item 4) and the new endpoints (item 5).
4. Persist and restore `holdoff_slope`.
5. Queue bounding (item 6) and async endpoints (item 7).
6. Logging (item 8), then configuration and lifecycle (item 9).
