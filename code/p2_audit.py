"""The schema that Contract B binds names to, and its precondition P2 checked on the snapshot: in which stored columns
do values that the engine compares as equal always print the same?

Only base tables are listed (no views). A column is `safe` when no two of its values can compare equal and print
differently:
  SQLite      one scan per table counts the storage classes of every column; a column is unsafe if it holds both
              INTEGER and REAL values (1 and 1.0 compare equal) or a negative zero (found with a Python function,
              because SQLite prints both zeros alike). Declared collations are reported by schema_collations.py.
  DuckDB      by declared type: integer, boolean, string, date/time, decimal, uuid and blob columns are safe; FLOAT /
              DOUBLE columns are safe unless they hold a negative zero; other types (intervals, lists, structs, ...)
              are not analysed and count as unsafe.
  PostgreSQL  by declared type and collation: integer, boolean, string (default or C collation), date/time and uuid
              columns are safe; real / double precision unless they hold a negative zero; numeric unless two equal
              values print differently (1.0 and 1.00); other types count as unsafe.
MongoDB collections are not audited: Contract B admits only lookups by `_id`.
Every column also gets a type family (str, num, flt, time, bool, other), which Contract B uses for DuckDB's comparisons
and arithmetic, and
every database a catalog check for objects that act behind the query text: `catalog_ok` is false when PostgreSQL has a
table with row-level security, a user-defined function, a relation outside `public` or a non-default search path, or
when DuckDB has a user-defined macro.

Run inside code/run_with_db.sh (it starts PostgreSQL when the dataset has one):
  run_with_db.sh <dataset> -- code/p2_audit.py <dab_repo> <dataset> <out.json>
Then merge the per-dataset files and summarize them:
  p2_audit.py merge <dir> <out.json>
"""
import json
import math
import os
import sqlite3
import sys

import yaml

DUCK_SAFE = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
             "UHUGEINT", "BOOLEAN", "VARCHAR", "DATE", "TIME", "TIMESTAMP", "UUID", "BLOB", "DECIMAL")
DUCK_FLOAT = ("FLOAT", "DOUBLE")
PG_SAFE = ("smallint", "integer", "bigint", "boolean", "text", "character varying", "character", "date",
           "timestamp without time zone", "timestamp with time zone", "time without time zone", "uuid", "bytea")
PG_FLOAT = ("real", "double precision")


