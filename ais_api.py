#!/usr/bin/env python3
"""
ais_api.py — lightweight HTTP API that serves live AIS vessel positions
and historical heatmap density as JSON. Reads from ais_capture.log and
decodes on the fly. Runs on port 8080.

Endpoints:
  /api/vessels          — live vessel state (tails the capture log)
  /api/heatmap?hours=N  — vessel-presence density grid (N hours, 0 = all logs)

Heatmap method (vessel-presence):
  Old method counted every decoded sentence, so a docked ferry beaconing
  every 2 s dominated the map (transmission rate != presence), Class B
  position reports (types 18/19) were ignored, and only the two newest
  log files were read.

  The new method counts distinct (vessel, grid cell, 10-minute bucket)
  presences across ALL rotated logs (log, .1, .2.gz ...), so heat
  accumulates where vessels actually spend time, at a rate every vessel
  contributes equally regardless of beacon interval.

Data integrity:
  - Only single-fragment sentences are treated as position reports.
    Types 1/2/3/18/19 are ALWAYS single-fragment in the AIS spec. A
    multi-fragment *continuation* payload (e.g. part 2 of a type-5
    static report) decoded as a standalone frame produces phantom
    vessels at (0, 113.6) — the tail padding bits of the pair. This was
    the source of "vessels" in the South China Sea on a Port Townsend
    receiver.
  - Sentinel (91/181) and no-fix (0,0) positions are dropped.
  - A geo-fence centered on the median received position (radius
    AIS_FENCE_KM, default 100 km) drops any transmitter with a broken
    GPS from smearing the map.
  - rtl_ais already enforces the 16-bit data-link CRC before emitting a
    sentence (verified in aisdecoder/lib/protodec.c), so log lines are
    CRC-valid at the source.
"""
import gzip
import json
import math
import os
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

LOGFILE = Path.home() / "ais_capture.log"

HEATMAP_BIN = 0.005                                  # ~0.5 km grid cells
HEATMAP_BUCKET_S = 600                               # vessel-presence bucket = 10 min
HEATMAP_FENCE_KM = float(os.environ.get("AIS_FENCE_KM", "100"))
HEATMAP_CACHE_TTL = 300                              # seconds between full rescans

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

def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))

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
    # Only decode the FIRST fragment of a sentence group. Everything we
    # use (type 1/2/3/18/19 positions, type 5/24 names) lives in
    # fragment 1. Decoding a continuation payload standalone yields
    # phantom positions — e.g. part 2 of a type-5 pair decodes as a
    # "type 3" vessel at (0, 113.6).
    if vdm[2] != '1':
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
                'mmsi': mmsi, 'speed': speed, 'course': course,
                'heading': heading,
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
        elif msg_type in (18, 19):
            # Class B "CS" position report: same fields as 1/2/3 but the
            # position block sits 4 bits earlier (no nav status field).
            lon = get_signed_bits(bits, 57, 28) / 600000.0
            lat = get_signed_bits(bits, 85, 27) / 600000.0
            speed = get_bits(bits, 46, 10) / 10.0
            course = get_bits(bits, 112, 12) / 10.0
            entry = vessels.setdefault(mmsi, {})
            entry.update({
                'mmsi': mmsi, 'speed': speed, 'course': course,
                'channel': channel, 'last_pos': ts,
            })
            if is_valid_position(lat, lon):
                entry['lat'] = lat
                entry['lon'] = lon
            else:
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

# ── Heatmap (vessel-presence density) ───────────────────────────────────────

_heatmap_cache = {}  # max_hours -> (expires_at, payload)

def _iter_capture_logs():
    """All capture logs including rotations: log, .1, .2.gz, ... .7.gz.

    logrotate runs daily with `copytruncate delaycompress rotate 7`, so
    yesterday's log is .1 (uncompressed) and older ones are .gz.
    """
    return sorted(Path.home().glob("ais_capture.log*"), key=lambda p: p.name)

