# Generator V3 — Diversity-First Symbolic Search

**Repository:** `Uwater1/wq-alpha-research`  
**Target:** replace the current field-centric generator with a typed, diversity-aware symbolic search layer without weakening validation, deduplication, lineage, replay, or scheduler safety.

## 0. Executive summary

The current generator is no longer a simple single-template enumerator: it now rotates across six structural templates and uses structural changes during correlation repair. That is a useful improvement, but the search remains narrow relative to the available search space:

- 4,367 fields and 66 operators are available, but initial generation uses only a small set of hand-written motifs;
- each field receives one deterministic recipe for a fixed seed;
- window, decay, template, neutralization, truncation, and composition are not independently explored;
- multi-field expressions are mainly introduced only after failures;
- cross-dataset mutations can retain the parent's `signal_family`, corrupting diversity statistics;
- `canonical.skeleton()` masks numeric constants but not field identity, so "different field, same idea" can appear structurally diverse;
- archive parent selection and adaptive family allocation exist, but are not yet part of a closed generation loop.

Generator V3 should make **research-hypothesis diversity** a first-class search objective, not merely field coverage.

The required architecture is:

```text
campaign budget
    ↓
generation policy
    ├── explore new motif / niche
    ├── exploit proven motif on new data
    ├── mutate diverse archive elite
    └── crossover distant elites
    ↓
typed expression grammar
    ↓
semantic field selection
    ↓
recipe / settings sampling
    ↓
novelty + compatibility pre-screen
    ↓
existing ResearchDB queue boundary
    ↓
validation / dedup / staged search / scheduler
    ↓
simulation outcome
    ↓
archive + mutation/motif statistics
    ↺
```

Generator V3 must remain deterministic for a fixed campaign seed and must preserve the existing trust boundaries: generated candidates still enter through `ResearchDB.queue_candidate()` and are never allowed to bypass validation, deduplication, provenance, or trial accounting.

---

# 1. Goals

Generator V3 must:

1. increase **structural**, **semantic**, and **economic-source** diversity;
2. distinguish true hypothesis diversity from field-name variation;
3. make multiple independent recipes available per field;
4. use operator type metadata to generate valid expressions by construction;
5. connect the quality-diversity archive to parent selection;
6. consume adaptive family budgets during generation;
7. add multi-parent crossover;
8. learn which motifs and mutation operations actually work;
9. expose enough diagnostics to prove whether diversity is improving;
10. remain reproducible and auditable.

The generator is still a proposal engine, not a submission authority.

---

# 2. Non-goals

Do **not**:

- bypass `ResearchDB.queue_candidate()`;
- remove existing canonical deduplication;
- weaken strict deterministic compatibility checks;
- let an LLM directly submit arbitrary expressions;
- make the surrogate a hard gate;
- replace staged search or successive halving;
- equate archive membership with submission readiness;
- optimize only for raw candidate count;
- treat a larger number of field IDs as sufficient evidence of diversity;
- add random operator soup with no typed grammar or economic motif;
- require live BRAIN calls for generator unit tests.

---

# 3. Current problems to fix

## 3.1 Field diversity is much larger than structural diversity

Current base generation uses six `SignalTemplate` shapes:

```text
group_rank(ts_rank(...))
group_zscore(ts_mean(...))
group_neutralize(ts_zscore(...))
group_rank(ts_av_diff(...))
winsorize(zscore(ts_delta(...)))
rank(ts_decay_linear(...))
```

This is materially better than the previous single-template design, but it still covers only a small portion of the local operator catalogue.

The generator should not solve this by manually growing `SIGNAL_TEMPLATES` from 6 to 50. That becomes another brittle template catalogue.

**Required change:** replace the flat template list as the primary search mechanism with a typed motif/grammar system.

---

## 3.2 One field receives too few independent recipes

Today these choices are partly coupled to the same seed:

- template;
- lookback/window;
- decay.

Neutralization and truncation are not normal independently sampled recipe dimensions.

