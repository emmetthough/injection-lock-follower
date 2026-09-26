import dash
from dash import html, dcc, Input, Output, State, MATCH, ctx
import dash_daq as daq
import dash_bootstrap_components as dbc
import visdcc
import requests, numpy as np
from scipy.signal import find_peaks
import plotly.graph_objs as go
from plotly.subplots import make_subplots
import subprocess, threading, queue
import signal
import json, os
import time

# ---------------- CONFIG ----------------
INJECTION_PI_URL = "http://10.19.33.12:8000"
LASERS = [{"name": "Injection Monitor", "pi_url": INJECTION_PI_URL}] # currenly just used for the injection monitor

N = 500
CALIBRATION_SWEEPS = 20
SLOWER_LOWER, SLOWER_UPPER = 1000, 2000
XBEAM_LOWER, XBEAM_UPPER = 500, 1000
GRAFANA_URL = "http://10.155.94.101:3000/public-dashboards/eb5da49884b045c194abb81284e53aae?orgId=1&kiosk=tv"
VIMBA_VENV_PATH = r"D:\Data\mako_env\Scripts\python.exe"
STATE_FILE = r"D:\Data\dash_state.json"

GLOBAL_INTERVAL_REFRESH_DICT = {"digilock-b-locked": False,
                                "digilock-b-happy": False,
                                "digilock-g-locked": False,
                                "digilock-g-happy": False,
                                "digilock-cur-active": False
                                }

# ---------------- digilock CONFIG ----------------
DIGILOCK_PI_URL = "http://10.18.3.176:8000"
AUTORANGE_COUNT = 0 


# ---------------- DASH APP ----------------
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP], suppress_callback_exceptions=True)
app.title = "B055 Laser Dashboard"

# ---------------- HELPER FUNCTIONS ----------------
def detect_peaks(y, height=180, prominence=5, smoothing=True, window=5):
    if smoothing:
        y = np.convolve(y, np.ones(window) / window, mode='same')
    peaks, properties = find_peaks(y, height=height, prominence=prominence)
    return peaks, properties

# ---------------- LASER MONITOR TABS ----------------
laser_tabs = []
for i, laser in enumerate(LASERS):
    laser_id = f"laser{i+1}"
    laser_tabs.append(
        dcc.Tab(label=laser["name"], value=laser_id, children=[
            html.H3(f"{laser['name']} Controls"),
            dcc.Graph(id=f"{laser_id}-plot"),
            html.Div([
                html.Button("Start Stream", id=f"{laser_id}-start-btn", n_clicks=0),
                html.Button("Stop Stream", id=f"{laser_id}-stop-btn", n_clicks=0),
                html.Button("Update Peaks", id=f"{laser_id}-update-peaks-btn", n_clicks=0)
            ], style={"marginTop": 10}),
            html.Div([
                html.Label("SPC Start (mA):"),
                dcc.Input(id=f"{laser_id}-spc-start", type="number", value=-1.5, style={"width":"80px", "marginRight":"8px"}),
                html.Label("Stop (mA):"),
                dcc.Input(id=f"{laser_id}-spc-stop", type="number", value=1.5, style={"width":"80px", "marginRight":"8px"}),
                html.Label("Step (uA):"),
                dcc.Input(id=f"{laser_id}-spc-step", type="number", value=25, style={"width":"80px", "marginRight":"8px"}),
                html.Button("Get SPC", id=f"{laser_id}-get-spc-btn", n_clicks=0),
                html.Button("Update SPC", id=f"{laser_id}-update-spc-btn", n_clicks=0, style={"marginLeft": "8px"})
            ], style={"marginTop": 10}),
            dcc.Graph(id=f"{laser_id}-spc-plot"),
            # --- SPC parameter table / changeable variables ---
            html.Div([
                html.H5("SPC / Follower Parameters"),
                html.Table(id=f"{laser_id}-params-table", children=[
                    html.Thead(html.Tr([html.Th("Variable"), html.Th("Value"), html.Th("Action"), html.Th("Status")])),
                    html.Tbody([
                        html.Tr([
                            html.Td("unlock_thresh"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "unlock_thresh"}, type="number", value=0.95, step=0.01, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "unlock_thresh"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "unlock_thresh"}, children="idle"))
                        ]),
                        html.Tr([
                            html.Td("lost_thresh"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "lost_thresh"}, type="number", value=0.25, step=0.01, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "lost_thresh"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "lost_thresh"}, children="idle"))
                        ]),
                        html.Tr([
                            html.Td("bump_thresh"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "bump_thresh"}, type="number", value=0.05, step=0.01, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "bump_thresh"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "bump_thresh"}, children="idle"))
                        ]),
                        html.Tr([
                            html.Td("relock_thresh"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "relock_thresh"}, type="number", value=0.95, step=0.01, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "relock_thresh"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "relock_thresh"}, children="idle"))
                        ]),
                        html.Tr([
                            html.Td("std_thresh"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "std_thresh"}, type="number", value=2.0, step=0.1, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "std_thresh"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "std_thresh"}, children="idle"))
                        ]),
                        html.Tr([
                            html.Td("slower_sign"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "slower_sign"}, type="number", value=1, step=1, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "slower_sign"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "slower_sign"}, children="idle"))
                        ]),
                        html.Tr([
                            html.Td("xbeam_sign"),
                            html.Td(dcc.Input(id={"type": f"{laser_id}-C-input", "var": "xbeam_sign"}, type="number", value=-1, step=1, style={"width":"120px"})),
                            html.Td(html.Button("Change", id={"type": f"{laser_id}-C-btn", "var": "xbeam_sign"}, n_clicks=0)),
                            html.Td(html.Div(id={"type": f"{laser_id}-C-status", "var": "xbeam_sign"}, children="idle"))
                        ])
                    ])
                ])
            ], style={"marginTop": 10}),
            html.Div([
                html.Label("Xbeam Status:"),
                daq.Indicator(id=f"{laser_id}-xbeam-led", value=False, color="green", size=20),
                html.Label("Slow Peaks Status:"),
                daq.Indicator(id=f"{laser_id}-slow-led", value=False, color="green", size=20)
            ], style={"display": "flex", "gap": "20px", "marginTop": 10}),
            html.Div([
                html.Button("Enable Feedback", id=f"{laser_id}-enable-fb-btn", n_clicks=0),
                html.Div(id=f"{laser_id}-enable-fb-status", children="idle", style={"marginLeft": "12px", "display": "inline-block"})
            ], style={"marginTop": 10}),
            html.Div([
                html.Label("Holdoff [us]:"),
                dcc.Input(id=f"{laser_id}-holdoff-input", type="number", value=200, step=100),
                html.Button("Update Holdoff", id=f"{laser_id}-update-holdoff-btn", n_clicks=0)
            ], style={"marginTop": 10}),
            html.Div([
                html.Label("Refresh Rate [Hz]:"),
                dcc.Slider(
                    id=f"{laser_id}-refresh-slider",
                    min=0.5, max=2, step=0.1, value=1.5,
                    marks={0.5:"0.5",1:"1",1.5:"1.5",2:"2"}
                )
            ], style={"marginTop": 10}),
            dcc.Interval(id=f"{laser_id}-interval", interval=500, n_intervals=0),
            dcc.Store(id=f"{laser_id}-peak-bands-store", data=None)
        ])
    )


