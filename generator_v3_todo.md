# Generator V3 TODO — Quality Roadmap

**Goal:** turn the now-correct Generator V3 engine into a generator that spends scarce simulations on economically plausible, diverse hypotheses.

**Current state:** P0–P15 are implementation-complete and CI-covered. Equal-budget replay infrastructure exists, but the live V3 campaigns recorded **0 IS passes from 138 simulations** versus a historical baseline around **23%**. V2 therefore remains the default.

---

# Completed implementation — P0–P15

The old checklist is intentionally compressed. Detailed regressions remain in the test suite and git history.

- [x] **P0–P3 — representation:** source profiles, four identity levels, typed AST, compatibility checks, role-aware motifs, deterministic recipes.
- [x] **P4–P7 — search mechanics:** explore/exploit/mutate/crossover, real structural mutations, exact plan→materialization hashes, archive loop, metadata-aware crossover, multi-parent novelty.
- [x] **P8–P11 — evidence/provenance:** archive-aware ranking, corrected adaptive statistics, complete queued/skipped lineage, explicit V2/V3 subsystem versions.
- [x] **P12–P15 — operation/evaluation:** V3 CLI, dry plan, diversity report, point-in-time replay, archive-V2 baseline, full regression suite and GitHub Actions execution.
- [x] V2 remains the live default.

No new feature should reopen P0–P15 unless it breaks one of these contracts.

---

# P16 — Small engineering carryovers

These are robustness cleanups, not a new redesign.

- [x] **Forced `--motif` plan/materialization consistency.**  
  The forced motif is now passed into `plan_campaign(force_motif=...)`, which pins every slot's motif and computes the planned skeleton hashes from that motif, the resolved sources and the exact recipe materialization samples. A direct out-of-band `materialize(force_motif=...)` call replaces the slot's planned structure instead of carrying a claim about another tree, and every proposal records `planned_motif_id` / `planned_grammar_hash` / `planned_structure_matched`, with `motif_fallback` set on any disagreement.  
  **Regression:** `test_forced_motif_plan_hashes_describe_the_emitted_structure` (1- and 2-source motifs), `test_out_of_band_forced_motif_does_not_reuse_the_planned_structure`.

- [x] **Archive refresh failure visibility.**  
  `refresh_archive()` now returns `False` only for a genuinely absent archive (`no such table`), and raises `generation_policy.ArchiveRefreshError` for real schema/locking/corruption failures while persisting an `archive_refresh_failed` diagnostic to `meta` and the event log. `plan_campaign` propagates it (with an explicit `refresh_archive_state=False` opt-out) and `Plan.archive_refresh` records whether derived state was actually rebuilt.  
  **Regression:** `test_injected_archive_rebuild_failure_cannot_produce_a_normal_plan`, `test_missing_derived_archive_is_not_reported_as_a_stale_one`.

- [x] **P16 complete** — 488 offline tests green; documented in `generator_v3.md`.

---

# P17 — Promotion gate status

- [x] P0–P15 implementation complete.
- [x] Equal-budget replay machinery executed.
- [x] Small live V3 campaigns executed.
- [ ] Promote V3 to default.

Observed 2026-09-28 live evidence:

```text
simulations  138
IS_PASS        0
pass rate      0%
historical    ~23%
```

Treat this as a **quality/search-distribution failure until disproven**, not as a reason to add more grammar complexity.

Primary success metric from this point:

```text
IS_PASS per 100 BRAIN simulations
→ CORR_PASS per 100
→ robustness-adjusted quality
→ survivor diversity
```

---

# P18 — Failure attribution before redesign

**Question:** where does the 0/138 collapse come from?

Do not tune policy weights until this stage can localize the failure.

Implemented in `scripts/quality_diagnostics.py` (`--out quality_diagnostics.json`, git-ignored artifact), covered by `tests/test_quality_diagnostics.py`.

## P18.1 Outcome cube

- [x] Reusable diagnostic over the permanent trial ledger with:
  - generation mode
  - motif
  - source dataset/category
  - semantic role signature
  - grammar/semantic skeleton
  - recipe dimensions
  - mutation operation
  - parent lineage quality
  - Sharpe / Fitness / turnover
  - BRAIN failure reason
