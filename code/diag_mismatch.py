"""Diagnose value mismatches between recorded observations and replayed results.

Classifies each inline value mismatch by the first differing row/field: float formatting,
NaN/None, date/time representation, extra/missing rows, or other; prints a few examples.
Usage: diag_mismatch.py <v2_dir> [examples_per_engine]
"""
import json
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import analyze2 as A  # noqa: E402


def kind(a, b):
    if not (isinstance(a, list) and isinstance(b, list)):
        return "non_list"
    if len(a) != len(b):
        return "row_count"
    for ra, rb in zip(sorted(a, key=lambda x: json.dumps(x, sort_keys=True)), sorted(b, key=lambda x: json.dumps(x, sort_keys=True))):
        if ra == rb:
            continue
        if not (isinstance(ra, dict) and isinstance(rb, dict)):
            return "non_dict_rows"
        if set(ra) != set(rb):
            return "columns"
        for k in ra:
            x, y = ra[k], rb[k]
            if x == y:
                continue
            if isinstance(x, float) and isinstance(y, float) and abs(x - y) <= 1e-9 * max(1.0, abs(x)):
                return "float_rounding"
            if (x is None) != (y is None):
                return "null_vs_value"
            if isinstance(x, str) and isinstance(y, str) and x.replace("T", " ")[:19] == y.replace("T", " ")[:19]:
                return "datetime_format"
            if type(x) is not type(y):
                return f"type:{type(x).__name__}->{type(y).__name__}"
            return "value"
    return "same_after_sort"


def main(v2, n_ex=3):
    calls, runs, rep, q = A.load(v2)
    mm = q[(q.fidelity == "mismatch") & (q.status == "inline") & ~q.spilled.fillna(False).astype(bool)].copy()
    out = []
    for r in mm.itertuples():
        try:
            out.append(kind(json.loads(r.recorded), json.loads(r.payload_head)))
        except Exception:
            out.append("unparsable")
    mm["kind"] = out
    print(mm.groupby(["engine", "kind"]).size().to_string())
    for (eng, k), g in mm.groupby(["engine", "kind"]):
        for r in g.head(int(n_ex)).itertuples():
            print(f"\n== {eng} {k} {r.dataset} {r.db_name}\nQ: {r.text[:300]}\nREC: {r.recorded[:300]}\nNEW: {r.payload_head[:300]}")


if __name__ == "__main__":
    main(*sys.argv[1:])