**Required change:** derive every recipe dimension from a candidate-specific deterministic RNG.

Recommended seed:

```text
recipe_seed =
    SHA256(
        campaign_id
        + global_seed
        + field_ids
        + motif_id
        + recipe_index
        + parent_ids
    )
```

The RNG must be local to one proposal so adding another proposal elsewhere cannot perturb already reproducible candidates.

---

## 3.3 Existing `skeleton_hash` overstates structural diversity

`canonical.skeleton()` masks numeric parameters but retains field names.

Therefore:

```text
group_rank(ts_rank(field_a, 20), subindustry)
group_rank(ts_rank(field_b, 60), subindustry)
```

are different skeletons even though they represent nearly the same structural hypothesis.

**Required change:** retain the existing skeleton for parameter-grid control, but introduce additional structure identities.

---

## 3.4 Archive diversity metadata is too coarse

`archive.niche()` currently uses:

- `signal_family`;
- a sorted operator set;
- approximate field category;
- depth;
- turnover bucket;
- mutation type;
- field count.

A sorted set of operators loses expression topology. Two different ASTs can have the same operator set.

**Required change:** use topology-preserving structural signatures and actual catalogue metadata.

---

## 3.5 Cross-dataset mutations can keep stale family labels

A child can replace or combine a field from another dataset while retaining the parent's `signal_family`.

This contaminates:

- family pass rates;
- family budget allocation;
- family diversity ranking;
- archive niches;
- field-category diagnostics.

**Required change:** derive family/source metadata from the child expression and catalogue, not blindly from the parent.

---

## 3.6 Archive and family allocation are advisory but disconnected

`scripts/archive.py` already provides:

- diversity-aware parent selection;
- niche elites;
- Beta-Bernoulli family allocation;
- exploration reserve.

But `CandidateGenerator.proposals()` does not consume these decisions.

**Required change:** add a generation-planning layer that turns archive parents and family allocations into actual proposal budgets.

---

# 4. Required architecture

Split V3 into three layers.

## 4.1 Layer A — typed expression grammar

New module:

```text
scripts/expression_grammar.py
```

Responsibilities:

- typed AST node definitions;
- operator signature lookup through `compatibility.py`;
- motif definitions;
- compatible child expansion;
- complexity limits;
- deterministic rendering to FASTEXPR;
- grammar/semantic signatures;
- structural distance helpers.

The grammar must not call BRAIN.

---

## 4.2 Layer B — generation policy

New module:

```text
scripts/generation_policy.py
```

Responsibilities:

- allocate campaign budget across generation modes;
- consume `archive.allocate_families()`;
- consume `archive.parents()`;
- choose motifs;
- choose fields/datasets;
- choose mutation/crossover operators;
- enforce exploration quotas;
- produce a deterministic generation plan.

The policy should return a plan, not queue candidates directly.

---

## 4.3 Layer C — proposal materialization

Keep:

```text
scripts/generator.py
```

as the public generator service.

Refactor it so it:

- accepts generation-plan items;
- materializes typed ASTs;
- renders expressions;
- records recipe metadata;
- routes every proposal through the existing queue boundary;
- keeps existing failure-directed repair as a supported mode.

Do not create a second independent queue path.

---

# 5. Typed expression grammar

## 5.1 AST nodes

Implement explicit nodes rather than constructing raw strings at every step.

Minimum model:

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

Optional:

```python
ExprNode = FieldNode | LiteralNode | CallNode
```

Every operator application must be checked against `compatibility.py` before the node is created.

The AST is the source of truth for generated candidates. Rendering to FASTEXPR happens only after a valid tree exists.

---

## 5.2 Complexity budget

Every generation request must support:

```text
max_depth
max_nodes
max_fields
max_binary_ops
```

Recommended defaults:

```text
max_depth      = 5
max_nodes      = 16
max_fields     = 2
max_binary_ops = 2
```

The defaults should prevent combinatorial explosion while allowing materially richer structures than V2.

Complexity limits are generator limits, not platform-validity assumptions.

---

