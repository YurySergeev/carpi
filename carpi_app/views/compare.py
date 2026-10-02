"""Compare drives: overlay traces, distributions per drive, histograms per tag, per-drive trend."""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update

from .. import channels as ch, config
from . import lazy_callback, G_DRIVES, G_FILTERS, G_QUERY, JUMP, TABS, View, control, empty_fig, note, register
from ._plot import group_colors, short_label

STATS = {"median": "Median", "mean": "Mean", "p95": "95th percentile", "p05": "5th percentile",
         "max": "Max", "min": "Min", "std": "Std dev", "count": "Rows"}


@register
class Compare(View):
    id, label, order = "cmp", "Compare drives", 30

    def layout(self, store):
        return html.Div([
            html.Div([
                control("Channel", dcc.Dropdown(id="cmp-ch", options=ch.options(store.columns()),
                                                value="trim_total", clearable=False), "300px"),
                control("Chart", dcc.RadioItems(id="cmp-mode", options=[
                    {"label": " Trend per drive", "value": "trend"},
                    {"label": " Distribution per drive", "value": "box"},
                    {"label": " Histogram per tag", "value": "hist"},
                    {"label": " Overlay traces", "value": "overlay"}],
                    value="trend", className="checks", inline=True)),
                control("Statistic (trend)", dcc.Dropdown(id="cmp-stat", value="median", clearable=False,
                                                          options=[{"label": v, "value": k} for k, v in STATS.items()]),
                        "180px"),
            ], className="toolbar"),
            html.Div(id="cmp-info", className="info"),
            dcc.Store(id="cmp-curves"),
            dcc.Loading(dcc.Graph(id="cmp-graph", style={"height": "600px"},
                                  config={"displaylogo": False, "scrollZoom": True}),
                        type="dot", color=config.SERIES[0]),
            html.Div("Tip: for idle fuel trim history pick Total fuel trim + the Warm, Closed loop and Idle filters. "
                     "Click a drive's point to open it in the time-series view.", className="ctl-hint"),
        ])

    def callbacks(self, app, store):
        @lazy_callback(app, self.id, Output("cmp-graph", "figure"), Output("cmp-info", "children"),
                       Output("cmp-curves", "data"),
                      Input("cmp-ch", "value"), Input("cmp-mode", "value"), Input("cmp-stat", "value"),
                      Input(G_DRIVES, "value"), Input(G_FILTERS, "value"), Input(G_QUERY, "value"))
        def draw(*args):
            fig, info = _draw(*args)
            return fig, info, [getattr(t, "meta", None) for t in fig.data]   # trace -> drive(s), for clicks

        def _draw(c, mode, how, ids, keys, query):
            if not ids:
                return empty_fig("Select drives in the sidebar."), ""
            df, total, err = store.frames(ids, keys, query, columns=[c, "t_min"])
            if df.empty or c not in df:
                return empty_fig("No rows match the filters."), note(total, 0, err)
            df = df.dropna(subset=[c])
            if df.empty:
                return empty_fig("No rows match the filters."), note(total, 0, err)
            fig = go.Figure()
            sel = list(df["drive"].cat.categories)              # selection order = the `order` column
            present = set(df["drive"].unique())
            order = sorted((d for d in sel if d in present), key=lambda d: (store.drives[d].start or "", d))
            tag_c = group_colors(store, df, "tag")
            by_drive = df.groupby("drive", observed=True)[c]

            if mode == "trend":
                q = {"median": 0.5, "p95": 0.95, "p05": 0.05}
                vals = by_drive.quantile(q[how]) if how in q else (by_drive.size() if how == "count" else
                                                                     by_drive.agg(how))
                counts = by_drive.size()
                t = pd.DataFrame({"drive": order})
                t["v"] = t["drive"].map(vals).astype(float)
                t["n"] = t["drive"].map(counts).astype(int)
                t["tag"] = [store.drives[d].tag for d in order]
                t["start"] = [pd.Timestamp(store.drives[d].start) if store.drives[d].start else pd.NaT for d in order]
                t["ok"] = [store.drives[d].start_ok for d in order]
                t = t.dropna(subset=["v"])
                for tag, g in t.groupby("tag", sort=False):
                    col = tag_c[tag][0]
                    fig.add_trace(go.Scatter(
                        x=g["start"], y=g["v"], mode="lines+markers", name=tag,
                        line=dict(width=2, color=col),
                        marker=dict(size=10, color=col, symbol=["circle" if ok else "circle-open" for ok in g["ok"]],
                                    line=dict(width=2, color=col)),
                        customdata=[[n] for n in g["n"]], meta=list(g["drive"]),
                        text=[short_label(store, d) for d in g["drive"]],
                        hovertemplate=f"%{{text}}<br>{STATS[how]} %{{y:.3~f}} {ch.meta(c)[1]}"
                                      f"<br>%{{customdata[0]:,}} rows<extra></extra>"))
                fig.update_layout(yaxis_title=f"{STATS[how]} {ch.label(c)}", xaxis_title="Drive start",
                                  hovermode="closest")
                extra = "open circles = start time unverified"
            elif mode == "box":
                # Box statistics are computed here and sent as 5 numbers per drive instead of the raw rows.
                qs = by_drive.quantile([0.0, 0.25, 0.5, 0.75, 1.0]).unstack()
                for d in order:
                    lo, q1, med_, q3, hi = qs.loc[d].tolist()
                    iqr = q3 - q1
                    g = df.loc[df["order"] == sel.index(d), c]
                    lf = float(g[g >= q1 - 1.5 * iqr].min())
                    uf = float(g[g <= q3 + 1.5 * iqr].max())
                    col = tag_c[store.drives[d].tag][0]
                    fig.add_trace(go.Box(x=[short_label(store, d)], q1=[q1], median=[med_], q3=[q3],
                                         lowerfence=[lf], upperfence=[uf], name=short_label(store, d),
                                         marker_color=col, line_width=1.5, showlegend=False, meta=d))
                for tag, (col, _) in tag_c.items():
                    if tag in set(df["tag"].unique()):
                        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=tag,
                                                 marker=dict(size=10, color=col, symbol="square")))
                fig.update_layout(yaxis_title=ch.label(c), xaxis_tickangle=-40, hovermode="closest",
                                  margin=dict(b=130))
                extra = "whiskers = 1.5 \u00d7 IQR"
            elif mode == "hist":
                lo, hi = df[c].quantile(0.005), df[c].quantile(0.995)
                edges = np.histogram_bin_edges(df[c].clip(lo, hi), bins=60)
                centers, width = (edges[:-1] + edges[1:]) / 2, edges[1] - edges[0]
                for tag, g in df.groupby("tag", sort=False, observed=True):
                    col = tag_c[tag][0]
                    dens, _ = np.histogram(g[c].clip(lo, hi), bins=edges, density=True)
                    fig.add_trace(go.Bar(x=centers, y=dens * width, width=width, opacity=0.55,
                                         name=f"{tag} ({g['drive'].nunique()} drives)",
                                         marker=dict(color=col, line=dict(width=1, color=config.SURFACE)),
                                         hovertemplate="%{x:.3~f}: %{y:.1%} of rows<extra></extra>"))
                    fig.add_vline(x=g[c].median(), line=dict(color=col, width=2, dash="dash"))
                fig.update_layout(barmode="overlay", bargap=0, xaxis_title=ch.label(c), yaxis_title="Share of rows",
                                  yaxis_tickformat=".0%", hovermode="closest")
                extra = "dashed lines = medians"
            else:  # overlay
                cmap = group_colors(store, df, "drive")
                step = max(1, len(df) // config.MAX_SCATTER_POINTS)
                for d in order:
                    g = df[df["order"] == sel.index(d)].iloc[::step]
                    col, dash = cmap[d]
                    fig.add_trace(go.Scattergl(
                        x=g["t_min"].to_numpy(), y=g[c].to_numpy(), mode="lines", name=short_label(store, d),
                        line=dict(width=1.5, color=col, dash=dash),
                        meta=d,
                        hovertemplate=f"%{{y:.3~f}}<extra>{short_label(store, d)}</extra>"))
                fig.update_layout(xaxis_title="Time into drive (min)", yaxis_title=ch.label(c), hovermode="x")
                extra = "rows outside the filters are left out, so lines may jump"
            fig.update_layout(uirevision=f"{c}|{mode}")
            return fig, note(total, len(df), err, extra)

        @app.callback(Output(JUMP, "data", allow_duplicate=True), Output(TABS, "value", allow_duplicate=True),
                      Input("cmp-graph", "clickData"), State("cmp-curves", "data"), State("cmp-mode", "value"),
                      prevent_initial_call=True)
        def jump(click, curves, mode):
            try:
                pt = click["points"][0]
                meta = (curves or [])[pt["curveNumber"]]
            except (TypeError, KeyError, IndexError):
                return no_update, no_update
            if isinstance(meta, list):                     # trend: one drive per point
                meta = meta[pt.get("pointNumber", 0)] if pt.get("pointNumber", 0) < len(meta) else None
            if not isinstance(meta, str) or meta not in store.drives:
                return no_update, no_update
            if mode == "overlay":                          # x is minutes into the drive
                t = float(pt["x"]) * 60
                return {"drive": meta, "t0": t, "t1": t}, "ts"
            return {"drive": meta, "t0": None}, "ts"       # whole drive
