#!/bin/bash
# Start a DAB dataset's PostgreSQL / MongoDB servers (if any) in node-local /tmp from
# Singularity images, load the dumps the way DAB does, then run a Python command in the
# analysis image with PGHOST/PGPORT/PGUSER/MONGO_URI exported.
# Usage: run_with_db.sh <dataset> -- <python args...>
set -euo pipefail
P=$PROJECT_DIR
D=$1; shift; [ "$1" = "--" ] && shift
module load singularity/4.4.1
W=/tmp/$USER-ed-${SLURM_JOB_ID:-$$}-$D-w$$
mkdir -p "$W"
export SINGULARITY_TMPDIR=$W
REPO=$P/data/dab/repo
IMG=${ED_IMG:-$P/envs/ed-py312-v2.sif}
export TZ=${ED_TZ:-America/Los_Angeles}          # DAB's recorded timestamptz offsets are US/Pacific
PY="singularity exec --no-home -B /scratch -B $W $IMG"
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
    -o "-k $W -p $PGPORT -c listen_addresses='' -c max_parallel_workers_per_gather=0 -c shared_buffers=1GB -c timezone=$TZ"
  export PGHOST=$W PGPORT PGUSER=$USER
  while IFS=$'\t' read -r typ name path; do
    [ "$typ" = postgres ] || continue
    t0=$(date +%s)
    singularity exec instance://pg$$ psql -h "$W" -p "$PGPORT" -U "$USER" -d postgres -q -c \
      "CREATE DATABASE \"$name\" WITH ENCODING='UTF8' LC_COLLATE='C' LC_CTYPE='C' TEMPLATE=template0;"
    if ! singularity exec instance://pg$$ psql -h "$W" -p "$PGPORT" -U "$USER" -d "$name" -q -f "$REPO/query_$D/$path" >/dev/null 2>"$W/psql_$name.err"; then
      echo "psql failed for $name" >&2; cat "$W/psql_$name.err" >&2; exit 3
    fi
    echo "pg_load $name $(( $(date +%s) - t0 ))s stderr_lines=$(wc -l < "$W/psql_$name.err")"; head -3 "$W/psql_$name.err"
    # dumps come from PostgreSQL 17 (SET transaction_timeout) and reference role "postgres"; anything else aborts
    if grep -v -e 'ERROR:  unrecognized configuration parameter "transaction_timeout"' -e 'ERROR:  role "postgres" does not exist' "$W/psql_$name.err" | grep -qE 'ERROR|FATAL|PANIC'; then
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
$PY python "$@"
