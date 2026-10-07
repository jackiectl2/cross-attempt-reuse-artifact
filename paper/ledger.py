"""Helpers for the number ledger (paper/NUMBERS_LEDGER.csv): every number printed in the paper has an expression over
files in results/ that reproduces its printed form; fixed parameters of the study are read from the code, the package
lists in envs/ and the job scripts and logs in results/raw/logs/. Standard library only."""
import csv
import glob
import json
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_J = {}


def J(path):
    if path not in _J:
        with open(os.path.join(ROOT, path)) as f:
            _J[path] = json.load(f)
    return _J[path]


def ROW(path, **match):
    with open(os.path.join(ROOT, path)) as f:
        for r in csv.DictReader(f):
            if all(r[k] == v for k, v in match.items()):
                return r
    raise KeyError(f"{path}: no row {match}")


def ROWS(path, **match):
    """Every row of a CSV file whose fields equal the given values."""
    with open(os.path.join(ROOT, path)) as f:
        return [r for r in csv.DictReader(f) if all(r[k] == v for k, v in match.items())]


def RE(path, pattern):
    """First group of the first match of a regular expression in a text file (code, package list, job script, log)."""
    with open(os.path.join(ROOT, path), encoding="utf-8") as f:
        m = re.search(pattern, f.read(), re.M)
    if not m:
        raise KeyError(f"{path}: no match for {pattern}")
    return m.group(1)


def FILES(pattern):
    """Paths under the project root that match a glob pattern."""
    return sorted(os.path.relpath(p, ROOT) for p in glob.glob(os.path.join(ROOT, pattern)))


def word(n):
    """A small count as the paper spells it."""
    return ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"][int(n)]


def pct(x, d=1):
    return f"{100 * float(x):.{d}f}"


def fx(x, d=2):
    return f"{float(x):.{d}f}"


def cnt(x):
    return f"{int(round(float(x))):,}"


def ST(name):
    """One test of results/analysis_dab_r3/stats.json by its name."""
    for d in J("results/analysis_dab_r3/stats.json"):
        if d["name"] == name:
            return d
    raise KeyError(name)


def mant(x, d=1):
    """Mantissa of x in scientific notation, as printed in a \\times10^{k} expression."""
    return f"{float(x):.{d}e}".split("e")[0]


ENV = {"J": J, "ROW": ROW, "ROWS": ROWS, "RE": RE, "FILES": FILES, "word": word, "pct": pct, "fx": fx, "cnt": cnt, "ST": ST,
       "mant": mant, "ROOT_": ROOT}


def ev(expr):
    return eval(expr, dict(ENV))
