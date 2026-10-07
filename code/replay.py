"""Replay every distinct agent-issued query of one DAB dataset and record its cost.

Execution mirrors DAB's QueryDBTool (read-only SQLite via pandas.read_sql_query,
DuckDB fetchdf, then DAB's own `serialize`), so result bytes equal what the
harness produced. Each distinct (db_name, query text) runs once, cold-ish
(fresh connection per query), with a wall-clock limit.
Usage: replay.py <calls.parquet> <dab_repo> <dataset> <out.parquet> [workers] [timeout_s]
"""
import hashlib
import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor

import duckdb
import pandas as pd
import yaml


def _serialize(repo):
    """Load DAB's own `serialize` verbatim; stub its dotenv/bson imports (unused for SQL engines)."""
    import importlib.util
    import types
    sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
    sys.modules.setdefault("bson", types.SimpleNamespace(ObjectId=type("ObjectId", (), {})))
    path = os.path.join(repo, "common_scaffold", "tools", "db_utils", "db_config.py")
    spec = importlib.util.spec_from_file_location("dab_db_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.serialize


def run_one(task):
    repo, engine, path, sql, timeout = task
    serialize = _serialize(repo)
    out = {"ok": False, "error": None, "exec_s": None, "ser_s": None,
           "rows": None, "result_bytes": None, "result_hash": None}
    t0 = time.perf_counter()
    try:
        if engine == "sqlite":
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            deadline = t0 + timeout
            conn.set_progress_handler(lambda: int(time.perf_counter() > deadline), 100000)
            try:
                df = pd.read_sql_query(sql, conn)
            finally:
                conn.close()
        else:
            conn = duckdb.connect(database=path, read_only=True)
            timer = threading.Timer(timeout, conn.interrupt)
            timer.start()
            try:
                df = conn.execute(sql).fetchdf()
            finally:
                timer.cancel()
                conn.close()
        t1 = time.perf_counter()
        payload = json.dumps(serialize(df))
        t2 = time.perf_counter()
        out.update(ok=True, exec_s=t1 - t0, ser_s=t2 - t1, rows=len(df), result_bytes=len(payload),
                   result_hash=hashlib.blake2b(payload.encode(), digest_size=16).hexdigest())
    except Exception as e:  # agent queries fail in many ways; record, do not stop
        out.update(error=f"{type(e).__name__}: {str(e)[:300]}", exec_s=time.perf_counter() - t0)
    return out


def main(calls, repo, dataset, out_path, workers=8, timeout=120.0):
    repo = os.path.abspath(repo)
    ds_dir = os.path.join(repo, f"query_{dataset}")
    cfg = yaml.safe_load(open(os.path.join(ds_dir, "db_config.yaml")))["db_clients"]
    c = pd.read_parquet(calls, columns=["dataset", "tool", "db_name", "text"])
    c = c[(c.dataset == dataset) & (c.tool == "query_db") & c.text.notna()]
    distinct = c.groupby(["db_name", "text"]).size().reset_index(name="n_calls")
    tasks, keep = [], []
    for r in distinct.itertuples():
        client = cfg.get(r.db_name)
        if not client or client["db_type"] not in ("sqlite", "duckdb"):
            continue
        tasks.append((repo, client["db_type"], os.path.join(ds_dir, client["db_path"]), r.text, float(timeout)))
        keep.append((r.db_name, r.text, client["db_type"], r.n_calls))
    print(f"{dataset}: {len(distinct)} distinct texts, {len(tasks)} on sqlite/duckdb", flush=True)
    with ProcessPoolExecutor(max_workers=int(workers)) as ex:
        res = list(ex.map(run_one, tasks, chunksize=4))
    rows = [{"dataset": dataset, "db_name": k[0], "text": k[1], "engine": k[2], "n_calls": k[3], **r}
            for k, r in zip(keep, res)]
    pd.DataFrame(rows).to_parquet(out_path)
    df = pd.DataFrame(rows)
    print(df.groupby("engine").agg(n=("ok", "size"), ok=("ok", "mean"), exec_s=("exec_s", "sum"),
                                   bytes=("result_bytes", "sum")).to_string(), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
