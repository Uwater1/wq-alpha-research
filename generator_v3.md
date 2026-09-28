# Generator V3 — Architecture and Current Status

**Repository:** `Uwater1/wq-alpha-research`  
**Purpose:** search over economically distinct hypotheses—motifs, structures, datasets, recipes, mutation and crossover—not merely field names.

This file is the compact architecture reference. **`generator_v3_todo.md` is the authoritative implementation checklist.**

## Status

Generator V3 is substantially implemented and remains **opt-in**. The V2 template generator is still the live default unless V3 is explicitly requested with `--strategy`, `--motif`, or `--dry-plan`.

- [x] Typed AST and compatibility-checked expression grammar.
- [x] Motif registry with single-source, two-source, and event/expectation motifs.
- [x] Deterministic per-proposal recipe sampling.
- [x] Explore / exploit / mutate / crossover / mixed planning modes.
- [x] Archive-informed family allocation and parent selection.
- [x] Two-parent crossover with bounded complexity.
- [x] Grammar and semantic structure identities.
- [x] Novelty screening, ranking hooks, V3 provenance, diversity reporting, and point-in-time replay.
- [x] V2 history and default generation remain backward compatible.
- [x] Close the audited correctness/spec gaps in `generator_v3_todo.md` before P16/P17 promotion (all closed with regression coverage).

## Architecture

```text
campaign budget
    ↓
generation policy
    ├── explore
    ├── exploit
    ├── mutate
    └── crossover
    ↓
typed expression grammar + motif registry
    ↓
field/source selection + deterministic recipe
    ↓
novelty + compatibility pre-screen
    ↓
ResearchDB.queue_candidate()
    ↓
validation / dedup / staged search / scheduler
    ↓
simulation outcome
    ↓
archive + empirical generation statistics
    ↺
```

### Trust boundaries

- [x] All generated work still enters through `ResearchDB.queue_candidate()`.
- [x] Canonical deduplication remains authoritative.
- [x] Deterministic compatibility checks remain strict at the queue boundary.
- [x] Research decisions remain recorded in `research_trials`.
- [x] Replay uses point-in-time information rather than future outcomes.
- [x] Random generation is seedable and reproducible.
- [x] Skipped V3 proposals preserve the same lineage detail as queued proposals (`parent_ids`, generation, mutation type, concrete mutation parameters).  
  **Regressions:** `test_skipped_mutation_preserves_generation_parent_and_operation`, `test_skipped_crossover_preserves_both_parents`.

## Core identities

Keep all four levels distinct:

- [x] `canonical_key`: exact normalized expression + settings identity.
- [x] `skeleton_hash`: exact fields, numeric parameter grid collapsed.
- [x] `grammar_skeleton_hash`: operator topology + field types, concrete fields/literals masked.
- [x] `semantic_skeleton_hash`: topology + dataset/category/type source identity.

These identities are complementary; do not replace one with another.

## Source metadata

Expected source profile:

```json
{
  "field_ids": [],
  "datasets": [],
  "categories": [],
  "field_types": [],
  "primary_family": "...",
  "cross_dataset": false
}
```

Rules:

- [x] One dataset → `primary_family = dataset`.
- [x] Multiple datasets → stable `multi:<dataset>+...` family.
- [x] Normal mutation children derive source metadata from the child expression.
- [x] Crossover children persist the child-derived family (`_crossover_proposal()` derives the profile once and sets `Proposal.family = profile["primary_family"]`).  
  **Regression:** `test_crossover_child_family_matches_its_own_source_profile`.

## Grammar and motifs

- [x] AST: `FieldNode`, `LiteralNode`, `CallNode`, `ExprNode`.
- [x] Operator arity/type validation reuses `scripts/compatibility.py`.
- [x] Complexity limits cover depth, nodes, fields, and binary operators.
- [x] FASTEXPR is rendered only after a valid AST is built.
- [x] Single-source and two-source motifs are implemented.
- [x] Event/expectation motifs are gated by compatible types/dataset allow-lists.
- [x] Event/expectation role semantics use inferred field roles (actual, estimate, revision, event/surprise), not dataset membership alone.  
  **Regression:** `test_event_expectation_motifs_are_role_aware_not_dataset_aware`.

## Generation policy

Default mixed allocation:

```text
explore   40%
exploit   25%
mutate    25%
crossover 10%
```

- [x] Family budgets consume `archive.allocate_families()`.
- [x] Mutation/crossover parent pools consume `archive.parents()`.
- [x] Same DB snapshot + seed gives a deterministic plan.
- [x] Empty archives fall back honestly instead of claiming nonexistent lineage.
- [x] Explore actively plans grammar/semantic/archive novelty: historical grammar/semantic counts and archive-cell occupancy feed slot selection, so unseen/sparse structures receive explicit budget.  
  **Regression:** `test_explore_slots_budget_unseen_structures_before_repeats`.
- [x] Cross-dataset motifs have an actual cross-dataset partner pool (second field chosen from the global/other-dataset source pool).  
  **Regression:** `test_ordinary_planning_produces_a_cross_dataset_motif`.
- [x] Exploit transfers proven motifs/semantic structures across compatible datasets (proven-structure → new-source trials are budgeted).  
  **Regression:** `test_exploit_transfers_proven_structure_to_a_new_source`.

## Mutation

