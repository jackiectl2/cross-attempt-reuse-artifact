"""Pilot for K6: why do data agents move query results into Python?

For each execute_python call, find which earlier query_db results it reads
(via the harness's `var_<tool_call_id>` keys, recovered from result text) and
classify the code with simple AST/regex heuristics. Signal only; not a rewriter.
"""
import ast
import re
import sys
import warnings

import duckdb
import pandas as pd

KEY_RE = re.compile(r"var_[A-Za-z0-9_.:\-]+")
RELATIONAL = {
    "filter": re.compile(r"\bif\b.*\bfor\b|\bfor\b.*\bif\b|\.query\(|\[\s*\w+\s*\[.*\]\s*(==|!=|<|>|<=|>=|\.isin)|\.isin\(|\.dropna\(|\.loc\["),
    "aggregate": re.compile(r"\.groupby\(|\bsum\(|\.sum\(|\.mean\(|\bmean\(|\.count\(|\blen\(|\bmax\(|\bmin\(|Counter\(|\.value_counts\(|\.nunique\("),
    "join": re.compile(r"\.merge\(|\.join\(|\bset\(.*\)\s*&|\bin\s+\w+_set\b|\{[^{}]*for[^{}]*\}"),
    "sort_limit": re.compile(r"sorted\(|\.sort_values\(|\.nlargest\(|\.head\("),
    "projection": re.compile(r"\[\s*['\"]\w+['\"]\s*\]\s*for\b|\[\s*\[\s*['\"]"),
}
TEXT = re.compile(r"\bre\.(search|findall|match|sub|compile)\(|\.str\.(extract|contains|replace)\(|\.split\(|json\.loads\(|datetime|strptime")


def classify(code, n_src_dbs):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ast.parse(code)
        parse_ok = True
    except SyntaxError:
        parse_ok = False
    rel = {k for k, r in RELATIONAL.items() if r.search(code)}
    text = bool(TEXT.search(code))
    if n_src_dbs >= 2:
        cause = "cross_db"
    elif rel and not text:
        cause = "relational_only"
    elif rel and text:
        cause = "relational_plus_text"
    elif text:
        cause = "text_only"
    else:
        cause = "other"
    return parse_ok, cause, ",".join(sorted(rel))


def main(calls_path):
    df = pd.read_parquet(calls_path)
    df = df.sort_values(["model", "dataset", "query", "run", "step"])
    rows = []
    for (model, dataset, query, run), g in df.groupby(["model", "dataset", "query", "run"], sort=False):
        key2call = {}
        for r in g.itertuples():
            if r.tool == "query_db":
                for k in set(KEY_RE.findall(r.result_head if isinstance(r.result_head, str) else "")):
                    key2call[k.rstrip(".:")] = r
            elif r.tool == "execute_python" and isinstance(r.text, str):
                refs = [key2call[k] for k in key2call if k in r.text]
                if not refs:
                    continue
                parse_ok, cause, ops = classify(r.text, len({x.db_name for x in refs}))
                rows.append({"model": model, "dataset": dataset, "query": query, "run": run, "step": r.step,
                             "n_refs": len(refs), "any_spilled": any(x.spilled for x in refs),
                             "in_chars": sum(x.result_chars for x in refs), "out_chars": r.result_chars,
                             "parse_ok": parse_ok, "cause": cause, "ops": ops})
    h = pd.DataFrame(rows)
    con = duckdb.connect()
    con.register("h", h)
    n_py = int((df.tool == "execute_python").sum())
    print(f"execute_python calls: {n_py}; reading >=1 query_db result: {len(h)} ({100*len(h)/n_py:.1f}%)")
    print(con.execute("""SELECT cause, count(*) n, round(100.0*count(*)/sum(count(*)) over (),1) pct,
        round(100*avg(any_spilled::int),1) pct_spilled_input,
        round(median(in_chars)) med_in_chars, round(median(out_chars)) med_out_chars
        FROM h GROUP BY 1 ORDER BY 2 DESC""").df().to_string(index=False))
    print(con.execute("""SELECT model, count(*) n,
        round(100*avg((cause='relational_only')::int),1) pct_rel_only,
        round(100*avg((cause IN ('relational_only','relational_plus_text'))::int),1) pct_rel_any,
        round(100*avg((cause='cross_db')::int),1) pct_cross_db,
        round(100*avg(any_spilled::int),1) pct_spilled
        FROM h GROUP BY 1 ORDER BY 1""").df().to_string(index=False))
    print(con.execute("""SELECT ops, count(*) n FROM h WHERE cause='relational_only' GROUP BY 1 ORDER BY 2 DESC LIMIT 12""").df().to_string(index=False))
    h.to_parquet(calls_path.replace("dab_calls.parquet", "pilot_k6_handoffs.parquet"))


if __name__ == "__main__":
    main(sys.argv[1])
