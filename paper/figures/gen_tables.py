"""Tables 1-4 as LaTeX. Every cell is produced by an expression over files in results/ and recorded, with its printed
form, in paper/NUMBERS_LEDGER.csv (checked by paper/check_numbers.py). Standard library only."""
import csv
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ledger import ROOT, ev  # noqa: E402

OUT = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(ROOT, "paper", "NUMBERS_LEDGER.csv")
TABLE_PREFIXES = ("tab1/", "tab2/", "tab3/", "tab3c/", "tab4/", "tabw/", "tabd/", "tabt/", "fig2/", "tabp/")
rows = []


def cell(loc, expr):
    v = ev(expr)
    rows.append({"id": loc, "location": loc, "printed": v, "expr": expr})
    return v


def write(name, lines):
    with open(os.path.join(OUT, name), "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote", name)


# ---- Table 1: admission funnel -------------------------------------------------------------------------------------
F = "results/analysis_dab_r3/B0_funnel.csv"
GATES = [("successful", "Successful replay"),
         ("fidelity_equiv", "Equivalent to recorded observation (oracle)"),
         ("stable", "Byte-identical, stable in 3 replays"),
         ("E_certified", "E-eval subset (static E, post hoc)"),
         ("B_certified", "B-eval subset (static B, post hoc)")]
body = []
for g, label in GATES:
    r = "ROW('%s', gate='%s', level='all', group='all')" % (F, g)
    calls = cell("tab1/%s/calls" % g, "cnt(%s['calls'])" % r)
    share = cell("tab1/%s/calls_share" % g, "pct(%s['calls_share'])" % r)
    time = cell("tab1/%s/time" % g, "pct(%s['time_lb_share'])" % r)
    byts = cell("tab1/%s/bytes" % g, "pct(%s['bytes_share'], %d)" % (r, 3 if g == "B_certified" else 1))
    body.append("%s & %s & %s & %s & %s \\\\" % (label, calls, share, time, byts))
