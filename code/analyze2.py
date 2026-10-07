"""Main analysis: gate funnel (B0), scope decomposition (B1), N-curves and S0-S3
cache simulation with capacity (B2/B3), robustness (B5).

Inputs : <v2>/calls.parquet, <v2>/runs.parquet, <v2>/replay/*.parquet
Outputs: <out>/*.csv, <out>/*.parquet, <out>/results.json
Usage  : analyze2.py <v2> <out> [n_orders] [n_resamples] [workers] [costs.json]
costs.json (from live calibration) may set lookup_s, alias_s, copy_s_per_byte.
"""
import glob
import json
import math
import os
import re
import sys
from collections import OrderedDict
from multiprocessing import Pool

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from certify import certify, load_schema  # noqa: E402

SCOPES = ["session", "trials", "models", "tasks", "none"]
SCOPE_ID = {s: i for i, s in enumerate(SCOPES)}
PREVIEW = 10000
META_BYTES = 256
NS = [1, 2, 4, 8, 16, 50]
BUDGET_NS = (2, 4, 8)
BUDGETS = {"inf": None, "1/16": 1 / 16, "1/4": 1 / 4, "1": 1.0}
COSTS = {"lookup_s": 2e-5, "alias_s": 5e-5, "copy_s_per_byte": 1e-9, "miss_overhead_s": 0.0, "hit_overhead_s": 0.0}
WRITE_RE = re.compile(r"""open\([^)]*var_[^)]*,\s*['"][wa+]|os\.(remove|unlink|rename|replace)\([^)]*var_|shutil\.(move|rmtree)\([^)]*var_""")


# ----------------------------------------------------------------------------- loading

def med(xs):
    xs = [x for x in (xs if xs is not None else []) if x is not None]
    return float(np.median(xs)) if xs else np.nan


def outcome_class(r):
    st = list(r.status_all) if r.status_all is not None else []
    if not st:
        return "not_replayed"
    if all(s == "ok" for s in st):
        ph = list(r.payload_hashes) if r.payload_hashes is not None else []
        bh = [b for b in (r.bag_hashes if r.bag_hashes is not None else [])]
        if len(ph) == len(st) and len(set(ph)) == 1:
            return "stable"
        if any(b is None for b in bh):
            return "unstable_unclassified"
        if len(bh) == len(st) and len(set(bh)) == 1:
            return "order_unstable"
        return "value_unstable"
    if any(s == "timeout" for s in st):
        return "timeout"
    if all(s == "error" for s in st):
        return "error"
    return "mixed"


