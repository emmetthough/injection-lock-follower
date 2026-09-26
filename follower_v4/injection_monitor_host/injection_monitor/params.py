"""The single source of truth for every settable value.

Today nobody owns these.  The firmware holds them in RAM and loses them on
reset; the Dash page holds them in browser state and loses them on refresh; the
Pi holds none of them.  That is why a page refresh blanks the controls, and why
a calibrated ``holdoff_slope`` dies with the Arduino.

This module makes the Pi the owner:

* seeded from the firmware's own compiled-in defaults, so a fresh install
  reports real numbers rather than nulls;
* written through to the firmware and persisted to disk on every change;
* replayed to the firmware on connect and after a detected reset;
* reconciled against ``HR``/``BR`` status blocks, which are authoritative when
  they disagree -- the firmware's value is the one actually acting.

Provenance is tracked per entry so the dashboard can distinguish "this is the
default" from "you set this" from "the firmware told us this".
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

#: Capability tags.  "core" is supported by every firmware including v3.
CORE = "core"
HOLDOFF = "holdoff"
BACKOFF = "backoff"
SCAN_TIMEOUT = "scan_timeout"


@dataclass(frozen=True)
class ParamSpec:
    name: str                       # the C<var> name on the wire
    default: float
    kind: str                       # "float" | "int" | "sign" | "bool"
    capability: str = CORE
    lo: float | None = None
    hi: float | None = None
    unit: str = ""
    group: str = "core"
    doc: str = ""
    #: True if the firmware reports this back in a status block, so it can be
    #: confirmed rather than merely assumed.
    confirmable: bool = False
    #: Applying this costs a re-initialisation or otherwise disturbs the lock.
    disruptive: bool = False


def _s(*a, **kw) -> ParamSpec:
    return ParamSpec(*a, **kw)


#: Defaults transcribed from injection_follower_v3.ino (lines 40-66) for the
#: core set, and from the firmware change summary's C-parameter table for the
#: rest.  Ranges are host-side sanity limits, not firmware limits -- the
#: firmware validates nothing and will happily accept backoff_floor > 1.0.
SPECS: tuple[ParamSpec, ...] = (
    # --- core (present in v3) ---
    _s("unlock_thresh", 0.95, "float", CORE, 0.30, 1.50, "fraction", "thresholds",
       "Nominal acting threshold (losing_meanThresh).", confirmable=True),
    _s("lost_thresh", 0.25, "float", CORE, 0.01, 0.99, "fraction", "thresholds",
       "Lost threshold. Not affected by back-off."),
    _s("bump_thresh", 0.05, "float", CORE, 0.005, 0.50, "fraction of FWHM", "search",
       "Bump step as a fraction of FWHM."),
    _s("relock_thresh", 0.95, "float", CORE, 0.10, 2.00, "gain", "search",
       "Gain on the relock extrapolation."),
    _s("std_thresh", 2.0, "float", CORE, 0.50, 20.0, "x refStd", "thresholds",
       "Running-std trigger, as a multiple of refStd."),
    _s("slower_sign", 1, "sign", CORE, -1, 1, "", "geometry",
       "fb_sign for the slower. Points at the gentle flank."),
    _s("xbeam_sign", -1, "sign", CORE, -1, 1, "", "geometry",
       "fb_sign for the xbeam. Points at the gentle flank."),

    # --- v4: acquisition watchdog ---
    _s("scan_timeout_ms", 500, "int", SCAN_TIMEOUT, 50, 10000, "ms", "acquisition",
       "Acquisition watchdog. Raise if the ramp is slower than 2 Hz."),

    # --- v4: safe-side bias ---
    _s("slower_bias", 1, "bool", BACKOFF, 0, 1, "", "bias",
       "Enable the fixed safe-side offset on the slower.", confirmable=True),
    _s("xbeam_bias", 0, "bool", BACKOFF, 0, 1, "", "bias",
       "Enable the fixed safe-side offset on the xbeam.", confirmable=True),
    _s("slower_contbump", 0, "bool", BACKOFF, 0, 1, "", "bias",
       "Continuous safe-side bump, slower. Costs an extra scan per iteration.",
       confirmable=True),
    _s("xbeam_contbump", 0, "bool", BACKOFF, 0, 1, "", "bias",
       "Continuous safe-side bump, xbeam. Costs an extra scan per iteration.",
       confirmable=True),
    _s("bias_frac", 0.25, "float", BACKOFF, 0.0, 1.0, "fraction of FWHM", "bias",
       "Fixed offset as a fraction of FWHM."),

    # --- v4: instability back-off ---
    _s("instab_window_ms", 60000, "int", BACKOFF, 1000, 3600000, "ms", "backoff",
       "Rolling window for counting instability events."),
    _s("instab_events", 3, "int", BACKOFF, 1, 100, "events", "backoff",
       "Events in window that trigger a back-off."),
    _s("backoff_step", 0.02, "float", BACKOFF, 0.001, 0.50, "fraction", "backoff",
       "Threshold drop per trigger."),
    _s("backoff_floor", 0.70, "float", BACKOFF, 0.10, 1.00, "fraction", "backoff",
       "Hard minimum; latches a fault.", confirmable=True),
    _s("backoff_quiet_ms", 600000, "int", BACKOFF, 1000, 86400000, "ms", "backoff",
       "Quiet time before creeping back."),
    _s("backoff_creep", 0.005, "float", BACKOFF, 0.0001, 0.50, "fraction", "backoff",
       "Increase per quiet interval."),

    # --- v4: holdoff drift servo ---
    _s("holdoff_slope", 0.0, "float", HOLDOFF, -100.0, 100.0, "samples/us", "holdoff",
       "Samples per us, negative. Set by HC, or restored by the host.",
       confirmable=True),
    _s("holdoff_cal_step", 10, "int", HOLDOFF, 1, 1000, "us", "holdoff",
       "Microseconds per calibration step.", confirmable=True),
    _s("holdoff_cal_frac", 0.5, "float", HOLDOFF, 0.05, 1.0, "x SEARCH_WINDOW", "holdoff",
       "Calibrate until displacement reaches this x SEARCH_WINDOW."),
    _s("holdoff_actuate_frac", 0.5, "float", HOLDOFF, 0.05, 2.0, "x SEARCH_WINDOW", "holdoff",
       "Actuate above this x SEARCH_WINDOW."),
    _s("holdoff_max_step", 20, "int", HOLDOFF, 1, 1000, "us", "holdoff",
       "Clamp on one correction."),
    _s("holdoff_interval_ms", 60000, "int", HOLDOFF, 1000, 86400000, "ms", "holdoff",
       "Minimum time between corrections."),
)

SPEC_BY_NAME = {s.name: s for s in SPECS}


class ValidationError(ValueError):
    """Raised for a value the firmware would accept but should not."""


def coerce(spec: ParamSpec, value: Any) -> float | int:
    """Type-check and range-check a single value."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{spec.name}: {value!r} is not a number")
    if num != num or num in (float("inf"), float("-inf")):
        raise ValidationError(f"{spec.name}: must be finite")

    if spec.kind == "sign":
        if num not in (-1, 1):
            raise ValidationError(f"{spec.name}: must be -1 or +1, got {num:g}")
        return int(num)
    if spec.kind == "bool":
        if num not in (0, 1):
            raise ValidationError(f"{spec.name}: must be 0 or 1, got {num:g}")
        return int(num)
    if spec.kind == "int":
        if num != int(num):
            raise ValidationError(f"{spec.name}: must be a whole number, got {num:g}")
        num = int(num)

    if spec.lo is not None and num < spec.lo:
        raise ValidationError(f"{spec.name}: {num:g} below minimum {spec.lo:g}")
    if spec.hi is not None and num > spec.hi:
        raise ValidationError(f"{spec.name}: {num:g} above maximum {spec.hi:g}")
    return num


