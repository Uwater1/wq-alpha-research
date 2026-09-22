---
name: wq-alpha-research
description: "Use for WorldQuant BRAIN alpha research: field and operator selection, FASTEXPR design, simulation diagnosis, IS gates, low-correlation portfolio construction, submission, and evidence-backed research learning."
---

# WQ Alpha Research Skill

Compact playbook for WorldQuant BRAIN research. Default empirical scope is **USA / TOP3000 / delay=1** unless the task explicitly says otherwise. Treat scoped empirical guidance as a prior, not a universal law.

Repository operation, credentials, tests, and command details live in `AGENTS.md`.

## 1. Research loop

1. **Inspect local state and recall relevant knowledge.** Do not rediscover a family the database already shows is crowded or repeatedly failing.
2. **Verify fields/operators locally.** The bundled snapshots are authoritative only for their recorded scope.
3. **Start with one interpretable structure.** Validate and simulate a representative before expanding a parameter grid.
4. **Use the persistent queue/cache.** Reuse exact completed requests; do not spend BRAIN capacity on duplicates.
5. **Diagnose the actual failing gate.** Change the signal only when signal quality is the problem; use decay/conditioning when turnover is the problem.
6. **Sync the complete ACTIVE book and compute aligned daily-return correlation.**
7. **Submit only after all local gates pass.** Re-check BRAIN until the alpha is actually `ACTIVE`.
8. **Record evidence in `research.db`.** Research outcomes become scoped observations/rule proposals, not appended experiment prose in this file.

Useful entry points:

```bash
./.venv/bin/python scripts/knowledge_cli.py recall "QUERY" --max-privacy SANITIZED
./.venv/bin/python scripts/research_db.py status
./.venv/bin/python scripts/sim_scheduler.py --dry-run
./.venv/bin/python scripts/correlation.py status
./.venv/bin/python scripts/submission_worker.py --dry-run
```

## 2. Field and candidate design

Local data references:

- `references/wq_usa_top3000_delay1_data_fields.json` — field metadata for the default scope.
- `references/wq_usa_top3000_delay1_data_fields.csv` — tabular copy.
- `references/wq_usa_top3000_delay1_data_fields_summary.json` — category summary.
- `references/wq_operators.json` — operator snapshot.

Before using a field, search the local catalog and validate a minimal expression such as `rank(field)`. Refresh or fetch a different catalog when region, universe, delay, or the platform catalog changes.

### Strong baseline

For slow fundamental/analyst signals, a useful starting shape is:

```fastexpr
group_rank(ts_rank(signal, N), subindustry)
```

Use it as a baseline, not a template to mine indefinitely. Vary **economic logic or data source** before spending slots on many windows, transforms, weights, or neutralizations of the same idea.

Practical priors for the default scope:

| Signal type | Starting bias |
|---|---|
| Fundamental / analyst | group normalization + medium time-series rank; low/moderate decay |
| Technical / fast price-volume | stronger smoothing/decay; watch turnover |
| Sentiment / sparse event data | `nanHandling=ON`; validate coverage and turnover carefully |
| Mixed | combine genuinely different return drivers, not cosmetic variants of one family |

Prefer simple expressions with a clear hypothesis. Deep nesting, parameter clones, and high-dimensional grids consume capacity without necessarily adding information.

## 3. Simulation and IS gates

When BRAIN returns explicit IS checks, those checks are primary. Local fallback defaults in the modern pipeline are:

| Gate | Local default / target |
|---|---|
| Sharpe | **>= 1.25**; prefer >= 1.5 |
| Fitness | **>= 1.10** |
| Turnover | **1%–20%** |
| Drawdown | target < 15% |
| Concentration | BRAIN `CONCENTRATED_WEIGHT` must pass |
| Sub-universe | BRAIN sub-universe check must pass where applicable |

Common diagnosis:

| Failure | First response |
|---|---|
| LOW_SHARPE | change field/economic hypothesis; then window/grouping |
| LOW_FITNESS with very low TO | improve return profile; lowering TO further may not help |
| HIGH_TURNOVER | increase decay/smoothing, use conditioning such as `trade_when`, or use a slower signal |
| LOW_TURNOVER | shorten horizon or use a more responsive field only if the signal still makes economic sense |
| CONCENTRATED_WEIGHT | normalize/rank, backfill sparse data, review truncation and coverage |
| LOW_SUB_UNIVERSE_SHARPE | reduce small-cap/liquidity dependence; use robust group normalization |
| simulation/operator error | verify field, operator arity, settings, and nested keyword syntax |

Fitness uses `max(turnover, 0.125)` in its turnover term, so once turnover is below roughly 12.5%, reducing turnover alone does not improve that denominator. A low-turnover LOW_FITNESS failure usually needs more return/Sharpe, not more smoothing.

## 4. Correlation and portfolio gate

Correlation policy is strict:

- use **aligned daily returns**, never cumulative PnL;
- compare against the **complete, fresh ACTIVE book**;
- missing, short, flat, incomplete, or stale data is a **hold**, not a low correlation;
- default local limit is `abs(corr) < 0.70`.

A candidate at/above 0.70 may use the configured Sharpe exception only when its Sharpe is at least **1.10x** the Sharpe of the specific ACTIVE alpha it correlates with most. The correlated alpha's Sharpe must be known; unknown means hold. Do not compare against a book average and do not round near the boundary.

BRAIN `SELF_CORRELATION` remains the final platform check.

For diversification, change the **return path**: data source, economic mechanism, event timing, or exposure. Different windows/transforms/weights inside one field family often remain the same bet.

## 5. Submission discipline

