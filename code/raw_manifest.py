"""Write the provenance manifest of the raw-table bundle (results/raw/): every file with its path inside the bundle,
its source path on the cluster, size, SHA-256, row count and columns (tables), plus the parser metadata; job logs and
sbatch scripts are listed file by file (except the log of the job running this script).
Usage: raw_manifest.py <project_root> <out.json>"""
import glob
import hashlib
import json
import os
import re
import sys

import pyarrow.parquet as pq

# (source glob relative to the project root, destination directory inside results/raw/)
BUNDLE = [
    ("work/v2/calls.parquet", "v2"),
    ("work/v2/runs.parquet", "v2"),
    ("work/v2/parse_meta.json", "v2"),
    ("work/v2/replay_dab/*.parquet", "v2/replay_dab"),
    ("work/v2/replay/*.parquet", "v2/replay"),
    ("work/v2/replay_xh/*.parquet", "v2/replay_xh"),
    ("work/v2/analysis_dab_r3/*.parquet", "sim"),                          # reported run (final Contract B)
    ("work/v2/analysis_dab_r2/B3_sim_Bcert.parquet", "sim_rules7"),        # B-gated rows under rules 1-7 of Contract B
    ("work/v2/analysis_dab_r1/B3_sim_Bcert.parquet", "sim_certv1"),        # B-gated rows under the original Contract B
    ("work/v2/ablate/*.parquet", "ablate"),
    ("work/v2/xharness/*.parquet", "xharness"),
    ("work/v2/xharness/*_provenance.json", "xharness"),
    ("logs/*.out", "logs"),
    ("logs/*.sbatch", "logs"),
    ("logs/*.sh", "logs"),
    ("logs/cpu_queue/*.tasks", "logs/cpu_queue"),
    ("logs/cpu_queue/submissions.tsv", "logs/cpu_queue"),
]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(root, out):
    # the log of the job generating this manifest is still being written: leave it out
    own = {os.environ.get(v) for v in ("SLURM_JOB_ID", "SLURM_ARRAY_JOB_ID")} - {None, ""}
    rows = []
    for pattern, dest in BUNDLE:
        for f in sorted(glob.glob(os.path.join(root, pattern))):
            if any(t in own for t in re.split(r"[-_.]", os.path.basename(f))):
                continue
            r = {"bundle_path": os.path.join(dest, os.path.basename(f)), "source": os.path.relpath(f, root),
                 "bytes": os.path.getsize(f), "sha256": sha256(f)}
            if f.endswith(".parquet"):
                pf = pq.ParquetFile(f)
                r.update({"rows": pf.metadata.num_rows, "columns": pf.schema_arrow.names})
            rows.append(r)
    meta = json.load(open(os.path.join(root, "work/v2/parse_meta.json")))
    json.dump({"parse_meta": meta, "bundle": BUNDLE, "files": rows}, open(out, "w"), indent=1)
    print(len(rows), "files")


if __name__ == "__main__":
    main(*sys.argv[1:])
