"""B4: preregistered live calibration of S0-S3 and the programmatic-consumer test.

Subcommands
  select <v2> <out.json>
      Preregistered 12-task subset: per engine, among tasks with successful calls on that engine,
      the tasks with minimum / median / maximum spill fraction (ties by name); per task the model
      with the median calls per run.
  calibrate <v2> <dab_repo> <dataset> <query> <model> <out.json> [N] [resamples]
      Executes S0-S3 for real (attempt = private directory; S2 = private copy of the cached file;
      S3 = symlink into a read-only shared store, physically evicted at refcount 0). Each
      (resample, budget, system) runs in a fresh process; system order is randomized per resample.
  costs <live_dir> <costs.json>
      Aggregates measured per-op costs (lookup, alias, copy per byte) over all calibration files.
  validate <v2> <live_dir> <costs.json> <out.json>
      Re-runs the simulator with calibrated costs on the identical orders and admitted calls and
      reports relative errors against the live admitted-call measurements.
  consumer-prep <v2> <dab_repo> <dataset> <case_dir> [quota]
  consumer-compare <case_root> <manifest_glob> <out.json>
  e2e <v2> <dab_repo> <dataset> <out.json> [calib|all] [contract] [selection.json]
      End-to-end equivalence pass: S0-S3 on identical attempts, every tool message / binding / bound file
      compared with S0, recorded execute_python calls run against each system's bindings, store write probes.
  e2e-summary <glob> <out.json>
"""
import glob
import hashlib
import json
import multiprocessing as mp
import os
import re
import shutil
from collections import OrderedDict
import stat
import subprocess
import sys
import tempfile
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
import analyze2 as A  # noqa: E402
import replay2 as R  # noqa: E402

PREVIEW = 10000
UNSAFE_CODE = ("requests", "urllib", "socket", "subprocess", "http.client", "os.system")


def admitted_frame(v2, contract="B"):
    """contract B = preregistered static certificate; E = exploratory pinned-runtime contract."""
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    q["admitted"] = gates["B_certified" if contract == "B" else "E_certified"]
    return calls, runs, rep, q


