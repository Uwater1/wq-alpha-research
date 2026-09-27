# Generator V3 TODO

**Goal:** replace the current field-centric generator with a diversity-first symbolic search system while preserving validation, dedup, lineage, replay, staged search, and scheduler behavior.

**Main problem:** current generation has strong field coverage but limited **structural / hypothesis diversity**. V3 should search over motifs, structures, datasets, and multi-field combinations — not just field names.

> **Status (P0–P15 landed).** Diversity measurement, the typed grammar + motif registry, independent
> recipe sampling, the generation-policy modes, the archive → generator campaign planner, crossover,
> novelty screening, ranking/adaptive-allocation integration, provenance, the V3 CLI, the diversity
> report, and V3-aware policy replay are implemented and covered by offline tests
> (`tests/test_expression_grammar.py`, `tests/test_diversity_metrics.py`,
> `tests/test_generation_policy.py`, `tests/test_generator_v3.py`, `tests/test_policy_replay.py`).
> The live default generator is **unchanged**: the V2 template path still runs unless
> `--strategy` / `--motif` / `--dry-plan` is supplied, and V3 rows carry
> `GENERATOR_VERSION_V3 = "catalog-generator-v3"`. **P16 promotion and P17 rollout gates remain
> open** — they need live V3 campaign evidence, not more code.
>
> Naming note: the P10 field `recipe_id` is realized as `recipe_index` (the per-slot recipe
> ordinal) plus a `recipe` JSON blob holding every sampled dimension, which is what the replay
> and report paths actually read.

---

# P0 — Fix diversity measurement first

## P0.1 Source profile

- [x] Add `derive_source_profile(expression | AST, catalog)`.
- [x] Return `field_ids`, `datasets`, `categories`, `field_types`, `primary_family`, `cross_dataset`.
- [x] One dataset → `primary_family = dataset`.
- [x] Multiple datasets → stable composite family, e.g. `multi:analyst4+fundamental2`.
- [x] Never inherit `signal_family` from the parent after field changes.

**Acceptance**
- [x] Cross-dataset mutations no longer retain stale parent family labels.
- [x] Archive/ranking/family stats use child-derived source metadata.

## P0.2 Structural diversity hashes

Keep:
- [x] `canonical_key`
- [x] `skeleton_hash`

Add:
- [x] `grammar_skeleton`
- [x] `grammar_skeleton_hash`
- [x] `semantic_skeleton`
- [x] `semantic_skeleton_hash`

`grammar_skeleton` masks exact fields + numbers, but preserves operator topology and field type:

```text
group_rank(ts_rank(<FIELD:MATRIX>, #), <GROUP>)
```

`semantic_skeleton` replaces fields with:

```text
<dataset:category:type>
```

**Acceptance**
- [x] Same topology + different fields → same grammar hash.
- [x] Same topology + different datasets → different semantic hash.
- [x] Numeric-only variants → same grammar + semantic hashes.
- [x] Existing `skeleton_hash` behavior remains unchanged.

## P0.3 Archive niche identity

Update `scripts/archive.py` to use:

- [x] `primary_family`
- [x] `dataset_set`
- [x] `category_set`
- [x] `motif_id`
- [x] `grammar_skeleton_hash`
- [x] `semantic_skeleton_hash`
- [x] `depth_bucket`
- [x] `field_count`
- [x] `cross_dataset`
- [x] `turnover_bucket`
- [x] `mutation_type`
- [x] `generation_mode`

Keep operator sets only as descriptive metadata.

---

# P1 — Typed expression grammar

Create:

```text
scripts/expression_grammar.py
```

## P1.1 AST

- [x] Add `FieldNode`.
- [x] Add `LiteralNode`.
- [x] Add `CallNode`.
- [x] Add `ExprNode`.

Suggested model:

```python
@dataclass(frozen=True)
class FieldNode:
    field_id: str
    value_type: str
    dataset: str
    category: str

@dataclass(frozen=True)
class LiteralNode:
    value: int | float | str
    literal_type: str

@dataclass(frozen=True)
class CallNode:
    operator: str
    args: tuple["ExprNode", ...]
    output_type: str
```

## P1.2 Compatibility

- [x] Reuse `scripts/compatibility.py`.
- [x] Do not create a second operator type system.
- [x] Validate operator arity/type before creating AST nodes.
- [x] Render FASTEXPR only after AST is valid.

## P1.3 Complexity limits

Support:
- [x] `max_depth`
- [x] `max_nodes`
- [x] `max_fields`
- [x] `max_binary_ops`

