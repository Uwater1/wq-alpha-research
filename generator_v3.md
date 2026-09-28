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
- [ ] Close the audited correctness/spec gaps in `generator_v3_todo.md` before P16/P17 promotion.

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
- [ ] Skipped V3 proposals must preserve the same lineage detail as queued proposals.  
  **Do:** extend the skip-decision path to persist `parent_ids`, generation, mutation type, and concrete mutation parameters.

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
- [ ] Crossover children must also persist the child-derived family.  
  **Do:** in `CandidateGenerator._crossover_proposal()`, derive the source profile once and set `Proposal.family = profile["primary_family"]`; add a regression asserting candidate/trial family equals the crossover child profile.

## Grammar and motifs

- [x] AST: `FieldNode`, `LiteralNode`, `CallNode`, `ExprNode`.
- [x] Operator arity/type validation reuses `scripts/compatibility.py`.
- [x] Complexity limits cover depth, nodes, fields, and binary operators.
- [x] FASTEXPR is rendered only after a valid AST is built.
- [x] Single-source and two-source motifs are implemented.
- [x] Event/expectation motifs are gated by compatible types/dataset allow-lists.
- [ ] Event/expectation role semantics need stronger eligibility than dataset membership alone.  
  **Do:** attach or infer field roles/tags (for example actual, estimate, revision, event/surprise) and require compatible roles for motifs such as `actual_vs_expectation` and `surprise_normalization`.

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
- [ ] Explore should actively plan grammar/semantic/archive novelty, not only check it after materialization.  
  **Do:** include historical grammar/semantic counts and archive-cell occupancy in slot selection so unseen/sparse structures receive explicit budget.
- [ ] Cross-dataset motifs need an actual cross-dataset partner pool.  
  **Do:** when a motif requires distinct datasets, choose the second field globally (or from another eligible dataset) rather than from the same family-only source list.
- [ ] Exploit should transfer proven motifs/semantic structures across compatible datasets.  
  **Do:** decouple “proven motif” evidence from the current family where appropriate and explicitly budget proven-structure → new-source trials.

## Mutation

Existing failure-directed repair remains useful, but V3 needs a real structural mutation vocabulary.

Implemented concrete edits include field swaps, template changes, window/decay changes, neutralization changes, component removal, group transforms, and signal combination.

- [ ] Implement the remaining promised V3 mutation operations as real AST edits:  
  `dataset_swap`, `motif_change`, `normalization_change`, `group_change`, `subtree_replace`.
- [ ] Normalize operation naming so statistics learn the concrete edit rather than only the failure class.  
  **Do:** persist a stable `operation` for every mutation and keep the diagnosed repair class separately.
- [ ] Adaptive mutation allocation is still missing.  
  **Do:** allocate bounded exploration/exploitation budget across concrete mutation operations from historical outcomes, analogous to motif allocation.

## Crossover

- [x] Two-parent lineage is supported.
- [x] Initial forms: add/subtract ranked parents, add z-scored parents, multiply ranked parents.
- [x] Child complexity scales with evolved parent complexity under a hard ceiling.
- [x] Near-identical grammar pairs are avoided when alternatives exist.
- [ ] Parent selection should use structural distance as an optimization signal, not only as a filter.  
  **Do:** score eligible pairs with `grammar_distance()` (including motif metadata when known) and choose/weight toward distant pairs deterministically.
- [ ] Persist child-derived `signal_family`; see Source metadata above.

## Novelty and ranking

Novelty outcomes:

```text
KEEP
DOWNWEIGHT
SKIP_REDUNDANT
```

- [x] Exact, current-skeleton, grammar, semantic, dataset, and motif novelty are available.
- [ ] Add explicit **category novelty**, **archive niche sparsity**, and numeric **parent→child grammar distance** to the pre-screen.  
  **Do:** pass parent structure metadata into `screen_novelty()`, query archive occupancy, and expose each component separately in the novelty report.
- [x] Ranking has bounded/capped correlated novelty inputs.
- [ ] `archive_sparsity` must measure actual archive niche occupancy rather than duplicate `grammar_novelty`.  
  **Do:** derive sparsity from archive-cell/member statistics or an equivalent persisted niche count.
- [ ] Clarify and test where `portfolio_diversification` applies.  
  **Do:** either include it in simulation ranking as documented or explicitly scope it to submission ranking and rename/document accordingly.

## Adaptive statistics

- [x] Motif outcome tables and bounded motif allocation exist.
- [ ] Source→target family transitions are not recorded correctly in generation statistics.  
  **Do:** derive source family from parent lineage and target family from the child profile; do not write both as the same family unconditionally.
- [ ] Concrete mutation operation statistics must use the actual edit (`parameters["operation"]`) rather than only broad `mutation_type`.
- [ ] Build bounded adaptive mutation allocation from those corrected statistics.

## Provenance

- [x] Candidate rows expose queryable V3 strategy/mode/motif/hash/profile fields.
- [x] `research_trials` records V3 decision metadata.
- [x] Historical rows are not rewritten.
- [ ] Novelty-skipped mutations/crossovers must retain complete lineage.  
  **Do:** extend `record_generation_decision()` and its caller to persist parents, generation, mutation type, and mutation parameters.

## Replay and promotion

- [x] V3 replay policies include grammar novelty, semantic novelty, archive V3, and mixed V3.
- [x] Replay leakage checks protect point-in-time decisions.
- [x] Replay reports efficiency and effective diversity metrics.
- [ ] Add the explicit **archive V2** comparison policy requested by the benchmark matrix, or revise the benchmark definition if another existing policy is the intentional baseline.

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
