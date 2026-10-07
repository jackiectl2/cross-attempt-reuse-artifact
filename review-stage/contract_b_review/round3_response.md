The certificate is unsound. I found six admitted counterexamples: three unconditional catalog/name-resolution failures, two plan-dependent row errors, and one `LIMIT 0`/one-row error hole.

The critical code is in [`certify.py`](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview3/certify.py:290), with the audit in [`p2_audit.py`](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview3/p2_audit.py:42).

## 1. PostgreSQL row-level security can inject volatility

Query and certificate input:

```sql
SELECT a FROM public.t ORDER BY a
```

```python
engine = "postgres"
schema = {"t": {"a": True}}
```

Example database:

```sql
CREATE TABLE public.t(a integer);
INSERT INTO public.t VALUES (1), (2);

CREATE ROLE reader;
GRANT SELECT ON public.t TO reader;

ALTER TABLE public.t ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.t FORCE ROW LEVEL SECURITY;
CREATE POLICY random_rows ON public.t
  USING (random() < 0.5);
```

Execute as `reader`, without `BYPASSRLS`.

Certificate trace:

- Parses as one `exp.Select`; only `expressions`, `from_`, and `order` are populated.
- `public.t` is accepted at lines 317–324.
- `a` binds uniquely to `t.a`.
- There is no predicate visible in the SQL AST and no aggregate.
- `a` is marked P2-safe.
- `ORDER BY a` covers projection index 0.
- Lines 455–469 return `(True, "total_order")`.

Actual semantics:

