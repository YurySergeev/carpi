"""Time series: stacked channels for one drive, shared zoom and crosshair, clickable event list."""
import numpy as np
from dash import Input, Output, Patch, State, ctx, dash_table, dcc, html, no_update
from plotly.subplots import make_subplots
import plotly.graph_objects as go

from .. import channels as ch, config, events, filters
from . import lazy_callback, G_DRIVES, G_FILTERS, G_QUERY, JUMP, View, control, empty_fig, register

DEFAULT_CHANNELS = ["rpm", "boost_psi", "timing_deg", "lambda", "lambda_cmd", "trim_total", "volts"]

# channels listed together share one panel when both are selected
PANEL_GROUPS = [("lambda", "lambda_cmd"), ("stft_pct", "ltft_pct", "trim_total"), ("pedal_pct", "throttle_pct"),
                ("boost_act_kpa", "boost_cmd_kpa", "map_kpa", "baro_kpa"), ("coolant_c", "iat_c", "ambient_c"),
                ("speed_kph", "mph"), ("load_pct", "abs_load_pct")]


def panels(chs):
    out, where = [], {}
    for c in chs:
        grp = next((g for g in PANEL_GROUPS if c in g), None)
        key = next((where[o] for o in (grp or ()) if o in where), None)
        if key is None:
            where[c] = len(out)
            out.append([c])
        else:
            out[key].append(c)
            where[c] = key
    return out


