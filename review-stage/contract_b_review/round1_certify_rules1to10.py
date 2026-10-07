"""Contract B certificate: is a query's serialized result byte-stable on a pinned snapshot?

Conservative static check on the query text only (deployable at runtime):
  1. single read-only statement;
  2. every LIMIT/OFFSET sits in a scope whose ORDER BY covers that scope's outputs;
  3. no order-sensitive window or aggregate (string_agg, group_concat, first, ...);
  4. every function is on a deterministic allowlist; no 'now' literals;
  5. the result has at most one row by construction, or the top-level ORDER BY
     covers every output column;
  6. no SUM / AVG / variance / standard deviation: their floating-point value depends on
     the order of accumulation, which the query text does not fix (added 2026-10-03; the
     analyses under results/analysis_dab_r1 and the files named *_certv1 used rules 1-5);
  7. no explicit COLLATE clause: the byte-stability argument needs comparisons that are
     injective on the values, which a non-binary collation is not (added 2026-10-03).
Rules 8-10 were added on 2026-10-03 after an adversarial review found that rules 1-7 admit
`SELECT a, b FROM t GROUP BY a ORDER BY a, b` on SQLite, where b comes from an arbitrary row of each group
(the analyses under results/analysis_dab_r2 and the files named *_certv2 used rules 1-7):
  8. in a block with GROUP BY or an aggregate, every column outside the aggregates belongs to a grouping
     expression (select list, HAVING, ORDER BY); no window function or subquery in such a block's select list;
  9. no set operation and no DISTINCT ON;
 10. value domains: wherever the engine may pick one of several values that compare equal (the argument of MIN /
     MAX / MEDIAN, the outputs of a block with DISTINCT, GROUP BY, LIMIT or OFFSET) or may order rows that
     compare equal (the outputs of an ordered multi-row result), the expression must be one whose equal values
     always print the same: a stored column that passed the snapshot audit (p2_audit.py), COUNT, MIN / MAX of
     such an expression, a string-, integer-, boolean- or date-valued function or cast, a literal, or a
     conditional whose branches are string or integer literals or such functions. Arithmetic, rounding, casts
     to floating-point or decimal types, JSON extraction and percentile aggregates are not.
MongoDB (DAB interface: collection/filter/projection/limit, no sort): only a find whose
filter is exactly an equality on `_id` (at most one document) and whose projection, if any,
only includes or excludes fields is certified.
"""
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
# Rule 10: expressions whose equal values always print the same, whatever their arguments.
_SAFE_VALUE_CLASSES = tuple(c for c in (getattr(exp, n, None) for n in (
    "Count", "CountIf", "Upper", "Lower", "Length", "Substring", "Trim", "Concat", "ConcatWs", "DPipe", "Replace",
    "StrPosition", "Left", "Right", "Pad", "Initcap", "Reverse", "RegexpReplace", "RegexpExtract", "RegexpLike",
    "RegexpILike", "Like", "ILike", "In", "Between", "StartsWith", "EndsWith", "Contains", "TimeToStr", "Year",
    "Month", "Day", "Quarter", "Extract", "Date", "DateTrunc", "TimestampTrunc", "TsOrDsToDate", "TimeStrToTime",
    "StrToDate", "StrToTime", "UnixToStr", "StrToUnix", "DateDiff", "Ascii", "Chr", "Hex", "MD5", "Levenshtein",
    "SplitPart", "ToChar", "Predicate", "Connector", "Not", "Exists")) if c is not None)
_SAFE_VALUE_ANON = {
    "count", "strftime", "date", "substr", "instr", "printf", "trim", "ltrim", "rtrim", "lower", "upper", "length",
    "len", "typeof", "unicode", "char", "hex", "replace", "reverse", "lpad", "rpad", "concat", "concat_ws",
    "regexp_extract", "regexp_replace", "regexp_matches", "regexp_like", "regexp_full_match", "regexp_substr",
    "split_part", "left", "right", "strpos", "position", "to_char", "to_date", "starts_with", "ends_with",
    "contains", "year", "month", "day", "extract", "date_trunc", "make_date", "levenshtein", "ascii",
    "list_contains", "array_length", "cardinality"}
_SAFE_CAST = {"TEXT", "VARCHAR", "CHAR", "NCHAR", "NVARCHAR", "INT", "BIGINT", "SMALLINT", "TINYINT", "BOOLEAN",
              "DATE", "DATETIME", "TIMESTAMP", "TIMESTAMPTZ"}
