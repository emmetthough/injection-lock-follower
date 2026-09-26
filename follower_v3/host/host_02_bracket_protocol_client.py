import serial
import re
import time
import numpy as np

PORT = "/dev/ttyACM0"
BAUD = 250000
TIMEOUT = 1
N = 500

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
    raw_data = ser.read(2*N)
    scan = np.frombuffer(raw_data, dtype=np.uint16)
    print(f"Received {len(scan)} samples.")
    return scan

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
            return "peaks", (slower_peaks, slower_peak_pos, xbeam_peaks, xbeam_peak_pos)
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
            return "clusters", (slower_mean_clusters, xbeam_mean_clusters)
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing cluster data: ", e)
        
    if "BEGIN_tracking" in lines[0]:
        slower_peaks = [[],[],[]]
        xbeam_peaks = [[],[],[]]
        try:
            for line in lines[1:-1]:
                parsed = parse_tracking_line(line)
                if parsed:
                    peak_type, pos, val, status = parsed
                    if peak_type.lower() == "high":
                        slower_peaks[0].append(pos)
                        slower_peaks[1].append(val)
                        slower_peaks[2].append(status)
                    elif peak_type.lower() == "low":
                        xbeam_peaks[0].append(pos)
                        xbeam_peaks[1].append(val)
                        xbeam_peaks[2].append(status)
            return "tracking", (slower_peaks, xbeam_peaks)
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing tracking data: ", e)
        
    if "BEGIN_stats" in lines[0]:
        try:
            slower_height = float(lines[1].split("=")[1].split()[0])
            slower_std = float(lines[1].split("=")[2].split()[0])
            xbeam_height = float(lines[2].split("=")[1].split()[0])
            xbeam_std = float(lines[2].split("=")[2].split()[0])
            return "stats", (slower_height, slower_std, xbeam_height, xbeam_std)
        except Exception as e:
            print(e)
            print(lines)
            raise ValueError("Error parsing stats data: ", e)

def main():
    running_data = {}
    with serial.Serial(PORT, BAUD, timeout=TIMEOUT) as ser:
        
        print("Opening serial port...")
        time.sleep(1.0)  # allow the Arduino to reset on connection

        ser.reset_input_buffer()
        ser.reset_output_buffer()
        
        ser.write(b"I\n")

        try:
            while True:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                if "[START]" in line:
                    if "Trace" in line:
                        scan = read_trace(ser)
                        running_data["scan"] = scan
                        print(f"Received scan with {scan.size} samples, sum of {np.sum(scan)}")
                    else:
                        lines_to_interpret = read_until(ser, "[END]")
                        try:
                            key, data = interpret(lines_to_interpret)
                            running_data[key] = data
                            print(f"Received {key} data: ")
                            print(data)
                            print()
                        except Exception as e:
                            print(e)
        except KeyboardInterrupt:
            print("Exiting.")

if __name__ == "__main__":
    main()
