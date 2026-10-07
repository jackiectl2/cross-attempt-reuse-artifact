I found eight admitted counterexamples. Seven exploit `LIMIT 0` as an exception-suppression boundary; the eighth exploits the one-row-output exception in `over_aggs`. No files were modified.

I use these audited dictionaries below:

```python
D_text = {
    "catalog_ok": True,
    "tables": {"t": {"a": {"safe": True, "family": None}}},
}

D_num = {
    "catalog_ok": True,
    "tables": {"t": {"a": {"safe": True, "family": "num"}}},
}

D_bool = {
    "catalog_ok": True,
    "tables": {
        "t": {
            "a": {"safe": True, "family": "num"},
            "b": {"safe": True, "family": "bool"},
        }
    },
}

D_date = {
    "catalog_ok": True,
    "tables": {"t": {"d": {"safe": True, "family": "time"}}},
}
```

## 1. SQLite oversized literal `LIKE` pattern

Engine/database/query:

```python
engine = "sqlite"
schema = D_text
query = (
    "SELECT a FROM t WHERE a LIKE '"
    + ("a" * 50001)
    + "' ORDER BY a LIMIT 0"
)
```

Dataset:

```sql
CREATE TABLE t(a TEXT);
INSERT INTO t VALUES ('x');
```

Why admitted:

- One `Select`; only allowed clauses.
- `t.a` binds and is marked safe.
- sqlglot parses the predicate as `Like(Column(a), Literal(...))`.
- `_may_raise` regards every SQLite `LIKE` with a string-literal pattern as safe, regardless of its length.
- The projection is the stored safe column `a`.
- `ORDER BY a` covers the output.
- `LIMIT 0` is an integer literal and is permitted for this shape.
- Result: `(True, "total_order")`.

Divergent executions:

- A zero-limit plan never opens the scan/filter child and returns empty serialized bytes.
- A materialize-filter-sort-limit plan evaluates `LIKE` and raises `LIKE or GLOB pattern too complex`.
- I reproduced both relevant behaviors locally: the exact query with `LIMIT 0` returns empty, while removing the limit raises.

