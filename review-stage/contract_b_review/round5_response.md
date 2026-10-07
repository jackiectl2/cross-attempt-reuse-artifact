## Verdict on G1

**G1 has concrete counterexamples on all three engines.**

The root defect is incorrect Unicode identifier folding. `certify_sql_b` uses Python’s full-Unicode `.lower()`, while:

- DuckDB compares identifiers using ASCII-only case folding. [DuckDB identifier rules](https://duckdb.org/docs/stable/sql/dialect/keywords_and_identifiers)
- SQLite uses its internal case-independent comparison for identifiers, with default case folding limited to ASCII. [SQLite identifier comparison API](https://www.sqlite.org/c3ref/stricmp.html), [SQLite Unicode case-folding limitation](https://www.sqlite.org/faq.html)
- PostgreSQL 16, for a multibyte database encoding such as UTF-8, folds ASCII `A`–`Z` but leaves high-bit bytes unchanged. [PostgreSQL 16 source](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/backend/parser/scansup.c#L41-L73)

Thus the checker considers `Ä` and `ä` the same name, while the engines distinguish them.

### Exact audited dictionaries

The order shown matters for SQLite and DuckDB because the comprehension at [certify.py:368](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/certify.py:368) overwrites the folded `Ä` entry with the later `ä` entry.

```python
D_sqlite = {
    "catalog_ok": True,
    "tables": {
        "t": {
            "Ä": {"safe": False, "family": None},
            "ä": {"safe": True,  "family": None},
        }
    },
}

D_duckdb = {
    "catalog_ok": True,
    "tables": {
        "t": {
            "Ä": {"safe": False, "family": "flt"},
            "ä": {"safe": True,  "family": "num"},
        }
    },
}

D_postgres = {
    "catalog_ok": True,
    "tables": {
        "t": {
            "Ä": {"safe": False, "family": "num"},
            "ä": {"safe": True,  "family": "num"},
        }
    },
}
```

These dictionaries are realizable outputs of `p2_audit.py`.

SQLite:

```sql
CREATE TABLE t("Ä", "ä" INTEGER);
INSERT INTO t VALUES (1, 10), (1.0, 20);
CREATE INDEX i_desc ON t("Ä" DESC);
```

The first column has both INTEGER and REAL storage classes, so [p2_audit.py:83-87](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/p2_audit.py:83) marks it unsafe.

DuckDB:

```sql
CREATE TABLE t("Ä" DOUBLE, "ä" INTEGER);
INSERT INTO t VALUES
    (CAST('0.0' AS DOUBLE), 10),
    (CAST('-0.0' AS DOUBLE), 20);
CREATE INDEX i ON t("Ä");
```

The `signbit` audit at [p2_audit.py:102-104](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/p2_audit.py:102) marks `Ä` unsafe.

PostgreSQL:

```sql
CREATE TABLE t("Ä" numeric, "ä" integer);
INSERT INTO t VALUES ('1.0', 10), ('1.00', 20);
CREATE INDEX i ON t("Ä");
```

The numeric audit at [p2_audit.py:130-132](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/p2_audit.py:130) observes equal numeric values with two textual representations and marks `Ä` unsafe.

### Counterexample 1: one-row shape

For each engine, with its corresponding dictionary:

```sql
SELECT MIN(Ä) FROM t
```

All three calls return:

```python
(True, "one_row")
```

Trace:

1. `catalog_ok` passes.
2. sqlglot 30.18.0 parses `Ä` as an unquoted `Identifier(this='Ä', quoted=False)` in every dialect.
3. The statement is one `Select`, one base table, with no forbidden clauses.
4. `_fold(Ä, engine)` becomes `ä` through Python `.lower()` at [certify.py:263-267](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/certify.py:263).
5. SQLite/DuckDB have already collapsed the two schema columns to the later safe `ä`; PostgreSQL directly looks up safe `ä`.
6. `agg_ok` therefore sees a P2-safe argument at [certify.py:411-412](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/certify.py:411).
7. `over_aggs` succeeds and the one-row branch admits it at lines 585–591.

At execution, however, each engine binds `Ä` to the unsafe uppercase column.

- SQLite can return either integer `1` or real `1.0`. On the sample above, a table aggregation returns integer `1`, while the descending covering-index MIN plan returns real `1.0`.
- DuckDB may select either `0.0` or `-0.0` as the equal minimum depending on vector/parallel combination order.
- PostgreSQL may select either numeric scale. Its `numeric_smaller` returns its second argument when the arguments compare equal, so transition/combine order determines whether `1.0` or `1.00` survives. [PostgreSQL numeric source](https://github.com/postgres/postgres/blob/REL_16_STABLE/src/backend/utils/adt/numeric.c#L3295-L3313)

The resulting Python values print differently, so the serialized bytes differ.

### Counterexample 2: ordered stored-column shape

```sql
SELECT Ä FROM t ORDER BY Ä
```

All three calls return:

```python
(True, "total_order")
```

The common parsing and incorrect binding trace is the same. In the ordered-row branch:

- `bound` contains the incorrectly selected safe `ä`.
- The P2 check at lines 596–600 passes.
- `order_cover` binds `ORDER BY Ä` to the same supposed stored column and covers output zero.
- The query is admitted at lines 601–606.

Runtime output may be:

- SQLite: `[1, 1.0]` or `[1.0, 1]`.
- DuckDB: `[0.0, -0.0]` or `[-0.0, 0.0]`.
- PostgreSQL: `[Decimal('1.0'), Decimal('1.00')]` or the reverse.

The values are equal for sorting, so `ORDER BY` does not constrain their relative order. Index order, sort implementation, scan order, or parallel merge can reverse them.

### Counterexample 3: grouped shape

```sql
SELECT Ä, COUNT(*) FROM t GROUP BY Ä ORDER BY Ä
```

All three calls return:

```python
(True, "total_order")
```

After the same false binding:

- The grouping key is accepted as safe at lines 611–619.
- `Ä` is accepted as a grouping-column output.
- `COUNT(*)` passes `agg_ok`.
- `ORDER BY Ä` is treated as covering every grouping key, so lines 640–642 admit it.

At runtime the two unsafe values form one equality group with count `2`. The group’s representative may be `1` versus `1.0`, `0.0` versus `-0.0`, or `1.0` versus `1.00`. Thus the single row’s first serialized value can differ.

### Smallest rejection rule

Reject an identifier when Python lowercasing changes any non-ASCII character. Applied to query identifiers—and preferably audited schema identifiers—this rejects all witnesses:

```python
any(ord(ch) > 127 and ch.lower() != ch for ch in identifier)
```

Equivalently, use engine-accurate ASCII-only folding instead of `str.lower`; but the smallest additional default-deny rule is simply to reject these identifiers.

I found no separate G1 failure in COUNT ratios, `ROUND`, or correctly bound safe FLOAT `MIN`/`MAX`. COUNT is exact, the expression tree fixes the arithmetic order, and the audits specifically exclude signed-zero ambiguity. The fatal issue is that the certificate can bind those expressions to the wrong stored column.

## G2 counterexamples

There is a common bug in `over_aggs`: `_may_raise` is checked only when `once` is false at [certify.py:512](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview5/certify.py:512), but descending through `AND` or `OR` never changes `once`. Only CASE/IF/COALESCE do so.

PostgreSQL explicitly states that subexpression evaluation order is undefined and that unnecessary subexpressions may be skipped. [PostgreSQL expression-evaluation rules](https://www.postgresql.org/docs/16/sql-expressions.html#SYNTAX-EXPRESS-EVAL)

Each query below is admitted as `(True, "one_row")`. A plan that evaluates the left side first returns `true`; a plan that evaluates the right side raises.

Use:

```python
D_num = {
    "catalog_ok": True,
    "tables": {"t": {"z": {"safe": True, "family": "num"}}},
}
D_text = {
    "catalog_ok": True,
    "tables": {"t": {"s": {"safe": True, "family": "str"}}},
}
```

1. Division by zero; PostgreSQL, `D_num`, one row `z = 0`:

   ```sql
   SELECT COUNT(*) > 0 OR 1 / MIN(z) > 0 FROM t
   ```

2. Failing cast; PostgreSQL, `D_text`, one row `s = 'x'`:

   ```sql
   SELECT COUNT(*) > 0 OR CAST(MIN(s) AS INT) > 0 FROM t
   ```

3. BIGINT addition overflow; PostgreSQL, `D_num`, one row `z = 9223372036854775807`:

   ```sql
   SELECT COUNT(*) > 0 OR MIN(z) + 1 > 0 FROM t
   ```

4. BIGINT ABS overflow; PostgreSQL, `D_num`, one row `z = -9223372036854775808`:

   ```sql
   SELECT COUNT(*) > 0 OR ABS(MIN(z)) > 0 FROM t
   ```

5. Negative substring length; PostgreSQL, `D_text`, one row `s = 'x'`:

   ```sql
   SELECT COUNT(*) > 0 OR SUBSTR(MAX(s), 1, -1) = 'x' FROM t
   ```

For all five, `MIN`/`MAX` binds to a safe stored column; `over_aggs` encounters `OR` with `once=True`, leaves it true, and consequently ignores the may-raise operation. The smallest corresponding rule is to treat descendants of `AND` and `OR` as conditionally evaluated, just as the implementation already does for CASE and COALESCE.

## Obviously stable false negatives

These are clean implementation-level false negatives.

1. SQLite audit over-approximation:

   ```sql
   -- Stored x values are integer 1 and real 2.5: no equal pair exists.
   SELECT x FROM t ORDER BY x
   ```

   `p2_audit.py` marks `x` unsafe merely because both storage classes occur, even though these particular values do not compare equal. Rejection: `p2_unsafe_column`.

2. DuckDB/PostgreSQL lone negative zero:

   ```sql
   -- The table contains exactly one row, f = -0.0.
   SELECT f FROM t ORDER BY f
   ```

   Both audits mark any occurrence of negative zero unsafe. With no positive zero—and only one row—there is no competing equal representation. Rejection: `p2_unsafe_column`.

3. Duplicate aliases with ordinal ordering:

   ```sql
   SELECT a AS x, a AS x FROM t ORDER BY 1, 2
   ```

   The values and ordering are unambiguous, but line 392 rejects every duplicate alias before considering that only ordinals are used. Rejection: `duplicate_alias`.

4. Identical grouped outputs:

   ```sql
   SELECT COUNT(*) AS n1, COUNT(*) AS n2
   FROM t
   GROUP BY c
   ORDER BY n1
   ```

   Every serialized row is `(n,n)`, so ordering by `n1` totally orders the bytes. `order_cover` covers only output zero and rejects output one. Rejection: `unordered_result`.

5. DISTINCT does not invalidate grouped-key totality:

   ```sql
   SELECT DISTINCT c, COUNT(*)
   FROM t
   GROUP BY c
   ORDER BY c
   ```

   `GROUP BY c` already gives at most one row per `c`; safe `c` therefore totally orders the rows. Line 641 disables the grouping-key proof whenever `DISTINCT` is present. Rejection: `unordered_result`.

6. Harmless clauses on an already one-row aggregate:

   ```sql
   SELECT COUNT(*) FROM t ORDER BY 1
   SELECT DISTINCT COUNT(*) FROM t
   ```

   Both are necessarily one-row and byte-stable. The blanket clause test at lines 586–588 rejects them as `one_row_clause_not_allowed`.

No files were modified.