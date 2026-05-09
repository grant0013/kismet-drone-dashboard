#!/usr/bin/env python3
"""
drone-dashboard: Flask app that polls Kismet's REST API and renders
a focused live view of drones + non-AP devices + a live GPS map.

Detects drones via:
- Kismet's built-in UAV phy (uav_match rules in /etc/kismet/kismet_uav.conf
  + extended UK police/enterprise/delivery patterns in conf/kismet_site_uav_uk.conf)
- SSID / manufacturer regex heuristics for drones not yet in the kismet rules
- Remote ID broadcasts on WiFi (NaN beacons) AND Bluetooth 5 Long Range

Persists Operator IDs (regulator-issued, e.g. UK CAA `GBR-OP-XXXXXXXXX`) to
SQLite for longitudinal tracking. Pattern-matches the operator's free-text
Self-ID against ~50 known UK organisations (police forces, fire/rescue,
delivery trial fleets, surveying, etc).

Optional ntfy push notifications when new operators appear or RSSI exceeds
a threshold (drone overhead).

GitHub: https://github.com/grant0013/kismet-drone-dashboard
License: MIT
"""

from flask import Flask, jsonify, render_template_string, request
import requests
import threading
import time
import re
import os
import logging
import sqlite3
import json
import subprocess
import shutil
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

# -----------------------------------------------------------------------------
# config

KISMET_URL  = os.environ.get("KISMET_URL",  "http://localhost:2501")
KISMET_CONF = os.environ.get("KISMET_CONF", "/etc/kismet/kismet_httpd.conf")
HTTP_HOST   = os.environ.get("HTTP_HOST",   "0.0.0.0")
HTTP_PORT   = int(os.environ.get("HTTP_PORT", "8081"))
POLL_SECS   = int(os.environ.get("POLL_SECS", "5"))
HISTORY_LEN = int(os.environ.get("HISTORY_LEN", "200"))

# Station coordinates — default map centre when no GPS data.
# Override via STATION_LAT / STATION_LON / STATION_NAME env vars in your
# systemd unit's EnvironmentFile (default fallback is central London).
STATION_LAT  = float(os.environ.get("STATION_LAT",  "51.5074"))
STATION_LON  = float(os.environ.get("STATION_LON",  "-0.1278"))
STATION_NAME = os.environ.get("STATION_NAME", "Drone Station")

# Persistent operator log
SQLITE_DB = os.environ.get("SQLITE_DB", "/var/lib/drone-dashboard/operators.db")

# ntfy push notifications — set NTFY_URL to e.g. https://ntfy.sh/your-topic-here
# Free service, no signup. Pick a hard-to-guess topic name.
NTFY_URL  = os.environ.get("NTFY_URL", "")
NTFY_RSSI_THRESHOLD = int(os.environ.get("NTFY_RSSI_THRESHOLD", "-60"))

# Drone heuristics — supplements kismet_uav.conf rules for SSIDs / manufacturers
# we want to flag even without explicit uav_match rules
DRONE_SSID_RE = re.compile(
    r"(?i)\b("
    r"DJI[-_]|MAVIC|SPARK|PHANTOM|TELLO|OSMO|MINI[-_]?\d|AVATA|INSPIRE|"
    r"M30[T]?|M300|M350|MATRICE|"
    r"BEBOP|ANAFI|PARROT|SKYDIO|AUTEL|EVO[-_]|YUNEEC|TYPHOON|HUBSAN|"
    r"POTENSIC|HOLY[-_]?STONE|RYZE|FIMI|RUKO|SNAPTAIN|XBM[-_]|YD[-_]?UFO|"
    r"WING[-_]?DELIVERY|MANNA[-_]|SKYPORTS|APIAN[-_]"
    r")"
)
DRONE_MANUF_RE = re.compile(
    r"(?i)\b(DJI|Parrot|Skydio|Autel|Yuneec|Hubsan|Ryze|3D ?Robotics|FIMI|Propel|Wing|Manna|Skyports)\b"
)

# -----------------------------------------------------------------------------
# Bluetooth identification helpers
# Kismet's BT helper passively listens; manufacturer-data parsing is limited.
# We enrich what we can: BLE Local Name → vendor inference, MAC → LE Privacy
# detection, BT SIG company ID lookup for when manuf data IS surfaced.

# Top ~120 Bluetooth SIG company IDs — covers ~95% of consumer devices.
# Full list at https://bitbucket.org/bluetooth-SIG/public/raw/main/assigned_numbers/company_identifiers/company_identifiers.yaml
BT_COMPANY_IDS = {
    0x0001: "Nokia Mobile Phones", 0x0002: "Intel Corp.", 0x0006: "Microsoft",
    0x0009: "Infineon Technologies AG", 0x000A: "Cambridge Silicon Radio",
    0x000D: "Texas Instruments Inc.", 0x000F: "Broadcom Corporation",
    0x0010: "Mitel Semiconductor", 0x002A: "Alps Electric Co., Ltd.",
    0x002F: "Hewlett-Packard Company", 0x0030: "ST Microelectronics",
    0x0036: "Plantronics, Inc.", 0x003D: "iAnywhere Solutions",
    0x0043: "Nintendo Co., Ltd.", 0x0046: "MediaTek, Inc.",
    0x0049: "Polar Electro OY", 0x004C: "Apple, Inc.",
    0x004F: "Linear Technology", 0x0054: "Toyota Motor Corporation",
    0x0058: "Hitachi Ltd.", 0x0059: "Nordic Semiconductor ASA",
    0x0063: "Plantronics", 0x0065: "Hewlett Packard Enterprise",
    0x006B: "Linear Technology", 0x0075: "Samsung Electronics Co. Ltd.",
    0x0085: "BlueRadios, Inc.", 0x0087: "Garmin International, Inc.",
    0x008A: "Realtek Semiconductor Corporation", 0x008C: "Bose Corporation",
    0x009E: "Bose Corporation", 0x00A0: "Onset Computer Corporation",
    0x00C4: "LG Electronics", 0x00DC: "Philips Lighting (Hue)",
    0x00E0: "Google", 0x00FE: "Atmel Corporation",
    0x0102: "Tile, Inc.", 0x010F: "MediaTek, Inc.",
    0x0120: "iSPORTS Analytics", 0x011D: "Bose Corporation",
    0x0131: "Cypress Semiconductor / Infineon",
    0x0140: "Bose Corporation", 0x0157: "Anhui Huami / Xiaomi Mi Band",
    0x015D: "Sony Corporation", 0x0171: "Amazon.com Services LLC",
    0x017A: "Yamaha Corporation", 0x017C: "HTC Corporation",
    0x0184: "LG Electronics Inc.", 0x019A: "Sennheiser electronic GmbH",
    0x019F: "LIFX, Inc.", 0x01A4: "OnePlus Electronics",
    0x01D6: "Withings", 0x01D7: "OnePlus",
    0x01DA: "Realtek", 0x0203: "Bose Corporation",
    0x0257: "Withings", 0x025E: "Anker Innovations",
    0x0265: "Logitech International SA", 0x027D: "Govee",
    0x02E0: "Polar Electro", 0x02E5: "Withings",
    0x0399: "Logitech, Inc.", 0x03DA: "Apple, Inc.",
    0x03D5: "Samsung Electronics", 0x05B5: "Govee",
    0x064F: "Tuya Smart", 0x0834: "JBL / Harman",
}

# Common BT Service UUIDs that immediately identify a device class
BT_SERVICE_UUIDS = {
    "0000180f": "Battery", "0000180a": "Device Info", "00001812": "HID",
    "00001801": "GATT", "0000110b": "Audio Sink", "0000110a": "Audio Source",
    "00001108": "Headset", "0000fe9f": "Apple Notification (ANCS)",
    "0000feaa": "Google Eddystone Beacon", "0000feed": "Tile Tracker",
    "0000fd5a": "Apple AirTag", "0000fe2c": "Google Fast Pair",
    "0000fd43": "Amazon", "0000fd6f": "Apple Continuity",
    "0000fd5d": "Bluetooth Mesh", "0000fe07": "Sonos",
    "0000fdef": "Polar", "0000fdcc": "Tile",
    "0000fe25": "Apple Health", "0000fd86": "Garmin",
}

# Vendor inference from BLE Local Name regex patterns
BT_NAME_VENDOR_HINTS = [
    (re.compile(r"(?i)^Govee"), "Govee"),
    (re.compile(r"(?i)^Mi[\s_-]|^Xiaomi|^Amazfit"), "Xiaomi / Amazfit"),
    (re.compile(r"(?i)^(iPhone|iPad|iPod|MacBook|AirPods|AirTag|HomePod|Apple)"), "Apple"),
    (re.compile(r"(?i)^Galaxy|^SM-|^Buds"), "Samsung"),
    (re.compile(r"(?i)^Tile"), "Tile, Inc."),
    (re.compile(r"(?i)^(Sony|WH-|WF-|WI-|MDR-|SRS-|LinkBuds)"), "Sony"),
    (re.compile(r"(?i)^(Bose|QC|Soundlink|QuietComfort)"), "Bose"),
    (re.compile(r"(?i)^Sennheiser|Momentum"), "Sennheiser"),
    (re.compile(r"(?i)^Square[\s_]"), "Square (POS terminal)"),
    (re.compile(r"(?i)^(Polar|H7|H10|OH1|Verity|Vantage)"), "Polar"),
    (re.compile(r"(?i)^(Garmin|Forerunner|Fenix|vivo|Edge\s)"), "Garmin"),
    (re.compile(r"(?i)^(Fitbit|Charge|Versa|Sense|Inspire|Luxe)"), "Fitbit"),
    (re.compile(r"(?i)^(LG\s|LG-|webOS|LGTV)"), "LG"),
    (re.compile(r"(?i)^(SmartTV|Samsung TV|Tv$)"), "Samsung TV"),
    (re.compile(r"(?i)^Hue\s|^Philips\sHue"), "Philips Hue"),
    (re.compile(r"(?i)^(TP-Link|Tapo|Kasa)"), "TP-Link / Tapo"),
    (re.compile(r"(?i)^Tuya"), "Tuya"),
    (re.compile(r"(?i)^(Echo|Alexa|Kindle|Ring|Eero|Fire)"), "Amazon"),
    (re.compile(r"(?i)^(Nest|Pixel|Chromecast|Wear OS)"), "Google"),
    (re.compile(r"(?i)^OnePlus|^OP\s"), "OnePlus"),
    (re.compile(r"(?i)^(WS|WBS|BPM|Body|ScanWatch|Steel HR)"), "Withings"),
    (re.compile(r"(?i)^(JBL|Charge|Flip|Clip|Boombox|Tune|LIVE\s)"), "JBL"),
    (re.compile(r"(?i)^(Anker|Soundcore|eufy)"), "Anker / Soundcore"),
    (re.compile(r"(?i)^(MX\s|Logitech|Logi|G915|G502|MX Master|MX Keys)"), "Logitech"),
    (re.compile(r"(?i)^(DJI|Mavic|Spark|Phantom|Tello|Mini[\s_-]?(2|3|4|Pro))"), "DJI (drone)"),
    (re.compile(r"(?i)^Skydio"), "Skydio (drone)"),
    (re.compile(r"(?i)^Autel"), "Autel (drone)"),
    (re.compile(r"(?i)^(Parrot|Anafi|Bebop)"), "Parrot (drone)"),
    (re.compile(r"(?i)^(Boltt|Mi\sBand|Realme|Honor|Huawei|Watch)"), "Wearable (generic)"),
    (re.compile(r"(?i)^TIZEN|Watch[0-9]"), "Samsung Watch"),
    (re.compile(r"(?i)^(LE-|LE\s)"), "Audio LE (generic)"),
    (re.compile(r"(?i)^(Roomba|Shark|Roborock)"), "Robot vacuum"),
    (re.compile(r"(?i)^(Ble-|BLE_|ESP_|esp32)"), "ESP32 / DIY"),
]


