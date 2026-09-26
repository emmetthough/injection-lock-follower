# Known firmware issues affecting the host

## `recover_xbeam()` prints to `Serial`, not `SerialUSB`

`injection_follower_v3.ino`, line 1083:

```c
bool recover_xbeam() {
  Serial.println("Recover xbeam called!");
```

Every other debug line in the file goes to `SerialUSB` — the port the host is
actually connected to. This one line goes to the Due's other UART instead, so
**the host cannot see it at all**, with or without debug mode on, regardless
of anything on the host side.

Practical effect: `/metrics`'s `recover_called` count is slower-channel-only.
The xbeam recovery *runs* — `xbeam_nrecoveryAttempts` still increments, the
sweep still executes, `[WARN] xbeam instability detected` and the eventual
`[WARN] xbeam unstable at the back-off floor` still arrive normally, since
those are separate, correctly-routed lines. Only this one entry-point marker
is invisible. The undercount is silent: nothing errors, the number is just
low.

**Fix:** change `Serial.println` to `SerialUSB.println` on that line, and
while there, add the same `if (debug) {...}` guard and `[DEBUG]` tag every
other call site in this file uses, for consistency with `recover_slower()`
two functions up.

## Untagged event lines

Not a bug, but worth noting: several lines that describe genuine control-path
events carry no `[DEBUG]` tag and are not guarded by `if (debug)`, so they
print unconditionally regardless of the debug toggle:

- `"Recovery failed."` (both channels)
- `"Max relock attempts reached!"` / `"Max recovery attempts reached! Exiting."`

The host's event log (`state.py: EVENT_PATTERNS`) matches these as untagged
chatter rather than through the `[DEBUG]` path, so they are still counted —
but they will also print during normal operation with debug off, which is
harmless but slightly noisy on a plain terminal.
