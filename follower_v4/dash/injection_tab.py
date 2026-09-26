#tabs/injection.py

from dash import html, dcc, Input, Output, State, ctx, MATCH
import dash
import dash_bootstrap_components as dbc
import dash_daq as daq
import numpy as np
import requests, time, json
from app import app
import plotly.graph_objs as go
from plotly.subplots import make_subplots
from globals import GLOBAL_INTERVAL_REFRESH_DICT, INJECTION_PI_URL, DIGILOCK_PI_URL, N

# --------------- INJECTION TAB -------------------

layout = dcc.Tab(label="Injection Follower", value="injection-control", children=[
        html.H3("Monitor and Feedback Controls"),
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
        html.Div([
            html.Label("Holdoff [us]:"),
            dcc.Input(id=f"holdoff-input", type="number", value=200, step=10),
            html.Button("Update Holdoff", id=f"update-holdoff-btn", n_clicks=0)
        ], style={"marginTop": 10}),
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
        dcc.Graph(id="spc-plot"),
        # --- SPC parameter table / changeable variables ---
        dbc.Row([
            dbc.Col([
                html.Div([
                    html.H5("SPC / Follower Parameters"),
                    html.Table(id="params-table", children=[
                        html.Thead(html.Tr([html.Th("Variable"), html.Th("Value"), html.Th("Action"), html.Th("Status")])),
                        html.Tbody([
                            html.Tr([
                                html.Td("unlock_thresh"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "unlock_thresh"}, type="number", value=0.95, step=0.01, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "unlock_thresh"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "unlock_thresh"}, children="idle"))
                            ]),
                            html.Tr([
                                html.Td("lost_thresh"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "lost_thresh"}, type="number", value=0.25, step=0.01, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "lost_thresh"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "lost_thresh"}, children="idle"))
                            ]),
                            html.Tr([
                                html.Td("bump_thresh"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "bump_thresh"}, type="number", value=0.05, step=0.01, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "bump_thresh"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "bump_thresh"}, children="idle"))
                            ]),
                            html.Tr([
                                html.Td("relock_thresh"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "relock_thresh"}, type="number", value=0.95, step=0.01, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "relock_thresh"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "relock_thresh"}, children="idle"))
                            ]),
                            html.Tr([
                                html.Td("std_thresh"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "std_thresh"}, type="number", value=2.0, step=0.1, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "std_thresh"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "std_thresh"}, children="idle"))
                            ]),
                            html.Tr([
                                html.Td("slower_sign"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "slower_sign"}, type="number", value=1, step=1, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "slower_sign"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "slower_sign"}, children="idle"))
                            ]),
                            html.Tr([
                                html.Td("xbeam_sign"),
                                html.Td(dcc.Input(id={"type": "C-input", "var": "xbeam_sign"}, type="number", value=-1, step=1, style={"width":"120px"})),
                                html.Td(html.Button("Change", id={"type": "C-btn", "var": "xbeam_sign"}, n_clicks=0)),
                                html.Td(html.Div(id={"type": "C-status", "var": "xbeam_sign"}, children="idle"))
                            ])
                        ])
                    ])
                ], style={"marginTop": 10})
            ], width=6),

            dbc.Col([
                html.H5("Status and Feedback"),
                html.Div([
                    # Replace the old Enable Feedback button with a mirrorable switch.
                    dbc.Switch(id=f"enable-fb-switch", value=False, label="Enable Feedback"),
                    html.Div(id=f"enable-fb-status", children="idle", style={"marginLeft": "12px", "display": "inline-block"})
                ], style={"marginTop": 10}),
                dbc.Row([
                    html.Div([
                        html.Label("Xbeam Peak Status:"),
                        daq.Indicator(id="xbeam-led", value=False, color="green", size=20),
                        html.Div([html.Label("Xbeam Feedback:"), dbc.Switch(id=f"xbeam-switch", value=False)], style={"display":"flex", "gap":"8px", "alignItems":"center", "marginTop":"8px"}),
                        html.Div([daq.Indicator(id="gpio-xbeam-led", color=GLOBAL_INTERVAL_REFRESH_DICT["GLOBAL_XBEAM_LED_COLOR"],
                                                    value=True, size=45), html.Span(" Xbeam GPIO")], style={"display":"flex", "gap":"6px", "alignItems":"center", "marginLeft": "12px"}),
                        
                    ], style={"display": "flex", "gap": "20px", "marginTop": 10})
                ]),
                dbc.Row([
                    # Show feedback-enabled indicators (single shared controls live in Central Control)
                    html.Div([
                        html.Label("Slow Peak Status:"),
                        daq.Indicator(id="slow-led", value=False, color="green", size=20),
                        # Mirror switches for central control (these are mirrors; central control is authoritative)
                        html.Div([html.Label("Slower Feedback:"), dbc.Switch(id=f"slower-switch", value=False)], style={"display":"flex", "gap":"8px", "alignItems":"center"}),
                        html.Div([daq.Indicator(id="gpio-slower-led", color=GLOBAL_INTERVAL_REFRESH_DICT["GLOBAL_SLOWER_LED_COLOR"],
                                                    value=True, size=45), html.Span(" Slower GPIO")], style={"display":"flex", "gap":"6px", "alignItems":"center", "marginLeft": "12px"}),

                        
                    ], style={"display": "flex", "gap": "20px", "marginTop": 10})
                ])
            ], width=6)
        ]),
        dcc.Interval(id=f"interval", interval=500, n_intervals=0),
        dcc.Store(id=f"peak-bands-store", data=None)
    ])

