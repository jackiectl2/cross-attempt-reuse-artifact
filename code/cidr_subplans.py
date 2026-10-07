"""B6 positioning: close reproduction of the CIDR'26 sub-plan redundancy statistic on DAB.

For every successful DuckDB-native SQL call, take DuckDB's physical plan (EXPLAIN (FORMAT json)),
canonicalize every subtree (operator name + extra info without cardinality estimates + children),
and, per (model, task) cell over its attempts, count total vs. distinct sub-plans by subtree size.
CIDR reported distinct sub-plans as "<10-20% of the total" over 50 attempts per BIRD problem.
Usage: cidr_subplans.py <v2_dir> <dab_repo> <out.json>
"""
import glob
import json
import os
import sys
from collections import Counter, defaultdict

import duckdb
import pandas as pd
import yaml

DROP_KEYS = {"Estimated Cardinality", "Timing", "Cardinality"}


def canon(node, acc):
    """Return (canonical string, size) of a plan subtree; append every subtree to acc."""
    name = node.get("name") or node.get("operator_name") or "?"
    info = node.get("extra_info") or {}
    if isinstance(info, dict):
        info = {k: v for k, v in info.items() if k not in DROP_KEYS}
    kids = [canon(c, acc) for c in node.get("children", [])]
    s = f"{name}{json.dumps(info, sort_keys=True)}(" + ",".join(k[0] for k in kids) + ")"
    size = 1 + sum(k[1] for k in kids)
    acc.append((s, size, name))
    return s, size


def plan_subtrees(con, sql):
    rows = con.execute("EXPLAIN (FORMAT json) " + sql).fetchall()
    plan = json.loads(rows[0][1])
    acc = []
    for root in (plan if isinstance(plan, list) else [plan]):
        canon(root, acc)
    return acc


def main(v2, repo, out):
    calls = pd.read_parquet(f"{v2}/calls.parquet", columns=["model", "dataset", "query", "run", "step", "tool", "db_name", "text", "status"])
    replay_dir = os.environ.get("ED_REPLAY_DIR", f"{v2}/replay")
    rep = pd.concat([pd.read_parquet(f, columns=["dataset", "db_name", "text", "engine", "status"]) for f in glob.glob(f"{replay_dir}/*.parquet")])
    q = calls[calls.tool == "query_db"].merge(rep.rename(columns={"status": "replay_status"}), on=["dataset", "db_name", "text"])
    q = q[(q.engine == "duckdb") & (q.replay_status == "ok")]
    cache, cons = {}, {}
    per_cell = []
    for (ds, qy, mo), cell in q.groupby(["dataset", "query", "model"]):
        cfg = yaml.safe_load(open(os.path.join(repo, f"query_{ds}", "db_config.yaml")))["db_clients"]
        total, distinct = Counter(), defaultdict(set)
        text_total, text_distinct = 0, set()
        for r in cell.itertuples():
            key = (ds, r.db_name, r.text)
            if key not in cache:
                if (ds, r.db_name) not in cons:
                    cons[(ds, r.db_name)] = duckdb.connect(os.path.join(repo, f"query_{ds}", cfg[r.db_name]["db_path"]),
                                                           read_only=True, config={"threads": 1})
                try:
                    cache[key] = plan_subtrees(cons[(ds, r.db_name)], r.text)
                except Exception:
                    cache[key] = None
            text_total += 1
            text_distinct.add(key)
            for s, size, _ in cache[key] or []:
                b = min(size, 6)
                total[b] += 1
                distinct[b].add(s)
        per_cell.append({"dataset": ds, "query": qy, "model": mo, "calls": text_total,
                         "text_distinct_frac": len(text_distinct) / max(text_total, 1),
                         **{f"size{b}_distinct_frac": len(distinct[b]) / total[b] for b in total}})
    df = pd.DataFrame(per_cell)
    summary = {"cells": int(len(df)), "duckdb_calls": int(df.calls.sum()),
               "median_text_distinct_frac": float(df.text_distinct_frac.median()),
               **{c: float(df[c].median()) for c in sorted(df.columns) if c.startswith("size")}}
    json.dump({"summary": summary, "per_cell": per_cell}, open(out, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])