def load(v2):
    calls = pd.read_parquet(f"{v2}/calls.parquet")
    runs = pd.read_parquet(f"{v2}/runs.parquet")
    replay_dir = os.environ.get("ED_REPLAY_DIR", f"{v2}/replay")
    rep = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(f"{replay_dir}/*.parquet"))], ignore_index=True)
    dup = rep.duplicated(["dataset", "db_name", "text"], keep=False)
    assert not dup.any(), f"duplicate replay rows: {int(dup.sum())}"
    rep["cls"] = rep.apply(outcome_class, axis=1)
    def by_status(r, which):
        st = list(r.status_all) if r.status_all is not None else []
        ex = list(r.exec_s) if r.exec_s is not None else []
        pairs = list(zip(st, ex))
        if which == "completed":
            return med([e for s_, e in pairs if s_ != "timeout"])
        if which == "steady":
            return med([e for s_, e in pairs[1:] if s_ == "ok"])
        if which == "first":
            return pairs[0][1] if pairs and pairs[0][0] != "timeout" else np.nan
    rep["exec_med"] = rep.apply(lambda r: by_status(r, "completed"), axis=1)
    rep["exec_steady"] = rep.apply(lambda r: by_status(r, "steady"), axis=1)
    rep["exec_first"] = rep.apply(lambda r: by_status(r, "first"), axis=1)
    rep["ser_med"] = rep.ser_s.map(med)
    rep["spill_med"] = rep.spill_s.map(med)
    rep["timeout_s"] = rep.timeout_s if "timeout_s" in rep else 120.0
    schema = load_schema(os.environ.get("ED_P2_AUDIT", f"{v2}/p2_audit.json"))   # stored columns, with P2 as audited
    rep["certified"], rep["cert_reason"] = zip(*[certify(t, e, schema=schema.get((d, n)))
                                               if e not in ("unknown", None) else (False, "unknown")
                                               for d, n, t, e in zip(rep.dataset, rep.db_name, rep.text, rep.engine)])
    rep["certified_E"], rep["certE_reason"] = zip(*[certify(t, e, strict=False) if e not in ("unknown", None)
                                                    else (False, "unknown") for t, e in zip(rep.text, rep.engine)])
    q = calls[calls.tool == "query_db"].merge(rep, on=["dataset", "db_name", "text"], how="left", suffixes=("", "_r"))
    q["replayed"] = q.engine.notna() & ~q.engine.isin(["unknown"])

    def fid(r):
        if not r.replayed:
            return "not_replayed"
        if r.status_r == "timeout":
            return "timeout"
        if r.status_r != "ok":
            return "both_error" if r.status == "error" else "replay_error"
        if r.status == "inline":
            return "match" if (not r.spilled and r.payload_head == r.recorded) else "mismatch"
        if r.status == "spilled":
            return "match" if (r.spilled and r.payload_head == r.recorded) else "mismatch"
        return "recorded_error"
    q["fidelity"] = q.apply(fid, axis=1)

    def mismatch_kind(r):
        """Inline mismatches: same rows in another order vs different values (spilled: prefix only)."""
        if r.fidelity != "mismatch":
            return None
        if r.status != "inline" or r.spilled:
            return "prefix_mismatch"
        try:
            a, b = json.loads(r.recorded), json.loads(r.payload_head)
            key = lambda rows: sorted(json.dumps(x, sort_keys=True) for x in rows)
            return "order_only" if isinstance(a, list) and isinstance(b, list) and key(a) == key(b) else "value_mismatch"
        except Exception:
            return "unparsable"
    q["mismatch_kind"] = q.apply(mismatch_kind, axis=1)

    def _norm_val(v):
        if v is None or v in ("nan", "None", "NaN", "NaT"):
            return None
        if isinstance(v, str):
            try:
                return round(float(v), 9)
            except ValueError:
                return v
        if isinstance(v, float):
            return None if v != v else round(v, 9)
        return v

    def _norm_rows(rows, engine):
        out = []
        for x in rows:
            if isinstance(x, dict):
                x = {k: _norm_val(v) for k, v in x.items() if not (engine == "mongo" and k == "_id")}
            out.append(json.dumps(x, sort_keys=True, default=str))
        return sorted(out)

    def equivalent(r):
        """Same answer modulo row order, NaN/None rendering, float last digits and Mongo _id (inline only)."""
        if r.fidelity == "match":
            return True
        if r.fidelity != "mismatch" or r.status != "inline" or r.spilled:
            return False
        try:
            a, b = json.loads(r.recorded), json.loads(r.payload_head)
            return isinstance(a, list) and isinstance(b, list) and _norm_rows(a, r.engine) == _norm_rows(b, r.engine)
        except Exception:
            return False
    q["fidelity_equiv"] = q.apply(equivalent, axis=1)
    q = q.merge(runs[["model", "dataset", "query", "run", "validation"]], on=["model", "dataset", "query", "run"], how="left")
    q["key"] = q.dataset + "\x00" + q.db_name.fillna("") + "\x00" + q.text
    q["ok"] = q.status_r == "ok"
    q["bytes"] = np.where(q.ok, q.utf8_bytes.fillna(0), 0.0)
    q["spill_b"] = np.where(q.ok & q.spilled.fillna(False).astype(bool), q.spill_bytes.fillna(0), 0.0)
    q["t_exec"] = np.where(q.status_r != "timeout", q.exec_med.fillna(0), 0.0)     # completed executions only
    q["t_exec_first"] = np.where(q.status_r != "timeout", q.exec_first.fillna(0), 0.0)
    q["t_exec_steady"] = np.where(q.ok, q.exec_steady.fillna(q.exec_med).fillna(0), 0.0)
    q["t_exec_lb"] = np.where(q.status_r == "timeout", q.timeout_s.fillna(120.0), q.exec_med.fillna(0))
    return calls, runs, rep, q


