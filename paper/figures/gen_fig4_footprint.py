"""Fig. 4 (b, c): peak footprint relative to no sharing (S0) for private copies (S2), copy-on-write clones (S2c) and
the shared read-only store (S3), E-eval subset, median task, 50 resamples. Solid: outputs retained to batch end (S3
references held to batch end, results/ablate/batchhold_E.json); dashed: sequential retries (results/ablate/base_E.json).
(b) unbounded budget across N, with the live S3/S0 values of the calibration cells that have admitted spilled bytes
(results/live2/calib_E_*.json, median over 5 resamples per cell); (c) N = 8 across budgets."""
import glob
import json
import os

import numpy as np

from plot_style import C, COL_W, RES, load, plt, save

bh, bs = load("ablate", "batchhold_E.json")["summary"], load("ablate", "base_E.json")["summary"]
STY = {"S2": (C["vermilion"], "s", "private copy (S2)"), "S2c": (C["green"], "^", "CoW clone (S2c)"),
       "S3": (C["blue"], "o", "shared read-only store (S3)")}
H, PW = 1.45, 1.62

live = []
for f in sorted(glob.glob(os.path.join(RES, "live2", "calib_E_*.json"))):
    cell = json.load(open(f))
    rows = [r for r in cell["results"] if r["budget"] == "inf"]
    s0 = np.median([r["peak_retained_a"] for r in rows if r["system"] == "S0"])
    s3 = np.median([r["peak_retained_a"] for r in rows if r["system"] == "S3"])
    if s0 > 0:
        live.append(s3 / s0)

fig, ax = plt.subplots(figsize=(PW, H))
Ns = [2, 4, 8]
for s, (col, mk, lab) in STY.items():
    ax.plot(Ns, [bh[f"N={n}|B=inf"][f"{s}_over_S0_peak_foot"] for n in Ns], color=col, marker=mk, ls="-",
            mfc=col if s != "S2c" else "white", ms=4 if s == "S2c" else 3.5, label=lab)
    ax.plot(Ns, [bs[f"N={n}|B=inf"][f"{s}_over_S0_peak_foot_seq"] for n in Ns], color=col, marker=mk, ls="--",
            mfc="white", ms=4 if s == "S2c" else 3.5)
rng = np.random.default_rng(0)
ax.scatter(8 + rng.uniform(-0.35, 0.35, len(live)), live, s=9, facecolor=C["blue"], edgecolor="black", lw=0.4, alpha=0.8,
           label=f"S3 live, {len(live)} cells")
ax.axhline(1.0, color="black", lw=0.6, ls=":")
ax.text(5.5, 1.05, "S0", fontsize=6.5, ha="center")
ax.set_xticks(Ns)
ax.set_xlim(1.5, 8.8)
ax.set_ylim(0, 2.8)
ax.set_xlabel("attempts N (unbounded)")
ax.set_ylabel("peak footprint / S0")
save(fig, "fig4b_footprint_N")
handles, labels = ax.get_legend_handles_labels()

fig, ax = plt.subplots(figsize=(PW, H))
B = ["inf", "1", "1/2", "1/4", "1/8", "1/16"]
xs = np.arange(len(B))
for s, (col, mk, lab) in STY.items():
    ax.plot(xs, [bh[f"N=8|B={b}"][f"{s}_over_S0_peak_foot"] for b in B], color=col, marker=mk, ls="-",
            mfc=col if s != "S2c" else "white", ms=4 if s == "S2c" else 3.5)
    ax.plot(xs, [bs[f"N=8|B={b}"][f"{s}_over_S0_peak_foot_seq"] for b in B], color=col, marker=mk, ls="--",
            mfc="white", ms=4 if s == "S2c" else 3.5)
ax.axhline(1.0, color="black", lw=0.6, ls=":")
ax.set_xticks(xs)
ax.set_xticklabels(["∞", "1", "1/2", "1/4", "1/8", "1/16"])
ax.set_ylim(0, 2.8)
ax.set_xlabel("budget (N = 8)")
save(fig, "fig4c_footprint_budget")

from matplotlib.lines import Line2D  # noqa: E402
extra = [Line2D([], [], color="black", ls="-", lw=1.1, label="retained to batch end"),
         Line2D([], [], color="black", ls="--", lw=1.1, label="sequential retries")]
fig = plt.figure(figsize=(COL_W, 0.42))
fig.legend(handles + extra, labels + [h.get_label() for h in extra], loc="center", ncol=2, handlelength=1.8,
           columnspacing=1.0, borderaxespad=0.0, fontsize=6.4)
save(fig, "fig4_legend")
