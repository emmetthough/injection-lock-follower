#tabs/injection.py

from dash import html, dcc, Input, Output, State, ctx, ALL, MATCH
import dash
import dash_bootstrap_components as dbc
import dash_daq as daq
import numpy as np
import requests, time
from app import app
import plotly.graph_objs as go
from plotly.subplots import make_subplots
from globals import GLOBAL_INTERVAL_REFRESH_DICT, INJECTION_PI_URL, DIGILOCK_PI_URL, N

# =====================================================================
# Parameter registry
#
# This mirrors injection_monitor/params.py's SPECS table on the host,
# by hand, on purpose. The host is the single source of truth for
# *values*; this table exists so the tab can render every parameter's
# name, description, range and grouping without a network round trip at
# import time -- if the Pi is unreachable when Dash starts, the panel
# still renders with correct labels, just with default values until the
# person clicks "Sync Variables".
#
# If you add or change a parameter on the host (injection_monitor/params.py
# SPECS), mirror the change here. Nothing enforces the two staying in
# sync automatically; the host is authoritative for validation -- a
# stale min/max here is cosmetic, not a safety issue, since the host
# still rejects an out-of-range POST regardless of what this table says.
# =====================================================================

GROUP_LABELS = {
    "thresholds": "Thresholds",
    "search": "Search & Relock",
    "geometry": "Geometry (fb_sign)",
    "acquisition": "Acquisition",
    "bias": "Safe-Side Bias",
    "backoff": "Instability Back-off",
    "holdoff": "Holdoff Drift Servo",
}
# Display order for the accordion.
GROUP_ORDER = ["thresholds", "search", "geometry", "acquisition", "bias",
               "backoff", "holdoff"]

# (name, kind, default, min, max, unit, group, doc, confirmable, capability)
PARAM_SPECS = [
    ("unlock_thresh", "float", 0.95, 0.30, 1.50, "fraction", "thresholds",
     "Nominal acting threshold (losing_meanThresh).", True, "core"),
    ("lost_thresh", "float", 0.25, 0.01, 0.99, "fraction", "thresholds",
     "Lost threshold. Not affected by back-off.", False, "core"),
    ("bump_thresh", "float", 0.05, 0.005, 0.50, "fraction of FWHM", "search",
     "Bump step as a fraction of FWHM.", False, "core"),
    ("relock_thresh", "float", 0.95, 0.10, 2.00, "gain", "search",
     "Gain on the relock extrapolation.", False, "core"),
    ("std_thresh", "float", 2.0, 0.50, 20.0, "x refStd", "thresholds",
     "Running-std trigger, as a multiple of refStd.", False, "core"),
    ("slower_sign", "sign", 1, -1, 1, "", "geometry",
     "fb_sign for the slower. Points at the gentle flank.", False, "core"),
    ("xbeam_sign", "sign", -1, -1, 1, "", "geometry",
     "fb_sign for the xbeam. Points at the gentle flank.", False, "core"),

    ("scan_timeout_ms", "int", 500, 50, 10000, "ms", "acquisition",
     "Acquisition watchdog. Raise if the ramp is slower than 2 Hz.", False,
     "scan_timeout"),

    ("slower_bias", "bool", 1, 0, 1, "", "bias",
     "Enable the fixed safe-side offset on the slower.", True, "backoff"),
    ("xbeam_bias", "bool", 0, 0, 1, "", "bias",
     "Enable the fixed safe-side offset on the xbeam.", True, "backoff"),
    ("slower_contbump", "bool", 0, 0, 1, "", "bias",
     "Continuous safe-side bump, slower. Costs an extra scan per iteration.",
     True, "backoff"),
    ("xbeam_contbump", "bool", 0, 0, 1, "", "bias",
     "Continuous safe-side bump, xbeam. Costs an extra scan per iteration.",
     True, "backoff"),
    ("bias_frac", "float", 0.25, 0.0, 1.0, "fraction of FWHM", "bias",
     "Fixed offset as a fraction of FWHM.", False, "backoff"),

    ("instab_window_ms", "int", 60000, 1000, 3600000, "ms", "backoff",
     "Rolling window for counting instability events.", False, "backoff"),
    ("instab_events", "int", 3, 1, 100, "events", "backoff",
     "Events in window that trigger a back-off.", False, "backoff"),
    ("backoff_step", "float", 0.02, 0.001, 0.50, "fraction", "backoff",
     "Threshold drop per trigger.", False, "backoff"),
    ("backoff_floor", "float", 0.70, 0.10, 1.00, "fraction", "backoff",
     "Hard minimum; latches a fault.", True, "backoff"),
    ("backoff_quiet_ms", "int", 600000, 1000, 86400000, "ms", "backoff",
     "Quiet time before creeping back.", False, "backoff"),
    ("backoff_creep", "float", 0.005, 0.0001, 0.50, "fraction", "backoff",
     "Increase per quiet interval.", False, "backoff"),

    ("holdoff_slope", "float", 0.0, -100.0, 100.0, "samples/us", "holdoff",
     "Samples per us, negative. Set by HC, or restored by the host.", True,
     "holdoff"),
    ("holdoff_cal_step", "int", 10, 1, 1000, "us", "holdoff",
     "Microseconds per calibration step.", True, "holdoff"),
    ("holdoff_cal_frac", "float", 0.5, 0.05, 1.0, "x SEARCH_WINDOW", "holdoff",
     "Calibrate until displacement reaches this x SEARCH_WINDOW.", False,
     "holdoff"),
    ("holdoff_actuate_frac", "float", 0.5, 0.05, 2.0, "x SEARCH_WINDOW",
     "holdoff", "Actuate above this x SEARCH_WINDOW.", False, "holdoff"),
    ("holdoff_max_step", "int", 20, 1, 1000, "us", "holdoff",
     "Clamp on one correction.", False, "holdoff"),
    ("holdoff_interval_ms", "int", 60000, 1000, 86400000, "ms", "holdoff",
     "Minimum time between corrections.", False, "holdoff"),
]

PARAM_BY_NAME = {p[0]: p for p in PARAM_SPECS}
PARAM_STEP = {
    "float": lambda lo, hi: max((hi - lo) / 200.0, 0.001),
    "int": lambda lo, hi: 1,
    "sign": lambda lo, hi: 2,
    "bool": lambda lo, hi: 1,
}


