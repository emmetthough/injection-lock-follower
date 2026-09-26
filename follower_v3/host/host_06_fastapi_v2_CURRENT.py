import serial, re, time
import threading
import numpy as np
import queue
import requests
import RPi.GPIO as GPIO

from fastapi import FastAPI
from fastapi.responses import JSONResponse
import copy
import traceback

PORT = "/dev/ttyACM0"
BAUD = 250000
TIMEOUT = 1
N = 500
OPERATION_TIMEOUT = 30  # seconds to wait for an in-progress operation to finish

# --- GPIO Pin Definitions (change as needed) ---
PIN_BLUE_LOCK_STATUS = 11      # Output: blue laser lock status (good/bad)
PIN_SLOWER_FEEDBACK_EN = 13    # Output: slower feedback enable
PIN_XBEAM_FEEDBACK_EN = 15     # Output: xbeam feedback enable
PIN_SLOWER_LOCK_STATE = 16      # Input: slower lock state
PIN_SLOWER_FAIL_STATE = 29      # Input: slower fail state
PIN_XBEAM_LOCK_STATE = 18      # Input: xbeam lock state
PIN_XBEAM_FAIL_STATE = 31      # Input: xbeam fail state

app = FastAPI()
running = True
running_data = {
    "scan": np.zeros(N).tolist(),
    "peaks": None,
    "tracking": None,
    "stats": None
}
command_queue = queue.Queue()
data_lock = threading.Lock()
# Event used to indicate a long-running operation (e.g. reading/parsing a block) is in progress.
# When set, endpoints will refuse to enqueue new commands and the serial loop will avoid
# sending queued commands until the operation completes.
operation_in_progress = threading.Event()

# --- GPIO Setup ---
GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)
GPIO.setup(PIN_BLUE_LOCK_STATUS, GPIO.OUT)
GPIO.setup(PIN_SLOWER_FEEDBACK_EN, GPIO.OUT)
GPIO.setup(PIN_XBEAM_FEEDBACK_EN, GPIO.OUT)
GPIO.setup(PIN_SLOWER_LOCK_STATE, GPIO.IN)
GPIO.setup(PIN_SLOWER_FAIL_STATE, GPIO.IN)
GPIO.setup(PIN_XBEAM_LOCK_STATE, GPIO.IN)
GPIO.setup(PIN_XBEAM_FAIL_STATE, GPIO.IN)

DASH_URL = "http://10.155.94.105:8050"  # Change if dashboard_v1 runs elsewhere

# --- GPIO/Status Thread ---
def gpio_status_thread():
    prev_states = {"slower_lock": None, "xbeam_lock": None}  # track last sent states
    while running:
        try:
            # Query Dash for blue laser lock status
            r = requests.get(f"{DASH_URL}/api/blue_lock_status", timeout=2)
            if r.ok:
                status = r.json().get("locked", False)
                GPIO.output(PIN_BLUE_LOCK_STATUS, GPIO.HIGH if status else GPIO.LOW)
            # Query Dash for feedback slider states
            r_fb = requests.get(f"{DASH_URL}/api/feedback_status", timeout=2)
            if r_fb.ok:
                fb = r_fb.json()
                GPIO.output(PIN_SLOWER_FEEDBACK_EN, GPIO.HIGH if fb.get("slower", False) else GPIO.LOW)
                GPIO.output(PIN_XBEAM_FEEDBACK_EN, GPIO.HIGH if fb.get("xbeam", False) else GPIO.LOW)

            # Read lock/fail input pins
            slower_lock = GPIO.input(PIN_SLOWER_LOCK_STATE)
            slower_fail = GPIO.input(PIN_SLOWER_FAIL_STATE)
            xbeam_lock = GPIO.input(PIN_XBEAM_LOCK_STATE)
            xbeam_fail = GPIO.input(PIN_XBEAM_FAIL_STATE)

            # --- NEW: send lock state updates to Dash ---
            lock_payload = {"slower": bool(slower_lock), "xbeam": bool(xbeam_lock)}
            if lock_payload != prev_states:
                try:
                    requests.post(f"{DASH_URL}/api/lock_status", json=lock_payload, timeout=2)
                    prev_states = lock_payload.copy()
                except Exception as e:
                    print(f"[GPIO Thread] Failed to post lock state: {e}")

            # Handle failure state for slower
            if slower_fail == GPIO.HIGH:
                # Override feedback enable (set GPIO low)
                GPIO.output(PIN_SLOWER_FEEDBACK_EN, GPIO.LOW)
                # Send Z[S] over serial
                command_queue.put("ZS\n")
                # Notify Dash to set LED RED and slider OFF
                try:
                    requests.post(f"{DASH_URL}/api/override_status", json={"channel": "slower", "status": "fail"}, timeout=2)
                except Exception as e:
                    print(f"[GPIO Thread] Dash override (slower) failed: {e}")

            # Handle failure state for xbeam
            if xbeam_fail == GPIO.HIGH:
                GPIO.output(PIN_XBEAM_FEEDBACK_EN, GPIO.LOW)
                command_queue.put("ZX\n")
                try:
                    requests.post(f"{DASH_URL}/api/override_status", json={"channel": "xbeam", "status": "fail"}, timeout=2)
                except Exception as e:
                    print(f"[GPIO Thread] Dash override (xbeam) failed: {e}")

        except Exception as e:
            print(f"[GPIO Thread] Error: {e}")
            GPIO.output(PIN_BLUE_LOCK_STATUS, GPIO.LOW)
            GPIO.output(PIN_SLOWER_FEEDBACK_EN, GPIO.LOW)
            GPIO.output(PIN_XBEAM_FEEDBACK_EN, GPIO.LOW)
        time.sleep(0.5)

