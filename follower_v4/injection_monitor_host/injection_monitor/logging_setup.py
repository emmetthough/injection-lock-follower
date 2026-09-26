"""Logging setup.

The old code used bare ``print()``: no timestamps, no levels, no persistence.
For a system whose purpose is surviving rare failures, that is the whole
problem -- you cannot debug what you did not record, and the failures worth
debugging happen at 3am.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys

from .config import Settings

FORMAT = "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%S%z"


def setup_logging(cfg: Settings, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(FORMAT, datefmt=DATEFMT)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.handlers.RotatingFileHandler(
            cfg.log_file, maxBytes=cfg.log_max_bytes,
            backupCount=cfg.log_backup_count,
        )
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
    except OSError as e:
        # A read-only or missing log directory must not prevent startup.
        root.warning("file logging disabled (%s): %s", cfg.log_file, e)

    # The firmware's 1 Hz debug dump is useful but voluminous; keep it out of
    # the file at INFO.
    logging.getLogger("injection_monitor.link").setLevel(level)
