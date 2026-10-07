"""Contract B certificate: is a query's serialized result byte-stable on a pinned snapshot?

Contract B (final, `certify(..., strict=True)` with the database's audited schema) is a whitelist: a query is admitted
only if every construct in it is on the list below and every name binds to a stored column. Anything else is
rejected, so the rule fails closed on SQL it does not model.
  Statement  one SELECT block: no CTE, subquery, set operation, window, DISTINCT ON, VALUES or table function.
  FROM       base tables of the audited schema, joined with JOIN ... ON or a comma; no USING, NATURAL or LATERAL.
  Names      every column binds to exactly one stored column of the tables in scope (case-insensitive in SQLite and
             DuckDB; PostgreSQL folds unquoted names to lower case and matches quoted names exactly; names with
             non-ASCII characters do not bind). An output alias may not shadow a different column in scope.
  Row expressions (WHERE, ON, the argument of COUNT, CASE): bound columns, literals other than clock words,
             comparisons, AND / OR / NOT, IS NULL, IN (literals), BETWEEN, LIKE, arithmetic, CASE, COALESCE,
             LOWER / UPPER / LENGTH / SUBSTR / TRIM / REPLACE / ABS / ROUND and casts to integer, floating-point or
             text, each in its plain form (no encoding, collation or format argument).
             No date or time function: SQLite reads the clock when a stored string says 'now'.
  Shapes     one row   no GROUP BY; every output is built from COUNT, MIN and MAX with literals and arithmetic;
                       the argument of MIN / MAX is a P2-safe stored column.
             ordered   no aggregate; every output is a P2-safe stored column; ORDER BY names every output column.
             grouped   GROUP BY P2-safe stored columns; every output is a grouping column or one COUNT / MIN / MAX;
                       ORDER BY names every output.
             LIMIT and OFFSET take integer literals.
P2-safe: the snapshot audit (p2_audit.py) found no two values of the column that compare equal and print differently.
A row expression may not hold an operation that can raise on some rows, because a plan may or may not evaluate it:
outside SQLite arithmetic, ABS, ROUND and casts other than TRY_CAST (DuckDB's arithmetic over FLOAT / DOUBLE columns
is allowed: it returns inf or nan), and SUBSTR in PostgreSQL; in SQLite ABS; a LIKE pattern that is not a string
literal of at most 1,000 characters, or that holds a backslash in PostgreSQL; in DuckDB a comparison, IN, BETWEEN,
CASE or COALESCE that puts a string or a string literal next to a value of another type, which DuckDB converts when it
evaluates a row (a well-formed date or timestamp literal next to a date or time column is allowed); CASE x WHEN y;
LIMIT 0, with which a plan may evaluate nothing. HAVING takes no such operation either. A one-row output is evaluated
once, so it may use arithmetic, but not under CASE, COALESCE, AND or OR, whose arms a planner may fold or skip.
The guarantee is about results: two executions that both complete return the same bytes. The rules above also remove
the operations we know to raise for some rows; six rounds of adversarial review produced that list
(review-stage/contract_b_review/), and we do not claim it complete. The sixth round found no admitted query whose
bytes can differ between two completing executions; the last change after it only rejects more.
Preconditions: (P1) a fixed snapshot, engine version and session configuration, and a fresh read-only session, with no
temporary or attached object that could shadow a base table; the audit also checks the catalog for objects that act
behind the query text (PostgreSQL: row-level security, user-defined functions, relations outside `public`, a
non-default search path; DuckDB: user-defined macros) and Contract B rejects every query of a database that has one;
(P2) the audit of the stored values; (P3) a deterministic serializer.
Why this is byte-stable under these preconditions: the joined and filtered rows form a multiset that
the query text and the data determine; COUNT is a function of that multiset; MIN / MAX and the grouping columns range
over P2-safe columns, so the engine has only one printable value to return; arithmetic on those values is
deterministic; and an ORDER BY over every output column leaves ties only between rows that print the same.
MongoDB (DAB interface: collection/filter/projection/limit, no sort): only a find whose filter is exactly an equality
on `_id` (at most one document) and whose projection, if any, only includes or excludes fields is certified.

Earlier rules, kept to reproduce earlier results (environment variable ED_CONTRACT_B):
  rules5   the rule of the experiment plan (results/analysis_dab_r1, files named *_certv1): single read-only statement;
           every LIMIT / OFFSET in a scope whose ORDER BY covers the scope's outputs; no order-sensitive window or
           aggregate; allow-listed deterministic functions; at most one row by construction or a top-level ORDER BY
           over every output column.
  rules7   rules5 plus: no SUM / AVG / variance (rule 6) and no explicit COLLATE (rule 7); used by
           results/analysis_dab_r2. An adversarial review on 2026-10-03 showed that it admits
           `SELECT a, b FROM t GROUP BY a ORDER BY a, b` on SQLite, where b comes from an arbitrary row of each group,
           and a patched blacklist (grouped projections, value domains) admitted eleven further counterexamples;
           the whitelist above replaced it.
Contract E (`strict=False`) keeps the read-only, determinism and allow-list checks of the earlier rules and drops the
ordering and shape checks; its byte stability rests on the pinned runtime.
"""
import datetime
import os
import re
import sys
import json
import warnings

import sqlglot
from sqlglot import exp

_ALLOWED_CLASSES = tuple(c for c in (getattr(exp, n, None) for n in (
    "Count", "Sum", "Avg", "Min", "Max", "Coalesce", "Cast", "TryCast", "Round", "Abs", "Upper", "Lower",
    "Length", "Substring", "Trim", "Concat", "ConcatWs", "Replace", "StrPosition", "Extract", "Nullif",
    "Greatest", "Least", "Floor", "Ceil", "Sqrt", "Pow", "Ln", "Log", "Exp", "Sign", "If", "Case",
    "Left", "Right", "Pad", "RegexpLike", "RegexpExtract", "RegexpReplace", "StrToDate", "StrToTime",
    "TimeToStr", "DateDiff", "DateAdd", "DateSub", "DateTrunc", "TimestampTrunc", "Year", "Month", "Day",
    "Quarter", "Date", "TsOrDsToDate", "TimeStrToTime", "UnixToTime", "JSONExtract", "JSONExtractScalar",
    "Variance", "VariancePop", "Stddev", "StddevPop", "StddevSamp", "Median", "PercentileCont",
    "PercentileDisc", "Quantile", "CountIf", "SafeDivide", "Div", "IsNan", "Initcap", "Reverse",
    "Ascii", "Chr", "Hex", "MD5", "Levenshtein", "StartsWith", "EndsWith", "Contains", "SplitPart",
    "ToChar", "TsOrDsToTimestamp", "RegexpILike", "Like", "ILike", "In", "Between", "Distinct",
    "StrToUnix", "UnixToStr", "Array", "Struct", "Bracket", "JSONBExtract", "JSONBExtractScalar")) if c is not None)
