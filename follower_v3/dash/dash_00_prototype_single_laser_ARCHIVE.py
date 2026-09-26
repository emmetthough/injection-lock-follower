
## OLD 2

# import dash
# from dash import html, dcc, Input, Output, State
# from dash import ClientsideFunction
# import dash_daq as daq
# import dash_bootstrap_components as dbc
# import requests, numpy as np
# from scipy.signal import find_peaks
# import plotly.graph_objs as go
# import subprocess, threading, queue
# import signal

# # ---------------- CONFIG ----------------
# INJECTION_PI_URL = "http://10.19.33.12:8000"  # <-- Replace with your Pi's IP
# LASERS = [
#     {"name": "Injection Monitor", "pi_url": INJECTION_PI_URL}
# ]
# N = 500
# CALIBRATION_SWEEPS = 20
# SLOWER_LOWER, SLOWER_UPPER = 1000, 2000
# XBEAM_LOWER, XBEAM_UPPER = 500, 1000
# GRAFANA_URL = "http://10.155.94.101:3000/public-dashboards/eb5da49884b045c194abb81284e53aae?orgId=1&kiosk=tv"



# # ---------------- DASH APP ----------------
# app = dash.Dash(__name__, external_stylesheets=[dbc.themes.BOOTSTRAP])
# app.title = "B055 Laser Dashboard"

# # app.index_string = '''
# # <!DOCTYPE html>
# # <html>
# #     <head>
# #         {%metas%}
# #         <title>{%title%}</title>
# #         {%favicon%}
# #         {%css%}
# #         <style>
# #             body { background-color: white; color: black; }
# #             pre.autoscroll {
# #                 background-color: white;
# #                 color: black;
# #                 height: 250px;
# #                 overflow-y: scroll;
# #                 padding: 10px;
# #                 border: 1px solid #ccc;
# #                 border-radius: 8px;
# #                 font-family: monospace;
# #             }
# #         </style>
# #     </head>
# #     <body>
# #         {%app_entry%}
# #         <footer>
# #             {%config%}
# #             {%scripts%}
# #             {%renderer%}
# #             <script>
# #                 function scrollConsoles() {
# #                     document.querySelectorAll('pre.autoscroll').forEach(el => {
# #                         el.scrollTop = el.scrollHeight;
# #                     });
# #                 }
# #                 setInterval(scrollConsoles, 200);
# #             </script>
# #         </footer>
# #     </body>
# # </html>
# # '''


# # ---------------- HELPER FUNCTIONS ----------------
# def detect_peaks(y, height=180, prominence=5, smoothing=True, window=5):
#     if smoothing:
#         y = np.convolve(y, np.ones(window) / window, mode='same')
#     peaks, properties = find_peaks(y, height=height, prominence=prominence)
#     return peaks, properties

# def check_led_status(all_peaks, lower, upper):
#     selected = all_peaks[(all_peaks >= lower) & (all_peaks <= upper)] if len(all_peaks) else np.array([])
#     return len(selected) > 0

# # ---------------- LASER MONITOR TABS ----------------
# laser_tabs = []
# for i, laser in enumerate(LASERS):
#     laser_id = f"laser{i+1}"
#     laser_tabs.append(
#         dcc.Tab(label=laser["name"], value=laser_id, children=[
#             html.H3(f"{laser['name']} Controls"),
#             dcc.Graph(id=f"{laser_id}-plot"),
#             html.Div([
#                 html.Button("Start Stream", id=f"{laser_id}-start-btn", n_clicks=0),
#                 html.Button("Stop Stream", id=f"{laser_id}-stop-btn", n_clicks=0),
#                 html.Button("Update Peaks", id=f"{laser_id}-update-peaks-btn", n_clicks=0)
#             ], style={"marginTop": 10}),
#             html.Div([
#                 html.Label("Xbeam Status:"),
#                 daq.Indicator(id=f"{laser_id}-xbeam-led", value=False, color="green", size=20),
#                 html.Label("Slow Peaks Status:"),
#                 daq.Indicator(id=f"{laser_id}-slow-led", value=False, color="green", size=20)
#             ], style={"display": "flex", "gap": "20px", "marginTop": 10}),
#             html.Div([
#                 html.Label("Holdoff [us]:"),
#                 dcc.Input(id=f"{laser_id}-holdoff-input", type="number", value=0, step=1),
#                 html.Button("Update Holdoff", id=f"{laser_id}-update-holdoff-btn", n_clicks=0)
#             ], style={"marginTop": 10}),
#             html.Div([
#                 html.Label("Refresh Rate [Hz]:"),
#                 dcc.Slider(
#                     id=f"{laser_id}-refresh-slider",
#                     min=0.5, max=5, step=0.1, value=2,
#                     marks={0.5:"0.5",1:"1",2:"2",3:"3",4:"4",5:"5"}
#                 )
#             ], style={"marginTop": 10}),
#             dcc.Interval(id=f"{laser_id}-interval", interval=500, n_intervals=0),
#             dcc.Store(id=f"{laser_id}-peak-bands-store", data={"slow": None, "xbeam": None})
#         ])
#     )

