"""HTTP API tests."""

from __future__ import annotations

import time

import pytest


# --------------------------------------------------------------------------
# Rehydration: the fix for the page-refresh problem
# --------------------------------------------------------------------------

def test_state_returns_every_parameter_with_provenance(client):
    body = client.get("/state").json()
    assert body["ready"] is True
    p = body["params"]["unlock_thresh"]
    assert set(p) >= {"value", "default", "source", "confirmed", "pending",
                      "supported", "min", "max", "unit", "group", "doc"}
    from injection_monitor.params import SPECS
    assert len(body["params"]) == len(SPECS)


def test_state_survives_a_refresh(client):
    """A reload must not blank the controls."""
    client.post("/params/bump_thresh", json={"value": 0.08})
    first = client.get("/state").json()["params"]["bump_thresh"]["value"]
    second = client.get("/state").json()["params"]["bump_thresh"]["value"]
    assert first == second == pytest.approx(0.08)


def test_state_is_json_safe_before_first_drain(client):
    """error,nan arrives on a fresh boot; NaN would break the whole fetch."""
    body = client.get("/state").json()
    assert body["holdoff"]["error_samples"] is None


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------

def test_post_returns_immediately_with_pending(client):
    r = client.post("/params/backoff_floor", json={"value": 0.65})
    assert r.status_code == 200
    body = r.json()
    assert body["pending"] is True
    assert body["command"] == "Cbackoff_floor,0.65"
    assert body["confirmable"] is True


def test_unconfirmable_parameter_is_flagged(client):
    """17 of 26 appear in no status block and can never be confirmed."""
    body = client.post("/params/bias_frac", json={"value": 0.3}).json()
    assert body["pending"] is True
    assert body["confirmable"] is False


def test_out_of_range_is_422_with_a_reason(client):
    r = client.post("/params/backoff_floor", json={"value": 9})
    assert r.status_code == 422
    assert "maximum" in r.json()["detail"]["error"]


def test_unknown_parameter_is_404(client):
    assert client.post("/params/nope", json={"value": 1}).status_code == 404


def test_cross_check_warning_is_returned(client):
    body = client.post("/params/unlock_thresh", json={"value": 0.40}).json()
    assert any("backoff_floor" in w for w in body["warnings"])


def test_sync_pushes_everything(client):
    from injection_monitor.params import SPECS
    r = client.post("/params/sync")
    assert r.status_code == 200, "literal route must beat /params/{name}"
    assert r.json()["count"] == len(SPECS)


# --------------------------------------------------------------------------
# Route ordering
# --------------------------------------------------------------------------

def test_literal_holdoff_routes_beat_the_converter(client):
    """Declared after /holdoff/{value}, 'calibrate' would 422 against int."""
    assert client.post("/holdoff/calibrate").status_code == 202
    assert client.post("/holdoff/set/240").status_code == 200
    assert client.get("/holdoff/status").status_code == 200


def test_legacy_holdoff_route_still_works_but_warns(client):
    body = client.post("/holdoff/240").json()
    assert body["deprecated"] is True
    assert body["replacement"] == "/holdoff/set/240"


def test_hs_does_not_reinitialise_but_d_does(client):
    """D runs 130 scans and resets refHeight; HS exists to avoid that."""
    client.post("/holdoff/set/240")
    time.sleep(0.5)
    assert "HS240" in client.device.received
    assert "D240" not in client.device.received


# --------------------------------------------------------------------------
# Long-running operations
# --------------------------------------------------------------------------

def test_calibration_returns_immediately_with_an_id(client):
    r = client.post("/holdoff/calibrate")
    assert r.status_code == 202
    op_id = r.json()["operation_id"]
    time.sleep(1.0)
    op = client.get(f"/operations/{op_id}").json()
    assert op["status"] == "done"
    assert op["result"]["slope"] == pytest.approx(-0.0962)


def test_unknown_operation_is_404(client):
    assert client.get("/operations/deadbeef").status_code == 404


# --------------------------------------------------------------------------
# Data and diagnostics
# --------------------------------------------------------------------------

def test_waveform_reports_staleness(client):
    client.get("/waveform")
    time.sleep(0.6)
    body = client.get("/waveform").json()
    assert len(body["scan_data"]) == 500
    assert body["age_s"] is not None


def test_waveform_polling_does_not_grow_the_queue(client):
    """A stalled firmware used to accumulate a burst of R commands."""
    for _ in range(30):
        client.get("/waveform")
    assert client.get("/status").json()["link"]["queue"]["size"] <= 16


def test_backoff_channel_validation(client):
    assert client.post("/backoff/reset/slower").status_code == 200
    assert client.post("/backoff/reset/purple").status_code == 422


def test_spc_argument_validation(client):
    assert client.post("/SPC/-1.5/1.5/25").status_code == 202
    assert client.post("/SPC/1.5/-1.5/25").status_code == 422
    assert client.post("/SPC/-1.5/1.5/0").status_code == 422


def test_status_exposes_parse_failure_rate(client):
    parsing = client.get("/status").json()["link"]["parsing"]
    assert "failure_rate" in parsing and parsing["failure_rate"] == 0.0


def test_metrics_endpoint(client):
    body = client.get("/metrics").json()
    assert "rates_per_hour" in body


def test_log_endpoint_and_acknowledge(client):
    body = client.get("/log/recent").json()
    assert "entries" in body and "sticky" in body
    assert client.post("/log/acknowledge", json={"text": None}).status_code == 200


# --------------------------------------------------------------------------
# Pre-v4 firmware
# --------------------------------------------------------------------------

@pytest.fixture
def v3_client(monkeypatch, tmp_path):
    monkeypatch.setenv("_TEST_FW_VERSION", "v3")
    from fastapi.testclient import TestClient

    from injection_monitor import api
    from injection_monitor import link as link_mod
    from injection_monitor.testing import FakeFirmware

    def fake_open(self):
        self._ser = FakeFirmware(version="v3")
        self._connect_count += 1
        self.connected.set()
        self._on_connect_change(True)

    monkeypatch.setattr(link_mod.SerialLink, "_open", fake_open)
    for k, v in {"IM_GPIO_ENABLED": "0", "IM_STATE_DIR": str(tmp_path),
                 "IM_LOG_DIR": str(tmp_path),
                 "IM_CAPABILITY_PROBE_TIMEOUT": "1.0"}.items():
        monkeypatch.setenv(k, v)
    with TestClient(api.app) as c:
        yield c


def test_v3_reports_legacy_capabilities(v3_client):
    caps = v3_client.get("/capabilities").json()
    assert caps["legacy_firmware"] is True
    assert caps["tags"] == ["core"]


def test_v3_returns_501_not_silence(v3_client):
    """The old host enqueued HC and reported 'queued'; nothing ever happened."""
    r = v3_client.post("/holdoff/calibrate")
    assert r.status_code == 501
    assert r.json()["detail"]["capability"] == "holdoff"


def test_v3_marks_unsupported_params(v3_client):
    params = v3_client.get("/state").json()["params"]
    assert params["holdoff_max_step"]["supported"] is False
    assert params["bump_thresh"]["supported"] is True
    assert v3_client.post("/params/holdoff_max_step",
                          json={"value": 30}).status_code == 501


def test_v3_legacy_blocks_still_parse(v3_client):
    v3_client.post("/init")
    time.sleep(1.0)
    stats = v3_client.get("/stats").json()
    assert stats["slower"]["height"] == pytest.approx(2412.55)
    assert stats["xbeam"]["std"] == pytest.approx(24.75)
