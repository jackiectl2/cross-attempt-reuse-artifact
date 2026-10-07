# Artifact: Cross-Attempt Result Reuse in Repeated LLM Data-Agent Workloads

Supplementary material of the EDBT 2027 submission "Reuse Lives Across Attempts: Database Work in Repeated LLM
Data-Agent Runs". Everything in the paper can be checked at three levels of effort.

| Level | What it does | Needs | Time |
|---|---|---|---|
| 1. Check the numbers | recompute every number printed in the paper from the result files | Python ≥ 3.9, standard library only | seconds |
| 2. Rebuild tables and figures | regenerate every table and figure from the result files | level 1 + matplotlib for the figures | a minute |
| 3. Rebuild the results | rerun the analyses from the raw tables, or the replay from the public inputs | Singularity/Apptainer images (recipes in `envs/`), a Linux machine or cluster | minutes to 24 CPU-job-hours |

## Layout

```
README.md            this file
MANIFEST.sha256      SHA-256 of every file in the archive
code/                parser, certificates, replay, analysis, simulator, live runner, statistics, job submission
envs/                container build scripts, pip package lists, SHA-256 of the images we used
paper/               LaTeX sources, table and figure builders, number ledger and its checker
results/             aggregated result files (JSON / CSV) cited by the paper
results/raw/         raw tables (Parquet), job ledger, manifest, and the six job scripts and logs the number ledger reads; see results/raw/README.md
review-stage/contract_b_review/   prompts, responses and reviewed versions of the six adversarial reviews of Contract B
```

All `.json` files are strict JSON: `NaN` is written as `null` and `±Infinity` as the strings `"Infinity"` /
`"-Infinity"`. The lean archive omits `results/raw/sim/` (294 MB of per-resample simulation rows); step 3a regenerates
them.

## 1. Check the numbers (no dependencies)

```bash
python3 paper/check_numbers.py --coverage
```

`paper/NUMBERS_LEDGER.csv` has one row per number in the paper: where it is printed, the printed form, and a Python
expression over files in `results/` (helpers in `paper/ledger.py`). Fixed parameters (versions, caps, counts of orders
and resamples, thresholds) are read from `code/`, `envs/`, the job scripts and logs in `results/raw/logs/` and the
experiment plan in `results/raw/plan/`. The checker evaluates every expression, compares
it with the printed form, and lists, as candidates for missing rows, the numeric tokens in `paper/sections/*.tex` and
`paper/figures/*_float.tex` that no row prints. It strips comments, the braced argument that follows `\cite`, `\ref`, `\cref`, `\Cref`, `\label`, `\input`
and `\includegraphics`, and the first braced argument of `\subcaptionbox` first, and it does not check single digits
or four-digit years (19xx, 20xx).
Coverage is by printed value: a token counts as covered when some row prints the same string; the checker does not
tie a row to one occurrence. Expected output: `N ledger rows, 0 mismatches` and no `UNLEDGERED` line.

## 2. Rebuild tables and figures

```bash
python3 paper/figures/gen_tables.py         # every table (paper/figures/tab*.tex), its ledger rows, and those of Figure 2(b)
python3 paper/ledger_text.py                # ledger rows of the numbers in the text and captions
python3 paper/figures/gen_fig1_teaser.py    # Figure 1 (a-c); needs matplotlib
python3 paper/figures/gen_fig2_locality.py  # Figure 2 (a, b)
python3 paper/figures/gen_fig3_certify.py   # Figure 3 (a, b)
python3 paper/figures/gen_fig4_footprint.py # Figure 4 (b, c); Figure 4(a) is a diagram image (paper/figures/fig4a_ownership.png)
cd paper && latexmk -pdf main.tex           # the PDF (acmart.cls and the EDBT macros come from the EDBT template)
```

The scripts read only `results/`. Which file feeds which table or figure:

| Item | Result files |
|---|---|
| Table 1 (datasets) | `results/dataset_table.json` |
| Table 2 (replay by engine) | `results/analysis_dab_r3/B0_funnel.csv`, `B0_outcome_classes.csv`, `B0_fidelity.csv`, `B0_mismatch_kinds.csv` |
| Table 3 (tiers), Table 6 (funnel) | `results/static_tiers.json`, `results/analysis_dab_r3/B0_funnel.csv` |
| Table 4 (Contract B in full) | no numbers; the rule is `certify_sql_b` in `code/certify.py` |
| Table 5 (other harnesses) | `results/xharness/results.json`, `results/xharness/results_dab_main5.json` |
| Table 7 (designs) | `results/ablate/base_E.json`, `results/ablate/batchhold_E.json`, `results/live/costs.json`, `results/live3/costs.json` |
| Table 8 (trace replay, concurrency) | `results/e2e_trace/summary_*.json`, `results/e2e_trace/mismatch_diag.json`, `results/concurrent_live/*.json` |
| Table 9 (pre-specified analyses) | `results/analysis_dab_r3/results_B1.json`, `results_B23.json`, `B0_fidelity.csv`, `B0_funnel.csv`, `results/live2/validate_E.json`, `results/live2_certv3/validate_B.json`, `results/live/consumer_summary*.json`, `results/b1_planned.json`; thresholds: `results/raw/plan/EXPERIMENT_PLAN.md` |
| Figure 1 | `results/analysis_dab_r3/results_B1.json`, `results_B23.json`, `results/claim_support.json`, `results/static_share.json` |
| Figure 2 (per task; conditions) | `results/analysis_dab_r3/B1_per_task.csv`, `results/analysis_dab_r3/results_B1.json`, `results/extra_analyses.json`, `results/analysis_m1/results_B1.json`; the values printed in panel (b) are the ledger rows `fig2/...` |
| Figure 3 (funnel per engine; verdicts of static B) | `results/analysis_dab_r3/B0_funnel.csv`, `results/analysis_dab_r3/B0_cert_reasons.csv` |
| Figure 4 | `results/ablate/base_E.json`, `results/ablate/batchhold_E.json`, `results/live2/calib_E_*.json` |

## 3. Rebuild the results

### Environment

The analyses ran in Singularity 4.4 images built from official Docker images:

| Image | Base | Built by | Packages | Used for |
|---|---|---|---|---|
| `ed-dab.sif` | `python:3.12.12-slim` | `envs/build_env3.sbatch` | `envs/ed-dab.freeze.txt` (DuckDB 1.3.1, pandas 2.3.0) | the pinned replay runtime, live runs |
| `ed-py312-v2.sif` | `python:3.12-slim` | `envs/build_env.sbatch`, `envs/build_env2.sbatch` | `envs/ed-py312-v2.freeze.txt` | analysis, simulation, statistics; robustness replay with newer libraries |
| `pg16.sif`, `mongo7.sif` | `postgres:16`, `mongo:7.0` | `envs/build_env2.sbatch` | PostgreSQL 16.15, MongoDB 7.0.43 | database servers started inside each job (`code/run_with_db.sh`) |

`envs/SHA256SUMS.sif.txt` lists the SHA-256 of the image files we used. The build scripts are Slurm batch scripts; the
`singularity build` / `pip install` lines in them run unchanged on any machine with Singularity or Apptainer. Set
`P` at the top of `code/run_with_db.sh` and `code/cpu_submit.py` to your project directory.

### 3a. From the raw tables (minutes)

`results/raw/v2/` holds the parsed call and run tables and the replay cost tables. `results/raw/README.md` lists every
table and the command that rebuilds each aggregate. The main ones, with `<v2>` a directory that contains
`calls.parquet`, `runs.parquet`, `replay_dab/`, `p2_audit.json` (copy `results/p2_audit.json` there: the audited
schema that Contract B binds names to) and `live/costs.json` (copy `results/live/costs.json` there):

```bash
PY="singularity exec --no-home envs/ed-py312-v2.sif python"
export SINGULARITYENV_ED_REPLAY_DIR=<v2>/replay_dab
$PY code/certify.py                                             # certificate unit tests (181 assertions)
$PY code/cert_reasons.py <v2>/replay_dab <v2>/p2_audit.json <out>/cert_reasons.json   # Contract B / E outcomes by reason
$PY code/analyze2.py <v2> <out> 200 200 16 <v2>/live/costs.json # funnel, scope shares, N-curves, B/E simulations (16 cores, ~16 min)
$PY code/stats_tests.py <out> <out>/stats.json                  # Wilcoxon + Holm
$PY code/extra_analyses.py <v2> <out> <out>/extra_analyses.json 50 50 16
$PY code/claim_support.py <v2> <out>/claim_support.json 200 8
$PY code/static_tiers.py <v2> <out>/static_tiers.json
$PY code/static_share.py <v2> <out>/static_share.json 200 8
$PY code/b1_planned.py <v2> <out>/b1_planned.json 200 16         # B1 / B5 on the population the plan prescribes
$PY code/paper_stats.py <v2> <out> <out>/paper_stats.json
$PY code/sim_ablate.py run <v2> <out>/base_E.json base 50 16 E results/live3/costs.json  # likewise base_B / base_F and the other variants
$PY code/concurrent_sim.py run <v2> <out>/concurrent.json 10 16
$PY code/reproduce.py results results/analysis_dab_r3 <out>     # compares every numeric leaf with the reported run
```

### 3b. From the public inputs (replay: 34 CPU jobs, 24.1 job-hours)