Recommended defaults:

```text
max_depth      = 5
max_nodes      = 16
max_fields     = 2
max_binary_ops = 2
```

**Acceptance**
- [x] Generated ASTs cannot contain known deterministic type incompatibilities.
- [x] No BRAIN calls are required for grammar tests.
- [x] Every candidate respects the complexity budget.

---

# P2 — Motif registry

Do **not** grow `SIGNAL_TEMPLATES` into a large manual list.

## P2.1 Motif model

- [x] Add `Motif` dataclass.

```python
@dataclass(frozen=True)
class Motif:
    id: str
    description: str
    input_roles: tuple[str, ...]
    allowed_field_types: tuple[str, ...]
    builder: Callable[..., ExprNode]
    tags: tuple[str, ...]
```

## P2.2 Single-source motifs

- [x] `cross_sectional_level`
- [x] `time_series_level`
- [x] `momentum`
- [x] `mean_reversion`
- [x] `change`
- [x] `acceleration`
- [x] `smoothed_change`
- [x] `volatility_adjusted`
- [x] `group_relative`
- [x] `group_neutralized`
- [x] `ranked_level`

## P2.3 Two-source motifs

- [x] `spread`
- [x] `ratio`
- [x] `difference_of_ranks`
- [x] `normalized_difference`
- [x] `confirming_signals`
- [x] `contrarian_pair`
- [x] `cross_dataset_composite`

## P2.4 Event / expectation motifs

Only where metadata supports them:

- [x] `actual_vs_expectation`
- [x] `estimate_revision`
- [x] `event_decay`
- [x] `surprise_normalization`

**Acceptance**
- [x] Motif eligibility is metadata/type driven.
- [x] Unsupported semantic combinations are not generated.
- [x] V2 templates may remain as compatibility motifs.

---

# P3 — Independent recipe generation

## P3.1 Per-proposal deterministic RNG

Use:

```text
SHA256(
    campaign_id
    + global_seed
    + field_ids
    + motif_id
    + recipe_index
    + parent_ids
)
```

- [x] Each proposal gets its own RNG.
- [x] Adding unrelated proposals does not perturb existing outputs.

## P3.2 Recipe dimensions

Support independent sampling of:

- [x] `lookback`
- [x] `smoothing_window`
- [x] `decay`
- [x] `neutralization`
- [x] `group_level`
- [x] `truncation`
- [x] `normalization`
- [x] `winsorization`
- [x] `rank_or_zscore`
- [x] `sign`

## P3.3 Persist recipe metadata

- [x] `motif_id`
- [x] `recipe_index`
- [x] fields
- [x] datasets
- [x] sampled parameters
- [x] settings
- [x] generator/policy versions

**Acceptance**
- [x] One field can generate multiple valid recipes.
- [x] Template/window/decay are no longer phase-locked.
- [x] Fixed seed + campaign + recipe reproduces identical output.

---

# P4 — Generation strategy layer

Create:

```text
scripts/generation_policy.py
```

## P4.1 Modes

- [x] `explore`
- [x] `exploit`
- [x] `mutate`
- [x] `crossover`
- [x] `mixed`

Every proposal records `generation_mode`.

## P4.2 Explore

Prefer:

- [x] unseen motifs
- [x] unseen grammar skeletons
- [x] unseen semantic skeletons
- [x] under-tested datasets/categories
- [x] sparse archive niches
- [x] under-tested fields

## P4.3 Exploit

Use:

- [x] proven motif + new field
- [x] proven motif + new dataset
- [x] proven semantic structure + nearby recipe
- [x] proven family + new structural realization

## P4.4 Mutate

Keep existing failure-directed repair and add:

- [x] `field_swap`
- [x] `dataset_swap`
- [x] `template_change`
- [x] `motif_change`
- [x] `window_change`
- [x] `decay_change`
- [x] `normalization_change`
- [x] `neutralization_change`
- [x] `group_change`
- [x] `add_component`
- [x] `remove_component`
- [x] `subtree_replace`

---

# P5 — Connect archive to generator

## P5.1 Campaign planner

Add:

```python
plan_campaign(
    db,
    catalog,
    campaign_id,
    budget,
    seed,
    mode,
)
```

Each slot records:

```text
slot
generation_mode
family
motif_id
parent_ids
recipe_index
reason
```

## P5.2 Family allocation

- [x] Use `archive.allocate_families()`.
- [x] Enforce `max_family_share`.
- [x] Reserve budget for under-tested families.