def is_random_bt_mac(mac):
    """BT LE Privacy detection — locally-administered bit (bit 1 of byte 0) set."""
    if not mac or ":" not in mac:
        return False
    try:
        top = int(mac.split(":")[0], 16)
    except ValueError:
        return False
    # Locally-administered bit = 1 means MAC is not in IEEE OUI registry — random
    return (top & 0b10) != 0


def infer_bt_vendor(name):
    if not name:
        return None
    for pattern, vendor in BT_NAME_VENDOR_HINTS:
        if pattern.search(name):
            return vendor
    return None


# -----------------------------------------------------------------------------
# Operator ID parsing + UK known-operator matcher
# Remote ID broadcasts include an Operator ID (regulator-issued — e.g. UK CAA
# `GBR-OP-XXXXXXXXX`) and a Self-ID (operator-supplied free text). The OperatorID
# is anonymous to the public by design — only authorities can resolve to a person
# — but Self-ID is often something like "Hampshire Police" or "Network Rail".
# We pattern-match Self-ID against known UK orgs + persist all sightings to SQLite.

COUNTRY_FLAGS = {
    "GBR": "🇬🇧", "USA": "🇺🇸", "DEU": "🇩🇪", "FRA": "🇫🇷",
    "ESP": "🇪🇸", "ITA": "🇮🇹", "NLD": "🇳🇱", "BEL": "🇧🇪",
    "POL": "🇵🇱", "CZE": "🇨🇿", "AUT": "🇦🇹", "CHE": "🇨🇭",
    "IRL": "🇮🇪", "DNK": "🇩🇰", "SWE": "🇸🇪", "NOR": "🇳🇴",
    "FIN": "🇫🇮", "PRT": "🇵🇹", "GRC": "🇬🇷", "HUN": "🇭🇺",
    "ROU": "🇷🇴", "BGR": "🇧🇬", "HRV": "🇭🇷", "SVK": "🇸🇰",
    "SVN": "🇸🇮", "LUX": "🇱🇺", "LTU": "🇱🇹", "LVA": "🇱🇻",
    "EST": "🇪🇪", "MLT": "🇲🇹", "CYP": "🇨🇾", "ISL": "🇮🇸",
    "CAN": "🇨🇦", "AUS": "🇦🇺", "NZL": "🇳🇿", "JPN": "🇯🇵",
    "KOR": "🇰🇷", "CHN": "🇨🇳", "IND": "🇮🇳", "ZAF": "🇿🇦",
    "BRA": "🇧🇷", "MEX": "🇲🇽", "ARG": "🇦🇷",
}

# 43 UK territorial police forces (England, Wales, Scotland, NI) + specials
_UK_POLICE_FORCES = [
    "Avon and Somerset", "Bedfordshire", "Cambridgeshire", "Cheshire",
    "City of London", "Cleveland", "Cumbria", "Derbyshire",
    "Devon and Cornwall", "Dorset", "Durham", "Dyfed-Powys",
    "Essex", "Gloucestershire", "Greater Manchester", "Gwent",
    "Hampshire", "Hertfordshire", "Humberside", "Kent",
    "Lancashire", "Leicestershire", "Lincolnshire", "Merseyside",
    "Metropolitan", "Norfolk", "North Wales",
    "North Yorkshire", "Northamptonshire", "Northumbria",
    "Nottinghamshire", "South Wales", "South Yorkshire",
    "Staffordshire", "Suffolk", "Surrey", "Sussex",
    "Thames Valley", "Warwickshire", "West Mercia", "West Midlands",
    "West Yorkshire", "Wiltshire",
]

# Pattern → label, category
def _build_operator_patterns():
    pats = []
    for force in _UK_POLICE_FORCES:
        # "Hampshire Police", "Hampshire Constabulary", "Hampshire Police Drone Unit"
        rx = re.compile(rf"(?i)\b{re.escape(force)}\b.*?\b(Police|Constabulary)\b|\b(Police|Constabulary)\b.*?\b{re.escape(force)}\b")
        pats.append((rx, f"{force} Police", "police"))
    pats.extend([
        # Specials
        (re.compile(r"(?i)\b(Police\s+Scotland|Scottish\s+Police)\b"), "Police Scotland", "police"),
        (re.compile(r"(?i)\b(PSNI|Police\s+Service\s+of\s+Northern\s+Ireland)\b"), "PSNI", "police"),
        (re.compile(r"(?i)\b(British\s+Transport\s+Police|BTP)\b"), "British Transport Police", "police"),
        (re.compile(r"(?i)\b(Civil\s+Nuclear\s+Constabulary|CNC)\b"), "Civil Nuclear Constabulary", "police"),
        (re.compile(r"(?i)\b(Ministry\s+of\s+Defence\s+Police|MoD\s+Police|MDP)\b"), "MoD Police", "police"),
        (re.compile(r"(?i)\b(NCA|National\s+Crime\s+Agency)\b"), "National Crime Agency", "police"),
        (re.compile(r"(?i)\bpolice\b"), "Police (force unspecified)", "police"),
        # Emergency services
        (re.compile(r"(?i)\b(HMCG|HM\s*Coast\s*guard|Coastguard)\b"), "HM Coastguard", "rescue"),
        (re.compile(r"(?i)\bRNLI\b"), "RNLI", "rescue"),
        (re.compile(r"(?i)\b(Mountain\s+Rescue|MR\s+team)\b"), "Mountain Rescue", "rescue"),
        (re.compile(r"(?i)\b(Fire\s+(?:and|&)\s+Rescue|Fire\s+Brigade|Fire\s+Service|FRS)\b"), "Fire & Rescue", "fire"),
        (re.compile(r"(?i)\b(Ambulance|HEMS|Air\s+Ambulance)\b"), "Ambulance", "medical"),
        # Government / infrastructure
        (re.compile(r"(?i)\bNetwork\s+Rail\b"), "Network Rail", "infrastructure"),
        (re.compile(r"(?i)\b(NHS|National\s+Health\s+Service)\b"), "NHS", "health"),
        (re.compile(r"(?i)\b(Border\s+Force|HMRC|Home\s+Office)\b"), "Border Force / HMRC", "government"),
        (re.compile(r"(?i)\b(Highways\s+England|National\s+Highways)\b"), "National Highways", "infrastructure"),
        (re.compile(r"(?i)\b(Environment\s+Agency)\b"), "Environment Agency", "government"),
        (re.compile(r"(?i)\b(Natural\s+England|Natural\s+Resources\s+Wales|NatureScot)\b"), "Conservation body", "government"),
        (re.compile(r"(?i)\b(MoD|Ministry\s+of\s+Defence|RAF|Royal\s+Air\s+Force|British\s+Army|Royal\s+Navy)\b"), "Ministry of Defence", "military"),
        # Delivery / logistics trials
        (re.compile(r"(?i)\bWing\b"), "Wing (Alphabet)", "delivery"),
        (re.compile(r"(?i)\bManna\b"), "Manna Drone Delivery", "delivery"),
        (re.compile(r"(?i)\bSkyports\b"), "Skyports", "delivery"),
        (re.compile(r"(?i)\bApian\b"), "Apian", "delivery"),
        (re.compile(r"(?i)\bRoyal\s+Mail\b"), "Royal Mail", "delivery"),
        (re.compile(r"(?i)\bAmazon\s+(Prime\s+)?Air\b"), "Amazon Prime Air", "delivery"),
        (re.compile(r"(?i)\bZipline\b"), "Zipline", "delivery"),
        # Survey / engineering / utilities
        (re.compile(r"(?i)\b(survey(?:ing|or)?|topographic|aerial\s+photograph|inspection|mapping|cartograph|orthomosaic)\b"), "Survey / Inspection", "commercial"),
        (re.compile(r"(?i)\b(RSK|WSP|Atkins|Arup|Mott\s+Macdonald|Ramboll|Jacobs|Stantec|Aecom)\b"), "Engineering Consultancy", "commercial"),
        (re.compile(r"(?i)\b(BBC|ITV|Sky\s+News|broadcast(?:er|ing)?|news\s+team)\b"), "Broadcaster", "media"),
        (re.compile(r"(?i)\b(Vattenfall|BP\b|Shell|National\s+Grid|UK\s+Power\s+Networks|SSE\b|Octopus)\b"), "Energy infrastructure", "commercial"),
        (re.compile(r"(?i)\b(Thames\s+Water|Severn\s+Trent|Anglian\s+Water|Yorkshire\s+Water|Welsh\s+Water)\b"), "Water utility", "commercial"),
        # Manufacturer / test
        (re.compile(r"(?i)\b(DJI\s+(?:Test|Demo|Service|Care))\b"), "DJI Test/Demo", "test"),
        (re.compile(r"(?i)^(test|demo)[\s_-]*(account|user|operator|flight)?$"), "Test / Demo", "test"),
        # Cinematography / professional
        (re.compile(r"(?i)\b(film(?:\s*crew)?|cinema|movie|production)\b"), "Film / Production", "media"),
    ])
    return pats

UK_KNOWN_OPERATORS = _build_operator_patterns()


