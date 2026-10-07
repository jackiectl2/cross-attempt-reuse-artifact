"""Sensitivity of the batch-cache DB-execution saving to the time assigned to each query.

Re-creates the attempt orders of analyze2.simulate for the oracle-gated simulation (same seeds, same loop order) and
computes the unbounded S2 DB-execution saving per task at N = 4 and N = 8 under three weights taken from the three
isolated replays of a query: the median of the completed executions (the reported weight), the first, cold execution,
and the steady-state executions (median of the later successful ones). With an unbounded budget a call is a hit iff
its key occurred earlier in the batch, so the saving is (time of repeats) / (time of all calls), summed over the
task's models and resamples as in analyze2.summarize_sim.

Usage: n_weights.py <v2> <analysis_dir> <out.json> [n_res]     (ED_REPLAY_DIR as for analyze2.py)
"""
import json
import sys

import numpy as np

import analyze2 as A

WEIGHTS = {"median_completed": "t_exec", "first_cold": "t_exec_first", "steady_state": "t_exec_steady"}
NS_OUT = (4, 8)


def main(v2, adir, out, n_res=200):
    n_res = int(n_res)
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    g = q[gates["fidelity_equiv"]].sort_values(["dataset", "query", "model", "run", "step"])
    saving = {N: {w: [] for w in WEIGHTS} for N in NS_OUT}
    for i, ((ds, qy), tr) in enumerate(runs.groupby(["dataset", "query"])):
        tg = g[(g.dataset == ds) & (g["query"] == qy)]
        rng = np.random.default_rng(11 + i)                       # analyze2.main: simulate(..., seed=11 + off), off = 0
        tot = {N: {w: [0.0, 0.0] for w in WEIGHTS} for N in NS_OUT}
        for mo, mr in tr.groupby("model"):
            mg = tg[tg.model == mo]
            by_run = {r: [] for r in mr.run}
            for r, rr in mg.groupby("run"):
                by_run[r] = list(zip(rr.key, *(rr[c] for c in WEIGHTS.values())))
            runs_all = np.array(sorted(by_run))
            for N in A.NS:
                if N > len(runs_all):
                    continue
                for bname, frac in A.BUDGETS.items():
                    if frac is not None and N not in A.BUDGET_NS:
                        continue
                    for _ in range(n_res):
                        order = rng.permutation(runs_all)[:N]
                        if bname != "inf" or N not in NS_OUT:
                            continue
                        seen = set()
                        for r in order:
                            for k, *ts in by_run[r]:
                                for (w, _), t in zip(WEIGHTS.items(), ts):
                                    tot[N][w][1] += t
                                    if k in seen:
                                        tot[N][w][0] += t
                                seen.add(k)
        for N in NS_OUT:
            for w in WEIGHTS:
                hit, allt = tot[N][w]
                if allt > 0:
                    saving[N][w].append(hit / allt)
    ref = json.load(open(f"{adir}/results_B23.json"))["B2_fidelity_gate"]
    res = {"n_resamples": n_res,
           **{f"N={N}": {**{w: A.boot_stats(saving[N][w]) for w in WEIGHTS},
                         "reported_S2_exec_saving_median": ref[f"N={N}|B=inf"]["S2_exec_saving"]["median"]} for N in NS_OUT}}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
