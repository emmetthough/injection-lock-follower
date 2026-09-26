"""Wire protocol: firmware text -> Python dicts.

Pure functions only.  Nothing in this module touches a serial port, a socket or
the clock, so the whole parser surface is testable from recorded fixtures.

Two invariants worth stating up front, because they are what makes the host
forwards- and backwards-compatible:

1. **Blocks are dispatched by name, never by "what did we last ask for".**
   ``[START] Backoff Status`` is emitted unsolicited whenever a bias-related
   ``C`` parameter is set, so correlating replies to requests would desync.

2. **An unrecognised block is drained and discarded, not an error.**
   Firmware newer than this host must never crash it.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

# --------------------------------------------------------------------------
# Line tags
# --------------------------------------------------------------------------

#: Maps a firmware line tag to the logging level it should be recorded at.
#: Anything not listed here is logged at INFO rather than dropped, so a future
#: firmware tag stays visible without a host change.
TAG_LEVELS = {
    "[DEBUG]": "DEBUG",
    "[INFO]": "INFO",
    "[WARN]": "WARNING",
    "[HOLDOFF]": "INFO",
}

TAG_RE = re.compile(r"^\s*(\[[A-Z]+\])\s*(.*)$")

#: Warnings that describe a latched physical fault.  These should stick in the
#: UI until explicitly acknowledged rather than scrolling away.
STICKY_WARNINGS = (
    "unstable at the back-off floor",
    "swept to the DAC rail",
    "holdoff feedback enabled but uncalibrated",
)

#: [HOLDOFF] err=<float> samples, delayus <old> -> <new>
HOLDOFF_EVENT_RE = re.compile(
    r"err=(?P<err>[-+0-9.eEnaN]+)\s*samples,\s*delayus\s+(?P<old>-?\d+)\s*->\s*(?P<new>-?\d+)"
)


@dataclass
class TaggedLine:
    tag: str
    level: str
    text: str
    sticky: bool = False
    #: Parsed payload for tags we understand structurally ([HOLDOFF]).
    event: dict | None = None


def classify_line(line: str) -> TaggedLine | None:
    """Return a TaggedLine for a diagnostic line, or None if it is not tagged."""
    m = TAG_RE.match(line)
    if not m:
        return None
    tag, text = m.group(1), m.group(2).strip()
    level = TAG_LEVELS.get(tag, "INFO")
    sticky = tag == "[WARN]" and any(s in text for s in STICKY_WARNINGS)
    event = None
    if tag == "[HOLDOFF]":
        em = HOLDOFF_EVENT_RE.search(text)
        if em:
            event = {
                "error_samples": _as_float(em.group("err")),
                "delayus_old": int(em.group("old")),
                "delayus_new": int(em.group("new")),
            }
    return TaggedLine(tag=tag, level=level, text=text, sticky=sticky, event=event)


# --------------------------------------------------------------------------
# Scalar helpers
# --------------------------------------------------------------------------


class BlockParseError(ValueError):
    """Raised when a block is recognised but its contents cannot be read."""


def _as_float(s: str) -> float | None:
    """Parse a float, mapping nan/inf to None.

    The firmware emits ``error,nan`` in the Holdoff Status block before the
    first drain.  ``float('nan')`` is *not* valid JSON -- json.dumps emits the
    bare token ``NaN``, which JSON.parse() in the browser rejects.  Mapping to
    None here means the Dash fetch cannot fail on a fresh boot.
    """
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return None if not math.isfinite(v) else v


def _as_int(s: str) -> int | None:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def _as_bool(s: str) -> bool | None:
    v = _as_int(s)
    return None if v is None else bool(v)


def kv_lines(lines: Iterable[str], sep: str = ",") -> dict[str, str]:
    """Split ``key,value`` lines into a dict, ignoring anything else.

    Ignoring unsplittable lines is what makes the parsers immune to a
    ``[WARN]`` interleaving mid-block -- the failure mode that currently
    corrupts the stats parse, which indexes lines[1] and lines[3] positionally.
    """
    out: dict[str, str] = {}
    for line in lines:
        if sep not in line:
            continue
        k, _, v = line.partition(sep)
        k = k.strip()
        if k and not k.startswith("["):
            out[k] = v.strip()
    return out


def require(d: dict[str, str], key: str, block: str) -> str:
    if key not in d:
        raise BlockParseError(f"{block}: missing field {key!r}")
    return d[key]


# --------------------------------------------------------------------------
# Block name normalisation
# --------------------------------------------------------------------------

START_RE = re.compile(r"^\s*\[START\]\s*(.*?)\s*$")
END_RE = re.compile(r"^\s*\[END\]\s*(.*?)\s*$")

#: The firmware spells it "Initalization".  Accept both so that fixing the
#: typo in a future firmware does not break the host.
_ALIASES = {
    "initalization": "initialization",
    "initialization": "initialization",
    "holdoff calibration": "holdoff calibration",
    "holdoff status": "holdoff status",
    "backoff status": "backoff status",
    "spectral purity curve": "spectral purity curve",
    "trace": "trace",
    "clusters": "clusters",
    "peaks": "peaks",
    "tracking": "tracking",
    "stats": "stats",
}


def block_name(line: str) -> str | None:
    """Canonical lowercase name from a ``[START] ...`` line."""
    m = START_RE.match(line)
    if not m:
        return None
    raw = m.group(1).strip().lower()
    return _ALIASES.get(raw, raw)


def is_end(line: str) -> bool:
    return END_RE.match(line) is not None


# --------------------------------------------------------------------------
# New-block parsers
# --------------------------------------------------------------------------


def parse_holdoff_calibration(lines: Sequence[str]) -> dict[str, Any]:
    """
    [START] Holdoff Calibration
    step_us,<int>
    nsteps,<int>
    START Points
    <delayus>,<mean_position_error>     x (2*nsteps+1)
    END Points
    slope,<float>
    [END] Holdoff Calibration
    """
    header: list[str] = []
    points: list[tuple[int, float]] = []
    trailer: list[str] = []
    where = "header"

    for line in lines:
        s = line.strip()
        if s == "START Points":
            where = "points"
            continue
        if s == "END Points":
            where = "trailer"
            continue
        if where == "points":
            if s.startswith("["):  # a [WARN] interleaved into the sweep
                continue
            parts = s.split(",")
            if len(parts) >= 2:
                d, e = _as_int(parts[0]), _as_float(parts[1])
                if d is not None and e is not None:
                    points.append((d, e))
            continue
        (header if where == "header" else trailer).append(s)

    fields_ = kv_lines(header)
    fields_.update(kv_lines(trailer))

    slope = _as_float(fields_.get("slope", ""))
    # slope,0 is the firmware's documented failure signal.  The reason is in an
    # accompanying [WARN]; the link layer attaches it.
    ok = slope is not None and slope != 0.0

    out: dict[str, Any] = {
        "step_us": _as_int(fields_.get("step_us", "")),
        "nsteps": _as_int(fields_.get("nsteps", "")),
        "slope": slope,
        "ok": ok,
        "points": [{"delayus": d, "error_samples": e} for d, e in points],
        "n_points": len(points),
    }

    # -1/slope is the ADC sample interval in us; the host has no other way to
    # measure it.  Also report the fit residual, which says whether the sweep
    # stayed linear.
    out["sample_interval_us"] = (-1.0 / slope) if ok else None
    out["residual_rms"] = _fit_residual(points, slope) if ok else None
    return out


def _fit_residual(points: Sequence[tuple[int, float]], slope: float | None) -> float | None:
    """RMS residual of the points about a line of the firmware's slope."""
    if not points or slope is None or len(points) < 2:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    # Intercept that minimises the residual for a fixed slope.
    b = sum(y - slope * x for x, y in zip(xs, ys)) / len(xs)
    sq = sum((y - (slope * x + b)) ** 2 for x, y in zip(xs, ys))
    return math.sqrt(sq / len(xs))


