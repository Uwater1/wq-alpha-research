# Generator V3 TODO

**Goal:** diversity-first symbolic alpha generation while preserving validation, canonical deduplication, lineage, the permanent trial ledger, staged search, scheduler safety, and point-in-time replay.

**Status after third audit:** P0–P15 implementation work is complete, with the remaining P4/P5/P6/P7/P9/P15 gaps closed and regression-covered. The only open final-acceptance items are promotion evidence (equal-budget V2/V3 replay + small live V3 campaigns), which are not implementation gaps.

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
- [x] **The planned structure exactly matches the materialized structure.**  
  `plan_campaign()` derives the exact per-slot recipe from the same `recipe_seed(campaign_id, seed, fields, motif_id, recipe_index, parent_ids)` inputs materialization uses, hashes that AST, and records `planned_grammar_hash`/`planned_semantic_hash` on the slot. Cross-dataset reachability is a deterministic seat rather than a novelty-tiebreak accident.  
  **Regression:** `test_planned_structure_hashes_match_materialized_structures` plans against saturated history, materializes the explore slots, and asserts the actual proposal hashes equal the planned ones and stay unseen.

## P4.3 Exploit

- [x] Proven motifs can transfer from one family to a compatible new dataset.
- [x] **Exploit contract narrowed to the implemented guarantee (Option B).**  
  Cross-family exploit transfer is proven **motif** transfer (`motif_id` evidence via `proven_anywhere`), not general proven-semantic-skeleton transfer. Docs, slot reasons, and tests now state the narrow contract: “proven motif → compatible new source”.  
  **Regression:** `test_exploit_transfers_a_proven_motif_to_a_compatible_source`.

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
- [x] **Adaptive mutation-operation allocation is honored honestly (Option 2).**  
  An inapplicable pinned operation yields an honest fallback: `planned_operation`, `realized_operation`, and `operation_fallback` are persisted, statistics/allocation are charged to the realized edit, and the dry plan exposes fallback counts.  
  **Regression:** `test_pinned_inapplicable_mutation_operation_records_an_honest_fallback` and `test_applicable_pinned_mutation_operation_is_recorded_without_a_fallback`.

---

# P5 — Archive → generator loop

- [x] Family budgets consume archive evidence.
- [x] Parent selection consumes archive elites.
- [x] Family caps/exploration reserve are enforced.
- [x] Same DB snapshot + seed reproduces the same plan.
- [x] **Stale archive cells are removed during rebuild.**  
  `archive_cells` is treated as derived state and atomically replaced inside one transaction, so obsolete niche versions and dropped members can never reappear in occupancy or parent selection.  
  **Regression:** `test_rebuild_drops_stale_niche_version_cells`.
- [x] **Archive refresh lifecycle defined and enforced.**  
  The planner refreshes derived archive state before reading it, so a newly settled candidate can influence the very next V3 plan with no manual rebuild step.  
  **Regression:** `test_newly_settled_candidate_reaches_the_next_plan_without_manual_rebuild`.

---

# P6 — Crossover

- [x] Two-parent lineage.
- [x] Bounded complexity.
- [x] Child-derived family.
- [x] Distance is an actual selection objective rather than only a filter.
- [x] **Real source metadata is passed into `grammar_distance()`.**  
  `_pair_distance()` / `_pick_crossover_pair()` receive the catalog field metadata, so dataset/category Jaccard components are real in parent selection.  
  **Regression:** `test_crossover_distance_uses_real_dataset_and_category_metadata`.

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
- [x] **Crossover novelty is evaluated against both parents.**  
  `screen_proposals()` computes every parent distance and uses `min(distance_to_parent)` for clone protection; the full distance vector is reported on the report/proposal and persisted on the queued/skipped trial.  
  **Regression:** `test_two_parent_crossover_novelty_reflects_the_closest_parent`.

---

# P8 — Ranking integration

- [x] Expected quality, novelty, information gain, family diversity, duplicate penalty, failure risk.
- [x] Grammar/semantic novelty terms are bounded.
- [x] Archive sparsity comes from archive occupancy rather than grammar-frequency duplication.
- [x] Portfolio diversification is submission-stage-only.
- [x] **Dependency:** P5 archive lifecycle/freshness is closed, so archive-sparsity ranking reads a rebuilt, current archive.

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
- [x] **Skipped duplicate trials do not inherit the existing candidate's success outcome.**  
  `refresh_generation_stats()` reads the trial's own decision/validation fields; a skipped rediscovery counts as an attempt but contributes zero to `simulated`, `is_pass`, `corr_pass`, and `active`.  
  **Regression:** `test_skipped_rediscovery_does_not_create_simulated_or_pass_evidence`.
- [x] **Planned vs realized mutation-operation accounting is honest.**  
  Closed with P4.4: statistics and future allocation learn from the realized operation, while `planned_operation` is retained for diagnostics.

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

- [x] **Actual planned-vs-materialized structure novelty regression.**  
  `test_planned_structure_hashes_match_materialized_structures` tests the final proposal hashes, not the planner's proxy hashes.
- [x] **Semantic-transfer contract regression.**  
  P4.3 keeps the narrowed motif-transfer contract, so no stronger regression is required; `test_exploit_transfers_a_proven_motif_to_a_compatible_source` covers it.
- [x] **Stale archive-version cleanup regression.** (`test_rebuild_drops_stale_niche_version_cells`)
- [x] **Archive refresh lifecycle regression.** (`test_newly_settled_candidate_reaches_the_next_plan_without_manual_rebuild`)
- [x] **Crossover dataset/category distance regression using real metadata.** (`test_crossover_distance_uses_real_dataset_and_category_metadata`)
- [x] **Two-parent crossover novelty-distance regression.** (`test_two_parent_crossover_novelty_reflects_the_closest_parent`)
- [x] **Skipped duplicate does not create simulated/pass evidence regression.** (`test_skipped_rediscovery_does_not_create_simulated_or_pass_evidence`)
- [x] **Pinned mutation operation fallback/reallocation accounting regression.** (`test_pinned_inapplicable_mutation_operation_records_an_honest_fallback`)
- [x] **Execution evidence.**  
  A GitHub Actions workflow runs the credential-free offline suite (`pytest -q`) on every push and pull request, so completion wording is tied to a reproducible check result rather than a documentation-only number.

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

Items 1–9 are complete. Item 10 (equal-budget replay + small live V3 campaigns) remains a promotion gate, not an implementation gap.

- [x] Keep V2 as live default until the implementation gaps are closed (gaps are now closed; V2 remains the default until replay/live evidence justifies promotion).
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

- [x] All remaining P4/P5/P6/P7/P9/P15 audit items are closed with regressions.
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
