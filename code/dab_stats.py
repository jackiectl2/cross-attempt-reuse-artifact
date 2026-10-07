"""First-pass workload statistics over parsed DAB tool calls (pilot)."""
import sys

import duckdb

con = duckdb.connect()
con.execute(f"CREATE VIEW c AS SELECT * FROM read_parquet('{sys.argv[1]}')")


def show(title, sql):
    print(f"\n## {title}")
    print(con.execute(sql).df().to_string(index=False))


show("tool mix", "SELECT tool, count(*) n, round(100.0*count(*)/sum(count(*)) over (),1) pct FROM c GROUP BY 1 ORDER BY 2 DESC")
show("calls per run by model", """
SELECT model, count(DISTINCT dataset||query||run) runs, round(count(*)/count(DISTINCT dataset||query||run),1) calls_per_run,
       round(100.0*avg((tool='query_db')::int),1) pct_query_db
FROM c GROUP BY 1 ORDER BY 1""")
show("query_db outcome by model", """
SELECT model, count(*) n, round(100*avg(failed::int),1) pct_failed, round(100*avg(spilled::int),1) pct_spilled,
       round(median(result_chars)) med_chars
FROM c WHERE tool='query_db' GROUP BY 1 ORDER BY 1""")
# exact duplicates: same (db_name, text) earlier in the same run / in any earlier run of same model+task / any model same task
show("exact-duplicate query_db (string identity)", """
WITH q AS (SELECT *, row_number() over () rid FROM c WHERE tool='query_db' AND text IS NOT NULL),
w AS (
  SELECT model, dataset, query, run, step, db_name, text,
    count(*) over (PARTITION BY model, dataset, query, run, db_name, text ORDER BY step ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) prev_in_run,
    count(*) over (PARTITION BY model, dataset, query, db_name, text) n_same_task_model,
    count(*) over (PARTITION BY dataset, query, db_name, text) n_same_task
  FROM q)
SELECT model, count(*) n,
  round(100*avg((prev_in_run>0)::int),1) pct_repeat_within_run,
  round(100*avg((n_same_task_model>1)::int),1) pct_text_seen_twice_same_model_task,
  round(100*avg((n_same_task>1)::int),1) pct_text_seen_twice_any_model_task
FROM w GROUP BY ROLLUP(model) ORDER BY model NULLS LAST""")
show("distinct query texts vs total (per task, all models)", """
SELECT count(*) total, count(DISTINCT dataset||query||coalesce(db_name,'')||text) distinct_texts,
       round(100.0*count(DISTINCT dataset||query||coalesce(db_name,'')||text)/count(*),1) pct_distinct
FROM c WHERE tool='query_db' AND text IS NOT NULL""")
show("query language split (crude)", """
SELECT CASE WHEN ltrim(text) LIKE '{%' THEN 'mongo-json' WHEN upper(ltrim(text)) LIKE 'SELECT%' OR upper(ltrim(text)) LIKE 'WITH%' THEN 'sql-select'
            ELSE 'other' END kind, count(*) n FROM c WHERE tool='query_db' GROUP BY 1 ORDER BY 2 DESC""")
show("dataset sizes in calls", "SELECT dataset, count(*) n, round(100*avg(spilled::int),1) pct_spilled FROM c WHERE tool='query_db' GROUP BY 1 ORDER BY 2 DESC")
