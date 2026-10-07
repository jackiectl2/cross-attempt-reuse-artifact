"""Classify every observation mismatch of the end-to-end passes (live.py e2e) against S0.

  live_error     one of the two live executions failed (error / 120 s cap) and the other returned a result
  value_on_hit   the cached result served on a hit differs from S0's successful result  (would be a cache defect)
  value_on_miss  both systems executed the query live and got different bytes          (live nondeterminism)
Consumer mismatches are split into timeout (either side hit the 120 s consumer cap) and output. For each mismatched
call the query text and its isolated-replay record (outcome class, execution times) are attached.
Usage: e2e_mismatch_diag.py <v2> <out.json> [e2e_dir]   (e2e_dir defaults to <v2>/e2e)
"""
import glob
import json
import sys
from collections import Counter

import pandas as pd

PASSES = ("all_E", "calib_E", "calib_B", "cow_calib_E")


def main(v2, out, e2e_dir=None):
    e2e_dir = e2e_dir or f"{v2}/e2e"
    calls = pd.read_parquet(f"{v2}/calls.parquet", columns=["dataset", "query", "model", "run", "step", "tool", "db_name", "text"])
    calls = calls[calls.tool == "query_db"].sort_values(["dataset", "query", "model", "run", "step"])
    rep = pd.concat([pd.read_parquet(f, columns=["dataset", "db_name", "text", "engine", "status_all", "exec_s", "payload_hashes"])
                     for f in glob.glob(f"{v2}/replay_dab/*.parquet")]).set_index(["dataset", "db_name", "text"])
    by_run = {k: g for k, g in calls.groupby(["dataset", "query", "model", "run"])}
    res = {}
    for ps in PASSES:
        files = sorted(glob.glob(f"{e2e_dir}/{ps}_*.json"))
        if not files:
            continue
        cls, cons, detail, truncated, skipped = Counter(), Counter(), [], Counter(), Counter()
        for f in files:
            for row in json.load(open(f)):
                ref = row["S0"]
                for s in [k for k in ("S1", "S2", "S2c", "S3") if k in row]:
                    x = row[s]
                    truncated[s] += x["mismatches"] - len(x["mismatch_detail"])
                    skipped[s] += ref["consumers"] - ref.get("consumers_nondeterministic", 0) - x["consumers"]
                    for m in x["mismatch_detail"]:
                        c = ("live_error" if "error" in (m["kind"], m["S0_kind"])
                             else "value_on_hit" if m["hit"] else "value_on_miss")
                        cls[(s, c)] += 1
                        g = by_run[(row["dataset"], row["query"], row["model"], m["run"])].iloc[m["i"]]
                        try:
                            r = rep.loc[(row["dataset"], g.db_name, g.text)]
                            st, ex = list(r.status_all), [round(float(e), 2) for e in r.exec_s]
                            stable = len(set(r.payload_hashes)) == 1 if r.payload_hashes is not None else None
                            eng = r.engine
                        except KeyError:
                            st, ex, stable, eng = None, None, None, None
                        detail.append({"cell": f"{row['dataset']}/{row['query']}/{row['model']}", "budget": row["budget"],
                                       "system": s, "class": c, "run": m["run"], "i": m["i"], "hit": m["hit"],
                                       "kind": m["kind"], "S0_kind": m["S0_kind"], "engine": eng, "db": g.db_name,
                                       "query": g.text[:300], "replay_status": st, "replay_exec_s": ex,
                                       "replay_3run_byte_stable": stable})
                    for m in x["consumer_mismatch_detail"]:
                        cons[(s, "timeout" if "timeout" in (m["rc"], m["S0_rc"]) else "output")] += 1
        res[ps] = {"mismatch_classes": {f"{s}:{c}": n for (s, c), n in sorted(cls.items())},
                   "unclassified_truncated": dict(truncated),
                   "consumers_skipped_unbound": dict(skipped),
                   "consumer_mismatch_classes": {f"{s}:{c}": n for (s, c), n in sorted(cons.items())},
                   "details": detail}
        print(ps, json.dumps({k: v for k, v in res[ps].items() if k != "details"}), flush=True)
    for ps, r in res.items():
        for d in r["details"]:
            if d["class"] != "live_error":
                print(ps, json.dumps(d), flush=True)
    json.dump(res, open(out, "w"), indent=1, default=str)


if __name__ == "__main__":
    main(*sys.argv[1:])
