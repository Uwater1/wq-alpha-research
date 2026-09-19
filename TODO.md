# TODO — BRAIN Throughput & Submission Pipeline

Goal: maximize useful alpha throughput under WorldQuant BRAIN simulation/submission limits without brute-force concurrency.

## P0 — Separate Research, Simulation, and Submission

> **Status: implemented** (`scripts/research_db.py`, wired into
> `legacy/wq_brain/batch_simulate.py`). Schema, state machine, leased claims and
> per-result commits are in place; P6/P8 will build their workers on the
> `submissions` primitives already exposed there.

Current workflows couple generation, simulation, checking, and submission too tightly.

Create explicit states:

```text
GENERATED
→ VALIDATED
→ QUEUED
→ SIMULATING
→ SIMULATED
→ IS_PASS
→ CORR_PASS
→ SUBMISSION_READY
→ SUBMITTING
→ ACTIVE / REJECTED / RETRY
```

Persist state so every process can stop/restart safely.

Use one local DB:

```text
research.db
```

Recommended tables:

```text
candidates
simulations
submissions
active_alphas
events
```

Each candidate should store at least:

```text
id
expression
expression_hash
settings_hash
status
priority
signal_family
source
created_at
updated_at
brain_alpha_id
simulation_id
sharpe
fitness
turnover
self_corr
failure_reason
attempt_count
```

Acceptance:

- no completed simulation is lost on interruption;
- submission can run independently of research;
- restarting scripts does not duplicate work.

---

## P1 — Canonicalization, Dedup, and Simulation Cache

> **Status: implemented** (`scripts/canonical.py` + the `simulations` cache table).
> Formatting/parameter variants collapse to one key, exact duplicates and in-flight
> requests are skipped, and `batch_simulate.py` reuses completed results instead of
> re-simulating. Near-duplicates are flagged via `skeleton_hash`/`near_duplicate_of`,
> never merged.

Before calling BRAIN, normalize candidate expressions and settings.

Canonical key:

```text
SHA256(
    normalized_expression
    + region
    + universe
    + delay
    + decay
    + neutralization
    + truncation
    + pasteurization
    + unitHandling
    + nanHandling
    + language
)
```

Normalize:

- whitespace;
- operator capitalization where safe;
- numeric formatting;
- settings defaults;
- deterministic field ordering.

Before simulation:

```text
if exact completed cache hit:
    reuse result
elif already queued/running:
    skip duplicate
else:
    queue
```

Also detect near-duplicates:

- same expression with trivial formatting differences;
- same economic signal with only tiny parameter changes;
- duplicate parameter grids produced by different agents.

Do not automatically merge semantically different expressions unless equivalence is proven.

Acceptance:

- identical simulation requests never hit BRAIN twice;
- old results are reusable across research sessions;
- cache survives process/container restart.

---

## P2 — Persistent BRAIN Simulation Scheduler

> **Status: implemented** (`scripts/sim_scheduler.py`, `scripts/brain_api.py`,
> `scripts/ranking.py`), verified against live BRAIN: two slots filled at once,
> progress polled, results committed and IS-gated. `Retry-After`, exponential
> backoff, re-authentication, orphan adoption, lease recovery and priority ordering
> are covered by tests, and the score components are stored on every candidate.

Replace simple `ThreadPoolExecutor` batch execution with a persistent dispatcher.

Default maximum:

```text
MAX_SIMULATION_SLOTS = 3
```

Scheduler responsibilities:

- maintain exactly the allowed number of active simulations;
- immediately refill a free slot;
- poll running simulations separately from submitting new ones;
- obey `Retry-After`;
- exponential backoff on transient failures;
- re-authenticate when session expires;
- retry recoverable failures;
- persist all state transitions.

Priority order should not be FIFO only.

Suggested score:

```text
priority =
    expected_quality
    + novelty
    + information_gain
    + family_diversity
    - duplicate_penalty
    - failure_risk
```

Initial version may use simple heuristics from `SKILL.md`.

Acceptance:

- slots remain occupied whenever queued work exists;
- 429 responses do not cause request storms;
- failed workers do not lose queue state.

---

## P3 — Multi-Simulation Support

