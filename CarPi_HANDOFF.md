# CarPi: session handoff

**Written:** Oct 6, 2026, at the end of a long Cowork session.
**For:** Claude in a new Cowork or Claude Code session picking up CarPi.
**Owner:** Yury, who is also your user.

Read sections 1–3 before doing anything. The rest is reference.

---

## 1. TL;DR

- **What it is.** CarPi is a Raspberry Pi 4 on the OBD-II port of Yury's 2017 Audi A3. It polls the engine ECU over CAN about 7.7 times a second and writes one CSV per drive. Two tools sit on top of the logs: a static drive viewer on GitHub Pages, and a Dash analyzer app on Render. It's a portfolio project and a before/after tool for planned car work.
- **Where the code is.** On GitHub, `YurySergeev/carpi` (`main` = `edd66c1`). The local clone is at `C:\Users\yumse\OneDrive\Documents\carpi` on the PC (Windows, device name `yura-pc`). On Oct 6 that clone matched GitHub exactly and had a clean working tree.
- **Status.**
  - Logging works end to end.
  - The 28 baseline drives (Sept 25 – Oct 1) show one finding: idle fuel trim is about +7.5–8.6% while cruise is about 0%. That's a vacuum leak, most likely the PCV.
  - The viewer and analyzer are live.
  - Nothing has been logged after the repair yet.
- **Most likely next tasks.**
  1. Get all the drives from the Pi onto the PC.
  2. Do the post-PCV comparison once Yury has done the repair.
  3. Clean-shutdown hardware.
  4. Read-only VW-specific data (per-cylinder knock, oil temp).
  See §9 for the full backlog.

## 2. Hard rules

1. **Read-only on the car, always.**
   - Allowed: OBD-II Mode 01 and Mode 06 requests, and UDS `0x22` ReadDataByIdentifier for specific, known DIDs.
   - Never send anything that writes or changes state: coding, adaptations, routine control, security access, session changes for flashing, or flashing itself. That applies to the ECU, the TCU (DSG) and every other module.
   - Tuning is out of scope. Yury knows it's high-risk; CarPi only measures.
