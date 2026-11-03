# CarPi: session handoff

**Written:** Oct 6, 2026, at the end of a long Cowork session. **Updated:** Nov 2, 2026 (Bluetooth phone page, Live tab, oil-temperature probe, Pi settings phase 1 and the time zone, tests in CI, current paths).
**Latest session state** (uncommitted work, open decisions, next steps): `notes/SESSION_HANDOFF.md` in the working clone. It's git-ignored, so it only exists on Yury's laptop; read it first if it's there.
**For:** Claude in a new Cowork or Claude Code session picking up CarPi.
**Owner:** Yury, who is also your user.

Read sections 1–3 before doing anything. The rest is reference.

---

## 1. TL;DR

- **What it is.** CarPi is a Raspberry Pi 4 on the OBD-II port of Yury's 2017 Audi A3. It polls the engine ECU over CAN about 7.7 times a second and writes one CSV per drive. A phone page over Bluetooth LE picks what gets logged (presets), shows the Pi's health, and shows chosen values live with a cold-start warm-up card. On the PC side: a static drive viewer on GitHub Pages, a Dash analyzer app on Render, and HTML reports from `analyze_drives.py`. It's a portfolio project and a before/after tool for planned car work.
- **Where the code is.** On GitHub, `YurySergeev/carpi`. The working clone on Yury's PC is `C:\Users\Yury\Documents\Software\carpi` (Oct 30: matched GitHub, clean). The Pi's `~/carpi` is a git checkout of the same repo and was up to date on Oct 30.
- **Status.**
  - Logging works end to end. The Live tab and presets work on the car (Oct 30).
  - The 28 baseline drives (Sept 25 – Oct 1) show one finding: idle fuel trim is about +7.5–8.6% while cruise is about 0%. That's a vacuum leak, most likely the PCV.
  - The viewer and analyzer are live.
  - Nothing has been logged after the repair yet.
  - Oil temperature: `probe_oil.py` is ready; Yury hasn't run the one-time check yet (§9, task 2).