# --------------- CALLBACKS -----------------------
# --- Store for stats, clusters, and peaks ---
@app.callback(
    Output(f"peak-bands-store", "data"),
    Input(f"update-peaks-btn", "n_clicks"),
    Input(f"update-holdoff-btn", "n_clicks"),
    State(f"peak-bands-store", "data"),
    prevent_initial_call=False
)
def fetch_and_update_bands(update_peaks_clicks, update_holdoff_clicks, bands, pi_url=INJECTION_PI_URL):
    """
    Fetch and update stats, clusters, and tracking data for the given laser.
    Always ensures a consistent dict structure, and fetches missing fields if needed.
    """

    # --- Initialize dict safely ---
    if not isinstance(bands, dict):
        print("[DEBUG] No bands found!")
        bands = {"stats": None, "clusters": None, "tracking": None}

    # --- Refresh all data ---
    try:
        bands["stats"] = requests.get(f"{INJECTION_PI_URL}/stats", timeout=2).json()
        bands["clusters"] = requests.get(f"{INJECTION_PI_URL}/clusters", timeout=2).json()
    except Exception as e:
        print(f"[ERROR] Fetching stats/clusters failed: {e}")

    return bands

@app.callback(
    Input("update-peaks-btn", "n_clicks")
)
def post_init(n_clicks):
    if n_clicks and n_clicks > 0:
        try:
            requests.post(f"{INJECTION_PI_URL}/init", timeout=5)
        except Exception as e:
            print(f"[ERROR] Posting init from update peaks: {e}")