# # ---------------- SCRIPT CONTROL TAB ----------------
# processes = {"script1": None, "script2": None}
# output_queues = {"script1": queue.Queue(), "script2": queue.Queue()}

# def stream_output2(name, proc):
#     for line in iter(proc.stdout.readline, b""):
#         output_queues[name].put(line.decode("utf-8"))
#     proc.stdout.close()

# def stream_output(name, proc):
#     try:
#         for line in iter(proc.stdout.readline, ''):
#             if line:
#                 output_queues[name].put(line)
#     except Exception as e:
#         output_queues[name].put(f"[ERROR in stream_output]: {e}\n")
#     finally:
#         output_queues[name].put(f"[DEBUG] Output thread for {name} finished.\n")



# def start_script(name, command):
#     # print(f"[DEBUG] Starting {name}: {command}")
#     if processes[name] is None or processes[name].poll() is not None:
#         try:
#             # On Windows, pass command as string and use shell=True
#             proc = subprocess.Popen(
#                 command,
#                 stdout=subprocess.PIPE,
#                 stderr=subprocess.STDOUT,
#                 bufsize=1,
#                 universal_newlines=True,  # ensures text mode
#                 shell=True,
#                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
#             )
#             processes[name] = proc
#             # print(f"[DEBUG] {name} started with PID {proc.pid}")

#             def reader_thread():
#                 # print(f"[DEBUG] Output thread for {name} started")
#                 for line in proc.stdout:
#                     if line:
#                         # print(f"[DEBUG] {name}: {line.strip()}")
#                         output_queues[name].put(line)
#                 proc.stdout.close()
#                 # print(f"[DEBUG] Output thread for {name} finished")

#             threading.Thread(target=reader_thread, daemon=True).start()
#         except Exception as e:
#             # print(f"[DEBUG] Failed to start {name}: {e}")
#             output_queues[name].put(f"[ERROR launching script]: {e}\n")
#     else:
#         print(f"[DEBUG] {name} already running with PID {processes[name].pid}")



# def read_output_thread(name, proc):
#     # print(f"[DEBUG] Output thread started for {name}")
#     try:
#         while True:
#             line = proc.stdout.readline()
#             if line == '' and proc.poll() is not None:
#                 # print(f"[DEBUG] Output thread for {name} exiting (process ended)")
#                 break
#             if line:
#                 # print(f"[DEBUG] {name} output: {line.strip()}")
#                 output_queues[name].put(line)
#     except Exception as e:
#         # print(f"[DEBUG] Exception in output thread for {name}: {e}")
#         output_queues[name].put(f"[ERROR in thread]: {e}\n")
#     finally:
#         proc.stdout.close()
#         # print(f"[DEBUG] Output thread for {name} finished")




# def stop_script(name):
#     if processes[name] and processes[name].poll() is None:
#         # print(f"[DEBUG] Sending CTRL_BREAK_EVENT to {name} (PID {processes[name].pid})")
#         processes[name].send_signal(signal.CTRL_BREAK_EVENT)
#         processes[name] = None
#         # output_queues[name].put(f"[DEBUG] {name} stop signal sent.\n")