def _fetch_json(path, timeout=3, method="get", **kw):
    """GET/POST to the Pi, returning (data, error_string_or_None).

    Every call site in the previous version of this file had its own
    try/except with slightly different logging; centralising it means a
    network blip degrades the same way everywhere instead of each
    callback inventing its own partial handling.
    """
    try:
        fn = requests.get if method == "get" else requests.post
        r = fn(f"{INJECTION_PI_URL}{path}", timeout=timeout, **kw)
        try:
            return r.json(), None
        except Exception:
            return None, f"non-JSON response ({r.status_code})"
    except Exception as e:
        return None, str(e)


# =====================================================================
# Parameter panel
# =====================================================================

def _param_row(spec):
    (name, kind, default, lo, hi, unit, group, doc, confirmable, cap) = spec
    step = PARAM_STEP[kind](lo, hi)
    unit_suffix = f" {unit}" if unit else ""
    range_text = "range: -1 or +1" if kind == "sign" else \
        f"range: {lo:g}{unit_suffix} .. {hi:g}{unit_suffix}"
    return html.Tr([
        html.Td([
            html.Div(name, style={"fontFamily": "monospace", "fontWeight": 600}),
            html.Div(doc, className="text-muted",
                     style={"fontSize": "12px", "maxWidth": "280px"}),
            html.Div(range_text, className="text-muted", style={"fontSize": "11px"}),
        ], style={"verticalAlign": "top", "paddingTop": "6px"}),
        html.Td(
            dcc.Input(
                id={"type": "C-input", "var": name}, type="number",
                value=default, step=step, min=lo, max=hi,
                style={"width": "110px"},
            ),
            style={"verticalAlign": "top", "paddingTop": "6px"},
        ),
        html.Td(
            html.Button("Set", id={"type": "C-btn", "var": name}, n_clicks=0,
                        className="btn btn-sm btn-outline-primary"),
            style={"verticalAlign": "top", "paddingTop": "6px"},
        ),
        html.Td(
            dbc.Badge("idle", id={"type": "C-badge", "var": name},
                      color="secondary", className="me-1"),
            style={"verticalAlign": "top", "paddingTop": "6px"},
        ),
    ], id={"type": "C-row", "var": name})


def _param_group_table(group_key):
    rows = [_param_row(p) for p in PARAM_SPECS if p[6] == group_key]
    return html.Table(
        [html.Thead(html.Tr([
            html.Th("Variable"), html.Th("Value"), html.Th(""), html.Th("Status"),
        ])), html.Tbody(rows)],
        style={"width": "100%"},
    )


def _param_accordion():
    items = [dbc.AccordionItem(_param_group_table(g), title=GROUP_LABELS[g],
                                item_id=g)
             for g in GROUP_ORDER]
    return dbc.Accordion(items, start_collapsed=True, always_open=True,
                          id="param-accordion")


PARAMETERS_PANEL = html.Div([
    dbc.Button(
        "Show Parameters \u25be", id="param-panel-toggle", n_clicks=0,
        color="secondary", outline=True, size="sm",
        style={"marginTop": "10px"},
    ),
    dbc.Collapse(
        html.Div([
            dbc.Alert(id="param-capability-alert", is_open=False,
                      color="warning", style={"marginTop": "10px"}),
            html.Div([
                dbc.Button("Sync Variables (pull from device)",
                           id="param-sync-btn", n_clicks=0, size="sm",
                           color="primary", style={"marginRight": "8px"}),
                dbc.Button("Push All to Firmware", id="param-push-btn",
                           n_clicks=0, size="sm", color="secondary",
                           outline=True, style={"marginRight": "8px"}),
                dbc.Button("Reset to Defaults", id="param-reset-btn",
                           n_clicks=0, size="sm", color="danger", outline=True),
                html.Span(id="param-sync-status", className="text-muted",
                          style={"marginLeft": "12px", "fontSize": "12px"}),
            ], style={"marginTop": "10px", "marginBottom": "6px"}),
            html.Div([
                dbc.Badge("confirmed", color="success", className="me-1"),
                dbc.Badge("pending", color="warning", className="me-1"),
                dbc.Badge("unsupported", color="secondary", className="me-1"),
                dbc.Badge("error", color="danger"),
                html.Span(" -- status legend", className="text-muted",
                          style={"fontSize": "11px", "marginLeft": "6px"}),
            ], style={"marginBottom": "8px"}),
            _param_accordion(),
        ]),
        id="param-panel-collapse", is_open=False,
    ),
], style={"marginTop": 10})


# =====================================================================
# Holdoff panel
# =====================================================================

HOLDOFF_PANEL = html.Div([
    html.H5("Holdoff Drift Servo", style={"marginTop": 16}),
    dbc.Alert(id="holdoff-capability-alert", is_open=False, color="warning"),
    html.Div([
        html.Label("Holdoff [us]:"),
        dcc.Input(id="holdoff-input", type="number", value=200, step=10,
                  style={"width": "100px", "marginRight": "8px"}),
        dbc.Button("Set (no reinit)", id="holdoff-set-btn", n_clicks=0,
                   size="sm", color="primary", style={"marginRight": "6px"}),
        dbc.Button("Set + Reinitialize", id="holdoff-set-reinit-btn",
                   n_clicks=0, size="sm", color="warning", outline=True,
                   style={"marginRight": "6px"},
                   title="Also runs 130 scans and resets refHeight/refStd. "
                         "Manual use only -- never call this from automation."),
        dbc.Button("Calibrate", id="holdoff-calibrate-btn", n_clicks=0,
                   size="sm", color="info"),
    ], style={"marginTop": 10, "display": "flex", "alignItems": "center",
              "gap": "4px"}),
    html.Div([
        dbc.Switch(id="holdoff-enable-switch", value=False,
                   label="Holdoff Feedback Enabled"),
    ], style={"marginTop": 10}),
    html.Div(id="holdoff-status-line", className="text-muted",
             style={"marginTop": 6, "fontSize": "13px"}),
    dcc.Store(id="holdoff-op-store", data=None),
    dcc.Interval(id="holdoff-poll-interval", interval=1500, n_intervals=0,
                 disabled=True),
], style={"marginTop": 10})


# =====================================================================
# Back-off panel
# =====================================================================

