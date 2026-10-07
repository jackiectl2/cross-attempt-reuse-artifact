"""Numbers requested at the outline review (2026-10-02). Reads existing tables only; no query is executed.

  clustered   task-level headline statistics with a dataset-clustered bootstrap (the 54 tasks sit in 12 datasets):
              B1 task-macro other-attempt and session shares (time, bytes) and their difference; B2 median-task S2
              DB-execution saving at N = 2 / 4 / 8 and the share of tasks reaching 20%; dataset-level sign test
  magnitudes  absolute sizes behind the ratios: isolated execution time per call and per attempt, payload bytes per
              call, spilled results, and the per-task distinct admitted stored bytes (E-eval subset) that the cache budgets are fractions of

Usage: paper_stats.py <v2_dir> <analysis_dir> <out.json>
"""
import json
import sys

import numpy as np
import pandas as pd
from scipy.stats import binomtest

import analyze2 as A


def cluster_boot(df, col, stat=np.median, n=4000, seed=7):
    rng = np.random.default_rng(seed)
    groups = [g[col].dropna().values for _, g in df.groupby("dataset")]
    reps = [stat(np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])) for _ in range(n)]
    return {"value": float(stat(df[col].dropna().values)), "ci95": [float(np.percentile(reps, 2.5)), float(np.percentile(reps, 97.5))],
            "tasks": int(df[col].notna().sum()), "datasets": len(groups)}


def clustered(adir):
    out = {}
    b1 = pd.read_csv(f"{adir}/B1_per_task.csv")
    for w in ("exec_s", "payload_bytes"):
        t = b1[b1.weight == w].assign(diff=lambda d: d.trials - d.session)
        ds = t.groupby("dataset")[["trials", "session"]].median()
        out[f"B1_{w}"] = {"other_attempts": cluster_boot(t, "trials"), "session": cluster_boot(t, "session"),
                          "difference": cluster_boot(t, "diff"),
                          "datasets_other_gt_session": f"{int((ds.trials > ds.session).sum())}/{len(ds)}",
                          "sign_test_p": float(binomtest(int((ds.trials > ds.session).sum()), len(ds)).pvalue)}
    sim = pd.read_parquet(f"{adir}/B2_sim_fidelity.parquet")
    per_task, _ = A.summarize_sim(sim[sim.budget == "inf"])
    for N in (2, 4, 8):
        t = per_task[(per_task.N == N) & (per_task.admitted_calls > 0)].assign(ge20=lambda d: (d.S2_exec_saving >= 0.2).astype(float))
        out[f"B2_N{N}"] = {"S2_exec_saving": cluster_boot(t, "S2_exec_saving"),
                           "share_tasks_ge_20pct": cluster_boot(t, "ge20", stat=np.mean)}
    return out


def magnitudes(v2):
    calls, runs, rep, q = A.load(v2)
    _, gates, _ = A.funnel(q, calls)
    ok = q[q.ok]
    per_attempt = ok.groupby(["dataset", "query", "model", "run"]).t_exec.sum()   # successful replays, as per call
    sp = ok[ok.spilled.fillna(False).astype(bool)]
    g = q[gates["E_certified"]]                       # budget base as in the simulator: distinct admitted stored bytes
    g_sp = g.spilled.fillna(False).astype(bool) & g.ok
    task_base = (g.assign(store_bytes=np.where(g_sp, g.spill_b, g.bytes))
                 .drop_duplicates(["dataset", "query", "key"]).groupby(["dataset", "query"]).store_bytes.sum())
    pct = lambda s, p: float(np.percentile(s, p))
    return {"exec_s_per_call": {"median": pct(ok.t_exec, 50), "p95": pct(ok.t_exec, 95), "max": float(ok.t_exec.max())},
            "exec_s_per_attempt": {"median": pct(per_attempt, 50), "p95": pct(per_attempt, 95)},
            "payload_bytes_per_call": {"median": pct(ok.bytes, 50), "p95": pct(ok.bytes, 95), "max": float(ok.bytes.max())},
            "spilled_share_of_successful_calls": float(len(sp) / len(ok)),
            "spill_file_bytes": {"median": pct(sp.spill_b, 50), "p95": pct(sp.spill_b, 95), "max": float(sp.spill_b.max())},
            "task_budget_base_bytes_E_eval": {"tasks": int(len(task_base)), "median": pct(task_base, 50), "p95": pct(task_base, 95),
                                              "max": float(task_base.max())}}


def main(v2, adir, out):
    res = {"clustered": clustered(adir), "magnitudes": magnitudes(v2)}
    print(json.dumps(res, indent=1), flush=True)
    json.dump(res, open(out, "w"), indent=1)


if __name__ == "__main__":
    main(*sys.argv[1:])