# script_tab = dcc.Tab(label="Camera Control", value="tab_scripts", children=[
#     html.H4("Script Control & Monitoring", className="text-info mb-3"),
#     dbc.Row([
#         dbc.Col([
#             html.H5("Script 1 Controls"),
#             dbc.Input(id="script1-command", value="python script1.py", type="text", className="mb-2"),
#             dbc.Button("Start Script 1", id="start-script1", color="success", className="me-2"),
#             dbc.Button("Stop Script 1", id="stop-script1", color="danger", className="mb-3"),
#             html.Pre(id="script1-output", className="autoscroll", style={
#                 "backgroundColor": "white", "color": "black",
#                 "height": "250px", "overflowY": "scroll",
#                 "padding": "10px", "border": "1px solid #333", "borderRadius": "8px"
#             })
#             # html.Pre(id="script1-output", className="autoscroll")
#         ], width=6),
#         dbc.Col([
#             html.H5("Script 2 Controls"),
#             dbc.Input(id="script2-command", value="python script2.py", type="text", className="mb-2"),
#             dbc.Button("Start Script 2", id="start-script2", color="success", className="me-2"),
#             dbc.Button("Stop Script 2", id="stop-script2", color="danger", className="mb-3"),
#             html.Pre(id="script2-output", className="autoscroll", style={
#                 "backgroundColor": "white", "color": "black",
#                 "height": "250px", "overflowY": "scroll",
#                 "padding": "10px", "border": "1px solid #333", "borderRadius": "8px"
#             })
#         ], width=6)
#     ]),
#     dcc.Interval(id="terminal-update", interval=1000, n_intervals=0)
# ])

# # ---------------- GRAFANA TAB ----------------
# grafana_tab = dcc.Tab(label="Grafana Dashboard", value="tab_grafana", children=[
#     html.H3("Historical Data"),
#     html.Iframe(
#         src=GRAFANA_URL,
#         width="100%", height="800", style={"border": "none"}
#     )
# ])

# # ---------------- APP LAYOUT ----------------
# tabs = dcc.Tabs(
#     id="tabs",
#     value=LASERS[0]["name"],
#     children=laser_tabs + [grafana_tab, script_tab]
# )

# # app.layout = dbc.Container([
# #     html.H2("Lab Dashboard", className="text-center text-light my-3"),
# #     tabs
# # ], fluid=True)

# app.layout = dbc.Container(
#     children=[
#         html.H2("Lab Dashboard", className="text-center my-3", style={'color':'black'}),
#         tabs,
#         html.Div(id="dummy-scroll", style={"display": "none"})
#     ],
#     fluid=True,
#     style={
#         'backgroundColor': 'white',  # main background
#         'color': 'black',            # default text color
#         'height': '100vh',
#         'padding': '10px'
#     }
# )


# # ---------------- CALLBACKS ----------------

# # --- LASER CALLBACKS ---
# for i, laser in enumerate(LASERS):
#     laser_id = f"laser{i+1}"
#     pi_url = laser["pi_url"]

#     @app.callback(
#         Output(f"{laser_id}-peak-bands-store", "data"),
#         Input(f"{laser_id}-update-peaks-btn", "n_clicks"),
#         prevent_initial_call=True
#     )
#     def update_bands(n_clicks, laser_index=i, pi_url=pi_url):
#         all_peaks = []
#         for _ in range(CALIBRATION_SWEEPS):
#             try:
#                 resp = requests.get(f"{pi_url}/waveform", timeout=1)
#                 y = np.array(resp.json()["data"])
#             except:
#                 y = np.zeros(N)
#             peaks, _ = detect_peaks(y)
#             all_peaks.extend(y[peaks])
#         all_peaks = np.array(all_peaks)
#         slow_peaks = all_peaks[(all_peaks >= SLOWER_LOWER) & (all_peaks <= SLOWER_UPPER)]
#         xbeam_peaks = all_peaks[(all_peaks >= XBEAM_LOWER) & (all_peaks <= XBEAM_UPPER)]
#         return {
#             "slow": {"mean": np.mean(slow_peaks), "std": np.std(slow_peaks)} if len(slow_peaks) else None,
#             "xbeam": {"mean": np.mean(xbeam_peaks), "std": np.std(xbeam_peaks)} if len(xbeam_peaks) else None
#         }

