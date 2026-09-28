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
- [ ] Close the remaining second-audit gaps below before treating P0–P15 as fully implementation-complete.

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
- [ ] **Planned structural novelty must match the structure actually materialized.**  
  Current pre-materialization scoring hashes a motif with `Recipe()`, while materialization later samples the real deterministic recipe; topology-changing recipe choices can therefore invalidate the planner's novelty assumption.  
  **Do:** derive the exact recipe inside planning from the same campaign/seed/fields/motif/recipe-index/parents inputs used by materialization, build that AST, and budget its real grammar/semantic hashes.
- [ ] **Clarify exploit semantics.**  
  Current cross-family transfer is proven **motif** transfer; it is not a general proven-semantic-skeleton transfer mechanism.  
  **Do:** either implement empirical semantic-skeleton transfer or narrow all docs/tests to the actual contract: “proven motif → compatible new source”.

## Mutation

- [x] Failure-directed repair remains available.
- [x] Structural operations include `dataset_swap`, `motif_change`, `normalization_change`, `group_change`, `subtree_replace`, and `add_component`.
- [x] Concrete operations are persisted separately from broad repair classes.
- [x] Adaptive mutation-operation budgets are planned from historical outcomes.
- [ ] **A planned mutation operation must be realized honestly.**  
  A pinned operation may fail for a parent and materialization can fall through to another structural edit or repair, while the planner still counted the original operation budget.  
  **Do:** if a pinned operation cannot apply, either (a) return a recorded fallback with both `planned_operation` and `realized_operation`, then account statistics/budget by the realized edit, or (b) deterministically reallocate that slot before materialization. Add a regression that forces an inapplicable operation.

## Crossover

- [x] Two-parent lineage.
- [x] Bounded crossover complexity.
- [x] Child-derived source family.
- [x] Distance is used as a parent-selection objective.
- [ ] **Feed real field metadata into crossover distance.**  
  `_pair_distance()` currently calls `grammar_distance()` on expression strings without catalog metadata, so parsed fields become `unknown` and dataset/category distance components collapse.  
  **Do:** pass the generator/catalog field metadata into `grammar_distance()`, or compute distance from persisted source metadata. Add a fixture with identical topology but different datasets/categories proving those components affect pair ordering.

## Archive lifecycle

- [x] Archive niches use topology-preserving grammar/semantic identities.
- [x] Planner and ranking can consume archive occupancy.
- [ ] **Rebuild must remove stale archive cells.**  
  `archive.rebuild()` upserts current cells but does not delete cells from obsolete niche definitions; long-lived databases can retain old-version cells in the parent pool.  
  **Do:** atomically replace derived archive cells, or delete cells whose stored niche version is not the current `NICHE_VERSION`. Add an upgrade regression with a pre-existing old-version cell.
- [ ] **Define/implement archive refresh lifecycle.**  
  V3 planning currently reads whatever is already in `archive_cells`; normal `generate` does not rebuild it automatically.  
  **Do:** refresh derived archive state at a deliberate lifecycle boundary (for example before V3 planning or after settled-result batches), or make the required refresh an explicit enforced campaign step. Test that newly settled candidates can influence the next V3 plan without hidden manual maintenance.

## Novelty

- [x] Exact, parameter-skeleton, grammar, semantic, dataset, category, motif, archive-sparsity, and parent-distance components.
- [x] `KEEP`, `DOWNWEIGHT`, `SKIP_REDUNDANT`.
- [x] Skipped proposals consume no simulation capacity and preserve lineage.
- [ ] **Crossover parent-distance novelty should use both parents.**  
  Current screening passes only `parent_ids[0]`, so a two-parent child is evaluated against one lineage branch.  
  **Do:** define the crossover aggregation rule (recommended: minimum distance to either parent for clone protection, with mean distance optionally reported) and persist/report both-parent distance evidence.

## Ranking

- [x] Archive sparsity uses archive occupancy rather than grammar-frequency duplication.
- [x] Correlated novelty terms are bounded.
- [x] Portfolio diversification is explicitly submission-stage-only.
- [ ] Ranking/archive sparsity correctness depends on the archive lifecycle gaps above; close those before final promotion.

## Adaptive statistics

- [x] Source family comes from parent lineage; target family from child profile.
- [x] Concrete mutation operations are distinct from repair classes.
- [x] Bounded adaptive motif and mutation-operation allocation exist.
- [ ] **Skipped duplicates must not inherit success evidence from the existing candidate.**  
  A `SKIP_REDUNDANT` trial can point to an already successful canonical candidate; `refresh_generation_stats()` currently joins candidate status and can count that skipped rediscovery as a successful simulated trial.  
  **Do:** include trial decision/validation outcome in aggregation. A skipped rediscovery may count as a generator attempt if desired, but must contribute zero `simulated`, `is_pass`, `corr_pass`, and `active` evidence.

## Provenance

- [x] Queryable V3 strategy/mode/motif/hash/profile fields.
- [x] Full lineage for queued and novelty-skipped mutation/crossover proposals.
- [x] Historical rows are not destructively rewritten.

## Replay and promotion

- [x] Grammar novelty, semantic novelty, archive V2, archive V3, and mixed V3 replay arms.
- [x] Point-in-time leakage protections.
- [x] Efficiency/diversity metrics.
- [ ] Equal-budget V2/V3 replay and small live V3 campaigns still gate promotion.

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
- [ ] Do not mark the remaining audit gaps complete until regressions prove the intended behavior, not merely the current code path.

> **Core rule: search over hypotheses, not merely field names.**
