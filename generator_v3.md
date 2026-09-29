# Generator V3 — Architecture, Evidence, and Next Phase

**Repository:** `Uwater1/wq-alpha-research`  
**Authoritative roadmap:** `generator_v3_todo.md`

## Current status

Generator V3's engineering core is complete and auditable. The offline suite is green, point-in-time replay exists, and the known P0–P15 correctness gaps are closed.

The remaining problem is **research quality, not generator plumbing**:

- [x] P0–P15 implementation complete.
- [x] Equal-budget replay infrastructure complete.
- [x] Small live V3 campaigns executed.
- [ ] V3 promotion **not justified**.
- [x] V2 remains the default.

Observed live evidence on 2026-09-28:

```text
V3 live simulations: 138
IS passes:           0
live IS pass rate:   0%
historical baseline: ~23%
```

Treat the 0% result as the entry point for the next phase: determine why V3 searches structurally diverse but economically weak regions, then make the search distribution quality-aware without collapsing diversity.

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
- [x] Diversity reporting and point-in-time policy replay.
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

## Small engineering carryovers

These do not reopen P0–P15, but should be cleaned up early in the quality phase.

- [ ] **Forced `--motif` planning consistency.** A forced motif is currently applied at materialization after a normal plan is built, so the plan's motif/hash metadata can describe a different structure. Either plan the forced motif from the start or explicitly clear/replace planned structure metadata.
- [ ] **Archive refresh failure visibility.** `refresh_archive()` currently swallows rebuild exceptions and lets planning continue. Persist/report refresh failure or fail fast for real DB/schema errors so stale derived state is never silent.

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