- **Most likely next tasks.**
  1. Pi settings (Settings tab: Wi-Fi, shut down/restart, clock, and the time zone matched to the phone's): run `pi-setup/install.sh` and test it on the Pi.
  2. Automatic upload to the public analyzer, with an Uploads card in Settings (automatic on/off, Upload now, publish on/off): built Nov 2 as protocol 5, together with the time zone (protocol 4), committed and pushed as `fb597d7`. Next: the setup in `docs/data-sync.md`, each GitHub step asking first.
  3. Drive analysis on the phone (protocol 6, the Analysis tab): built Nov 3, not committed yet. On the Pi: `git pull`, `sudo systemctl restart carpi-ble`, then try the tab during a drive and on a saved drive.
  4. Pi settings phase 2: tools from the phone (the oil check), services, Update CarPi. Phase 3: diagnostics, SSH toggle, pairing (the read-only drives list is part of the Analysis tab).
  5. Oil-temperature check, then the logger reads it.
  6. The post-PCV comparison once Yury has done the repair.
  7. Planned but not started: boot speed, a replay simulator.
  See §9 for the full backlog.

## 2. Hard rules

1. **Read-only on the car, always.**
   - Allowed: OBD-II Mode 01 and Mode 06 requests, and UDS `0x22` ReadDataByIdentifier for specific, known DIDs.
   - Never send anything that writes or changes state: coding, adaptations, routine control, security access, session changes for flashing, or flashing itself. That applies to the ECU, the TCU (DSG) and every other module.
   - Tuning is out of scope. Yury knows it's high-risk; CarPi only measures.
2. **Don't write sweeping probes.** No scripts that brute-force or enumerate identifiers, services or modules. Add VW identifiers one at a time, from a list confirmed with VCDS (see §9, task 4).
3. **Never transmit while the car's bus is asleep.** The logger only polls after it sees bus traffic, and gives up after 40 tries (~2 min) per wake-up. That keeps it from holding a parked car awake and draining the battery. Keep that behavior in any change.
4. **One talker at a time.** Stop `carpi-logger` before running `probe_addressing.py`, `probe_oil.py` or any other tool on `can0`, or the two will read each other's replies. (`probe_oil.py` refuses to start while the logger is active.) The phone page never talks to the car: it only reads the logger's files.
5. **Ask before every commit and every push.** Leave changes uncommitted, show what changed, and wait for an explicit yes, separately for the commit and for the push. Approving a change isn't approving the commit. Also ask before `deploy/publish.py render`, Render or Pages deploys, and posting anywhere. One standing exception (Nov 2): once uploads are set up, the `carpi-data` Action republishing the analyzer on every upload from the Pi is approved. Setting that up (repos, deploy keys, the `CARPI_LIVE_KEY` secret) still asks, one step at a time. No `Co-Authored-By: Claude` lines in commits: the README credits Claude instead.
6. **Keep private things out of git.**
   - `CarPi_Documents/` is excluded via `.git/info/exclude`. `logs/`, `probes/`, `candump/` and `tag.txt` are in `.gitignore`.
   - Never commit Wi-Fi names or passwords, including the Pi's password.
   - The public viewer deliberately shows `elapsed_s` only, so it never reveals when the car was driven. Don't add wall-clock times to the viewer or the hosted app.
7. **No secrets in chat or files.** Don't ask for the Pi's password or type it anywhere; Yury enters it himself when `ssh` or `scp` prompts.

## 3. Working with Yury

- He's a CS student at Kent State, graduating Dec 2026. CarPi is on his resume as an in-progress project. He's on Windows with PowerShell and reaches the Pi over SSH.
- Once he's decided on something, he wants execution-focused help: concrete commands, file paths, prior art. Don't re-argue the risks.
- **Always say which machine and which terminal a command runs in.** "PowerShell on the PC" and "SSH session on the Pi" are different places. Mixing them up has cost time before:
  - `scp` typed inside the SSH session
  - shell commands typed into a Python REPL
  - a script saved to `~/` instead of `~/carpi/`
- Give copy-paste-ready commands.

---

## 4. Where everything lives

| What | Where |
| --- | --- |
| Code (master copy) | https://github.com/YurySergeev/carpi |
| Working clone (PC) | `C:\Users\Yury\Documents\Software\carpi`. Git-ignored `notes\specs\` there holds the design specs (e.g. the Live tab). No `logs\` folder. |
| Older copy (PC) | `C:\Users\Yury\source\CarPi`: stopped at Oct 2 in git, but holds the 28 drives (Sept 25 – Oct 1) in `logs\logs\`, and git-ignored `notes\` (viewer spec, mockups). Don't delete it. |
| Compiled docs (PC, git-ignored) | `carpi\CarPi_Documents\` in the earlier OneDrive clone (`C:\Users\yumse\OneDrive\Documents\carpi`, device `yura-pc`): PDFs of the Claude docs, reports, scripts, notes. Not on the `C:\Users\Yury` machine. |
| Phone page | https://yurysergeev.github.io/carpi/config/ (Web Bluetooth: Chrome on Android, Bluefy on iPhone). `?demo` tries it without the car. |
| The Pi | `ssh carpi@carpi.local`, code in `~/carpi/`, logs in `~/carpi/logs/<YYYY-MM-DD>/drive-HHMMSS.csv` + `.json` |
| All drives (complete set) | On the Pi (`~/carpi/logs/`). Also in the Render source repo `YurySergeev/carpi-live` under `drives/` (everything published so far). Once uploads are set up: the private `YurySergeev/carpi-data`, which the Pi pushes to (`docs/data-sync.md`). |
| Upload pipeline (not set up yet) | Pi `carpi_sync.py` → private `carpi-data` → its Action (`check_drives.py`, `publish.py render`) → `carpi-live` → Render. Master copy of `carpi-data`'s README and workflow: `deploy/data-repo/`. |
| Drive viewer | https://yurysergeev.github.io/carpi/: built by GitHub Actions from `data/drives.toml` + `data/` |
| Analyzer app (public, view-only) | https://carpi-analyzer.onrender.com/: Render free tier, deployed from `carpi-live` |
| Claude doc: Documentation + Drive report | https://claude.ai/code/artifact/0e1c4734-8122-4ed4-9464-7b7bd5a47238 |
| Claude doc: Analyzer User Guide | https://claude.ai/code/artifact/10ef509b-ce76-4e50-be49-13c121c7754b |

Drives logged after Oct 1 exist only on the Pi until they're copied over or uploads are set up (§9, task 1).

## 5. Car and hardware

- **Car:** 2017 Audi A3 8V. EA888 Gen 3 (CNTC), IS20 turbo, Simos18 ECU, DQ250 DSG, Haldex Gen 5, about 68k miles, Northeast Ohio.
- **Pi:** Raspberry Pi 4 Model B Rev 1.5, 2 GB, Raspberry Pi OS 13 (trixie), headless. Hostname and user are both `carpi`.
- **CAN:**
  - RS485 CAN HAT: MCP2515 + SIT65HVD230, 12 MHz crystal, IRQ on GPIO 25, SPI0 CS0.
  - OBD-II pigtail: pin 6 to CAN-H, pin 14 to CAN-L.
  - Bus: 500 kbps, 11-bit IDs (ISO 15765-4). Verified error-active with zero errors.
- **Power:** the car's USB port. **It cuts power the instant the ignition turns off**, so every drive ends in a hard power cut. Until clean-shutdown hardware exists, the software works around it (§7).
- **The gateway matters.** The J533 gateway keeps powertrain traffic off the OBD port. Passive `candump` sees only the gateway keep-alive `0x17F00010#2010000000000000` at about 2 Hz. That's why CarPi polls.
- **Addressing:**
  - The engine answers on `0x7E8` to physical requests on `0x7E0`. Functional `0x7DF` is the fallback.
  - The TCU answers on `0x7E9`.
  - Nothing answers on 29-bit IDs.
- **Open question:** which microSD card is installed (Inland or Samsung Pro Endurance).

## 6. Pi setup (current)

- **`/boot/firmware/config.txt`:**
  ```ini
  dtparam=spi=on
  dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25,spimaxfrequency=2000000
  ```
- **Services:** all three are in `systemd/` in the repo and installed to `/etc/systemd/system/`.
  - `can0.service` (oneshot) runs `ip link set can0 up type can bitrate 500000 restart-ms 100`.
  - `carpi-logger.service` runs as `User=carpi` with `WorkingDirectory=/home/carpi/carpi`, `After`/`Requires` `can0.service`, and `Restart=always`. `RuntimeDirectory=carpi` creates `/run/carpi` for `status.json` and `live.json`.
  - `carpi-ble.service` runs `carpi_ble.py` (Bluetooth LE for the phone page) with `CAP_NET_ADMIN` and `CAP_SYS_TIME`, after `rfkill unblock bluetooth`.
  - `carpi-clock-save.timer` (from `pi-setup/`) touches `/var/lib/systemd/timesync/clock` every minute, so a power cut restarts the clock from that minute.
  - Not installed yet: `carpi-sync.service` (oneshot, `Nice=19`, idle I/O) run by `carpi-sync.timer` (60 s after boot, then every 2 min) and `carpi-sync.path` (Upload now: `/run/carpi/sync-now`). `carpi-ble.service` now also has `RuntimeDirectory=carpi`. Setup: `docs/data-sync.md` step 6.
- **Settings permissions:** `/etc/polkit-1/rules.d/50-carpi.rules` (from `pi-setup/install.sh`) lets user `carpi` manage Wi-Fi, shut down/restart and set the time zone without a password, nothing else. Without it the Settings tab shows the install command.
- **Bluetooth:** `ControllerMode = le` in `/etc/bluetooth/main.conf` (LE only; with classic on, Android pairing breaks). Phones pair once through `sudo python3 ~/carpi/carpi_ble.py --pair`. Details: `docs/phone-config.md`.
- **Packages:** `can-utils`, `python3-can`; for Bluetooth `python3-dbus`, `python3-gi`. For running `analyze_drives.py` on the Pi: `python3-pandas`, `python3-matplotlib`.
- **Updating the Pi:** in an SSH session, `cd ~/carpi && git pull && sudo systemctl restart carpi-logger carpi-ble`. If anything under `systemd/` changed, copy those files to `/etc/systemd/system/` and run `sudo systemctl daemon-reload` first.
- **Wi-Fi (NetworkManager):** Yury's home Wi-Fi at priority 10, then the phone hotspot at priority 5 with `connection.autoconnect-retries 0`, so the Pi keeps retrying the hotspot in the car. The real network names stay out of git: the Settings tab on the phone page shows them, and manages them.
- **SSH:** password login, enabled. SSH key login is still a to-do.
- **Layout of `~/carpi` on the Pi:** a git checkout of the repo. It also holds git-ignored files: `tag.txt`, `logs/`, `probes/`, `candump/`, and the phone's preset state (`active_preset.json`, `presets/`, `supported.json`) and upload switches (`upload.json`). Once uploads are set up, `logs/` is itself a git checkout of `carpi-data`. A leftover `carpi-logger.service` from September may still sit in `~/carpi` itself; the real one is `systemd/carpi-logger.service`, so it can be deleted. Full list of runtime files: `docs/how-it-works.md`.
- **Recovery history:** Yury once lost the Pi login. `CarPi_Documents/4_Pi_Scripts/carpi-rescue.sh` is a one-shot script run from the boot partition through `systemd.run=` in `cmdline.txt`. It copies home folders out, enables SSH and resets the password from `newpass.txt`. It isn't in the repo, on purpose.

## 7. Software map (repo)

| Path | What it is | Run / test |
| --- | --- | --- |
| `carpi_logger.py` | The logger on the Pi. Depends only on `python-can`. Writes `/run/carpi/status.json` and `live.json` for the phone. `Engine.read_did()` does UDS `0x22`. | systemd. Tests: `python -m pytest tests -q` (simulated ECU on a `python-can` virtual bus). |
| `carpi_presets.py` | The catalog of values a preset may pick (`CATALOG`), built-in presets, the weight scheduler (`Schedule`), and the preset/tag store under `~/carpi`. | `tests/test_presets.py` |
| `carpi_ble.py`, `carpi_control.py`, `carpi_mgmt.py`, `carpi_system.py` | Bluetooth LE service for the phone page. `carpi_control.py` is the protocol (JSON lines; commands documented at its top; `PROTOCOL = 6`: 3 settings, 4 time zone, 5 uploads, 6 analysis), `carpi_system.py` the Pi and CAN readings, `carpi_mgmt.py` the kernel advertising fallback. | `carpi-ble.service`. Tests in `tests/` |
| `carpi_net.py`, `carpi_admin.py` | The Settings tab's back end: Wi-Fi through NetworkManager (`nmcli` for reads/changes, D-Bus `AddConnection` so passwords never hit a command line), shut down/restart through logind, the clock from the phone (`CAP_SYS_TIME`), the time zone through timedated. Injectable backends. | `tests/test_net.py`, `tests/test_admin.py` |
| `carpi_sync.py` | Uploads finished drives: `~/carpi/logs` is a git checkout of the private `carpi-data`; each run repairs power-cut damage, checks GitHub answers, commits everything but the drive being written, pushes (rebasing if GitHub is ahead) and writes `/run/carpi/sync.json`. Own deploy key (`--keygen`) and pinned GitHub host key. `Uploads` is the phone's back end (`upload`, `upload_now`, `~/carpi/upload.json`). | `carpi-sync.timer`; by hand `python3 ~/carpi/carpi_sync.py --now`. Tests: `tests/test_sync.py` (a local bare repo stands in for GitHub) |
| `carpi_stats.py`, `carpi_drives.py` | The Analysis tab. `carpi_stats`: stdlib engine (rows in; summary, events, maps out; incremental; the same definitions as `analysis/carpi_analysis.py` and `carpi_app`, parity-tested). `carpi_drives`: drive ids (only names inside `~/carpi/logs`), the saved list, one worker thread at nice 10 that follows the drive being recorded and loads one saved drive. Writes nothing. | `tests/test_stats.py`, `tests/test_drives.py`; parity: `analysis/test_stats_parity.py` (CI), `carpi_app/tests/test_stats_parity.py` (local) |
| `pi-setup/` | One-time system setup for the Settings tab: polkit rule `50-carpi.rules` (8 Wi-Fi/power/time-zone actions for user `carpi`), the updated `carpi-ble.service`, a save-the-time-every-minute timer. `install.sh` ends with a `pkcheck` self-test; `uninstall.sh` reverts. | `sudo ~/carpi/pi-setup/install.sh` |
| `web/config/` | The phone page: Dashboard, Live (values twice a second + warm-up card), Analysis (drive picker, summary, map, events; `analysis.js`, with its demo), Logging (tag, presets), Settings (Wi-Fi, addresses, clock, time zone, uploads, power). Plain JS, offline copy via a service worker: bump `?v=` in `index.html` and `CACHE` in `sw.js` together. | `?demo`, `?demo=oil`, `?demo=unplugged`, `?debug` |
| `probe_addressing.py` | One-off probe: tests 11-bit and 29-bit addressing, maps supported PIDs, decodes live values. `--watch` prints at 1 Hz. Saves `~/carpi/probes/probe-*.json`. | By hand, logger stopped |
| `probe_oil.py` | One-time oil-temperature check: reads only UDS `22 20 2F` (allow-list), plus `01 00` and `01 05`. Saves `~/carpi/probes/oil-check-*.csv`. Refuses to run while the logger is active. | By hand, logger stopped. Tests: `tests/test_probe_oil.py` |
| `systemd/` | `can0.service`, `carpi-logger.service`, `carpi-ble.service`, `carpi-sync.service` + `.timer` + `.path` | |
| `tests/`, `.github/workflows/tests.yml` | Logger, presets, Bluetooth protocol, Pi readings, UDS. CI runs them on every push (Python 3.11). `tests/test_system.py` fails on Windows only (it creates a `soc:firmware` folder); it passes in CI. | `python -m pytest tests -q` |
| `analyze_drives.py` | Local batch reporter. Takes CSVs, folders, wildcards or a `.txt` list and writes `reports/`: an `index.html` (compare by tag, idle-trim history), one HTML + JSON per drive, and `drives.csv`. Incremental: skips drives already reported unless `--force`. Thresholds are in the `T` dict. | `python analyze_drives.py logs --open` |
| `analysis/carpi_analysis.py`, `analysis/build_site.py` | Analysis behind the GitHub Pages viewer. Builds `_site/` from `data/drives.toml`. Uses `elapsed_s` only. | `python -m pytest analysis -q`, `python analysis/build_site.py --out _site` |
| `web/` | Static viewer front end (HTML/CSS/JS) | Built into `_site/` by CI |
| `.github/workflows/pages.yml` | On push to `main`: Python 3.11, pytest, build, deploy Pages. A failure leaves the last good site live. | |
| `carpi_app/` | Dash analyzer: time series, X vs Y, compare drives, 3-axis map. Built from registries: `channels.py` (`@derived`), `filters.py` (`@row_filter`), `events.py` (`@detector`), `views/` (`@register`). The query box goes through `safe_query.py`, a whitelist parser. | `python -m carpi_app` (or `analyzer.bat`) opens `http://127.0.0.1:8050`. Tests: `python -m pytest carpi_app/tests -q` |
| `deploy/publish.py` | Bundles the app and every unique drive into `deploy/build/site/` and pushes it to `carpi-live`, which Render redeploys (3–5 min). `--exclude` / `--include` filter drives; `--logs DIR` reads drives from DIR; drives with `"publish": false` are left out. | `python deploy/publish.py build` (local only) / `render` (pushes: ask first) |
| `deploy/check_drives.py`, `deploy/data-repo/` | The safety check the `carpi-data` Action runs before publishing (only drive files, sane sizes, known columns and metadata keys), and the master copy of `carpi-data`'s README, `.gitignore` and `publish.yml`. | `python deploy/check_drives.py <folder>`. Tests: `tests/test_check_drives.py` |
| `data/` | Drives published on the viewer, plus `drives.toml` (titles, groups, findings) | |

**Logger internals worth knowing:**

- **States:** Waiting, then Detecting (asks every 3 s, 40 tries per wake), then Logging, then Ended (no reply for `--end-after` 8 s).
- **Polling:** the active preset (`~/carpi/active_preset.json`, set from the phone; none = built-in default) gives each value a weight. Values at the top weight are read on every row, half the top weight every other row, and so on. The default reproduces the original 12 every-row + 12 rotating values. Values not read on a row are blank, so use `ffill`. `--hz 10` caps the rate; the car gives about 7.7 rows/s with the default. A preset change mid-drive closes the file (`ended_by: preset_change`) and starts a new one (`follows`).
- **Boost:** PID `0x70` needs multi-frame ISO-TP. `Engine._receive()` sends flow control `30 00 05` and reassembles the reply, for both `request()` (Mode 01) and `read_did()` (UDS `0x22`, which also waits through "response pending" `7F 22 78`).
- **For the phone:** `status.json` every 2 s (state, file, rows, rates) and `live.json` every 0.5 s while logging (latest value + age per column), both in `/run/carpi` (RAM).
- **Power-cut safety:**
  - CSV `fsync` every 5 s.
  - The JSON metadata is rewritten atomically (tmp file + `os.replace`) every 30 s, with `complete: false` until a clean end.
  - Drives are stored in per-day folders. If a name is taken, a `-2`, `-3`… suffix is added, and files open with mode `"x"` so they're never overwritten.
- **Clock:** the Pi has no RTC. `clock_ok` = 1 once `/run/systemd/timesync/synchronized` exists. Until then wall time is wrong and can jump minutes forward. Trust `elapsed_s` and `run_time_s`.
- **Tags:** one word in `~/carpi/tag.txt` (e.g. `baseline`, `post-pcv`, `stage1`) is copied into each drive's JSON.

## 8. Data notes and findings

- **Columns and decoding:** `docs/data-format.md` (also in the Claude doc).
  - Always blank on this ECU: `boost_cmd_kpa` and `o2s2_trim_pct`.
  - Boost in psi = (`boost_act_kpa` − `baro_kpa`) / 6.895.
- **Car quirks:**
  - The pedal reads about 14.5% at rest and about 60% at the floor, so "hard throttle" means pedal ≥ 45% and throttle ≥ 75%.
  - Loaded DSG upshifts cut timing to about −25° and spike λ for about 0.3 s. `carpi_app` filters these out with `shift_mask()`.
  - Smart charging: 12.5–13 V while cruising can be normal.
- **Validated decoding** (Sept 25 baseline): idle boost = baro (98 kPa); λ 0.998 vs 1.000 commanded; rail pressure 57 bar idle, 132 cruise, 203 max; 799 rpm warm idle; 14.0 V charging.
- **Main finding:** total fuel trim +8.6% at idle vs about +0.8% at cruise on the baseline drive, and about +7.5% at idle across all 28 drives. That's the unmetered-air pattern; the EA888 PCV diaphragm is the prime suspect. Yury plans to replace the PCV, log with tag `post-pcv`, then compare. Use the analyzer's Compare drives tab (histogram per tag) or `analyze_drives.py`'s compare-by-tag table.
- **First hard pull (Sept 26):** 21.0 psi at 3,276 rpm with fueling on target, but timing fell to −7° at the peak. OBD can't separate planned timing from knock retard. Per-cylinder knock data (§9, task 5) will.
- **Rich blips after decel fuel cut** are catalyst refills, not faults.

## 9. Backlog (in rough priority order)

1. **Get every drive onto the PC.** In PowerShell on the PC, with the Pi on home Wi-Fi:
   ```powershell
   cd $HOME\Documents\Software\carpi
   scp -r "carpi@carpi.local:~/carpi/logs" .
   ```
   Then run `python -m carpi_app` and `python analyze_drives.py logs`. The 28 drives up to Oct 1 are also in `C:\Users\Yury\source\CarPi\logs\logs\`. Fallback: clone `YurySergeev/carpi-live` and use its `drives/`. Once uploads are set up, `gh repo clone YurySergeev/carpi-data logs\carpi-data` (then `git -C logs\carpi-data pull`) replaces `scp` (`docs/data-sync.md` step 8).
2. **Oil temperature.** Spec: `notes\specs\2026-10-30-live-tab-design.md` (git-ignored, in the working clone).
   - Phase 1, Yury's job: on a cold start, with the logger stopped, run `python3 ~/carpi/probe_oil.py --watch` and note the dash's oil temperature against the elapsed seconds at 3+ points (cold, ~60 °C, warm). Not while driving. It saves `~/carpi/probes/oil-check-*.csv`.
   - Phase 2, once one conversion tracks the dash at 3+ points: add `0x202F` to the logger as a catalog value (`oil_c`, "Temperatures" group), decode it in `carpi_logger.py`, add it to the default preset at the slow weight, test against the fake ECU. The phone page already handles `oil_c` (the warm-up card switches to oil by itself; `?demo=oil` shows how).
   - If the engine refuses `0x202F`, stop: the identifier needs confirming with VCDS (task 5).
3. **Post-PCV comparison.** This waits on Yury doing the repair.
   - Before the repair, on the Pi: `echo post-pcv > ~/carpi/tag.txt`.
   - After a few drives, compare idle trim by tag. Target: idle total trim drops toward about 0–3%.
   - Then add the result to `data/drives.toml` (the `upcoming = "After PCV repair"` placeholder is waiting), and to the README and baseline doc.
4. **Clean shutdown at ignition-off** (being designed Oct 30). Every drive ends in a hard power cut (SD-card risk); the Pi also boots after the engine starts, so the first 20–36 s of each drive are missed; and without an RTC, dates are wrong whenever NTP hasn't synced. Options:
   - a UPS/supercap HAT with a shutdown signal
   - ignition-sense (switched 12 V through an optocoupler to a GPIO), plus a small systemd service that calls `shutdown -h`
   - a 12 V to 5 V converter from OBD pin 16 (always on) with a timed shutdown, using the CAN bus going quiet as the "car is off" signal
   - plus a DS3231 RTC on I²C for correct dates

   Hardware choice is Yury's. The software side is small.
5. **VW-specific data (read-only UDS `0x22`).** `Engine.read_did()` exists and is tested. Candidate DIDs are in `CarPi_Documents/5_Notes/VW-specific data research.md`: per-cylinder knock `0x200A`–`0x200D`, oil temp `0x202F`, and others. **None are confirmed on this car yet.**
   - Plan: Yury gets an OBD Y-splitter and sniffs a VCDS session to confirm IDs and scaling. (Oil temperature is being confirmed against the dash instead, task 2.)
   - Then add a small, fixed list of confirmed DIDs to the catalog (new columns, the same power-cut safety), plus tests on recorded frames.
   - Oil pressure: there's no sensor (switches only), so skip it.
   - TCU data (`0x7E9`) is a later step, same approach.
6. **Housekeeping.**
   - Decide whether `analyze_drives.py` and `analysis/carpi_analysis.py` should share code.
   - Update the Claude Documentation doc's "Software" section. It predates the analyzer app, the viewer, the day-folder logger, presets and the phone page.
7. **Full-throttle baseline pulls** before any tune: 2–3 third-gear pulls from about 2,000 rpm, somewhere safe and legal. This is Yury's job; check the data afterwards.
8. **Later:**
   - alerts on the Live tab (coolant hot, low charging voltage, later knock)
   - SSH key login from the PC and phone
   - LinkedIn post and Projects entry (Yury wants these)
   - a resume bullet refresh

## 10. Gotchas from past sessions

- **Paths on the Pi:** the service expects `/home/carpi/carpi/carpi_logger.py`. A script saved to `~/` instead caused `status=2` crash loops.
- **`scp` runs on the PC, not inside SSH.** Copying a file to the Pi from PowerShell looks like this: `scp .\file.py carpi@carpi.local:~/carpi/`. After replacing the logger, run `sudo systemctl restart carpi-logger` on the Pi.
- **Python versions:** CI runs 3.11; the Pi has trixie's Python 3.13. Keep code 3.11-safe. In particular, **no backslashes inside f-string expressions** (`analyze_drives.py` uses `DEG`/`DEG_C` constants for that).
- **Working through the Cowork device bridge on the PC:**
  - The repo is in OneDrive, and file deletion is blocked by default.
  - A plain `git status` there once left a stale `.git/index.lock` that couldn't be removed. Use `git --no-optional-locks status` for read-only git commands from the bridge.
  - The old lock is parked at `.git/_to_delete/index.lock.stale`. Yury can delete it.
  - Files have CRLF line endings when read from the bridge (`.gitattributes` sets `eol=lf` in the repo), so strip `\r` before diffing.
- **Working in Claude Code on `C:\Users\Yury`:**
  - Git there has `core.autocrlf=true`, but this repo's files check out with LF (`.gitattributes`). Shell heredocs mangle backslashes in Python one-liners (`\n`, `\0`): use the Edit tool for code with escapes, and check for stray NUL bytes afterwards.
  - The phone page can be previewed with `python -m http.server 8002 --directory web`, then `http://localhost:8002/config/?demo`.
  - `tests/test_system.py` fails on Windows only (`soc:firmware` isn't a valid folder name there). Everything else passes locally.
- **Phone page cache:** the page is cached offline by a service worker. Any change to `web/config/` needs the `?v=` in `index.html` and `CACHE` in `sw.js` bumped together, or phones keep the old copy.
- **Hotspot reconnects:** the Pi sometimes doesn't rejoin the phone hotspot right after leaving home Wi-Fi. Fix: `autoconnect-retries 0` (done), plus Maximize Compatibility (iPhone) or 2.4 GHz with WPA2 (Android) on the phone.
- **Undervoltage check after a drive:** run `vcgencmd get_throttled` on the Pi. `0x0` means no undervoltage was detected.
- **Render free tier** sleeps after 15 minutes, so the first load takes about a minute. Hugging Face Docker Spaces need PRO since July 2026, which is why the app moved to Render.

## 11. Command cheat sheet

**On the Pi (SSH session):**

```bash
systemctl status carpi-logger carpi-ble       # running?
journalctl -u carpi-logger -f                 # live log: "drive started", status every 15 s
journalctl -u carpi-ble -n 20                 # Bluetooth: "GATT service registered", "advertising as CarPi"
sudo systemctl stop carpi-logger              # before probing; start again after
python3 ~/carpi/probe_addressing.py --watch   # live values, 1 Hz
python3 ~/carpi/probe_oil.py --watch          # one-time oil-temperature check (logger stopped)
sudo python3 ~/carpi/carpi_ble.py --pair      # 120 s window to pair a new phone
sudo ~/carpi/pi-setup/install.sh              # Settings tab permissions (once, and after pulls that change pi-setup/)
echo post-pcv > ~/carpi/tag.txt               # tag upcoming drives (or from the phone page)
ls -lh ~/carpi/logs/*/                        # list drives
ip -details -statistics link show can0        # want ERROR-ACTIVE, 0 errors
nmcli -t -f NAME,DEVICE connection show --active
cd ~/carpi && git pull && sudo systemctl restart carpi-logger carpi-ble   # update code
python3 ~/carpi/carpi_sync.py --now           # one upload with output (once uploads are set up)
journalctl -u carpi-sync -n 30                # upload log
```

**On the PC (PowerShell, in the repo folder):**

```powershell
scp -r "carpi@carpi.local:~/carpi/logs" .     # pull every drive (Pi on home Wi-Fi), until uploads are set up
git -C logs\carpi-data pull                   # after: new drives from carpi-data
pip install -r carpi_app/requirements.txt pandas matplotlib
python -m carpi_app                           # analyzer at http://127.0.0.1:8050
python analyze_drives.py logs --open          # HTML reports in .\reports
python -m pytest tests -q                     # logger, presets, Bluetooth, UDS (CI: tests.yml)
python -m pytest analysis -q                  # viewer analysis tests (CI: pages.yml)
python -m pytest carpi_app/tests -q           # analyzer app tests
python deploy/publish.py build                # local build of the hosted app (render = push; ask first)
```

## 12. Open questions for Yury

- Has the PCV been replaced yet, and is `tag.txt` set to `post-pcv`?
- Has the one-time oil check (`probe_oil.py`) been run? If so, where's the CSV and the dash notes?
- Which SD card is in the Pi?
- Is there a Y-splitter and VCDS access for confirming VW DIDs?
- Which clean-shutdown option is Yury buying or building? (Being designed Oct 30.)
- Should any of `CarPi_Documents/` (the Claude doc PDFs, the notes) go into the public repo's `docs/`? Right now it's deliberately git-ignored.
