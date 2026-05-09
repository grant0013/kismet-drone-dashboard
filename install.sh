#!/usr/bin/env bash
# install.sh — set up kismet-drone-dashboard on a Linux host already running Kismet
#
# Usage:  sudo ./install.sh
#
# Idempotent: safe to re-run. Won't overwrite an existing config.env, won't
# duplicate UAV signatures in kismet_site.conf, won't clobber an existing
# operators.db.

set -euo pipefail

# ---- helpers ---------------------------------------------------------------
RED=$'\033[31m'; GRN=$'\033[32m'; YLW=$'\033[33m'; NC=$'\033[0m'
info()  { printf "${GRN}[+]${NC} %s\n" "$*"; }
warn()  { printf "${YLW}[!]${NC} %s\n" "$*"; }
fail()  { printf "${RED}[✗]${NC} %s\n" "$*" >&2; exit 1; }
ask()   { local q="$1" def="${2:-Y}" reply; read -rp "$q [${def}] " reply; reply="${reply:-$def}"; [[ "$reply" =~ ^[Yy]$ ]]; }

# ---- preflight -------------------------------------------------------------
[[ $EUID -eq 0 ]] || fail "Run with sudo: sudo ./install.sh"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$REPO_DIR/server.py" ]] || fail "server.py not found in $REPO_DIR"

info "Repo: $REPO_DIR"

# Identify the user who invoked sudo (we'll run the service as them so they
# can read /etc/kismet/kismet_httpd.conf via the kismet group)
SERVICE_USER="${SUDO_USER:-${USER:-root}}"
if [[ "$SERVICE_USER" == "root" ]]; then
    warn "Running as root with no SUDO_USER; service will run as root."
    warn "Recommended: re-run via sudo from a regular user account."
fi
info "Service user: $SERVICE_USER"

# ---- check kismet ----------------------------------------------------------
if ! command -v kismet >/dev/null 2>&1; then
    fail "Kismet not found. Install it first from https://www.kismetwireless.net/packages/"
fi
info "Kismet found: $(kismet --version 2>&1 | head -1 || echo 'version unknown')"

# Service user needs to be in the kismet group to read kismet_httpd.conf
if [[ "$SERVICE_USER" != "root" ]] && ! id -nG "$SERVICE_USER" 2>/dev/null | grep -qw kismet; then
    info "Adding $SERVICE_USER to kismet group"
    usermod -aG kismet "$SERVICE_USER"
    warn "$SERVICE_USER will need to log out & back in for the group change"
    warn "to apply to interactive shells (the service inherits it on (re)start)."
fi

# ---- python deps -----------------------------------------------------------
info "Installing Python deps (flask, requests)"
if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y python3-flask python3-requests >/dev/null
elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3-flask python3-requests >/dev/null
elif command -v pacman >/dev/null 2>&1; then
    pacman -S --noconfirm python-flask python-requests >/dev/null
else
    warn "No supported package manager — falling back to pip"
    pip3 install --break-system-packages -r "$REPO_DIR/requirements.txt"
fi

# ---- create dirs -----------------------------------------------------------
INSTALL_DIR="/opt/drone-dashboard"
STATE_DIR="/var/lib/drone-dashboard"
CONFIG_DIR="/etc/drone-dashboard"

info "Creating directories: $INSTALL_DIR, $STATE_DIR, $CONFIG_DIR"
install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -d -m 0755 -o "$SERVICE_USER" "$STATE_DIR"

# ---- copy server.py --------------------------------------------------------
info "Installing server.py → $INSTALL_DIR/server.py"
install -m 0755 -o "$SERVICE_USER" "$REPO_DIR/server.py" "$INSTALL_DIR/server.py"

# ---- config.env ------------------------------------------------------------
CONFIG_FILE="$CONFIG_DIR/config.env"
if [[ -f "$CONFIG_FILE" ]]; then
    info "Existing $CONFIG_FILE preserved (will not overwrite)"
else
    info "Creating $CONFIG_FILE template"
    cat > "$CONFIG_FILE" <<EOF
# kismet-drone-dashboard configuration
# Reload changes:  sudo systemctl restart drone-dashboard

# Map centre — set to your station's latitude / longitude
STATION_LAT=51.5074
STATION_LON=-0.1278
STATION_NAME=Drone Station

# ntfy push notifications (optional)
# Pick a hard-to-guess topic name — it's the only access control on free ntfy.
# Example:  NTFY_URL=https://ntfy.sh/sdr-pi-drones-7f9k2x
# Subscribe to the same URL in the ntfy.sh app on your phone.
NTFY_URL=
NTFY_RSSI_THRESHOLD=-60

# Where the SQLite operator log lives (survives reboots)
SQLITE_DB=$STATE_DIR/operators.db

# Kismet API
KISMET_URL=http://localhost:2501
KISMET_CONF=/etc/kismet/kismet_httpd.conf

# HTTP listen
HTTP_HOST=0.0.0.0
HTTP_PORT=8081

# Polling cadence (seconds)
POLL_SECS=5
EOF
    chmod 0644 "$CONFIG_FILE"
fi

# ---- systemd unit ----------------------------------------------------------
UNIT_FILE="/etc/systemd/system/drone-dashboard.service"
info "Installing systemd unit → $UNIT_FILE"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Drone detection dashboard (polls Kismet REST API)
Documentation=https://github.com/grant0013/kismet-drone-dashboard
After=network-online.target kismet.service
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=kismet
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$CONFIG_FILE
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $INSTALL_DIR/server.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_FILE"

# ---- optional: append UK UAV signatures ------------------------------------
KISMET_SITE_CONF="/etc/kismet/kismet_site.conf"
UAV_RULES="$REPO_DIR/conf/kismet_site_uav_uk.conf"
APPEND_MARKER="# ---- UK-relevant drone signatures ----"

if [[ -f "$UAV_RULES" ]]; then
    if grep -qF "$APPEND_MARKER" "$KISMET_SITE_CONF" 2>/dev/null; then
        info "UK drone signatures already in $KISMET_SITE_CONF"
    else
        if ask "Append UK police/enterprise drone signatures to $KISMET_SITE_CONF?"; then
            if [[ -f "$KISMET_SITE_CONF" ]]; then
                cp "$KISMET_SITE_CONF" "$KISMET_SITE_CONF.bak.$(date +%s)"
            else
                touch "$KISMET_SITE_CONF"
            fi
            cat "$UAV_RULES" >> "$KISMET_SITE_CONF"
            info "Appended UK signatures to $KISMET_SITE_CONF"
            info "Run 'sudo systemctl restart kismet' to load them"
        fi
    fi
fi

# ---- enable + summary ------------------------------------------------------
systemctl daemon-reload
systemctl enable drone-dashboard >/dev/null 2>&1 || true

cat <<EOF

${GRN}Install complete.${NC}

Next steps:
  1. Edit ${YLW}$CONFIG_FILE${NC} (set your STATION_LAT / STATION_LON at minimum)
  2. Start the service:
       ${GRN}sudo systemctl start drone-dashboard${NC}
  3. Browse to:
       ${GRN}http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):8081/${NC}
  4. (Optional) enable ntfy push notifications — see config.env

Logs:    ${GRN}journalctl -u drone-dashboard -f${NC}
Stop:    ${GRN}sudo systemctl stop drone-dashboard${NC}
Disable: ${GRN}sudo systemctl disable drone-dashboard${NC}
Remove:  ${GRN}sudo ./uninstall.sh${NC}

EOF