# 6. Motif registry

A motif is an economically interpretable grammar recipe, not a full hard-coded expression.

Create a registry similar to:

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

Minimum V3 motifs:

## 6.1 Single-source motifs

```text
cross_sectional_level
time_series_level
momentum
mean_reversion
change
acceleration
smoothed_change
volatility_adjusted
group_relative
group_neutralized
ranked_level
```

## 6.2 Two-source motifs

```text
spread
ratio
difference_of_ranks
normalized_difference
confirming_signals
contrarian_pair
cross_dataset_composite
```

## 6.3 Event / expectation motifs

Where compatible fields exist:

```text
actual_vs_expectation
estimate_revision
event_decay
surprise_normalization
```

Do not hard-code unsupported semantics. Motif eligibility must depend on catalogue metadata and field compatibility.

---

# 7. Recipe dimensions

For every motif, choose recipe dimensions independently from a deterministic proposal RNG.

Minimum dimensions:

```text
lookback
smoothing_window
decay
neutralization
group_level
truncation
normalization
winsorization
rank_or_zscore
sign
```

Not every motif needs every dimension.

Candidate settings and expression parameters must be stored separately.

Example recipe metadata:

```json
{
  "motif_id": "momentum",
  "recipe_index": 3,
  "fields": ["field_a"],
  "datasets": ["fundamental2"],
  "lookback": 126,
  "smoothing_window": 10,
  "decay": 6,
  "neutralization": "SUBINDUSTRY",
  "truncation": 0.05,
  "normalization": "rank",
  "sign": 1
}
```

---

# 8. New structure identities

Keep existing:

```text
canonical_key
skeleton_hash
```

Add:

```text
grammar_skeleton
grammar_skeleton_hash
semantic_skeleton
semantic_skeleton_hash
```

## 8.1 `grammar_skeleton`

Purpose: detect "same expression topology, different concrete fields/settings".

It should:

- preserve AST topology;
- preserve operator names;
- mask numeric literals;
- replace field IDs with typed placeholders;
- optionally preserve group literals.

Example:

```text
group_rank(
    ts_rank(<FIELD:MATRIX>, #),
    <GROUP>
)
```

Two expressions using different MATRIX fields should share this hash if the operator tree is otherwise identical.

---

## 8.2 `semantic_skeleton`

Purpose: distinguish economically different source combinations while still ignoring exact field IDs.

Replace field IDs with:

```text
<dataset:category:type>
```

Example:

```text
subtract(
    rank(<analyst4:estimate:MATRIX>),
    rank(<fundamental2:earnings:MATRIX>)
)
```

This makes:

- exact-field diversity;
- grammar diversity;
- semantic-source diversity

separately measurable.

---

## 8.3 Existing `skeleton_hash`

Do not remove it.

Its current behavior remains useful for collapsing numeric parameter grids around the same exact fields.

---

# 9. Source metadata must be derived from the child

Add a helper:

```text
derive_source_profile(expression | AST, catalog)
```

Return at least:

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

- one dataset → `primary_family = dataset`;
- multiple datasets → stable composite family, for example:
  `multi:analyst4+fundamental2`;
- no recognized field → `unknown`;
- never inherit a parent's family without recomputing the child profile.

Use this profile for:

- `signal_family`;
- archive niche metadata;
- ranking diversity;
- family statistics;
- diagnostics.

---

# 10. Generation modes

Every generated proposal must record one of these modes.

## 10.1 `explore`

Goal: fill unoccupied or under-tested semantic/grammar niches.

Prefer:

- unseen motifs;
- unseen grammar skeletons;
- unseen dataset/category combinations;
- sparse archive cells;
- under-tested fields.

---

## 10.2 `exploit`

Goal: apply proven structures to new compatible sources.

Examples:

- proven motif + under-tested field;
- proven semantic skeleton + new dataset;
- proven motif + nearby parameter recipes.

Exploit must still obey family-share and duplicate controls.

---

## 10.3 `mutate`

Use existing failure-directed repair, but allow V3 operations.

