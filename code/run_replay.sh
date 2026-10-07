#!/bin/bash
# Run one replay shard: start the dataset's PostgreSQL / MongoDB servers (if any) in
# node-local /tmp from Singularity images, load the DAB dumps the way DAB does, then
# execute code/replay2.py. Usage: run_replay.sh <dataset> <shard> <n_shards> <out_dir> [repeats] [timeout_s]
# RETIRED (2026-10-02): used only for the M0 smoke test and the M1 robustness run (results/analysis_m1/). It does not
# stop on a non-zero psql exit and checks only for ERROR lines; every other job uses code/run_with_db.sh.
set -euo pipefail
P=$PROJECT_DIR
D=$1; S=$2; N=$3; OUT=$4; REP=${5:-3}; TO=${6:-120}
module load singularity/4.4.1
W=/tmp/$USER-ed-${SLURM_JOB_ID:-$$}-$D-$S
mkdir -p "$W" "$OUT"
export SINGULARITY_TMPDIR=$W
REPO=$P/data/dab/repo
PY="singularity exec --no-home -B /scratch -B $W $P/envs/ed-py312-v2.sif"
cleanup() {
  singularity instance stop "pg$$" >/dev/null 2>&1 || true
  singularity instance stop "mg$$" >/dev/null 2>&1 || true
  rm -rf "$W"
}
trap cleanup EXIT

# db clients of this dataset: "<type>\t<name>\t<path>"
CLIENTS=$($PY python -c "
import yaml,sys
c=yaml.safe_load(open('$REPO/query_$D/db_config.yaml'))['db_clients']
for k,v in c.items(): print(v['db_type'], v.get('db_name',''), v.get('sql_file') or v.get('dump_folder') or v.get('db_path',''), sep='\t')
")

if echo "$CLIENTS" | grep -q '^postgres'; then
  PGPORT=$((20000 + (${SLURM_JOB_ID:-$$} % 20000)))
  singularity instance start -B "$W" -B /scratch "$P/envs/pg16.sif" "pg$$" >/dev/null
  singularity exec instance://pg$$ initdb -D "$W/pg" -U "$USER" --auth=trust -E UTF8 --locale=C >/dev/null
  singularity exec instance://pg$$ pg_ctl -D "$W/pg" -l "$W/pg.log" -w start \
    -o "-k $W -p $PGPORT -c listen_addresses='' -c max_parallel_workers_per_gather=0 -c shared_buffers=1GB"
  export PGHOST=$W PGPORT PGUSER=$USER
  while IFS=$'\t' read -r typ name path; do
    [ "$typ" = postgres ] || continue
    t0=$(date +%s)
    singularity exec instance://pg$$ psql -h "$W" -p "$PGPORT" -U "$USER" -d postgres -q -c \
      "CREATE DATABASE \"$name\" WITH ENCODING='UTF8' LC_COLLATE='C' LC_CTYPE='C' TEMPLATE=template0;"
    singularity exec instance://pg$$ psql -h "$W" -p "$PGPORT" -U "$USER" -d "$name" -q -f "$REPO/query_$D/$path" >/dev/null 2>"$W/psql_$name.err" || true
    echo "pg_load $name $(( $(date +%s) - t0 ))s stderr_lines=$(wc -l < "$W/psql_$name.err")"; head -3 "$W/psql_$name.err"
    # dumps come from PostgreSQL 17 (SET transaction_timeout) and reference role "postgres"; anything else aborts
    if grep -v -e 'unrecognized configuration parameter "transaction_timeout"' -e 'role "postgres" does not exist' "$W/psql_$name.err" | grep -q ERROR; then
      echo "unexpected PostgreSQL restore error for $name" >&2; exit 3
    fi
  done <<< "$CLIENTS"
  singularity exec instance://pg$$ postgres --version
fi

if echo "$CLIENTS" | grep -q '^mongo'; then
  MGPORT=$((40000 + (${SLURM_JOB_ID:-$$} % 20000)))
  mkdir -p "$W/mongo"
  singularity instance start -B "$W" -B /scratch "$P/envs/mongo7.sif" "mg$$" >/dev/null
  singularity exec instance://mg$$ mongod --dbpath "$W/mongo" --bind_ip 127.0.0.1 --port "$MGPORT" --fork --logpath "$W/mongod.log" >/dev/null
  export MONGO_URI="mongodb://127.0.0.1:$MGPORT"
  while IFS=$'\t' read -r typ name path; do
    [ "$typ" = mongo ] || continue
    t0=$(date +%s)
    singularity exec instance://mg$$ mongorestore --quiet --uri "$MONGO_URI" --nsInclude="$name.*" "$REPO/query_$D/$path"
    echo "mongo_load $name $(( $(date +%s) - t0 ))s"
  done <<< "$CLIENTS"
  singularity exec instance://mg$$ mongod --version | head -1
fi

cd "$P"
$PY python code/replay2.py work/v2/calls.parquet "$REPO" "$D" "$S" "$N" "$OUT/$D.$S.parquet" "$REP" "$TO"
