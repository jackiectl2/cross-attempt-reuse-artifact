"""Isolated cost table: execute each distinct agent query of one dataset shard, one at a time.

Mirrors DAB's QueryDBTool per engine (SQLite read-only + pandas, DuckDB fetchdf,
PostgreSQL via SQLAlchemy read-only, MongoDB find(filter, projection).limit(limit
default 5)), then DAB's own `serialize`. Pinned runtime config: DuckDB threads=1;
PostgreSQL max_parallel_workers_per_gather=0. Each query runs REPEATS times; a
timeout on the first execution is not repeated.
Usage: replay2.py <calls.parquet> <dab_repo> <dataset> <shard> <n_shards> <out.parquet> [repeats] [timeout_s]
Env: PGHOST/PGPORT/PGUSER for PostgreSQL datasets; MONGO_URI for MongoDB datasets.
"""
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import zlib

import duckdb
import pandas as pd
import yaml

PREVIEW = 10000
BAG_HASH_MAX_CHARS = 20_000_000  # order-instability diagnostic skipped above this size


def load_serialize(repo):
    sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
    path = os.path.join(repo, "common_scaffold", "tools", "db_utils", "db_config.py")
    spec = importlib.util.spec_from_file_location("dab_db_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.serialize


def h(s):
    return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


class Engines:
    def __init__(self, ds_dir, cfg, timeout):
        self.ds_dir, self.cfg, self.timeout = ds_dir, cfg, timeout
        self._pg = {}
        self._mongo = None

    def execute(self, db_name, sql):
        """Return a DataFrame (or raise). Timeouts raise TimeoutError."""
        c = self.cfg[db_name]
        t = c["db_type"]
        if t == "sqlite":
            conn = sqlite3.connect(f"file:{os.path.join(self.ds_dir, c['db_path'])}?mode=ro", uri=True)
            deadline = time.perf_counter() + self.timeout
            conn.set_progress_handler(lambda: int(time.perf_counter() > deadline), 100000)
            try:
                return pd.read_sql_query(sql, conn)
            except Exception as e:
                if time.perf_counter() > deadline:
                    raise TimeoutError("sqlite timeout") from e
                raise
            finally:
                conn.close()
        if t == "duckdb":
            conn = duckdb.connect(database=os.path.join(self.ds_dir, c["db_path"]), read_only=True,
                                  config={"threads": 1})
            timer = threading.Timer(self.timeout, conn.interrupt)
            timer.start()
            try:
                return conn.execute(sql).fetchdf()
            except Exception as e:
                if not timer.is_alive():
                    raise TimeoutError("duckdb timeout") from e
                raise
            finally:
                timer.cancel()
                conn.close()
        if t == "postgres":
            import sqlalchemy
            name = c["db_name"]
            if name not in self._pg:
                opts = ("-c default_transaction_read_only=on -c max_parallel_workers_per_gather=0 "
                        f"-c statement_timeout={int(self.timeout * 1000)}")
                uri = (f"postgresql+psycopg2://{os.environ['PGUSER']}@/{name}"
                       f"?host={os.environ['PGHOST']}&port={os.environ['PGPORT']}&options={opts.replace(' ', '%20')}")
                self._pg[name] = sqlalchemy.create_engine(uri)
            with self._pg[name].connect() as conn:
                try:
                    return pd.read_sql(sqlalchemy.text(sql), conn)
                except Exception as e:
                    if "statement timeout" in str(e):
                        raise TimeoutError("postgres timeout") from e
                    raise
        if t == "mongo":
            from pymongo import MongoClient
            from pymongo.errors import ExecutionTimeout
            if self._mongo is None:
                self._mongo = MongoClient(os.environ["MONGO_URI"])
            q = json.loads(sql)
            if "collection" not in q:
                raise ValueError("Invalid Mongo query: missing required field `collection`")
            db = self._mongo[c["db_name"]]
            if q["collection"] not in db.list_collection_names():
                raise ValueError(f"Collection does not exist: {q['collection']}")
            cur = db[q["collection"]].find(q.get("filter", {}), q.get("projection", None),
                                           max_time_ms=int(self.timeout * 1000))
            limit = q.get("limit", 5)
            if limit is not None:
                cur = cur.limit(limit)
            try:
                rows = list(cur)
            except ExecutionTimeout as e:
                raise TimeoutError("mongo timeout") from e
            return pd.DataFrame(rows) if rows else pd.DataFrame()
        raise ValueError(f"unsupported db_type {t}")


def run_once(eng, serialize, db_name, text, spill_dir):
    r = {"status": "ok", "error": None, "exec_s": None, "ser_s": None, "spill_s": 0.0}
    t0 = time.perf_counter()
    try:
        df = eng.execute(db_name, text)
    except TimeoutError as e:
        r.update(status="timeout", error=str(e), exec_s=time.perf_counter() - t0)
        return r, None
    except Exception as e:  # agent queries fail in many ways; record the class, do not stop
        r.update(status="error", error=f"{type(e).__name__}: {str(e)[:300]}", exec_s=time.perf_counter() - t0)
        return r, None
    t1 = time.perf_counter()
    result = serialize(df)
    payload = json.dumps(result)
    t2 = time.perf_counter()
    r.update(exec_s=t1 - t0, ser_s=t2 - t1, rows=len(df), chars=len(payload),
             utf8_bytes=len(payload.encode("utf-8", "surrogatepass")), payload_hash=h(payload),
             bag_hash=(None if len(payload) > BAG_HASH_MAX_CHARS else
                       h("\n".join(sorted(json.dumps(x, sort_keys=True) for x in result))) if isinstance(result, list) else h(payload)),
             spilled=len(payload) > PREVIEW)
    if r["spilled"]:
        t3 = time.perf_counter()
        with tempfile.NamedTemporaryFile("w", dir=spill_dir, suffix=".json", delete=True, encoding="utf-8") as f:
            json.dump(result, f, indent=2)
            f.flush()
            r["spill_bytes"] = f.tell()
        r["spill_s"] = time.perf_counter() - t3
    return r, payload


def main(calls_path, repo, dataset, shard, n_shards, out_path, repeats=3, timeout=120.0):
    shard, n_shards, repeats, timeout = int(shard), int(n_shards), int(repeats), float(timeout)
    assert timeout >= 120.0 or os.environ.get("ED_SMOKE") == "1", "preregistered cap is 120 s (smoke runs set ED_SMOKE=1)"
    repo = os.path.abspath(repo)
    ds_dir = os.path.join(repo, f"query_{dataset}")
    cfg = yaml.safe_load(open(os.path.join(ds_dir, "db_config.yaml")))["db_clients"]
    serialize = load_serialize(repo)
    c = pd.read_parquet(calls_path, columns=["dataset", "tool", "db_name", "text"])
    c = c[(c.dataset == dataset) & (c.tool == "query_db") & c.text.notna() & c.db_name.notna()]
    d = c.groupby(["db_name", "text"]).size().reset_index(name="n_calls")
    d = d[[zlib.crc32((a + "\x00" + b).encode()) % n_shards == shard for a, b in zip(d.db_name, d.text)]]
    eng = Engines(ds_dir, cfg, timeout)
    spill_dir = tempfile.mkdtemp(prefix=f"spill-{dataset}-{shard}-")
    rows = []
    t_start = time.time()
    for k, q in enumerate(d.itertuples()):
        engine = cfg.get(q.db_name, {}).get("db_type", "unknown")
        rec = {"dataset": dataset, "db_name": q.db_name, "engine": engine, "text": q.text, "n_calls": q.n_calls,
               "timeout_s": timeout, "repeats_requested": repeats}
        if engine == "unknown":
            rec.update(status="unknown_db", reps=0)
            rows.append(rec)
            continue
        reps, head = [], None
        for i in range(repeats):
            r, payload = run_once(eng, serialize, q.db_name, q.text, spill_dir)
            reps.append(r)
            if i == 0 and payload is not None:
                head = payload[:PREVIEW]
            if r["status"] == "timeout":
                break
        first = reps[0]
        ok_reps = [r for r in reps if r["status"] == "ok"]
        rec.update(status=first["status"], error=first["error"], reps=len(reps),
                   exec_s=[r["exec_s"] for r in reps], ser_s=[r["ser_s"] for r in reps], spill_s=[r["spill_s"] for r in reps],
                   payload_head=head,
                   rows=first.get("rows"), chars=first.get("chars"), utf8_bytes=first.get("utf8_bytes"),
                   spilled=first.get("spilled"), spill_bytes=first.get("spill_bytes"),
                   payload_hashes=[r.get("payload_hash") for r in ok_reps], bag_hashes=[r.get("bag_hash") for r in ok_reps],
                   status_all=[r["status"] for r in reps])
        rows.append(rec)
        if k % 200 == 0:
            print(f"{dataset}[{shard}/{n_shards}] {k}/{len(d)} elapsed={time.time()-t_start:.0f}s", flush=True)
    pd.DataFrame(rows).to_parquet(out_path)
    df = pd.DataFrame(rows)
    print(df.groupby(["engine", "status"]).size().to_string(), flush=True)
    print(f"done {dataset}[{shard}/{n_shards}] n={len(df)} wall={time.time()-t_start:.0f}s", flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