> **Status: checked — the platform does not offer it.** A live probe on this account
> returns HTTP 400 `{"type": ["Object with name=MULTI does not exist."],
> "regular": ["Not a valid string."]}`: the only simulation type is REGULAR with a
> single expression string, so there is nothing to pack. `scripts/multi_sim.py`
> records the verdict in `research.db.meta` (`--probe` / `--status`) and the pipeline
> never assumes support, so the 3-slot REGULAR scheduler is the implemented path.
> Packing is deliberately not built for an endpoint that rejects it — re-run
> `--probe` after an account/platform change and this section becomes actionable.

Detect whether the account/API supports BRAIN multi-simulation.

If supported, pack compatible candidates into batches.

Candidates in one batch must share all required common settings, e.g.:

```text
region
universe
delay
language
instrument type
```

Group by compatible settings before packing.

Target:

```text
MULTI_SIM_BATCH_SIZE = platform-supported maximum
```

Architecture:

```text
candidate queue
→ group by compatible settings
→ pack batch
→ multi-simulation
→ unpack child results
→ persist individually
```

Fallback:

```text
if unsupported:
    use REGULAR simulation scheduler
```

Never assume multi-simulation support.

Acceptance:

- automatic capability detection;
- REGULAR fallback remains fully functional;
- each child alpha retains independent metrics/status.

---

## P4 — Candidate Pre-Screening

Do not spend BRAIN capacity on obviously weak candidates.

### P4.1 Static validation

Reject before simulation when:

- unknown field;
- unsupported operator;
- invalid operator arity;
- impossible settings;
- malformed FASTEXPR;
- known incompatible field/operator combination.

Use local field catalog first.

### P4.2 Structural screening

Record features:

```text
field categories
operator counts
expression depth
time-series windows
group operators
decay
neutralization
truncation
signal family
```

Flag candidates strongly resembling previously poor families.

Do not hard-reject solely from heuristic predictions at first; lower their priority instead.

### P4.3 Diversity gate

Avoid simulating hundreds of minor variants simultaneously.

Example:

```text
same base expression
windows = [20, 40, 60, 120, 250]
decays = [0, 2, 4, 8, 16]
```

Initially test only representative configurations.

Expand parameter search only if the base signal works.

---

## P5 — Successive-Halving Search

Replace exhaustive parameter grids.

Example:

```text
Stage 0
2000 generated ideas

Stage 1
local validation / dedup
→ 800

Stage 2
one baseline simulation per core signal
→ retain top ~15–25%

Stage 3
parameter tuning on survivors
→ retain top ~20–30%

Stage 4
final IS / correlation validation
→ submission-ready set
```

Parameters worth tuning after a signal proves viable:

```text
window
decay
neutralization
truncation
signal blend weights
trade_when / smoothing
```

Avoid tuning dozens of variants of a signal with clearly poor Sharpe/Fitness.

Persist parent-child relationships:

```text
candidate.parent_id
candidate.generation
candidate.mutation_type
```

This lets the agent learn which modifications improved results.

---

## P6 — Submission Queue

Do not submit directly from simulation workers.

Create a dedicated queue containing only candidates that pass configured gates.

Suggested default gates:

```text
IS checks pass
Sharpe >= configured floor
Fitness >= configured floor
turnover within configured range
local self-correlation gate passes
not already ACTIVE
not already queued/submitted
```

Queue ordering should consider:

```text
submission_priority =
    quality
    + novelty
    + portfolio_diversification
```

Do not stop the entire queue after the first successful submission.

Track independently:

```text
READY
SUBMITTING
CHECK_PENDING
ACTIVE
SELF_CORR_FAIL
PLATFORM_REJECTED
RETRY
```

---

## P7 — Hourly GitHub Actions Submission Worker

Use GitHub Actions as a low-frequency queue drainer.

Purpose:

```text
research continuously builds SUBMISSION_READY candidates
GitHub Action wakes hourly
→ attempts limited submissions/checks
→ persists result
→ exits
```

Workflow:

```yaml
schedule:
  - cron: "17 * * * *"

workflow_dispatch:
```

Use a non-zero minute to avoid common `:00` scheduler congestion.

Worker behavior:

