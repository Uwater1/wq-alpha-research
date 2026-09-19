# WQ Alpha Research Skill — 4 Days, Zero Human Intervention, From Zero to WorldQuant BRAIN Gold Medal 


<p align="center"><img src="pic/20260626094115.png" width="600"></p>


A self-evolving WorldQuant BRAIN alpha research skill. It helps agents design WQ Alpha expressions, search fields, diagnose simulation failures, check IS metrics, manage self-correlation, and turn each BRAIN interaction into reusable research rules.

The core idea is simple: do not let alpha research stay as scattered one-off prompts. Use the skill to build alphas, test them, inspect failures, compare daily-return correlations, and then distill the useful lessons back into `SKILL.md`.

This repository is a reusable agent skill, not a credential bundle. Keep all account credentials, raw alpha IDs, PnL records, and private candidate expressions local.

## Why This Skill Exists

WQ Alpha mining is not only about writing more expressions. The bottleneck is usually the research loop:

- finding usable fields fast enough;
- avoiding repeated low-Sharpe templates;
- controlling turnover before submission;
- checking SELF_CORRELATION against existing ACTIVE alphas;
- preserving lessons from failed simulations instead of rediscovering them later.

This skill packages that loop into an agent-readable playbook plus scripts. It gives the agent local field context, practical expression templates, IS failure diagnostics, submission checks, and a self-evolution mechanism for turning new runs into better future behavior.

## Mining Effect

The current playbook is built around USA TOP3000 delay=1 and includes a local snapshot of 4,367 BRAIN data fields. It has already distilled several practical mining rules:

- Fundamental fields are usually the strongest starting point; `group_rank + ts_rank` with `SUBINDUSTRY` neutralization is the default baseline.
- Analyst expectation fields are useful but need more turnover control, usually via modest decay and industry/subindustry neutralization.
- Pure technical signals fail more often unless decay is high or they are mixed with slower fundamental signals.
- Fitness failures are often turnover problems in disguise; decay and signal mixing are the first levers to check.
- Self-correlation must be measured on daily PnL changes, not cumulative PnL curves.
- Changing windows, weights, or neutralization rarely creates truly low-correlation alphas by itself; low correlation usually needs a different data source or economic logic.

The goal is not to promise a fixed Sharpe or submission pass rate. The value is a faster, cleaner alpha mining loop: fewer invalid-field attempts, less repeated correlation failure, better default templates, and a growing memory of what worked or failed.

## Self-Evolution Loop

`scripts/evolve_skill.py` is the feedback engine. After simulations, submissions, or status checks, it can fetch the user's alpha list, compare new/changed alphas with the local snapshot, compute daily-return correlations against ACTIVE alphas, and generate a Markdown lesson snippet.

Recommended loop:

1. Generate or modify candidate expressions from `SKILL.md`.
2. Simulate and inspect IS checks on BRAIN.
3. Pull all ACTIVE alphas and compare daily-return correlation.
4. Run `python scripts/evolve_skill.py` to preview new lessons.
5. Review the output manually.
6. Run `python scripts/evolve_skill.py --apply` only for local/private updates.
7. Publish only sanitized general rules, never raw account-linked records.

## Contents

```text
wq-alpha-research/
├── SKILL.md
├── scripts/
│   ├── evolve_skill.py
│   ├── credential_crypto.py
│   ├── fetch_operators.py
│   ├── canonical.py         # expression/settings normalization + cache keys
│   ├── research_db.py       # local research.db: queue, dedup, simulation cache
│   ├── brain_api.py         # BRAIN HTTP client (Retry-After, session refresh)
│   ├── ranking.py           # candidate priority components
│   ├── sim_scheduler.py     # persistent 3-slot simulation dispatcher
│   └── multi_sim.py         # multi-simulation capability check
├── legacy/
│   └── wq_brain/            # batch simulate / scrape / submit tooling
│       ├── wq_session.py
│       ├── batch_simulate.py
│       ├── scrape_submittable.py
│       └── submit_from_csv.py
└── references/
    ├── wq_usa_top3000_delay1_data_fields.csv
    ├── wq_usa_top3000_delay1_data_fields.json
    ├── wq_usa_top3000_delay1_data_fields_summary.json
    └── wq_operators.json    # 66 BRAIN operators (GET /operators snapshot)
```

## What It Helps With

- Search USA TOP3000 delay=1 BRAIN fields locally.
- Look up exact operator signatures locally (`references/wq_operators.json`, 66 ops).
- Build Alpha expressions from common WQ operator patterns.
- Diagnose low Sharpe, low Fitness, high Turnover, concentrated weights, and sub-universe failures.
- Compare candidate alphas against existing ACTIVE alphas using daily-return correlation.
- Automate simulation/submission workflows with explicit post-submit status checks.
- Evolve the skill from local alpha research notes without committing private records.

## Quick Start

Read `SKILL.md` first. It contains the actual playbook and trigger instructions.

For local field lookup:

```python
import json
from pathlib import Path

fields = json.loads(
    Path("references/wq_usa_top3000_delay1_data_fields.json").read_text(encoding="utf-8")
)

keyword = "operating_income"
matches = [
    f for f in fields
    if keyword in f["id"].lower()
    or keyword in (f.get("description") or "").lower()
]
print(matches[:5])
```

## BRAIN Credentials & Agent Security Setup

Before running research scripts or launching AI agents, configure your WorldQuant BRAIN credentials using either method below:

### Option 1: Environment Variables
```bash
export WQ_BRAIN_USERNAME="your_username"
export WQ_BRAIN_PASSWORD="your_password"
```

### Option 2: Encrypted Local File (Recommended for AI Agents)
To prevent AI agents from accidentally viewing your plaintext password when inspecting files, use the zero-dependency encryption layer:

1. Create `credential.txt` at the repo root:
   ```json
   ["your_username", "your_password"]
   ```

2. Encrypt it before starting your agent session:
   ```bash
   ./.venv/bin/python scripts/credential_crypto.py --encrypt
   ```
   *This generates a git-ignored passphrase in `credential.key` (`0600` permissions) and converts `credential.txt` into encrypted ciphertext.*

3. Verify status without revealing secrets:
   ```bash
   ./.venv/bin/python scripts/credential_crypto.py --status
   ```

All tooling scripts (`wq_session.py`, `evolve_skill.py`, etc.) automatically decrypt credentials in memory. Both `credential.txt` and `credential.key` are ignored by git.

## Scripts

Preview skill evolution output without modifying files:

```bash
./.venv/bin/python scripts/evolve_skill.py
```

Apply updates to local `SKILL.md` and `alpha_db.json` after reviewing the preview:

```bash
./.venv/bin/python scripts/evolve_skill.py --apply
```

The generated record is sanitized by default (pseudonymous alpha IDs plus an operator
skeleton instead of the exact expression). `--raw` writes the real IDs and expressions
and is only safe for a private local `SKILL.md`.

Refresh the operator reference (rarely needed — operators barely change):

```bash
./.venv/bin/python scripts/fetch_operators.py
```

Simulate, scrape, and submit a batch of expressions:

```bash
# 1. Simulate every expression in a CSV (results stream to data/results_<ts>.csv)
./.venv/bin/python legacy/wq_brain/batch_simulate.py legacy/wq_brain/data/input.csv --workers 3

# 2. Keep the alphas that pass every IS check
./.venv/bin/python legacy/wq_brain/scrape_submittable.py --min-sharpe 1.3

# 3. Submit sharpe-first and confirm the alpha reaches ACTIVE
./.venv/bin/python legacy/wq_brain/submit_from_csv.py legacy/wq_brain/data/scrape_<ts>.csv
```

Track candidate state locally so an identical simulation is never paid for twice:

```bash
# Normalize + dedup + cache-check candidates from a CSV (no BRAIN calls yet)
./.venv/bin/python scripts/research_db.py queue legacy/wq_brain/data/input.csv

# Queue, cache-hit and submission counters as JSON
./.venv/bin/python scripts/research_db.py status
```

`research.db` is created at the repo root (override with `WQ_RESEARCH_DB`), is ignored
by git, and holds only research state: candidates, simulation cache, submissions,
ACTIVE-alpha bookkeeping and an event log. `batch_simulate.py` uses it by default, so
re-running a batch reuses completed results instead of re-simulating them; pass
`--no-db` for the legacy CSV-only behavior.

Drain a queue with the persistent dispatcher instead of a one-shot batch:

```bash
./.venv/bin/python scripts/sim_scheduler.py --dry-run         # see the priority order
./.venv/bin/python scripts/sim_scheduler.py --max-runtime 30  # keep 3 slots busy for 30 minutes
```

The scheduler refills a freed slot immediately, polls running simulations without
blocking submissions, honours `Retry-After`, re-authenticates when the session
 expires, adopts simulations a killed process left behind, and writes every state
transition to `research.db`. Multi-simulation is not offered by the BRAIN API today
(`scripts/multi_sim.py --status`); the scheduler therefore runs REGULAR simulations
with three concurrent slots, which is the platform limit.

The scripts require `requests` and `numpy`. `alpha_db.json` is a local memory file and is intentionally ignored by git, as are the CSV/log/JSON outputs under `legacy/wq_brain/data/` and the submission record `batch_submit_results.json`.

## Safety Notes

The following files are intentionally ignored:

- `credential.txt` (encrypted ciphertext)
- `credential.key` (random secret key)
- `alpha_db.json`
- `batch_submit_results.json`
- `research.db` (+ `-wal`/`-shm`) — local candidate queue and simulation cache
- `legacy/wq_brain/credentials.json` (legacy credential format)
- `legacy/wq_brain/data/` (account-linked CSV/log/JSON outputs)
- `.env`
- Python caches and virtual environments

If you want to publish research lessons from local runs, summarize them into general rules before adding them to `SKILL.md`. Do not publish raw alpha IDs, PnL series, account-linked submission statuses, or private candidate expressions.

## Get in Touch

Join the QuantML factor-research WeChat group, or scan the personal QR code to add me as a friend.

| WeChat Group: QuantML Factor Research | Add Me as Friend |
| :---: | :---: |
| <img src="pic/20260624124943.jpg" width="300" alt="QuantML Factor Research Group QR"> | <img src="pic/20260624125220.jpg" width="300" alt="Add Friend QR"> |


## License & Attribution

This project is licensed under the [Creative Commons Attribution-NonCommercial 4.0 International License (CC BY-NC 4.0)](https://creativecommons.org/licenses/by-nc/4.0/).

You are free to use, modify, and share the code and content for non-commercial purposes, provided that you clearly cite the source and provide attribution to the original author.

If you use this work in your research, project, or publication, please include a link or reference back to this repository.