def scan_position_frames(cutoff_ts=None):
    """Yield (ts, mmsi, lat, lon) for plausible position reports.

    Single-fragment position messages only: types 1/2/3 (Class A) and
    18/19 (Class B) — per the AIS spec these are always one fragment.
    Continuation fragments of multi-part sentences are not position
    reports; decoding them standalone produces garbage coordinates.
    """
    for logfile in _iter_capture_logs():
        try:
            opener = gzip.open if logfile.suffix == '.gz' else open
            with opener(logfile, 'rt', errors='replace') as f:
                for line in f:
                    if '!AIVDM' not in line and '!AIVDO' not in line:
                        continue
                    parts = line.split(' ', 1)
                    if len(parts) < 2:
                        continue
                    ts = parts[0]
                    if cutoff_ts and ts < cutoff_ts:
                        continue
                    vdm = parts[1].split(',')
                    if len(vdm) < 6:
                        continue
                    # Position reports are always single-fragment
                    if vdm[1] != '1' or vdm[2] != '1':
                        continue
                    payload = vdm[5]
                    if not payload:
                        continue
                    # First payload char encodes the first 6 bits (= msg
                    # type): '1'/'2'/'3' = Class A positions, 'B'=18 and
                    # 'C'=19 = Class B positions.
                    c0 = payload[0]
                    if c0 not in '123BC':
                        continue
                    # Only the bits we read live below 144; decoding 24
                    # chars (padded) is ~2x faster than the full payload.
                    bits = decode_payload(payload[:24])
                    mmsi = get_bits(bits, 8, 30)
                    if c0 in '123':
                        lon = get_signed_bits(bits, 61, 28) / 600000.0
                        lat = get_signed_bits(bits, 89, 27) / 600000.0
                    else:  # 'B' (18) / 'C' (19): Class B, no nav status
                        lon = get_signed_bits(bits, 57, 28) / 600000.0
                        lat = get_signed_bits(bits, 85, 27) / 600000.0
                    if not is_valid_position(lat, lon):
                        continue
                    yield ts, mmsi, lat, lon
        except (OSError, IOError):
            continue

def build_heatmap(max_hours=24):
    """Vessel-presence density grid.

    Weight per grid cell = number of distinct (vessel, cell, 10-minute
    bucket) presences. A docked ferry beaconing every 2 seconds adds 6
    units per hour (not 1800); a yacht that transits a cell in 10
    minutes adds 1. Heat accumulates where vessel TIME is spent, not
    where transmitters are chatty.
    """
    cached = _heatmap_cache.get(max_hours)
    if cached and time.time() < cached[0]:
        return cached[1]

    cutoff_ts = None
    if max_hours > 0:
        cutoff_ts = (datetime.now() - timedelta(hours=max_hours)) \
            .isoformat(timespec='seconds')

    payload = {
        'timestamp': datetime.now().isoformat(),
        'max_hours': max_hours,
        'bin_size': HEATMAP_BIN,
        'bucket_seconds': HEATMAP_BUCKET_S,
        'fence_km': HEATMAP_FENCE_KM,
        'method': 'vessel-presence',
    }

    frames = list(scan_position_frames(cutoff_ts))
    if not frames:
        payload.update({'point_count': 0, 'vessel_count': 0, 'points': []})
        _heatmap_cache[max_hours] = (time.time() + HEATMAP_CACHE_TTL, payload)
        return payload

    # Geo-fence: center on the median received position. The median is
    # robust to outliers; combined with the fragment guard this stops
    # both phantom positions and any real transmitter with a broken GPS.
    lats = sorted(f[2] for f in frames)
    lons = sorted(f[3] for f in frames)
    clat, clon = lats[len(lats) // 2], lons[len(lons) // 2]

    presence = set()   # (mmsi, cell_lat, cell_lon, bucket)
    vessels = set()
    bucket_min = HEATMAP_BUCKET_S // 60
    for ts, mmsi, lat, lon in frames:
        if haversine_km(clat, clon, lat, lon) > HEATMAP_FENCE_KM:
            continue
        vessels.add(mmsi)
        cell_lat = round(lat / HEATMAP_BIN) * HEATMAP_BIN
        cell_lon = round(lon / HEATMAP_BIN) * HEATMAP_BIN
        bucket = ts[:14] + '%02d' % ((int(ts[14:16]) // bucket_min) * bucket_min)
        presence.add((mmsi, cell_lat, cell_lon, bucket))

    grid = {}
    for mmsi, cell_lat, cell_lon, bucket in presence:
        key = (cell_lat, cell_lon)
        grid[key] = grid.get(key, 0) + 1

    points = [[round(lat, 5), round(lon, 5), cnt]
              for (lat, lon), cnt in sorted(grid.items())]
    payload.update({
        'point_count': len(points),
        'vessel_count': len(vessels),
        'points': points,
    })
    _heatmap_cache[max_hours] = (time.time() + HEATMAP_CACHE_TTL, payload)
    return payload

# ── HTTP server ─────────────────────────────────────────────────────────────

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
            self.wfile.write(body)
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
            data = build_heatmap(max_hours)
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
