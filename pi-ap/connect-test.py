import serial, threading

s = serial.Serial('/dev/ttyACM0', 115200, timeout=0.1)
s.dtr = True

def reader():
    while True:
        line = s.readline().decode(errors='ignore').strip()
        if line:
            print('<', line)

t = threading.Thread(target=reader, daemon=True)
t.start()

while True:
    cmd = input('> ')
    s.write((cmd + '\n').encode())