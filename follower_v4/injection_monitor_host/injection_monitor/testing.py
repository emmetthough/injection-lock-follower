"""A fake Arduino, so the host can be exercised without the lab.

Emulates both generations:

* ``FakeFirmware(version="v3")`` answers only the seven commands
  injection_follower_v3.ino recognises and stays silent on everything else --
  which is exactly what the capability probe has to cope with.
* ``FakeFirmware(version="v4")`` adds the holdoff and back-off blocks.

Byte-for-byte reproductions of the firmware's print statements, including the
misspellings (``[START] Initalization``), the mismatched terminator
(``[END] Peaks``), the missing ``END_tracking``, the trailing commas in the SPC
rows and the unterminated binary trace.
"""

from __future__ import annotations

import random
import struct
import threading
import time


class FakeFirmware:
    """Quacks like serial.Serial, for the subset SerialLink uses."""

    def __init__(self, version: str = "v4", n: int = 500,
                 drop_terminator: bool = False, short_trace: bool = False,
                 interleave_warnings: bool = False, fail_calibration: bool = False,
                 emit_debug: bool = False):
        self.version = version
        self.n = n
        self.drop_terminator = drop_terminator
        self.short_trace = short_trace
        self.interleave_warnings = interleave_warnings
        self.fail_calibration = fail_calibration
        self.emit_debug = emit_debug

        self.is_open = True
        self._out = bytearray()          # host -> device
        self._in = bytearray()           # device -> host
        self._lock = threading.Lock()
        self.received: list[str] = []
        self.timeout = 0.1

        # Mutable device state, so C commands are visibly applied.
        self.state = {
            "holdoff_slope": 0.0, "delayus": 200, "holdoff_enabled": 0,
            "cal_step": 10, "unlock_thresh": 0.95, "backoff_floor": 0.70,
            "slower_bias": 1, "xbeam_bias": 0,
            "slower_contbump": 0, "xbeam_contbump": 0,
        }

    # ---- serial.Serial surface ----

    def write(self, data: bytes) -> int:
        with self._lock:
            self._out.extend(data)
            while b"\n" in self._out:
                line, _, rest = bytes(self._out).partition(b"\n")
                self._out = bytearray(rest)
                self._dispatch(line.decode().strip())
        return len(data)

    def readline(self) -> bytes:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            with self._lock:
                idx = self._in.find(b"\n")
                if idx >= 0:
                    out = bytes(self._in[: idx + 1])
                    del self._in[: idx + 1]
                    return out
                if self._in:
                    # Binary payload with no newline: hand it over as-is.
                    out = bytes(self._in)
                    self._in.clear()
                    return out
            time.sleep(0.001)
        return b""

    def read(self, size: int) -> bytes:
        deadline = time.monotonic() + self.timeout
        buf = bytearray()
        while len(buf) < size and time.monotonic() < deadline:
            with self._lock:
                take = min(size - len(buf), len(self._in))
                if take:
                    buf.extend(self._in[:take])
                    del self._in[:take]
            if len(buf) < size:
                time.sleep(0.001)
        return bytes(buf)

    def reset_input_buffer(self):
        with self._lock:
            self._in.clear()

    def reset_output_buffer(self):
        with self._lock:
            self._out.clear()

    def close(self):
        self.is_open = False

    # ---- emission helpers (called with the lock held) ----

    def _p(self, text: str = "") -> None:
        self._in.extend((text + "\r\n").encode())

    def _raw(self, data: bytes) -> None:
        self._in.extend(data)

    def _maybe_warn(self, text: str) -> None:
        if self.interleave_warnings:
            self._p(f"[WARN] {text}")

    def _end(self, text: str) -> None:
        if not self.drop_terminator:
            self._p(text)

    # ---- command dispatch ----

    def _dispatch(self, cmd: str) -> None:
        self.received.append(cmd)
        v4 = self.version == "v4"

        if cmd == "I":
            self._send_init()
        elif cmd == "R":
            self._send_trace()
            self._send_tracking()
        elif cmd.startswith("C"):
            self._apply_c(cmd[1:], v4)
        elif cmd == "FB" or cmd == "TD":
            pass
        elif cmd in ("Z", "ZS", "ZX"):
            pass
        elif cmd.startswith("D"):
            self.state["delayus"] = int(cmd[1:] or 0)
            self._p(f"delayus set to: {self.state['delayus']}")
            self._send_init()
        elif v4 and cmd == "HR":
            self._send_holdoff_status()
        elif v4 and cmd == "BR":
            self._send_backoff_status()
        elif v4 and cmd == "HF":
            self.state["holdoff_enabled"] ^= 1
            if self.state["holdoff_slope"] == 0:
                self._p("[WARN] holdoff feedback enabled but uncalibrated")
            self._send_holdoff_status()
        elif v4 and cmd.startswith("HS"):
            self.state["delayus"] = int(cmd[2:] or 0)
            self._send_holdoff_status()
        elif v4 and cmd == "HC":
            self._send_calibration()
        elif v4 and cmd.startswith("BZ"):
            self._send_backoff_status()
        elif v4:
            self._p(f"[WARN] unknown command: {cmd}")
        # v3: unrecognised commands produce nothing at all.

    def _apply_c(self, args: str, v4: bool) -> None:
        var, _, val = args.partition(",")
        v3_names = {"unlock_thresh", "lost_thresh", "bump_thresh", "relock_thresh",
                    "std_thresh", "slower_sign", "xbeam_sign"}
        if var not in v3_names and not v4:
            return  # v3 falls off the end of parse_change_command, silently
        if var in self.state:
            self.state[var] = float(val)
        if v4 and var in ("slower_bias", "xbeam_bias", "slower_contbump",
                          "xbeam_contbump", "unlock_thresh", "backoff_floor"):
            self._send_backoff_status()   # unsolicited, per the spec

    # ---- blocks ----

    def _send_init(self) -> None:
        self._p("[START] Initalization")
        self._p("BEGIN_peaks")
        self._p("High 0: val=2412 pos=56")
        self._maybe_warn("scan trigger timeout")
        self._p("High 1: val=2390 pos=142")
        self._p("Low 0: val=1980 pos=301")
        self._p("END_peaks")
        self._end("[END] Peaks")
        if self.emit_debug:
            self._p("[DEBUG] Relock slower called! nattempts: 1")
            self._p("[DEBUG] Slower relock successful on iteration 1")
        self._p("[START] Stats")
        self._p("BEGIN_stats")
        self._p("meanHeight=2412.55 stdHeight=31.20")
        self._maybe_warn("relock: degenerate SPC fit")
        self._p("BEGIN_lowStats")
        self._p("meanHeight=1980.10 stdHeight=24.75")
        self._p("END_stats")
        self._end("[END] Stats")
        self._p("[START] Clusters")
        self._p("BEGIN_clusters")
        self._p("High cluster 0 meanPos = 123.45")
        self._p("Low cluster 0 meanPos = 301.20")
        self._p("END_clusters")
        self._end("[END] Clusters")

    def _send_trace(self) -> None:
        self._p("[START] Trace")
        n = self.n - 3 if self.short_trace else self.n
        self._raw(struct.pack(f"<{n}H", *(random.randint(0, 4095) for _ in range(n))))

    def _send_tracking(self) -> None:
        self._p("[START] peak_tracking")
        self._p("BEGIN_tracking")
        self._p("High 0 pos=12 val=2345 status=OK")
        self._p("Low 0 pos=310 val=1975 status=LOST")   # dropped by the old host
        self._end("[END] peak_tracking")

    def _send_holdoff_status(self) -> None:
        s = self.state
        self._p("[START] Holdoff Status")
        self._p(f"enabled,{int(s['holdoff_enabled'])}")
        self._p(f"slope,{s['holdoff_slope']:.4f}")
        self._p(f"delayus,{int(s['delayus'])}")
        self._p(f"cal_step,{int(s['cal_step'])}")
        self._p("deadband,12.50")
        self._p("error,nan" if s["holdoff_slope"] == 0 else "error,3.42")
        self._end("[END] Holdoff Status")

    def _send_backoff_status(self) -> None:
        s = self.state
        self._p("[START] Backoff Status")
        self._p(f"nominal,{s['unlock_thresh']:.3f}")
        self._p(f"floor,{s['backoff_floor']:.3f}")
        self._p(f"slower,0.910,4,0,{int(s['slower_bias'])},{int(s['slower_contbump'])}")
        self._p(f"xbeam,{s['unlock_thresh']:.3f},0,0,{int(s['xbeam_bias'])},{int(s['xbeam_contbump'])}")
        self._end("[END] Backoff Status")

    def _send_calibration(self) -> None:
        step, nsteps = int(self.state["cal_step"]), 4
        slope = 0.0 if self.fail_calibration else -0.0962
        self._p("[START] Holdoff Calibration")
        self._p(f"step_us,{step}")
        self._p(f"nsteps,{nsteps}")
        self._p("START Points")
        base = int(self.state["delayus"])
        for k in range(-nsteps, nsteps + 1):
            d = base + k * step
            self._p(f"{d},{(-0.0962) * d + 20.0:.3f}")
        self._maybe_warn("holdoff calibration: cluster went lost mid-sweep")
        self._p("END Points")
        self._p(f"slope,{slope:.4f}")
        if self.fail_calibration:
            self._p("[WARN] holdoff calibration: swept to the rail without displacement")
        else:
            self.state["holdoff_slope"] = slope
        self._end("[END] Holdoff Calibration")
