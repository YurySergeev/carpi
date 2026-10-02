"""3-axis map: bin X and Y like an ECU table and show an aggregate of Z in each cell.
Optional A - B difference between two tags (e.g. before/after a repair or tune)."""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, dcc, html

from .. import channels as ch, config
from . import lazy_callback, G_DRIVES, G_FILTERS, G_QUERY, View, control, empty_fig, note, register

AGGS = {"mean": "Mean", "median": "Median", "min": "Min", "max": "Max", "std": "Std dev",
        "p95": "95th pct", "p05": "5th pct", "count": "Rows"}
MAX_BINS = 40


def edges_for(s, step):
    # ignore the extreme 0.1% (sensor glitches) on big data so one bad row can't stretch the grid
    lo, hi = (s.quantile(0.001), s.quantile(0.999)) if len(s) >= 1000 else (s.min(), s.max())
    if not step or step <= 0 or pd.isna(lo):
        step = (hi - lo) / 12 if hi > lo else 1
    while (hi - lo) / step > MAX_BINS:
        step *= 2
    start = np.floor(lo / step) * step
    stop = np.floor(hi / step) * step + step          # last bin always contains hi (bins are [a, b))
    e = np.arange(start, stop + step / 2, step)
    return e if len(e) >= 2 else np.array([start, start + step]), step


def table(df, x, y, z, ex, ey, agg, min_n):
    xi = pd.cut(df[x], ex, right=False, labels=False)
    yi = pd.cut(df[y], ey, right=False, labels=False)
    g = pd.DataFrame({"xi": xi, "yi": yi, "z": df[z] if z in df else 0}).dropna(subset=["xi", "yi"])
    grp = g.groupby(["yi", "xi"])["z"]
    funcs = {"p95": lambda s: s.quantile(0.95), "p05": lambda s: s.quantile(0.05)}
    val = grp.agg(funcs.get(agg, agg if agg != "count" else "size"))
    n = grp.size()
    val = val[n >= min_n] if agg != "count" else n[n >= min_n].astype(float)
    Z = np.full((len(ey) - 1, len(ex) - 1), np.nan)
    N = np.zeros_like(Z)
    for (yy, xx), v in val.items():
        Z[int(yy), int(xx)] = v
    for (yy, xx), v in n.items():
        N[int(yy), int(xx)] = v
    return Z, N


def fmt_cell(v, rng):
    if np.isnan(v):
        return ""
    if rng >= 100:
        return f"{v:.0f}"
    if rng >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}" if rng >= 0.5 else f"{v:.3f}"