The security-policy predicate is appended by PostgreSQL after parsing. Different fresh executions can return `[]`, `[1]`, `[2]`, or `[1,2]`, all correctly sorted, on the same data snapshot. PostgreSQL explicitly recognizes that functions and RLS policies can execute code not visible in the submitted query ([PostgreSQL function-security documentation](https://www.postgresql.org/docs/16/perm-functions.html)).

Smallest rejecting rule: reject PostgreSQL tables with `relrowsecurity` or `relforcerowsecurity` set.

## 2. An unqualified PostgreSQL table can resolve to a volatile view

Query:

```sql
SELECT a FROM t ORDER BY a
```

```python
engine = "postgres"
schema = {"t": {"a": True}}
```

Example database and fixed session configuration:

```sql
CREATE TABLE public.t(a integer);
INSERT INTO public.t VALUES (1);

CREATE SCHEMA shadow;
CREATE VIEW shadow.t AS
  SELECT floor(random() * 1000000)::integer AS a;

SET search_path = shadow, public;
```

This does not use a temporary object. The configured search path is fixed, as P1 requires.

Certificate trace:

- One permitted `Select`.
- The unqualified `exp.Table("t")` is looked up solely by its name in `schema` at lines 322–324.
- The code never resolves the relation OID or verifies `relkind`.
- `a` binds to the purported audited base table column.
- It is P2-safe, and `ORDER BY a` covers the only output.
- Result: `(True, "total_order")`.

Actual semantics:

PostgreSQL resolves an unqualified relation to the first matching object in `search_path`, including a view. Thus the engine executes `shadow.t`, not `public.t`, and returns a different random integer across executions. PostgreSQL documents exactly this first-match behavior ([search_path documentation](https://www.postgresql.org/docs/16/runtime-config-client.html)).

This aligns with another gap: `p2_audit.py` audits only `BASE TABLE` objects in `public` at lines 94–96 and records no relation identity.

Smallest rejecting rule: in PostgreSQL, reject unqualified table references; requiring `public.t` rejects this exact query.

## 3. `IFNULL` is normalized to `Coalesce`, but PostgreSQL executes a user function

Query:

```sql
SELECT COUNT(IFNULL(a, 0)) FROM public.t
```

```python
engine = "postgres"
schema = {"t": {"a": True}}
```

Example database:

```sql
CREATE TABLE public.t(a integer);
INSERT INTO public.t VALUES (7);

CREATE FUNCTION public.ifnull(integer, integer)
RETURNS integer
LANGUAGE sql
VOLATILE
AS 'SELECT CASE WHEN random() < 0.5 THEN $1 ELSE NULL END';
```

Certificate trace:

- sqlglot’s common parser maps `COALESCE`, `IFNULL`, and `NVL` to `exp.Coalesce`; this mapping applies while reading PostgreSQL syntax ([sqlglot parser source](https://github.com/tobymao/sqlglot/blob/main/sqlglot/parser.py)).
- `exp.Coalesce`, `Column`, and `Literal` are all in `_B_ROW`.
- `a` binds.
- `agg_ok(Count)` calls `_row_ok` on the normalized `Coalesce` and succeeds.
- There is no group, order, distinct, offset, or having.
- `over_aggs` accepts the `Count`.
- Lines 448–454 return `(True, "one_row")`.

Actual semantics:

PostgreSQL does not have special `IFNULL` syntax. It parses the original text as an ordinary function call and resolves `public.ifnull(integer,integer)`. The volatile function makes the count independently 0 or 1.

This is precisely the semantic information lost by the sqlglot normalization. PostgreSQL permits overloaded user functions and resolves them by name, search path, and argument types ([PostgreSQL `CREATE FUNCTION`](https://www.postgresql.org/docs/16/sql-createfunction.html)).

Smallest rejecting rule: for PostgreSQL, reject the original spellings `IFNULL`, `NVL`, `IF`, and `IIF`; for this exact counterexample, rejecting `IFNULL` is sufficient.

## 4. DuckDB comparison combination-casting can raise on a discarded join row

Query:

```sql
SELECT COUNT(*)
FROM t
JOIN u ON t.i = u.i AND t.s = t.i
```

```python
engine = "duckdb"
schema = {
    "t": {"i": True, "s": True},
    "u": {"i": True},
}
```

Dataset:

```text
t:
i  s
1  '1'
2  'bad'

u:
i
1
```

Certificate trace:

- One `Select`; both FROM items are plain audited tables.
- Both join terms parse as permitted `exp.EQ` nodes.
- Every column binds uniquely.
- `_row_ok` regards comparisons as non-raising: `_B_MAY_RAISE` contains arithmetic, casts, substring, `ABS`, and `ROUND`, but not comparisons.
- The whole `ON` condition passes lines 398–402.
- `COUNT(*)` passes `agg_ok`.
- One-row shape returns `(True, "one_row")`.

Actual semantics:

DuckDB uses combination casting for mixed-type comparisons. Comparing `VARCHAR` with `INTEGER` can cast the string to an integer, and converting `'bad'` raises a conversion error ([DuckDB comparisons](https://duckdb.org/docs/current/sql/expressions/comparison_operators), [casting rules](https://duckdb.org/docs/current/sql/expressions/cast)).

Two valid plans differ:

- Join `t` to `u` on `i` first, discarding `t.i=2`; then evaluate `t.s=t.i` only for `'1'`. Result: `1`.
- Push the `t`-local comparison into the `t` scan; evaluating `'bad'=2` raises a conversion error.

DuckDB explicitly performs filter pushdown and join reordering ([optimizer documentation](https://duckdb.org/2024/11/14/optimizers)).

Smallest rejecting rule: reject a DuckDB comparison, `IN`, or `BETWEEN` whose inferred common type requires a potentially failing `VARCHAR`-to-non-`VARCHAR` conversion.

The same hole can be expressed with the hinted one-element `IN` form, because DuckDB rewrites `c IN (1)` to `c = 1`.

## 5. SQLite `ABS` can raise despite `_may_raise` returning false for all SQLite nodes

Query:

```sql
SELECT COUNT(*)
FROM t
JOIN u ON t.i = u.i AND ABS(t.x) > 0
```

```python
engine = "sqlite"
schema = {
    "t": {"i": True, "x": True},
    "u": {"i": True},
}
```

Dataset:

```text
t:
i  x
1  1
2  -9223372036854775808

u:
i
1
```

Both `t.x` values have INTEGER storage class, so `p2_audit.py` marks the column safe.

Certificate trace:

- One permitted `Select`; both tables bind.
- The `ON` expression consists only of `And`, `EQ`, `GT`, `Abs`, columns, and a literal, all in `_B_ROW`.
- `_may_raise` returns `False` immediately for SQLite at lines 260–263.
- Thus the join predicate passes `row_expr`.
- `COUNT(*)` is valid.
- The query is admitted as `"one_row"`.

Actual semantics:

SQLite explicitly documents that:

```sql
abs(-9223372036854775808)
```

raises `integer overflow` ([SQLite core-function documentation](https://www.sqlite.org/lang_corefunc.html)).

A plan that joins/probes on `i` first never visits the bad row and returns `1`. A plan that pushes `ABS(t.x)>0` into the `t` scan raises before discovering that `i=2` has no match.

Smallest rejecting rule: treat `exp.Abs` as may-raise in SQLite whenever its argument is not a literal known to be safe.

A second SQLite error class also exists: a nonliteral `LIKE` pattern can exceed SQLite’s configured maximum pattern length. I did not count it separately because it is the same `_may_raise` defect.

## 6. `LIMIT 0` invalidates the “one-row expression is evaluated once” argument

Query:

```sql
SELECT 1 / COUNT(*) FROM t LIMIT 0
```

```python
engine = "postgres"
schema = {"t": {"a": True}}
```

Dataset: `t` is empty.

Certificate trace:

- `LIMIT 0` is an integer literal and passes lines 403–408.
- The projection contains `Count`, so the query enters the one-row shape.
- The one-row clause check at lines 448–451 rejects `OFFSET`, `ORDER BY`, `DISTINCT`, and `HAVING`, but not `LIMIT`.
- `over_aggs(..., once=True)` deliberately does not call `_may_raise`; therefore the `Div` is accepted.
- `COUNT(*)` is valid.
- Result: `(True, "one_row")`.

Actual semantics:

- A lazy `Limit(0)` plan does not request a tuple from the aggregate/projection and returns empty bytes.
- An eager/materializing plan evaluates the aggregate, obtains zero, and raises division-by-zero while calculating `1 / 0`.

The output expression is therefore not necessarily “evaluated exactly once”; it may be evaluated zero times. This is precisely the evaluation-skipping distinction the rest of the certificate tries to guard against.

Smallest rejecting rule: reject `LIMIT 0` on the one-row shape when any output contains `_may_raise`.

## Categories where I found no counterexample

- **P2 audit false positives:** none found for the actual value domains classified safe. SQLite catches mixed INTEGER/REAL and negative zero; PostgreSQL numeric scale and floating negative zero are checked; DuckDB’s fixed decimal and temporal values have canonical returned representations. The catalog/view/RLS cases bypass the intended audited relation but are not equal-value P2 failures.
- **Double-quoted SQLite strings:** no admission found. When SQLite’s DQS fallback would make the token a string, `bind()` cannot find the alleged stored column, so the query is rejected.
- **Bare SQLite `TRUE`/`FALSE`:** no remaining admission found; lines 348–350 conservatively reject every Boolean node whenever either name occurs in scope.
- **Keywords as column names and quoted-name folding:** no unsound admission found under the stated schema precondition.
- **`Div` typing, `TRIM ... FROM`, `CASE`/`IF`, `BETWEEN`, non-null `IS`, and `LIKE ... ESCAPE`:** no additional admission beyond the error-evaluation cases above. `ESCAPE` introduces an unwhitelisted node.
- **Aliases, alias references in `ORDER BY`/`HAVING`, ordinals, self joins, and comma joins:** no result-order counterexample found.
- **Join sides/kinds:** no counterexample among the admitted kinds. `NATURAL`, `USING`, `ASOF`, and DuckDB `POSITIONAL JOIN` carry extra/method arguments and are rejected at lines 314–316.
- **DISTINCT and grouped ordering:** no counterexample found once every actual grouping key and aggregate is P2-safe.
- **PostgreSQL implicit conversions:** no additional plan-dependent case found; invalid unknown literals generally fail during parse/bind rather than only on selected rows.
- **`FETCH` and exotic `LIMIT` forms:** no admission found; sqlglot’s extra option nodes cause `limit_not_literal`.

## Stable queries rejected by implementation slips

These are not counterexamples to soundness, but they are stable and not prohibited by the stated module rules.

1. Redundant order on a one-row result:

   ```sql
   SELECT COUNT(*) FROM t ORDER BY 1
   ```

   Rejected as `one_row_clause_not_allowed`, although it always has one row.

2. Redundant DISTINCT on a one-row result:

   ```sql
   SELECT DISTINCT COUNT(*) FROM t
   ```

   Also rejected as `one_row_clause_not_allowed`.

3. Deterministic removal of the sole aggregate row:

   ```sql
   SELECT COUNT(*) FROM t OFFSET 1
   ```

   Always returns no rows, but `OFFSET` is categorically rejected for the one-row shape.

4. Non-raising `TRY_CAST` in DuckDB:

   ```sql
   SELECT COUNT(*) FROM t
   WHERE TRY_CAST(s AS INTEGER) > 0
   ```

   sqlglot defines `TryCast` as a subclass of `Cast`, so `_may_raise` catches it through `isinstance(n, exp.Cast)`. DuckDB guarantees that `TRY_CAST` returns `NULL` rather than throwing ([DuckDB casting documentation](https://duckdb.org/docs/current/sql/expressions/cast)). The query is rejected as `predicate_not_allowed`.

No files were modified.