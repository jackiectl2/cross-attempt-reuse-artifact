"""X1 cross-harness validation on public DAB leaderboard traces (5 trials per query, one model per harness).

Extracts every database call (dataset, query, run, step, database, query text, result size, timestamps) from:
  qwen     DAB scaffold, Qwen3.8-Flash-Next (PR #99)   final_agent.json per run, DAB message format
  scout    Scout (OceanBase Lab), GLM-5.2 (PR #96)      inline source records of DAB-style final_agent.json
  stela    Stela, DeepSeek-v4.1-flash (PR #103)        messages with toolCall / toolResult parts, ms timestamps
  phoenix  Phoenix-CLI, Claude Opus 5 (PR #94)          Claude Code stream; SQL inside `dab_db query <db> "<sql>"`
Then: scope decomposition by calls and result characters (session / other attempts of the same query / other
queries of the dataset / none) over random attempt orders; exact-repeat reuse potential for N = 2..5 sampled
attempts; LLM latency per turn where traces carry timestamps. No query is executed (no cost weights).

Usage: xharness.py extract <work_dir_with_extracted_archives> <out.parquet>
       xharness.py analyze <calls.parquet> <out.json>
       xharness.py dab-main <v2/calls.parquet> <out.parquet> [max_runs]   (released DAB traces, first 5 runs)
       xharness.py replay-input <calls.parquet> <dab_replay_dir> <out.parquet>
       xharness.py costs <calls.parquet> <dab_main5.parquet> <replay_dir>[,<replay_dir>...] <out.json>
(replay-input and costs were added after the first analysis, at the result-to-claim review of 2026-10-02: the harness
queries are replayed with replay2.py in the same pinned runtime as the DAB cost table, so cost weights exist.)
"""
import glob
import json
import os
import re
import shlex
import sys
from datetime import datetime

import numpy as np
import pandas as pd

DB_TOOLS = {"query_db", "query_database", "run_query", "sql_query", "execute_sql", "query"}


def _s(x):
    return x if isinstance(x, str) else (json.dumps(x) if x is not None else None)


def dab_calls(msgs, meta):
    """DAB message format; tool calls paired with the tool message answering them after the same turn."""
    out, answer_of = [], {}
    for turn, m in enumerate(msgs):
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        pending = list(m["tool_calls"])
        for later in msgs[turn + 1:]:
            if later.get("role") == "assistant":
                break
            if later.get("role") != "tool" or not pending:
                continue
            j = next((i for i, tc in enumerate(pending) if tc.get("id") == later.get("tool_call_id")), 0)
            answer_of[(turn, id(pending[j]))] = later
            pending.pop(j)
    step = 0
    for turn, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") == "query_db":
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (json.JSONDecodeError, TypeError):
                    args = {}
                args = args if isinstance(args, dict) else {}
                res = _s((answer_of.get((turn, id(tc))) or {}).get("content")) or ""
                out.append({**meta, "step": step, "turn": turn, "tool": "query_db", "db_name": _s(args.get("db_name")),
                            "text": _s(args.get("query") or args.get("sql")), "result_chars": len(res)})
            step += 1
    return out


def ex_qwen(root):
    rows = []
    for f in glob.glob(f"{root}/qwen/*/query_*/query*/logs/data_agent/*_run_*/final_agent.json"):
        parts = f.split("/")
        ds = parts[-6][len("query_"):]
        q = parts[-5][len("query"):]
        run = int(parts[-2].rsplit("_run_", 1)[1])
        d = json.load(open(f))
        rows += dab_calls(d.get("messages") or [], {"harness": "qwen", "model": "qwen3.8-flash-next", "dataset": ds,
                                                    "query": q, "run": run})
    return rows