_BRANCH_ANON = {"ifnull": slice(0, None), "coalesce": slice(0, None), "iif": slice(1, None), "nullif": slice(0, 1),
                "greatest": slice(0, None), "least": slice(0, None)}
_PERCENTILE_CLASSES = tuple(c for c in (getattr(exp, n, None) for n in (
    "PercentileCont", "PercentileDisc", "Quantile", "ApproxQuantile")) if c is not None)


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


def _selects(top):
    """Every SELECT block of the statement, the top-level one first."""
    out = []
    for s_ in ([top] if isinstance(top, exp.Select) else []) + list(top.find_all(exp.Select)):
        if not any(s_ is o for o in out):
            out.append(s_)
    return out


def _own_aggregates(sel):
    """Aggregates of this block: not inside a window and not inside a nested block."""
    for part in list(sel.expressions) + [sel.args.get(k) for k in ("having", "order", "qualify")]:
        if part is None:
            continue
        for a in ([part] if isinstance(part, exp.AggFunc) else []) + list(part.find_all(exp.AggFunc)):
            p, own = a.parent, True
            while p is not None and p is not sel:
                if isinstance(p, (exp.Window, exp.Select)):
                    own = False
                    break
                p = p.parent
            if own:
                yield a


def _bare_column(sel):
    """Rule 8: does a grouped or aggregated block use a column that is neither aggregated nor a grouping expression?"""
    group = sel.args.get("group")
    if group is None and not any(True for _ in _own_aggregates(sel)):
        return False
    if group is not None and any(group.args.get(k) for k in ("all", "rollup", "cube", "grouping_sets", "totals")):
        return True
    aliases = {p.alias.lower(): p.this for p in sel.expressions if isinstance(p, exp.Alias)}
    keys = []
    for k in (group.expressions if group is not None else []):
        if isinstance(k, exp.Literal) and k.is_int:
            i = int(k.this) - 1
            if not 0 <= i < len(sel.expressions):
                return True
            k = sel.expressions[i]
            k = k.this if isinstance(k, exp.Alias) else k
        elif isinstance(k, exp.Column) and not k.table and k.name.lower() in aliases:
            k = aliases[k.name.lower()]                 # GROUP BY <output alias>: the aliased expression is the key
        keys.append(k)
    key_sql = {k.sql().lower() for k in keys}
    key_cols = [k for k in keys if isinstance(k, exp.Column)]

    def ok(node, in_order):
        if node.sql().lower() in key_sql:
            return True
        if isinstance(node, exp.AggFunc):
            return True
        if isinstance(node, (exp.Subquery, exp.Select, exp.Window, exp.Star)):
            return False
        if isinstance(node, exp.Column):
            if in_order and not node.table and node.name.lower() in aliases:
                return True                             # ORDER BY / HAVING on an output alias, checked as a projection
            return any(k.name.lower() == node.name.lower() and
                       (not k.table or not node.table or k.table.lower() == node.table.lower()) for k in key_cols)
        return all(ok(c, in_order) for c in node.iter_expressions())

    if not all(ok(p.this if isinstance(p, exp.Alias) else p, False) for p in sel.expressions):
        return True
    for k in ("having", "order", "qualify"):
        part = sel.args.get(k)
        if part is not None and not all(ok(c, True) for c in part.iter_expressions()):
            return True
    return False


