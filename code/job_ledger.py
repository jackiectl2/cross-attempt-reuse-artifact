"""Ledger of every Slurm job that left a log: final state, exit code, elapsed time, and, for a job that did not finish
cleanly (state other than COMPLETED, or a Python traceback in its log), the job that replaced it: the next job with the
same job name, array index and submitted script (sacct SubmitLine; one job name was reused for per-dataset scripts)
that finished cleanly. Jobs without such a successor are listed with superseded_by null
(abandoned smoke tests, or failures fixed under a new job name; the tracker and EXPERIMENT_RESULTS.md name those).
Bookkeeping only (sacct queries and log reads). Usage: job_ledger.py <log_dir> <out.json>
"""
import glob
import json
import os
import re
import subprocess
import sys

LOG_RE = re.compile(r"^(?P<name>.+?)[-_](?P<job>\d{7,9})(?:_(?P<idx>\d+))?\.out$")


def main(log_dir, out):
    jobs = []
    for f in sorted(glob.glob(os.path.join(log_dir, "*.out"))):
        m = LOG_RE.match(os.path.basename(f))
        if m:
            with open(f, errors="replace") as fh:
                text = fh.read()
            jobs.append({"log": os.path.basename(f), "name": m.group("name"), "job": m.group("job"), "idx": m.group("idx"),
                         "traceback": "Traceback (most recent call last)" in text})
    ids = sorted({j["job"] for j in jobs})
    acct = {}
    for i in range(0, len(ids), 100):
        r = subprocess.run(["sacct", "-n", "-P", "-X", "-j", ",".join(ids[i:i + 100]),
                            "-o", "JobID,JobName,Account,State,ExitCode,Elapsed,Start,End,SubmitLine"],
                           stdout=subprocess.PIPE, universal_newlines=True, check=True)
        for line in r.stdout.splitlines():
            jid, name, account, state, ec, el, start, end, submit = line.split("|", 8)
            scripts = [os.path.basename(t) for t in submit.split() if t.endswith(".sbatch")]
            acct[jid] = {"account": account, "state": state.split()[0], "exit_code": ec, "elapsed": el,
                         "start": start, "end": end, "script": scripts[-1] if scripts else submit}
    for j in jobs:
        a = acct.get(j["job"] + ("_" + j["idx"] if j["idx"] is not None else "")) or acct.get(j["job"]) or {}
        j.update(a)
        j["clean"] = a.get("state") == "COMPLETED" and not j["traceback"]
    for j in jobs:
        if not j["clean"]:
            later = [k for k in jobs if k["clean"] and k["name"] == j["name"] and k["idx"] == j["idx"]
                     and k.get("script") == j.get("script") and int(k["job"]) > int(j["job"])]
            j["superseded_by"] = min(later, key=lambda k: int(k["job"]))["log"] if later else None
    summary = {"jobs": len(jobs), "clean": sum(j["clean"] for j in jobs),
               "not_clean": sum(not j["clean"] for j in jobs),
               "not_clean_with_successor": sum((not j["clean"]) and j["superseded_by"] is not None for j in jobs),
               "states": {s: sum(j.get("state") == s for j in jobs) for s in sorted({j.get("state") for j in jobs if j.get("state")})}}
    json.dump({"summary": summary, "jobs": jobs}, open(out, "w"), indent=1)
    print(json.dumps(summary, indent=1))
    for j in jobs:
        if not j["clean"]:
            print(j["log"], j.get("state"), j.get("exit_code"), "traceback" if j["traceback"] else "", "->", j["superseded_by"])


if __name__ == "__main__":
    main(*sys.argv[1:])
