"""Record when the experiment plan was written relative to the pilot and replay jobs.

The plan's write times come from the authoring session's log (JSON lines, one UTC timestamp per tool call). The script
checks that the plan's text before its first amendment is byte-identical to what the session first wrote, copies the
plan next to the record, and adds the start and end of the pilot, replay and analysis job arrays from the job ledger
(cluster-local times, converted to UTC). The log records it used (the write of the plan and the commands that appended
its amendments, reduced to timestamp, tool and input) are saved as session_log_excerpt.jsonl; the script accepts that
excerpt in place of the full log, which is not part of the archive.

Usage: plan_provenance.py <session.jsonl | session_log_excerpt.jsonl> <EXPERIMENT_PLAN.md> <job_ledger.json> <out_dir>
"""
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
from zoneinfo import ZoneInfo

CLUSTER_TZ = "America/Detroit"
JOBS = {"ed-eval-01": "pilot replay (SQLite and DuckDB calls)",
        "ed-eval-02": "pilot analysis",
        "ed-eval-03": "first full replay, newer libraries (results/analysis_m1)",
        "ed-eval-06": "reported full replay, pinned runtime",
        "ed-eval-08": "analysis of the full replay"}


def utc(local):
    t = datetime.datetime.fromisoformat(local).replace(tzinfo=ZoneInfo(CLUSTER_TZ))
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def main(session, plan, ledger, out_dir):
    name = os.path.basename(plan)
    first, amendments, used = None, {}, []
    with open(session) as f:
        for line in f:
            d = json.loads(line)
            content = (d.get("message") or {}).get("content")
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                inp = b.get("input") or {}
                keep = False
                if b["name"] == "Write" and str(inp.get("file_path", "")).endswith(name) and first is None:
                    first, keep = (d["timestamp"][:19] + "Z", inp["content"]), True
                cmd = str(inp.get("command", ""))
                if b["name"] == "Bash" and name in cmd:
                    for h in re.findall(r"^## (Amendment A\d+)", cmd, re.M):
                        keep = keep or h not in amendments
                        amendments.setdefault(h, d["timestamp"][:19] + "Z")
                if keep:
                    used.append({"timestamp": d["timestamp"], "message": {"content": [
                        {"type": "tool_use", "name": b["name"], "input": inp}]}})
    text = open(plan).read()
    head = text[:text.index("\n## Amendment A1")]
    jobs = {}
    for j in json.load(open(ledger))["jobs"]:
        if j["name"] in JOBS and j.get("start") and j.get("end"):
            r = jobs.setdefault(j["name"], {"what": JOBS[j["name"]], "first_start_utc": utc(j["start"]),
                                            "last_end_utc": utc(j["end"]), "job_ledger_entries": 0})
            r["first_start_utc"] = min(r["first_start_utc"], utc(j["start"]))
            r["last_end_utc"] = max(r["last_end_utc"], utc(j["end"]))
            r["job_ledger_entries"] += 1
    record = {
        "plan": name,
        "plan_sha256": sha(text),
        "original_text": {"written_utc": first[0], "bytes": len(first[1].encode()), "sha256": sha(first[1]),
                          "identical_to_plan_text_before_amendment_A1": first[1].rstrip("\n") == head.rstrip("\n")},
        "amendments_first_written_utc": amendments,
        "jobs": jobs,
        "order": {"pilot_analysis_ended_before_plan": jobs["ed-eval-02"]["last_end_utc"] < first[0],
                  "plan_written_before_first_full_replay": first[0] < jobs["ed-eval-03"]["first_start_utc"],
                  "amendment_A1_before_reported_replay": amendments["Amendment A1"] < jobs["ed-eval-06"]["first_start_utc"],
                  "amendment_A2_after_analysis_of_full_replay": amendments["Amendment A2"] > jobs["ed-eval-08"]["first_start_utc"]},
        "cluster_timezone": CLUSTER_TZ,
        "sources": "write times: the authoring session's log (records used: session_log_excerpt.jsonl); "
                   "job times: results/raw/job_ledger.json",
    }
    os.makedirs(out_dir, exist_ok=True)
    if os.path.abspath(plan) != os.path.abspath(os.path.join(out_dir, name)):
        shutil.copyfile(plan, os.path.join(out_dir, name))
    excerpt = "".join(json.dumps(r) + "\n" for r in used)
    if os.path.abspath(session) != os.path.abspath(os.path.join(out_dir, "session_log_excerpt.jsonl")):
        with open(os.path.join(out_dir, "session_log_excerpt.jsonl"), "w") as f:
            f.write(excerpt)
    with open(os.path.join(out_dir, "provenance.json"), "w") as f:
        json.dump(record, f, indent=1)
    print(json.dumps(record, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:])