def ex_scout(root):
    files, recs = {}, {}
    for line in open(f"{root}/scout_submission/traces.jsonl"):
        r = json.loads(line)
        if r.get("record_type") == "source_record" and r.get("source_file", "").endswith("final_agent.json"):
            recs.setdefault((r["dataset"], r["query"], r["run"], r["source_file"]), []).append((r["record_index"], r["data"]))
    rows = []
    for (ds, q, run, sf), parts in recs.items():
        parts.sort(key=lambda x: x[0])
        merged, msgs = {}, []
        for _, d in parts:
            if isinstance(d, dict) and "role" in d:
                msgs.append(d)
            elif isinstance(d, dict):
                if isinstance(d.get("messages"), list):
                    msgs += d["messages"]
                merged.update({k: v for k, v in d.items() if k != "messages"})
        rows += dab_calls(msgs, {"harness": "scout", "model": "glm-5.2", "dataset": ds, "query": str(q), "run": int(run)})
    return rows


def ex_stela(root):
    rows = []
    for line in open(f"{root}/stela/traces.jsonl"):
        d = json.loads(line)
        meta = {"harness": "stela", "model": "deepseek-v4.1-flash", "dataset": d["dataset"], "query": str(d["query"]),
                "run": int(d["run"])}
        res_of, t_res = {}, {}
        for m in d["messages"]:
            if m.get("role") == "toolResult":
                cid = m.get("toolCallId") or m.get("tool_call_id") or m.get("id")
                txt = "".join(p.get("text", "") for p in m["content"]) if isinstance(m["content"], list) else str(m["content"])
                res_of[cid], t_res[cid] = txt, m.get("timestamp")
        step = 0
        for turn, m in enumerate(d["messages"]):
            if m.get("role") != "assistant" or not isinstance(m.get("content"), list):
                continue
            for p in m["content"]:
                if p.get("type") != "toolCall":
                    continue
                name = p.get("name") or ""
                if name not in DB_TOOLS:                 # e.g. Stela's `search_skills` is not a database call
                    step += 1
                    continue
                args = p.get("arguments") or p.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                text = args.get("query") or args.get("sql") if isinstance(args, dict) else None
                if text:
                    cid = p.get("id")
                    rows.append({**meta, "step": step, "turn": turn, "tool": name, "db_name": _s(args.get("db_name") or args.get("database")),
                                 "text": _s(text), "result_chars": len(res_of.get(cid, "")),
                                 "t_issue": m.get("timestamp"), "t_result": t_res.get(cid)})
                step += 1
    return rows


DAB_DB_RE = re.compile(r"""dab_db\s+query\s+(\S+)\s+""")


def _dab_db_queries(cmd):
    """(db, sql) pairs from `dab_db query <db> "<sql>"` invocations inside a shell command."""
    out = []
    for seg in re.split(r";\s*(?=dab_db|echo)|&&|\n(?=dab_db)", cmd):
        seg = seg.strip()
        if not seg.startswith("dab_db query"):
            continue
        try:
            tok = shlex.split(seg)
        except ValueError:
            continue
        if len(tok) >= 4 and tok[0] == "dab_db" and tok[1] == "query":
            out.append((tok[2], tok[3]))
    return out


