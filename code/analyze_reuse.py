"""Decompose agent query redundancy by scope x equivalence x weighting (pilot).

Each query_db call is assigned the innermost scope in which an equivalent call
occurred earlier, under a fixed global order dataset > task > model > run > step:
  session      earlier in the same run
  trials       an earlier run of the same model on the same task
  models       another model on the same task
  tasks        another task on the same dataset
  none         first occurrence
Equivalence keys: exact text, sqlglot-canonical text, result fingerprint (ok queries only).
Weights: calls, replayed execution seconds, result bytes.
Usage: analyze_reuse.py <calls.parquet> <replay_dir> <out_prefix>
"""
import glob
import sys
import warnings

import pandas as pd
import sqlglot

SCOPES = ["session", "trials", "models", "tasks", "none"]


def canon(text, engine):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return sqlglot.parse_one(text, read=engine).sql(dialect=engine, normalize=True, comments=False)
    except Exception:
        return text


def assign_scope(df, key):
    seen_run, seen_mt, seen_t, seen_d = set(), set(), set(), set()
    out = []
    for r in df.itertuples():
        k = getattr(r, key)
        if k is None:
            out.append("none")
            continue
        kr, kmt, kt, kd = (r.model, r.query, r.run, k), (r.model, r.query, k), (r.query, k), k
        if kr in seen_run:
            s = "session"
        elif kmt in seen_mt:
            s = "trials"
        elif kt in seen_t:
            s = "models"
        elif kd in seen_d:
            s = "tasks"
        else:
            s = "none"
        out.append(s)
        seen_run.add(kr); seen_mt.add(kmt); seen_t.add(kt); seen_d.add(kd)
    return out


def main(calls_path, replay_dir, out_prefix):
    rep = pd.concat([pd.read_parquet(f) for f in glob.glob(f"{replay_dir}/*.parquet")], ignore_index=True)
    rep["canon"] = [canon(t, e) for t, e in zip(rep.text, rep.engine)]
    calls = pd.read_parquet(calls_path, columns=["model", "dataset", "query", "run", "step", "tool", "db_name", "text"])
    calls = calls[calls.tool == "query_db"].merge(
        rep[["dataset", "db_name", "text", "engine", "ok", "exec_s", "result_bytes", "result_hash", "canon"]],
        on=["dataset", "db_name", "text"], how="inner")
    calls["k_text"] = calls.db_name + "\x00" + calls.text
    calls["k_canon"] = calls.db_name + "\x00" + calls.canon
    calls["k_result"] = [d + "\x00" + h if ok else None for d, h, ok in zip(calls.db_name, calls.result_hash, calls.ok)]
    calls["bytes"] = calls.result_bytes.fillna(0)
    calls = calls.sort_values(["dataset", "query", "model", "run", "step"]).reset_index(drop=True)
    parts = []
    for _, g in calls.groupby("dataset", sort=False):
        g = g.copy()
        for key in ["k_text", "k_canon", "k_result"]:
            g[f"scope_{key}"] = assign_scope(g, key)
        parts.append(g)
    calls = pd.concat(parts)
    rows = []
    for key in ["k_text", "k_canon", "k_result"]:
        col = f"scope_{key}"
        for w, wcol in [("calls", None), ("exec_s", "exec_s"), ("bytes", "bytes")]:
            weights = calls[wcol] if wcol else pd.Series(1.0, index=calls.index)
            tot = weights.sum()
            share = {s: float(weights[calls[col] == s].sum() / tot) for s in SCOPES}
            rows.append({"equivalence": key[2:], "weight": w, **{s: round(100 * v, 1) for s, v in share.items()},
                         "reusable": round(100 * (1 - share["none"]), 1)})
    res = pd.DataFrame(rows)
    print(f"calls analysed: {len(calls)} of query_db; engines: {calls.engine.value_counts().to_dict()}")
    print(f"total replay exec_s={calls.exec_s.sum():.1f}  total result bytes={calls.bytes.sum()/1e9:.2f} GB")
    print("\n## % of weight by innermost scope of an earlier equivalent call")
    print(res.to_string(index=False))
    print("\n## per model (text equivalence, exec_s weight): % reusable within session / any scope")
    for m, g in calls.groupby("model"):
        w = g.exec_s.sum()
        print(f"{m:18s} session={100*g.exec_s[g.scope_k_text=='session'].sum()/w:5.1f}  "
              f"any={100*g.exec_s[g.scope_k_text!='none'].sum()/w:5.1f}  calls={len(g)}")
    dist = calls.drop_duplicates("k_text")
    top = dist.exec_s.sort_values(ascending=False)
    print(f"\n## heavy tail: top 1% of distinct queries hold {100*top.head(max(1,len(top)//100)).sum()/top.sum():.1f}% of distinct exec time")
    res.to_csv(f"{out_prefix}_matrix.csv", index=False)
    calls.drop(columns=["text", "canon"]).to_parquet(f"{out_prefix}_calls.parquet")


if __name__ == "__main__":
    main(*sys.argv[1:])
