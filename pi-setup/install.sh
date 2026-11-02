#!/usr/bin/env bash
# CarPi system setup for the phone page's Settings tab. Run on the Pi, in an SSH session:
#   sudo ~/carpi/pi-setup/install.sh
# Safe to run again (after a git pull that changes pi-setup/ or systemd/carpi-ble.service).
# Undo with  sudo ~/carpi/pi-setup/uninstall.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run it with sudo:  sudo $0" >&2
  exit 1
fi
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(dirname "$here")"

if ! command -v pkcheck >/dev/null; then
  echo "polkit isn't installed. Install it first:  sudo apt install -y polkitd" >&2
  exit 1
fi

echo "1/4 Permission rules -> /etc/polkit-1/rules.d/50-carpi.rules"
install -d -m 755 /etc/polkit-1/rules.d
install -m 644 -o root -g root "$here/50-carpi.rules" /etc/polkit-1/rules.d/50-carpi.rules

echo "2/4 Bluetooth service unit (adds CAP_SYS_TIME for setting the clock from the phone)"
install -m 644 "$repo/systemd/carpi-ble.service" /etc/systemd/system/carpi-ble.service
rm -f /etc/systemd/system/carpi-ble.service.d/carpi-no-clock.conf    # left by uninstall.sh

echo "3/4 Save the time every minute (carpi-clock-save.timer)"
install -m 644 "$here/carpi-clock-save.service" "$here/carpi-clock-save.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now carpi-clock-save.timer >/dev/null

echo "4/4 Restarting carpi-ble"
systemctl restart carpi-ble
sleep 2

# ---- self-test: ask polkit, as the running Bluetooth service, about every action ----
pid="$(systemctl show -p MainPID --value carpi-ble)"
if [ -z "$pid" ] || [ "$pid" = "0" ]; then
  echo "carpi-ble isn't running; check: journalctl -u carpi-ble -n 30" >&2
  exit 1
fi
ok=1
echo
echo "Checking what the Bluetooth service (pid $pid) is now allowed to do:"
for action in org.freedesktop.NetworkManager.settings.modify.system \
              org.freedesktop.NetworkManager.network-control \
              org.freedesktop.NetworkManager.wifi.scan \
              org.freedesktop.login1.power-off \
              org.freedesktop.login1.reboot; do
  if pkcheck --action-id "$action" --process "$pid" >/dev/null 2>&1; then
    echo "  yes  $action"
  else
    echo "  NO   $action"
    ok=0
  fi
done
caps="$(awk '/^CapAmb/ {print $2}' "/proc/$pid/status")"
if [ $(( (16#$caps >> 25) & 1 )) -eq 1 ]; then      # CAP_SYS_TIME is capability 25
  echo "  yes  set the clock (CAP_SYS_TIME)"
else
  echo "  NO   set the clock (CAP_SYS_TIME)"
  ok=0
fi
echo
if [ "$ok" -eq 1 ]; then
  echo "All set. Open the phone page, Settings tab."
else
  echo "Something above wasn't granted. Send the output of this script and: journalctl -u carpi-ble -n 30" >&2
  exit 1
fi
