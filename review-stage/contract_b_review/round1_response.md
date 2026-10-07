## Verdict

The certificate is unsound. I found 11 strict-mode admissions whose bytes are not execution-invariant. I invoked the current code directly with sqlglot 30.18.0; every query below returned `True`.

Common pass path: each parses as one `exp.Select`, is read-only, contains no sampling/collation/set operation/`DISTINCT ON`, and uses only allow-listed functions. Derived-table subqueries are exempt at [certify.py:335](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/certify.py:335). The individual notes below cover the decisive shape, grouping, and value-domain checks.

### 1. SQLite clock value hidden in a stored column

```sql
SELECT MAX(date(d)) FROM t
```

Schema/data:

```sql
CREATE TABLE t(d TEXT);
INSERT INTO t VALUES ('now');
```

Certificate result: `(True, 'one_row')`.

- sqlglot parses `date(d)` as `exp.Date`.
- The literal scan sees no literal `'now'`.
- `_CLOCK_MIN_ARGS["date"] == 1`, and the call has one argument, so the implicit-clock check passes.
- `MAX` makes `_at_most_one_row` true.
- `_bare_column` sees `d` below `MAX`.
- `_value_safe(date(d))` returns true solely because `Date` is in `_SAFE_VALUE_CLASSES`; it never considers that `d` contains a SQLite clock keyword.

SQLite interprets the stored string `now` at execution time. Executions on opposite sides of midnight can return different dates on the identical database snapshot.

Smallest rule: for SQLite temporal functions, accept a time-value only when it is a literal proved not to be `now`/`localtime`; ordinary P2 string safety says nothing about clock keywords.

### 2. PostgreSQL orderless `ROWS` window frame

```sql
SELECT a,
       MIN(b) OVER (ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS m
FROM t
ORDER BY a, m
```

Use integer columns and rows `(1,10), (2,20), (3,30)`.

Certificate result: `(True, 'total_order')`.

- The window is `exp.Window(order=None, spec=WindowSpec(kind=ROWS,...))`.
- The window check tests only `w.args["order"]` and whether `w.this` is a safe aggregate. It ignores `spec`, so `MIN` passes.
- `_order_covers` finds both `a` and alias `m`.
- `_own_aggregates` excludes aggregates under windows, so `_bare_column` returns false.
- `_value_safe(Window(MIN(b)))` unwraps the window and accepts audited integer `b`.

