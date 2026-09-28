# Generator V3 TODO

**Goal:** diversity-first symbolic alpha generation while preserving validation, canonical deduplication, lineage, the permanent trial ledger, staged search, scheduler safety, and point-in-time replay.

**Status:** All P0–P15 audit items are closed with regression coverage (473 offline tests green). Promotion gates remain open: equal-budget V2 vs V3 replay and small explicitly named live V3 campaigns must justify promotion before the default changes.

**Live default:** unchanged. V2 remains the default unless `--strategy`, `--motif`, or `--dry-plan` explicitly selects V3.

---

# P0 — Diversity identities and source metadata

## P0.1 Child source profile

- [x] `derive_source_profile()` returns field IDs, datasets, categories, types, primary family, and cross-dataset flag.
- [x] Single-dataset children use that dataset as `primary_family`.
- [x] Multi-dataset children use a stable composite family such as `multi:analyst4+fundamental2`.
- [x] Normal mutation children recompute family/source metadata from the child.
- [x] **Fix crossover family persistence.** `_crossover_proposal()` derives the child profile once and sets `Proposal.family = profile["primary_family"]`.  
  **Regressions:** `test_crossover_child_family_matches_its_own_source_profile`, `test_v2_mutation_child_family_is_derived_from_the_child`.

## P0.2 Structural identities

- [x] Keep `canonical_key`.
- [x] Keep existing `skeleton_hash` behavior.
- [x] Add `grammar_skeleton` / `grammar_skeleton_hash`.
- [x] Add `semantic_skeleton` / `semantic_skeleton_hash`.
- [x] Same topology + different fields collapse under grammar hash.
- [x] Dataset/category source changes remain visible under semantic hash.
- [x] Numeric-only variants collapse under grammar/semantic hashes.

## P0.3 Archive niche identity

- [x] Archive niches use topology-preserving grammar/semantic hashes.
- [x] Niche metadata includes family, dataset/category sets, motif, depth, field count, cross-dataset flag, turnover bucket, mutation type, and generation mode.
- [x] Operator sets are descriptive metadata only.

---

# P1 — Typed expression grammar

- [x] `FieldNode`, `LiteralNode`, `CallNode`, `ExprNode`.
- [x] Reuse `scripts/compatibility.py`; no second type system.
- [x] Validate known arity/type constraints before generated calls are accepted.
- [x] Deterministic FASTEXPR rendering.
- [x] Complexity budgets for depth, nodes, fields, and binary operators.
- [x] Grammar tests require no BRAIN calls.
- [x] Tolerant parsing covers real stored shapes such as `trade_when(a>b,...)` with fallback hashing for unmodelled legacy syntax.

---

# P2 — Motif registry

- [x] `Motif` registry replaces a large flat template catalogue as the V3 search primitive.
- [x] Single-source motifs: level, momentum/reversion/change/acceleration, smoothing, volatility adjustment, group transforms, ranked level.
- [x] Two-source motifs: spread, ratio, rank difference, normalized difference, confirmation/contrarian, cross-dataset composite.
- [x] Event/expectation motifs exist and are gated by type/dataset metadata.
- [x] **Make event/expectation eligibility role-aware.** Semantic roles (actual, estimate, revision, event/surprise, …) are inferred deterministically and `motif_eligible()` requires compatible roles, not just dataset membership.  
  **Regressions:** `test_event_expectation_motifs_are_role_aware_not_dataset_aware`, `test_field_roles_are_inferred_deterministically` (positive and negative).

---

# P3 — Independent deterministic recipes

- [x] Per-proposal RNG seed includes campaign, global seed, field IDs, motif, recipe index, and parents.
- [x] Unrelated proposals do not perturb an existing proposal's recipe.
- [x] Independently sample lookback, smoothing, decay, neutralization, group, truncation, normalization, winsorization, rank/zscore, and sign.
- [x] Persist recipe index + full recipe metadata.
- [x] One field can produce multiple reproducible recipes.

---

# P4 — Generation strategy layer

## P4.1 Modes

- [x] `explore`
- [x] `exploit`
- [x] `mutate`
- [x] `crossover`
- [x] `mixed`
- [x] Every realized V3 proposal records its generation mode; unavailable lineage modes fall back honestly.

## P4.2 Explore