# ---------------- DIGILOCK MONITOR TAB ----------------

# Default empty figure with compact layout

#If want ch1 on green scope look in callback

# helper func for initializing tables with good info 
def grab_monitor_params(name):
    r = requests.get(f'{DIGILOCK_PI_URL}/monitor_params', params={"laser": name})
    if r.ok:
        data = r.json()
        print(data)
    else:
        print('Error:', r.status_code, r.text)
    return data

monitor_params_b = grab_monitor_params('blue')
monitor_params_g = grab_monitor_params('green')

default_graph_layout = go.Figure()

blue_parameter_table = dbc.Table(
    html.Tbody([
        html.Tr([
            html.Td("Parameter", style={"fontWeight": "bold", "width": "33%"}),
            html.Td("Current Setting", style={"fontWeight": "bold", "width": "33%"}),
            html.Td("Input", style={"fontWeight": "bold", "width": "33%"}),
        ]),
        html.Tr([
            html.Td("RMS Threshold", style={"width": "33%"}),
            html.Td(html.Div(monitor_params_b['rms threshold'], id={'type': 'rms-display', 'laser': 'blue'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'rms-input', 'laser': 'blue'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
        html.Tr([
            html.Td("Window Length", style={"width": "33%"}),
            html.Td(html.Div(monitor_params_b['window length'], id={'type': 'win-len-display', 'laser': 'blue'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'win-len-input', 'laser': 'blue'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
        html.Tr([
            html.Td("Window Fill Threshold", style={"width": "33%"}),
            html.Td(html.Div(monitor_params_b['sum threshold'], id={'type': 'fill-frac-display', 'laser': 'blue'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'fill-frac-input', 'laser': 'blue'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
        html.Tr([
            html.Td("Parameter D", style={"width": "33%"}),
            html.Td(html.Div("0.577", id={'type': 'param-d-display', 'laser': 'blue'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'param-d-input', 'laser': 'blue'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
    ]),
    bordered=True,
    striped=True,
    hover=True,
    style={"width": "100%", "textAlign": "center"}
)

green_parameter_table = dbc.Table(
    html.Tbody([
        html.Tr([
            html.Td("Parameter", style={"fontWeight": "bold", "width": "33%"}),
            html.Td("Current Setting", style={"fontWeight": "bold", "width": "33%"}),
            html.Td("Input", style={"fontWeight": "bold", "width": "33%"}),
        ]),
        html.Tr([
            html.Td("RMS Threshold", style={"width": "33%"}),
            html.Td(html.Div(monitor_params_g['rms threshold'], id={'type': 'rms-display', 'laser': 'green'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'rms-input', 'laser': 'green'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
        html.Tr([
            html.Td("Window Length", style={"width": "33%"}),
            html.Td(html.Div(monitor_params_g['window length'], id={'type': 'win-len-display', 'laser': 'green'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'win-len-input', 'laser': 'green'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
        html.Tr([
            html.Td("Window Fill Threshold", style={"width": "33%"}),
            html.Td(html.Div(monitor_params_g['sum threshold'], id={'type': 'fill-frac-display', 'laser': 'green'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'fill-frac-input', 'laser': 'green'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
        html.Tr([
            html.Td("Parameter D", style={"width": "33%"}),
            html.Td(html.Div("0.577", id={'type': 'param-d-display', 'laser': 'green'}), style={"width": "33%"}),
            html.Td(dcc.Input(id={'type': 'param-d-input', 'laser': 'green'}, type="number", value=0.0, step=0.001, style={"width": "33%"}))
        ]),
    ]),
    bordered=True,
    striped=True,
    hover=True,
    style={"width": "100%", "textAlign": "center"}
)

digilock_tab = dcc.Tab(label='DigiLock Monitor', value='digilock', children=[
    html.H4("DigiLock Status and Control", className="text-info mb-3"),
    dbc.Row([
        dbc.Col([
            html.H5("Blue"),
            dbc.Row([
                dbc.Col([
                    # DAQ indicators
                    html.Div([
                        daq.Indicator(id="b-lock-status", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
                        html.Label([html.Span(":", style={"marginRight": "10px"}), "Lock Status"])
                    ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
                    html.Div([
                        daq.Indicator(id="b-lock-happy", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
                        html.Label([html.Span(":", style={"marginRight": "10px"}), "Lock is Happy :)"])
                    ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
                    html.Div([
                        daq.Indicator(id="b-current-active", value=False, color="red", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
                        html.Label([html.Span(":", style={"marginRight": "10px"}), "Current Ctrl Active"])
                    ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
                    dbc.Switch(
                        id='b-cur-bump',
                        label="Enable Current Ctrl",
                        value=False,  # default off
                        style={"fontSize":18, "marginBottom": "10px","marginTop": "10px", "marginLeft": "7px", "marginRight": "10px"}
                    ),
                    dbc.Switch(
                        id="b-axes-lock",
                        label="Lock Scope Axes",
                        value=False,  # default off
                        style={"fontSize":18, "marginBottom": "10px","marginTop": "10px", "marginLeft": "7px", "marginRight": "10px"}
                    ),
                    dbc.Row(html.Button("Refresh Params", id={'type':'param-refresh-btn', 'laser':'blue'}, n_clicks=0), style={"marginBottom": "10px","marginTop": "10px", "marginLeft": "10px", "marginRight": "10px"})
                ], width=4),
                dbc.Col(dcc.Graph(id='b-scope', figure=default_graph_layout), width=8)
            ]),
            dbc.Row([
                dbc.Col([
                    html.H5("Monitor Parameters"),
                    blue_parameter_table
                ])
            ])   
        ]), 
        dbc.Col([
            html.H5("Green"),
            dbc.Row([
                dbc.Col([
                    # DAQ indicators
                    html.Div([
                        daq.Indicator(id="g-lock-status", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
                        html.Label([html.Span(":", style={"marginRight": "10px"}), "Lock Status"])
                    ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
                    html.Div([
                        daq.Indicator(id="g-lock-happy", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
                        html.Label([html.Span(":", style={"marginRight": "10px"}), "Lock is Happy :)"])
                    ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
                    dbc.Switch(
                        id="g-axes-lock",
                        label="Lock Scope Axes",
                        value=False,  # default off
                        style={'fontSize':18, "marginBottom": "10px","marginTop": "10px", "marginLeft": "7px", "marginRight": "10px"}
                    ),
                    dbc.Row(html.Button("Refresh Params", id={'type':'param-refresh-btn', 'laser':'green'}, n_clicks=0), style={"marginBottom": "10px","marginTop": "10px", "marginLeft": "10px", "marginRight": "10px"})
                ], width=4),
                dbc.Col(dcc.Graph(id='g-scope', figure=default_graph_layout), width=8)
            ]),
            dbc.Row([
                dbc.Col([
                    html.H5("Monitor Parameters"),
                    green_parameter_table
                ])
            ])   
        ]),
    ]),
    dcc.Interval(id="digilock-interval", interval=1000, n_intervals=0),  
])



# ---------------- SCRIPT CONTROL TAB ----------------
processes = {"script1": None, "script2": None}
output_queues = {"script1": queue.Queue(), "script2": queue.Queue()}

def start_script(name, command):
    if processes[name] is None or processes[name].poll() is not None:
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
                shell=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            )
            processes[name] = proc

            def reader_thread():
                for line in proc.stdout:
                    if line:
                        output_queues[name].put(line)
                proc.stdout.close()

            threading.Thread(target=reader_thread, daemon=True).start()
        except Exception as e:
            output_queues[name].put(f"[ERROR launching script]: {e}\n")

def stop_script0(name):
    if processes[name] and processes[name].poll() is None:
        processes[name].send_signal(signal.CTRL_BREAK_EVENT)
        processes[name] = None

def send_interrupt(name):
    """Send an interrupt signal to the subprocess without killing it."""
    proc = processes.get(name)
    if not proc or proc.poll() is not None:
        print(f"[send_interrupt] No active process '{name}' to interrupt.")
        return
    try:
        if os.name == 'nt':
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        # print(f"[send_interrupt] Interrupt sent to '{name}' (PID {proc.pid}).")
    except Exception as e:
        print(f"[send_interrupt] Error sending interrupt to '{name}': {e}")

def stop_script(name):
    """Gracefully stop a subprocess by sending a Ctrl+C or SIGINT event."""
    proc = processes.get(name)

    if not proc:
        print(f"[stop_script] No active process named '{name}'.")
        return

    if proc.poll() is not None:
        print(f"[stop_script] Process '{name}' already exited.")
        processes[name] = None
        return

    try:
        if os.name == 'nt':  # Windows
            # CTRL_BREAK_EVENT is safer than CTRL_C_EVENT for subprocesses not in same console group
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:  # Linux / macOS
            proc.send_signal(signal.SIGINT)

        print(f"[stop_script] Sent interrupt signal to '{name}' (PID {proc.pid}).")

        # Optionally, wait briefly to ensure it shuts down cleanly
        proc.wait(timeout=5)
        print(f"[stop_script] '{name}' exited cleanly.")
    except subprocess.TimeoutExpired:
        print(f"[stop_script] '{name}' did not exit in time. Killing it...")
        proc.kill()
    except Exception as e:
        print(f"[stop_script] Error stopping '{name}': {e}")
    finally:
        processes[name] = None

script_tab = dcc.Tab(label="Camera Control", value="tab_scripts", children=[
    html.H4("Script Control & Monitoring", className="text-info mb-3"),
    dbc.Row([
        dbc.Col([
            html.H5("Camera Controls"),
            dbc.Input(id="script1-command", value=r"python -u D:\Data\multi_run_cam_dash.py", type="text"),
            dcc.Dropdown(
                id="script1-cam-dropdown",
                options=[],  # will be populated via callback
                placeholder="Select Camera",
                style={"width": "700px"}
            ),
            html.Label("Action:"),
            dcc.Dropdown(
                id="script1-action-dropdown",
                options=[{"label": "Quit", "value": "q"},
                {"label": "Restart", "value": "r"},
                {"label": "Swap Camera", "value": "s"},
                {"label": "Continue", "value": "c"}
                ],
                value="r",
                style={"width": "200px"}
            ),
            dbc.Button("Start Camera Script", id="start-script1", color="success", className="me-2"),
            dbc.Button("Send Action", id="interrupt-script1", color="warning", className="mb-2 me-2"),
            # dbc.Button("Stop Camera Script", id="stop-script1", color="danger", className="mb-3"),
            html.Pre(id="script1-output", className="autoscroll", style={
                "backgroundColor": "white", "color": "black",
                "height": "250px", "overflowY": "auto",
                "padding": "10px", "border": "1px solid #333",
                "borderRadius": "8px", "whiteSpace": "pre-wrap"#, "scrollBehavior": "auto"
            }),
            dcc.Store(id="script1-scroll-state", data={"at_bottom": True}),
            dcc.Store(id="dummy1-scroll-output")
        ], width=6),
        dbc.Col([
            html.H5("Cicero Watchdog"),
            dbc.Input(id="script2-command", value=r"python -u D:\Data\Watchdog\Watchdog_RunLogs.py", type="text", className="mb-2"),
            dbc.Button("Start Script 2", id="start-script2", color="success", className="me-2"),
            dbc.Button("Stop Script 2", id="stop-script2", color="danger", className="mb-3"),
            html.Pre(id="script2-output", className="autoscroll", style={
                "backgroundColor": "white", "color": "black",
                "height": "250px", "overflowY": "auto",
                "padding": "10px", "border": "1px solid #333", "borderRadius": "8px", "whiteSpace": "pre-wrap", "scrollBehavior": "auto"
            }),
            dcc.Store(id="script2-scroll-state", data={"at_bottom": True}),
            dcc.Store(id="dummy2-scroll-output")
        ], width=6)
    ]),
    dcc.Interval(id="terminal-update", interval=500, n_intervals=0),
])

app.clientside_callback(
    """
    function(n_intervals) {
        var el = document.getElementById("script1-output");
        if(el) {
            var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
            return {"at_bottom": atBottom};
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("script1-scroll-state", "data"),
    Input("terminal-update", "n_intervals")
)

app.clientside_callback(
    """
    function(children, scroll_state) {
        if(!children) return window.dash_clientside.no_update;
        var el = document.getElementById("script1-output");
        if(el && scroll_state && scroll_state.at_bottom){
            el.scrollTop = el.scrollHeight;
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("dummy1-scroll-output", "data"),  # dummy output
    Input("script1-output", "children"),
    State("script1-scroll-state", "data")
)

app.clientside_callback(
    """
    function(n_intervals) {
        var el = document.getElementById("script2-output");
        if(el) {
            var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 5;
            return {"at_bottom": atBottom};
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("script2-scroll-state", "data"),
    Input("terminal-update", "n_intervals")
)

app.clientside_callback(
    """
    function(children, scroll_state) {
        if(!children) return window.dash_clientside.no_update;
        var el = document.getElementById("script2-output");
        if(el && scroll_state && scroll_state.at_bottom){
            el.scrollTop = el.scrollHeight;
        }
        return window.dash_clientside.no_update;
    }
    """,
    Output("dummy2-scroll-output", "data"),  # dummy output
    Input("script2-output", "children"),
    State("script2-scroll-state", "data")
)

# ---------------- GRAFANA TAB ----------------
grafana_tab = dcc.Tab(label="Grafana Dashboard", value="tab_grafana", children=[
    html.H3("Historical Data"),
    html.Iframe(
        src=GRAFANA_URL,
        width="100%", height="800", style={"border": "none"}
    )
])



# --------------- CENTRAL TAB -------------------

central_tab = dcc.Tab(label='Central Control', value='central-control', children=[
    html.H4("Central Control", className="text-info mb-3"),
    # Injection Monitor section
        

    # Digilock Section
        html.Div([
            daq.Indicator(id="b-lock-status-tabc", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
            html.Label([html.Span(":", style={"marginRight": "10px"}), "Blue Digilock Lock Status"])
        ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
        html.Div([
            daq.Indicator(id="b-lock-happy-tabc", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
            html.Label([html.Span(":", style={"marginRight": "10px"}), "Blue Digilock is Happy :)"])
        ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
        html.Div([
            daq.Indicator(id="b-current-active-tabc", value=False, color="red", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
            html.Label([html.Span(":", style={"marginRight": "10px"}), "Blue Digilock Current Ctrl Active"])
        ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
        dbc.Switch(
            id='b-cur-bump-tabc',
            label="Enable Current Ctrl",
            value=False,  # default off
            style={"fontSize":18, "marginBottom": "10px","marginTop": "10px", "marginLeft": "7px", "marginRight": "10px"}
        ),
        html.Div([
            daq.Indicator(id="g-lock-status-tabc", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
            html.Label([html.Span(":", style={"marginRight": "10px"}), "Green Digilock Lock Status"])
        ], style={"display": "flex", "gap": "20px", "marginTop": 20}),
        html.Div([
            daq.Indicator(id="g-lock-happy-tabc", value=False, color="green", size=20, style={"transform": "translateY(-4px) translateX(6px)"}),
            html.Label([html.Span(":", style={"marginRight": "10px"}), "Green Digilock is Happy :)"])
        ], style={"display": "flex", "gap": "20px", "marginTop": 20}),

    # Beatnote section

    # Log choice

    dcc.Interval(id="central-tab-interval", interval=1000, n_intervals=0),  
])

# ---------------- APP LAYOUT ----------------
tabs = dcc.Tabs(
    id="tabs",
    value=LASERS[0]["name"],
    children=[central_tab] + laser_tabs + [digilock_tab] + [grafana_tab, script_tab]
)

app.layout = dbc.Container(
    children=[
        dcc.Interval(
        id="global-interval",
        interval=500,  # every .5 second
        n_intervals=0
        ),
        dcc.Store(id="global-b-cur-switch", data=False, storage_type='local'),
        html.H2("Lab Dashboard", className="text-center my-3", style={'color':'black'}),
        tabs,
        # Hidden Div for dash_state callback
        html.Div(id="dummy-output", style={"display": "none"})
    ],
    fluid=True,
    style={'backgroundColor': 'white', 'color': 'black', 'height': '100vh', 'padding': '10px'}
)


# ---------------- CALLBACKS ----------------


# ------------------ GLOBAL VARIABLE CALLBACKS -----------------------------

@app.callback(
        Input("global-interval", "n_intervals")
)
def global_refresh(n):
    global GLOBAL_INTERVAL_REFRESH_DICT
    try: # intial fetch and set indicators
        resp = requests.get(f"{DIGILOCK_PI_URL}/monitor_states", timeout=1)
        resp.raise_for_status()  
        data = resp.json()

        # print('dict before: ',GLOBAL_INTERVAL_REFRESH_DICT['digilock-b-happy'])
        # print("data: ", not data.get("b_lock_unhappy", True))
        GLOBAL_INTERVAL_REFRESH_DICT['digilock-b-locked'] = data.get('b_locked', False)
        GLOBAL_INTERVAL_REFRESH_DICT['digilock-b-happy'] = not data.get("b_lock_unhappy", True) # unfortunate choice of default bool state my bad :')
        GLOBAL_INTERVAL_REFRESH_DICT['digilock-g-locked'] = data.get('g_locked', False)
        GLOBAL_INTERVAL_REFRESH_DICT['digilock-g-happy'] = not data.get("g_lock_unhappy", True)
        GLOBAL_INTERVAL_REFRESH_DICT["digilock-cur-active"] = data.get("b_cur_ctrl_active", False)

        # print('dict after: ', GLOBAL_INTERVAL_REFRESH_DICT['digilock-b-happy'])



        # dont think we need or want to pull injection lock data here,
        # only need to pull the seed laser states as the follower algorithms
        # need that data, but not the other way around.
        # In the global tab we will have an interval which QUERIES
        # the beatnote and lock follower states and displays. 
        # As for the digilock data display on global tab
        # we just need to read out from the dictionary
        # every time the tab interval increments

        ### PUSH TO INJ LOCK FUNC PSEUDOCODE (maybe want to do this in separate try/except block for debugging clarity later) ###
        '''
        resp2 = requests.post('URL AND ENDPOINT FOR THIS SPECIFIC UPDATE FUNC', 
                                json={"b_locked": data.get('b_locked', False)
                                      "b_happy": not data.get("b_lock_unhappy", True)
                                      }, 
                                timeout=1)
        resp2.raise_for_status()
        '''

        ### PUSH TO BEATNOTE LOCK PSEUDOCODE ###
        """
        resp3 = requests.post('URL AND ENDPOINT FOR THIS SPECIFIC UPDATE FUNC', 
                                json={"g_locked": data.get('g_locked', False)
                                      "g_happy": not data.get("g_lock_unhappy", True)
                                      }, 
                                timeout=1)
        resp3.raise_for_status()
        """

    except requests.HTTPError as e: # added all these exceptions to make debugging easier later
        print(f"[HTTP ERROR] {e.response.status_code}: {e.response.text}")
    except requests.Timeout:
        print("[TIMEOUT] Could not reach DigiLock Pi within 1s.")
    except requests.RequestException as e:
        print(f"[REQUEST ERROR] {e}")
    except Exception as e:
        print(f"[UNEXPECTED ERROR] {e}")


# When any switch is toggled, update both
@app.callback(
    Output('b-cur-bump', "value"),
    Output("b-cur-bump-tabc", "value"),
    Output("global-b-cur-switch", 'data'),
    [Input('b-cur-bump', "value"),
     Input("b-cur-bump-tabc", "value")],
    State("global-b-cur-switch", 'data')
)
def cur_ctrl_toggles(tab1_val, tab2_val, saved_state):#, current_store
    trigger = ctx.triggered_id
    if trigger in ['b-cur-bump', 'b-cur-bump-tabc']:
        val = tab1_val if trigger == 'b-cur-bump' else tab2_val
        try:
            resp = requests.post(
                f'{DIGILOCK_PI_URL}/set_cur_ctrl',
                json={'laser': 'blue', 'value': bool(val)}
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"[REQUEST ERROR] {e}")
    else:
        # Don't update server, just return the saved state
        val = saved_state

    return val, val, val



# -----------------Central Tab Callbacks -------------------------

@app.callback(
    Output("b-lock-status-tabc", 'color'),
    Output("b-lock-happy-tabc", 'color'),
    Output("g-lock-status-tabc", 'color'),
    Output("g-lock-happy-tabc", 'color'),
    Output("b-current-active-tabc", 'color'),
    Input("central-tab-interval", "n_intervals")
)
def refresh_central_display(n):
    global GLOBAL_INTERVAL_REFRESH_DICT

    # default (offline) colors
    digi_b_status = "red"
    digi_b_happy = "red"
    digi_g_status = "red"
    digi_g_happy = "red"
    digi_cur_active = 'gray'

    try: # set indicators from global dict
        # Blue indicators
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-b-locked", False):
            digi_b_status = "green"
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-b-happy", False):
            digi_b_happy = "green"
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-cur-active", False):
            digi_cur_active = 'yellow'
        # Green indicators
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-g-locked", False):
            digi_g_status = "green"
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-g-happy", False):
            digi_g_happy = "green"

    except Exception as e:
        print(f"Lock state display failed: {e}")
        digi_b_status, digi_b_happy, digi_g_status, digi_g_happy = "gray", "gray", "gray", "gray"

    return digi_b_status, digi_b_happy, digi_g_status, digi_g_happy, digi_cur_active




# -----------------Injection Lock Callbacks ----------------------
for i, laser in enumerate(LASERS):
    laser_id = f"laser{i+1}"
    pi_url = laser["pi_url"]

    # --- Store for stats, clusters, and peaks ---
    @app.callback(
        Output(f"{laser_id}-peak-bands-store", "data"),
        Input(f"{laser_id}-update-peaks-btn", "n_clicks"),
        Input(f"{laser_id}-update-holdoff-btn", "n_clicks"),
        State(f"{laser_id}-peak-bands-store", "data"),
        prevent_initial_call=False
    )
    def fetch_and_update_bands(update_peaks_clicks, update_holdoff_clicks, bands, pi_url=pi_url):
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
            bands["stats"] = requests.get(f"{pi_url}/stats", timeout=2).json()
            bands["clusters"] = requests.get(f"{pi_url}/clusters", timeout=2).json()
        except Exception as e:
            print(f"[ERROR] Fetching stats/clusters failed for {laser_id}: {e}")

        return bands
    
    @app.callback(
        Output(f"{laser_id}-plot", "figure"),
        Output(f"{laser_id}-xbeam-led", "color"),
        Output(f"{laser_id}-slow-led", "color"),
        Input(f"{laser_id}-interval", "n_intervals"),
        State(f"{laser_id}-peak-bands-store", "data")
    )
    def update_plot(n, bands, pi_url=pi_url):

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
            title=f"{LASERS[i]['name']} Injection Monitor",
            xaxis={"title": "Sample", "range": [0, N]},
            yaxis={"title": "ADC Value", "range": [0, 1750]},
            plot_bgcolor="#f5f5f5", paper_bgcolor="#ffffff"
        )

        return fig, xbeam_color, slow_color

    @app.callback(
        Output(f"{laser_id}-spc-plot", "figure"),
        Input(f"{laser_id}-get-spc-btn", "n_clicks"),
        Input(f"{laser_id}-update-spc-btn", "n_clicks"),
        State(f"{laser_id}-spc-start", "value"),
        State(f"{laser_id}-spc-stop", "value"),
        State(f"{laser_id}-spc-step", "value"),
        prevent_initial_call=True
    )
    def get_or_update_spc_and_plot(get_n_clicks, update_n_clicks, startmA, stopmA, stepuA, pi_url=pi_url):
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
        def poll_for_spc(timeout=35):
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
        if trigger_id == f"{laser_id}-update-spc-btn":
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
        if trigger_id == f"{laser_id}-get-spc-btn":
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
        Output({"type": f"{laser_id}-C-status", "var": MATCH}, "children"),
        Input({"type": f"{laser_id}-C-btn", "var": MATCH}, "n_clicks"),
        State({"type": f"{laser_id}-C-input", "var": MATCH}, "value"),
        prevent_initial_call=True
    )
    def change_param(n_clicks, value, pi_url=pi_url):
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
        Output(f"{laser_id}-holdoff-input", "value"),
        Input(f"{laser_id}-update-holdoff-btn", "n_clicks"),
        State(f"{laser_id}-holdoff-input", "value"),
        State(f"{laser_id}-peak-bands-store", "data")
    )
    def update_holdoff(n_clicks, value, bands, pi_url=pi_url):
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
                print(f"[ERROR] Updating holdoff failed for {laser_id}: {e}")
        return value

    @app.callback(
        Output(f"{laser_id}-enable-fb-status", "children"),
        Input(f"{laser_id}-enable-fb-btn", "n_clicks"),
        prevent_initial_call=True
    )
    def enable_feedback(n_clicks, pi_url=pi_url):
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
                print(f"[ERROR] Enable feedback returned {status_code} for {laser_id}: {body}")
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
            print(f"[ERROR] Enable feedback failed for {laser_id}: {e}")
            return f"error: {e}"
    
    # --- Start/Stop Stream ---
    @app.callback(
        Output(f"{laser_id}-interval", "disabled"),
        [Input(f"{laser_id}-start-btn", "n_clicks"), Input(f"{laser_id}-stop-btn", "n_clicks")]
    )
    def toggle_stream(start_clicks, stop_clicks):
        return stop_clicks > start_clicks

    # --- Refresh Rate Slider ---
    @app.callback(
        Output(f"{laser_id}-interval", "interval"),
        Input(f"{laser_id}-refresh-slider", "value")
    )
    def update_interval(value_hz):
        return int(1000 / value_hz)




# ---------------- DigiLock Callbacks ---------------------




# for initializing the figure
def init_fig(y1, y2, range1=None, range2=None):
    fig = go.Figure()

    if isinstance(y1, list):
        # First trace on left y-axis
        fig.add_trace(go.Scattergl(y=y1, mode="lines", line=dict(color="yellow"), showlegend=False ))
        fig.update_layout(
            yaxis=dict(title="Ch1", color="yellow", title_standoff=5, automargin=False),
        )
    if isinstance(y2, list):
        # Second trace on right y-axis
        fig.add_trace(go.Scattergl(y=y2, mode="lines", line=dict(color="red"), yaxis="y2", showlegend=False ))
        fig.update_layout(
            yaxis2=dict(title="Ch2", overlaying="y", side="right", color="red", title_standoff=5, automargin=False),

        )
    # Update layout with two y-axes
    fig.update_layout(
        title={'text': "Error Signal", "x": 0.5, "y": 0.98, "xanchor": "center", "yanchor":'top'},
        plot_bgcolor="#8f8e8e",
        paper_bgcolor="#B8B5B5",
        font=dict(color="black", size=14),
        height=300,
        margin=dict(l=60, r=60, t=30, b=40),
        xaxis=dict(title='Sample',title_standoff=5, automargin=False)
    )

    if range1 is not None:
        fig.layout.yaxis.range = range1
    if range2 is not None: 
        fig.layout.yaxis2.range = range2

    return fig

def update_fig(fig, y1=None, y2=None):
    # Add new data without changing layout
    if y1 is not None:
        fig.data[0].y = y1  # assuming trace 0 is Ch1
    if y2 is not None:
        fig.data[1].y = y2  # assuming trace 1 is Ch2

    return fig

LOCK_G_FIG = None
LOCK_B_FIG = None
AUTO_B_FIG = None
AUTO_G_FIG = None

@app.callback(
    Output("b-lock-status", "color"),
    Output("b-lock-happy", "color"),
    Output("b-scope", "figure"),
    Output("g-lock-status", "color"),
    Output("g-lock-happy", "color"),
    Output("g-scope", "figure"),
    Input("digilock-interval", "n_intervals"),
    State("b-axes-lock", "value"),
    State("g-axes-lock", "value")
)
def digilock_refresh(n, b_axes_lock, g_axes_lock):
    global GLOBAL_INTERVAL_REFRESH_DICT
    global LOCK_G_FIG
    global LOCK_B_FIG
    global AUTO_G_FIG
    global AUTO_B_FIG
    # default (offline) colors
    b_lock_status = "red"
    b_lock_happy = "red"
    g_lock_status = "red"
    g_lock_happy = "red"

    try: # set indicators from global dict
        # Blue indicators
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-b-locked", False):
            b_lock_status = "green"
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-b-happy", False):
            b_lock_happy = "green"

        # Green indicators
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-g-locked", False):
            g_lock_status = "green"
        if GLOBAL_INTERVAL_REFRESH_DICT.get("digilock-g-happy", False):
            g_lock_happy = "green"

    except Exception as e:
        print(f"Lock state display failed: {e}")
        b_lock_status, b_lock_happy, g_lock_status, g_lock_happy = "gray", "gray", "gray", "gray"

    try: # scope refresh
        resp = requests.get(f"{DIGILOCK_PI_URL}/scope_traces", timeout=3)
        resp.raise_for_status()

        data = resp.json()

        # gch1 = data.get("g_ch1", [])
        gch2 = data.get("g_ch2", [])
        if len(gch2) == 0:
            raise ValueError("(GREEN) Empty response data")
        bch1 = data.get("b_ch1", [])
        bch2 = data.get("b_ch2", [])
        if len(bch2) == 0:
            raise ValueError("(BLUE) Empty response data")

        if g_axes_lock:
            if LOCK_G_FIG is None:
                g_fig = init_fig(None, gch2, range2=[min(gch2),max(gch2)])
                LOCK_G_FIG = g_fig
            else:
                g_fig = update_fig(LOCK_G_FIG, gch2, None)
        else:
            LOCK_G_FIG = None
            if AUTO_G_FIG is None:
                g_fig = init_fig(None, gch2)
                AUTO_G_FIG = g_fig
            else:
                g_fig = update_fig(AUTO_G_FIG, gch2, None)

        if b_axes_lock:
            if LOCK_B_FIG is None:
                b_fig = init_fig(bch1, bch2, range1=[min(bch1),max(bch1)], range2=[min(bch2),max(bch2)])
                LOCK_B_FIG = b_fig
            else:
                b_fig = update_fig(LOCK_B_FIG, bch1, bch2)
        else:
            LOCK_B_FIG = None
            if AUTO_B_FIG is None:
                b_fig = init_fig(bch1, bch2)
                AUTO_B_FIG = b_fig
            else:
                b_fig = update_fig(AUTO_B_FIG, bch1, bch2)


        # # g_fig.add_trace(go.Scattergl(y=gch1, mode="lines", name="ch1", line=dict(color="red", width=2)))
        # g_fig.update_layout() # arg used to be **default_graph_layout.layout.to_plotly_json()

        
        # # b_fig.add_trace(go.Scattergl(y=bch1, mode="lines", name="ch1", line=dict(color="yellow", width=2)))
        # # b_fig.add_trace(go.Scattergl(y=bch2, mode="lines", name="ch1", line=dict(color="red", width=2)))
        # b_fig.update_layout()
    except requests.RequestException as e:
        print(f"[REQUEST ERROR] {e}")
    except Exception as e:
        print(f"Error displaying scope data: {e}")
        g_fig = go.Figure()
        b_fig = go.Figure()

    return b_lock_status, b_lock_happy, b_fig, g_lock_status, g_lock_happy, g_fig



@app.callback(
    Output({"type": 'rms-display', "laser": MATCH}, "children"),
    Output({"type": 'win-len-display', "laser": MATCH}, "children"),
    Output({"type": 'fill-frac-display', "laser": MATCH}, "children"),
    Input({'type':'param-refresh-btn', 'laser': MATCH}, "n_clicks"),
    State({'type':'rms-input', 'laser': MATCH}, "value"),
    State({'type':'win-len-input', 'laser': MATCH}, "value"),
    State({'type':'fill-frac-input', 'laser': MATCH}, "value"),
    prevent_initial_call=True
)
def refresh_params(n, rms_thresh, window_len, fill_frac_thresh):
    try:
        if window_len < 1:
            raise ValueError('Window length must be greater than 1')
        if fill_frac_thresh > window_len:
            raise ValueError('Fill threshold greater than full sliding window length')
        if fill_frac_thresh < 1: 
            raise ValueError('Fill threshold must be greater than 0')
        if rms_thresh < 0: 
            raise ValueError('RMS error must be non negative')
        ctx = dash.callback_context
        laser_color = ctx.triggered_id["laser"]
        data = {
            'name': laser_color,
            'rms threshold': rms_thresh,
            'window length': int(window_len),
            'fill fraction threshold': int(fill_frac_thresh) # unfortunately named but im too lazy to change
        }
        r = requests.post(f"{DIGILOCK_PI_URL}/refresh_params", json=data, timeout=2)
        r.raise_for_status()
        return rms_thresh, int(window_len), f'{int(fill_frac_thresh)}   ({np.round((fill_frac_thresh/window_len)*100)} %)'
    except requests.RequestException as e:
        print("Error Updating Digilock Params:", e)
        return 'Err', 'Err', 'Err'
    except Exception as e:
        print("Client Error:", e)
        return 'Err', 'Err', 'Err'


# SCRIPT CONTROL CALLBACKS
# ---------------- SCRIPT CONTROL CALLBACKS ----------------
# ---------------- CALLBACKS ----------------
# Start/Stop scripts
@app.callback(
    Output("start-script1", "n_clicks"),
    Input("start-script1", "n_clicks"),
    State("script1-command", "value"),
    prevent_initial_call=True
)



def start_script1_cb(n, cmd=None):
    if not n:
        return 0
    cmd = cmd or r"python -u D:\Data\multi_run_cam_dash.py"
    args = [VIMBA_VENV_PATH, "-u", r"D:\Data\multi_run_cam_dash.py"]
    start_script("script1", args)
    return 0


@app.callback(
    Output("stop-script1", "n_clicks"),
    Input("stop-script1", "n_clicks"),
    prevent_initial_call=True
)
def stop_script1_cb(n):
    if n:
        stop_script("script1")
    return 0

@app.callback(
    Output("start-script2", "n_clicks"),
    Input("start-script2", "n_clicks"),
    State("script2-command", "value"),
    prevent_initial_call=True
)
def start_script2_cb(n, cmd):
    if n:
        start_script("script2", cmd.split())
    return 0

@app.callback(
    Output("stop-script2", "n_clicks"),
    Input("stop-script2", "n_clicks"),
    prevent_initial_call=True
)
def stop_script2_cb(n):
    if n:
        stop_script("script2")
    return 0


@app.callback(
    Output("dummy-output", "children"),  # hidden div, we don't need actual output
    Input("script1-cam-dropdown", "value"),
    Input("script1-action-dropdown", "value"),
    prevent_initial_call=True
)
def update_dash_state(cam_index, action_str):
    if cam_index is None:
        cam_index = None  # fallback if nothing selected

    # Save state to JSON
    state = {"cam_index": cam_index, "action": action_str}
    # print("[DEBUG] update_dash_state called, state is: ", state)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

    return ""


@app.callback(
    Output("script1-cam-dropdown", "options"),
    Input("tabs", "value")  # trigger when switching to the camera tab
)
def populate_cam_dropdown(_):
    return get_camera_dropdown_options()

def get_camera_dropdown_options(_=None):
    cmd = [VIMBA_VENV_PATH, r"D:\Data\list_cameras.py"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    lines = result.stdout.splitlines()

    options = []
    for line in lines:
        if ":" in line:
            idx, rest = line.split(":", 1)
            cam_name = rest.strip().split("Allied Vision Technologies")[-1]
            cam_id_str = cam_name.split("(")[-1][:-1]
            # print("[DEBUG] Rest.strip(): ", cam_name)
            # print("[DEBUG] cam_id_str: ", cam_id_str)
            options.append({"label": cam_name, "value": cam_id_str})
    return options

@app.callback(
    Output("interrupt-script1", "n_clicks"),
    Input("interrupt-script1", "n_clicks"),
    prevent_initial_call=True
)
def interrupt_script1_cb(n):
    if n:
        send_interrupt("script1")
    return 0

# Update outputs and autoscroll
@app.callback(
    Output("script1-output", "children"),
    Output("script2-output", "children"),
    # Output("script1-js", "run"),
    # Output("script2-js", "run"),
    # Output("autoscroll-js", "run"),
    Input("terminal-update", "n_intervals"),
    State("script1-output", "children"),
    State("script2-output", "children"),
    # State("script1-scroll-state", "data")
)
def update_script_outputs(_n, prev1, prev2):
    # prev1 and prev2 are lists of html.P children
    prev1 = prev1 or []
    prev2 = prev2 or []

    # Append new lines from queues
    while not output_queues["script1"].empty():
        prev1.append(html.P(output_queues["script1"].get()))
    while not output_queues["script2"].empty():
        prev2.append(html.P(output_queues["script2"].get()))

    return prev1, prev2




# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8050, debug=True, use_reloader=False)