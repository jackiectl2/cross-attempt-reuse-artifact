"""Fig. 1 panels (titles live in the LaTeX subcaptions): (a) pooled share of calls / isolated DB time / payload bytes
whose exact repeat happened earlier within the session vs in another attempt of the same task and model, task medians,
and the part in the B-eval subset; (b) median-task DB-execution saving vs N (oracle gate, unbounded) for the session
memo (S1) and the cache shared by a batch of N attempts (S2) with task-bootstrap 95% CIs; (c) share of the cross-attempt reusable DB time and bytes each
admission tier admits, and the share of the calls that are not order-ambiguous (all calls except the successful ones
with at least two distinct rows and no top-level ORDER BY: a conservative upper bound for a rule that guarantees the
serialized bytes across query plans in the row order the engine returns)."""
import numpy as np

from plot_style import C, FULL_W, load, plt, save

b1 = load("analysis_dab_r3", "results_B1.json")["B1_fidelity_equiv_gate"]
b2 = load("analysis_dab_r3", "results_B23.json")["B2_fidelity_gate"]
cs = load("claim_support.json")["b_share"]["other_attempts"]
ss = load("static_share.json")
H = 1.62

# (a)
fig, ax = plt.subplots(figsize=(FULL_W * 0.37, H))
W = [("calls", "Calls"), ("exec_s", "DB time"), ("payload_bytes", "Bytes")]
d = 0.27
for i, (w, _) in enumerate(W):
    ov = b1["overall"][w]
    ses, oth = ov["session"]["mean"] * 100, ov["trials"]["mean"] * 100
    mt = (ov["models"]["mean"] + ov["tasks"]["mean"]) * 100
    tm_s, tm_o = b1["task_macro"][w]["session"]["median"] * 100, b1["task_macro"][w]["trials"]["median"] * 100
    yo, ys, ym = i - d, i, i + d
    ax.barh(yo, oth, 0.25, color=C["blue"], label="across attempts" if i == 0 else None)
    ax.barh(yo, cs[w]["admitted_by_B"] * oth, 0.25, color="white", edgecolor=C["blue"], hatch="//////", lw=0,
            label="  part in B-eval" if i == 0 else None)
    ax.barh(ys, ses, 0.25, color=C["gray"], label="within session" if i == 0 else None)
    ax.barh(ym, mt, 0.25, color=C["sky"], label="other model/task" if i == 0 else None)
    ax.plot([tm_o], [yo], "D", ms=2.6, color="black")
    ax.plot([tm_s], [ys], "D", ms=2.6, color="black")
    ax.text(oth * 0.55, yo, f"{oth:.1f}%", va="center", ha="center", fontsize=6.4, color="white")
    ax.text(ses + 1.2, ys, f"{ses:.1f}%", va="center", fontsize=6.4)
    ax.text(mt + 1.2, ym, f"{mt:.1f}%", va="center", fontsize=6.4)
ax.set_yticks(range(len(W)))
ax.set_yticklabels([lab for _, lab in W])
ax.invert_yaxis()
ax.set_xlim(0, 105)
ax.set_xlabel("share repeated from an earlier call (%)")
ax.legend(loc="lower right", handlelength=1.1, borderaxespad=0.0, labelspacing=0.2, fontsize=6.2)
save(fig, "fig1a_scope")

# (b)
fig, ax = plt.subplots(figsize=(FULL_W * 0.31, H))
Ns = [1, 2, 4, 8, 16, 50]
for s, col, lab in (("S2", C["blue"], "batch cache (S2)"), ("S1", C["gray"], "session memo (S1)")):
    st = [b2[f"N={n}|B=inf"][f"{s}_exec_saving"] for n in Ns]
    ax.fill_between(Ns, [x["median_ci"][0] * 100 for x in st], [x["median_ci"][1] * 100 for x in st], color=col,
                    alpha=0.22, lw=0)
    ax.plot(Ns, [x["median"] * 100 for x in st], marker="o", color=col, label=lab)
for n in (8, 50):
    v = b2[f"N={n}|B=inf"]["S2_exec_saving"]["median"] * 100
    ax.annotate(f"{v:.1f}%", (n, v), textcoords="offset points", xytext=(-16, 5), fontsize=6.8)
ax.set_xscale("log")
ax.set_xticks(Ns)
ax.set_xticklabels([str(n) for n in Ns])
ax.minorticks_off()
ax.set_ylim(0, 60)
ax.set_xlabel("attempts per task and model, N")
ax.set_ylabel("DB-time saving, median task (%)")
ax.legend(loc="upper left", handlelength=1.4, borderaxespad=0.0)
save(fig, "fig1b_ncurve")

# (c)
fig, ax = plt.subplots(figsize=(FULL_W * 0.31, H))
tiers = [("B", cs["exec_s"]["admitted_by_B"], cs["payload_bytes"]["admitted_by_B"], "B-eval"),
         ("O", ss["exec_s"]["admitted_by_order_fixed"], ss["payload_bytes"]["admitted_by_order_fixed"], "not order-\nambiguous"),
         ("E", cs["exec_s"]["admitted_by_E_eval"], cs["payload_bytes"]["admitted_by_E_eval"], "E-eval"),
         ("F", 1.0, 1.0, "oracle")]
x = np.arange(len(tiers))
t_share = [t * 100 for _, t, _, _ in tiers]
b_share = [b * 100 for _, _, b, _ in tiers]
ax.bar(x - 0.19, t_share, 0.36, color=C["blue"], label="reusable DB time")
ax.bar(x + 0.19, b_share, 0.36, color=C["orange"], edgecolor="white", hatch="////", lw=0, label="reusable bytes")
def lab(v):
    return "<0.001" if v < 0.001 else "100" if v >= 99.95 else f"{v:.1f}"


for xi, v in zip(x, t_share):
    ax.text(xi - 0.19, v + 2, lab(v), ha="center", fontsize=5.8)
for xi, v in zip(x, b_share):
    if v < 0.001:
        ax.text(xi + 0.03, v + 2, lab(v), ha="left", fontsize=5.8)
    else:
        ax.text(xi + 0.19, v + 2, lab(v), ha="center", fontsize=5.8)
ax.set_xticks(x)
ax.set_xticklabels([lab for _, _, _, lab in tiers])
ax.set_ylim(0, 112)
ax.set_yticks([0, 25, 50, 75, 100])
ax.set_ylabel("share admitted (%)")
ax.legend(loc="lower left", bbox_to_anchor=(0.0, 1.0), ncol=2, handlelength=1.1, columnspacing=0.8,
          borderaxespad=0.0)
save(fig, "fig1c_tiers")
