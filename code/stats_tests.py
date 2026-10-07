"""X6: paired tests over tasks for the headline comparisons, Holm-corrected, with effect sizes.

Tests (two-sided Wilcoxon signed-rank unless noted; unit = task; per-task values aggregate all models and resamples):
  B1   other-attempt share vs session share of isolated execution time and of payload bytes (one-sided: greater)
  B2   S2 vs S1 DB-execution time at N = 2, 4, 8 (unbounded)
  B3-E S3 vs S2 bytes written, peak footprint (batch / attempt lifetime), tool time at N = 2, 4, 8 (unbounded, budget 1/4)
Effect sizes: median paired difference (or ratio) and matched-pairs rank-biserial correlation.
Usage: stats_tests.py <analysis_dir> <out.json>
"""
import json
import sys

import numpy as np
import pandas as pd
from scipy.stats import wilcoxon


def rank_biserial(d):
    d = d[d != 0]
    if len(d) == 0:
        return 0.0
    r = pd.Series(np.abs(d)).rank().values
    return float((r[d > 0].sum() - r[d < 0].sum()) / r.sum())


def test(name, a, b, alternative="two-sided", ratio=False):
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = ~(np.isnan(a) | np.isnan(b))
    a, b = a[ok], b[ok]
    d = a - b
    res = {"name": name, "n": int(len(d)), "median_diff": float(np.median(d)), "rank_biserial": rank_biserial(d)}
    if ratio:
        rr = a[b > 0] / b[b > 0]
        res["median_ratio"] = float(np.median(rr)) if len(rr) else None
    if np.any(d != 0):
        st = wilcoxon(a, b, alternative=alternative, zero_method="wilcox")
        res.update({"stat": float(st.statistic), "p": float(st.pvalue), "alternative": alternative})
    else:
        res.update({"stat": None, "p": 1.0, "alternative": alternative})
    return res


def holm(results):
    ps = sorted(range(len(results)), key=lambda i: results[i]["p"])
    m, prev = len(results), 0.0
    for rank, i in enumerate(ps):
        adj = min(1.0, max(prev, (m - rank) * results[i]["p"]))
        results[i]["p_holm"] = adj
        prev = adj
    return results


def main(adir, out):
    res = []
    b1 = pd.read_csv(f"{adir}/B1_per_task.csv")
    for w in ("exec_s", "payload_bytes"):
        g = b1[b1.weight == w]
        res.append(test(f"B1 {w}: other attempts > session", g.trials, g.session, alternative="greater"))
    sim = pd.read_parquet(f"{adir}/B2_sim_fidelity.parquet")
    for N in (2, 4, 8):
        g = sim[(sim.budget == "inf") & (sim.N == N)].groupby(["dataset", "query"])[["S1_exec", "S2_exec"]].sum()
        res.append(test(f"B2 N={N}: S2 vs S1 DB execution", g.S2_exec, g.S1_exec, ratio=True))
    pt = pd.read_csv(f"{adir}/B3_per_task_E.csv")
    for N in (2, 4, 8):
        for b in ("inf", "1/4"):
            g = pt[(pt.N == N) & (pt.budget == b) & (pt.S2_written > 0)]
            if not len(g):
                continue
            for col in ("written", "peak_foot", "peak_foot_seq", "time"):
                res.append(test(f"B3-E N={N} B={b}: S3 vs S2 {col}", g[f"S3_{col}"], g[f"S2_{col}"], ratio=True))
    res = holm(res)
    json.dump(res, open(out, "w"), indent=1)
    for r in res:
        print(f"{r['name']:48s} n={r['n']:3d} p={r['p']:.2e} p_holm={r['p_holm']:.2e} rb={r['rank_biserial']:+.3f} "
              f"ratio={r.get('median_ratio')}")


if __name__ == "__main__":
    main(*sys.argv[1:])
