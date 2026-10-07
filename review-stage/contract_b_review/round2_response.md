I found four genuine byte-instability counterexamples. Three are name-resolution failures; one is a grouped-`DISTINCT` ordering failure.

I could not execute `certify()` itself because this workspace’s Python lacks `sqlglot`. I did execute the SQLite cases directly with the system SQLite CLI. The admission results below follow the SQLGlot 30 AST structure and the checks in [certify.py](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview2/certify.py:268). No files were modified.

## 1. SQLite: explicitly attached `public` view bypasses both the base-table and volatility checks

Query:

```sql
SELECT a FROM public.t ORDER BY a
```

Schema:

```python
{"t": {"a": True}}
```

Example setup:

```sql
CREATE TABLE main.t(a INTEGER);
INSERT INTO main.t VALUES (1);

ATTACH ':memory:' AS public;
CREATE VIEW public.t AS SELECT random() AS a;
```

Expected certificate result:

```python
(True, "total_order")
```

Why every check passes:

- The AST contains one `Select`, one `Table(db=public, this=t)`, one projected `Column(a)`, and `ORDER BY a`.
- `public` is explicitly accepted for every engine at lines 298–299.
- The schema lookup ignores `item.db` and looks up only basename `t`, finding the audited `main.t`.
- `a` consequently binds as P2-safe.
- The projection is a stored column according to the false binding, and `ORDER BY a` covers it.
- The volatile `random()` is inside the unresolved view, not the submitted AST.

SQLite produced different values on two executions of this exact query:

```text
4625253863058751921
-2855949641855071787
```