def _value_safe(e, p2, aliases, seen=frozenset()):
    """Rule 10: do equal values of this expression always print the same?"""
    while isinstance(e, (exp.Alias, exp.Paren, exp.Ordered)):
        e = e.this
    if isinstance(e, exp.Column):
        n = e.name.lower()
        if n in aliases and n not in seen:              # an output alias of some block of this statement
            return all(_value_safe(x, p2, aliases, seen | {n}) for x in aliases[n]) and \
                (p2 is None or n not in p2["unsafe"])
        return p2 is None or n in p2["safe"]
    if isinstance(e, (exp.Literal, exp.Null, exp.Boolean)):
        return True
    if isinstance(e, exp.Window):
        return _value_safe(e.this, p2, aliases, seen)
    if isinstance(e, (exp.Min, exp.Max)):                # one of the argument's values; MAX(a, b) mixes two sources
        return not e.expressions and _value_safe(e.this, p2, aliases, seen)
    if isinstance(e, (exp.Cast, exp.TryCast)):
        return getattr(e.to.this, "name", "") in _SAFE_CAST
    branches = None
    if isinstance(e, exp.Case):
        branches = [i.args.get("true") for i in e.args.get("ifs", [])] + [e.args.get("default")]
    elif isinstance(e, exp.If):
        branches = [e.args.get("true"), e.args.get("false")]
    elif isinstance(e, (exp.Coalesce, exp.Greatest, exp.Least)):
        branches = [e.this] + list(e.expressions)
    elif isinstance(e, exp.Nullif):
        branches = [e.this]
    elif isinstance(e, exp.Anonymous) and str(e.this).lower() in _BRANCH_ANON:
        branches = list(e.expressions)[_BRANCH_ANON[str(e.this).lower()]]
    if branches is not None:
        kinds = set()
        for b in branches:
            while isinstance(b, exp.Paren):
                b = b.this
            if b is None or isinstance(b, exp.Null):
                continue
            if isinstance(b, exp.Literal):
                kinds.add("string" if b.is_string else "integer" if b.is_int else "number")
            elif isinstance(b, (exp.Cast, exp.TryCast)) and getattr(b.to.this, "name", "") in _SAFE_CAST:
                kinds.add("function")
            elif isinstance(b, _SAFE_VALUE_CLASSES) or \
                    (isinstance(b, exp.Anonymous) and str(b.this).lower() in _SAFE_VALUE_ANON):
                kinds.add("function")
            else:
                return False                            # a column, an aggregate value or arithmetic in a branch
        return "number" not in kinds
    if isinstance(e, exp.Anonymous):
        return str(e.this).lower() in _SAFE_VALUE_ANON
    return isinstance(e, _SAFE_VALUE_CLASSES)


def _value_domains(top, shape, p2):
    """Rule 10: None if every expression that needs a safe value domain has one, else the reason."""
    sels = _selects(top)
    aliases = {}
    for sel in sels:
        for p in sel.expressions:
            if isinstance(p, exp.Alias):
                aliases.setdefault(p.alias.lower(), []).append(p.this)
    if top.find(*_PERCENTILE_CLASSES) or top.find(exp.WithinGroup):
        return "order_statistic"
    for f in top.find_all(exp.Min, exp.Max, exp.Median):
        if isinstance(f, exp.Median) and not isinstance(f.this, exp.Column):
            return "order_statistic"
        if f.expressions or not _value_safe(f.this, p2, aliases):   # SQLite's MAX(a, b) is a scalar function of each row
            return "value_domain"
    for sel in sels:
        picks = (sel is top and shape == "total_order") or any(sel.args.get(k) is not None
                                                               for k in ("distinct", "group", "limit", "offset"))
        if picks and not all(_value_safe(p, p2, aliases) for p in sel.expressions):
            return "value_domain"
    return None


def certify_sql(text, engine, strict=True, p2=None):
    """strict=True: Contract B (static certificate). strict=False: Contract E (exploratory, added after the
    B coverage measurement): byte stability rests on the pinned runtime (engine version, single thread,
    static snapshot); ordering/shape checks are dropped, read-only/volatility/allowlist checks are kept."""
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
    if any(isinstance(f, _FLOAT_ORDER_CLASSES) or (isinstance(f, exp.Anonymous) and str(f.this).lower() in _FLOAT_ORDER_ANON)
           for f in top.find_all(exp.Func)):
        return False, "float_aggregate"                # rules 6 and 7 are checked last, so earlier reasons keep their counts
    if top.find(exp.Collate):
        return False, "collate"
    if isinstance(top, exp.SetOperation) or top.find(exp.SetOperation):   # rules 8-10 come after rules 1-7: their
        return False, "set_operation"                                      # reasons count queries that rules 1-7 admit
    for sel in _selects(top):
        d = sel.args.get("distinct")
        if d is not None and d.args.get("on") is not None:
            return False, "distinct_on"
        if _bare_column(sel):
            return False, "bare_column"
    why = _value_domains(top, shape, p2)
    if why:
        return False, why
    return True, shape


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


def load_p2(path):
    """{(dataset, database): {"safe": names, "unsafe": names}} from the outputs of p2_audit.py: a column name is safe
    when every stored column of that name in the database passed the audit."""
    out = {}
    for ds, dbs in json.load(open(path))["datasets"].items():
        for db, entry in dbs.items():
            by_name = {}
            for c in entry["columns"].values():
                by_name.setdefault(c["column"].lower(), []).append(c["safe"])
            out[(ds, db)] = {"safe": {n for n, v in by_name.items() if all(v)},
                             "unsafe": {n for n, v in by_name.items() if not all(v)}}
    return out