@register
class Map3D(View):
    id, label, order = "map", "3-axis map", 40

    def layout(self, store):
        opts = ch.options(store.columns())
        tags = [{"label": t, "value": t} for t in store.tags()]
        num = dict(type="number", debounce=True, className="num")
        return html.Div([
            html.Div([
                control("X axis", dcc.Dropdown(id="map-x", options=opts, value="rpm", clearable=False), "230px"),
                control("X step", dcc.Input(id="map-xstep", value=500, **num), "90px"),
                control("Y axis", dcc.Dropdown(id="map-y", options=opts, value="load_pct", clearable=False), "230px"),
                control("Y step", dcc.Input(id="map-ystep", value=10, **num), "90px"),
                control("Value (Z)", dcc.Dropdown(id="map-z", options=opts, value="timing_deg", clearable=False),
                        "230px"),
                control("Aggregate", dcc.Dropdown(id="map-agg", value="mean", clearable=False,
                                                  options=[{"label": v, "value": k} for k, v in AGGS.items()]),
                        "130px"),
                control("Min rows/cell", dcc.Input(id="map-min", value=10, min=1, **num), "100px"),
            ], className="toolbar"),
            html.Div([
                control("Display", dcc.RadioItems(id="map-view", options=[
                    {"label": " Table", "value": "table"}, {"label": " 3D surface", "value": "surface"}],
                    value="table", className="checks", inline=True)),
                control("Difference (A − B)", html.Div([
                    dcc.Dropdown(id="map-a", options=tags, placeholder="Tag A (e.g. after)", style={"width": "190px"}),
                    dcc.Dropdown(id="map-b", options=tags, placeholder="Tag B (e.g. baseline)",
                                 style={"width": "190px"}),
                ], style={"display": "flex", "gap": "8px"})),
            ], className="toolbar"),
            html.Div(id="map-info", className="info"),
            dcc.Loading(dcc.Graph(id="map-graph", style={"height": "680px"}, config={"displaylogo": False}),
                        type="dot", color=config.SERIES[0]),
            html.Div("Steps reset to sensible defaults when you change an axis. Empty cells have fewer rows than "
                     "the minimum. Uses the drives, filters and query from the sidebar.", className="ctl-hint"),
        ])

    def callbacks(self, app, store):
        @app.callback(Output("map-xstep", "value"), Input("map-x", "value"), prevent_initial_call=True)
        def xs(c):
            return ch.step(c)

        @app.callback(Output("map-ystep", "value"), Input("map-y", "value"), prevent_initial_call=True)
        def ys(c):
            return ch.step(c)

        @lazy_callback(app, self.id, Output("map-graph", "figure"), Output("map-info", "children"),
                      Input("map-x", "value"), Input("map-xstep", "value"), Input("map-y", "value"),
                      Input("map-ystep", "value"), Input("map-z", "value"), Input("map-agg", "value"),
                      Input("map-min", "value"), Input("map-view", "value"), Input("map-a", "value"),
                      Input("map-b", "value"),
                      Input(G_DRIVES, "value"), Input(G_FILTERS, "value"), Input(G_QUERY, "value"))
        def draw(x, xstep, y, ystep, z, agg, min_n, view, tag_a, tag_b, ids, keys, query):
            if not ids:
                return empty_fig("Select drives in the sidebar."), ""
            df, total, err = store.frames(ids, keys, query, columns=[x, y, z])
            if df.empty:
                return empty_fig("No rows match the filters."), note(total, 0, err)
            df = df.dropna(subset=[x, y] + ([z] if agg != "count" else []))
            if df.empty:
                return empty_fig("No rows with all three channels."), note(total, 0, err)
            ex, sx = edges_for(df[x], xstep)
            ey, sy = edges_for(df[y], ystep)
            min_n = int(min_n or 1)
            diff = tag_a and tag_b and tag_a != tag_b
            if diff:
                Za, Na = table(df[df.tag == tag_a], x, y, z, ex, ey, agg, min_n)
                Zb, Nb = table(df[df.tag == tag_b], x, y, z, ex, ey, agg, min_n)
                Z, N = Za - Zb, np.minimum(Na, Nb)
                ztitle = f"Δ {AGGS[agg]} {ch.label(z)}  ({tag_a} − {tag_b})"
            else:
                Z, N = table(df, x, y, z, ex, ey, agg, min_n)
                ztitle = f"{AGGS[agg]} {ch.label(z)}" if agg != "count" else "Rows"
            if np.isnan(Z).all():
                return empty_fig("No cell has enough rows. Lower 'Min rows/cell' or widen the steps."), \
                    note(total, len(df), err)
            xc, yc = (ex[:-1] + ex[1:]) / 2, (ey[:-1] + ey[1:]) / 2
            zmin, zmax = np.nanmin(Z), np.nanmax(Z)
            signed = diff or (zmin < 0 < zmax)
            if signed:
                lim = max(abs(zmin), abs(zmax))
                scale = dict(colorscale=config.DIVERGING, zmid=0, zmin=-lim, zmax=lim)
            else:
                scale = dict(colorscale=config.SEQUENTIAL)
            hover = (f"{ch.label(x)} %{{customdata[0]}}<br>{ch.label(y)} %{{customdata[1]}}<br>"
                     f"{ztitle}: %{{z:.3~f}}<br>%{{customdata[2]:,}} rows<extra></extra>")
            xr = [f"{a:g}–{b:g}" for a, b in zip(ex[:-1], ex[1:])]
            yr = [f"{a:g}–{b:g}" for a, b in zip(ey[:-1], ey[1:])]
            cd = np.empty(Z.shape + (3,), dtype=object)
            for i in range(Z.shape[0]):
                for j in range(Z.shape[1]):
                    cd[i, j] = (xr[j], yr[i], int(N[i, j]))
            if view == "surface":
                fig = go.Figure(go.Surface(x=xc, y=yc, z=Z, customdata=cd, hovertemplate=hover,
                                           colorbar=dict(title=ztitle, thickness=12, title_side="right"),
                                           connectgaps=False, **{k.replace("z", "c"): v for k, v in scale.items()
                                                                 if k != "colorscale"},
                                           colorscale=scale["colorscale"]))
                fig.update_layout(scene=dict(xaxis_title=ch.label(x), yaxis_title=ch.label(y), zaxis_title=ztitle,
                                             bgcolor=config.SURFACE),
                                  margin=dict(l=0, r=0, t=20, b=0))
            else:
                rng = (zmax - zmin) if zmax > zmin else abs(zmax)
                text = [[fmt_cell(v, rng) for v in row] for row in Z]
                fig = go.Figure(go.Heatmap(x=xc, y=yc, z=Z, text=text, texttemplate="%{text}",
                                           textfont=dict(size=11), customdata=cd, hovertemplate=hover,
                                           xgap=2, ygap=2, colorbar=dict(title=ztitle, thickness=12, title_side="right"),
                                           **scale))
                fig.update_layout(xaxis=dict(title=ch.label(x), tickvals=ex, showgrid=False, showspikes=False),
                                  yaxis=dict(title=ch.label(y), tickvals=ey, showgrid=False))
            fig.update_layout(uirevision=f"{x}|{y}|{view}")
            filled = int((~np.isnan(Z)).sum())
            return fig, note(total, len(df), err, f"{filled} cells · X step {sx:g} · Y step {sy:g}")
