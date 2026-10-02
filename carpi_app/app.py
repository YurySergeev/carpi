"""CarPi Analyzer - run with:  python -m carpi_app   (from the CarPi folder)"""
import argparse
import threading
import webbrowser

from dash import Dash, Input, Output, State, ctx, dcc, html, no_update

from . import __version__, config, filters
from .examples import EXAMPLES
from .data import Store
from .views import G_DRIVES, G_FILTERS, G_QUERY, G_STATUS, JUMP, TABS, VIEWS, sig_stores

QUERY_HELP = ("Press Enter to apply. Any column, e.g.  rpm > 3000 and coolant_c >= 80   \u00b7   "
              "abs(lambda_err) > 0.05   \u00b7   iat_c.between(30, 45)   \u00b7   2000 < rpm < 3000")

def sidebar(store):
    newest = [o["value"] for o in store.options()[:5]]
    return html.Aside([
        html.Div([html.Span("CarPi", className="brand"), html.Span(" Analyzer", className="brand2")],
                 className="title"),
        *([html.P(config.ABOUT, className="about")] if config.PUBLIC and config.ABOUT else []),
        *([html.P(["New here? Start with the ", html.B("Guide"), " tab."], className="ctl-hint")]
          if config.PUBLIC else []),
        dcc.Dropdown(id="g-example", placeholder="Open an example\u2026", className="ex-pick", searchable=False,
                     options=[{"label": e["title"], "value": e["id"]} for e in EXAMPLES]),
        html.Div(id=G_STATUS, className="muted small"),
        html.Label("Drives", className="ctl-label"),
        dcc.Dropdown(id=G_DRIVES, options=store.options(), value=newest, multi=True,
                     placeholder="Pick drives…", optionHeight=34, maxHeight=420),
        html.Div([
            html.Button("Newest", id="g-newest", className="btn sm"),
            html.Button("All", id="g-all", className="btn sm"),
            html.Button("Clear", id="g-clear", className="btn sm"),
            dcc.Dropdown(id="g-tag", options=[{"label": f"All '{t}'", "value": t} for t in store.tags()],
                         placeholder="By tag…", clearable=True, style={"minWidth": "120px", "flex": 1}),
        ], className="btnrow"),
        html.Div("? after a time = Pi clock wasn't synced, so the start time is a guess.", className="ctl-hint"),
        html.Label("Filters (all must match)", className="ctl-label"),
        dcc.Checklist(id=G_FILTERS, options=[{"label": f" {lbl}", "value": k} for k, (lbl, _) in filters.FILTERS.items()],
                      value=["running"], className="checks col"),
        html.Label("Query", className="ctl-label"),
        dcc.Input(id=G_QUERY, type="text", debounce=True, placeholder="e.g. rpm > 3000 and coolant_c >= 80",
                  className="query"),
        html.Div(id="g-query-msg", className="query-msg"),
        html.Div(QUERY_HELP, className="ctl-hint"),
        *([] if config.PUBLIC else [
            html.Button("Rescan log folders", id="g-refresh", className="btn"),
            html.Div(", ".join(str(d) for d in store.dirs), className="ctl-hint mono")]),
    ], className="sidebar")


def create_app(store):
    # Public mode loads Dash/Plotly JavaScript from a CDN so a small hosted server only serves data.
    app = Dash(__name__, title="CarPi Analyzer", suppress_callback_exceptions=True,
               serve_locally=not config.PUBLIC,
               update_title=None,
               meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}])
    ids = [v.id for v in VIEWS]
    start_tab = "guide" if config.PUBLIC and "guide" in ids else next(i for i in ids if i != "guide")
    app.layout = html.Div([
        dcc.Store(id=JUMP),
        dcc.Location(id="g-url", refresh=False),
        *sig_stores(),
        sidebar(store),
        html.Main([
            dcc.Tabs(id=TABS, value=start_tab, className="tabs", mobile_breakpoint=0, children=[
                dcc.Tab(label=v.label, value=v.id, className="tab", selected_className="tab--on",
                        children=html.Div(v.layout(store), className="pane"))
                for v in VIEWS]),
        ], className="main"),
    ], className="shell")

    @app.callback(Output(G_DRIVES, "value"),
                  Input("g-newest", "n_clicks"), Input("g-all", "n_clicks"), Input("g-clear", "n_clicks"),
                  Input("g-tag", "value"), State(G_DRIVES, "value"), prevent_initial_call=True)
    def quick(_a, _b, _c, tag, cur):
        t = ctx.triggered_id
        if t == "g-newest":
            return [o["value"] for o in store.options()[:5]]
        if t == "g-all":
            return [o["value"] for o in store.options()]
        if t == "g-clear":
            return []
        if t == "g-tag" and tag:
            return [i.id for i in reversed(list(store.drives.values())) if i.tag == tag]
        return no_update

    if not config.PUBLIC:
        @app.callback(Output(G_DRIVES, "options"), Output("ts-drive", "options"), Output("g-tag", "options"),
                      Input("g-refresh", "n_clicks"), prevent_initial_call=True)
        def rescan(_n):
            store.refresh()
            opts = store.options()
            return opts, opts, [{"label": f"All '{t}'", "value": t} for t in store.tags()]

    @app.callback(Output("g-query-msg", "children"), Input(G_QUERY, "value"))
    def check_query(q):
        if not (q or "").strip() or not store.drives:
            return ""
        sample = store.frame(list(store.drives)[-1]).head(50)
        _, err = filters.mask(sample, (), q)
        return html.Span(err, className="err-inline") if err else "\u2713 Query applied to every tab"

    @app.callback(Output(G_STATUS, "children"), Input(G_DRIVES, "value"), Input(G_DRIVES, "options"))
    def status(ids, _opts):
        sel = [store.drives[i] for i in ids or [] if i in store.drives]
        allv = list(store.drives.values())
        return (f"{len(sel)} of {len(allv)} drives selected · "
                f"{sum(i.distance_mi for i in sel):,.0f} mi · {sum(i.duration_min for i in sel) / 60:.1f} h")

    for v in VIEWS:
        v.callbacks(app, store)
    return app


def main(argv=None):
    ap = argparse.ArgumentParser(description="Interactive CarPi drive-log analyzer")
    ap.add_argument("--logs", action="append", help="folder to scan for drive-*.csv (repeatable; "
                                                    "default: ./logs and ./data in the CarPi folder)")
    ap.add_argument("--port", type=int, default=8050)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--debug", action="store_true", help="auto-reload on code changes + Dash dev tools")
    a = ap.parse_args(argv)

    print(f"CarPi Analyzer {__version__}: scanning logs…")
    store = Store(a.logs or config.LOG_DIRS)
    print(f"  {len(store.drives)} drives found")
    app = create_app(store)
    url = f"http://{a.host}:{a.port}/"
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"  open {url}  (Ctrl+C to stop)")
    app.run(host=a.host, port=a.port, debug=a.debug)


if __name__ == "__main__":
    main()
