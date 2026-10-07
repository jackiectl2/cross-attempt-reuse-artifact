"""Collations declared in DAB's SQLite and DuckDB databases (precondition P2 of Contract B: injective comparisons).

For every SQLite / DuckDB database of the workload's datasets (keys of dataset_table.json): number of schema objects
and how many declare a COLLATE clause;
for DuckDB also the default collation. PostgreSQL databases are created with LC_COLLATE='C' by code/run_with_db.sh.

Usage: schema_collations.py <dab_repo> <dataset_table.json> <out.json>
"""
import json
import os
import sqlite3
import sys

import duckdb
import yaml


def main(repo, datasets, out):
    res = {"sqlite": {}, "duckdb": {}}
    for ds in sorted(json.load(open(datasets))):
        cfgf = os.path.join(repo, f"query_{ds}", "db_config.yaml")
        for name, c in yaml.safe_load(open(cfgf))["db_clients"].items():
            p = os.path.join(os.path.dirname(cfgf), c.get("db_path") or "")
            if c["db_type"] == "sqlite" and os.path.isfile(p):
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                sqls = [r[0] for r in con.execute("select sql from sqlite_master where sql is not null")]
                res["sqlite"][f"{ds}/{name}"] = {"objects": len(sqls), "with_collate": sum("collate" in s.lower() for s in sqls)}
            elif c["db_type"] == "duckdb" and os.path.isfile(p):
                con = duckdb.connect(p, read_only=True)
                sqls = [r[0] or "" for r in con.execute("select sql from duckdb_tables()").fetchall()]
                res["duckdb"][f"{ds}/{name}"] = {"tables": len(sqls), "with_collate": sum("collate" in s.lower() for s in sqls),
                                               "default_collation": con.execute("select current_setting('default_collation')").fetchone()[0]}
    res["summary"] = {e: {"databases": len(v), "databases_with_collate": sum(1 for x in v.values() if x["with_collate"])}
                      for e, v in res.items()}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps(res["summary"]), {x.get("default_collation") for x in res["duckdb"].values()})


if __name__ == "__main__":
    main(*sys.argv[1:4])
