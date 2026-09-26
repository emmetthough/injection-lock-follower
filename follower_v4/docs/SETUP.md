# injection_monitor — Setup Manual

Replaces `injection_monitor_v2.py`. This document covers installing it on the
Pi, wiring it to the Arduino, and confirming it's working before you connect
Dash to it. It assumes no prior familiarity with the package beyond what's in
`README.md`.

---

## 1. Prerequisites

- Raspberry Pi with the Arduino Due connected over USB, currently enumerating
  as `/dev/ttyACM0` (confirm with `ls /dev/ttyACM*`).
- Python 3.10 or newer (`python3 --version`).
- The service user has permission to open the serial port. If you see
  `PermissionError` opening `/dev/ttyACM0`, add the user to the `dialout`
  group:

  ```bash
  sudo usermod -aG dialout $USER
  ```

  then log out and back in (group membership doesn't apply to an existing
  session).

---

## 2. Unpack and install

```bash
cd /opt                                  # or wherever you keep services
sudo mkdir -p injection_monitor && sudo chown $USER injection_monitor
cd injection_monitor
unzip injection_monitor_host.zip
```

You should now have:

```
injection_monitor/
  injection_monitor/     <- the Python package
  shared/                <- constants.json, source of truth
  tools/                 <- gen_constants.py
  firmware/              <- constants.h, boot_banner.md, known_issues.md
  test/                  <- check.sh
  requirements.txt
  README.md
```

Create a virtual environment and install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

`pydantic-settings` is in `requirements.txt` but not strictly required — the
config module falls back to reading `IM_*` environment variables directly if
it isn't installed. Install it anyway; the fallback exists for portability,
not because it's preferred.

---

## 3. Run the check script

Before touching the Arduino, confirm the package itself is sound:

```bash
INO=/path/to/injection_follower_v3.ino ./test/check.sh
```

This:
1. Verifies `firmware/constants.h` and `injection_monitor/constants.py` are
   current relative to `shared/constants.json`, and (with `INO` set) that the
   real firmware's `#define` values still agree with all three.
2. Runs the full test suite (110 tests) against the built-in fake firmware —
   no hardware required.
3. Confirms the package imports cleanly with GPIO disabled.

If `INO` is omitted, step 1 skips the firmware comparison and only checks
internal consistency. Run it with `INO` set at least once after unpacking,
and again any time you flash new firmware.

Expected output ends with:
```
all checks passed
```

If a test fails here, stop — do not proceed to wiring up the Arduino until
this passes. Everything below assumes a clean run.

---

## 4. First manual run

With the Arduino connected and already flashed, do a manual foreground run
before setting up the service, so you can see startup logging directly:

```bash
source venv/bin/activate
export IM_STATE_DIR=/var/lib/injection_monitor
export IM_LOG_DIR=/var/log/injection_monitor
sudo mkdir -p "$IM_STATE_DIR" "$IM_LOG_DIR"
sudo chown $USER "$IM_STATE_DIR" "$IM_LOG_DIR"

uvicorn injection_monitor.api:app --host 0.0.0.0 --port 8000
```

**Important:** the board does not reset when the Pi opens the port. If the
Arduino is already running and locked, this command will not disturb it — no
`I` is sent on connect. If the board isn't running at all, flash it first (see
`firmware/boot_banner.md` if you're adding the boot banner), then start the
Python service.

Watch the startup log for:

```
INFO ... opening /dev/ttyACM0 at 250000 baud
INFO ... serial link up (connection #1)
INFO ... capabilities: ['backoff', 'core', 'holdoff', 'scan_timeout']
```

If capabilities come back as just `['core']`, the firmware on the board
predates the holdoff/back-off features (see §7). That's not necessarily wrong
— it just means the newer endpoints will return `501` until you flash v4
firmware.

In a second terminal, confirm the API answers:

```bash
curl -s localhost:8000/status | python3 -m json.tool
curl -s localhost:8000/capabilities | python3 -m json.tool
curl -s localhost:8000/state | python3 -m json.tool
```

