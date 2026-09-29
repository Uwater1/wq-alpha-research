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

Implemented in `scripts/seed_bank.py` (bank, ladder, recipe prior) and `generator.warm_start` / `generator.warm_start_campaign` (the warm-started arm), covered by `tests/test_seed_bank.py` and `tests/test_warm_start.py`.

## P19.1 Historical seed bank

- [x] Point-in-time-safe seed bank from candidates that had reached an IS/CORR gate **before** the target campaign clock (`build_seed_bank(..., as_of=...)`, settlement timestamp is the simulation's `completed_at`).
- [x] Only evidence available at that time is stored, and `seed.public()` exposes a sanitized view that never carries the private expression.
- [x] Each seed is tagged with motif, source profile, grammar/semantic skeleton, recipe (reconstructed from settings + first window for legacy V2 rows), settings and outcome stage.

## P19.2 V2 reconstruction control

- [x] V3 generates controlled candidates structurally near proven successes through the warm-start ladder: D1 = same source + nearby recipe, D3 = same motif/topology + new compatible source, D2 = one-edit mutation (field-preserving operations only), D4 = semantic/motif change over the same sources.
- [x] Equal-budget arms are runnable: `--warm-start` (warm-started V3), `--strategy mixed` (ordinary V3) and the V2 template path are all one command; the arm harness and promotion gate are P25.
- [x] Verdict on the 0/138 collapse: **search distribution, not BRAIN conditions.** The proven bank contains 139 gate-reaching candidates, and 120 of 139 (86%) used truncation `0.08` — a value V3's recipe grid could not even express (`{0.05, 0.1, 0.15}`). 109 of 139 prove an `add(...)` multi-component shape, while 153 of V3's 204 simulations were the single-blob `group_rank` shape with zero passes.

## P19.3 Distance ladder

- [x] Bands are defined and measurable from a proven seed: D0 exact/control, D1 parameter-only (identical ordered operator sequence, same sources), D2 one structural edit (`operator_edit_distance <= 2`, same sources), D3 source transfer (same topology, sources substituted), D4 semantic/motif change.
- [x] `distance_outcomes()` measures pass probability per rung over the live ledger, optionally restricted to one generator's children (`child_versions`) so the rung rates describe the arm under test rather than every campaign that ever mutated a proven alpha.
- [x] `perturb_recipe()` moves exactly one recipe dimension, and `substitute_field_choice()` returns the replaced field as well as a type- and role-compatible replacement, so each rung is a real one-thing-changed edit.
- [x] Recipe prior: `proven_recipe_prior()` reports which recipe cells the platform actually accepted (truncation 0.08 in 120/139 seeds, SUBINDUSTRY in 93/139, lookback 22 in 68/139) — the exploitation prior for P21.3/P22.1.

**D0 is defined but not budgeted by default**: a byte-identical child is an exact duplicate, so the canonical cache refuses it and it can never consume a BRAIN slot; spending campaign budget on it would silently shrink the arm. It is opt-in through explicit band weights.

**Research:** warm-started GP and AutoAlpha both motivate focusing search around promising regions rather than restarting from nearly uniform novelty.

- Weizhe Ren, Yichen Qin, Yang Li — *Alpha Mining and Enhancing via Warm Start Genetic Programming for Quantitative Investment*, arXiv:2412.00896.
- Tianping Zhang, Yuanqi Li, Yifei Jin, Jian Li — *AutoAlpha*, arXiv:2002.08245.

**Exit gate:** demonstrate a non-trivial V3-controlled region whose live pass rate is materially above the current 0% and whose behavior is reproducible.

---

# P20 — Quality-conditioned quality-diversity

**Question:** how do we keep diversity without spending most capacity on diverse junk?

Current novelty/search signals should become **local competition inside meaningful niches**.

## P20.1 Redefine archive objective

- [x] Keep niche dimensions interpretable and stable.
- [x] Within each niche, rank candidates by point-in-time quality evidence instead of sparsity alone.
- [x] Retain one or a few elites per niche based on:
  - IS/CORR stage reached
  - Sharpe/Fitness
  - turnover acceptability
  - robustness/stability evidence
- [x] Keep exploration reserve for empty/under-tested niches.

**Implemented:** `archive.quality_elite_score` makes *stage reached* the primary term (an
IS-gate pass is worth more than the whole metric term can add, so a gate pass can never be
outranked by a large number from a candidate that never cleared one) and metrics a bounded
secondary one (Sharpe/Fitness clamped to `[-1, 3]` each, turnover acceptability as a bounded
bonus normalised by the wider admissible side so the penalty is strictly decreasing on both
sides instead of saturating, and self-correlation as a redundancy cost). `archive.rebuild` now
selects the niche elite with it, `archive.quality_elites(db, per_niche=)` re-derives the top few
members of each niche for exploitation, and `archive.under_tested_niches(db, max_members=)`
reports the least-occupied niches as the exploration reserve — reported, never invented.

## P20.2 Quality-conditioned novelty

Replace the mental model:

```text
novelty → simulate
```

with:

```text
quality prior × novelty × uncertainty / cost
```

- [x] Novelty must not compensate for strongly negative quality evidence.
- [x] Quality must not collapse the search into one family/skeleton.
- [x] Use bounded terms and explicit floors/caps.
- [x] Keep exact deduplication and point-in-time safety unchanged.

**Implemented:** `quality_prior.quality_conditioned_score` is `bounded quality x novelty x
uncertainty / cost` with every term bounded — the quality term uses the Beta posterior's *upper*
bound so genuine uncertainty can pay, clamped to `[0.01, 1.0]` so strongly negative evidence
downweights a region without ever deleting it; novelty is floored at `0.25` so it can neither
compensate for absent quality nor be switched off; uncertainty is a UCB bonus
`1 + 0.6*sqrt(upper-lower)` so a two-sample bucket is never as authoritative as a fifty-sample
one; cost divides the whole thing with a floor.
Mode weights are conditioned by `generation_policy.quality_conditioned_weights`, which sees only
modes with `>= 5` simulated runs, clamps each evidenced mode to `1/3 .. 3x` of its base share,
and then **projects** the result onto `floor`/`max_share` **and** that ratio band with a
water-filling projection (`_project_onto_bounds`) — clamping before renormalizing does not bound
the final weight, because renormalization pushes the clamped mode straight back out of band.
**Bug found and fixed while implementing this:** `refresh_generation_stats` read `simulated`
from the candidate status alone, and a candidate refused by the IS gate is moved to `REJECTED`
(never `SIMULATED`). Every refused simulation therefore dropped out of the denominator and each
learned rate (`is_pass / simulated`) inflated toward 100%. It now counts a finished simulation
row as spent capacity, so the live ledger reads `explore 84/0`, `exploit 46/0`, `mutate 13/0`,
`crossover 6/0` for V3 instead of four cells of `0/0` — with all four modes unproven, the bounded
prior correctly declines to move any weight.
Deduplication (`canonical_key`) and point-in-time filtering are untouched; `prior=None` /
`quality_conditioned=False` reproduce the previous unconditioned distribution as the control arm.

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

- [x] Add point-in-time outcome statistics over a bounded hierarchy such as:

```text
motif × dataset × recipe bucket
motif × dataset
motif × source category
motif × semantic-role signature
motif × mutation operation
motif
mutation operation × parent-quality bucket
global
```

- [x] Use hierarchical backoff when samples are sparse:
  `specific → category → motif → global prior`.
- [x] Require minimum evidence before a narrow bucket can dominate allocation.

**Implemented:** `scripts/quality_prior.py` builds the whole hierarchy in one pass over the P18
ledger (`QualityPrior.build(db, as_of=...)`, point-in-time safe). `lookup(context)` returns the
Beta posterior of the most specific level with `>= min_evidence` (default 3) simulations and
*always* reports the narrowest cell that has any evidence at all as `specific`/
`specific_level`, plus `backed_off` when nothing met the bar — so a thin bucket is visible even
when the answer came from a coarser level. `generation_policy.allocate_motifs_conditioned`
spends a campaign's motif budget from this prior *per dataset*, with the same guarantees as
`allocate_motifs` (slots sum to exactly the budget, an unobserved motif keeps the exploration
floor, no motif exceeds `max_share`). `Plan.quality_allocation` / `Plan.prior_version` / `Plan.mode_weights`
record the evidence behind every allocation.

**Live evidence (as of the diagnosis clock, `research.db`):** global 753 simulations / 141 IS
passes; `motif+dataset` — `analyst4 80/142 (50.0%)`, `news18 28/56 (38.2%)`, `model16 9/42
(16.1%)`, `fundamental2 5/28 (17.9%)`, `fundamental6 7/53 (13.2%)`, `option8 5/45 (11.1%)`;
`motif+operation` — `add_component 6/6 (100%)`. The prior is therefore *not* uniform over
sources, which is exactly the signal the V3 search was ignoring.

## P21.2 Posterior quality prior

- [x] Estimate `P(IS_PASS | context)` or an equivalent bounded quality score.
- [x] Keep exploration via Thompson/UCB-style uncertainty rather than zeroing weak buckets forever.
- [x] Distinguish:
  - attempts
  - simulations
  - IS passes
  - CORR passes
  - skipped duplicates
- [x] Do not treat skipped/non-simulated proposals as negative performance outcomes.

**Implemented:** `QualityPrior` is a Beta(`1, 19`) posterior per cell — a 5% prior pass rate, the
campaign's own measured baseline — with an approximate credible interval from the posterior
variance. `Evidence` keeps `attempts`, `simulations`, `is_pass`, `corr_pass`, `skipped` and
`sharpe_sum` **separate**; `from_rows` classifies a row as skipped (`SKIP_REDUNDANT` or
`simulated=False`) and gives it zero outcome evidence, so a rediscovery that never spent a slot
can never look like a failure. Uncertainty re-enters only as the UCB bonus in
`quality_conditioned_score`; nothing is ever zeroed out permanently.

## P21.3 Recipe learning

- [x] Measure whether lookback, normalization, decay, neutralization, sign and winsorization effects are motif/source dependent.
- [x] Stop sampling obviously poor recipe regions uniformly once evidence is strong.
- [x] Retain an exploration floor to detect regime change.

**Implemented (this is the single highest-leverage fix in the roadmap):** `generation_policy`
now samples each recipe dimension from a grid *conditioned on the proven recipe counts*
(`seed_bank.proven_recipe_counts(db, as_of=..., scope=...)`, one query over gate-reaching
candidates; the recipe is the stored one or reconstructed from settings plus the first window for
V2-era rows). `recipe_value_weights` adds every value the platform has actually accepted — even
one the local grid could not express — as long as it lies **between the grid's own endpoints**,
gives observed values `1 - floor` of the mass in proportion to how often they were accepted, and
spreads `floor` (0.25) over the never-observed values so a regime change stays detectable.
`Plan.recipe_prior` carries the counts and `generate`/`materialize` sample from them, so the
planned skeleton hashes still describe the emitted tree (P4.2). With no evidence the draw is
`rng.choice` unchanged, value for value and RNG-draw for RNG-draw, so existing outputs do not move.

**Live evidence:** the proven recipe cells are `truncation 0.08 in 120/139`, `decay 8 in 69`,
`SUBINDUSTRY in 93`, `lookback 22 in 68` — and **none** of `truncation 0.08`, `decay 8` or
`lookback 22` was expressible by the V3 grid (`{0.05, 0.1, 0.15}`, `{4, 6, 10, 20}`,
`{20, 60, 126, 252}`). After conditioning, 400 draws put 66% of truncation mass on `0.08`, 39% of
decay mass on `8`, 37% of lookback mass on `22`, 68% of neutralization mass on `SUBINDUSTRY`, and
the out-of-range ledger values (`truncation 0.02/0.03`, `decay 0/2`) are correctly *not* admitted.

**The recipe × shape interaction is the whole story (live, by generator version):**

| arm | simulations | passing cells |
| --- | --- | --- |
| `catalog-generator-v2` | 123 | `add(...)` @ t0.08 **7/8**, `winsorize` @ t0.05 1/24; every other cell 0, including `group_rank` @ t0.1 0/31 |
| `catalog-generator-v3` | 204 | **no passing cell at all** — `divide` 0/35, `subtract` 0/26, `add` 0/24, all at t0.05/t0.1/t0.15 |
| truncation 0.1 across `catalog-generator-v1..v3` | 196 | 0 |
| truncation 0.08 across `agent-hypothesis-v2..v11` | 138 | 121 |

The target cell is therefore **multi-component `add(...)` at truncation 0.08** — the one cell V3
could not express and the one the earlier diagnosis flagged (`add|21src`, 109/139 seeds). V3's
ordinary plan now emits composites (`add`/`divide`/`subtract`) and concentrates 66% of its
truncation mass on `0.08`, so the previously unreachable cell is now *the default*, not an outlier.
This is the mechanism by which V3 is expected to overtake V2; the confirmation is a matched live
campaign (P25.1).

**Exit gate:** conditional allocation must beat global motif allocation in point-in-time replay and then in a small matched live campaign. *(Mechanism complete; the arm comparison is P25.1/P20.3.)*

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
P16  small correctness/observability cleanup   [done]
P18  explain the 0/138 result                   [done]
P19  recover a known-good control surface       [done]
P20  quality-conditioned QD                     [done]
P21  conditional motif/source/recipe evidence   [done]
P22  warm-start exploit + controlled mutation   [in progress]
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