- [x] Prefer unseen motifs and under-tested fields/families.
- [x] Post-materialization novelty screening sees grammar/semantic history.
- [x] **Plan grammar/semantic novelty before materialization.** Explore slots budget unseen grammar skeletons/semantic structures and sparse archive cells from grammar/semantic counts + archive occupancy before re-seating history-saturated structures.  
  **Regression:** `test_explore_slots_budget_unseen_structures_before_repeats`.
- [x] **Make cross-dataset motifs reachable in ordinary planning.** Motifs requiring distinct datasets draw partners from the compatible global source pool, so `cross_dataset_composite` is reachable in ordinary planning.  
  **Regression:** `test_ordinary_planning_produces_a_cross_dataset_motif` (deterministic fixture).

## P4.3 Exploit

- [x] Prefer proven motifs within observed family evidence.
- [x] **Support proven-structure transfer to new datasets/sources.** Proven motif/semantic evidence seeds compatible new datasets instead of being keyed only to the current family.  
  **Regression:** `test_exploit_transfers_proven_structure_to_a_new_source` (“proven structure → new source”).

## P4.4 Mutate

Existing concrete edits include field swap, template change, window change, decay change, neutralization change, group transform, component removal, and signal combination.

- [x] Keep failure-directed repair.
- [x] Implement real V3 `dataset_swap`.
- [x] Implement real V3 `motif_change`.
- [x] Implement real V3 `normalization_change`.
- [x] Implement real V3 `group_change`.
- [x] Implement typed `subtree_replace`.
- [x] Normalize `add_component` / structural-combine naming as a stable concrete operation.  
  **Done for all mutation items:** typed AST edits with AST/type/complexity validation and a stable persisted `operation`. **Regressions:** `test_every_v3_mutation_operation_is_generatable` (one positive case per promised operation), `test_structural_dataset_swap_moves_to_another_dataset`, `test_structural_normalization_change_flips_the_normalizer`, `test_structural_group_change_moves_one_level`, `test_structural_add_component_combines_a_fresh_source`, `test_add_component_is_the_stable_combine_operation_name`.

---

# P5 — Archive → generator loop

- [x] `plan_campaign()` produces explicit slots with mode, family, motif, parents, recipe index, and reason.
- [x] Consume `archive.allocate_families()`.
- [x] Enforce bounded family share.
- [x] Reserve under-tested-family exploration.
- [x] Consume `archive.parents()`.
- [x] Parent selection rotates across archive families/niches.
- [x] Planned budget equals requested budget.
- [x] Same DB snapshot + seed reproduces the same plan.
- [x] Archive state affects actual generation.

---

# P6 — Crossover

## P6.1 Structural distance

- [x] `grammar_distance(A, B) -> [0,1]`.
- [x] Operator-tree difference.
- [x] Dataset-set Jaccard distance.
- [x] Category-set Jaccard distance.
- [x] Field-count difference.
- [x] Depth difference.
- [x] Optional motif mismatch component.

## P6.2 Parent selection

- [x] Reject same-grammar/near-identical pairs when better alternatives exist.
- [x] **Use distance as a real selection objective.** Eligible pairs are scored with `grammar_distance()` (motif IDs included when known) and the structurally most distant pair wins deterministically; the seeded rng only breaks exact ties.  
  **Regression:** `test_crossover_distance_is_a_selection_objective_not_only_a_filter`.

## P6.3 Crossover forms and complexity

- [x] `add(rank(A), rank(B))`.
- [x] `subtract(rank(A), rank(B))`.
- [x] `add(zscore(A), zscore(B))`.
- [x] `multiply(rank(A), rank(B))`.
- [x] Complexity budget scales to evolved parents under a hard ceiling.
- [x] Persist both parent IDs.
- [x] Child family must be derived from final child sources.  
  **Regression:** `test_crossover_child_family_matches_its_own_source_profile` (genuinely cross-family parent pair).

---

# P7 — Novelty-aware generation

Implemented novelty facts:

- [x] exact-candidate novelty
- [x] current `skeleton_hash` novelty
- [x] grammar-skeleton novelty
- [x] semantic-skeleton novelty
- [x] dataset novelty
- [x] motif novelty

Outcomes:

- [x] `KEEP`
- [x] `DOWNWEIGHT`
- [x] `SKIP_REDUNDANT`
- [x] Skips consume no simulation capacity and remain in `research_trials`.

Remaining dimensions:

- [x] **Category novelty.** Category history is tracked separately from dataset history and exposed as a `NoveltyReport` component.  
  **Regression:** `test_category_novelty_is_tracked_separately_from_datasets`.