#     @app.callback(
#         Output(f"{laser_id}-plot", "figure"),
#         Output(f"{laser_id}-xbeam-led", "color"),
#         Output(f"{laser_id}-slow-led", "color"),
#         Input(f"{laser_id}-interval", "n_intervals"),
#         State(f"{laser_id}-peak-bands-store", "data")
#     )
#     def update_plot_and_leds(n, bands, laser_index=i, pi_url=pi_url):
#         try:
#             resp = requests.get(f"{pi_url}/waveform", timeout=1)
#             y = np.array(resp.json()["data"])
#         except:
#             y = np.zeros(N)
#         peaks, _ = detect_peaks(y)
#         peak_vals = y[peaks]

#         xbeam_ok = any((peak_vals >= XBEAM_LOWER) & (peak_vals <= XBEAM_UPPER))
#         slow_ok  = any((peak_vals >= SLOWER_LOWER) & (peak_vals <= SLOWER_UPPER))
#         xbeam_color = "green" if xbeam_ok else "red"
#         slow_color  = "green" if slow_ok else "red"

#         fig = go.Figure()
#         fig.add_trace(go.Scatter(y=y, mode="lines", name="Waveform", line=dict(color="blue", width=2)))
#         fig.add_trace(go.Scatter(x=peaks, y=peak_vals, mode="markers", name="Peaks", marker=dict(color="red", size=6)))

#         if bands:
#             if bands.get("slow"):
#                 mean_slow, std_slow = bands["slow"]["mean"], bands["slow"]["std"]
#                 fig.add_hrect(y0=mean_slow-std_slow, y1=mean_slow+std_slow, fillcolor="green", opacity=0.2)
#             if bands.get("xbeam"):
#                 mean_x, std_x = bands["xbeam"]["mean"], bands["xbeam"]["std"]
#                 fig.add_hrect(y0=mean_x-std_x, y1=mean_x+std_x, fillcolor="orange", opacity=0.2)

#         fig.update_layout(
#             title=f"{LASERS[laser_index]['name']} Injection Monitor",
#             xaxis={"title": "Sample", "range": [0, N]},
#             yaxis={"title": "ADC Value", "range": [0, 1750]},
#             plot_bgcolor="#f5f5f5", paper_bgcolor="#ffffff"
#         )

#         return fig, xbeam_color, slow_color

# # --- SCRIPT CONTROL CALLBACKS ---
# # Python callback that updates output
# @app.callback(
#     Output("script1-output", "children"),
#     Output("script2-output", "children"),
#     Input("terminal-update", "n_intervals"),
#     State("script1-output", "children"),
#     State("script2-output", "children")
# )
# def update_outputs(_n, prev1, prev2):
#     out1 = prev1 or ""
#     out2 = prev2 or ""
#     while not output_queues["script1"].empty():
#         out1 += output_queues["script1"].get()
#     while not output_queues["script2"].empty():
#         out2 += output_queues["script2"].get()
#     return out1, out2



# @app.callback(Output("start-script1", "n_clicks"),
#               Input("start-script1", "n_clicks"),
#               State("script1-command", "value"),
#               prevent_initial_call=True)
# def start_script1(n, cmd):
#     if n:
#         start_script("script1", cmd.split())
#     return 0

# @app.callback(Output("stop-script1", "n_clicks"),
#               Input("stop-script1", "n_clicks"),
#               prevent_initial_call=True)
# def stop_script1(n):
#     if n:
#         stop_script("script1")
#     return 0

# @app.callback(Output("start-script2", "n_clicks"),
#               Input("start-script2", "n_clicks"),
#               State("script2-command", "value"),
#               prevent_initial_call=True)
# def start_script2(n, cmd):
#     if n:
#         start_script("script2", cmd.split())
#     return 0

