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

- the project can generate and queue new candidates without a manually prepared CSV;
- candidate lineage is complete;
- failed candidates can produce targeted children;
- generation is reproducible when given the same campaign state and seed;
- agent-generated expressions use the same service layer as deterministic generators.

---

# P2 — Quality-diversity archive and adaptive research allocation

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

- one successful family cannot monopolize the research budget;
- unexplored but promising niches continue receiving bounded exploration;
- family allocation adapts from empirical outcomes;
- deterministic staged-search remains available as fallback.

---

# P3 — Search-aware statistical robustness

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

- best-performing candidates can be viewed in the context of all trials that produced them;
- campaign reports expose search size and independence structure;
- robustness diagnostics cannot silently ignore losing variants.

---

# P4 — Empirical field/operator intelligence

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

- field coverage is queryable from the database;
- generators can prioritize under-explored datasets;
- known field/operator type incompatibilities are rejected locally;
- research history remains interpretable after catalog updates.

---

# P5 — Offline research-policy benchmark

Before letting a new search policy spend real BRAIN capacity, evaluate it offline.

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

### Acceptance

- candidate selection/search policies can be evaluated without BRAIN calls;
- replay enforces point-in-time information boundaries;
- a new policy has measurable evidence before becoming default.

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