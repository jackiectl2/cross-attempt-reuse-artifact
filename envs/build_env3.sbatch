#!/bin/bash
#SBATCH -J ed-build-04
#SBATCH -A drjieliu99
#SBATCH -p standard
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 01:00:00
#SBATCH -o $PROJECT_DIR/logs/%x-%j.out
set -euo pipefail
W=/tmp/$USER-ed-build-$SLURM_JOB_ID; mkdir -p $W/tmp $W/cache
export SINGULARITY_TMPDIR=$W/tmp SINGULARITY_CACHEDIR=$W/cache TMPDIR=$W/tmp
module load singularity/4.4.1
cd $W
singularity build --sandbox sb docker://python:3.12.12-slim
singularity exec --writable --no-home sb mkdir -p /scratch /nfs
singularity exec --writable --no-home sb pip install --no-cache-dir duckdb==1.3.1 pandas==2.3.0 numpy==2.2.6 sqlalchemy==2.0.41 psycopg2-binary==2.9.9 pymongo==4.13.2 pyarrow==20.0.0 sqlglot==30.21.0 pyyaml zstandard 2>&1 | tail -2
singularity exec --no-home sb python -c 'import duckdb, pandas, numpy, sqlalchemy, psycopg2, pymongo, sqlglot; print("ok", duckdb.__version__, pandas.__version__, numpy.__version__, sqlalchemy.__version__, psycopg2.__version__, pymongo.__version__, sqlglot.__version__)'
singularity exec --no-home sb pip freeze > $PROJECT_DIR/envs/ed-dab.freeze.txt
singularity build ed-dab.sif sb
cp ed-dab.sif $PROJECT_DIR/envs/ed-dab.sif
sha256sum ed-dab.sif $PROJECT_DIR/envs/ed-dab.sif
rm -rf $W
echo BUILD_DONE
