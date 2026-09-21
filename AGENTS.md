# AGENTS.md — Repository Operating Guide

This repository is an agent-agnostic WorldQuant BRAIN research system.

Use the files by role:

- `AGENTS.md` — how to operate, test, and change the repository.
- `SKILL.md` — the compact alpha-research playbook and decision gates.
- `research.db` — local operational state, observations, evidence, rules, and audit history.
- `references/` — machine-readable field/operator snapshots and other detailed reference data.

Do not duplicate the full research playbook in this file, and do not turn `SKILL.md` into an experiment log.

## 1. Environment and secrets

Use the repository virtual environment:

```bash
./.venv/bin/python <script.py>
```

Canonical BRAIN credential sources are:

1. `WQ_BRAIN_USERNAME` / `WQ_BRAIN_PASSWORD`;
2. local encrypted `credential.txt` + `credential.key`.

Useful checks:

```bash
./.venv/bin/python scripts/credential_crypto.py --status
./.venv/bin/python scripts/credential_crypto.py --encrypt
```

Never print, inspect, commit, or copy credential material into prompts, logs, tests, `research.db`, tracked Markdown, or issue/PR text. Treat `credential.txt` and `credential.key` as opaque secrets. Legacy credential formats exist only for `legacy/wq_brain/` compatibility.

## 2. Sources of truth

`research.db` is the durable local source of truth for candidates, simulations, submissions, ACTIVE-book state, transport events, and learned knowledge. It is git-ignored and may contain account-linked/private research state.

Candidate identity is the canonical hash of normalized expression + settings (`scripts/canonical.py`). Reuse the database/cache instead of re-sending identical work to BRAIN.

`alpha_db.json` and `scripts/evolve_skill.py` are legacy compatibility/reporting paths. They are not the canonical learning system.

Tracked `SKILL.md` is reviewed public/SANITIZED guidance. Runtime research events must not be appended to it automatically. If `SKILL.md` itself needs a durable rule change, make that as a reviewed Git change/PR after the rule is evidenced and approved.

## 3. Standard research workflow

Start with local state and learned guidance:

```bash
./.venv/bin/python scripts/research_db.py status
./.venv/bin/python scripts/knowledge_cli.py status
./.venv/bin/python scripts/knowledge_cli.py recall "cash flow" --max-privacy SANITIZED
```

Queue validated candidates and inspect ranking before spending BRAIN capacity:

```bash
./.venv/bin/python scripts/research_db.py queue PATH.csv
./.venv/bin/python scripts/sim_scheduler.py --dry-run
./.venv/bin/python scripts/sim_scheduler.py --max-runtime 30
```

The scheduler uses the persistent queue/cache, bounded leases, retry/backoff, staged search, successive halving, and advisory ranking/surrogate signals. It keeps ordinary simulation work restart-safe. `scripts/multi_sim.py --status` reports whether multi-simulation is available; do not assume it is.

Before submission, sync the complete ACTIVE book and require a fresh local correlation gate:

```bash
./.venv/bin/python scripts/correlation.py sync
./.venv/bin/python scripts/correlation.py status
./.venv/bin/python scripts/submission_worker.py --dry-run
./.venv/bin/python scripts/submission_worker.py --require-correlation --max-submissions 1
```

Submission invariants:

- local correlation uses aligned **daily returns**, never cumulative PnL;
- the ACTIVE list must be fully paginated and fresh;
- missing/short/flat/incomplete/stale correlation data is a hold, not a pass;
- an ambiguous non-idempotent submission POST must reconcile against BRAIN before another POST;
- `201`/accepted is not success; only `status == ACTIVE` is final success;
- BRAIN `SELF_CORRELATION` remains the platform confirmation.

## 4. Knowledge and learning

The canonical learning path is:

```text
events -> scoped observations -> evidence-backed rule proposal
       -> evaluation -> active/pinned rule
```

Use the agent-agnostic CLI:

```bash
./.venv/bin/python scripts/knowledge_cli.py rules --state active
./.venv/bin/python scripts/knowledge_cli.py recall "analyst turnover" --max-privacy SANITIZED

./.venv/bin/python scripts/knowledge_cli.py observe \
  --subject-type signal_structure --subject-key STRUCTURE \
  --claim simulation_outcome --value '{"is_pass":true}' \
  --scope '{"region":"USA","universe":"TOP3000","delay":1}' \
  --evidence-group CAMPAIGN_OR_LINEAGE

./.venv/bin/python scripts/knowledge_cli.py propose \
  --title "Scoped rule" --body "General sanitized guidance" \
  --scope '{"region":"USA","universe":"TOP3000","delay":1}' \
  --evidence OBS_ID:support

./.venv/bin/python scripts/knowledge_cli.py evaluate RULE_ID
./.venv/bin/python scripts/knowledge_cli.py transition RULE_ID active --expected-version VERSION
```

Rules:

- observations are PRIVATE by default;
- evidence must be scoped and grouped by an independence boundary so parameter clones do not manufacture confidence;
- one simulation is not a global rule;
- contradiction is first-class evidence;
- `recall` may surface proposed rules; treat them as hypotheses. `active`/`pinned` rules are durable guidance;
- user-owned/pinned guidance must not be autonomously rewritten;
- credentials/SECRET data never enter the learning store;
- only PUBLIC/SANITIZED material may enter tracked documentation.

`scripts/evolve_skill.py` may be used only as legacy preview/reporting when needed. Do **not** use `evolve_skill.py --apply` as the learning workflow, and never use `--raw` to write tracked files.

Do not rely on agent-specific memory, MCP, Codex, Pi, OpenCode, Hermes, or another runtime for correctness. The stable interface is files + SQLite + shell commands.

## 5. Validation, ranking, and surrogate

`scripts/validate.py` rejects malformed expressions, invalid settings, and unknown fields/operators within the local catalog scope before a simulation slot is spent. Warnings should lower priority rather than silently change semantics.

`scripts/ranking.py` ranks candidates using interpretable quality/novelty/diversity/risk components. `scripts/surrogate.py` is advisory only:

```bash
./.venv/bin/python scripts/surrogate.py status
./.venv/bin/python scripts/surrogate.py train --min-samples 5
./.venv/bin/python scripts/surrogate.py evaluate
./.venv/bin/python scripts/surrogate.py rank --limit 20
```

A model prediction may reorder candidates; it must not be the sole reason to reject one.

## 6. Tests and changes

Run the default offline suite before merging repository changes:

```bash
./.venv/bin/python -m pytest -q
```

Default tests must remain credential-free and network-free. Live BRAIN probes are explicit, bounded, manual operations.

For tracked code/docs, use normal Git branches/PRs. Do not allow multiple agents to concurrently mutate the same tracked skill file outside Git review.

## 7. Local/private files

Never commit or publish:

- `credential.txt`, `credential.key`, `.env`;
- `research.db`, `research.db-wal`, `research.db-shm`;
- `alpha_db.json`, `batch_submit_results.json`;
- `.skill-history/`;
- `legacy/wq_brain/data/*` generated account-linked outputs;
- raw alpha IDs, exact private expressions, PnL series, or submission history.

## 8. Legacy tooling

`legacy/wq_brain/` is retained for compatibility and historical utilities. New queue/simulation/submission work should use the modern `scripts/` pipeline unless a legacy tool is specifically required.

When touching legacy behavior, preserve its credential privacy and generated-output ignore rules, and run the relevant offline tests before using it live.