1. Inputs: the DAB repository and trajectory release at revision `f6b1ad07a61da2077ae7e074416900dbb452d1bc`
   (`code/` expects the repository under `data/dab/repo` and `all_trajectories.zip` under `data/dab/`), and the trace
   archives attached to DAB leaderboard pull requests #94, #96, #99 and #103
   (`results/raw/xharness/calls_provenance.json` records each archive's URL and size). The inputs are not copied into
   this archive. `results/raw/dab_inputs.json` (`python code/dab_inputs.py data/dab results/dataset_table.json
   <out.json>`) records the repository remote and revision; that no tracked file outside `query_*/query_dataset/`
   differs from the revision; that every task's `validate.py` (the benchmark's own answer validators, which
   `code/parse2.py` runs to label each run) and every `db_config.yaml` on disk is the Git blob of that revision (54 of
   54, 12 of 12); that the 25 data files (8.35 GB) match the sizes and SHA-256 values of DAB's own
   `dataset_manifest.tsv`; and the download URL, size and SHA-256 of `all_trajectories.zip`. Running the script on a
   fresh download checks it against the same values.
2. Parse: `python code/parse2.py data/dab/all_trajectories.zip data/dab/repo <v2>`.
3. Audit the snapshot (schema, precondition P2 and catalog checks of Contract B), one dataset at a time, and merge:
   `ED_IMG=envs/ed-dab.sif bash code/run_with_db.sh <dataset> -- code/p2_audit.py data/dab/repo <dataset> <dir>/<dataset>.json`,
   then `python code/p2_audit.py merge <dir> <v2>/p2_audit.json`.
4. Replay each dataset shard in the pinned runtime, three executions per query, 120 s cap:
   `ED_IMG=envs/ed-dab.sif bash code/run_with_db.sh <dataset> -- code/replay2.py <v2>/calls.parquet data/dab/repo <dataset> <shard> <n_shards> <v2>/replay_dab/<dataset>.<shard>.parquet 3 120`
   (`code/run_with_db.sh` starts PostgreSQL and MongoDB in node-local `/tmp` and loads DAB's dumps).
5. Live runs: `code/live.py` (`select`, `calibrate`, `costs`, `validate`, `e2e`, `e2e-summary`, `concurrent`); cross-harness
   analysis: `code/xharness.py` (`extract`, `analyze`, `dab-main`, `replay-input`, `costs`). Each script's docstring gives
   its arguments; `results/raw/logs/*.sbatch` and `logs/cpu_queue/*.tasks` in `results/raw/logs/` are the exact job
   scripts we ran, and `results/raw/job_ledger.json` gives every job's final state.

## Provenance notes

- Contract B has three versions, selected in `code/certify.py` by the environment variable `ED_CONTRACT_B`.
  Unset (the reported rule): the schema-bound whitelist described in the paper; it needs the audited schema
  `results/p2_audit.json` (`code/p2_audit.py`: 12 SQLite, 9 DuckDB and 5 PostgreSQL databases, 19,772 stored columns,
  none with two values that compare equal and print differently; catalog checks pass in all 26).
  `ED_CONTRACT_B=rules5`: the rule of the experiment plan, which also admitted `SUM`, `AVG` and the variance family,
  explicit `COLLATE` clauses and MongoDB lookups with extra predicates (`results/analysis_dab_r1/` and the files
  named `*_certv1*`). `ED_CONTRACT_B=rules7`: rules 1-7, which excluded those (`results/analysis_dab_r2/`, the files
  named `*_rules7*`, `results/live2_certv2/`); an adversarial review showed that it admits
  `SELECT a, b FROM t GROUP BY a ORDER BY a, b` on SQLite, where `b` comes from an arbitrary row of each group.
- `results/analysis_dab_r3/` is the reported analysis. `results/analysis_dab_r3/compare_r2_r3_summary.json` and
  `results/analysis_dab_r2/compare_r1_r2_summary.json` count the numeric leaves that differ between consecutive
  analyses: only Contract-B entries. `results/reproduce/` holds the clean-room re-parse and re-analysis compared with
  the reported run. `results/cert_reasons.json`: Contract B and E outcomes by reason over the distinct queries and
  their calls, with example queries per outcome.
- `review-stage/contract_b_review/`: for each of the six rounds of adversarial review of Contract B, the prompt, the
  reviewer's verbatim response and the `certify.py` it reviewed (round 1: rules 1-7 with a patch; rounds 2-6: the
  whitelist). Rounds 1-5 found admitted queries whose bytes can differ or admitted operations that can raise under one
  plan and not another; round 6 found no query whose bytes can differ and one operation that can raise
  (PostgreSQL `LENGTH(bytea, encoding)`). Each finding is a case of the unit suite. After round 6 the rule was only
  restricted: each whitelisted node may carry only its plain arguments.
