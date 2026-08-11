#!/usr/bin/env python3
"""
ais_api.py — lightweight HTTP API that serves live AIS vessel positions as JSON.
Reads from ais_capture.log and decodes on the fly. Runs on port 8080.
"""
import json
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

LOGFILE = Path.home() / "ais_capture.log"
LOGFILE_ROT = Path.home() / "ais_capture.log.1"

# ── AIS decoder (shared logic) ──────────────────────────────────────────────

def ais_char_to_6bit(c):
    val = ord(c) - 48
    return ((val - 8) if val > 39 else val) & 0x3F

def decode_payload(payload):
    bits = []
    for c in payload:
        v = ais_char_to_6bit(c)
        for i in range(5, -1, -1):
            bits.append((v >> i) & 1)
    while len(bits) < 424:
        bits.append(0)
    return bits

def get_bits(bits, start, length):
    val = 0
    for i in range(length):
        idx = start + i
        if idx >= len(bits):
            break
        val = (val << 1) | bits[idx]
    return val

def get_signed_bits(bits, start, length):
    val = get_bits(bits, start, length)
    if val >= (1 << (length - 1)):
        val -= (1 << length)
    return val

def decode_sixbit_text(bits, start, num_chars):
    s = ''
    for i in range(num_chars):
        c = get_bits(bits, start + i * 6, 6)
        if c == 0:
            break
        s += chr(c + 64) if c < 32 else chr(c)
    return s.rstrip('@ ').strip()

def is_valid_position(lat, lon):
    """Check if lat/lon represent a real position (not AIS null/default).

    AIS spec defines "not available" sentinels: longitude=181, latitude=91.
    Some devices emit (0, 0) when no GPS fix is available. Both produce
    bogus map markers (e.g. in the South China Sea or Gulf of Guinea)
    and must be filtered out.
    """
    if lat == 91 or lon == 181:          # AIS spec "not available"
        return False
    if lat == 0 and lon == 0:             # no GPS fix
        return False
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return False
    return True

NAV_STATUS = {
    0: "Under way", 1: "At anchor", 2: "Not under command",
    3: "Restricted", 4: "Constrained", 5: "Moored",
    6: "Aground", 7: "Fishing", 8: "Under sail",
    14: "AIS-SART", 15: "Unknown",
}

SHIP_TYPES = {
    30: "Fishing", 31: "Towing", 33: "Dredger", 35: "Military",
    36: "Sailing", 37: "Pleasure", 40: "High speed", 50: "Pilot",
    51: "SAR", 52: "Tug", 53: "Port tender", 60: "Passenger",
    70: "Cargo", 80: "Tanker",
}

# ── Vessel store (updated by log reader thread) ─────────────────────────────

vessels = {}  # mmsi -> dict
vessels_lock = threading.Lock()

def parse_line(line):
    if '!AIVDM' not in line and '!AIVDO' not in line:
        return
    parts = line.split(' ', 1)
    if len(parts) < 2:
        return
    ts = parts[0]
    vdm = parts[1].split(',')
    if len(vdm) < 6:
        return
    payload = vdm[5]
    channel = vdm[4]
    bits = decode_payload(payload)
    msg_type = get_bits(bits, 0, 6)
    mmsi = get_bits(bits, 8, 30)

    with vessels_lock:
        if msg_type in (1, 2, 3):
            lon = get_signed_bits(bits, 61, 28) / 600000.0
            lat = get_signed_bits(bits, 89, 27) / 600000.0
            speed = get_bits(bits, 50, 10) / 10.0
            course = get_bits(bits, 116, 12) / 10.0
            heading = get_bits(bits, 128, 9)
            nav = get_bits(bits, 38, 4)
            entry = vessels.setdefault(mmsi, {})
            entry.update({
                'mmsi': mmsi,
                'speed': speed, 'course': course, 'heading': heading,
                'nav_status': NAV_STATUS.get(nav, str(nav)),
                'channel': channel, 'last_pos': ts,
            })
            if is_valid_position(lat, lon):
                entry['lat'] = lat
                entry['lon'] = lon
            else:
                # Drop stale bogus position from a previous valid fix
                entry.pop('lat', None)
                entry.pop('lon', None)
        elif msg_type == 5:
            name = decode_sixbit_text(bits, 112, 20)
            ship_type = get_bits(bits, 232, 8)
            dest = decode_sixbit_text(bits, 302, 20)
            vessels.setdefault(mmsi, {}).update({
                'mmsi': mmsi, 'name': name,
                'ship_type': SHIP_TYPES.get(ship_type, f"Type {ship_type}"),
                'dest': dest, 'last_static': ts,
            })
        elif msg_type == 24:
            part = get_bits(bits, 38, 2)
            if part == 0:
                name = decode_sixbit_text(bits, 40, 20)
                vessels.setdefault(mmsi, {}).update({'mmsi': mmsi, 'name': name, 'last_static': ts})
            elif part == 1:
                ship_type = get_bits(bits, 40, 8)
                vessels.setdefault(mmsi, {}).update({
                    'mmsi': mmsi,
                    'ship_type': SHIP_TYPES.get(ship_type, f"Type {ship_type}"),
                    'last_static': ts,
                })