def _backoff_channel_card(channel, label):
    return dbc.Col([
        html.H6(label),
        dbc.Progress(id=f"backoff-{channel}-bar", value=100, color="success",
                     style={"height": "16px", "marginBottom": 4}),
        html.Div(id=f"backoff-{channel}-text", className="text-muted",
                 style={"fontSize": "12px"}),
        dbc.Badge("nominal", id=f"backoff-{channel}-latch",
                  color="success", style={"marginTop": 6}),
        html.Div(
            dbc.Button(f"Reset {label}", id=f"backoff-reset-{channel}-btn",
                       n_clicks=0, size="sm", color="secondary", outline=True,
                       style={"marginTop": 6}),
        ),
    ], width=6)


BACKOFF_PANEL = html.Div([
    html.H5("Instability Back-off", style={"marginTop": 16}),
    dbc.Alert(id="backoff-capability-alert", is_open=False, color="warning"),
    dbc.Row([
        _backoff_channel_card("slower", "Slower"),
        _backoff_channel_card("xbeam", "Xbeam"),
    ]),
    dbc.Button("Reset Both", id="backoff-reset-both-btn", n_clicks=0,
               size="sm", color="danger", outline=True,
               style={"marginTop": 8}),
], style={"marginTop": 10})


# =====================================================================
# Alerts / faults panel
# =====================================================================

ALERTS_PANEL = html.Div([
    html.Div([
        html.H5("Alerts", style={"display": "inline-block", "marginRight": 10}),
        dbc.Badge(id="alerts-count-badge", children="0", color="secondary"),
    ]),
    html.Div(id="alerts-list", style={"marginTop": 6}),
    dbc.Button("Acknowledge All", id="alerts-ack-btn", n_clicks=0, size="sm",
               color="secondary", outline=True, style={"marginTop": 6}),
], style={"marginTop": 16})


# =====================================================================
# Link / connection status strip
# =====================================================================

CONNECTION_STRIP = html.Div([
    daq.Indicator(id="link-connected-led", value=False, color="red", size=14),
    html.Span(" link", style={"marginRight": 14}),
    html.Span(id="link-age-text", className="text-muted",
              style={"marginRight": 14, "fontSize": "12px"}),
    html.Span(id="link-fw-text", className="text-muted", style={"fontSize": "12px"}),
], style={"display": "flex", "alignItems": "center", "gap": "4px",
          "marginTop": 8, "marginBottom": 4})


# =====================================================================
# Metrics panel (relock/recover rate)
# =====================================================================

METRICS_PANEL = html.Div([
    html.H5("Relock Activity (24h)", style={"marginTop": 16}),
    dcc.Graph(id="metrics-graph", style={"height": "220px"}),
], style={"marginTop": 10})


# --------------- INJECTION TAB -------------------

layout = dcc.Tab(label="Injection Follower", value="injection-control", children=[
        html.H3("Monitor and Feedback Controls"),
        CONNECTION_STRIP,
        dcc.Graph(id="plot"),
        html.Div([
            html.Label("Scope Refresh Rate [Hz]:"),
            dcc.Slider(
                id=f"refresh-slider",
                min=0.5, max=2, step=0.1, value=1.5,
                marks={0.5:"0.5",1:"1",1.5:"1.5",2:"2"}
            )
        ], style={"marginTop": 10}),
        html.Div([
            html.Button("Start Stream", id="start-btn", n_clicks=0),
            html.Button("Stop Stream", id="stop-btn", n_clicks=0)
        ], style={"marginTop": 10}),
        html.Div([
            html.Button("Zero Slower", id="zero-slower-btn", n_clicks=0),
            html.Button("Zero Xeams", id="zero-xbeam-btn", n_clicks=0),
            html.Button("RESET", id="reset-btn", n_clicks=0),
        ], style={"marginTop": 10}),
        html.Div([html.Button("Update Peaks", id="update-peaks-btn", n_clicks=0)], style={"marginTop": 10}),

        HOLDOFF_PANEL,
        BACKOFF_PANEL,

        html.Div([
            html.Label("SPC Start (mA):"),
            dcc.Input(id="spc-start", type="number", value=-1.5, style={"width":"80px", "marginRight":"8px"}),
            html.Label("Stop (mA):"),
            dcc.Input(id="spc-stop", type="number", value=1.5, style={"width":"80px", "marginRight":"8px"}),
            html.Label("Step (uA):"),
            dcc.Input(id="spc-step", type="number", value=25, style={"width":"80px", "marginRight":"8px"}),
            html.Button("Get SPC", id="get-spc-btn", n_clicks=0),
            html.Button("Update SPC", id="update-spc-btn", n_clicks=0, style={"marginLeft": "8px"})
        ], style={"marginTop": 10}),
        html.Div(id="spc-status-text", className="text-muted",
                 style={"marginTop": 4, "fontSize": "13px"}),
        dcc.Graph(id="spc-plot"),
        dcc.Store(id="spc-op-store", data=None),
        dcc.Interval(id="spc-poll-interval", interval=1500, n_intervals=0,
                     disabled=True),

        PARAMETERS_PANEL,
        METRICS_PANEL,
        ALERTS_PANEL,

        dbc.Row([
            dbc.Col([
                html.H5("Status and Feedback", style={"marginTop": 16}),
                html.Div([
                    dbc.Switch(id="enable-fb-switch", value=False, label="Enable Feedback"),
                    html.Div(id="enable-fb-status", children="idle", style={"marginLeft": "12px", "display": "inline-block"})
                ], style={"marginTop": 10}),
                dbc.Row([
                    html.Div([
                        html.Label("Xbeam Peak Status:"),
                        daq.Indicator(id="xbeam-led", value=False, color="green", size=20),
                        html.Div([html.Label("Xbeam Feedback:"), dbc.Switch(id="xbeam-switch", value=False, disabled=True)], style={"display":"flex", "gap":"8px", "alignItems":"center", "marginTop":"8px"}),
                        html.Div([daq.Indicator(id="gpio-xbeam-led", color=GLOBAL_INTERVAL_REFRESH_DICT["GLOBAL_XBEAM_LED_COLOR"],
                                                    value=True, size=45), html.Span(" Xbeam GPIO")], style={"display":"flex", "gap":"6px", "alignItems":"center", "marginLeft": "12px"}),

                    ], style={"display": "flex", "gap": "20px", "marginTop": 10})
                ]),
                dbc.Row([
                    html.Div([
                        html.Label("Slow Peak Status:"),
                        daq.Indicator(id="slow-led", value=False, color="green", size=20),
                        html.Div([html.Label("Slower Feedback:"), dbc.Switch(id="slower-switch", value=False, disabled=True)], style={"display":"flex", "gap":"8px", "alignItems":"center"}),
                        html.Div([daq.Indicator(id="gpio-slower-led", color=GLOBAL_INTERVAL_REFRESH_DICT["GLOBAL_SLOWER_LED_COLOR"],
                                                    value=True, size=45), html.Span(" Slower GPIO")], style={"display":"flex", "gap":"6px", "alignItems":"center", "marginLeft": "12px"}),


                    ], style={"display": "flex", "gap": "20px", "marginTop": 10})
                ])
            ], width=12)
        ]),
        dcc.Interval(id="interval", interval=500, n_intervals=0),
        dcc.Interval(id="slow-poll-interval", interval=5000, n_intervals=0),
        dcc.Store(id="peak-bands-store", data=None),
        dcc.Store(id="state-store", data=None),
    ])