2. **Don't write sweeping probes.** No scripts that brute-force or enumerate identifiers, services or modules. Add VW identifiers one at a time, from a list confirmed with VCDS (see §9, task 4).
3. **Never transmit while the car's bus is asleep.** The logger only polls after it sees bus traffic, and gives up after 40 tries (~2 min) per wake-up. That keeps it from holding a parked car awake and draining the battery. Keep that behavior in any change.
4. **One talker at a time.** Stop `carpi-logger` before running `probe_addressing.py` or any other tool on `can0`, or the two will read each other's replies.
5. **Ask before anything public or remote.** That means `git push`, `deploy/publish.py render`, Render or Pages deploys, and posting anywhere. Local commits are fine once Yury agrees to the change.
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
| Local clone (PC) | `C:\Users\yumse\OneDrive\Documents\carpi`, synced by OneDrive |
| Compiled docs (PC, git-ignored) | `carpi\CarPi_Documents\`: start with its `README.md`. Holds PDFs of the Claude docs, reports, the Sept 25–26 logs, scripts, notes, and this file. |
| The Pi | `ssh carpi@carpi.local`, code in `~/carpi/`, logs in `~/carpi/logs/<YYYY-MM-DD>/drive-HHMMSS.csv` + `.json` |
| All drives (complete set) | On the Pi (`~/carpi/logs/`). Also in the Render source repo `YurySergeev/carpi-live` under `drives/` (everything published so far). |
| Drive viewer | https://yurysergeev.github.io/carpi/: built by GitHub Actions from `data/drives.toml` + `data/` |
| Analyzer app (public, view-only) | https://carpi-analyzer.onrender.com/: Render free tier, deployed from `carpi-live` |
| Claude doc: Documentation + Drive report | https://claude.ai/code/artifact/0e1c4734-8122-4ed4-9464-7b7bd5a47238 |
| Claude doc: Analyzer User Guide | https://claude.ai/code/artifact/10ef509b-ce76-4e50-be49-13c121c7754b |

The PC clone was re-cloned on Oct 6, so it has **no `logs/` folder**. The earlier working copy with all 28 drives isn't on this PC.

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
- **Services:** both are in `systemd/` in the repo and installed to `/etc/systemd/system/`.
  - `can0.service` (oneshot) runs `ip link set can0 up type can bitrate 500000 restart-ms 100`.
  - `carpi-logger.service` runs as `User=carpi` with `WorkingDirectory=/home/carpi/carpi`, `After`/`Requires` `can0.service`, and `Restart=always`.
- **Packages:** `can-utils`, `python3-can`. For running `analyze_drives.py` on the Pi: `python3-pandas`, `python3-matplotlib`.
- **Wi-Fi (NetworkManager):** home network `home-att` at priority 10. Phone hotspot `MyNet` at priority 5 with `connection.autoconnect-retries 0`, so the Pi keeps retrying the hotspot in the car.
- **SSH:** password login, enabled. SSH key login is still a to-do.
- **Layout of `~/carpi` on the Pi:** a `git clone` of the repo (see the README). It also holds `tag.txt`, `logs/`, `probes/` and `candump/`, which are git-ignored.
- **Recovery history:** Yury once lost the Pi login. `CarPi_Documents/4_Pi_Scripts/carpi-rescue.sh` is a one-shot script run from the boot partition through `systemd.run=` in `cmdline.txt`. It copies home folders out, enables SSH and resets the password from `newpass.txt`. It isn't in the repo, on purpose.

## 7. Software map (repo)

| Path | What it is | Run / test |
| --- | --- | --- |
| `carpi_logger.py` | The logger on the Pi. Depends only on `python-can`. | systemd. Test with `--once`, or on a `python-can` virtual bus. |
| `probe_addressing.py` | One-off probe: tests 11-bit and 29-bit addressing, maps supported PIDs, decodes live values. `--watch` prints at 1 Hz. Saves `~/carpi/probes/probe-*.json`. | By hand, logger stopped |
| `systemd/` | `can0.service`, `carpi-logger.service` | |
| `analyze_drives.py` | Local batch reporter. Takes CSVs, folders, wildcards or a `.txt` list and writes `reports/`: an `index.html` (compare by tag, idle-trim history), one HTML + JSON per drive, and `drives.csv`. Incremental: skips drives already reported unless `--force`. Thresholds are in the `T` dict. | `python analyze_drives.py logs --open` |
| `analysis/carpi_analysis.py`, `analysis/build_site.py` | Analysis behind the GitHub Pages viewer. Builds `_site/` from `data/drives.toml`. Uses `elapsed_s` only. | `python -m pytest analysis -q`, `python analysis/build_site.py --out _site` |
| `web/` | Static viewer front end (HTML/CSS/JS) | Built into `_site/` by CI |
| `.github/workflows/pages.yml` | On push to `main`: Python 3.11, pytest, build, deploy Pages. A failure leaves the last good site live. | |
| `carpi_app/` | Dash analyzer: time series, X vs Y, compare drives, 3-axis map. Built from registries: `channels.py` (`@derived`), `filters.py` (`@row_filter`), `events.py` (`@detector`), `views/` (`@register`). The query box goes through `safe_query.py`, a whitelist parser. | `python -m carpi_app` (or `analyzer.bat`) opens `http://127.0.0.1:8050`. Tests: `python -m pytest carpi_app/tests -q` |
| `deploy/publish.py` | Bundles the app and every unique drive into `deploy/build/site/` and pushes it to `carpi-live`, which Render redeploys (3–5 min). `--exclude` / `--include` filter drives. | `python deploy/publish.py build` (local only) / `render` (pushes: ask first) |
| `data/` | Drives published on the viewer, plus `drives.toml` (titles, groups, findings) | |

**Logger internals worth knowing:**

- **States:** Waiting, then Detecting (asks every 3 s, 40 tries per wake), then Logging, then Ended (no reply for `--end-after` 8 s).
- **Polling:** `FAST` = 12 PIDs on every row. `SLOW` = 12 PIDs rotating one per row (each refreshes about every 1.6 s; blank in between, so use `ffill`). `--hz 10` caps the rate; the car gives about 7.7 rows/s.
- **Boost:** PID `0x70` needs multi-frame ISO-TP. `Engine.request()` sends flow control `30 00 05` and reassembles the reply.
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
- **First hard pull (Sept 26):** 21.0 psi at 3,276 rpm with fueling on target, but timing fell to −7° at the peak. OBD can't separate planned timing from knock retard. Per-cylinder knock data (task 4) will.
- **Rich blips after decel fuel cut** are catalyst refills, not faults.

## 9. Backlog (in rough priority order)

1. **Get every drive onto the PC.** In PowerShell on the PC, with the Pi on home Wi-Fi:
   ```powershell
   cd $HOME\OneDrive\Documents\carpi
   scp -r "carpi@carpi.local:~/carpi/logs" .
   ```
   Then run `python -m carpi_app` and `python analyze_drives.py logs`. Fallback: clone `YurySergeev/carpi-live` and use its `drives/`.
