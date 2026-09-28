# Generator V3 TODO

**Goal:** diversity-first symbolic alpha generation while preserving validation, canonical deduplication, lineage, the permanent trial ledger, staged search, scheduler safety, and point-in-time replay.

**Status after second audit:** most P0–P15 implementation work is complete. The remaining issues are concentrated in P4/P5/P6/P7/P9/P15. Keep those items open until the implementation and regressions below are complete.

**Live default:** V2 remains the default unless V3 is explicitly selected.

---

# P0 — Diversity identities and source metadata

- [x] Child source profile exposes fields, datasets, categories, types, primary family, and cross-dataset flag.
- [x] Single-source and multi-source family derivation are stable.
- [x] Mutation children derive family from the child.
- [x] Crossover children derive family from the child.
- [x] Canonical, parameter-skeleton, grammar-skeleton, and semantic-skeleton identities remain distinct.
- [x] Archive niches use topology-preserving identities.

---

# P1 — Typed expression grammar

- [x] Typed AST.
- [x] Reuse `compatibility.py`.
- [x] Arity/type validation.
- [x] Deterministic FASTEXPR rendering.
- [x] Complexity limits.
- [x] Tolerant parsing for stored legacy expressions.

---

# P2 — Motif registry

- [x] Single-source motifs.
- [x] Two-source motifs.
- [x] Cross-dataset composite.
- [x] Role-aware event/expectation motifs.
- [x] Deterministic role inference + positive/negative eligibility tests.

---

# P3 — Deterministic recipes

- [x] Per-proposal deterministic RNG.
- [x] Independent recipe dimensions.
- [x] Recipe index + full recipe provenance.
- [x] Unrelated proposal insertion does not perturb an existing recipe.

---

# P4 — Generation strategy layer

## P4.1 Modes

- [x] `explore`
- [x] `exploit`
- [x] `mutate`
- [x] `crossover`
- [x] `mixed`
- [x] Realized mode is reported honestly when lineage modes fall back.

## P4.2 Explore / structural novelty

- [x] Under-tested fields/families and unseen motifs influence explore allocation.
- [x] Cross-dataset motifs are reachable in ordinary planning.
- [x] Grammar/semantic history and archive occupancy are consulted before materialization.
- [ ] **Make the planned structure exactly match the materialized structure.**  
  Current planner novelty scoring builds a representative AST with `grammar.Recipe()`; materialization later samples the real deterministic recipe. Topology-changing recipe choices such as rank vs zscore, winsorization, or sign can therefore change the grammar hash after the planner allocated the slot.  
  **Do:** during `plan_campaign()`, derive the exact recipe using the same `recipe_seed(campaign_id, seed, fields, motif_id, recipe_index, parent_ids)` inputs used by materialization; build/hash that exact AST.  
  **Regression:** plan against saturated history → materialize the selected explore slots → assert the **actual proposal** grammar/semantic hashes satisfy the planner's unseen/sparse guarantee.

## P4.3 Exploit

- [x] Proven motifs can transfer from one family to a compatible new dataset.
- [ ] **Either implement semantic-structure transfer or narrow the contract.**  
  Current code transfers `motif_id` evidence via `proven_anywhere`; it does not generally learn “this semantic skeleton worked, transfer that structure to a nearby source”.  
  **Option A:** persist/query successful semantic-skeleton evidence and use it in exploit slot selection.  
  **Option B:** change docs/tests from “proven motif/semantic structure transfer” to the narrower implemented guarantee: “proven motif → compatible new source”.  
  **Regression if A:** prove a successful semantic structure influences exploit selection even when motif identity alone is insufficient.

## P4.4 Mutation vocabulary

- [x] Failure-directed repair.
- [x] `dataset_swap`.
- [x] `motif_change`.
- [x] `normalization_change`.
- [x] `group_change`.
- [x] `subtree_replace`.
- [x] `add_component`.
- [x] Stable concrete `operation` provenance.
- [x] Type/arity/complexity validation.
- [ ] **Honor adaptive mutation-operation allocation at realization time.**  
  A slot can be budgeted for operation X, but if X cannot apply to that parent, the mutation path can fall back to another structural edit or repair. The campaign then consumes X's planned budget without actually testing X.  
  **Do:** choose one explicit contract:
  1. strict realization — an inapplicable pinned operation causes deterministic reallocation to another parent/slot before materialization; or
  2. honest fallback — persist `planned_operation` and `realized_operation`, charge empirical statistics to the realized operation, and expose fallback counts.
  **Regression:** pin an operation that cannot apply to the selected parent and prove the resulting ledger/allocation is honest.

---

# P5 — Archive → generator loop