# @app.callback(Output("stop-script2", "n_clicks"),
#               Input("stop-script2", "n_clicks"),
#               prevent_initial_call=True)
# def stop_script2(n):
#     if n:
#         stop_script("script2")
#     return 0

# # Add clientside callback for script1
# app.clientside_callback(
#     """
#     function(n_intervals) {
#         const el1 = document.getElementById('script1-output');
#         const el2 = document.getElementById('script2-output');
#         if (el1) el1.scrollTop = el1.scrollHeight;
#         if (el2) el2.scrollTop = el2.scrollHeight;
#         return '';
#     }
#     """,
#     Output("dummy-scroll", "children"),  # this exists, safe to use
#     Input("terminal-update", "n_intervals")
# )





# # ---------------- RUN SERVER ----------------
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=8050, debug=True, use_reloader=False)



# OLD VERSION:

# import dash
# from dash import html, dcc, Input, Output, State
# import dash_daq as daq
# import requests, numpy as np
# from scipy.signal import find_peaks
# import plotly.graph_objs as go
# import subprocess, threading, queue

# #http://10.155.94.101:3000/public-dashboards/eb5da49884b045c194abb81284e53aae

# # ---------------- CONFIG ----------------
# INJECTION_PI_URL = "http://10.19.33.12:8000"  # <-- Replace with your Pi's IP

# # ---------------- CONFIG ----------------
# LASERS = [
#     {"name": "Injection Monitor", "pi_url": INJECTION_PI_URL}  # Add more as needed
# ]
# N = 500
# CALIBRATION_SWEEPS = 20

# SLOWER_LOWER = 1000
# SLOWER_UPPER = 2000
# XBEAM_LOWER = 500
# XBEAM_UPPER = 1000

# # Grafana URL (replace with your dashboard)
# # GRAFANA_URL = "http://10.155.94.101:3000/dashboard/snapshot/NQoctvuzNvMsXqxETbjugqAWhGpwUtOa"
# GRAFANA_URL = "http://10.155.94.101:3000/public-dashboards/eb5da49884b045c194abb81284e53aae?orgId=1&kiosk=tv"

# # ---------------- DASH APP ----------------
# app = dash.Dash(__name__)
# app.title = "Multi-Laser Injection Monitor"

# # ---------------- LAYOUT ----------------
# tabs = []

# for i, laser in enumerate(LASERS):
#     laser_id = f"laser{i+1}"
#     tabs.append(
#         dcc.Tab(label=laser["name"], children=[
#             html.H3(f"{laser['name']} Controls"),
#             dcc.Graph(id=f"{laser_id}-plot"),
#             html.Div([
#                 html.Button("Start Stream", id=f"{laser_id}-start-btn", n_clicks=0),
#                 html.Button("Stop Stream", id=f"{laser_id}-stop-btn", n_clicks=0),
#                 html.Button("Update Peaks", id=f"{laser_id}-update-peaks-btn", n_clicks=0)
#             ], style={"marginTop": 10}),
#             # LEDs
#             html.Div([
#                 html.Label("Xbeam Status:"),
#                 daq.Indicator(id=f"{laser_id}-xbeam-led", value=False, color="green", size=20),
#                 html.Label("Slow Peaks Status:"),
#                 daq.Indicator(id=f"{laser_id}-slow-led", value=False, color="green", size=20)
#             ], style={"display": "flex", "gap": "20px", "marginTop": 10}),
#             html.Div([
#                 html.Label("Holdoff [us]:"),
#                 dcc.Input(id=f"{laser_id}-holdoff-input", type="number", value=0, step=1),
#                 html.Button("Update Holdoff", id=f"{laser_id}-update-holdoff-btn", n_clicks=0)
#             ], style={"marginTop": 10}),
#             html.Div([
#                 html.Label("Refresh Rate [Hz]:"),
#                 dcc.Slider(
#                     id=f"{laser_id}-refresh-slider",
#                     min=0.5, max=5, step=0.1, value=2,
#                     marks={0.5:"0.5",1:"1",2:"2",3:"3",4:"4",5:"5"}
#                 )
#             ], style={"marginTop": 10}),
#             dcc.Interval(id=f"{laser_id}-interval", interval=500, n_intervals=0),
#             dcc.Store(id=f"{laser_id}-peak-bands-store", data={"slow": None, "xbeam": None})
#         ])
#     )