_ALLOWED_ANON = {
    "strftime", "date", "julianday", "substr", "instr", "printf", "ifnull", "iif", "regexp_matches",
    "regexp_extract", "regexp_replace", "split_part", "year", "month", "day", "json_extract", "ltrim",
    "rtrim", "trim", "lower", "upper", "length", "round", "abs", "typeof", "unicode", "char", "hex",
    "total", "contains", "starts_with", "ends_with", "epoch", "date_part", "datepart", "to_char",
    "to_date", "to_timestamp", "log", "ln", "power", "pow", "sqrt", "floor", "ceil", "ceiling", "sign",
    "mod", "levenshtein", "ascii", "strpos", "position", "left", "right", "replace", "reverse", "lpad",
    "rpad", "concat", "concat_ws", "regexp_like", "regexp_substr", "try_cast", "cast", "coalesce",
    "nullif", "greatest", "least", "count", "sum", "avg", "min", "max", "strptime", "date_trunc",
    "make_date", "extract", "json_extract_string", "json_extract_path_text", "lower", "nullif",
    "try_strptime", "regexp_full_match", "string_split", "len", "list_contains", "array_length", "cardinality",
}
_ORDER_SENSITIVE_ANON = {"group_concat", "string_agg", "listagg", "list", "array_agg", "json_group_array",
                         "first", "last", "any_value", "arbitrary", "first_value", "last_value", "nth_value",
                         "row_number", "rank", "dense_rank", "ntile", "lag", "lead", "random", "rand",
                         "now", "uuid", "gen_random_uuid", "current_date", "current_timestamp"}
# SQLite-style temporal functions read the clock when the time-value argument is omitted.
_CLOCK_MIN_ARGS = {"date": 1, "time": 1, "datetime": 1, "julianday": 1, "unixepoch": 1, "strftime": 2,
                   "ts_or_ds_to_date": 1, "time_str_to_time": 1, "str_to_time": 1, "time_to_str": 2,
                   "ts_or_ds_to_timestamp": 1}
_VOLATILE_ANON = {"random", "rand", "now", "uuid", "gen_random_uuid", "current_date", "current_timestamp"}
_ORDER_DEP_CLASSES = tuple(c for c in (getattr(exp, n, None) for n in (
    "GroupConcat", "ArrayAgg", "First", "Last", "AnyValue", "RowNumber", "Rank", "DenseRank", "Ntile",
    "Lag", "Lead", "FirstValue", "LastValue", "NthValue", "ArgMax", "ArgMin")) if c is not None)
_SAFE_WINDOW_AGGS = tuple(getattr(exp, n) for n in ("Count", "Sum", "Avg", "Min", "Max"))
_FLOAT_ORDER_CLASSES = tuple(c for c in (getattr(exp, n, None) for n in (
    "Sum", "Avg", "Variance", "VariancePop", "Stddev", "StddevPop", "StddevSamp")) if c is not None)
_FLOAT_ORDER_ANON = {"sum", "avg", "total", "stddev", "stddev_pop", "stddev_samp", "variance", "var_pop", "var_samp"}
_DIALECT = {"sqlite": "sqlite", "duckdb": "duckdb", "postgres": "postgres"}


def _out_keys(scope):
    """SQL strings by which each output column of a scope can be ordered (expr, and alias if unique)."""
    sel = scope
    while isinstance(sel, exp.SetOperation):
        sel = sel.left
    if not isinstance(sel, exp.Select):
        return None
    names = [p.alias_or_name.lower() for p in sel.expressions]
    keys = []
    for p, name in zip(sel.expressions, names):
        if p.is_star:
            return None
        inner = p.this if isinstance(p, exp.Alias) else p
        k = {inner.sql().lower()}
        if names.count(name) == 1:
            k.add(name)
        keys.append(k)
    return keys


def _order_covers(scope):
    order = scope.args.get("order")
    outs = _out_keys(scope)
    if order is None or outs is None:
        return False
    order_keys = set()
    for o in order.expressions:
        e = o.this if isinstance(o, exp.Ordered) else o
        if isinstance(e, exp.Literal) and e.is_int:
            i = int(e.this) - 1
            if 0 <= i < len(outs):
                order_keys |= outs[i]
            continue
        order_keys.add(e.sql().lower())
        if isinstance(e, exp.Column):
            order_keys.add(e.name.lower())
    return all(k & order_keys for k in outs)


def _has(node, cls):
    """sqlglot's find() does not test the node itself."""
    return isinstance(node, cls) or node.find(cls) is not None


def _under_agg(node, root):
    while node is not None:
        if isinstance(node, exp.AggFunc):
            return True
        if node is root:
            return False
        node = node.parent
    return False


def _at_most_one_row(top):
    """Top-level aggregate without GROUP BY whose every column reference sits under an aggregate."""
    if not isinstance(top, exp.Select) or top.args.get("group") or top.find(exp.Window):
        return False
    for p in top.expressions:
        if not _has(p, exp.AggFunc) or _has(p, exp.Subquery):
            return False
        if any(not _under_agg(c, p) for c in p.find_all(exp.Column)):
            return False
    return bool(top.expressions)


