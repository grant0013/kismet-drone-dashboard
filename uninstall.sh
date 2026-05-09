#!/usr/bin/env bash
# uninstall.sh — remove kismet-drone-dashboard
#
# By default keeps your operators.db (your data). Pass --purge to nuke it too.

set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo: sudo ./uninstall.sh"; exit 1; }

PURGE=false
[[ "${1:-}" == "--purge" ]] && PURGE=true

INSTALL_DIR="/opt/drone-dashboard"
STATE_DIR="/var/lib/drone-dashboard"
CONFIG_DIR="/etc/drone-dashboard"
UNIT_FILE="/etc/systemd/system/drone-dashboard.service"

echo "[+] Stopping + disabling drone-dashboard"
systemctl stop drone-dashboard 2>/dev/null || true
systemctl disable drone-dashboard 2>/dev/null || true

echo "[+] Removing systemd unit"
rm -f "$UNIT_FILE"
systemctl daemon-reload

echo "[+] Removing $INSTALL_DIR"
rm -rf "$INSTALL_DIR"

if $PURGE; then
    echo "[!] --purge: removing $STATE_DIR (including operators.db) and $CONFIG_DIR"
    rm -rf "$STATE_DIR" "$CONFIG_DIR"
else
    echo "[+] Preserving $STATE_DIR (operators.db) and $CONFIG_DIR"
    echo "    Pass --purge to remove these too."
fi

echo
echo "Note: any UAV signatures appended to /etc/kismet/kismet_site.conf are"
echo "left in place. Remove the block starting '# ---- UK-relevant drone"
echo "signatures ----' manually if you want to undo that part."
echo
echo "Done."
