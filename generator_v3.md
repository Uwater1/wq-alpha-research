# Generator V3 — Architecture and Current Status

**Repository:** `Uwater1/wq-alpha-research`  
**Purpose:** search economically distinct hypotheses—motifs, structures, datasets, recipes, mutation and crossover—without weakening validation, deduplication, lineage, scheduler safety, or point-in-time replay.

**Authoritative checklist:** `generator_v3_todo.md`.

## Status

Generator V3 is substantially implemented and remains **opt-in**. V2 stays the live default unless V3 is explicitly requested.

Confirmed complete:

- [x] Typed AST + compatibility-checked expression grammar.
- [x] Motif registry, deterministic recipes, explore/exploit/mutate/crossover planning.
- [x] Child-derived source profiles/families for normal mutation and crossover.
- [x] Concrete structural mutation vocabulary.
- [x] Archive-informed family allocation and parent selection.
- [x] Grammar/semantic identities, novelty screening, ranking hooks, provenance, diversity reporting, and replay.
- [x] Adaptive motif allocation and mutation-operation statistics/allocation.
- [x] Archive-V2 replay baseline.
- [x] V2 compatibility and default behavior remain intact.
- [x] Second-audit gaps (P4.2/P4.3/P4.4/P5/P6/P7/P9.2/P15) are closed with regressions; only promotion evidence remains.

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
typed grammar + motif registry + deterministic recipe
    ↓
novelty / compatibility pre-screen
    ↓
ResearchDB.queue_candidate()
    ↓
staged search / scheduler / simulation
    ↓
trial ledger + archive + empirical generation statistics
    ↺
```

## Core identities

- [x] `canonical_key`: exact expression + settings identity.
- [x] `skeleton_hash`: exact fields, numeric grid collapsed.
- [x] `grammar_skeleton_hash`: operator topology + field types, concrete fields/literals masked.
- [x] `semantic_skeleton_hash`: topology + dataset/category/type identity.

These identities are complementary.

## Source metadata

Expected profile:

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

- [x] One dataset → that dataset is the primary family.
- [x] Multiple datasets → stable `multi:<dataset>+...` family.
- [x] Crossover children persist the family derived from their final sources.

## Generation policy

Default mixed allocation:

```text
explore   40%
exploit   25%
mutate    25%
crossover 10%
```

- [x] Family allocation consumes archive evidence.
- [x] Parent selection consumes archive elites.
- [x] Cross-dataset motifs are reachable in ordinary planning.
- [x] Proven motifs can transfer to compatible new datasets.
- [x] **Planned structural novelty matches the structure actually materialized.**  
  Planning derives the exact per-slot recipe from the same campaign/seed/fields/motif/recipe-index/parents inputs materialization uses, hashes that AST, and records the planned grammar/semantic hashes on the slot. Cross-dataset reachability is a deterministic guarantee rather than a tiebreak accident.
- [x] **Exploit semantics are the narrowed, implemented contract.**  
  Cross-family transfer is proven **motif** transfer (“proven motif → compatible new source”), and the docs/tests now say exactly that; it is not a general proven-semantic-skeleton transfer mechanism.

## Mutation

- [x] Failure-directed repair remains available.
- [x] Structural operations include `dataset_swap`, `motif_change`, `normalization_change`, `group_change`, `subtree_replace`, and `add_component`.
- [x] Concrete operations are persisted separately from broad repair classes.
- [x] Adaptive mutation-operation budgets are planned from historical outcomes.
- [x] **A planned mutation operation is realized honestly.**  
  A pinned operation that cannot apply to its parent returns a recorded fallback carrying both `planned_operation` and `realized_operation` plus an `operation_fallback` flag. Statistics and future allocation are charged to the realized edit, and the dry plan reports fallback counts.

## Crossover

- [x] Two-parent lineage.
- [x] Bounded crossover complexity.
- [x] Child-derived source family.
- [x] Distance is used as a parent-selection objective.
- [x] **Real field metadata feeds crossover distance.**  
  `_pair_distance()` passes the catalog field metadata into `grammar_distance()`, so dataset/category Jaccard components separate a cross-dataset pair from a same-topology, same-dataset one and affect pair ordering.

## Archive lifecycle

- [x] Archive niches use topology-preserving grammar/semantic identities.
- [x] Planner and ranking can consume archive occupancy.
- [x] **Rebuild removes stale archive cells.**  
  `archive.rebuild()` atomically replaces the derived `archive_cells` table, so cells from obsolete niche definitions (or whose members are gone) cannot linger in occupancy or parent selection.
- [x] **Archive refresh lifecycle is defined and enforced.**  
  The planner refreshes derived archive state before reading it, so newly settled candidates influence the next V3 plan without a manual rebuild.

## Novelty

- [x] Exact, parameter-skeleton, grammar, semantic, dataset, category, motif, archive-sparsity, and parent-distance components.
- [x] `KEEP`, `DOWNWEIGHT`, `SKIP_REDUNDANT`.
- [x] Skipped proposals consume no simulation capacity and preserve lineage.
- [x] **Crossover parent-distance novelty uses both parents.**  
  Screening computes every parent distance and uses the **minimum** for clone protection; the full distance vector is reported and persisted on the trial, so a child close to parent B no longer passes on the strength of being far from parent A.

## Ranking

- [x] Archive sparsity uses archive occupancy rather than grammar-frequency duplication.
- [x] Correlated novelty terms are bounded.
- [x] Portfolio diversification is explicitly submission-stage-only.
- [x] Ranking/archive sparsity correctness now rests on a rebuilt, current archive (lifecycle closed above).

## Adaptive statistics

- [x] Source family comes from parent lineage; target family from child profile.
- [x] Concrete mutation operations are distinct from repair classes.
- [x] Bounded adaptive motif and mutation-operation allocation exist.
- [x] **Skipped duplicates do not inherit success evidence from the existing candidate.**  
  `refresh_generation_stats()` reads the trial's own decision/validation, so a `SKIP_REDUNDANT` rediscovery counts as a generator attempt but contributes zero `simulated`, `is_pass`, `corr_pass`, and `active` evidence.

## Provenance

- [x] Queryable V3 strategy/mode/motif/hash/profile fields.
- [x] Full lineage for queued and novelty-skipped mutation/crossover proposals.
- [x] Historical rows are not destructively rewritten.

## Replay and promotion

- [x] Grammar novelty, semantic novelty, archive V2, archive V3, and mixed V3 replay arms.
- [x] Point-in-time leakage protections.
- [x] Efficiency/diversity metrics.
- [x] Equal-budget V2/V3 replay is available; small live V3 campaigns still gate promotion.

## Primary files

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

Primary regressions:

```text
tests/test_expression_grammar.py
tests/test_generation_policy.py
tests/test_generator_v3.py
tests/test_diversity_metrics.py
tests/test_archive.py
tests/test_policy_replay.py
```

## Agent rules

- [x] Reuse `compatibility.py`.
- [x] Reuse `ResearchDB.queue_candidate()`.
- [x] Preserve canonical identity and the permanent trial ledger.
- [x] Keep deterministic planning/materialization for a fixed snapshot and seed.
- [x] Keep replay point-in-time safe.
- [x] Remaining audit gaps were marked complete only after regressions proved the intended behavior, not merely the current code path.

> **Core rule: search over hypotheses, not merely field names.**