def family(typ):
    t = typ.upper()
    if t.startswith(("VARCHAR", "TEXT", "CHAR", "CHARACTER")):
        return "str"
    if t.startswith(("FLOAT", "DOUBLE", "REAL")):
        return "flt"
    if t.startswith(("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
                     "UHUGEINT", "DECIMAL", "NUMERIC", "INT")):
        return "num"
    if t.startswith(("DATE", "TIME")):
        return "time"
    return "bool" if t.startswith("BOOL") else "other"


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def audit_sqlite(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.create_function("ed_negzero", 1, lambda v: int(isinstance(v, float) and v == 0 and math.copysign(1.0, v) < 0),
                        deterministic=True)
    tables = {}
    for (table,) in con.execute("select name from sqlite_master where type = 'table' and name not like 'sqlite_%'").fetchall():
        names = [(r[1], r[2]) for r in con.execute(f"pragma table_info({qi(table)})")]
        for i in range(0, len(names), 300):                       # SQLite returns at most 2000 columns per row
            part = names[i:i + 300]
            exprs = ["count(*)"]
            for n, _ in part:
                c = qi(n)
                exprs += [f"sum(typeof({c}) = '{k}')" for k in ("integer", "real", "text", "blob")]
                exprs.append(f"sum(ed_negzero({c}))")
            try:
                row = con.execute(f"select {', '.join(exprs)} from {qi(table)}").fetchone()
            except sqlite3.Error as e:                           # e.g. a virtual table whose module is not loaded
                for n, decl in part:
                    tables.setdefault(table, {})[n] = {"type": decl or "", "family": None, "safe": False,
                                                       "why": f"scan failed: {e}"}
                continue
            for j, (n, decl) in enumerate(part):
                ni, nr, nt, nb, neg = (int(x or 0) for x in row[1 + 5 * j: 6 + 5 * j])
                why = "integer and real values" if ni and nr else "negative zero" if neg else None
                tables.setdefault(table, {})[n] = {"type": decl or "", "family": None, "rows": int(row[0]),
                                                   "safe": why is None, "why": why,
                                                   "classes": {"integer": ni, "real": nr, "text": nt, "blob": nb}}
    catalog = {"views": con.execute("select count(*) from sqlite_master where type = 'view'").fetchone()[0]}
    return tables, catalog, True                        # a view cannot share a table's name; P1 excludes TEMP and ATTACH


def audit_duckdb(path):
    import duckdb
    con = duckdb.connect(path, read_only=True, config={"threads": 1})
    tables = {}
    rows = con.execute("select c.table_schema, c.table_name, c.column_name, c.data_type from information_schema.columns c "
                       "join information_schema.tables t using (table_catalog, table_schema, table_name) "
                       "where t.table_type = 'BASE TABLE' and c.table_schema = 'main'").fetchall()
    for schema, table, name, typ in rows:
        base = typ.split("(")[0].split(" ")[0].upper()
        why = None
        if base in DUCK_FLOAT:
            neg = con.execute(f"select count(*) from {qi(schema)}.{qi(table)} where {qi(name)} = 0 and signbit({qi(name)})").fetchone()[0]
            why = "negative zero" if neg else None
        elif "[" in typ or not typ.upper().startswith(DUCK_SAFE):
            why = "type not analysed"
        tables.setdefault(table, {})[name] = {"type": typ, "family": family(typ), "safe": why is None, "why": why}
    catalog = {"user_functions": con.execute("select count(*) from duckdb_functions() where not internal").fetchone()[0],
               "views": con.execute("select count(*) from duckdb_views() where not internal").fetchone()[0]}
    return tables, catalog, catalog["user_functions"] == 0


def audit_postgres(db):
    import psycopg2
    con = psycopg2.connect(dbname=db, user=os.environ["PGUSER"], host=os.environ["PGHOST"], port=os.environ["PGPORT"])
    con.autocommit = True
    cur = con.cursor()
    cur.execute("select c.table_schema, c.table_name, c.column_name, c.data_type, c.collation_name "
                "from information_schema.columns c join information_schema.tables t using (table_catalog, table_schema, table_name) "
                "where t.table_type = 'BASE TABLE' and c.table_schema = 'public'")
    tables = {}
    for schema, table, name, typ, coll in cur.fetchall():
        t, c = f"{qi(schema)}.{qi(table)}", qi(name)
        why = None
        if coll not in (None, "C", "POSIX", "default"):
            why = f"collation {coll}"
        elif typ in PG_FLOAT:
            cur.execute(f"select count(*) from {t} where {c} = 0 and {c}::text like '-%'")
            why = "negative zero" if cur.fetchone()[0] else None
        elif typ == "numeric":
            cur.execute(f"select count(*) from (select 1 from {t} group by {c} having count(distinct {c}::text) > 1) s")
            why = "equal values with different scales" if cur.fetchone()[0] else None
        elif typ not in PG_SAFE:
            why = "type not analysed"
        tables.setdefault(table, {})[name] = {"type": typ, "family": family(typ), "safe": why is None, "why": why}
    catalog = {}
    for k, q in (("lc_collate", "select datcollate from pg_database where datname = current_database()"),
                 ("search_path", "show search_path"),
                 ("row_security_tables", "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                         "where n.nspname = 'public' and (c.relrowsecurity or c.relforcerowsecurity)"),
                 ("user_functions", "select count(*) from pg_proc p join pg_namespace n on n.oid = p.pronamespace "
                                    "where n.nspname not in ('pg_catalog', 'information_schema')"),
                 ("relations_outside_public", "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                                              "where n.nspname not in ('pg_catalog', 'information_schema', 'public') "
                                              "and n.nspname !~ '^pg_'"),
                 ("views", "select count(*) from pg_views where schemaname = 'public'")):
        cur.execute(q)
        catalog[k] = cur.fetchone()[0]
    ok = (catalog["row_security_tables"] == 0 and catalog["user_functions"] == 0 and catalog["relations_outside_public"] == 0
          and catalog["search_path"].replace(" ", "") == '"$user",public')
    return tables, catalog, ok


def main(repo, dataset, out):
    ds_dir = os.path.join(repo, f"query_{dataset}")
    res = {}
    for name, c in yaml.safe_load(open(os.path.join(ds_dir, "db_config.yaml")))["db_clients"].items():
        t = c["db_type"]
        entry = {"engine": t}
        if t == "sqlite":
            entry["tables"], entry["catalog"], entry["catalog_ok"] = audit_sqlite(os.path.join(ds_dir, c["db_path"]))
        elif t == "duckdb":
            entry["tables"], entry["catalog"], entry["catalog_ok"] = audit_duckdb(os.path.join(ds_dir, c["db_path"]))
        elif t == "postgres":
            entry["tables"], entry["catalog"], entry["catalog_ok"] = audit_postgres(c["db_name"])
        else:
            entry["tables"], entry["catalog"], entry["catalog_ok"], entry["note"] = {}, {}, False, "not audited"
        v = [x for cols in entry["tables"].values() for x in cols.values()]
        entry["n_tables"], entry["n_columns"], entry["n_unsafe"] = len(entry["tables"]), len(v), sum(1 for x in v if not x["safe"])
        res[name] = entry
        print(dataset, name, t, entry["n_tables"], "tables,", entry["n_columns"], "columns,", entry["n_unsafe"], "unsafe",
              sorted({x["why"] for x in v if not x["safe"]}), "catalog", entry["catalog"], entry["catalog_ok"], flush=True)
    with open(out, "w") as f:
        json.dump({dataset: res}, f, indent=1)


def merge(src, out):
    import collections
    import glob
    res = {}
    for f in sorted(glob.glob(os.path.join(src, "*.json"))):
        res.update(json.load(open(f)))
    summ = collections.defaultdict(lambda: {"databases": 0, "catalog_ok": 0, "tables": 0, "columns": 0, "unsafe": 0,
                                            "unsafe_by_reason": collections.Counter()})
    for dbs in res.values():
        for entry in dbs.values():
            s_ = summ[entry["engine"]]
            s_["databases"] += 1
            s_["catalog_ok"] += bool(entry["catalog_ok"])
            s_["tables"] += len(entry["tables"])
            for c in (x for cols in entry["tables"].values() for x in cols.values()):
                s_["columns"] += 1
                if not c["safe"]:
                    s_["unsafe"] += 1
                    s_["unsafe_by_reason"][c["why"].split(":")[0]] += 1
    res = {"summary": {"datasets": len(res), **{e: {**v, "unsafe_by_reason": dict(v["unsafe_by_reason"])} for e, v in summ.items()}},
           "datasets": res}
    with open(out, "w") as f:
        json.dump(res, f, indent=1)
    print(json.dumps(res["summary"], indent=1))


if __name__ == "__main__":
    if sys.argv[1] == "merge":
        merge(*sys.argv[2:4])
    else:
        main(*sys.argv[1:4])