def certify(text, engine, strict=True, p2=None):
    """p2: the audited column names of the query's database (load_p2); None treats every stored column as safe."""
    if engine == "mongo":
        return certify_mongo(text, strict)
    return certify_sql(text, engine, strict, p2)


if __name__ == "__main__":
    cases = [
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
        # rule 8: grouped projections
        ("SELECT a, b FROM t GROUP BY a ORDER BY a, b", "sqlite", False),
        ("SELECT MAX(x), y FROM t ORDER BY 1, 2", "sqlite", False),
        ("SELECT a, COUNT(*) FROM t GROUP BY a HAVING b > 1 ORDER BY 1, 2", "sqlite", False),
        ("SELECT a AS b, b FROM t GROUP BY b ORDER BY 1, 2", "sqlite", False),
        ("SELECT a, COUNT(*) FROM t GROUP BY 1 ORDER BY 1, 2", "duckdb", True),
        ("SELECT t.a, COUNT(*) FROM t GROUP BY a ORDER BY 1, 2", "postgres", True),
        ("SELECT * FROM (SELECT a, b FROM t GROUP BY a) s ORDER BY a, b", "sqlite", False),
        # rule 9: set operations and DISTINCT ON
        ("SELECT DISTINCT ON (a) a, b FROM t ORDER BY a, b", "postgres", False),
        ("SELECT a FROM t UNION SELECT a FROM u ORDER BY a", "duckdb", False),
        # rule 10: value domains
        ("SELECT a, x * 2 AS y FROM t ORDER BY a, y", "duckdb", False),
        ("SELECT COUNT(*) * 100.0 / MAX(n) FROM t", "sqlite", True),
        ("SELECT MAX(x * 2) FROM t", "sqlite", False),
        ("SELECT MAX(a, b) FROM t", "sqlite", False),
        ("SELECT a, UPPER(b) AS u FROM t ORDER BY a, u", "duckdb", True),
        ("SELECT a, CASE WHEN x > 0 THEN 'pos' ELSE 'neg' END AS s FROM t ORDER BY a, s", "sqlite", True),
        ("SELECT a, COALESCE(x, 0) AS v FROM t ORDER BY a, v", "sqlite", False),
        ("SELECT a, CAST(x AS REAL) AS v FROM t ORDER BY a, v", "sqlite", False),
        ("SELECT a, CAST(x AS INTEGER) AS v FROM t ORDER BY a, v", "sqlite", True),
        ("SELECT a, ROUND(x, 2) AS v FROM t ORDER BY a, v", "duckdb", False),
        ("SELECT y, c FROM (SELECT x AS y, COUNT(*) AS c FROM t GROUP BY x) s ORDER BY y, c", "duckdb", True),
        ("SELECT y FROM (SELECT x / 2 AS y FROM t) s ORDER BY y", "duckdb", False),
        ("SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY x) FROM t", "postgres", False),
        ("SELECT DISTINCT x / 2 FROM t ORDER BY 1", "duckdb", False),
    ]
    bad = 0
    for q, e, want in cases:
        got, why = certify(q, e)
        flag = "ok " if got == want else "BAD"
        bad += got != want
        print(f"{flag} {got!s:5} {why:24} {q}")
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
    audit = {"safe": {"a"}, "unsafe": {"b"}}                # precondition P2 as audited: column b fails
    p2_cases = [("SELECT a, b FROM t ORDER BY a, b", "duckdb", False), ("SELECT a FROM t ORDER BY a", "duckdb", True),
                ("SELECT MIN(b) FROM t", "sqlite", False), ("SELECT COUNT(b) FROM t", "sqlite", True),
                ("SELECT a, c FROM t ORDER BY a, c", "postgres", False)]
    for q, e, want in p2_cases:
        got, why = certify(q, e, p2=audit)
        bad += got != want
        print(f"{'ok ' if got == want else 'BAD'} P2:{got!s:5} {why:24} {q}")
    n = len(cases) + 5 + 6 + len(p2_cases)
    print(f"unit tests: {n} assertions ({len(cases)} SQL B, 5 Mongo B, 6 E, {len(p2_cases)} P2), failures: {bad}")
    sys.exit(1 if bad else 0)