## P5.3 Parent selection

- [x] Use `archive.parents()`.
- [x] Select parents across families/niches.
- [x] Avoid repeatedly mutating one top-Sharpe lineage.

**Acceptance**
- [x] Planned budget exactly equals requested budget.
- [x] Family caps are respected.
- [x] Same DB snapshot + seed → same plan.
- [x] Archive decisions affect real generation.

---

# P6 — Crossover

## P6.1 Structural distance

Add:

```text
grammar_distance(A, B) -> [0, 1]
```

Use:

- [x] operator-tree difference
- [x] motif mismatch
- [x] dataset-set Jaccard distance
- [x] category-set Jaccard distance
- [x] field-count difference
- [x] depth difference

## P6.2 Parent selection

- [x] Select parents from distant archive niches.
- [x] Reject near-identical pairs by default.

## P6.3 Initial crossover forms

- [x] `add(rank(A), rank(B))`
- [x] `subtract(rank(A), rank(B))`
- [x] `add(zscore(A), zscore(B))`
- [x] `multiply(rank(A), rank(B))`

Only when type/complexity constraints pass.

## P6.4 Lineage

- [x] Persist both parent IDs.
- [x] Derive child source profile from the final AST.

---

# P7 — Novelty-aware generation

Calculate:

- [x] exact-candidate novelty
- [x] current-skeleton novelty
- [x] grammar-skeleton novelty
- [x] semantic-skeleton novelty
- [x] dataset/category novelty
- [x] archive niche sparsity
- [x] parent-child grammar distance

Support:

```text
KEEP
DOWNWEIGHT
SKIP_REDUNDANT
```

- [x] Record skipped proposals in `research_trials`.
- [x] Preserve provenance and skip reason.

---

# P8 — Ranking integration

Update `scripts/ranking.py`.

Add:

- [x] `exact_novelty`
- [x] `grammar_novelty`
- [x] `semantic_novelty`
- [x] `information_gain`
- [x] `family_diversity`
- [x] `archive_sparsity`
- [x] `portfolio_diversification`
- [x] `failure_risk`
- [x] `duplicate_penalty`

- [x] Normalize/cap correlated novelty terms.
- [x] Keep ranking advisory.

---

# P9 — Adaptive motif / mutation allocation

Track:

```text
scope_hash
generator_version
generation_mode
motif_id
mutation_operation
source_family
target_family
attempts
validated
simulated
is_pass
corr_pass
active
updated_at
```

Use bounded:

- [x] Thompson sampling, or
- [x] UCB

Rules:

- [x] under-tested actions get exploration
- [x] poor actions lose budget gradually
- [x] successful actions gain exploitation budget
- [x] no action may monopolize the campaign

---

# P10 — Database / provenance

Add candidate fields:

- [x] `generator_strategy`
- [x] `generation_mode`
- [x] `motif_id`
- [x] `recipe_id`
- [x] `grammar_skeleton_hash`
- [x] `semantic_skeleton_hash`
- [x] `source_profile_json`

Persist in `research_trials`:

- [x] campaign ID
- [x] candidate ID
- [x] generation mode
- [x] generator version
- [x] motif ID
- [x] recipe metadata
- [x] parent IDs
- [x] grammar skeleton hash
- [x] semantic skeleton hash
- [x] source profile
- [x] keep/downweight/skip decision
- [x] reason

- [x] Do not rewrite historical rows.

---

# P11 — Versioning

Set:

```text
GENERATOR_VERSION = "catalog-generator-v3"
```

Add:

- [x] `GRAMMAR_VERSION`
- [x] `MOTIF_REGISTRY_VERSION`
- [x] `GENERATION_POLICY_VERSION`

---

# P12 — CLI

Keep:

- [x] `python -m wq generate`
- [x] `python -m wq mutate`

Add:

```bash
python -m wq generate --campaign v3-test --count 100 --strategy mixed --seed 7
python -m wq generate --campaign explore-fundamental --count 50 --strategy explore --family fundamental2
python -m wq generate --campaign motif-test --count 25 --motif normalized_difference
python -m wq crossover PARENT_A PARENT_B --campaign crossover-test --count 4
python -m wq generate --campaign dry-plan --count 100 --strategy mixed --dry-plan
```

`--dry-plan`:

- [x] no BRAIN calls
- [x] no queue writes
- [x] print mode/family/motif/dataset/grammar/semantic distribution

---

# P13 — Diversity report

Add:

```bash
python -m wq diversity-report --campaign CAMPAIGN_ID
```

