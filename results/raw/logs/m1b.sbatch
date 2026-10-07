#!/bin/bash
#SBATCH -J ed-eval-06
#SBATCH -A drjieliu99
#SBATCH -p standard
#SBATCH -c 2
#SBATCH --mem=48G
#SBATCH -t 10:00:00
#SBATCH --array=0-33
#SBATCH -o $PROJECT_DIR/logs/%x-%A_%a.out
set -euo pipefail
PLAN=(PATENTS:12 stockmarket:4 GITHUB_REPOS:4 DEPS_DEV_V1:4 PANCANCER_ATLAS:2 crmarenapro:2 agnews:1 bookreview:1 googlelocal:1 music_brainz_20k:1 stockindex:1 yelp:1)
i=$SLURM_ARRAY_TASK_ID
for e in "${PLAN[@]}"; do d=${e%%:*}; n=${e##*:}; if [ $i -lt $n ]; then break; fi; i=$((i-n)); done
echo "task $SLURM_ARRAY_TASK_ID -> $d shard $i/$n"
export ED_IMG=$PROJECT_DIR/envs/ed-dab.sif ED_TZ=America/Los_Angeles
bash $PROJECT_DIR/code/run_with_db.sh $d -- code/replay2.py work/v2/calls.parquet data/dab/repo $d $i $n work/v2/replay_dab/$d.$i.parquet 3 120