# ----------------------------------------------------------------------------- B0

def funnel(q, calls):
    gates = OrderedDict()
    gates["all_query_db"] = pd.Series(True, index=q.index)
    gates["replayed"] = q.replayed
    gates["successful"] = q.replayed & q.ok
    gates["fidelity_equiv"] = gates["successful"] & q.fidelity_equiv
    gates["fidelity_pass"] = gates["successful"] & (q.fidelity == "match")
    gates["stable"] = gates["fidelity_pass"] & (q.cls == "stable")
    gates["E_certified"] = gates["stable"] & q.certified_E.fillna(False).astype(bool)
    gates["B_certified"] = gates["stable"] & q.certified.fillna(False).astype(bool)
    rows = []
    for name, m in gates.items():
        for level, groups in (("engine", q.groupby("engine")), ("dataset", q.groupby("dataset")), ("all", [("all", q)])):
            for gname, g in groups:
                mm = m[g.index]
                rows.append({"gate": name, "level": level, "group": gname, "calls": int(mm.sum()),
                             "calls_share": float(mm.mean()),
                             "time_lb_share": float(g.t_exec_lb[mm].sum() / max(g.t_exec_lb.sum(), 1e-12)),
                             "bytes_share": float(g.bytes[mm].sum() / max(g.bytes.sum(), 1))})
    py = calls[(calls.tool == "execute_python") & calls.text.notna()]
    audit = {"python_calls": int(len(py)), "python_writes_result_path": int(py.text.str.contains(WRITE_RE).sum())}
    return pd.DataFrame(rows), gates, audit


# ----------------------------------------------------------------------------- B1

def scope_codes(ds_frame, rng):
    """One fully random order of attempts (preserving steps); innermost scope code per call."""
    att = ds_frame[["query", "model", "run"]].drop_duplicates()
    rank = dict(zip(att.itertuples(index=False, name=None), rng.permutation(len(att))))
    K, Q, M, R, S = (ds_frame[c].tolist() for c in ("key", "query", "model", "run", "step"))
    order = sorted(range(len(K)), key=lambda i: (rank[(Q[i], M[i], R[i])], S[i]))
    out = np.empty(len(K), dtype=np.int8)
    seen_run, seen_mt, seen_t, seen_d = set(), set(), set(), set()
    for i in order:
        k, qy, mo, ru = K[i], Q[i], M[i], R[i]
        if (qy, mo, ru, k) in seen_run:
            s = 0
        elif (qy, mo, k) in seen_mt:
            s = 1
        elif (qy, k) in seen_t:
            s = 2
        elif k in seen_d:
            s = 3
        else:
            s = 4
        out[i] = s
        seen_run.add((qy, mo, ru, k)); seen_mt.add((qy, mo, k)); seen_t.add((qy, k)); seen_d.add(k)
    return out


def _orders_for_dataset(args):
    frame, n_orders, seed = args
    rng = np.random.default_rng(seed)
    return np.stack([scope_codes(frame, rng) for _ in range(n_orders)])


