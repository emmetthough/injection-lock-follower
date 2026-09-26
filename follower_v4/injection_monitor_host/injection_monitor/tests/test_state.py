"""State layer tests: warning ring, event log, metrics.

The event patterns are matched against text transcribed verbatim from
injection_follower_v3.ino's SerialUSB.print calls -- a previous version of
this table used invented phrasing that matched nothing, so /metrics silently
reported zero events forever without any test catching it. These tests pin
the patterns to the firmware's actual strings so that regression cannot
recur silently.
"""

from __future__ import annotations

import pytest

from injection_monitor import protocol as P
from injection_monitor import state as S


def _fed(ev: S.EventLog, raw_line: str) -> dict | None:
    """Feed a raw firmware line through exactly the path api.py uses:
    tagged lines are stripped of their tag by classify_line before reaching
    note_debug_line; untagged lines are passed through as-is.
    """
    tag = P.classify_line(raw_line)
    text = tag.text if tag else raw_line
    ev.note_debug_line(text)
    return ev.recent(1)[0] if ev.recent(1) else None


# (raw firmware line, expected kind, expected channel)
FIRMWARE_LINES = [
    ("[DEBUG] bump slower up called!", "bump_called", "slower"),
    ("[DEBUG] bump xbeam up called!", "bump_called", "xbeam"),
    ("[DEBUG] Relock slower called! nattempts: 3", "relock_called", "slower"),
    ("Relock xbeam called! nattempts: 1", "relock_called", "xbeam"),
    ("[DEBUG] Slower relock successful on iteration 2", "relock_success", None),
    ("[DEBUG] Relock xbeam successful!", "relock_success", None),
    ("[DEBUG] Slower relock failed. Passing to recovery on next loop.",
     "relock_failed", None),
    ("[DEBUG] Xbeam relock failed. Passing to recovery on next loop.",
     "relock_failed", None),
    ("[DEBUG] Recover slower called!", "recover_called", None),
    ("[DEBUG] Peak found during recovery. Breaking.", "recover_success", None),
    ("Recovery failed.", "recovery_failed", None),
    ("[DEBUG] Max relock attempts reached!", "max_attempts", None),
    ("[DEBUG] Max recovery attempts reached! Exiting.", "max_attempts", None),
    ("[DEBUG] Losing slower detected", "losing", "slower"),
    ("[DEBUG] Losing xbeam detected", "losing", "xbeam"),
    ("[DEBUG] Lost slower detected", "lost", "slower"),
    ("[DEBUG] Lost xbeam detected", "lost", "xbeam"),
    ("[DEBUG] Slower bump 1 failed. Bumping other way.", "bump_fallback", "slower"),
    ("[DEBUG] Xbeam bump 1 failed. Bumping other way.", "bump_fallback", "xbeam"),
    ("[DEBUG] Slower bump 2 failed. Exiting.", "channel_fail", "slower"),
    ("[DEBUG] Xbeam bump 2 failed. Exiting.", "channel_fail", "xbeam"),
]


@pytest.mark.parametrize("raw,kind,channel", FIRMWARE_LINES)
def test_event_pattern_matches_real_firmware_text(tmp_path, raw, kind, channel):
    ev = S.EventLog(tmp_path / "events.jsonl")
    got = _fed(ev, raw)
    assert got is not None, f"no pattern matched: {raw!r}"
    assert got["kind"] == kind
    assert got.get("channel") == channel


def test_recover_xbeam_is_not_visible_to_the_host(tmp_path):
    """Documents a firmware defect (firmware/known_issues.md): recover_xbeam()
    prints to Serial, not SerialUSB, so this line never reaches the host at
    all. There is nothing to match -- included so the gap is explicit rather
    than an unexplained absence from FIRMWARE_LINES.
    """
    ev = S.EventLog(tmp_path / "events.jsonl")
    got = _fed(ev, "Recover xbeam called!")
    assert got is None  # correctly unmatched: this text never actually arrives


def test_metrics_reports_relock_rate_per_channel(tmp_path):
    ev = S.EventLog(tmp_path / "events.jsonl")
    for _ in range(3):
        _fed(ev, "[DEBUG] Relock slower called! nattempts: 1")
    _fed(ev, "Relock xbeam called! nattempts: 1")
    m = ev.metrics(hours=24)
    assert m["rates_per_hour"]["relock_called"]["slower"] > 0
    assert m["rates_per_hour"]["relock_called"]["xbeam"] > 0


def test_metrics_empty_and_populated_share_a_shape(tmp_path):
    """A Dash callback that special-cases the empty response breaks on the
    first quiet night after deployment; both cases must have the same keys.
    """
    ev = S.EventLog(tmp_path / "events.jsonl")
    empty = ev.metrics()
    _fed(ev, "[DEBUG] Losing slower detected")
    populated = ev.metrics()
    assert set(empty) == set(populated)


def test_nan_is_stripped_before_it_reaches_json():
    assert S.jsonable({"error": float("nan"), "ok": 1.5}) == {"error": None, "ok": 1.5}


def test_sticky_warning_persists_until_acknowledged():
    ring = S.WarningRing(size=10)
    ring.add("[WARN]", "WARNING", "slower unstable at the back-off floor", sticky=True)
    ring.add("[WARN]", "WARNING", "unknown command: QQ", sticky=False)
    sticky = ring.sticky()
    assert len(sticky) == 1
    assert ring.acknowledge() == 1
    assert ring.sticky() == []