@app.callback(
    Output(f"plot", "figure"),
    Output(f"xbeam-led", "color"),
    Output(f"slow-led", "color"),
    Input(f"interval", "n_intervals"),
    State(f"peak-bands-store", "data")
)
def update_plot(n, bands, pi_url=INJECTION_PI_URL):

    # --- Fetch waveform ---
    try:
        # waveform endpoint is lightweight; it may return {'skipped': True, ...} when the server is busy
        r_waveform = requests.get(f"{pi_url}/waveform", timeout=2).json()
        # If the server indicated the request was skipped, we still get a snapshot to display
        if isinstance(r_waveform, dict) and r_waveform.get("skipped", False):
            # skipped: server didn't enqueue a new read; use returned snapshot
            y = np.array(r_waveform.get("scan_data", np.zeros(N)))
            tracking_data = r_waveform.get("tracking_data", {"slower": {}, "xbeam": {}})
        else:
            y = np.array(r_waveform.get("scan_data", np.zeros(N)))
            tracking_data = r_waveform.get("tracking_data", {"slower": {}, "xbeam": {}})
    except Exception as e:
        print(f"[ERROR] Fetching waveform failed: {e}")
        y = np.zeros(N)
        tracking_data = {"slower": {}, "xbeam": {}}

    # --- Update bands ---

    bands = fetch_and_update_bands(0,0,bands)

    # --- LED Status ---
    slow_ok = any(s.upper() == "OK" for s in tracking_data.get("slower", {}).get("status", []))
    xbeam_ok = any(s.upper() == "OK" for s in tracking_data.get("xbeam", {}).get("status", []))
    slow_color = "green" if slow_ok else "red"
    xbeam_color = "green" if xbeam_ok else "red"

    # --- Build figure ---
    fig = go.Figure()
    fig.add_trace(go.Scatter(y=y, mode="lines", name="Waveform",
                            line=dict(color="blue", width=2)))

    # --- Overlay peaks ---
    for beam_type, color in [("slower", "green"), ("xbeam", "orange")]:
        pos = tracking_data.get(beam_type, {}).get("pos", [])
        vals = tracking_data.get(beam_type, {}).get("val", [])
        if pos and vals:
            fig.add_trace(go.Scatter(
                x=pos, y=vals, mode="markers",
                name=f"{beam_type} peaks", marker=dict(color=color, size=6)
            ))

    # --- Overlay mean/std bands + clusters ---
    if isinstance(bands, dict):
        stats_dict = bands.get("stats", {})
        clusters_dict = bands.get("clusters", {})

        for beam_type, color in [("slower", "green"), ("xbeam", "orange")]:
            stats = stats_dict.get(beam_type, {})
            if stats:
                h = stats.get("height")
                std = stats.get("std")
                if h is not None and std is not None:
                    fig.add_hrect(y0=h - std, y1=h + std, fillcolor=color, opacity=0.2)

            for c in clusters_dict.get(beam_type, []):
                if c is not None:
                    x0 = max(0, c - 50)
                    x1 = min(N - 1, c + 50)
                    fig.add_vrect(x0=x0, x1=x1, fillcolor=color, opacity=0.15)
    else:
        print("[ERROR] No bands found to plot!")

    fig.update_layout(
        title=f"Injection Monitor",
        xaxis={"title": "Sample", "range": [0, N]},
        yaxis={"title": "ADC Value", "range": [0, 1750]},
        plot_bgcolor="#f5f5f5", paper_bgcolor="#ffffff"
    )

    return fig, xbeam_color, slow_color

@app.callback(
    Output(f"spc-plot", "figure"),
    Input(f"get-spc-btn", "n_clicks"),
    Input(f"update-spc-btn", "n_clicks"),
    State(f"spc-start", "value"),
    State(f"spc-stop", "value"),
    State(f"spc-step", "value"),
    prevent_initial_call=True
)
def get_or_update_spc_and_plot(get_n_clicks, update_n_clicks, startmA, stopmA, stepuA, pi_url=INJECTION_PI_URL):
    """
    Handles both 'Get SPC' and 'Update SPC' buttons.
    - If triggered by Get SPC: try to GET existing SPC and only POST to start a new SPC if none exists.
    - If triggered by Update SPC: always POST to start a fresh SPC and wait for completion, then fetch and plot.
    """
    empty_fig = go.Figure()

    # Determine which button triggered the callback
    triggered = ctx.triggered_id
    try:
        trigger_id = triggered if isinstance(triggered, str) else None
    except Exception:
        trigger_id = None

    # Helper to poll for SPC result after starting operation
    def poll_for_spc(timeout=60):
        wait_start = time.time()
        while time.time() - wait_start < timeout:
            try:
                s = requests.get(f"{pi_url}/status", timeout=3).json()
                if not s.get("operation_in_progress", False):
                    r2 = requests.get(f"{pi_url}/spc", timeout=3)
                    if r2.ok:
                        spc = r2.json()
                        if spc:
                            return spc
            except Exception:
                pass
            time.sleep(0.5)
        return None

    # If Update SPC button triggered -> force re-run
    if trigger_id == f"update-spc-btn":
        try:
            requests.post(f"{pi_url}/SPC/{float(startmA)}/{float(stopmA)}/{float(stepuA)}", timeout=5)
        except Exception as e:
            print(f"[ERROR] Failed to request SPC (update): {e}")
            return empty_fig

        spc = poll_for_spc()
        if not spc:
            print("[WARN] SPC update did not produce data in time")
            return empty_fig
        return build_spc_figure(spc)

    # If Get SPC button triggered -> try GET first, otherwise start one
    if trigger_id == f"get-spc-btn":
        try:
            r = requests.get(f"{pi_url}/spc", timeout=2)
            if r.ok:
                spc = r.json()
                if spc:
                    return build_spc_figure(spc)
        except Exception:
            pass

        # No existing SPC -> request one and poll
        try:
            requests.post(f"{pi_url}/SPC/{int(startmA)}/{int(stopmA)}/{int(stepuA)}", timeout=5)
        except Exception as e:
            print(f"[ERROR] Failed to request SPC: {e}")
            return empty_fig

        spc = poll_for_spc()
        if not spc:
            print("[WARN] SPC data not available after waiting")
            return empty_fig

        return build_spc_figure(spc)

    # Fallback
    return empty_fig