def certify_sql(text, engine, strict=True, rules=7):
    """strict=True: the earlier Contract B (rules 1-5, or 1-7 with rules=7). strict=False: Contract E (exploratory,
    added after the B coverage measurement): byte stability rests on the pinned runtime (engine version, single
    thread, static snapshot); ordering/shape checks are dropped, read-only/volatility/allowlist checks are kept."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stmts = [s for s in sqlglot.parse(text, read=_DIALECT[engine]) if s is not None]
    except Exception:
        return False, "parse_error"
    if len(stmts) != 1:
        return False, "not_single_statement"
    top = stmts[0]
    if not isinstance(top, (exp.Select, exp.SetOperation)):
        return False, "not_select"
    if top.find(exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command):
        return False, "not_read_only"
    if top.find(exp.TableSample):
        return False, "table_sample"
    for lit in top.find_all(exp.Literal):
        if lit.is_string and lit.this.strip().lower() in ("now", "localtime"):
            return False, "volatile_literal"
    for sq in top.find_all(exp.Subquery):
        parent = sq.parent
        if isinstance(parent, (exp.From, exp.Join, exp.In, exp.Exists)):
            continue
        inner = sq.this
        if strict and not _at_most_one_row(inner):
            return False, "scalar_subquery"
    for node in (top.find_all(exp.Limit, exp.Offset) if strict else ()):
        scope = node.parent
        if not isinstance(scope, (exp.Select, exp.SetOperation)) or not _order_covers(scope):
            return False, "unordered_limit"
    for w in (top.find_all(exp.Window) if strict else ()):
        if w.args.get("order") is not None or not isinstance(w.this, _SAFE_WINDOW_AGGS):
            return False, "order_sensitive_window"
    for f in top.find_all(exp.Func):
        if isinstance(f, (exp.Window, exp.Connector, exp.Predicate, exp.Binary, exp.Unary, exp.Paren)):
            continue                                   # boolean/comparison operators, not functions
        fname = (str(f.this) if isinstance(f, exp.Anonymous) else f.sql_name()).lower()
        n_args = sum(1 for _ in f.iter_expressions())
        if fname in _CLOCK_MIN_ARGS and n_args < _CLOCK_MIN_ARGS[fname]:
            return False, f"implicit_now:{fname}"
        if isinstance(f, exp.Anonymous):
            name = str(f.this).lower()
            if name in _VOLATILE_ANON or (strict and name in _ORDER_SENSITIVE_ANON) or \
                    (name not in _ALLOWED_ANON and not (not strict and name in _ORDER_SENSITIVE_ANON)):
                return False, f"function:{name}"
        elif not isinstance(f, _ALLOWED_CLASSES) and not (not strict and isinstance(f, _ORDER_DEP_CLASSES)):
            return False, f"function:{type(f).__name__}"
    if not strict:
        return True, "pinned_runtime"
    shape = "one_row" if _at_most_one_row(top) else "total_order" if _order_covers(top) else None
    if shape is None:
        return False, "unordered_result"
    if rules < 7:
        return True, shape
    if any(isinstance(f, _FLOAT_ORDER_CLASSES) or (isinstance(f, exp.Anonymous) and str(f.this).lower() in _FLOAT_ORDER_ANON)
           for f in top.find_all(exp.Func)):
        return False, "float_aggregate"                # rules 6 and 7 are checked last, so earlier reasons keep their counts
    if top.find(exp.Collate):
        return False, "collate"
    return True, shape


# ----------------------------------------------------------------------------- Contract B (final): a schema-bound whitelist

_B_ARGS = {"expressions", "from_", "from", "joins", "where", "group", "having", "order", "limit", "offset", "distinct"}
_B_ROW = (exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean, exp.Paren, exp.And, exp.Or, exp.Not, exp.EQ,
          exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Is, exp.In, exp.Between, exp.Like, exp.ILike, exp.Add, exp.Sub,
          exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Case, exp.If, exp.Coalesce, exp.Lower, exp.Upper, exp.Length,
          exp.Substring, exp.Trim, exp.Replace, exp.Abs, exp.Round, exp.Cast, exp.TryCast, exp.DataType, exp.DataTypeParam)
_B_ANON = {"lower", "upper", "length", "substr", "trim", "ltrim", "rtrim", "replace", "abs", "round", "coalesce", "ifnull"}
# The arguments a whitelisted node may carry; any other (LENGTH(x, encoding), TRIM ... COLLATE, CAST ... FORMAT,
# BETWEEN SYMMETRIC, ...) is syntax the list above does not describe.
_B_NODE_ARGS = {exp.Length: {"this"}, exp.Trim: {"this", "expression", "position"}, exp.Round: {"this", "decimals"},
                exp.Substring: {"this", "start", "length"}, exp.Cast: {"this", "to"},
                exp.TryCast: {"this", "to", "safe", "requires_string"}, exp.In: {"this", "expressions"},
                exp.Between: {"this", "low", "high"}, exp.Like: {"this", "expression", "negate"},     # negate: NOT LIKE
                exp.ILike: {"this", "expression", "negate"},
                exp.DataType: {"this", "expressions"}, exp.Column: {"this", "table"}, exp.Identifier: {"this", "quoted"}}
# PostgreSQL and DuckDB raise on an overflow, a failing cast or (PostgreSQL) a division by zero or a negative substring
# length, and a plan may or may not evaluate such an expression (short-circuit, join order); SQLite returns NULL or a
# real instead. DuckDB's arithmetic on FLOAT / DOUBLE columns returns inf or nan instead of raising (`float_arith`), and
# DuckDB casts a string column to the other operand's type when the two are compared (see `families`).
_B_MAY_RAISE = (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Neg, exp.Cast, exp.Abs, exp.Round)
_B_MAY_RAISE_ANON = {"abs", "round"}
_B_CAST = {"INT", "BIGINT", "SMALLINT", "TINYINT", "TEXT", "VARCHAR", "CHAR", "FLOAT", "DOUBLE", "DECIMAL"}
_B_AGG = (exp.Count, exp.Min, exp.Max)
_CLOCK_WORDS = {"now", "today", "tomorrow", "yesterday", "localtime", "utc", "current_date", "current_time",
                "current_timestamp", "localtimestamp"}


def _fold(ident, engine):
    """The name an identifier denotes: PostgreSQL keeps the case of quoted names, every other name folds to lower case.
    None for a name with a non-ASCII character: the engines fold only ASCII letters, each in its own way."""
    if not isinstance(ident, exp.Identifier) or not ident.name.isascii():
        return None
    return ident.name if engine == "postgres" and ident.args.get("quoted") else ident.name.lower()


def _clock_literal(n):
    """A string literal with a word that a date parser reads as the current time ('now', 'today 10:00', ...)."""
    return isinstance(n, exp.Literal) and n.is_string and bool(set(re.split(r"[^a-z_]+", n.this.lower())) & _CLOCK_WORDS)


def _may_raise(n, engine):
    """Can evaluating this node raise an error that depends on the row?"""
    if isinstance(n, (exp.Like, exp.ILike)):            # SQLite rejects a pattern over 50,000 bytes; a pattern that
        pat = n.expression                              # ends in the escape character raises in PostgreSQL
        return not (isinstance(pat, exp.Literal) and pat.is_string and len(pat.this) <= 1000
                    and not (engine == "postgres" and "\\" in pat.this))
    if engine == "sqlite":                              # SQLite returns NULL or a real for the rest, except abs(-2^63)
        return isinstance(n, exp.Abs) or (isinstance(n, exp.Anonymous) and str(n.this).lower() == "abs")
    if isinstance(n, exp.Neg):
        return not isinstance(n.this, exp.Literal)
    if type(n) is exp.TryCast:
        return False
    if isinstance(n, exp.Substring) or (isinstance(n, exp.Anonymous) and str(n.this).lower() == "substr"):
        return engine == "postgres"                     # negative substring length
    return isinstance(n, _B_MAY_RAISE) or (isinstance(n, exp.Anonymous) and str(n.this).lower() in _B_MAY_RAISE_ANON)


def _iso_time(text):
    """A well-formed ISO date or timestamp, which every engine converts without error."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?)?", text):
        return False
    try:
        datetime.datetime.fromisoformat(text)
        return int(text[:4]) >= 1
    except ValueError:
        return False


def _row_ok(e, engine, exempt=None):
    """A deterministic expression over one row that cannot raise: only whitelisted nodes (bindings are checked
    separately). exempt(node): the node cannot raise although its class can."""
    for n in e.walk():
        if isinstance(n, exp.Anonymous):
            if str(n.this).lower() not in _B_ANON:
                return False
        elif type(n) not in _B_ROW:
            return False
        if _clock_literal(n) or (_may_raise(n, engine) and not (exempt is not None and exempt(n))):
            return False
        if isinstance(n, exp.Case) and n.args.get("this") is not None:
            return False                                # CASE x WHEN y: an implicit comparison
        if isinstance(n, (exp.Cast, exp.TryCast)) and getattr(n.to.this, "name", "") not in _B_CAST:
            return False
        if type(n) in _B_NODE_ARGS and any(v for k, v in n.args.items() if k not in _B_NODE_ARGS[type(n)]):
            return False
    return True


