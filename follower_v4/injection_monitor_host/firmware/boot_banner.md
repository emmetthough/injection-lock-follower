# Firmware patch: boot banner

Three lines, at the end of `setup()` in `injection_follower_v3.ino` (after
`pinModeSetup()` and any `SerialUSB.begin()`), before `loop()` runs:

```c
#define FW_VERSION "v4.0"

// At the end of setup():
SerialUSB.print("[INFO] boot fw=");
SerialUSB.print(FW_VERSION);
SerialUSB.print(" N=");
SerialUSB.println(N);
```

## Why the host wants this

Without it, reset detection is a heuristic. `link.py` infers a restart from
`holdoff_slope`: it is zero at boot, can only become non-zero through `HC` or a
host replay, and there is no flash to keep it in — so a stored non-zero slope
against a firmware-reported zero means the board restarted. That inference is
sound but narrow. It cannot fire at all on firmware without the holdoff servo,
and it cannot fire on a board that rebooted before it was ever calibrated.

With the banner, detection is exact and works at any moment, not just at
connect: `_on_boot_banner()` marks everything unconfirmed, clears the queue of
commands aimed at the old state, and replays. A mid-session brownout is
currently invisible to the host; with the banner it is a log line and an
automatic recovery.

## Why `N` in the banner

`N` exists independently in the firmware and in `config.trace_samples`, and a
mismatch silently corrupts every trace. The host now catches this after the
fact using the 12-bit invariant (every high byte of a valid sample is ≤ 0x0F,
so ASCII in a high-byte position means the payload ran short), and it reports
the true length — but that is diagnosis after a corrupted trace, once per poll.
Printing `N` at boot turns it into one warning at startup.

If you would rather not change the banner format, the host matches on the word
`boot` alone and ignores the rest, so `[INFO] boot` is sufficient.

## Compatibility

`[INFO]` is already in the host's tag table, so an unpatched host logs the line
and ignores it. A patched host talking to unpatched firmware falls back to the
`holdoff_slope` heuristic. Neither direction breaks.

## One caveat

The banner is printed at power-up, when the Pi may not have the port open — in
which case it is simply lost, and detection falls back to the heuristic. That
is the normal case for a flash-then-connect workflow and is not a problem; the
banner earns its place on the *unexpected* reset, which is exactly the case the
heuristic handles worst.