Use the submission worker rather than ad-hoc POST loops.

A submit response only means the request was accepted for evaluation. Success is:

```text
alpha.status == ACTIVE
```

Non-idempotent submission safety is mandatory: if the POST outcome is ambiguous, reconcile the alpha/check state before another POST. Never blindly retry an uncertain submission.

Passing standalone metrics is insufficient if the candidate duplicates the existing book. Prefer a somewhat weaker but genuinely different candidate over a near-clone only when it still clears all required gates.

## 6. Search efficiency and ranking

Treat a parameter grid as **one idea**. Simulate a representative, then expand only when evidence justifies it. The repository's successive-halving/staged-search logic exists to enforce this.

Prioritize:

1. required gate probability / expected quality;
2. new structure or economic information;
3. family and portfolio diversity;
4. low duplication and failure risk.

`scripts/surrogate.py` is advisory. Its prediction may change order, never become a hard rejection gate by itself.

A new dataset is not automatically an orthogonal signal. First establish usable Fitness/Sharpe, then test whether the realized return path is actually different.

## 7. Evidence-backed learning

Canonical learning flow:

```text
events -> scoped observations -> independent evidence
       -> proposed rule -> evaluation -> active/pinned rule
```

Before researching a familiar problem, recall local knowledge:

```bash
./.venv/bin/python scripts/knowledge_cli.py recall "analyst high turnover" \
  --scope '{"region":"USA","universe":"TOP3000","delay":1}' \
  --max-privacy SANITIZED
```

When recording a reusable finding:

- scope it by region/universe/delay and relevant dataset/family/settings;
- group related parameter variants under one independence group;
- record contradictions as well as support;
- keep account-linked/raw evidence PRIVATE;
- require evaluation before promotion.

`recall` may include **proposed** rules. Treat them as hypotheses. Only ACTIVE/PINNED rules are durable research guidance.

Do **not** append simulation reports, campaign logs, alpha IDs, or generated lessons directly to `SKILL.md`. Do not use `scripts/evolve_skill.py --apply` as the learning mechanism. Durable skill changes belong in reviewed tracked documentation only after evidence supports them.

## 8. Privacy and safety

Never place these in tracked skill/reference text:

- credentials or authentication material;
- exact private alpha IDs;
- exact private expressions;
- account-linked PnL;
- submission history or other account-specific records.

The learning store rejects SECRET material; PUBLIC/SANITIZED information is the only class eligible for tracked documentation.

Core correctness must remain agent-agnostic: shell + files + SQLite are sufficient. Agent-specific memory, plugins, MCP, or runtime features may assist but must not contain unique logic.

## 9. Scoped empirical priors

The following are compressed priors from prior **USA/TOP3000/D1** research. Re-check them as new evidence accumulates:

- `group_rank + ts_rank` is a strong baseline for many slow fundamental/analyst signals, not a guarantee.
- Repeated transform/window/neutralization changes inside one economic family often preserve high correlation; change the underlying driver instead.
- Profitability/estimate-yield variants can achieve strong IS metrics while still crowding the same return family.
- Price-like denominators have often improved the return/Fitness profile of profitability-style numerators versus purely balance-sheet denominators.
- Higher decay is an effective turnover lever but commonly trades away some Sharpe.
- A family failing only a repairable concentration/coverage check is usually more promising to revisit than a family repeatedly failing LOW_FITNESS.
- Low correlation without adequate Sharpe/Fitness has little portfolio value.

For current evidence, query `research.db`; do not grow this section with chronological campaign entries.

## 10. Code and reference map

Use deterministic code rather than copying API examples into prompts:

- `scripts/validate.py` — field/operator/settings screening.
- `scripts/generator.py` — deterministic catalog coverage and failure-directed mutations.
- `scripts/archive.py` — quality-diversity archive and family allocation.
- `scripts/robustness.py` — advisory search-aware diagnostics.
- `scripts/field_intelligence.py` — empirical field/operator coverage.
- `python -m wq generate|mutate` — stable candidate-generation CLI.
- `scripts/research_db.py` — queue/cache/state machine/knowledge store.
- `scripts/sim_scheduler.py` — bounded persistent simulation dispatcher.
- `scripts/correlation.py` — ACTIVE-book sync and daily-return correlation.
- `scripts/submission_worker.py` — gated, recoverable submission.
- `scripts/ranking.py` / `scripts/surrogate.py` — advisory candidate ordering.
- `scripts/knowledge_cli.py` — recall, observations, proposals, evaluation, lifecycle.
- `AGENTS.md` — environment, secrets, operational commands, testing, and repository-change rules.

## 11. Dataset/operator coverage protocol

The local catalog is a **catalog, not proof**: it covers USA/TOP3000/delay=1 with 4,367 fields across 14 dataset families and 66 operators. Before calling a family or operator validated, search the local references, choose up to three representative fields/structures, run static validation, then simulate; submit at most the strongest candidate after IS, correlation, and status gates pass. Record coverage as `cataloged`, `validated`, `submitted`, `ACTIVE`, or `rejected`, with the field family, operator shape, settings, and sanitized outcome. Skip already-tested families unless the goal is a deliberate re-test. Never infer that an untested field works because another field in its dataset works, and never infer that a successful simulation became ACTIVE without checking status. The 2026-09-20 coverage probe found that Fundamental Scores, Relationship Data, and Volatility Data fields were accepted by the API but the sampled structures failed IS metrics; Universe Dataset fields were rejected as `Unit[Universe]` where a group operator required `Unit[Group]`. These are API/type and performance findings, not proof that every field in those families fails.