#: Cross-parameter constraints.  The firmware checks none of these, and every
#: one of them produces a servo that misbehaves in a way that looks like a
#: hardware fault.
CrossCheck = Callable[[dict], str | None]


def _c_lost_below_unlock(v: dict) -> str | None:
    if v["lost_thresh"] >= v["unlock_thresh"]:
        return (
            f"lost_thresh ({v['lost_thresh']:g}) must be below unlock_thresh "
            f"({v['unlock_thresh']:g}); otherwise every losing channel is "
            f"immediately classified lost and bumpUp never runs"
        )
    return None


def _c_floor_below_unlock(v: dict) -> str | None:
    if v["backoff_floor"] >= v["unlock_thresh"]:
        return (
            f"backoff_floor ({v['backoff_floor']:g}) must be below unlock_thresh "
            f"({v['unlock_thresh']:g}); the back-off would latch a fault on its "
            f"first step"
        )
    return None


def _c_floor_above_lost(v: dict) -> str | None:
    if v["backoff_floor"] <= v["lost_thresh"]:
        return (
            f"backoff_floor ({v['backoff_floor']:g}) must stay above lost_thresh "
            f"({v['lost_thresh']:g}); a fully backed-off channel would never "
            f"detect a loss"
        )
    return None


def _c_creep_below_step(v: dict) -> str | None:
    if v["backoff_creep"] >= v["backoff_step"]:
        return (
            f"backoff_creep ({v['backoff_creep']:g}) should be well below "
            f"backoff_step ({v['backoff_step']:g}); symmetric recovery "
            f"oscillates at the instability period"
        )
    return None


