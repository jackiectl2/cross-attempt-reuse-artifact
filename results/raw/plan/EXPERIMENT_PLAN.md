# Experiment Plan

**Problem**: How much database work do repeated data-agent attempts on one snapshot regenerate, where does it live, what does it cost, and can it be shared without changing any byte an attempt observes?
**Method Thesis**: In repeated data-agent attempts on one snapshot, reusable database work lives across attempts rather than within sessions; an exact, byte-preserving cross-attempt layer in the agent tool runtime realizes it for certified queries, and exposing one immutable materialization read-only to every attempt's sandbox avoids per-attempt result materialization.
**Date**: 2026-10-01
**Source**: `refine-logs/FINAL_PROPOSAL.md` (READY, 9.25/10, reviewer gpt-5.6-sol)
**Effort**: max (resamples 200 analytic / 5 live; 3 isolated executions per query; 4 baseline families S0–S2 + CIDR metric)
**Compute**: CPU only, Great Lakes `standard`, account `drjieliu99`; 0 GPU-hours; no LLM calls.

## Claim Map

| Claim | Why It Matters | Minimum Convincing Evidence | Linked Blocks |
|-------|-----------------|-----------------------------|---------------|
| **C1 (dominant)**: reusable DB work in repeated data-agent execution lives across attempts, not within sessions, and is substantial at practical N | Tells agent/DB builders which scope to share at; corrects the session-local assumption and the unmeasured CIDR'26 redundancy claim | Validated isolated replay on all 4 engines; cross-attempt share of time and bytes ≫ within-session share, with task-bootstrap CIs excluding the session-only value, already at N = 2–4; robust to leave-one-dataset-out and timeout lower bound | B0, B1, B2, B5 |
| **C2 (supporting, conditional)**: exposing one immutable B-certified materialization read-only to all attempts beats a strong private-copy exact cache | Defines the reuse contract at the agent tool boundary (two consumers, isolated sandboxes) | S3 vs S2: median-task bytes written and peak footprint lower at N = 2–8, ≤5% time regression, direction per engine; 100% byte-identical observations; identical consumer outputs in the Apptainer test; simulator agrees with live runs | B3, B4 |
| **Anti-claim A1**: "the redundancy is a benchmark artefact of 50 trials" | Must show the effect at practical N | N-curves with N = 2, 4, 8 from resampling | B2 |
| **Anti-claim A2**: "the redundant work is cheap probes" | Count ≠ cost | time- and byte-weighted decomposition, concentration analysis | B1, B5 |
| **Anti-claim A3**: "S3 only beats a straw-man cache" | Strong baseline required | S2 memoizes template + payload; S3 differs only in ownership | B3 |

## Paper Storyline
- **Main paper must prove**: B0 (coverage/fidelity table), B1 (scope decomposition), B2 (N-curves), B3 (S0–S3 + capacity), B4 (live calibration + consumer test), B5 (robustness).
- **Appendix / compact diagnostics**: B6 (canonical AST, equal-result oracle with trivial results separated, Contract S increment, CIDR sub-plan reproduction on DuckDB-native SQL).
- **Nice-to-have**: B7 cross-harness validation on public DAB leaderboard submissions.
- **Intentionally cut**: K6 Python pushdown; containment reuse; GDSF/other policies; parallel-schedule headline (kept only as optional sensitivity); new LLM traces.
- **Frontier necessity check**: not applicable — the mechanism is intentionally non-learned; frontier LLMs are the workload.

## Experiment Blocks

