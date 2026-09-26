import serial
import struct
import matplotlib.pyplot as plt

PORT = "/dev/ttyACM0"   # change if needed, e.g. COM5 on Windows
BAUD = 250000
N = 500                 # samples per scan (must match Arduino)
TIMEOUT = 5             # seconds for serial reads

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

def parse_peaks(lines):
    """Extract peak info from Arduino output."""
    high_peaks = []
    low_peaks = []
    for line in lines:
        if line.startswith("High"):
            parts = line.split()
            val = int(parts[3].split("=")[1])
            pos = int(parts[4].split("=")[1])
            high_peaks.append((pos, val))
        elif line.startswith("Low"):
            parts = line.split()
            val = int(parts[3].split("=")[1])
            pos = int(parts[4].split("=")[1])
            low_peaks.append((pos, val))
    return high_peaks, low_peaks

def read_scan_data(ser):
    """Read raw buffer after BEGIN_SCAN."""
    data = bytearray()
    while True:
        line = ser.readline()
        if b"END_SCAN" in line:
            break
        data += line
    # interpret as unsigned 16-bit integers
    scan = list(struct.unpack("<" + "H"*(len(data)//2), data))
    return scan

def main():
    with serial.Serial(PORT, BAUD, timeout=TIMEOUT) as ser:
        print("Waiting for Arduino to initialize...")
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        input("Press ENTER to start a scan...")

        ser.write(b"R\n")
        lines = read_until(ser, "END_RESULTS")
        high_peaks, low_peaks = parse_peaks(lines)
        print("High peaks:", high_peaks)
        print("Low peaks:", low_peaks)

        # Now read scan data
        read_until(ser, "BEGIN_SCAN")
        scan = read_scan_data(ser)
        print(f"Received {len(scan)} samples.")

    # Plot
    plt.figure(figsize=(8,4))
    plt.plot(scan, lw=1)
    for (x, y) in high_peaks:
        plt.plot(x, y, 'ro', label="High peaks" if 'High peaks' not in plt.gca().get_legend_handles_labels()[1] else "")
    for (x, y) in low_peaks:
        plt.plot(x, y, 'go', label="Low peaks" if 'Low peaks' not in plt.gca().get_legend_handles_labels()[1] else "")
    plt.title("Fabry?Perot Scan with Peak Detection")
    plt.xlabel("Sample index")
    plt.ylabel("ADC value")
    plt.legend()
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    main()
