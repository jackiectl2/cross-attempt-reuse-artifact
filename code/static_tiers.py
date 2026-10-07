"""Static admission per tier over all query_db calls, and the cell sizes behind the N-curves.

Separates what each static rule admits (decidable before execution) from the post-hoc gates that the analyses add
(successful replay, fidelity to the recorded observation, three-run stability), and counts the task-model cells that
can contribute N attempts without replacement.

Usage: static_tiers.py <v2> <out.json>     (ED_REPLAY_DIR selects the replay tables, as for analyze2.py)
"""
import json
import sys

import analyze2 as A

GATES = ("all_query_db", "replayed", "successful", "fidelity_equiv", "fidelity_pass", "stable")


def main(v2, out):
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    res = {"n_query_db": int(len(q))}
    for tier, col in (("B", "certified"), ("E", "certified_E")):
        s = q[col].fillna(False).astype(bool)
        res[tier] = {g: {"calls": int((gates[g] & s).sum()), "keys": int(q.key[gates[g] & s].nunique()),
                         "time_lb_share": float(q.t_exec_lb[gates[g] & s].sum() / q.t_exec_lb.sum()),
                         "bytes_share": float(q.bytes[gates[g] & s].sum() / q.bytes.sum())} for g in GATES}
        res[tier]["outcome_class_calls"] = {str(k): int(v) for k, v in q.cls[s].fillna("not_replayed").value_counts().items()}
        res[tier]["fidelity_calls"] = {str(k): int(v) for k, v in q.fidelity[s].value_counts().items()}
    b, e = (q[c].fillna(False).astype(bool) for c in ("certified", "certified_E"))
    res["B_not_E_calls"] = int((b & ~e).sum())
    n = runs.groupby(["dataset", "query", "model"]).size()
    res["cells"] = {"total": int(len(n)), "runs_total": int(n.sum()),
                    **{f"with_at_least_{k}_runs": int((n >= k).sum()) for k in (8, 16, 50)}}
    full = n[n >= 50].reset_index().groupby(["dataset", "query"]).size()
    res["tasks_with_five_cells_at_50"] = int((full == 5).sum())
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:3])
