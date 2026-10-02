"""X vs Y scatter across drives, coloured by drive, tag or any channel. Click a point to inspect it."""
import numpy as np
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html, no_update

from .. import channels as ch, config
from . import lazy_callback, G_DRIVES, G_FILTERS, G_QUERY, JUMP, TABS, View, control, empty_fig, note, register
from ._plot import binned_median, colorscale_for, group_colors, short_label


@register
class Scatter(View):
    id, label, order = "sc", "X vs Y", 20

    def layout(self, store):
        opts = ch.options(store.columns())
        color_opts = [{"label": "Drive", "value": "drive"}, {"label": "Tag", "value": "tag"}] + opts
        return html.Div([
            html.Div([
                control("X", dcc.Dropdown(id="sc-x", options=opts, value="rpm", clearable=False), "260px"),
                control("Y", dcc.Dropdown(id="sc-y", options=opts, value="timing_deg", clearable=False), "260px"),
                control("Colour by", dcc.Dropdown(id="sc-color", options=color_opts, value="boost_psi",
                                                  clearable=False), "260px"),
                control("Show", dcc.RadioItems(id="sc-mode", options=[
                    {"label": " Points", "value": "points"}, {"label": " Density", "value": "density"}],
                    value="points", className="checks", inline=True)),
                control("Overlay", dcc.Checklist(id="sc-trend", options=[
                    {"label": " Binned median line", "value": "median"}], value=[], className="checks")),
            ], className="toolbar"),
            html.Div(id="sc-info", className="info"),
            dcc.Store(id="sc-curves"),
            dcc.Loading(dcc.Graph(id="sc-graph", style={"height": "640px"},
                                  config={"displaylogo": False, "scrollZoom": True}),
                        type="dot", color=config.SERIES[0]),
            html.Div("Click any point to open that moment in the time-series view.", className="ctl-hint"),
        ])

    def callbacks(self, app, store):
        @lazy_callback(app, self.id, Output("sc-graph", "figure"), Output("sc-info", "children"),
                       Output("sc-curves", "data"),
                      Input("sc-x", "value"), Input("sc-y", "value"), Input("sc-color", "value"),
                      Input("sc-mode", "value"), Input("sc-trend", "value"),
                      Input(G_DRIVES, "value"), Input(G_FILTERS, "value"), Input(G_QUERY, "value"))
        def draw(*args):
            fig, info = _draw(*args)
            return fig, info, [getattr(t, "meta", None) for t in fig.data]   # trace -> drive, for clicks

        def _draw(x, y, color, mode, trend, ids, keys, query):
            if not ids:
                return empty_fig("Select drives in the sidebar."), ""
            cols = [x, y] + ([color] if color not in ("drive", "tag") else [])
            df, total, err = store.frames(ids, keys, query, columns=cols)
            if df.empty or x not in df or y not in df:
                return empty_fig("No rows match the filters."), note(total, 0, err)
            df = df.dropna(subset=[x, y])
            matched = len(df)
            extra = ""
            if matched > config.MAX_SCATTER_POINTS:
                df = df.sample(config.MAX_SCATTER_POINTS, random_state=0).sort_index()
                extra = f"showing a random {config.MAX_SCATTER_POINTS:,}"
            fig = go.Figure()
            if mode == "density":
                fig.add_trace(go.Histogram2d(x=df[x], y=df[y], nbinsx=90, nbinsy=70, colorscale=config.SEQUENTIAL,
                                             colorbar=dict(title="rows", thickness=12),
                                             hovertemplate="x %{x}<br>y %{y}<br>%{z} rows<extra></extra>"))
                fig.data[0].update(zmin=1)
            else:
                # One trace per drive keeps every array numeric (fast to send) and lets hover name the drive.
                by_channel = color not in ("drive", "tag")
                cmap = group_colors(store, df, "tag" if color == "tag" else "drive")
                if by_channel:
                    cs = colorscale_for(df[color])
                    fig.update_layout(coloraxis=dict(
                        colorscale=cs["colorscale"], cmin=cs.get("cmin"), cmax=cs.get("cmax"), cmid=cs.get("cmid"),
                        colorbar=dict(title=ch.label(color), thickness=12, title_side="right")))
                shown_tags = set()
                for did, g in df.groupby("drive", sort=False, observed=True):
                    name = short_label(store, did)
                    tag = store.drives[did].tag
                    cols = ["order", "elapsed_s"] + ([color] if by_channel else [])
                    cd = g[cols].to_numpy(dtype="float32")
                    if by_channel:
                        marker = dict(size=6, color=g[color].to_numpy(), coloraxis="coloraxis", opacity=0.7)
                        extra_hover = f"<br>{ch.label(color)} %{{customdata[2]:.3~f}}"
                        legend = dict(showlegend=False)
                    else:
                        c = cmap.get(tag if color == "tag" else did, (config.SERIES[0], ""))[0]
                        marker = dict(size=6, color=c, opacity=0.6)
                        extra_hover = ""
                        legend = (dict(name=name) if color == "drive" else
                                  dict(name=tag, legendgroup=tag, showlegend=tag not in shown_tags))
                        shown_tags.add(tag)
                    fig.add_trace(go.Scattergl(
                        x=g[x].to_numpy(), y=g[y].to_numpy(), mode="markers", marker=dict(line=dict(width=0), **marker),
                        customdata=cd, meta=did, **legend,
                        hovertemplate=f"{ch.label(x)} %{{x:.3~f}}<br>{ch.label(y)} %{{y:.3~f}}{extra_hover}"
                                      f"<br>{name} \u00b7 %{{customdata[1]:.0f}} s<extra></extra>"))
            if "median" in (trend or []):
                by = color if color in ("drive", "tag") else None
                groups = df.groupby(by, sort=False, observed=True) if by else [(None, df)]
                cmap = group_colors(store, df, by) if by else {}
                for key, g in groups:
                    bx, by_ = binned_median(g[x], g[y])
                    if len(bx):
                        c = cmap.get(key, (config.INK, ""))[0]
                        # Scattergl so the line draws above the WebGL points, not under them
                        fig.add_trace(go.Scattergl(x=bx, y=by_, mode="lines+markers", showlegend=False,
                                                 line=dict(width=3, color=c), marker=dict(size=8, color=c,
                                                 line=dict(width=2, color=config.SURFACE)),
                                                 hovertemplate="median %{y:.3~f}<extra></extra>"))
            fig.update_layout(xaxis_title=ch.label(x), yaxis_title=ch.label(y), hovermode="closest",
                              uirevision=f"{x}|{y}", legend=dict(itemsizing="constant"))
            fig.update_xaxes(showspikes=False)
            return fig, note(total, matched, err, extra)

        @app.callback(Output(JUMP, "data", allow_duplicate=True), Output(TABS, "value", allow_duplicate=True),
                      Input("sc-graph", "clickData"), State("sc-curves", "data"), State("sc-x", "value"),
                      State("sc-y", "value"), prevent_initial_call=True)
        def jump(click, curves, x, y):
            # Find the clicked row on the server: the trace tells us the drive, x/y pin down the moment.
            try:
                pt = click["points"][0]
                did = (curves or [])[pt["curveNumber"]]
                px, py = float(pt["x"]), float(pt["y"])
            except (TypeError, KeyError, IndexError, ValueError):
                return no_update, no_update
            if not isinstance(did, str) or did not in store.drives:
                return no_update, no_update
            f = store.frame(did)
            if x not in f or y not in f:
                return no_update, no_update
            sx, sy = (f[x].std() or 1.0), (f[y].std() or 1.0)
            dist = ((f[x] - px).abs() / sx + (f[y] - py).abs() / sy).fillna(np.inf)
            t = float(f["elapsed_s"].iloc[int(np.argmin(dist.to_numpy()))])
            return {"drive": did, "t0": t, "t1": t}, "ts"
