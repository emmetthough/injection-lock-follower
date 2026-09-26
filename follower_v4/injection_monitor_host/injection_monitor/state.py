"""Shared in-memory state, the warning ring and the event log.

The event log is the part that matters long-term.  The host document is right
that the single most useful derived number is **relock events per hour, per
channel** -- it is what distinguishes a healthy lock from one that is being held
together by the servo, and plotting it against time of day is how you find a
physical cause instead of tuning thresholds by hand.  Nothing currently records
it, so it is written to JSONL here and summarised by ``metrics()``.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: [DEBUG] lines that mark a control-path event worth counting, transcribed
#: verbatim from injection_follower_v3.ino's SerialUSB.print calls (not
#: guessed -- the previous version of this table used invented phrasing that
#: never matched anything, so /metrics silently reported zero events forever).
#:
#: Two firmware quirks this has to work around:
#:   * recover_xbeam()'s "Recover xbeam called!" (line 1083) is written to
#:     Serial, not SerialUSB -- a different UART the host is not connected to.
#:     It is invisible here regardless of pattern; recovery-start counts are
#:     therefore slower-only until that line is moved to SerialUSB. See
#:     firmware/known_issues.md.
#:   * "Recovery failed." (both channels) and "Max relock/recovery attempts
#:     reached!" are printed without the [DEBUG] tag, so they arrive as
#:     untagged chatter rather than through classify_line(). They are matched
#:     here as plain substrings for that reason.
EVENT_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("bump_called", re.compile(r"^bump (?P<channel>slower|xbeam) up called!$")),
    ("bump_success", re.compile(r"^bump (?P<channel>slower|xbeam) up successful")),
    ("bump_to_relock", re.compile(
        r"^(Negative slope detected\. Passing to relock|"
        r"bump (?P<channel>slower|xbeam) up resulted in LOST state)"
    )),
    ("relock_called", re.compile(r"^Relock (?P<channel>slower|xbeam) called!")),
    ("relock_success", re.compile(
        r"^(Slower relock successful|Relock xbeam successful)", re.I
    )),
    ("relock_failed", re.compile(
        r"^(Slower relock failed|Xbeam relock failed)", re.I
    )),
    # recover_slower is correctly tagged and on SerialUSB; recover_xbeam's
    # equivalent line is not visible to the host at all (see docstring).
    ("recover_called", re.compile(r"^Recover slower called!$")),
    ("recover_success", re.compile(r"^Peak found during recovery\. Breaking\.$")),
    # Untagged in the firmware on at least one path; matched without anchoring
    # to a channel, so channel is None and the metric is a combined count.
    ("recovery_failed", re.compile(r"Recovery failed\.$")),
    ("max_attempts", re.compile(r"^Max (relock|recovery) attempts reached!")),
    ("losing", re.compile(r"^Losing (?P<channel>slower|xbeam) detected$")),
    ("lost", re.compile(r"^Lost (?P<channel>slower|xbeam) detected$")),
    ("bump_fallback", re.compile(
        r"^(?P<channel>Slower|Xbeam) bump 1 failed\. Bumping other way\.$"
    )),
    ("channel_fail", re.compile(
        r"^(?P<channel>Slower|Xbeam) bump 2 failed\. Exiting\.$"
    )),
)


def jsonable(obj: Any) -> Any:
    """Recursively strip NaN/Inf, which json.dumps emits as bare tokens.

    ``NaN`` is not valid JSON and the browser's JSON.parse() rejects it, so one
    un-sanitised value anywhere in a response fails the whole Dash fetch rather
    than just that field.  The firmware emits ``error,nan`` in Holdoff Status
    before the first drain, so this is the normal case on a fresh boot, not an
    edge case.
    """
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


class RunningData:
    """Latest parsed block of each kind, under one lock."""

    def __init__(self, n: int):
        self._lock = threading.Lock()
        self._d: dict[str, Any] = {
            "scan": [0] * n,
            "peaks": None,
            "clusters": None,
            "tracking": None,
            "stats": None,
            "spectral purity curve": None,
            "holdoff_status": None,
            "holdoff_calibration": None,
            "backoff_status": None,
        }
        self._updated: dict[str, float] = {}

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._d[key] = value
            self._updated[key] = time.time()

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            v = self._d.get(key, default)
            return jsonable(v)

    def age(self, key: str) -> float | None:
        with self._lock:
            t = self._updated.get(key)
        return None if t is None else time.time() - t

    def snapshot(self) -> dict:
        with self._lock:
            return jsonable(dict(self._d))

    def ages(self) -> dict:
        now = time.time()
        with self._lock:
            return {k: round(now - t, 2) for k, t in self._updated.items()}


class WarningRing:
    """Bounded history of diagnostic lines, plus sticky faults.

    Sticky entries (``unstable at the back-off floor``, ``swept to the DAC
    rail``) describe a latched physical problem and stay until acknowledged --
    otherwise the one message that matters scrolls past at 1 Hz behind debug
    chatter.
    """

    def __init__(self, size: int = 200):
        self._lock = threading.Lock()
        self._items: deque[dict] = deque(maxlen=size)
        self._sticky: dict[str, dict] = {}
        self._counts: dict[str, int] = {}

    def add(self, tag: str, level: str, text: str, sticky: bool = False) -> None:
        entry = {"t": time.time(), "tag": tag, "level": level, "text": text}
        with self._lock:
            self._items.append(entry)
            self._counts[level] = self._counts.get(level, 0) + 1
            if sticky:
                prev = self._sticky.get(text)
                self._sticky[text] = {
                    **entry,
                    "count": (prev or {}).get("count", 0) + 1,
                    "first": (prev or entry)["t"],
                    "acknowledged": False,
                }

    def recent(self, limit: int = 50, level: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._items)
        if level:
            items = [i for i in items if i["level"] == level]
        return items[-limit:]

    def sticky(self, include_acknowledged: bool = False) -> list[dict]:
        with self._lock:
            return [
                v for v in self._sticky.values()
                if include_acknowledged or not v["acknowledged"]
            ]

    def acknowledge(self, text: str | None = None) -> int:
        with self._lock:
            targets = ([self._sticky[text]] if text in self._sticky
                       else list(self._sticky.values()) if text is None else [])
            for t in targets:
                t["acknowledged"] = True
            return len(targets)

    def counts(self) -> dict:
        with self._lock:
            return dict(self._counts)


class EventLog:
    """Append-only JSONL of control events, with an in-memory window.

    Writes are best-effort: a full or read-only disk must degrade to "no
    history" rather than taking down acquisition.
    """

    def __init__(self, path: Path, window: int = 5000):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._recent: deque[dict] = deque(maxlen=window)
        self._write_failed = False

    def record(self, kind: str, data: dict | None = None) -> None:
        entry = {"t": time.time(), "kind": kind, **(data or {})}
        with self._lock:
            self._recent.append(entry)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as fh:
                fh.write(json.dumps(jsonable(entry)) + "\n")
            self._write_failed = False
        except OSError as e:
            if not self._write_failed:
                log.error("event log unwritable (%s); keeping memory only", e)
                self._write_failed = True

    def note_debug_line(self, text: str) -> None:
        """Extract countable control events from firmware debug output.

        Called for both [DEBUG]-tagged text (via classify_line) and untagged
        chatter -- several of the firmware's own event lines ("Recovery
        failed.", "Max relock attempts reached!") carry no tag at all, so
        restricting this to tagged lines would silently miss them.
        """
        for kind, pattern in EVENT_PATTERNS:
            m = pattern.search(text)
            if m:
                ch = (m.groupdict().get("channel") if m.groupdict() else None)
                self.record(kind, {"channel": ch.lower() if ch else None})
                return

    def recent(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        with self._lock:
            items = list(self._recent)
        if kind:
            items = [i for i in items if i["kind"] == kind]
        return items[-limit:]

    def metrics(self, hours: float = 24.0) -> dict:
        """Rate summary.  Relocks per hour per channel is the headline number."""
        cutoff = time.time() - hours * 3600
        with self._lock:
            items = [i for i in self._recent if i["t"] >= cutoff]
        if not items:
            return {
                "window_hours": hours, "observed_span_hours": 0.0,
                "n_events": 0, "rates_per_hour": {},
                "note": (
                    "Rates are over the in-memory window only. For history "
                    "beyond that, read the JSONL file."
                ),
            }

        span = max((items[-1]["t"] - items[0]["t"]) / 3600.0, 1 / 60.0)
        rates: dict[str, dict[str, float]] = {}
        for i in items:
            kind, ch = i["kind"], i.get("channel") or "both"
            rates.setdefault(kind, {}).setdefault(ch, 0)
            rates[kind][ch] += 1
        for kind, by_ch in rates.items():
            rates[kind] = {ch: round(n / span, 3) for ch, n in by_ch.items()}

        return {
            "window_hours": hours,
            "observed_span_hours": round(span, 3),
            "n_events": len(items),
            "rates_per_hour": rates,
            "note": (
                "Rates are over the in-memory window only. For history beyond "
                "that, read the JSONL file."
            ),
        }
