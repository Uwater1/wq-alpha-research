# TODO — Active Roadmap

Last audited: 2026-09-19 against `main@611f818`.

Goal: maximize useful WorldQuant BRAIN research throughput while keeping simulation, submission, and learned knowledge restart-safe, private, auditable, and agent-agnostic.

## Status legend

- ✅ done: keep only regression coverage; do not rebuild.
- ⚠️ partial/problem: implementation exists but has a known gap.
- ⏳ pending: not implemented yet.
- 💤 deferred: intentionally last.

---

## Completed baseline

| Area | Status | Current implementation / note |
|---|---|---|
| P0 research state | ✅ | `research.db`, candidate/simulation/submission lifecycle, events, restartable state. |
| P1 canonicalization/cache | ✅ | `scripts/canonical.py` + reusable simulation cache + exact dedup. |
| P2 persistent scheduler | ✅ | `scripts/sim_scheduler.py`; slot refill, polling, retry/backoff, re-auth, orphan adoption. |
| P3 multi-simulation | ✅ blocked by platform | Live probe rejected `MULTI`; keep REGULAR scheduler. Re-probe only after account/platform changes. |
| P4 pre-screening | ✅ scoped | Static validation + structural features/ranking. Current field coverage is mainly USA/TOP3000/delay=1; expand the catalog before treating other scopes as fully validated. |
| P6 submission queue | ✅ core | Dedicated priority queue and worker exist. |
| P8 submission recovery | ✅ | `submissions.post_attempted_at` separates "never POSTed" from "POST outcome unknown"; expired POST leases become CHECK_PENDING and reconcile against BRAIN; explicit READY/RETRY backoff plus an EXHAUSTED attempt budget; ACTIVE identity comes from the stored candidate row. |
| P9 local self-correlation | ✅ | `scripts/correlation.py`: fully paginated + versioned ACTIVE book, locally cached daily PnL, aligned daily-return checks persisted per candidate (`max_corr`, `max_corr_alpha_id`, `corr_checked_at`, `active_set_version`, `corr_status`); a real submission gate behind `--require-correlation`. |
| P5 staged search | ✅ volume-gated | `scripts/staged_search.py`: evidence-derived per-structure budgets above `--staged-threshold`, expand on proof, stop saturated families; spend recorded in `structure_budget`. |
| P10 heuristic ranking | ✅ baseline | `scripts/ranking.py` already scores quality/novelty/information/diversity/risk. Keep it heuristic until P11. |

Do not expand these sections again unless a regression or design change requires work.

Caveats kept on purpose:

- P8 reconciliation trusts BRAIN's alpha status plus the submit endpoint's checks; a POST that BRAIN never registered resolves to READY, with the platform's own 404-on-resubmit as the final backstop.
- P9 is a local pre-gate only — BRAIN's own SELF_CORRELATION remains the confirmation. An ACTIVE book with uncached PnL holds every candidate rather than reporting a false low correlation.
- P5 still has no mass candidate generator, so no fixed 2000→800 style ratios were added; budgets are evidence-derived and only engage above the volume threshold.

---

# Priority 1 — Open issue #1: agent-agnostic self-evolving knowledge + skills (P12)

