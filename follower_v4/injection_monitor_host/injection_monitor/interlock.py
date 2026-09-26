"""GPIO interlock thread.

Two defects in the original are fixed here, and they are the two that matter
most for safety.

**Safety logic sat behind network calls.**  The fail-state response ran *after*
two blocking ``requests`` calls with 2 s timeouts, and was skipped entirely if
either raised.  A slow or down dashboard delayed the interlock by up to 4 s per
iteration, or prevented it running at all.  Here the pins are read and acted on
at the top of the loop, unconditionally, before any network I/O -- and the
dashboard updates are handed to a separate thread with a bounded queue that
drops rather than blocks.

**The fail response was not latched.**  While the fail line was high, ``ZS`` was
enqueued every 500 ms forever into an unbounded queue.  Now it fires once on the
rising edge and re-arms when the line clears.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .config import Settings

log = logging.getLogger(__name__)


class NullGpio:
    """Stand-in so the service imports and runs off-Pi."""

    BOARD = OUT = IN = 0
    HIGH, LOW = 1, 0

    def setmode(self, *a): pass
    def setwarnings(self, *a): pass
    def setup(self, *a, **kw): pass
    def output(self, *a): pass
    def input(self, *a): return 0
    def cleanup(self): pass


def _get_gpio(enabled: bool):
    if not enabled:
        return NullGpio(), False
    try:
        import RPi.GPIO as GPIO
        return GPIO, True
    except (ImportError, RuntimeError) as e:
        log.warning("RPi.GPIO unavailable (%s); interlock runs in null mode. "
                    "Lock and fail lines will read low and no outputs will be "
                    "driven.", e)
        return NullGpio(), False


class DashNotifier:
    """Fire-and-forget dashboard updates on their own thread.

    Bounded and drop-on-full: a dashboard that stops responding must not be
    able to slow the interlock loop or grow memory without limit.
    """

    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self._q: queue.Queue = queue.Queue(maxsize=cfg.dash_queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped = 0
        self.failures = 0
        self._session = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dash-notify",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def post(self, path: str, payload: dict) -> None:
        try:
            self._q.put_nowait(("POST", path, payload))
        except queue.Full:
            self.dropped += 1

    def get(self, path: str, callback) -> None:
        try:
            self._q.put_nowait(("GET", path, callback))
        except queue.Full:
            self.dropped += 1

    def _run(self) -> None:
        try:
            import requests
            self._session = requests.Session()
        except ImportError:
            log.warning("requests unavailable; dashboard notifications disabled")
            return
        while not self._stop.is_set():
            try:
                verb, path, arg = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            url = f"{self.cfg.dash_url}{path}"
            try:
                if verb == "POST":
                    self._session.post(url, json=arg, timeout=self.cfg.dash_timeout)
                else:
                    r = self._session.get(url, timeout=self.cfg.dash_timeout)
                    if r.ok:
                        arg(r.json())
            except Exception as e:
                self.failures += 1
                log.debug("dashboard %s %s failed: %s", verb, path, e)

    def stats(self) -> dict:
        return {"queued": self._q.qsize(), "dropped": self.dropped,
                "failures": self.failures}


class Interlock:
    def __init__(self, cfg: Settings, link):
        self.cfg = cfg
        self.link = link
        self.gpio, self.real = _get_gpio(cfg.gpio_enabled)
        self.notifier = DashNotifier(cfg)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        self._state: dict[str, Any] = {
            "slower_lock": None, "xbeam_lock": None,
            "slower_fail": False, "xbeam_fail": False,
            "slower_fail_latched": False, "xbeam_fail_latched": False,
            "blue_locked": None, "last_read": None, "iterations": 0,
        }
        self._lock = threading.Lock()
        self._fb_wanted = {"slower": False, "xbeam": False}

    # ---- lifecycle ----

    def start(self) -> None:
        p = self.cfg.pins
        self.gpio.setmode(self.gpio.BOARD)
        self.gpio.setwarnings(False)
        for pin in (p.blue_lock_status, p.slower_feedback_en, p.xbeam_feedback_en):
            self.gpio.setup(pin, self.gpio.OUT)
        for pin in (p.slower_lock_state, p.slower_fail_state,
                    p.xbeam_lock_state, p.xbeam_fail_state):
            self.gpio.setup(pin, self.gpio.IN)
        self.notifier.start()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="interlock",
                                        daemon=True)
        self._thread.start()
        log.info("interlock started (%s mode)", "GPIO" if self.real else "null")

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self.notifier.stop()
        try:
            p = self.cfg.pins
            for pin in (p.blue_lock_status, p.slower_feedback_en,
                        p.xbeam_feedback_en):
                self.gpio.output(pin, self.gpio.LOW)
            # Never called in the original, so pins kept their last state and a
            # restart hit "channel already in use" warnings.
            self.gpio.cleanup()
        except Exception:
            log.exception("GPIO cleanup failed")
        log.info("interlock stopped")

    # ---- loop ----

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._iterate()
            except Exception:
                log.exception("interlock iteration failed; failing safe")
                self._fail_safe()
            self._stop.wait(self.cfg.gpio_poll_interval)

    def _iterate(self) -> None:
        p, g = self.cfg.pins, self.gpio

        # --- 1. Read inputs and act. No network I/O above this line. ---
        slower_fail = g.input(p.slower_fail_state) == g.HIGH
        xbeam_fail = g.input(p.xbeam_fail_state) == g.HIGH
        slower_lock = bool(g.input(p.slower_lock_state))
        xbeam_lock = bool(g.input(p.xbeam_lock_state))

        with self._lock:
            prev_sf = self._state["slower_fail"]
            prev_xf = self._state["xbeam_fail"]
            prev_lock = (self._state["slower_lock"], self._state["xbeam_lock"])
            self._state.update(
                slower_fail=slower_fail, xbeam_fail=xbeam_fail,
                slower_lock=slower_lock, xbeam_lock=xbeam_lock,
                last_read=time.time(),
                iterations=self._state["iterations"] + 1,
            )

        for ch, failing, prev, en_pin, cmd in (
            ("slower", slower_fail, prev_sf, p.slower_feedback_en, "ZS"),
            ("xbeam", xbeam_fail, prev_xf, p.xbeam_feedback_en, "ZX"),
        ):
            if failing:
                g.output(en_pin, g.LOW)         # drop the enable every pass
                if not prev:                    # but command only on the edge
                    log.error("%s fail line asserted; zeroing DAC", ch)
                    with self._lock:
                        self._state[f"{ch}_fail_latched"] = True
                    self.link.send(cmd)
                    self.notifier.post("/api/override_status",
                                       {"channel": ch, "status": "fail"})
            elif prev:
                log.warning("%s fail line cleared", ch)
                self.notifier.post("/api/override_status",
                                   {"channel": ch, "status": "clear"})

        # --- 2. Everything below is best-effort reporting. ---
        if (slower_lock, xbeam_lock) != prev_lock:
            self.notifier.post("/api/lock_status",
                               {"slower": slower_lock, "xbeam": xbeam_lock})

        self.notifier.get("/api/blue_lock_status", self._apply_blue)
        self.notifier.get("/api/feedback_status", self._apply_feedback)

        # Apply the last known feedback intent, honouring any latched fail.
        for ch, pin, failing in (("slower", p.slower_feedback_en, slower_fail),
                                 ("xbeam", p.xbeam_feedback_en, xbeam_fail)):
            if not failing:
                g.output(pin, g.HIGH if self._fb_wanted[ch] else g.LOW)

    def _apply_blue(self, payload: dict) -> None:
        locked = bool(payload.get("locked", False))
        with self._lock:
            self._state["blue_locked"] = locked
        self.gpio.output(self.cfg.pins.blue_lock_status,
                         self.gpio.HIGH if locked else self.gpio.LOW)

    def _apply_feedback(self, payload: dict) -> None:
        self._fb_wanted["slower"] = bool(payload.get("slower", False))
        self._fb_wanted["xbeam"] = bool(payload.get("xbeam", False))

    def _fail_safe(self) -> None:
        p, g = self.cfg.pins, self.gpio
        for pin in (p.blue_lock_status, p.slower_feedback_en, p.xbeam_feedback_en):
            try:
                g.output(pin, g.LOW)
            except Exception:
                pass

    # ---- reporting ----

    def clear_latched(self, channel: str | None = None) -> None:
        with self._lock:
            for ch in ("slower", "xbeam"):
                if channel in (None, ch):
                    self._state[f"{ch}_fail_latched"] = False

    def status(self) -> dict:
        with self._lock:
            s = dict(self._state)
        s["gpio_mode"] = "real" if self.real else "null"
        s["feedback_requested"] = dict(self._fb_wanted)
        s["dashboard"] = self.notifier.stats()
        return s