def mmss(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


def build_figure(df, chs, evs, shade, hide_mask=None, rng=None):
    groups = panels(chs)
    n = len(groups)
    fig = make_subplots(rows=n, cols=1, shared_xaxes=True, vertical_spacing=min(0.035, 0.25 / max(n, 1)))
    x = df["t_min"]
    k = 0
    for r, grp in enumerate(groups, start=1):
        for c in grp:
            y = df[c] if c in df.columns else np.full(len(df), np.nan)
            if hide_mask is not None:
                y = np.where(hide_mask, y, np.nan)
            _, unit, _ = ch.meta(c)
            fig.add_trace(go.Scattergl(
                x=x, y=y, mode="lines", name=ch.label(c, unit=False), legendgroup=c,
                showlegend=len(grp) > 1, line=dict(width=1.6, color=config.color(k)),
                hovertemplate=f"%{{y:.3~f}} {unit}<extra>{ch.label(c, unit=False)}</extra>"), row=r, col=1)
            k += 1
        title = ch.label(grp[0]) if len(grp) == 1 else " / ".join(ch.label(c, unit=False) for c in grp)
        fig.update_yaxes(title_text=title if len(title) < 34 else title[:32] + "…", title_font_size=11,
                         row=r, col=1)
    fig.update_xaxes(showspikes=True, spikemode="across")
    fig.update_xaxes(title_text="Time into drive (min)", row=n, col=1)
    if shade:
        for e in evs:
            fig.add_shape(type="rect", xref="x", yref="paper", x0=e.start_s / 60 - 0.003, x1=e.end_s / 60 + 0.003,
                          y0=0, y1=1, fillcolor=config.EVENT_SHADE, line_width=0, layer="below")
    fig.update_layout(height=max(380, 150 * n + 80), hovermode="x unified", hoversubplots="axis",
                      xaxis=dict(hoverformat=".2f"), uirevision=None, margin=dict(l=70, r=20, t=30, b=44))
    if rng:   # every panel's x axis, since shared panels "match" the bottom one
        fig.update_xaxes(range=list(rng), autorange=False)
    return fig


def zoom_to(t0, t1):
    pad = max(3.0, 0.6 * (t1 - t0))
    return [(t0 - pad) / 60, (t1 + pad) / 60]


@register
class TimeSeries(View):
    id, label, order = "ts", "Time series", 10

    def layout(self, store):
        kinds = [{"label": lbl, "value": k} for k, (lbl, _) in events.DETECTORS.items()]
        return html.Div([
            html.Div([
                control("Drive", dcc.Dropdown(id="ts-drive", options=store.options(), clearable=False), "420px"),
                control("Channels", dcc.Dropdown(id="ts-channels", options=ch.options(store.columns()),
                                                 value=DEFAULT_CHANNELS, multi=True), "520px",
                        hint="Related channels (λ act/cmd, trims, temps…) share a panel."),
                control("Options", dcc.Checklist(id="ts-opts", options=[
                    {"label": " Shade events", "value": "shade"},
                    {"label": " Blank rows outside sidebar filters", "value": "filtered"}],
                    value=["shade"], className="checks")),
                html.Button("Reset zoom", id="ts-reset", className="btn"),
            ], className="toolbar"),
            html.Div(id="ts-info", className="info"),
            dcc.Loading(dcc.Graph(id="ts-graph", config={"displaylogo": False, "scrollZoom": True}),
                        type="dot", color=config.SERIES[0]),
            html.H3("Events", className="section"),
            html.Div([
                control("Show", dcc.Dropdown(id="ts-kinds", options=kinds, multi=True,
                                             value=[k for k in events.DETECTORS if k != "shift"]), "100%"),
            ], className="toolbar"),
            html.Div("Click a row to zoom the plots to it.", className="ctl-hint"),
            dash_table.DataTable(
                id="ts-events", columns=[{"name": n, "id": i} for n, i in
                                         [("At", "at"), ("Event", "kind"), ("Length", "len"), ("Detail", "detail"), ("sev", "sev")]],
                hidden_columns=["sev"],
                data=[], page_size=15, sort_action="native", filter_action="native",
                style_as_list_view=True, style_cell={"fontFamily": "Inter, 'Segoe UI', system-ui, sans-serif", "fontSize": 13, "padding": "6px 10px",
                                                     "textAlign": "left", "backgroundColor": config.SURFACE},
                style_header={"fontWeight": 600, "backgroundColor": "#f3f2ee"},
                style_data_conditional=[
                    {"if": {"filter_query": '{sev} = "crit"', "column_id": "kind"}, "color": "#c23030", "fontWeight": 600},
                    {"if": {"filter_query": '{sev} = "warn"', "column_id": "kind"}, "color": "#a26b00", "fontWeight": 600},
                    {"if": {"state": "active"}, "backgroundColor": "#e8f1fc", "border": "1px solid #2a78d6"}]),
        ])

    def callbacks(self, app, store):
        @lazy_callback(
            app, self.id,
            Output("ts-drive", "value"), Output("ts-graph", "figure"), Output("ts-events", "data"),
            Output("ts-info", "children"),
            Input("ts-drive", "value"), Input("ts-channels", "value"), Input("ts-opts", "value"),
            Input("ts-kinds", "value"), Input(G_DRIVES, "value"), Input(G_FILTERS, "value"),
            Input(G_QUERY, "value"), Input(JUMP, "data"), Input("ts-events", "active_cell"),
            Input("ts-reset", "n_clicks"),
            State("ts-events", "data"))
        def draw(drive, chs, opts, kinds, g_ids, keys, query, jump, cell, _reset, rows):
            fired = set(ctx.triggered_prop_ids.values())   # several can fire at once (jump + tab)
            if "ts-events" in fired:
                if not cell or not rows:
                    return no_update, no_update, no_update, no_update
                r = next((x for x in rows if x["id"] == cell.get("row_id")), None)
                if r is None:
                    return no_update, no_update, no_update, no_update
                p = Patch()
                p["layout"]["xaxis"]["range"] = zoom_to(r["t0"], r["t1"])
                p["layout"]["xaxis"]["autorange"] = False
                return no_update, p, no_update, no_update
            if "ts-reset" in fired:
                p = Patch()
                p["layout"]["xaxis"]["autorange"] = True
                for i in range(1, 12):
                    p["layout"][f"yaxis{'' if i == 1 else i}"]["autorange"] = True
                return no_update, p, no_update, no_update

            rng = None
            if JUMP in fired and jump and jump.get("drive") in store.drives:
                drive = jump["drive"]
                if jump.get("t0") is not None:
                    rng = zoom_to(jump["t0"], jump.get("t1") or jump["t0"])
            elif G_DRIVES in fired and g_ids and drive not in g_ids:
                drive = g_ids[0]
            if not drive or drive not in store.drives:
                drive = (g_ids or [None])[0] or (list(store.drives) or [None])[-1]
            if not drive:
                return drive, empty_fig("No drives found."), [], ""
            if not chs:
                return drive, empty_fig("Pick at least one channel."), [], ""

            df = store.frame(drive)
            evs = events.detect(df, kinds or None) if kinds else []
            hide = None
            err = None
            if "filtered" in (opts or []):
                hide, err = filters.mask(df, keys, query)
                hide = hide.to_numpy()
            fig = build_figure(df, chs, evs, "shade" in (opts or []), hide, rng)
            labels = {k: lbl for k, (lbl, _) in events.DETECTORS.items()}
            data = [dict(id=i, at=mmss(e.start_s), kind=labels.get(e.kind, e.kind),
                         len=f"{e.end_s - e.start_s:.1f} s", detail=e.detail, sev=e.severity,
                         t0=e.start_s, t1=e.end_s) for i, e in enumerate(evs)]
            info = store.drives[drive]
            msg = [html.B(info.label), html.Span(f"  ·  {info.id}", className="muted"),
                   html.Span(f"  ·  {len(evs)} events")]
            if not info.start_ok:
                msg.append(html.Span("  ·  start time unverified (Pi clock never synced)", className="muted"))
            if err:
                msg.append(html.Span(err, className="err"))
            return drive, fig, data, msg