def decomposition(q, mask, n_orders, workers, seed=0):
    g = q[mask].reset_index(drop=True)
    parts = [(gg, n_orders, seed + i) for i, (_, gg) in enumerate(g.groupby("dataset", sort=True))]
    with Pool(workers) as pool:
        codes = pool.map(_orders_for_dataset, parts)
    C = np.empty((n_orders, len(g)), dtype=np.int8)
    for (gg, _, _), c in zip(parts, codes):
        C[:, gg.index.values] = c
    weights = {"calls": np.ones(len(g)), "exec_s": g.t_exec.values, "exec_s_first": g.t_exec_first.values,
               "exec_s_steady": g.t_exec_steady.values,
               "exec_s_lb": g.t_exec_lb.values, "payload_bytes": g.bytes.values, "spill_bytes": g.spill_b.values}

    def shares(idx):
        res = {}
        for w, wv in weights.items():
            wv_i = wv[idx]
            tot = wv_i.sum()
            per_order = np.array([[wv_i[C[o, idx] == s].sum() / tot if tot > 0 else np.nan for s in range(5)]
                                  for o in range(n_orders)])
            res[w] = {s: {"mean": float(np.nanmean(per_order[:, j])), "p2.5": float(np.nanpercentile(per_order[:, j], 2.5)),
                          "p97.5": float(np.nanpercentile(per_order[:, j], 97.5))} for j, s in enumerate(SCOPES)}
        return res

    allidx = np.arange(len(g))
    out = {"overall": shares(allidx)}
    out["by_model"] = {m: shares(gg.index.values) for m, gg in g.groupby("model")}
    out["by_engine"] = {e: shares(gg.index.values) for e, gg in g.groupby("engine")}
    out["by_validation"] = {v: shares(gg.index.values) for v, gg in g.groupby("validation")}
    out["lodo"] = {d: shares(g.index.values[g.dataset != d]) for d in sorted(g.dataset.unique())}
    # task-level cross-attempt vs session shares (mean over orders), then macro statistics
    task_rows = []
    for (ds, qy), tg in g.groupby(["dataset", "query"]):
        idx = tg.index.values
        for w in ("exec_s", "payload_bytes", "calls"):
            wv = weights[w][idx]
            if wv.sum() <= 0:
                continue
            sh = [[wv[C[o, idx] == s].sum() / wv.sum() for s in range(5)] for o in range(n_orders)]
            m = np.mean(sh, axis=0)
            task_rows.append({"dataset": ds, "query": qy, "weight": w, **{s: float(m[j]) for j, s in enumerate(SCOPES)}})
    tasks = pd.DataFrame(task_rows)
    out["task_macro"] = {}
    for w, tw in tasks.groupby("weight"):
        out["task_macro"][w] = {s: boot_stats(tw[s].values) for s in ("session", "trials", "models", "tasks")}
        out["task_macro"][w]["cross_attempt_minus_session"] = boot_stats((tw.trials - tw.session).values)
    # concentration of reusable completed execution time (order 0)
    reuse = g.assign(code=C[0])[lambda d: d.code != 4].groupby("key").t_exec.sum().sort_values(ascending=False)
    k1 = max(1, math.ceil(len(reuse) / 100))
    out["concentration"] = {"n_reusable_keys": int(len(reuse)),
                            "top1pct_share": float(reuse.head(k1).sum() / max(reuse.sum(), 1e-12)),
                            "top10_share": float(reuse.head(10).sum() / max(reuse.sum(), 1e-12))}
    return out, tasks


def boot_stats(x, n=2000, seed=7):
    x = np.asarray([v for v in x if v is not None and not np.isnan(v)])
    if len(x) == 0:
        return {"n": 0}
    rng = np.random.default_rng(seed)
    b = [np.median(rng.choice(x, len(x))) for _ in range(n)]
    return {"n": int(len(x)), "median": float(np.median(x)), "q25": float(np.percentile(x, 25)),
            "q75": float(np.percentile(x, 75)), "mean": float(np.mean(x)),
            "median_ci": [float(np.percentile(b, 2.5)), float(np.percentile(b, 97.5))]}


# ----------------------------------------------------------------------------- B2/B3