```text
1. authenticate
2. load submission queue
3. refresh ACTIVE/current statuses
4. select highest-priority READY candidate
5. re-check stale gates if needed
6. submit
7. poll only within bounded runtime
8. persist result
9. optionally process next candidate up to configured limit
10. exit
```

Configuration:

```text
MAX_SUBMISSIONS_PER_RUN
MAX_RUNTIME_MINUTES
CHECK_POLL_SECONDS
MAX_CHECK_POLLS
```

Start conservatively:

```text
MAX_SUBMISSIONS_PER_RUN=1
```

Increase only if platform behavior confirms it is safe and useful.

### Secrets

Store only in GitHub Actions secrets:

```text
WQ_BRAIN_USERNAME
WQ_BRAIN_PASSWORD
```

Never commit credentials/session cookies.

Prefer an isolated submission script:

```text
scripts/submission_worker.py
```

### Persistence problem

GitHub Actions runners are ephemeral.

Do not use runner-local SQLite as the canonical queue.

Choose one persistent backend:

```text
A. committed sanitized queue metadata — only if no private/account-linked data
B. GitHub artifact — poor choice for canonical mutable state
C. external DB/object store
D. self-hosted runner with persistent disk
E. API-backed private service
```

Preferred architecture:

```text
private persistent DB/service
        ↑
local researcher
        ↓
GitHub Actions submission worker
```

Do not commit:

```text
alpha IDs
private expressions
PnL
account-linked metrics
submission history
credentials
```

If all research state must remain local/private, use a self-hosted runner instead of GitHub-hosted runners.

---

## P8 — Idempotent Submission Worker

Hourly jobs must be safe if duplicated or delayed.

Before submit:

```text
SELECT candidate
WHERE status = SUBMISSION_READY
AND next_attempt_at <= now
ORDER BY priority DESC
LIMIT 1
```

Acquire lock/lease:

```text
status = SUBMITTING
lease_until = now + N minutes
worker_id = ...
```

Then submit.

After crash:

```text
expired SUBMITTING lease
→ reconcile against BRAIN
→ READY / ACTIVE / REJECTED
```

Use BRAIN state as source of truth before retrying an uncertain submission.

Never blindly resubmit after timeout.

Acceptance:

- two overlapping GitHub Actions cannot submit the same alpha twice;
- crash during submission is recoverable;
- every attempt is auditable.

---

## P9 — Self-Correlation Pipeline

Separate cheap local correlation from expensive platform checks.

Before submission:

```text
candidate daily PnL
vs
ACTIVE alpha daily PnL
```

Use daily PnL changes/returns, not cumulative PnL curves.

Cache ACTIVE PnL locally and refresh when:

- an alpha becomes ACTIVE;
- old cache exceeds configured age;
- BRAIN data changes.

Record:

```text
max_corr
max_corr_alpha_id
corr_checked_at
active_set_version
```

If ACTIVE portfolio changes after local checking, mark old candidates:

```text
CORR_STALE
```

and re-check before submission.

---

## P10 — Better Candidate Ranking

Initially derive ranking from known empirical rules.

Example positive features:

```text
fundamental signal
good field coverage
reasonable expression depth
group normalization
moderate turnover family
novel signal family
successful parent candidate
```

Negative features:

```text
historically poor family
extreme expected turnover
many nested operators
tiny mutation of failed alpha
similarity to existing ACTIVE alpha
```

Store the ranking components, not only the final score.

Example:

```json
{
  "quality_score": 0.72,
  "novelty_score": 0.84,
  "failure_risk": 0.21,
  "priority": 1.35
}
```

This makes later model training possible.

---

## P11 — Learn a Simulation Surrogate

After enough simulations are collected, train a model to predict candidate quality.

Input features:

```text
AST/operator features
fields/categories
field metadata
windows
decay
neutralization
truncation
expression depth
signal family
parent performance
```

Targets:

```text
Sharpe
Fitness
Turnover
IS pass/fail
LOW_SUB_UNIVERSE_SHARPE
CONCENTRATED_WEIGHT
submission-ready probability
```

Primary goal is ranking, not exact metric prediction.

Useful metric:

```text
precision/recall for identifying top simulation candidates
```

Use model output as scheduler priority.

Do not initially hard-reject candidates solely from model prediction.

---

## P12 — Research Feedback Loop

Extend `evolve_skill.py`.

