"""Log an ESP32's serial output to a file without resetting it.

Opening a port with pyserial's defaults asserts DTR/RTS, which on these boards holds the
chip in reset (or drops it into download mode). This opens with both de-asserted, and
reopens if the port vanishes (USB re-enumeration after a reset or flash).

    python serial_log.py /dev/cu.usbmodem1101 1800 /tmp/s3run.log

Run it in the background, tail the log with `less +F`, then tap RST on the board.
"""
import sys
import time

import serial

port, secs, path = sys.argv[1], float(sys.argv[2]), sys.argv[3]
end = time.time() + secs
with open(path, 'w') as out:
    while time.time() < end:
        try:
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, 115200, 0.5
            s.dtr = s.rts = False
            s.open()
            while time.time() < end:
                d = s.read(4096)
                if d:
                    out.write(d.decode(errors='replace'))
                    out.flush()
        except (serial.SerialException, OSError):
            time.sleep(0.5)
