# TODO — Active Priorities

Last audited: 2026-09-19.

Goal: maximize useful WorldQuant BRAIN research throughput while keeping simulation,
submission, and learned knowledge restart-safe, private, auditable, and agent-agnostic.

## Priority 1 — Agent-agnostic self-evolving knowledge and skill system

Tracking issue: [#1 — Build agent-agnostic self-evolving knowledge + skill system](https://github.com/Uwater1/wq-alpha-research/issues/1)

Status: implemented in the local knowledge/skill-management slice. Replace direct lesson appends to `SKILL.md` with durable,
evidence-backed knowledge and evaluated skill updates.

### Storage and compatibility

Extend `research.db` with structured knowledge for:

- observations and evidence;
- scoped rules and rule proposals;
- support and contradiction counts;
- lineage, mutation outcomes, and family aggregates;
- provenance, privacy class, and lifecycle state.

Keep current simulation/submission behavior and legacy export compatibility.

### Terse skill and retrieval

Refactor root `SKILL.md` into a compact router containing only:

1. scope;
2. research workflow;
3. generation rules;
4. validation and simulation gates;
5. correlation and submission gates;
6. recall and learning commands;
7. privacy and safety;
8. pointers to references and deterministic scripts.

Move campaign logs, raw experiments, large field/operator material, and occasional
examples into topical references. Start retrieval with SQLite FTS5 and structured
filters; embeddings are optional later.

### Proposal-based learning

Replace the direct event-to-skill path with:

```text
events -> scoped observations -> aggregate evidence -> rule proposal
```

Each rule must carry scope, supporting and contradicting evidence, provenance,
independence grouping, and an explicit state such as proposed, active, weakened,
retired, or pinned. One simulation result must not become a global instruction.

### Safe skill management

Route all skill mutations through one manager with:

- expected-SHA/version guards;
- atomic writes;
- mutation ledger and content-addressed backups;
- diff and rollback;
- user-owned/pinned rule protection;
- privacy checks before writing tracked files.

### Universal interface and privacy

Expose stable shell commands for status, candidate/simulation operations, recall,
knowledge/evidence, learning review/evaluation/promotion/rejection, skill validation,
history, and rollback. Core correctness must not depend on Codex, Pi, OpenCode,
Hermes, MCP, or another agent runtime.

Use at least `PUBLIC`, `SANITIZED`, `PRIVATE`, and `SECRET` privacy classes. Credentials
must never enter the learning store. Exact alpha IDs, private expressions, account-
linked PnL, and submission history must not enter public tracked skill/reference files.

### Evaluation gate

Before autonomous promotion, replay historical research where possible and compare the
candidate rule/skill with the current baseline on selection quality, wasted variants,
diversity, turnover, correlation failures, regressions, and privacy violations.
Promotion, rejection, retention, and rollback must all be normal outcomes.

Acceptance:

- a fresh shell-capable agent can operate from `AGENTS.md` and `SKILL.md`;
- raw experiments stay outside prompt-loaded skill text;
- rules are scoped, evidenced, versioned, auditable, and reversible;
- contradictory evidence can weaken or retire a rule;
- concurrent agents cannot overwrite stale skill state;
- private data cannot be promoted into public files;
- no specific agent runtime is required for correctness.

## Priority 2 — Learned simulation surrogate

Status: implemented as an advisory ridge surrogate. It trains from settled local history and contributes only a ranking bonus; predictions never hard-reject candidates.

Train ranking models from structural features, fields/categories/coverage, windows and
settings, signal family, lineage/mutation features, parent outcomes, and local
correlation/diversification.

Targets may include Sharpe, Fitness, Turnover, IS pass/fail, common failure checks, and
submission readiness. Optimize ranking quality and top-candidate recall, not exact
metric prediction. Do not hard-reject candidates solely from model prediction at first.

## Priority 3 — Complete observability

Status: implemented. Structured transport/event dimensions and expanded local throughput counters are persisted in `research.db`; remaining work is to add deployment dashboards if needed.

Extend the existing event and status groundwork to record operation, timestamp,
candidate/simulation/submission identifiers, HTTP status/category, retry count,
latency, rate-limit/backoff events, and result class.

Track at least:

- generated, validated, and simulated candidates per hour;
- cache-hit and simulation-success rates;
- IS-pass, correlation-pass, and submission-success rates;
- ACTIVE alphas per week;
- BRAIN simulations per IS pass and per ACTIVE alpha.

## Priority 4 — Regression coverage and safe deployment

Status: implemented for offline correctness and bounded local deployment; live BRAIN/GitHub Actions submission remains explicit/manual.

Keep the default test suite offline. Live BRAIN probes remain explicit/manual and may
not require credentials in default tests.

Regression coverage now includes submission recovery, correlation pagination/cache/
staleness, canonical identity, staged-search budgeting, and the recent cases where:

- an uncertain submit response must reconcile before another POST;
- a failed ACTIVE-book refresh holds correlation-enabled submissions;
- `--no-staged` actually disables staged search above its volume threshold.

Covered:

- rule evidence, proposal, promotion, rollback, concurrency, and privacy cases;
- a mocked end-to-end flow:
  `generate -> queue -> simulate -> correlate -> submit -> ACTIVE -> learn`.

Deployment remains bounded and restart-safe: default tests are offline, credentials are resolved only in memory, uncertain submissions reconcile before retry, correlation freshness is required when enabled, and unattended GitHub Actions submission remains deferred.

Unattended GitHub Actions submission remains deferred until submission recovery,
correlation freshness, persistent/private state, bounded runs, secret handling, and
overlap safety are proven.
