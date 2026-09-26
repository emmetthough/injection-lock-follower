import serial, re, time
import threading
import numpy as np
import queue
import matplotlib.pyplot as plt
# import requests


PORT = "/dev/ttyACM0"
BAUD = 250000
TIMEOUT = 1
N = 500

running = True
running_data = {
    "scan": np.zeros(N).tolist(),
    "peaks": None,
    "tracking": None,
    "stats": None
}
command_queue = queue.Queue()

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
        
def main():
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
                if "[START]" in line:
                    if "Trace" in line:
                        scan = read_trace(ser)
                        running_data["scan"] = scan
                        # print(f"Received scan with {len(scan)} samples, sum of {np.sum(scan)}")
                    elif "Spectral" in line:
                        key, spc = read_purity_curve(ser)
                        running_data[key] = spc
                        print("Recieved spectral purity curve!")
                        if spc is not None:
                            fig,ax = plt.subplots(2,1)
                            ax[0].plot(spc["slower"]["steps"], spc["slower"]["peaks"]["up"], label="up")
                            ax[0].plot(spc["slower"]["steps"], spc["slower"]["peaks"]["down"], label="down")
                            ax[0].plot([spc["slower"]["fb"]["x0"], spc["slower"]["fb"]["x0"]+300], spc["slower"]["fb"]["y0"]+np.array([1, spc["slower"]["fb"]["m"]*300]), lw=5, alpha=0.7, label="slope")
                            ax[0].legend(fontsize=10, loc="best")
                            ax[0].set_xlabel("DAC Out [bit]", fontsize=10)
                            ax[0].set_ylabel("Peak height [bit]", fontsize=10)
                            ax[0].set_title("Slower", fontsize=12)
                            ax[1].plot(spc["xbeam"]["steps"], spc["xbeam"]["peaks"]["up"], label="up")
                            ax[1].plot(spc["xbeam"]["steps"], spc["xbeam"]["peaks"]["down"], label="down")
                            ax[1].plot([spc["xbeam"]["fb"]["x0"], spc["xbeam"]["fb"]["x0"]-300], spc["xbeam"]["fb"]["y0"]+np.array([1, -spc["xbeam"]["fb"]["m"]*300]), lw=5, alpha=0.7, label="slope")
                            ax[1].legend(fontsize=10, loc="best")
                            ax[1].set_xlabel("DAC Out [bit]", fontsize=10)
                            ax[1].set_ylabel("Peak height [bit]", fontsize=10)
                            ax[1].set_title("Xbeam", fontsize=12)
                            fig.tight_layout()
                            plt.show()
                    else:
                        lines_to_interpret = read_until(ser, "[END]")
                        try:
                            key, data = interpret(lines_to_interpret[:-1])
                            running_data[key] = data
                            print(f"Received {key} data: ")
                            print(data)
                            print()
                        except Exception as e:
                            print(e)
                            
            else:

                # check for pending commands
                try:
                    cmd = command_queue.get_nowait()
                    # print("Found command in queue: ", cmd)
                    ser.write(cmd.encode())
                    # print("Wrote command")
                except queue.Empty:
                    pass
                time.sleep(0.01)
                
                cmd = input("Send command?")
                command_queue.put(cmd)
    except serial.SerialException as e:
        print("Serial error:", e)
        time.sleep(1)
    except Exception as e:
        print("Unexpected serial error: ", e)
        
if __name__ == '__main__':
    main()