ser = serial.Serial(PORT, BAUD, timeout=TIMEOUT)
time.sleep(1)
ser.reset_input_buffer()
ser.reset_output_buffer()
ser.write(b"I\n")
print("Initalization complete")

def read_until(ser, target):
    """Read lines until target string is found."""
    lines = []
    while True:
        line = ser.readline().decode(errors="ignore").strip()
        if not line:
            continue
        lines.append(line)
        if target in line:
            break
    return lines

def parse_tracking_line(line):
    """Parse a single line of active peak tracking."""
    if not line.startswith(("High", "Low")):
        return None
    peak_type = line.split()[0]  # High or Low
    pos = int(re.search(r"pos=(\d+)", line).group(1))
    val = int(re.search(r"val=(\d+)", line).group(1))
    status = re.search(r"status=(\w+)", line).group(1)
    return peak_type, pos, val, status


def read_trace(ser):
    try:
        raw_data = ser.read(2*N)
        scan = np.frombuffer(raw_data, dtype=np.uint16)
        # print(f"Received {len(scan)} samples.")
        return scan.tolist()
    except Exception:
        return np.zeros(N).tolist()

def interpret(lines):
    if "BEGIN_peaks" in lines[0]:
        slower_peaks = []
        slower_peak_pos = []
        xbeam_peaks = []
        xbeam_peak_pos = []
        try:
            for line in lines[1:-1]:
                val = float(re.search(r"val=(\d+)", line).group(1))
                pos = float(re.search(r"pos=(\d+)", line).group(1))

                if "high" in line.lower():
                    slower_peaks.append(val)
                    slower_peak_pos.append(pos)
                if "low" in line.lower():
                    xbeam_peaks.append(val)
                    xbeam_peak_pos.append(pos)
            return "peaks", {
                "slower": {"vals": slower_peaks, "pos": slower_peak_pos},
                "xbeam": {"vals": xbeam_peaks, "pos": xbeam_peak_pos},
            }
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing peaks data: ", e)
    
    if "BEGIN_clusters" in lines[0]:
        slower_mean_clusters = []
        xbeam_mean_clusters = []
        try:
            for line in lines[1:-1]:
                if "high" in line.lower():
                    slower_mean_clusters.append(float(line.split("=")[-1].split()[-1]))
                if "low" in line.lower():
                    xbeam_mean_clusters.append(float(line.split("=")[-1].split()[-1]))
            return "clusters", {"slower": slower_mean_clusters, "xbeam": xbeam_mean_clusters}
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing cluster data: ", e)
        
    if "BEGIN_tracking" in lines[0]:
        slower_peaks = {"pos": [], "val": [], "status": []}
        xbeam_peaks = {"pos": [], "val": [], "status": []}
        try:
            for line in lines[1:-1]:
                parsed = parse_tracking_line(line)
                if parsed:
                    peak_type, pos, val, status = parsed
                    if peak_type.lower() == "high":
                        slower_peaks["pos"].append(pos)
                        slower_peaks["val"].append(val)
                        slower_peaks["status"].append(status)
                    elif peak_type.lower() == "low":
                        xbeam_peaks["pos"].append(pos)
                        xbeam_peaks["val"].append(val)
                        xbeam_peaks["status"].append(status)
            return "tracking", {"slower": slower_peaks, "xbeam": xbeam_peaks}
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing tracking data: ", e)
        
    if "BEGIN_stats" in lines[0]:
        try:
            slower_height = float(lines[1].split("=")[1].split()[0])
            slower_std = float(lines[1].split("=")[2].split()[0])
            xbeam_height = float(lines[3].split("=")[1].split()[0])
            xbeam_std = float(lines[3].split("=")[2].split()[0])
            return "stats", {
                "slower": {"height": slower_height, "std": slower_std}, 
                "xbeam": {"height": xbeam_height, "std": xbeam_std},
            }
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing stats data: ", e)

