"""Held-out check of the static E rule on queries it was not written against: the database calls of the four leaderboard
harnesses, replayed three times in the pinned runtime (replay2.py). Per harness and in total: calls and distinct queries
whose replay succeeded, those the static E rule admits, and how many of the admitted ones returned the same bytes in all
three replays; the admitted queries that did not are listed. `new` restricts to queries that occur in no released DAB
trajectory (they come from the second replay directory). With a frozen certify.py (the copy in
results/code_snapshot/code-2026-10-02.tar.gz predates the replay of the harness queries) the Contract E verdicts of that
version are compared with the current rule.

Usage: xh_static_e.py <xharness/calls.parquet> <dab_replay_dir> <xh_replay_dir> <out.json> [frozen_certify.py]
"""
import glob
import importlib.util
import json
import sys

import pandas as pd

import analyze2 as A
from certify import certify

COLS = ["dataset", "db_name", "text", "engine", "status", "status_all", "payload_hashes", "bag_hashes"]


def main(calls_path, dab_dir, xh_dir, out, frozen=None):
    parts = []
    for d, new in ((dab_dir, False), (xh_dir, True)):
        for f in sorted(glob.glob(f"{d}/*.parquet")):
            p = pd.read_parquet(f, columns=COLS)
            p["new"] = new
            parts.append(p)
    rep = pd.concat(parts, ignore_index=True)
    assert not rep.duplicated(["dataset", "db_name", "text"]).any()
    df = pd.read_parquet(calls_path)
    df = df[df.text.notna() & df.db_name.notna()]
    keys = df[["dataset", "db_name", "text"]].drop_duplicates()
    rep = rep.merge(keys, on=["dataset", "db_name", "text"])           # the queries the harnesses issued
    rep["cls"] = rep.apply(A.outcome_class, axis=1)
    known = ~rep.engine.isin(["unknown"]) & rep.engine.notna()
    rep["E"] = [certify(t, e, strict=False)[0] if k else False for t, e, k in zip(rep.text, rep.engine, known)]
    res = {}
    if frozen:
        spec = importlib.util.spec_from_file_location("certify_frozen", frozen)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old = [mod.certify(t, e, strict=False)[0] if k else False for t, e, k in zip(rep.text, rep.engine, known)]
        res["frozen_rule"] = {"file": frozen, "queries": int(len(rep)),
                              "verdicts_that_differ_from_the_current_rule": int((pd.Series(old) != rep.E).sum())}
    df = df.merge(rep[["dataset", "db_name", "text", "engine", "status", "cls", "E", "new"]],
                  on=["dataset", "db_name", "text"], how="left")

    def block(g):
        ok = g[g.status == "ok"]
        adm = ok[ok.E.fillna(False).astype(bool)]
        key = ["dataset", "db_name", "text"]
        return {"calls": int(len(g)), "ok_calls": int(len(ok)), "ok_queries": int(len(ok.drop_duplicates(key))),
                "E_admitted_ok_calls": int(len(adm)), "E_admitted_ok_queries": int(len(adm.drop_duplicates(key))),
                "E_admitted_stable_calls": int((adm.cls == "stable").sum()),
                "E_admitted_stable_queries": int(len(adm[adm.cls == "stable"].drop_duplicates(key))),
                "E_admitted_classes_calls": {str(k): int(v) for k, v in adm.cls.value_counts().items()},
                "E_rejected_ok_classes_calls": {str(k): int(v) for k, v in ok[~ok.E.fillna(False).astype(bool)].cls.value_counts().items()}}

    res["all"] = block(df)
    res["new_queries_only"] = block(df[df.new.fillna(False).astype(bool)])
    res["by_harness"] = {h: block(g) for h, g in df.groupby("harness")}
    bad = rep[(rep.status == "ok") & rep.E & (rep.cls != "stable")]
    res["E_admitted_not_stable"] = [{"dataset": r.dataset, "engine": r.engine, "class": r.cls, "new": bool(r.new),
                                     "query": " ".join(r.text.split())[:400]} for r in bad.itertuples()]
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "by_harness"}, indent=1)[:6000])


if __name__ == "__main__":
    main(*sys.argv[1:6])
