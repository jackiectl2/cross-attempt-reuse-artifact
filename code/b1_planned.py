"""Pre-specified analyses B1 and B5 on the population the experiment plan prescribes: successful calls whose replay is
byte-identical to the recorded observation, restricted to the engines that meet the B0 bar (byte identity for at least
90% of an engine's successful calls). The same decomposition over every engine is written next to it.

Usage: b1_planned.py <v2> <out.json> [n_orders] [workers]
"""
import json
import sys

import analyze2 as A

B0_BAR = 0.9


def main(v2, out, n_orders=200, workers=16):
    n_orders, workers = int(n_orders), int(workers)
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    ok, strict = gates["successful"], gates["fidelity_pass"]
    share = {e: float((strict & (q.engine == e)).sum() / (ok & (q.engine == e)).sum())
             for e in sorted(q.engine.dropna().unique()) if (ok & (q.engine == e)).any()}
    passing = [e for e, s in share.items() if s >= B0_BAR]
    res = {"n_orders": n_orders, "B0_bar": B0_BAR, "byte_identical_share_of_successful_calls": share,
           "engines_meeting_B0": passing}
    for name, mask in (("byte_identical_engines_meeting_B0", strict & q.engine.isin(passing)),
                       ("byte_identical_all_engines", strict)):
        dec, _ = A.decomposition(q, mask, n_orders, workers, seed=0)
        res[name] = {"calls": int(mask.sum()), **{k: dec[k] for k in ("overall", "task_macro", "lodo", "by_model")}}
        print(name, int(mask.sum()), {w: round(dec["overall"][w]["trials"]["mean"] / dec["overall"][w]["session"]["mean"], 2)
                                      for w in ("exec_s", "payload_bytes")}, flush=True)
    with open(out, "w") as f:
        json.dump(res, f, indent=1, default=float)


if __name__ == "__main__":
    main(*sys.argv[1:])