Tracking issue: [#1 — Build agent-agnostic self-evolving knowledge + skill system](https://github.com/Uwater1/wq-alpha-research/issues/1)

Status: ⏳ open. This replaces the old small “append lessons to SKILL.md” concept.

Important current problem: the latest research flow still appends large dated campaign records into root `SKILL.md`. That directly conflicts with the goal of keeping prompt-loaded context terse.

### P12.1 — Storage + compatibility

Extend `research.db` with structured knowledge, not another agent-specific store:

- observations / evidence;
- scoped rules;
- rule proposals;
- support vs contradiction counts;
- lineage/mutation outcomes;
- family aggregates;
- provenance;
- privacy class;
- lifecycle state.

Keep existing simulation/submission behavior and legacy export compatibility.

### P12.2 — Terse skill + retrieval

Refactor root `SKILL.md` into a short procedure only:

1. scope;
2. research workflow;
3. generation rules;
4. validation/simulation gates;
5. correlation/submission gates;
6. recall command;
7. learning command;
8. privacy/safety;
9. pointers to references.

Move dated campaign logs, raw experiment records, large field/operator material, and rarely needed examples out of root `SKILL.md`.

Start retrieval with SQLite FTS5 + structured filters; embeddings are optional later.

### P12.3 — Proposal-based learning

Replace direct append/update behavior with:

`events -> scoped observations -> aggregate evidence -> rule proposal`

Each learned rule must carry:

- scope;
- supporting evidence;
- contradicting evidence;
- provenance;
- independence/grouping information;
- status: proposed / active / weakened / retired / pinned.

One simulation result must not become a global instruction.

### P12.4 — Safe skill manager

All skill mutations go through one manager:

- expected-SHA/version guard;
- atomic write;
- mutation ledger;
- content-addressed backup;
- diff;
- rollback;
- user-owned/pinned rule protection;
- privacy checks before compiling tracked files.

### P12.5 — Universal CLI

Core correctness must not depend on Codex, Pi, OpenCode, Hermes, MCP, etc.

Expose stable commands such as:

```bash
python -m wq status --json
python -m wq candidate ...
python -m wq simulate ...
python -m wq recall ... --json
python -m wq knowledge rules --json
python -m wq knowledge evidence <rule-id> --json
python -m wq learn review
python -m wq learn proposals
python -m wq learn evaluate <proposal-id>
python -m wq learn promote <proposal-id>
python -m wq learn reject <proposal-id>
python -m wq skill validate
python -m wq skill history
python -m wq skill rollback <mutation-id>
```

Optional agent integrations must wrap the same service layer and contain no unique logic.

### P12.6 — Privacy classes

At minimum:

- `PUBLIC`
- `SANITIZED`
- `PRIVATE`
- `SECRET`

Credentials must never enter the learning store. Exact private expressions, account-linked alpha IDs/PnL, and submission history must not be compiled into public git-tracked skill files.

### P12.7 — Evaluation-gated promotion

Before autonomous promotion:

- replay historical research;
- compare candidate rule/skill vs current baseline;
- measure ranking/search utility, regressions, and privacy violations;
- promote only on configured criteria;
- automatically reject/retire harmful rules.

### Acceptance for issue #1

- a fresh shell-capable agent can operate from `AGENTS.md` + terse `SKILL.md`;
- raw experiments stay outside prompt-loaded skill;
- rules are scoped, evidenced, versioned, auditable, reversible;
- conflicting evidence can weaken/retire a rule;
- concurrent agents cannot overwrite stale skill state;
- private data cannot be promoted into public skill/reference files;
- no specific agent runtime is required for correctness.

---

# Priority 2 — Learned simulation surrogate (P11)

Status: ⏳ wait for enough clean history.

Train ranking models from:

- AST/operator structure;
- fields/categories/coverage;
- windows/settings;
- signal family;
- lineage/mutation features;
- parent outcomes;
- local correlation/diversification.

Targets may include Sharpe, Fitness, Turnover, IS pass/fail, common failure checks, and submission-readiness.

Primary objective: **ranking quality / top-candidate recall**, not exact metric prediction.

Do not hard-reject solely from model prediction at first.

---

# Priority 3 — Complete observability (P13)

Status: ⚠️ partial.

Existing DB events and `status` counters are useful groundwork, but full BRAIN interaction observability is missing.

Add:

- operation + timestamp;
- candidate/simulation/submission identifiers;
- HTTP status/category;
- retry count;
- latency;
- rate-limit/backoff events;
- result class.

Track at least:

- generated/hour;
- validated/hour;
- simulated/hour;
- cache-hit rate;
- simulation success rate;
- IS pass rate;
- correlation pass rate;
- submission success rate;
- ACTIVE/week;
- BRAIN simulations per IS_PASS;
- BRAIN simulations per ACTIVE.

---

# Priority 4 — Keep tests aligned with active work (P14)

Status: ⚠️ broad coverage exists; extend with each active phase.

Done: P8 uncertain-submit/retry/restart cases, P9 pagination/cache/staleness/correlation
cases, the non-default ACTIVE canonical identity regression, and P5 staged-budget cases.

Still required:

- P12 rule evidence/proposal/promotion/rollback/concurrency/privacy cases;
- mocked end-to-end flow:
  `generate -> queue -> simulate -> correlate -> submit -> ACTIVE -> learn`.

No default test may require live credentials. Live BRAIN probes remain explicit/manual.

---

# Implementation order

```text
1. Issue #1 / P12 storage + terse skill + retrieval
2. P12 proposal learning + safe mutation + eval gate
3. P11 surrogate model
4. P13 observability + P14 regression coverage throughout
5. GitHub Actions submission automation — LAST
```

---

# LAST / DEFERRED — GitHub Actions submission worker (old P7)

Status: 💤 deliberately postponed.

Do not build unattended GitHub-hosted submission until P8/P9 are proven and persistent/private state has a safe home.

Requirements before enabling:

- P8 uncertain-submit recovery is tested;
- P9 stale-correlation gate is mandatory;
- canonical queue state is **not** runner-local SQLite;
- private/account-linked research state is not committed;
- secrets exist only in an approved secret store;
- run is bounded by `MAX_SUBMISSIONS_PER_RUN`, runtime, and polling budget;
- duplicate overlapping runs are harmless.

Preferred order of deployment:

1. local/manual worker;
2. persistent self-hosted runner or private service;
3. only then consider GitHub-hosted Actions with a private persistent backend.

If GitHub Actions is eventually used:

```text
workflow_dispatch first
-> low-frequency schedule later
-> MAX_SUBMISSIONS_PER_RUN=1 initially
```

A compiled binary does **not** make credentials safe by itself; the runtime still needs access to the secret. Do not rely on obfuscation as a security boundary.

---

## Maintenance rule

Keep this file as the **active roadmap**, not a historical design document.

When work is completed:

- collapse it to one short row in “Completed baseline”;
- keep only unresolved regressions/caveats;
- link detailed open work to a GitHub issue instead of duplicating a full issue body here.
