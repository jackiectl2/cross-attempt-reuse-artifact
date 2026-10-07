"""Ablation simulator (Amendment A3). Generalizes analyze2.simulate_cell; with default options its S0-S3
outputs are identical to analyze2.simulate_cell (checked by `regress`).

Systems: S0 no sharing; S1 session memo; SD engine-level result cache (task scope; a hit saves DB execution
only, every attempt still serializes, spills and keeps its private file); S2 task cache with private copies;
S2c S2 with copy-on-write clones (a clone writes no data; physical bytes are counted once per shared extent, where
an extent is one written file generation: after its cached object is evicted, a re-executed key writes a new extent);
S3 shared read-only store with aliases.

Options (opts dict):
  policy   lru | fifo | none (admit until full, never evict) | belady (clairvoyant eviction, upper bound)
  classes  both | inline | spilled   (which result classes may be cached; others are always executed)
  regen    False | True              (a spilled hit regenerates the preview from the file: json.load + json.dumps of the
                                      cached object, modelled as 2 x the measured spill-write time; the first model,
                                      2 x DAB serialization, was refuted by the live run: DAB serializes from a DataFrame)
  clone_s  per-clone time for S2c (default: measured alias cost; `run` takes it from clone_costs.json, the
           calibration that measured clones, or from <v2>/live/costs.json when that file has it)
  hold     attempt | batch           (when S3 drops an attempt's references: at attempt end — sequential retries, whose
                                      files are deleted at attempt end — or at batch end — attempts whose outputs stay
                                      readable until the batch ends; use peak_foot_seq with attempt, peak_foot with batch)

Usage:
  sim_ablate.py regress <v2> <n_cells>
  sim_ablate.py run <v2> <out.json> <variant-name> [n_res] [workers] [gate] [clone_costs.json]
      gate: E (E-eval subset), B (B-eval subset), F (oracle: replay equivalent to the recorded observation),
            Es (every call of the static E rule whose replay succeeded)
"""
import bisect
import json
import math
import re
import sys
from collections import OrderedDict, defaultdict
from multiprocessing import Pool

import numpy as np
import pandas as pd

import analyze2 as A

SYSTEMS = ("S0", "S1", "SD", "S2", "S2c", "S3")
COMP = ("exec", "ser", "spill", "copy", "lookup", "alias", "regen")


class Store:
    """Byte-budgeted store; refcount > 0 entries are never evicted. Victim order by policy."""

    def __init__(self, budget, policy="lru", next_use=None):
        self.budget, self.used, self.d, self.policy, self.next_use = budget, 0.0, OrderedDict(), policy, next_use

    def get(self, k):
        if k in self.d:
            if self.policy == "lru":
                self.d.move_to_end(k)
            return True
        return False

    def put(self, k, charge, on_evict=None):
        if charge > self.budget:
            return False
        if self.policy != "none" and self.used + charge > self.budget:
            victims = list(self.d)
            if self.policy == "belady":
                victims.sort(key=lambda x: -self.next_use(x))
            for ek in victims:
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
        return True


