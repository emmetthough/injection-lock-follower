"""Configuration.

Everything that was a module-level constant in injection_monitor_v2.py lives
here, validated, with environment overrides under the ``IM_`` prefix.

Uses pydantic-settings when it is installed and falls back to reading the
environment directly otherwise, because pydantic-settings is a separate package
from pydantic and may not be on a Pi that only ever installed FastAPI.  Field
definitions and validation are shared between both paths; only the source of
the values differs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from .constants import N as FIRMWARE_N

try:  # pragma: no cover - depends on the deployment
    from pydantic_settings import BaseSettings, SettingsConfigDict
    HAVE_PYDANTIC_SETTINGS = True
except ImportError:  # pragma: no cover
    BaseSettings = BaseModel  # type: ignore[misc,assignment]
    SettingsConfigDict = dict  # type: ignore[misc,assignment]
    HAVE_PYDANTIC_SETTINGS = False

ENV_PREFIX = "IM_"


class GpioPins(BaseModel):
    """BOARD numbering, as in the original file."""

    blue_lock_status: int = 11      # out: blue laser lock status
    slower_feedback_en: int = 13    # out
    xbeam_feedback_en: int = 15     # out
    slower_lock_state: int = 16     # in
    xbeam_lock_state: int = 18      # in
    slower_fail_state: int = 29     # in
    xbeam_fail_state: int = 31      # in

    @field_validator("*")
    @classmethod
    def _sane_pin(cls, v: int) -> int:
        if not 1 <= v <= 40:
            raise ValueError(f"BOARD pin {v} is outside 1..40")
        return v


class Settings(BaseSettings):
    if HAVE_PYDANTIC_SETTINGS:
        model_config = SettingsConfigDict(
            env_prefix=ENV_PREFIX, env_nested_delimiter="__",
            extra="ignore", frozen=True,
        )

    # --- serial link ---
    port: str = "/dev/ttyACM0"
    baud: int = Field(250000, gt=0)
    #: Per-read timeout.  A *poll interval*, not an operation timeout:
    #: readline() returning "" is the normal idle case and the reader loop uses
    #: those returns to service the command queue.  Block-level deadlines are
    #: the timeout_* values below.  The original 1.0 s meant a queued command
    #: could sit for a second behind an idle readline.
    serial_timeout: float = Field(0.1, gt=0, le=2.0)
    #: Samples per trace.  Sourced from shared/constants.json via the
    #: generator, so it cannot drift from the firmware's N by accident.
    trace_samples: int = Field(FIRMWARE_N, gt=0)

    #: Send "I" on connect.  Off by default: the Due's native USB port does not
    #: reset the board when the Pi opens it, so the firmware keeps running
    #: across a host restart.  The old host wrote "I" twice on startup, which
    #: re-ran initialize_peak_vals_locations() and reset refHeight/refStd on a
    #: lock that was already good.
    send_init_on_connect: bool = False

    reconnect_delay_min: float = Field(1.0, gt=0)
    reconnect_delay_max: float = Field(30.0, gt=0)

    # --- command timeouts, seconds ---
    timeout_status: float = Field(5.0, gt=0)
    timeout_init: float = Field(60.0, gt=0)
    timeout_spc: float = Field(180.0, gt=0)
    timeout_calibration: float = Field(180.0, gt=0)
    timeout_unknown: float = Field(10.0, gt=0)
    max_block_lines: int = Field(5000, gt=0)
    capability_probe_timeout: float = Field(3.0, gt=0)

    #: When to push stored parameters back to the firmware.
    #:   "boot"    - only on evidence of a firmware restart. Safest, because
    #:               the board keeps running across a host restart.
    #:   "connect" - every time the port is opened.
    #:   "never"   - manual POST /params/sync only.
    replay_policy: Literal["boot", "connect", "never"] = "boot"

    # --- queue ---
    command_queue_size: int = Field(16, gt=0)

    # --- interlock thread ---
    gpio_poll_interval: float = Field(0.5, gt=0)
    dash_url: str = "http://10.155.94.105:8050"
    dash_timeout: float = Field(2.0, gt=0)
    dash_queue_size: int = Field(32, gt=0)
    pins: GpioPins = Field(default_factory=GpioPins)
    #: Set false to run off-Pi against the null GPIO backend.
    gpio_enabled: bool = True

    # --- persistence ---
    state_dir: Path = Path("/var/lib/injection_monitor")
    log_dir: Path = Path("/var/log/injection_monitor")
    log_max_bytes: int = Field(20 * 1024 * 1024, gt=0)
    log_backup_count: int = Field(10, ge=0)
    warning_ring_size: int = Field(200, gt=0)

    @field_validator("dash_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("reconnect_delay_max")
    @classmethod
    def _max_above_min(cls, v: float, info) -> float:
        lo = info.data.get("reconnect_delay_min")
        if lo is not None and v < lo:
            raise ValueError("reconnect_delay_max must be >= reconnect_delay_min")
        return v

    @property
    def params_file(self) -> Path:
        return self.state_dir / "parameters.json"

    @property
    def events_file(self) -> Path:
        """JSONL: holdoff corrections, relock entries, back-off events."""
        return self.state_dir / "events.jsonl"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "monitor.log"


def _env_overrides() -> dict:
    """Read IM_* variables for the no-pydantic-settings path.

    Values are passed to the model as strings; pydantic coerces and validates
    them, so this path gets the same checking as the other one.
    """
    out: dict = {}
    fields = Settings.model_fields
    for name in fields:
        raw = os.environ.get(ENV_PREFIX + name.upper())
        if raw is None:
            continue
        if fields[name].annotation is bool:
            out[name] = raw.strip().lower() in ("1", "true", "yes", "on")
        else:
            out[name] = raw
    pins = {}
    for pin in GpioPins.model_fields:
        raw = os.environ.get(f"{ENV_PREFIX}PINS__{pin.upper()}")
        if raw is not None:
            pins[pin] = raw
    if pins:
        out["pins"] = GpioPins(**{**GpioPins().model_dump(), **pins})
    return out


def load() -> Settings:
    if HAVE_PYDANTIC_SETTINGS:
        return Settings()
    return Settings(**_env_overrides())


def describe(s: Settings) -> dict:
    """Serialisable view, for /status and the startup log line."""
    return s.model_dump(mode="json")