SQLite’s default maximum pattern length is 50,000 bytes and can also be lowered per connection. [SQLite limits](https://www.sqlite.org/limits.html)

Smallest rejecting rule: reject SQLite literal `LIKE`/`ILIKE` patterns exceeding the connection’s `SQLITE_LIMIT_LIKE_PATTERN_LENGTH`; without auditing that limit, rejecting literal patterns over 50,000 fixes the default build example.

## 2. DuckDB comparison with an invalid string literal

```python
engine = "duckdb"
schema = D_num
query = "SELECT a FROM t WHERE a = 'x' ORDER BY a LIMIT 0"
```

Dataset:

```sql
CREATE TABLE t(a INTEGER);
INSERT INTO t VALUES (1);
```

Why admitted:

- `EQ`, `Column`, and `Literal` are all in `_B_ROW`.
- `_may_raise(EQ, "duckdb")` is false.
- `families` computes `{"num", "strlit"}`.
- Its rejection condition only triggers when `"str"` is present, not `"strlit"`, so it passes.
- The safe direct output, ordering, and literal zero limit pass.

Divergence:

- A zero-limit plan returns empty bytes without converting `'x'`.
- A plan that initializes or evaluates the predicate attempts the implicit `STRING_LITERAL → INTEGER` cast and raises a conversion error.

DuckDB explicitly permits string literals to be implicitly converted to any type, while failed conversions raise. [DuckDB literal types](https://duckdb.org/docs/current/sql/data_types/literal_types), [DuckDB casting](https://duckdb.org/docs/current/sql/expressions/cast)

Smallest rule: in `families`, treat `strlit` like `str`:

```python
if fs & {"str", "strlit"} and not fs <= {"str", "strlit", "null"}:
    return False
```

This conservatively rejects even valid-looking literals such as `'1'`, which is appropriate for this static certificate.

## 3. DuckDB `IN` has the same literal-conversion hole

```python
engine = "duckdb"
schema = D_num
query = "SELECT a FROM t WHERE a IN ('x') ORDER BY a LIMIT 0"
```

Dataset: the same one-row integer table.

Trace:

- sqlglot parses `a IN ('x')` as `exp.In`, with no query or unnest.
- `_B_ROW` permits it.
- `families` sees `{"num", "strlit"}` and passes for the same reason as counterexample 2.
- Binding, output safety, ordering, and limit checks all pass.

Divergence: zero-limit pruning returns empty bytes; an evaluation plan converts `'x'` to the type of `a` and raises.

Smallest rule: the same `strlit` change above.

## 4. DuckDB `BETWEEN` has the same hole for temporal values

```python
engine = "duckdb"
schema = D_date
query = (
    "SELECT d FROM t "
    "WHERE d BETWEEN 'not-a-date' AND 'also-not-a-date' "
    "ORDER BY d LIMIT 0"
)
```

Dataset:

```sql
CREATE TABLE t(d DATE);
INSERT INTO t VALUES (DATE '2020-01-01');
```

Trace:

- `Between`, its column, and both literals are `_B_ROW`.
- `_may_raise` does not classify `Between` as raising.
- `families` obtains `{"time", "strlit"}`, which passes.
- `d` is safe, `ORDER BY d` covers it, and `LIMIT 0` passes.

Divergence: zero-limit pruning avoids the date conversions; a predicate-evaluating plan raises on the invalid date literal.

Smallest rule: again treat `strlit` as a potentially failing string operand.

## 5. DuckDB simple `CASE` hides an implicit comparison

```python
engine = "duckdb"
schema = D_num
query = (
    "SELECT a FROM t "
    "WHERE CASE a WHEN 'x' THEN TRUE ELSE TRUE END "
    "ORDER BY a LIMIT 0"
)
```

Dataset: the same one-row integer table.

Trace:

- sqlglot represents this as `Case(this=Column(a), ifs=[If(this=Literal('x'), ...)])`.
- Every node is in `_B_ROW`.
- `families` checks only the `THEN` and `ELSE` result expressions of a `Case`; both are `boollit`.
- It never checks the implicit equality between the simple-`CASE` base `a` and the `WHEN 'x'` operand.
- Remaining shape checks pass.

Divergence: a zero-limit plan returns empty bytes; an evaluating plan performs the implicit integer comparison and raises converting `'x'`.

Smallest rule: for a simple `CASE` with `n.this is not None`, apply comparison-family checking to `n.this` and every `If.this`/`WHEN` operand.

## 6. DuckDB `COALESCE` accepts a failing string-literal fallback

```python
engine = "duckdb"
schema = D_bool
query = (
    "SELECT a FROM t "
    "WHERE COALESCE(b, 'not-a-boolean') "
    "ORDER BY a LIMIT 0"
)
```

Dataset:

```sql
CREATE TABLE t(a INTEGER, b BOOLEAN);
INSERT INTO t VALUES (1, NULL);
```

Trace:

- `Coalesce`, `Column`, and `Literal` are allowed row nodes.
- Its operands have families `{"bool", "strlit"}`.
- The current check only rejects mixtures containing `"str"`, so it passes.
- `a` is a safe direct output, ordering covers it, and zero is accepted as the limit.

Divergence: the zero-limit plan avoids `COALESCE`; a plan evaluating the row reaches the fallback and raises converting the literal to Boolean.

Smallest rule: the same `strlit` fix from counterexample 2.

## 7. DuckDB `HAVING` accepts the same failing conversion

```python
engine = "duckdb"
schema = D_num
query = (
    "SELECT a, COUNT(*) FROM t "
    "GROUP BY a "
    "HAVING COUNT(*) = 'x' "
    "ORDER BY a LIMIT 0"
)
```

Dataset:

```sql
CREATE TABLE t(a INTEGER);
INSERT INTO t VALUES (1);
```

Trace:

- `a` is a safe grouping key.
- Both grouped outputs are allowed.
- In `over_aggs(..., once=False)`, `EQ` is not `_B_MAY_RAISE`; `COUNT(*)` and the literal pass.
- `families` sees `{"num", "strlit"}` and incorrectly accepts it.
- `ORDER BY a` names every grouping key, so the grouped result is treated as totally ordered.
- Grouped `LIMIT 0` is permitted.

Divergence: early zero-limit pruning returns empty bytes; aggregation followed by `HAVING` attempts to convert `'x'` to the count’s numeric type and raises.

Smallest rule: the same `strlit` correction.

## 8. PostgreSQL one-row output admits a dead, raising `CASE` arm

```python
engine = "postgres"
schema = D_num
query = (
    "SELECT CASE "
    "WHEN COUNT(*) >= 0 THEN 1 "
    "ELSE 1 / 0 "
    "END FROM t"
)
```

Dataset:

```sql
CREATE TABLE t(a INTEGER);
INSERT INTO t VALUES (1);
```

Trace:

- The projection contains `COUNT`, so `has_agg` selects the one-row shape.
- `over_aggs` starts with `once=True`.
- The `Case`, comparison, `Count`, literals, and `Div` are all in `_B_ROW`.
- At line 488, `_may_raise` is checked only when `not once`; therefore the division is accepted.
- There is no disallowed one-row clause.
- Result: `(True, "one_row")`.

Divergence:

- A runtime-evaluated `CASE` computes `COUNT(*) >= 0`, skips the `ELSE`, and returns `1`.
- PostgreSQL is explicitly permitted to pre-evaluate immutable constant subexpressions during planning; its documentation uses a dead `CASE ... ELSE 1/0` arm as the canonical example of a planning-time error. [PostgreSQL 16 expression evaluation rules](https://www.postgresql.org/docs/16/sql-expressions.html)

Smallest rule: enforce the stated one-row output grammar. In particular, reject `CASE` and `COALESCE` in one-row projections, or at least reject `_may_raise` nodes appearing below conditionally evaluated constructs. The docstring only promises aggregates, literals, and arithmetic here; `over_aggs` is broader than that promise.

## Stable queries rejected by implementation slips

With `D_num`, these are stable but rejected despite no corresponding stated prohibition:

```sql
-- One row is already totally ordered.
SELECT COUNT(*) FROM t ORDER BY 1;
-- rejected: one_row_clause_not_allowed

-- DISTINCT has no effect on a one-row aggregate result.
SELECT DISTINCT COUNT(*) FROM t;
-- rejected: one_row_clause_not_allowed

-- A constant safe HAVING condition cannot introduce instability.
SELECT COUNT(*) FROM t HAVING COUNT(*) >= 0;
-- rejected: one_row_clause_not_allowed

-- GROUP BY refers to the output alias of the stored safe column.
SELECT a AS x, COUNT(*) FROM t GROUP BY x ORDER BY x;
-- rejected: group_key_not_a_stored_column

-- Parentheses do not stop this from being the same stored column.
SELECT (a) FROM t ORDER BY a;
-- normally rejected as output_not_a_stored_column if sqlglot retains exp.Paren
```

The first three are especially clear slips: the docstring discusses HAVING and does not state that `ORDER BY`, plain `DISTINCT`, or safe `HAVING` is forbidden for the one-row shape.

## Categories where I found no additional concrete failure

- Different serialized bytes: none; all eight counterexamples are error-versus-no-error failures.
- SQLite arithmetic, casts, `ABS`, `ROUND`, `LOWER`/`UPPER`/`LENGTH`/`TRIM`/`REPLACE`/`SUBSTR`: no additional admitted plan-dependent exception found. The oversized literal pattern is the SQLite failure.
- PostgreSQL row predicates: no additional one found. Arithmetic, casts, substring, and unsafe patterns are mostly rejected; incompatible comparisons/functions generally fail uniformly during binding.
- DuckDB `float_arith`: no concrete failing FLOAT/DOUBLE arithmetic case found. Ignoring `ROUND`’s precision operand is suspicious, but I did not establish a legal bound call whose precision evaluation can raise in 1.3.
- DuckDB DECIMAL/HUGEINT, DATE/TIMESTAMP, BLOB, UUID, ENUM, and nested types: no additional confirmed counterexample. The coarse families cause uniform binder failures in several cross-type cases, but I did not count uniform failure as breaking the stated claim.
- Name binding and alias shadowing: no admitted instability found.
- `DISTINCT` and `OFFSET`: no admitted instability found beyond the documented false rejections above.
- `p2_audit.py` value safety: no confirmed equal-but-differently-printed pair that passes the audit. DuckDB `TIMETZ` normalizes offsets, fixed-scale DECIMALs print consistently, and unsupported nested/ENUM-like types are marked unsafe.
- Unchecked catalog objects: no concrete catalog-backed counterexample satisfying P1–P3.
- sqlglot-specific alternate meanings: the simple-`CASE` representation in counterexample 5 is the concrete parser-induced hole. `POSITIONAL`, `ASOF`, `NATURAL`, `USING`, sampling, and similar constructs acquire rejected AST arguments or nodes.