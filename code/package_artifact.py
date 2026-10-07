"""Build the supplementary archive of the paper.

Copies code/, envs/, the paper's build scripts and number ledger, and results/ into a staging directory, rewrites
every .json as strict JSON (NaN -> null, +/-Infinity -> the strings "Infinity" / "-Infinity"), writes MANIFEST.sha256,
and zips the directory. `--lean` leaves out results/raw/sim (the per-resample simulation rows, which
code/analyze2.py regenerates from results/raw/v2).

Usage: package_artifact.py <project_root> <out.zip> [--lean]
"""
import hashlib
import json
import math
import os
import shutil
import sys
import zipfile

INCLUDE = ["code", "envs", "results", "review-stage/contract_b_review",
           "paper/ledger.py", "paper/ledger_text.py", "paper/check_numbers.py", "paper/NUMBERS_LEDGER.csv",
           "paper/figures", "paper/sections", "paper/main.tex", "paper/preamble_extra.tex", "paper/references.bib"]
SKIP_DIRS = {"__pycache__", ".DS_Store"}
LATEX_AUX = (".aux", ".log", ".fls", ".fdb_latexmk", ".out", ".blg", ".bbl")     # under paper/ only; job logs are *.out too
# Cluster job logs and scripts are not shipped, except the six files that the number ledger reads.
KEEP_LOGS = {"e2e_all_E_PATENTS.sbatch", "ed-eval-06-62968329_0.out", "ed-eval-06-62968329_33.out",
             "ed-eval-73_63146231_0.out", "extra.sbatch", "m1b.sbatch"}
TEXT_EXT = (".py", ".sh", ".sbatch", ".out", ".txt", ".json", ".jsonl", ".md", ".csv", ".tasks", ".tex", ".def")
# Site-specific paths, the cluster user name and the billing accounts are replaced in the shipped copy.
SCRUB = [(r"/scratch/[A-Za-z0-9_]+_root/[A-Za-z0-9_]+/[A-Za-z0-9_]+/EDBT_Conference_27", "$PROJECT_DIR"),
         (r"/scratch/[A-Za-z0-9_]+_root/[A-Za-z0-9_]+/[A-Za-z0-9_]+", "$SCRATCH_DIR"),
         (r"/Users/[A-Za-z0-9_]+/[^\s\"']*EDBT[_-]Conference[_-]27", "$LOCAL_PROJECT_DIR"), (r"/Users/[A-Za-z0-9_]+", "$HOME"),
         (r"/home/[a-z0-9]+", "$HOME"), (r"\b" + os.environ.get("ED_CLUSTER_USER", "ct" + "lang") + r"\b", "user"), (r"[a-z0-9]+_owned_root", "ACCOUNT_root"),
         (r"[a-z0-9]+_owned1", "ACCOUNT"), (r"eecs\d+s\d+w\d+_class", "ACCOUNT_B"), (r"\bengin1\b", "ACCOUNT_C")]


def scrub(path):
    """Replace site-specific identifiers in one staged text file."""
    import re
    try:
        with open(path, encoding="utf-8") as fh:
            t = fh.read()
    except UnicodeDecodeError:
        return
    u = t
    for pat, repl in SCRUB:
        u = re.sub(pat, repl, u)
    if u != t:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(u)


def strict(x):
    if isinstance(x, dict):
        return {k: strict(v) for k, v in x.items()}
    if isinstance(x, list):
        return [strict(v) for v in x]
    if isinstance(x, float) and not math.isfinite(x):
        return None if math.isnan(x) else ("Infinity" if x > 0 else "-Infinity")
    return x


def main(root, out, lean=False):
    stage = out[:-4]
    shutil.rmtree(stage, ignore_errors=True)
    n_json = 0
    for item in INCLUDE:
        src = os.path.join(root, item)
        files = [src] if os.path.isfile(src) else [os.path.join(d, f) for d, ds, fs in os.walk(src) for f in fs
                                                   if not (set(d.split(os.sep)) & SKIP_DIRS)]
        for f in files:
            rel = os.path.relpath(f, root)
            if (f.endswith(".pyc") or (rel.startswith("paper/") and f.endswith(LATEX_AUX))
                    or os.path.basename(f) in SKIP_DIRS or rel.startswith("results/superseded/")
                    or (lean and rel.startswith("results/raw/sim/"))
                    or (rel.startswith("results/raw/logs/") and os.path.relpath(f, os.path.join(root, "results/raw/logs")) not in KEEP_LOGS)):
                continue
            dst = os.path.join(stage, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if f.endswith(".json"):
                with open(f) as fh:
                    data = json.load(fh)
                with open(dst, "w") as fh:
                    json.dump(strict(data), fh, indent=1, allow_nan=False)
                n_json += 1
            else:
                shutil.copyfile(f, dst)
    shutil.copyfile(os.path.join(root, "ARTIFACT_README.md"), os.path.join(stage, "README.md"))
    for d, _, fs in os.walk(stage):
        for f in fs:
            if f.endswith(TEXT_EXT):
                scrub(os.path.join(d, f))
    lines = []
    for d, _, fs in os.walk(stage):
        for f in sorted(fs):
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                lines.append("%s  %s" % (hashlib.sha256(fh.read()).hexdigest(), os.path.relpath(p, stage)))
    with open(os.path.join(stage, "MANIFEST.sha256"), "w") as fh:
        fh.write("\n".join(sorted(lines, key=lambda s: s.split("  ", 1)[1])) + "\n")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for d, _, fs in os.walk(stage):
            for f in sorted(fs):
                p = os.path.join(d, f)
                z.write(p, os.path.join(os.path.basename(stage), os.path.relpath(p, stage)))
    print("%d files, %d strict JSON, %.1f MB -> %s" % (len(lines) + 1, n_json, os.path.getsize(out) / 1e6, out))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], "--lean" in sys.argv[3:])