- [x] Family budgets consume archive evidence.
- [x] Parent selection consumes archive elites.
- [x] Family caps/exploration reserve are enforced.
- [x] Same DB snapshot + seed reproduces the same plan.
- [ ] **Remove stale archive cells during rebuild.**  
  `archive.rebuild()` currently upserts current cells but does not remove obsolete cells. Because niche version participates in the cell identity and `parents()` reads all rows, a long-lived DB can retain pre-current-version niches after a rebuild.  
  **Do:** treat `archive_cells` as derived state and atomically replace it, or delete cells whose stored `niche_version != NICHE_VERSION` before/within rebuild.  
  **Regression:** seed an old-version archive cell, run rebuild, assert the stale cell cannot appear in occupancy or parent selection.
- [ ] **Define and enforce archive refresh lifecycle.**  
  V3 planning reads the current `archive_cells` table but `generate`/plan does not automatically rebuild it. Newly settled candidates therefore may not affect the next campaign until a manual rebuild happens.  
  **Do:** pick one explicit lifecycle:
  - rebuild before V3 planning;
  - rebuild after batches of settled outcomes; or
  - require an explicit archive-refresh step and enforce/check freshness before planning.  
  **Regression:** settle a new candidate and prove the next V3 plan can use it without undocumented manual intervention.

---

# P6 — Crossover

- [x] Two-parent lineage.
- [x] Bounded complexity.
- [x] Child-derived family.
- [x] Distance is an actual selection objective rather than only a filter.
- [ ] **Pass real source metadata into `grammar_distance()`.**  
  `_pair_distance()` currently parses expression strings without the field catalog, so source fields become dataset/category `unknown`; dataset/category Jaccard components therefore contribute little or nothing during real parent selection.  
  **Do:** pass the generator/catalog metadata into distance calculation, or compute source-distance components from persisted source profiles/archive dimensions.  
  **Regression:** use parent expressions with the same topology/depth but known different datasets/categories and assert dataset/category distance changes pair ordering.

---

# P7 — Novelty-aware generation

- [x] Exact novelty.
- [x] Parameter-skeleton novelty.
- [x] Grammar novelty.
- [x] Semantic novelty.
- [x] Dataset novelty.
- [x] Category novelty.
- [x] Motif novelty.
- [x] Archive sparsity.
- [x] Numeric parent→child distance.
- [x] `KEEP`, `DOWNWEIGHT`, `SKIP_REDUNDANT`.
- [x] Skips consume no simulation capacity and preserve lineage.
- [ ] **Evaluate crossover novelty against both parents.**  
  `screen_proposals()` currently loads only `parent_ids[0]`. For a two-parent child, novelty can therefore look favorable relative to parent A while being a near-clone of parent B.  
  **Do:** compute both parent distances. Recommended clone-protection signal: `min(distance_to_parent_A, distance_to_parent_B)`; optionally also report the mean/max for diagnostics. Persist enough detail to audit the decision.  
  **Regression:** create a crossover child close to parent B but far from parent A and prove the novelty score/decision reflects the close parent.

---

# P8 — Ranking integration

- [x] Expected quality, novelty, information gain, family diversity, duplicate penalty, failure risk.
- [x] Grammar/semantic novelty terms are bounded.
- [x] Archive sparsity comes from archive occupancy rather than grammar-frequency duplication.
- [x] Portfolio diversification is submission-stage-only.
- [ ] **Dependency:** final archive-sparsity correctness requires P5 archive lifecycle/freshness to be closed.

---

# P9 — Adaptive motif / mutation allocation

## P9.1 Motifs

- [x] Persist outcome counters.
- [x] Bounded Thompson-style allocation.
- [x] Exploration floor.
- [x] Max-share protection.

## P9.2 Mutation/source statistics

- [x] Source family comes from parent lineage.
- [x] Target family comes from child profile.
- [x] Concrete mutation operation is separate from broad repair class.
- [x] Bounded mutation-operation allocation exists.
- [ ] **Do not let skipped duplicate trials inherit the existing candidate's success outcome.**  
  A `SKIP_REDUNDANT` trial may point to an already successful canonical candidate. `refresh_generation_stats()` joins candidate status, so repeated skipped rediscoveries can be counted as simulated/pass evidence even though no new simulation occurred.  
  **Do:** include trial decision/validation fields in aggregation. A skipped rediscovery may count as a generator attempt if useful, but it must contribute zero to `simulated`, `is_pass`, `corr_pass`, and `active`.  
  **Regression:** start with one existing IS_PASS candidate, rediscover/skip it several times, refresh stats, and prove pass/simulation counters do not increase.
- [ ] **Keep planned vs realized mutation-operation accounting honest.**  
  Close together with P4.4: statistics and future allocation must learn from the operation that actually ran, while optionally retaining the planned operation for policy diagnostics.

---

# P10 — Database / provenance

- [x] V3 strategy/mode/motif/recipe/hash/source profile are queryable.
- [x] Queued mutation/crossover lineage persists.
- [x] Skipped mutation/crossover lineage persists.
- [x] Historical rows are preserved.

