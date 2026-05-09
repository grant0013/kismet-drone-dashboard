# kismet-drone-dashboard

A focused live dashboard for **drone detection** built on top of [Kismet](https://www.kismetwireless.net/). Filters out the noise of regular WiFi APs and gives you:

- 🚁 **Drones** detected via Kismet's UAV phy + extended UK police/enterprise pattern matching
- 👤 **Operator IDs** logged to SQLite — country flags, format validation, UK organisation pattern matching, persistent sightings, editable notes
- 🗺️ **Live map** plotting drone GPS + operator GPS from Remote ID broadcasts (Leaflet + OpenStreetMap)
- 📡 **WiFi clients** + 🔵 **Bluetooth devices** with vendor inference, BT5 LE Privacy detection, BT SIG company ID lookup
- 🔔 Optional **ntfy push notifications** when a new operator appears or a drone is overhead (RSSI threshold)

Designed for a Raspberry Pi 3B+ / 4 with a USB monitor-mode WiFi adapter, but runs on any Linux box that can host Kismet.

---

## What problem does it solve?

Kismet's web UI is excellent but it's a general-purpose Wi-Fi/BT/SDR analyser — most of the screen is taken up by neighbour APs and routine traffic. If you specifically care about **what drones are flying overhead, who's operating them, and where**, you don't want to wade through 200 access points to find that one entry.

This dashboard polls Kismet's REST API, classifies devices using:
- Kismet's built-in `uav_match` rules
- An extra ~30 `uav_match=` rules tuned for UK police / enterprise / delivery fleets
- SSID + manufacturer regex fallbacks
- Remote ID payload extraction (operator ID, self-ID, drone serial, GPS)

…then renders only what's drone-relevant, with a live map for any GPS data the broadcasts contain.

---

## Hardware requirements

Minimum:
- **Linux host** (Pi 3B+ / Pi 4 / any x86 box). Tested on Pi OS Trixie / Debian 13
- **Monitor-mode capable USB WiFi adapter**. Known good: any RTL8812AU/RTL8811AU adapter with the [aircrack-ng/rtl8812au](https://github.com/aircrack-ng/rtl8812au) driver. Avoid in-tree drivers — they're often hobbled for monitor mode.

Recommended:
- **Bluetooth 5 USB dongle** with Realtek RTL8761B chip (e.g. TP-Link UB500, ASUS BT500). The Pi's onboard Bluetooth (BCM43438, BT4.1) **cannot** decode BT5 Long Range Coded PHY which is one of the Remote ID transports. A BT5 dongle catches drones the onboard chip can't.
- **Powered USB hub** if running multiple adapters on a Pi 3B — the bus power budget is tight.

Doesn't help (yet):
- RTL-SDR — caps at 1.766 GHz, can't see DJI's 2.4/5.8 GHz control links. Useful for **ADS-B cross-correlation** (some commercial drones broadcast ADS-B Out), but not direct drone detection.
- **For real DJI OcuSync DroneID decode** you'd want an [AntSDR E200](https://github.com/MicroPhase/antsdr_uk) (~£250) running the DroneID firmware — Kismet has the `kismet-capture-antsdr-droneid` package ready for it.

---

## Software requirements

- **Kismet 2025-09-R1+** from the official [APT repo](https://www.kismetwireless.net/packages/) (Debian/Ubuntu) or built from source. Default Debian repo packages are usually too old.
- **Python 3.10+**
- **Flask + requests** (Python deps installed automatically by `install.sh`)

---

## Quick install

```bash
# 1. Clone
git clone https://github.com/grant0013/kismet-drone-dashboard.git
cd kismet-drone-dashboard

# 2. Run the installer (will prompt for sudo where needed)
sudo ./install.sh

# 3. Edit config (set your station coordinates at minimum)
sudo nano /etc/drone-dashboard/config.env

# 4. Start
sudo systemctl start drone-dashboard

# 5. Open the dashboard
xdg-open http://localhost:8081/   # or browse from another machine on your LAN
```

`install.sh` will:

1. Verify Kismet is installed
2. Add your service user to the `kismet` group (so it can read `/etc/kismet/kismet_httpd.conf`)
3. Install Python deps (`flask`, `requests`) via apt where possible, pip otherwise
4. Create `/opt/drone-dashboard/`, `/var/lib/drone-dashboard/`, `/etc/drone-dashboard/`
5. Copy `server.py` into place, render the systemd unit
6. **Optionally** append the bundled UK police/enterprise drone signatures to `/etc/kismet/kismet_site.conf` (you'll be prompted; backs up first)
7. Enable the service at boot

---

## Configuration

Edit `/etc/drone-dashboard/config.env`:

```ini
# Map centre — set to your station's lat/lon
STATION_LAT=51.5074
STATION_LON=-0.1278
STATION_NAME=My Drone Station

# ntfy push notifications — pick a hard-to-guess topic name
# (it's the only access control on the free tier)
# Subscribe to https://ntfy.sh/<topic> in the ntfy.sh phone app first
NTFY_URL=
NTFY_RSSI_THRESHOLD=-60

# Where to find Kismet's HTTP creds
KISMET_URL=http://localhost:2501
KISMET_CONF=/etc/kismet/kismet_httpd.conf

# Persistent operator log
SQLITE_DB=/var/lib/drone-dashboard/operators.db

# Listening interface
HTTP_HOST=0.0.0.0
HTTP_PORT=8081

# Polling cadence
POLL_SECS=5
```

After editing: `sudo systemctl restart drone-dashboard`.

---

## Setting up Kismet's drone-detection sources

This dashboard expects Kismet to already be capturing on at least one source. Recommended:

```ini
# /etc/kismet/kismet_site.conf

# WiFi monitor adapter — replace wlan1 with your monitor-mode interface
source=wlan1:type=linuxwifi,name=wlan1-monitor

# Bluetooth (active scan picks up more device names than passive)
source=hci1:type=linuxbluetooth,name=bt-monitor,active=true

# Channel hop weighted toward drone bands
# 2.4 GHz: 1, 6, 11 (Remote ID + WiFi Direct)
# 5 GHz UNII-1: 36, 44 (some drones)
# 5 GHz UNII-3: 149, 153, 157, 161 (DJI OcuSync 5.8 GHz)
channels="1,6,11,1,6,11,36,44,149,153,157,161"
channel_hop_speed=5/sec

# Web UI access (server.py reads this for REST API auth)
httpd_home=/usr/share/kismet/httpd

# UK-specific drone signatures (auto-added if you said yes during install)
# See conf/kismet_site_uav_uk.conf in this repo for the full list
```

Make sure your service user is in the `kismet` group so it can read `/etc/kismet/kismet_httpd.conf` (mode 640):

```bash
sudo usermod -aG kismet $USER
```

---

## Customising operator pattern matching

The bundled patterns in `server.py` (`UK_KNOWN_OPERATORS`) cover ~50 UK organisations — police forces, fire & rescue, NHS, delivery trials, etc. To extend for other countries / orgs, edit the `_build_operator_patterns()` function. PRs welcome for non-UK pattern sets.

The `COUNTRY_FLAGS` dict at the top of `server.py` handles country-code → flag emoji mapping for parsed Operator IDs.

---

## Operator ID — the realistic picture

Remote ID broadcasts include an **Operator ID** field — a regulator-issued identifier like `GBR-OP-XXXXXXXXX` (UK CAA), 16-character EU EASA codes, or FAA Part 107 numbers.

**There is no public database mapping Operator ID → person/company.** This is intentional — Remote ID is designed for "privacy-preserving accountability." Only the relevant aviation authority (CAA, FAA, EASA) can resolve an OperatorID to its registered owner, and they do so only for police investigations.

What this dashboard *does* give you:

- **Country flag** decoded from the prefix
- **Format validation** (catches obviously malformed / spoofed IDs)
- **Self-ID pattern matching** — operators voluntarily add free-text descriptions like "Hampshire Police drone unit" or "Network Rail track survey". These match against the bundled UK org regex list
- **Persistent log** — every Operator ID seen is stored with timestamps, so over weeks you build up a private intelligence picture: "GBR-OP-XXX has flown over my area 4 times this month, always 09:00–11:00 weekday mornings"
- **Editable notes** per operator — add your own annotations

---

## API endpoints

The dashboard also exposes its own REST API for integrations:

- `GET /api/devices` — full snapshot of drones, WiFi clients, BT devices, map targets
- `GET /api/operators` — all logged operators with sightings counts
- `GET /api/operator/<id>` — single operator detail with sighting history
- `POST /api/operator/<id>/notes` — update notes (`{"notes": "..."}`)
- `GET /api/health` — minimal liveness check

Useful for piping into Home Assistant, Grafana, MQTT, etc.

---

## Limitations

- **Passive scan only** — Kismet's `linuxbluetooth` capture helper doesn't fully expose BT manufacturer-data company IDs even with active scanning enabled. We work around it with BLE Local Name vendor inference.
- **No DJI OcuSync RF decode** — that needs an SDR (HackRF / AntSDR). Kismet's `kismet-capture-antsdr-droneid` package is available but requires the AntSDR hardware.
- **Remote ID compliance is patchy** — many older hobby drones don't broadcast Operator ID at all, even though regulations require it. Police / commercial drones (DJI Matrice/Mavic Enterprise series) are typically compliant.
- **UK-focused defaults** — bundled patterns target UK organisations. Other regions need pattern customisation.

---

## Privacy note

The dashboard processes only **publicly broadcast** Remote ID data — that's the regulatory intent. It logs Operator IDs locally, not to any cloud service. You alone decide what to do with the data.

If you publish your dashboard externally (e.g. via a tunnel), **do not include screenshots of real Operator IDs from other people's drones** without consent — even though they're public broadcasts, normalising aggregation may run afoul of UK GDPR's "purpose limitation" principle.

---

## Architecture

```
                 ┌─────────────────────────────────────┐
                 │          Browser (you)              │
                 │   http://<host>:8081/               │
                 └─────────────┬───────────────────────┘
                               │ HTTP + JSON
                 ┌─────────────▼───────────────────────┐
                 │   drone-dashboard (Flask, port 8081)│
                 │   - polls Kismet every 5s           │
                 │   - classifies drones / clients     │
                 │   - persists operators to SQLite    │
                 │   - serves single-page UI + API     │
                 └─────┬───────────────────────────┬───┘
                       │ HTTP + Basic auth         │
                       │                           │ ntfy POST
                 ┌─────▼─────────────┐    ┌────────▼────────┐
                 │ Kismet (port 2501)│    │ ntfy.sh (opt)   │
                 └─────┬─────────────┘    └─────────────────┘
                       │ HCI / nl80211
            ┌──────────┴──────────┐
            │                     │
       ┌────▼────┐           ┌────▼────┐
       │ wlan1   │           │ hci1    │
       │ monitor │           │ BT5     │
       │ mode    │           │ active  │
       └─────────┘           └─────────┘
```

`server.py` is intentionally a **single file** so you can read top-to-bottom and audit / fork easily.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Contributing

PRs welcome, especially for:
- **Operator pattern matchers for non-UK regions** (US, EU member states, AU, NZ, CA)
- **Drone signature additions** to `conf/kismet_site_uav_uk.conf` as new models reach market
- **Test cases** with known-format Operator ID strings (sanitised)
- **Better Remote ID field extraction** if you find Kismet exposing fields we're missing

Please don't commit screenshots containing real Operator IDs unless you've checked with the operator.
