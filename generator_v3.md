# Generator V3 — Architecture, Evidence, and Next Phase

**Repository:** `Uwater1/wq-alpha-research`  
**Authoritative roadmap:** `generator_v3_todo.md`

## Current status

Generator V3's P0–P15 engineering core is implemented and historically CI-covered. **Selection-policy** replay (`policy_replay.py`) is event-clock-safe, but historical **candidate-generation** replay remains incomplete: current archive/coverage/statistics cannot be used with a past prior. Explicit `generate(as_of=...)` calls now fail closed pending [#12](https://github.com/Uwater1/wq-alpha-research/issues/12). Verify CI against the new head; do not infer status from older green runs.

The remaining problem is **research quality, not generator plumbing**:

- [x] P0–P15 implementation complete.
- [x] Point-in-time selection replay infrastructure complete (distinct from generator counterfactual replay).
- [x] Small live V3 campaigns executed.
- [x] **V3 promoted to default** (`python -m wq generate`; V2 is `--legacy-v2`).
- [x] V2 remains reachable as the explicit control arm. `--legacy-v2 --dry-plan` must not queue candidates; `--seed-as-of` is rejected outside warm-start.

**Descriptive historical ledger only**, not a matched prospective experiment (`scripts/promotion_gate.py`):

```text
catalog-generator-v2    123 sims    9 IS pass    7.3%   ci95 [3.9%, 13.3%]
catalog-generator-v3    540 sims   96 IS pass   17.8%   ci95 [14.8%, 21.2%]
pooled observed ratio   2.43x (not causal or matched)
```

The former "V2 effective grammar 1.0" was an artifact of null V3-only skeleton-hash columns. The corrected gate reconstructs hashes for legacy records; the true ledger-wide V2/V3 diversity comparison needs a rerun on the private database.

The original 0/138 result was a search-distribution failure, and it was localized to two fixes rather than more grammar: the recipe grid could not express the truncation `0.08` the platform was actually paying for (P21.3), and a child's edit budget was capped below the size of the proven parents it was editing, so every derivation of them silently degraded to fresh exploration (P22.2). The final gate still reports `promoted: false`: matched prospective arms, historical generator causality, correlation/robustness and configuration replay remain unverified. V3 is an **operational/provisional** default, not a fully validated winner. Existing historical IS-pass evidence is useful for prioritizing live tests, not for claiming downstream submission value.

## Implemented core — P0–P15

The completed implementation is intentionally compressed here; regressions remain in the test suite and git history.

- [x] Typed grammar, compatibility validation, deterministic recipes, motif registry.
- [x] Exact / parameter / grammar / semantic identities and source profiles.
- [x] Explore / exploit / mutate / crossover / mixed planning.
- [x] Structural mutation vocabulary with honest planned-vs-realized operation accounting.
- [x] Two-parent crossover with real metadata-aware structural distance.
- [x] Archive-informed family/parent allocation, atomic rebuild, automatic refresh.
- [x] Novelty screen across structure/source/archive dimensions and both crossover parents.
- [x] Adaptive motif/mutation allocation with corrected source→target and skip statistics.
- [x] Full queued/skipped lineage and permanent trial provenance.
- [x] V2/V3 versioning and V2-safe default CLI behavior.
- [x] Diversity reporting and point-in-time **selection** policy replay.
- [x] Reproducible CI regression suite.

Core dataflow:

```text
campaign budget
    ↓
quality-aware generation policy
    ├── explore
    ├── exploit
    ├── mutate
    └── crossover
    ↓
typed grammar + motifs + deterministic recipe
    ↓
novelty / quality / compatibility screening
    ↓
ResearchDB.queue_candidate()
    ↓
simulation / checks
    ↓
trial ledger + archive + empirical statistics
    ↺
```

## 2026-10-09 follow-up: realized vs planned lineage edits (P22.4)

The parent-relative budget fix (P22.2) made structural edits possible on proven parents; a
fixed-seed probe of a `--strategy mutate` plan showed they were still not reliably *spent*. Two
leaks, measured with no simulation spent:

- a `SUBMISSION_READY` parent stores a **submission-gate** `failure_reason`, and `_mutate_child`
  treated any diagnosed mode as a repair case — including `SELF_CORRELATION`, which a
  generation-time repair cannot address — so the allocated edit was replaced by the legacy
  `field_swap` (1 pass in 24 simulations);
- a pinned edit that failed on a particular parent fell straight through to that same legacy
  edit instead of retrying the campaign's other budgeted edits.

Realized operation mix went from `18/30 planned + 12 field_swap` to **`30/30` planned,
`0 field_swap`**. `REPAIRABLE_FAILURE_MODES` now separates IS metrics from portfolio gates, and
`materialize` passes the allocated operations to `_mutate_child` as ordered alternatives.

**Still open (diagnosed, not shipped):** the default `mixed` plan's `exploit` slot materializes as
fresh `motif_generation` (0/96 settled simulations) while the `exploit` **mode** statistic is
inflated by the separate warm-started arm (`recipe_perturbation`, 29/34). Routing the default
exploit slot to the warm-start ladder is a plan-contract change (the planner must select
`(seed, rung)` so planned skeleton hashes still describe the emitted tree) and is the next work
item rather than a half-planned patch.

## Small engineering carryovers

These did not reopen P0–P15 and are now closed (P16).