`GET /state` is the endpoint the dashboard will eventually rehydrate from — if
it returns a full parameter table with no errors, the core path is working.

Stop the foreground run with Ctrl-C once you've confirmed this. Shutdown sends
`Z` (zero both channels) before exiting; give it a second to complete rather
than force-killing it (`SIGKILL` skips that cleanup).

---

## 5. Configuration

Everything is set through `IM_`-prefixed environment variables. Defaults live
in `injection_monitor/config.py`; the ones worth reviewing before production:

| Variable | Default | Notes |
|---|---|---|
| `IM_PORT` | `/dev/ttyACM0` | Change if the Due enumerates differently. |
| `IM_STATE_DIR` | `/var/lib/injection_monitor` | Holds `parameters.json` (the persisted parameter store) and `events.jsonl`. Must be writable. |
| `IM_LOG_DIR` | `/var/log/injection_monitor` | Rotating log file. Missing/unwritable disables file logging with a warning rather than refusing to start. |
| `IM_DASH_URL` | `http://10.155.94.105:8050` | Where the interlock thread posts lock/fail status. Update for your network. |
| `IM_GPIO_ENABLED` | `true` | Set `0`/`false` to run without `RPi.GPIO` — useful for testing on a non-Pi machine. |
| `IM_REPLAY_POLICY` | `boot` | When to push stored parameters to the firmware. `boot` (only on detected restart) is the safe default; see §6. |
| `IM_SEND_INIT_ON_CONNECT` | `false` | Leave this off. Turning it on re-initializes the lock (resets `refHeight`/`refStd`) every time the service starts. |

Set these in whatever your process supervisor uses for environment — a
systemd `Environment=` line (see §8), an `.env` file loaded by your shell
profile, or exported before running `uvicorn` directly.

---

## 6. Understanding parameter replay (read this once)

The parameter store (`/var/lib/injection_monitor/parameters.json`) is the
single source of truth for every tunable — it survives a service restart and
a Dash page refresh. What it does **not** always do is push those values back
to the Arduino, and that's deliberate:

- The Due keeps running when the Pi's service restarts. If the board has been
  locked and running for a week, replaying 25 `C` commands into it on every
  service bounce would be actively harmful — at minimum a noisy log, at worst
  briefly odd behavior mid-write.
- So with the default `IM_REPLAY_POLICY=boot`, the host only replays when it
  has evidence the *board* restarted — either a boot banner (if you've added
  one, see `firmware/boot_banner.md`) or, for the holdoff calibration
  specifically, noticing the firmware reports slope `0` while the host has a
  non-zero value stored.

If you ever need to force it — say, you flashed new firmware and want to push
every stored parameter immediately — call:

```bash
curl -X POST localhost:8000/params/sync
```

This is also the answer if `/state` shows more than a couple of parameters
stuck `"confirmed": false` for longer than you'd expect.

---

## 7. Firmware version detection

`GET /capabilities` tells you what the connected firmware supports:

```json
{
  "holdoff": true,
  "backoff": true,
  "legacy_firmware": false,
  "tags": ["backoff", "core", "holdoff", "scan_timeout"]
}
```

Detection works by sending `HR` and `BR` at connect and seeing whether a
status block comes back within `IM_CAPABILITY_PROBE_TIMEOUT` (default 3s).
Pre-v4 firmware silently ignores unrecognized commands, so a timeout is
correctly read as "not supported," not an error.

Endpoints gated on a missing capability return `501` with a body naming which
capability is missing:

```json
{"error": "firmware does not support: holdoff", "capability": "holdoff"}
```

If you see this immediately after startup rather than a moment later, it may
just mean the probe hasn't finished yet — the service waits up to 6 seconds
for it during startup, but a slow-to-respond board could still race a very
early request. Retry once.

---

## 8. Running as a systemd service

Create `/etc/systemd/system/injection-monitor.service`:

