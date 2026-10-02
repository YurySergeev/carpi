"""X vs Y scatter across drives, coloured by drive, tag or any channel. Click a point to inspect it."""
import plotly.graph_objects as go
from dash import Input, Output, dcc, html, no_update

from .. import channels as ch, config
from . import G_DRIVES, G_FILTERS, G_QUERY, JUMP, TABS, View, control, empty_fig, note, register
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
            dcc.Loading(dcc.Graph(id="sc-graph", style={"height": "640px"},
                                  config={"displaylogo": False, "scrollZoom": True}),
                        type="dot", color=config.SERIES[0]),
            html.Div("Click any point to open that moment in the time-series view.", className="ctl-hint"),
        ])

    def callbacks(self, app, store):
        @app.callback(Output("sc-graph", "figure"), Output("sc-info", "children"),
                      Input("sc-x", "value"), Input("sc-y", "value"), Input("sc-color", "value"),
                      Input("sc-mode", "value"), Input("sc-trend", "value"),
                      Input(G_DRIVES, "value"), Input(G_FILTERS, "value"), Input(G_QUERY, "value"))
        def draw(x, y, color, mode, trend, ids, keys, query):
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
            elif color in ("drive", "tag"):
                cmap = group_colors(store, df, color)
                for key, g in df.groupby(color, sort=False):
                    c, _ = cmap.get(key, (config.SERIES[0], "solid"))
                    name = short_label(store, key) if color == "drive" else key
                    fig.add_trace(go.Scattergl(
                        x=g[x], y=g[y], mode="markers", name=name,
                        marker=dict(size=6, color=c, opacity=0.6, line=dict(width=0)),
                        customdata=g[["drive", "elapsed_s"]].to_numpy(),
                        hovertemplate=f"{ch.label(x)} %{{x:.3~f}}<br>{ch.label(y)} %{{y:.3~f}}"
                                      f"<br>{name} · %{{customdata[1]:.0f}} s<extra></extra>"))
            else:
                cs = colorscale_for(df[color])
                fig.add_trace(go.Scattergl(
                    x=df[x], y=df[y], mode="markers", showlegend=False,
                    marker=dict(size=6, color=df[color], opacity=0.7, line=dict(width=0),
                                colorbar=dict(title=ch.label(color), thickness=12, title_side="right"), **cs),
                    customdata=df[["drive", "elapsed_s", color]].to_numpy(),
                    hovertemplate=f"{ch.label(x)} %{{x:.3~f}}<br>{ch.label(y)} %{{y:.3~f}}<br>"
                                  f"{ch.label(color)} %{{customdata[2]:.3~f}}<br>%{{customdata[0]}} · "
                                  f"%{{customdata[1]:.0f}} s<extra></extra>"))
            if "median" in (trend or []):
                by = color if color in ("drive", "tag") else None
                groups = df.groupby(by, sort=False) if by else [(None, df)]
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
                      Input("sc-graph", "clickData"), prevent_initial_call=True)
        def jump(click):
            try:
                did, t = click["points"][0]["customdata"][:2]
            except (TypeError, KeyError, IndexError, ValueError):
                return no_update, no_update
            return {"drive": did, "t0": float(t), "t1": float(t)}, "ts"
