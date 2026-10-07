#!/usr/bin/env python3
"""Local submission queue for this project's pure-CPU work on Great Lakes (rules: REMOTE-COMPUTE.md, 2026-10-02).

- Partition `standard`; accounts in order ACCOUNT_B -> ACCOUNT_C -> ACCOUNT. The course account takes a
  task only if its walltime is <= 8 h and the user has fewer than 2 jobs there (running, plus this project's pending).
  ACCOUNT_C and ACCOUNT take as many tasks as their remaining user- and account-level cpu/memory allow.
- At most 6 of the project's CPU tasks (job names `ed-*` on `standard`, array elements counted one by one) may be
  RUNNING or PENDING; the rest wait here and are submitted, one job array per account, as slots free up.
- Request = cores x1, memory x1.2, walltime x1.5 of the estimate, rounded up (memory to whole G from 1 G on, walltime to
  minutes); requests over 2 days are refused (they need the user's approval). No salloc, no --exclusive.

Usage: cpu_submit.py <task>-<NN> <task_file> <est_cores> <est_mem> <est_time> [extra sbatch args ...]
  <task>-<NN>  job name suffix, e.g. eval-28 (job name ed-eval-28; task in prep/train/eval/build/run/test)
  task_file    one shell command per line, run from the project directory with `bash -lc`
  est_mem      e.g. 14.4G or 800M;  est_time: minutes, or [D-]HH:MM:SS
  DRY_RUN=1    print the plan for the next chunk using `sbatch --test-only` (creates no job) and exit
Long batches: run with nohup on the login node; every submission is appended to logs/cpu_queue/submissions.tsv.
"""
import fcntl
import math
import os
import re
import subprocess
import sys
import time

P = "$PROJECT_DIR"
Q = P + "/logs/cpu_queue"
USER = os.environ.get("USER", "user")
ACCOUNTS = ["ACCOUNT_B", "ACCOUNT_C", "ACCOUNT"]
COURSE, COURSE_MAX_JOBS, COURSE_MAX_MIN = "ACCOUNT_B", 2, 480
MAX_TASKS, MAX_MIN = 6, 2 * 24 * 60
INF = float("inf")


def sh(args):
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(args[:3]), r.stderr.strip()))
    return r.stdout


def ceil(x):
    return int(math.ceil(round(x, 6)))


def mem_mb(text):
    m = re.match(r"^\s*([\d.]+)\s*([MGT]?)B?\s*$", text, re.I)
    if not m:
        raise ValueError("memory must look like 14.4G or 800M: %r" % text)
    return float(m.group(1)) * {"": 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2).upper()]


def minutes(text):
    if re.match(r"^\s*[\d.]+\s*$", text):
        return float(text)
    m = re.match(r"^(?:(\d+)-)?(\d+):(\d+):(\d+)$", text.strip())
    if not m:
        raise ValueError("time must be minutes or [D-]HH:MM:SS: %r" % text)
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi + s / 60.0


def request(est_cores, est_mem, est_time):
    cores = ceil(float(est_cores))
    req_mb = ceil(1.2 * mem_mb(est_mem))
    if req_mb >= 1024:
        gb = ceil(req_mb / 1024.0)
        mem_str, req_mb = "%dG" % gb, gb * 1024
    else:
        mem_str = "%dM" % req_mb
    req_min = ceil(1.5 * minutes(est_time))
    if req_min > MAX_MIN:
        sys.exit("walltime %d min exceeds 2 days: needs the user's approval (REMOTE-COMPUTE.md)" % req_min)
    return cores, mem_str, req_mb, req_min


def slurm_mb(text):
    m = re.match(r"^([\d.]+)([KMGT]?)", text)
    return float(m.group(1)) * {"K": 1 / 1024.0, "": 1, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2)] if m else 0.0


def project_rows(state=None):
    """(account, cpus, mem_mb) of each unfinished task of this project's CPU jobs, array elements one by one."""
    args = ["squeue", "-u", USER, "-h", "-r", "-p", "standard", "-o", "%a|%j|%C|%m"]
    if state:
        args[5:5] = ["-t", state]
    rows = []
    for line in sh(args).splitlines():
        a, j, c, m = line.split("|")
        if j.startswith("ed-"):
            rows.append((a, int(c), slurm_mb(m)))
    return rows


def assoc_free():
    """Remaining cpu and memory (MB) per account: min over the user-level and account-level GrpTRES (limit - used)."""
    out = sh(["scontrol", "show", "assoc_mgr", "users=" + USER, "accounts=" + ",".join(ACCOUNTS), "flags=assoc"])
    free = {a: [INF, INF] for a in ACCOUNTS}
    for block in out.split("ClusterName=")[1:]:
        acct = re.search(r"Account=(\S+)", block).group(1)
        grp = re.search(r"GrpTRES=(\S+)", block)
        if acct not in free or not grp:
            continue
        for i, tres in enumerate(("cpu", "mem")):
            m = re.search(r"(?:^|,)%s=(N|\d+)\((\d+)\)" % tres, grp.group(1))
            if m and m.group(1) != "N":
                free[acct][i] = min(free[acct][i], int(m.group(1)) - int(m.group(2)))
    return free