Without a window `ORDER BY`, PostgreSQL processes partition rows in an unspecified order; an explicit `ROWS` frame therefore changes with that order. [PostgreSQL documents this unspecified ordering](https://www.postgresql.org/docs/16/sql-expressions.html).

Input order `1,2,3` yields, after final sorting:

```text
(1,10), (2,10), (3,20)
```

Input order `3,2,1` yields:

```text
(1,10), (2,20), (3,30)
```

Smallest rule: reject an orderless window with a non-whole-partition `ROWS` frame.

### 3. Qualified `ORDER BY` falsely matched to an output alias

```sql
SELECT MAX(a)
FROM (
  SELECT b AS a
  FROM t
  ORDER BY t.a
  LIMIT 1
) s
```

PostgreSQL; integer rows `(a,b) = (0,1), (0,2)`.

Certificate result: `(True, 'one_row')`.

- `_out_keys` gives the inner output keys `{b, a}`.
- For qualified `t.a`, `_order_covers` adds both `"t.a"` and the bare name `"a"` at [certify.py:136](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/certify.py:136).
- It therefore incorrectly concludes that `ORDER BY t.a` orders output alias `a`, admitting the `LIMIT`.
- The outer `MAX` passes `_at_most_one_row`; both source columns are audited integers.

SQL resolves `t.a` to the input column, never to output alias `a`. Since both input keys are zero, `LIMIT 1` may choose `b=1` or `b=2`; the final byte sequence is one row containing 1 or 2.

Smallest rule: a qualified order column must not satisfy an output-alias key; it may match only the same bound source expression.

### 4. Quoted-alias case is erased

```sql
SELECT MAX("X")
FROM (
  SELECT a AS "X"
  FROM t
  ORDER BY x
  LIMIT 1
) s
```

PostgreSQL; integer rows `(a,x) = (1,0), (2,0)`.

Certificate result: `(True, 'one_row')`.

- `_out_keys` lowercases `alias_or_name`, turning quoted `"X"` into `x`.
- It concludes that unquoted `ORDER BY x` covers `"X"`.
- The inner limit passes; the outer `MAX` establishes one-row shape.
- `_value_safe` also lowercases `"X"` and resolves it to the inner alias expression.

In PostgreSQL, `"X"` and unquoted `x` are different identifiers. `ORDER BY x` binds the stored input column, so either `a` can survive the tied limit.

Smallest rule: preserve `Identifier.quoted` and exact case in alias matching.

### 5. SQLite `GROUP BY` source-column/alias collision

```sql
SELECT a AS b, COUNT(*) AS n
FROM t
GROUP BY b
ORDER BY b, n
```

Rows `(a,b) = (1,0), (2,0)`, with integer columns.

Certificate result: `(True, 'total_order')`.

- `_bare_column` builds alias `b → a`.
- It unconditionally rewrites unqualified grouping key `b` to `a` at [certify.py:210](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/certify.py:210).
- Projection `a` is consequently judged grouped.
- Top ordering and value-domain checks pass for the integer outputs.

SQLite instead resolves `GROUP BY b` to the input column when that name exists. Thus `a` is a bare column and may be taken from either group member, yielding `(1,2)` or `(2,2)`. SQLite explicitly permits arbitrary bare-column selection in aggregate queries. [SQLite SELECT semantics](https://www.sqlite.org/lang_select.html).

Smallest rule: reject an unqualified grouping alias when it can collide with an input-column name; without schema binding, reject such alias-based grouping.

### 6. SQLite `HAVING` collision treated as an output alias

```sql
SELECT a, COUNT(*) AS b
FROM t
GROUP BY a
HAVING b > 0
ORDER BY a, b
```

Rows `(a,b) = (1,-1), (1,1)`.

Certificate result: `(True, 'total_order')`.

- Projection `a` is grouped and `COUNT(*)` is aggregated.
- In `_bare_column.ok`, an unqualified `b` in `HAVING` is accepted immediately because it matches an output alias; see [certify.py:223](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/certify.py:223).
- Ordering and value-domain checks pass.

SQLite resolves the colliding `HAVING b` to the stored column. It is then a bare group value, so the group may be retained or discarded depending on which row supplies `b`. The result is either empty or `(1,2)`.

Smallest rule: do not accept a `HAVING` alias reference when the name can bind an input column.

### 7. DuckDB interval equality is not congruent with `EXTRACT`

```sql
SELECT MAX(months)
FROM (
  SELECT EXTRACT(MONTH FROM d) AS months
  FROM t
  GROUP BY d
) s
```

Schema/data:

```sql
CREATE TABLE t(d INTERVAL);
INSERT INTO t VALUES (INTERVAL '1 month'), (INTERVAL '30 days');
```

Certificate result: `(True, 'one_row')`.

- sqlglot parses the function as `exp.Extract`, which `_value_safe` accepts unconditionally.
- `_bare_column` accepts `d` because it is the grouping key.
- `_value_domains` checks the projected `Extract`, not whether it is congruent under the grouping equality of `d`.
- Alias `months` resolves globally to the accepted `Extract`.
- Outer `MAX` gives one-row shape.

DuckDB deliberately compares `INTERVAL '1 month'` equal to `INTERVAL '30 days'`, while retaining distinct month/day components. [DuckDB’s interval documentation confirms both facts](https://duckdb.org/docs/current/sql/data_types/interval). The group can retain either representation; `EXTRACT(MONTH...)` is then 1 or 0, so the final integer bytes differ.

The audit compounds the issue by listing `INTERVAL` in `DUCK_SAFE` at [p2_audit.py:28](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/p2_audit.py:28).

Smallest rule: classify `EXTRACT` from a DuckDB interval as value-unsafe across grouping, and remove `INTERVAL` from the unconditional audit-safe types.

### 8. SQLite `CAST(... AS DATE)` is not a safe date-domain cast

```sql
SELECT CAST(x AS DATE) AS z
FROM t
GROUP BY z
ORDER BY z
```

Declare `x` with no type and bind the two values as integer `1` and real `1.0`.

Certificate result: `(True, 'total_order')`, even if P2 marks `x` unsafe.

- `_bare_column` resolves grouping alias `z` to the cast expression.
- `_value_safe` returns true for any cast whose target name is in `_SAFE_CAST`; `DATE` is included at [certify.py:95](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/certify.py:95).
- It does not inspect `x` or apply dialect-specific cast semantics.

In SQLite, `DATE` is not a native date type; this cast applies numeric affinity. It preserves `1` as INTEGER and `1.0` as REAL. Those values group as equal, but the chosen group representative can serialize as `1` or `1.0`. SQLite confirms numerically equal INTEGER and REAL values are equal for grouping. [SQLite datatype rules](https://www.sqlite.org/datatype3.html).

Smallest rule: in SQLite, do not consider casts to `DATE`, `DATETIME`, `TIMESTAMP`, or `BOOLEAN` value-safe; only accept targets that actually canonicalize the storage class, such as integer or text casts.

### 9. CTE column-list renaming bypasses expression lineage

```sql
WITH s(y) AS (
  SELECT x / 2 FROM t
)
SELECT y
FROM s
GROUP BY y
ORDER BY y
```

SQLite schema: untyped `x`, integer `y`. Store `(x,y) = (2,0), (2.0,0)`. Thus P2 correctly has `x` unsafe but `y` safe.

Certificate result: `(True, 'total_order')`.

- sqlglot’s CTE column list `s(y)` does not create an `exp.Alias` on `x / 2`.
- The inner ungrouped/unlimited block is not checked by `_value_domains`.
- Outer `y` is treated as a stored column and accepted solely because name `y` is in `p2["safe"]`.
- Grouping and top ordering therefore pass.

The CTE values are integer `1` and real `1.0`. SQLite groups them together, and the retained representation depends on the input/access order.

Smallest rule: resolve CTE column-list names to their defining expressions; never apply P2 merely by an unresolved column name.

### 10. `VALUES` column is blessed by an unrelated stored name

```sql
SELECT v.a
FROM t
CROSS JOIN (VALUES (1::numeric), (1.0::numeric)) AS v(a)
GROUP BY v.a
ORDER BY v.a
```

PostgreSQL; let stored `t.a` be an audited integer column.

Certificate result: `(True, 'total_order')`.

- The `VALUES` node is not a `Select` visited by `_selects`.
- `v.a` is an `exp.Column`; `_value_safe` ignores its qualifier and accepts it because unrelated stored name `a` is P2-safe.
- `_bare_column` sees exact grouping key `v.a`.
- The casts in `VALUES` are allow-listed.

The unconstrained numeric values compare equal but retain scale, so a group representative can print as `1` or `1.0`. PostgreSQL explicitly distinguishes unconstrained numeric scale, and the audit itself recognizes equal numerics with different text forms at [p2_audit.py:102](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/p2_audit.py:102). [PostgreSQL numeric documentation](https://www.postgresql.org/docs/16/datatype-numeric.html).

Smallest rule: P2 may apply only to a resolved base-table column, not a `VALUES`, CTE, derived-table, or table-function output that happens to share its name.

### 11. SQLite negative-zero audit does not detect negative zero

```sql
SELECT x FROM t ORDER BY x
```

Schema/data:

```sql
CREATE TABLE t(x);  -- no affinity
```

Bind `-0.0` and `+0.0` as real values.

Certificate result: `(True, 'total_order')` with the actual audit output.

- The audit tests negative zero using `CAST(x AS TEXT) LIKE '-%'` at [p2_audit.py:51](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview/p2_audit.py:51).
- SQLite renders both signs as `"0.0"` in that cast, so `neg == 0` and `x` is marked safe.
- `_order_covers` sees `ORDER BY x`; `_value_safe(x)` trusts P2.

The Python SQLite driver retrieves distinct IEEE bit patterns `0x8000000000000000` and `0x0000000000000000`. SQLite compares them equal, so their result order is unconstrained; a serializer that preserves the sign emits `[-0.0,0.0]` or `[0.0,-0.0]`.

Smallest rule: detect REAL zero signs in Python with `math.copysign` or a registered `signbit` UDF rather than through SQLite’s text cast.

## Clear implementation-slip false rejections

These are stable but rejected for reasons not implied by the stated rules:

- `VALUES (1)` → `not_select`. A standalone `VALUES` is a single read-only query, but the top-level type gate accepts only `Select`/`SetOperation`.

- `SELECT COUNT(*), 1 FROM t` → `unordered_result`. An aggregate query without `GROUP BY` has one group row, but `_at_most_one_row` incorrectly requires every projection to contain an aggregate.

- `SELECT COUNT(*) FILTER (WHERE b > 0) FROM t` → `unordered_result`. sqlglot represents `FILTER` as a sibling predicate around `Count`; `_under_agg` therefore thinks `b` is outside the aggregate. With a redundant `ORDER BY`, `_bare_column` rejects it instead. The result is nevertheless exactly one integer row.

- With P2-safe stored `a`:

  ```sql
  WITH unused AS (SELECT x / 2 AS a FROM t)
  SELECT a FROM t ORDER BY a
  ```

  → `value_domain`. `_value_domains` builds one global alias map across all scopes, so the unused CTE’s alias poisons the unrelated top-level base column.

## Categories with no separate break found

- SQLite two-argument `MIN/MAX` are parsed as `exp.Min/Max` with nonempty `.expressions` and are rejected by `_value_domains`.
- I found no additional independent admission from ordinals, `NULLS FIRST/LAST`, aggregate `ORDER BY`, `FILTER`, `QUALIFY`, `USING`/`NATURAL`, `LATERAL`, recursive CTEs, `UNNEST`/table functions, `IN` subqueries, scalar subqueries, or derived `SELECT *`.
- I found no separate break from `char(n)` padding, NaN, integer overflow, or implicit comparison casts. `FILTER` does expose the false rejection above, while CTEs and `VALUES` expose the P2 lineage failure.
- No files were modified.