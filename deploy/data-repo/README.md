# carpi-data

Drive logs from [CarPi](https://github.com/YurySergeev/carpi), the Raspberry Pi logger on my car's
OBD-II port. **Private.** The Pi writes here on its own; the public part is the
[CarPi Analyzer](https://carpi-analyzer.onrender.com), rebuilt from these files.

## How files get here

- The Pi uploads finished drives over any internet it's on (`carpi_sync.py`, a systemd timer every
  2 minutes, plus **Upload now** on the phone page). It pushes with its own deploy key, which can
  write to this repo only.
- Each push of a `.csv` or `.json` runs [Publish analyzer](.github/workflows/publish.yml): a safety
  check (`deploy/check_drives.py`: only drive files, sane sizes, numbers and metadata), then a
  rebuild of the analyzer, pushed to `carpi-live`, which Render deploys.
- If a run fails, the workflow opens an issue labelled `publish-failed` (or comments on the open
  one). The analyzer keeps the last good build.

The layout is the Pi's `~/carpi/logs`: `YYYY-MM-DD/drive-HHMMSS.csv` with a `.json` next to each
(see `docs/data-format.md` in the CarPi repo). Anything else makes the safety check fail, so don't
add other files.

## Getting the drives on a PC

```bash
git clone git@github.com:YurySergeev/carpi-data.git logs
```

then `git -C logs pull` for new ones.

## Keeping a drive off the analyzer

New drives: switch **Publish new drives** off on the phone page (Settings → Uploads). The drive being
recorded at that moment is included. Those drives are still backed up here, with `"publish": false`
in their `.json`.

A drive that's already public: edit its `.json` here on GitHub and add `"publish": false` (for
example after `"tag"`), then commit. The push reruns the workflow and the analyzer drops the drive
within a few minutes. The Pi picks up the edit on its next upload.

## This README and the workflow

The master copies live in the CarPi repo under `deploy/data-repo/`. Change them there, then copy
them here.