class LRUStore:
    """Byte-budgeted LRU; entries with refcount > 0 are never evicted."""

    def __init__(self, budget):
        self.budget, self.used, self.peak, self.d = budget, 0.0, 0.0, OrderedDict()

    def get(self, k):
        if k in self.d:
            self.d.move_to_end(k)
            return True
        return False

    def put(self, k, charge, on_evict=None):
        if charge > self.budget:
            return False
        for ek in list(self.d):
            if self.used + charge <= self.budget:
                break
            if self.d[ek][1] == 0:
                self.used -= self.d.pop(ek)[0]
                if on_evict:
                    on_evict(ek)
        if self.used + charge > self.budget:
            return False
        self.d[k] = [charge, 0]
        self.used += charge
        self.peak = max(self.peak, self.used)
        return True


SYSTEMS = ("S0", "S1", "S2", "S3")
SUMS = ("time", "exec", "payload", "written", "hits", "bypass", "calls")


def simulate_cell(by_run, order, budget, costs):
    """Sequential attempts. S0 no sharing; S1 per-attempt memo; S2 task cache with private copies
    (a cached spilled object is a store copy, written once); S3 shared read-only store with aliases,
    refcounts, LRU among refcount-0 objects, bypass (private, re-executed on repeat) when nothing fits.
    Footprint = per-attempt files + current store files, peak tracked after every call under two lifetimes:
    peak_foot (private files retained until the batch ends: concurrent best-of-N / pass@k) and peak_foot_seq
    (private files deleted when their attempt ends: sequential retries)."""
    m = {f"{s}_{x}": 0.0 for s in SYSTEMS for x in SUMS}
    lat = {s: [] for s in SYSTEMS}
    priv = {s: 0.0 for s in SYSTEMS}
    cur = {s: 0.0 for s in SYSTEMS}
    peak = {s: 0.0 for s in SYSTEMS}
    peak_seq = {s: 0.0 for s in SYSTEMS}
    files = {"S2": {}, "S3": {}}
    s2, s3 = LRUStore(budget), LRUStore(budget)
    lk, al, cp = costs["lookup_s"], costs["alias_s"], costs["copy_s_per_byte"]
    om, oh = costs.get("miss_overhead_s", 0.0), costs.get("hit_overhead_s", 0.0)   # per-call harness overheads (live fit)

    def foot(s):
        st = sum(files[s].values()) if s in files else 0.0
        peak[s] = max(peak[s], priv[s] + st)
        peak_seq[s] = max(peak_seq[s], cur[s] + st)

    def add_priv(s, b):
        priv[s] += b
        cur[s] += b

    def ev(s):
        return lambda k: files[s].pop(k, None)

    for r in order:
        for s in SYSTEMS:
            cur[s] = 0.0
        s1 = LRUStore(budget)
        local2, local3, held3 = set(), set(), []
        for k, ex, ser, sp, spb, b, spilled, charge, eng in by_run[r]:
            full = ex + ser + (sp if spilled else 0.0) + om
            wb = spb if spilled else 0.0
            lk_ = lk + oh

            def acc(s, t, written=0.0, hit=False, execd=False):
                m[f"{s}_time"] += t; m[f"{s}_calls"] += 1; m[f"{s}_written"] += written
                if hit:
                    m[f"{s}_hits"] += 1
                if execd:
                    m[f"{s}_exec"] += ex; m[f"{s}_payload"] += b
                m[f"{s}_time_{eng}"] = m.get(f"{s}_time_{eng}", 0.0) + t
                m[f"{s}_written_{eng}"] = m.get(f"{s}_written_{eng}", 0.0) + written
                lat[s].append(t)
            # S0
            acc("S0", full, wb, execd=True); add_priv("S0", wb); foot("S0")
            # S1
            if s1.get(k):
                acc("S1", lk_ + al, hit=True)
            else:
                acc("S1", full, wb, execd=True); add_priv("S1", wb); foot("S1")
                if not s1.put(k, charge):
                    m["S1_bypass"] += 1
            # S2
            if k in local2:
                acc("S2", lk_ + al, hit=True)
            elif s2.get(k):
                acc("S2", lk_ + wb * cp, wb, hit=True); add_priv("S2", wb); local2.add(k); foot("S2")
            else:
                t, w = full, wb
                add_priv("S2", wb); local2.add(k)
                if s2.put(k, charge, on_evict=ev("S2")):
                    files["S2"][k] = wb; t += wb * cp; w += wb      # retained cache copy
                else:
                    m["S2_bypass"] += 1
                acc("S2", t, w, execd=True); foot("S2")
            # S3
            if s3.get(k):
                acc("S3", lk_ + al, hit=True)
                if k not in local3:
                    s3.d[k][1] += 1; held3.append(k); local3.add(k)
            else:
                if s3.put(k, charge, on_evict=ev("S3")):
                    files["S3"][k] = wb; s3.d[k][1] += 1; held3.append(k); local3.add(k)
                else:
                    m["S3_bypass"] += 1; add_priv("S3", wb)
                acc("S3", full, wb, execd=True); foot("S3")
        for k in held3:
            if k in s3.d:
                s3.d[k][1] -= 1
    for s in SYSTEMS:
        m[f"{s}_peak_foot"] = peak[s]
        m[f"{s}_peak_foot_seq"] = peak_seq[s]
        m[f"{s}_p50"] = float(np.percentile(lat[s], 50)) if lat[s] else np.nan
        m[f"{s}_p95"] = float(np.percentile(lat[s], 95)) if lat[s] else np.nan
    return m