def simulate_cell_x(by_run, order, budget, costs, opts=None):
    opts = opts or {}
    policy, classes, regen = opts.get("policy", "lru"), opts.get("classes", "both"), opts.get("regen", False)
    hold = opts.get("hold", "attempt")
    lk, al, cp = costs["lookup_s"], costs["alias_s"], costs["copy_s_per_byte"]
    cl = opts.get("clone_s", al)
    # clairvoyant next use: global positions of each key in this order
    pos_of, p = defaultdict(list), 0
    for r in order:
        for c in by_run[r]:
            pos_of[c[0]].append(p)
            p += 1
    now = [0]

    def next_use(k):
        ps = pos_of.get(k, [])
        i = bisect.bisect_right(ps, now[0])
        return ps[i] if i < len(ps) else math.inf

    m = {f"{s}_{x}": 0.0 for s in SYSTEMS for x in A.SUMS}
    m.update({f"{s}_t_{c}": 0.0 for s in SYSTEMS for c in COMP})
    lat = {s: [] for s in SYSTEMS}
    priv = {s: 0.0 for s in SYSTEMS}
    cur = {s: 0.0 for s in SYSTEMS}
    peak = {s: 0.0 for s in SYSTEMS}
    peak_seq = {s: 0.0 for s in SYSTEMS}
    files = {"S2": {}, "S3": {}}
    stores = {s: Store(budget, policy, next_use) for s in ("SD", "S2", "S2c", "S3")}
    # S2c physical accounting: live private clones per extent (key, generation), the extent of each cached object
    ext_all, ext_cur, ext_size = defaultdict(int), defaultdict(int), {}
    c_store, gen = {}, [0]

    def phys_c(seq):
        e = ext_cur if seq else ext_all
        live = {x for x, n in e.items() if n > 0} | {x for x in c_store.values() if x is not None}
        return sum(ext_size[x] for x in live)

    def foot(s):
        if s == "S2c":
            peak[s] = max(peak[s], priv[s] + phys_c(False))
            peak_seq[s] = max(peak_seq[s], cur[s] + phys_c(True))
            return
        st = sum(files[s].values()) if s in files else 0.0
        peak[s] = max(peak[s], priv[s] + st)
        peak_seq[s] = max(peak_seq[s], cur[s] + st)

    def add_priv(s, b):
        priv[s] += b
        cur[s] += b

    def ev(s):
        if s == "S2c":
            return lambda k: c_store.pop(k, None)
        if s == "SD":
            return None
        return lambda k: files[s].pop(k, None)

    for r in order:
        for s in SYSTEMS:
            cur[s] = 0.0
        ext_cur.clear()
        s1 = Store(budget, policy, next_use)
        local2, localc, local3, held3 = set(), set(), set(), []
        for k, ex, ser, sp, spb, b, spilled, charge, eng in by_run[r]:
            full = ex + ser + (sp if spilled else 0.0)
            wb = spb if spilled else 0.0
            rg = 2 * sp if (regen and spilled) else 0.0
            cacheable = classes == "both" or (classes == "spilled") == bool(spilled)

            def acc(s, comps, written=0.0, hit=False, execd=False):
                t = sum(comps.values())
                m[f"{s}_time"] += t; m[f"{s}_calls"] += 1; m[f"{s}_written"] += written
                for c_, v in comps.items():
                    m[f"{s}_t_{c_}"] += v
                if hit:
                    m[f"{s}_hits"] += 1
                if execd:
                    m[f"{s}_exec"] += ex; m[f"{s}_payload"] += b
                m[f"{s}_time_{eng}"] = m.get(f"{s}_time_{eng}", 0.0) + t
                m[f"{s}_written_{eng}"] = m.get(f"{s}_written_{eng}", 0.0) + written
                lat[s].append(t)

            miss = {"exec": ex, "ser": ser, "spill": sp if spilled else 0.0}
            acc("S0", miss, wb, execd=True); add_priv("S0", wb); foot("S0")
            if not cacheable:
                for s in SYSTEMS[1:]:
                    acc(s, miss, wb, execd=True); add_priv(s, wb); foot(s)
                now[0] += 1
                continue
            # S1
            if s1.get(k):
                acc("S1", {"lookup": lk, "alias": al, "regen": rg}, hit=True)
            else:
                acc("S1", miss, wb, execd=True); add_priv("S1", wb); foot("S1")
                if not s1.put(k, charge):
                    m["S1_bypass"] += 1
            # SD: engine-level result cache (saves execution only)
            sd = stores["SD"]
            if sd.get(k):
                acc("SD", {"lookup": lk, "ser": ser, "spill": sp if spilled else 0.0}, wb, hit=True)
            else:
                acc("SD", miss, wb, execd=True)
                if not sd.put(k, charge):
                    m["SD_bypass"] += 1
            add_priv("SD", wb); foot("SD")
            # S2
            s2 = stores["S2"]
            if k in local2:
                acc("S2", {"lookup": lk, "alias": al, "regen": rg}, hit=True)
            elif s2.get(k):
                acc("S2", {"lookup": lk, "copy": wb * cp, "regen": rg}, wb, hit=True); add_priv("S2", wb); local2.add(k); foot("S2")
            else:
                comps, w = dict(miss), wb
                add_priv("S2", wb); local2.add(k)
                if s2.put(k, charge, on_evict=ev("S2")):
                    files["S2"][k] = wb; comps["copy"] = wb * cp; w += wb
                else:
                    m["S2_bypass"] += 1
                acc("S2", comps, w, execd=True); foot("S2")
            # S2c: S2 with copy-on-write clones
            s2c = stores["S2c"]
            if k in localc:
                acc("S2c", {"lookup": lk, "alias": al, "regen": rg}, hit=True)
            elif s2c.get(k):
                acc("S2c", {"lookup": lk, "copy": cl if spilled else 0.0, "regen": rg}, 0.0, hit=True); localc.add(k)
                if spilled:
                    x = c_store[k]; ext_all[x] += 1; ext_cur[x] += 1
                foot("S2c")
            else:
                comps = dict(miss)
                localc.add(k)
                x = None
                if spilled:
                    gen[0] += 1; x = (k, gen[0]); ext_size[x] = wb; ext_all[x] += 1; ext_cur[x] += 1
                if s2c.put(k, charge, on_evict=ev("S2c")):
                    c_store[k] = x
                    if spilled:
                        comps["copy"] = cl
                else:
                    m["S2c_bypass"] += 1
                acc("S2c", comps, wb, execd=True); foot("S2c")
            # S3
            s3 = stores["S3"]
            if s3.get(k):
                acc("S3", {"lookup": lk, "alias": al, "regen": rg}, hit=True)
                if k not in local3:
                    s3.d[k][1] += 1; held3.append(k); local3.add(k)
            else:
                if s3.put(k, charge, on_evict=ev("S3")):
                    files["S3"][k] = wb; s3.d[k][1] += 1; held3.append(k); local3.add(k)
                else:
                    m["S3_bypass"] += 1; add_priv("S3", wb)
                acc("S3", miss, wb, execd=True); foot("S3")
            now[0] += 1
        if hold == "attempt":
            for k in held3:
                if k in stores["S3"].d:
                    stores["S3"].d[k][1] -= 1
    for s in SYSTEMS:
        m[f"{s}_peak_foot"] = peak[s]
        m[f"{s}_peak_foot_seq"] = peak_seq[s]
        m[f"{s}_p50"] = float(np.percentile(lat[s], 50)) if lat[s] else np.nan
        m[f"{s}_p95"] = float(np.percentile(lat[s], 95)) if lat[s] else np.nan
    return m


