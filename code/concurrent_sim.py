"""A10-A12: event-driven simulation of N attempts run c at a time against one task-scoped store (S2 or S3).

An attempt is its recorded sequence of query_db calls (Contract-E admitted); before the first call of each LLM
turn the attempt spends tau seconds (assumed LLM latency; DAB traces carry no timestamps). Call durations come
from the isolated cost table and the measured per-op costs.
Concurrent-miss policy: optimistic (execute; at completion re-check the table and discard the result if another
attempt published first) or singleflight (wait for the in-flight execution, then hit).
Overflow when a result does not fit because live attempts pin objects: bypass (private copy) or wait (block
until an attempt ends and frees space; bypass if nothing else is running).
Pinning: objects referenced by a live attempt are never evicted; `would_evict_live` counts the evictions LRU
would otherwise have made of such objects. Tool latency (`tool_s`) includes waiting (single-flight and overflow
wait), so `wait_s / tool_s` is the share of tool latency spent waiting; a discarded duplicate candidate counts
toward peak footprint at the instant it exists.

Usage: concurrent_sim.py regress <v2> <n> | run <v2> <out.json> [n_res] [workers]
"""
import heapq
import json
import math
import sys
from collections import OrderedDict
from multiprocessing import Pool

import numpy as np
import pandas as pd

import analyze2 as A
import sim_ablate as X

CS = (1, 2, 4, 8)
TAUS = (0.0, 2.0, 10.0)
BUDGETS = {"inf": None, "1/4": 0.25, "1/16": 1 / 16}
POLICIES = (("optimistic", "bypass"), ("singleflight", "bypass"), ("optimistic", "wait"))


def run_batch(by_run, order, budget, costs, c, tau, system, miss_policy, overflow):
    lk, al, cp = costs["lookup_s"], costs["alias_s"], costs["copy_s_per_byte"]
    store = OrderedDict()            # key -> [charge, refcount, store_bytes]
    st = {"used": 0.0, "store_b": 0.0, "pinned": 0.0}
    inflight = {}                    # key -> completion time of the first in-flight execution
    att = {a: {"i": 0, "turn": None, "held": set(), "local": set(), "priv": 0.0, "llm": False} for a in order}
    m = {k: 0.0 for k in ("tool_s", "exec_s", "n_exec", "dup_exec", "dup_exec_s", "wait_s", "bypass", "written",
                          "wasted_written", "hits", "peak_foot", "peak_foot_seq", "peak_pinned", "would_evict_live",
                          "makespan")}
    priv_all, live_priv = [0.0], {}
    heap, seq = [], [0]
    pending = list(order)
    waiters = []                     # (attempt, call tuple, start time, time blocked) waiting for space

    def push(t, kind, a, extra=None):
        seq[0] += 1
        heapq.heappush(heap, (t, seq[0], kind, a, extra))

    def foot():
        m["peak_foot"] = max(m["peak_foot"], priv_all[0] + st["store_b"])
        m["peak_foot_seq"] = max(m["peak_foot_seq"], sum(live_priv.values()) + st["store_b"])
        m["peak_pinned"] = max(m["peak_pinned"], st["pinned"])

    def add_priv(a, b):
        priv_all[0] += b
        live_priv[a] = live_priv.get(a, 0.0) + b

    def pin(a, k):
        if system == "S3" and k not in att[a]["held"] and k in store:
            att[a]["held"].add(k)
            store[k][1] += 1
            if store[k][1] == 1:
                st["pinned"] += store[k][0]

    def try_put(k, charge, sb):
        if charge > budget:
            return False
        for ek in list(store):
            if st["used"] + charge <= budget:
                break
            if store[ek][1] == 0:
                ch, _, b_ = store.pop(ek)
                st["used"] -= ch
                st["store_b"] -= b_
            else:
                m["would_evict_live"] += 1
        if st["used"] + charge > budget:
            return False
        store[k] = [charge, 0, sb]
        st["used"] += charge
        st["store_b"] += sb
        return True

    def start_next(t):
        if pending:
            a = pending.pop(0)
            live_priv[a] = 0.0
            push(t, "ready", a)

    for _ in range(min(c, len(pending))):
        start_next(0.0)
    while heap or waiters:
        if not heap:                                         # every running attempt waits for space: bypass
            retry, waiters[:] = list(waiters), []
            for (wa, wc, t0, tb) in retry:
                push(tb, "publish", wa, (wc, t0, True, tb, True))
            continue
        t, _, kind, a, extra = heapq.heappop(heap)
        s = att[a]
        calls = by_run[a]
        if kind == "ready":
            if s["i"] >= len(calls):                         # attempt end: release pins, private files (seq lifetime)
                for k in s["held"]:
                    if k in store:
                        store[k][1] -= 1
                        if store[k][1] == 0:
                            st["pinned"] -= store[k][0]
                live_priv.pop(a, None)
                m["makespan"] = max(m["makespan"], t)
                foot()
                retry, waiters[:] = list(waiters), []
                for (wa, wc, t0, tb) in retry:
                    push(t, "publish", wa, (wc, t0, True, tb, False))
                start_next(t)
                continue
            k, ex, ser, sp, spb, b, spilled, charge, eng, turn = calls[s["i"]]
            if turn != s["turn"] and not s["llm"]:
                s["llm"] = True
                push(t + tau, "ready", a)
                continue
            s["llm"], s["turn"] = False, turn
            wb = spb if spilled else 0.0
            if system == "S2" and k in s["local"]:           # S2: repeat within the attempt aliases its own file
                push(t + lk + al, "done", a, lk + al)
                m["hits"] += 1
                continue
            if k in store:
                store.move_to_end(k)
                d = lk + (al if system == "S3" else wb * cp)
                if system == "S2":
                    add_priv(a, wb)
                    m["written"] += wb
                pin(a, k)
                s["local"].add(k)
                m["hits"] += 1
                foot()
                push(t + d, "done", a, d)
                continue
            if k in inflight and miss_policy == "singleflight":
                m["wait_s"] += inflight[k] - t
                s["sf_wait"] = s.get("sf_wait", 0.0) + inflight[k] - t   # part of this call's latency
                push(inflight[k], "ready", a)                # re-issue when the producer finishes
                s["llm"] = True                              # do not add the LLM latency again
                continue
            full = ex + ser + (sp if spilled else 0.0)
            m["n_exec"] += 1
            m["exec_s"] += ex
            if k in inflight:
                m["dup_exec"] += 1
                m["dup_exec_s"] += ex
            else:
                inflight[k] = t + full
            push(t + full, "publish", a, (calls[s["i"]], t, False, None, False))
            continue
        if kind == "publish":
            call, t0, is_retry, t_block, force_bypass = extra
            k, ex, ser, sp, spb, b, spilled, charge, eng, turn = call
            wb = spb if spilled else 0.0
            if is_retry:
                m["wait_s"] += t - t_block
            elif inflight.get(k, -1) <= t:
                inflight.pop(k, None)
            extra_t = 0.0
            if system == "S2" and not is_retry:              # private file written by the attempt itself
                add_priv(a, wb)
                m["written"] += wb
            if k in store:                                   # another attempt published first: discard ours
                if system == "S3":                           # the discarded candidate existed until this instant
                    m["peak_foot"] = max(m["peak_foot"], priv_all[0] + st["store_b"] + wb)
                    m["peak_foot_seq"] = max(m["peak_foot_seq"], sum(live_priv.values()) + st["store_b"] + wb)
                m["wasted_written"] += wb if system == "S3" else 0.0
                m["written"] += wb if system == "S3" else 0.0
                pin(a, k)
            elif not force_bypass and try_put(k, charge, wb):
                m["written"] += wb
                if system == "S2":
                    extra_t = wb * cp
                pin(a, k)
            elif not force_bypass and overflow == "wait" and any(x != a and x not in {w[0] for w in waiters} for x in live_priv):
                waiters.append((a, call, t0, t))
                continue
            else:
                m["bypass"] += 1
                if system == "S3":
                    add_priv(a, wb)
                    m["written"] += wb
            att[a]["local"].add(k)
            foot()
            dur = (t - t0) + extra_t
            push(t + extra_t, "done", a, dur)
            continue
        if kind == "done":
            m["tool_s"] += extra + s.pop("sf_wait", 0.0)   # tool latency includes single-flight waiting
            s["i"] += 1
            push(t, "ready", a)
    return m