write("tab1_funnel.tex", ["\\begin{tabular}{@{}L{3.15cm}rrrr@{}}", "\\toprule",
                          "Gate (cumulative) & Calls & \\% calls & \\% DB time & \\% bytes \\\\", "\\midrule",
                          *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table 2: designs at N = 8 (E-eval subset, unbounded, median task, 50 resamples) -------------------------------
BE, BH = "results/ablate/base_E.json", "results/ablate/batchhold_E.json"
K = "['summary']['N=8|B=inf']"
copy_ns = cell("tab2/copy_ns_per_byte", "fx(J('%s')['costs']['copy_s_per_byte'] * 1e9)" % BE)      # as charged by the simulation
cell("tab2/copy_n", "cnt(J('results/live/costs.json')['n_samples']['copy_s'])")
cell("tab2/clone_n", "cnt(J('results/live3/costs.json')['n_samples']['clone_s'])")
cell("tab2/alias_n", "cnt(J('results/live/costs.json')['n_samples']['alias_s'])")
clone_ms = cell("tab2/clone_ms", "fx(J('%s')['opts']['clone_s'] * 1e3)" % BE)
alias_us = cell("tab2/alias_us", "fx(J('%s')['costs']['alias_s'] * 1e6, 1)" % BE)
DESIGNS = [("S1", "Session memo (S1)", "---"),
           ("SD", "Execution-only cache model (SD)", "---"),
           ("S2", "Private copy (S2)", "copy %s ns/B" % copy_ns),
           ("S2c", "CoW clone (S2c)", "clone %s ms" % clone_ms),
           ("S3", "Shared read-only store (S3)", "alias %s\\,\\textmu s" % alias_us)]
body = ["Isolated (S0) & 0.0 & 1.00 & 1.00 & 1.00 & --- & 0.0 \\\\"]
for s, label, cost in DESIGNS:
    x = cell("tab2/%s/exec_saving" % s, "pct(J('%s')%s['%s_exec_saving']['median'])" % (BE, K, s))
    t = cell("tab2/%s/time_saving" % s, "pct(J('%s')%s['%s_time_saving']['median'])" % (BE, K, s))
    w = cell("tab2/%s/written_vs_S0" % s, "fx(1 - J('%s')%s['%s_written_saving']['median'])" % (BE, K, s))
    fr = cell("tab2/%s/foot_retained" % s, "fx(J('%s')%s['%s_over_S0_peak_foot'])" % (BH, K, s))
    fs = cell("tab2/%s/foot_seq" % s, "fx(J('%s')%s['%s_over_S0_peak_foot_seq'])" % (BE, K, s))
    body.append("%s & %s & %s & %s & %s & %s & %s \\\\" % (label, x, w, fr, fs, cost, t))
write("tab2_designs.tex", ["\\begin{tabular}{@{}lrrrrlr@{}}", "\\toprule",
                           " & DB-time & Written & \\multicolumn{2}{c}{Footprint / S0} & Measured & Modeled tool-time \\\\",
                           "\\cmidrule(lr){4-5}",
                           "Design & saving \\% & / S0 & retained & sequential & primitive & saving \\% \\\\", "\\midrule",
                           *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table 3 (left): end-to-end equivalence, S3 ---------------------------------------------------------------------
DIAG = "results/e2e_trace/mismatch_diag.json"
PASSES = [("all", "summary_all_E.json", DIAG, "all_E", "All cells, $\\infty$"),
          ("rerun", "rerun_PATENTS/summary_all_E.json", "results/e2e_trace/rerun_PATENTS/mismatch_diag.json", "all_E",
           "PATENTS rerun, $\\infty$"),
          ("calib", "summary_calib_E.json", DIAG, "calib_E", "Calibration, $\\infty$, 1/4"),
          ("cow", "summary_cow_calib_E.json", DIAG, "cow_calib_E", "Calibration, CoW pass")]
body = []
for p, fn, diagf, diag, label in PASSES:
    s = "J('results/e2e_trace/%s')['S3']" % fn
    cells_ = cell("tab3/%s/cells" % p, "cnt(J('results/e2e_trace/%s')['cells'])" % fn)
    calls = cell("tab3/%s/calls" % p, "cnt(%s['calls'])" % s)
    hits = cell("tab3/%s/hits" % p, "cnt(%s['hits'])" % s)
    mhit = cell("tab3/%s/mismatched_hits" % p, "cnt(%s['mismatches_on_hits'])" % s)
    vdiff = cell("tab3/%s/value_on_hit" % p,
                 "cnt(sum(1 for d in J('%s')['%s']['details'] "
                 "if d['system'] == 'S3' and d['class'] == 'value_on_hit'))" % (diagf, diag))
    ok = cell("tab3/%s/cons_ok" % p, "cnt(%s['consumers_identical_ok'])" % s)
    fail = cell("tab3/%s/cons_fail" % p, "cnt(%s['consumers_identical_failed'])" % s)
    diff = cell("tab3/%s/cons_diff" % p, "cnt(%s['consumers'] - %s['consumers_identical'])" % (s, s))
    pr = cell("tab3/%s/probes_refused" % p, "cnt(%s['probes_refused'])" % s)
    pn = cell("tab3/%s/probes" % p, "cnt(%s['probes'])" % s)
    body.append("%s & %s & %s & %s & %s & %s & %s\\,/\\,%s\\,/\\,%s & %s\\,/\\,%s \\\\"
                % (label, cells_, calls, hits, mhit, vdiff, ok, fail, diff, pr, pn))
write("tab3_e2e.tex", ["\\begin{tabular}{@{}lrrrrrcc@{}}", "\\toprule",
                       "Pass (S3 vs.\\ S0) & Cells & Calls & Hits & Hits $\\neq$ S0 & Value mismatches & Consumers same ok\\,/\\,fail\\,/\\,diff & Probes refused\\,/\\,total \\\\",
                       "\\midrule", *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table 3 (right): live concurrency stress test ------------------------------------------------------------------
AGG = ("[r for f in sorted(__import__('glob').glob(__import__('os').path.join(ROOT_, 'results/concurrent_live/*.json'))) "
       "for r in J(__import__('os').path.relpath(f, ROOT_)) "
       "if r['budget'] == '%s' and r['policy'] == '%s' and r['pinning'] == %s]")
body = []
for b, blabel in (("inf", "$\\infty$"), ("1/16", "1/16")):
    for pol, pin, plabel in (("optimistic", True, "optimistic"), ("singleflight", True, "single-flight"),
                             ("optimistic", False, "optimistic, unpinned")):
        q = AGG % (b, pol, pin)
        key = "tab3c/%s/%s/%s" % (b, pol, "pin" if pin else "nopin")
        m = cell(key + "/mismatch", "cnt(sum(r['mismatches_vs_S0'] for r in %s))" % q)
        ex = cell(key + "/execs", "cnt(sum(r['execs'] for r in %s))" % q)
        du = cell(key + "/dup", "cnt(sum(r['dup_exec'] for r in %s))" % q)
        wt = cell(key + "/wait_s", "fx(sum(r['wait_s'] for r in %s), 0)" % q)
        dg = cell(key + "/dangling", "cnt(sum(r['dangling'] for r in %s))" % q)
        body.append("%s & %s & %s & %s & %s & %s & %s \\\\" % (blabel, plabel, m, ex, du, wt, dg))
write("tab3_concurrency.tex", ["\\begin{tabular}{@{}llrrrrr@{}}", "\\toprule",
                               "Budget & Policy & Mismatches & Executions & Duplicates & Wait (s) & Dangling aliases \\\\", "\\midrule",
                               *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table 4: cross-harness (trace level, calls) ---------------------------------------------------------------------
X, XD = "results/xharness/results.json", "results/xharness/results_dab_main5.json"
HARN = [("phoenix", "Phoenix-CLI / Opus 5"), ("qwen", "DAB scaffold / Qwen3.8"),
        ("scout", "Scout / GLM-5.2"), ("stela", "Stela / DeepSeek-v4.1")]
dab = json.load(open(os.path.join(ROOT, XD)))
NAMES = {"gemini-2.5-flash": "Gemini-2.5-Flash", "gemini-3-pro": "Gemini-3-Pro", "gpt-5-mini": "GPT-5-mini",
         "gpt-5.2": "GPT-5.2", "kimi-k2": "Kimi-K2"}
DABROWS = [(k, "DAB / " + NAMES[k.split(":", 1)[1]]) for k in sorted(dab)]
body = []
for path, items in ((X, HARN), (XD, DABROWS)):
    for h, label in items:
        base = "J('%s')['%s']" % (path, h)
        runs = cell("tab4/%s/runs" % h, "cnt(%s['runs'])" % base)
        calls = cell("tab4/%s/calls" % h, "cnt(%s['db_calls'])" % base)
        ses = cell("tab4/%s/session" % h, "pct(%s['scope_shares']['calls']['session'])" % base)
        oth = cell("tab4/%s/other" % h, "pct(%s['scope_shares']['calls']['other_attempts'])" % base)
        n5 = cell("tab4/%s/n5" % h, "pct(%s['reuse_curve']['5']['median_task_call_reuse'])" % base)
        nt = cell("tab4/%s/n5_tasks" % h, "cnt(%s['reuse_curve']['5']['tasks'])" % base)
        body.append("%s & %s & %s & %s & %s & %s (%s) \\\\" % (label, runs, calls, ses, oth, n5, nt))
    if path == X:
        body.append("\\midrule")
write("tab4_xharness.tex", ["\\begin{tabular}{@{}lrrrrr@{}}", "\\toprule",
                            "Harness / model & Runs & Calls & \\% session & \\% other att. & $N{=}5$ \\% (tasks) \\\\",
                            "\\midrule", *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table W: workload and replay by engine ----------------------------------------------------------------------
FN, OC, FI, MK = ("results/analysis_dab_r3/B0_funnel.csv", "results/analysis_dab_r3/B0_outcome_classes.csv",
                  "results/analysis_dab_r3/B0_fidelity.csv", "results/analysis_dab_r3/B0_mismatch_kinds.csv")
PAIRS = "sum(int(v) for k, v in ROW('%s', engine='%s').items() if k != 'engine')"
body = []
for e, label in (("duckdb", "DuckDB"), ("mongo", "MongoDB"), ("postgres", "PostgreSQL"), ("sqlite", "SQLite")):
    calls = cell("tabw/%s/calls" % e, "cnt(ROW('%s', gate='all_query_db', level='engine', group='%s')['calls'])" % (FN, e))
    pairs = cell("tabw/%s/pairs" % e, "cnt(%s)" % (PAIRS % (OC, e)))
    ok = cell("tabw/%s/ok" % e, "pct(ROW('%s', gate='successful', level='engine', group='%s')['calls_share'])" % (FN, e))
    ident = cell("tabw/%s/identical" % e, "pct(int(ROW('%s', engine='%s')['match']) / int(ROW('%s', gate='successful', level='engine', group='%s')['calls']))" % (FI, e, FN, e))
    order = cell("tabw/%s/order" % e, "cnt(ROW('%s', engine='%s')['order_only'])" % (MK, e))
    other = cell("tabw/%s/other" % e, "cnt(int(ROW('%s', engine='%s')['prefix_mismatch']) + int(ROW('%s', engine='%s')['value_mismatch']))" % (MK, e, MK, e))
    body.append("%s & %s & %s & %s & %s & %s & %s \\\\" % (label, calls, pairs, ok, ident, order, other))
ENG = "['duckdb', 'mongo', 'postgres', 'sqlite']"
calls = cell("tabw/all/calls", "cnt(ROW('%s', gate='all_query_db', level='all', group='all')['calls'])" % FN)
pairs = cell("tabw/all/pairs", "cnt(sum(%s for e in %s) + int(ROW('%s', engine='unknown')['not_replayed']))" % ((PAIRS % (OC, "' + e + '")).replace("'%s'" % "' + e + '", "e"), ENG, OC))
ok = cell("tabw/all/ok", "pct(ROW('%s', gate='successful', level='all', group='all')['calls_share'])" % FN)
ident = cell("tabw/all/identical", "pct(sum(int(ROW('%s', engine=e)['match']) for e in %s) / int(ROW('%s', gate='successful', level='all', group='all')['calls']))" % (FI, ENG, FN))
order = cell("tabw/all/order", "cnt(sum(int(ROW('%s', engine=e)['order_only']) for e in %s))" % (MK, ENG))
other = cell("tabw/all/other", "cnt(sum(int(ROW('%s', engine=e)['prefix_mismatch']) + int(ROW('%s', engine=e)['value_mismatch']) for e in %s))" % (MK, MK, ENG))
body.append("\\midrule")
body.append("All & %s & %s & %s & %s & %s & %s \\\\" % (calls, pairs, ok, ident, order, other))
write("tabw_workload.tex", ["\\begin{tabular}{@{}lrrrrrr@{}}", "\\toprule",
                            " & & Distinct & Replay & Byte- & \\multicolumn{2}{c}{Mismatches} \\\\ \\cmidrule(l){6-7}",
                            "Engine & Calls & pairs & ok \\% & identical \\% & order & other \\\\", "\\midrule",
                            *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table D: the datasets ------------------------------------------------------------------------------------------
DT = "J('results/dataset_table.json')"
ENGS = {"sqlite": "S", "duckdb": "D", "postgres": "P", "mongo": "M"}
body = []
for ds in sorted(ev("list(%s)" % DT), key=str.lower):
    d = "%s['%s']" % (DT, ds)
    engines = ", ".join(ENGS[e] for e in ("sqlite", "duckdb", "postgres", "mongo") if e in ev("%s['engines']" % d))
    body.append(" & ".join([ds.replace("_", "\\_"), engines,
                            cell("tabd/%s/tasks" % ds, "cnt(%s['tasks'])" % d),
                            cell("tabd/%s/data_MB" % ds, "f\"{%s['data_bytes'] / 1e6:,.1f}\"" % d),
                            cell("tabd/%s/calls" % ds, "cnt(%s['calls'])" % d),
                            cell("tabd/%s/pairs" % ds, "cnt(%s['pairs'])" % d),
                            cell("tabd/%s/spilled" % ds, "pct(%s['spilled_share_ok'])" % d),
                            cell("tabd/%s/exec_s" % ds, "cnt(%s['exec_s_total'])" % d),
                            cell("tabd/%s/bytes_GB" % ds, "fx(%s['payload_bytes_total'] / 1e9)" % d),
                            cell("tabd/%s/other" % ds, "pct(%s['task_median_other_attempts_exec'])" % d),
                            cell("tabd/%s/session" % ds, "pct(%s['task_median_session_exec'])" % d)]) + " \\\\")
TOT = "sum(v['%s'] for v in " + DT + ".values())"
body.append("\\midrule")
body.append(" & ".join(["All", "",
                        cell("tabd/all/tasks", "cnt(%s)" % (TOT % "tasks")),
                        cell("tabd/all/data_MB", "f\"{%s / 1e6:,.1f}\"" % (TOT % "data_bytes")),
                        cell("tabd/all/calls", "cnt(%s)" % (TOT % "calls")),
                        cell("tabd/all/pairs", "cnt(%s)" % (TOT % "pairs")),
                        cell("tabd/all/spilled", "pct(J('results/paper_stats.json')['magnitudes']['spilled_share_of_successful_calls'])"),
                        cell("tabd/all/exec_s", "cnt(%s)" % (TOT % "exec_s_total")),
                        cell("tabd/all/bytes_GB", "fx(%s / 1e9)" % (TOT % "payload_bytes_total")),
                        cell("tabd/all/other", "pct(J('results/analysis_dab_r3/results_B1.json')['B1_fidelity_equiv_gate']['task_macro']['exec_s']['trials']['median'])"),
                        cell("tabd/all/session", "pct(J('results/analysis_dab_r3/results_B1.json')['B1_fidelity_equiv_gate']['task_macro']['exec_s']['session']['median'])")]) + " \\\\")
write("tabd_datasets.tex", ["\\begin{tabular}{@{}llrrrrrrrrr@{}}", "\\toprule",
                            " & & & Data & & Distinct & Spilled & DB time & Bytes & \\multicolumn{2}{c}{Task-median repeat \\%} \\\\ \\cmidrule(l){10-11}",
                            "Dataset & Engines & Tasks & (MB) & Calls & pairs & \\% & (s) & (GB) & other attempts & session \\\\", "\\midrule",
                            *body, "\\bottomrule", "\\end{tabular}"])

# ---- Table T: admission tiers ---------------------------------------------------------------------------------------
ST_ = "J('results/static_tiers.json')"
b_static = cell("tabt/B/static_calls", "cnt(%s['B']['all_query_db']['calls'])" % ST_)
b_calls = cell("tabt/B/calls", "cnt(ROW('%s', gate='B_certified', level='all', group='all')['calls'])" % FN)
e_static = cell("tabt/E/static_calls", "cnt(%s['E']['all_query_db']['calls'])" % ST_)
e_eval = cell("tabt/E/eval_calls", "cnt(ROW('%s', gate='E_certified', level='all', group='all')['calls'])" % FN)
o_calls = cell("tabt/F/calls", "cnt(ROW('%s', gate='fidelity_equiv', level='all', group='all')['calls'])" % FN)
write("tabt_tiers.tex", ["\\begin{tabular}{@{}L{1.55cm}L{3.75cm}cr@{}}", "\\toprule",
                         "Tier & Admission rule & Online & Calls \\\\", "\\midrule",
                         "Certified & Contract B: conditional static byte-stability certificate & yes & %s \\\\" % b_static,
                         " & \\quad B-eval subset (post hoc) & no & %s \\\\" % b_calls,
                         "Assumption-based & static Contract E, byte stability from a pinned runtime & yes & %s \\\\" % e_static,
                         " & \\quad E-eval subset (post hoc) & no & %s \\\\" % e_eval,
                         "Potential & oracle: replay equivalent to the recorded observation & no & %s \\\\" % o_calls,
                         "\\bottomrule", "\\end{tabular}"])

# ---- Fig. 2(b): robustness of the scope decomposition (pooled isolated DB-execution time), ledger rows only --------
B1 = "J('results/analysis_dab_r3/results_B1.json')['B1_fidelity_equiv_gate']"
EXS = "J('results/extra_analyses.json')['sensitivity']"
rowsR = [("all", "All oracle-admitted calls", B1 + "['overall']['exec_s']['%s']['mean']"),
         None]
body = []
def rrow(key, label, other_expr, ses_expr):
    o = cell("fig2/%s/other" % key, "pct(%s)" % other_expr)
    se = cell("fig2/%s/session" % key, "pct(%s)" % ses_expr)
    body.append("%s & %s & %s \\\\" % (label, o, se))
rrow("all", "All oracle-admitted calls", B1 + "['overall']['exec_s']['trials']['mean']", B1 + "['overall']['exec_s']['session']['mean']")
B1R = "J('results/analysis_dab_r3/results_B1.json')['%s']['exec_s']['%s']['mean']"
rrow("strict", "Byte-identical replays only", B1R % ("B1_strict_fidelity", "trials"), B1R % ("B1_strict_fidelity", "session"))
rrow("allrep", "All replayed calls (no fidelity gate)", B1R % ("B1_all_replayed", "trials"), B1R % ("B1_all_replayed", "session"))
body.append("\\midrule")
for m, label in (("gemini-2.5-flash", "Gemini-2.5-Flash"), ("gemini-3-pro", "Gemini-3-Pro"), ("gpt-5-mini", "GPT-5-mini"),
                 ("gpt-5.2", "GPT-5.2"), ("kimi-k2", "Kimi-K2")):
    rrow(m, label, B1 + "['by_model']['%s']['exec_s']['trials']['mean']" % m, B1 + "['by_model']['%s']['exec_s']['session']['mean']" % m)
body.append("\\midrule")
for e, label in (("duckdb", "DuckDB"), ("mongo", "MongoDB"), ("postgres", "PostgreSQL"), ("sqlite", "SQLite")):
    rrow(e, label, B1 + "['by_engine']['%s']['exec_s']['trials']['mean']" % e, B1 + "['by_engine']['%s']['exec_s']['session']['mean']" % e)
body.append("\\midrule")
rrow("pass", "Passed runs only", B1 + "['by_validation']['pass']['exec_s']['trials']['mean']", B1 + "['by_validation']['pass']['exec_s']['session']['mean']")
rrow("fail", "Failed runs only", B1 + "['by_validation']['fail']['exec_s']['trials']['mean']", B1 + "['by_validation']['fail']['exec_s']['session']['mean']")
rrow("first", "First execution as weight", B1 + "['overall']['exec_s_first']['trials']['mean']", B1 + "['overall']['exec_s_first']['session']['mean']")
rrow("top1", "Top 1\\% of reusable queries removed", EXS + "['drop_top1pct']['exec_s_shares']['trials']", EXS + "['drop_top1pct']['exec_s_shares']['session']")
rrow("m1", "Earlier replay, newer libraries", "J('results/analysis_m1/results_B1.json')['B1_fidelity_gate']['overall']['exec_s']['trials']['mean']",
     "J('results/analysis_m1/results_B1.json')['B1_fidelity_gate']['overall']['exec_s']['session']['mean']")
lo_min = cell("fig2/lodo/other_min", "pct(min(v['exec_s']['trials']['mean'] for v in %s['lodo'].values()))" % B1)
lo_max = cell("fig2/lodo/other_max", "pct(max(v['exec_s']['trials']['mean'] for v in %s['lodo'].values()))" % B1)
ls_min = cell("fig2/lodo/session_min", "pct(min(v['exec_s']['session']['mean'] for v in %s['lodo'].values()))" % B1)
ls_max = cell("fig2/lodo/session_max", "pct(max(v['exec_s']['session']['mean'] for v in %s['lodo'].values()))" % B1)
body.append("Leave one dataset out & %s--%s & %s--%s \\\\" % (lo_min, lo_max, ls_min, ls_max))
# the rows above are the values printed in Fig. 2(b) (figures/gen_fig2_locality.py); no table file is written

# ---- Table P: preregistered hypotheses ------------------------------------------------------------------------------
B23 = "J('results/analysis_dab_r3/results_B23.json')"
b1t = cell("tabp/B1/time_ratio", "fx(%s['overall']['exec_s']['trials']['mean'] / %s['overall']['exec_s']['session']['mean'], 1)" % (B1, B1))
b1b = cell("tabp/B1/bytes_ratio", "fx(%s['overall']['payload_bytes']['trials']['mean'] / %s['overall']['payload_bytes']['session']['mean'], 1)" % (B1, B1))
b1tm = cell("tabp/B1/time_ratio_task_median", "fx(%s['task_macro']['exec_s']['trials']['median'] / %s['task_macro']['exec_s']['session']['median'], 1)" % (B1, B1))
b1bm = cell("tabp/B1/bytes_ratio_task_median", "fx(%s['task_macro']['payload_bytes']['trials']['median'] / %s['task_macro']['payload_bytes']['session']['median'], 1)" % (B1, B1))
BP = "J('results/b1_planned.json')['byte_identical_engines_meeting_B0']"   # the population the plan prescribes for B1 and B5
RATIO = "%s['%s']['%s']['trials']['%s'] / %s['%s']['%s']['session']['%s']"
p1t = cell("tabp/B1/planned_time_ratio", "fx(%s, 1)" % (RATIO % (BP, "overall", "exec_s", "mean", BP, "overall", "exec_s", "mean")))
p1b = cell("tabp/B1/planned_bytes_ratio", "fx(%s, 1)" % (RATIO % (BP, "overall", "payload_bytes", "mean", BP, "overall", "payload_bytes", "mean")))
p1tm = cell("tabp/B1/planned_time_ratio_task_median", "fx(%s, 1)" % (RATIO % (BP, "task_macro", "exec_s", "median", BP, "task_macro", "exec_s", "median")))
p1bm = cell("tabp/B1/planned_bytes_ratio_task_median", "fx(%s, 1)" % (RATIO % (BP, "task_macro", "payload_bytes", "median", BP, "task_macro", "payload_bytes", "median")))
p1e = cell("tabp/B1/planned_engines", "word(len(J('results/b1_planned.json')['engines_meeting_B0']))")
FOLD = "min(v['%s']['trials']['mean'] / v['%s']['session']['mean'] for v in %s['lodo'].values())"
p5t = cell("tabp/B5/planned_lodo_min_time_ratio", "fx(%s, 1)" % (FOLD % ("exec_s", "exec_s", BP)))
p5b = cell("tabp/B5/planned_lodo_min_bytes_ratio", "fx(%s, 1)" % (FOLD % ("payload_bytes", "payload_bytes", BP)))
p5n = cell("tabp/B5/planned_lodo_folds_met", "cnt(sum(1 for v in %s['lodo'].values() if min(v[w]['trials']['mean'] / v[w]['session']['mean'] for w in ('exec_s', 'payload_bytes')) >= 3))" % BP)
p5m = cell("tabp/B5/planned_models_ge3", "cnt(sum(1 for v in %s['by_model'].values() if min(v[w]['trials']['mean'] / v[w]['session']['mean'] for w in ('exec_s', 'payload_bytes')) >= 3))" % BP)
b2t4 = cell("tabp/B2/time_N4", "pct(%s['B2_fidelity_gate']['N=4|B=inf']['S2_exec_saving']['median'])" % B23)
b2b4 = cell("tabp/B2/bytes_N4", "pct(%s['B2_fidelity_gate']['N=4|B=inf']['S2_payload_saving']['median'])" % B23)
b2t8 = cell("tabp/B2/time_N8", "pct(%s['B2_fidelity_gate']['N=8|B=inf']['S2_exec_saving']['median'])" % B23)
b3lo = cell("tabp/B3/E_written_N8", "fx(%s['B3_E_certified_exploratory']['N=8|B=inf']['S3_over_S2_written']['median'], 3)" % B23)
b3hi = cell("tabp/B3/E_written_N2", "fx(%s['B3_E_certified_exploratory']['N=2|B=inf']['S3_over_S2_written']['median'], 3)" % B23)
b3fl = cell("tabp/B3/E_foot_min", "fx(min(%s['B3_E_certified_exploratory']['N=%%d|B=inf' %% n][k]['median'] for n in (2, 4, 8) for k in ('S3_over_S2_peak_foot', 'S3_over_S2_peak_foot_seq')), 3)" % B23)
b3fh = cell("tabp/B3/E_foot_max", "fx(max(%s['B3_E_certified_exploratory']['N=%%d|B=inf' %% n][k]['median'] for n in (2, 4, 8) for k in ('S3_over_S2_peak_foot', 'S3_over_S2_peak_foot_seq')), 3)" % B23)
b3tl = cell("tabp/B3/E_time_min", "fx(min(%s['B3_E_certified_exploratory']['N=%%d|B=inf' %% n]['S3_over_S2_time']['median'] for n in (2, 4, 8)), 3)" % B23)
b3th = cell("tabp/B3/E_time_max", "fx(max(%s['B3_E_certified_exploratory']['N=%%d|B=inf' %% n]['S3_over_S2_time']['median'] for n in (2, 4, 8)), 3)" % B23)
b3e = cell("tabp/B3/E_engine_max_written", "fx(max(d['S3_over_S2_written'] for k, v in %s['B3_E_certified_exploratory'].items() if k.split('|')[0] in ('N=2', 'N=4', 'N=8') for d in v['by_engine'].values() if d.get('S3_over_S2_written') is not None), 2)" % B23)
b3et = cell("tabp/B3/E_engine_max_time", "fx(max(d['S3_over_S2_time'] for k, v in %s['B3_E_certified_exploratory'].items() if k.split('|')[0] in ('N=2', 'N=4', 'N=8') for d in v['by_engine'].values() if d.get('S3_over_S2_time') is not None), 2)" % B23)
b3n = cell("tabp/B3/B_tasks", "cnt(%s['B3_B_certified']['N=8|B=inf']['S3_written_saving']['n'])" % B23)
b3s = cell("tabp/B3/B_S3_saving", "pct(%s['B3_B_certified']['N=8|B=inf']['S3_written_saving']['median'], 0)" % B23)
b3n1 = cell("tabp/B3/B_tasks_planned", "cnt(J('results/analysis_dab_r1/results_B23.json')['B3_B_certified']['N=8|B=inf']['S3_written_saving']['n'])")
b3s1 = cell("tabp/B3/B_S3_saving_planned", "pct(J('results/analysis_dab_r1/results_B23.json')['B3_B_certified']['N=8|B=inf']['S3_written_saving']['median'], 0)")
b4b1 = cell("tabp/B4/B_time_planned", "pct(J('results/live2/validate_B.json')['median_time_rel_err_uncalibrated'])")
b4b = cell("tabp/B4/B_time", "pct(J('results/live2_certv3/validate_B.json')['median_time_rel_err_uncalibrated'])")
b4e = cell("tabp/B4/E_time", "pct(J('results/live2/validate_E.json')['median_time_rel_err_uncalibrated'])")
b4w = cell("tabp/B4/E_written_max", "pct(J('results/live2/validate_E.json')['max_written_rel_err'], 0)")
CT0, CT = "J('results/live/consumer_summary_seed0.json')", "J('results/live/consumer_summary.json')"   # the planned consumer test
b4c = cell("tabp/B4/consumer_cases", "cnt(%s['valid'])" % CT0)
b4n = cell("tabp/B4/calibration_cells", "cnt(len(J('results/live/selection.json')['picks']))")
b4i = cell("tabp/B4/consumers_identical_seed_fixed", "cnt(%s['identical_S2_S3'])" % CT0)
b4r = cell("tabp/B4/consumers_identical_random_seed", "cnt(%s['identical_S2_S3'])" % CT)
b4p = cell("tabp/B4/probes", "cnt(len(%s['probes']))" % CT)
b4q = cell("tabp/B4/probes_refused", "cnt(sum(1 for p in %s['probes'] if p['S3_refused']))" % CT)
FID = "pct(int(ROW('results/analysis_dab_r3/B0_fidelity.csv', engine='%s')['match']) / int(ROW('results/analysis_dab_r3/B0_funnel.csv', gate='successful', level='engine', group='%s')['calls']))"
b0 = {e: cell("tabp/B0/%s_identical" % e, FID % (e, e)) for e in ("duckdb", "postgres", "sqlite", "mongo")}
b0q = cell("tabp/B0/mongo_equivalent", "pct(int(ROW('results/analysis_dab_r3/B0_funnel.csv', gate='fidelity_equiv', level='engine', group='mongo')['calls']) / int(ROW('results/analysis_dab_r3/B0_funnel.csv', gate='successful', level='engine', group='mongo')['calls']))")
write("tabp_prereg.tex", ["\\begin{tabular}{@{}L{3.3cm}L{6.1cm}L{6.7cm}@{}}", "\\toprule",
    "Analysis & Threshold & Outcome \\\\", "\\midrule",
    "B0 replay fidelity & replayed result byte-identical to the recorded observation for $\\geq$90\\%% of the successful calls of every engine & met for DuckDB, PostgreSQL and SQLite (%s\\%%, %s\\%%, %s\\%%); MongoDB %s\\%%, and %s\\%% equivalent after the normalization of Section~\\ref{sec:method} \\\\" % (b0["duckdb"], b0["postgres"], b0["sqlite"], b0["mongo"], b0q),
    "B1 cross-attempt dominance & other attempts $\\geq 3\\times$ session in time and bytes, pooled and for the median task, on byte-identical replays of the engines that meet B0 & met on that population (%s engines): %s$\\times$ (time), %s$\\times$ (bytes) pooled; %s$\\times$, %s$\\times$ for the median task. On all oracle-admitted calls (Section~\\ref{sec:locality}): %s$\\times$, %s$\\times$; %s$\\times$, %s$\\times$ \\\\" % (p1e, p1t, p1b, p1tm, p1bm, b1t, b1b, b1tm, b1bm),
    "B2 practical savings & under S2, the median task saves $\\geq$20\\%% of DB time or $\\geq$30\\%% of bytes at $N=4$ & not met at $N=4$ on the oracle-admitted calls with an unbounded budget (%s\\%%, %s\\%%); %s\\%% of DB time at $N=8$ \\\\" % (b2t4, b2b4, b2t8),
    "B3 ownership & on the calls that Contract B admits, at $N=2$--8: S3 below S2 in median-task bytes written and peak footprint, tool time within 5\\%%, same direction in every engine & not met on Contract B, which admits a spilled result in only %s tasks as planned and %s under the final rule, with a median S3 write saving of %s\\%% and %s\\%%; met on E-eval (exploratory, unbounded budget): S3/S2 writes %s--%s, footprint %s--%s under both lifetimes, tool time %s--%s; in every engine, writes at most %s and tool time at most %s (footprint was not computed per engine) \\\\" % (b3n1, b3n, b3s1, b3s, b3lo, b3hi, b3fl, b3fh, b3tl, b3th, b3e, b3et),
    "B4 simulator validity and consumer test & on %s calibration cells at $N=8$ with an unbounded and a 1/4 budget: simulated tool time within 10\\%%, bytes within 1\\%%; consumer outputs identical in $\\geq$99\\%% of cases; writes to shared objects refused & bytes and footprint met (max error %s\\%%); tool time %s\\%% under B as planned (met), %s\\%% under the final B and %s\\%% on E-eval (not met); consumer test, planned on inputs that Contract B admits and run on E-eval inputs, on %s recorded Python calls: %s outputs identical under S2 and S3 with the hash seed fixed (%s with random seeds), %s of %s write probes refused by a read-only mount (met) \\\\" % (b4n, b4w, b4b1, b4b, b4e, b4c, b4i, b4r, b4q, b4p),
    "B5 heterogeneity & B1 holds in $\\geq 10$ of 12 leave-one-dataset-out folds and for every model & on the B1 population, met in %s of 12 folds (smallest ratio %s$\\times$ in time, %s$\\times$ in bytes) and for %s of 5 models \\\\" % (p5n, p5t, p5b, p5m),
    "\\bottomrule", "\\end{tabular}"])

# ---- ledger: table rows are regenerated, text rows are kept -----------------------------------------------------------
keep = []
if os.path.exists(LEDGER):
    with open(LEDGER) as f:
        keep = [r for r in csv.DictReader(f) if not r["location"].startswith(TABLE_PREFIXES)]
with open(LEDGER, "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["id", "location", "printed", "expr"])
    w.writeheader()
    w.writerows(keep + rows)
print(len(rows), "table cells written to the ledger;", len(keep), "text rows kept")
