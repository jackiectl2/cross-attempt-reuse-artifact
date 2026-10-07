"""Peak footprint of S1-S3 relative to no sharing (S0) and S3 relative to S2, median over tasks with spilled bytes,
under both lifetimes (peak_foot: private files kept until the batch ends; peak_foot_seq: deleted at attempt end).
Usage: footprint_summary.py <analysis_dir> <out.json>"""
import json
import sys

import pandas as pd


def main(adir, out):
    res = {}
    for name, f in (("E", "B3_per_task_E.csv"), ("B", "B3_per_task.csv")):
        pt = pd.read_csv(f"{adir}/{f}")
        pt = pt[pt.S0_peak_foot > 0]
        res[name] = {}
        for (N, b), g in pt.groupby(["N", "budget"]):
            res[name][f"N={N}|B={b}"] = {
                "tasks": int(len(g)),
                **{f"{s}_over_S0_{lt}": float((g[f"{s}_{lt}"] / g[f"S0_{lt}"]).median())
                   for s in ("S1", "S2", "S3") for lt in ("peak_foot", "peak_foot_seq")},
                **{f"S3_over_S2_{lt}": float((g[f"S3_{lt}"] / g[f"S2_{lt}"]).median()) for lt in ("peak_foot", "peak_foot_seq")}}
    json.dump(res, open(out, "w"), indent=1)
    for k in ("N=2|B=inf", "N=4|B=inf", "N=8|B=inf", "N=8|B=1/4", "N=8|B=1/16"):
        print("E", k, {m: round(v, 3) for m, v in res["E"][k].items()})


if __name__ == "__main__":
    main(*sys.argv[1:])