def cells_with_turns(q, runs, mask):
    g = q[mask].copy()
    sp = g.spilled.fillna(False).astype(bool) & g.ok
    g["store_bytes"] = np.where(sp, g.spill_b, g.bytes)
    g["charge"] = g.store_bytes + np.where(sp, np.minimum(g.bytes, A.PREVIEW), 0) + A.META_BYTES
    g = g.sort_values(["dataset", "query", "model", "run", "step"])
    base = g.drop_duplicates(["dataset", "query", "key"]).groupby(["dataset", "query"]).store_bytes.sum().to_dict()
    out = []
    for (ds, qy), tr in runs.groupby(["dataset", "query"]):
        tg = g[(g.dataset == ds) & (g["query"] == qy)]
        cells = {}
        for mo, mr in tr.groupby("model"):
            mg = tg[tg.model == mo]
            by_run = {r: [] for r in mr.run}
            for r, rr in mg.groupby("run"):
                by_run[r] = list(zip(rr.key, rr.t_exec, rr.ser_med.fillna(0), rr.spill_med.fillna(0), rr.spill_b, rr.bytes,
                                     (rr.spilled.fillna(False).astype(bool) & rr.ok), rr.charge, rr.engine, rr.turn))
            cells[mo] = (by_run, np.array(sorted(by_run)))
        out.append(((ds, qy), cells, max(base.get((ds, qy), 0.0), 1.0)))
    return out