def read_purity_curve(ser):
    try:
        begin_slower_line = ser.readline().decode(errors='ignore').strip()
        n_slower_steps = int(ser.readline().decode(errors='ignore').strip().split('=')[-1])
        
        slower_steps = np.array([int(x) for x in ser.readline().decode().strip().split(',')[:-1]])
        slower_peaks_up = np.array([int(x) for x in ser.readline().decode().strip().split(',')[:-1]])
        slower_peaks_down = np.array([int(x) for x in ser.readline().decode().strip().split(',')[:-1]])

        x = read_until(ser, "Xbeam size")
        xbeam_steps = np.array([int(x) for x in ser.readline().decode().strip().split(',')[:-1]])
        xbeam_peaks_up = np.array([int(x) for x in ser.readline().decode().strip().split(',')[:-1]])
        xbeam_peaks_down = np.array([int(x) for x in ser.readline().decode().strip().split(',')[:-1]])
        y = read_until(ser, "START Slopes")
        slower_fbparams = np.array([float(x) for x in ser.readline().decode().strip().split(",")])
        xbeam_fbparams = np.array([float(x) for x in ser.readline().decode().strip().split(",")])
        return "spectral purity curve", {
            "slower": {"steps": slower_steps, "peaks": {"up": slower_peaks_up, "down": slower_peaks_down},\
                "fb": {"m": slower_fbparams[0], "x0": slower_fbparams[1], "y0": slower_fbparams[2]}},
            "xbeam": {"steps": xbeam_steps, "peaks": {"up": xbeam_peaks_up, "down": xbeam_peaks_down}, \
                "fb": {"m": xbeam_fbparams[0], "x0": xbeam_fbparams[1], "y0": xbeam_fbparams[2]}},
        }
    except Exception as e:
        print("Error reading spectral purity curve! ", e)
        
def serial_read_loop():
    global running_data
    global ser
    global running
    global command_queue
    # with serial.Serial(PORT, BAUD, timeout=TIMEOUT) as ser:
    # time.sleep(1.0)  # allow the Arduino to reset on connection

    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    ser.write(b"I\n")
    # ser.write(b"S\n")

    try:
        while running:
            if ser.in_waiting:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                if "[DEBUG]" in line:
                    print(line)
                if "[START]" in line:
                    # mark that a blocking/long-running operation is in progress
                    operation_in_progress.set()
                    try:
                        if "Trace" in line:
                            scan = read_trace(ser)
                            # protect running_data with the lock
                            with data_lock:
                                running_data["scan"] = scan
                            # print(f"Received scan with {len(scan)} samples, sum of {np.sum(scan)}")
                        elif "Spectral" in line:
                            key, spc = read_purity_curve(ser)
                            with data_lock:
                                running_data[key] = spc
                            # clear any pending commands that may interfere with spectral operation
                            try:
                                while True:
                                    command_queue.get_nowait()
                            except queue.Empty:
                                pass
                        else:
                            lines_to_interpret = read_until(ser, "[END]")
                            try:
                                key, data = interpret(lines_to_interpret[:-1])
                                with data_lock:
                                    running_data[key] = data
                                # print(f"Received {key} data: ")
                                # print(data)
                                # print()
                            except Exception as e:
                                print(e)
                    finally:
                        # operation done, allow endpoints to enqueue/send commands again
                        operation_in_progress.clear()

            # check for pending commands (only send if no operation currently in progress)
            try:
                if not operation_in_progress.is_set():
                    cmd = command_queue.get_nowait()
                    # print("Found command in queue: ", cmd)
                    ser.write(cmd.encode())
                    # print("Wrote command")
            except queue.Empty:
                pass
            time.sleep(0.01)
    except serial.SerialException as e:
        print("Serial error:", e)
        time.sleep(1)
    except Exception as e:
        print("Unexpected serial error: ", e)


def wait_for_operation_clear(timeout=OPERATION_TIMEOUT, poll_interval=0.05):
    """Block until operation_in_progress is cleared or timeout expires.
    Returns True if the operation cleared, False if timed out.
    """
    start = time.time()
    while operation_in_progress.is_set():
        if time.time() - start >= timeout:
            return False
        time.sleep(poll_interval)
    return True