def parse_operator_id(opid):
    """Parse Operator ID string. Returns dict with country, flag, scheme, valid, formatted."""
    if not opid:
        return None
    raw = str(opid).strip()
    result = {"raw": raw, "country": None, "flag": "🏳️",
              "valid_format": False, "scheme": "Unknown", "formatted": raw}

    # UK CAA: GBR-OP-XXXXXXXXX
    m = re.match(r"^(GBR)[-_\s]?OP[-_\s]?([A-Z0-9]{6,15})$", raw, re.I)
    if m:
        result.update(country="GBR", scheme="UK CAA", valid_format=True,
                      flag=COUNTRY_FLAGS["GBR"],
                      formatted=f"GBR-OP-{m.group(2).upper()}")
        return result

    # EU EASA 16-char: 3-char country + 13-char id
    m = re.match(r"^([A-Z]{3})([A-Z0-9]{13})$", raw)
    if m:
        cc = m.group(1)
        result.update(country=cc, scheme="EU EASA", valid_format=True,
                      flag=COUNTRY_FLAGS.get(cc, "🏳️"))
        return result

    # FAA / US — Part 107 cert format or FA + serial
    if re.match(r"^(USA[-_]|FA\d|107|REG)", raw, re.I):
        result.update(country="USA", scheme="FAA", valid_format=True,
                      flag=COUNTRY_FLAGS["USA"])
        return result

    # Generic: 3-letter country code prefix
    if len(raw) >= 3 and raw[:3].isalpha():
        cc = raw[:3].upper()
        if cc in COUNTRY_FLAGS:
            result["country"] = cc
            result["flag"] = COUNTRY_FLAGS[cc]
    return result


def match_known_operator(self_id_text, operator_id_text=""):
    """Match Self-ID + Operator-ID against UK organisation patterns."""
    text = " ".join(filter(None, [self_id_text, operator_id_text]))
    if not text:
        return None, None
    for pattern, label, category in UK_KNOWN_OPERATORS:
        if pattern.search(text):
            return label, category
    return None, None


def extract_uav_metadata(d):
    """Best-effort extraction of operator_id, self_id, drone serial from a Kismet
    device record. Field paths vary across Kismet versions and RID parser
    implementations — this walks the dict for likely keys."""
    out = {"operator_id": None, "self_id": None, "drone_serial": None,
           "basic_id": None, "uas_id": None}

    def walk(obj, depth=0):
        if depth > 6 or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = str(k).lower()
                if isinstance(v, (str, int, float)):
                    val = str(v).strip()
                    if not val or val == "0":
                        continue
                    if "operator_id" in kl or "opid" in kl or kl.endswith(".op_id"):
                        out["operator_id"] = out["operator_id"] or val
                    elif "self_id" in kl or "self-id" in kl or kl.endswith(".self"):
                        out["self_id"] = out["self_id"] or val
                    elif "serial" in kl and "uav" in kl:
                        out["drone_serial"] = out["drone_serial"] or val
                    elif "basic_id" in kl or kl.endswith(".uas_id"):
                        out["basic_id"] = out["basic_id"] or val
                else:
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    walk(d)
    return out


# -----------------------------------------------------------------------------
# SQLite persistence

_db_lock = threading.Lock()

