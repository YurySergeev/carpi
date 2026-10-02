"""Paths, limits and the chart theme. Everything tweakable lives here."""
import os
from pathlib import Path

import plotly.graph_objects as go
import plotly.io as pio

ROOT = Path(__file__).resolve().parent.parent          # the CarPi repo

# Environment overrides (used by the hosted copy; locally you don't need any of these)
#   CARPI_LOGS    folders to scan, separated by ; or :     CARPI_CACHE   where summaries are cached
#   CARPI_PUBLIC  1 = read-only public mode (no rescan button, no local paths shown)
#   CARPI_ABOUT   a line of intro text shown at the top of the sidebar in public mode
_env_logs = os.environ.get("CARPI_LOGS")
LOG_DIRS = ([Path(p) for p in _env_logs.replace(";", os.pathsep).split(os.pathsep) if p]
            if _env_logs else [ROOT / "logs", ROOT / "data"])  # searched recursively for drive-*.csv
CACHE_DIR = Path(os.environ.get("CARPI_CACHE") or ROOT / "carpi_app" / ".cache")   # per-drive summaries
PUBLIC = os.environ.get("CARPI_PUBLIC", "") not in ("", "0", "false", "False")
ABOUT = os.environ.get("CARPI_ABOUT", "")

MAX_SCATTER_POINTS = 150_000   # scatter is randomly thinned above this (seeded, so it is stable)
if PUBLIC:                     # small hosted box (Render free: 0.1 CPU, 512 MB)
    MAX_SCATTER_POINTS = 60_000
FRAME_CACHE_SIZE = 200         # parsed drives kept in memory (~2 MB each)

# ---- palette (same as analyze_drives.py so reports and app match)
INK, INK2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
DASHES = ["solid", "dash", "dot", "dashdot"]           # second encoding once colours repeat
SEQUENTIAL = [[0, "#e8f1fc"], [0.5, "#5b9be3"], [1, "#0d3f7f"]]          # one hue, light -> dark
DIVERGING = [[0, "#2a78d6"], [0.5, "#efeee8"], [1, "#e0542a"]]           # blue - grey - orange
EVENT_SHADE = "rgba(235,104,52,0.13)"

pio.templates["carpi"] = go.layout.Template(layout=dict(
    font=dict(family="Inter, 'Segoe UI', system-ui, sans-serif", size=12, color=INK),
    paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, colorway=SERIES,
    xaxis=dict(gridcolor=GRID, linecolor=AXIS, zeroline=False, ticks="", title_font_color=INK2,
               tickfont_color=MUTED, showspikes=True, spikemode="across", spikecolor=MUTED,
               spikethickness=1, spikedash="dot", spikesnap="cursor"),
    yaxis=dict(gridcolor=GRID, linecolor=AXIS, zeroline=False, ticks="", title_font_color=INK2,
               tickfont_color=MUTED),
    hoverlabel=dict(bgcolor="white", bordercolor=AXIS, font=dict(color=INK, size=12)),
    legend=dict(orientation="h", yanchor="bottom", y=1.01, x=0, font=dict(color=INK2)),
    margin=dict(l=64, r=24, t=36, b=44),
    colorscale=dict(sequential=SEQUENTIAL, diverging=DIVERGING),
))
pio.templates.default = "carpi"


def color(i):
    return SERIES[i % len(SERIES)]


def dash_style(i):
    return DASHES[(i // len(SERIES)) % len(DASHES)]
