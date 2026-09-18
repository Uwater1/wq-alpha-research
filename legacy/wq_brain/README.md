# WQ-Brain (legacy, modernized)

The original code is from https://github.com/AbnerTeng/WorldQuant-Brain (2023).
It has been merged into this repo's tooling with the bugs fixed and the
credential handling unified with the rest of the project.

## What survived and what changed

| Old file | Status | Replacement |
|---|---|---|
| `main.py` | replaced | `batch_simulate.py` — same purpose, but per-row results stream to CSV immediately, 429 backoff, real error messages, worker count = 3 (BRAIN's concurrent limit) instead of 10 |
| `scrape_alphas.py` | replaced | `scrape_submittable.py` — paginates your UNSUBMITTED alphas, keeps only all-IS-checks-PASS, writes sharpe-first CSV |
| `submit_alphas.py` | replaced | `submit_from_csv.py` — submits in CSV order and polls SELF_CORRELATION until PASS |
| `commands.py` | kept as-is | brute-force expression generators (`from_wq_1`, `from_arxiv`, `sample_*`) |
| `database.py` | kept as-is | field/operator lists used by `commands.py` |
| `arxiv.txt` | kept as-is | reference notes for the 101-alpha expressions |

Fixed bugs from the original:
- `scrape_alphas.py` wrote a `fieldnames` list that silently dropped the
  `sharpe` column, so the submission sort key degraded. The new scraper always
  writes `sharpe` and sorts by it.
- The old session wrapped every request in `try/except` that retried forever and
  swallowed real errors (including auth failures). The shared session now
  raises typed errors and backs off properly on 429.
- Credentials no longer live only in `credentials.json` — see below.

## Credentials (all three locations are git-ignored)

The shared session (`wq_session.py`) resolves credentials in this order:

1. `WQ_BRAIN_USERNAME` / `WQ_BRAIN_PASSWORD` environment variables
2. `credential.txt` at the repo root — JSON array: `["user", "pass"]`
3. `legacy/wq_brain/credentials.json` — JSON object (old WQ-Brain format):
   `{"email": "...", "password": "..."}`

Check any of them:
```bash
./.venv/bin/python legacy/wq_brain/wq_session.py
```

## Usage

```bash
# 1. Batch-simulate expressions from a CSV (see data/input.example.csv)
./.venv/bin/python legacy/wq_brain/batch_simulate.py [input.csv] [--workers 3]

# 2. Scrape your UNSUBMITTED alphas that pass all IS checks
./.venv/bin/python legacy/wq_brain/scrape_submittable.py [--min-sharpe 1.3]

# 3. Submit the scraped alphas, sharpe first, stop at first correlation PASS
./.venv/bin/python legacy/wq_brain/submit_from_csv.py data/scrape_<ts>.csv
```

Generate expression batches with the old generators:
```bash
./.venv/bin/python -c "
import sys; sys.path.insert(0, 'legacy/wq_brain')
from commands import from_wq_2, from_arxiv
import csv
with open('legacy/wq_brain/data/input.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['code', 'neutralization', 'decay', 'truncation', 'delay', 'universe', 'region'])
    for c in from_wq_2(): w.writerow([c, 'SUBINDUSTRY', 10, 0.1, 1, 'TOP3000', 'USA'])
"
```

`data/` outputs (`results_*.csv`, `scrape_*.csv`, `*.log`) are git-ignored —
they can contain account-linked alpha records and must not be published.