```ini
[Unit]
Description=Injection lock follower host monitor
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/opt/injection_monitor
Environment=IM_STATE_DIR=/var/lib/injection_monitor
Environment=IM_LOG_DIR=/var/log/injection_monitor
Environment=IM_DASH_URL=http://10.155.94.105:8050
ExecStart=/opt/injection_monitor/venv/bin/uvicorn injection_monitor.api:app --host 0.0.0.0 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Adjust `User` and paths to match your install. Then:

```bash
sudo mkdir -p /var/lib/injection_monitor /var/log/injection_monitor
sudo chown pi:pi /var/lib/injection_monitor /var/log/injection_monitor

sudo systemctl daemon-reload
sudo systemctl enable --now injection-monitor
sudo systemctl status injection-monitor
```

Logs are in `/var/log/injection_monitor/monitor.log` (rotated, 20MB × 10
files by default) and also visible via `journalctl -u injection-monitor -f`.

`Restart=on-failure` matters less than it did with the old script — the
supervised reconnect loop inside the service already handles a dropped USB
connection without the process dying — but it's a reasonable backstop for
anything that does crash the process outright.

---

## 9. Smoke test checklist

Once the service is running, work through this in order:

1. `curl localhost:8000/status` — `link.connected` should be `true`.
2. `curl localhost:8000/capabilities` — matches the firmware you flashed.
3. `curl -X POST localhost:8000/init` then, a few seconds later,
   `curl localhost:8000/stats` — should return real height/std numbers, not
   empty.
4. `curl localhost:8000/waveform` — `scan_data` should be 500 numbers (or
   whatever `N` is), `age_s` should be small.
5. If on v4 firmware: `curl -X POST localhost:8000/holdoff/calibrate`, wait
   ~30s, then `curl localhost:8000/operations/<id from the previous response>`
   — status should reach `"done"` with a `result.slope` that's negative.
6. `curl localhost:8000/log/recent` — check for anything under `sticky` that
   shouldn't be there (a latched fault from before you started, for
   instance).
7. Trigger a slower-channel fail line manually if you can, and confirm
   `curl localhost:8000/status` shows `slower_fail_latched: true` and the
   dashboard notification queue (`interlock.dashboard`) isn't backing up.

If all seven pass, the host side is ready for the Dash page to be pointed at
it.

---

## 10. Troubleshooting

**Service won't start, `PermissionError` on the serial port.**
See §1 — `dialout` group membership.

**`capabilities` stuck at `{"probed": false}` indefinitely.**
The Arduino likely isn't answering at all — check `IM_PORT` matches
`ls /dev/ttyACM*`, and that nothing else (an Arduino IDE serial monitor, a
second instance of this service) is holding the port open.

**`/status` shows a non-zero `parsing.failure_rate`.**
New — the old script had no visibility into this at all. Check
`parsing.failed` for which block type is failing, and check `/log/recent` for
`[WARN]` lines around the same time; a `command exceeded buffer` warning
often means something sent a malformed or oversized command.

**Trace looks corrupted / `parsing.truncated` incrementing.**
Almost always an `N` mismatch between firmware and host. Run
`python3 tools/gen_constants.py --check --scan path/to/injection_follower_v3.ino`
— if it reports a mismatch, the firmware's `#define N` and
`shared/constants.json` disagree. Fix the firmware or the JSON and
regenerate.

**A parameter never shows `"confirmed": true`.**
Check `"confirmable"` in the same `/state` entry first — 16 of the 25
parameters are never echoed back by any firmware status block, so those can
never be confirmed by design; that's expected, not a fault.

**Holdoff slope resets to 0 after a Pi reboot but the board never rebooted.**
This shouldn't happen with `IM_REPLAY_POLICY=boot` — if it does, check
`/status` → `link.firmware_resets_seen`; if it's incrementing when the board
is provably still running, the reset heuristic is false-triggering, which
would mean `holdoff_slope` genuinely reported `0` from the firmware for some
other reason (a fresh `HC` failure, for instance) — check `/log/recent` for
context.

**Need to get back to a known-good state.**
`POST /params/reset` restores every parameter to the firmware's compiled-in
default and pushes it. Use this if the stored parameter file has drifted into
a state you don't trust; it's the equivalent of deleting
`/var/lib/injection_monitor/parameters.json` except it also replays
immediately.
