#!/bin/bash
#SBATCH -J ed-eval-15
#SBATCH -A drjieliu99
#SBATCH -p standard
#SBATCH -c 16
#SBATCH --mem=96G
#SBATCH -t 04:00:00
#SBATCH -o $PROJECT_DIR/logs/%x-%j.out
module load singularity/4.4.1
cd $PROJECT_DIR
singularity exec --no-home -B /scratch --env ED_REPLAY_DIR=work/v2/replay_dab envs/ed-py312-v2.sif python code/extra_analyses.py work/v2 work/v2/analysis_dab_ops work/v2/extra_analyses.json 50 50 16
