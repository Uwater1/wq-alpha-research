# AGENTS.md — Working Guide for This WQ Alpha Research Repo

Project root: `wq-alpha-research/` (contains `SKILL.md`, `scripts/`, `references/`).
This is the only directory that matters for this project.

## 1. Environment

- Python venv at `.venv/` (already created). Always use it:

  ```bash
  ./.venv/bin/python <script.py>
  ```

- Dependencies (`requests`, `numpy`) are already installed in the venv.

## 2. Credentials (never commit)

Resolved by `scripts/evolve_skill.py` and `legacy/wq_brain/wq_session.py` in order:

1. Env vars: `WQ_BRAIN_USERNAME` / `WQ_BRAIN_PASSWORD`
2. Root `credential.txt` encrypted with `credential.key` (or plaintext JSON array `["your_username", "your_password"]`)
3. `legacy/wq_brain/credentials.json` (JSON object, legacy format)

```bash
./.venv/bin/python scripts/credential_crypto.py --status   # check, reveals nothing
./.venv/bin/python scripts/credential_crypto.py --encrypt  # encrypt in-place, creates credential.key (0600) if missing
```

Session/submit scripts decrypt in memory. **Agents must never view or print
`credential.txt` / `credential.key` — treat as opaque secrets.**


## 3. How to Run Things

- Agent-agnostic knowledge and safe skill management:

  ```bash
  ./.venv/bin/python scripts/knowledge_cli.py status
  ./.venv/bin/python scripts/knowledge_cli.py recall "cash flow" --max-privacy SANITIZED
  ./.venv/bin/python scripts/knowledge_cli.py observe --subject-type signal_structure --subject-key STRUCTURE \\
      --claim simulation_outcome --value '{"is_pass":true}' --evidence-group campaign-1
  ./.venv/bin/python scripts/knowledge_cli.py propose --title "General rule" --body "Sanitized rule text" \\
      --evidence OBS_ID:support
  ./.venv/bin/python scripts/knowledge_cli.py evaluate RULE_ID
  ./.venv/bin/python scripts/knowledge_cli.py transition RULE_ID active --expected-version 1
  ```

  Observations are scoped and private by default. Rules require independent evidence and
  evaluation before promotion; `skill_manager.py` enforces SHA compare-and-swap, atomic
  content-addressed backups, rollback, and PUBLIC/SANITIZED-only tracked mutations.

- Preview skill evolution (does NOT modify files):

  ```bash
  ./.venv/bin/python scripts/evolve_skill.py
  ```

- Apply (writes to `SKILL.md` Section 12 + `alpha_db.json`):

  ```bash
  ./.venv/bin/python scripts/evolve_skill.py --apply
  ```

- Local research state store (`research.db`, git-ignored):

  ```bash
  ./.venv/bin/python scripts/research_db.py init                     # create/upgrade schema
  ./.venv/bin/python scripts/research_db.py status                   # queue + cache counters as JSON
  ./.venv/bin/python scripts/research_db.py queue legacy/wq_brain/data/input.csv
  ./.venv/bin/python scripts/research_db.py cache "rank(close)" --decay 6
  ```

  `WQ_RESEARCH_DB` overrides the default `<repo root>/research.db`. Candidates are
  keyed by `SHA256(normalized_expression + settings)` (see `scripts/canonical.py`), so
  an identical request is never sent to BRAIN twice; `batch_simulate.py` uses the
  store by default (`--no-db` for the legacy CSV-only behavior).

- Learned simulation surrogate (advisory only):

  ```bash
  ./.venv/bin/python scripts/surrogate.py status
  ./.venv/bin/python scripts/surrogate.py train --min-samples 5
  ./.venv/bin/python scripts/surrogate.py evaluate
  ./.venv/bin/python scripts/surrogate.py rank --limit 20
  ```

  It learns from settled local candidates using structural fields/operators, settings,
  signal family, lineage, and outcomes. It only reorders candidates; it never rejects a
  candidate solely from model prediction.

- Persistent 3-slot simulation dispatcher. Queue work first, then run it:

  ```bash
  ./.venv/bin/python scripts/sim_scheduler.py --dry-run         # rank the queue, no BRAIN calls
  ./.venv/bin/python scripts/sim_scheduler.py --max-runtime 30  # bounded run in minutes
  ./.venv/bin/python scripts/sim_scheduler.py --once            # one fill+poll pass (cron style)
  ```

  It keeps 3 simulations in flight, polls separately from submitting, honours
  `Retry-After`, backs off, re-authenticates on session expiry, adopts simulations
  orphaned by a killed process, and persists every transition. Priority comes from
  `scripts/ranking.py` (quality/novelty/diversity/risk, components stored per
  candidate). Multi-simulation is **not** available on this platform — check with
  `./.venv/bin/python scripts/multi_sim.py --status`.

- Pre-screening, variant gate, submission queue:

  ```bash
  ./.venv/bin/python scripts/research_db.py queue data/input.csv   # validates + dedups first
  ./.venv/bin/python scripts/successive_halving.py --status        # what is deferred and why
  ./.venv/bin/python scripts/staged_search.py --status             # per-structure search budgets
  ./.venv/bin/python scripts/submission_worker.py --dry-run        # submission queue order
  ./.venv/bin/python scripts/submission_worker.py --max-submissions 1
  ```

  `scripts/validate.py` rejects malformed expressions, unknown operators/fields (within
  the USA/TOP3000/delay 1 catalog scope) and impossible settings before a slot is spent;
  warnings only lower priority. `scripts/successive_halving.py` admits one representative
  variant per structure and defers the rest until it passes (or the horizon elapses).
  `scripts/staged_search.py` additionally budgets each structure once the queue passes
  `--staged-threshold`, so a parameter grid holds one slot until its base proves useful,
  expands when it does, and stops when its marginal pass rate goes bad. Passing candidates
  land in the submission queue automatically; the worker leases one, re-checks the gates,
  submits, polls and continues.