# # Add Grafana tab
# tabs.append(
#     dcc.Tab(label='Grafana Dashboard', children=[
#     html.H3('Historical Data'),
#     html.Iframe(
#         src=GRAFANA_URL,
#         width="100%",
#         height="800",
#         style={"border": "none"}
#     )
# ])
# )


# # app.layout = html.Div([dcc.Tabs(tabs)])

# # ---------------- HELPER FUNCTIONS ----------------
# def detect_peaks(y, height=180, prominence=5, smoothing=True, window=5):
#     if smoothing:
#         y = np.convolve(y, np.ones(window)/window, mode='same')
#     peaks, properties = find_peaks(y, height=height, prominence=prominence)
#     return peaks, properties

# def check_led_status(all_peaks, lower, upper):
#     """Return True if at least one peak is in bounds, else False"""
#     selected = all_peaks[(all_peaks >= lower) & (all_peaks <= upper)] if len(all_peaks) else np.array([])
#     return len(selected) > 0

# # ---------------- CALLBACKS ----------------
# for i, laser in enumerate(LASERS):
#     laser_id = f"laser{i+1}"
#     pi_url = laser["pi_url"]

#     # Update peaks and bands
#     @app.callback(
#         Output(f"{laser_id}-peak-bands-store", "data"),
#         Input(f"{laser_id}-update-peaks-btn", "n_clicks"),
#         prevent_initial_call=True
#     )
#     def update_bands(n_clicks, laser_index=i, pi_url=pi_url):
#         all_peaks = []
#         for _ in range(CALIBRATION_SWEEPS):
#             try:
#                 resp = requests.get(f"{pi_url}/waveform", timeout=1)
#                 y = np.array(resp.json()["data"])
#             except:
#                 y = np.zeros(N)
#             peaks, _ = detect_peaks(y)
#             all_peaks.extend(y[peaks])
#         all_peaks = np.array(all_peaks)
        
#         slow_peaks = all_peaks[(all_peaks >= SLOWER_LOWER) & (all_peaks <= SLOWER_UPPER)] if len(all_peaks) else np.array([])
#         xbeam_peaks = all_peaks[(all_peaks >= XBEAM_LOWER) & (all_peaks <= XBEAM_UPPER)] if len(all_peaks) else np.array([])
        
#         bands = {
#             "slow": {"mean": np.mean(slow_peaks), "std": np.std(slow_peaks)} if len(slow_peaks) else None,
#             "xbeam": {"mean": np.mean(xbeam_peaks), "std": np.std(xbeam_peaks)} if len(xbeam_peaks) else None,
#             "all_peaks": all_peaks.tolist()
#         }
#         return bands

#     # Update plot and LEDs
#     @app.callback(
#         Output(f"{laser_id}-plot", "figure"),
#         Output(f"{laser_id}-xbeam-led", "color"),
#         Output(f"{laser_id}-slow-led", "color"),
#         Input(f"{laser_id}-interval", "n_intervals"),
#         State(f"{laser_id}-peak-bands-store", "data")
#     )
#     def update_plot_and_leds(n, bands, laser_index=i, pi_url=pi_url):
#         try:
#             resp = requests.get(f"{pi_url}/waveform", timeout=1)
#             y = np.array(resp.json()["data"])
#         except:
#             y = np.zeros(N)
#         peaks, _ = detect_peaks(y)
#         peak_vals = y[peaks]

#         # LED status based on bands thresholds
#         # all_peaks = np.array(bands["all_peaks"]) if bands and "all_peaks" in bands else np.array([])
#         # xbeam_ok = check_led_status(all_peaks, 100, 1000)
#         # slow_ok = check_led_status(all_peaks, 700, 1750)
#         all_peaks_live = y[peaks]

#         xbeam_ok = any((all_peaks_live >= XBEAM_LOWER) & (all_peaks_live <= XBEAM_UPPER))
#         slow_ok  = any((all_peaks_live >= SLOWER_LOWER) & (all_peaks_live <= SLOWER_UPPER))