def _sim_task(args):
    (ds, qy), cells, task_budget_base, n_res, costs, seed = args
    rng = np.random.default_rng(seed)
    rows = []
    for mo, (by_run, runs_all) in cells.items():
        for N in NS:
            if N > len(runs_all):
                continue
            for bname, frac in BUDGETS.items():
                if frac is not None and N not in BUDGET_NS:
                    continue
                budget = math.inf if frac is None else frac * task_budget_base
                for i in range(n_res):
                    order = rng.permutation(runs_all)[:N]
                    m = simulate_cell(by_run, order, budget, costs)
                    rows.append({"dataset": ds, "query": qy, "model": mo, "N": N, "budget": bname, "res": i,
                                 "admitted_calls": sum(len(by_run[r]) for r in order), **m})
    return rows


def simulate(q, runs, gate_mask, n_res, workers, costs, seed=11):
    g = q[gate_mask].copy()
    sp = g.spilled.fillna(False).astype(bool) & g.ok
    g["store_bytes"] = np.where(sp, g.spill_b, g.bytes)
    g["charge"] = g.store_bytes + np.where(sp, np.minimum(g.bytes, PREVIEW), 0) + META_BYTES
    g = g.sort_values(["dataset", "query", "model", "run", "step"])
    task_base = g.drop_duplicates(["dataset", "query", "key"]).groupby(["dataset", "query"]).store_bytes.sum().to_dict()
    tasks = []
    for i, ((ds, qy), tr) in enumerate(runs.groupby(["dataset", "query"])):
        tg = g[(g.dataset == ds) & (g["query"] == qy)]
        cells = {}
        for mo, mr in tr.groupby("model"):
            mg = tg[tg.model == mo]
            by_run = {r: [] for r in mr.run}
            for r, rr in mg.groupby("run"):
                by_run[r] = list(zip(rr.key, rr.t_exec, rr.ser_med.fillna(0), rr.spill_med.fillna(0), rr.spill_b,
                                     rr.bytes, (rr.spilled.fillna(False).astype(bool) & rr.ok), rr.charge, rr.engine))
            cells[mo] = (by_run, np.array(sorted(by_run)))
        tasks.append(((ds, qy), cells, max(task_base.get((ds, qy), 0.0), 1.0), n_res, costs, seed + i))
    with Pool(workers) as pool:
        rows = [r for part in pool.imap_unordered(_sim_task, tasks) for r in part]
    return pd.DataFrame(rows)


