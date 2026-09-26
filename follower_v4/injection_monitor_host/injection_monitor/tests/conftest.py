"""Shared fixtures.

Everything runs against ``injection_monitor.testing.FakeFirmware``; no serial
port, no Pi, no lab.  That matters here beyond the usual convenience: the
firmware summary notes that the LOST branch and the xbeam probe have never
executed, and the host's new block parsers have never seen real hardware
either, so these tests are the only thing exercising either side.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_state(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def cfg(tmp_path):
    """Settings with fast timeouts and GPIO disabled."""
    from injection_monitor.config import Settings

    return Settings(
        gpio_enabled=False,
        state_dir=tmp_path,
        log_dir=tmp_path,
        capability_probe_timeout=1.0,
        timeout_status=1.5,
        timeout_init=2.0,
        timeout_calibration=2.0,
        timeout_unknown=1.0,
        reconnect_delay_min=0.05,
        reconnect_delay_max=0.1,
    )


@pytest.fixture
def store(cfg):
    from injection_monitor.params import ParameterStore

    return ParameterStore(cfg.params_file)


def build_link(cfg, store, version="v4", **fw_kwargs):
    """A SerialLink wired to a fake device, not yet probed."""
    from injection_monitor.link import SerialLink
    from injection_monitor.testing import FakeFirmware

    blocks: dict = {}
    tagged: list = []
    events: list = []
    device = FakeFirmware(version=version, **fw_kwargs)
    link = SerialLink(
        cfg, store,
        on_block=lambda k, v: blocks.__setitem__(k, v),
        on_tagged=tagged.append,
        on_event=lambda k, v: events.append((k, v)),
    )
    link._ser = device
    link.connected.set()
    return link, device, blocks, tagged, events


@pytest.fixture
def linked(cfg, store):
    """A connected, probed v4 link."""
    link, device, blocks, tagged, events = build_link(cfg, store)
    link._after_connect()
    return link, device, blocks, tagged, events


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with the serial port replaced by a fake device."""
    from fastapi.testclient import TestClient

    from injection_monitor import api
    from injection_monitor import link as link_mod
    from injection_monitor.testing import FakeFirmware

    version = os.environ.get("_TEST_FW_VERSION", "v4")
    holder: dict = {}

    def fake_open(self):
        self._ser = FakeFirmware(version=version)
        holder["device"] = self._ser
        self._connect_count += 1
        self.connected.set()
        self._on_connect_change(True)

    monkeypatch.setattr(link_mod.SerialLink, "_open", fake_open)
    for key, value in {
        "IM_GPIO_ENABLED": "0",
        "IM_STATE_DIR": str(tmp_path),
        "IM_LOG_DIR": str(tmp_path),
        "IM_CAPABILITY_PROBE_TIMEOUT": "1.0",
        "IM_TIMEOUT_STATUS": "1.5",
        "IM_TIMEOUT_CALIBRATION": "3.0",
    }.items():
        monkeypatch.setenv(key, value)

    with TestClient(api.app) as c:
        c.device = holder.get("device")
        yield c