- [x] Reports attempts, simulations, IS pass rate, median/quantiles of Sharpe/Fitness/turnover and failure-reason shares.
- [x] Wilson confidence intervals plus explicit `low_confidence` / `thin` minimum-sample flags; a bucket below the minimum sample is excluded from ranking rather than allowed to look extreme.

## P18.2 Matched V2/V3 comparison

- [x] Matched cohorts by scope/universe/delay, source dataset, expression shape and recipe cell, compared only on strata present in **both** versions.
- [x] Separates:
  1. field/source selection gap (`scope → scope+source`)
  2. expression/motif gap (`+source → +shape`)
  3. recipe/settings gap (`+shape → +recipe`)
  4. search-policy residual (once source, shape and recipe are held fixed)
- [x] Compare live V3 failures against historical successful and failed V2 candidates.

Legacy V2 rows have no stored source profile, so the profile is derived from the expression; without that every V2 row reads as dataset `unknown` and the source level is empty by construction.

## P18.3 Failure taxonomy

- [x] Quantifies how much of the gap is explained by low Sharpe, low Fitness, turnover, correlation, invalid/unsupported expressions, weak source families and specific motif/recipe cells. One failed simulation is attributed to exactly one region; an unknown check name is reported as `other_check` rather than guessed.
- [x] Machine-readable artifact `quality_diagnostics.json` (git-ignored; no expression, alpha id or canonical key ever enters it — enforced by `test_artifact_carries_no_expression_and_no_alpha_id`).

**Exit gate: met.** Findings on the 2026-09-29 ledger (point-in-time clock enforced, `leakage_check` green):

```text
catalog-generator-v2   123 sims   9 pass   7.3%   ci95 [3.9%, 13.3%]
catalog-generator-v3   204 sims   0 pass   0.0%   ci95 [0.0%,  1.8%]
```

- The collapse is **`LOW_SHARPE` (146 of 204 V3 simulations)**, not turnover, concentration or correlation. Median Sharpe is 0.00 and p90 is 0.80, so V3 is not bottlenecked by recipe hygiene — it reaches regions with no economic signal at all.
- **Sources are not the cause.** The matched `field_or_source_gap` is `0.0`: once V2 rows get a derived source profile, both versions search comparable datasets.
- **Expression shape is the first material separation.** 153 of 204 V3 simulations have the shape `group_rank(<single blob with two sources>)` with 0 passes, while every well-sampled multi-component `add(...)` composite passes. `add|21src` (13 sims) is the best cell and `group_rank|2src` (153 sims) is the worst.
- **The largest single term is the residual search-policy gap (-0.077)**: within a comparable source/shape/recipe region V3 still picks the worse cells. The other terms are `expression_or_motif_gap` -0.051, `recipe_or_settings_gap` +0.055 and `field_or_source_gap` 0.0.
- Single-source wrappers (`|1src`) never passed once in 382 simulations across all generator versions.

Conclusion: this is a **search-distribution failure**, and it is localised. The fix is P19–P22 — warm-start around proven structures and condition allocation on quality evidence — not more grammar.

## P18.4 Point-in-time safety

- [x] Every row is filtered by its settlement clock, the report clock is explicit (`as_of`), and `leakage_check` re-verifies afterwards that no later outcome entered the report.


---

# P19 — Recover a known-good control surface

**Question:** can V3 reproduce/search near historically successful regions before asking it to discover new ones?

This is the most important control experiment.

## P19.1 Historical seed bank

- [ ] Build a point-in-time-safe seed bank from historical candidates that had reached IS/CORR gates **before** the target campaign clock.
- [ ] Store only evidence available at that time.
- [ ] Tag each seed with motif, source profile, grammar/semantic skeleton, settings and outcome stage.

## P19.2 V2 reconstruction control

- [ ] Make V3 generate controlled candidates structurally near known V2 successes:
  - same source + nearby recipe
  - same motif + new compatible source
  - same semantic skeleton + field substitution
  - one-edit mutation
- [ ] Run equal-budget:
  - original/near-original V2 controls
  - warm-started V3
  - ordinary V3
- [ ] Determine whether V3's weak live output is caused primarily by its search distribution rather than current BRAIN conditions.