def summarize_sim(sim):
    """Sum absolute costs over resamples and models per task, then form ratios; task-level statistics."""
    sums = [c for c in sim.columns if any(c.startswith(f"{s}_") for s in SYSTEMS)
            and not c.endswith(("_p50", "_p95", "_peak_foot", "_peak_foot_seq"))]
    sim = sim.copy()
    sim[sums] = sim[sums].fillna(0.0)
    agg = {c: "sum" for c in sums}
    agg.update({c: "mean" for c in sim.columns if c.endswith(("_p50", "_p95", "_peak_foot", "_peak_foot_seq"))})
    agg["admitted_calls"] = "sum"
    per_task = sim.groupby(["dataset", "query", "N", "budget"]).agg(agg).reset_index()
    for sname in ("S1", "S2", "S3"):
        for x in ("exec", "time", "payload", "written"):
            per_task[f"{sname}_{x}_saving"] = np.where(per_task[f"S0_{x}"] > 0, 1 - per_task[f"{sname}_{x}"] / per_task[f"S0_{x}"], np.nan)
        per_task[f"{sname}_hit_rate"] = per_task[f"{sname}_hits"] / per_task[f"{sname}_calls"].replace(0, np.nan)
    per_task["S3_over_S2_time"] = per_task.S3_time / per_task.S2_time.replace(0, np.nan)
    per_task["S3_over_S2_written"] = per_task.S3_written / per_task.S2_written.replace(0, np.nan)
    per_task["S3_over_S2_peak_foot"] = per_task.S3_peak_foot / per_task.S2_peak_foot.replace(0, np.nan)
    per_task["S3_over_S2_peak_foot_seq"] = per_task.S3_peak_foot_seq / per_task.S2_peak_foot_seq.replace(0, np.nan)
    engines = sorted({c.split("_", 2)[2] for c in sums if c.startswith("S2_time_")})
    out = {}
    stat_cols = [c for c in per_task.columns if c.endswith(("_saving", "_hit_rate")) or c.startswith("S3_over_S2")]
    for (N, b), pt in per_task.groupby(["N", "budget"]):
        live = pt[pt.admitted_calls > 0]
        res = {"tasks_with_work": int(len(live)), "tasks_total": int(len(pt)),
               **{c: boot_stats(live[c].values) for c in stat_cols}}
        res["by_engine"] = {e: {"S3_over_S2_time": float(pt[f"S3_time_{e}"].sum() / max(pt[f"S2_time_{e}"].sum(), 1e-12)),
                                "S3_over_S2_written": float(pt[f"S3_written_{e}"].sum() / max(pt[f"S2_written_{e}"].sum(), 1e-12))
                                if pt[f"S2_written_{e}"].sum() > 0 else None}
                            for e in engines if f"S2_time_{e}" in pt}
        res["lodo_S2_exec_saving_median"] = {d: float(live[live.dataset != d].S2_exec_saving.median())
                                             for d in sorted(live.dataset.unique())}
        out[f"N={N}|B={b}"] = res
    return per_task, out


# ----------------------------------------------------------------------------- main

