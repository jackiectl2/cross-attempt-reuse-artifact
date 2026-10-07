"""X7 clean-room check: compare a from-scratch re-parse + re-analysis with the reported outputs.

  tables  <v2_reported> <v2_fresh>      calls/runs tables equal row for row (after sorting), parse metadata equal
  results <dir_reported> <dir_fresh>    every numeric leaf of results_B1.json / results_B23.json equal (rel. tol 1e-9)
Writes a JSON report next to the fresh directory and exits non-zero on any difference.
"""
import json
import math
import os
import sys

import pandas as pd


def leaves(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items():
            yield from leaves(v, f"{path}/{k}")
    elif isinstance(x, list):
        for i, v in enumerate(x):
            yield from leaves(v, f"{path}[{i}]")
    else:
        yield path, x


def cmd_tables(rep, fresh):
    out = {}
    for t, keys in (("calls", ["model", "dataset", "query", "run", "step"]), ("runs", ["model", "dataset", "query", "run"])):
        a = pd.read_parquet(f"{rep}/{t}.parquet").sort_values(keys).reset_index(drop=True)
        b = pd.read_parquet(f"{fresh}/{t}.parquet").sort_values(keys).reset_index(drop=True)
        same_cols = list(a.columns) == list(b.columns)
        equal = same_cols and len(a) == len(b) and a.astype(str).equals(b.astype(str))
        out[t] = {"rows_reported": len(a), "rows_fresh": len(b), "same_columns": same_cols, "equal": bool(equal)}
    out["parse_meta_equal"] = json.load(open(f"{rep}/parse_meta.json")) == json.load(open(f"{fresh}/parse_meta.json"))
    json.dump(out, open(f"{fresh}/reproduce_tables.json", "w"), indent=1)
    print(json.dumps(out, indent=1))
    return all(v["equal"] for k, v in out.items() if isinstance(v, dict)) and out["parse_meta_equal"]


def cmd_results(rep, fresh):
    out, ok = {}, True
    for f in ("results_B1.json", "results_B23.json"):
        a = dict(leaves(json.load(open(f"{rep}/{f}"))))
        b = dict(leaves(json.load(open(f"{fresh}/{f}"))))
        diffs = []
        for k, v in a.items():
            w = b.get(k, "<missing>")
            if isinstance(v, (int, float)) and isinstance(w, (int, float)):
                if not (v == w or (math.isnan(v) and math.isnan(w)) or abs(v - w) <= 1e-9 * max(1.0, abs(v))):
                    diffs.append((k, v, w))
            elif v != w:
                diffs.append((k, v, w))
        diffs += [(k, "<missing>", b[k]) for k in b.keys() - a.keys() if not k.endswith("/seed_offset")]   # field added later
        out[f] = {"leaves": len(a), "differences": len(diffs), "examples": diffs[:20]}
        ok &= not diffs
    json.dump(out, open(f"{fresh}/reproduce_results.json", "w"), indent=1, default=str)
    print(json.dumps(out, indent=1, default=str))
    return ok


if __name__ == "__main__":
    cmd, a, b = sys.argv[1:4]
    sys.exit(0 if {"tables": cmd_tables, "results": cmd_results}[cmd](a, b) else 1)