### Block B0: Coverage, fidelity and certification (sanity, MUST-RUN)
- **Claim tested**: the replay is a faithful counterfactual for the calls we count.
- **Why**: every later number is conditional on it (reviewer round 1–4).
- **Data**: all 13,482 runs (180 without tool calls stated), all 76,213 query_db calls, 12 datasets, 4 engines.
- **Procedure**: full re-parse keeping complete tool messages and final answers; recompute success labels with each task's `validate.py` (recorded as "recomputed labels", validator revision noted); isolated 3× execution per distinct (database, query); fidelity vs recorded observation (inline: exact payload equality; spilled: exact 10,000-char preview); certificate `certify(q)`; gate funnel: all → successful → fidelity-pass → stable → B-certified.
- **Metrics**: counts and shares (calls / time / bytes) surviving each gate, by engine and dataset; timeout and error classes; mutation-audit count (Python writing/deleting result paths).
- **Success criterion**: fidelity-pass ≥ 90% of successful calls per engine (else that engine's numbers are reported separately with the caveat); funnel reported in full.
- **Failure interpretation**: low fidelity for an engine → exclude it from headline claims, scope claims to engines that pass.
- **Target**: Table 2 (coverage and fidelity funnel).

### Block B1: Where reuse lives — scope decomposition (main anchor, MUST-RUN)
- **Claim tested**: C1, A2.
- **Data**: all fidelity-passing successful calls (headline); all calls incl. errors/timeouts (sensitivity).
- **Compared**: scopes session / other attempts same model+task / other models same task / other tasks same dataset / none; weights calls, isolated time (completed-only; timeout lower bound 120 s), UTF-8 payload bytes, spill-file bytes; exact-text equivalence (headline).
- **Setup**: innermost-scope attribution under 200 random attempt orders (attempt order randomized, within-attempt order preserved); mean ± CI over orders for the cross-model/cross-task split.
- **Success criterion**: cross-attempt (same model+task) share of time and bytes ≥ 3× the within-session share overall and in the task-macro median.
- **Failure interpretation**: if within-session ≈ cross-attempt, the paper reports "reuse is scope-agnostic" and S1 suffices.
- **Target**: Fig. 3 (stacked bars by weight), Table 3 (by model × engine).

### Block B2: Practical N — N-curves (MUST-RUN)
- **Claim tested**: C1, A1.
- **Setup**: for each (model, task) cell, sample N ∈ {1,2,4,8,16,50} attempts without replacement, 200 resamples; compute reusable share and simulated S2 savings (time, bytes) per sample; aggregate per task, then task-macro median/IQR and task-bootstrap 95% CI (resampling tasks).
- **Success criterion**: at N = 4, the median task saves ≥ 20% of isolated DB time or ≥ 30% of payload bytes under S2.
- **Failure interpretation**: if savings appear only near N = 50 → claim restricted to repeated benchmark evaluation (reviewer claims matrix).
- **Target**: Fig. 1 (headline) and Fig. 4 (per engine / per model).

### Block B3: Mechanism comparison S0–S3 with capacity (MUST-RUN)
- **Claim tested**: C2 (conditional), A3; also realizes C1 (S1 vs S2).
- **Compared systems**: S0 no sharing; S1 strong exact cache, session-scoped; S2 strong exact cache, task-scoped, private spill copy per attempt; S3 task-scoped read-only shared store with attempt-local aliases. All admit only B-certified ∩ successful ∩ fidelity-pass ∩ stable calls.
- **Setup**: exact simulation of the cache state machine on all cells, N ∈ {2,4,8}, 200 resamples; budgets B ∈ {∞, 1/16, 1/4, 1} × (distinct result bytes of the task), LRU among refcount-0 objects, bypass when pinned; costs from the isolated cost table plus measured per-op costs (lookup, alias, spill write/copy, consumer read) from B4.
- **Metrics (decisive first)**: bytes written; peak storage footprint; total tool time (exec+fetch+serialize+spill/copy+lookup); p50/p95 per-call latency; cache bypasses; hit rate.
- **Success criterion (promotion rule)**: S3 < S2 on median-task bytes written and peak footprint at N = 2–8, ≤ 5% time regression overall, same direction within each engine (or claim scoped).
- **Failure interpretation**: S3 ≈ S2 → present S3 as the reference implementation; paper identity = workload study + S2-level sharing.
- **Target**: Fig. 6 (S0–S3 across N), Fig. 7 (capacity), Table 4 (cost breakdown).

### Block B4: Live calibration and programmatic-consumer test (MUST-RUN)
- **Claim tested**: simulator validity; C2's two-consumer contract at the real sandbox boundary.
- **Live calibration**: preregistered 12 tasks (3 per engine spanning inline-only / mixed / spill-heavy; per task the model with median calls/run); N = 8; 5 resamples; S0–S3 executed for real (one call at a time, per-attempt processes with private work dirs, shared store opened read-only), B ∈ {∞, 1/4}; report simulator prediction error per metric.
- **Consumer test**: 300 recorded `execute_python` calls stratified over {inline, private spilled, shared spilled} × engines × models, restricted to B-certified inputs and self-contained code, run in Apptainer with `--bind store:/shared:ro`; compare outputs and error classes S2 vs S3; isolation probes (write, delete, in-place dump) on the read-only mount.
- **Success criterion**: simulator relative error ≤ 10% on time and ≤ 1% on bytes; identical outputs ≥ 99% (all divergences explained); probes fail with permission errors confined to the attempt.
- **Target**: Table 4 footnote / Table 6 (calibration), §6 text.

### Block B5: Robustness and claim boundaries (MUST-RUN)
- **Claim tested**: C1 generality within DAB.
- **Analyses**: task-bootstrap CIs; leave-one-dataset-out for B1/B2 headline numbers; per model and per engine; top-1% / top-10-query concentration of reusable time; timeout lower-bound weighting; successful vs failed trajectories (recomputed labels); first-pass vs steady-state timing.
- **Success criterion**: headline cross-attempt dominance holds in ≥ 10 of 12 leave-one-dataset-out folds and for every model.
- **Target**: Table 5.

### Block B6: Diagnostics (NICE-TO-HAVE, compact subsection/appendix)
- Canonical-AST increment over exact text; equal-result oracle (empty and single-scalar results separated); Contract S increment over B; CIDR sub-plan metric (distinct/total sub-plans by size on DuckDB-native SQL, close reproduction of their definition) compared with our exact-text and result-level numbers.
- **Target**: Fig. 8 / appendix table.

### Block B7: Cross-harness validation (NICE-TO-HAVE)
- Public DAB leaderboard submissions (independent agent harnesses, ~5 trials/query): repeat B1/B2 (N ≤ 5) on their traces if downloadable and parseable within 3 hours of effort.

## Run Order and Milestones

| Milestone | Goal | Runs | Decision Gate | Cost | Risk |
|-----------|------|------|---------------|------|------|
| M0 Sanity | Full re-parse; success labels; certificate + unit tests; PG/Mongo server-in-job smoke test | R001–R005 | parser counts reproduce 13,482 / 151,293; PG and Mongo answer a recorded query identically | ~2 node-h | PG/Mongo restore time; validate.py deps |
| M1 Cost table | Isolated 3× execution of all distinct pairs on 4 engines; fidelity comparison | R006–R009 | fidelity ≥ 90% per engine | ~28 single-query core-h (≈ 2–3 h wall, sharded) | PATENTS / heavy queries; timeouts |
| M2 Analysis | B1 decomposition, B2 N-curves, B3 simulation | R010–R012 | C1 success criteria | < 2 node-h | ordering assumptions |
| M3 Validation | B4 live calibration + consumer test | R013–R014 | simulator error and identical-output criteria | ≤ 7 node-h | Apptainer bind mounts; heavy tasks |
| M4 Polish | B5 robustness, B6 diagnostics, figures | R015–R018 | — | ~2 node-h | — |
| M5 Optional | B7 cross-harness | R019 | — | ≤ 2 node-h | data availability |

## Compute and Data Budget
- GPU-hours: 0. CPU: ≈ 45 node-hours total, ≤ 24 h per job (project limit), all as on-demand `sbatch` jobs.
- Data: DAB 12 datasets (8.35 GB, on Neda scratch), trajectories (195.5 MB zip). No annotation.
- Biggest bottleneck: isolated replay of heavy SQLite queries (PATENTS) and PostgreSQL/Mongo restore inside jobs.

## Risks and Mitigations
- **Contract-B coverage small** → bounds C2 only; C1 is computed on all fidelity-passing successful calls; report B coverage prominently and the Contract S increment as sensitivity.
- **PG/Mongo replay fidelity** → per-engine reporting; scope claims to passing engines.
- **Timing noise on shared nodes** → 3 repeats, medians, first-pass vs steady-state; byte metrics are noise-free.
- **S3 ≈ S2** → preregistered demotion to reference implementation.
- **Time** → M0–M3 must finish by 2026-10-03 12:00 UTC to leave writing time.

## Final Checklist
- [x] Main paper tables are covered (Tables 2–5, Figs 1, 3, 4, 6, 7)
- [x] Novelty is isolated (S1 vs S2 scope; S2 vs S3 ownership)
- [x] Simplicity is defended (exact keys only; strong baseline; diagnostics show semantic/containment increments are not needed for the claim)
- [x] Frontier contribution explicitly not claimed (LLM agents are the workload)
- [x] Nice-to-have runs separated (B6, B7)

## Amendment A1 — 2026-10-01 (post-smoke, before full analysis; original text above left unchanged)

1. **Run count correction.** The archive has 13,482 runs of which **30** contain no tool calls (the "180" above was a pilot artefact from counting runs with query_db calls). Parser asserts 13,482 runs / 151,293 tool calls / 76,213 query_db calls / 30 runs without tool calls.
2. **First-timeout exception.** A distinct query whose first isolated execution reaches the 120 s cap is not repeated (no completed execution exists to time). Completed-only weights exclude timeouts; the lower-bound weight assigns the cap.
3. **Contract E (exploratory, added after measuring Contract-B coverage).** The smoke run showed Contract B admits ≈2% of calls (dominant reasons: LIMIT without a covering ORDER BY; multi-row results without a total order; Mongo interface has no sort). Contract B remains the preregistered headline contract. We additionally report **Contract E**: read-only, no volatile/clock functions, allowlisted functions, no TABLESAMPLE, but no ordering/shape requirement; its byte stability rests on the pinned runtime (engine versions, DuckDB threads=1, PostgreSQL without parallel workers, static snapshot) and is checked empirically by 3-run stability and byte-identity with the observation recorded in the original DAB run. E and the fidelity gate (≈ Contract S) are reported as relaxations, never as the headline guarantee.
4. **Recomputed success labels.** Labels come from each task's `validate.py` at DAB revision f6b1ad07 (pass 4,543 / fail 8,939 / validator error 0); these may differ from the originally published labels because DAB re-scored validators.
5. **Mongo `limit: null`.** Replay mirrors DAB's actual code (`if limit is not None: cursor.limit(limit)`).
6. **Consumer test execution.** Agent code is embedded with `repr` (DAB embeds it in triple quotes, which breaks on code containing triple quotes); cases are restricted to self-contained, non-mutating calls; 25 cases per dataset (≈300 total) stratified by model × spilled input.
7. **Live calibration processes.** Each (resample, budget, system) runs in a fresh process with randomized system order; attempts of one system/resample share that process (cache/store state is external in files).

## Amendment A2 — 2026-10-01 (after the full results and the round-1 external review; post-hoc)

Added analyses and runs (none changes a preregistered criterion; the scorecard in EXPERIMENT_RESULTS.md reports the original criteria):
1. **B4-e2e** end-to-end equivalence pass (`code/live.py e2e`): (a) the 12 calibration cells, 2 resamples, budgets ∞ and 1/4, Contracts E and B; (b) every (task, model) cell (54 × 5), 1 resample, budget ∞, Contract E. N = 8. Metrics: per-call mismatches of S1/S2/S3 vs S0 in tool-message bytes, inline value, bound path and bound-file bytes (split hit vs miss); recorded `execute_python` outputs + return codes vs S0; S3 write/delete probes and store-object integrity.
2. **B4 re-calibration** with per-call footprint accounting under both lifetimes (`peak_retained`, `peak_seq`, admitted-only variants) and DAB tool-message construction on every call; simulator footprint validated against it.
3. **B5 additions** (`code/extra_analyses.py`): share of task/model cells and tasks reaching 10/20/30% S2 saving at N = 2/4/8; B1/B2 with the top 1% and top 10 reusable keys removed; online-E exposure (static E admission without fidelity/stability filters, by 3-run outcome class and fidelity); MongoDB fidelity by inline/spilled.
4. **Artifacts**: raw tables (calls, runs, replay cost tables, simulation outputs), parse metadata and job logs with a provenance manifest (`results/raw/`).

## Amendment A3 — 2026-10-01 (effort: beast; post-hoc; ablation design by the cross-model reviewer)

Ablation list designed by gpt-5.6-sol (`review-stage/ABLATION_RESPONSE.md`, trace `.aris/traces/ablation-planner/`), executor feasibility notes in brackets. All are reported as post-hoc analyses.

| ID | Ablation | Mode | Priority | Status / plan |
|---|---|---|---|---|
| A1 | Sharing-scope ladder: S0, S1, same model+task, task across models, dataset across tasks | sim (+ existing live) | 1 | Mostly done (B1 shares, S1/S2); add wider-scope savings to the simulator, one figure |
| A2 | Ownership isolation S2 vs S3 (same key/admission/scope/capacity) | both | 1 | Done |
| A3 | Copy-on-write (reflink) private-copy baseline | live | 1 | Filesystem capability probe first; if unsupported, report and stop |
| A4 | Admission safety–payoff frontier: B, static E, offline fidelity-equivalence oracle | both | 1 | Mostly done; E end-to-end pass rerun after the S2 store-naming fix |
| A5 | Cache inline only / spilled only / both | both | 1 | Simulator variant + live cells |
| A6 | Cache preview/template inputs vs regenerate the preview from the full result on hit | both | 2 | Simulator + live variant |
| A7 | Capacity knee: add budgets 1/8, 1/2 | sim | 2 | Simulator |
| A8 | Replacement policy: LRU, FIFO, no eviction (admit until full) | sim | 3 | Simulator |
| A9 | N sweep + threshold shares | sim | 1 | Done |
| A10 | True concurrent schedule: concurrency 1/2/4/8 at N = 8 | both | 1 | Event-driven simulator, then 6 live cells |
| A11 | Concurrent-miss policy: optimistic execute + publish-time recheck vs single-flight | both | 2 | With A10 |
| A12 | Pinning and overflow: pin + bypass vs pin + wait; count would-evict-live-alias without pinning | both | 1 | With A10 |
| A13 | Private-file lifetime | both | 1 | Done (live-validated) |
| A14 | Reuse key: exact text vs whitespace/comment-normalized vs canonical AST | sim | 3 | Simulator (with X2) |
| A15 | Hit-path cost stack by component | both | 2 | Simulator decomposition + live phase timing |

Also from PIPELINE_PLAN_BEAST.md: X1 cross-harness validation, X2 diagnostics (equal-result oracle, Contract S), X3 engine-level result-cache baseline (S-DB) in the simulator, X5 seed replication, X6 statistical tests, X7 experiment audit + clean-room reproduction, X8 result-to-claim. Reviewer-listed unnecessary ablations (preview-threshold changes, more replacement policies, metadata-charge sweeps, more resamples, buffer-cache flushing) are not run.