# =====================================================================
# CALLBACKS
# =====================================================================

# --- Store for stats, clusters, and peaks (unchanged from before) ---
@app.callback(
    Output("peak-bands-store", "data"),
    Input("update-peaks-btn", "n_clicks"),
    Input("holdoff-set-btn", "n_clicks"),
    Input("holdoff-set-reinit-btn", "n_clicks"),
    State("peak-bands-store", "data"),
    prevent_initial_call=False
)
def fetch_and_update_bands(update_peaks_clicks, holdoff_set_clicks,
                            holdoff_reinit_clicks, bands):
    """Fetch and update stats/clusters. Structure is always a dict."""
    if not isinstance(bands, dict):
        bands = {"stats": None, "clusters": None, "tracking": None}
    stats, err = _fetch_json("/stats")
    if err:
        print(f"[ERROR] Fetching stats failed: {err}")
    else:
        bands["stats"] = stats
    clusters, err = _fetch_json("/clusters")
    if err:
        print(f"[ERROR] Fetching clusters failed: {err}")
    else:
        bands["clusters"] = clusters
    return bands


@app.callback(
    Input("update-peaks-btn", "n_clicks")
)
def post_init(n_clicks):
    if n_clicks and n_clicks > 0:
        _fetch_json("/init", timeout=5, method="post")


@app.callback(
    Output("plot", "figure"),
    Output("xbeam-led", "color"),
    Output("slow-led", "color"),
    Output("link-connected-led", "value"),
    Output("link-connected-led", "color"),
    Output("link-age-text", "children"),
    Input("interval", "n_intervals"),
    State("peak-bands-store", "data")
)
def update_plot(n, bands):
    # --- Fetch waveform ---
    waveform, err = _fetch_json("/waveform")
    if err or not isinstance(waveform, dict):
        print(f"[ERROR] Fetching waveform failed: {err}")
        y = np.zeros(N)
        tracking_data = {"slower": {}, "xbeam": {}}
        connected = False
        age_s = None
    else:
        y = np.array(waveform.get("scan_data", np.zeros(N)))
        tracking_data = waveform.get("tracking_data", {"slower": {}, "xbeam": {}})
        connected = bool(waveform.get("connected", False))
        age_s = waveform.get("age_s")

    bands = fetch_and_update_bands(0, 0, 0, bands)

    slow_ok = any(s.upper() == "OK" for s in tracking_data.get("slower", {}).get("status", []))
    xbeam_ok = any(s.upper() == "OK" for s in tracking_data.get("xbeam", {}).get("status", []))
    slow_color = "green" if slow_ok else "red"
    xbeam_color = "green" if xbeam_ok else "red"
    link_color = "green" if connected else "red"
    age_text = f"waveform age: {age_s:.2f}s" if isinstance(age_s, (int, float)) else "waveform age: --"

    fig = go.Figure()
    fig.add_trace(go.Scatter(y=y, mode="lines", name="Waveform",
                            line=dict(color="blue", width=2)))

    for beam_type, color in [("slower", "green"), ("xbeam", "orange")]:
        pos = tracking_data.get(beam_type, {}).get("pos", [])
        vals = tracking_data.get(beam_type, {}).get("val", [])
        if pos and vals:
            fig.add_trace(go.Scatter(
                x=pos, y=vals, mode="markers",
                name=f"{beam_type} peaks", marker=dict(color=color, size=6)
            ))

    if isinstance(bands, dict):
        stats_dict = bands.get("stats") or {}
        clusters_dict = bands.get("clusters") or {}

        for beam_type, color in [("slower", "green"), ("xbeam", "orange")]:
            stats = stats_dict.get(beam_type, {}) or {}
            h = stats.get("height")
            std = stats.get("std")
            if h is not None and std is not None:
                fig.add_hrect(y0=h - std, y1=h + std, fillcolor=color, opacity=0.2)

            for c in (clusters_dict.get(beam_type, []) or []):
                if c is not None:
                    x0 = max(0, c - 50)
                    x1 = min(N - 1, c + 50)
                    fig.add_vrect(x0=x0, x1=x1, fillcolor=color, opacity=0.15)

    fig.update_layout(
        title="Injection Monitor",
        xaxis={"title": "Sample", "range": [0, N]},
        yaxis={"title": "ADC Value", "range": [0, 1750]},
        plot_bgcolor="#f5f5f5", paper_bgcolor="#ffffff"
    )

    return fig, xbeam_color, slow_color, connected, link_color, age_text