def plan(k, cores, req_mb, req_min):
    """Split up to k tasks over the accounts in order; [] means wait locally."""
    free, pending = assoc_free(), project_rows("PD")
    course_running = len(sh(["squeue", "-u", USER, "-h", "-r", "-t", "R", "-A", COURSE, "-o", "%i"]).split())
    out, left = [], k
    for a in ACCOUNTS:
        mine = [r for r in pending if r[0] == a]
        if a == COURSE:
            if req_min > COURSE_MAX_MIN:
                continue
            cap = COURSE_MAX_JOBS - course_running - len(mine)
        else:
            fc = free[a][0] - sum(r[1] for r in mine)
            fm = free[a][1] - sum(r[2] for r in mine)
            cap = min(left if fc == INF else int(fc // cores), left if fm == INF else int(fm // req_mb))
        n = max(0, min(left, cap))
        if n:
            out.append((a, n))
            left -= n
    if not out and not pending:   # nothing has room now: queue one task in Slurm at the first eligible account
        course_open = req_min <= COURSE_MAX_MIN and course_running < COURSE_MAX_JOBS   # at its job cap a task would wait behind other projects
        out.append((COURSE if course_open else ACCOUNTS[1], 1))
    return out


def submit(name, tag, acct, lines, cores, mem_str, req_min, extra, dry):
    f = "%s/%s_%s.tasks" % (Q, name, tag)
    with open(f, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    # the job log records the code it runs: SHA-256 of every script in code/ at task start
    wrap = ('cmd=$(sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" %s) && echo "[ed-%s %s task $SLURM_ARRAY_TASK_ID] $cmd" '
            '&& sha256sum code/*.py code/*.sh | sed "s/^/code-sha256 /" && bash -lc "$cmd"' % (f, name, tag))
    args = ["sbatch", "--test-only" if dry else "--parsable", "-J", "ed-" + name, "-A", acct, "-p", "standard",
            "-c", str(cores), "--mem=" + mem_str, "-t", str(req_min), "--array=0-%d" % (len(lines) - 1),
            "--chdir=" + P, "-o", "%s/logs/ed-%s_%%A_%%a.out" % (P, name)] + extra + ["--wrap", wrap]
    r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
    if r.returncode != 0:
        raise RuntimeError("sbatch failed: " + r.stderr.strip())
    if dry:
        return (r.stdout + r.stderr).strip()
    jid = r.stdout.strip().split(";")[0]
    for _ in range(30):   # wait until squeue lists the tasks, so a concurrent dispatcher counts them
        r = subprocess.run(["squeue", "-h", "-r", "-j", jid, "-o", "%i"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        if r.stdout.strip() or r.returncode != 0:   # listed, or already finished
            break
        time.sleep(1)
    with open(Q + "/submissions.tsv", "a") as fh:
        fh.write("\t".join([time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ed-" + name, tag, acct, jid, str(len(lines)),
                            str(cores), mem_str, str(req_min)]) + "\n")
    return jid


def main():
    if len(sys.argv) < 6:
        sys.exit(__doc__)
    name, task_file, est_cores, est_mem, est_time = sys.argv[1:6]
    extra = sys.argv[6:]
    if not re.match(r"^(prep|train|eval|build|run|test)-\d+$", name):
        sys.exit("name must be <task>-<NN> with task in prep/train/eval/build/run/test (job name ed-<task>-<NN>)")
    if any(x in ("--exclusive",) or x.startswith("--gres") for x in extra):
        sys.exit("--exclusive and GPU GRES are not allowed for CPU jobs")
    lines = [l for l in open(task_file).read().splitlines() if l.strip()]
    if not lines:
        sys.exit("no tasks in " + task_file)
    cores, mem_str, req_mb, req_min = request(est_cores, est_mem, est_time)
    dry = os.environ.get("DRY_RUN") == "1"
    os.makedirs(Q, exist_ok=True)
    print("ed-%s: %d tasks, request %d cores / %s / %d min each" % (name, len(lines), cores, mem_str, req_min), flush=True)
    lock = open(Q + "/.lock", "w")
    nxt, chunk = 0, 0
    while nxt < len(lines):
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            free = MAX_TASKS - len(project_rows())
            parts = plan(min(free, len(lines) - nxt), cores, req_mb, req_min) if free > 0 else []
            if dry:
                print("free slots %d; plan %s" % (max(free, 0), parts))
                for acct, n in parts:
                    print(" ", acct, n, submit(name, "dry", acct, lines[nxt:nxt + n], cores, mem_str, req_min, extra, True))
                return
            for acct, n in parts:
                jid = submit(name, "c%d" % chunk, acct, lines[nxt:nxt + n], cores, mem_str, req_min, extra, False)
                print("ed-%s: chunk %d -> %s job array %s, tasks %d..%d of %d"
                      % (name, chunk, acct, jid, nxt, nxt + n - 1, len(lines)), flush=True)
                nxt += n
                chunk += 1
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
        if nxt < len(lines):
            time.sleep(30)
    print("ed-%s: all %d tasks submitted" % (name, len(lines)), flush=True)


if __name__ == "__main__":
    main()
