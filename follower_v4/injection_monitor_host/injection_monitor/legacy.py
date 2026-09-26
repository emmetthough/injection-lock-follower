"""Legacy (pre-v4) block bodies, transcribed from injection_follower_v3.ino.

The outer ``[START]``/``[END]`` names do NOT identify the content.  Three
separate outer blocks are emitted by one ``I``, and the first of them opens as
``Initalization`` but closes as ``Peaks``:

    [START] Initalization        <- sendPeaks(),   line 348
    BEGIN_peaks
    High 0: val=1234 pos=56
    Low 0: val=987 pos=301
    END_peaks
    [END] Peaks                  <- note the mismatch

    [START] Stats                <- sendStats(),   line 351
    BEGIN_stats
    meanHeight=2412.55 stdHeight=31.20
    BEGIN_lowStats               <- a *second* BEGIN inside the same block
    meanHeight=1980.10 stdHeight=24.75
    END_stats
    [END] Stats

    [START] Clusters             <- sendClusters(), line 354
    BEGIN_clusters
    High cluster 0 meanPos = 123.45
    Low cluster 0 meanPos = 301.20
    END_clusters
    [END] Clusters

``R`` emits, in order (loop(), line 1365):

    [START] Trace
    <2*N raw bytes, no trailing newline>
    [START] peak_tracking        <- printPeakStatus(), line 487
    BEGIN_tracking
    High 0 pos=12 val=2345 status=OK
    Low 0 pos=310 val=1975 status=LOST
    [END] peak_tracking          <- no END_tracking marker

``S`` emits (spectralPurityCurve(), line 622):

    [START] Spectral Purity Curve
    BEGIN_Slower
    Slower size = 240
    <csv, trailing comma>        x3: steps, peaks_up, peaks_down
    END_Slower
    BEGIN_Xbeam
    Xbeam size = 240
    <csv, trailing comma>        x3
    END Xbeam                    <- underscore dropped
    START Slopes
    <m>,<x0>,<y0>                slower
    <m>,<x0>,<y0>                xbeam
    END Slopes
    [END] Spectral Purity Curve

Consequences for the host:

* Text blocks must be dispatched on the inner ``BEGIN_*`` marker, not on the
  outer ``[START]`` name.  New-style blocks (Holdoff/Backoff) carry no inner
  marker and are dispatched on the outer name instead.
* ``BEGIN_tracking`` has no closing ``END_tracking``.  The old host code slices
  ``lines[1:-1]`` uniformly, which for tracking discards the final real entry --
  see ``parse_tracking`` below.
* Markers here are matched by name, never by position, so an interleaved
  ``[WARN]`` cannot shift a field.
"""

from __future__ import annotations

import re
from typing import Any, Sequence

from .protocol import BlockParseError, _as_float, _as_int

# "High 0: val=1234 pos=56"   (sendPeaks)
PEAK_RE = re.compile(
    r"^(?P<side>High|Low)\s+(?P<idx>\d+)\s*:\s*val=(?P<val>-?\d+)\s+pos=(?P<pos>-?\d+)",
    re.IGNORECASE,
)

# "High 0 pos=12 val=2345 status=OK"   (printPeakStatus) -- note pos before val,
# and no colon, unlike sendPeaks.
TRACK_RE = re.compile(
    r"^(?P<side>High|Low)\s+(?P<idx>\d+)\s+pos=(?P<pos>-?\d+)\s+val=(?P<val>-?\d+)"
    r"\s+status=(?P<status>\w+)",
    re.IGNORECASE,
)

# "High cluster 0 meanPos = 123.45"
CLUSTER_RE = re.compile(
    r"^(?P<side>High|Low)\s+cluster\s+(?P<idx>\d+)\s+meanPos\s*=\s*(?P<pos>-?[\d.]+)",
    re.IGNORECASE,
)

# "meanHeight=2412.55 stdHeight=31.20"
STATS_RE = re.compile(
    r"meanHeight=(?P<mean>-?[\d.]+)\s+stdHeight=(?P<std>-?[\d.]+)", re.IGNORECASE
)

#: Inner marker -> canonical running_data key.
INNER_MARKERS = {
    "BEGIN_peaks": "peaks",
    "BEGIN_stats": "stats",
    "BEGIN_clusters": "clusters",
    "BEGIN_tracking": "tracking",
    "BEGIN_Slower": "spectral purity curve",
}

#: High == slower, Low == xbeam, throughout the firmware.
_SIDE = {"high": "slower", "low": "xbeam"}


def inner_marker(lines: Sequence[str]) -> str | None:
    """Canonical key from the first non-tagged line of a legacy block body."""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("["):
            continue
        for marker, key in INNER_MARKERS.items():
            if s.startswith(marker):
                return key
        return None
    return None


def parse_peaks(lines: Sequence[str]) -> dict[str, Any]:
    out = {
        "slower": {"vals": [], "pos": [], "idx": []},
        "xbeam": {"vals": [], "pos": [], "idx": []},
    }
    for line in lines:
        m = PEAK_RE.match(line.strip())
        if not m:
            continue
        ch = out[_SIDE[m.group("side").lower()]]
        ch["vals"].append(_as_int(m.group("val")))
        ch["pos"].append(_as_int(m.group("pos")))
        ch["idx"].append(int(m.group("idx")))
    if not out["slower"]["vals"] and not out["xbeam"]["vals"]:
        raise BlockParseError("peaks: no parsable entries")
    return out