@app.callback(
    Output("spc-plot", "figure"),
    Output("spc-op-store", "data"),
    Output("spc-poll-interval", "disabled"),
    Output("spc-status-text", "children"),
    Input("get-spc-btn", "n_clicks"),
    Input("update-spc-btn", "n_clicks"),
    State("spc-start", "value"),
    State("spc-stop", "value"),
    State("spc-step", "value"),
    prevent_initial_call=True
)
def start_or_fetch_spc(get_n_clicks, update_n_clicks, startmA, stopmA, stepuA):
    """Starts an SPC run and returns immediately -- it does not wait for it.

    /SPC/{...} is long-running (the host doc puts a full run at up to
    ~13s of blocked firmware time, longer if a recovery sweep runs mid-scan)
    and is async on the host: it returns 202 + an operation_id right away.
    The previous version of this callback then blocked *this* callback for
    up to 90s polling that operation from inside a single HTTP request --
    which is exactly the failure mode being fixed. Any reverse proxy or the
    browser's own fetch timeout sitting in front of Dash (commonly 30-60s)
    tears the connection down before Dash ever gets to respond, so the
    button appears to time out even though the SPC itself may finish fine
    a few seconds later.

    This callback only ever does quick, bounded work (a few seconds at
    most) and hands off waiting to spc-poll-interval below, which polls in
    its own short callback every 1.5s. The plot updates itself the moment
    the operation completes -- no second click on "Get SPC" required.
    """
    figure = dash.no_update
    triggered = ctx.triggered_id
    trigger_id = triggered if isinstance(triggered, str) else None

    def start_run():
        data, err = _fetch_json(
            f"/SPC/{float(startmA)}/{float(stopmA)}/{float(stepuA)}",
            timeout=5, method="post",
        )
        if err:
            return None, f"failed to start SPC: {err[:80]}"
        op_id = (data or {}).get("operation_id")
        if not op_id:
            return None, "SPC start did not return an operation id"
        return {"op_id": op_id, "started": time.time()}, "SPC running..."

    if trigger_id == "update-spc-btn":
        store, status = start_run()
        return figure, store, store is None, status

    if trigger_id == "get-spc-btn":
        spc, err = _fetch_json("/spc", timeout=2)
        if not err and spc:
            return build_spc_figure(spc), None, True, \
                f"loaded cached SPC @ {time.strftime('%H:%M:%S')}"
        store, status = start_run()
        return figure, store, store is None, \
            ("no cached SPC on the host -- " + status) if store else status

    return figure, dash.no_update, dash.no_update, dash.no_update


@app.callback(
    Output("spc-plot", "figure", allow_duplicate=True),
    Output("spc-op-store", "data", allow_duplicate=True),
    Output("spc-poll-interval", "disabled", allow_duplicate=True),
    Output("spc-status-text", "children", allow_duplicate=True),
    Input("spc-poll-interval", "n_intervals"),
    State("spc-op-store", "data"),
    prevent_initial_call=True,
)
def poll_spc_operation(n_intervals, store):
    """The other half of the split above: a short, cheap poll every 1.5s.

    Each tick is its own quick request-response, so nothing ever holds a
    connection open for the duration of the SPC run -- the fix for the
    timeout. Gives up client-side after 3 minutes of polling so a lost or
    stuck operation doesn't spin the interval forever; the host's own
    operation TTL (timeout_spc, default 180s) would have marked it
    timed_out by then regardless.
    """
    if not store or not store.get("op_id"):
        return dash.no_update, dash.no_update, True, dash.no_update

    if time.time() - store.get("started", 0) > 180:
        return dash.no_update, None, True, "SPC polling gave up after 3 minutes"

    op, err = _fetch_json(f"/operations/{store['op_id']}", timeout=3)
    if err:
        # A transient blip mid-poll shouldn't abort the wait; keep going.
        return dash.no_update, dash.no_update, False, "SPC running... (poll blip)"

    status = (op or {}).get("status")
    if status == "done":
        result = (op or {}).get("result")
        if not result:
            return dash.no_update, None, True, "SPC finished with no data"
        return (build_spc_figure(result), None, True,
                f"SPC updated @ {time.strftime('%H:%M:%S')}")
    if status in ("failed", "timed_out"):
        return dash.no_update, None, True, f"SPC {status}"

    elapsed = int(time.time() - store.get("started", time.time()))
    return dash.no_update, dash.no_update, False, f"SPC running... ({elapsed}s)"


def build_spc_figure(spc):
    fig = make_subplots(rows=2, cols=1, subplot_titles=("Slower", "Xbeam"))
    try:
        for row, key in enumerate(["slower", "xbeam"], start=1):
            data = spc.get(key, {}) or {}
            steps = data.get("steps", [])
            up = data.get("peaks", {}).get("up", [])
            down = data.get("peaks", {}).get("down", [])
            fb = data.get("fb", {}) or {}

            if steps:
                fig.add_trace(go.Scatter(x=steps, y=up, mode="lines+markers", name=f"{key} up"), row=row, col=1)
                fig.add_trace(go.Scatter(x=steps, y=down, mode="lines+markers", name=f"{key} down"), row=row, col=1)

            if fb and fb.get("m") is not None:
                x0 = fb.get("x0", 0) or 0
                y0 = fb.get("y0", 0) or 0
                m = fb.get("m", 0) or 0
                if key == "slower":
                    x_line = [x0, x0 + 300]
                    y_line = [y0, y0 + m * 300]
                else:
                    x_line = [x0, x0 - 300]
                    y_line = [y0, y0 - m * 300]
                fig.add_trace(go.Scatter(x=x_line, y=y_line, mode="lines", line=dict(width=4), name=f"{key} slope"), row=row, col=1)

            fig.update_xaxes(title_text="DAC Out [bit]", row=row, col=1)
            fig.update_yaxes(title_text="Peak height [bit]", row=row, col=1)
    except Exception as e:
        print("[ERROR] Building SPC figure:", e)

    fig.update_layout(height=700)
    return fig

# =====================================================================
# Parameter panel callbacks
# =====================================================================

@app.callback(
    Output("param-panel-collapse", "is_open"),
    Output("param-panel-toggle", "children"),
    Input("param-panel-toggle", "n_clicks"),
    State("param-panel-collapse", "is_open"),
    prevent_initial_call=True,
)
def toggle_param_panel(n_clicks, is_open):
    new_state = not is_open
    label = ("Hide Parameters \u25b4" if new_state else "Show Parameters \u25be")
    return new_state, label


