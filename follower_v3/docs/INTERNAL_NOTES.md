# Internal notes — current state & open items

For the full history and how-to-use-each-file reference, see `DEVELOPMENT_GUIDE.md`. This file
is just the current picture and the running TODO list.

## What's actually in use right now

- **Firmware:** the post-refactor version described in `firmware_changes_summary.md` /
  `feedback_scheme_v4.html` (Channel struct, holdoff drift servo, safe-side bias, instability
  back-off). **The `.ino` itself isn't in this archive** — only its changelog and flowchart
  survived. If you still have it, add it as `firmware/firmware_05_channel_refactor.ino`; until
  then, those two docs are the only spec for what the host is supposed to be talking to.
- **Host:** `host/host_06_fastapi_v2_CURRENT.py` (identical to the `injection_monitor_v2.py`
  already in the project).
- **Dashboard:** `dash/dash_03_adds_gpio_api_CURRENT.py` (`dashboard_v2.py`).

## Open items, roughly in the order worth doing them

1. **The host doesn't understand the current firmware yet.** `host_06` still runs the
   gen-4/5 parser — no `[WARN]`, `[HOLDOFF]`, `[START] Holdoff Calibration/Status`, or
   `[START] Backoff Status` handling. This is the entire subject of `host_side_changes.md`;
   its suggested order is: `[WARN]` filter → `read_until` deadline + interlock reordering →
   the three new block parsers/endpoints → persist `holdoff_slope` → queue bounding + async
   endpoints → logging → configuration/lifecycle.
2. **`dashboard_v2.py` is missing the `/api/lock_status` route.** `host_06`'s
   `gpio_status_thread` POSTs slower/xbeam lock state to `{DASH_URL}/api/lock_status`
   (added in the `v2_OLD` → `v2` step), but the dashboard never defines that route. The
   `try/except` around the post means this fails silently — no crash, the push just goes
   nowhere. Add the route, or confirm on purpose that this data isn't needed and remove the
   push instead of leaving it as a silent no-op.
3. **Stale comment in `host_06`:** `DASH_URL = "..."  # Change if dashboard_v1 runs elsewhere`.
   The GPIO integration (`/api/blue_lock_status`, `/api/feedback_status`,
   `/api/override_status`) only exists in `dashboard_v2.py` — `dashboard_v1.py` would 404 on
   all of it. Harmless today since the right dashboard is already running, but worth fixing so
   it doesn't send a future you looking for the wrong file.
4. **Before calling the GPIO fail-safe path done:** it was built (`host_04`) before any
   dashboard implemented the routes it polls (those only appeared in `dashboard_v2.py`), so it
   was never actually exercised end-to-end at the time it was written. Worth deliberately
   triggering `slower_fail`/`xbeam_fail` on the bench and confirming the dashboard's indicator
   actually goes red before trusting it in an unattended run.

## Reference docs already in the project

- `host_side_changes.md` — the full, ordered punch list for item 1 above.
- `firmware_changes_summary.md` — changelog for the firmware generation not present as a file.
- `feedback_scheme_v4.html` — current control-flow reference and worked examples; use these as
  acceptance tests once the refactored firmware is actually running against `host_06`.