The snapshot and session were unchanged. SQLite permits attached schemas to be referenced as `schema.table`; `public` has no special main-database meaning in SQLite. [SQLite ATTACH documentation](https://www3.sqlite.org/lang_attach.html)

Smallest rejecting rule: for SQLite, reject any explicit database qualifier other than `main`. More generally, the resolved object must be verified as the audited base table, but rejecting `public` alone closes this exact hole.

## 2. SQLite: a TEMP view shadows an audited main table

Query:

```sql
SELECT a FROM t ORDER BY a
```

Schema:

```python
{"t": {"a": True}}
```

Example setup:

```sql
CREATE TABLE main.t(a INTEGER);
INSERT INTO main.t VALUES (1);

CREATE TEMP VIEW t AS SELECT random() AS a;
```

Expected certificate result:

```python
(True, "total_order")
```

Trace:

- The unqualified AST table name `t` matches the schema entry.
- The checker never asks SQLite which object `t` resolves to or whether it is a table.
- `a` binds to the audited schema entry and is considered P2-safe.
- The direct-column projection and `ORDER BY a` pass lines 428–438.
- `p2_audit.py` scans only `sqlite_master` entries whose type is `table`; it neither sees TEMP objects nor audits views ([p2_audit.py](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview2/p2_audit.py:42)).

SQLite’s documented resolution order searches `temp` before `main`, so the query runs the view. [SQLite name-resolution documentation](https://www.sqlite.org/lang_naming.html)

Two executions in the same connection returned different random integers. `main.t` remained unchanged.

Smallest rejecting rule: require SQLite tables to be explicitly qualified with `main`, or reject an unqualified name whenever a same-named TEMP object exists.

## 3. SQLite: `TRUE` is parsed as a Boolean but binds as a column

Query:

```sql
SELECT MIN(a) + TRUE FROM t
```

Schema:

```python
{"t": {"a": True, "true": True}}
```

Dataset:

```sql
CREATE TABLE t(a, true);
INSERT INTO t VALUES (1, 10), (1, 20);
```

Expected certificate result:

```python
(True, "one_row")
```

Trace:

- SQLGlot parses `TRUE` as `exp.Boolean`, without schema-aware binding.
- `has_agg` is true because of `MIN(a)`.
- `MIN(a)` passes `agg_ok`: `a` binds and is P2-safe.
- `exp.Add`, `exp.Min`, and `exp.Boolean` are all accepted by `over_aggs`.
- Since the checker sees no column outside the aggregate, lines 417–423 certify the expression as one-row.

SQLite semantics differ: `TRUE` and `FALSE` are Boolean literals only when those names do not already denote database objects. With column `true` in scope, `TRUE` refers to that column. [SQLite Boolean-expression documentation](https://www.sqlite.org/lang_expr.html)

The real query is therefore an aggregate containing a bare column:

```sql
SELECT MIN(a) + t.true FROM t
```

Both rows tie for `MIN(a)`. SQLite permits the bare value to come from any tied minimum row. [SQLite bare-column rules](https://www2.sqlite.org/matrix/lang_select.html)

Thus two correct plans may return:

```text
11
```

or:

```text
21
```

The local SQLite run returned 11 with one physical order and 21 with the rows reversed, confirming the binding and selection mechanism.

Smallest rejecting rule: on SQLite, reject an `exp.Boolean` token if any table in scope has an unquoted, case-insensitive column named `true` or `false`.

## 4. SQLite: grouped `DISTINCT` is “ordered” by a value eliminated by DISTINCT

Query:

```sql
SELECT DISTINCT COUNT(*)
FROM t
GROUP BY a
ORDER BY a
```

Schema:

```python
{"t": {"a": True}}
```

Dataset:

```sql
CREATE TABLE t(a INTEGER);
INSERT INTO t VALUES (1), (2), (2), (3);
```

The grouped intermediate rows are:

```text
a=1, count=1
a=2, count=2
a=3, count=1
```

Expected certificate result:

```python
(True, "total_order")
```

Trace:

- Plain `DISTINCT` has neither `on` nor expressions, so lines 382–384 accept it.
- `a` is a P2-safe grouping key.
- `COUNT(*)` passes `agg_ok` and is entered in `agg_proj`.
- `ORDER BY a` binds to the grouping key, so `order_cover` records `a` in `named` but covers no projected output.
- Line 473 nevertheless returns true because `named >= keys`.

After `DISTINCT`, there is only one output row for count 1, but it has two possible hidden `a` values: 1 or 3. If `a=1` is retained, ordering gives:

```text
1
2
```

If `a=3` is retained, it gives:

```text
2
1
```

SQLite allows arbitrary expressions in `ORDER BY` for a simple SELECT, while `DISTINCT` removes duplicate result rows. Nothing defines which eliminated group supplies the non-output ordering value. [SQLite SELECT documentation](https://www.sqlite.org/lang_select.html)

Smallest rejecting rule: when `DISTINCT` and `GROUP BY` are both present, do not accept the `cov[1] >= keys` alternative; require `cov[0] == set(range(len(proj)))`, i.e. order by every surviving output.

## Admitted evaluation-order hazard, but not two successful byte strings

PostgreSQL:

```sql
SELECT COUNT(*)
FROM t
WHERE a <> 0 AND 10 / a > 1
```

Schema:

```python
{"t": {"a": True}}
```

Dataset:

```text
a = 0
a = 1
```

This is admitted as `(True, "one_row")`: all predicate nodes are whitelisted, `a` binds, and the projection is `COUNT(*)`.

PostgreSQL explicitly says Boolean subexpressions may be reordered and gives division guarded by an `AND` predicate as an unsafe pattern. One allowed evaluation avoids `a=0` and returns 1; another can raise division-by-zero. [PostgreSQL 16 expression-evaluation rules](https://www.postgresql.org/docs/16/sql-expressions.html)

I am not counting this among the four byte counterexamples because one execution errors instead of producing a second serialized result. It still disproves any stronger claim that certification guarantees successful, plan-independent evaluation.

## Byte-stable queries rejected by implementation slips

With `S = {"t": {"a": True}}`:

- SQLite DQS literal:

  ```sql
  SELECT COUNT("missing") FROM t
  ```

  If no column `missing` exists, SQLite treats `"missing"` as a string literal, so this is exactly the row count. SQLGlot presents it as a quoted column; `bind` fails and the checker returns `one_row_output_not_allowed`.

- Harmless ordering of one aggregate row:

  ```sql
  SELECT COUNT(*) FROM t ORDER BY 1
  ```

  Rejected as `one_row_clause_not_allowed` at line 419.

- Redundant plain DISTINCT on one aggregate row:

  ```sql
  SELECT DISTINCT COUNT(*) FROM t
  ```

  Also rejected as `one_row_clause_not_allowed`.

- Stable removal of the sole aggregate row:

  ```sql
  SELECT COUNT(*) FROM t OFFSET 1
  ```

  PostgreSQL returns zero rows deterministically; the literal offset passes the literal check and is then rejected by line 419.

- Schema-qualified column of an accepted table:

  ```sql
  SELECT public.t.a FROM public.t ORDER BY public.t.a
  ```

  On PostgreSQL this is stable for an audited `public.t`, but `bind` categorically rejects every column having `db` or `catalog`, yielding `output_not_a_stored_column`.

- SQL-standard FETCH:

  ```sql
  SELECT a FROM t ORDER BY a FETCH FIRST 2 ROWS ONLY
  ```

  SQLGlot 30 represents this with `exp.Fetch(count=...)`. The checker reads only `args["expression"]`, so it returns `limit_not_literal` even though the count is an integer literal and the result has a total certified ordering.

## Category verdicts

- Name binding and object identity: three breaks found—attached `public` view, TEMP shadowing, and SQLite `TRUE`/`FALSE`.
- Shapes and ordering: one break found—grouped `DISTINCT` ordered through hidden grouping keys.
- One-row shape otherwise: no further successful-byte break found.
- Ordinary ordered shape otherwise: no further break once the relation and P2 binding are genuine.
- P2 type classifications: no direct false-safe type found under the stated serializer, fixed-session, and binary-collation assumptions. The two view/schema cases bypass the audit rather than defeat its per-column comparison.
- `MIN`/`MAX`, integer-versus-real, negative zero, PostgreSQL numeric scale, `char(n)`, blobs, booleans, and NaNs: no additional break on a correctly resolved, correctly audited table.
- `Div`, `Round`, `Trim`, `Substring`, casts, `ILIKE`, `IS`, `BETWEEN`, `IN`, `CASE`, `IF`, `COALESCE`, and `_B_ANON`: no differing successful result bytes found; division/casts can create the error-versus-result hazard above.
- Joins: no admitted break found. SQLGlot 30 encodes `ASOF`, `POSITIONAL`, and `NATURAL` as `Join.method`; line 293 rejects `method`. The admitted inner/left/right/full/cross/semi/anti and comma joins determine a stable multiset once their inputs are genuine base tables.
- LIMIT/OFFSET: no admitted byte-instability found. `FETCH`, signed negative forms, and several harmless one-row uses are over-rejected instead.
- Aliases, quoted identifiers, ordinals, rowid names, regular schema-qualified tables, and non-output ORDER terms: no further admitted break found.