def init_db():
    Path(SQLITE_DB).parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS operators (
                operator_id TEXT PRIMARY KEY,
                country TEXT,
                scheme TEXT,
                last_self_id TEXT,
                known_label TEXT,
                known_category TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                sightings INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sightings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                operator_id TEXT,
                drone_mac TEXT,
                drone_serial TEXT,
                drone_lat REAL, drone_lon REAL, drone_alt REAL,
                op_lat REAL, op_lon REAL,
                rssi INTEGER,
                self_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_sightings_op ON sightings(operator_id);
            CREATE INDEX IF NOT EXISTS idx_sightings_ts ON sightings(ts DESC);
            CREATE TABLE IF NOT EXISTS drones (
                mac TEXT PRIMARY KEY,
                serial TEXT,
                manuf TEXT,
                model TEXT,
                first_seen INTEGER,
                last_seen INTEGER,
                sightings INTEGER DEFAULT 0,
                last_operator_id TEXT
            );
        """)


def upsert_operator(opid, country, scheme, self_id, label, category):
    if not opid:
        return False
    now = int(time.time())
    is_new = False
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        existing = conn.execute("SELECT operator_id FROM operators WHERE operator_id=?", (opid,)).fetchone()
        is_new = existing is None
        conn.execute("""
            INSERT INTO operators (operator_id, country, scheme, last_self_id,
                                   known_label, known_category, first_seen, last_seen, sightings)
            VALUES (?,?,?,?,?,?,?,?,1)
            ON CONFLICT(operator_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                sightings = sightings + 1,
                last_self_id = COALESCE(excluded.last_self_id, last_self_id),
                known_label = COALESCE(excluded.known_label, known_label),
                known_category = COALESCE(excluded.known_category, known_category)
        """, (opid, country, scheme, self_id, label, category, now, now))
    return is_new


def log_sighting(opid, mac, serial, drone_lat, drone_lon, drone_alt,
                 op_lat, op_lon, rssi, self_id):
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        conn.execute("""
            INSERT INTO sightings (ts, operator_id, drone_mac, drone_serial,
                                   drone_lat, drone_lon, drone_alt, op_lat, op_lon, rssi, self_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (int(time.time()), opid, mac, serial,
              drone_lat, drone_lon, drone_alt, op_lat, op_lon, rssi, self_id))


def upsert_drone(mac, serial, manuf, model, opid):
    if not mac:
        return
    now = int(time.time())
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        conn.execute("""
            INSERT INTO drones (mac, serial, manuf, model, first_seen, last_seen, sightings, last_operator_id)
            VALUES (?,?,?,?,?,?,1,?)
            ON CONFLICT(mac) DO UPDATE SET
                last_seen = excluded.last_seen,
                sightings = sightings + 1,
                serial = COALESCE(excluded.serial, serial),
                model = COALESCE(excluded.model, model),
                last_operator_id = COALESCE(excluded.last_operator_id, last_operator_id)
        """, (mac, serial, manuf, model, now, now, opid))


def list_operators():
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT o.*,
                   (SELECT COUNT(DISTINCT drone_mac) FROM sightings s WHERE s.operator_id = o.operator_id) AS drones_count
            FROM operators o
            ORDER BY last_seen DESC
            LIMIT 200
        """).fetchall()
        return [dict(r) for r in rows]


def operator_detail(opid):
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        conn.row_factory = sqlite3.Row
        op = conn.execute("SELECT * FROM operators WHERE operator_id=?", (opid,)).fetchone()
        if not op:
            return None
        sightings = conn.execute("""
            SELECT * FROM sightings WHERE operator_id=? ORDER BY ts DESC LIMIT 100
        """, (opid,)).fetchall()
        return {"operator": dict(op), "sightings": [dict(s) for s in sightings]}


def update_operator_notes(opid, notes):
    with _db_lock, sqlite3.connect(SQLITE_DB) as conn:
        conn.execute("UPDATE operators SET notes=? WHERE operator_id=?", (notes or "", opid))


# -----------------------------------------------------------------------------
# ntfy push notifications

_ntfy_seen_operators = set()  # operators we've already first-seen-notified
_ntfy_overhead_cooldown = {}  # operator -> ts of last overhead alert

def ntfy_notify(title, message, priority="default", tags=""):
    if not NTFY_URL:
        return
    try:
        requests.post(NTFY_URL, data=message.encode("utf-8"), headers={
            "Title": title.encode("utf-8").decode("latin-1", errors="replace"),
            "Priority": priority,
            "Tags": tags,
        }, timeout=5)
    except Exception as e:
        log.warning("ntfy POST failed: %s", e)


def maybe_notify(operator_id, label, drone_label, rssi, is_new_operator):
    if not NTFY_URL:
        return
    label = label or operator_id or "Unknown operator"
    if is_new_operator:
        ntfy_notify(
            f"🚁 New drone operator: {label}",
            f"Drone: {drone_label}\nOperator ID: {operator_id or 'not broadcast'}\nRSSI: {rssi or '?'} dBm",
            priority="default", tags="airplane,new",
        )
    if rssi is not None and rssi > NTFY_RSSI_THRESHOLD:
        # Cooldown — don't spam more than 1 overhead alert per operator per 10 min
        now = time.time()
        last = _ntfy_overhead_cooldown.get(operator_id or drone_label, 0)
        if now - last >= 600:
            _ntfy_overhead_cooldown[operator_id or drone_label] = now
            ntfy_notify(
                f"⚠️ Drone overhead — {label}",
                f"{drone_label}\nRSSI: {rssi} dBm (very strong, likely overhead)",
                priority="high", tags="warning,rotating_light",
            )


# -----------------------------------------------------------------------------

def enrich_bt(d):
    """Annotate a BT device record with display-friendly fields:
    _bt_name (resolved name), _bt_manuf (vendor), _bt_type (Classic/LE/Dual),
    _bt_manuf_source (how we resolved it: OUI / name-hint / company-id / random_mac).
    """
    mac = d.get("kismet.device.base.macaddr", "")
    name_raw = (d.get("kismet.device.base.commonname") or "").strip()
    real_name = bool(name_raw and name_raw != mac)
    oui_manuf = d.get("kismet.device.base.manuf") or ""
    randomized = is_random_bt_mac(mac)

    if real_name:
        d["_bt_name"] = name_raw
    elif randomized:
        d["_bt_name"] = "(anonymous LE)"
    else:
        d["_bt_name"] = "(no name)"

    # Vendor resolution chain: registered OUI > BLE name hint > LE Privacy > Unknown
    if oui_manuf and oui_manuf.lower() != "unknown":
        d["_bt_manuf"] = oui_manuf
        d["_bt_manuf_source"] = "OUI"
    elif real_name and (vendor := infer_bt_vendor(name_raw)):
        d["_bt_manuf"] = vendor
        d["_bt_manuf_source"] = "name"
    elif randomized:
        d["_bt_manuf"] = "LE Privacy"
        d["_bt_manuf_source"] = "random_mac"
    else:
        d["_bt_manuf"] = "Unknown"
        d["_bt_manuf_source"] = "?"

    # BT type code: 0 Classic, 1 LE, 2 Dual
    bt_sub = d.get("bluetooth.device") or {}
    if isinstance(bt_sub, dict):
        type_code = bt_sub.get("bluetooth.device.type", 0)
        d["_bt_type"] = {0: "Classic", 1: "LE", 2: "Dual"}.get(type_code, "?")
        # Surface service UUIDs if any are recognizable
        uuids = bt_sub.get("bluetooth.device.service_uuid_vec") or []
        labels = []
        for u in uuids[:5]:  # only first few
            short = u.lower().replace("-", "")[:8] if isinstance(u, str) else ""
            if short and short in BT_SERVICE_UUIDS:
                labels.append(BT_SERVICE_UUIDS[short])
        if labels:
            d["_bt_services"] = ", ".join(labels)
    else:
        d["_bt_type"] = "?"

    return d

# Fields requested from Kismet — keeps payload small
DEVICE_FIELDS = [
    "kismet.device.base.macaddr",
    "kismet.device.base.commonname",
    "kismet.device.base.phyname",
    "kismet.device.base.type",
    "kismet.device.base.manuf",
    "kismet.device.base.first_time",
    "kismet.device.base.last_time",
    "kismet.device.base.channel",
    "kismet.device.base.frequency",
    "kismet.device.base.packets.total",
    ["kismet.device.base.signal/kismet.common.signal.last_signal", "last_signal"],
    ["kismet.device.base.signal/kismet.common.signal.max_signal",  "max_signal"],
    # WiFi
    ["dot11.device/dot11.device.last_beaconed_ssid_record/dot11.advertisedssid.ssid", "last_ssid"],
    ["dot11.device/dot11.device.uav_match", "uav_match"],
    # Kismet's own GPS-tagged location (only populated if Kismet has a gps source)
    ["kismet.device.base.location/kismet.common.location.last/kismet.common.location.geopoint", "kismet_geopoint"],
    # UAV phy / RID-parsed location (varies by Kismet version — fetch defensively)
    "dot11.device.uav",
    "bluetooth.device.uav",
    "kismet.device.base.uav",
    # Full bluetooth subtree for BT identification (type, service UUIDs, scan data)
    "bluetooth.device",
]

# -----------------------------------------------------------------------------
# state

app = Flask(__name__)
log = logging.getLogger("drone-dashboard")

_state_lock = threading.Lock()
_state = {
    "kismet_ok": False,
    "kismet_error": None,
    "last_poll": None,
    "drones": [],
    "wifi_clients": [],
    "bt_devices": [],
    "map_targets": [],
    "totals": {"drones": 0, "wifi_clients": 0, "bt_devices": 0, "aps_skipped": 0, "all": 0},
    "stats": {"polls": 0, "errors": 0, "started_at": time.time()},
}
_drone_history = deque(maxlen=HISTORY_LEN)
_drone_ever_seen = {}


def load_kismet_creds():
    p = Path(KISMET_CONF)
    if not p.exists():
        return None, None
    user = pw = None
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("httpd_username="):
            user = line.split("=", 1)[1].strip()
        elif line.startswith("httpd_password="):
            pw = line.split("=", 1)[1].strip()
    return user, pw


KISMET_USER, KISMET_PASS = load_kismet_creds()
if not KISMET_USER:
    log.warning("Could not load kismet creds from %s — API calls will likely 401", KISMET_CONF)
AUTH = (KISMET_USER, KISMET_PASS) if KISMET_USER else None


# -----------------------------------------------------------------------------
# helpers

def _walk(obj, predicate, depth=0, max_depth=6):
    """Recursively walk a nested dict/list, yield (path, value) where predicate(key) matches."""
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and predicate(k):
                yield k, v
            yield from _walk(v, predicate, depth + 1, max_depth)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk(item, predicate, depth + 1, max_depth)


def extract_locations(d):
    """Pull every plausible GPS coordinate out of a device record.
    Returns list of dicts: [{kind, lat, lon, alt?, label?}].

    Kinds: 'drone' (drone's own GPS), 'operator' (drone pilot's GPS),
    'station' (Kismet's GPS view of the device — rare without Pi GPS),
    'unknown' (anything else with lat/lon).
    """
    results = []
    seen_pairs = set()  # avoid emitting the same (lat,lon) twice

    def add(kind, lat, lon, alt=None, label=None):
        try:
            lat_f = float(lat); lon_f = float(lon)
        except (TypeError, ValueError):
            return
        if lat_f == 0.0 and lon_f == 0.0:  # Kismet null fill
            return
        if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            return
        key = (round(lat_f, 5), round(lon_f, 5), kind)
        if key in seen_pairs:
            return
        seen_pairs.add(key)
        item = {"kind": kind, "lat": lat_f, "lon": lon_f}
        if alt is not None:
            try: item["alt"] = float(alt)
            except (TypeError, ValueError): pass
        if label:
            item["label"] = label
        results.append(item)

    # 1. Kismet's tagged geopoint (only if Kismet has GPS hardware — we don't,
    #    but defensive). Format is [lon, lat] array.
    geo = d.get("kismet_geopoint")
    if isinstance(geo, list) and len(geo) >= 2:
        add("station", geo[1], geo[0])

    # 2. Walk the entire record for any *_lat / *_lon / *_latitude / *_longitude pairs
    lats = {}
    lons = {}
    alts = {}
    lat_re = re.compile(r"(?i)(.*)(?:\.|_)(?:lat|latitude)$")
    lon_re = re.compile(r"(?i)(.*)(?:\.|_)(?:lon|long|longitude)$")
    alt_re = re.compile(r"(?i)(.*)(?:\.|_)(?:alt|altitude|height)$")

    def flatten(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{prefix}.{k}" if prefix else k
                if isinstance(v, (dict, list)):
                    flatten(v, p)
                else:
                    yield p, v
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                yield from flatten(item, f"{prefix}[{i}]")

    for path, val in flatten(d):
        m = lat_re.match(path)
        if m: lats[m.group(1)] = val; continue
        m = lon_re.match(path)
        if m: lons[m.group(1)] = val; continue
        m = alt_re.match(path)
        if m: alts[m.group(1)] = val

    for stem, lat in lats.items():
        if stem in lons:
            lon = lons[stem]
            alt = alts.get(stem)
            kind = "unknown"
            stem_lc = stem.lower()
            if "operator" in stem_lc or "pilot" in stem_lc:
                kind = "operator"
            elif "uav" in stem_lc or "drone" in stem_lc or stem_lc.endswith("location"):
                kind = "drone"
            add(kind, lat, lon, alt, stem)

    return results


# -----------------------------------------------------------------------------
# classification

def is_drone(d):
    """Multi-signal drone classifier. Returns (is_drone, reason, details)."""
    # 1. Kismet's own UAV match (best signal)
    uav = d.get("uav_match") or d.get("dot11.device.uav") or d.get("bluetooth.device.uav") or d.get("kismet.device.base.uav")
    if uav and isinstance(uav, dict) and uav:
        name = uav.get("dot11.device.uav.name") or uav.get("name") or "Unknown"
        model = uav.get("dot11.device.uav.model") or uav.get("model") or ""
        match = uav.get("dot11.device.uav.match_name") or uav.get("match_name") or ""
        return True, "uav_match", f"{name} {model}".strip() or match or "UAV"

    ssid = (d.get("last_ssid") or "").strip()
    manuf = (d.get("kismet.device.base.manuf") or "").strip()

    if ssid and DRONE_SSID_RE.search(ssid):
        return True, "ssid_pattern", f"SSID: {ssid}"

    if manuf and DRONE_MANUF_RE.search(manuf):
        return True, "manuf_pattern", f"Manuf: {manuf}"

    return False, None, None


def classify(devices):
    drones = []
    wifi_clients = []
    bt_devices = []
    aps_skipped = 0
    map_targets = []
    seen_macs = set()
    now = time.time()

    for d in devices:
        mac = d.get("kismet.device.base.macaddr")
        if not mac or mac in seen_macs:
            continue
        seen_macs.add(mac)
        dtype = d.get("kismet.device.base.type", "")
        phy = d.get("kismet.device.base.phyname", "")

        # Pull GPS for ANY device — even non-drones
        locations = extract_locations(d)

        drone_flag, reason, details = is_drone(d)
        if drone_flag:
            d["_drone_reason"] = reason
            d["_drone_details"] = details
            d["_locations"] = locations

            # Extract Remote ID metadata: operator_id, self_id, drone serial
            uav_meta = extract_uav_metadata(d)
            opid_raw = uav_meta.get("operator_id")
            self_id_text = uav_meta.get("self_id")
            drone_serial = uav_meta.get("drone_serial") or uav_meta.get("basic_id") or uav_meta.get("uas_id")

            opid_parsed = parse_operator_id(opid_raw) if opid_raw else None
            label, category = match_known_operator(self_id_text or "", opid_raw or "")

            d["_operator_id"] = opid_raw
            d["_operator_parsed"] = opid_parsed
            d["_self_id"] = self_id_text
            d["_known_label"] = label
            d["_known_category"] = category
            d["_drone_serial"] = drone_serial

            # Persist + maybe notify (only when we have a real Operator ID)
            if opid_raw:
                try:
                    drone_lat = next((l["lat"] for l in locations if l["kind"] == "drone"), None)
                    drone_lon = next((l["lon"] for l in locations if l["kind"] == "drone"), None)
                    drone_alt = next((l.get("alt") for l in locations if l["kind"] == "drone"), None)
                    op_lat = next((l["lat"] for l in locations if l["kind"] == "operator"), None)
                    op_lon = next((l["lon"] for l in locations if l["kind"] == "operator"), None)
                    is_new = upsert_operator(
                        opid_raw,
                        opid_parsed.get("country") if opid_parsed else None,
                        opid_parsed.get("scheme") if opid_parsed else None,
                        self_id_text, label, category,
                    )
                    log_sighting(opid_raw, mac, drone_serial, drone_lat, drone_lon,
                                 drone_alt, op_lat, op_lon, d.get("last_signal"), self_id_text)
                    upsert_drone(mac, drone_serial,
                                 d.get("kismet.device.base.manuf", ""),
                                 details, opid_raw)
                    maybe_notify(opid_raw, label, details, d.get("last_signal"), is_new)
                except Exception as e:
                    log.warning("operator persist failed for %s: %s", opid_raw, e)

            drones.append(d)
            for loc in locations:
                kind = loc["kind"] if loc["kind"] in ("drone", "operator") else "drone"
                map_targets.append({
                    "kind": kind, "lat": loc["lat"], "lon": loc["lon"],
                    "alt": loc.get("alt"),
                    "label": details, "mac": mac,
                    "manuf": d.get("kismet.device.base.manuf", ""),
                    "ssid": d.get("last_ssid", ""),
                    "rssi": d.get("last_signal"),
                    "last_time": d.get("kismet.device.base.last_time"),
                    "operator_id": opid_raw,
                    "known_label": label,
                })
            if mac not in _drone_ever_seen:
                _drone_ever_seen[mac] = d.get("kismet.device.base.first_time") or int(now)
                _drone_history.append((int(now), mac, details or "drone"))
            continue

        if dtype == "Wi-Fi AP":
            aps_skipped += 1
            # APs can still have GPS in some cases (not common, but defensive)
            for loc in locations:
                map_targets.append({
                    "kind": loc["kind"] if loc["kind"] != "unknown" else "device",
                    "lat": loc["lat"], "lon": loc["lon"],
                    "label": d.get("last_ssid", "") or mac,
                    "mac": mac, "manuf": d.get("kismet.device.base.manuf", ""),
                    "rssi": d.get("last_signal"),
                    "last_time": d.get("kismet.device.base.last_time"),
                })
            continue

        # Non-AP device — classify by phy and add GPS if present
        if phy == "IEEE802.11":
            wifi_clients.append(d)
        elif phy in ("BTLE", "Bluetooth"):
            enrich_bt(d)
            bt_devices.append(d)
        else:
            wifi_clients.append(d)  # other phys → bucket with WiFi for now

        for loc in locations:
            map_targets.append({
                "kind": loc["kind"] if loc["kind"] != "unknown" else "device",
                "lat": loc["lat"], "lon": loc["lon"],
                "label": d.get("last_ssid", "") or mac,
                "mac": mac, "manuf": d.get("kismet.device.base.manuf", ""),
                "rssi": d.get("last_signal"),
                "last_time": d.get("kismet.device.base.last_time"),
            })

    drones.sort(key=lambda x: x.get("kismet.device.base.last_time", 0), reverse=True)
    wifi_clients.sort(key=lambda x: x.get("kismet.device.base.last_time", 0), reverse=True)
    bt_devices.sort(key=lambda x: x.get("kismet.device.base.last_time", 0), reverse=True)
    return drones, wifi_clients[:80], bt_devices[:80], aps_skipped, map_targets


# -----------------------------------------------------------------------------
# poller (background thread)

def poller_loop():
    while True:
        try:
            payload = {"fields": DEVICE_FIELDS}
            r = requests.post(
                f"{KISMET_URL}/devices/views/all/devices.json",
                json=payload, auth=AUTH, timeout=10,
            )
            r.raise_for_status()
            devices = r.json()
            drones, wifi_clients, bt_devices, aps_skipped, map_targets = classify(devices)
            with _state_lock:
                _state["kismet_ok"] = True
                _state["kismet_error"] = None
                _state["last_poll"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _state["drones"] = drones
                _state["wifi_clients"] = wifi_clients
                _state["bt_devices"] = bt_devices
                _state["map_targets"] = map_targets
                _state["totals"] = {
                    "drones": len(drones),
                    "wifi_clients": len(wifi_clients),
                    "bt_devices": len(bt_devices),
                    "aps_skipped": aps_skipped,
                    "all": len(devices),
                }
                _state["stats"]["polls"] += 1
        except Exception as e:
            with _state_lock:
                _state["kismet_ok"] = False
                _state["kismet_error"] = str(e)
                _state["stats"]["errors"] += 1
            log.warning("Kismet poll failed: %s", e)
        time.sleep(POLL_SECS)


# -----------------------------------------------------------------------------
# routes

@app.route("/")
def index():
    return render_template_string(
        TEMPLATE,
        station_lat=STATION_LAT, station_lon=STATION_LON, station_name=STATION_NAME,
    )


@app.route("/api/devices")
def api_devices():
    with _state_lock:
        return jsonify({
            "kismet_ok": _state["kismet_ok"],
            "kismet_error": _state["kismet_error"],
            "last_poll": _state["last_poll"],
            "drones": _state["drones"],
            "wifi_clients": _state["wifi_clients"],
            "bt_devices": _state["bt_devices"],
            "map_targets": _state["map_targets"],
            "totals": _state["totals"],
            "stats": _state["stats"],
            "history": list(_drone_history)[-50:],
            "uptime_secs": int(time.time() - _state["stats"]["started_at"]),
            "station": {"lat": STATION_LAT, "lon": STATION_LON, "name": STATION_NAME},
        })


@app.route("/api/health")
def api_health():
    with _state_lock:
        return jsonify({"ok": _state["kismet_ok"], "last_poll": _state["last_poll"]})


# -----------------------------------------------------------------------------
# Adapter discovery & live source management
# Reads system state via iw/bluetoothctl/nmcli/ip; mutates Kismet via its REST API.

def _which(*candidates):
    for c in candidates:
        p = shutil.which(c)
        if p:
            return p
    return None


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:
        log.debug("cmd failed %s: %s", cmd, e)
        return ""


def _wifi_phys():
    """Parse `iw dev` → list of {phy, iface, type, addr, ssid?, channel?}."""
    iw = _which("iw", "/usr/sbin/iw")
    if not iw:
        return []
    out = _run([iw, "dev"])
    devices, current = [], {}
    cur_phy = None
    for line in out.splitlines():
        s = line.strip()
        if line.startswith("phy#"):
            cur_phy = int(line[4:].strip())
        elif s.startswith("Interface "):
            if current:
                devices.append(current)
            current = {"phy": cur_phy, "iface": s.split(None, 1)[1], "type": "?", "addr": ""}
        elif s.startswith("addr "):
            current["addr"] = s.split(None, 1)[1]
        elif s.startswith("type "):
            current["type"] = s.split(None, 1)[1]
        elif s.startswith("ssid "):
            current["ssid"] = s.split(None, 1)[1]
        elif s.startswith("channel "):
            current["channel"] = s.split("channel", 1)[1].split("(", 1)[0].strip()
    if current:
        devices.append(current)
    # Annotate each with monitor-mode capability + bands
    for d in devices:
        info = _run([iw, "phy", f"phy{d['phy']}", "info"])
        d["monitor_capable"] = " * monitor" in info or "* monitor\n" in info
        d["bands"] = []
        for line in info.splitlines():
            if line.strip().startswith("Band "):
                d["bands"].append(line.strip().rstrip(":"))
    return devices


def _hci_to_mac():
    """Map hciN → BD Address via hciconfig (sysfs `/address` doesn't exist on
    every kernel — hciconfig is the reliable path)."""
    hciconfig = _which("hciconfig", "/usr/bin/hciconfig")
    if not hciconfig:
        return {}
    out = _run([hciconfig])
    result = {}
    cur = None
    for line in out.splitlines():
        # "hci1:\tType: Primary  Bus: USB"
        if line and not line[0].isspace() and ":" in line:
            cur = line.split(":", 1)[0].strip()
        elif cur and "BD Address:" in line:
            mac = line.split("BD Address:", 1)[1].split()[0].strip().upper()
            result[cur] = mac
    return result


def _bt_controllers():
    """Enumerate BT controllers from hciconfig + cross-reference bluetoothctl
    for default/name/powered status. Returns [{hci, mac, name, default, powered}]."""
    hci_macs = _hci_to_mac()  # {'hci0': 'B8:27:...', 'hci1': '00:E0:...'}
    if not hci_macs:
        return []

    # Pull bluetoothctl info for friendly name + default flag (best-effort)
    btctl_info = {}  # mac → {name, default}
    out = _run(["bluetoothctl", "list"])
    for line in out.splitlines():
        if line.startswith("Controller "):
            parts = line.split()
            if len(parts) >= 2:
                mac = parts[1].upper()
                rest = line.split(None, 2)[2] if len(parts) >= 3 else ""
                btctl_info[mac] = {
                    "name": rest.replace("[default]", "").strip(),
                    "default": "[default]" in rest,
                }

    controllers = []
    for hci, mac in sorted(hci_macs.items()):
        info = btctl_info.get(mac, {})
        controllers.append({
            "hci": hci,
            "mac": mac,
            "name": info.get("name", ""),
            "default": info.get("default", False),
            "powered": _bt_powered(mac),
        })
    return controllers


def _bt_powered(mac):
    """Check if a controller is powered on via bluetoothctl."""
    if not mac:
        return False
    out = _run(["bluetoothctl", "show", mac], timeout=3)
    for line in out.splitlines():
        if line.strip().startswith("Powered:"):
            return line.strip().endswith("yes")
    return False


def _default_route_iface():
    """Identifies the management interface (default gateway) — DON'T touch this."""
    out = _run(["ip", "-o", "route"])
    for line in out.splitlines():
        if line.startswith("default "):
            parts = line.split()
            if "dev" in parts:
                return parts[parts.index("dev") + 1]
    return ""


def _nm_status():
    """Parse `nmcli -t -f DEVICE,TYPE,STATE device` → {iface: state}."""
    out = _run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])
    result = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) >= 3:
            result[parts[0]] = {"type": parts[1], "state": parts[2]}
    return result