def parse_holdoff_status(lines: Sequence[str]) -> dict[str, Any]:
    """
    [START] Holdoff Status
    enabled,<0|1>
    slope,<float>
    delayus,<int>
    cal_step,<int>
    deadband,<float>          (samples)
    error,<float|nan>         (nan before the first drain)
    [END] Holdoff Status
    """
    f = kv_lines(lines)
    return {
        "enabled": _as_bool(f.get("enabled", "")),
        "slope": _as_float(f.get("slope", "")),
        "delayus": _as_int(f.get("delayus", "")),
        "cal_step": _as_int(f.get("cal_step", "")),
        "deadband_samples": _as_float(f.get("deadband", "")),
        "error_samples": _as_float(f.get("error", "")),  # nan -> None
        "calibrated": (_as_float(f.get("slope", "")) or 0.0) != 0.0,
    }


_BACKOFF_CHANNELS = ("slower", "xbeam")


def parse_backoff_status(lines: Sequence[str]) -> dict[str, Any]:
    """
    [START] Backoff Status
    nominal,<float>
    floor,<float>
    slower,<losingThresh>,<eventCount>,<floorLatched>,<biasEnabled>,<contBumpEnabled>
    xbeam,<...>
    [END] Backoff Status
    """
    nominal = floor = None
    channels: dict[str, Any] = {}

    for line in lines:
        s = line.strip()
        if "," not in s:
            continue
        key, _, rest = s.partition(",")
        key = key.strip()
        if key == "nominal":
            nominal = _as_float(rest)
        elif key == "floor":
            floor = _as_float(rest)
        elif key in _BACKOFF_CHANNELS:
            parts = [p.strip() for p in rest.split(",")]
            if len(parts) < 5:
                raise BlockParseError(
                    f"backoff status: {key} has {len(parts)} fields, expected 5"
                )
            channels[key] = {
                "losing_thresh": _as_float(parts[0]),
                "event_count": _as_int(parts[1]),
                "floor_latched": _as_bool(parts[2]),
                "bias_enabled": _as_bool(parts[3]),
                "contbump_enabled": _as_bool(parts[4]),
            }

    for ch in _BACKOFF_CHANNELS:
        c = channels.get(ch)
        if not c:
            continue
        # Fraction of the way from floor to nominal, for the dashboard bar.
        if nominal is not None and floor is not None and nominal > floor:
            lt = c["losing_thresh"]
            c["headroom_frac"] = (
                None if lt is None else max(0.0, min(1.0, (lt - floor) / (nominal - floor)))
            )
        else:
            c["headroom_frac"] = None
        c["backed_off"] = (
            None if (c["losing_thresh"] is None or nominal is None)
            else c["losing_thresh"] < nominal
        )

    return {"nominal": nominal, "floor": floor, "channels": channels}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

