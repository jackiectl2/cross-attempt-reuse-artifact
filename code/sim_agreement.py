"""Simulator-vs-live agreement on relative quantities (savings vs S0, S3/S2 ratios)."""
import sys

import numpy as np
import pandas as pd

for c in ("B", "E"):
    df = pd.read_csv(f"{sys.argv[1]}/validate_{c}.csv")
    df["signed"] = (df.pred_time - df.live_time) / df.live_time
    print(c, "signed time error, median by system:", df.groupby("system").signed.median().round(3).to_dict())
    p = df.groupby(["task", "budget", "system"])[["live_time", "pred_time", "live_written", "pred_written"]].sum().unstack("system")
    rows = []
    for idx, r in p.iterrows():
        for s in ("S1", "S2", "S3"):
            lv = 1 - r[("live_time", s)] / r[("live_time", "S0")] if r[("live_time", "S0")] > 0 else np.nan
            pv = 1 - r[("pred_time", s)] / r[("pred_time", "S0")] if r[("pred_time", "S0")] > 0 else np.nan
            rows.append({"task": idx[0], "budget": idx[1], "system": s, "live_saving": lv, "pred_saving": pv})
    sv = pd.DataFrame(rows)
    sv["abs_diff"] = (sv.live_saving - sv.pred_saving).abs()
    print(c, "time-saving agreement, median |live - pred| by system:", sv.groupby("system").abs_diff.median().round(3).to_dict())
    print(c, "median live saving vs pred saving (S2):", round(sv[sv.system == "S2"].live_saving.median(), 3), round(sv[sv.system == "S2"].pred_saving.median(), 3))
    lw = p[("live_written", "S3")] / p[("live_written", "S2")].replace(0, np.nan)
    pw = p[("pred_written", "S3")] / p[("pred_written", "S2")].replace(0, np.nan)
    lt = p[("live_time", "S3")] / p[("live_time", "S2")]
    pt = p[("pred_time", "S3")] / p[("pred_time", "S2")]
    print(c, "S3/S2 written live/pred median:", round(lw.median(), 3), round(pw.median(), 3), "| S3/S2 time live/pred median:", round(lt.median(), 3), round(pt.median(), 3))

# Totals over the calibration subset (time-weighted), using the uncalibrated simulator (measured op costs only)
for c in ("B", "E"):
    df = pd.read_csv(f"{sys.argv[1]}/validate_{c}.csv")
    for b, g in df.groupby("budget"):
        t = g.groupby("system")[["live_time", "pred_time0", "live_written", "pred_written"]].sum()
        out = {s: {"live_time_saving": round(1 - t.live_time[s] / t.live_time["S0"], 3),
                   "pred_time_saving": round(1 - t.pred_time0[s] / t.pred_time0["S0"], 3),
                   "live_saved_s": round(t.live_time["S0"] - t.live_time[s], 2),
                   "pred_saved_s": round(t.pred_time0["S0"] - t.pred_time0[s], 2)} for s in ("S1", "S2", "S3")}
        print(c, b, "totals:", out, "| S3/S2 written live/pred:",
              round(t.live_written["S3"] / max(t.live_written["S2"], 1), 3), round(t.pred_written["S3"] / max(t.pred_written["S2"], 1), 3),
              "| S3/S2 time live/pred:", round(t.live_time["S3"] / t.live_time["S2"], 3), round(t.pred_time0["S3"] / t.pred_time0["S2"], 3))

# Per-task savings (live vs simulated with measured op costs only), written next to the inputs
rows = []
for c in ("B", "E"):
    df = pd.read_csv(f"{sys.argv[1]}/validate_{c}.csv")
    for (task, b), g in df.groupby(["task", "budget"]):
        t = g.groupby("system")[["live_time", "pred_time0", "live_written", "pred_written"]].sum()
        if t.live_time["S0"] <= 0 or t.pred_time0["S0"] <= 0:
            continue
        rows.append({"contract": c, "task": task, "budget": b, "S0_live_s": t.live_time["S0"], "S0_pred_s": t.pred_time0["S0"],
                     **{f"{s}_{w}_saving": 1 - t[f"{w}_time" if w == "live" else "pred_time0"][s] / t[f"{w}_time" if w == "live" else "pred_time0"]["S0"]
                        for s in ("S1", "S2", "S3") for w in ("live", "pred")},
                     "S3_over_S2_written_live": t.live_written["S3"] / t.live_written["S2"] if t.live_written["S2"] else np.nan,
                     "S3_over_S2_written_pred": t.pred_written["S3"] / t.pred_written["S2"] if t.pred_written["S2"] else np.nan})
pt = pd.DataFrame(rows)
pt.to_csv(f"{sys.argv[1]}/pertask_agreement.csv", index=False)
for (c, b), g in pt.groupby(["contract", "budget"]):
    heavy = g.S0_live_s >= 5.0
    print(c, b, "tasks", len(g), "| S3 >= S2 live saving:", int((g.S3_live_saving >= g.S2_live_saving).sum()),
          "| heavy (S0 live >= 5 s) |live-pred| S2 saving:", g[heavy].eval("abs(S2_live_saving - S2_pred_saving)").round(3).tolist(),
          "| cheap median live/pred S2 saving:", round(g[~heavy].S2_live_saving.median(), 3), round(g[~heavy].S2_pred_saving.median(), 3))