# ------------------ Parameter change callbacks (pattern-matching per-variable) ------------------
@app.callback(
    Output({"type": f"C-status", "var": MATCH}, "children"),
    Input({"type": f"C-btn", "var": MATCH}, "n_clicks"),
    State({"type": f"C-input", "var": MATCH}, "value"),
    prevent_initial_call=True
)
def change_param(n_clicks, value, pi_url=INJECTION_PI_URL):
    # MATCH will bind the 'var' key from the id; retrieve it from callback context
    try:
        triggered = ctx.triggered_id
        var_name = triggered.get("var") if isinstance(triggered, dict) else None
    except Exception:
        var_name = None

    if not var_name:
        return "error: unknown var"

    try:
        # POST to change variable; server may block until current operation finishes
        resp = requests.post(f"{pi_url}/C/{var_name}/{value}", timeout=35)
        resp.raise_for_status()
        try:
            data = resp.json()
        except Exception:
            data = {}

        op_cleared = data.get("operation_cleared", True) if isinstance(data, dict) else True
        if op_cleared:
            # show success with timestamp
            return f"OK @ {time.strftime('%H:%M:%S')}"
        else:
            return f"queued @ {time.strftime('%H:%M:%S')}"
    except Exception as e:
        print(f"[ERROR] Changing {var_name} failed: {e}")
        return f"error"

def build_spc_figure(spc):
    # spc is expected to be a dict with 'slower' and 'xbeam' entries
    fig = make_subplots(rows=2, cols=1, subplot_titles=("Slower", "Xbeam"))
    try:
        for row, key in enumerate(["slower", "xbeam"], start=1):
            data = spc.get(key, {})
            steps = data.get("steps", [])
            up = data.get("peaks", {}).get("up", [])
            down = data.get("peaks", {}).get("down", [])
            fb = data.get("fb", {})

            if steps:
                fig.add_trace(go.Scatter(x=steps, y=up, mode="lines+markers", name=f"{key} up"), row=row, col=1)
                fig.add_trace(go.Scatter(x=steps, y=down, mode="lines+markers", name=f"{key} down"), row=row, col=1)

            if fb:
                x0 = fb.get("x0", 0)
                y0 = fb.get("y0", 0)
                m = fb.get("m", 0)
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

