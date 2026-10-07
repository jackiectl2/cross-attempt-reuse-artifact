#!/bin/bash
#SBATCH -J ed-build-03
#SBATCH -A drjieliu99
#SBATCH -p standard
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 01:30:00
#SBATCH -o $PROJECT_DIR/logs/%x-%j.out
set -euo pipefail
P=$PROJECT_DIR
W=/tmp/$USER-ed-build-$SLURM_JOB_ID; mkdir -p $W/tmp $W/cache
export SINGULARITY_TMPDIR=$W/tmp SINGULARITY_CACHEDIR=$W/cache TMPDIR=$W/tmp
module load singularity/4.4.1
cd $W
singularity pull pg16.sif docker://postgres:16
singularity pull mongo7.sif docker://mongo:7.0
singularity exec pg16.sif postgres --version
singularity exec mongo7.sif mongod --version | head -2
singularity exec mongo7.sif sh -c "command -v mongorestore && mongorestore --version | head -1 || echo NO_MONGORESTORE"
singularity build --sandbox sb $P/envs/ed-py312.sif
singularity exec --writable --no-home sb pip install --no-cache-dir psycopg2-binary sqlalchemy pymongo 2>&1 | tail -2
singularity exec --no-home sb python -c "import psycopg2, sqlalchemy, pymongo, duckdb; print(\"ok\", psycopg2.__version__, sqlalchemy.__version__, pymongo.__version__)"
singularity exec --no-home sb pip freeze > $P/envs/ed-py312-v2.freeze.txt
singularity build ed-py312-v2.sif sb
for f in pg16.sif mongo7.sif ed-py312-v2.sif; do cp $f $P/envs/$f; done
sha256sum $P/envs/pg16.sif $P/envs/mongo7.sif $P/envs/ed-py312-v2.sif pg16.sif mongo7.sif ed-py312-v2.sif
rm -rf $W
echo BUILD_DONE
