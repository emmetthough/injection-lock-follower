import subprocess, threading, queue, time

output_queue = queue.Queue()

def stream_output(proc):
    for line in iter(proc.stdout.readline, ''):
        output_queue.put(line)
    proc.stdout.close()

cmd = "python -u D:\\Projects\\Injection_Web_Monitoring\\test.py"
proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, shell=True)
threading.Thread(target=stream_output, args=(proc,), daemon=True).start()

while True:
    while not output_queue.empty():
        print(output_queue.get(), end='')
    time.sleep(0.1)