Possible operations:

```text
field_swap
dataset_swap
template_change
motif_change
window_change
decay_change
normalization_change
neutralization_change
group_change
add_component
remove_component
subtree_replace
```

---

## 10.4 `crossover`

New.

Select two archive parents from sufficiently different niches and combine them through a typed composition.

Allowed initial crossover forms:

```text
add(rank(A), rank(B))
subtract(rank(A), rank(B))
add(zscore(A), zscore(B))
multiply(rank(A), rank(B))
```

Only generate a crossover when:

- both parent trees validate;
- resulting types are compatible;
- parents are not near-identical under `grammar_skeleton_hash`;
- complexity remains within budget.

Record both parent IDs in `parent_ids`.

---

# 11. Structural distance

Add a deterministic distance function:

```text
grammar_distance(A, B) -> [0, 1]
```

Minimum components:

```text
operator-tree distance
motif mismatch
dataset-set Jaccard distance
category-set Jaccard distance
field-count difference
depth difference
```

The first implementation does not need a sophisticated tree-edit-distance library.

A weighted normalized score is sufficient if deterministic and tested.

Use it for:

- crossover-parent selection;
- archive parent diversity;
- novelty scoring;
- diversity diagnostics.

---

# 12. Close the archive → generator loop

Introduce a campaign planner.

Example interface:

```python
plan = generation_policy.plan_campaign(
    db=db,
    catalog=catalog,
    campaign_id="campaign-42",
    budget=100,
    seed=7,
    mode="mixed",
)
```

The plan should contain explicit slots such as:

```json
{
  "slot": 17,
  "generation_mode": "explore",
  "family": "fundamental2",
  "motif_id": "normalized_difference",
  "parent_ids": [],
  "recipe_index": 2,
  "reason": "under-tested semantic niche"
}
```

For mutation/crossover slots, include selected parent IDs.

---

# 13. Mixed campaign budget

Support:

```text
--strategy coverage
--strategy explore
--strategy exploit
--strategy mutate
--strategy crossover
--strategy mixed
```

Recommended initial `mixed` defaults:

```text
explore   40%
exploit   25%
mutate    25%
crossover 10%
```

These are defaults only.

The allocation policy must be configurable and later learnable.

Family budgets from `archive.allocate_families()` should constrain all modes.

No family may exceed the configured maximum share unless the number of eligible families makes the cap mathematically impossible.

---

# 14. Adaptive motif and mutation allocation

Add empirical outcome tables.

Recommended logical tables:

```text
generation_operator_stats
motif_stats
```

Track at least:

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

Use the same principle as family allocation:

- under-tested actions receive bounded exploration;
- repeatedly poor actions receive less budget;
- successful actions receive more exploitation;
- no action receives 100% of the budget.

A Beta-Bernoulli or UCB policy is sufficient.

Do not let this become a hard rejection system.

---

# 15. Novelty pre-screen

Before queueing, compute proposal novelty against local research history.

Minimum novelty components:

```text
exact candidate novelty
current skeleton novelty
grammar skeleton novelty
semantic skeleton novelty
dataset/category novelty
archive niche sparsity
parent-child grammar distance
```

The pre-screen must not silently discard everything that is similar.

Support three outcomes:

```text
KEEP
DOWNWEIGHT
SKIP_REDUNDANT
```

`SKIP_REDUNDANT` should only be used for extremely redundant generated work where the generator intentionally requested novelty.

Every skipped proposal must still be recorded in the research trial ledger with its provenance and skip reason.

---

# 16. Ranking integration

Extend `scripts/ranking.py`.

Current novelty/family-diversity terms should become more explicit.

Recommended components:

```text
expected_quality
exact_novelty
grammar_novelty
semantic_novelty
information_gain
family_diversity
archive_sparsity
portfolio_diversification
failure_risk
duplicate_penalty
```

Do not simply sum multiple correlated novelty terms at full weight.

Normalize and cap the total novelty contribution.

Ranking remains advisory.

---