Report:

- [x] trial count
- [x] unique exact candidates
- [x] unique fields
- [x] unique datasets
- [x] unique current skeleton hashes
- [x] unique grammar skeleton hashes
- [x] unique semantic skeleton hashes
- [x] unique motifs
- [x] effective motif count
- [x] effective dataset count
- [x] effective grammar count
- [x] cross-dataset share
- [x] duplicate rate
- [x] near-duplicate rate
- [x] median parent→child grammar distance
- [x] median pairwise PnL correlation when available
- [x] IS_PASS by motif
- [x] CORR_PASS by motif
- [x] ACTIVE by motif

Use:

```text
effective_count = exp(Shannon entropy)
```

---

# P14 — Policy replay

Extend `scripts/policy_replay.py`.

Add:

- [x] `grammar_novelty`
- [x] `semantic_novelty`
- [x] `archive_v3`
- [x] `mixed_v3`

Point-in-time rules:

- [x] only outcomes settled before decision clock
- [x] only prior archive state
- [x] only prior motif/mutation stats
- [x] only prior ranking outputs
- [x] extend leakage checks for V3 features

Compare at equal simulation budget:

```text
V2 ranking
coverage
archive V2
grammar novelty
semantic novelty
mixed V3
```

Metrics:

- [x] simulations to first IS_PASS
- [x] IS_PASS / simulation
- [x] CORR_PASS / simulation
- [x] top-k recall
- [x] wasted near-duplicate variants
- [x] effective grammar diversity
- [x] effective semantic diversity
- [x] effective family diversity
- [x] robustness-adjusted quality

---

# P15 — Tests

Add:

```text
tests/test_expression_grammar.py
tests/test_generation_policy.py
tests/test_generator_v3.py
tests/test_diversity_metrics.py
```

## Unit tests

- [x] AST type checking
- [x] motif eligibility
- [x] deterministic rendering
- [x] recipe independence
- [x] source-profile derivation
- [x] grammar hash behavior
- [x] semantic hash behavior
- [x] structural distance
- [x] family relabeling
- [x] archive niche identity
- [x] family budget conservation
- [x] crossover lineage
- [x] complexity limits
- [x] deterministic dry plans

## Regression tests

Preserve:

- [x] `canonical_key`
- [x] exact duplicate deduplication
- [x] research-trial ledger
- [x] queue safety
- [x] V2 generation CLI
- [x] failure-directed mutation
- [x] staged search
- [x] successive halving
- [x] scheduler behavior
- [x] policy-replay leakage invariants

---

# P16 — Rollout order

Implement in this order:

```text
P0  diversity measurement
P1  typed grammar
P2  motif registry
P3  multi-recipe generation
P4  generation modes
P5  archive → generator integration
P6  crossover
P7  novelty screening
P8  ranking integration
P9  adaptive motif allocation
P10 DB/provenance
P11 versioning
P12 CLI
P13 diversity report
P14 policy replay
P15 tests / regression
```

Development loop:

```text
implement
→ unit tests
→ regression tests
→ dry-run
→ inspect provenance
→ commit
```

- [ ] Do not change the live default generator until replay evidence exists.

---

# P17 — Initial V3 defaults

Start with:

```text
explore   40%
exploit   25%
mutate    25%
crossover 10%
```

Recommended:

```text
max_family_share = 0.35–0.50
```

Promote V3 only if it improves:

- [ ] simulation efficiency
- [ ] grammar diversity
- [ ] semantic diversity
- [ ] correlation diversity

without materially degrading:

- [ ] IS_PASS rate
- [ ] CORR_PASS rate
- [ ] robustness-adjusted quality

---

# Final acceptance

Generator V3 is complete when:

- [x] Child source/family metadata is always correct.
- [x] Exact, parameter, grammar, and semantic diversity are separately measurable.
- [x] Archive niches preserve AST topology.
- [x] One field can generate multiple independent recipes.
- [x] Multiple economic motifs exist beyond the original six templates.
- [x] Multi-field motifs can be generated before failure repair.
- [x] Archive parent selection affects real generation.
- [x] Family allocation affects real generation.
- [x] Crossover works with two-parent lineage.
- [x] Novelty affects generation/ranking.
- [x] Every proposal remains auditable.
- [x] Same DB snapshot + seed + versions reproduces the same campaign plan.
- [x] V3 can be evaluated against V2 with point-in-time-safe replay.
- [x] Live default remains unchanged until replay shows improvement.

> **Core rule: search over hypotheses, not merely field names.**