def cmd_select(v2, out):
    calls, runs, rep, q = admitted_frame(v2)
    ok = q[q.ok]
    picks = []
    for eng, eg in ok.groupby("engine"):
        frac = eg.groupby(["dataset", "query"]).spilled.mean().reset_index().sort_values(["spilled", "dataset", "query"])
        for i in sorted({0, len(frac) // 2, len(frac) - 1}):
            ds, qy = frac.iloc[i][["dataset", "query"]]
            cpr = runs[(runs.dataset == ds) & (runs["query"] == qy)].groupby("model").n_tool_calls.mean().sort_values()
            picks.append({"engine": eng, "dataset": ds, "query": qy, "model": cpr.index[len(cpr) // 2],
                          "spill_fraction": float(frac.iloc[i].spilled)})
    json.dump({"rule": "per engine: min/median/max spill-fraction task; model with median calls/run", "picks": picks},
              open(out, "w"), indent=1)
    print(json.dumps(picks, indent=1))


def _du(root):
    tot = 0
    for d, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(d, f)
            if not os.path.islink(p):
                tot += os.path.getsize(p)
    return tot


def _remove(path):
    if path and os.path.exists(path):
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
        os.remove(path)


# DAB common_scaffold/DataAgent.py templates (revision f6b1ad07), copied verbatim
SUCCESS_TOOL_RESULT_TMPL = """
The tool {tool_name} was executed successfully.

The result is stored under key:
{result_key}

The result is:
{tool_result}
"""

SUCCESS_TOOL_PREVIEW_TMPL = """
The tool {tool_name} was executed successfully.

The result is too large, so it is stored in a file. The file path is stored under key: 
{result_key}

The preview (first {preview_length} characters) of the result is:
{tool_result_preview}
"""

FAIL_TOOL_RESULT_TMPL = """
The tool {tool_name} execution failed. The error message is:
{tool_result}
"""

FAIL_TOOL_PREVIEW_TMPL = """
The tool {tool_name} execution failed. The error message is:
{tool_result_preview}
... (too long, truncated)
"""


def dab_message(result_key, spilled, text, ok=True):
    """query_db tool message as DataAgent._handle_tool_call builds it; text = full serialized result,
    or (spilled) its first PREVIEW characters."""
    if not ok:
        if len(text) > PREVIEW:
            return FAIL_TOOL_PREVIEW_TMPL.replace("{tool_name}", "query_db").replace("{tool_result_preview}", text[:PREVIEW])
        return FAIL_TOOL_RESULT_TMPL.replace("{tool_name}", "query_db").replace("{tool_result}", text)
    if spilled:
        return (SUCCESS_TOOL_PREVIEW_TMPL.replace("{tool_name}", "query_db").replace("{result_key}", result_key)
                .replace("{preview_length}", str(PREVIEW)).replace("{tool_result_preview}", text[:PREVIEW]))
    return SUCCESS_TOOL_RESULT_TMPL.replace("{tool_name}", "query_db").replace("{result_key}", result_key).replace("{tool_result}", text)


FICLONE = 0x40049409   # linux/fs.h: share all extents of src with dst (XFS / Btrfs)


def _clone(src, dst):
    import fcntl
    with open(src, "rb") as fs, open(dst, "wb") as fd:
        fcntl.ioctl(fd.fileno(), FICLONE, fs.fileno())


def _h(b):
    return hashlib.blake2b(b, digest_size=16).hexdigest()


def _file_h(path):
    with open(path, "rb") as f:
        return _h(f.read())


PROBES = ("open({rel!r}, 'w').write('x')", "import os; os.remove(os.path.realpath({rel!r}))",
          "import json; json.dump([], open({rel!r}, 'w'))")


def _run_py(script, body, cwd, timeout=120):
    with open(script, "w") as f:
        f.write(body)
    try:
        cp = subprocess.run([sys.executable, script], cwd=cwd, capture_output=True, text=True, timeout=timeout,
                            env=dict(os.environ, PYTHONHASHSEED="0"))
        return cp.returncode, cp.stdout + cp.stderr
    except subprocess.TimeoutExpired:
        return "timeout", ""


def run_system(args):
    """One (resample, budget, system) execution in its own process.
    Every query_db call returns DAB's tool message and binds DAB's value (inline result object, or the
    relative path file_storage/<id>.json); S1/S2/S3 hits rebuild the message with the attempt-local key.
    Footprint is accounted from the real file sizes after every call under two lifetimes: private files
    retained until the batch ends (concurrent best-of-N) and deleted at attempt end (sequential retries).
    With record_dir (end-to-end equivalence pass) attempt directories are kept under record_dir/<system>,
    every message and binding is recorded, and the attempt's recorded execute_python calls (py_by_run)
    are run at their trace position (before the next query_db call) against the bindings present at that
    point, with the S3 store directory read-only."""
    system, order, by_run, dataset, repo, budget = args[:6]
    record_dir, py_by_run = (args[6], args[7]) if len(args) > 6 else (None, None)
    opts = args[8] if len(args) > 8 else {}
    classes, regen = opts.get("classes", "both"), opts.get("regen", False)
    s2like = system in ("S2", "S2c")          # S2c = S2 with copy-on-write clones instead of copies
    import yaml
    cfg = yaml.safe_load(open(os.path.join(repo, f"query_{dataset}", "db_config.yaml")))["db_clients"]
    eng = R.Engines(os.path.join(repo, f"query_{dataset}"), cfg, 120.0)
    serialize = R.load_serialize(repo)
    base = os.path.join(record_dir, system) if record_dir else tempfile.mkdtemp(prefix=f"live-{system}-")
    store_dir = os.path.join(base, "store")
    os.makedirs(store_dir)
    s2, s3 = A.LRUStore(budget), A.LRUStore(budget)
    paths, meta, size = {}, {}, {}
    fp = {"priv_all": 0, "priv_cur": 0, "priv_all_a": 0, "priv_cur_a": 0, "store": 0,
          "peak_retained": 0, "peak_seq": 0, "peak_retained_a": 0, "peak_seq_a": 0}   # _a: admitted calls only

    def evict(k):
        p = paths.pop(k, None)
        if p:
            fp["store"] -= size.pop(p, 0)
        _remove(p)

    def add_priv(n, admitted):
        fp["priv_all"] += n
        fp["priv_cur"] += n
        if admitted:
            fp["priv_all_a"] += n
            fp["priv_cur_a"] += n

    m = {"time": 0.0, "time_admitted": 0.0, "written": 0, "written_admitted": 0, "hits": 0, "bypass": 0, "calls": 0,
         "admitted_calls": 0}
    lat, ops = [], {"lookup_s": [], "alias_s": [], "copy_s": [], "copy_bytes": [], "clone_s": [], "regen_s": []}
    recs, cons, probes, rel2key = [], [], [], {}
    pydir = os.path.join(record_dir, "py", system) if record_dir else None

    def run_consumers(r, pend, bind, upto):
        """Run the attempt's recorded execute_python calls issued before trace step `upto`, at that position,
        against the bindings that exist at that point (S3 store directory read-only while they run)."""
        due = [x for x in pend if x[1][0] < upto]
        if not due:
            return
        os.makedirs(pydir, exist_ok=True)
        if system == "S3":
            os.chmod(store_dir, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
        cwd = os.path.join(base, f"attempt_{r}")
        for x in due:
            pend.remove(x)
            j, (step, code, keys) = x
            if not all(k in bind for k in keys):
                cons.append({"run": int(r), "j": j, "rc": "unbound", "out_h": None})
                continue
            env = {k: bind[k] for k in keys}
            body = f"code = {code.strip()!r}\n\nenv_args = {env!r}\n\nexec(code, env_args)\n"
            rc, out = _run_py(os.path.join(pydir, f"a{r}_{j}.py"), body, cwd)
            out = out.replace(pydir, "<PY>").replace(base, "<BASE>")
            cons.append({"run": int(r), "j": j, "rc": rc, "out_h": _h(out.encode("utf-8", "surrogatepass")),
                         "out_tail": out[-300:]})
            if system == "S0":   # rerun on the same bindings: detects code whose output is nondeterministic
                rc2, out2 = _run_py(os.path.join(pydir, f"a{r}_{j}_rerun.py"), body, cwd)
                out2 = out2.replace(pydir, "<PY>").replace(base, "<BASE>").replace("_rerun.py", ".py")
                cons[-1]["self_identical"] = (rc2, _h(out2.encode("utf-8", "surrogatepass"))) == (rc, cons[-1]["out_h"])
        if system == "S3":
            os.chmod(store_dir, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)

    for r in order:
        adir = os.path.join(base, f"attempt_{r}", "file_storage")
        os.makedirs(adir)
        local, s1, held, bind, used = {}, A.LRUStore(budget), [], {}, set()
        pend = list(enumerate(py_by_run.get(r, []))) if (record_dir and py_by_run) else []
        fp["priv_cur"] = fp["priv_cur_a"] = 0
        for ci, c in enumerate(by_run[r]):
            if pend:
                run_consumers(r, pend, bind, c["step"])
            m["calls"] += 1
            t0 = time.perf_counter()
            name, n = c["tool_call_id"], 1
            while name in used:   # some providers reuse tool-call ids within a run; same suffix in every system
                name, n = f"{c['tool_call_id']}#{n}", n + 1
            used.add(name)
            fname = os.path.join(adir, f"{name}.json")
            rel = os.path.join("file_storage", os.path.basename(fname))
            rkey = c["result_key"]
            written = 0
            hit, content, kind = None, None, "skipped"
            if c["admitted"]:
                m["admitted_calls"] += 1
                k = c["key"]
                tl = time.perf_counter()
                if system == "S1" and s1.get(k):
                    hit = ("alias", local.get(k))
                elif s2like and k in local:
                    hit = ("alias", local[k])
                elif s2like and s2.get(k):
                    hit = ("copy", paths.get(k))
                elif system == "S3" and s3.get(k):
                    hit = ("alias", paths.get(k))
                ops["lookup_s"].append(time.perf_counter() - tl)
            if hit is not None:
                m["hits"] += 1
                kind, src = hit
                spilled, text, obj = meta[c["key"]]
                content = dab_message(rkey, spilled, text)
                if src is not None:
                    t1 = time.perf_counter()
                    if kind == "alias":
                        os.symlink(src, fname)
                        ops["alias_s"].append(time.perf_counter() - t1)
                    elif system == "S2c":
                        _clone(src, fname)               # shares extents: no data written
                        ops["clone_s"].append(time.perf_counter() - t1)
                        add_priv(os.path.getsize(fname), True)
                    else:
                        shutil.copyfile(src, fname)
                        ops["copy_s"].append(time.perf_counter() - t1)
                        ops["copy_bytes"].append(os.path.getsize(fname))
                        written += os.path.getsize(fname)
                        add_priv(os.path.getsize(fname), True)
                    if regen and spilled:                # A6: rebuild the preview from the bound file
                        t2 = time.perf_counter()
                        with open(fname, encoding="utf-8") as f:
                            text = json.dumps(json.load(f))[:PREVIEW]
                        content = dab_message(rkey, spilled, text)
                        ops["regen_s"].append(time.perf_counter() - t2)
                    local[c["key"]] = fname
                    bind[rkey] = rel
                    rel2key[rel] = c["key"]
                else:
                    bind[rkey] = obj
                kind = "spilled" if spilled else "inline"
                if system == "S3" and c["key"] not in held:
                    s3.d[c["key"]][1] += 1
                    held.append(c["key"])
            elif c["replay_ok"]:
                try:
                    result, ok = serialize(eng.execute(c["db_name"], c["text"])), True
                except Exception as e:  # live failure of a call that succeeded in replay; never cached
                    result, ok = f"{type(e).__name__}: {e}", False
                payload = json.dumps(result)
                spilled = ok and len(payload) > PREVIEW
                cache_ok = classes == "both" or (classes == "spilled") == spilled     # A5
                content = dab_message(rkey, spilled, payload, ok)
                kind = ("spilled" if spilled else "inline") if ok else "error"
                path = None
                if spilled:
                    to_store = system == "S3" and c["admitted"] and cache_ok
                    path = os.path.join(store_dir, hashlib.blake2b(c["key"].encode(), digest_size=12).hexdigest() + ".json") if to_store else fname
                    with open(path, "w", encoding="utf-8") as f:
                        json.dump(result, f, indent=2)
                    sz = os.path.getsize(path)
                    written += sz
                    if to_store:
                        fp["store"] += sz
                        size[path] = sz
                    else:
                        add_priv(sz, c["admitted"])
                if ok:
                    bind[rkey] = rel if spilled else result
                    if spilled:
                        rel2key[rel] = c["key"]
                if c["admitted"] and ok and cache_ok:
                    k = c["key"]
                    meta[k] = (spilled, payload[:PREVIEW] if spilled else payload, None if spilled else result)
                    charge = (os.path.getsize(path) + min(len(payload), PREVIEW) if path else len(payload.encode())) + A.META_BYTES
                    if system == "S1":
                        local[k] = path
                        if not s1.put(k, charge):
                            m["bypass"] += 1
                    elif s2like:
                        local[k] = path
                        if s2.put(k, charge, on_evict=evict):
                            if path:
                                sp = os.path.join(store_dir, hashlib.blake2b(k.encode(), digest_size=12).hexdigest() + ".json")
                                if system == "S2c":
                                    t1 = time.perf_counter()
                                    _clone(path, sp)
                                    ops["clone_s"].append(time.perf_counter() - t1)
                                else:
                                    shutil.copyfile(path, sp)
                                    written += os.path.getsize(sp)
                                fp["store"] += os.path.getsize(sp)
                                size[sp] = os.path.getsize(sp)
                            paths[k] = sp if path else None
                        else:
                            m["bypass"] += 1
                    elif system == "S3":
                        if s3.put(k, charge, on_evict=evict):
                            if path:
                                os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                                os.symlink(path, fname)
                            paths[k] = path
                            s3.d[k][1] += 1
                            held.append(k)
                        else:
                            m["bypass"] += 1
                            if path:
                                shutil.move(path, fname)
                                fp["store"] -= size.pop(path)
                                add_priv(os.path.getsize(fname), True)
            dt = time.perf_counter() - t0
            m["time"] += dt
            m["written"] += written
            if c["admitted"]:
                m["time_admitted"] += dt
                m["written_admitted"] += written
            lat.append(dt)
            fp["peak_retained"] = max(fp["peak_retained"], fp["priv_all"] + fp["store"])
            fp["peak_seq"] = max(fp["peak_seq"], fp["priv_cur"] + fp["store"])
            fp["peak_retained_a"] = max(fp["peak_retained_a"], fp["priv_all_a"] + fp["store"])
            fp["peak_seq_a"] = max(fp["peak_seq_a"], fp["priv_cur_a"] + fp["store"])
            if record_dir:
                b = bind.get(rkey)
                recs.append({"run": int(r), "i": ci, "kind": kind, "admitted": bool(c["admitted"]), "hit": hit is not None,
                             "content_h": _h(content.encode("utf-8", "surrogatepass")) if content is not None else None,
                             "inline_h": _h(json.dumps(b).encode("utf-8", "surrogatepass")) if kind == "inline" else None,
                             "rel": rel if kind == "spilled" else None,
                             "file_h": _file_h(os.path.join(base, f"attempt_{r}", rel)) if kind == "spilled" else None})
        if record_dir and py_by_run:
            run_consumers(r, pend, bind, float("inf"))       # calls issued after the attempt's last query_db call
            cwd = os.path.join(base, f"attempt_{r}")
            os.makedirs(pydir, exist_ok=True)
            if system == "S3":
                os.chmod(store_dir, stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP)
            if system in ("S2", "S2c", "S3") and not probes:
                shared = [rel_ for rel_ in bind.values() if isinstance(rel_, str) and rel_.startswith("file_storage/")
                          and os.path.islink(os.path.join(cwd, rel_)) == (system == "S3")]
                if shared:
                    before = _file_h(os.path.join(cwd, shared[0]))
                    store_obj = paths.get(rel2key.get(shared[0])) if s2like else None
                    store_before = _file_h(store_obj) if store_obj and os.path.exists(store_obj) else None
                    for pi, body in enumerate(PROBES):
                        rc, out = _run_py(os.path.join(pydir, f"probe{pi}.py"), body.format(rel=shared[0]) + "\n", cwd)
                        probes.append({"probe": pi, "rc": rc, "refused": rc != 0 and ("Permission denied" in out or "Read-only" in out)})
                    if system == "S3":
                        m["store_object_intact"] = _file_h(os.path.join(cwd, shared[0])) == before
                    elif store_before is not None:   # private copy was modified; the cached object must not change
                        m["store_object_intact"] = _file_h(store_obj) == store_before
            if system == "S3":
                os.chmod(store_dir, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP)
        for k in held:
            if k in s3.d:
                s3.d[k][1] -= 1
    m["footprint"] = _du(base)
    for x in ("peak_retained", "peak_seq", "peak_retained_a", "peak_seq_a"):
        m[x] = fp[x]
    m["end_accounted"] = fp["priv_all"] + fp["store"]
    m["p50"] = float(np.percentile(lat, 50)) if lat else np.nan
    m["p95"] = float(np.percentile(lat, 95)) if lat else np.nan
    if record_dir:
        m["records"], m["consumers"], m["probes"] = recs, cons, probes
    else:
        shutil.rmtree(base, ignore_errors=True)
    return m, ops


def _calls(rr):
    return [{"key": x.key, "db_name": x.db_name, "text": x.text, "tool_call_id": x.tool_call_id, "step": int(x.step),
             "result_key": x.result_key if isinstance(x.result_key, str) else f"var_{x.tool_call_id}",
             "admitted": bool(x.admitted), "replay_ok": bool(x.ok)} for x in rr.itertuples()]


def _task_base(tq):
    """Distinct admitted store bytes of a task (budget base, as in the simulator)."""
    adm = tq[tq.admitted]
    sp = adm.spilled.fillna(False).astype(bool) & adm.ok
    return float(np.where(sp, adm.spill_b, adm.bytes)[~adm.duplicated(["key"]).values].sum()) if len(adm) else 0.0


LIVE_VARIANTS = {"base": (("S0", "S1", "S2", "S3"), {}), "cow": (("S0", "S2", "S2c", "S3"), {}),
                 "inline_only": (("S0", "S2", "S3"), {"classes": "inline"}),
                 "spilled_only": (("S0", "S2", "S3"), {"classes": "spilled"}),
                 "regen": (("S0", "S2", "S3"), {"regen": True})}


def cmd_calibrate(v2, repo, dataset, query, model, out, N=8, n_res=5, contract="B", variant="base"):
    N, n_res = int(N), int(n_res)
    systems, opts = LIVE_VARIANTS[variant]
    calls, runs, rep, q = admitted_frame(v2, contract)
    tq = q[(q.dataset == dataset) & (q["query"] == query)]
    cq = tq[tq.model == model].sort_values(["run", "step"])
    run_ids = sorted(runs[(runs.dataset == dataset) & (runs["query"] == query) & (runs.model == model)].run)
    by_run = {r: [] for r in run_ids}
    for r, rr in cq.groupby("run"):
        by_run[r] = _calls(rr)
    task_base = _task_base(tq)
    rng = np.random.default_rng(1234)
    ctx = mp.get_context("spawn")
    results, all_ops = [], {"lookup_s": [], "alias_s": [], "copy_s": [], "copy_bytes": [], "clone_s": [], "regen_s": []}
    for i in range(n_res):
        order = [int(x) for x in rng.permutation(run_ids)[:N]]
        for bname, frac in (("inf", None), ("1/4", 0.25)):
            budget = float("inf") if frac is None else frac * max(task_base, 1.0)
            for system in rng.permutation(list(systems)):
                with ctx.Pool(1) as pool:
                    m, ops = pool.apply(run_system, ((str(system), order, by_run, dataset, repo, budget, None, None, opts),))
                for k in all_ops:
                    all_ops[k] += ops[k]
                results.append({"res": i, "budget": bname, "budget_bytes": budget, "system": str(system), "order": order, **m})
                print(json.dumps(results[-1], default=float), flush=True)
    json.dump({"dataset": dataset, "query": query, "model": model, "N": N, "contract": contract, "variant": variant,
               "opts": opts, "task_budget_base": task_base,
               "results": results, "ops": all_ops}, open(out, "w"), indent=1, default=float)


PY_CAP = 8   # recorded execute_python calls run per attempt in the end-to-end pass


def _py_calls(calls, q, dataset, query, model):
    """Recorded execute_python calls per run that reference only this run's query_db results, are
    self-contained and do not write a result path (same filter as the consumer test)."""
    sel = (calls.dataset == dataset) & (calls["query"] == query) & (calls.model == model)
    cq = q[(q.dataset == dataset) & (q["query"] == query) & (q.model == model)]
    qkeys = {r: {c["result_key"] for c in _calls(g)} for r, g in cq.groupby("run")}
    allkeys = calls[sel & calls.result_key.notna()].groupby("run").result_key.apply(list).to_dict()
    out = {}
    for c in calls[sel & (calls.tool == "execute_python") & calls.text.notna()].sort_values(["run", "step"]).itertuples():
        if any(u in c.text for u in UNSAFE_CODE) or A.WRITE_RE.search(c.text):
            continue
        keys = [k for k in allkeys.get(c.run, []) if re.search(re.escape(k) + r"(?!\w)", c.text)]
        if keys and all(k in qkeys.get(c.run, set()) for k in keys):
            out.setdefault(int(c.run), []).append((int(c.step), c.text, keys))
    return {r: v[:PY_CAP] for r, v in out.items()}


def _compare(ms):
    """S1-S3 against S0 on identical attempts: tool message bytes, inline value, bound path, bytes of the
    bound file as read through the binding, and consumer stdout/stderr + return code."""
    ref = {(x["run"], x["i"]): x for x in ms["S0"]["records"]}
    refc = {(x["run"], x["j"]): x for x in ms["S0"]["consumers"]}
    out = {"S0": {"calls": sum(x["kind"] != "skipped" for x in ref.values()),
                  "live_errors": sum(x["kind"] == "error" for x in ref.values()),
                  "consumers": sum(x["rc"] != "unbound" for x in refc.values()),
                  "consumers_nondeterministic": sum(x["rc"] != "unbound" and not x.get("self_identical", True) for x in refc.values()),
                  "consumer_nonzero_rc": sum(x["rc"] not in (0, "unbound") for x in refc.values()),
                  "peak_retained": ms["S0"]["peak_retained"], "peak_seq": ms["S0"]["peak_seq"]}}
    for s in [x for x in ms if x != "S0"]:
        n = hits = 0
        mism = []
        for x in ms[s]["records"]:
            y = ref[(x["run"], x["i"])]
            if x["kind"] == "skipped" and y["kind"] == "skipped":
                continue
            n += 1
            hits += x["hit"]
            diff = [f for f in ("kind", "content_h", "inline_h", "rel", "file_h") if x[f] != y[f]]
            if diff:
                mism.append({"run": x["run"], "i": x["i"], "hit": x["hit"], "fields": diff, "kind": x["kind"], "S0_kind": y["kind"]})
        cn = cid = cid_ok = 0
        cmism = []
        for x in ms[s]["consumers"]:
            y = refc[(x["run"], x["j"])]
            if "unbound" in (x["rc"], y["rc"]) or not y.get("self_identical", True):
                continue
            cn += 1
            if (x["rc"], x["out_h"]) == (y["rc"], y["out_h"]):
                cid += 1
                cid_ok += y["rc"] == 0
            else:
                cmism.append({"run": x["run"], "j": x["j"], "rc": x["rc"], "S0_rc": y["rc"], "tail": x.get("out_tail"),
                              "S0_tail": y.get("out_tail")})
        out[s] = {"calls": n, "hits": hits, "mismatches": len(mism), "mismatches_on_hits": sum(x["hit"] for x in mism),
                  "mismatch_detail": mism[:20], "consumers": cn, "consumers_identical": cid,
                  "consumers_identical_ok": cid_ok, "consumers_identical_failed": cid - cid_ok,
                  "consumer_mismatch_detail": cmism[:10], "probes": ms[s].get("probes"),
                  "store_object_intact": ms[s].get("store_object_intact"),
                  "peak_retained": ms[s]["peak_retained"], "peak_seq": ms[s]["peak_seq"]}
    return out


def _e2e_job(args):
    """S0-S3 on one (cell, resample, budget), sequentially in this worker process (not timed)."""
    dataset, repo, qy, mo, contract, i, bname, budget, order, by_run, py_by_run = args
    root = tempfile.mkdtemp(prefix="e2e-")
    systems = os.environ.get("ED_E2E_SYSTEMS", "S0,S1,S2,S3").split(",")
    ms = {system: run_system((system, order, by_run, dataset, repo, budget, root, py_by_run))[0] for system in systems}
    shutil.rmtree(root, ignore_errors=True)
    return {"dataset": dataset, "query": qy, "model": mo, "contract": contract, "res": i, "budget": bname,
            "order": order, **_compare(ms)}


def cmd_e2e(v2, repo, dataset, out, mode="calib", contract="E", selection=None):
    """End-to-end equivalence pass. mode calib: the preregistered calibration cells of this dataset, 2 resamples,
    budgets inf and 1/4; mode all: every (task, model) cell of the dataset, 1 resample, budget inf.
    N = 8 attempts; S0-S3 executed on identical attempt orders, sequentially in one worker process per
    (cell, resample, budget); ED_E2E_WORKERS worker processes in parallel; not timed."""
    calls, runs, rep, q = admitted_frame(v2, contract)
    if mode == "calib":
        cells = [(p["query"], p["model"]) for p in json.load(open(selection))["picks"] if p["dataset"] == dataset]
        n_res, budgets = 2, (("inf", None), ("1/4", 0.25))
    else:
        cells = sorted(runs[runs.dataset == dataset][["query", "model"]].drop_duplicates().itertuples(index=False, name=None))
        n_res, budgets = 1, (("inf", None),)
    rng = np.random.default_rng(4321)
    jobs = []
    for qy, mo in cells:
        tq = q[(q.dataset == dataset) & (q["query"] == qy)]
        cq = tq[tq.model == mo].sort_values(["run", "step"])
        run_ids = sorted(runs[(runs.dataset == dataset) & (runs["query"] == qy) & (runs.model == mo)].run)
        by_run = {r: [] for r in run_ids}
        for r, rr in cq.groupby("run"):
            by_run[r] = _calls(rr)
        task_base = _task_base(tq)
        py_by_run = _py_calls(calls, q, dataset, qy, mo)
        for i in range(n_res):
            order = [int(x) for x in rng.permutation(run_ids)[:8]]
            for bname, frac in budgets:
                budget = float("inf") if frac is None else frac * max(task_base, 1.0)
                jobs.append((dataset, repo, qy, mo, contract, i, bname, budget, order, by_run, py_by_run))
    del calls, runs, rep, q
    shard = os.environ.get("ED_E2E_SHARD")   # "i/n": every n-th job from i; attempt orders identical to the full run
    if shard:
        i_, n_ = (int(x) for x in shard.split("/"))
        jobs = jobs[i_::n_]
    workers = int(os.environ.get("ED_E2E_WORKERS", "1"))
    results = []
    with mp.get_context("spawn").Pool(workers) as pool:
        for r_ in pool.imap_unordered(_e2e_job, jobs):
            results.append(r_)
            print(json.dumps({k: r_[k] for k in ("query", "model", "res", "budget")}),
                  {s: (r_[s]["calls"], r_[s]["hits"], r_[s]["mismatches"], r_[s]["consumers_identical"], r_[s]["consumers"])
                   for s in ("S1", "S2", "S2c", "S3") if s in r_}, flush=True)
    json.dump(results, open(out, "w"), indent=1, default=str)


def cmd_e2e_summary(pattern, out):
    rows = [r for f in sorted(glob.glob(pattern)) for r in json.load(open(f))]
    summ = {"cells": len({(r["dataset"], r["query"], r["model"]) for r in rows}), "runs": len(rows)}
    for s in [x for x in ("S1", "S2", "S2c", "S3") if rows and x in rows[0]]:
        summ[s] = {k: sum(r[s].get(k, 0) for r in rows) for k in ("calls", "hits", "mismatches", "mismatches_on_hits",
                                                                  "consumers", "consumers_identical", "consumers_identical_ok",
                                                                  "consumers_identical_failed")}
        summ[s]["mismatch_examples"] = [dict(cell=f"{r['dataset']}/{r['query']}/{r['model']}", budget=r["budget"], **x)
                                        for r in rows for x in r[s]["mismatch_detail"]][:30]
        summ[s]["consumer_mismatch_examples"] = [dict(cell=f"{r['dataset']}/{r['query']}/{r['model']}", **x)
                                                 for r in rows for x in r[s]["consumer_mismatch_detail"]][:20]
        pr = [p for r in rows for p in (r[s]["probes"] or [])]
        summ[s]["probes"] = len(pr)
        summ[s]["probes_refused"] = sum(p["refused"] for p in pr)
    for s in [x for x in ("S2", "S2c", "S3") if x in summ]:
        summ[s]["store_object_intact"] = [r[s]["store_object_intact"] for r in rows if r[s].get("store_object_intact") is not None]
    summ["S0"] = {k: sum(r["S0"].get(k, 0) for r in rows) for k in ("calls", "live_errors", "consumers",
                                                                       "consumers_nondeterministic", "consumer_nonzero_rc")}
    json.dump(summ, open(out, "w"), indent=1, default=str)
    print(json.dumps({k: (v if k != "S3" else {kk: vv for kk, vv in v.items() if "examples" not in kk}) for k, v in summ.items()
                      if k in ("cells", "runs", "S0", "S3")}, indent=1, default=str))


def run_concurrent(args):
    """A10-A12 live stress test: the N attempts of one cell run as c concurrent threads against one shared S3 store
    (lock-protected table; refcount pinning; LRU among unpinned objects; bypass when nothing fits). Miss policy
    `optimistic` executes and re-checks the table at publication (a losing candidate is discarded and the published
    object is bound instead); `singleflight` waits for the in-flight producer. `pinning=False` is a negative control:
    eviction ignores references, and bound files that disappear while their attempt runs are counted as dangling.
    Every tool message / inline value / bound file is recorded as in the end-to-end pass."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    import yaml
    dataset, repo, order, by_run, budget, c, tau, policy, pinning, base = args
    cfg = yaml.safe_load(open(os.path.join(repo, f"query_{dataset}", "db_config.yaml")))["db_clients"]
    serialize = R.load_serialize(repo)
    store_dir = os.path.join(base, "store")
    os.makedirs(store_dir)
    table, lock, inflight = OrderedDict(), threading.Lock(), {}
    used = [0.0]
    st = {k: 0 for k in ("hits", "execs", "dup_exec", "discarded", "waits", "bypass", "would_evict_live",
                         "dangling", "self_mismatch")}
    st["wait_s"] = 0.0
    st_lock = threading.Lock()

    def inc(name, v=1):
        with st_lock:
            st[name] += v

    def evict_until(charge):
        for ek in list(table):
            if used[0] + charge <= budget:
                break
            e = table[ek]
            if e["ref"] > 0:
                inc("would_evict_live", 1)
                if pinning:
                    continue
            table.pop(ek)
            used[0] -= e["charge"]
            if e["path"]:
                _remove(e["path"])
        return used[0] + charge <= budget

    def attempt(r):
        eng = R.Engines(os.path.join(repo, f"query_{dataset}"), cfg, 120.0)
        adir = os.path.join(base, f"attempt_{r}", "file_storage")
        os.makedirs(adir)
        recs, bind, held, names, turn = [], {}, set(), set(), None
        for ci, cc in enumerate(by_run[r]):
            if cc["step"] != turn:
                time.sleep(tau)
                turn = cc["step"]
            name, n = cc["tool_call_id"], 1
            while name in names:
                name, n = f"{cc['tool_call_id']}#{n}", n + 1
            names.add(name)
            fname = os.path.join(adir, f"{name}.json")
            rel = os.path.join("file_storage", os.path.basename(fname))
            rkey, k = cc["result_key"], cc["key"]
            content, kind, e = None, "skipped", None
            while cc["admitted"]:
                ev = None
                with lock:
                    e = table.get(k)
                    if e is not None:
                        table.move_to_end(k)
                        if k not in held:
                            e["ref"] += 1
                            held.add(k)
                    elif policy == "singleflight":
                        ev = inflight.get(k)
                        if ev is None:
                            inflight[k] = threading.Event()
                if e is not None or ev is None:
                    break
                t_w = time.perf_counter()
                ev.wait()
                inc("waits", 1)
                inc("wait_s", time.perf_counter() - t_w)
            if e is not None:
                inc("hits", 1)
                content = dab_message(rkey, e["spilled"], e["text"])
                if e["path"]:
                    os.symlink(e["path"], fname)
                    bind[rkey] = rel
                else:
                    bind[rkey] = e["inline"]
                kind = "spilled" if e["spilled"] else "inline"
            elif cc["replay_ok"]:
                try:
                    result, ok = serialize(eng.execute(cc["db_name"], cc["text"])), True
                except Exception as ex_:
                    result, ok = f"{type(ex_).__name__}: {ex_}", False
                inc("execs", 1)
                payload = json.dumps(result)
                spilled = ok and len(payload) > PREVIEW
                kind = ("spilled" if spilled else "inline") if ok else "error"
                content = dab_message(rkey, spilled, payload, ok)
                cand = None
                if spilled:
                    cand = os.path.join(store_dir if cc["admitted"] else adir,
                                        f"cand-{r}-{ci}.json" if cc["admitted"] else f"{name}.json")
                    with open(cand, "w", encoding="utf-8") as f:
                        json.dump(result, f, indent=2)
                if ok and cc["admitted"]:
                    final = os.path.join(store_dir, hashlib.blake2b(k.encode(), digest_size=12).hexdigest() + ".json")
                    with lock:
                        e = table.get(k)
                        if e is not None:                                  # another attempt published first
                            inc("dup_exec", 1)
                            inc("discarded", 1)
                            if (e["text"] if e["spilled"] else e["text"]) != (payload[:PREVIEW] if spilled else payload):
                                inc("self_mismatch", 1)
                            if k not in held:
                                e["ref"] += 1
                                held.add(k)
                        else:
                            charge = (os.path.getsize(cand) + min(len(payload), PREVIEW) if cand else len(payload.encode())) + A.META_BYTES
                            if evict_until(charge):
                                if cand:
                                    os.chmod(cand, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                                    os.rename(cand, final)
                                e = {"path": final if cand else None, "spilled": spilled,
                                     "text": payload[:PREVIEW] if spilled else payload, "inline": None if spilled else result,
                                     "charge": charge, "ref": 1}
                                table[k] = e
                                used[0] += charge
                                held.add(k)
                            else:
                                inc("bypass", 1)
                        if policy == "singleflight" and k in inflight:
                            inflight.pop(k).set()
                    if e is not None and e.get("path") is not None and cand and os.path.exists(cand):
                        os.remove(cand)                                     # losing candidate
                    if e is not None:
                        content = dab_message(rkey, e["spilled"], e["text"])
                        if e["path"]:
                            os.symlink(e["path"], fname)
                            bind[rkey] = rel
                        else:
                            bind[rkey] = e["inline"]
                    elif cand:                                              # bypass: private copy
                        shutil.move(cand, fname)
                        bind[rkey] = rel
                    else:
                        bind[rkey] = result
                elif ok:
                    if policy == "singleflight" and cc["admitted"]:
                        with lock:
                            if k in inflight:
                                inflight.pop(k).set()
                    bind[rkey] = rel if spilled else result
                elif policy == "singleflight" and cc["admitted"]:
                    with lock:
                        if k in inflight:
                            inflight.pop(k).set()
            fpath = os.path.join(base, f"attempt_{r}", rel) if kind == "spilled" else None
            recs.append({"run": int(r), "i": ci, "kind": kind,
                         "content_h": _h(content.encode("utf-8", "surrogatepass")) if content is not None else None,
                         "inline_h": _h(json.dumps(bind.get(rkey)).encode("utf-8", "surrogatepass")) if kind == "inline" else None,
                         "rel": rel if kind == "spilled" else None,
                         "file_h": _file_h(fpath) if fpath and os.path.exists(fpath) else ("DANGLING" if fpath else None)})
        for v in bind.values():                                             # bound files must still exist at attempt end
            if isinstance(v, str) and v.startswith("file_storage/") and not os.path.exists(os.path.join(base, f"attempt_{r}", v)):
                inc("dangling", 1)
        with lock:
            for k in held:
                if k in table:
                    table[k]["ref"] -= 1
        return recs

    with ThreadPoolExecutor(max_workers=int(c)) as pool:
        recs = [x for part in pool.map(attempt, order) for x in part]
    return recs, st


def cmd_concurrent(v2, repo, dataset, out, contract="E", n_res=2):
    """Live concurrency stress test on the calibration cells of `dataset` (plus the S0 serial reference)."""
    n_res = int(n_res)
    calls, runs, rep, q = admitted_frame(v2, contract)
    sel = json.load(open(os.path.join(v2, "live", "selection.json")))["picks"]
    cells = [(p["query"], p["model"]) for p in sel if p["dataset"] == dataset]
    rng = np.random.default_rng(97)
    results = []
    for qy, mo in cells:
        tq = q[(q.dataset == dataset) & (q["query"] == qy)]
        cq = tq[tq.model == mo].sort_values(["run", "step"])
        run_ids = sorted(runs[(runs.dataset == dataset) & (runs["query"] == qy) & (runs.model == mo)].run)
        by_run = {r: [] for r in run_ids}
        for r, rr in cq.groupby("run"):
            by_run[r] = _calls(rr)
        task_base = _task_base(tq)
        for i in range(n_res):
            order = [int(x) for x in rng.permutation(run_ids)[:8]]
            root = tempfile.mkdtemp(prefix="conc-")
            ref = run_system(("S0", order, by_run, dataset, repo, float("inf"), root, None))[0]["records"]
            refd = {(x["run"], x["i"]): x for x in ref}
            for bname, frac in (("inf", None), ("1/16", 1 / 16)):
                budget = float("inf") if frac is None else frac * max(task_base, 1.0)
                for policy in ("optimistic", "singleflight"):
                    for pinning in (True, False):
                        if not pinning and policy == "singleflight":
                            continue
                        base = tempfile.mkdtemp(prefix="cc-", dir=root)
                        recs, st = run_concurrent((dataset, repo, order, by_run, budget, 8, 0.05, policy, pinning, base))
                        mism = sum(1 for x in recs if (x["kind"], x["content_h"], x["inline_h"], x["file_h"]) !=
                                   tuple(refd[(x["run"], x["i"])][f] for f in ("kind", "content_h", "inline_h", "file_h")))
                        results.append({"dataset": dataset, "query": qy, "model": mo, "res": i, "budget": bname,
                                        "policy": policy, "pinning": pinning, "calls": len(recs), "mismatches_vs_S0": mism, **st})
                        print(json.dumps(results[-1]), flush=True)
                        shutil.rmtree(base, ignore_errors=True)
            shutil.rmtree(root, ignore_errors=True)
    json.dump(results, open(out, "w"), indent=1)


def cmd_costs(live_dir, out):
    ops = {"lookup_s": [], "alias_s": [], "copy_s": [], "copy_bytes": [], "clone_s": [], "regen_s": []}
    for f in glob.glob(os.path.join(live_dir, "calib_*.json")):
        d = json.load(open(f))
        for k in ops:
            ops[k] += d["ops"].get(k, [])
    costs = {"lookup_s": float(np.median(ops["lookup_s"])) if ops["lookup_s"] else None,
             "alias_s": float(np.median(ops["alias_s"])) if ops["alias_s"] else None,
             "copy_s_per_byte": float(np.sum(ops["copy_s"]) / np.sum(ops["copy_bytes"])) if ops["copy_bytes"] else None,
             "clone_s": float(np.median(ops["clone_s"])) if ops["clone_s"] else None,
             "regen_s_median": float(np.median(ops["regen_s"])) if ops["regen_s"] else None,
             "n_samples": {k: len(v) for k, v in ops.items()}}
    json.dump(costs, open(out, "w"), indent=1)
    print(json.dumps(costs, indent=1))


def cmd_validate(v2, live_dir, costs_path, out, contract="B"):
    calls, runs, rep, q = admitted_frame(v2, contract)
    costs = dict(A.COSTS)
    costs.update({k: v for k, v in json.load(open(costs_path)).items() if k in A.COSTS and v is not None})
    rows = []
    for f in sorted(glob.glob(os.path.join(live_dir, f"calib_{contract}_*.json"))):
        d = json.load(open(f))
        g = q[(q.dataset == d["dataset"]) & (q["query"] == d["query"]) & (q.model == d["model"]) & q.admitted].copy()
        sp = g.spilled.fillna(False).astype(bool) & g.ok
        g["charge"] = np.where(sp, g.spill_b + np.minimum(g.bytes, PREVIEW), g.bytes) + A.META_BYTES
        run_ids = sorted(runs[(runs.dataset == d["dataset"]) & (runs["query"] == d["query"]) & (runs.model == d["model"])].run)
        by_run = {r: [] for r in run_ids}
        for r, rr in g.groupby("run"):
            by_run[r] = list(zip(rr.key, rr.t_exec, rr.ser_med.fillna(0), rr.spill_med.fillna(0), rr.spill_b, rr.bytes,
                                 sp[rr.index], rr.charge, rr.engine))
        base_costs = dict(costs, miss_overhead_s=0.0, hit_overhead_s=0.0)
        for res in d["results"]:
            pred = A.simulate_cell(by_run, res["order"], res["budget_bytes"], base_costs)
            s = res["system"]
            rows.append({"task": f"{d['dataset']}/{d['query']}/{d['model']}", "system": s, "budget": res["budget"],
                         "live_time": res["time_admitted"], "pred_time0": pred[f"{s}_time"],
                         "hits": pred[f"{s}_hits"], "misses": pred[f"{s}_calls"] - pred[f"{s}_hits"],
                         "live_written": res["written_admitted"], "pred_written": pred[f"{s}_written"],
                         "live_peak_retained": res.get("peak_retained_a"), "pred_peak_retained": pred[f"{s}_peak_foot"],
                         "live_peak_seq": res.get("peak_seq_a"), "pred_peak_seq": pred[f"{s}_peak_foot_seq"]})
    df = pd.DataFrame(rows)
    # per-call overheads (miss, hit) by least squares on live - simulated; leave-one-task-out for the reported error
    def fit(d_):
        X = d_[["misses", "hits"]].values
        y = (d_.live_time - d_.pred_time0).values
        coef, *_ = np.linalg.lstsq(X, y, rcond=None)
        return np.clip(coef, 0, None)
    om_all, oh_all = fit(df)
    df["pred_time"] = np.nan
    for t in df.task.unique():
        om, oh = fit(df[df.task != t])
        m = df.task == t
        df.loc[m, "pred_time"] = df.loc[m, "pred_time0"] + om * df.loc[m, "misses"] + oh * df.loc[m, "hits"]
    df["time_rel_err"] = (df.pred_time - df.live_time).abs() / df.live_time.replace(0, np.nan)
    df["written_rel_err"] = (df.pred_written - df.live_written).abs() / df.live_written.replace(0, np.nan)
    df["time_rel_err_uncal"] = (df.pred_time0 - df.live_time).abs() / df.live_time.replace(0, np.nan)
    for x in ("peak_retained", "peak_seq"):
        if df[f"live_{x}"].notna().any():
            df[f"{x}_rel_err"] = (df[f"pred_{x}"] - df[f"live_{x}"]).abs() / df[f"live_{x}"].replace(0, np.nan)
    summary = {"n": int(len(df)), "fitted_miss_overhead_s": float(om_all), "fitted_hit_overhead_s": float(oh_all),
               "median_time_rel_err_uncalibrated": float(df.time_rel_err_uncal.median()),
               "median_time_rel_err": float(df.time_rel_err.median()),
               "p90_time_rel_err": float(df.time_rel_err.quantile(0.9)),
               "median_written_rel_err": float(df.written_rel_err.median()),
               "max_written_rel_err": float(df.written_rel_err.max()),
               **{f"{x}_rel_err_{st}": float(getattr(df[f"{x}_rel_err"], st)()) for x in ("peak_retained", "peak_seq")
                  if f"{x}_rel_err" in df for st in ("median", "max")},
               "by_system": df.groupby("system")[["time_rel_err", "written_rel_err"]].median().to_dict()}
    df.to_csv(out.replace(".json", ".csv"), index=False)
    json.dump(summary, open(out, "w"), indent=1, default=float)
    print(json.dumps(summary, indent=1, default=float))


def cmd_validate_variants(v2, live_dir, costs_path, out, contract="E"):
    """Live ablation runs (cow / inline_only / spilled_only / regen) against sim_ablate.simulate_cell_x with the
    same options, attempt orders and budgets; measured op costs (incl. clone) only."""
    import sim_ablate as X
    calls, runs, rep, q = admitted_frame(v2, contract)
    costs = dict(A.COSTS)
    cj = json.load(open(costs_path))
    costs.update({k: v for k, v in cj.items() if k in A.COSTS and v is not None})
    rows = []
    for f in sorted(glob.glob(os.path.join(live_dir, f"calib_{contract}_*.json"))):
        d = json.load(open(f))
        opts = dict(d.get("opts", {}))
        if cj.get("clone_s"):
            opts["clone_s"] = cj["clone_s"]
        g = q[(q.dataset == d["dataset"]) & (q["query"] == d["query"]) & (q.model == d["model"]) & q.admitted].copy()
        sp = g.spilled.fillna(False).astype(bool) & g.ok
        g["charge"] = np.where(sp, g.spill_b + np.minimum(g.bytes, PREVIEW), g.bytes) + A.META_BYTES
        run_ids = sorted(runs[(runs.dataset == d["dataset"]) & (runs["query"] == d["query"]) & (runs.model == d["model"])].run)
        by_run = {r: [] for r in run_ids}
        for r, rr in g.groupby("run"):
            by_run[r] = list(zip(rr.key, rr.t_exec, rr.ser_med.fillna(0), rr.spill_med.fillna(0), rr.spill_b, rr.bytes,
                                 sp[rr.index], rr.charge, rr.engine))
        for res in d["results"]:
            pred = X.simulate_cell_x(by_run, res["order"], res["budget_bytes"], costs, opts)
            s_ = res["system"]
            rows.append({"variant": d.get("variant", "base"), "task": f"{d['dataset']}/{d['query']}/{d['model']}",
                         "system": s_, "budget": res["budget"], "res": res["res"],
                         "live_time": res["time_admitted"], "pred_time": pred[f"{s_}_time"],
                         "live_written": res["written_admitted"], "pred_written": pred[f"{s_}_written"],
                         "live_hits": res["hits"], "pred_hits": pred[f"{s_}_hits"]})
    df = pd.DataFrame(rows)
    df.to_csv(out.replace(".json", ".csv"), index=False)
    summ = {}
    for (v, b_), g in df.groupby(["variant", "budget"]):
        t = g.groupby(["task", "system"])[["live_time", "pred_time", "live_written", "pred_written"]].sum().reset_index()
        per = {}
        for s_ in sorted(t.system.unique()):
            if s_ == "S0":
                continue
            a_ = t[t.system == s_].set_index("task")
            z = t[t.system == "S0"].set_index("task")
            per[s_] = {"live_time_saving_tw": float(1 - a_.live_time.sum() / z.live_time.sum()),
                       "pred_time_saving_tw": float(1 - a_.pred_time.sum() / z.pred_time.sum()),
                       "live_written_vs_S0": float(a_.live_written.sum() / max(z.live_written.sum(), 1)),
                       "pred_written_vs_S0": float(a_.pred_written.sum() / max(z.pred_written.sum(), 1)),
                       "max_written_rel_err": float(((a_.pred_written - a_.live_written).abs() / a_.live_written.replace(0, np.nan)).max())}
        summ[f"{v}|{b_}"] = per
    json.dump(summ, open(out, "w"), indent=1, default=float)
    print(json.dumps(summ, indent=1, default=float))


def cmd_consumer_prep(v2, repo, dataset, case_dir, quota=25, contract="E"):
    """Cases: recorded execute_python calls whose referenced keys are all admitted query_db results,
    self-contained (no network/subprocess) and not mutating a result path; stratified by model x spilled."""
    quota = int(quota)
    calls, runs, rep, q = admitted_frame(v2, contract)
    import yaml
    cfg = yaml.safe_load(open(os.path.join(repo, f"query_{dataset}", "db_config.yaml")))["db_clients"]
    eng = R.Engines(os.path.join(repo, f"query_{dataset}"), cfg, 120.0)
    serialize = R.load_serialize(repo)
    adm = q[(q.dataset == dataset) & q.admitted & q.result_key.notna()].set_index(["model", "query", "run", "result_key"])
    py = calls[(calls.dataset == dataset) & (calls.tool == "execute_python") & calls.text.notna()]
    keys_by_run = calls[(calls.dataset == dataset) & calls.result_key.notna()].groupby(["model", "query", "run"]).result_key.apply(list)
    cands = []
    for c in py.itertuples():
        if any(u in c.text for u in UNSAFE_CODE) or A.WRITE_RE.search(c.text):
            continue
        keys = [k for k in keys_by_run.get((c.model, c.query, c.run), []) if k in c.text]
        if not keys or not all((c.model, c.query, c.run, k) in adm.index for k in keys):
            continue
        spilled = any(adm.loc[(c.model, c.query, c.run, k)].status == "spilled" for k in keys)
        cands.append((c, keys, spilled))
    strata = {}
    for x in cands:
        strata.setdefault((x[0].model, x[2]), []).append(x)
    rng = np.random.default_rng(5)
    per = max(1, quota // max(len(strata), 1))
    picked = [xs[i] for _, xs in sorted(strata.items(), key=lambda kv: str(kv[0])) for i in rng.permutation(len(xs))[:per]][:quota]
    os.makedirs(case_dir, exist_ok=True)
    store = os.path.join(case_dir, "store")
    os.makedirs(store, exist_ok=True)
    manifest = []
    for n, (c, keys, spilled) in enumerate(picked):
        cdir = os.path.join(case_dir, f"case{n}")
        for v in ("S2", "S3"):
            os.makedirs(os.path.join(cdir, v, "file_storage"), exist_ok=True)
        env = {}
        for k in keys:
            row = adm.loc[(c.model, c.query, c.run, k)]
            if row.status == "spilled":
                result = serialize(eng.execute(row.db_name, row.text))
                rel = f"file_storage/{row.tool_call_id}.json"
                env[k] = rel
                with open(os.path.join(cdir, "S2", rel), "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2)
                obj = hashlib.blake2b(row.key.encode(), digest_size=12).hexdigest() + ".json"
                if not os.path.exists(os.path.join(store, obj)):
                    shutil.copyfile(os.path.join(cdir, "S2", rel), os.path.join(store, obj))
                    os.chmod(os.path.join(store, obj), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
                os.symlink(f"/shared/{obj}", os.path.join(cdir, "S3", rel))
            else:
                env[k] = json.loads(row.recorded)
        exec_str = f"code = {c.text.strip()!r}\n\nenv_args = {env!r}\n\nexec(code, env_args)\n"
        for v in ("S2", "S3"):
            open(os.path.join(cdir, v, "_run.py"), "w").write(exec_str)
        manifest.append({"case": f"case{n}", "dataset": dataset, "model": c.model, "query": c.query, "run": int(c.run),
                         "spilled_input": bool(spilled), "recorded_status": c.status})
    sp = [m for m in manifest if m["spilled_input"]]
    if sp:
        n0 = sp[0]["case"]
        rel = next("file_storage/" + f for f in os.listdir(os.path.join(case_dir, n0, "S3", "file_storage")))
        for j, body in enumerate([f"open({rel!r}, 'w').write('x')", f"import os; os.remove(os.path.realpath({rel!r}))",
                                  f"import json; json.dump([], open({rel!r}, 'w'))"]):
            pdir = os.path.join(case_dir, f"probe{j}")
            for v in ("S2", "S3"):
                shutil.copytree(os.path.join(case_dir, n0, v), os.path.join(pdir, v), symlinks=True)
                open(os.path.join(pdir, v, "_run.py"), "w").write(body + "\n")
    json.dump(manifest, open(os.path.join(case_dir, "manifest.json"), "w"), indent=1)
    print(f"{dataset}: prepared {len(manifest)} cases from {len(cands)} candidates; strata={len(strata)}")


def cmd_consumer_compare(case_root, out):
    rows, probes = [], []
    for man in sorted(glob.glob(os.path.join(case_root, "*", "manifest.json"))):
        cdir = os.path.dirname(man)
        meta = {m["case"]: m for m in json.load(open(man))}
        for name in sorted(os.listdir(cdir)):
            if not (name.startswith("case") or name.startswith("probe")):
                continue
            res = {}
            for v in ("S2", "S3"):
                p = os.path.join(cdir, name, v)
                rc = open(os.path.join(p, "_rc")).read().strip() if os.path.exists(os.path.join(p, "_rc")) else None
                outp = open(os.path.join(p, "_out"), errors="replace").read() if os.path.exists(os.path.join(p, "_out")) else ""
                res[v] = (rc, outp)
            infra = any(r[0] in (None, "124") or "FATAL:" in r[1] for r in res.values())
            if name.startswith("probe"):
                probes.append({"dir": f"{os.path.basename(cdir)}/{name}", "S2_rc": res["S2"][0], "S3_rc": res["S3"][0],
                               "S3_refused": res["S3"][0] != "0" and ("Read-only file system" in res["S3"][1] or "Permission denied" in res["S3"][1])})
                continue
            m = meta.get(name, {})
            s2_err = res["S2"][0] != "0" or "Traceback" in res["S2"][1]
            rows.append({"dataset": os.path.basename(cdir), "case": name, "infra_failure": infra, "identical": res["S2"] == res["S3"],
                         "S2_error": s2_err, "recorded_error": m.get("recorded_status") == "error",
                         "spilled_input": m.get("spilled_input")})
    df = pd.DataFrame(rows)
    valid = df[~df.infra_failure]
    summary = {"cases": int(len(df)), "infra_failures": int(df.infra_failure.sum()), "valid": int(len(valid)),
               "identical_S2_S3": int(valid.identical.sum()),
               "identical_rate": float(valid.identical.mean()) if len(valid) else None,
               "spilled_input_cases": int(valid.spilled_input.sum()),
               "S2_error_class_agrees_with_recorded": float((valid.S2_error == valid.recorded_error).mean()) if len(valid) else None,
               "divergent": valid[~valid.identical][["dataset", "case"]].to_dict("records"),
               "probes": probes, "all_S3_probes_refused": all(p["S3_refused"] for p in probes) if probes else None}
    json.dump(summary, open(out, "w"), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"select": cmd_select, "calibrate": cmd_calibrate, "costs": cmd_costs, "validate": cmd_validate,
     "consumer-prep": cmd_consumer_prep, "consumer-compare": cmd_consumer_compare, "e2e": cmd_e2e,
     "validate-variants": cmd_validate_variants, "concurrent": cmd_concurrent,
     "e2e-summary": cmd_e2e_summary}[cmd](*args)