#: name -> (running_data key, parser).  Blocks handled by the link layer
#: because they need to read raw bytes (trace) or interleaved reads (spc) are
#: registered as None and handled there.
BLOCK_PARSERS: dict[str, tuple[str, Callable[[Sequence[str]], dict]]] = {
    "holdoff calibration": ("holdoff_calibration", parse_holdoff_calibration),
    "holdoff status": ("holdoff_status", parse_holdoff_status),
    "backoff status": ("backoff_status", parse_backoff_status),
}

#: Blocks the link layer reads itself rather than as newline-delimited text.
BINARY_BLOCKS = {"trace"}
STREAMED_BLOCKS = {"spectral purity curve"}


@dataclass
class ParseStats:
    """Per-block-type counters, exposed on /status.

    The point is to make a low-rate corruption visible.  The current code
    swallows AttributeError -> ValueError -> broad except -> print, so a 5%
    corruption rate is invisible.
    """

    ok: dict[str, int] = field(default_factory=dict)
    failed: dict[str, int] = field(default_factory=dict)
    unknown: dict[str, int] = field(default_factory=dict)
    truncated: int = 0

    def record_ok(self, name: str) -> None:
        self.ok[name] = self.ok.get(name, 0) + 1

    def record_fail(self, name: str) -> None:
        self.failed[name] = self.failed.get(name, 0) + 1

    def record_unknown(self, name: str) -> None:
        self.unknown[name] = self.unknown.get(name, 0) + 1

    def as_dict(self) -> dict:
        total_ok = sum(self.ok.values())
        total_bad = sum(self.failed.values())
        return {
            "ok": dict(self.ok),
            "failed": dict(self.failed),
            "unknown_blocks": dict(self.unknown),
            "truncated": self.truncated,
            "failure_rate": (total_bad / (total_ok + total_bad)) if (total_ok + total_bad) else 0.0,
        }
