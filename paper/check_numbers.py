"""Check paper/NUMBERS_LEDGER.csv: every expression must reproduce its printed value from results/. With --coverage,
also list numeric tokens in the section sources that no ledger row prints (candidates for missing ledger rows)."""
import csv
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ledger import ROOT, ev  # noqa: E402

LEDGER = os.path.join(ROOT, "paper", "NUMBERS_LEDGER.csv")


def main(coverage=False):
    with open(LEDGER) as f:
        rows = list(csv.DictReader(f))
    bad = 0
    for r in rows:
        try:
            got = ev(r["expr"])
        except Exception as e:  # report and continue; a broken expression is a mismatch
            got = "ERROR %s: %s" % (type(e).__name__, e)
        if got != r["printed"]:
            bad += 1
            print("MISMATCH %s: printed %r, source gives %r" % (r["location"], r["printed"], got))
    print("%d ledger rows, %d mismatches" % (len(rows), bad))
    if coverage:
        printed = {r["printed"] for r in rows}
        secdir, figdir = os.path.join(ROOT, "paper", "sections"), os.path.join(ROOT, "paper", "figures")
        files = [os.path.join(secdir, f) for f in sorted(os.listdir(secdir))]
        files += [os.path.join(figdir, f) for f in sorted(os.listdir(figdir)) if f.endswith("_float.tex")]
        for path in files:
            fn = os.path.relpath(path, os.path.join(ROOT, "paper"))
            text = open(path).read()
            text = re.sub(r"%.*", "", text)                                    # comments
            text = re.sub(r"\\(cite|ref|label|cref|Cref|input|includegraphics)\{[^}]*\}", "", text)
            text = re.sub(r"\\(input|subcaptionbox)\{[^}]*\}", "", text)
            for m in re.finditer(r"(?<![\w.#~'/-])\d[\d,]*(?:\.\d+)*(?!\.\d)(?![\w/]|-(?!-))", text):
                tok = m.group(0).rstrip(",")
                if tok not in printed and not re.fullmatch(r"(19|20)\d\d|[0-9]", tok):
                    print("UNLEDGERED %s: %s ...%s..." % (fn, tok, text[max(0, m.start() - 30):m.end() + 10].replace("\n", " ")))
    return bad


if __name__ == "__main__":
    sys.exit(1 if main("--coverage" in sys.argv) else 0)
