"""Per-dataset workload summary behind the paper's dataset table.

For each dataset: tasks, runs, engines and databases (db_config.yaml), size on disk of the database files and dumps as
DAB ships them, query_db calls, distinct (database, query) pairs, share of successfully replayed calls whose result
spills, total isolated DB-execution time and payload bytes over all calls, and the median over the dataset's tasks of
the DB-time share repeated from other attempts and within the session (B1_per_task.csv of the analysis directory).

Usage: dataset_table.py <v2> <dab_repo> <analysis_dir> <out.json>     (ED_REPLAY_DIR as for analyze2.py)
"""
import json
import os
import sys

import pandas as pd
import yaml

import analyze2 as A


def disk_bytes(path):
    if os.path.isdir(path):
        return sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs)
    return os.path.getsize(path)


def main(v2, repo, adir, out):
    calls, runs, rep, q = A.load(v2)
    b1 = pd.read_csv(f"{adir}/B1_per_task.csv")
    b1 = b1[b1.weight == "exec_s"].groupby("dataset")[["trials", "session"]].median()
    res = {}
    for ds, g in q.groupby("dataset"):
        cfg = yaml.safe_load(open(os.path.join(repo, f"query_{ds}", "db_config.yaml")))["db_clients"]
        paths = [os.path.join(repo, f"query_{ds}", c.get("sql_file") or c.get("dump_folder") or c.get("db_path"))
                 for c in cfg.values()]
        ok = g[g.ok]
        res[ds] = {"tasks": int(runs[runs.dataset == ds]["query"].nunique()), "runs": int((runs.dataset == ds).sum()),
                   "engines": sorted({c["db_type"] for c in cfg.values()}), "databases": len(cfg),
                   "data_bytes": int(sum(disk_bytes(p) for p in paths)),
                   "calls": int(len(g)), "pairs": int(g.dropna(subset=["db_name", "text"]).key.nunique()),   # complete arguments only
                   "ok_calls": int(len(ok)),
                   "spilled_share_ok": float(ok.spilled.fillna(False).astype(bool).mean()),
                   "exec_s_total": float(g.t_exec.sum()), "payload_bytes_total": float(g.bytes.sum()),
                   "task_median_other_attempts_exec": float(b1.loc[ds, "trials"]),
                   "task_median_session_exec": float(b1.loc[ds, "session"])}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
