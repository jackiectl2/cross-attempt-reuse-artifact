"""Parse the DAB trajectory archive into call- and run-level tables (v2).

Keeps the complete recorded tool message, extracts the recorded payload (inline
results) or preview (spilled results) exactly as DAB's templates emit them, and
recomputes each run's success label with the task's own validate.py.
Usage: parse2.py <all_trajectories.zip> <dab_repo> <out_dir>
"""
import importlib.util
import json
import os
import sys
import zipfile
from multiprocessing import Pool

import re

import pyarrow as pa
import pyarrow.parquet as pq

INLINE_MARK = "\nThe result is:\n"
PREVIEW_MARK = "of the result is:\n"
SPILL_MARK = "The result is too large, so it is stored in a file."
FAIL_MARK = "execution failed. The error message is:\n"
KEY_RE = re.compile(r"under key: ?\n(\S+)")
EXPECTED = {"runs": 13482, "tool_calls": 151293, "query_db": 76213, "runs_without_tool_calls": 30}


def _s(v):
    return v if isinstance(v, str) or v is None else json.dumps(v)


def recorded_view(content):
    """Return (status, recorded_text) from a DAB tool message."""
    if FAIL_MARK in content:
        return "error", content.split(FAIL_MARK, 1)[1].removesuffix("\n")
    if SPILL_MARK in content:
        return "spilled", content.split(PREVIEW_MARK, 1)[1].removesuffix("\n")
    if INLINE_MARK in content:
        return "inline", content.split(INLINE_MARK, 1)[1].removesuffix("\n")
    return "unknown", content


def git_head(repo):
    """Commit id of the DAB checkout, read from .git without needing a git binary."""
    head = open(os.path.join(repo, ".git", "HEAD")).read().strip()
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    path = os.path.join(repo, ".git", ref)
    if os.path.exists(path):
        return open(path).read().strip()
    for line in open(os.path.join(repo, ".git", "packed-refs")):
        if line.strip().endswith(ref):
            return line.split()[0]
    return "unknown"


def load_validators(repo):
    """Some validators import DAB helpers (common_scaffold); make the repo importable."""
    import types
    sys.path.insert(0, repo)
    sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *a, **k: None))
    cache = {}

    def get(dataset, query):
        key = (dataset, query)
        if key not in cache:
            path = os.path.join(repo, f"query_{dataset}", query, "validate.py")
            spec = importlib.util.spec_from_file_location(f"v_{dataset}_{query}", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cache[key] = mod.validate
        return cache[key]
    return get


_VALIDATOR = None


def _init_worker(repo):
    global _VALIDATOR
    _VALIDATOR = load_validators(repo)


def _validate(args):
    dataset, query, answer = args
    if answer is None:
        return "fail", "no answer"
    try:
        ok, reason = _VALIDATOR(dataset, query)(answer)
        return ("pass" if ok else "fail"), reason
    except Exception as e:  # a validator crash is not a judgement; kept as its own class
        return "validator_error", f"{type(e).__name__}: {e}"


def main(zip_path, repo, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    z = zipfile.ZipFile(zip_path)
    calls, runs = [], []
    for name in sorted(z.namelist()):
        if not name.endswith(".json"):
            continue
        _, model, dataset, query, fname = name.split("/")
        run = int(fname[len("run_"):-len(".json")])
        msgs = json.loads(z.read(name))
        # Pair each assistant tool call with the tool message that answers it *after that turn*:
        # some providers reuse ids across turns (e.g. "functions.query_db:0"), so a global id map is wrong.
        answer_of = {}
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
        step, answer = 0, None
        for turn, m in enumerate(msgs):
            if m.get("role") != "assistant":
                continue
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                tool = fn.get("name")
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                    args_ok = isinstance(args, dict)
                except json.JSONDecodeError:
                    args, args_ok = {}, False
                if not isinstance(args, dict):
                    args = {}
                out = _s((answer_of.get((turn, id(tc))) or {}).get("content")) or ""
                status, rec = recorded_view(out)
                if tool == "return_answer" and answer is None:
                    answer = _s(args.get("answer"))
                calls.append({
                    "model": model, "dataset": dataset, "query": query, "run": run, "step": step, "turn": turn,
                    "tool": tool, "tool_call_id": tc.get("id"), "db_name": _s(args.get("db_name")),
                    "text": _s(args.get("query") if tool == "query_db" else
                               args.get("code") if tool == "execute_python" else args),
                    "status": status, "recorded": rec, "message_chars": len(out), "tool_message": out,
                    "result_key": (KEY_RE.search(out).group(1) if KEY_RE.search(out) else None), "args_parse_ok": args_ok,
                })
                step += 1
        runs.append({"model": model, "dataset": dataset, "query": query, "run": run, "n_messages": len(msgs),
                     "n_tool_calls": step, "answer": answer})
    with Pool(int(os.environ.get("SLURM_CPUS_PER_TASK", "4")), initializer=_init_worker, initargs=(repo,)) as pool:
        verdicts = pool.map(_validate, [(r["dataset"], r["query"], r["answer"]) for r in runs], chunksize=16)
    for r, (validation, reason) in zip(runs, verdicts):
        r.update(validation=validation, success=validation == "pass", validator_reason=str(reason)[:200])
    pq.write_table(pa.Table.from_pylist(calls), os.path.join(out_dir, "calls.parquet"), compression="zstd")
    pq.write_table(pa.Table.from_pylist(runs), os.path.join(out_dir, "runs.parquet"), compression="zstd")
    got = {"runs": len(runs), "tool_calls": len(calls), "query_db": sum(c["tool"] == "query_db" for c in calls),
           "runs_without_tool_calls": sum(r["n_tool_calls"] == 0 for r in runs)}
    rev = git_head(repo)
    v = {k: sum(r["validation"] == k for r in runs) for k in ("pass", "fail", "validator_error")}
    print(json.dumps({**got, "validation": v, "dab_revision": rev}))
    json.dump({**got, "validation": v, "dab_revision": rev}, open(os.path.join(out_dir, "parse_meta.json"), "w"))
    assert got == EXPECTED, f"archive counts differ from preregistration: {got} vs {EXPECTED}"
    assert v["validator_error"] == 0, "validator errors must be resolved before analysis"


if __name__ == "__main__":
    main(*sys.argv[1:])