- `results/xharness/static_e_heldout.json` (`code/xh_static_e.py`): a held-out check of the static E rule on the
  queries of the four leaderboard harnesses. `frozen_rule` compares the Contract E verdicts of the `certify.py` in
  `results/code_snapshot/code-2026-10-02.tar.gz`, taken before those queries were replayed, with the current rule.
- `results/static_share.json` also holds a bound that does not depend on any certificate: `order_free` marks the
  calls whose replayed result has at least two distinct rows and whose statement has no top-level `ORDER BY` (MongoDB
  finds through DAB's interface cannot sort); `admitted_by_order_fixed` is the share of the cross-attempt reusable
  work in the remaining calls, an upper bound for any rule that guarantees the bytes across plans.
- `results/schema_collations.json`: no SQLite or DuckDB database of the workload declares a collation;
  `results/n_weights.json`: the N = 4 / N = 8 saving under three time weights;
  `results/static_tiers.json`, `results/static_share.json`: what the static rules admit before any post-hoc gate.
- Operation costs. Every simulation (`code/analyze2.py`, `code/sim_ablate.py`, `code/concurrent_sim.py`) charges the
  lookup, alias and copy costs of the first live calibration, `results/live/costs.json`; each result file records them
  under `costs`. `code/sim_ablate.py run` charges a clone the cost measured in the copy-on-write calibration
  (`results/live3/costs.json`, recorded under `opts.clone_s`). `results/live2/costs.json` and `results/live3/costs.json`
  are later measurements of the same costs; the validation of the simulator against a live run
  (`results/live2/validate_*.json`, `results/live3/validate_variants.json`) charges the costs of that run.
- `results/raw/plan/`: the experiment plan with its dated amendments, and `provenance.json`
  (`code/plan_provenance.py`): when the original text and each amendment were written, that the text before the first
  amendment is unchanged, and when the pilot, replay and analysis jobs ran. The write times come from the authoring
  session's log; the four log records used (the write of the plan and the commands that appended its amendments) are in
  `session_log_excerpt.jsonl`, and `python code/plan_provenance.py results/raw/plan/session_log_excerpt.jsonl
  results/raw/plan/EXPERIMENT_PLAN.md results/raw/job_ledger.json <out_dir>` rebuilds the record from them. The job
  times come from `results/raw/job_ledger.json`. A pilot replay of the SQLite and DuckDB calls preceded the plan.
- `results/ablate/*.json` (except `concurrent*.json` and the files named `*_certv1*`, `*_v1_*` and `*_refuted*`)
  come from one run of the current simulator on 2026-10-03 (cluster directory `work/v2/ablate_clone/`). Against the
  files they replace, only S2c fields changed: tool time (a clone had been charged the alias cost) and, in ten files
  other than `base_B`, `base_E` and `batchhold_E`, the S2c bytes and footprint, which predated the extent accounting
  of `base_E`. No other leaf changed. `base_B.json` was then rerun with the final Contract B (cluster directory
  `work/v2/certv3/`); `base_B_rules7.json` is the file it replaced. `base_Es.json` (gate `Es`) admits every call of
  the static E rule whose replay succeeded, without the post-hoc fidelity gate of the E-eval subset.
- The job name `ed-eval-53` was used twice: job 63143226 (task files kept as
  `results/raw/logs/cpu_queue/eval-53_first_use*.tasks`) and the simulation reruns from job 63144008 on
  (`eval-53*.tasks`, 12 jobs; the two remaining variants ran as `ed-eval-59` and `ed-eval-60` after their first
  submissions, 63144017 and 63144074, were cancelled while pending and left no log). Every log starts with the
  command it ran.
- `results/analysis_seed*/` are four seed replications and `results/analysis_m1/` the replay with newer library
  versions; both predate the final Contract B and are used only for quantities that do not depend on it.
  `results/analysis_m1/` and `results/analysis_dab/` (the first analysis, run before the live calibration) charge
  placeholder operation costs, recorded in their `costs` field; the paper uses only the scope shares of
  `analysis_m1`, which do not depend on them.
- Timing was measured on shared cluster nodes (`results/raw/cluster_nodes.json`).

## What is not shipped

The logs and scripts of the cluster jobs are not part of this archive, except six files under `results/raw/logs/` that
the number ledger reads. `results/raw/job_ledger.json` keeps the final state, exit code and elapsed time of every job.
In the shipped copy, site-specific paths, the cluster user name and the billing accounts are replaced by placeholders
(`$PROJECT_DIR`, `$SCRATCH_DIR`, `$HOME`, `user`, `ACCOUNT`). `results/raw/raw_manifest.json` lists the files of the
original bundle with their original checksums, so files that were removed or had identifiers replaced do not match it.
Passages of this README that refer to `results/raw/logs/*.sbatch` or `logs/cpu_queue/` describe the original bundle.

