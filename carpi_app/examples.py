"""Ready-made examples: one click (or a ?example=<id> link) sets the drives, filters and tab.

Add one by appending to EXAMPLES. Keys:
  id, title, blurb      shown on the Guide tab card (thumbnail: assets/examples/<id>.png, optional)
  tab                   ts | sc | cmp | map
  drives                "all", "newest", or a list of drive-id endings, e.g. ["drive-084835"]
  filters               sidebar filter keys (see filters.py)
  set                   {component id: value} for the tab's own controls
  jump                  ts only: {"drive": <id ending>, "t0": s, "t1": s} to zoom the time series
"""
EXAMPLES = [
    dict(id="idle-trim", title="Idle fuel trim, drive by drive",
         blurb="The vacuum-leak pattern: about +7.5% extra fuel at idle on almost every drive.",
         tab="cmp", drives="all", filters=["running", "warm", "closed_loop", "idle"],
         set={"cmp-ch": "trim_total", "cmp-mode": "trend", "cmp-stat": "median"}),
    dict(id="trim-vs-rpm", title="Fuel trim vs engine speed",
         blurb="Trim is high near idle and falls to about 0% above 1,500 rpm. That's unmetered air at low airflow.",
         tab="sc", drives="all", filters=["running", "warm", "closed_loop"],
         set={"sc-x": "rpm", "sc-y": "trim_total", "sc-color": "load_pct", "sc-mode": "points",
              "sc-trend": ["median"]}),
    dict(id="timing-boost", title="Ignition timing under boost",
         blurb="Every boosted sample, coloured by intake air temp, with DSG shift moments removed.",
         tab="sc", drives="all", filters=["running", "boost", "no_shift"],
         set={"sc-x": "rpm", "sc-y": "timing_deg", "sc-color": "iat_c", "sc-mode": "points",
              "sc-trend": ["median"]}),
    dict(id="timing-map", title="Timing map: rpm × load",
         blurb="The ECU's effective timing table as driven, warm engine only.",
         tab="map", drives="all", filters=["running", "warm"],
         set={"map-x": "rpm", "map-xstep": 500, "map-y": "load_pct", "map-ystep": 10, "map-z": "timing_deg",
              "map-agg": "mean", "map-min": 10, "map-view": "table"}),
    dict(id="timing-3d", title="Timing map in 3D",
         blurb="The same table as a surface. Drag to rotate, scroll to zoom.",
         tab="map", drives="all", filters=["running", "warm"],
         set={"map-x": "rpm", "map-xstep": 500, "map-y": "load_pct", "map-ystep": 10, "map-z": "timing_deg",
              "map-agg": "mean", "map-min": 10, "map-view": "surface"}),
    dict(id="boost-3d", title="Boost by rpm and throttle, 3D",
         blurb="Mean boost in each rpm × throttle cell: where the turbo actually builds pressure.",
         tab="map", drives="all", filters=["running", "warm"],
         set={"map-x": "rpm", "map-xstep": 500, "map-y": "throttle_pct", "map-ystep": 10, "map-z": "boost_psi",
              "map-agg": "mean", "map-min": 10, "map-view": "surface"}),
    dict(id="hard-pull", title="A pull to 6,300 rpm",
         blurb="Boost, timing and lambda through a hard pull and a DSG upshift (watch timing drop for the shift).",
         tab="ts", drives="newest", filters=["running"],
         set={"ts-channels": ["rpm", "boost_psi", "timing_deg", "lambda", "lambda_cmd", "throttle_pct"]},
         jump={"drive": "drive-084835", "t0": 1953, "t1": 1961}),
    dict(id="warmup", title="Cold-start warm-up curves",
         blurb="Coolant temperature from five cold starts on one time axis.",
         tab="cmp", filters=["running"],
         drives=["drive-2026-09-25_223818", "drive-222941", "drive-171409", "drive-084835", "drive-192645"],
         set={"cmp-ch": "coolant_c", "cmp-mode": "overlay"}),
    dict(id="volts", title="Charging voltage by drive",
         blurb="A box per drive. Smart charging lets cruise voltage sit low; the whiskers show the coasting spikes.",
         tab="cmp", drives="all", filters=["running"],
         set={"cmp-ch": "volts", "cmp-mode": "box"}),
]
BY_ID = {e["id"]: e for e in EXAMPLES}


def resolve_drives(store, spec):
    newest_first = [i.id for i in reversed(list(store.drives.values()))]
    if spec == "all":
        return newest_first
    if spec == "newest":
        return newest_first[:5]
    return [d for d in newest_first if any(d.endswith(s) for s in spec)]


def resolve_drive(store, ending):
    return next((d for d in store.drives if d.endswith(ending)), None)
