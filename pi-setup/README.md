# pi-setup

System changes on the Pi that the code can't make itself. Everything here is installed by one script and removed by another, so the Pi can always be rebuilt or rolled back.

```bash
sudo ~/carpi/pi-setup/install.sh     # in an SSH session on the Pi; safe to run again
sudo ~/carpi/pi-setup/uninstall.sh   # undo
```

`install.sh` ends with a self-test: it asks polkit, as the running Bluetooth service, whether each action is allowed, and prints `yes` / `NO` per line.

## What it installs, and why

| File | Installed to | What it allows |
| --- | --- | --- |
| `50-carpi.rules` | `/etc/polkit-1/rules.d/` | The user `carpi` (the Bluetooth service) may manage Wi-Fi through NetworkManager (add, change, forget, connect, scan) and shut down or restart the Pi, without a password. **Only those seven actions.** `carpi` can already do all of this with `sudo` when logged in; this only lets the background service do it for the phone page's Settings tab. |
| `../systemd/carpi-ble.service` | `/etc/systemd/system/` | Adds `CAP_SYS_TIME`, so the service can set the clock from the phone, only when the Pi isn't synced over the internet and the phone's time is later. |
| `carpi-clock-save.service` + `.timer` | `/etc/systemd/system/` | Saves the time every minute. After a power cut the Pi then starts from the minute it lost power instead of its last internet sync. |

## What it never allows

Nothing on the car's CAN bus, no arbitrary commands, no access to saved Wi-Fi passwords (NetworkManager keeps them root-only, and the phone can't read them back). The Bluetooth link is encrypted and only paired phones get in; pairing needs a window you open yourself over SSH.

## Checking by hand

```bash
pid=$(systemctl show -p MainPID --value carpi-ble)
pkcheck --action-id org.freedesktop.login1.reboot --process "$pid" && echo allowed
```
