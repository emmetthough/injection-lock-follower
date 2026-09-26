"""Parser tests.

Several of these encode bugs found in injection_monitor_v2.py rather than
merely checking that the new code works; they are named accordingly so a
regression is recognisable.
"""

from __future__ import annotations

import json
import math

import pytest

from injection_monitor import legacy, protocol


# --------------------------------------------------------------------------
# New blocks
# --------------------------------------------------------------------------

CAL_BLOCK = """step_us,10
nsteps,2
START Points
220,-1.16
230,-2.12
240,-3.08
250,-4.04
260,-5.00
END Points
slope,-0.0962""".splitlines()


def test_holdoff_calibration_parses():
    out = protocol.parse_holdoff_calibration(CAL_BLOCK)
    assert out["ok"] is True
    assert out["step_us"] == 10 and out["nsteps"] == 2
    assert out["n_points"] == 5
    assert out["slope"] == pytest.approx(-0.0962)


def test_holdoff_calibration_derives_sample_interval():
    """-1/slope is the ADC sample interval, otherwise unmeasurable."""
    out = protocol.parse_holdoff_calibration(CAL_BLOCK)
    assert out["sample_interval_us"] == pytest.approx(10.395, abs=1e-3)
    # Residual is reported against the firmware's own slope, so it measures
    # how linear the sweep stayed -- the number that says whether to trust
    # the calibration at all.
    assert out["residual_rms"] < 0.01


def test_holdoff_calibration_zero_slope_is_failure():
    lines = [l.replace("slope,-0.0962", "slope,0") for l in CAL_BLOCK]
    out = protocol.parse_holdoff_calibration(lines)
    assert out["ok"] is False
    assert out["sample_interval_us"] is None


def test_warning_interleaved_in_points_is_skipped():
    lines = list(CAL_BLOCK)
    lines.insert(5, "[WARN] holdoff calibration: cluster went lost, n=3")
    out = protocol.parse_holdoff_calibration(lines)
    assert out["n_points"] == 5  # the warning has commas but is not a point


def test_holdoff_status_nan_becomes_none():
    """NaN is not valid JSON; JSON.parse rejects it and the Dash fetch fails."""
    lines = "enabled,1\nslope,-0.1\ndelayus,240\ncal_step,10\ndeadband,12.5\nerror,nan".splitlines()
    out = protocol.parse_holdoff_status(lines)
    assert out["error_samples"] is None
    json.dumps(out)  # must not emit a bare NaN token


def test_backoff_status_derives_headroom():
    lines = """nominal,0.95
floor,0.70
slower,0.910,4,0,1,0
xbeam,0.950,0,0,0,0""".splitlines()
    out = protocol.parse_backoff_status(lines)
    assert out["channels"]["slower"]["backed_off"] is True
    assert out["channels"]["slower"]["headroom_frac"] == pytest.approx(0.84)
    assert out["channels"]["xbeam"]["backed_off"] is False


def test_backoff_status_short_row_raises():
    lines = "nominal,0.95\nfloor,0.70\nslower,0.910,4".splitlines()
    with pytest.raises(protocol.BlockParseError):
        protocol.parse_backoff_status(lines)


# --------------------------------------------------------------------------
# Line tags
# --------------------------------------------------------------------------

def test_holdoff_event_line_parses():
    t = protocol.classify_line("[HOLDOFF] err=3.4 samples, delayus 240 -> 227")
    assert t.event == {"error_samples": 3.4, "delayus_old": 240, "delayus_new": 227}


def test_sticky_warning_flagged():
    t = protocol.classify_line("[WARN] slower unstable at the back-off floor")
    assert t.sticky is True
    assert protocol.classify_line("[WARN] unknown command: QQ").sticky is False


def test_unknown_tag_is_kept_not_dropped():
    """A future firmware tag must stay visible without a host change."""
    t = protocol.classify_line("[TELEMETRY] rate=48.2")
    assert t is not None and t.level == "INFO"


