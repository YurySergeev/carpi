#!/usr/bin/env bash
# Undo pi-setup/install.sh: the Settings tab then can't change Wi-Fi, power or the clock (the phone
# shows the setup message). Presets, the Live tab and logging are unaffected.
#   sudo ~/carpi/pi-setup/uninstall.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo:  sudo $0" >&2
  exit 1
fi

rm -f /etc/polkit-1/rules.d/50-carpi.rules
echo "Removed the permission rules."

systemctl disable --now carpi-clock-save.timer >/dev/null 2>&1 || true
rm -f /etc/systemd/system/carpi-clock-save.service /etc/systemd/system/carpi-clock-save.timer
echo "Removed the clock-save timer."

# The repo's carpi-ble.service grants CAP_SYS_TIME; take it away again with a drop-in, so
# re-running install.sh (which deletes this drop-in) is the only way to bring it back.
install -d -m 755 /etc/systemd/system/carpi-ble.service.d
printf '[Service]\nAmbientCapabilities=\nAmbientCapabilities=CAP_NET_ADMIN\n' \
  > /etc/systemd/system/carpi-ble.service.d/carpi-no-clock.conf
systemctl daemon-reload
systemctl restart carpi-ble
echo "carpi-ble can no longer set the clock. Done."
