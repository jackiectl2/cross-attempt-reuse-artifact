"""Fig. 3 panels (titles live in the LaTeX subcaptions): (a) the admission funnel of Table 7 per engine: the share of
each engine's database calls that passes each cumulative gate (results/analysis_dab_r3/B0_funnel.csv, level engine);
(b) the verdict of the static Contract B rule on the 41,616 distinct (database, query) pairs, by reason
(results/analysis_dab_r3/B0_cert_reasons.csv), with the printed counts."""
import csv
import os

from plot_style import C, COL_W, RES, plt, save

A3 = os.path.join(RES, "analysis_dab_r3")

# (a) funnel per engine
fun = {(r["gate"], r["group"]): float(r["calls_share"]) * 100
       for r in csv.DictReader(open(os.path.join(A3, "B0_funnel.csv"))) if r["level"] == "engine"}
ENG = [("duckdb", "DuckDB"), ("mongo", "MongoDB"), ("postgres", "PostgreSQL"), ("sqlite", "SQLite")]
GATES = [("successful", "replay succeeded", "#0b3c5d", None),
         ("fidelity_equiv", "equivalent to recorded (oracle)", C["blue"], None),
         ("stable", "byte-identical, stable", C["sky"], None),
         ("E_certified", "E-eval", "#bfe3f7", "////"),
         ("B_certified", "B-eval", C["vermilion"], None)]
fig, ax = plt.subplots(figsize=(COL_W, 1.25), layout="constrained")
bw = 0.16
for j, (g, lab, col, hatch) in enumerate(GATES):
    xs = [i + (j - 2) * bw for i in range(len(ENG))]
    ax.bar(xs, [fun[(g, e)] for e, _ in ENG], bw * 0.92, color=col, edgecolor="white" if hatch is None else C["blue"],
           hatch=hatch, lw=0 if hatch is None else 0.0, label=lab)
ax.set_xticks(range(len(ENG)))
ax.set_xticklabels([lab for _, lab in ENG])
ax.tick_params(axis="x", length=0)
ax.set_ylim(0, 100)
ax.set_yticks([0, 25, 50, 75, 100])
ax.set_ylabel("engine's calls (%)")
h, lb = ax.get_legend_handles_labels()
order = [0, 3, 1, 4, 2]                      # column-major order, so that the rows read in the order of the bars
ax.legend([h[i] for i in order], [lb[i] for i in order], loc="lower left", bbox_to_anchor=(-0.13, 1.0), ncol=3,
          handlelength=1.0, columnspacing=0.7, borderaxespad=0.0, handletextpad=0.3, labelspacing=0.2, fontsize=6.2)
save(fig, "fig3a_funnel_engine")

# (b) verdicts of the static B rule by distinct query
cnt = {r["cert_reason"]: int(r["count"]) for r in csv.DictReader(open(os.path.join(A3, "B0_cert_reasons.csv")))}
total = sum(cnt.values())
ADMIT = ("one_row", "total_order", "id_lookup")
NAMED = [("no ORDER BY naming every output", ("unordered_result",)),
         ("output not a stored column", ("output_not_a_stored_column",)),
         ("not a single SELECT block", ("nested_block", "not_one_select_block", "clause_not_allowed")),
         ("MongoDB find (cannot sort)", ("mongo_no_sort",)),
         ("predicate outside the whitelist", ("predicate_not_allowed",))]
bars = [(lab, sum(cnt[k] for k in ks), C["gray"]) for lab, ks in NAMED]
named = {k for _, ks in NAMED for k in ks}
bars.append(("other rejections", sum(v for k, v in cnt.items() if k not in named and k not in ADMIT), C["gray"]))
bars.append(("admitted", sum(cnt[k] for k in ADMIT), C["vermilion"]))
fig, ax = plt.subplots(figsize=(COL_W, 1.08), layout="constrained")
for i, (lab, v, col) in enumerate(bars):
    ax.barh(i, v / 1000, 0.68, color=col)
    ax.text(v / 1000 + 0.3, i, f"{v:,}", va="center", fontsize=6.4)
ax.set_yticks(range(len(bars)))
ax.set_yticklabels([b[0] for b in bars], fontsize=6.6)
ax.tick_params(axis="y", length=0)
ax.invert_yaxis()
ax.set_xlim(0, 22)
ax.set_xlabel("distinct (database, query) pairs (thousands)")
save(fig, "fig3b_b_verdicts")
print("total", total, "bars", [(b[0], b[1]) for b in bars])
