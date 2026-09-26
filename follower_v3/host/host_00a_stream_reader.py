import serial
import re
import time

PORT = "/dev/ttyACM0"
BAUD = 250000
TIMEOUT = 1

def parse_active_line(line):
    """Parse a single line of active peak tracking."""
    if not line.startswith(("High", "Low")):
        return None
    peak_type = line.split()[0]  # High or Low
    pos = int(re.search(r"pos=(\d+)", line).group(1))
    val = int(re.search(r"val=(\d+)", line).group(1))
    status = re.search(r"status=(\w+)", line).group(1)
    return peak_type, pos, val, status

def main():
    with serial.Serial(PORT, BAUD, timeout=TIMEOUT) as ser:
        print("Waiting for Arduino...")
        time.sleep(2)
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        # Trigger initialization scan
        ser.write(b"R\n")

        print("Reading active monitoring stream. Ctrl+C to exit.")
        try:
            while True:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                parsed = parse_active_line(line)
                if parsed:
                    peak_type, pos, val, status = parsed
                    print(f"{peak_type} peak at pos={pos} val={val} status={status}")
        except KeyboardInterrupt:
            print("Exiting.")

if __name__ == "__main__":
    main()
