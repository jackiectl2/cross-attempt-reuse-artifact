"""Parse DAB trajectory zip into one row per tool call (Parquet).

Each run file is a list of chat messages. Assistant messages carry `tool_calls`;
the matching tool message (by tool_call_id) carries the execution outcome.
"""
import json
import sys
import zipfile

import pyarrow as pa
import pyarrow.parquet as pq

SPILL_MARK = "The result is too large"
FAIL_MARK = "execution failed"


def _s(v):
    """Tool arguments are usually strings, but some agents pass JSON objects."""
    return v if isinstance(v, str) or v is None else json.dumps(v)


def parse_run(model, dataset, query, run, msgs):
    results = {}
    for m in msgs:
        if m.get("role") == "tool":
            results[m.get("tool_call_id")] = m.get("content") or ""
    rows = []
    step = 0
    for turn, m in enumerate(msgs):
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {}
            out = results.get(tc.get("id"), "")
            if not isinstance(out, str):
                out = json.dumps(out)
            rows.append({
                "model": model, "dataset": dataset, "query": query, "run": run,
                "step": step, "turn": turn, "tool": fn.get("name"),
                "db_name": _s(args.get("db_name")),
                "text": _s(args.get("query") if fn.get("name") == "query_db"
                           else args.get("code") if fn.get("name") == "execute_python"
                           else args),
                "args_parse_ok": bool(args) or raw.strip() in ("", "{}"),
                "result_chars": len(out),
                "spilled": SPILL_MARK in out,
                "failed": FAIL_MARK in out[:200],
                "result_head": out[:2000],
            })
            step += 1
    return rows


def main(zip_path, out_path):
    z = zipfile.ZipFile(zip_path)
    rows, runs = [], []
    for name in z.namelist():
        if not name.endswith(".json"):
            continue
        _, model, dataset, query, fname = name.split("/")
        run = int(fname[len("run_"):-len(".json")])
        msgs = json.loads(z.read(name))
        r = parse_run(model, dataset, query, run, msgs)
        rows.extend(r)
        runs.append({"model": model, "dataset": dataset, "query": query, "run": run,
                     "n_messages": len(msgs), "n_tool_calls": len(r)})
    pq.write_table(pa.Table.from_pylist(rows), out_path, compression="zstd")
    pq.write_table(pa.Table.from_pylist(runs), out_path.replace(".parquet", "_runs.parquet"), compression="zstd")
    print(f"runs={len(runs)} tool_calls={len(rows)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
