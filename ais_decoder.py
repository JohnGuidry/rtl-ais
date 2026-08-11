#!/usr/bin/env python3
"""
ais_decoder.py — decode AIS NMEA sentences from ais_capture.log and display vessel info.

Usage:
    python3 ais_decoder.py [logfile]          # parse once and print summary
    python3 ais_decoder.py -w [logfile]       # watch mode — live updates as data arrives
    python3 ais_decoder.py -w                 # watch mode, default log file
"""
import sys
import time
from datetime import datetime
from pathlib import Path

LOGFILE = Path.home() / "ais_capture.log"

# AIS 6-bit ASCII lookup
def ais_char_to_6bit(c):
    val = ord(c) - 48
    if val > 39:
        val -= 8
    return val & 0x3F

def decode_payload(payload):
    bits = []
    for c in payload:
        v = ais_char_to_6bit(c)
        for i in range(5, -1, -1):
            bits.append((v >> i) & 1)
    # Pad to 424 bits (max AIS message length)
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

SHIP_TYPES = {
    0: "Not available", 20: "Wing in ground", 30: "Fishing", 31: "Towing",
    32: "Towing (large)", 33: "Dredger", 34: "Diving ops", 35: "Military",
    36: "Sailing", 37: "Pleasure craft", 40: "High speed craft", 50: "Pilot",
    51: "SAR", 52: "Tug", 53: "Port tender", 54: "Anti-pollution",
    55: "Law enforcement", 60: "Passenger", 70: "Cargo", 80: "Tanker",
    90: "Other",
}

def nav_status_str(code):
    statuses = {
        0: "Under way using engine", 1: "At anchor", 2: "Not under command",
        3: "Restricted maneuverability", 4: "Constrained by draft", 5: "Moored",
        6: "Aground", 7: "Engaged in fishing", 8: "Under way sailing",
        9: "Reserved (HSC)", 10: "Reserved (WIG)", 11: "Power-driven vessel towing astern",
        12: "Power-driven vessel pushing ahead", 13: "Reserved",
        14: "AIS-SART/MOB-AIS/EPIRB-AIS", 15: "Not defined",
    }
    return statuses.get(code, f"Unknown ({code})")

vessels = {}  # mmsi -> dict of latest known data

def parse_line(line):
    """Parse one !AIVDM line and update vessels dict."""
    if '!AIVDM' not in line and '!AIVDO' not in line:
        return None, None
    
    parts = line.split(' ', 1)
    timestamp = parts[0] if len(parts) > 1 else ''
    vdm_parts = parts[-1].split(',')
    
    if len(vdm_parts) < 6:
        return None, None
    
    payload = vdm_parts[5]
    channel = vdm_parts[4]
    
    bits = decode_payload(payload)
    msg_type = get_bits(bits, 0, 6)
    mmsi = get_bits(bits, 8, 30)
    
    if msg_type in (1, 2, 3):  # Position reports
        nav_status = get_bits(bits, 38, 4)
        speed = get_bits(bits, 50, 10) / 10.0
        lon_raw = get_signed_bits(bits, 61, 28)
        lat_raw = get_signed_bits(bits, 89, 27)
        course = get_bits(bits, 116, 12) / 10.0
        heading = get_bits(bits, 128, 9)
        
        lon = lon_raw / 600000.0
        lat = lat_raw / 600000.0
        
        vessels.setdefault(mmsi, {}).update({
            'mmsi': mmsi,
            'msg_type': msg_type,
            'lat': lat,
            'lon': lon,
            'speed': speed,
            'course': course,
            'heading': heading,
            'nav_status': nav_status,
            'nav_status_str': nav_status_str(nav_status),
            'channel': channel,
            'last_pos': timestamp,
        })
        return msg_type, mmsi
    
    elif msg_type == 5:  # Static and voyage data
        name = decode_sixbit_text(bits, 112, 20)
        ship_type = get_bits(bits, 232, 8)
        dest = decode_sixbit_text(bits, 302, 20)
        
        vessels.setdefault(mmsi, {}).update({
            'mmsi': mmsi,
            'msg_type': 5,
            'name': name,
            'ship_type': ship_type,
            'ship_type_str': SHIP_TYPES.get(ship_type, f"Type {ship_type}"),
            'dest': dest,
            'last_static': timestamp,
        })
        return 5, mmsi
    
    elif msg_type == 24:  # Static data report (part A/B)
        part = get_bits(bits, 38, 2)
        if part == 0:  # Part A - name
            name = decode_sixbit_text(bits, 40, 20)
            vessels.setdefault(mmsi, {}).update({
                'mmsi': mmsi,
                'msg_type': 24,
                'name': name,
                'last_static': timestamp,
            })
        elif part == 1:  # Part B - type
            ship_type = get_bits(bits, 40, 8)
            vessels.setdefault(mmsi, {}).update({
                'mmsi': mmsi,
                'msg_type': 24,
                'ship_type': ship_type,
                'ship_type_str': SHIP_TYPES.get(ship_type, f"Type {ship_type}"),
                'last_static': timestamp,
            })
        return 24, mmsi
    
    return msg_type, mmsi