## P19.3 Distance ladder

- [ ] Define mutation/search distance bands from a proven seed:
  - D0 exact/control
  - D1 parameter-only
  - D2 one structural edit
  - D3 source transfer
  - D4 semantic/motif change
- [ ] Measure pass probability versus distance.

**Research:** warm-started GP and AutoAlpha both motivate focusing search around promising regions rather than restarting from nearly uniform novelty.

- Weizhe Ren, Yichen Qin, Yang Li — *Alpha Mining and Enhancing via Warm Start Genetic Programming for Quantitative Investment*, arXiv:2412.00896.
- Tianping Zhang, Yuanqi Li, Yifei Jin, Jian Li — *AutoAlpha*, arXiv:2002.08245.

**Exit gate:** demonstrate a non-trivial V3-controlled region whose live pass rate is materially above the current 0% and whose behavior is reproducible.

---

# P20 — Quality-conditioned quality-diversity

**Question:** how do we keep diversity without spending most capacity on diverse junk?

Current novelty/search signals should become **local competition inside meaningful niches**.

## P20.1 Redefine archive objective

- [ ] Keep niche dimensions interpretable and stable.
- [ ] Within each niche, rank candidates by point-in-time quality evidence instead of sparsity alone.
- [ ] Retain one or a few elites per niche based on:
  - IS/CORR stage reached
  - Sharpe/Fitness
  - turnover acceptability
  - robustness/stability evidence
- [ ] Keep exploration reserve for empty/under-tested niches.

## P20.2 Quality-conditioned novelty

Replace the mental model:

```text
novelty → simulate
```

with:

```text
quality prior × novelty × uncertainty / cost
```

- [ ] Novelty must not compensate for strongly negative quality evidence.
- [ ] Quality must not collapse the search into one family/skeleton.
- [ ] Use bounded terms and explicit floors/caps.
- [ ] Keep exact deduplication and point-in-time safety unchanged.

## P20.3 Local-competition experiment

Run equal-budget arms:

```text
novelty-only V3
archive-quality only
quality-conditioned QD
V2 control
```

Measure IS_PASS/simulation first, then survivor diversity.

**Research:**

- Mouret & Clune — *Illuminating search spaces by mapping elites*, arXiv:1504.04909.
- Pugh, Soros & Stanley — *Quality Diversity: A New Frontier for Evolutionary Computation*, DOI:10.3389/frobt.2016.00040.
- AutoAlpha's PCA-QD is a domain-specific comparison point.

**Exit gate:** improve live/replay efficiency without reducing effective survivor diversity to a trivial single niche.

---

# P21 — Learn conditional motif/source/recipe quality

**Question:** which hypotheses work **where**, rather than which motif works globally?

A global motif success count is too coarse.

## P21.1 Conditional statistics

- [ ] Add point-in-time outcome statistics over a bounded hierarchy such as:

```text
motif
motif × source category
motif × dataset
motif × semantic-role signature
motif × recipe bucket
mutation operation × parent-quality bucket
```

- [ ] Use hierarchical backoff when samples are sparse:
  `specific → category → motif → global prior`.
- [ ] Require minimum evidence before a narrow bucket can dominate allocation.

## P21.2 Posterior quality prior

- [ ] Estimate `P(IS_PASS | context)` or an equivalent bounded quality score.
- [ ] Keep exploration via Thompson/UCB-style uncertainty rather than zeroing weak buckets forever.
- [ ] Distinguish:
  - attempts
  - simulations
  - IS passes
  - CORR passes
  - skipped duplicates
- [ ] Do not treat skipped/non-simulated proposals as negative performance outcomes.

## P21.3 Recipe learning

- [ ] Measure whether lookback, normalization, decay, neutralization, sign and winsorization effects are motif/source dependent.
- [ ] Stop sampling obviously poor recipe regions uniformly once evidence is strong.
- [ ] Retain an exploration floor to detect regime change.

**Exit gate:** conditional allocation must beat global motif allocation in point-in-time replay and then in a small matched live campaign.

---

# P22 — Warm-started exploit and structure-preserving mutation

**Question:** can exploitation produce useful novelty by changing one justified component at a time?

## P22.1 Mutation ladder