@app.callback(
    Output({"type": "C-input", "var": ALL}, "value"),
    Output({"type": "C-badge", "var": ALL}, "children"),
    Output({"type": "C-badge", "var": ALL}, "color"),
    Output({"type": "C-input", "var": ALL}, "disabled"),
    Output("param-sync-status", "children"),
    Output("param-capability-alert", "children"),
    Output("param-capability-alert", "is_open"),
    Output("state-store", "data"),
    Input("param-sync-btn", "n_clicks"),
    prevent_initial_call=True,
)
def sync_params_from_device(n_clicks):
    """The requested 'sync variables' button.

    Pulls GET /state -- the single source of truth on the host -- and
    overwrites every parameter input with the host's current value. This
    only runs on an explicit click, never on a timer, so it can never
    clobber a value someone is mid-way through typing.

    Uses dash.callback_context.outputs_list to discover which 'var' each
    matched output corresponds to, rather than assuming the ALL wildcard's
    order matches PARAM_SPECS -- Dash does not guarantee that ordering is
    stable as the panel grows, and getting it wrong here would silently
    write one parameter's value into another's box.
    """
    state, err = _fetch_json("/state", timeout=5)
    outputs_list = ctx.outputs_list  # [inputs_list, badges_children_list, badges_color_list, disabled_list, ...]
    input_ids = [o["id"]["var"] for o in outputs_list[0]]
    badge_ids = [o["id"]["var"] for o in outputs_list[1]]
    disabled_ids = [o["id"]["var"] for o in outputs_list[3]]

    if err or not isinstance(state, dict):
        msg = f"sync failed: {err or 'bad response'}"
        no_values = [dash.no_update] * len(input_ids)
        no_badges = [dash.no_update] * len(badge_ids)
        no_colors = [dash.no_update] * len(badge_ids)
        no_disabled = [dash.no_update] * len(disabled_ids)
        return (no_values, no_badges, no_colors, no_disabled, msg,
                "Could not reach the host.", True, dash.no_update)

    params = state.get("params", {})
    caps = set((state.get("capabilities") or {}).get("tags", []))

    values = []
    for var in input_ids:
        p = params.get(var)
        values.append(p["value"] if p else dash.no_update)

    badge_text, badge_color = [], []
    for var in badge_ids:
        p = params.get(var)
        if not p:
            badge_text.append("?"); badge_color.append("secondary"); continue
        if not p.get("supported", True):
            badge_text.append("unsupported"); badge_color.append("secondary")
        elif p.get("confirmed"):
            badge_text.append("confirmed"); badge_color.append("success")
        elif p.get("pending"):
            badge_text.append("pending"); badge_color.append("warning")
        else:
            badge_text.append("default"); badge_color.append("secondary")

    disabled = []
    for var in disabled_ids:
        p = params.get(var)
        disabled.append(not p.get("supported", True) if p else False)

    legacy_fw = (state.get("capabilities") or {}).get("legacy_firmware", False)
    alert_text = (
        "This firmware predates the holdoff/back-off features (pre-v4). "
        "Those parameters are shown disabled." if legacy_fw else ""
    )

    ts = time.strftime("%H:%M:%S")
    return (values, badge_text, badge_color, disabled,
            f"synced @ {ts}", alert_text, bool(alert_text), state)


@app.callback(
    Output("param-sync-status", "children", allow_duplicate=True),
    Input("param-push-btn", "n_clicks"),
    prevent_initial_call=True,
)
def push_all_params(n_clicks):
    """POST /params/sync -- forces a replay of every stored parameter to
    the firmware. Normally unnecessary (the host replays automatically on
    a detected firmware restart); this is the manual escape hatch for when
    that heuristic didn't fire, e.g. a board that rebooted before its
    first calibration.
    """
    data, err = _fetch_json("/params/sync", timeout=15, method="post")
    ts = time.strftime("%H:%M:%S")
    if err:
        return f"push failed @ {ts}: {err}"
    count = (data or {}).get("count", "?")
    return f"pushed {count} parameters @ {ts}"


@app.callback(
    Output("param-sync-status", "children", allow_duplicate=True),
    Input("param-reset-btn", "n_clicks"),
    prevent_initial_call=True,
)
def reset_all_params(n_clicks):
    data, err = _fetch_json("/params/reset", timeout=15, method="post")
    ts = time.strftime("%H:%M:%S")
    if err:
        return f"reset failed @ {ts}: {err}"
    return f"reset to defaults @ {ts} -- click Sync Variables to refresh the panel"


@app.callback(
    Output({"type": "C-badge", "var": MATCH}, "children", allow_duplicate=True),
    Output({"type": "C-badge", "var": MATCH}, "color", allow_duplicate=True),
    Input({"type": "C-btn", "var": MATCH}, "n_clicks"),
    State({"type": "C-input", "var": MATCH}, "value"),
    prevent_initial_call=True,
)
def change_param(n_clicks, value):
    """POST /params/{name} with a JSON body.

    The host returns immediately with pending: true (values are almost
    never confirmed synchronously -- 16 of 25 parameters are never echoed
    back by any firmware status block at all). The badge shows 'pending'
    until the next Sync Variables click reads back a 'confirmed' or
    'unsupported' state from /state.
    """
    var_name = ctx.triggered_id.get("var") if isinstance(ctx.triggered_id, dict) else None
    if not var_name:
        return "error", "danger"

    data, err = _fetch_json(f"/params/{var_name}", timeout=10, method="post",
                            json={"value": value})
    if err:
        return f"error: {err[:40]}", "danger"

    if isinstance(data, dict) and data.get("pending"):
        warnings = data.get("warnings") or []
        if warnings:
            print(f"[WARN] {var_name}: {'; '.join(warnings)}")
        return f"pending @ {time.strftime('%H:%M:%S')}", "warning"

    return f"sent @ {time.strftime('%H:%M:%S')}", "secondary"

# =====================================================================
# Holdoff callbacks
# =====================================================================

