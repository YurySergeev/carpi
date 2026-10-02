"""Guide tab: a short how-to for people opening the hosted copy. The hosted copy opens on this tab."""
from dash import dcc, html

from . import View, register

GUIDE = """
### What this is

Real drive logs from a **2017 Audi A3** (8V, EA888 Gen 3, DQ250), logged by a Raspberry Pi on the OBD-II port at
about 7.7 samples a second. It's view-only, so click around freely.

### Quick start

1. **Pick drives** in the sidebar (below the charts on a phone). The 5 newest are selected to start; **All** selects every drive. A `?` after a time means the
   Pi's clock hadn't synced, so that start time is a guess.
2. **Optional: filter rows** with the sidebar boxes (warm engine, idle, under boost…) or the **Query** box, e.g.
   `rpm > 3000 and coolant_c >= 80`, then press Enter.
3. **Pick a tab** above.

In any chart: **drag or scroll to zoom, double-click to reset, hover to read values**. Click a legend name to hide a line.

### The tabs

| Tab | What it does | Try this |
| --- | --- | --- |
| **Time series** | One drive, channels stacked, one crosshair across all of them. The **Events** table lists hard accelerations, timing dips under boost, lean spots, low voltage, high trims and long idles. | Click an event row to zoom straight to it |
| **X vs Y** | Any channel against any other, across every selected drive | Click a dot to jump to that moment in Time series |
| **Compare drives** | One channel: trend per drive, box per drive, histogram per tag, or overlaid traces | Total fuel trim, Trend, with the Idle filter |
| **3-axis map** | ECU-style table: bins X × Y and shows the mean, max, etc. of Z, or A − B between two tags | Timing by rpm × load, Warm engine filter |

### Three quick checks

- **Vacuum leak / PCV:** tick *Warm engine*, *Closed loop* and *Idle*, then Compare drives → *Total fuel trim* → *Trend*.
  Then swap *Idle* for *Steady cruise*. This car adds about +7.5% fuel at idle but about 0% at cruise, the classic
  small-leak pattern.
- **Timing under boost:** tick *Under boost* and *Exclude DSG shift moments*, then X vs Y with rpm against
  *Ignition timing*, coloured by *Intake air temp*. Standard OBD can't separate knock retard from a planned low value.
- **Fueling at load:** Time series → click a *Hard acceleration* event and check that *Lambda actual* tracks *Lambda commanded*.

### Good to know

- **Standard OBD-II only:** no per-cylinder knock, oil temp/pressure or DSG data yet.
- **DSG upshifts under load** cut timing to about −25° and spike λ for ~0.3 s. That's normal torque reduction, and the
  detectors ignore it.
- **The pedal PID tops out around 60%** on this car. About 86% throttle is wide open.
- **Smart charging:** 12.5–13 V while cruising can be normal. Look for 14.3 V+ spikes when coasting.
- **Slow channels** (temps, voltage, long-term trim, fuel level) update about every 1.6 s. Everything else updates every 0.13 s.
- **The first visit after a quiet spell** takes about a minute while the free server wakes up.
"""


@register
class Guide(View):
    id, label, order = "guide", "Guide", 90

    def layout(self, store):
        return html.Div(dcc.Markdown(GUIDE, className="guide-md"), className="guide-pane")