Existing failure-directed repair remains useful, but V3 needs a real structural mutation vocabulary.

Implemented concrete edits include field swaps, template changes, window/decay changes, neutralization changes, component removal, group transforms, and signal combination.

- [x] All promised V3 mutation operations are real typed AST edits:  
  `dataset_swap`, `motif_change`, `normalization_change`, `group_change`, `subtree_replace` (+ stable `add_component`).  
  **Regression:** `test_every_v3_mutation_operation_is_generatable` (one positive case per operation).
- [x] Operation naming is normalized: a stable concrete `operation` is persisted for every mutation, separate from the diagnosed repair class.  
  **Regression:** `test_generation_stats_learn_concrete_operations_apart_from_repair_class`.
- [x] Adaptive mutation allocation exists: bounded exploration/exploitation budget across concrete operations from historical outcomes, analogous to motif allocation.  
  **Regressions:** `test_mutation_allocation_rewards_success_and_reserves_exploration`, `test_mutation_allocation_responds_to_new_evidence`.

## Crossover

- [x] Two-parent lineage is supported.
- [x] Initial forms: add/subtract ranked parents, add z-scored parents, multiply ranked parents.
- [x] Child complexity scales with evolved parent complexity under a hard ceiling.
- [x] Near-identical grammar pairs are avoided when alternatives exist.
- [x] Parent selection uses structural distance as an optimization signal: the most distant eligible pair wins deterministically, rng only breaks exact ties.  
  **Regression:** `test_crossover_distance_is_a_selection_objective_not_only_a_filter`.
- [x] Persist child-derived `signal_family`; see Source metadata above.

## Novelty and ranking

Novelty outcomes:

```text
KEEP
DOWNWEIGHT
SKIP_REDUNDANT
```

- [x] Exact, current-skeleton, grammar, semantic, dataset, and motif novelty are available.
- [x] Explicit **category novelty**, **archive niche sparsity**, and numeric **parent→child grammar distance** are in the pre-screen, each exposed separately in the novelty report.  
  **Regressions:** `test_category_novelty_is_tracked_separately_from_datasets`, `test_archive_sparsity_reads_cell_occupancy_not_grammar_frequency`, `test_parent_child_distance_is_numeric_not_equality_only`.
- [x] Ranking has bounded/capped correlated novelty inputs.
- [x] `archive_sparsity` measures actual archive niche occupancy (cell/member counts), distinct from `grammar_novelty`.  
  **Regression:** `test_archive_sparsity_is_real_occupancy_not_grammar_frequency`.
- [x] `portfolio_diversification` is explicitly scoped and documented as submission-ranking-only and tested as such.  
  **Regression:** `test_portfolio_diversification_is_submission_stage_only`.

## Adaptive statistics

- [x] Motif outcome tables and bounded motif allocation exist.
- [x] Source→target family transitions are recorded correctly (source from parent lineage, target from the child profile).  
  **Regression:** `test_generation_stats_preserve_cross_family_mutation_lineage`.
- [x] Concrete mutation operation statistics use the actual edit (`parameters["operation"]`) rather than only broad `mutation_type`.
- [x] Bounded adaptive mutation allocation is built from those corrected statistics.  
  **Regressions:** `test_mutation_allocation_rewards_success_and_reserves_exploration`, `test_mutation_allocation_responds_to_new_evidence`.

## Provenance

- [x] Candidate rows expose queryable V3 strategy/mode/motif/hash/profile fields.
- [x] `research_trials` records V3 decision metadata.
- [x] Historical rows are not rewritten.
- [x] Novelty-skipped mutations/crossovers retain complete lineage (`record_generation_decision()` + caller persist parents, generation, mutation type, mutation parameters).

## Replay and promotion

- [x] V3 replay policies include grammar novelty, semantic novelty, archive V3, and mixed V3.
- [x] Replay leakage checks protect point-in-time decisions.
- [x] Replay reports efficiency and effective diversity metrics.
- [x] The explicit **archive V2** comparison policy is in the benchmark matrix (pre-V3 global top-elite selection).  
  **Regressions:** `test_archive_v2_reproduces_pre_v3_global_top_elite_selection`, `test_archive_v2_is_in_the_benchmark_matrix`.

Do not promote V3 to the live default until replay and small live campaigns show better simulation efficiency with maintained/improved structural, semantic, correlation, and robustness quality.

## Files

Primary V3 implementation:

```text
scripts/expression_grammar.py
scripts/generation_policy.py
scripts/generator.py
scripts/diversity.py
scripts/archive.py
scripts/ranking.py
scripts/research_db.py
scripts/policy_replay.py
```

Primary tests:

```text
tests/test_expression_grammar.py
tests/test_generation_policy.py
tests/test_generator_v3.py
tests/test_diversity_metrics.py
tests/test_policy_replay.py
```

## Agent rules

- [x] Reuse `compatibility.py`; never create a second type system.
- [x] Reuse `ResearchDB.queue_candidate()`; never create a bypass queue.
- [x] Preserve canonical identity and the permanent trial ledger.
- [x] Keep generation deterministic for a fixed snapshot/seed/version.
- [x] Keep replay point-in-time safe.
- [ ] Do not mark an audited gap complete until a regression test proves the intended behavior, not merely the current implementation.

> **Core rule: search over hypotheses, not merely field names.**
