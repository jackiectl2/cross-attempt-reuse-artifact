"""Round-1 review analyses on top of analyze2 outputs.

1. Cell-level savings thresholds: share of (task, model) cells and of tasks whose S2 DB-execution saving
   reaches 10/20/30% at N = 2/4/8 (B2 simulation, fidelity-equivalence gate, unbounded cache).
2. Concentration sensitivity: B1 scope shares and B2 savings with the top 1% / top 10 reusable keys removed
   from the workload. A key's reusable time is t_exec x (calls of the key in its dataset - 1), which equals
   the order-independent "not first occurrence" weight used by the concentration statistic.
3. Online Contract E exposure: calls admitted by the static E certificate alone (successful replay, no
   fidelity or stability filter), split by 3-run outcome class and by fidelity to the original observation.
4. MongoDB fidelity by result size class (inline vs spilled), weighted by calls, time and bytes.

Usage: extra_analyses.py <v2> <analysis_dir> <out.json> [n_orders] [n_res] [workers]
"""
import json
import math
import sys

import numpy as np
import pandas as pd

import analyze2 as A


def thresholds(sim):
    s = sim[(sim.budget == "inf") & sim.N.isin([2, 4, 8])]
    cell = s.groupby(["dataset", "query", "model", "N"])[["S0_exec", "S2_exec"]].sum().reset_index()
    cell = cell[cell.S0_exec > 0]
    cell["saving"] = 1 - cell.S2_exec / cell.S0_exec
    task = s.groupby(["dataset", "query", "N"])[["S0_exec", "S2_exec"]].sum().reset_index()
    task = task[task.S0_exec > 0]
    task["saving"] = 1 - task.S2_exec / task.S0_exec
    out = {}
    for N in (2, 4, 8):
        c, t = cell[cell.N == N], task[task.N == N]
        out[f"N={N}"] = {"cells": int(len(c)), "tasks": int(len(t)),
                         **{f"cells_ge_{p}pct": float((c.saving >= p / 100).mean()) for p in (10, 20, 30)},
                         **{f"tasks_ge_{p}pct": float((t.saving >= p / 100).mean()) for p in (10, 20, 30)},
                         "by_model_median_cell_saving": c.groupby("model").saving.median().round(4).to_dict()}
    return out


def top_keys(q, mask):
    g = q[mask]
    cnt = g.groupby("key").size()
    t = g.groupby("key").t_exec.first()
    reuse = (t * (cnt - 1)).sort_values(ascending=False)
    reuse = reuse[reuse > 0]
    k1 = max(1, math.ceil(len(reuse) / 100))
    return reuse, {"top1pct": set(reuse.index[:k1]), "top10": set(reuse.index[:10])}


def b1_b2(q, runs, mask, n_orders, n_res, workers, costs):
    dec, tasks = A.decomposition(q, mask, n_orders, workers)
    sim = A.simulate(q, runs, mask, n_res, workers, costs)
    _, curves = A.summarize_sim(sim[sim.budget == "inf"])
    tm = tasks[tasks.weight == "exec_s"]
    return {"exec_s_shares": {s: round(dec["overall"]["exec_s"][s]["mean"], 4) for s in A.SCOPES},
            "payload_shares": {s: round(dec["overall"]["payload_bytes"][s]["mean"], 4) for s in A.SCOPES},
            "task_macro_exec": {"trials_median": float(tm.trials.median()), "session_median": float(tm.session.median())},
            "S2_exec_saving_median": {N: curves[f"N={N}|B=inf"]["S2_exec_saving"]["median"] for N in (2, 4, 8)}}


def online_E(q, gates):
    e = gates["successful"] & q.certified_E.fillna(False).astype(bool)
    g = q[e].copy()
    first = ~g.duplicated(["dataset", "query", "model", "key"])
    out = {}
    for name, sub in (("all_E_static_calls", g), ("repeat_calls_served_from_cache", g[~first.values])):
        w = {"calls": np.ones(len(sub)), "time": sub.t_exec.values, "bytes": sub.bytes.values}
        tot = {k: v.sum() for k, v in w.items()}
        out[name] = {"n_calls": int(len(sub)), "n_keys": int(sub.key.nunique()),
                     "by_outcome_class": {c: {k: float(w[k][(sub.cls == c).values].sum() / max(tot[k], 1e-12)) for k in w}
                                          for c in sorted(sub.cls.unique())},
                     "fidelity_not_byte_identical": {k: float(w[k][(sub.fidelity != "match").values].sum() / max(tot[k], 1e-12))
                                                     for k in w}}
    return out


def mongo_fidelity(q, gates):
    g = q[gates["successful"] & (q.engine == "mongo")]
    out = {}
    for st, sub in g.groupby("status"):
        out[st] = {"calls": int(len(sub)), "time_s": float(sub.t_exec.sum()), "bytes": float(sub.bytes.sum()),
                   "equiv_share_calls": float(sub.fidelity_equiv.mean()),
                   "equiv_share_time": float(sub.t_exec[sub.fidelity_equiv].sum() / max(sub.t_exec.sum(), 1e-12)),
                   "equiv_share_bytes": float(sub.bytes[sub.fidelity_equiv].sum() / max(sub.bytes.sum(), 1)),
                   "mismatch_kinds": sub.mismatch_kind.value_counts().to_dict()}
    return out


def main(v2, adir, out, n_orders=50, n_res=50, workers=16):
    n_orders, n_res, workers = int(n_orders), int(n_res), int(workers)
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    costs = dict(A.COSTS)
    costs.update({k: v for k, v in json.load(open(f"{v2}/live/costs.json")).items() if k in A.COSTS and v is not None})
    res = {"thresholds": thresholds(pd.read_parquet(f"{adir}/B2_sim_fidelity.parquet")),
           "online_E": online_E(q, gates), "mongo_fidelity": mongo_fidelity(q, gates)}
    print(json.dumps(res["thresholds"], indent=1), flush=True)
    mask = gates["fidelity_equiv"]
    reuse, tops = top_keys(q, mask)
    res["concentration_keys"] = {"n_reusable_keys": int(len(reuse)), "top1pct_n": len(tops["top1pct"]),
                                 "top1pct_share": float(reuse.head(len(tops["top1pct"])).sum() / reuse.sum()),
                                 "top10_share": float(reuse.head(10).sum() / reuse.sum())}
    res["sensitivity"] = {"baseline": b1_b2(q, runs, mask, n_orders, n_res, workers, costs)}
    for name, ks in tops.items():
        res["sensitivity"][f"drop_{name}"] = b1_b2(q, runs, mask & ~q.key.isin(ks), n_orders, n_res, workers, costs)
        print(name, json.dumps(res["sensitivity"][f"drop_{name}"]), flush=True)
    json.dump(res, open(out, "w"), indent=1, default=float)


if __name__ == "__main__":
    main(*sys.argv[1:])
