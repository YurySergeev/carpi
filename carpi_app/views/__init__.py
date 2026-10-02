"""Analysis views. Each view is one tab in the app.

To add a view, drop a module in this folder:

    from dash import html, dcc, Input, Output
    from . import View, register, G_DRIVES, G_FILTERS, G_QUERY

    @register
    class MyView(View):
        id, label, order = "myview", "My view", 50

        def layout(self, store):
            return html.Div([dcc.Graph(id="myview-graph")])

        def callbacks(self, app, store):
            @app.callback(Output("myview-graph", "figure"),
                          Input(G_DRIVES, "value"), Input(G_FILTERS, "value"), Input(G_QUERY, "value"))
            def draw(ids, keys, query):
                df, total, err = store.frames(ids, keys, query)
                ...

Then add it to the import list at the bottom of this file. Component ids must start with the view id.
To make clicks in your view open the time-series view at that moment, write
{"drive": <drive id>, "t0": <seconds>, "t1": <seconds>} to Output(JUMP, "data", allow_duplicate=True).
"""
import importlib

import plotly.graph_objects as go
from dash import html

# ids of the shared sidebar controls and stores
G_DRIVES, G_FILTERS, G_QUERY, G_STATUS = "g-drives", "g-filters", "g-query", "g-status"
JUMP, TABS = "g-jump", "g-tabs"

VIEWS = []


class View:
    id = "view"
    label = "View"
    order = 100

    def layout(self, store):
        raise NotImplementedError

    def callbacks(self, app, store):
        pass


def register(cls):
    VIEWS.append(cls())
    VIEWS.sort(key=lambda v: v.order)
    return cls


# ---- small shared helpers
def control(label, component, width=None, hint=None):
    style = {"width": width} if width else {}
    kids = [html.Label(label, className="ctl-label"), component]
    if hint:
        kids.append(html.Div(hint, className="ctl-hint"))
    return html.Div(kids, className="ctl", style=style)


def empty_fig(msg, height=420):
    fig = go.Figure()
    fig.add_annotation(text=msg, showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper",
                       font=dict(size=14, color="#898781"))
    fig.update_layout(height=height, xaxis_visible=False, yaxis_visible=False,
                      margin=dict(l=10, r=10, t=10, b=10))
    return fig


def note(total, shown, err=None, extra=""):
    bits = [f"{shown:,} of {total:,} rows"]
    if extra:
        bits.append(extra)
    out = [html.Span("  ·  ".join(bits))]
    if err:
        out.append(html.Span(err, className="err"))
    return out


for _m in ("timeseries", "scatter", "compare", "map3d"):
    importlib.import_module(f"{__name__}.{_m}")
