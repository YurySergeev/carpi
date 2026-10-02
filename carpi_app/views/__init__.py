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
import hashlib
import importlib
import json

import plotly.graph_objects as go
from dash import Input, Output, State, ctx, dcc, html, no_update

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


def lazy_callback(app, view_id, *deps, **kw):
    """Drop-in for @app.callback on a view's main draw function.

    Only runs while the view's tab is showing, and skips the work when you come back to a tab whose
    inputs haven't changed (the figure is still there). Everything below the tabs would otherwise
    recompute on every sidebar change, which is slow on a small hosted CPU. The "last drawn" signature
    lives in a per-browser dcc.Store, so viewers never interfere with each other."""
    outs = [d for d in deps if isinstance(d, Output)]
    ins = [d for d in deps if isinstance(d, Input)]
    sts = [d for d in deps if isinstance(d, State)]
    sig_id = f"{view_id}-sig"
    skip = (no_update,) * (len(outs) + 1)

    def deco(fn):
        @app.callback(*outs, Output(sig_id, "data"), *ins, Input(TABS, "value"), *sts, State(sig_id, "data"), **kw)
        def wrapped(*args):
            a_in, tab, a_st, last = args[:len(ins)], args[len(ins)], args[len(ins) + 1:-1], args[-1]
            if tab != view_id:
                return skip
            sig = hashlib.md5(json.dumps(a_in, default=str, sort_keys=True).encode()).hexdigest()
            if ctx.triggered_id == TABS and sig == last:
                return skip
            res = fn(*a_in, *a_st)
            res = res if isinstance(res, tuple) else (res,)
            return (*res, sig)
        return wrapped
    return deco


def sig_stores():
    return [dcc.Store(id=f"{v.id}-sig") for v in VIEWS]


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


for _m in ("timeseries", "scatter", "compare", "map3d", "guide"):
    importlib.import_module(f"{__name__}.{_m}")