def print_summary():
    """Print a summary of all known vessels."""
    print(f"\n{'='*80}")
    print(f"  AIS Vessel Summary — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  {len(vessels)} vessel(s) tracked")
    print(f"{'='*80}\n")
    
    for mmsi in sorted(vessels.keys()):
        v = vessels[mmsi]
        name = v.get('name', 'Unknown')
        ship_type = v.get('ship_type_str', '')
        
        print(f"  MMSI {mmsi}")
        if name:
            print(f"    Name: {name}")
        if ship_type:
            print(f"    Type: {ship_type}")
        if 'lat' in v:
            print(f"    Position: {v['lat']:.6f}, {v['lon']:.6f}")
            print(f"    Speed: {v['speed']} kn  Course: {v['course']}°  Heading: {v['heading']}°")
            print(f"    Status: {v['nav_status_str']}")
            print(f"    Last position: {v['last_pos']}")
        if 'dest' in v and v['dest']:
            print(f"    Destination: {v['dest']}")
        print()

def main():
    watch = '-w' in sys.argv
    logfile = None
    for arg in sys.argv[1:]:
        if not arg.startswith('-'):
            logfile = Path(arg)
            break
    if logfile is None:
        logfile = LOGFILE
    
    if not logfile.exists():
        print(f"Log file not found: {logfile}")
        print(f"Waiting for it to appear...")
        while not logfile.exists():
            time.sleep(2)
    
    # Parse all existing lines
    with open(logfile) as f:
        for line in f:
            line = line.strip()
            if line:
                parse_line(line)
    
    print_summary()
    
    if not watch:
        return
    
    # Watch mode — tail the file
    print(f"\nWatching {logfile} for new messages... (Ctrl+C to stop)\n")
    with open(logfile) as f:
        f.seek(0, 2)  # seek to end
        while True:
            line = f.readline()
            if line:
                line = line.strip()
                if line:
                    msg_type, mmsi = parse_line(line)
                    if msg_type in (1, 2, 3) and mmsi in vessels:
                        v = vessels[mmsi]
                        name = v.get('name', f'MMSI {mmsi}')
                        print(f"  [{line[:19]}] {name}: {v['lat']:.6f}, {v['lon']:.6f} — "
                              f"{v['speed']} kn, {v['course']}°, {v['nav_status_str']}")
                    elif msg_type == 5:
                        v = vessels[mmsi]
                        print(f"  [{line[:19]}] Static: {v.get('name', '?')} — "
                              f"{v.get('ship_type_str', '?')}, dest: {v.get('dest', '?')}")
                    elif msg_type == 24:
                        v = vessels[mmsi]
                        name = v.get('name', '?')
                        stype = v.get('ship_type_str', '')
                        print(f"  [{line[:19]}] Static: {name} {f'({stype})' if stype else ''}")
            else:
                time.sleep(1)

if __name__ == '__main__':
    main()