def snapshot_running_data():
    """Return a deep copy snapshot of running_data under the data_lock."""
    with data_lock:
        return copy.deepcopy(running_data)

# ------------------- FastAPI Routes -------------------
@app.get("/waveform")
def get_waveform():
    cmd = f"R\n"
    # Waveform is not important during an ongoing operation: skip enqueueing and return current snapshot
    if operation_in_progress.is_set():
        snap = snapshot_running_data()
        return JSONResponse(content={"skipped": True, "scan_data": snap.get("scan"), "tracking_data": snap.get("tracking")})

    # not busy: enqueue and return current snapshot immediately (don't wait)
    command_queue.put(cmd)
    snap = snapshot_running_data()
    return JSONResponse(content={"skipped": False, "scan_data": snap.get("scan"), "tracking_data": snap.get("tracking")})

@app.get("/peaks")
def get_peaks():
    with data_lock:
        data = running_data.get("peaks") or {}
    return JSONResponse(content=data)

@app.get("/clusters")
def get_clusters():
    with data_lock:
        data = running_data.get("clusters") or {}
    return JSONResponse(content=data)

@app.get("/tracking")
def get_tracking():
    with data_lock:
        data = running_data.get("tracking") or {}
    return JSONResponse(content=data)

@app.get("/stats")
def get_stats():
    with data_lock:
        data = running_data.get("stats") or {}
    return JSONResponse(content=data)

@app.post("/holdoff/{value}")
def set_holdoff(value: int):
    cmd = f"D{value}\n"
    # Always enqueue; wait for current operation to finish (up to timeout)
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.post("/init")
def reinitialize_peaks():
    cmd = "I\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.post("/SPC/{startmA}/{stopmA}/{stepuA}")
def get_SPC(startmA: float, stopmA: float, stepuA: float):
    cmd = f"S{startmA},{stopmA},{stepuA}\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.post("/C/{variable}/{value}")
def change_var(variable: str, value: float):
    cmd = f"C{variable},{value}\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})
    
@app.post("/TD")
def toggle_debug():
    cmd = "TD\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.post("/ZS")
def zero_slower():
    cmd = "ZS\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.post("/ZX")
def zero_xbeam():
    cmd = "ZX\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.post("/Z")
def reset_all():
    cmd = "Z\n"
    command_queue.put(cmd)
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    return JSONResponse(content={"status": "queued", "command": cmd, "operation_cleared": operation_cleared})

@app.on_event("startup")
def startup_event():
    threading.Thread(target=serial_read_loop, daemon=True).start()
    threading.Thread(target=gpio_status_thread, daemon=True).start()
    print("Serial reader thread started.")
    print("GPIO status thread started.")


@app.get("/status")
def get_status():
    with data_lock:
        snap = {"running": running, "operation_in_progress": operation_in_progress.is_set(), "queue_size": command_queue.qsize(), "serial_open": getattr(ser, "is_open", False)}
    return JSONResponse(content=snap)


@app.get("/spc")
def get_spc():
    """Return the latest spectral purity curve in a JSON-serializable form."""
    with data_lock:
        spc = running_data.get("spectral purity curve")
    if not spc:
        return JSONResponse(content={})

    # Convert numpy arrays to lists recursively
    def _convert(o):
        if isinstance(o, dict):
            return {k: _convert(v) for k, v in o.items()}
        try:
            if isinstance(o, np.ndarray):
                return o.tolist()
        except Exception:
            pass
        if isinstance(o, list):
            return [_convert(x) for x in o]
        return o

    return JSONResponse(content=_convert(spc))

@app.post("/FB")
def send_fb():
    """Enqueue the 'FB' command to be sent to the device.
    Returns queued status and whether any currently running operation cleared within the timeout.
    """
    try:
        cmd = "FB\n"
        command_queue.put(cmd)

        operation_cleared = True
        if operation_in_progress.is_set():
            operation_cleared = wait_for_operation_clear()

        return JSONResponse(content={
            "status": "queued",
            "command": cmd,
            "operation_cleared": operation_cleared
        })
    except Exception as e:
        # Print full traceback to server console for debugging; keep client response concise
        print(f"Error in /FB endpoint: {e}")
        print(traceback.format_exc())
        return JSONResponse(status_code=500, content={"status": "error", "error": str(e)})
    
@app.on_event("shutdown")
def shutdown_event():
    command_queue.put("Z\n")  # send zero feedback command on shutdown
    operation_cleared = True
    if operation_in_progress.is_set():
        operation_cleared = wait_for_operation_clear()
    global running
    running = False
    ser.close()