- Offline end-to-end regression coverage is in `tests/test_pipeline_e2e.py` and uses only fakes.
  Run `./.venv/bin/python -m pytest -q` before any live probe. Live BRAIN probes are explicit,
  bounded, and never part of the default test suite.

- Local self-correlation + submission recovery:

  ```bash
  ./.venv/bin/python scripts/correlation.py sync    # paginated ACTIVE book + cached daily PnL
  ./.venv/bin/python scripts/correlation.py status  # book version, cached PnL, check counts
  ./.venv/bin/python scripts/submission_worker.py --require-correlation --max-submissions 1
  ```

  `research.db` events also retain operation, HTTP status/category, retry count, latency,
  rate-limit/backoff seconds, and result class without response bodies or credentials;
  `research_db.py status` reports throughput and pass-rate metrics.

  `--require-correlation` is a real gate: the worker syncs and versions the ACTIVE book,
  fetches the candidate's PnL, correlates aligned **daily returns** (never cumulative
  curves) and refuses anything at or above `--correlation-limit`. Unusable inputs (missing
  or short PnL, a flat series, an incompletely cached book) become explicit holds, not low
  correlations; a book change invalidates every cached check. Submission rows recover
  themselves: an expired lease returns to `READY` only when the POST never left the
  process, otherwise it becomes `CHECK_PENDING` and is reconciled against BRAIN before any
  retry. Ambiguous transport/server submit outcomes also become `CHECK_PENDING`; only
  definite pre-submit errors use `RETRY`. A failed ACTIVE-book refresh holds the whole
  correlation-enabled run rather than trusting an old cached check. Transient failures
  back off and retire as `EXHAUSTED` after `--max-submission-attempts`. BRAIN's own
  SELF_CORRELATION check remains the confirmation.

- Load the local field catalog (4,367 USA TOP3000 delay=1 fields):

  ```python
  import json
  from pathlib import Path
  data = json.loads(Path("references/wq_usa_top3000_delay1_data_fields.json").read_text(encoding="utf-8"))
  # f["id"], f["category"]["id"], f["dataset"]["name"], f["coverage"], f["alphaCount"]
  ```

- Load the operator reference (66 ops, `GET /operators` snapshot):

  ```python
  ops = json.loads(Path("references/wq_operators.json").read_text(encoding="utf-8"))
  # o["name"], o["category"], o["scope"], o["definition"], o["description"]
  ```

  Refresh it rarely (operators barely change):

  ```bash
  ./.venv/bin/python scripts/fetch_operators.py
  ```

## 4. Workflow (from SKILL.md — read it before doing alpha research)

1. Search/verify a field locally (`references/*.json`) before using it in an
   expression; test with a simple `rank(field)` simulation first.
2. Build candidate expressions from `SKILL.md` Section 4 (templates: `group_rank
   + ts_rank`, SUBINDUSTRY neutralization is the default baseline).
3. Simulate on BRAIN (`POST /simulations`), check IS metrics against Section 5
   thresholds: Sharpe >= 1.25, Fitness >= 1.1, Turnover 1–20%, DD < 15%.
4. Compute **daily-return** correlation vs. existing ACTIVE alphas (not cumulative
   PnL); abs(corr) >= 0.7 → discard or rebuild.
5. Submit, then re-verify `status == ACTIVE` (a 201 response is not success).
6. After any BRAIN interaction, run `evolve_skill.py` (preview → review → `--apply`)
   to distill lessons back into `SKILL.md`.

## 5. Legacy WQ-Brain tooling (`legacy/wq_brain/`)

The old WQ-Brain project (AbnerTeng/WorldQuant-Brain, 2023) is merged here with
fixed bugs — see `legacy/wq_brain/README.md`. Shared session in
`legacy/wq_brain/wq_session.py` resolves credentials from (in order):
env vars `WQ_BRAIN_USERNAME`/`WQ_BRAIN_PASSWORD` → root `credential.txt`
(JSON array) → `legacy/wq_brain/credentials.json` (JSON object, old format).
All three are git-ignored.

```bash
# Self-test credential resolution + login
./.venv/bin/python legacy/wq_brain/wq_session.py

# Batch-simulate expressions from CSV (results stream to data/results_*.csv)
./.venv/bin/python legacy/wq_brain/batch_simulate.py [input.csv] [--workers 3]

# Scrape UNSUBMITTED alphas that pass all IS checks -> data/scrape_*.csv
./.venv/bin/python legacy/wq_brain/scrape_submittable.py [--min-sharpe 1.3]

# Submit scraped alphas sharpe-first; stops at first SELF_CORRELATION PASS
./.venv/bin/python legacy/wq_brain/submit_from_csv.py data/scrape_<ts>.csv
```

`commands.py` still generates expression batches (101 arxiv alphas, etc.);
run from `legacy/wq_brain/` with the venv python and `sys.path.insert(0, '.')`.
Everything `legacy/wq_brain/data/` produces (CSVs, logs) is git-ignored and
account-linked — never commit or publish it.

## 6. Files That Stay Local (git-ignored)

- `credential.txt` — BRAIN credentials (encrypted)
- `credential.key` — BRAIN encryption key
- `alpha_db.json` — local alpha snapshot / PnL store
- `batch_submit_results.json` — submission results
- `research.db` (+ `-wal`/`-shm`) — candidate queue, simulation cache, submissions

Never commit these. Never publish raw alpha IDs, PnL series, or account-linked
records; only sanitized general rules go into `SKILL.md`.