- [x] **Forced `--motif` planning consistency.** The pin is planned, not patched: `plan_campaign(force_motif=...)` pins the slot motif and computes planned skeleton hashes from the pinned motif, its resolved sources and the exact recipe materialization samples. Out-of-band `materialize(force_motif=...)` replaces the planned structure instead of mislabelling it; proposals carry `planned_motif_id` / `planned_grammar_hash` / `planned_structure_matched`, dry plans report `structure_mismatch`.
- [x] **Archive refresh failure visibility.** `refresh_archive()` returns `False` only for an absent archive and raises `ArchiveRefreshError` (persisting an `archive_refresh_failed` diagnostic to `meta` and the event log) for real schema/locking failures. `Plan.archive_refresh` reports whether derived state actually rebuilt, and `refresh_archive_state=False` is the explicit opt-out.

## 2026-10-07 audit: corrected code versus open empirical gates

- **Fixed:** source-family-specific motif allocation rather than best-dataset/global consumption; per-campaign quality-preference accounting; explicit error on unsafe historical generation plans.
- **Privacy mitigation:** tracked operational alpha-ID exports removed and ignored. Public history may still expose them; owner-approved remediation and CI scanning remain open in [#14](https://github.com/Uwater1/wq-alpha-research/issues/14).
- **Still open:** replayable point-in-time generator snapshots ([#12](https://github.com/Uwater1/wq-alpha-research/issues/12)), joint structure×recipe quality and measured operator/crossover value ([#13](https://github.com/Uwater1/wq-alpha-research/issues/13)), replicated matched live V2/V3 campaigns and BRAIN correlation outcomes ([#11](https://github.com/Uwater1/wq-alpha-research/issues/11)).
- **Status correction:** P20 and P21 are *mechanism-complete but not empirically closed*; P22 has unchecked live mutation/crossover tests. Do not mark P17 promotion complete.

Recommended sequencing: finish the bounded correctness/CI fixes → **P25** matched live ablation → P24 advisory calibration → P23 collection-aware work when enough genuine survivors exist. V2 stays the default.

## New objective

The old V3 question was:

> Can we search a broad, diverse symbolic hypothesis space correctly?

That is now largely answered.

The next question is:

> Can we spend scarce BRAIN simulations on diverse hypotheses with a materially higher probability of surviving IS and correlation gates?

Primary optimization metric:

```text
IS_PASS per 100 simulations
    ↓
CORR_PASS per 100 simulations
    ↓
robustness-adjusted quality
    ↓
diversity among survivors
```

Diversity remains necessary, but it is no longer the top-level objective.

## Research direction

The next roadmap uses literature as design input, not as implementation authority.

### Warm-start and promising-region search

**AutoAlpha** proposes hierarchical search, quality-diversity search, warm starts, and replacement to focus alpha mining on promising regions rather than pure random exploration.

- Tianping Zhang, Yuanqi Li, Yifei Jin, Jian Li. *AutoAlpha: an Efficient Hierarchical Evolutionary Algorithm for Mining Alpha Factors in Quantitative Investment*. arXiv:2002.08245.
- Weizhe Ren, Yichen Qin, Yang Li. *Alpha Mining and Enhancing via Warm Start Genetic Programming for Quantitative Investment*. arXiv:2412.00896.

Use these to motivate **quality-conditioned exploration and warm-started mutation**, not to copy their search spaces directly.

### Quality-diversity rather than novelty alone

MAP-Elites / quality-diversity work optimizes quality inside niches rather than rewarding diversity without local competition.

- Jean-Baptiste Mouret, Jeff Clune. *Illuminating search spaces by mapping elites*. arXiv:1504.04909.
- Justin K. Pugh, Lisa B. Soros, Kenneth O. Stanley. *Quality Diversity: A New Frontier for Evolutionary Computation*. DOI: 10.3389/frobt.2016.00040.

Use this to redesign the archive from “sparse/novel is good” toward “retain the best evidence-backed candidate in each economically meaningful niche”.

### Collection-aware alpha objectives

Mining isolated alphas can reward factors that look novel individually but add little to a portfolio.

- Shuo Yu et al. *Generating Synergistic Formulaic Alpha Collections via Reinforcement Learning*. KDD 2023, DOI: 10.1145/3580305.3599831.
- Hao Shi et al. *AlphaForge: A Framework to Mine and Dynamically Combine Formulaic Alpha Factors*. AAAI 2025, DOI: 10.1609/aaai.v39i12.33365.

Use these to investigate **marginal contribution to an existing alpha set**, dynamic quality priors, and portfolio-aware exploitation.

### Evaluation and surrogate screening

Backtesting every candidate is expensive and a single metric misses stability/robustness.

- Hongjun Ding et al. *AlphaEval: A Comprehensive and Efficient Evaluation Framework for Formula Alpha Mining*. KDD 2026 / arXiv:2508.13174.

Use this as a research prompt for a cheap local pre-simulation score spanning predictive power, stability, robustness, financial logic, and diversity. Do not replace BRAIN gates until local evidence validates the surrogate.

### Search-policy alternatives

- Yu Shi, Yitong Duan, Jian Li. *Navigating the Alpha Jungle: An LLM-Powered MCTS Framework for Formulaic Alpha Factor Mining*. AAAI 2026, DOI: 10.1609/aaai.v40i2.37069.
- Can Cui et al. *AlphaEvolve: A Learning Framework to Discover Novel Alphas in Quantitative Investment*. SIGMOD 2021, DOI: 10.1145/3448016.3457324.

These are useful comparison points for hierarchical / tree-guided search once the basic evidence model is working. They are **not** P18 prerequisites.

## Promotion rule

Do not promote V3 because the architecture is cleaner or the archive is more diverse. Promote only after repeated matched-budget live campaigns show materially better simulation efficiency without collapsing downstream diversity or robustness.

> **Core rule for the next phase: quality-condition diversity; do not replace diversity with quality or quality with novelty.**