# ----------------------------------------------------------------------------- alternative reuse keys (A14 / X2)

_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


def key_ws(text, engine):
    if not isinstance(text, str):          # call without parsable query text: key unchanged
        return text
    if engine == "mongo":
        try:
            return json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
        except Exception:
            return text
    return " ".join(_COMMENT.sub(" ", text).split())


def key_ast(text, engine):
    if not isinstance(text, str):
        return text
    if engine == "mongo":
        return key_ws(text, engine)
    import sqlglot
    dialect = {"sqlite": "sqlite", "duckdb": "duckdb", "postgres": "postgres"}.get(engine)
    try:
        return sqlglot.parse_one(text, read=dialect).sql(dialect=dialect)
    except Exception:
        return key_ws(text, engine)


def rekey(q, mode):
    """Replace q.key by an alternative equivalence key (prefixed by dataset and database)."""
    q = q.copy()
    pre = q.dataset + "\x00" + q.db_name.fillna("") + "\x00"
    if mode == "text":
        return q
    if mode in ("ws", "ast"):
        f = key_ws if mode == "ws" else key_ast
        uniq = q[["text", "engine"]].drop_duplicates()
        mp = {(t, e): f(t, e) for t, e in zip(uniq.text, uniq.engine)}
        q["key"] = pre + [mp[(t, e)] for t, e in zip(q.text, q.engine)]
        return q
    if mode in ("result", "result_nontrivial"):
        h0 = q.payload_hashes.map(lambda x: x[0] if isinstance(x, (list, np.ndarray)) and len(x) else None)
        trivial = q.payload_head.isin(["[]", "{}", "null", '""']) | q.rows.fillna(0).le(1)
        use = q.ok & h0.notna() & ((mode == "result") | ~trivial)
        q["key"] = np.where(use, pre + "res:" + h0.fillna(""), q.key)
        return q
    raise ValueError(mode)


# ----------------------------------------------------------------------------- driver

