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

- Preview skill evolution (does NOT modify files):

  ```bash
  ./.venv/bin/python scripts/evolve_skill.py
  ```

- Apply (writes to `SKILL.md` Section 12 + `alpha_db.json`):

  ```bash
  ./.venv/bin/python scripts/evolve_skill.py --apply
  ```

- Local research state store (`research.db`, git-ignored — TODO P0/P1):

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