- [x] **Archive niche sparsity.** Scored from actual archive-cell occupancy/member counts, not grammar frequency.  
  **Regression:** `test_archive_sparsity_reads_cell_occupancy_not_grammar_frequency`.
- [x] **Numeric parent→child grammar distance.** `screen_novelty()` scores `grammar_distance(parent, child)` from parent grammar/source metadata; equality-only penalty is retained only as a fallback.  
  **Regression:** `test_parent_child_distance_is_numeric_not_equality_only`, `test_parent_child_distance_sees_parents_from_an_earlier_campaign`.
- [x] **Full skip provenance.** Skipped mutations/crossovers retain their parents and operation (P10 skip-lineage work).  
  **Regressions:** `test_skipped_mutation_preserves_generation_parent_and_operation`, `test_skipped_crossover_preserves_both_parents`.

---

# P8 — Ranking integration

- [x] Expected quality, exact novelty, grammar novelty, semantic novelty, information gain, family diversity, failure risk, duplicate penalty.
- [x] Correlated novelty contribution is bounded/capped.
- [x] Ranking remains advisory.
- [x] **Make `archive_sparsity` a real archive signal.** Derived from archive niche occupancy/member counts, distinct from grammar novelty.  
  **Regressions:** `test_archive_sparsity_is_real_occupancy_not_grammar_frequency` (equal grammar frequency, differing sparsity), `test_ranking_penalises_parameter_clones_and_rewards_new_structure`.
- [x] **Clarify `portfolio_diversification` stage.** Documented and tested as submission-stage-only: it never affects simulation priority, only submission ranking.  
  **Regression:** `test_portfolio_diversification_is_submission_stage_only`.

---

# P9 — Adaptive motif / mutation allocation

## P9.1 Motifs

- [x] Persist motif outcome counters.
- [x] Bounded Thompson-style motif allocation.
- [x] Reserve exploration for under-tested motifs.
- [x] Successful motifs can earn more budget.
- [x] No motif can monopolize the campaign.

## P9.2 Mutation/source transitions

- [x] **Record correct source→target families.** Source family = parent/source lineage; target family = final child-derived profile.  
  **Regression:** `test_generation_stats_preserve_cross_family_mutation_lineage` (cross-family mutation).
- [x] **Record concrete mutation operation, not only broad repair class.** `mutation_parameters["operation"]` (normalized operation vocabulary) is aggregated separately from `mutation_type`.  
  **Regression:** `test_generation_stats_learn_concrete_operations_apart_from_repair_class`.
- [x] **Implement adaptive mutation allocation.** Bounded Thompson-style budget across concrete mutation operations from corrected historical outcomes, analogous to motif allocation, with reserved exploration.  
  **Regressions:** `test_mutation_allocation_rewards_success_and_reserves_exploration` (successful operation gains budget, untested operation retains exploration), `test_mutation_allocation_responds_to_new_evidence`.

---

# P10 — Database / provenance

- [x] Candidate fields expose strategy, generation mode, motif, recipe ID/index, grammar hash, semantic hash, source profile, policy/grammar versions.
- [x] `research_trials` records campaign/candidate IDs, V3 mode/version/motif/recipe, hashes, source profile, decision, and reason.
- [x] Queued crossover lineage persists both parents.
- [x] Historical rows are migrated in place; no destructive rewrite.
- [x] **Preserve complete lineage for novelty-skipped proposals.** `record_generation_decision()` + caller persist `parent_ids`, generation, mutation type, and mutation parameters for skipped proposals.  
  **Regressions:** `test_skipped_mutation_preserves_generation_parent_and_operation`, `test_skipped_crossover_preserves_both_parents`.

---

# P11 — Versioning

- [x] V3 identity: `catalog-generator-v3`.
- [x] V2 path remains honestly labelled `catalog-generator-v2`.
- [x] `GRAMMAR_VERSION`.
- [x] `MOTIF_REGISTRY_VERSION`.
- [x] `GENERATION_POLICY_VERSION`.

---

# P12 — CLI

- [x] V2-compatible `python -m wq generate`.
- [x] `python -m wq mutate`.
- [x] V3 `--strategy`.
- [x] V3 `--motif`.
- [x] Explicit `crossover` command.
- [x] `--dry-plan` performs no BRAIN calls and no queue writes.
- [x] Dry plan reports mode/family/motif/dataset/grammar/semantic distribution.

---

# P13 — Diversity report