Prefer controlled mutations around proven seeds:

1. [ ] recipe/parameter adjustment
2. [ ] normalization/group adjustment
3. [ ] compatible field substitution
4. [ ] same-motif dataset transfer
5. [ ] add/remove one component
6. [ ] motif change
7. [ ] crossover only after evidence supports it

- [ ] Track pass rate by mutation distance and operation.
- [ ] Preserve the economic core of a parent when the operation is intended as exploitation.
- [ ] Penalize operations whose live children repeatedly destroy parent quality.

## P22.2 Parent quality

- [ ] Parent selection should include point-in-time outcome quality, not only archive diversity.
- [ ] Distinguish exploration parents from exploitation parents.
- [ ] Test whether parents closer to IS/CORR success produce better children.

## P22.3 Crossover budget

- [ ] Keep crossover share low until it demonstrates positive marginal value.
- [ ] Compare crossover children against one-edit mutations from the same parent pool.
- [ ] If crossover remains weak, allow adaptive allocation to shrink it close to the exploration floor.

**Research:** warm-start GP and AutoAlpha directly motivate promising-region initialization and controlled evolution.

**Exit gate:** exploit/mutation must show a measurable pass-rate gradient with parent quality and/or mutation distance.

---

# P23 — Optimize marginal contribution, not isolated alpha quality

**Question:** does a candidate improve the **set** of surviving alphas?

A high-quality isolated factor can still be redundant.

## P23.1 Collection-aware evidence

For candidates with sufficient local evidence, estimate:

- [ ] marginal IC / predictive contribution versus the current survivor set
- [ ] correlation to existing survivors
- [ ] incremental combination-model value where feasible
- [ ] incremental coverage of source/semantic niches

Do not use future outcomes relative to the campaign clock.

## P23.2 Two-stage objective

Keep simulation search and final portfolio selection distinct:

```text
stage 1: probability of surviving BRAIN gates
stage 2: marginal contribution among survivors
```

- [ ] Do not sacrifice basic pass probability for portfolio novelty too early.
- [ ] Once candidates are credible, reward low redundancy and incremental set value.

## P23.3 Dynamic exploit prior

- [ ] Test whether exploit should prefer motifs/sources that add value to the current alpha collection rather than merely repeating individually strong structures.

**Research:**

- Shuo Yu et al. — *Generating Synergistic Formulaic Alpha Collections via Reinforcement Learning*, KDD 2023, DOI:10.1145/3580305.3599831.
- Hao Shi et al. — *AlphaForge*, AAAI 2025, DOI:10.1609/aaai.v39i12.33365.

**Exit gate:** collection-aware ranking must improve downstream correlation/diversification metrics without lowering IS_PASS/simulation materially.

---

# P24 — Cheap surrogate and staged pre-simulation screening

**Question:** can local evidence rank obviously weak candidates before expensive BRAIN simulation?

Start advisory; do not hard-reject from an unvalidated model.

## P24.1 Feature set

Build features only from information available pre-simulation:

- [ ] grammar/semantic hashes and complexity
- [ ] motif/source/category/role signature
- [ ] recipe/settings
- [ ] archive occupancy and parent quality
- [ ] mutation distance / operation
- [ ] historical conditional statistics
- [ ] inexpensive local signal statistics when available and point-in-time safe

## P24.2 Targets

Train/calibrate separate targets where possible:

- [ ] probability of LOW_SHARPE
- [ ] probability of LOW_FITNESS
- [ ] turnover risk
- [ ] IS_PASS probability
- [ ] expected robustness/stability

## P24.3 Validation

- [ ] strict time split / rolling validation
- [ ] calibration curves
- [ ] precision/recall in the top-ranked simulation bucket
- [ ] compare against simple priors before using complex models
- [ ] measure **passes captured per fixed simulation budget**

Initially use the surrogate to **order/downweight**, not hard reject.

**Research:**

- Ding et al. — *AlphaEval: A Comprehensive and Efficient Evaluation Framework for Formula Alpha Mining*, KDD 2026 / arXiv:2508.13174.

Use its multi-dimensional evaluation idea as a comparison point; reproduce only dimensions supported by this repo's data.