def _task(args):
    (ds, qy), cells, base, n_res, costs, seed = args
    rows = []
    for mi, (mo, (by_run, runs_all)) in enumerate(sorted(cells.items())):
        if len(runs_all) < 8:
            continue
        for i in range(n_res):
            order = [int(x) for x in np.random.default_rng([seed, mi, 8, i]).permutation(runs_all)[:8]]
            for bname, frac in BUDGETS.items():
                budget = math.inf if frac is None else frac * base
                for c in CS:
                    for tau in TAUS:
                        for system in ("S2", "S3"):
                            for mp, ov in POLICIES:
                                r = run_batch(by_run, order, budget, costs, c, tau, system, mp, ov)
                                rows.append({"dataset": ds, "query": qy, "model": mo, "res": i, "budget": bname, "c": c,
                                             "tau": tau, "system": system, "miss": mp, "overflow": ov, **r})
    return rows


def cmd_regress(v2, n=200):
    """c = 1, tau = 0, optimistic + bypass must reproduce the sequential simulator's S2/S3 hits, executions and bytes."""
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    costs = X.load_costs(v2)
    tasks = cells_with_turns(q, runs, gates["E_certified"])
    rng = np.random.default_rng(1)
    bad = 0
    for _ in range(int(n)):
        (t, cells, base) = tasks[rng.integers(len(tasks))]
        mo = list(cells)[rng.integers(len(cells))]
        by_run, runs_all = cells[mo]
        order = [int(x) for x in rng.permutation(runs_all)[:8]]
        budget = [math.inf, base / 4, base / 16][rng.integers(3)]
        seq = X.simulate_cell_x({r: [c[:9] for c in by_run[r]] for r in by_run}, order, budget, costs)
        for s in ("S2", "S3"):
            ev = run_batch(by_run, order, budget, costs, 1, 0.0, s, "optimistic", "bypass")
            pairs = [("hits", seq[f"{s}_hits"]), ("exec_s", seq[f"{s}_exec"]), ("written", seq[f"{s}_written"]),
                     ("bypass", seq[f"{s}_bypass"]), ("tool_s", seq[f"{s}_time"]), ("peak_foot", seq[f"{s}_peak_foot"]),
                     ("peak_foot_seq", seq[f"{s}_peak_foot_seq"])]
            for name, v in pairs:
                if abs(ev[name] - v) > 1e-6 * max(1.0, abs(v)):
                    bad += 1
                    if bad <= 10:
                        print("MISMATCH", t, mo, s, budget, name, ev[name], v)
    print(f"regress: {n} cells x 2 systems, {bad} mismatches")
    assert bad == 0


def cmd_run(v2, out, n_res=10, workers=16):
    n_res, workers = int(n_res), int(workers)
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    costs = X.load_costs(v2)
    tasks = [(t, cells, base, n_res, costs, 707 + i) for i, (t, cells, base) in enumerate(cells_with_turns(q, runs, gates["E_certified"]))]
    with Pool(workers) as pool:
        rows = [r for part in pool.imap_unordered(_task, tasks) for r in part]
    df = pd.DataFrame(rows)
    df.to_parquet(out.replace(".json", ".parquet"))
    keys = ["budget", "c", "tau", "system", "miss", "overflow"]
    pt = df.groupby(["dataset", "query"] + keys).sum(numeric_only=True).reset_index()
    summ = []
    for kk, g in pt.groupby(keys):
        ref = pt[(pt.budget == kk[0]) & (pt.c == 1) & (pt.tau == kk[2]) & (pt.system == kk[3]) & (pt.miss == "optimistic")
                 & (pt.overflow == "bypass")].set_index(["dataset", "query"])
        gi = g.set_index(["dataset", "query"])
        rel = lambda col: float((gi[col] / ref[col].reindex(gi.index).replace(0, np.nan)).median())
        summ.append({**dict(zip(keys, kk)), "tasks": int(len(g)),
                     "dup_exec_share_of_exec": float(g.dup_exec.sum() / max(g.n_exec.sum(), 1)),
                     "dup_exec_s_share": float(g.dup_exec_s.sum() / max(g.exec_s.sum(), 1e-12)),
                     "wait_s_share_of_tool": float(g.wait_s.sum() / max(g.tool_s.sum(), 1e-12)),
                     "bypass": float(g.bypass.sum()), "would_evict_live": float(g.would_evict_live.sum()),
                     "wasted_written_share": float(g.wasted_written.sum() / max(g.written.sum(), 1)),
                     "exec_s_vs_c1": rel("exec_s"), "written_vs_c1": rel("written"),
                     "peak_foot_vs_c1": rel("peak_foot"), "peak_pinned_vs_store_budget": None,
                     "makespan_vs_c1": rel("makespan")})
    json.dump({"n_res": n_res, "costs": costs, "taus": TAUS, "summary": summ}, open(out, "w"), indent=1, default=float)
    s = pd.DataFrame(summ)
    print(s[(s.tau == 2.0) & (s.miss == "optimistic") & (s.overflow == "bypass")][
        ["budget", "c", "system", "dup_exec_s_share", "bypass", "would_evict_live", "exec_s_vs_c1", "written_vs_c1",
         "peak_foot_vs_c1", "makespan_vs_c1"]].to_string())


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"run": cmd_run, "regress": cmd_regress}[cmd](*args)
