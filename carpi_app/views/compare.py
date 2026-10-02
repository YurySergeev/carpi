"""Compare drives: overlay traces, distributions per drive, histograms per tag, per-drive trend."""
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from dash import Input, Output, dcc, html, no_update

from .. import channels as ch, config
from . import G_DRIVES, G_FILTERS, G_QUERY, JUMP, TABS, View, control, empty_fig, note, register
from ._plot import group_colors, short_label

STATS = {"median": "Median", "mean": "Mean", "p95": "95th percentile", "p05": "5th percentile",
         "max": "Max", "min": "Min", "std": "Std dev", "count": "Rows"}


def stat(s, how):
    s = s.dropna()
    if not len(s):
        return np.nan
    return {"median": s.median, "mean": s.mean, "max": s.max, "min": s.min, "std": s.std, "count": s.size,
            "p95": lambda: s.quantile(0.95), "p05": lambda: s.quantile(0.05)}[how]()


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
            dcc.Loading(dcc.Graph(id="cmp-graph", style={"height": "600px"},
                                  config={"displaylogo": False, "scrollZoom": True}),
                        type="dot", color=config.SERIES[0]),
            html.Div("Tip: for idle fuel trim history pick Total fuel trim + the Warm, Closed loop and Idle filters. "
                     "Click a drive's point to open it in the time-series view.", className="ctl-hint"),
        ])

    def callbacks(self, app, store):
        @app.callback(Output("cmp-graph", "figure"), Output("cmp-info", "children"),
                      Input("cmp-ch", "value"), Input("cmp-mode", "value"), Input("cmp-stat", "value"),
                      Input(G_DRIVES, "value"), Input(G_FILTERS, "value"), Input(G_QUERY, "value"))
        def draw(c, mode, how, ids, keys, query):
            if not ids:
                return empty_fig("Select drives in the sidebar."), ""
            df, total, err = store.frames(ids, keys, query, columns=[c, "t_min"])
            if df.empty or c not in df:
                return empty_fig("No rows match the filters."), note(total, 0, err)
            df = df.dropna(subset=[c])
            fig = go.Figure()
            order = sorted(df["drive"].unique(), key=lambda d: (store.drives[d].start or "", d))
            tag_c = group_colors(store, df, "tag")

            if mode == "trend":
                rows = []
                for d in order:
                    i = store.drives[d]
                    rows.append(dict(drive=d, tag=i.tag, start=pd.Timestamp(i.start) if i.start else pd.NaT,
                                     v=stat(df.loc[df.drive == d, c], how), n=int((df.drive == d).sum()),
                                     ok=i.start_ok))
                t = pd.DataFrame(rows).dropna(subset=["v"])
                for tag, g in t.groupby("tag", sort=False):
                    col = tag_c[tag][0]
                    fig.add_trace(go.Scatter(
                        x=g["start"], y=g["v"], mode="lines+markers", name=tag,
                        line=dict(width=2, color=col),
                        marker=dict(size=10, color=col, symbol=["circle" if ok else "circle-open" for ok in g["ok"]],
                                    line=dict(width=2, color=col)),
                        customdata=np.array([[d, -1, n, short_label(store, d)] for d, n in zip(g["drive"], g["n"])], dtype=object),
                        hovertemplate=f"%{{customdata[3]}}<br>{STATS[how]} %{{y:.3~f}} {ch.meta(c)[1]}"
                                      f"<br>%{{customdata[2]:,}} rows<extra></extra>"))
                fig.update_layout(yaxis_title=f"{STATS[how]} {ch.label(c)}", xaxis_title="Drive start",
                                  hovermode="closest")
                extra = "open circles = start time unverified"
            elif mode == "box":
                for d in order:
                    i = store.drives[d]
                    g = df.loc[df.drive == d, c]
                    if len(g) > 20000:
                        g = g.sample(20000, random_state=0)
                    col = tag_c[i.tag][0]
                    fig.add_trace(go.Box(y=g, name=short_label(store, d), marker_color=col, line_width=1.5,
                                         boxpoints=False, legendgroup=i.tag, showlegend=False, hoverinfo="y+name"))
                for tag, (col, _) in tag_c.items():
                    if tag in set(df["tag"]):
                        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers", name=tag,
                                                 marker=dict(size=10, color=col, symbol="square")))
                fig.update_layout(yaxis_title=ch.label(c), xaxis_tickangle=-40, hovermode="closest",
                                  margin=dict(b=130))
                extra = ""
            elif mode == "hist":
                lo, hi = df[c].quantile(0.005), df[c].quantile(0.995)
                for tag, g in df.groupby("tag", sort=False):
                    col = tag_c[tag][0]
                    fig.add_trace(go.Histogram(x=g[c].clip(lo, hi), name=f"{tag} ({g['drive'].nunique()} drives)",
                                               histnorm="probability density", nbinsx=60, opacity=0.55,
                                               marker=dict(color=col, line=dict(width=1, color=config.SURFACE))))
                    fig.add_vline(x=g[c].median(), line=dict(color=col, width=2, dash="dash"))
                fig.update_layout(barmode="overlay", xaxis_title=ch.label(c), yaxis_title="Share of rows",
                                  hovermode="closest")
                extra = "dashed lines = medians"
            else:  # overlay
                cmap = group_colors(store, df, "drive")
                step = max(1, len(df) // 120_000)
                for d in order:
                    g = df[df.drive == d].iloc[::step]
                    col, dash = cmap[d]
                    fig.add_trace(go.Scattergl(
                        x=g["t_min"], y=g[c], mode="lines", name=short_label(store, d),
                        line=dict(width=1.5, color=col, dash=dash),
                        customdata=g[["drive", "elapsed_s"]].to_numpy(),
                        hovertemplate=f"%{{y:.3~f}}<extra>{short_label(store, d)}</extra>"))
                fig.update_layout(xaxis_title="Time into drive (min)", yaxis_title=ch.label(c), hovermode="x")
                extra = "rows outside the filters are left out, so lines may jump"
            fig.update_layout(uirevision=f"{c}|{mode}")
            return fig, note(total, len(df), err, extra)

        @app.callback(Output(JUMP, "data", allow_duplicate=True), Output(TABS, "value", allow_duplicate=True),
                      Input("cmp-graph", "clickData"), prevent_initial_call=True)
        def jump(click):
            try:
                cd = click["points"][0]["customdata"]
                did, t = cd[0], float(cd[1])
            except (TypeError, KeyError, IndexError, ValueError):
                return no_update, no_update
            if did not in store.drives:
                return no_update, no_update
            if t < 0:                       # trend point = the whole drive
                return {"drive": did, "t0": None}, "ts"
            return {"drive": did, "t0": t, "t1": t}, "ts"