def _task(args):
    """Attempt orders depend only on (task, model, N, resample), so every variant and budget sees the same orders."""
    (ds, qy), cells, base, n_res, costs, seed, opts, Ns, budgets = args
    rows = []
    for mi, (mo, (by_run, runs_all)) in enumerate(sorted(cells.items())):
        for N in Ns:
            if N > len(runs_all):
                continue
            for i in range(n_res):
                order = np.random.default_rng([seed, mi, N, i]).permutation(runs_all)[:N]
                for bname, frac in budgets.items():
                    budget = math.inf if frac is None else frac * base
                    rows.append({"dataset": ds, "query": qy, "model": mo, "N": N, "budget": bname, "res": i,
                                 "admitted_calls": sum(len(by_run[r]) for r in order),
                                 **simulate_cell_x(by_run, order, budget, costs, opts)})
    return rows


def cells_for(q, runs, mask):
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
                by_run[r] = list(zip(rr.key, rr.t_exec, rr.ser_med.fillna(0), rr.spill_med.fillna(0), rr.spill_b,
                                     rr.bytes, (rr.spilled.fillna(False).astype(bool) & rr.ok), rr.charge, rr.engine))
            cells[mo] = (by_run, np.array(sorted(by_run)))
        out.append(((ds, qy), cells, max(base.get((ds, qy), 0.0), 1.0)))
    return out


def summarize(sim):
    sums = [c for c in sim.columns if any(c.startswith(f"{s}_") for s in SYSTEMS)
            and not c.endswith(("_p50", "_p95", "_peak_foot", "_peak_foot_seq"))]
    agg = {c: "sum" for c in sums}
    agg.update({c: "mean" for c in sim.columns if c.endswith(("_p50", "_p95", "_peak_foot", "_peak_foot_seq"))})
    agg["admitted_calls"] = "sum"
    pt = sim.fillna({c: 0.0 for c in sums}).groupby(["dataset", "query", "N", "budget"]).agg(agg).reset_index()
    pt = pt[pt.admitted_calls > 0]
    out = {}
    for (N, b), g in pt.groupby(["N", "budget"]):
        r = {"tasks": int(len(g))}
        for s in SYSTEMS[1:]:
            for x in ("exec", "time", "written"):
                v = np.where(g[f"S0_{x}"] > 0, 1 - g[f"{s}_{x}"] / g[f"S0_{x}"].replace(0, np.nan), np.nan)
                r[f"{s}_{x}_saving"] = A.boot_stats(v)
            r[f"{s}_hit_rate"] = float((g[f"{s}_hits"] / g[f"{s}_calls"].replace(0, np.nan)).median())
            r[f"{s}_bypass_total"] = float(g[f"{s}_bypass"].sum())
            for lt in ("peak_foot", "peak_foot_seq"):
                rr = (g[f"{s}_{lt}"] / g[f"S0_{lt}"].replace(0, np.nan)).dropna()
                r[f"{s}_over_S0_{lt}"] = float(rr.median()) if len(rr) else None
            r[f"{s}_cost_stack_s"] = {c: float(g[f"{s}_t_{c}"].sum()) for c in COMP}
        r["S0_cost_stack_s"] = {c: float(g[f"S0_t_{c}"].sum()) for c in COMP}
        for a, bb in (("S3", "S2"), ("S3", "S2c"), ("S2c", "S2"), ("SD", "S2")):
            for x in ("time", "written", "peak_foot", "peak_foot_seq"):
                rr = (g[f"{a}_{x}"] / g[f"{bb}_{x}"].replace(0, np.nan)).dropna()
                r[f"{a}_over_{bb}_{x}"] = A.boot_stats(rr.values) if len(rr) else {"n": 0}
        out[f"N={N}|B={b}"] = r
    return pt, out


VARIANTS = {
    "base":          ({}, (2, 4, 8), {"inf": None, "1": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125, "1/16": 1 / 16}),
    "batchhold":     ({"hold": "batch"}, (2, 4, 8), {"inf": None, "1": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125, "1/16": 1 / 16}),
    "fifo":          ({"policy": "fifo"}, (8,), {"1": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125, "1/16": 1 / 16}),
    "noevict":       ({"policy": "none"}, (8,), {"1": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125, "1/16": 1 / 16}),
    "belady":        ({"policy": "belady"}, (8,), {"1": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125, "1/16": 1 / 16}),
    "inline_only":   ({"classes": "inline"}, (2, 4, 8), {"inf": None, "1/4": 0.25}),
    "spilled_only":  ({"classes": "spilled"}, (2, 4, 8), {"inf": None, "1/4": 0.25}),
    "regen_preview": ({"regen": True}, (2, 4, 8), {"inf": None, "1/4": 0.25}),
    "key_ws":        ({"key": "ws"}, (2, 4, 8), {"inf": None}),
    "key_ast":       ({"key": "ast"}, (2, 4, 8), {"inf": None}),
    "key_result":    ({"key": "result"}, (2, 4, 8), {"inf": None}),
    "key_result_nontrivial": ({"key": "result_nontrivial"}, (2, 4, 8), {"inf": None}),
}