2. **Post-PCV comparison.** This waits on Yury doing the repair.
   - Before the repair, on the Pi: `echo post-pcv > ~/carpi/tag.txt`.
   - After a few drives, compare idle trim by tag. Target: idle total trim drops toward about 0–3%.
   - Then add the result to `data/drives.toml` (the `upcoming = "After PCV repair"` placeholder is waiting), and to the README and baseline doc.
3. **Clean shutdown at ignition-off.** Options:
   - a UPS/supercap HAT with a shutdown signal
   - ignition-sense (switched 12 V through an optocoupler to a GPIO), plus a small systemd service that calls `shutdown -h`
   - a 12 V to 5 V converter from OBD pin 16 (always on) with a timed shutdown

   Hardware choice is Yury's. The software side is small.
4. **VW-specific data (read-only UDS `0x22`).** Candidate DIDs are in `CarPi_Documents/5_Notes/VW-specific data research.md`: per-cylinder knock `0x200A`–`0x200D`, oil temp `0x202F`, and others. **None are confirmed on this car yet.**
   - Plan: Yury gets an OBD Y-splitter and sniffs a VCDS session to confirm IDs and scaling.
   - Then add a small, fixed list of confirmed DIDs as an extra slow-rotation group in the logger (new columns, the same power-cut safety), plus unit tests on recorded frames.
   - Oil pressure: there's no sensor (switches only), so skip it.
   - TCU data (`0x7E9`) is a later step, same approach.
5. **Logger tests in the repo.** Analysis and app have pytest; the logger doesn't. Earlier sessions tested it against a simulated ECU on a `python-can` virtual bus, covering:
   - bus sleep and wake
   - ignition on and off
   - the multi-frame `0x70` reply
   - SIGTERM mid-drive

   Those tests were never committed. Rebuild them under `tests/` and add them to CI.
6. **Housekeeping.**
   - The README roadmap still lists "Drive-summary script" as unchecked, but `analyze_drives.py` exists. It isn't mentioned in the README either.
   - Decide whether `analyze_drives.py` and `analysis/carpi_analysis.py` should share code.
   - Update the Claude Documentation doc's "Software" section. It predates the analyzer app, the viewer and the day-folder logger.
7. **Full-throttle baseline pulls** before any tune: 2–3 third-gear pulls from about 2,000 rpm, somewhere safe and legal. This is Yury's job; check the data afterwards.
8. **Later:**
   - SSH key login from the PC and phone
   - live dashboard (MQTT or FastAPI), stage 2 of the original plan
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
- **Hotspot reconnects:** the Pi sometimes doesn't rejoin the phone hotspot right after leaving home Wi-Fi. Fix: `autoconnect-retries 0` (done), plus Maximize Compatibility (iPhone) or 2.4 GHz with WPA2 (Android) on the phone.
- **Undervoltage check after a drive:** run `vcgencmd get_throttled` on the Pi. `0x0` means no undervoltage was detected.
- **Render free tier** sleeps after 15 minutes, so the first load takes about a minute. Hugging Face Docker Spaces need PRO since July 2026, which is why the app moved to Render.

## 11. Command cheat sheet

**On the Pi (SSH session):**

```bash
systemctl status carpi-logger                 # running?
journalctl -u carpi-logger -f                 # live log: "drive started", status every 15 s
sudo systemctl stop carpi-logger              # before probing; start again after
python3 ~/carpi/probe_addressing.py --watch   # live values, 1 Hz
echo post-pcv > ~/carpi/tag.txt               # tag upcoming drives
ls -lh ~/carpi/logs/*/                        # list drives
ip -details -statistics link show can0        # want ERROR-ACTIVE, 0 errors
nmcli -t -f NAME,DEVICE connection show --active
cd ~/carpi && git pull && sudo systemctl restart carpi-logger   # update code
```

**On the PC (PowerShell, in the repo folder):**

```powershell
scp -r "carpi@carpi.local:~/carpi/logs" .     # pull every drive (Pi on home Wi-Fi)
pip install -r carpi_app/requirements.txt pandas matplotlib
python -m carpi_app                           # analyzer at http://127.0.0.1:8050
python analyze_drives.py logs --open          # HTML reports in .\reports
python -m pytest analysis -q                  # viewer analysis tests (what CI runs)
python -m pytest carpi_app/tests -q           # analyzer app tests
python deploy/publish.py build                # local build of the hosted app (render = push; ask first)
```

## 12. Open questions for Yury

- Has the PCV been replaced yet, and is `tag.txt` set to `post-pcv`?
- Which SD card is in the Pi?
- Does he have the Y-splitter and VCDS access for confirming VW DIDs?
- Which clean-shutdown option does he want to buy or build?
- Should any of `CarPi_Documents/` (the Claude doc PDFs, the notes) go into the public repo's `docs/`? Right now it's deliberately git-ignored.