#         xbeam_color = "green" if xbeam_ok else "red"
#         slow_color  = "green" if slow_ok else "red"


#         # Build figure
#         fig = go.Figure()
#         fig.add_trace(go.Scatter(y=y, mode="lines", name="Waveform", line=dict(color="blue", width=2)))
#         fig.add_trace(go.Scatter(x=peaks, y=peak_vals, mode="markers", name="Peaks", marker=dict(color="red", size=6)))

#         if bands:
#             if bands["slow"]:
#                 mean_slow = bands["slow"]["mean"]
#                 std_slow = bands["slow"]["std"]
#                 fig.add_hline(y=mean_slow, line=dict(color="green", dash="dash"), annotation_text="Slow Mean")
#                 fig.add_hrect(y0=mean_slow-std_slow, y1=mean_slow+std_slow, fillcolor="green", opacity=0.2)
#             if bands["xbeam"]:
#                 mean_x = bands["xbeam"]["mean"]
#                 std_x = bands["xbeam"]["std"]
#                 fig.add_hline(y=mean_x, line=dict(color="orange", dash="dash"), annotation_text="Xbeam Mean")
#                 fig.add_hrect(y0=mean_x-std_x, y1=mean_x+std_x, fillcolor="orange", opacity=0.2)

#         fig.update_layout(
#             title=f"{LASERS[laser_index]['name']} Injection Monitor",
#             xaxis={"title": "Sample", "range": [0, N]},
#             yaxis={"title": "ADC Value", "range": [0, 1750]},
#             plot_bgcolor="#f5f5f5",
#             paper_bgcolor="#ffffff",
#             font={"family": "Arial", "size": 12, "color": "#333333"}
#         )

#         return fig, xbeam_color, slow_color

#     # --- Start/Stop Stream ---
#     @app.callback(
#         Output(f"{laser_id}-interval", "disabled"),
#         [Input(f"{laser_id}-start-btn", "n_clicks"), Input(f"{laser_id}-stop-btn", "n_clicks")]
#     )
#     def toggle_stream(start_clicks, stop_clicks):
#         return stop_clicks > start_clicks

#     # --- Holdoff ---
#     @app.callback(
#         Output(f"{laser_id}-holdoff-input", "value"),
#         Input(f"{laser_id}-update-holdoff-btn", "n_clicks"),
#         State(f"{laser_id}-holdoff-input", "value")
#     )
#     def update_holdoff(n_clicks, value):
#         if n_clicks > 0:
#             try:
#                 requests.post(f"{pi_url}/holdoff/{int(value)}", timeout=1)
#             except:
#                 pass
#         return value

#     # --- Refresh Rate Slider ---
#     @app.callback(
#         Output(f"{laser_id}-interval", "interval"),
#         Input(f"{laser_id}-refresh-slider", "value")
#     )
#     def update_interval(value_hz):
#         return int(1000 / value_hz)



# # Store subprocesses and output buffers
# processes = {"script1": None, "script2": None}
# output_queues = {"script1": queue.Queue(), "script2": queue.Queue()}

# # --- HELPER: read stdout asynchronously ---
# def stream_output(name, proc):
#     for line in iter(proc.stdout.readline, b""):
#         output_queues[name].put(line.decode("utf-8"))
#     proc.stdout.close()

# # --- START a script ---
# def start_script(name, command):
#     if processes[name] is None or processes[name].poll() is not None:
#         proc = subprocess.Popen(
#             command,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.STDOUT,
#             text=False,
#             bufsize=1,
#         )
#         processes[name] = proc
#         threading.Thread(target=stream_output, args=(name, proc), daemon=True).start()

# # --- STOP a script ---
# def stop_script(name):
#     if processes[name] and processes[name].poll() is None:
#         processes[name].terminate()
#         processes[name] = None

# # --- BUILD THE NEW TAB LAYOUT ---
# script_tab = dbc.Container([
#     html.H4("Script Control & Monitoring", className="text-info mb-3"),

