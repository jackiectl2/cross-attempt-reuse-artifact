"""Provenance of the public inputs: the DAB repository checkout, its data files, and the trajectory archive.

Records the repository revision and remote; every tracked file that differs from the revision (expected: only data
files under query_*/query_dataset/, which the clone holds as Git LFS stubs until DAB's download.sh replaces them) and
every untracked Python file (code that is not part of the revision); for
the workload's datasets (keys of dataset_table.json) the Git blob id of every task's validate.py and of db_config.yaml
and whether the file on disk is that blob; the SHA-256 and size of every data file against DAB's own
dataset_manifest.tsv; and the size and SHA-256 of the trajectory archive. Bookkeeping only.

Usage: dab_inputs.py <dab_dir> <dataset_table.json> <out.json>      (<dab_dir> holds repo/ and all_trajectories.zip)
"""
import csv
import glob
import hashlib
import json
import os
import subprocess
import sys

TRAJECTORY_URL = "https://drive.usercontent.google.com/download?id=1SjCkvwsc4m1S17l_rzu9PHAAei3jAL4i"   # logs/dab_fetch.sbatch


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main(dab, datasets, out):
    repo, zp = os.path.join(dab, "repo"), os.path.join(dab, "all_trajectories.zip")
    git = lambda *a: subprocess.run(["git", "-C", repo, *a], stdout=subprocess.PIPE, universal_newlines=True,
                                    check=True).stdout.strip()
    names = sorted(json.load(open(datasets)))
    modified = [l[3:] for l in git("status", "--porcelain", "--untracked-files=no").splitlines()]
    res = {"repo_remote": git("remote", "get-url", "origin"), "repo_revision": git("rev-parse", "HEAD"),
           "tracked_files_modified": modified,
           "tracked_files_modified_outside_query_dataset": [m for m in modified if "/query_dataset/" not in m],
           "untracked_python_files": [l[3:] for l in git("status", "--porcelain", "--untracked-files=all").splitlines()
                                      if l.startswith("??") and l.endswith(".py")],
           "all_trajectories.zip": {"url": TRAJECTORY_URL, "bytes": os.path.getsize(zp), "sha256": sha256(zp)},
           "datasets": {}}

    def blob(rel):                       # blob id at the revision, and whether the file on disk is that blob
        b = git("rev-parse", f"HEAD:{rel}")
        return {"git_blob": b, "on_disk_is_blob": git("hash-object", rel) == b, "sha256": sha256(os.path.join(repo, rel))}
    with open(os.path.join(repo, "dataset_manifest.tsv")) as f:
        manifest = [r for r in csv.reader(f, delimiter="\t") if r]
    for ds in names:
        d = f"query_{ds}"
        data = {}
        for rel, digest, size in manifest:
            if rel.startswith(d + "/"):
                p = os.path.join(repo, rel)
                data[rel] = {"bytes": os.path.getsize(p), "sha256_matches_dab_manifest":
                             os.path.getsize(p) == int(size) and sha256(p) == digest}
        res["datasets"][ds] = {"db_config.yaml": blob(f"{d}/db_config.yaml"),
                               "validate.py": {os.path.basename(os.path.dirname(f)): blob(os.path.relpath(f, repo))
                                               for f in sorted(glob.glob(os.path.join(repo, d, "*", "validate.py")))},
                               "data_files": data}
    v = [x for ds in res["datasets"].values() for x in ds["validate.py"].values()]
    c = [ds["db_config.yaml"] for ds in res["datasets"].values()]
    f_ = [x for ds in res["datasets"].values() for x in ds["data_files"].values()]
    res["summary"] = {"validators": len(v), "validators_equal_revision_blob": sum(x["on_disk_is_blob"] for x in v),
                      "db_configs": len(c), "db_configs_equal_revision_blob": sum(x["on_disk_is_blob"] for x in c),
                      "data_files": len(f_), "data_files_matching_dab_manifest": sum(x["sha256_matches_dab_manifest"] for x in f_),
                      "data_bytes": sum(x["bytes"] for x in f_)}
    json.dump(res, open(out, "w"), indent=1)
    print(json.dumps({**{k: res[k] for k in ("repo_remote", "repo_revision", "tracked_files_modified_outside_query_dataset",
                                             "untracked_python_files", "summary")},
                      "tracked_files_modified": len(modified)}, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:4])