Current output:

```text
human-readable lessons
```

Add machine-readable observations:

```text
research_events
candidate lineage
simulation outcomes
mutation outcomes
family statistics
```

Compute aggregates:

```text
pass rate by signal family
pass rate by dataset/category
parameter success rate
mutation improvement rate
median Sharpe/Fitness/turnover
simulation cost per successful alpha
```

Write only sanitized/general lessons into `SKILL.md`.

Keep private research data outside git.

---

## P13 — Observability

Log every BRAIN interaction with:

```text
timestamp
operation
candidate_id
simulation_id
alpha_id
HTTP status
retry count
latency
result
```

Track throughput:

```text
generated/hour
validated/hour
simulated/hour
cache-hit rate
simulation success rate
IS pass rate
correlation pass rate
submission success rate
ACTIVE/week
```

Most important efficiency metric:

```text
BRAIN simulations per ACTIVE alpha
```

Also track:

```text
BRAIN simulations per IS_PASS alpha
```

Goal is to reduce these over time.

---

## P14 — Tests

Add unit tests for:

```text
expression normalization
simulation-key generation
exact dedup
queue state transitions
priority ordering
retry/backoff
lease recovery
GitHub worker idempotency
correlation calculation
successive-halving selection
multi-sim packing
multi-sim fallback
```

Add mocked integration flow:

```text
generate
→ queue
→ simulate
→ result
→ correlation
→ submission queue
→ submit
→ ACTIVE
```

No test should require live credentials by default.

Live BRAIN tests must be explicit/manual.

---

## P15 — Recommended Implementation Order

### Phase 1 — Make Current Pipeline Reliable

```text
P0 state model
P1 cache/dedup
P2 persistent 3-slot scheduler
P6 submission queue
P8 idempotency
```

### Phase 2 — Use Time Better

```text
P7 hourly GitHub Actions worker
P9 correlation cache
P4 pre-screening
P5 successive halving
```

### Phase 3 — Increase Effective Capacity

```text
P3 multi-simulation
P10 smarter ranking
P12 structured learning
```

### Phase 4 — Learned Research System

```text
P11 surrogate model
advanced candidate generation
adaptive search budgets
```

---

# Target Architecture

```text
                     ┌─────────────────────┐
                     │   Agent Generator   │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │ Validate + Normalize│
                     │ Dedup + Cache Check │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │ Priority Queue / DB │
                     └──────────┬──────────┘
                                │
                ┌───────────────┴───────────────┐
                ▼                               ▼
      ┌───────────────────┐          ┌───────────────────┐
      │ Multi-Sim Packer  │          │ REGULAR Fallback  │
      └─────────┬─────────┘          └─────────┬─────────┘
                └───────────────┬───────────────┘
                                ▼
                     ┌─────────────────────┐
                     │ BRAIN Slot Scheduler│
                     │      max = 3        │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │ Results + Learning  │
                     └──────────┬──────────┘
                                │
                      successive halving
                                │
                                ▼
                     ┌─────────────────────┐
                     │ IS + Correlation    │
                     │       Gate          │
                     └──────────┬──────────┘
                                │
                                ▼
                     ┌─────────────────────┐
                     │ Submission Queue    │
                     └──────────┬──────────┘
                                │
                       hourly / manual
                                │
                                ▼
                     ┌─────────────────────┐
                     │ Submission Worker   │
                     │ GitHub Action or    │
                     │ self-hosted runner  │
                     └──────────┬──────────┘
                                │
                                ▼
                           ACTIVE
```

# Definition of Done

The pipeline is complete when:

- research can generate candidates faster than BRAIN can consume them without losing state;
- duplicate simulations are eliminated;
- three simulation slots stay efficiently utilized;
- multi-simulation is used automatically when a probe proves the platform supports
  it (it does not today — see P3), with the REGULAR scheduler as the fallback;
- parameter brute force is replaced by staged search;
- simulation and submission operate independently;
- submission-ready alphas remain queued until processed;
- an hourly worker can safely drain the queue;
- overlapping/restarted workers cannot duplicate submissions;
- private/account-linked research data never enters the public repository;
- each simulation improves future candidate ranking;
- useful output is measured by `simulations / ACTIVE alpha`, not raw simulation count.