# 17. Archive V3 niche definition

Refactor `archive.niche()`.

Recommended dimensions:

```text
primary_family
dataset_set
category_set
motif_id
grammar_skeleton_hash
semantic_skeleton_hash
depth_bucket
field_count
cross_dataset
turnover_bucket
mutation_type
generation_mode
```

Do not use sorted operator names as the main structural identity.

The archive may still expose operator sets as descriptive metadata.

---

# 18. Database changes

Add candidate columns or equivalent queryable fields:

```text
generator_strategy
generation_mode
motif_id
recipe_id
grammar_skeleton_hash
semantic_skeleton_hash
source_profile_json
```

If schema bloat becomes undesirable, `source_profile_json` and recipe metadata can live in JSON, but the two new hashes and `motif_id` should be directly queryable.

Persist in every `research_trials` row:

```text
campaign_id
candidate_id
generation_mode
generator_version
motif_id
recipe metadata
parent_ids
grammar_skeleton_hash
semantic_skeleton_hash
source profile
decision / skip reason
```

Duplicate candidates across campaigns still create separate research-trial decisions.

---

# 19. Generator versioning

Set:

```text
GENERATOR_VERSION = "catalog-generator-v3"
```

Do not rewrite historical rows.

Any change that can materially alter proposal distribution should either:

- bump generator version; or
- persist an explicit policy/grammar version.

Recommended:

```text
GRAMMAR_VERSION
MOTIF_REGISTRY_VERSION
GENERATION_POLICY_VERSION
```

---

# 20. CLI changes

Keep existing commands working.

Existing:

```bash
python -m wq generate ...
python -m wq mutate ...
```

Add:

```bash
python -m wq generate \
  --campaign v3-test \
  --count 100 \
  --strategy mixed \
  --seed 7

python -m wq generate \
  --campaign explore-fundamental \
  --count 50 \
  --strategy explore \
  --family fundamental2

python -m wq generate \
  --campaign motif-test \
  --count 25 \
  --motif normalized_difference

python -m wq crossover \
  PARENT_A PARENT_B \
  --campaign crossover-test \
  --count 4

python -m wq generate \
  --campaign dry-plan \
  --count 100 \
  --strategy mixed \
  --dry-plan
```

`--dry-plan` must perform no BRAIN calls and queue nothing.

It should print the planned distribution by:

```text
generation mode
family
motif
dataset
grammar skeleton
semantic skeleton
```

---

# 21. Observability

Add a diversity report.

Suggested command:

```bash
python -m wq diversity-report --campaign CAMPAIGN_ID
```

Minimum metrics:

```text
trial count
unique exact candidates
unique fields
unique datasets
unique current skeleton hashes
unique grammar skeleton hashes
unique semantic skeleton hashes
unique motifs
effective motif count
effective dataset count
effective grammar count
cross-dataset share
duplicate rate
near-duplicate rate
median parent→child grammar distance
median pairwise PnL correlation when available
IS_PASS by motif
CORR_PASS by motif
ACTIVE by motif
IS_PASS by grammar niche
CORR_PASS by semantic niche
```

Use entropy-based effective counts:

```text
effective_count = exp(Shannon entropy)
```

This prevents "20 motifs exist but 95% of candidates use one motif" from looking diverse.

---

# 22. Policy replay integration

Extend `scripts/policy_replay.py` with V3-aware policies:

```text
grammar_novelty
semantic_novelty
mixed_v3
archive_v3
```

Replay must remain point-in-time safe.

At decision time, a V3 policy may only use:

- catalogue metadata available then;
- archived outcomes settled before the decision clock;
- motif/mutation statistics settled before the decision clock;
- ranking values recorded before the decision clock.

Never use later archive state or later motif success rates.

Extend leakage checks accordingly.

---

# 23. Implementation order

## Phase 1 — Correct diversity measurement

Implement first:

1. typed field/source profile helper;
2. `grammar_skeleton_hash`;
3. `semantic_skeleton_hash`;
4. correct child `signal_family`;
5. archive niche migration;
6. diversity report.