def main(v2, out, n_orders=200, n_res=200, workers=8, costs_path=None):
    n_orders, n_res, workers = int(n_orders), int(n_res), int(workers)
    costs = dict(COSTS)
    costs_source = "placeholder (NOT calibrated)"
    if costs_path and os.path.exists(costs_path):
        costs.update({k: v for k, v in json.load(open(costs_path)).items() if k in COSTS and v is not None})
        costs_source = costs_path
    os.makedirs(out, exist_ok=True)
    off = int(os.environ.get("ED_SEED_OFFSET", "0"))   # seed replication (X5); 0 reproduces the reported run
    calls, runs, rep, q = load(v2)
    fun, gates, audit = funnel(q, calls)
    fun.to_csv(f"{out}/B0_funnel.csv", index=False)
    q.groupby(["engine", "fidelity"]).size().unstack(fill_value=0).to_csv(f"{out}/B0_fidelity.csv")
    q[q.fidelity == "mismatch"].groupby(["engine", "mismatch_kind"]).size().unstack(fill_value=0).to_csv(f"{out}/B0_mismatch_kinds.csv")
    rep.groupby(["engine", "cls"]).size().unstack(fill_value=0).to_csv(f"{out}/B0_outcome_classes.csv")
    rep.cert_reason.value_counts().to_csv(f"{out}/B0_cert_reasons.csv")
    summary = {"runs": int(len(runs)), "runs_without_tool_calls": int((runs.n_tool_calls == 0).sum()),
               "validation": runs.validation.value_counts().to_dict(), "tool_calls": int(len(calls)),
               "query_db": int((calls.tool == "query_db").sum()), "distinct_replayed": int(len(rep)),
               "outcome_classes": rep.cls.value_counts().to_dict(), "mutation_audit": audit, "costs": costs, "costs_source": costs_source,
               "n_orders": n_orders, "n_resamples": n_res, "seed_offset": off}
    print(json.dumps(summary, indent=1, default=float), flush=True)
    dec, tasks = decomposition(q, gates["fidelity_equiv"], n_orders, workers, seed=off)
    tasks.to_csv(f"{out}/B1_per_task.csv", index=False)
    dec_all, _ = decomposition(q, gates["replayed"], max(20, n_orders // 10), workers, seed=99 + off)
    dec_strict, _ = decomposition(q, gates["fidelity_pass"], max(20, n_orders // 10), workers, seed=55 + off)
    dec_B, _ = decomposition(q, gates["B_certified"], max(20, n_orders // 10), workers, seed=77 + off)
    json.dump({"summary": summary, "B1_fidelity_equiv_gate": dec, "B1_all_replayed": dec_all["overall"],
               "B1_strict_fidelity": dec_strict["overall"],
               "B1_B_certified": dec_B["overall"]}, open(f"{out}/results_B1.json", "w"), indent=1, default=float)
    print("B1 overall exec_s:", {s: round(v["mean"], 3) for s, v in dec["overall"]["exec_s"].items()}, flush=True)
    print("B1 overall payload_bytes:", {s: round(v["mean"], 3) for s, v in dec["overall"]["payload_bytes"].items()}, flush=True)
    simF = simulate(q, runs, gates["fidelity_equiv"], n_res, workers, costs, seed=11 + off)
    simF.to_parquet(f"{out}/B2_sim_fidelity.parquet")
    _, curvesF = summarize_sim(simF[simF.budget == "inf"])
    simB = simulate(q, runs, gates["B_certified"], n_res, workers, costs, seed=23 + off)
    simB.to_parquet(f"{out}/B3_sim_Bcert.parquet")
    per_taskB, curvesB = summarize_sim(simB)
    per_taskB.to_csv(f"{out}/B3_per_task.csv", index=False)
    simE = simulate(q, runs, gates["E_certified"], n_res, workers, costs, seed=29 + off)
    simE.to_parquet(f"{out}/B3_sim_Ecert.parquet")
    per_taskE, curvesE = summarize_sim(simE)
    per_taskE.to_csv(f"{out}/B3_per_task_E.csv", index=False)
    json.dump({"B2_fidelity_gate": curvesF, "B3_B_certified": curvesB, "B3_E_certified_exploratory": curvesE},
              open(f"{out}/results_B23.json", "w"), indent=1, default=float)
    for name, cv in (("fidelity", curvesF), ("Bcert", curvesB), ("Ecert", curvesE)):
        for k, v in cv.items():
            meds = {c: round(x.get("median", np.nan), 3) for c, x in v.items() if isinstance(x, dict) and "median" in x
                    and (c.split("_")[1] in ("exec", "written", "payload") or c.startswith("S3_over"))}
            print(name, k, v["tasks_with_work"], meds, flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
