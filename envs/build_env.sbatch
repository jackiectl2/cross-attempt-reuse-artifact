#!/bin/bash
#SBATCH -J ed-build-02
#SBATCH -A drjieliu99
#SBATCH -p standard
#SBATCH -c 4
#SBATCH --mem=16G
#SBATCH -t 01:00:00
#SBATCH -o $PROJECT_DIR/logs/%x-%j.out
set -euo pipefail
P=$PROJECT_DIR
W=/tmp/$USER-ed-build-$SLURM_JOB_ID
mkdir -p $W
export SINGULARITY_TMPDIR=$W/tmp SINGULARITY_CACHEDIR=$W/cache TMPDIR=$W/tmp
mkdir -p $SINGULARITY_TMPDIR $SINGULARITY_CACHEDIR
module load singularity/4.4.1
cd $W
singularity build --sandbox sb docker://python:3.12-slim
singularity exec --writable --no-home sb mkdir -p /scratch /nfs
singularity exec --writable --no-home sb pip install --no-cache-dir \
  numpy pandas pyarrow duckdb sqlglot zstandard lz4 orjson xxhash psutil scipy matplotlib tqdm \
  huggingface_hub datasets chdb tiktoken 2>&1 | tail -5
singularity exec --no-home sb python -c "import duckdb, pyarrow, sqlglot, chdb, datasets, zstandard; print(\"ok\", duckdb.__version__, pyarrow.__version__, sqlglot.__version__)"
singularity exec --no-home sb pip freeze > $P/envs/ed-py312.freeze.txt
singularity build $W/ed-py312.sif sb
cp $W/ed-py312.sif $P/envs/ed-py312.sif
sha256sum $P/envs/ed-py312.sif $W/ed-py312.sif
rm -rf $W
echo BUILD_DONE
