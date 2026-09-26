# injection_monitor

Host-side monitor for the injection lock follower. Replaces
`injection_monitor_v2.py`.

## Running

    uvicorn injection_monitor.api:app --host 0.0.0.0 --port 8000

or `python -m injection_monitor`. Configuration is environment-driven with an
`IM_` prefix, e.g. `IM_PORT=/dev/ttyACM1`, `IM_GPIO_ENABLED=0`,
`IM_REPLAY_POLICY=connect`.

## For the dashboard

`GET /state` returns everything needed to rehydrate the UI in one request:
every parameter with its value, default, provenance and whether this firmware
supports it, plus capabilities, link health, holdoff and back-off status and
sticky faults. A page refresh reads from here rather than starting blank.

`GET /capabilities` says which controls to enable. Against pre-v4 firmware the
holdoff and back-off endpoints return 501 rather than silently queueing a
command the Arduino will discard.

`POST /params/{name}` returns immediately with `pending: true`. Only 9 of the
25 parameters are ever echoed back by the firmware; `confirmable` says which
will eventually turn solid.

## Firmware compatibility

Capabilities are probed at connect by sending `HR` and `BR` and seeing whether
a status block comes back. Pre-v4 firmware answers nothing, which is treated
as "not supported" rather than an error.

The board does not reset when the Pi opens the port, so on connect the
firmware may already be running with state the host knows nothing about. The
host therefore does **not** send `I` on connect, and replays stored parameters
only on evidence of a firmware restart (`IM_REPLAY_POLICY`, default `boot`).
Applying `firmware/boot_banner.md` makes that detection exact.

## Shared constants

`shared/constants.json` is the source of truth for values that must agree with
the firmware, `N` above all. Regenerate with:

    python3 tools/gen_constants.py
    python3 tools/gen_constants.py --check --scan path/to/injection_follower_v3.ino

Never edit `firmware/constants.h` or `injection_monitor/constants.py`.

## Tests

    ./test/check.sh
    INO=../injection_follower_v3.ino ./test/check.sh   # also verify constants

Everything runs against `injection_monitor.testing.FakeFirmware`, which
emulates both firmware generations byte-for-byte. No hardware needed.
