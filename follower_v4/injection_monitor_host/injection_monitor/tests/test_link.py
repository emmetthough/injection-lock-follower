"""Serial link tests.

The interesting cases are the failure modes: a firmware that answers nothing, a
dropped terminator, a trace that runs short, and a board that rebooted while the
host was away.
"""

from __future__ import annotations

import time

import pytest

from injection_monitor.link import CapabilityError
from injection_monitor.tests.conftest import build_link


# --------------------------------------------------------------------------
# Capability probe
# --------------------------------------------------------------------------

def test_v4_probe_finds_everything(cfg, store):
    link, *_ = build_link(cfg, store, version="v4")
    link._after_connect()
    assert link.caps.holdoff and link.caps.backoff
    assert link.caps.legacy_firmware is False
    assert link.ready.is_set()


def test_v3_probe_degrades_without_hanging(cfg, store):
    """v3 falls off the end of loop() and answers nothing at all."""
    link, *_ = build_link(cfg, store, version="v3")
    started = time.monotonic()
    link._after_connect()
    assert time.monotonic() - started < 5.0
    assert link.caps.legacy_firmware is True
    assert link.caps.tags() == {"core"}


def test_unsupported_capability_raises(cfg, store):
    link, *_ = build_link(cfg, store, version="v3")
    link._after_connect()
    with pytest.raises(CapabilityError) as e:
        link.send("HC", capability="holdoff")
    assert e.value.capability == "holdoff"


def test_capability_gate_refuses_before_probe(cfg, store):
    """Provisional capabilities must not produce a spurious refusal."""
    link, *_ = build_link(cfg, store, version="v4")
    with pytest.raises(CapabilityError):
        link.send("HC", capability="holdoff")


# --------------------------------------------------------------------------
# Reset detection and replay
# --------------------------------------------------------------------------

def test_rebooted_board_is_detected_and_replayed(cfg, store):
    """slope is 0 at boot and has no flash to survive in."""
    store.set("holdoff_slope", -0.0962)
    link, device, *_ = build_link(cfg, store, version="v4")
    link._after_connect()
    assert "Choldoff_slope,-0.0962" in device.received
    assert device.state["holdoff_slope"] == pytest.approx(-0.0962)


def test_replay_does_not_lose_host_values_to_firmware_defaults(cfg, store):
    """Regression: confirming before deciding overwrote the store with defaults.

    The firmware reports compiled-in defaults after a reboot. Adopting them
    first and replaying second pushed those defaults straight back, silently
    discarding every value the operator had set.
    """
    store.set("holdoff_slope", -0.0962)
    store.set("backoff_floor", 0.65)          # firmware default is 0.70
    link, device, *_ = build_link(cfg, store, version="v4")
    link._after_connect()
    assert store.value("backoff_floor") == pytest.approx(0.65)
    assert device.state["backoff_floor"] == pytest.approx(0.65)


def test_running_board_is_adopted_not_overwritten(cfg, store):
    """A host restart must not perturb a board that has been locked for a week."""
    link, device, *_ = build_link(cfg, store, version="v4")
    device.state["holdoff_slope"] = -0.0962
    link._after_connect()
    assert not any(c.startswith("Choldoff_slope") for c in device.received)
    assert store.value("holdoff_slope") == pytest.approx(-0.0962)


def test_no_init_sent_on_connect(cfg, store):
    """D and I reset refHeight; the board does not reboot when we open the port."""
    link, device, *_ = build_link(cfg, store, version="v4")
    link._after_connect()
    assert "I" not in device.received


def test_boot_banner_triggers_mid_session_replay(cfg, store):
    store.set("holdoff_slope", -0.0962)
    link, device, *_ = build_link(cfg, store, version="v4")
    link._after_connect()
    before = sum(c.startswith("Choldoff_slope") for c in device.received)
    device.state["holdoff_slope"] = 0.0
    link._handle_line("[INFO] boot fw=v4.0 N=500")
    after = sum(c.startswith("Choldoff_slope") for c in device.received)
    assert after == before + 1
    assert device.state["holdoff_slope"] == pytest.approx(-0.0962)


def test_replay_policy_never(cfg, store):
    store.set("holdoff_slope", -0.0962)
    link, device, *_ = build_link(
        cfg.model_copy(update={"replay_policy": "never"}), store, version="v4")
    link._after_connect()
    assert not any(c.startswith("Choldoff_slope") for c in device.received)


