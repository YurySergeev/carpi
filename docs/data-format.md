# Data format

Each drive produces two files in a folder for its day, named after the drive's start time: `~/carpi/logs/2026-09-26/drive-231349.csv` and `.json`. The date and time come from the Pi's clock, which has no battery-backed RTC, so they can be off until the Pi syncs over the internet (`clock_ok`).

## `drive-HHMMSS.csv`

One row per poll loop, about 7.7 rows/s on this car. Fast columns appear in every row. Slow columns rotate, so each one refreshes about every 1.6 s and is blank on the rows in between (`df.ffill()` fills them).

| Column | PID | Decoding (A, B, … = data bytes) | Unit | Rate |
| --- | --- | --- | --- | --- |
| `time` | — | Pi wall clock | ISO 8601 | every row |
| `elapsed_s` | — | monotonic seconds since the drive started | s | every row |
| `clock_ok` | — | 1 once NTP has synced the clock | 0/1 | every row |
| `rpm` | 0x0C | (256A+B)/4 | rpm | fast |
| `map_kpa` | 0x0B | A | kPa abs | fast |
| `boost_act_kpa` | 0x70 | (256D+E)/32, multi-frame | kPa abs | fast |
| `boost_cmd_kpa` | 0x70 | (256B+C)/32 | kPa abs | fast (not reported by this ECU) |
| `load_pct` | 0x04 | A·100/255 | % | fast |
| `timing_deg` | 0x0E | A/2 − 64 | ° BTDC | fast |
| `stft_pct` | 0x06 | (A−128)·100/128 | % | fast |
| `lambda` | 0x34 | (256A+B)·2/65536 | λ | fast |
| `o2s1_ma` | 0x34 | (256C+D)/256 − 128 | mA | fast |
| `lambda_cmd` | 0x44 | (256A+B)·2/65536 | λ | fast |
| `rail_kpa` | 0x23 | (256A+B)·10 | kPa gauge | fast |
| `pedal_pct` | 0x49 | A·100/255 | % | fast |
| `throttle_pct` | 0x11 | A·100/255 | % | fast |
| `speed_kph` | 0x0D | A | km/h | fast |
| `coolant_c` | 0x05 | A − 40 | °C | slow |
| `iat_c` | 0x0F | A − 40 | °C | slow |
| `ltft_pct` | 0x07 | (A−128)·100/128 | % | slow |
| `volts` | 0x42 | (256A+B)/1000 | V | slow |
| `baro_kpa` | 0x33 | A | kPa | slow |
| `ambient_c` | 0x46 | A − 40 | °C | slow |
| `fuel_pct` | 0x2F | A·100/255 | % | slow |
| `cat_temp_c` | 0x3C | (256A+B)/10 − 40 | °C | slow |
| `o2s2_v` | 0x15 | A/200 | V | slow |
| `o2s2_trim_pct` | 0x15 | (B−128)·100/128 | % | slow (not used on this car) |
| `lt_o2s2_trim_pct` | 0x56 | (A−128)·100/128 | % | slow |
| `abs_load_pct` | 0x43 | (256A+B)·100/255 | % | slow |
| `run_time_s` | 0x1F | 256A+B | s | slow |

The Pi has no real-time clock, so `time` can be wrong until NTP syncs. Use `elapsed_s` for analysis and `clock_ok` to know when `time` is trustworthy.

## `drive-HHMMSS.json`

Written when the drive starts: `start`, `tag`, `request_id`, `supported_pids`, `fast_pids`, `slow_pids`.
Refreshed every 30 s: `rows`, `duration_s`, `complete`. `"complete": false` means power was cut before the drive ended cleanly.

## Tags

Put one word in `~/carpi/tag.txt` (for example `baseline`, `post-pcv`, `stage1`). Every drive after that carries it in its metadata, which is how before/after groups get compared.

## Handy conversions

- Boost in psi = (`boost_act_kpa` − `baro_kpa`) / 6.895
- °F = °C × 1.8 + 32
- mph = km/h × 0.621

```python
import pandas as pd

df = pd.read_csv("data/drive-2026-09-25_203526.csv").ffill()  # the sample drive in this repo
df["boost_psi"] = (df["boost_act_kpa"] - df["baro_kpa"]) / 6.895
df["fuel_trim_pct"] = df["stft_pct"] + df["ltft_pct"]
```