- [x] trial count
- [x] unique exact candidates
- [x] unique fields/datasets/categories
- [x] current/grammar/semantic skeleton counts
- [x] motif count
- [x] entropy-based effective motif/dataset/grammar/semantic/family counts
- [x] cross-dataset share
- [x] duplicate / near-duplicate rates
- [x] median parent→child grammar distance
- [x] median pairwise PnL correlation when cached data permits
- [x] IS_PASS / CORR_PASS / ACTIVE breakdowns by motif

---

# P14 — Point-in-time policy replay

- [x] `grammar_novelty`.
- [x] `semantic_novelty`.
- [x] `archive_v3`.
- [x] `mixed_v3`.
- [x] Decision cards exclude outcomes and raw hidden state.
- [x] Outcomes/ranking/surrogate evidence are resolved as-of the decision clock.
- [x] Leakage self-checks cover future candidates and future outcome stages.
- [x] Metrics include simulation efficiency, top-k recall, waste, effective V3 diversity, turnover/correlation failures, and robustness adjustment.
- [x] **Add the explicit archive-V2 comparison requested by the benchmark matrix.** `archive_v2` reproduces the intended pre-V3 global top-elite archive behavior and is included in the benchmark matrix for equal-budget comparisons.  
  **Regressions:** `test_archive_v2_reproduces_pre_v3_global_top_elite_selection`, `test_archive_v2_is_in_the_benchmark_matrix`.

---

# P15 — Tests / regression

Existing coverage:

- [x] AST typing / arity.
- [x] motif eligibility and rendering.
- [x] recipe determinism/independence.
- [x] source-profile derivation.
- [x] grammar/semantic hash behavior.
- [x] structural distance.
- [x] archive niche identity.
- [x] family budget conservation.
- [x] crossover lineage/complexity.
- [x] deterministic plans.
- [x] novelty skip/no-capacity behavior.
- [x] V3 provenance columns.
- [x] generation-stat refresh.
- [x] legacy DB migration.
- [x] real comparison-expression parsing.
- [x] V2 canonical/dedup/queue/replay invariants remain covered.

Required regressions before P15 can be closed:

- [x] Crossover `family == source_profile.primary_family`.
- [x] Ordinary V3 planning produces at least one genuine cross-dataset motif under a deterministic fixture.
- [x] Positive test for each promised V3 mutation operation.
- [x] Negative/positive role-semantic tests for event/expectation motifs.
- [x] Novelty report tests category novelty, archive sparsity, and numeric parent-child distance.
- [x] Ranking test distinguishes archive sparsity from grammar novelty.
- [x] Generation stats preserve `source_family != target_family` when appropriate.
- [x] Generation stats learn concrete mutation operation separately from repair class.
- [x] Skipped mutation preserves generation/parent/operation.
- [x] Skipped crossover preserves both parents.
- [x] Adaptive mutation allocation responds to evidence while reserving exploration.
- [x] Archive-V2 replay policy/equivalence is benchmarked.

---

# P16 — Promotion order

Do not reinterpret P0–P15 as a reason to switch the live default. Close the reopened audit items first, then benchmark.

Recommended remaining implementation order:

```text
1. P0/P6 crossover family correctness
2. P4 cross-dataset planning + mutation vocabulary
3. P7 novelty completeness
4. P8 real archive sparsity
5. P9 source→target stats + adaptive mutation allocation
6. P10 skipped-lineage provenance
7. P2 semantic-role tightening
8. P14 archive-V2 benchmark
9. P15 regression closure
10. replay + small live V3 campaigns
```

- [ ] **Keep V2 as live default until comparative evidence exists.**
- [ ] Run equal-budget V2 vs V3 replay after the above fixes.
- [ ] Run small explicitly named live V3 campaigns only after replay is clean.

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

Promote only if evidence shows improvement in:

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

Generator V3 is ready for promotion when:

- [x] All open P0–P15 audit items above have regression coverage.
- [x] Exact, parameter, grammar, and semantic diversity are separately measurable.
- [x] Archive niches preserve expression topology.
- [x] One field can generate multiple deterministic recipes.
- [x] Multiple economic motifs and multi-field expressions exist.
- [x] Archive family/parent decisions affect generation.
- [x] Two-parent crossover works under bounded complexity.
- [x] V3 is auditable and reproducible.
- [x] V3 can be evaluated with point-in-time-safe replay.
- [ ] Equal-budget replay and small live campaigns justify promotion over V2.
- [x] Until then, V2 remains the default.

> **Core rule: search over hypotheses, not merely field names.**
