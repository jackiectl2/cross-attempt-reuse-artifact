"""Contract B / E outcomes over the distinct replayed queries and their calls, with the current certificate: counts by
reason, the admitted queries and calls by engine, and up to eight example queries for each Contract B outcome.

Usage: cert_reasons.py <replay_dir> <p2_audit.json> <out.json>
"""
import collections
import glob
import json
import sys

import pandas as pd

from certify import certify, load_schema


def main(replay_dir, audit_path, out):
    rep = pd.concat([pd.read_parquet(f, columns=["dataset", "db_name", "text", "engine", "n_calls"])
                     for f in sorted(glob.glob(f"{replay_dir}/*.parquet"))], ignore_index=True)
    schema = load_schema(audit_path)
    res = {"queries": int(len(rep)), "calls": int(rep.n_calls.sum())}
    for name, strict in (("B", True), ("E", False)):
        got = [(False, "unknown") if e in ("unknown", None) else certify(t, e, strict=strict, schema=schema.get((d, n)))
               for d, n, t, e in zip(rep.dataset, rep.db_name, rep.text, rep.engine)]
        why = pd.Series([w for _, w in got])
        ok = pd.Series([a for a, _ in got])
        res[name] = {k: int(v) for k, v in why.value_counts().items()}
        res[name + "_calls"] = {k: int(v) for k, v in rep.n_calls.groupby(why).sum().sort_values(ascending=False).items()}
        res[name + "_admitted"] = {"queries": int(ok.sum()), "calls": int(rep.n_calls[ok].sum()),
                                   "by_engine": {e: int(v) for e, v in rep.engine[ok].value_counts().items()}}
        if strict:
            ex = collections.defaultdict(list)
            for a, w, e, t in zip(ok, why, rep.engine, rep.text):
                if len(ex[w]) < 8:
                    ex[w].append({"engine": e, "query": " ".join(t.split())[:300]})
            res["examples_B"] = dict(ex)
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps({k: v for k, v in res.items() if k != "examples_B"}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:4])