# --------------------------------------------------------------------------
# Block reading
# --------------------------------------------------------------------------

def test_full_round_trip(linked):
    link, device, blocks, *_ = linked
    link.send("I")
    link._service_queue()
    link._pump(1.0)
    link.send("R")
    link._service_queue()
    link._pump(1.0)
    assert {"peaks", "stats", "clusters", "tracking", "scan"} <= set(blocks)
    assert len(blocks["scan"]) == link.cfg.trace_samples


def test_dropped_terminator_times_out(cfg, store):
    """The old read_until looped forever and held the busy flag while doing it."""
    link, device, *_ = build_link(cfg, store, version="v4", drop_terminator=True)
    link.send("I")
    link._service_queue()
    started = time.monotonic()
    link._pump(4.0)
    elapsed = time.monotonic() - started
    assert elapsed < cfg.timeout_init + 1.0
    assert link.busy.is_set() is False
    assert link.parse_stats.truncated >= 1


def test_short_trace_is_detected(cfg, store):
    """Length alone cannot catch it: the next block's text pads it back out.

    sendTrace writes no terminator, so a 497-sample trace plus six bytes of
    '[START' still totals the expected 1000. The 12-bit invariant catches it.
    """
    link, device, blocks, *_ = build_link(cfg, store, version="v4",
                                          short_trace=True)
    link.send("R")
    link._service_queue()
    link._pump(2.0)
    assert "scan" not in blocks
    assert link.parse_stats.truncated == 1


def test_unknown_block_is_discarded_not_fatal(linked):
    """Firmware newer than this host must never crash it."""
    link, device, blocks, *_ = linked
    with device._lock:
        device._p("[START] Quantum Telemetry")
        device._p("qubits,7")
        device._p("[END] Quantum Telemetry")
    link._pump(1.0)
    assert link.parse_stats.unknown.get("quantum telemetry") == 1


def test_calibration_failure_carries_the_warning(cfg, store):
    """slope,0 is the failure signal; the reason is in the accompanying [WARN]."""
    link, device, blocks, _, events = build_link(cfg, store, version="v4",
                                                 fail_calibration=True)
    link._after_connect()
    link.send("HC", capability="holdoff")
    link._service_queue()
    link._pump(1.5)
    cal = blocks["holdoff_calibration"]
    assert cal["ok"] is False
    assert "rail" in cal["failure_reason"]


def test_successful_calibration_stores_the_slope(linked):
    link, device, blocks, *_ = linked
    link.send("HC", capability="holdoff")
    link._service_queue()
    link._pump(1.5)
    assert blocks["holdoff_calibration"]["ok"] is True
    assert link.store.value("holdoff_slope") == pytest.approx(-0.0962)


def test_unsolicited_backoff_block_is_handled(linked):
    """Emitted on any bias-related C, without having been requested."""
    link, device, blocks, *_ = linked
    blocks.pop("backoff_status", None)
    link.send("Cslower_bias,0")
    link._service_queue()
    link._pump(1.0)
    assert blocks["backoff_status"]["channels"]["slower"]["bias_enabled"] is False


# --------------------------------------------------------------------------
# Command queue
# --------------------------------------------------------------------------

def test_waveform_requests_coalesce(cfg, store):
    link, *_ = build_link(cfg, store)
    for _ in range(50):
        link.queue.put("R", coalesce_key="R")
    assert len(link.queue) == 1
    assert link.queue.coalesced == 49


def test_queue_is_bounded(cfg, store):
    link, *_ = build_link(cfg, store)
    for i in range(100):
        link.queue.put(f"Cbump_thresh,{i / 1000}")
    assert len(link.queue) <= cfg.command_queue_size
    assert link.queue.dropped > 0


def test_safety_commands_bypass_the_bound_and_go_first(cfg, store):
    link, *_ = build_link(cfg, store)
    for i in range(100):
        link.queue.put(f"Cbump_thresh,{i / 1000}")
    accepted, _ = link.queue.put("ZS")
    assert accepted is True
    assert link.queue.get().text == "ZS"


def test_overlong_command_refused(linked):
    """The firmware buffer is 64 bytes; longer lines are discarded with a WARN."""
    link, device, *_ = linked
    link.send("C" + "x" * 100 + ",1")
    link._service_queue()
    assert not any(len(c) > 64 for c in device.received)
