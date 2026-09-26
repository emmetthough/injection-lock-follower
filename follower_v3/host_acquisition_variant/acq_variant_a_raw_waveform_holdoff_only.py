# acquisition_server.py
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import serial, threading, time, numpy as np

BAUD_RATE = 250000
N = 500
SERIAL_PORT = "/dev/ttyACM0"

app = FastAPI()
latest_samples = np.zeros(N)
holdoff_value = 0
running = True

# Serial read loop
def read_from_arduino():
    global latest_samples
    ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    print("Connected to Arduino")
    time.sleep(0.1)
    while running:
        try:
            ser.write(b"R\n")
            time.sleep(0.05)
            if ser.in_waiting >= 2*N:
                raw = ser.read(2*N)
                latest_samples = np.frombuffer(raw, dtype=np.uint16)
        except Exception as e:
            print("Read error:", e)
            time.sleep(1)

@app.get("/waveform")
def get_waveform():
    return JSONResponse(content={"data": latest_samples.tolist()})

@app.post("/holdoff/{value}")
def set_holdoff(value: int):
    global holdoff_value
    holdoff_value = value
    # Send to Arduino
    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
        ser.write(f"D{value}\n".encode())
        response = ser.readline().decode().strip()
        ser.close()
        return {"status": "ok", "arduino_response": response}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.on_event("startup")
def startup_event():
    threading.Thread(target=read_from_arduino, daemon=True).start()