def _ts(x):
    try:
        return datetime.fromisoformat(x.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return None


def ex_phoenix(root):
    rows = []
    for f in glob.glob(f"{root}/phoenix/query_*/query*/run_*/stream.jsonl"):
        parts = f.split("/")
        ds, q, run = parts[-4][len("query_"):], parts[-3][len("query"):], int(parts[-2][len("run_"):])
        meta = {"harness": "phoenix", "model": "claude-opus-5", "dataset": ds, "query": q, "run": run}
        events = [json.loads(l) for l in open(f)]
        res_of = {}
        for e in events:
            if e.get("type") == "user":
                for p in (e.get("message") or {}).get("content") or []:
                    if isinstance(p, dict) and p.get("type") == "tool_result":
                        c = p.get("content")
                        txt = c if isinstance(c, str) else "".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
                        res_of[p.get("tool_use_id")] = (txt, _ts(e.get("timestamp", "")))
        step = 0
        for turn, e in enumerate(events):
            if e.get("type") != "assistant":
                continue
            for p in (e.get("message") or {}).get("content") or []:
                if p.get("type") != "tool_use":
                    continue
                cmd = (p.get("input") or {}).get("command") or ""
                qs = _dab_db_queries(cmd) if p.get("name") == "Bash" else []
                txt, tr = res_of.get(p.get("id"), ("", None))
                for db, sql in qs:
                    rows.append({**meta, "step": step, "turn": turn, "tool": "dab_db", "db_name": db, "text": sql,
                                 "result_chars": len(txt) / max(len(qs), 1), "t_issue": _ts(e.get("timestamp", "")), "t_result": tr})
                    step += 1
    return rows


SOURCES = {
    "qwen": {"pr": 99, "model": "qwen3.8-flash-next", "harness": "DAB scaffold (hints)",
             "url": "https://github.com/qiboyu301-crypto/DataAgentBench/releases/download/qwen38flash-parallel-v1-traces/qwen38flash_parallel_v1-traces.tar.gz",
             "file": "qwen38flash_dab.tar.gz"},
    "scout": {"pr": 96, "model": "glm-5.2", "harness": "Scout (OceanBase Lab)",
              "url": "https://github.com/WeiJiuQi/DataAgentBench/releases/download/scout-traces-20260908/scout_trace.tar.xz",
              "file": "scout_glm52.tar.xz"},
    "stela": {"pr": 103, "model": "deepseek-v4.1-flash", "harness": "Stela",
              "url": "https://github.com/user-attachments/files/32157381/stela-leaderboard-submission.zip",
              "file": "stela_dsv41.zip"},
    "phoenix": {"pr": 94, "model": "claude-opus-5", "harness": "Phoenix-CLI (Claude Code)",
                "url": "https://github.com/marc-shade/DataAgentBench/releases/download/phoenix-cli-traces-2026-09-04/traces.tar.gz",
                "file": "phoenix_cli.tar.gz"},
}


def cmd_extract(root, out, archive_dir=None):
    rows = []
    for name, fn in (("qwen", ex_qwen), ("scout", ex_scout), ("stela", ex_stela), ("phoenix", ex_phoenix)):
        r = fn(root)
        print(name, "calls", len(r), "runs", len({(x["dataset"], x["query"], x["run"]) for x in r}), flush=True)
        rows += r
    df = pd.DataFrame(rows)
    df["query"] = df["query"].astype(str)
    df.to_parquet(out)
    prov = {}
    for h, src in SOURCES.items():
        g = df[df.harness == h]
        f = os.path.join(archive_dir, src["file"]) if archive_dir else None
        prov[h] = {**src, "downloaded": "2026-10-01", "archive_bytes": os.path.getsize(f) if f and os.path.exists(f) else None,
                   "runs": int(g[["dataset", "query", "run"]].drop_duplicates().shape[0]), "db_calls": int(len(g)),
                   "tool_counts": g.tool.value_counts(dropna=False).to_dict()}
    json.dump(prov, open(out.replace(".parquet", "_provenance.json"), "w"), indent=1, default=str)


def cmd_dab_main(calls_path, out, max_runs=5):
    """The released DAB traces in the same schema, restricted to the first `max_runs` runs per (model, query) so that
    the comparison with 5-trial leaderboard submissions uses the same number of attempts."""
    c = pd.read_parquet(calls_path, columns=["model", "dataset", "query", "run", "step", "turn", "tool", "db_name", "text",
                                             "message_chars"])
    c = c[(c.tool == "query_db") & (c.run < int(max_runs))].copy()
    c["harness"] = "dab:" + c.model
    c["result_chars"] = c.message_chars
    c["query"] = c["query"].astype(str).str.replace("query", "", regex=False)
    c.drop(columns=["message_chars"]).to_parquet(out)
    print(c.groupby("harness").size().to_dict())


def _scope_shares(g, rng, n_orders, weights=()):
    """Innermost scope of each call under random attempt orders: session / trials / tasks / none."""
    att = g[["query", "run"]].drop_duplicates().itertuples(index=False, name=None)
    att = list(att)
    K = (g.db_name.fillna("") + "\x00" + g.text.fillna("")).tolist()
    Q, R, S = g["query"].tolist(), g.run.tolist(), g.step.tolist()
    W = {"calls": np.ones(len(g)), "result_chars": g.result_chars.fillna(0).values}
    W.update({w: g[w].fillna(0).values for w in weights})
    acc = {w: np.zeros(4) for w in W}
    for _ in range(n_orders):
        rank = dict(zip(att, rng.permutation(len(att))))
        order = sorted(range(len(K)), key=lambda i: (rank[(Q[i], R[i])], S[i]))
        seen_run, seen_q, seen_d = set(), set(), set()
        code = np.empty(len(K), dtype=int)
        for i in order:
            k = K[i]
            code[i] = 0 if (Q[i], R[i], k) in seen_run else 1 if (Q[i], k) in seen_q else 2 if k in seen_d else 3
            seen_run.add((Q[i], R[i], k)); seen_q.add((Q[i], k)); seen_d.add(k)
        for w, wv in W.items():
            acc[w] += np.array([wv[code == s].sum() for s in range(4)])
    return {w: dict(zip(("session", "other_attempts", "other_queries", "none"), (v / v.sum()).round(4).tolist()))
            for w, v in acc.items() if v.sum() > 0}


def cmd_analyze(calls_path, out, n_orders=50, n_res=50):
    n_orders, n_res = int(n_orders), int(n_res)
    df = pd.read_parquet(calls_path)
    df = df[df.text.notna()]
    res = {}
    for h, g in df.groupby("harness"):
        rng = np.random.default_rng(3)
        shares = {}
        tot = {w: np.zeros(4) for w in ("calls", "result_chars")}
        for ds, gd in g.groupby("dataset"):
            sh = _scope_shares(gd.reset_index(drop=True), rng, n_orders)
            for w in sh:
                weight = len(gd) if w == "calls" else gd.result_chars.sum()
                tot[w] += np.array(list(sh[w].values())) * weight
        shares = {w: dict(zip(("session", "other_attempts", "other_queries", "none"), (v / v.sum()).round(4).tolist()))
                  for w, v in tot.items() if v.sum() > 0}
        # task-level: other-attempt vs session share of calls per (dataset, query)
        task_rows = []
        for (ds, qy), gt in g.groupby(["dataset", "query"]):
            sh = _scope_shares(gt.reset_index(drop=True), np.random.default_rng(5), 20)
            task_rows.append({"calls_session": sh["calls"]["session"], "calls_other_attempts": sh["calls"]["other_attempts"],
                              **({"bytes_session": sh["result_chars"]["session"], "bytes_other_attempts": sh["result_chars"]["other_attempts"]}
                                 if "result_chars" in sh else {})})
        tr = pd.DataFrame(task_rows)
        # exact-repeat reuse potential within N sampled attempts of one query (S2 scope), by calls and bytes
        curve = {}
        for N in (1, 2, 3, 4, 5):
            vals_c, vals_b = [], []
            for ti, ((ds, qy), gt) in enumerate(g.groupby(["dataset", "query"])):   # sorted groups: stable task index
                runs_ = sorted(gt.run.unique())
                if len(runs_) < N:
                    continue
                rng = np.random.default_rng([7, N, ti])
                c_hit = c_all = b_hit = b_all = 0.0
                for _ in range(n_res):
                    sel = set(rng.permutation(runs_)[:N].tolist())
                    gs = gt[gt.run.isin(sel)].sort_values(["run", "step"])
                    seen = set()
                    for k, b in zip(gs.db_name.fillna("") + "\x00" + gs.text, gs.result_chars.fillna(0)):
                        c_all += 1; b_all += b
                        if k in seen:
                            c_hit += 1; b_hit += b
                        seen.add(k)
                vals_c.append(c_hit / max(c_all, 1)); vals_b.append(b_hit / max(b_all, 1))
            curve[N] = {"median_task_call_reuse": float(np.median(vals_c)), "median_task_byte_reuse": float(np.median(vals_b)),
                        "tasks": len(vals_c)}
        # LLM latency per turn: time from a tool result to the next tool call of the same run
        lat = []
        if "t_issue" in g and g.t_issue.notna().any():
            for _, gr in g.sort_values(["dataset", "query", "run", "step"]).groupby(["dataset", "query", "run"]):
                ti, trs = gr.t_issue.values, gr.t_result.values
                for i in range(1, len(gr)):
                    if ti[i] != ti[i - 1] and trs[i - 1] is not None and not pd.isna(trs[i - 1]) and not pd.isna(ti[i]):
                        d = (ti[i] - trs[i - 1]) / 1000.0
                        if 0 <= d < 3600:
                            lat.append(d)
        res[h] = {"model": g.model.iloc[0], "runs": int(g[["dataset", "query", "run"]].drop_duplicates().shape[0]),
                  "queries": int(g[["dataset", "query"]].drop_duplicates().shape[0]), "db_calls": int(len(g)),
                  "scope_shares": shares,
                  "task_median": {c: float(tr[c].median()) for c in tr.columns},
                  "reuse_curve": curve,
                  "llm_latency_s": ({"n": len(lat), "p25": float(np.percentile(lat, 25)), "median": float(np.median(lat)),
                                     "p75": float(np.percentile(lat, 75))} if lat else None)}
        print(h, json.dumps(res[h], default=float)[:1200], flush=True)
    json.dump(res, open(out, "w"), indent=1, default=float)


def cmd_replay_input(calls_path, replay_dir, out):
    """Distinct (dataset, database, query text) pairs of the harness traces that the DAB cost table does not contain,
    in replay2.py's input schema (tool set to query_db: replay2 executes every pair with DAB's QueryDBTool semantics)."""
    x = pd.read_parquet(calls_path)
    x = x[x.text.notna() & x.db_name.notna()]
    rep = pd.concat([pd.read_parquet(f, columns=["dataset", "db_name", "text"]) for f in sorted(glob.glob(f"{replay_dir}/*.parquet"))])
    known = set(zip(rep.dataset, rep.db_name, rep.text))
    d = x[["dataset", "db_name", "text"]].drop_duplicates()
    new = d[[k not in known for k in zip(d.dataset, d.db_name, d.text)]].assign(tool="query_db").reset_index(drop=True)
    new.to_parquet(out)
    print("distinct pairs", len(d), "in DAB cost table", len(d) - len(new), "to replay", len(new),
          new.groupby("dataset").size().to_dict(), flush=True)


def cmd_costs(calls_path, main5_path, replay_dirs, out, n_orders=50, n_res=50):
    """Cost-weighted repeat of `analyze` for the four harnesses and the released DAB traces (first 5 runs per cell).
    Each call gets the isolated-replay record of its (dataset, database, text) in the pinned runtime (replay2.py: DAB
    QueryDBTool semantics, 3 runs, 120 s cap). Cacheable = replay succeeded 3/3 with byte-identical results
    ("replay-stable"); there is no comparison with the harness's own observation (the traces keep only its size).
    Within the replay-stable calls: scope shares weighted by completed execution time and DAB-serialized payload bytes,
    and the task-shared (S2, unbounded) execution-time saving within N sampled attempts, summed over resamples per
    task (as analyze2.summarize_sim), median over tasks."""
    import analyze2 as A
    n_orders, n_res = int(n_orders), int(n_res)
    cols = ["dataset", "db_name", "text", "engine", "status", "status_all", "exec_s", "payload_hashes", "bag_hashes", "utf8_bytes"]
    rep = pd.concat([pd.read_parquet(f, columns=cols) for d in replay_dirs.split(",")
                     for f in sorted(glob.glob(f"{d}/*.parquet"))], ignore_index=True)
    assert not rep.duplicated(["dataset", "db_name", "text"]).any()
    rep["cls"] = rep.apply(A.outcome_class, axis=1)
    rep["exec_med"] = [A.med([e for s_, e in zip(st, ex) if s_ != "timeout"]) if st is not None and ex is not None else np.nan
                       for st, ex in zip(rep.status_all, rep.exec_s)]
    df = pd.concat([pd.read_parquet(calls_path), pd.read_parquet(main5_path)], ignore_index=True)
    df = df[df.text.notna() & df.db_name.notna()].merge(rep.drop(columns=["status_all", "exec_s", "payload_hashes", "bag_hashes"]),
                                                         on=["dataset", "db_name", "text"], how="left")
    df["stable"] = (df.status == "ok") & (df.cls == "stable")
    df["exec_s"] = np.where(df.status.notna() & (df.status != "timeout"), df.exec_med.fillna(0), 0.0)
    df["payload_bytes"] = np.where(df.status == "ok", df.utf8_bytes.fillna(0), 0.0)
    scopes = ("session", "other_attempts", "other_queries", "none")
    res = {}
    for h, g in df.groupby("harness"):
        gs = g[g.stable]
        rng = np.random.default_rng(3)
        tot = {w: np.zeros(4) for w in ("calls", "exec_s", "payload_bytes")}
        for ds, gd in gs.groupby("dataset"):
            sh = _scope_shares(gd.reset_index(drop=True), rng, n_orders, weights=("exec_s", "payload_bytes"))
            for w in tot:
                if w in sh:
                    tot[w] += np.array([sh[w][s] for s in scopes]) * (len(gd) if w == "calls" else gd[w].sum())
        task_rows = []
        for (ds, qy), gt in gs.groupby(["dataset", "query"]):
            if gt.exec_s.sum() <= 0:
                continue
            sh = _scope_shares(gt.reset_index(drop=True), np.random.default_rng(5), 20, weights=("exec_s",))
            task_rows.append({"session": sh["exec_s"]["session"], "other_attempts": sh["exec_s"]["other_attempts"]})
        tr = pd.DataFrame(task_rows)
        curve = {}
        for N in (2, 3, 4, 5):
            vals_t, vals_b = [], []
            for ti, ((ds, qy), gt) in enumerate(gs.groupby(["dataset", "query"])):
                runs_ = sorted(g[(g.dataset == ds) & (g["query"] == qy)].run.unique())
                if len(runs_) < N:
                    continue
                rng = np.random.default_rng([11, N, ti])
                t_hit = t_all = b_hit = b_all = 0.0
                for _ in range(n_res):
                    sel = set(rng.permutation(runs_)[:N].tolist())
                    gsel = gt[gt.run.isin(sel)].sort_values(["run", "step"])
                    seen = set()
                    for k, t, b in zip(gsel.db_name + "\x00" + gsel.text, gsel.exec_s, gsel.payload_bytes):
                        t_all += t; b_all += b
                        if k in seen:
                            t_hit += t; b_hit += b
                        seen.add(k)
                if t_all > 0:
                    vals_t.append(t_hit / t_all)
                if b_all > 0:
                    vals_b.append(b_hit / b_all)
            curve[N] = {"median_task_S2_exec_saving": float(np.median(vals_t)) if vals_t else None, "tasks": len(vals_t),
                        "median_task_S2_payload_saving": float(np.median(vals_b)) if vals_b else None}
        res[h] = {"db_calls": int(len(g)),
                  "replay": {"replayed": float(g.status.notna().mean()), "ok": float((g.status == "ok").mean()),
                             "replay_stable": float(g.stable.mean()),
                             "status_calls": g.status.fillna("not_replayed").value_counts().to_dict()},
                  "replay_stable_exec_s_total": float(gs.exec_s.sum()),
                  "scope_shares": {w: dict(zip(scopes, (v / v.sum()).round(4).tolist())) for w, v in tot.items() if v.sum() > 0},
                  "task_exec_s": {"tasks": int(len(tr)),
                                  "median_session": float(tr.session.median()) if len(tr) else None,
                                  "median_other_attempts": float(tr.other_attempts.median()) if len(tr) else None,
                                  "tasks_other_attempts_gt_session": int((tr.other_attempts > tr.session).sum()) if len(tr) else 0},
                  "S2_curve": curve}
        print(h, json.dumps(res[h], default=float)[:1500], flush=True)
    json.dump(res, open(out, "w"), indent=1, default=float)


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"extract": cmd_extract, "analyze": cmd_analyze, "dab-main": cmd_dab_main, "replay-input": cmd_replay_input,
     "costs": cmd_costs}[cmd](*args)
