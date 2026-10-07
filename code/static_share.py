"""Share of the cross-attempt reusable work that each static admission rule admits, without post-hoc gates.

Same population and attempt orders as claim_support.b_share (oracle-gated calls, n_orders random orders, seed = dataset
index), with the static rules themselves (Contract B, Contract E, decided from the query text alone) next to their
post-hoc subsets (byte-identical to the recorded observation and stable over three replays). The post-hoc columns
reproduce claim_support.json's admitted_by_B / admitted_by_E_eval.

Next to the tiers, a bound that does not depend on any certificate: `order_free` marks the queries whose replayed
result has at least two distinct rows and whose statement has no top-level ORDER BY (a MongoDB find through the DAB
interface has no sort). An engine may return those rows in any order, so no rule that guarantees the bytes across
plans can admit them; `admitted_by_order_fixed` is therefore an upper bound for every such rule.

Usage: static_share.py <v2> <out.json> [n_orders] [workers]     (ED_REPLAY_DIR as for analyze2.py)
"""
import json
import sys
from multiprocessing import Pool

import numpy as np
import sqlglot
from sqlglot import exp

import analyze2 as A
from certify import _DIALECT


def distinct_rows(payload, spilled):
    """Does the serialized result hold two different rows? A spilled result is judged on the complete rows of its
    preview; when the preview shows fewer than two different rows the answer is False."""
    try:
        if not spilled:
            rows = json.loads(payload)
            return isinstance(rows, list) and len({json.dumps(r, sort_keys=True) for r in rows}) >= 2
        dec, pos, seen = json.JSONDecoder(), payload.index("[") + 1, set()
        while len(seen) < 2:
            row, pos = dec.raw_decode(payload, pos)
            seen.add(json.dumps(row, sort_keys=True))
            pos = payload.index(",", pos) + 1
            while payload[pos] == " ":
                pos += 1
        return True
    except (ValueError, IndexError):
        return False


def top_order(text, engine):
    """Does the statement have a top-level ORDER BY? A text that is not one parsed query counts as ordered."""
    if engine == "mongo":
        return False
    try:
        stmts = [s for s in sqlglot.parse(text, read=_DIALECT[engine]) if s is not None]
    except Exception:
        return True
    if len(stmts) != 1 or not isinstance(stmts[0], (exp.Select, exp.SetOperation)):
        return True
    return stmts[0].args.get("order") is not None


def order_free(rep):
    return [st == "ok" and e in ("mongo", *_DIALECT) and distinct_rows(p, bool(sp)) and not top_order(t, e)
            for st, e, p, sp, t in zip(rep.status, rep.engine, rep.payload_head, rep.spilled, rep.text)]


def main(v2, out, n_orders=200, workers=8):
    n_orders, workers = int(n_orders), int(workers)
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    rep["order_free"] = order_free(rep)
    free = q.merge(rep[["dataset", "db_name", "text", "order_free"]], on=["dataset", "db_name", "text"],
                   how="left").order_free.fillna(False).astype(bool).values
    mask = gates["fidelity_equiv"]
    g = q[mask].reset_index(drop=True)
    tiers = {"B_static": q.certified.fillna(False).astype(bool)[mask].values,
             "E_static": q.certified_E.fillna(False).astype(bool)[mask].values,
             "B_eval": gates["B_certified"][mask].values, "E_eval": gates["E_certified"][mask].values,
             "order_fixed": ~free[mask.values]}
    parts = [(gg, n_orders, i) for i, (_, gg) in enumerate(g.groupby("dataset", sort=True))]   # = decomposition(seed=0)
    with Pool(workers) as pool:
        codes = pool.map(A._orders_for_dataset, parts)
    C = np.empty((n_orders, len(g)), dtype=np.int8)
    for (gg, _, _), c in zip(parts, codes):
        C[:, gg.index.values] = c
    W = {"calls": np.ones(len(g)), "exec_s": g.t_exec.values, "payload_bytes": g.bytes.values}
    res = {"n_orders": n_orders, "gated_calls": int(len(g)), "scope": "other_attempts"}
    ok = gates["successful"].values
    res["order_free"] = {"queries": int(rep.order_free.sum()), "calls": int((free & ok).sum()),
                         "successful_calls": int(ok.sum()),
                         "B_static_and_order_free_calls": int((free & q.certified.fillna(False).astype(bool).values).sum()),
                         "share_of_successful": {w: float(v[free & ok].sum() / v[ok].sum()) for w, v in
                                                 (("calls", np.ones(len(q))), ("exec_s", q.t_exec.values),
                                                  ("payload_bytes", q.bytes.values))},
                         "calls_by_engine": {e: {"order_free": int((free & ok & (q.engine == e).values).sum()),
                                                 "successful": int((ok & (q.engine == e).values).sum())}
                                             for e in sorted(q.engine.dropna().unique()) if e != "unknown"}}
    for w, wv in W.items():
        adm = {t: [] for t in tiers}
        for o in range(n_orders):
            s = C[o] == 1
            tot = wv[s].sum()
            for t, m in tiers.items():
                adm[t].append(wv[s & m].sum() / tot if tot > 0 else np.nan)
        res[w] = {f"admitted_by_{t}": float(np.nanmean(v)) for t, v in adm.items()}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
