# TODO — Autonomous Alpha Research

Last redesigned: 2026-09-21.

Goal: evolve `wq-alpha-research` from a reliable simulation/submission/learning pipeline into an autonomous research system that:

- decides what experiments to run next;
- spends scarce BRAIN simulations efficiently;
- preserves diverse research directions;
- learns from failures and successful mutations;
- discounts results caused by large adaptive searches;
- remains restart-safe, private, auditable, and agent-agnostic.

The existing queue, scheduler, correlation, submission recovery, knowledge store, skill manager, surrogate, observability, and offline regression foundation should be preserved.

---

## P0 — Reconcile docs and close the previous roadmap

**Status: docs implemented; issue #1 reconciliation recorded, not closed.**

Clean up artifacts left by the completed roadmap before adding new architecture.

### Issue #1 reconciliation (audited 2026-09-22)

Issue [#1](https://github.com/Uwater1/wq-alpha-research/issues/1) is still open. Its
explicit acceptance criteria are now met except one, which is the only item that blocks
closing it:

- met: `AGENTS.md` + compact `SKILL.md` are the entry points; raw experiments live in
  `research.db`; learned rules carry scope/evidence/provenance/lifecycle; contradiction
  can weaken/retire a rule; skill mutations are atomic, auditable and rollbackable;
  concurrent writers cannot overwrite a stale skill; user-owned/pinned guidance is
  protected; private data cannot be compiled into tracked files; the loop is CLI/SQLite
  based with no agent-specific runtime;
- met: "historical simulation cache can evaluate at least some proposed policy/skill
  changes offline" — `scripts/policy_replay.py` replays selection policies and the
  calibration evidence against local history with no BRAIN calls (P5). Closing #1 now
  depends only on the CLI-surface decision below.

It also lists P12 implementation items that are outside the acceptance criteria and are
still open, tracked here rather than in #1:

- the universal `python -m wq ...` CLI surface (P12G/P12M: `wq status`, `wq candidate`,
  `wq recall`, `wq knowledge`, `wq learn`, `wq skill`); today `python -m wq` exposes the
  generator only and the equivalent work lives in `scripts/*_cli.py`;
- agent/model telemetry tables (`agent_runs`, `skill_versions`, `skill_usage`,
  `eval_runs`); `skill_mutations` currently covers the mutation ledger only.

Closing #1 therefore depends on the CLI-surface decision above. This issue is the
consolidation tracker for that work.

Clean up artifacts left by the completed roadmap before adding new architecture.

### Documentation

Update `README.md` to match the canonical learning flow:

```text
research events
→ observations
→ evidence
→ evaluated rules
→ skill manager
→ SKILL.md
```

Remove/deprecate instructions that present:

```text
evolve_skill.py --apply
alpha_db.json
direct SKILL.md lesson appends
```

as the normal self-evolution path.

Fix stale section numbering/pointers in `SKILL.md`, `AGENTS.md`, and README.

### Issue cleanup

Audit issue #1 against current `main`.

If its acceptance criteria are now satisfied:

- record any genuine remaining gaps in this TODO;
- close #1.

### Acceptance

- README, `AGENTS.md`, `SKILL.md`, CLI behavior, and implementation agree;
- no documented workflow recommends disabled legacy mutation paths;
- previous roadmap issues contain no hidden unfinished work.

---

# P1 — Autonomous candidate generation and targeted mutation

**Status: implemented in the current working tree** (`scripts/generator.py`,
generator version `catalog-generator-v2`); hardened by issue #7.

This is the main missing capability.

Today the project can efficiently process candidates once expressions already exist. Add a native research layer that decides what should be generated next.

Target loop:

```text
research state
+ recalled knowledge
+ field/operator catalog
+ previous outcomes
        ↓
research planner
        ↓
parent / niche selection
        ↓
candidate generation or mutation
        ↓
static validation
        ↓
dedup / lineage
        ↓
research.db queue
        ↓
existing scheduler
```

## P1.1 — Candidate generator

Create a generator service with stable Python/CLI interfaces.

Example:

```bash
python -m wq generate \
  --campaign <id> \
  --count 50 \
  --family fundamental
```

Generation should support:

- deterministic templates;
- parameterized structural templates;
- mutations from existing candidates;
- optional agent/LLM proposals;
- combinations of compatible fields/operators;
- knowledge-retrieval context.

LLMs must not bypass validation, dedup, privacy, or lineage tracking.

## P1.2 — Explicit mutation operators

Represent mutations as structured operations rather than opaque new expressions.

Examples:

```text
field_swap
dataset_swap
window_change
decay_change
neutralization_change
operator_change
add_smoothing
add_group_transform
combine_signals
remove_component
replace_component
turnover_repair
correlation_repair
```

Every generated child should record:

```text
parent_id / parent_ids
generation
mutation_type
mutation_parameters
generator_version
reason
campaign_id
```

## P1.3 — Failure-directed repair

Generate mutations from diagnosed failure modes.

Examples:

```text
high turnover
→ decay / hump / smoothing / slower data

low Sharpe
→ field, dataset, or structural change

low Fitness
→ identify Sharpe-vs-turnover bottleneck

self-correlation
→ change economic source or combine orthogonal signals

weight concentration
→ grouping / transform / truncation repair

sub-universe failure
→ improve robustness / grouping / signal breadth
```

Do not treat parameter perturbation as sufficient diversification.

## P1.4 — Generator safety

Generated candidates must pass through existing:

- canonicalization;
- static field/operator validation;
- settings validation;
- duplicate detection;
- near-duplicate detection;
- privacy rules.

### Acceptance

Implemented by `scripts/generator.py` and `python -m wq generate|mutate`. The generator uses the supplied catalog (4,367 fields / 14 dataset families), deterministic seeds, type-compatible templates, explicit mutation metadata, and the existing queue safety boundary.

Mutation dispatch covers the failure modes this section requires: `HIGH_TURNOVER`,
`LOW_TURNOVER`, `LOW_SHARPE`, `LOW_FITNESS`, `CONCENTRATED_WEIGHT`,
`LOW_SUB_UNIVERSE_SHARPE`, and `SELF_CORRELATION`/`CORR_FAIL`. Each child records the
repair `mutation_type` plus `parameters["operation"]` (hump smoothing, window change,
decay change, neutralization change, group transform, rank normalization, truncation,
field swap, signal combination, component removal), so the concrete edit is auditable
even though the decision is labelled by the failure it answers. Coverage-aware ordering
is a primary sort on attempts with a seeded tie-break inside one attempts bucket, and
generation is `max(parent generations) + 1`, so descendants can no longer stay at
generation 1.

Research basis: AlphaAgent (arXiv:2502.16789), Human-AI Interactive Alpha Mining / Alpha-GPT (arXiv:2308.00016), and constrained MCTS formulaic-factor mining (arXiv:2505.11122). These support structured operand/operator metadata, regularized exploration, and auditable search; they do not justify treating a generated backtest as independent evidence.

- the project can generate and queue new candidates without a manually prepared CSV;
- candidate lineage is complete;
- failed candidates can produce targeted children;
- generation is reproducible when given the same campaign state and seed;
- agent-generated expressions use the same service layer as deterministic generators.

---

# P2 — Quality-diversity archive and adaptive research allocation

**Status: implemented in the current working tree** (`scripts/archive.py`); hardened by
issue #7.

Avoid converging the whole search onto one temporarily successful family.

## P2.1 — Elite archive

Maintain a local archive of strong candidates across meaningful niches.

Potential niche dimensions:

```text
dataset family
economic signal family
operator skeleton
field category
complexity/depth bucket
turnover bucket
neutralization
generation/mutation family
ACTIVE-PnL correlation cluster
```

Each niche should retain one or a small number of elites based on configurable objectives.

Example:

```text
archive cell:
  family = analyst_expectation
  turnover = low
  correlation_cluster = 4

elite:
  candidate_id = ...
  sharpe = ...
  fitness = ...
  turnover = ...
  self_corr = ...
```

Archive membership is not equivalent to submission readiness.

## P2.2 — Diversity-aware parent selection

Candidate generation should sample parents from the archive rather than only from the highest-Sharpe candidate.

Balance:

```text
quality
novelty
uncertainty
family diversity
portfolio diversification
under-explored niches
```

## P2.3 — Adaptive family budgets

Replace fixed search budgets as the long-term default.

Current staged-search constants remain safe fallbacks.

Add an adaptive allocator using a simple interpretable method first, such as:

```text
Beta-Bernoulli Thompson sampling
or
UCB-style family allocation
```

Possible rewards:

```text
IS_PASS
CORR_PASS
ACTIVE
quality-adjusted reward
information gained
```

Keep reward definitions versioned.

## P2.4 — Exploration reserve

Never spend the entire simulation budget exploiting known families.

A campaign should reserve configurable capacity for:

```text
new fields
new datasets
new structures
new mutation families
high-uncertainty candidates
```

### Acceptance

Implemented by `scripts/archive.py`: persisted archive cells, deterministic cross-niche parent selection, seeded Beta-Bernoulli allocation, reward-version metadata, and an exploration reserve. Allocation is advisory until a caller explicitly consumes the returned plan.

Allocation now conserves the requested budget exactly (`sum(budgets) == budget`) for every
input, seats only a bounded subset of families when the budget is smaller than the family
count, honours a configurable `max_family_share` so one family cannot consume the campaign,
and hands out slots with sequential Thompson draws. Parent selection scores quality together
with niche sparsity and lineage depth and then rotates across families and niches instead of
taking the global top-elite list.

- one successful family cannot monopolize the research budget;
- unexplored but promising niches continue receiving bounded exploration;
- family allocation adapts from empirical outcomes;
- deterministic staged-search remains available as fallback.

---

# P3 — Search-aware statistical robustness

**Status: implemented in the current working tree** (`scripts/robustness.py`); hardened by
issue #7.

A large autonomous search can discover impressive results by chance.

The system must account for the research process, not only final winners.

## P3.1 — Permanent trial ledger

Every generated candidate must belong to a campaign/trial history.

Track:

```text
campaign_id
candidate_id
parent lineage
generation mechanism
independence/family group
creation order
validation result
simulation result
correlation result
submission result
final lifecycle state
```

Do not delete failed trials from statistical accounting.

## P3.2 — Multiple-testing diagnostics

Where sufficient data exists, add advisory metrics such as:

- effective number of independent trials;
- Probabilistic Sharpe Ratio;
- Deflated Sharpe Ratio;
- Probability of Backtest Overfitting / CSCV-style diagnostics;
- family winner uplift versus family baseline;
- best-result-vs-trial-count curve.

Start advisory. Do not hard-reject candidates until the implementation is validated.

## P3.3 — Stability analysis

Where BRAIN/PnL history permits, compute:

```text
year-by-year performance
rolling performance
subperiod Sharpe/Fitness
turnover stability
drawdown stability
correlation stability
```

Prefer robust candidates over isolated full-period spikes.

## P3.4 — Search provenance

A reported result should make clear whether it came from:

```text
1 manually designed candidate
10 mutations
1,000 variants
10,000 adaptive trials
```

The search cost itself is part of the evidence.

### Acceptance

Implemented by `scripts/robustness.py`: advisory campaign reports preserve trial counts, independence groups, family diagnostics, multiple-testing proxies, provenance, and explicit missing-PnL stability status. Reports are persisted in `robustness_reports` and never hard-reject candidates.

The permanent ledger is the `research_trials` table: one row per research decision,
keyed to the campaign with parentage, generation, mutation type/parameters, generator
version, reason, scope, field/operator catalog versions and the queue outcome, so a
canonical candidate can have several trial rows without spending duplicate BRAIN
capacity. **Campaign membership is the ledger, never `candidates.campaign_id`**: candidate
identity is the canonical expression, so the same canonical candidate generated by two
campaigns is one candidate row and two trial rows, and it appears in *both* campaign
reports. Campaign reports count every attempt — validation reject, duplicate/cache
hit, simulation fail, IS fail, correlation fail, submission reject, ACTIVE — and the
outcome buckets partition the ledger exactly.

Multiple-testing diagnostics keep three counts apart, because conflating them is what makes
a large adaptive search look small: `trial_count` (research decisions, the raw search
effort), `candidate_count` (unique canonical candidates) and `independence_count` (distinct
skeletons, the effective number of independent tests). The discount keys off
`independence_count`, so duplicate decisions add search cost but never inflate significance;
`effective_number_of_trials` stays advisory.

Stability metrics first-difference the cached cumulative PnL into daily returns before
computing subperiod, yearly and rolling Sharpe, and the rolling window also carries its mean
and cumulative return so a stable-but-negative stretch is visible. Drawdown is reported on
the PnL path, turnover/correlation dispersion is added, and the reason is named whenever a
candidate has no usable PnL series. Subperiod **Fitness** is reported as
`{"fitness": null, "fitness_status": "unavailable_from_cached_pnl"}` rather than invented:
BRAIN Fitness needs a per-period turnover the PnL cache does not store.

- best-performing candidates can be viewed in the context of all trials that produced them;
- campaign reports expose search size and independence structure;
- robustness diagnostics cannot silently ignore losing variants.

---

# P4 — Empirical field/operator intelligence

**Status: implemented in the current working tree** (`scripts/field_intelligence.py`);
hardened by issue #7.

Turn the static reference catalog into a continuously updated research map.

## P4.1 — Structured field coverage

Track per field and dataset:

```text
cataloged
validated
simulated
IS_PASS
CORR_PASS
submitted
ACTIVE
rejected
```

Record aggregates such as:

```text
attempts
pass rate
median Sharpe
median Fitness
median turnover
common failure reasons
successful structures
last tested
```

## P4.2 — Operator compatibility knowledge

Move beyond:

```text
field exists
operator exists
```

Capture machine-readable compatibility where possible:

```text
field unit/type
operator input type
operator output type
group requirements
vector requirements
delay restrictions
region/universe restrictions
```

Static validation should reject known impossible combinations before BRAIN.

## P4.3 — Coverage-aware generation

Candidate generation should explicitly search under-tested areas.

Example:

```text
dataset has 300 cataloged fields
only 7 tested
2 structurally valid
0 ACTIVE
```

This should be visible to the planner.

## P4.4 — Catalog versioning

Persist:

```text
field catalog SHA/version
operator catalog SHA/version
scope
fetch timestamp
```

Candidates/campaigns should reference the catalog version used when generated.

## P4.5 — Multi-scope preparation

Remove unnecessary architectural assumptions that all research is permanently:

```text
USA / TOP3000 / delay=1
```

Do not add broad new scopes until data references and validation rules exist, but make the core scope-aware.

### Acceptance

Implemented by `scripts/field_intelligence.py`: catalog-versioned field coverage, dataset aggregates, machine-readable operator compatibility, and under-tested-field ordering for the generator. Historical rows retain their catalog version and scope.

Coverage identity is now `field_id + catalog_version + scope_hash`, so USA/TOP3000/delay=1
evidence is never pooled with another scope (an existing pre-scope table is migrated in
place into the USA/TOP3000/delay=1 scope). Each stage is derived from evidence — a
`validation_passed` event, a terminal `simulations` row, a `submissions` row, an ACTIVE
state — instead of the candidate's current status, so a statically rejected candidate is
never counted as simulated. Type rules live in `scripts/compatibility.py` and are shared by
the generator, the validator, and the persisted `operator_compatibility` table.

The **queue is the safety boundary**: `ResearchDB.queue_candidate()` screens with strict
type policy by default, so a known deterministic field/operator incompatibility
(`TYPE_*` findings — a non-GROUP `group` argument, a bare VECTOR outside
`vec_avg`/`vec_sum`, a MATRIX passed to a vector operator) is a local reject and creates no
simulation row. The reusable `validate()` keeps its advisory default for standalone callers,
and a caller that deliberately wants to queue flagged work passes
`type_policy="advisory"`. Uncertain or out-of-scope findings (an unknown keyword, deep
nesting, a field outside the catalog scope, a positional optional argument) stay advisory
either way, because the local references cannot prove those — provably-broken requests
(unknown field or operator, wrong arity, malformed, impossible settings) stay errors under
every policy.

- field coverage is queryable from the database;
- generators can prioritize under-explored datasets;
- known deterministic field/operator type incompatibilities are rejected locally by the
  normal queue path before any simulation, while uncertain/out-of-scope compatibility
  findings remain advisory;
- research history remains interpretable after catalog updates.

---

# P5 — Offline research-policy benchmark

**Status: implemented (2026-09-22).** `scripts/policy_replay.py` replays candidate-selection
policies over local history and scores the funnel each would have produced; there are no BRAIN
calls and no credentials involved.

The point-in-time contract is enforced by one rule: a policy only ever sees pre-simulation
facts, and an outcome is reachable only when it had settled *strictly before* the decision's
clock. The clock is the monotonic `events.id`, not a timestamp — wall-clock stamps are
second-granularity in this database, so a candidate queued and simulated inside one second
would otherwise look settled the moment it was created and every decision would score as a
free cache hit. A candidate created after the decision is invisible; one that settled at or
after it exposes nothing (so neither the choice nor the calibration it consults can look
ahead); one that settled before it costs no slot, because the real queue would serve it from
cache. `replay()` re-checks all of this afterwards and reports `leakage_check`, naming the
failing step — so a reintroduced leak cannot pass silently.

The single "settled strictly before" rule is enforced in every place it can be violated:

- outcomes are exposed **stage by stage**. A result is several facts, each with its own
  `events.id`: the simulation result, the correlation check (`self_corr`), and the move beyond
  the IS gate (correlation pass, submission verdict, ACTIVE). A policy sees only the stages
  that had settled at its clock, so an IS pass that later failed correlation reads as an IS
  pass at the moment it passed.
- the decision's **own history** records earlier choices as attempts but withholds each
  result until it settled before the current clock, so a later pick in the same round cannot
  read an earlier pick's outcome.
- **ranking components** are resolved from the `ranked` events that existed before the clock,
  never from the candidate's current row, so a later re-rank cannot reach back in time.
- the **surrogate baseline is trained as of the decision clock** from already-settled results
  and never consults the persisted live model; below the sample floor it degrades to FIFO.
- **campaign membership is the `research_trials` ledger**, not `candidates.campaign_id`, so a
  cross-campaign duplicate is replayed for every campaign that decided on it.

Decisions follow the **generation waves** — work that arrived together (one `source`, one
clock second), which is how this history actually arrived (batches of candidates per second).
A round opens when a batch has finished arriving; the policy picks from everything visible
then and may spend its remaining budget there, but the round's information is frozen, so it
cannot learn from its own picks. Presenting one new candidate per step instead would force
every policy to reproduce creation order and make the comparison meaningless. The per-policy
numbers move with the corpus and with these corrections; reproduce them locally with
`--compare`. Policy comparison is discriminating exactly where the funnel is: the budget is
the knob, and the default is deliberately scarce (10% of the corpus) because a budget
covering the corpus makes every policy score the same.

## P5.1 — Historical replay environment

Replay decisions over settled candidate history.

A policy should only see information that existed at the historical decision point.

Prevent future-data leakage.

## P5.2 — Benchmark metrics

Compare research policies on:

```text
simulations to first IS_PASS
IS_PASS per simulation
CORR_PASS per simulation
ACTIVE per simulation
top-k successful candidate recall
wasted variants
family/niche diversity
correlation failure rate
turnover failure rate
robustness-adjusted quality
```

## P5.3 — Baselines

Maintain simple baselines:

```text
FIFO
existing ranking
existing staged search
surrogate ranking
```

New policies must be compared against existing behavior.

## P5.4 — Policy versioning

Record:

```text
policy name
policy version
parameters
training cutoff
evaluation window
seed
result metrics
```

Each run is appended to the `policy_replay_runs` table with its comparison id, corpus size,
simulation budget, seed, metrics and baseline deltas, so a policy's evidence is durable and
attributable.

### Acceptance

- candidate selection/search policies can be evaluated without BRAIN calls;
- replay enforces point-in-time information boundaries;
- a new policy has measurable evidence before becoming default.

### Shipped surface

```bash
./.venv/bin/python scripts/policy_replay.py --list
./.venv/bin/python scripts/policy_replay.py --compare --budget 20
./.venv/bin/python scripts/policy_replay.py --run calibrated_skip --budget 20 --json
```

Policies: `fifo`, `ranking`, `staged_search` and `surrogate` (the baselines), plus `coverage`,
`calibrated_rank` and `calibrated_skip`. Returning nothing from `order()` means *decline*: the
rest of that round is skipped and no slot is spent, which is how "do not run work history says
BRAIN refuses" is expressed. The calibrated policies read a calibration computed only from
outcomes settled strictly before the round's clock.

### Calibration of the advisory rules (`scripts/finding_calibration.py`)

The P4 decision to keep type findings advisory was a guess: the offline reference files cannot
prove what the platform accepts. This measures it instead. Every candidate that actually
reached BRAIN is re-screened locally with a *neutral* severity policy, and each finding code
is scored against the real outcome, where only `simulations.status = 'ERROR'` counts as a
refusal. An IS-gate failure means BRAIN accepted the request and evaluated it, so it is not
evidence that a local rule was right; a candidate the local gate itself refused never reached
the platform, so it carries no evidence at all. Both are excluded from the denominator, and
the per-code request counts are reported so the denominator is visible.

On the local corpus a rule such as `POSITIONAL_OPTIONAL_ARGUMENT` fires on many candidates
while BRAIN refuses only a small share of them, so it stays advisory and is explicitly marked
inconclusive — the same rule a naive "this looks wrong" heuristic would have promoted. Codes
with a handful of samples are refused promotion for insufficient evidence. The counts are
corpus-dependent and reproduced by `--refresh`, so they are not quoted as fixed numbers here;
the deterministic `TYPE_*` findings are hard gates at the queue boundary (P4) and are no
longer calibratable.

Nothing is enforced by measuring, and a rejection rate is not by itself a reason to enforce
either: refusing work that *would* have passed costs passes, so an accurate rule can still be
useless or harmful. `--approve` therefore replays each proposed code over the whole corpus
with the flagged work declined, and enforces only the codes that reach the *same passes for
less capacity*. The three outcomes are distinct on purpose:

- **improved** — same passes, fewer simulations: worth enforcing;
- **regression** — fewer passes: refused, with the lost passes named;
- **insufficient_evidence** — no baseline ever bought a slot on a candidate carrying the code,
  or the rule would decline everything and leave nothing to compare.

The two views are reported side by side (`--refresh --funnel`) because they disagree in both
directions on the real corpus: `POSITIONAL_OPTIONAL_ARGUMENT` is refused by BRAIN only 2 times
out of 17, so the rate view calls it inconclusive, yet all 17 candidates delivered nothing, so
the funnel view shows the same 7 passes for 17 fewer simulations. Conversely a code carried by
passing work is refused by the funnel however perfectly it predicts refusals. `--code` proposes
a specific rule, `--force` records an explicit override, and `--clear` reverses enforcement;
approval covers the whole corpus by default, because a scarce budget would only reach the first
generation wave and report no evidence for anything tripped later.

That keeps "the pipeline now rejects this" an explicit, reviewable, reversible act, and the
point-in-time replay is what prices it: on a corpus with refused `group_rank` work first,
`calibrated_skip` at `min_samples=1` pays for exactly one refusal and then declines every
flagged candidate, while the same policy at a threshold history cannot meet behaves exactly
like FIFO. The difference between those two runs is the measured price of requiring evidence.

---

# P6 — Uncertainty-aware surrogate and acquisition

Keep the surrogate lightweight and advisory, but make it useful for exploration decisions.

## P6.1 — Predictive uncertainty

Extend predictions from:

```text
predicted metric
```

to:

```text
predicted mean
uncertainty
sample count/support
```

Start with simple approaches such as:

```text
bootstrap ridge ensemble
small model ensemble
```

Do not introduce a heavy ML stack without evidence that it improves policy replay.

## P6.2 — Acquisition score

Candidate priority should eventually combine:

```text
expected quality
uncertainty / information gain
novelty
family diversity
portfolio diversification
failure risk
search cost
```

Possible behavior:

```text
high mean + low uncertainty
→ exploit

moderate mean + high uncertainty
→ explore

low mean + low uncertainty
→ deprioritize
```

## P6.3 — Calibrate before trust

Track:

```text
ranking quality
top-k recall
prediction calibration
error by family
error by generation
error on unseen fields/operators
```

The surrogate must remain advisory until its OOS performance is consistently useful.

### Acceptance

- prediction uncertainty is available to the scheduler/planner;
- acquisition behavior improves offline policy replay;
- surrogate estimates never silently become hard truth.

---

# P7 — Reproducible research campaigns

Unify individual scripts into a durable research-run abstraction.

## P7.1 — Campaign model

Add a campaign record with at least:

```text
campaign_id
name/objective
scope
seed
status
created_at
started_at
finished_at

simulation_budget
submission_budget

allowed / excluded families
exploration policy
generation policy
ranking policy

git commit
SKILL SHA
catalog/operator versions
surrogate/policy versions
agent/model metadata
```

## P7.2 — Universal CLI

Target interface:

```bash
python -m wq campaign create ...
python -m wq campaign run <id>
python -m wq campaign status <id>
python -m wq campaign pause <id>
python -m wq campaign resume <id>
python -m wq campaign report <id>
```

Existing scripts should become service modules or compatibility wrappers rather than duplicated implementations.

## P7.3 — Restart safety

A killed campaign must resume without:

- duplicate simulation;
- duplicate submission;
- lost parentage;
- lost search-budget state;
- regenerated incompatible candidates;
- forgetting which policy/version produced previous decisions.

## P7.4 — Bounded autonomy

Campaigns must have explicit limits:

```text
max generated candidates
max BRAIN simulations
max submissions
max runtime
max retry budget
```

No infinite autonomous loop.

### Acceptance

- a complete research run can be created, stopped, resumed, inspected, and reproduced;
- every candidate can be traced to the campaign/policy/generator that produced it;
- campaign budgets remain enforceable after restart.

---

# P8 — Reporting and observability UI

Do this after the research policy becomes useful.

The database already contains much of the required telemetry.

Provide campaign/research reports for:

```text
simulation throughput
pass funnel
family allocation
archive coverage
field coverage
trial count
quality vs simulation spend
surrogate calibration
failure reasons
ACTIVE yield
correlation clusters
knowledge-rule changes
```

Prefer generated local HTML/JSON or a lightweight UI before introducing a large web stack.

The CLI and database remain canonical.

---

# P9 — Optional integrations

Only after P1-P7 are stable.

Possible additions:

```text
optional MCP facade
agent convenience adapters
local REST API
dashboard service
embedding retrieval
remote/private state backup
scheduled workers
```

Requirements:

- integrations use the same core Python service layer;
- no integration contains unique research logic;
- no specific agent runtime is required;
- secrets and private research state remain local/private by default.

---

# Deferred — unattended remote submission

Do not prioritize autonomous GitHub Actions submission yet.

Revisit only after:

- campaign budgets are proven;
- search policy is replay-tested;
- persistent private state is available;
- secret handling is proven;
- overlapping workers are impossible;
- live submission recovery remains reliable;
- remote automation complies with platform/provider constraints.

Local bounded workers remain the preferred execution model.

---

# Non-goals

Do **not**:

- optimize solely for maximum raw Sharpe;
- generate thousands of trivial parameter clones;
- treat the highest observed result as proof of skill;
- let one family consume the whole simulation budget;
- use an LLM as the source of truth for validation;
- bypass `research.db` for experimental history;
- hide failed trials from robustness calculations;
- replace interpretable policies with deep ML without benchmark evidence;
- build separate cores for Codex, Pi, OpenCode, Hermes, or another agent;
- require MCP for correctness;
- store credentials in the learning system;
- publish alpha IDs, exact private expressions, PnL, or account-linked state;
- mutate `SKILL.md` directly from raw experiment output.

---

# Target architecture

```text
                    ┌──────────────────────┐
                    │   Research Campaign  │
                    └──────────┬───────────┘
                               │
                    ┌──────────▼───────────┐
                    │   Research Planner   │
                    │ archive / knowledge  │
                    │ surrogate / coverage │
                    └──────────┬───────────┘
                               │
             ┌─────────────────▼─────────────────┐
             │ Generator + Targeted Mutations    │
             └─────────────────┬─────────────────┘
                               │
                       static validation
                               │
                               ▼
                         research.db
                               │
              ┌────────────────┼────────────────┐
              │                │                │
              ▼                ▼                ▼
         ranking         staged search       archive
              │                │
              └────────┬───────┘
                       ▼
                simulation scheduler
                       │
                       ▼
                  BRAIN results
                       │
          ┌────────────┼──────────────┐
          ▼            ▼              ▼
     robustness    correlation     knowledge
       analysis        gate          learning
          │            │              │
          └──────┬─────┘              ▼
                 ▼                 rules/skill
             submission
                 │
                 ▼
               ACTIVE
                 │
                 └──────────────► next generation
```

---

# Implementation order

Recommended order:

```text
P0  docs + previous-roadmap closure

P1  generation / mutation
 ↓
P2  archive + adaptive budget
 ↓
P3  trial ledger + robustness
 ↓
P4  field/operator intelligence
 ↓
P5  policy replay benchmark
 ↓
P6  uncertainty-aware acquisition
 ↓
P7  campaign abstraction
 ↓
P8  reporting/UI
 ↓
P9  optional integrations
```

P1-P3 provide the largest increase in research capability.

---

# Project-level success criteria

The next roadmap is complete when:

- the system can decide what candidates to generate instead of requiring a prepared expression list;
- mutations are structured, explainable, and failure-directed;
- candidate lineage is complete and restart-safe;
- research preserves multiple high-quality, meaningfully different families;
- simulation capacity is allocated from evidence rather than only fixed heuristics;
- every discovery is interpreted relative to the search that produced it;
- large candidate searches receive multiple-testing/robustness diagnostics;
- field/operator exploration is tracked systematically;
- research policies can be replayed offline before spending real BRAIN capacity;
- surrogate uncertainty helps choose informative experiments;
- a bounded campaign can autonomously generate, simulate, diagnose, mutate, correlate, submit, learn, stop, and resume;
- the system remains usable by any shell-capable agent;
- private research data and credentials never become public tracked knowledge.