@app.callback(
    Output(f"holdoff-input", "value"),
    Input(f"update-holdoff-btn", "n_clicks"),
    State(f"holdoff-input", "value"),
    State(f"peak-bands-store", "data")
)
def update_holdoff(n_clicks, value, bands, pi_url=INJECTION_PI_URL):
    if n_clicks and n_clicks > 0:
        try:
                # holdoff endpoint may block until current operation completes; allow a longer timeout
                resp = requests.post(f"{pi_url}/holdoff/{int(value)}", timeout=35)
                try:
                    data = resp.json()
                    # If the server indicates the operation didn't clear in time, log for debugging
                    if isinstance(data, dict) and not data.get("operation_cleared", True):
                        print(f"[WARN] holdoff enqueued but operation did not clear within timeout: {data}")
                except Exception:
                    pass
        except Exception as e:
            print(f"[ERROR] Updating holdoff failed: {e}")
    return value

@app.callback(
    Input(f"zero-slower-btn", "n_clicks")
)
def post_zero_slower(n_clicks):
    if n_clicks and n_clicks > 0:
        try:
            resp = requests.post(f"{INJECTION_PI_URL}/ZS")
            try:
                data = resp.json()
                if data.get("operation_cleared", True):
                    print("Zeroed slower!")
                else:
                    print("Zero slower failed! Response: ", data)
            except Exception as e:
                print(f"[ERROR] No json response found for zero slower: {e}")
        except Exception as e:
                print(f"[ERROR] Zero slower post failed: {e}")        

@app.callback(
    Input(f"zero-xbeam-btn", "n_clicks")
)
def post_zero_xbeam(n_clicks):
    if n_clicks and n_clicks > 0:
        try:
            resp = requests.post(f"{INJECTION_PI_URL}/ZX")
            try:
                data = resp.json()
                if data.get("operation_cleared", True):
                    print("Zeroed xbeams!")
                else:
                    print("Zero xbeam failed! Response: ", data)
            except Exception as e:
                print(f"[ERROR] No json response found for zero xbeam: {e}")
        except Exception as e:
                print(f"[ERROR] Zero xbeam post failed: {e}") 

@app.callback(
    Input(f"reset-btn", "n_clicks")
)
def post_zero(n_clicks):
    if n_clicks and n_clicks > 0:
        try:
            resp = requests.post(f"{INJECTION_PI_URL}/Z")
            try:
                data = resp.json()
                if data.get("operation_cleared", True):
                    print("Reset successful!")
                else:
                    print("Zero both failed! Response: ", data)
            except Exception as e:
                print(f"[ERROR] No json response found for zero both: {e}")
        except Exception as e:
                print(f"[ERROR] Zero both post failed: {e}") 

@app.callback(
    Output(f"enable-fb-status", "children"),
    Input(f"enable-fb-btn", "n_clicks"),
    prevent_initial_call=True
)
def enable_feedback(n_clicks, pi_url=INJECTION_PI_URL):
    if not n_clicks:
        return dash.no_update
    try:
        resp = requests.post(f"{pi_url}/FB", timeout=10)
        status_code = getattr(resp, 'status_code', None)
        # Try to get useful message from response
        body = None
        try:
            body = resp.json()
        except Exception:
            try:
                body = resp.text
            except Exception:
                body = None

        if status_code and 200 <= status_code < 300:
            return f"OK @ {time.strftime('%H:%M:%S')}"
        else:
            # Log detailed info and return a concise snippet to the UI
            print(f"[ERROR] Enable feedback returned {status_code}: {body}")
            body_snip = None
            if isinstance(body, str):
                body_snip = body[:200]
            elif isinstance(body, dict):
                try:
                    body_snip = json.dumps(body)[:200]
                except Exception:
                    body_snip = str(body)[:200]
            else:
                body_snip = str(body)[:200]

            return f"{status_code}: {body_snip}"
    except Exception as e:
        print(f"[ERROR] Enable feedback failed: {e}")
        return f"error: {e}"



# --- Start/Stop Stream ---
@app.callback(
    Output(f"interval", "disabled"),
    [Input(f"start-btn", "n_clicks"), Input(f"stop-btn", "n_clicks")]
)
def toggle_stream(start_clicks, stop_clicks):
    return stop_clicks > start_clicks

# --- Refresh Rate Slider ---
@app.callback(
    Output(f"interval", "interval"),
    Input(f"refresh-slider", "value")
)
def update_interval(value_hz):
    return int(1000 / value_hz)


