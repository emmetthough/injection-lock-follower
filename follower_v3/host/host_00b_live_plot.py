import serial
import struct
import matplotlib.pyplot as plt
import numpy as np
import time

# ----------------- CONFIG -----------------
PORT = "/dev/ttyACM0"  # Arduino port
BAUD = 250000
N = 500                 # Must match Arduino
TIMEOUT = 5

MAX_CLUSTERS = 3        # From Arduino

# ----------------- SERIAL UTILS -----------------
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
            high_peaks.append((pos, val, status))
        elif line.startswith("Low"):
            parts = line.split()
            pos = int(parts[2].split('=')[1])
            val = int(parts[3].split('=')[1])
            status = parts[4].split('=')[1]
            low_peaks.append((pos, val, status))
    return high_peaks, low_peaks

# ----------------- MAIN -----------------
def main():
    ser = serial.Serial(PORT, BAUD, timeout=TIMEOUT)
    time.sleep(2)  # Wait for Arduino reset
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    print("Sending initialization command to Arduino...")
    ser.write(b"R\n")  # Initialize peaks on Arduino

    # ----------------- PLOT SETUP -----------------
    plt.ion()
    fig, ax = plt.subplots(figsize=(10,4))
    trace_line, = ax.plot([], [], lw=1, c='k', label='ADC Trace')
    high_scat = ax.scatter([], [], c='r', label='High Peaks')
    low_scat  = ax.scatter([], [], c='g', label='Low Peaks')
    ax.set_xlim(0, N-1)
    ax.set_ylim(0, 4095)
    ax.set_xlabel("Sample Index")
    ax.set_ylabel("ADC Value")
    ax.set_title("Fabry-Perot Active Peak Tracking")
    ax.legend()
    fig.show()
    fig.canvas.draw()

    try:
        while True:
            # ----------------- READ STATUS -----------------
            lines = read_until(ser, "Active peak tracking:")
            lines += read_until(ser, "High 0" if MAX_CLUSTERS>0 else "Low 0")  # read peaks
            high_peaks, low_peaks = parse_peak_status(lines)

            # ----------------- READ SCAN DATA -----------------
            read_until(ser, "BEGIN_SCAN")
            raw_data = ser.read(2*N)
            scan = np.frombuffer(raw_data, dtype=np.uint16)

            # ----------------- UPDATE PLOT -----------------
            trace_line.set_data(np.arange(N), scan)

            # Update high peaks
            if high_peaks:
                xh = [p[0] for p in high_peaks]
                yh = [p[1] for p in high_peaks]
                high_scat.set_offsets(np.c_[xh, yh])
            else:
                high_scat.set_offsets(np.c_[[],[]])

            # Update low peaks
            if low_peaks:
                xl = [p[0] for p in low_peaks]
                yl = [p[1] for p in low_peaks]
                low_scat.set_offsets(np.c_[xl, yl])
            else:
                low_scat.set_offsets(np.c_[[],[]])

            ax.relim()
            ax.autoscale_view()
            fig.canvas.draw()
            fig.canvas.flush_events()

            # ----------------- PRINT STATUS -----------------
            for p in high_peaks:
                print(f"High peak pos={p[0]} val={p[1]} status={p[2]}")
            for p in low_peaks:
                print(f"Low peak pos={p[0]} val={p[1]} status={p[2]}")
            print("-"*50)

    except KeyboardInterrupt:
        print("Stopped by user.")
        ser.close()

if __name__ == "__main__":
    main()