def test_block_name_accepts_the_firmware_misspelling():
    assert protocol.block_name("[START] Initalization") == "initialization"
    assert protocol.block_name("[START] Initialization") == "initialization"


# --------------------------------------------------------------------------
# Legacy blocks, transcribed from injection_follower_v3.ino
# --------------------------------------------------------------------------

def test_stats_survives_interleaved_warning():
    """The old parser indexed lines[1] and lines[3]; one [WARN] shifted both."""
    lines = ["BEGIN_stats",
             "meanHeight=2412.55 stdHeight=31.20",
             "[WARN] scan trigger timeout",
             "BEGIN_lowStats",
             "meanHeight=1980.10 stdHeight=24.75",
             "END_stats"]
    out = legacy.parse_stats(lines)
    assert out["slower"]["height"] == pytest.approx(2412.55)
    assert out["xbeam"]["std"] == pytest.approx(24.75)


def test_tracking_keeps_the_last_entry():
    """Regression: BEGIN_tracking has no END_tracking marker.

    The old host sliced lines[1:-1] uniformly, which is right for
    BEGIN_peaks/END_peaks but drops the final real entry here. Low entries are
    printed last, so the casualty was always an xbeam cluster -- and with one
    low cluster the xbeam vanished from the tracking plot entirely.
    """
    lines = ["BEGIN_tracking",
             "High 0 pos=12 val=2345 status=OK",
             "Low 0 pos=310 val=1975 status=LOST"]
    out = legacy.parse_tracking(lines)
    assert out["xbeam"]["n_tracked"] == 1
    assert out["xbeam"]["status"] == ["LOST"]
    assert out["xbeam"]["n_lost"] == 1


def test_peaks_and_tracking_field_orders_differ():
    """sendPeaks writes 'val= pos=' with a colon; printPeakStatus 'pos= val='."""
    p = legacy.parse_peaks(["High 0: val=2412 pos=56"])
    assert p["slower"] == {"vals": [2412], "pos": [56], "idx": [0]}
    t = legacy.parse_tracking(["High 0 pos=56 val=2412 status=OK"])
    assert t["slower"]["pos"] == [56] and t["slower"]["val"] == [2412]


def test_clusters_parse():
    out = legacy.parse_clusters(["High cluster 0 meanPos = 123.45",
                                 "Low cluster 0 meanPos = 301.20"])
    assert out["slower"] == [pytest.approx(123.45)]
    assert out["xbeam"] == [pytest.approx(301.20)]


def test_inner_marker_identifies_content_not_outer_name():
    """[START] Initalization contains BEGIN_peaks and closes as [END] Peaks."""
    assert legacy.inner_marker(["BEGIN_peaks", "High 0: val=1 pos=2"]) == "peaks"
    assert legacy.inner_marker(["[WARN] noise", "BEGIN_stats"]) == "stats"
    assert legacy.inner_marker(["something else"]) is None


SPC_BLOCK = """BEGIN_Slower
Slower size = 3
1,2,3,
10,20,30,
11,21,31,
END_Slower
BEGIN_Xbeam
Xbeam size = 3
4,5,6,
40,50,60,
41,51,61,
END Xbeam
START Slopes
-3.43,2048.00,2400.00
3.43,2050.00,2380.00
END Slopes""".splitlines()


def test_spc_parses_trailing_comma_rows():
    out = legacy.parse_spc(SPC_BLOCK)
    assert out["slower"]["steps"] == [1, 2, 3]
    assert out["slower"]["fb"]["m"] == pytest.approx(-3.43)
    assert out["xbeam"]["fb"]["m"] == pytest.approx(3.43)
    assert out["slower"]["consistent"] is True


def test_spc_flags_length_disagreement():
    lines = [l.replace("10,20,30,", "10,20,") for l in SPC_BLOCK]
    assert legacy.parse_spc(lines)["slower"]["consistent"] is False


def test_spc_missing_rows_raises():
    with pytest.raises(protocol.BlockParseError):
        legacy.parse_spc(["BEGIN_Slower", "Slower size = 3", "1,2,3,"])