---

# P11 — Versioning

- [x] V2 and V3 generator identities are distinct.
- [x] Grammar, motif-registry, and generation-policy versions are explicit.
- [x] V2 remains honestly labelled and backward compatible.

---

# P12 — CLI

- [x] V2 generate path remains available.
- [x] V3 `--strategy`.
- [x] V3 `--motif`.
- [x] `crossover` command.
- [x] `--dry-plan` performs no BRAIN calls or queue writes.
- [x] Dry plan reports V3 distributions.

---

# P13 — Diversity report

- [x] Exact/field/dataset/category diversity.
- [x] Parameter/grammar/semantic structure diversity.
- [x] Motif and effective-count metrics.
- [x] Cross-dataset share.
- [x] Duplicate/near-duplicate rates.
- [x] Parent-child grammar distance.
- [x] Pairwise PnL correlation when available.
- [x] Funnel outcomes by motif.

---

# P14 — Point-in-time policy replay

- [x] Grammar novelty.
- [x] Semantic novelty.
- [x] Archive V2.
- [x] Archive V3.
- [x] Mixed V3.
- [x] Point-in-time leakage protections.
- [x] Equal-budget comparison framework.
- [x] Efficiency/diversity/robustness metrics.

---

# P15 — Tests / regression

Existing V3 regression coverage now includes:

- [x] crossover child family
- [x] role-aware event motifs
- [x] cross-dataset reachability
- [x] explore novelty budgeting proxy
- [x] proven motif transfer
- [x] every promised structural mutation operation
- [x] distance-driven crossover selection
- [x] category novelty / archive sparsity / parent distance
- [x] real archive sparsity ranking
- [x] source→target mutation lineage
- [x] concrete operation statistics
- [x] adaptive mutation allocation
- [x] skipped mutation/crossover provenance
- [x] archive-V2 replay baseline

Required before P15 is truly closed:

- [ ] **Actual planned-vs-materialized structure novelty regression.**  
  Plan and materialize the same slots; test the final proposal hashes, not the planner's proxy hashes.
- [ ] **Semantic-transfer contract regression.**  
  Required only if P4.3 keeps the stronger semantic-structure-transfer claim.
- [ ] **Stale archive-version cleanup regression.**
- [ ] **Archive refresh lifecycle regression.**
- [ ] **Crossover dataset/category distance regression using real metadata.**
- [ ] **Two-parent crossover novelty-distance regression.**
- [ ] **Skipped duplicate does not create simulated/pass evidence regression.**
- [ ] **Pinned mutation operation fallback/reallocation accounting regression.**
- [ ] **Execution evidence.**  
  The named tests exist in the repo, but current GitHub head has no attached Actions/check result proving the documented full-suite count. Keep completion wording tied to reproducible test execution rather than a documentation-only number.

---

# P16 — Remaining implementation order

Recommended order:

```text
1. P9 skipped-trial outcome contamination
2. P6 real metadata in crossover distance
3. P5 stale archive cleanup
4. P5 archive refresh lifecycle
5. P4.2 exact planned-vs-materialized structure hashes
6. P4.4 planned vs realized mutation operation accounting
7. P7 both-parent crossover novelty
8. P4.3 semantic-transfer contract
9. P15 full regression closure
10. equal-budget replay + small live campaigns
```

- [ ] Keep V2 as live default until the above implementation gaps are closed.
- [ ] Run equal-budget V2 vs V3 replay after P15 closes.
- [ ] Run small explicitly named live V3 campaigns after replay is clean.

---

# P17 — Promotion gates

Initial V3 mix remains:

```text
explore   40%
exploit   25%
mutate    25%
crossover 10%
```

Recommended family cap:

```text
max_family_share = 0.35–0.50
```

Promote only if evidence improves:

- [ ] simulation efficiency
- [ ] grammar diversity
- [ ] semantic diversity
- [ ] correlation diversity

without material degradation in:

- [ ] IS_PASS rate
- [ ] CORR_PASS rate
- [ ] robustness-adjusted quality

---

# Final acceptance

Generator V3 is implementation-complete when:

- [ ] All remaining P4/P5/P6/P7/P9/P15 audit items are closed with regressions.
- [x] Exact, parameter, grammar, and semantic diversity are separately measurable.
- [x] Archive niches preserve topology.
- [x] Multi-recipe, multi-field, cross-dataset generation exists.
- [x] Archive family/parent decisions affect generation.
- [x] Two-parent crossover works under bounded complexity.
- [x] V3 is auditable and reproducible.
- [x] Point-in-time replay exists.
- [ ] Equal-budget replay and small live campaigns justify promotion.
- [x] Until then, V2 remains the default.

> **Core rule: search over hypotheses, not merely field names.**