def _kismet_active_sources():
    """Pull live Kismet datasource list."""
    try:
        r = requests.get(f"{KISMET_URL}/datasource/all_sources.json", auth=AUTH, timeout=5)
        r.raise_for_status()
        srcs = r.json()
        out = []
        for s in srcs:
            out.append({
                "name": s.get("kismet.datasource.name"),
                "iface": s.get("kismet.datasource.interface"),
                "uuid": s.get("kismet.datasource.uuid"),
                "running": bool(s.get("kismet.datasource.running")),
                "type": (s.get("kismet.datasource.type_driver") or {}).get("kismet.datasource.type_driver.type"),
                "hop": bool(s.get("kismet.datasource.hopping")),
                "channel": s.get("kismet.datasource.channel"),
            })
        return out
    except Exception as e:
        log.warning("Kismet sources fetch failed: %s", e)
        return []


@app.route("/api/sources")
def api_sources():
    """Discover available adapters and current Kismet sources."""
    wifi_phys = _wifi_phys()
    bt = _bt_controllers()
    mgmt = _default_route_iface()
    nm = _nm_status()
    kismet_sources = _kismet_active_sources()

    # Mark each adapter with current Kismet status + safety flags
    in_use_ifaces = {s["iface"]: s for s in kismet_sources if s.get("iface")}

    for w in wifi_phys:
        iface = w["iface"]
        w["is_mgmt"] = (iface == mgmt)
        w["nm_state"] = (nm.get(iface) or {}).get("state", "unknown")
        w["in_kismet"] = iface in in_use_ifaces
        w["kismet_source"] = in_use_ifaces.get(iface) if w["in_kismet"] else None
        # Suggested kismet source line for kismet_site.conf
        w["suggested_source"] = f"source={iface}:type=linuxwifi,name={iface}-monitor"

    for b in bt:
        hci = b.get("hci") or ""
        b["in_kismet"] = hci in in_use_ifaces
        b["kismet_source"] = in_use_ifaces.get(hci) if b["in_kismet"] else None
        b["suggested_source"] = f"source={hci}:type=linuxbluetooth,name={hci}-monitor,active=true" if hci else ""

    return jsonify({
        "kismet_sources": kismet_sources,
        "wifi": wifi_phys,
        "bluetooth": bt,
        "mgmt_iface": mgmt,
        "nm_managed": nm,
    })


