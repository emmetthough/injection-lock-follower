"""Parameter store tests."""

from __future__ import annotations

import json

import pytest

from injection_monitor import params
from injection_monitor.params import BACKOFF, CORE, HOLDOFF, ParameterStore, ValidationError


def test_defaults_match_the_firmware_source():
    """Transcribed from injection_follower_v3.ino lines 40-66."""
    assert params.SPEC_BY_NAME["unlock_thresh"].default == 0.95
    assert params.SPEC_BY_NAME["lost_thresh"].default == 0.25
    assert params.SPEC_BY_NAME["bump_thresh"].default == 0.05
    assert params.SPEC_BY_NAME["relock_thresh"].default == 0.95
    # The change summary lists std_thresh as "-"; the firmware says 2.0.
    assert params.SPEC_BY_NAME["std_thresh"].default == 2.0
    assert params.SPEC_BY_NAME["slower_sign"].default == 1
    assert params.SPEC_BY_NAME["xbeam_sign"].default == -1


@pytest.mark.parametrize("name,value", [
    ("backoff_floor", 2.0),        # above 1.0; the firmware accepts it
    ("slower_sign", 0),            # fb_sign 0 takes getSlope's early return
    ("instab_events", 2.5),        # must be whole
    ("bump_thresh", -0.1),
    ("holdoff_interval_ms", 0),
])
def test_out_of_range_rejected(store, name, value):
    with pytest.raises(ValidationError):
        store.set(name, value)


def test_nonfinite_rejected(store):
    with pytest.raises(ValidationError):
        store.set("bias_frac", float("nan"))


def test_unknown_parameter_rejected(store):
    with pytest.raises(ValidationError):
        store.set("nonexistent", 1)


def test_cross_check_warns_without_blocking(store):
    """Warnings, not errors: some legitimate edit orders transiently violate."""
    _, warnings = store.set("unlock_thresh", 0.40)   # below backoff_floor 0.70
    assert any("backoff_floor" in w for w in warnings)
    assert store.value("unlock_thresh") == 0.40      # still applied


def test_positive_holdoff_slope_warns(store):
    """Increasing the holdoff makes peaks appear earlier: slope is negative."""
    _, warnings = store.set("holdoff_slope", 0.05)
    assert any("wrong way" in w for w in warnings)


def test_persistence_round_trip(cfg, store):
    store.set("holdoff_slope", -0.0962)
    store.set("backoff_floor", 0.65)
    reloaded = ParameterStore(cfg.params_file)
    assert reloaded.load() is True
    assert reloaded.value("holdoff_slope") == pytest.approx(-0.0962)
    assert reloaded.value("backoff_floor") == pytest.approx(0.65)


def test_restored_values_are_not_confirmed(cfg, store):
    """Nothing is trusted until the firmware says so."""
    store.set("holdoff_slope", -0.0962)
    reloaded = ParameterStore(cfg.params_file)
    reloaded.load()
    assert reloaded.snapshot()["params"]["holdoff_slope"]["confirmed"] is False


def test_corrupt_file_falls_back_to_defaults(cfg):
    cfg.params_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.params_file.write_text("{not json")
    s = ParameterStore(cfg.params_file)
    assert s.load() is False
    assert s.value("unlock_thresh") == 0.95


def test_stale_file_with_bad_value_keeps_default(cfg):
    cfg.params_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.params_file.write_text(json.dumps({
        "schema": 1,
        "params": {"backoff_floor": {"value": 99.0},
                   "removed_param": {"value": 1.0}},
    }))
    s = ParameterStore(cfg.params_file)
    s.load()
    assert s.value("backoff_floor") == 0.70


def test_firmware_wins_on_disagreement(store):
    store.set("backoff_floor", 0.65)
    assert store.confirm("backoff_floor", 0.70) is True
    assert store.value("backoff_floor") == pytest.approx(0.70)


def test_replay_is_capability_filtered(store):
    """v3 recognises seven C names; the rest fall off the end silently."""
    core_only = store.replay_commands({CORE})
    assert len(core_only) == 7
    assert not any("holdoff" in c for c in core_only)
    full = store.replay_commands({CORE, HOLDOFF, BACKOFF, params.SCAN_TIMEOUT})
    assert len(full) == len(params.SPECS)


def test_wire_format_avoids_exponent_notation(store):
    """String::toFloat() reads '1e-05' as 1, silently setting 1e5 times too much."""
    store.set("backoff_creep", 0.0001)
    line = [c for c in store.replay_commands({CORE, BACKOFF})
            if c.startswith("Cbackoff_creep")][0]
    value = line.split(",", 1)[1]
    assert "e" not in value.lower()
    assert value == "0.0001"


def test_integers_are_sent_without_decimals(store):
    line = [c for c in store.replay_commands({CORE}) if "slower_sign" in c][0]
    assert line == "Cslower_sign,1"


def test_mark_unconfirmed_clears_runtime(store):
    store.set_runtime("holdoff_delayus", 240)
    store.confirm("unlock_thresh", 0.95)
    store.mark_unconfirmed()
    assert store.runtime()["holdoff_delayus"] is None
    assert store.snapshot()["params"]["unlock_thresh"]["confirmed"] is False