@app.callback(
    Output("holdoff-status-line", "children"),
    Output("holdoff-enable-switch", "value"),
    Output("holdoff-capability-alert", "children"),
    Output("holdoff-capability-alert", "is_open"),
    Input("slow-poll-interval", "n_intervals"),
    Input("holdoff-set-btn", "n_clicks"),
    Input("holdoff-set-reinit-btn", "n_clicks"),
    State("holdoff-input", "value"),
    State("holdoff-op-store", "data"),
    prevent_initial_call=False,
)
def poll_and_set_holdoff(n_intervals, set_clicks, set_reinit_clicks, us, cal_store):
    """Sets the holdoff on a button click, and refreshes the status line
    on the slow poll interval either way.

    HS (via /holdoff/set/{us}) is the automation-safe route -- it does
    not re-run initialize_peak_vals_locations(). D (via
    /holdoff/set_and_init/{us}) does, and resets refHeight/refStd, so it
    is a separate, clearly-labelled button rather than the default.

    While a calibration is in flight, this yields the status line to
    poll_holdoff_calibration (below), which ticks every 1.5s -- otherwise
    the two pollers would visibly overwrite each other's text every few
    seconds during a run.
    """
    triggered = ctx.triggered_id
    if triggered == "holdoff-set-btn" and us is not None:
        _, err = _fetch_json(f"/holdoff/set/{int(us)}", timeout=10, method="post")
        if err:
            print(f"[ERROR] holdoff set failed: {err}")
    elif triggered == "holdoff-set-reinit-btn" and us is not None:
        _, err = _fetch_json(f"/holdoff/set_and_init/{int(us)}", timeout=10,
                             method="post")
        if err:
            print(f"[ERROR] holdoff set+reinit failed: {err}")

    calibrating = bool(cal_store and cal_store.get("op_id"))

    status, err = _fetch_json("/holdoff/status", timeout=3)
    if err or not isinstance(status, dict):
        return (dash.no_update if calibrating else "holdoff status unavailable",
                dash.no_update, "", False)

    s = status.get("status") or {}
    if not s:
        # capability likely missing on this firmware
        return (dash.no_update if calibrating else "no holdoff status reported",
                dash.no_update,
                "This firmware does not report holdoff status (pre-v4?).", True)

    calibrated = s.get("calibrated")
    slope = s.get("slope")
    delayus = s.get("delayus")
    deadband = s.get("deadband_samples")
    error = s.get("error_samples")
    line = (
        f"delayus={delayus}  slope={slope}  deadband={deadband} samples  "
        f"error={'n/a (no drain yet)' if error is None else f'{error:.2f} samples'}  "
        f"{'CALIBRATED' if calibrated else 'NOT CALIBRATED'}"
    )
    return (dash.no_update if calibrating else line), bool(s.get("enabled")), "", False


@app.callback(
    Output("holdoff-op-store", "data"),
    Output("holdoff-poll-interval", "disabled"),
    Output("holdoff-status-line", "children", allow_duplicate=True),
    Input("holdoff-calibrate-btn", "n_clicks"),
    prevent_initial_call=True,
)
def start_holdoff_calibration(n_clicks):
    """Starts HC and returns immediately -- see start_or_fetch_spc's
    docstring for why blocking a callback on a long-running host operation
    causes the request to time out before a result ever arrives.
    holdoff-poll-interval (below) does the actual waiting, in short,
    independent ticks.
    """
    data, err = _fetch_json("/holdoff/calibrate", timeout=10, method="post")
    if err:
        return None, True, f"calibration request failed: {err[:80]}"
    op_id = (data or {}).get("operation_id")
    if not op_id:
        return None, True, "calibration did not return an operation id"
    return {"op_id": op_id, "started": time.time()}, False, "calibration running..."


@app.callback(
    Output("holdoff-op-store", "data", allow_duplicate=True),
    Output("holdoff-poll-interval", "disabled", allow_duplicate=True),
    Output("holdoff-status-line", "children", allow_duplicate=True),
    Input("holdoff-poll-interval", "n_intervals"),
    State("holdoff-op-store", "data"),
    prevent_initial_call=True,
)
def poll_holdoff_calibration(n_intervals, store):
    if not store or not store.get("op_id"):
        return dash.no_update, True, dash.no_update

    if time.time() - store.get("started", 0) > 180:
        return None, True, "calibration polling gave up after 3 minutes"

    op, err = _fetch_json(f"/operations/{store['op_id']}", timeout=3)
    if err:
        return dash.no_update, False, "calibration running... (poll blip)"

    status = (op or {}).get("status")
    if status == "done":
        r = (op or {}).get("result") or {}
        if r.get("ok"):
            line = (
                f"calibration OK: slope={r.get('slope'):.4f} samples/us, "
                f"sample interval={r.get('sample_interval_us'):.3f}us, "
                f"residual={r.get('residual_rms'):.3g}"
            )
        else:
            line = f"calibration FAILED: {r.get('failure_reason') or 'no reason reported'}"
        return None, True, line
    if status in ("failed", "timed_out"):
        return None, True, f"calibration {status}"

    elapsed = int(time.time() - store.get("started", time.time()))
    return dash.no_update, False, f"calibration running... ({elapsed}s)"


@app.callback(
    Output("holdoff-status-line", "children", allow_duplicate=True),
    Input("holdoff-enable-switch", "value"),
    prevent_initial_call=True,
)
def toggle_holdoff_feedback(value):
    data, err = _fetch_json("/holdoff/toggle", timeout=10, method="post")
    if err:
        return f"toggle failed: {err}"
    warning = (data or {}).get("warning")
    if warning:
        return f"toggled -- {warning}"
    return dash.no_update


# =====================================================================
# Back-off callbacks
# =====================================================================