@app.route("/api/sources/add", methods=["POST"])
def api_source_add():
    """Add a source live to Kismet (NOT persistent across kismet restarts).
    Refuses to use the management interface to avoid breaking SSH.
    """
    data = request.get_json(silent=True) or {}
    src = data.get("definition", "").strip()
    if not src or "type=" not in src:
        return jsonify({"error": "missing or malformed source definition"}), 400
    mgmt = _default_route_iface()
    if mgmt and src.split(":", 1)[0] == mgmt:
        return jsonify({"error": f"refusing to add management interface '{mgmt}' — this would kill SSH"}), 400
    try:
        r = requests.post(
            f"{KISMET_URL}/datasource/add_source.cmd",
            data={"json": json.dumps({"definition": src})},
            auth=AUTH, timeout=15,
        )
        return jsonify({"ok": r.ok, "status": r.status_code,
                        "kismet_response": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/<src_uuid>/close", methods=["POST"])
def api_source_close(src_uuid):
    """Close (stop) a Kismet source by UUID. Live, non-persistent."""
    try:
        r = requests.post(
            f"{KISMET_URL}/datasource/by-uuid/{src_uuid}/close_source.cmd",
            auth=AUTH, timeout=10,
        )
        return jsonify({"ok": r.ok, "status": r.status_code,
                        "kismet_response": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sources/<src_uuid>/open", methods=["POST"])
def api_source_open(src_uuid):
    """Re-open a previously closed Kismet source by UUID."""
    try:
        r = requests.post(
            f"{KISMET_URL}/datasource/by-uuid/{src_uuid}/open_source.cmd",
            auth=AUTH, timeout=10,
        )
        return jsonify({"ok": r.ok, "status": r.status_code,
                        "kismet_response": r.text[:500]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/operators")
def api_operators():
    rows = list_operators()
    # Enrich each with parsed format / flag (re-parse so format flags survive DB roundtrip)
    for r in rows:
        parsed = parse_operator_id(r["operator_id"])
        r["flag"] = parsed["flag"] if parsed else "🏳️"
        r["scheme"] = r.get("scheme") or (parsed["scheme"] if parsed else "Unknown")
    return jsonify({"operators": rows, "ntfy_enabled": bool(NTFY_URL)})


@app.route("/api/operator/<path:opid>")
def api_operator_detail(opid):
    detail = operator_detail(opid)
    if not detail:
        return jsonify({"error": "not found"}), 404
    return jsonify(detail)


@app.route("/api/operator/<path:opid>/notes", methods=["POST"])
def api_update_notes(opid):
    notes = request.get_json(silent=True) or {}
    text = notes.get("notes", "")
    update_operator_notes(opid, text)
    return jsonify({"ok": True})


# -----------------------------------------------------------------------------
# template

TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Drone detection — sdr-pi</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="" />
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --border: #30363d;
    --text: #c9d1d9;
    --muted: #8b949e;
    --accent: #58a6ff;
    --drone: #f78166;
    --operator: #d29922;
    --bt: #a371f7;
    --good: #3fb950;
    --warn: #d29922;
    --bad: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    font: 14px/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    background: var(--bg); color: var(--text); margin: 0; padding: 16px;
  }
  header {
    display: flex; align-items: baseline; gap: 16px; margin-bottom: 16px;
    border-bottom: 1px solid var(--border); padding-bottom: 12px;
  }
  h1 { margin: 0; font-size: 20px; font-weight: 600; }
  h1 .pulse { display:inline-block; width:8px; height:8px; border-radius:50%;
    background: var(--good); margin-right: 8px; vertical-align: middle;
    animation: pulse 1.5s ease-in-out infinite; }
  h1 .pulse.bad { background: var(--bad); animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .status { color: var(--muted); font-size: 12px; }
  .status .err { color: var(--bad); }
  main { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 1100px) {
    main { grid-template-columns: 1fr 1fr; }
    .full { grid-column: 1 / -1; }
  }
  section {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 12px 16px;
  }
  section h2 {
    margin: 0 0 12px; font-size: 14px; text-transform: uppercase;
    letter-spacing: 0.05em; color: var(--muted); display: flex;
    align-items: center; gap: 10px;
  }
  .count {
    background: var(--accent); color: #fff; padding: 1px 8px;
    border-radius: 10px; font-size: 11px; font-weight: 600;
  }
  .count.drone { background: var(--drone); }
  .count.bt { background: var(--bt); }
  .count.zero { background: var(--border); color: var(--muted); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td {
    text-align: left; padding: 5px 8px; border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  th {
    color: var(--muted); font-weight: 500; text-transform: uppercase;
    letter-spacing: 0.05em; font-size: 10px;
  }
  td.mac, td.ssid { font-family: ui-monospace, monospace; }
  td.signal { text-align: right; }
  td.signal.strong { color: var(--good); }
  td.signal.med { color: var(--warn); }
  td.signal.weak { color: var(--muted); }
  tr.drone-row td:first-child { border-left: 3px solid var(--drone); }
  tr:hover { background: rgba(255,255,255,0.02); }
  .empty { color: var(--muted); padding: 20px; text-align: center; font-style: italic; font-size: 12px; }
  .badge {
    display: inline-block; padding: 1px 6px; border-radius: 3px;
    font-size: 11px; background: var(--border);
  }
  .badge.uav { background: var(--drone); color: #fff; }
  .badge.ssid { background: var(--accent); color: #fff; }
  .badge.manuf { background: var(--warn); color: #000; }
  .stats { display: flex; gap: 20px; flex-wrap: wrap; font-size: 12px; color: var(--muted); }
  .stats span b { color: var(--text); font-variant-numeric: tabular-nums; }
  footer { margin-top: 16px; color: var(--muted); font-size: 11px; text-align: center; }
  footer code { background: var(--panel); padding: 1px 5px; border-radius: 3px; }
  #map {
    height: 480px; border-radius: 4px; border: 1px solid var(--border);
    background: #1a1f27;
  }
  .leaflet-container { background: #1a1f27 !important; font-family: inherit; }
  .leaflet-popup-content-wrapper, .leaflet-popup-tip {
    background: var(--panel); color: var(--text); border: 1px solid var(--border);
  }
  .leaflet-popup-content { font-size: 12px; }
  .leaflet-popup-content b { color: var(--text); }
  .legend {
    display: flex; gap: 16px; font-size: 11px; color: var(--muted); margin-top: 8px;
  }
  .legend-dot {
    display: inline-block; width: 10px; height: 10px; border-radius: 50%;
    margin-right: 5px; vertical-align: middle; border: 2px solid #fff;
  }
  table.compact th, table.compact td { padding: 4px 6px; font-size: 11px; }
</style>
</head>
<body>

<header>
  <h1><span class="pulse" id="pulse"></span>Drone Detection</h1>
  <div class="status" id="status">connecting…</div>
</header>

<main>

  <section class="full">
    <h2>🚁 Drones <span class="count drone zero" id="drone-count">0</span></h2>
    <div id="drone-list"><div class="empty">No drones detected yet — Kismet is sweeping (WiFi + BT5).</div></div>
  </section>

  <section class="full">
    <h2>👤 Drone Operators <span class="count zero" id="op-count">0</span> <span style="font-weight:normal; text-transform:none; color:var(--muted); font-size:11px;">(persistent log — survives reboots)</span></h2>
    <div id="operator-list"><div class="empty">No Operator IDs logged yet. Persistent SQLite log builds up as drones broadcast Remote ID over time.</div></div>
  </section>

  <section class="full">
    <h2>📶 Capture Sources <span class="count zero" id="src-count">0</span> <span style="font-weight:normal; text-transform:none; color:var(--muted); font-size:11px;">(live state — changes here are not saved across Kismet restarts)</span></h2>
    <div id="sources-panel"><div class="empty">Loading adapters…</div></div>
  </section>

  <section class="full">
    <h2>🗺️ Live Map <span style="font-weight:normal; text-transform:none; color:var(--muted); font-size:11px;">(GPS data from Remote ID + any other broadcasting device)</span></h2>
    <div id="map"></div>
    <div class="legend">
      <span><span class="legend-dot" style="background:var(--accent)"></span>Pi station</span>
      <span><span class="legend-dot" style="background:var(--drone)"></span>Drone (RID GPS)</span>
      <span><span class="legend-dot" style="background:var(--operator)"></span>Operator</span>
      <span><span class="legend-dot" style="background:var(--good)"></span>Other GPS-broadcasting device</span>
    </div>
  </section>

  <section>
    <h2>📡 WiFi Clients <span class="count zero" id="wifi-count">0</span></h2>
    <div id="wifi-list"><div class="empty">No WiFi clients yet.</div></div>
  </section>

  <section>
    <h2>🔵 Bluetooth Devices <span class="count bt zero" id="bt-count">0</span></h2>
    <div id="bt-list"><div class="empty">No BT devices yet.</div></div>
  </section>

  <section>
    <h2>⏱ Recent Drone Sightings</h2>
    <div id="history-list"><div class="empty">No history yet.</div></div>
  </section>

  <section>
    <h2>📊 Stats</h2>
    <div class="stats" id="stats">—</div>
  </section>

</main>

<footer>
  Polls Kismet at <code>localhost:2501</code> every <span id="poll-secs">?</span>s ·
  <a href="http://192.168.3.73:2501/" style="color:var(--accent)">Kismet UI</a>
</footer>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin=""></script>
<script>
const POLL_MS = 3000;
const STATION = { lat: {{ station_lat }}, lon: {{ station_lon }}, name: "{{ station_name }}" };

// ----- map -----
const map = L.map('map', { preferCanvas: true }).setView([STATION.lat, STATION.lon], 13);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap', maxZoom: 19,
}).addTo(map);

const stationIcon = L.divIcon({
  className: '', html: '<div style="width:14px;height:14px;border-radius:50%;background:#58a6ff;border:2px solid #fff;box-shadow:0 0 8px #58a6ff;"></div>',
  iconSize: [18,18], iconAnchor: [9,9],
});
L.marker([STATION.lat, STATION.lon], { icon: stationIcon, title: STATION.name })
  .bindPopup('<b>' + STATION.name + '</b><br>Pi station (Kismet)')
  .addTo(map);

const dynLayer = L.layerGroup().addTo(map);

function markerFor(t) {
  const colour = t.kind === 'drone' ? '#f78166'
              : t.kind === 'operator' ? '#d29922'
              : '#3fb950';
  const size = t.kind === 'drone' ? 14 : 10;
  return L.divIcon({
    className: '',
    html: `<div style="width:${size}px;height:${size}px;border-radius:50%;background:${colour};border:2px solid #fff;box-shadow:0 0 6px ${colour};"></div>`,
    iconSize: [size+4, size+4], iconAnchor: [(size+4)/2, (size+4)/2],
  });
}

function popupFor(t) {
  const lines = [];
  lines.push(`<b>${escapeHtml(t.label || t.mac || '?')}</b>`);
  lines.push(`Kind: <b style="color:${t.kind==='drone'?'#f78166':t.kind==='operator'?'#d29922':'#3fb950'}">${t.kind}</b>`);
  if (t.mac) lines.push(`MAC: <code>${escapeHtml(t.mac)}</code>`);
  if (t.manuf) lines.push(`Manuf: ${escapeHtml(t.manuf)}`);
  if (t.ssid) lines.push(`SSID: ${escapeHtml(t.ssid)}`);
  if (t.rssi != null && t.rssi !== 0) lines.push(`RSSI: ${t.rssi} dBm`);
  if (t.alt != null) lines.push(`Alt: ${t.alt} m`);
  if (t.last_time) lines.push(`Seen: ${fmtRel(t.last_time)}`);
  lines.push(`Pos: ${t.lat.toFixed(5)}, ${t.lon.toFixed(5)}`);
  return lines.join('<br>');
}

function refreshMap(targets) {
  dynLayer.clearLayers();
  if (!targets || !targets.length) return;
  const bounds = [[STATION.lat, STATION.lon]];
  // Pair drones with their operators for connecting lines
  const droneByMac = {};
  const operatorByMac = {};
  targets.forEach(t => {
    if (t.kind === 'drone') droneByMac[t.mac] = t;
    if (t.kind === 'operator') operatorByMac[t.mac] = t;
    L.marker([t.lat, t.lon], { icon: markerFor(t) })
      .bindPopup(popupFor(t)).addTo(dynLayer);
    bounds.push([t.lat, t.lon]);
  });
  // Draw drone-to-operator connector lines (dashed)
  Object.keys(droneByMac).forEach(mac => {
    if (operatorByMac[mac]) {
      const d = droneByMac[mac]; const o = operatorByMac[mac];
      L.polyline([[d.lat,d.lon],[o.lat,o.lon]],
        { color: '#f78166', weight: 1.5, dashArray: '4,4', opacity: 0.8 })
        .addTo(dynLayer);
    }
  });
  // Auto-fit on first targets
  if (bounds.length > 1 && !window._fitDone) {
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 16 });
    window._fitDone = true;
  }
}

// ----- helpers -----
function fmtRel(ts) {
  if (!ts) return '—';
  const ago = Math.floor(Date.now()/1000 - ts);
  if (ago < 60) return ago + 's ago';
  if (ago < 3600) return Math.floor(ago/60) + 'm ago';
  if (ago < 86400) return Math.floor(ago/3600) + 'h ago';
  return Math.floor(ago/86400) + 'd ago';
}

function sigClass(s) {
  if (s == null || s === 0) return 'weak';
  if (s >= -55) return 'strong';
  if (s >= -75) return 'med';
  return 'weak';
}

function fmtFreq(hz) {
  if (!hz) return '—';
  return (hz/1000).toFixed(0) + ' MHz';
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderDrones(drones) {
  const el = document.getElementById('drone-list');
  document.getElementById('drone-count').textContent = drones.length;
  document.getElementById('drone-count').classList.toggle('zero', drones.length === 0);
  if (!drones.length) {
    el.innerHTML = '<div class="empty">No drones detected yet — Kismet is sweeping (WiFi + BT5).</div>';
    return;
  }
  el.innerHTML = '<table><thead><tr>' +
    '<th>Detected as</th><th>MAC</th><th>SSID</th><th>Manuf</th>' +
    '<th>Channel</th><th class="signal">RSSI</th><th>Last seen</th><th>Why</th>' +
    '</tr></thead><tbody>' +
    drones.map(d => {
      const sig = d.last_signal;
      const ssid = d.last_ssid || '<span style="color:var(--muted)">—</span>';
      const reason = d._drone_reason;
      const badgeCls = reason === 'uav_match' ? 'uav' :
                       reason === 'ssid_pattern' ? 'ssid' : 'manuf';
      return `<tr class="drone-row">
        <td><b>${escapeHtml(d._drone_details || '?')}</b></td>
        <td class="mac">${escapeHtml(d['kismet.device.base.macaddr'])}</td>
        <td class="ssid">${ssid === '—' ? ssid : escapeHtml(ssid)}</td>
        <td>${escapeHtml(d['kismet.device.base.manuf'] || '—')}</td>
        <td>${escapeHtml(d['kismet.device.base.channel'] || '—')} (${fmtFreq(d['kismet.device.base.frequency'])})</td>
        <td class="signal ${sigClass(sig)}">${sig ?? '—'}</td>
        <td>${fmtRel(d['kismet.device.base.last_time'])}</td>
        <td><span class="badge ${badgeCls}">${escapeHtml(reason || '')}</span></td>
      </tr>`;
    }).join('') +
    '</tbody></table>';
}

function renderWifiTable(elId, devices) {
  const el = document.getElementById(elId);
  if (!devices.length) { el.innerHTML = '<div class="empty">Nothing yet.</div>'; return; }
  el.innerHTML = '<table class="compact"><thead><tr>' +
    ['MAC','Manuf','SSID','Type','Channel','RSSI','Pkts','Last seen'].map(h=>`<th>${h}</th>`).join('') +
    '</tr></thead><tbody>' +
    devices.map(d => {
      const sig = d.last_signal;
      return `<tr>
        <td class="mac">${escapeHtml(d['kismet.device.base.macaddr'])}</td>
        <td>${escapeHtml(d['kismet.device.base.manuf'] || '—')}</td>
        <td class="ssid">${escapeHtml(d.last_ssid || '—')}</td>
        <td>${escapeHtml(d['kismet.device.base.type'] || '—')}</td>
        <td>${escapeHtml(d['kismet.device.base.channel'] || '—')}</td>
        <td class="signal ${sigClass(sig)}">${sig ?? '—'}</td>
        <td>${d['kismet.device.base.packets.total'] ?? 0}</td>
        <td>${fmtRel(d['kismet.device.base.last_time'])}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderBtTable(elId, devices) {
  const el = document.getElementById(elId);
  if (!devices.length) { el.innerHTML = '<div class="empty">Nothing yet.</div>'; return; }
  el.innerHTML = '<table class="compact"><thead><tr>' +
    ['Name','MAC','Vendor','Type','Services','RSSI','Pkts','Last seen'].map(h=>`<th>${h}</th>`).join('') +
    '</tr></thead><tbody>' +
    devices.map(d => {
      const sig = d.last_signal;
      const name = d._bt_name || '—';
      const isAnon = name === '(anonymous LE)' || name === '(no name)';
      const manufSource = d._bt_manuf_source || '';
      const sourceBadge = manufSource && manufSource !== 'OUI'
        ? `<span style="color:var(--muted);font-size:10px;margin-left:4px">[${escapeHtml(manufSource)}]</span>`
        : '';
      const typeBadge = d._bt_type === 'LE'
        ? `<span class="badge" style="background:var(--bt);color:#fff">LE</span>`
        : d._bt_type === 'Classic'
        ? `<span class="badge" style="background:var(--accent);color:#fff">Classic</span>`
        : d._bt_type === 'Dual'
        ? `<span class="badge" style="background:var(--good);color:#fff">Dual</span>`
        : '<span class="badge">?</span>';
      return `<tr>
        <td class="ssid" style="${isAnon?'color:var(--muted);font-style:italic':''}">${escapeHtml(name)}</td>
        <td class="mac">${escapeHtml(d['kismet.device.base.macaddr'])}</td>
        <td>${escapeHtml(d._bt_manuf || '—')}${sourceBadge}</td>
        <td>${typeBadge}</td>
        <td style="color:var(--muted);font-size:11px">${escapeHtml(d._bt_services || '—')}</td>
        <td class="signal ${sigClass(sig)}">${sig ?? '—'}</td>
        <td>${d['kismet.device.base.packets.total'] ?? 0}</td>
        <td>${fmtRel(d['kismet.device.base.last_time'])}</td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderHistory(history) {
  const el = document.getElementById('history-list');
  if (!history.length) { el.innerHTML = '<div class="empty">No history yet.</div>'; return; }
  el.innerHTML = '<table class="compact"><tbody>' +
    history.slice().reverse().map(h => `<tr>
      <td style="color:var(--muted)">${fmtRel(h[0])}</td>
      <td class="mac">${escapeHtml(h[1])}</td>
      <td><b>${escapeHtml(h[2])}</b></td>
    </tr>`).join('') + '</tbody></table>';
}

function renderStats(s) {
  const el = document.getElementById('stats');
  const t = s.totals; const st = s.stats;
  const up = s.uptime_secs;
  const upStr = up < 3600 ? Math.floor(up/60)+'m' :
                up < 86400 ? (up/3600).toFixed(1)+'h' : (up/86400).toFixed(1)+'d';
  el.innerHTML = `
    <span>uptime <b>${upStr}</b></span>
    <span>polls <b>${st.polls}</b></span>
    <span>errors <b>${st.errors}</b></span>
    <span>kismet devices <b>${t.all}</b></span>
    <span>APs hidden <b>${t.aps_skipped}</b></span>
    <span>map markers <b>${(s.map_targets||[]).length}</b></span>
    <span>last poll <b>${s.last_poll || '—'}</b></span>
  `;
}

function categoryColour(cat) {
  return ({
    police: '#f85149', rescue: '#3fb950', fire: '#f78166',
    medical: '#a371f7', government: '#58a6ff', infrastructure: '#d29922',
    health: '#3fb950', military: '#8b949e', delivery: '#58a6ff',
    commercial: '#c9d1d9', media: '#a371f7', test: '#8b949e',
  }[cat] || '#8b949e');
}

// ----- Capture Sources panel -----
async function renderSources() {
  try {
    const r = await fetch('/api/sources');
    const s = await r.json();
    const wifi = s.wifi || [];
    const bt = s.bluetooth || [];
    const ks = s.kismet_sources || [];
    const mgmt = s.mgmt_iface || '';

    document.getElementById('src-count').textContent = ks.length;
    document.getElementById('src-count').classList.toggle('zero', ks.length === 0);

    const wifiRows = wifi.map(w => {
      const inUse = w.in_kismet;
      const monitor = w.monitor_capable;
      const isMgmt = w.is_mgmt;
      let status, action;
      if (isMgmt) {
        status = '<span class="badge" style="background:var(--bad);color:#fff">MGMT — leave alone</span>';
        action = '<span style="color:var(--muted)">(SSH path)</span>';
      } else if (inUse) {
        status = `<span class="badge" style="background:var(--good);color:#fff">capturing as ${escapeHtml(w.kismet_source.name)}</span>`;
        action = `<button class="btn-stop" data-uuid="${escapeHtml(w.kismet_source.uuid)}">Stop</button>`;
      } else if (!monitor) {
        status = '<span class="badge" style="background:var(--border)">no monitor mode</span>';
        action = '<span style="color:var(--muted)">incompatible</span>';
      } else if (w.nm_state === 'connected') {
        status = `<span class="badge" style="background:var(--warn);color:#000">in use by NetworkManager</span>`;
        action = `<button class="btn-add" data-def="${escapeHtml(w.suggested_source)}" disabled title="NM manages this interface — set unmanaged first">Add</button>`;
      } else {
        status = '<span class="badge" style="background:var(--accent);color:#fff">available</span>';
        action = `<button class="btn-add" data-def="${escapeHtml(w.suggested_source)}">Add to Kismet</button>`;
      }
      const phyBadge = `<span class="badge">phy${w.phy}</span>`;
      const bandBadge = (w.bands || []).length
        ? `<span class="badge" style="background:var(--accent);color:#fff">${(w.bands||[]).length} band${w.bands.length>1?'s':''}</span>`
        : '<span class="badge">single-band</span>';
      const monBadge = monitor
        ? '<span class="badge" style="background:var(--good);color:#fff">monitor ✓</span>'
        : '<span class="badge" style="background:var(--border)">monitor ✗</span>';
      return `<tr>
        <td><b>${escapeHtml(w.iface)}</b> ${phyBadge}</td>
        <td class="mac">${escapeHtml(w.addr || '—')}</td>
        <td>${escapeHtml(w.type || '?')}</td>
        <td>${monBadge} ${bandBadge}</td>
        <td>${status}</td>
        <td>${action}</td>
      </tr>`;
    }).join('');

    const btRows = bt.map(b => {
      const inUse = b.in_kismet;
      let status, action;
      if (!b.hci) {
        status = '<span class="badge" style="background:var(--border)">no hci device</span>';
        action = '—';
      } else if (inUse) {
        status = `<span class="badge" style="background:var(--good);color:#fff">capturing as ${escapeHtml(b.kismet_source.name)}</span>`;
        action = `<button class="btn-stop" data-uuid="${escapeHtml(b.kismet_source.uuid)}">Stop</button>`;
      } else if (!b.powered) {
        status = '<span class="badge" style="background:var(--warn);color:#000">powered off</span>';
        action = '<span style="color:var(--muted)">power on first</span>';
      } else {
        status = '<span class="badge" style="background:var(--accent);color:#fff">available</span>';
        action = `<button class="btn-add" data-def="${escapeHtml(b.suggested_source)}">Add to Kismet</button>`;
      }
      const defBadge = b.default ? '<span class="badge" style="background:var(--good);color:#fff">default</span>' : '';
      return `<tr>
        <td><b>${escapeHtml(b.hci || '?')}</b> ${defBadge}</td>
        <td class="mac">${escapeHtml(b.mac)}</td>
        <td>${escapeHtml(b.name || '—')}</td>
        <td>${status}</td>
        <td>${action}</td>
      </tr>`;
    }).join('');

    let html = '';
    html += '<h3 style="margin:8px 0 6px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">📡 WiFi adapters</h3>';
    if (wifi.length) {
      html += '<table class="compact"><thead><tr>' +
              '<th>Interface</th><th>MAC</th><th>Mode</th><th>Capabilities</th><th>Status</th><th>Action</th>' +
              '</tr></thead><tbody>' + wifiRows + '</tbody></table>';
    } else {
      html += '<div class="empty">No WiFi adapters detected (is `iw` installed?).</div>';
    }
    html += '<h3 style="margin:14px 0 6px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em">🔵 Bluetooth controllers</h3>';
    if (bt.length) {
      html += '<table class="compact"><thead><tr>' +
              '<th>Controller</th><th>BD Address</th><th>Name</th><th>Status</th><th>Action</th>' +
              '</tr></thead><tbody>' + btRows + '</tbody></table>';
    } else {
      html += '<div class="empty">No Bluetooth controllers detected.</div>';
    }
    html += `<div style="margin-top:10px;font-size:11px;color:var(--muted)">
      Tip: changes here are <b>live but not persistent</b>. To make a source survive
      Kismet restarts, append the <code>source=…</code> line to <code>/etc/kismet/kismet_site.conf</code>
      and run <code>sudo systemctl restart kismet</code>.
    </div>`;
    document.getElementById('sources-panel').innerHTML = html;

    // Wire buttons
    document.querySelectorAll('#sources-panel .btn-add').forEach(b => {
      b.addEventListener('click', async () => {
        b.disabled = true; b.textContent = 'adding…';
        const def = b.dataset.def;
        try {
          const r = await fetch('/api/sources/add', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({definition: def}),
          });
          const j = await r.json();
          if (j.ok) {
            b.textContent = 'added ✓'; b.style.background = 'var(--good)';
            navigator.clipboard?.writeText(def).catch(()=>{});
            setTimeout(renderSources, 2000);
          } else {
            b.textContent = 'failed';
            alert('Kismet rejected the source:\n\n' + (j.error || j.kismet_response || 'unknown'));
            b.disabled = false; b.textContent = 'Add to Kismet';
          }
        } catch (e) {
          b.textContent = 'error'; alert(e.message);
        }
      });
    });
    document.querySelectorAll('#sources-panel .btn-stop').forEach(b => {
      b.addEventListener('click', async () => {
        if (!confirm('Stop this Kismet source? Capture will halt until you re-add it.')) return;
        b.disabled = true; b.textContent = 'stopping…';
        try {
          const r = await fetch('/api/sources/' + encodeURIComponent(b.dataset.uuid) + '/close', {method: 'POST'});
          const j = await r.json();
          if (j.ok) { setTimeout(renderSources, 1500); } else { alert('Failed: ' + (j.error || j.kismet_response)); }
        } catch (e) { alert(e.message); }
      });
    });
  } catch (e) {
    console.warn('sources fetch failed', e);
    document.getElementById('sources-panel').innerHTML = '<div class="empty">Source discovery failed: ' + escapeHtml(e.message) + '</div>';
  }
}

// Buttons need a touch of styling
const _btnStyle = document.createElement('style');
_btnStyle.textContent = `
  #sources-panel button {
    background: var(--accent); color:#fff; border:0; padding:3px 10px;
    border-radius:3px; font:inherit; font-size:11px; cursor:pointer;
  }
  #sources-panel button:disabled { opacity:.5; cursor:not-allowed; }
  #sources-panel button.btn-stop { background: var(--bad); }
  #sources-panel button:hover:not(:disabled) { filter: brightness(1.15); }
  #sources-panel code { background: var(--bg); padding:1px 5px; border-radius:3px; }
`;
document.head.appendChild(_btnStyle);

async function renderOperators() {
  try {
    const r = await fetch('/api/operators');
    const s = await r.json();
    const ops = s.operators || [];
    document.getElementById('op-count').textContent = ops.length;
    document.getElementById('op-count').classList.toggle('zero', ops.length === 0);
    const el = document.getElementById('operator-list');
    if (!ops.length) {
      el.innerHTML = '<div class="empty">No Operator IDs logged yet. Persistent SQLite log builds up as drones broadcast Remote ID over time.</div>';
      return;
    }
    el.innerHTML = '<table class="compact"><thead><tr>' +
      ['Operator ID','Country','Scheme','Known as','Self-ID','First seen','Last seen','×','Drones','Notes']
        .map(h=>`<th>${h}</th>`).join('') + '</tr></thead><tbody>' +
      ops.map(o => {
        const cat = o.known_category || '';
        const labelHtml = o.known_label
          ? `<b style="color:${categoryColour(cat)}">${escapeHtml(o.known_label)}</b>`
          : '<span style="color:var(--muted)">—</span>';
        return `<tr>
          <td class="mac">${escapeHtml(o.operator_id)}</td>
          <td>${o.flag||'🏳️'} ${escapeHtml(o.country||'')}</td>
          <td>${escapeHtml(o.scheme||'?')}</td>
          <td>${labelHtml}</td>
          <td class="ssid" style="max-width:240px;overflow:hidden;text-overflow:ellipsis">${escapeHtml(o.last_self_id||'—')}</td>
          <td>${fmtRel(o.first_seen)}</td>
          <td>${fmtRel(o.last_seen)}</td>
          <td>${o.sightings}</td>
          <td>${o.drones_count||0}</td>
          <td><input type="text" class="op-notes" data-opid="${escapeHtml(o.operator_id)}"
                value="${escapeHtml(o.notes||'')}" placeholder="add note..."
                style="background:var(--bg);color:var(--text);border:1px solid var(--border);padding:2px 6px;font:inherit;width:200px"></td>
        </tr>`;
      }).join('') + '</tbody></table>';
    // Wire notes editing — debounced save on blur
    el.querySelectorAll('.op-notes').forEach(inp => {
      inp.addEventListener('change', async () => {
        await fetch('/api/operator/' + encodeURIComponent(inp.dataset.opid) + '/notes', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({notes: inp.value}),
        });
        inp.style.borderColor = 'var(--good)';
        setTimeout(() => inp.style.borderColor = 'var(--border)', 1500);
      });
    });
  } catch (e) {
    console.warn('operator fetch failed', e);
  }
}

async function refresh() {
  try {
    const r = await fetch('/api/devices');
    const s = await r.json();
    document.getElementById('pulse').classList.toggle('bad', !s.kismet_ok);
    if (!s.kismet_ok) {
      document.getElementById('status').innerHTML =
        '<span class="err">kismet error: ' + escapeHtml(s.kismet_error || 'unknown') + '</span>';
    } else {
      document.getElementById('status').textContent = 'kismet ok · last poll ' + (s.last_poll || '—');
    }
    renderDrones(s.drones || []);
    document.getElementById('wifi-count').textContent = (s.wifi_clients||[]).length;
    document.getElementById('wifi-count').classList.toggle('zero', !(s.wifi_clients||[]).length);
    document.getElementById('bt-count').textContent = (s.bt_devices||[]).length;
    document.getElementById('bt-count').classList.toggle('zero', !(s.bt_devices||[]).length);
    renderWifiTable('wifi-list', s.wifi_clients || []);
    renderBtTable('bt-list', s.bt_devices || []);
    renderHistory(s.history || []);
    renderStats(s);
    refreshMap(s.map_targets || []);
  } catch (e) {
    document.getElementById('pulse').classList.add('bad');
    document.getElementById('status').innerHTML = '<span class="err">dashboard error: ' + escapeHtml(e.message) + '</span>';
  }
}

document.getElementById('poll-secs').textContent = (POLL_MS/1000).toFixed(0);
refresh();
renderOperators();
renderSources();
setInterval(refresh, POLL_MS);
setInterval(renderOperators, 15000);  // operators panel refreshes slower
setInterval(renderSources, 30000);    // sources panel refreshes slowest
</script>
</body>
</html>
"""


# -----------------------------------------------------------------------------
# main

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("starting drone-dashboard on %s:%d, polling %s every %ds (station %.4f,%.4f)",
             HTTP_HOST, HTTP_PORT, KISMET_URL, POLL_SECS, STATION_LAT, STATION_LON)
    log.info("operator log at %s, ntfy: %s", SQLITE_DB, NTFY_URL or "DISABLED (set NTFY_URL env)")
    init_db()
    t = threading.Thread(target=poller_loop, daemon=True, name="kismet-poller")
    t.start()
    app.run(host=HTTP_HOST, port=HTTP_PORT, threaded=True, debug=False)
