"""Plot helpers shared by views."""
import numpy as np
import pandas as pd

from .. import config


def colorscale_for(values):
    """Diverging around 0 when the data has both signs, otherwise one-hue sequential."""
    v = pd.Series(values).dropna()
    if len(v) and v.quantile(0.05) < 0 < v.quantile(0.95):
        lim = float(max(abs(v.quantile(0.005)), abs(v.quantile(0.995))))
        return dict(colorscale=config.DIVERGING, cmid=0, cmin=-lim, cmax=lim)
    return dict(colorscale=config.SEQUENTIAL)


def group_colors(store, df, by):
    """Stable colours: drives by their order in the sidebar selection, tags by first appearance."""
    if by == "tag":
        keys = store.tags()
    else:
        d = df["drive"]
        keys = list(d.cat.categories) if hasattr(d, "cat") else list(dict.fromkeys(d))
    return {k: (config.color(i), config.dash_style(i)) for i, k in enumerate(keys)}


def short_label(store, did):
    i = store.drives.get(did)
    if not i or not i.start:
        return did
    return pd.Timestamp(i.start).strftime("%b %d %H:%M") + ("" if i.start_ok else "?") + f" · {i.tag}"


def binned_median(x, y, nbins=30, min_n=10):
    ok = x.notna() & y.notna()
    x, y = x[ok], y[ok]
    if len(x) < min_n:
        return np.array([]), np.array([])
    edges = np.linspace(x.quantile(0.005), x.quantile(0.995), nbins + 1)
    idx = np.digitize(x, edges) - 1
    g = pd.DataFrame({"i": idx, "y": y.to_numpy()})
    g = g[(g.i >= 0) & (g.i < nbins)].groupby("i")["y"].agg(["median", "size"])
    g = g[g["size"] >= min_n]
    centers = (edges[:-1] + edges[1:]) / 2
    return centers[g.index.to_numpy()], g["median"].to_numpy()