**Reason:** do not build a more complex generator before the project can correctly measure diversity.

Acceptance:

- same topology + different exact fields → same grammar hash;
- same topology + different datasets/categories → grammar hash may match, semantic hash differs;
- numeric-only parameter variants retain the same grammar and semantic hashes;
- cross-dataset children no longer inherit a stale single-family label.

---

## Phase 2 — Typed grammar foundation

Implement:

1. AST nodes;
2. operator signature integration;
3. rendering;
4. complexity budget;
5. motif registry;
6. deterministic recipe RNG.

Acceptance:

- generated AST cannot contain a known deterministic type incompatibility;
- rendering is deterministic;
- fixed campaign/seed/plan reproduces byte-identical expressions and metadata;
- adding unrelated proposals does not change existing recipe outputs.

---

## Phase 3 — Multi-recipe base generation

Replace V2's one-field/one-recipe behavior with:

```text
field × motif × recipe_index
```

under bounded policy control.

Acceptance:

- one eligible field can generate multiple distinct grammar-valid recipes;
- lookback/decay/template dimensions are no longer unintentionally phase-locked;
- neutralization/truncation can be explored where applicable;
- all proposals retain lineage and recipe provenance.

---

## Phase 4 — Archive-integrated campaign planning

Implement:

1. family budgets;
2. diverse archive-parent selection;
3. explore/exploit/mutate allocation;
4. `--dry-plan`.

Acceptance:

- generated campaign counts match the planned budget exactly;
- max-family-share is respected;
- sparse/untested families receive bounded exploration;
- repeated runs with same inputs are deterministic.

---

## Phase 5 — Crossover

Implement:

1. grammar-distance metric;
2. distant-parent selection;
3. two-parent AST composition;
4. complexity/type checks;
5. two-parent lineage.

Acceptance:

- near-identical parents are not selected for diversity crossover by default;
- crossover produces valid typed ASTs;
- both parent IDs are persisted;
- resulting source profile reflects all child fields.

---

## Phase 6 — Adaptive motif/mutation allocation

Implement:

1. motif outcome counters;
2. mutation-operation outcome counters;
3. bounded Thompson/UCB allocation;
4. replay-visible policy metadata.

Acceptance:

- untested operations receive exploration;
- no single operation can monopolize the campaign;
- policy decisions are reproducible from persisted pre-decision evidence.

---

## Phase 7 — Replay and calibration

Add V3 policies to offline replay before changing live defaults.

Compare at equal simulation budget:

```text
V2 ranking
coverage
archive V2
grammar novelty
semantic novelty
mixed V3
```

Primary offline metrics:

```text
simulations to first IS_PASS
IS_PASS per simulation
CORR_PASS per simulation
top-k recall
wasted near-duplicate variants
effective grammar diversity
effective semantic diversity
effective family diversity
robustness-adjusted quality
```

Do not promote V3 merely because it generates more unique formulas.

---

# 24. Test plan

## 24.1 Unit tests

New files:

```text
tests/test_expression_grammar.py
tests/test_generation_policy.py
tests/test_generator_v3.py
tests/test_diversity_metrics.py
```

Test:

- AST type checking;
- motif eligibility;
- deterministic rendering;
- recipe independence;
- source-profile derivation;
- grammar hash behavior;
- semantic hash behavior;
- structural distance;
- family relabeling;
- archive niche identity;
- family budget conservation;
- crossover lineage;
- complexity limits;
- deterministic dry plans.

---

## 24.2 Property tests

For large dry-run samples:

```text
all generated candidates satisfy local deterministic type rules
no AST exceeds configured complexity
same seed reproduces same proposals
different recipe_index usually changes at least one recipe dimension
every proposal has complete provenance
every multi-field proposal reports all source datasets
```

Do not assert profitability.

---

## 24.3 Regression tests

Preserve:

- existing `canonical_key`;
- exact duplicate deduplication;
- trial-ledger semantics;
- queue safety;
- candidate generation CLI;
- failure-directed mutation;
- staged search;
- successive halving;
- scheduler behavior;
- policy-replay leakage invariants.