def parse_clusters(lines: Sequence[str]) -> dict[str, Any]:
    out: dict[str, list] = {"slower": [], "xbeam": []}
    for line in lines:
        m = CLUSTER_RE.match(line.strip())
        if not m:
            continue
        out[_SIDE[m.group("side").lower()]].append(_as_float(m.group("pos")))
    return out


def parse_tracking(lines: Sequence[str]) -> dict[str, Any]:
    """Parse BEGIN_tracking .. [END] peak_tracking.

    The old host did ``for line in lines[1:-1]``, which is correct for
    ``BEGIN_peaks``/``END_peaks`` but wrong here, because the tracking body has
    no closing ``END_tracking`` -- the block is terminated by the outer
    ``[END] peak_tracking`` line, which read_until() already consumed.  The
    slice therefore dropped the LAST tracked cluster on every single poll.  As
    the Low entries are printed last, that is an xbeam cluster; with one low
    cluster the xbeam vanished from the tracking plot entirely.

    Matching on TRACK_RE instead of slicing removes the class of bug.
    """
    out = {
        "slower": {"pos": [], "val": [], "status": [], "idx": []},
        "xbeam": {"pos": [], "val": [], "status": [], "idx": []},
    }
    for line in lines:
        m = TRACK_RE.match(line.strip())
        if not m:
            continue
        ch = out[_SIDE[m.group("side").lower()]]
        ch["pos"].append(_as_int(m.group("pos")))
        ch["val"].append(_as_int(m.group("val")))
        ch["status"].append(m.group("status").upper())
        ch["idx"].append(int(m.group("idx")))
    for ch in ("slower", "xbeam"):
        st = out[ch]["status"]
        out[ch]["n_lost"] = sum(1 for s in st if s == "LOST")
        out[ch]["n_tracked"] = len(st)
    return out


def parse_stats(lines: Sequence[str]) -> dict[str, Any]:
    """Parse BEGIN_stats / BEGIN_lowStats / END_stats.

    Two identically-formatted meanHeight lines, disambiguated only by which
    side of ``BEGIN_lowStats`` they fall on.  The old code used lines[1] and
    lines[3]; one interleaved [WARN] shifted both.
    """
    side = "slower"
    out: dict[str, Any] = {}
    for line in lines:
        s = line.strip()
        if s.startswith("BEGIN_lowStats"):
            side = "xbeam"
            continue
        m = STATS_RE.search(s)
        if not m:
            continue
        out[side] = {
            "height": _as_float(m.group("mean")),
            "std": _as_float(m.group("std")),
        }
    if not out:
        raise BlockParseError("stats: no meanHeight line found")
    return out


def _csv_ints(line: str) -> list[int]:
    """Parse a trailing-comma CSV row, tolerating the empty final field."""
    vals = []
    for tok in line.split(","):
        tok = tok.strip()
        if not tok:
            continue
        v = _as_int(tok)
        if v is not None:
            vals.append(v)
    return vals


def parse_spc(lines: Sequence[str]) -> dict[str, Any]:
    """Parse the full SPC block from already-collected lines.

    Marker-driven rather than the old read-exactly-three-lines-then-seek, so an
    interleaved diagnostic does not consume a data row.  Note the firmware
    writes ``END Xbeam`` and ``START Slopes`` without underscores.
    """
    section = None
    rows: dict[str, list[list[int]]] = {"slower": [], "xbeam": []}
    slopes: list[list[float]] = []
    sizes: dict[str, int | None] = {"slower": None, "xbeam": None}

    for raw in lines:
        s = raw.strip()
        if not s or s.startswith("["):
            continue
        if s.startswith("BEGIN_Slower"):
            section = "slower"
            continue
        if s.startswith("BEGIN_Xbeam"):
            section = "xbeam"
            continue
        if s.startswith("START Slopes"):
            section = "slopes"
            continue
        if s.startswith(("END_Slower", "END Xbeam", "END_Xbeam", "END Slopes", "END_Slopes")):
            section = None
            continue
        if section in ("slower", "xbeam"):
            if "size" in s and "=" in s:
                sizes[section] = _as_int(s.split("=")[-1])
                continue
            if "," in s:
                rows[section].append(_csv_ints(s))
        elif section == "slopes" and "," in s:
            vals = [_as_float(t) for t in s.split(",")]
            if len(vals) >= 3:
                slopes.append(vals[:3])

    out: dict[str, Any] = {}
    for i, ch in enumerate(("slower", "xbeam")):
        r = rows[ch]
        if len(r) < 3:
            raise BlockParseError(f"spc: {ch} had {len(r)} data rows, expected 3")
        fb = slopes[i] if i < len(slopes) else [None, None, None]
        out[ch] = {
            "steps": r[0],
            "peaks": {"up": r[1], "down": r[2]},
            "fb": {"m": fb[0], "x0": fb[1], "y0": fb[2]},
            "n": sizes[ch],
        }
        # Length agreement is the cheap corruption check the old code lacked.
        lens = {len(r[0]), len(r[1]), len(r[2])}
        out[ch]["consistent"] = len(lens) == 1 and (
            sizes[ch] is None or sizes[ch] in lens
        )
    return out


LEGACY_PARSERS = {
    "peaks": parse_peaks,
    "clusters": parse_clusters,
    "tracking": parse_tracking,
    "stats": parse_stats,
    "spectral purity curve": parse_spc,
}
