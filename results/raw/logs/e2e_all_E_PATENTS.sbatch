#!/bin/bash
#SBATCH -J ed-eval-18
#SBATCH -A drjieliu99
#SBATCH -p standard
#SBATCH -c 8
#SBATCH --mem=64G
#SBATCH -t 16:00:00
#SBATCH -o $PROJECT_DIR/logs/%x-%j.out
export ED_IMG=$PROJECT_DIR/envs/ed-dab.sif ED_TZ=America/Los_Angeles ED_REPLAY_DIR=work/v2/replay_dab
cd $PROJECT_DIR
export ED_E2E_WORKERS=6
bash code/run_with_db.sh PATENTS -- code/live.py e2e work/v2 data/dab/repo PATENTS work/v2/e2e/all_E_PATENTS.json all E
