from fastapi import FastAPI
from fastapi.responses import JSONResponse
import serial, threading, time, numpy as np

BAUD_RATE = 250000
N = 500
SERIAL_PORT = "/dev/ttyACM0"
MAX_CLUSTERS = 3

app = FastAPI()
latest_samples = np.zeros(N)
latest_high_peaks = []
latest_low_peaks = []
holdoff_value = 0
running = True

# ----------------- UTILS -----------------
def parse_peak_status(lines):
    """Parse lines like 'High 0 pos=123 val=456 status=OK'"""
    high_peaks = []
    low_peaks = []
    for line in lines:
        if line.startswith("High"):
            parts = line.split()
            pos = int(parts[2].split('=')[1])
            val = int(parts[3].split('=')[1])
            status = parts[4].split('=')[1]
            high_peaks.append({"pos": pos, "val": val, "status": status})
        elif line.startswith("Low"):
            parts = line.split()
            pos = int(parts[2].split('=')[1])
            val = int(parts[3].split('=')[1])
            status = parts[4].split('=')[1]
            low_peaks.append({"pos": pos, "val": val, "status": status})
    return high_peaks, low_peaks

# ----------------- SERIAL READ LOOP -----------------
def read_from_arduino():
    global latest_samples, latest_high_peaks, latest_low_peaks
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    print("Connected to Arduino")
    time.sleep(0.1)

    while running:
        try:
            ser.write(b"R\n")  # request scan / re-init peaks
            time.sleep(0.05)

            # ----------------- Read status lines -----------------
            status_lines = []
            while ser.in_waiting:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                status_lines.append(line)
                if "Active peak tracking:" in line:
                    break

            # Parse peaks if available
            high_peaks, low_peaks = parse_peak_status(status_lines)
            if high_peaks:
                latest_high_peaks = high_peaks
            if low_peaks:
                latest_low_peaks = low_peaks

            # ----------------- Read waveform -----------------
            if ser.in_waiting >= 2*N:
                raw = ser.read(2*N)
                latest_samples = np.frombuffer(raw, dtype=np.uint16)

        except Exception as e:
            print("Read error:", e)
            time.sleep(0.5)

# ----------------- API ENDPOINTS -----------------
@app.get("/waveform")
def get_waveform():
    return JSONResponse(content={"data": latest_samples.tolist()})

@app.get("/peaks")
def get_peaks():
    return JSONResponse(content={
        "high_peaks": latest_high_peaks,
        "low_peaks": latest_low_peaks
    })

@app.post("/holdoff/{value}")
def set_holdoff(value: int):
    global holdoff_value
    holdoff_value = value
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        ser.write(f"D{value}\n".encode())
        response = ser.readline().decode().strip()
        ser.close()
        return {"status": "ok", "arduino_response": response}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# ----------------- STARTUP EVENT -----------------
@app.on_event("startup")
def startup_event():
    threading.Thread(target=read_from_arduino, daemon=True).start()
