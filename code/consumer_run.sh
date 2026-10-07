#!/bin/bash
# Run every prepared consumer case twice in an isolated Singularity sandbox, mirroring
# DAB's per-attempt executor: S2 sees a private writable file_storage; S3 sees symlinks
# into the shared store bind-mounted read-only at /shared. Usage: consumer_run.sh <case_root>
# (case_root/<dataset>/{case*,probe*,store})
set -uo pipefail
P=$PROJECT_DIR
ROOT=$1
module load singularity/4.4.1
IMG=$P/envs/ed-py312-v2.sif
for c in "$ROOT"/*/case* "$ROOT"/*/probe*; do
  [ -d "$c" ] || continue
  for v in S2 S3; do
    d="$c/$v"
    extra=()
    [ "$v" = S3 ] && extra=(-B "$(dirname "$c")/store:/shared:ro")
    timeout 120 singularity exec --containall --no-home --env PYTHONHASHSEED=${ED_HASHSEED:-random} -B "$d:/work" "${extra[@]}" --pwd /work "$IMG" \
      python _run.py > "$d/_out" 2>&1
    echo $? > "$d/_rc"
  done
done
echo CONSUMER_RUN_DONE
