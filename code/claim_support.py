"""Derived numbers requested by the result-to-claim jury (2026-10-02). Reads existing tables only; no query is executed.

  b_share   share of the cross-attempt reusable work (B1 scope "other attempts" under the fidelity-equivalence gate,
            same orders and seeds as the reported B1 run) that Contract B and the E-eval subset admit, by calls,
            completed execution time and payload bytes
  e_diff    statically E-admitted calls that are not byte-identical to the observation recorded in the original
            DAB run (extra_analyses.online_E), split by fidelity outcome and mismatch kind
  retained  live footprint with every attempt's files kept to batch end, unbounded budget (no eviction, so the S3
            reference lifetime cannot change it): per cell S2/S2c/S3 peak vs S0, live vs simulator, du vs accounting
  timing    simulator tool-time error on the E calibration (live2), heavy vs cheap tasks

Usage: claim_support.py <v2_dir> <out.json> [n_orders] [workers]
"""
import glob
import json
import os
import sys
from multiprocessing import Pool

import numpy as np
import pandas as pd

import analyze2 as A


def b_share(q, gates, n_orders, workers):
    mask = gates["fidelity_equiv"]
    g = q[mask].reset_index(drop=True)
    tiers = {"B": gates["B_certified"][mask].values, "E_eval": gates["E_certified"][mask].values}
    parts = [(gg, n_orders, i) for i, (_, gg) in enumerate(g.groupby("dataset", sort=True))]   # = decomposition(seed=0)
    with Pool(workers) as pool:
        codes = pool.map(A._orders_for_dataset, parts)
    C = np.empty((n_orders, len(g)), dtype=np.int8)
    for (gg, _, _), c in zip(parts, codes):
        C[:, gg.index.values] = c
    W = {"calls": np.ones(len(g)), "exec_s": g.t_exec.values, "payload_bytes": g.bytes.values}
    out = {"n_orders": n_orders, "gated_calls": int(len(g))}
    for scope, sel in (("other_attempts", 1), ("any_repeat", None)):
        res = {}
        for w, wv in W.items():
            share, adm = [], {t: [] for t in tiers}
            for o in range(n_orders):
                s = (C[o] == 1) if sel == 1 else (C[o] != 4)
                tot = wv[s].sum()
                share.append(tot / wv.sum())
                for t, m in tiers.items():
                    adm[t].append(wv[s & m].sum() / tot if tot > 0 else np.nan)
            res[w] = {"share_of_gated": float(np.mean(share)),
                      **{f"admitted_by_{t}": float(np.nanmean(v)) for t, v in adm.items()}}
        out[scope] = res
    return out


def e_diff(q, gates):
    e = gates["successful"] & q.certified_E.fillna(False).astype(bool)
    g = q[e]
    w = {"calls": np.ones(len(g)), "time": g.t_exec.values, "bytes": g.bytes.values}
    tot = {k: v.sum() for k, v in w.items()}
    diff = (g.fidelity != "match").values
    kind = np.where(g.fidelity.values == "mismatch", g.mismatch_kind.fillna("none").values, g.fidelity.values)
    out = {"n_calls": int(len(g)),
           "not_byte_identical": {k: float(v[diff].sum() / tot[k]) for k, v in w.items()},
           "equivalent_after_normalization": {k: float(v[diff & g.fidelity_equiv.values].sum() / tot[k]) for k, v in w.items()},
           "by_kind": {c: {k: float(v[diff & (kind == c)].sum() / tot[k]) for k, v in w.items()}
                       for c in sorted(set(kind[diff]))},
           "by_engine": {e_: {k: float(v[diff & (g.engine.values == e_)].sum() / tot[k]) for k, v in w.items()}
                         for e_ in sorted(set(g.engine.values[diff]))}}
    return out


def retained(v2):
    rows = []
    for d, systems in (("live2", ("S0", "S2", "S3")), ("live3", ("S0", "S2", "S2c", "S3"))):
        pat = f"{v2}/{d}/calib_E_" + ("cow_" if d == "live3" else "") + "*.json"
        for f in sorted(glob.glob(pat)):
            cell = json.load(open(f))
            for r in cell["results"]:
                if r["budget"] != "inf" or r["system"] not in systems:
                    continue
                rows.append({"src": d, "cell": f"{cell['dataset']}/{cell['query']}/{cell['model']}", "res": r["res"],
                             "system": r["system"], "peak": r["peak_retained_a"], "peak_all": r["peak_retained"],
                             "du": r["footprint"], "end_accounted": r["end_accounted"]})
    df = pd.DataFrame(rows)
    out = {"du_equals_accounting": {src: f"{int((g.du == g.end_accounted).sum())}/{len(g)}" for src, g in df.groupby("src")}}
    piv = df.pivot_table(index=["src", "cell", "res"], columns="system", values="peak").reset_index()
    val = pd.read_csv(f"{v2}/live2/validate_E.csv")
    val = val[val.budget == "inf"]
    pred = val.groupby(["task", "system"]).pred_peak_retained.median().unstack()
    for src, g in piv.groupby("src"):
        sys_ = [s for s in ("S2", "S2c", "S3") if s in g]
        per_cell = g.groupby("cell")[["S0"] + sys_].median()
        per_cell = per_cell[per_cell.S0 > 0]
        o = {"cells_with_admitted_bytes": int(len(per_cell))}
        for s in sys_:
            r = per_cell[s] / per_cell.S0
            o[f"{s}_over_S0_live"] = {"median": float(r.median()), "min": float(r.min()), "max": float(r.max()),
                                      "pooled": float(per_cell[s].sum() / per_cell.S0.sum())}
            if src == "live2" and s in pred:
                pc = pred.reindex(per_cell.index)
                o[f"{s}_over_S0_sim"] = {"median": float((pc[s] / pc.S0).median()),
                                         "pooled": float(pc[s].sum() / pc.S0.sum())}
        out[src] = o
    return out


def timing(v2):
    val = pd.read_csv(f"{v2}/live2/validate_E.csv")
    s0 = val[val.system == "S0"].groupby(["task", "budget"]).live_time.sum()
    heavy = {k for k, v in s0.items() if v >= 5.0}      # sim_agreement.py: S0 live time of the task >= 5 s
    val["class"] = ["heavy" if (t, b) in heavy else "cheap" for t, b in zip(val.task, val.budget)]
    return {c: {"tasks": int(g.task.nunique()), "rows": int(len(g)),
                "median_time_rel_err_uncal": float(g.time_rel_err_uncal.median()),
                "median_time_rel_err": float(g.time_rel_err.median())}
            for c, g in val.groupby("class")}


def main(v2, out, n_orders=200, workers=8):
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    res = {"b_share": b_share(q, gates, int(n_orders), int(workers)), "e_diff": e_diff(q, gates),
           "retained": retained(v2), "timing": timing(v2)}
    print(json.dumps(res, indent=1), flush=True)
    json.dump(res, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:])