def _c_holdoff_calibrated(v: dict) -> str | None:
    if v["holdoff_slope"] > 0:
        return (
            f"holdoff_slope is {v['holdoff_slope']:g}; increasing the holdoff "
            f"starts the window later, so peaks appear earlier and the slope "
            f"must be negative. A positive slope drives the servo the wrong way"
        )
    return None


CROSS_CHECKS: tuple[CrossCheck, ...] = (
    _c_lost_below_unlock,
    _c_floor_below_unlock,
    _c_floor_above_lost,
    _c_creep_below_step,
    _c_holdoff_calibrated,
)


@dataclass
class Param:
    value: float
    source: str = "default"      # "default" | "host" | "firmware" | "restored"
    updated: float = 0.0         # wall clock, seconds
    confirmed: bool = False      # firmware echoed it in a status block
    pending: bool = False        # written to the queue, not yet confirmed


class ParameterStore:
    """Thread-safe, persistent, validated parameter state."""

    SCHEMA = 1

    def __init__(self, path: Path, autosave: bool = True):
        self._path = Path(path)
        self._autosave = autosave
        self._lock = threading.RLock()
        self._params: dict[str, Param] = {
            s.name: Param(value=s.default, source="default") for s in SPECS
        }
        #: Firmware-owned runtime state, not settable via C.
        self._runtime: dict[str, Any] = {
            "holdoff_delayus": None,
            "holdoff_enabled": None,
            "feedback_active": None,
            "debug": None,
            "initialized": None,
        }
        self._dirty = False

    # ---------------- persistence ----------------

    def load(self) -> bool:
        """Restore from disk.  Returns True if anything was restored.

        Unknown keys are ignored and out-of-range values fall back to the
        default with a warning, so a stale file written by an older host can
        never prevent startup.
        """
        with self._lock:
            if not self._path.exists():
                log.info("no parameter file at %s; using firmware defaults", self._path)
                return False
            try:
                blob = json.loads(self._path.read_text())
            except (OSError, json.JSONDecodeError) as e:
                log.error("parameter file %s unreadable (%s); using defaults",
                          self._path, e)
                return False

            if blob.get("schema") != self.SCHEMA:
                log.warning("parameter file schema %r != %d; merging what matches",
                            blob.get("schema"), self.SCHEMA)

            restored = 0
            for name, entry in (blob.get("params") or {}).items():
                spec = SPEC_BY_NAME.get(name)
                if spec is None:
                    log.info("ignoring unknown stored parameter %r", name)
                    continue
                try:
                    value = coerce(spec, entry.get("value"))
                except ValidationError as e:
                    log.warning("stored %s rejected (%s); keeping default", name, e)
                    continue
                self._params[name] = Param(
                    value=value,
                    source="restored",
                    updated=float(entry.get("updated") or 0.0),
                    confirmed=False,      # nothing is confirmed until the firmware says so
                    pending=False,
                )
                restored += 1
            log.info("restored %d/%d parameters from %s", restored, len(SPECS), self._path)
            return restored > 0

    def save(self) -> None:
        """Atomic write: temp file in the same directory, then os.replace."""
        with self._lock:
            payload = {
                "schema": self.SCHEMA,
                "saved": time.time(),
                "params": {n: asdict(p) for n, p in self._params.items()},
            }
            blob = json.dumps(payload, indent=1, sort_keys=True)
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            with open(tmp, "w") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._path)
        except OSError as e:
            log.error("could not persist parameters to %s: %s", self._path, e)

    # ---------------- reads ----------------

    def value(self, name: str) -> float:
        with self._lock:
            return self._params[name].value

    def values(self) -> dict[str, float]:
        with self._lock:
            return {n: p.value for n, p in self._params.items()}

    def snapshot(self, capabilities: Iterable[str] | None = None) -> dict:
        """Full view for GET /params.  This is what Dash calls on page load."""
        caps = set(capabilities) if capabilities is not None else None
        with self._lock:
            out = {}
            for spec in SPECS:
                p = self._params[spec.name]
                out[spec.name] = {
                    "value": p.value,
                    "default": spec.default,
                    "source": p.source,
                    "confirmed": p.confirmed,
                    "pending": p.pending,
                    "updated": p.updated or None,
                    "kind": spec.kind,
                    "min": spec.lo,
                    "max": spec.hi,
                    "unit": spec.unit,
                    "group": spec.group,
                    "doc": spec.doc,
                    "capability": spec.capability,
                    "supported": caps is None or spec.capability in caps,
                    "at_default": p.value == spec.default,
                }
            return {
                "params": out,
                "runtime": dict(self._runtime),
                "warnings": self.cross_check_warnings(),
            }

    def cross_check_warnings(self) -> list[str]:
        vals = self.values()
        out = []
        for check in CROSS_CHECKS:
            try:
                msg = check(vals)
            except KeyError:
                continue
            if msg:
                out.append(msg)
        return out

    # ---------------- writes ----------------

    def set(self, name: str, value: Any, source: str = "host",
            strict: bool = True) -> tuple[float, list[str]]:
        """Validate and store.  Returns (coerced value, cross-check warnings).

        Range violations raise.  Cross-parameter problems are returned as
        warnings rather than raising, because there is always an ordering in
        which a legitimate pair of edits transiently violates one -- lowering
        unlock_thresh below backoff_floor before lowering the floor, say.  The
        API surfaces them; it does not block on them.
        """
        spec = SPEC_BY_NAME.get(name)
        if spec is None:
            raise ValidationError(f"unknown parameter {name!r}")
        coerced = coerce(spec, value)

        with self._lock:
            p = self._params[name]
            p.value = coerced
            p.source = source
            p.updated = time.time()
            p.confirmed = source == "firmware"
            p.pending = source in ("host", "restored")
            self._dirty = True
            warnings = self.cross_check_warnings() if strict else []

        if self._autosave:
            self.save()
        return coerced, warnings

    def confirm(self, name: str, value: Any) -> bool:
        """Reconcile against a firmware status block.

        The firmware's value wins on disagreement: it is what is actually
        acting.  Returns True if the values differed, so the caller can log it.
        """
        spec = SPEC_BY_NAME.get(name)
        if spec is None:
            return False
        try:
            fw = coerce(spec, value)
        except ValidationError:
            return False

        with self._lock:
            p = self._params[name]
            differed = p.value != fw
            if differed:
                log.warning(
                    "%s: host held %g, firmware reports %g -- taking the "
                    "firmware value", name, p.value, fw
                )
            p.value = fw
            p.confirmed = True
            p.pending = False
            if differed:
                p.source = "firmware"
                p.updated = time.time()
                self._dirty = True

        if differed and self._autosave:
            self.save()
        return differed

    def set_runtime(self, key: str, value: Any) -> None:
        with self._lock:
            self._runtime[key] = value

    def runtime(self) -> dict:
        with self._lock:
            return dict(self._runtime)

    def mark_unconfirmed(self) -> None:
        """Called on reconnect / detected reset: nothing is trusted any more."""
        with self._lock:
            for p in self._params.values():
                p.confirmed = False
                p.pending = True
            for k in self._runtime:
                self._runtime[k] = None

    # ---------------- replay ----------------

    def replay_commands(self, capabilities: Iterable[str],
                        only_non_default: bool = False) -> list[str]:
        """The C lines to push after a connect or a reset.

        Filtered by capability so we do not spray holdoff parameters at v3
        firmware, which would either ignore them silently (harmless) or, on an
        intermediate build, emit a [WARN] per line and drown the log.

        This is where a calibrated holdoff_slope comes back: the firmware has
        no flash storage and loses it on every reset, so restoring it here is
        the difference between a recalibration and a reconnect.
        """
        caps = set(capabilities)
        out = []
        with self._lock:
            for spec in SPECS:
                if spec.capability not in caps:
                    continue
                p = self._params[spec.name]
                if only_non_default and p.value == spec.default:
                    continue
                out.append(f"C{spec.name},{_fmt(p.value, spec)}")
        return out

    def pending_names(self) -> list[str]:
        with self._lock:
            return [n for n, p in self._params.items() if p.pending]


def _fmt(value: float, spec: ParamSpec) -> str:
    """Format for the wire.

    The firmware parses with String::toFloat()/toInt(), neither of which
    understands exponent notation -- ``5e-05`` is read as 5.  backoff_creep at
    its default of 0.005 is nowhere near that, but a user lowering it to 1e-5
    through the API would silently set it to 1.  Fixed notation always.
    """
    if spec.kind in ("int", "sign", "bool"):
        return str(int(round(value)))
    s = f"{value:.6f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"
