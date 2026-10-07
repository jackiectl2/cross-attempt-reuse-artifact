I found no G1 counterexample.

Under P1–P3, every admitted query I could construct remained byte-stable. I did find one concrete G2 hole and several clear false rejections.

## G1: no counterexample found

The three admitted shapes appear sound:

- One-row queries use only exact `COUNT` and `MIN`/`MAX` over P2-safe columns. Parallel `MIN`/`MAX` may select different equal values, but P2 requires those alternatives to serialize identically. Arithmetic and `ROUND` are applied once to already-determined aggregate values; there is no order-dependent reduction such as `SUM` or `AVG`.
- Ordered-row queries output only P2-safe stored columns and must order-cover every output. Any remaining ordering tie therefore consists of byte-identical output rows. This remains true at a `LIMIT`/`OFFSET` boundary and after `DISTINCT`.
- Grouped queries use only P2-safe stored keys and `COUNT`/safe `MIN`/`MAX`. Ordering every key, or every output under `DISTINCT`, totally orders all byte-distinct result rows.

Specific suspected attacks did not work:

- DuckDB float division follows IEEE behavior, so its admitted FLOAT/DOUBLE arithmetic produces infinities or NaNs rather than row-dependent division errors. [DuckDB documentation](https://duckdb.org/docs/current/sql/dialect/postgresql_compatibility)
- Negative zero—the important equal-comparing/different-print float case—is explicitly audited in all three engines ([SQLite audit](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/p2_audit.py:61), [DuckDB audit](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/p2_audit.py:92), [PostgreSQL audit](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/p2_audit.py:113)).
- DuckDB `TIMETZ` was not a P2 counterexample: DuckDB normalizes offset-bearing inputs for output, as its examples demonstrate. [DuckDB time documentation](https://duckdb.org/docs/lts/sql/data_types/time)
- Alias and name-binding anomalies either fail certification, fail binding in the engine, or ultimately order by the same projected value; I found no plan-dependent completing result.

Therefore there is no additional G1 rule to propose.

## G2: one concrete admitted counterexample

### PostgreSQL two-argument `LENGTH(bytea, encoding)`

Query:

```sql
SELECT COUNT(*)
FROM t
WHERE a <> 0 AND LENGTH(b, 'UTF8') > 0
```

Database dictionary:

```python
{
    "catalog_ok": True,
    "tables": {
        "t": {
            "a": {"safe": True, "family": "num"},
            "b": {"safe": True, "family": "other"},
        }
    },
}
```

Example database:

```sql
CREATE TABLE t(a integer, b bytea);
INSERT INTO t VALUES (0, decode('ff', 'hex'));
```

PostgreSQL documents `length(bytea, encoding)`: it interprets the bytes in the specified encoding, and invalid encoded data raises an error. Here `ff` is invalid standalone UTF-8. [PostgreSQL 16 binary-string functions](https://www.postgresql.org/docs/16/functions-binarystring.html)

Two legal evaluations are possible:

1. Evaluate `a <> 0` first. It is false, so the second conjunct is unnecessary; the query completes with `0`.
2. Evaluate `LENGTH(b, 'UTF8')` first. It raises an invalid-byte-sequence error.

PostgreSQL explicitly says Boolean subexpression order is undefined and `WHERE` expressions may be reorganized. [PostgreSQL 16 expression-evaluation rules](https://www.postgresql.org/docs/16/sql-expressions.html#SQL-EXPRESSIONS-EVALUATION-RULES)

Certificate trace:

- SQLGlot parses the call as `exp.Length` with an optional `encoding` argument; `Length` is in `_B_ROW`.
- `t`, `a`, and `b` bind successfully.
- `p2_audit.py` declares PostgreSQL `bytea` P2-safe at [p2_audit.py:38](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/p2_audit.py:38).
- `_row_ok` accepts `AND`, `<>`, `>`, columns, literals, and `Length`.
- `_may_raise` has special cases for `LIKE`, arithmetic, casts, `ABS`, `ROUND`, and PostgreSQL `SUBSTRING`, but none for two-argument `Length`; see [certify.py:276](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/certify.py:276).
- `families` is a no-op outside DuckDB.
- The output is `COUNT(*)`, so the one-row branch accepts it at [certify.py:590](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/certify.py:590).

Smallest additional rule: on PostgreSQL, classify an `exp.Length` having a nonempty `encoding` argument as potentially raising in row expressions.

This does not violate G1 because only one completing execution exists in the failing evaluation scenario; it is strictly a G2 hole.

## Stable queries rejected by implementation slips

Assume the referenced columns are P2-safe.

| Engine | Stable query | Rejection cause |
|---|---|---|
| DuckDB | `SELECT COUNT(*) FROM t WHERE tm = '12:00:00'` | `_iso_time` recognizes dates and timestamps but not time-only ISO literals, so `families` incorrectly treats this valid TIME comparison as mixed string/time. DuckDB documents `hh:mm:ss` TIME syntax. [DuckDB time types](https://duckdb.org/docs/lts/sql/data_types/time) |
| PostgreSQL | `SELECT COUNT(*) FROM t WHERE SUBSTR(c, 1, 2) = 'ab'` | `_may_raise` rejects every PostgreSQL substring because a negative length can raise, without noticing that the literal length is positive. |
| SQLite | `SELECT a AS x, a AS x FROM t ORDER BY 1, 2` | Rejected as `duplicate_alias` at [certify.py:396](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/certify.py:396), although ordinal ordering removes all alias ambiguity. |
| PostgreSQL | `SELECT COUNT(*) FROM t HAVING COUNT(*) >= 0` | Always one row, but the one-row branch rejects every `HAVING` before applying the existing safe-HAVING logic. |
| Any engine | `SELECT DISTINCT COUNT(*) FROM t` | Plain `DISTINCT` cannot change a one-row result, but the one-row branch rejects all `DISTINCT` at [certify.py:592](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/certify.py:592). |
| Any engine | `SELECT a FROM t ORDER BY a LIMIT 0` | The ordered-row shape contains no evaluable output expression that can raise, yet the global `LIMIT 0` check rejects it before shape classification at [certify.py:543](/private/tmp/claude-501/-Users-ctl-Local-Documents-1-personal-projs-ARIS-Auto-Research-EDBT-Conference-27/03a726a5-62dd-4772-abdc-be11a36e0a1f/scratchpad/certreview6/certify.py:543). |

The highest-priority correction is the PostgreSQL encoded-`bytea` `LENGTH` hole because it is an actual admitted G2 counterexample. G1 survived this review.