# ── Log tail thread ─────────────────────────────────────────────────────────

def tail_log():
    # Tail the AIS log, handling logrotate copytruncate.
    while not LOGFILE.exists():
        time.sleep(2)
    while True:
        try:
            with open(LOGFILE) as f:
                # Parse existing lines
                for line in f:
                    parse_line(line.strip())
                # Tail new lines, detecting truncation
                while True:
                    pos = f.tell()
                    line = f.readline()
                    if line:
                        parse_line(line.strip())
                    else:
                        # Check if file was truncated (copytruncate)
                        try:
                            size = LOGFILE.stat().st_size
                        except OSError:
                            size = pos
                        if pos > size:
                            # File was truncated, reopen from start
                            break
                        time.sleep(1)
        except (OSError, IOError):
            time.sleep(2)

# ── HTTP server ─────────────────────────────────────────────────────────────


# ── Heatmap (historical position density) ───────────────────────────────────

from collections import defaultdict as _dd
from datetime import timedelta as _td

HEATMAP_BIN = 0.005  # ~0.5 km grid cells

def build_heatmap(max_hours=24):
    """Scan log files and build a lat/lon density grid.
    Returns list of [lat, lon, weight] for leaflet.heat."""
    grid = _dd(int)  # (lat_bin, lon_bin) -> count

    logfiles = []
    if LOGFILE.exists():
        logfiles.append(LOGFILE)
    if LOGFILE_ROT.exists():
        logfiles.append(LOGFILE_ROT)

    cutoff_ts = None
    if max_hours > 0:
        cutoff = datetime.now() - _td(hours=max_hours)
        cutoff_ts = cutoff.isoformat(timespec="seconds")

    for logfile in logfiles:
        try:
            with open(logfile) as f:
                for line in f:
                    if "!AIVDM" not in line and "!AIVDO" not in line:
                        continue
                    parts = line.split(" ", 1)
                    if len(parts) < 2:
                        continue
                    ts = parts[0]
                    if cutoff_ts and ts < cutoff_ts:
                        continue
                    vdm = parts[1].split(",")
                    if len(vdm) < 6:
                        continue
                    payload = vdm[5]
                    bits = decode_payload(payload)
                    msg_type = get_bits(bits, 0, 6)
                    if msg_type not in (1, 2, 3):
                        continue
                    lon = get_signed_bits(bits, 61, 28) / 600000.0
                    lat = get_signed_bits(bits, 89, 27) / 600000.0
                    if not is_valid_position(lat, lon):
                        continue
                    lat_bin = round(lat / HEATMAP_BIN) * HEATMAP_BIN
                    lon_bin = round(lon / HEATMAP_BIN) * HEATMAP_BIN
                    grid[(lat_bin, lon_bin)] += 1
        except (OSError, IOError):
            continue

    points = []
    for (lat, lon), count in sorted(grid.items()):
        points.append([round(lat, 5), round(lon, 5), count])
    return points

class AISHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/vessels':
            with vessels_lock:
                # Filter out vessels with null/default positions from the response.
                # Vessels with only static data (no lat/lon keys) are still included.
                all_vessels = list(vessels.values())
                filtered = []
                for v in all_vessels:
                    lat = v.get('lat')
                    lon = v.get('lon')
                    if lat is not None and lon is not None:
                        if not is_valid_position(lat, lon):
                            continue
                    filtered.append(v)
                data = {
                    'timestamp': datetime.utcnow().isoformat() + 'Z',
                    'count': len(filtered),
                    'vessels': filtered,
                }
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.write = self.wfile.write
            self.write(body)
        elif self.path == '/api/heatmap' or self.path.startswith('/api/heatmap?'):
            from urllib.parse import urlparse, parse_qs
            max_hours = 24
            if '?' in self.path:
                params = parse_qs(urlparse(self.path).query)
                if 'hours' in params:
                    try:
                        max_hours = int(params['hours'][0])
                    except ValueError:
                        pass
            points = build_heatmap(max_hours)
            data = {
                'timestamp': datetime.now().isoformat(),
                'max_hours': max_hours,
                'bin_size': HEATMAP_BIN,
                'point_count': len(points),
                'points': points,
            }
            body = json.dumps(data, default=str).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # quiet

def main():
    t = threading.Thread(target=tail_log, daemon=True)
    t.start()
    server = HTTPServer(('0.0.0.0', 8080), AISHandler)
    print(f"ais_api: listening on :8080, reading {LOGFILE}", flush=True)
    server.serve_forever()

if __name__ == '__main__':
    main()