def certify_sql_b(text, engine, db):
    """Contract B (final). db: the audited database of the query, {"catalog_ok": bool, "tables": {table: {column:
    {"safe": bool, "family": str}}}}, names as stored."""
    if not db or not db.get("catalog_ok"):
        return False, "catalog_precondition"            # not audited, or an object that the query text does not show
    schema = db["tables"]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stmts = [s for s in sqlglot.parse(text, read=_DIALECT[engine]) if s is not None]
    except Exception:
        return False, "parse_error"
    if len(stmts) != 1:
        return False, "not_single_statement"
    top = stmts[0]
    if type(top) is not exp.Select:
        return False, "not_one_select_block"
    if any(v for k, v in top.args.items() if k not in _B_ARGS):
        return False, "clause_not_allowed"              # WITH, QUALIFY, WINDOW, hints, ...
    if top.find(exp.Select, exp.Subquery, exp.SetOperation, exp.Window, exp.Values) is not None and \
            any(n is not top for n in top.find_all(exp.Select, exp.Subquery, exp.SetOperation, exp.Window, exp.Values)):
        return False, "nested_block"
    fold = (lambda n: n) if engine == "postgres" else str.lower
    # ---- FROM: base tables of the schema
    frm = top.args.get("from_") or top.args.get("from")
    if frm is None:
        return False, "no_table"
    scope = {}
    for item, join in [(frm.this, None)] + [(j.this, j) for j in top.args.get("joins") or []]:
        if join is not None and any(v for k, v in join.args.items() if k not in ("this", "on", "kind", "side")):
            return False, "join_not_allowed"            # USING, NATURAL (method), ...
        if type(item) is not exp.Table or type(item.this) is not exp.Identifier or \
                any(v for k, v in item.args.items() if k not in ("this", "db", "alias")):
            return False, "from_not_a_table"
        if item.args.get("db") is not None and item.db.lower() != ("public" if engine == "postgres" else "main"):
            return False, "from_not_a_table"            # another schema or an attached database
        cols = {fold(t): c for t, c in schema.items() if t.isascii()}.get(_fold(item.this, engine))
        if cols is None:
            return False, "unbound_table"
        alias = item.args.get("alias")
        if alias is not None and (type(alias) is not exp.TableAlias or alias.args.get("columns")):
            return False, "from_not_a_table"
        name = _fold(alias.this, engine) if alias is not None else _fold(item.this, engine)
        if name is None or name in scope:
            return False, "duplicate_table_name"
        scope[name] = {fold(c): (c, v["safe"], v.get("family")) for c, v in cols.items() if c.isascii()}

    def bind(col):
        """(table name in scope, stored column name, P2-safe, type family) or None."""
        if type(col) is not exp.Column or col.args.get("db") is not None or col.args.get("catalog") is not None:
            return None
        key = _fold(col.this, engine)
        if key is None:
            return None
        if col.args.get("table") is not None:
            t = _fold(col.args["table"], engine)
            hit = scope.get(t, {}).get(key)
            return (t,) + hit if hit else None
        hits = [(t,) + cols[key] for t, cols in scope.items() if key in cols]
        return hits[0] if len(hits) == 1 else None

    proj = [(p.this, _fold(p.args["alias"], engine)) if isinstance(p, exp.Alias) else (p, None) for p in top.expressions]
    if not proj:
        return False, "no_output"
    if any(isinstance(p, exp.Alias) and _fold(p.args["alias"], engine) is None for p in top.expressions):
        return False, "alias_not_allowed"               # a non-ASCII alias
    in_scope = {c for cols in scope.values() for c in cols}
    if engine == "sqlite" and in_scope & {"true", "false"} and top.find(exp.Boolean) is not None:
        return False, "boolean_names_a_column"          # SQLite reads TRUE as the column when one has that name
    for e, alias in proj:
        if alias is not None and alias in in_scope and not (type(e) is exp.Column and bind(e) and fold(bind(e)[1]) == alias):
            return False, "alias_shadows_column"
    aliases = {a: i for i, (_, a) in enumerate(proj) if a is not None}
    if len(aliases) != sum(1 for _, a in proj if a is not None):
        return False, "duplicate_alias"

    def agg_ok(a):
        if a.find(*_B_AGG) is not None and any(n is not a for n in a.find_all(*_B_AGG)):
            return False                                # nested aggregate
        if not families(a):
            return False
        if any(v for k, v in a.args.items() if k not in ("this", "big_int")):
            return False                                # MAX(a, b) is a scalar function of each row in SQLite
        arg = a.this
        if isinstance(a, exp.Count):
            if type(arg) is exp.Star:
                return True
            if type(arg) is exp.Distinct:
                return not arg.args.get("on") and bool(arg.expressions) and \
                    all(_row_ok(x, engine, float_arith) and all(bind(c) for c in ([x] if isinstance(x, exp.Column) else []) + list(x.find_all(exp.Column)))
                        for x in arg.expressions)
            return arg is not None and _row_ok(arg, engine, float_arith) and all(bind(c) for c in ([arg] if isinstance(arg, exp.Column) else []) + list(arg.find_all(exp.Column)))
        b = bind(arg) if arg is not None else None
        return bool(b and b[2])                         # MIN / MAX of a P2-safe stored column

    def fam(e):
        """Type family of an operand: str, num, time, bool, other for values; strlit, numlit, boollit, null for
        literals; None if unknown."""
        while isinstance(e, exp.Paren):
            e = e.this
        if isinstance(e, exp.Column):
            b = bind(e)
            return b[3] if b else None
        if isinstance(e, exp.Literal):
            return ("timelit" if _iso_time(e.this) else "strlit") if e.is_string else "numlit"
        if isinstance(e, exp.Neg) and isinstance(e.this, exp.Literal) and not e.this.is_string:
            return "numlit"
        if isinstance(e, exp.Boolean):
            return "boollit"
        if isinstance(e, exp.Null):
            return "null"
        if isinstance(e, (exp.Lower, exp.Upper, exp.Trim, exp.Replace, exp.Substring)) or \
                (isinstance(e, exp.Anonymous) and str(e.this).lower() in ("lower", "upper", "trim", "ltrim", "rtrim", "replace", "substr")):
            return "str"
        if float_arith(e):
            return "flt"
        if isinstance(e, (exp.Length, exp.Count)) or (isinstance(e, exp.Anonymous) and str(e.this).lower() == "length"):
            return "num"
        if isinstance(e, (exp.Min, exp.Max)):
            return fam(e.this) if e.this is not None else None
        if type(e) is exp.TryCast:
            return "str" if getattr(e.to.this, "name", "") in ("TEXT", "VARCHAR", "CHAR") else "num"
        return None

    def float_arith(e):
        """DuckDB: arithmetic whose columns are all FLOAT / DOUBLE yields a DOUBLE and cannot raise."""
        if engine != "duckdb":
            return False
        if isinstance(e, exp.Paren):
            return float_arith(e.this)
        if isinstance(e, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
            ops = [e.this, e.expression]
        elif isinstance(e, (exp.Neg, exp.Abs)) or (isinstance(e, exp.Anonymous) and str(e.this).lower() == "abs"):
            ops = [e.this] if not isinstance(e, exp.Anonymous) else list(e.expressions)
        elif isinstance(e, exp.Round) or (isinstance(e, exp.Anonymous) and str(e.this).lower() == "round"):
            ops = [e.this] if not isinstance(e, exp.Anonymous) else list(e.expressions)[:1]
        else:
            return False
        cols = False
        for o in ops:
            while isinstance(o, exp.Paren):
                o = o.this
            if isinstance(o, exp.Column):
                b = bind(o)
                if not b or b[3] != "flt":
                    return False
                cols = True
            elif isinstance(o, exp.Literal):
                if o.is_string:
                    return False
            elif float_arith(o):
                cols = True
            else:
                return False
        return cols                                     # at least one DOUBLE operand makes the result a DOUBLE

    def families(e):
        """DuckDB casts a string to the type of the value it is compared or combined with, and the cast can fail when
        a row is evaluated: a string-valued operand or literal may meet only strings."""
        if engine != "duckdb":
            return True
        for n in ([e] + [x for x in e.walk() if x is not e]):
            if isinstance(n, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
                ops = [n.this, n.expression]
            elif isinstance(n, exp.In):
                ops = [n.this] + list(n.expressions)
            elif isinstance(n, exp.Between):
                ops = [n.this, n.args.get("low"), n.args.get("high")]
            elif isinstance(n, exp.Case):
                ops = [i.args.get("true") for i in n.args.get("ifs", [])] + ([n.args["default"]] if n.args.get("default") is not None else [])
            elif isinstance(n, exp.Coalesce):
                ops = [n.this] + list(n.expressions)
            elif isinstance(n, exp.Anonymous) and str(n.this).lower() in ("coalesce", "ifnull"):
                ops = list(n.expressions)
            else:
                continue
            fs = {fam(o) for o in ops if o is not None}
            if None in fs:
                return False                            # an operand whose type the rule does not know
            if fs & {"str", "strlit", "timelit"} and not (fs <= {"str", "strlit", "timelit", "null"} or
                                                          fs <= {"time", "timelit", "null"}):
                return False                            # a string next to another type: only a well-formed date or
                                                        # timestamp literal next to a date or time column converts safely
        return True

    def row_expr(e):
        return _row_ok(e, engine, float_arith) and families(e) and all(bind(c) for c in ([e] if isinstance(e, exp.Column) else []) + list(e.find_all(exp.Column)))

    def over_aggs(e, keys=(), once=True):
        """Built from aggregates, literals and arithmetic; a column outside an aggregate must be a grouping column.
        once: the expression is evaluated exactly once (a one-row output), so an error does not depend on the plan."""
        if isinstance(e, _B_AGG):
            return agg_ok(e)
        if not once and _may_raise(e, engine) and not float_arith(e):
            return False
        if isinstance(e, exp.Column):
            b = bind(e)
            return b is not None and b[:2] in keys
        if isinstance(e, exp.Anonymous):
            name = str(e.this).lower()
            return name in _B_ANON and all(over_aggs(c, keys, once and name not in ("coalesce", "ifnull"))
                                           for c in e.iter_expressions())
        if type(e) not in _B_ROW or _clock_literal(e):
            return False
        if isinstance(e, (exp.Cast, exp.TryCast)):
            return getattr(e.to.this, "name", "") in _B_CAST and over_aggs(e.this, keys, once)
        if isinstance(e, exp.In) and (e.args.get("query") is not None or e.args.get("unnest") is not None):
            return False
        if isinstance(e, exp.Case) and e.args.get("this") is not None:
            return False
        if isinstance(e, (exp.Case, exp.If, exp.Coalesce, exp.And, exp.Or)):
            once = False                                # an arm may be skipped, or folded while planning
        return all(over_aggs(c, keys, once) for c in e.iter_expressions())

    # ---- clauses that take row expressions
    where = top.args.get("where")
    for cond in ([where.this] if where is not None else []) + [j.args["on"] for j in top.args.get("joins") or [] if j.args.get("on") is not None]:
        if cond.find(*_B_AGG) is not None or isinstance(cond, _B_AGG) or not row_expr(cond):
            return False, "predicate_not_allowed"
    for k in ("limit", "offset"):
        n = top.args.get(k)
        if n is not None:
            v = n.args.get("expression")
            if type(v) is not exp.Literal or not v.is_int or any(x for kk, x in n.args.items() if kk != "expression"):
                return False, "limit_not_literal"
            if k == "limit" and int(v.this) < 1:
                return False, "limit_not_literal"       # LIMIT 0 lets a plan skip every expression that could raise
    distinct = top.args.get("distinct")
    if distinct is not None and (distinct.args.get("on") is not None or distinct.expressions):
        return False, "distinct_on"
    group, having, order = top.args.get("group"), top.args.get("having"), top.args.get("order")
    has_agg = any(e.find(*_B_AGG) is not None or isinstance(e, _B_AGG) for e, _ in proj)

    def order_cover(keys, agg_proj):
        """(indexes of the outputs, stored columns) that the ORDER BY names, or None if a term is not allowed. With
        DISTINCT every term must be an output: the rows it removes would otherwise supply the ordering value."""
        covered, named = set(), set()
        for o in order.expressions:
            if type(o) is not exp.Ordered or any(v for k, v in o.args.items() if k not in ("this", "desc", "nulls_first")):  # WITH FILL
                return None
            t = o.this
            if type(t) is exp.Literal and t.is_int:
                i = int(t.this) - 1
                if not 0 <= i < len(proj):
                    return None
            elif type(t) is exp.Column and bind(t):
                b = bind(t)[:2]
                if keys is not None and b not in keys:
                    return None                         # a grouped block may order by its grouping columns only
                hits = {i for i, (e, _) in enumerate(proj) if type(e) is exp.Column and bind(e) and bind(e)[:2] == b}
                if distinct is not None and not hits:
                    return None
                named.add(b)
                covered |= hits
                continue
            elif type(t) is exp.Column and t.args.get("table") is None and _fold(t.this, engine) in aliases:
                i = aliases[_fold(t.this, engine)]
            elif isinstance(t, _B_AGG) and t.sql() in agg_proj:
                i = agg_proj[t.sql()]
            else:
                return None
            covered.add(i)
            if type(proj[i][0]) is exp.Column and bind(proj[i][0]):
                named.add(bind(proj[i][0])[:2])
        return covered, named

    # ---- shape: one row
    if group is None and has_agg:
        if having is not None or order is not None or top.args.get("offset") is not None or distinct is not None:
            return False, "one_row_clause_not_allowed"
        if not all(over_aggs(e) and families(e) for e, _ in proj):
            return False, "one_row_output_not_allowed"
        return True, "one_row"
    # ---- shape: ordered rows of stored columns
    if group is None:
        if having is not None:
            return False, "having_without_group"
        bound = [bind(e) if type(e) is exp.Column else None for e, _ in proj]
        if not all(bound):
            return False, "output_not_a_stored_column"
        if not all(b[2] for b in bound):
            return False, "p2_unsafe_column"
        if order is None:
            return False, "unordered_result"
        cov = order_cover(None, {})
        if cov is None:
            return False, "order_term_not_allowed"
        return (True, "total_order") if cov[0] == set(range(len(proj))) else (False, "unordered_result")
    # ---- shape: grouped
    if any(group.args.get(k) for k in group.args if k != "expressions"):
        return False, "group_not_allowed"
    keys = set()
    for g in group.expressions:
        if type(g) is exp.Literal and g.is_int and 0 <= int(g.this) - 1 < len(proj):
            g = proj[int(g.this) - 1][0]
        b = bind(g) if type(g) is exp.Column else None
        if b is None:
            return False, "group_key_not_a_stored_column"
        if not b[2]:
            return False, "p2_unsafe_column"
        keys.add(b[:2])
    agg_proj = {}
    for i, (e, _) in enumerate(proj):
        if isinstance(e, _B_AGG):
            if not agg_ok(e):
                return False, "aggregate_not_allowed"
            agg_proj[e.sql()] = i
        elif not (type(e) is exp.Column and bind(e) and bind(e)[:2] in keys):
            return False, "grouped_output_not_allowed"  # a bare column, or arithmetic on aggregates
    if having is not None:
        h = having.this
        for c in ([h] if isinstance(h, exp.Column) else []) + list(h.find_all(exp.Column)):   # an output alias stands for
            if c.args.get("table") is None and not bind(c) and _fold(c.this, engine) in aliases:  # its aggregate
                c.replace(proj[aliases[_fold(c.this, engine)]][0].copy())
        if not over_aggs(having.this, keys, once=False) or not families(having.this):
            return False, "having_not_allowed"
    if order is None:
        return False, "unordered_result"
    cov = order_cover(keys, agg_proj)
    if cov is None:
        return False, "order_term_not_allowed"
    # one row per combination of grouping columns: naming all of them, or every output, orders the rows totally
    total = cov[0] == set(range(len(proj))) or (distinct is None and cov[1] >= keys)
    return (True, "total_order") if total else (False, "unordered_result")


def certify_mongo(text, strict=True):
    try:
        q = json.loads(text)
    except Exception:
        return False, "parse_error"
    if not isinstance(q, dict):
        return False, "not_find_spec"
    f = q.get("filter") or {}
    id_lookup = isinstance(f, dict) and "_id" in f and not isinstance(f["_id"], (dict, list))
    if not strict:
        return True, ("id_lookup" if id_lookup else "pinned_runtime")
    if not id_lookup:
        return False, "mongo_no_sort"
    proj = q.get("projection")
    plain = (set(q) <= {"collection", "filter", "projection", "limit"} and len(f) == 1
             and (proj is None or (isinstance(proj, dict) and all(v in (0, 1, True, False) for v in proj.values()))))
    return (True, "id_lookup") if plain else (False, "id_lookup_not_plain")


def load_schema(path):
    """{(dataset, database): {"catalog_ok", "tables": {table: {column: {"safe", "family"}}}}} from the merged output
    of p2_audit.py."""
    return {(ds, db): {"catalog_ok": entry["catalog_ok"],
                       "tables": {t: {c: {"safe": v["safe"], "family": v.get("family")} for c, v in cols.items()}
                                  for t, cols in entry["tables"].items()}}
            for ds, dbs in json.load(open(path))["datasets"].items() for db, entry in dbs.items()}


def certify(text, engine, strict=True, schema=None):
    """Contract B (strict; needs the audited schema of the query's database) or Contract E (strict=False).
    ED_CONTRACT_B=rules5 or rules7 selects an earlier Contract B, which needs no schema."""
    if engine == "mongo":
        return certify_mongo(text, strict)
    rule = os.environ.get("ED_CONTRACT_B", "final")
    if not strict or rule in ("rules5", "rules7"):
        return certify_sql(text, engine, strict, rules=5 if rule == "rules5" else 7)
    return certify_sql_b(text, engine, schema)


if __name__ == "__main__":
    # ---- the earlier rule (rules 1-7), kept for the earlier results
    legacy = [
        ("SELECT COUNT(*) FROM t", "sqlite", True),
        ("SELECT a, b FROM t ORDER BY a, b", "duckdb", True),
        ("SELECT a, b FROM t ORDER BY a", "duckdb", False),
        ("SELECT a FROM t LIMIT 5", "sqlite", False),
        ("SELECT SUM(x) FROM (SELECT x FROM t LIMIT 10) s", "duckdb", False),
        ("SELECT MAX(x) FROM (SELECT x FROM t ORDER BY x LIMIT 10) s", "duckdb", True),
        ("SELECT SUM(x) FROM (SELECT x FROM t ORDER BY x LIMIT 10) s", "duckdb", False),
        ("SELECT * FROM t", "sqlite", False),
        ("SELECT group_concat(a) FROM t", "sqlite", False),
        ("SELECT strftime('%Y', 'now')", "sqlite", False),
        ("SELECT AVG(r) AS m FROM t WHERE y > 2000", "postgres", False),
        ("SELECT MIN(r), MAX(r), COUNT(*) FROM t WHERE y > 2000", "postgres", True),
        ("SELECT a, SUM(x) AS s FROM t GROUP BY a ORDER BY a, s", "duckdb", False),
        ("SELECT total(x) FROM t", "sqlite", False),
        ("SELECT median(x) FROM t", "duckdb", True),
        ("SELECT a FROM t WHERE b > (SELECT AVG(b) FROM t) ORDER BY a", "sqlite", False),
        ("SELECT a FROM t ORDER BY a COLLATE NOCASE", "sqlite", False),
        ("SELECT MIN(a COLLATE NOCASE) FROM t", "sqlite", False),
        ("SELECT a, COUNT(*) c FROM t GROUP BY a ORDER BY c DESC", "duckdb", False),
        ("SELECT a, COUNT(*) c FROM t GROUP BY a ORDER BY c DESC, a", "duckdb", True),
        ("SELECT row_number() OVER (ORDER BY a) FROM t", "duckdb", False),
        ("SELECT a FROM t ORDER BY 1", "sqlite", True),
        ("INSERT INTO t VALUES (1)", "sqlite", False),
        ("SELECT random()", "duckdb", False),
        ("SELECT date() ORDER BY 1", "sqlite", False),
        ("SELECT strftime('%Y')", "sqlite", False),
        ("SELECT SUM(x) FROM t TABLESAMPLE SYSTEM (10)", "postgres", False),
        ("SELECT COUNT(*), name || MIN('') FROM t", "sqlite", False),
        ("SELECT a AS x, b AS x FROM t ORDER BY x", "duckdb", False),
        ("SELECT COUNT(*), (SELECT b FROM u) FROM t", "sqlite", False),
        ("SELECT strftime('%Y', d) AS y, COUNT(*) AS c FROM t GROUP BY y ORDER BY y, c", "sqlite", True),
        ("SELECT COUNT(*) FROM t WHERE a = 1 AND (b LIKE 'x%' OR c IN (1, 2)) AND d BETWEEN 1 AND 5", "postgres", True),
        ("SELECT a FROM t WHERE a > 1 AND b < 2 ORDER BY a", "duckdb", True),
        ("SELECT (SELECT a FROM t) AS x ORDER BY 1", "sqlite", False),
        ("SELECT a FROM t WHERE b = (SELECT MAX(b) FROM t) ORDER BY a", "sqlite", True),
        ("SELECT a FROM t WHERE b IN (SELECT b FROM u) ORDER BY a", "duckdb", True),
    ]
    bad = 0
    for q, e, want in legacy:
        got, why = certify_sql(q, e, True, rules=7)
        bad += got != want
        print(f"{'ok ' if got == want else 'BAD'} rules7:{got!s:5} {why:24} {q}")
    for q, e, want5 in [("SELECT AVG(r) AS m FROM t WHERE y > 2000", "postgres", True),
                        ("SELECT MIN(a COLLATE NOCASE) FROM t", "sqlite", True),
                        ("SELECT a FROM t LIMIT 5", "sqlite", False)]:
        got, why = certify_sql(q, e, True, rules=5)
        bad += got != want5
        print(f"{'ok ' if got == want5 else 'BAD'} rules5:{got!s:5} {why:24} {q}")
    # ---- Contract B (final): the whitelist, on a small schema; x fails the P2 audit
    def db(tables, catalog_ok=True):
        return {"catalog_ok": catalog_ok, "tables": {t: {c: {"safe": safe, "family": f} for c, (safe, f) in cols.items()}
                                                     for t, cols in tables.items()}}
    S = db({"t": {"a": (True, "num"), "b": (True, "num"), "c": (True, "str"), "x": (False, "flt"), "y": (True, "num"),
                  "d": (True, "str"), "e": (True, "time"), "f": (True, "flt"), "g": (True, "flt")},
            "u": {"a": (True, "num"), "z": (True, "num")}})
    PG = db({"t": {"a": (True, "num"), "Mixed": (True, "str")}})
    final = [
        ("SELECT COUNT(*) FROM t", "sqlite", S, True),
        ("SELECT COUNT(*) FROM t WHERE a = 1 AND (c LIKE 'x%' OR b IN (1, 2)) AND y BETWEEN 1 AND 5", "postgres", S, True),
        ("SELECT COUNT(DISTINCT a), MIN(b), MAX(c) FROM t", "duckdb", S, True),
        ("SELECT COUNT(*) * 100.0 / COUNT(a) FROM t", "sqlite", S, True),
        ("SELECT ROUND(MAX(b) - MIN(b), 2) AS r, COUNT(*) FROM t WHERE x > 0.5", "duckdb", S, True),
        ("SELECT COUNT(CASE WHEN a > 1 THEN 1 END) FROM t", "duckdb", S, True),
        ("SELECT COUNT(*), 1 FROM t LIMIT 5", "sqlite", S, True),
        ("SELECT a, c FROM t ORDER BY a, c", "duckdb", S, True),
        ("SELECT DISTINCT c FROM t ORDER BY c", "sqlite", S, True),
        ("SELECT a, c FROM t WHERE b > 2 ORDER BY 2, 1 LIMIT 10 OFFSET 2", "sqlite", S, True),
        ("SELECT t.a, u.z FROM t JOIN u ON t.a = u.a ORDER BY t.a, u.z", "postgres", S, True),
        ("SELECT m.a AS k, n.z FROM t AS m LEFT JOIN u AS n ON m.a = n.a ORDER BY k, n.z", "duckdb", S, True),
        ("SELECT c, COUNT(*) FROM t GROUP BY c ORDER BY c", "sqlite", S, True),
        ("SELECT c, COUNT(*) AS n FROM t GROUP BY c ORDER BY n DESC, c", "duckdb", S, True),
        ("SELECT c, COUNT(*) FROM t GROUP BY 1 HAVING COUNT(*) > 2 ORDER BY 2 DESC, 1", "postgres", S, True),
        ("SELECT c, COUNT(*) AS n FROM t GROUP BY c HAVING n > 2 ORDER BY COUNT(*) DESC, c", "sqlite", S, True),
        ("SELECT c, MAX(b) FROM t GROUP BY c ORDER BY c", "duckdb", S, True),
        ('SELECT "Mixed" FROM t ORDER BY "Mixed"', "postgres", PG, True),
        # grouping and aggregation
        ("SELECT a, b FROM t GROUP BY a ORDER BY a, b", "sqlite", S, False),
        ("SELECT MAX(a), y FROM t", "sqlite", S, False),
        ("SELECT MAX(a, b) FROM t", "sqlite", S, False),
        ("SELECT SUM(a) FROM t", "sqlite", S, False),
        ("SELECT AVG(a) FROM t", "postgres", S, False),
        ("SELECT a AS b, COUNT(*) AS n FROM t GROUP BY b ORDER BY b, n", "sqlite", S, False),
        ("SELECT a, COUNT(*) AS b FROM t GROUP BY a HAVING b > 0 ORDER BY a, b", "sqlite", S, False),
        ("SELECT c, COUNT(*) * 2 FROM t GROUP BY c ORDER BY 1, 2", "duckdb", S, False),
        ("SELECT c, COUNT(*) FROM t GROUP BY c", "sqlite", S, False),
        ("SELECT c, COUNT(*) AS n FROM t GROUP BY c ORDER BY n DESC", "sqlite", S, False),
        ("SELECT c, COUNT(*) FROM t GROUP BY c ORDER BY c, a", "sqlite", S, False),
        ("SELECT COUNT(*) FROM t GROUP BY c ORDER BY 1", "sqlite", S, True),
        ("SELECT a, c, COUNT(*) FROM t GROUP BY a, c ORDER BY a", "duckdb", S, False),
        ("SELECT a, c, COUNT(*) FROM t GROUP BY a, c ORDER BY c, a", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t HAVING COUNT(*) > 1", "sqlite", S, False),
        # ordering and shape
        ("SELECT a, c FROM t ORDER BY a", "duckdb", S, False),
        ("SELECT a FROM t LIMIT 5", "sqlite", S, False),
        ("SELECT * FROM t ORDER BY a", "sqlite", S, False),
        ("SELECT a FROM t ORDER BY a COLLATE NOCASE", "sqlite", S, False),
        ("SELECT a, a * 2 AS v FROM t ORDER BY a, v", "duckdb", S, False),
        ("SELECT a FROM t ORDER BY a LIMIT b", "sqlite", S, False),
        # value domains (P2)
        ("SELECT a, x FROM t ORDER BY a, x", "duckdb", S, False),
        ("SELECT MIN(x) FROM t", "sqlite", S, False),
        ("SELECT x, COUNT(*) FROM t GROUP BY x ORDER BY x", "sqlite", S, False),
        ("SELECT MAX(a * 2) FROM t", "sqlite", S, False),
        # the clock
        ("SELECT MAX(date(d)) FROM t", "sqlite", S, False),
        ("SELECT COUNT(*) FROM t WHERE d > date('now')", "sqlite", S, False),
        ("SELECT COUNT(*) FROM t WHERE d > 'now'", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE d > 'today 10:00'", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE d > CURRENT_DATE", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE strftime('%Y', d) = '2020'", "sqlite", S, False),
        ("SELECT random()", "duckdb", S, False),
        # blocks and bindings
        ("SELECT MAX(a) FROM (SELECT b AS a FROM t ORDER BY t.a LIMIT 1) s", "postgres", S, False),
        ("WITH s(y) AS (SELECT x / 2 FROM t) SELECT y FROM s GROUP BY y ORDER BY y", "sqlite", S, False),
        ("SELECT v.a FROM t CROSS JOIN (VALUES (1), (2)) AS v(a) GROUP BY v.a ORDER BY v.a", "postgres", S, False),
        ("SELECT a, MIN(b) OVER (ROWS BETWEEN 1 PRECEDING AND CURRENT ROW) AS m FROM t ORDER BY a, m", "postgres", S, False),
        ("SELECT a FROM t UNION SELECT a FROM u ORDER BY a", "duckdb", S, False),
        ("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE a IN (SELECT a FROM u)", "duckdb", S, False),
        ("SELECT a FROM t JOIN u USING (a) ORDER BY a", "duckdb", S, False),
        ("SELECT a FROM t NATURAL JOIN u ORDER BY a", "sqlite", S, False),
        ("SELECT a FROM t, u ORDER BY a", "sqlite", S, False),
        ("SELECT q FROM t ORDER BY q", "sqlite", S, False),
        ("SELECT a FROM nosuch ORDER BY a", "sqlite", S, False),
        ("SELECT a FROM t ORDER BY rowid", "sqlite", S, False),
        ('SELECT "A" FROM t ORDER BY "A"', "postgres", PG, False),
        ("SELECT mixed FROM t ORDER BY mixed", "postgres", PG, False),
        ("SELECT COUNT(*) FROM read_csv_auto('f.csv')", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t TABLESAMPLE SYSTEM (10)", "postgres", S, False),
        ("SELECT COUNT(*) FILTER (WHERE b > 0) FROM t", "postgres", S, False),
        ("INSERT INTO t VALUES (1)", "sqlite", S, False),
        ("SELECT COUNT(*) FROM t; SELECT COUNT(*) FROM u", "sqlite", S, False),
        # second adversarial review
        ("SELECT a FROM public.t ORDER BY a", "sqlite", S, False),
        ("SELECT a FROM public.t ORDER BY a", "postgres", S, True),
        ("SELECT a FROM main.t ORDER BY a", "sqlite", S, True),
        ("SELECT MIN(a) + TRUE FROM t", "sqlite", db({"t": {"a": (True, "num"), "true": (True, "num")}}), False),
        ("SELECT MIN(a) + TRUE FROM t", "sqlite", S, True),
        ("SELECT DISTINCT COUNT(*) FROM t GROUP BY a ORDER BY a", "sqlite", S, False),
        ("SELECT DISTINCT a FROM t ORDER BY b, a", "sqlite", S, False),
        ("SELECT a FROM t ORDER BY b, a", "sqlite", S, True),
        ("SELECT COUNT(*) FROM t WHERE a <> 0 AND 10 / a > 1", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE a <> 0 AND 10 / a > 1", "sqlite", S, True),
        ("SELECT COUNT(*) FROM t WHERE CAST(c AS INTEGER) > 1", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE c LIKE 'x\\'", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE a > -1", "postgres", S, True),
        ("SELECT c, COUNT(*) FROM t GROUP BY c HAVING COUNT(*) / MIN(b) > 1 ORDER BY c", "duckdb", S, False),
        ("SELECT COUNT(*) * 100.0 / COUNT(a) FROM t", "postgres", S, True),
        # third adversarial review
        ("SELECT a FROM t ORDER BY a", "postgres", db({"t": {"a": (True, "num")}}, catalog_ok=False), False),
        ("SELECT COUNT(*) FROM t JOIN u ON t.a = u.a AND t.c = t.a", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE c = 5", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE c = 'x' AND a = 5", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t WHERE c IN ('x', 1)", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE LOWER(c) = 'x' AND LENGTH(c) > 3", "duckdb", S, True),
        ("SELECT COUNT(CASE WHEN a > 1 THEN c ELSE 0 END) FROM t", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE TRY_CAST(c AS INTEGER) > 0", "duckdb", S, True),
        ("SELECT c, COUNT(*) FROM t GROUP BY c HAVING MIN(c) > 5 ORDER BY c", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t JOIN u ON t.a = u.a AND ABS(t.b) > 0", "sqlite", S, False),
        ("SELECT COUNT(*) FROM t WHERE c LIKE d", "sqlite", S, False),
        ("SELECT 1 / COUNT(*) FROM t LIMIT 0", "postgres", S, False),
        ("SELECT 1 / COUNT(*) FROM t LIMIT 1", "postgres", S, True),
        # fourth adversarial review
        ("SELECT a FROM t ORDER BY a LIMIT 0", "sqlite", S, False),
        ("SELECT a FROM t WHERE a = 'x' ORDER BY a", "duckdb", S, False),
        ("SELECT a FROM t WHERE a IN ('1') ORDER BY a", "duckdb", S, False),
        ("SELECT a FROM t WHERE a = 'x' ORDER BY a", "postgres", S, True),
        ("SELECT COUNT(*) FROM t WHERE e >= '2019-01-01' AND e <= '2019-12-31'", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t WHERE e BETWEEN 'not-a-date' AND '2019-13-45'", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE c >= '2019-01-01'", "duckdb", S, True),
        ("SELECT a FROM t WHERE CASE a WHEN 1 THEN TRUE ELSE FALSE END ORDER BY a", "duckdb", S, False),
        ("SELECT c, COUNT(*) FROM t GROUP BY c HAVING COUNT(*) = 'x' ORDER BY c", "duckdb", S, False),
        ("SELECT CASE WHEN COUNT(*) >= 0 THEN 1 ELSE 1 / 0 END FROM t", "postgres", S, False),
        ("SELECT COALESCE(MAX(a), 0), CASE WHEN COUNT(*) > 0 THEN 'some' ELSE 'none' END FROM t", "postgres", S, True),
        ("SELECT COUNT(*) FROM t WHERE c LIKE '" + "a" * 2000 + "'", "sqlite", S, False),
        # fifth adversarial review
        ("SELECT MIN(\u00c4) FROM t", "duckdb", db({"t": {"\u00c4": (False, "flt"), "\u00e4": (True, "num")}}), False),
        ("SELECT \u00e4 FROM t ORDER BY \u00e4", "sqlite", db({"t": {"\u00e4": (True, "num")}}), False),
        ("SELECT COUNT(*) > 0 OR 1 / MIN(a) > 0 FROM t", "postgres", S, False),
        ("SELECT a AS \u00e4 FROM t ORDER BY a", "sqlite", S, False),
        # sixth adversarial review
        ("SELECT COUNT(*) FROM t WHERE a <> 0 AND LENGTH(c, 'UTF8') > 0", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE a <> 0 AND LENGTH(c) > 0", "postgres", S, True),
        ("SELECT COUNT(*) FROM t WHERE a IN (SELECT a FROM t)", "sqlite", S, False),
        ("SELECT COUNT(*) FROM t WHERE c NOT LIKE 'a%'", "sqlite", S, True),
        ("SELECT COUNT(*) FROM t WHERE c NOT LIKE 'a%'", "postgres", S, True),
        ("SELECT COUNT(*) FROM t WHERE c NOT ILIKE '%a%' AND c NOT IN ('x', 'y') AND a NOT BETWEEN 1 AND 2", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t WHERE c NOT LIKE '" + "a" * 2000 + "'", "sqlite", S, False),
        ("SELECT COUNT(*) > 0 AND MAX(a) > 3 FROM t", "postgres", S, True),
        # operations that cannot raise
        ("SELECT COUNT(*) FROM t WHERE g > 0 AND ((f - g) / g * 100) > 20", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t WHERE g > 0 AND ((f - g) / g * 100) > 20", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE a * 2 > 20", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE f * a > 20", "duckdb", S, False),
        ("SELECT COUNT(*) FROM t WHERE ABS(f - g) > 1 AND ROUND(f, 2) = 1.5", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t WHERE REPLACE(c, '#', '') = 'x' AND SUBSTR(c, 1, 2) = 'ab'", "duckdb", S, True),
        ("SELECT COUNT(*) FROM t WHERE REPLACE(c, '#', '') = 'x'", "postgres", S, True),
        ("SELECT COUNT(*) FROM t WHERE SUBSTR(c, 1, a) = 'ab'", "postgres", S, False),
        ("SELECT COUNT(*) FROM t WHERE REPLACE(c, '#', '') = 5", "duckdb", S, False),
    ]
    for q, e, schema, want in final:
        got, why = certify(q, e, schema=schema)
        bad += got != want
        print(f"{'ok ' if got == want else 'BAD'} B:{got!s:5} {why:28} {q}")
    for q, want in (('{"collection": "b", "filter": {"_id": "x"}}', True), ('{"collection": "b", "limit": 5}', False),
                    ('{"collection": "b", "filter": {"_id": "x"}, "projection": {"name": 1}}', True),
                    ('{"collection": "b", "filter": {"_id": "x", "n": {"$gt": 1}}}', False),
                    ('{"collection": "b", "filter": {"_id": "x"}, "projection": {"r": {"$rand": {}}}}', False)):
        got, why = certify(q, "mongo")
        bad += got != want
        print(f"{'ok ' if got == want else 'BAD'} {got!s:5} {why:24} mongo {q}")
    for q, e, want_e in [("SELECT a FROM t LIMIT 5", "sqlite", True), ("SELECT group_concat(a) FROM t", "sqlite", True),
                         ("SELECT AVG(r) AS m FROM t WHERE y > 2000", "postgres", True),
                         ("SELECT random()", "duckdb", False), ("SELECT date() ORDER BY 1", "sqlite", False),
                         ("SELECT SUM(x) FROM t TABLESAMPLE SYSTEM (10)", "postgres", False)]:
        got, why = certify(q, e, strict=False)
        bad += got != want_e
        print(f"{'ok ' if got == want_e else 'BAD'} E:{got!s:5} {why:24} {q}")
    n = len(final) + len(legacy) + 3 + 5 + 6
    print(f"unit tests: {n} assertions ({len(final)} SQL B, 5 Mongo B, 6 E, {len(legacy) + 3} earlier rules), failures: {bad}")
    sys.exit(1 if bad else 0)
