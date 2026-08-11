#!/usr/bin/env python3
import socket, sys
from datetime import datetime
HOST = "127.0.0.1"
PORT = 10110
LOGFILE = sys.argv[1] if len(sys.argv) > 1 else "/home/john/ais_capture.log"
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind((HOST, PORT))
print(f"ais_udp_listener: listening on {HOST}:{PORT} -> {LOGFILE}", flush=True)
buffer = b""
with open(LOGFILE, "a") as log:
    while True:
        try:
            data, _ = sock.recvfrom(4096)
        except KeyboardInterrupt:
            break
        buffer += data
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            ts = datetime.now().isoformat(timespec="seconds")
            out = f"{ts} {text}"
            print(out, flush=True)
            log.write(out + "\n")
            log.flush()