@app.callback(
    Output("backoff-slower-bar", "value"),
    Output("backoff-slower-bar", "color"),
    Output("backoff-slower-text", "children"),
    Output("backoff-slower-latch", "children"),
    Output("backoff-slower-latch", "color"),
    Output("backoff-xbeam-bar", "value"),
    Output("backoff-xbeam-bar", "color"),
    Output("backoff-xbeam-text", "children"),
    Output("backoff-xbeam-latch", "children"),
    Output("backoff-xbeam-latch", "color"),
    Output("backoff-capability-alert", "children"),
    Output("backoff-capability-alert", "is_open"),
    Input("slow-poll-interval", "n_intervals"),
    Input("backoff-reset-both-btn", "n_clicks"),
    Input("backoff-reset-slower-btn", "n_clicks"),
    Input("backoff-reset-xbeam-btn", "n_clicks"),
)
def poll_and_reset_backoff(n_intervals, reset_both, reset_slower, reset_xbeam):
    triggered = ctx.triggered_id
    if triggered == "backoff-reset-both-btn":
        _fetch_json("/backoff/reset", timeout=10, method="post")
    elif triggered == "backoff-reset-slower-btn":
        _fetch_json("/backoff/reset/slower", timeout=10, method="post")
    elif triggered == "backoff-reset-xbeam-btn":
        _fetch_json("/backoff/reset/xbeam", timeout=10, method="post")

    status, err = _fetch_json("/backoff/status", timeout=3)
    empty = (100, "secondary", "no data", "unknown", "secondary")
    if err or not isinstance(status, dict) or not status.get("status"):
        return (*empty, *empty, "This firmware does not report back-off status "
                                 "(pre-v4?).", True)

    s = status["status"]
    channels = s.get("channels", {})

    def channel_view(ch):
        c = channels.get(ch)
        if not c:
            return empty
        frac = c.get("headroom_frac")
        pct = max(0, min(100, round((frac or 0) * 100)))
        color = "success" if pct > 60 else ("warning" if pct > 20 else "danger")
        text = (f"acting thresh {c.get('losing_thresh')}  "
                f"events={c.get('event_count')}  "
                f"bias={'on' if c.get('bias_enabled') else 'off'}  "
                f"contbump={'on' if c.get('contbump_enabled') else 'off'}")
        latch_text = "FLOOR LATCHED" if c.get("floor_latched") else "nominal"
        latch_color = "danger" if c.get("floor_latched") else "success"
        return pct, color, text, latch_text, latch_color

    return (*channel_view("slower"), *channel_view("xbeam"), "", False)


# =====================================================================
# Alerts panel callbacks
# =====================================================================

@app.callback(
    Output("alerts-list", "children"),
    Output("alerts-count-badge", "children"),
    Output("alerts-count-badge", "color"),
    Input("slow-poll-interval", "n_intervals"),
    Input("alerts-ack-btn", "n_clicks"),
)
def poll_alerts(n_intervals, ack_clicks):
    if ctx.triggered_id == "alerts-ack-btn":
        _fetch_json("/log/acknowledge", timeout=5, method="post", json={"text": None})

    data, err = _fetch_json("/log/recent", timeout=3)
    if err or not isinstance(data, dict):
        return [html.Div("alerts unavailable", className="text-muted")], "?", "secondary"

    sticky = data.get("sticky") or []
    if not sticky:
        return [html.Div("no active faults", className="text-muted")], "0", "success"

    items = [
        dbc.Alert(
            f"{s.get('text', '')} (x{s.get('count', 1)}, since "
            f"{time.strftime('%H:%M:%S', time.localtime(s.get('first', time.time())))})",
            color="danger", style={"padding": "6px 10px", "marginBottom": "4px"},
        )
        for s in sticky
    ]
    return items, str(len(sticky)), "danger"


# =====================================================================
# Metrics callback
# =====================================================================

@app.callback(
    Output("metrics-graph", "figure"),
    Input("slow-poll-interval", "n_intervals"),
)
def poll_metrics(n_intervals):
    data, err = _fetch_json("/metrics", timeout=3, method="get")
    fig = go.Figure()
    if err or not isinstance(data, dict):
        fig.update_layout(title="Relock activity (unavailable)", height=220)
        return fig

    rates = data.get("rates_per_hour", {}) or {}
    kinds_of_interest = ["relock_called", "recover_called", "lost", "losing"]
    channels = ["slower", "xbeam"]
    for ch, color in zip(channels, ["#2ca02c", "#ff7f0e"]):
        y = [rates.get(k, {}).get(ch, 0) for k in kinds_of_interest]
        fig.add_trace(go.Bar(x=kinds_of_interest, y=y, name=ch, marker_color=color))

    fig.update_layout(
        barmode="group", height=220, margin=dict(t=30, b=30, l=40, r=10),
        yaxis_title="events/hour",
        title=f"n_events(24h)={data.get('n_events', 0)}",
    )
    return fig

# =====================================================================
# Core commands (zero, reset, feedback)
# =====================================================================

@app.callback(
    Input("zero-slower-btn", "n_clicks")
)
def post_zero_slower(n_clicks):
    if n_clicks and n_clicks > 0:
        data, err = _fetch_json("/ZS", timeout=10, method="post")
        if err:
            print(f"[ERROR] Zero slower post failed: {err}")
        else:
            print("Zeroed slower!" if data else "Zero slower: no response body")


@app.callback(
    Input("zero-xbeam-btn", "n_clicks")
)
def post_zero_xbeam(n_clicks):
    if n_clicks and n_clicks > 0:
        data, err = _fetch_json("/ZX", timeout=10, method="post")
        if err:
            print(f"[ERROR] Zero xbeam post failed: {err}")
        else:
            print("Zeroed xbeams!" if data else "Zero xbeam: no response body")


@app.callback(
    Input("reset-btn", "n_clicks")
)
def post_zero(n_clicks):
    if n_clicks and n_clicks > 0:
        data, err = _fetch_json("/Z", timeout=10, method="post")
        if err:
            print(f"[ERROR] Zero both post failed: {err}")
        else:
            print("Reset successful!" if data else "Zero both: no response body")


@app.callback(
    Output("enable-fb-status", "children"),
    Input("enable-fb-switch", "value"),
    prevent_initial_call=True,
)
def enable_feedback(value):
    """Fixed: the previous version of this callback listened for
    'enable-fb-btn', a button that does not exist in this layout (it was
    replaced by the 'enable-fb-switch' toggle at some point and the
    callback was never updated) -- so this never fired at all. FB toggles
    feedback on the firmware regardless of which direction the switch was
    flipped; there is currently no host-reported field that says which
    state feedback is actually in, so this switch is a fire-and-forget
    toggle, not a true mirror. If the firmware ever reports FB state in a
    block, wire this switch's 'value' to poll it the same way
    holdoff-enable-switch does.
    """
    data, err = _fetch_json("/FB", timeout=10, method="post")
    if err:
        return f"error: {err[:60]}"
    return f"toggled @ {time.strftime('%H:%M:%S')}"


# --- Start/Stop Stream ---
@app.callback(
    Output("interval", "disabled"),
    Input("start-btn", "n_clicks"), Input("stop-btn", "n_clicks"),
)
def toggle_stream(start_clicks, stop_clicks):
    return (stop_clicks or 0) > (start_clicks or 0)


# --- Refresh Rate Slider ---
@app.callback(
    Output("interval", "interval"),
    Input("refresh-slider", "value")
)
def update_interval(value_hz):
    return int(1000 / value_hz)