Existing V2 templates may remain available as motifs or compatibility aliases so old campaigns remain reproducible.

---

# 25. Acceptance criteria for V3

Generator V3 is complete when all of the following are true:

### Correctness

- all generated candidates still enter through the existing queue boundary;
- deterministic type-invalid generated ASTs are blocked locally;
- cross-dataset children have correct source/family metadata;
- every proposal has campaign, policy, motif, recipe, lineage, and version provenance.

### Diversity measurement

- exact, skeleton, grammar, and semantic diversity are separately measurable;
- archive niches preserve expression topology;
- campaign reports expose entropy/effective-count metrics.

### Search behavior

- base generation supports multiple independent recipes per field;
- multiple motifs are available beyond the original six templates;
- multi-field motifs exist before failure repair;
- archive parents influence actual generation;
- family allocation constrains actual generation;
- crossover is available;
- novelty can affect generation and ranking.

### Reproducibility

- same DB snapshot + catalogue + versions + seed + campaign plan produces identical proposal order and metadata;
- historical V2 candidates are untouched.

### Evidence

- V3 can be compared against V2 using point-in-time offline replay;
- live default policy is not changed until replay results justify it.

---

# 26. Recommended initial rollout policy

After implementation, do **not** immediately make V3 fully exploitative.

Use:

```text
40% explore
25% exploit
25% mutate
10% crossover
```

with:

```text
max_family_share = 0.35 to 0.50
```

and a conservative complexity budget.

Run V2 and V3 side-by-side in policy replay and in small explicitly named campaigns.

Promotion criterion should be better **simulation efficiency plus maintained/improved diversity**, not raw candidate count.

---

# 27. Files expected to change

Primary:

```text
scripts/generator.py
scripts/canonical.py
scripts/archive.py
scripts/ranking.py
scripts/research_db.py
scripts/policy_replay.py
wq/__main__.py
```

New:

```text
scripts/expression_grammar.py
scripts/generation_policy.py
scripts/diversity.py
tests/test_expression_grammar.py
tests/test_generation_policy.py
tests/test_generator_v3.py
tests/test_diversity_metrics.py
```

Likely updates:

```text
README.md
SKILL.md
TODO.md
tests/test_archive.py
tests/test_policy_replay.py
tests/test_generator.py
tests/test_research_db.py
```

Keep documentation changes concise. `AGENTS.md` should only change if an agent-facing invariant or workflow genuinely changes.

---

# 28. Agent implementation constraints

Any coding agent implementing this document must follow these rules:

1. inspect existing schema/migrations before adding columns;
2. reuse `compatibility.py` rather than creating a second operator type system;
3. reuse existing `ResearchDB.queue_candidate()`;
4. preserve the permanent `research_trials` ledger;
5. never derive research-family metadata solely from the parent after a field change;
6. never use current/future outcomes in point-in-time replay;
7. keep all random behavior seedable and deterministic;
8. add tests before changing the default live generation policy;
9. avoid one giant rewrite—land phases independently;
10. preserve backward compatibility for V2 campaign history.

---

# 29. Expected end state

V2:

```text
under-tested field
    ↓
one of a few templates
    ↓
simulate
    ↓
local repair
```

V3:

```text
campaign objective + budget
        ↓
quality-diversity generation policy
        ↓
explore / exploit / mutate / crossover
        ↓
typed economic motif
        ↓
semantic field selection
        ↓
independent deterministic recipe
        ↓
grammar + semantic novelty accounting
        ↓
existing queue / validation / staged search
        ↓
simulation
        ↓
archive + empirical motif/mutation learning
        ↺
```

The central design principle is:

> **The miner should search over hypotheses, not merely over field names.**

A successful V3 does not just produce more expressions. It spends scarce BRAIN simulations on a wider set of structurally and economically distinct, auditable research hypotheses while preserving the project's existing safety and evidence infrastructure.