**Exit gate:** a point-in-time surrogate must improve pass capture under a fixed replay/live budget and remain calibrated out of sample.

---

# P25 — Controlled ablation and promotion

Do not judge the new roadmap from one mixed campaign.

## P25.1 Equal-budget arms

At minimum compare:

```text
A  V2 default
B  current V3
C  V3 exploit-only / warm-start
D  V3 quality-conditioned QD
E  V3 conditional motif/source allocation
F  V3 + surrogate ordering
G  full quality-aware V3
```

Where budget allows, separately test mutation and crossover contribution.

## P25.2 Required metrics

Primary:

- [ ] IS_PASS / simulation
- [ ] CORR_PASS / simulation
- [ ] simulations per IS_PASS
- [ ] simulations per CORR_PASS

Secondary:

- [ ] median/upper-tail Sharpe and Fitness
- [ ] turnover-failure rate
- [ ] effective grammar/semantic/family diversity **among survivors**
- [ ] pairwise survivor correlation
- [ ] robustness/stability metrics
- [ ] mode/motif/source concentration

## P25.3 Promotion gate

Promote V3 only after multiple independent matched-budget live campaigns show:

- [ ] materially better simulation efficiency than the current V3
- [ ] competitive performance versus V2
- [ ] no material collapse in survivor diversity
- [ ] acceptable correlation and robustness
- [ ] no point-in-time leakage
- [ ] reproducibility from stored campaign configuration

A single strong campaign is evidence, not promotion.

---

# Deferred research branch — only after P18–P24 evidence

Do **not** jump here to avoid fixing the basic search distribution.

Potential follow-ups:

- [ ] **Tree/MCTS search:** compare against *Navigating the Alpha Jungle: An LLM-Powered MCTS Framework for Formulaic Alpha Factor Mining* (AAAI 2026, DOI:10.1609/aaai.v40i2.37069).
- [ ] **Learned alpha-search policy:** compare concepts from *AlphaEvolve* (SIGMOD 2021, DOI:10.1145/3448016.3457324).
- [ ] Learned embeddings for semantic niches.
- [ ] Regime-conditioned policy allocation.
- [ ] Dynamic factor combination after a credible survivor pool exists.

These become worthwhile only if P18–P24 show that the local quality model and search objective are calibrated.

---

# Recommended order

```text
P16  small correctness/observability cleanup
P18  explain the 0/138 result
P19  recover a known-good control surface
P20  quality-conditioned QD
P21  conditional motif/source/recipe evidence
P22  warm-start exploit + controlled mutation
P23  collection-aware contribution
P24  surrogate ordering
P25  ablation + promotion
```

P23 and P24 may proceed in parallel after P20/P21 produce stable evidence tables.

---

# Research reading queue

Read with a concrete implementation question; record the useful mechanism and the assumptions that do **not** transfer to WorldQuant BRAIN.

1. [ ] **AutoAlpha** — hierarchical promising-region search, PCA-QD, warm start. arXiv:2002.08245.
2. [ ] **Warm Start Genetic Programming** — promising-region initialization and structural constraints. arXiv:2412.00896.
3. [ ] **MAP-Elites** — quality within behavior niches. arXiv:1504.04909.
4. [ ] **Quality Diversity: A New Frontier** — local competition and QD failure modes. DOI:10.3389/frobt.2016.00040.
5. [ ] **Generating Synergistic Formulaic Alpha Collections via RL** — optimize incremental collection value. DOI:10.1145/3580305.3599831.
6. [ ] **AlphaForge** — generation + dynamic combination and diversity. DOI:10.1609/aaai.v39i12.33365.
7. [ ] **AlphaEval** — multi-dimensional cheap evaluation beyond backtest/IC alone. arXiv:2508.13174.
8. [ ] **AlphaEvolve / Alpha Jungle MCTS** — later comparison for learned/hierarchical search, not immediate prerequisites.

For each paper add a short note to the relevant P-section:

```text
mechanism worth testing:
assumption that differs from BRAIN:
minimal experiment:
metric that would falsify the idea:
```

---

# Final rule

> **The generator is no longer rewarded for being different. It is rewarded for discovering distinct hypotheses in regions where point-in-time evidence says useful alphas are plausible.**
