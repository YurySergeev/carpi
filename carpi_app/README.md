# CarPi Analyzer

A local app for exploring drive logs interactively: stacked time series with a shared crosshair,
X-vs-Y scatter, multi-drive comparison, and ECU-style 3-axis maps. It runs on your machine and
reads the CSVs straight from `logs/` and `data/`. Nothing is uploaded.

## Run it

```
pip install -r carpi_app/requirements.txt
python -m carpi_app            # or double-click analyzer.bat
```

It scans `logs/` and `data/` (including subfolders), then opens http://127.0.0.1:8050.
Options: `--logs D:\some\folder` (repeatable), `--port 8060`, `--no-browser`, `--debug` (reloads when you edit code).
After copying new logs over from the Pi, click **Rescan log folders**. Only new or changed files get parsed.
Identical copies of a file in two folders are only listed once.

## The sidebar (applies to every tab)

- **Drives**: pick any number. `?` after a start time means the Pi clock never synced on that drive, so the time is a guess.
- **Filters**: tick to keep only matching rows (they combine with AND). Examples: Warm + Closed loop + Idle for idle trims, or Under boost + Exclude DSG shifts for timing/lambda under load.
- **Query**: any pandas condition on any column, applied on Enter. Examples: `rpm > 3000 and coolant_c >= 80`, `abs(lambda_err) > 0.05`, `iat_c.between(30, 45)`.

## Tabs

| Tab | What it's for |
|---|---|
| **Time series** | One drive, any channels stacked with one shared zoom (scroll, drag or double-click) and a crosshair across all panels. Related channels share a panel (λ act/cmd, trims, temps…). The **Events** table lists detected moments (hard accelerations, timing dips under boost, lean under boost, low voltage, high trims, long idles, peak boost, DSG shifts). Click a row to zoom to it. |
| **X vs Y** | Any channel against any other across all selected drives, coloured by drive, tag or a third channel. Turn on *Density* for big selections and *Binned median line* to compare trends. **Click a point to jump to that moment in the time series.** |
| **Compare drives** | *Trend per drive* (e.g. median idle trim per drive over time, coloured by tag), *Distribution per drive* (box plots), *Histogram per tag* (baseline vs post-repair), *Overlay traces*. |
| **3-axis map** | Bins X and Y like an ECU table (e.g. rpm × load) and shows mean, median, max, p95 or count of Z in each cell, as a table or a 3D surface. Pick **Tag A and Tag B** to get an A − B difference map (e.g. timing after the tune minus baseline). |

## Extending it

Everything is a small registry, so new analyses never touch the core:

| Add… | Where | How |
|---|---|---|
| A derived channel | `channels.py` | function + `@derived("name", "Label", "unit")`. It appears in every dropdown. |
| A sidebar filter | `filters.py` | function returning a True/False Series + `@row_filter("key", "Label")` |
| An event detector | `events.py` | function returning `Event`s + `@detector("kind", "Label")`. Use `spans()` to turn a mask into events. |
| A whole new tab | `views/yourview.py` | subclass `View`, decorate with `@register`, add the module name to the list at the bottom of `views/__init__.py`. `views/map3d.py` is a complete example. |

Tabs get data through `store.frames(drive_ids, filter_keys, query, columns=[...])`. That returns every selected
drive stacked into one DataFrame with `drive`, `tag` and `elapsed_s` columns, already filtered. To make clicks in
your tab open the time series at that moment, write `{"drive": id, "t0": seconds, "t1": seconds}` to the
`JUMP` store (see `views/scatter.py`).

Tests: `python -m pytest carpi_app/tests -q`

## Notes specific to this car

- The pedal PID reads about 14.5% at rest and only about 60% at the floor, so "hard throttle" means pedal ≥ 45% and throttle ≥ 75%.
  The values are `HARD_PEDAL` / `HARD_THROTTLE` in `events.py`.
- DSG upshifts under load cut timing to about −25° and spike lambda for ~0.3 s. `shift_mask()` finds these moments,
  the timing-dip and lean-under-boost detectors ignore them, and the *Exclude DSG shift moments* filter removes them from charts.
- Charging voltage on this car is managed by the car itself ("smart charging"), so 12.5–13 V while cruising
  can be normal. The low-voltage event only fires after more than 60 s below 12.4 V.

## Putting it online (Render, free)

`deploy/publish.py` bundles the app and every unique drive into `deploy/build/site/` (Dockerfile + gunicorn,
read-only "public" mode) and pushes it to a separate GitHub repo that Render deploys from.

One-time setup:
1. Create an **empty** GitHub repo (no README), e.g. `carpi-live`.
2. `python deploy/publish.py render`, then paste the repo URL when asked. It's remembered in `deploy/build/.remote`.
3. On render.com (sign in with GitHub): **New > Blueprint**, pick the repo, **Apply**. `render.yaml` sets it up as a
   free Docker web service in Ohio. If Render asks for a card for the Blueprint, use
   **New > Web Service** instead: pick the repo, keep the Docker runtime and choose the **Free** instance type.

After that, `python deploy/publish.py render` after every batch of new logs. Render redeploys on its own
(3–5 min). Leave drives out with `--exclude "*2026-09-25*"`. `python deploy/publish.py build` builds without pushing.

How it stays usable on Render's free box (0.1 CPU, 512 MB, sleeps after 15 min idle):
- CSVs are parsed once during the Docker build (`python -m carpi_app.warm`), so startup only loads ready-made frames.
- Only the visible tab recomputes, and coming back to a tab with unchanged inputs costs nothing (`lazy_callback`).
- Figures send compact numeric arrays: box plots and histograms are computed on the server, and scatter is capped at 60k points.
- Dash/Plotly JavaScript comes from a CDN, not the tiny server.

I tested this at 0.1 CPU and 512 MB with all 28 drives: every interaction finished in about 3 s or less, with peak memory around 220 MB.
A first visit after the service has been asleep takes about a minute while Render wakes it.

Hugging Face also works (`python deploy/publish.py hf --repo NAME/carpi-analyzer`), but Docker Spaces need PRO since July 2026.

Public mode (`CARPI_PUBLIC=1`) hides the rescan button and local paths. The Query box always uses
`safe_query.py`, a whitelist parser (columns, numbers, comparisons, and/or/not, arithmetic, `abs`,
`.between`, `.isna`), so nothing typed there can run code. Locally `python -m carpi_app` works as before.
