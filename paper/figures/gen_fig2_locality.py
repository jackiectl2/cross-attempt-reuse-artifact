"""Fig. 2 panels (titles live in the LaTeX subcaptions), potential tier: (a) one point per task and weight: the share
of the task's isolated DB-execution time (and of its payload bytes) whose exact repeat occurred earlier in the same
session (x) against the share whose repeat occurred in another attempt of the same task and model (y), means over 200
random attempt orders (results/analysis_dab_r3/B1_per_task.csv); (b) the pooled share of isolated DB-execution time in
both scopes under different conditions (fidelity gates, models, engines, run outcome, time weight, removal of the
heaviest queries, an earlier replay, leave-one-dataset-out range), with the printed values."""
import csv
import os
import statistics

from plot_style import C, FULL_W, RES, load, plt, save

H = 2.3

# (a) per task
rows = list(csv.DictReader(open(os.path.join(RES, "analysis_dab_r3", "B1_per_task.csv"))))
fig, ax = plt.subplots(figsize=(FULL_W * 0.325, H), layout="constrained")
lim = 100.0
ax.plot([0, lim], [0, lim], ls="--", lw=0.6, color=C["gray"])
ax.plot([0, lim / 3], [0, lim], ls=":", lw=0.7, color=C["gray"])
ax.text(54, 50, "equal", fontsize=6, color=C["gray"], ha="left", va="top")
ax.text(31.5, 84, r"3$\times$", fontsize=6, color=C["gray"], ha="left", va="center")
for w, col, mk, fill, lab, mmk in (("exec_s", C["blue"], "o", C["blue"], "DB time", "P"),
                                   ("payload_bytes", C["orange"], "^", "none", "bytes", "X")):
    pts = [(float(r["session"]) * 100, float(r["trials"]) * 100) for r in rows if r["weight"] == w]
    assert len(pts) == 54
    ax.plot([p[0] for p in pts], [p[1] for p in pts], mk, ms=3.0, mfc=fill, mec=col, mew=0.6, ls="none", alpha=0.9,
            label=lab, zorder=3 if w == "exec_s" else 2)
    ax.plot([statistics.median(p[0] for p in pts)], [statistics.median(p[1] for p in pts)], mmk, ms=5.2, mfc=col,
            mec="black", mew=0.5, ls="none", label="median", zorder=4)
ax.set_xlim(-1.5, 62)
ax.set_ylim(-2, 100)
ax.set_xlabel("repeated within the session (% of task)")
ax.set_ylabel("repeated across attempts (% of task)")
ax.legend(loc="lower left", bbox_to_anchor=(-0.08, 1.0), ncol=4, handlelength=0.8, columnspacing=0.7, borderaxespad=0.0,
          handletextpad=0.2, fontsize=6.4)
save(fig, "fig2a_pertask")

# (b) conditions
b1f = load("analysis_dab_r3", "results_B1.json")
b1 = b1f["B1_fidelity_equiv_gate"]
exs = load("extra_analyses.json")["sensitivity"]["drop_top1pct"]["exec_s_shares"]
m1 = load("analysis_m1", "results_B1.json")["B1_fidelity_gate"]["overall"]["exec_s"]


def pair(d):
    return d["trials"]["mean"] * 100, d["session"]["mean"] * 100


GROUPS = [
    [("All oracle-admitted calls", pair(b1["overall"]["exec_s"])),
     ("Byte-identical replays only", pair(b1f["B1_strict_fidelity"]["exec_s"])),
     ("All replayed calls (no fidelity gate)", pair(b1f["B1_all_replayed"]["exec_s"]))],
    [(lab, pair(b1["by_model"][m]["exec_s"])) for m, lab in (
        ("gemini-2.5-flash", "Gemini-2.5-Flash"), ("gemini-3-pro", "Gemini-3-Pro"), ("gpt-5-mini", "GPT-5-mini"),
        ("gpt-5.2", "GPT-5.2"), ("kimi-k2", "Kimi-K2"))],
    [(lab, pair(b1["by_engine"][e]["exec_s"])) for e, lab in (
        ("duckdb", "DuckDB"), ("mongo", "MongoDB"), ("postgres", "PostgreSQL"), ("sqlite", "SQLite"))],
    [("Passed runs only", pair(b1["by_validation"]["pass"]["exec_s"])),
     ("Failed runs only", pair(b1["by_validation"]["fail"]["exec_s"])),
     ("First execution as weight", pair(b1["overall"]["exec_s_first"])),
     ("Top 1% of reusable queries removed", (exs["trials"] * 100, exs["session"] * 100)),
     ("Earlier replay, newer libraries", pair(m1)),
     ("Leave one dataset out (range)", None)],
]
lodo = b1["lodo"].values()
lo = [v["exec_s"]["trials"]["mean"] * 100 for v in lodo]
ls = [v["exec_s"]["session"]["mean"] * 100 for v in lodo]

fig, ax = plt.subplots(figsize=(FULL_W * 0.655, H), layout="constrained")
y, yt, yl = 0.0, [], []
XT_O, XT_S = 60.0, 71.0                      # x positions (data units) of the two value columns
for gi, g in enumerate(GROUPS):
    for lab, v in g:
        if v is None:                        # leave-one-dataset-out: the range over the twelve folds
            ax.plot([min(lo), max(lo)], [y, y], lw=2.6, color=C["blue"], solid_capstyle="butt", zorder=2)
            ax.plot([min(ls), max(ls)], [y, y], lw=2.6, color=C["gray"], solid_capstyle="butt", zorder=2)
            to, ts = f"{min(lo):.1f}\u2013{max(lo):.1f}", f"{min(ls):.1f}\u2013{max(ls):.1f}"
        else:
            o, s = v
            ax.plot([s, o], [y, y], lw=0.7, color=C["gray"], zorder=1)
            ax.plot([o], [y], "o", ms=3.6, color=C["blue"], zorder=2)
            ax.plot([s], [y], "o", ms=3.6, mfc="white", mec=C["gray"], mew=0.9, zorder=2)
            to, ts = f"{o:.1f}", f"{s:.1f}"
        ax.text(XT_O, y, to, ha="right", va="center", fontsize=6.4, color=C["blue"])
        ax.text(XT_S, y, ts, ha="right", va="center", fontsize=6.4)
        yt.append(y)
        yl.append(lab)
        y += 1
    if gi < len(GROUPS) - 1:
        ax.axhline(y - 0.25, lw=0.3, color=C["gray"])
        y += 0.5
ax.text(XT_O, -1.15, "attempts", ha="right", va="center", fontsize=6.4, color=C["blue"])
ax.text(XT_S, -1.15, "session", ha="right", va="center", fontsize=6.4)
ax.set_yticks(yt)
ax.set_yticklabels(yl, fontsize=6.6)
ax.tick_params(axis="y", length=0)
ax.set_ylim(y - 0.3, -1.7)
ax.set_xlim(-1.5, 72)
ax.set_xticks([0, 10, 20, 30, 40, 50])
ax.spines["bottom"].set_bounds(0, 50)
ax.set_xlabel("pooled share of isolated DB-execution time repeated (%)", x=0.35)
ax.plot([], [], "o", ms=3.6, color=C["blue"], label="across attempts")
ax.plot([], [], "o", ms=3.6, mfc="white", mec=C["gray"], mew=0.9, label="within session")
ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=2, handlelength=1.0, columnspacing=1.0, borderaxespad=0.0,
          handletextpad=0.3)
save(fig, "fig2b_conditions")