#     dbc.Row([
#         dbc.Col([
#             html.H5("Script 1 Controls"),
#             dbc.Input(id="script1-command", value="python script1.py", type="text", className="mb-2"),
#             dbc.Button("Start Script 1", id="start-script1", color="success", className="me-2"),
#             dbc.Button("Stop Script 1", id="stop-script1", color="danger", className="mb-3"),
#             html.Pre(id="script1-output", style={
#                 "backgroundColor": "#111",
#                 "color": "#0f0",
#                 "height": "250px",
#                 "overflowY": "scroll",
#                 "padding": "10px",
#                 "border": "1px solid #333",
#                 "borderRadius": "8px"
#             })
#         ], width=6),

#         dbc.Col([
#             html.H5("Script 2 Controls"),
#             dbc.Input(id="script2-command", value="python script2.py", type="text", className="mb-2"),
#             dbc.Button("Start Script 2", id="start-script2", color="success", className="me-2"),
#             dbc.Button("Stop Script 2", id="stop-script2", color="danger", className="mb-3"),
#             html.Pre(id="script2-output", style={
#                 "backgroundColor": "#111",
#                 "color": "#0f0",
#                 "height": "250px",
#                 "overflowY": "scroll",
#                 "padding": "10px",
#                 "border": "1px solid #333",
#                 "borderRadius": "8px"
#             })
#         ], width=6)
#     ]),

#     dcc.Interval(id="terminal-update", interval=1000, n_intervals=0)
# ])

# # --- ADD THIS TAB TO EXISTING DASH APP ---
# tabs = dcc.Tabs([
#     dcc.Tab(label='Oscilloscope Monitor', value='tab1'),
#     dcc.Tab(label='Grafana Dashboard', value='tab2'),
#     dcc.Tab(label='Script Control', value='tab3')
# ], id='tabs', value='tab1')

# app.layout = dbc.Container([
#     html.H2("Lab Dashboard", className="text-center text-light my-3"),
#     tabs,
#     html.Div(id='tab-content')
# ], fluid=True)

# # --- SWITCH BETWEEN TABS ---
# @app.callback(Output('tab-content', 'children'), Input('tabs', 'value'))
# def render_tab(tab):
#     if tab == 'tab3':
#         return script_tab
#     elif tab == 'tab2':
#         return html.Iframe(src="http://your-grafana-url", width="100%", height="800px")
#     else:
#         return html.Div("Your existing oscilloscope monitor layout here")

# # --- SCRIPT CONTROL CALLBACKS ---
# @app.callback(
#     Output("script1-output", "children"),
#     Output("script2-output", "children"),
#     Input("terminal-update", "n_intervals"),
#     prevent_initial_call=False
# )
# def update_outputs(_):
#     out1, out2 = "", ""
#     while not output_queues["script1"].empty():
#         out1 += output_queues["script1"].get()
#     while not output_queues["script2"].empty():
#         out2 += output_queues["script2"].get()
#     return out1, out2

# @app.callback(
#     Output("start-script1", "n_clicks"),
#     Input("start-script1", "n_clicks"),
#     State("script1-command", "value"),
#     prevent_initial_call=True
# )
# def start_script1(n, cmd):
#     if n:
#         start_script("script1", cmd.split())
#     return 0

# @app.callback(
#     Output("stop-script1", "n_clicks"),
#     Input("stop-script1", "n_clicks"),
#     prevent_initial_call=True
# )
# def stop_script1(n):
#     if n:
#         stop_script("script1")
#     return 0

# @app.callback(
#     Output("start-script2", "n_clicks"),
#     Input("start-script2", "n_clicks"),
#     State("script2-command", "value"),
#     prevent_initial_call=True
# )
# def start_script2(n, cmd):
#     if n:
#         start_script("script2", cmd.split())
#     return 0

# @app.callback(
#     Output("stop-script2", "n_clicks"),
#     Input("stop-script2", "n_clicks"),
#     prevent_initial_call=True
# )
# def stop_script2(n):
#     if n:
#         stop_script("script2")
#     return 0

# # --- RUN SERVER ---
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=8050, debug=True)


# # ---------------- RUN ----------------
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=8050, debug=True)