def load_costs(v2):
    costs = dict(A.COSTS)
    costs.update({k: v for k, v in json.load(open(f"{v2}/live/costs.json")).items() if k in A.COSTS and v is not None})
    return costs


def cmd_run(v2, out, variant, n_res=50, workers=16, gate="E", clone_costs=None):
    n_res, workers = int(n_res), int(workers)
    opts, Ns, budgets = VARIANTS[variant]
    opts = dict(opts)
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    mask = {"E": gates["E_certified"], "B": gates["B_certified"], "F": gates["fidelity_equiv"],
            # Es: what the static E rule admits online, with a successful replay and no post-hoc fidelity gate
            "Es": gates["successful"] & q.certified_E.fillna(False).astype(bool)}[gate]
    if "key" in opts:
        q = rekey(q, opts.pop("key"))
    costs = load_costs(v2)
    clone_s = json.load(open(clone_costs or f"{v2}/live/costs.json")).get("clone_s")
    if "clone_s" not in opts and clone_s:
        opts["clone_s"] = clone_s
    tasks = [(t, cells, base, n_res, costs, 101 + i, opts, Ns, budgets) for i, (t, cells, base) in enumerate(cells_for(q, runs, mask))]
    with Pool(workers) as pool:
        rows = [r for part in pool.imap_unordered(_task, tasks) for r in part]
    sim = pd.DataFrame(rows)
    pt, summ = summarize(sim)
    pt.to_csv(out.replace(".json", "_per_task.csv"), index=False)
    json.dump({"variant": variant, "gate": gate, "opts": opts, "n_res": n_res, "costs": costs, "summary": summ},
              open(out, "w"), indent=1, default=float)
    for k, r in summ.items():
        print(variant, gate, k, {x: round(r[x]["median"], 3) for x in ("S2_exec_saving", "S3_time_saving", "S2c_time_saving", "SD_time_saving")
                                 if isinstance(r.get(x), dict) and "median" in r[x]},
              {x: round(r[x]["median"], 3) for x in ("S3_over_S2_written", "S3_over_S2c_peak_foot", "S3_over_S2_peak_foot_seq")
               if isinstance(r.get(x), dict) and "median" in r[x]}, flush=True)


def cmd_regress(v2, n_cells=300):
    """Default options must reproduce analyze2.simulate_cell exactly for S0-S3 on random cells and budgets."""
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    costs = load_costs(v2)
    rng = np.random.default_rng(0)
    tasks = cells_for(q, runs, gates["E_certified"])
    checked = bad = 0
    for _ in range(int(n_cells)):
        (t, cells, base) = tasks[rng.integers(len(tasks))]
        mo = list(cells)[rng.integers(len(cells))]
        by_run, runs_all = cells[mo]
        N = int(rng.choice([2, 4, 8]))
        order = rng.permutation(runs_all)[:N]
        budget = [math.inf, base, base / 4, base / 16][rng.integers(4)]
        a = A.simulate_cell(by_run, order, budget, costs)
        b = simulate_cell_x(by_run, order, budget, costs)
        for kk, v in a.items():
            checked += 1
            if not (v == b.get(kk) or (isinstance(v, float) and math.isnan(v) and math.isnan(b.get(kk, 0.0)))
                    or abs(v - b.get(kk, math.inf)) <= 1e-9 * max(1.0, abs(v))):
                bad += 1
                if bad <= 10:
                    print("MISMATCH", t, mo, N, budget, kk, v, b.get(kk))
    print(f"regress: {checked} values compared, {bad} mismatches")
    assert bad == 0


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    {"run": cmd_run, "regress": cmd_regress}[cmd](*args)
