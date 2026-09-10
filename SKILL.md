---
name: wq-alpha-research
description: "Use for WorldQuant BRAIN alpha research: designing WQ Alpha expressions, selecting fields and operators, diagnosing simulation and IS check failures, tuning Sharpe, Fitness, and Turnover, submitting alphas, and building low-correlation alpha portfolios."
---

# WQ Alpha Research Skill

> A structured playbook: fields -> expressions -> backtests -> checks -> submissions -> portfolio construction. Built from WorldQuant BRAIN documentation knowledge and USA TOP3000 empirical experience.

## 1. Quick Decision Tree

```
Start
  ├── Pull the full alpha list -> only inspect ACTIVE alphas; compute daily-return correlation; if > 0.7, modify or discard
  ├── Design a new factor
  │    ├── Field verified? -> No -> check Section 2 (local field search / simulate rank(field))
  │    └── Yes
  │         ├── Fundamental -> group_rank + ts_rank, SUBINDUSTRY, decay=0
  │         ├── Analyst -> group_rank + ts_rank, INDUSTRY/SUBINDUSTRY, decay=0-4
  │         ├── Technical -> high decay (10-30) or mix with fundamentals to reduce turnover
  │         └── Sentiment -> nanHandling=ON, use short windows carefully
  └── After submission -> verify status == ACTIVE; otherwise inspect SELF_CORRELATION
```

## 2. Local Field Lookup

This skill includes the full USA TOP3000 delay=1 field catalog (4,367 fields), so there is no need to fetch the list from the web or API every time:

- `references/wq_usa_top3000_delay1_data_fields.json`: full field metadata array
- `references/wq_usa_top3000_delay1_data_fields.csv`: CSV version for Excel or pandas
- `references/wq_usa_top3000_delay1_data_fields_summary.json`: category counts and example fields

Field distribution:

| Category | Count | Notes |
|------|------:|------|
| fundamental | 1652 | Financial statements and footnotes |
| analyst | 1324 | Analyst expectations and consensus |
| news | 996 | News and earnings events |
| pv | 195 | Price/volume, ADV, VWAP, and related data |
| option | 138 | Implied volatility, put/call, and related data |
| model | 40 | Model factors |
| socialmedia | 22 | Social sentiment |
| univ1 | 6 | Universe-related fields |

### 2.1 Search Local Fields

```python
import json
from pathlib import Path

# Assume you are running inside the skill directory; adjust the path if needed.
skill_dir = Path(".")
field_dir = skill_dir / "references"
data = json.loads((field_dir / "wq_usa_top3000_delay1_data_fields.json").read_text(encoding="utf-8"))

keyword = "operating_income"
matches = [
    f for f in data
    if keyword.lower() in f["id"].lower()
    or (f.get("description") and keyword.lower() in f["description"].lower())
]

for f in matches[:10]:
    print(f"{f['id']} | {f.get('category', {}).get('name')} | {f.get('dataset', {}).get('name')} | coverage={f.get('coverage')} | alphaCount={f.get('alphaCount')}")
```

### 2.2 Filter by Category

```python
category = "pv"  # or fundamental / analyst / news / option / model / socialmedia
fields = [f for f in data if f.get("category", {}).get("id") == category]
print(f"{category}: {len(fields)} fields")
for f in sorted(fields, key=lambda x: x.get("alphaCount", 0), reverse=True)[:10]:
    print(f"  {f['id']} | alphaCount={f.get('alphaCount')} | coverage={f.get('coverage')}")
```

### 2.3 Verify a Field

Once you have a candidate field, **first test it with a simple expression** to confirm that it is actually usable:

```python
payload = {
    "type": "REGULAR",
    "settings": {
        "instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000",
        "delay": 1, "decay": 0, "neutralization": "MARKET",
        "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY",
        "nanHandling": "ON", "language": "FASTEXPR", "visualization": False,
    },
    "regular": "rank(my_candidate_field)",
}
resp = session.post("https://api.worldquantbrain.com/simulations", json=payload)
# 201 usually means the field is usable; a non-201 response often means the field does not exist or the parameters are invalid.
```

### 2.4 When to Refresh the Field Snapshot

The local field catalog covers USA TOP3000 delay=1. Refresh it from BRAIN only when:

- the region changes, for example CHN or EUR;
- the universe changes, for example TOP500 or TOP1000;
- the delay changes, for example 0;
- the BRAIN field catalog clearly changes and should be compared against the local snapshot.

## 3. Operator Reference

| Type | Operator | Purpose |
|------|------|------|
| Cross-sectional | `rank(x)`, `zscore(x)`, `normalize(x)`, `scale(x)`, `winsorize(x, std=4)` | Normalize all stocks each day |
| Time-series | `ts_mean`, `ts_std_dev`, `ts_delta`, `ts_rank`, `ts_corr`, `ts_decay_linear`, `ts_backfill`, `ts_zscore` | Compute historical windows per stock |
| Group | `group_rank(x, group)`, `group_neutralize(x, group)`, `group_zscore(x, group)`, `group_backfill(x, group, N)` | Apply intra-group normalization |
| Conditional | `if_else(cond, a, b)`, `trade_when(x, cond, delay)` | Conditional exposure |
| Vector | `vec_avg(a, b, c)`, `vec_sum(a, b, c)` | Element-wise averaging and summation |

**Golden combination**: `group_rank(ts_rank(signal, N), subindustry)`

## 4. Factor Template Library

### 4.1 High-Probability Templates

```fastexpr
-- Template A: ROE trend (highest pass rate)
group_rank(ts_rank(operating_income / equity, 126), subindustry)

-- Template B: EPS yield adjustment
group_rank(ts_rank(est_eps / close, 126), industry)

-- Template C: FCF yield
group_rank(ts_rank(free_cash_flow_reported_value / equity, 126), industry)

-- Template D: Multi-factor blend (high Fitness)
0.5 * group_rank(ts_rank(operating_income / equity, 126), subindustry)
+ 0.5 * group_rank(ts_rank(est_eps / close, 126), industry)

-- Template E: Low-correlation technical + fundamental blend
0.5 * rank(-(close / open - 1)) + 0.5 * rank(ts_rank(operating_income / equity, 126))

-- Template F: Asset turnover x margin
rank(ts_rank(operating_income / sales * sales / assets, 126))
```

### 4.2 Recommended Defaults

| Factor type | Decay | Neutralization | Truncation | nanHandling | Expected TO |
|----------|-------:|----------------|------------|-------------|------------:|
| Fundamental quality | 0 | SUBINDUSTRY | 0.08 | ON | 2-8% |
| Analyst expectations | 0-4 | INDUSTRY/SUBINDUSTRY | 0.08 | ON | 9-16% |
| Technical reversal | 10-30 | INDUSTRY | 0.08 | OFF | 15-35% |
| Mixed factors | 4-20 | INDUSTRY/SUBINDUSTRY | 0.08 | ON | 10-20% |
| Sentiment | 4-10 | INDUSTRY | 0.05-0.08 | ON | 8-30% |

## 5. Metrics and Checks

### 5.1 Core Metrics

| Metric | Formula / Meaning | Target |
|------|-------------------|--------|
| Sharpe | daily IR x sqrt(252) | >= 1.5, minimum 1.25 |
| Fitness | Sharpe x sqrt(|Returns| / max(TO, 0.125)) | >= 1.1, minimum 1.0 |
| Returns | annualized return / $10M | >= 7% |
| Turnover | daily traded value / Book Size | 1%-20% |
| Drawdown | maximum peak-to-trough drawdown | < 15% |
| Margin | PnL / total traded value | Higher is better |

### 5.2 IS Checklist

| Check | Threshold | Failure cause | Fix |
|--------|-----------|---------------|-----|
| LOW_SHARPE | >= 1.25 | Weak signal | Change field, window, or add `group_rank` |
| LOW_FITNESS | >= 1.0 | Excess turnover | Increase decay or blend with stable signals |
| LOW_TURNOVER | >= 1% | Signal too stable | Shorten the window or use a more active field |
| HIGH_TURNOVER | <= 70% | Turnover explosion | Increase decay, use `trade_when`, or blend signals |
| CONCENTRATED_WEIGHT | Single name < 10% and diversified | Weight concentration | Use `rank()`, reduce truncation, or add `ts_backfill` |
| LOW_SUB_UNIVERSE_SHARPE | Still works in TOP1000 | Small-cap dependence | Use fundamentals, `SUBINDUSTRY`, and avoid market-cap bias |
| SELF_CORRELATION | **daily-return** correlation < 0.7 | Too similar to an existing factor | Change the signal family, add filtering, or change the universe; do not only tune parameters |
| MATCHES_COMPETITION | Informational | - | No action |

### 5.3 Failure Statistics

| Failure reason | Share | Takeaway |
|----------|------:|----------|
| LOW_SHARPE | 90.7% | Signal quality is the main bottleneck |
| LOW_FITNESS | 66.2% | Often a softer version of HIGH_TURNOVER |
| LOW_SUB_UNIVERSE_SHARPE | 51.0% | Avoid small-cap and liquidity bias |

**Pass rate by data type**: fundamental 40% > mixed 12.7% > pure technical 5.3% > other 0%

## 6. Diagnosis and Fixes

| Symptom | Likely cause | Fix |
|------|--------------|-----|
| Fitness < 1.0 | Turnover > 30% | Increase decay, blend fundamentals, or use `ts_decay_linear` |
| Sharpe < 1.25 | Weak signal | Extend the window, use `group_rank`, or change the field |
| TO > 50% | Signal changes too fast | Use decay 10-30, `trade_when`, or blend |
| DD > 15% | High volatility or leverage | Increase decay, lower truncation, and blend lower-volatility signals |
| CONCENTRATED_WEIGHT FAIL | Sparse or extreme weights | Use `rank()`, truncation 0.05, or `ts_backfill` |
| Sub-Universe FAIL | Small-cap dependence | Avoid `rank(-assets)`, use `group_rank`, and add liquidity filters |
| simulation_error | Invalid field or wrong operator arguments | Verify the field first with `rank(field)` and check operator arity |
| trade_when no trades | Condition too strict | Relax the condition or use `if_else` |

## 7. BRAIN API Automation

### 7.1 Authentication

**You must provide credentials before running this section.** Use environment variables if possible; alternatively place an untracked local `credential.txt` file in the skill directory with a JSON array containing the BRAIN username and password:

```json
["your_username", "your_password"]
```

```python
import json
import requests
from requests.auth import HTTPBasicAuth

API_BASE = "https://api.worldquantbrain.com"

# 1. Load credentials.
import os

username = os.getenv("WQ_BRAIN_USERNAME")
password = os.getenv("WQ_BRAIN_PASSWORD")
if not (username and password):
    with open("credential.txt") as f:
        username, password = json.load(f)

# 2. Create a session and authenticate.
session = requests.Session()
session.auth = HTTPBasicAuth(username, password)
session.headers.update({
    "Content-Type": "application/json",
    "Accept": "application/json",
})

resp = session.post(f"{API_BASE}/authentication")
assert resp.status_code == 201, f"Authentication failed: {resp.status_code} {resp.text}"
print("Authentication succeeded")
```

### 7.2 Fetch Submitted Alphas and Compute Correlation

**Purpose**: before submitting a new factor, avoid high correlation with existing factors. Correlation should be based on daily returns, not cumulative PnL.

```python
import numpy as np

def fetch_pnl(session, alpha_id):
    """Fetch an alpha's cumulative PnL series; schema.properties may be a list or dict."""
    r = session.get(f"{API_BASE}/alphas/{alpha_id}/recordsets/pnl")
    if r.status_code != 200 or not r.text.strip():
        return []
    data = r.json()
    props = data.get("schema", {}).get("properties", [])
    if isinstance(props, list):
        date_idx = next((i for i, p in enumerate(props) if p.get("name", "").lower() == "date"), 0)
        pnl_idx = next((i for i, p in enumerate(props) if p.get("name", "").lower() in ("pnl", "cum_pnl", "returns", "ret")), 1)
    else:
        date_idx = next((v["index"] for k, v in props.items() if k.lower() == "date"), 0)
        pnl_idx = next((v["index"] for k, v in props.items() if k.lower() in ("pnl", "cum_pnl", "returns", "ret")), 1)
    records = sorted(data.get("records", []), key=lambda r: r[date_idx])
    out = []
    for row in records:
        rec = row[0] if isinstance(row, list) and len(row) == 1 and isinstance(row[0], list) else row
        try:
            out.append(float(rec[pnl_idx]))
        except Exception:
            continue
    return out

def daily_returns(cum_pnl):
    """Convert cumulative PnL to daily returns; correlation should use daily returns, not the cumulative curve."""
    return [cum_pnl[i+1] - cum_pnl[i] for i in range(len(cum_pnl) - 1)]

def get_active_alphas(session, user_id="self", limit=100):
    """Fetch all alphas, including ACTIVE and UNSUBMITTED, with pagination."""
    all_alphas = []
    offset = 0
    while True:
        data = session.get(f"{API_BASE}/users/{user_id}/alphas", params={"limit": limit, "offset": offset}).json()
        batch = data.get("results", data.get("alphas", []))
        if not batch:
            break
        all_alphas.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_alphas

# Compute daily-return correlation between a new factor and all ACTIVE alphas.
new_pnl = fetch_pnl(session, new_alpha_id)
new_ret = daily_returns(new_pnl)
existing = get_active_alphas(session)
active = [a for a in existing if a.get("status") == "ACTIVE"]

high_corr = []
for alpha in active:
    old_id = alpha.get("id")
    try:
        old_pnl = fetch_pnl(session, old_id)
        old_ret = daily_returns(old_pnl)
        if len(new_ret) == len(old_ret) and len(new_ret) > 20:
            corr = float(np.corrcoef(new_ret, old_ret)[0, 1])
            print(f"Daily-return correlation with {old_id}: {corr:.3f}")
            if abs(corr) >= 0.7:
                high_corr.append((old_id, corr))
    except Exception:
        continue

if high_corr:
    print(f"Warning: found {len(high_corr)} highly correlated factors; modify or discard them")
```

**Decision rule, based on daily returns, not cumulative PnL**:

| Correlation | Action |
|----------|------|
| abs(corr) < 0.5 | OK to submit |
| 0.5 <= abs(corr) < 0.7 | Proceed carefully; improve Sharpe or adjust the signal |
| abs(corr) >= 0.7 | Discard or rebuild, unless the new factor's Sharpe is at least 10% better than the old one |

> **Do not correlate cumulative PnL series.** Cumulative curves carry a strong trend and will overstate similarity between unrelated signals.

### 7.3 Backtest

```python
payload = {
    "type": "REGULAR",
    "settings": {
        "instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000",
        "delay": 1, "decay": 0, "neutralization": "SUBINDUSTRY",
        "truncation": 0.08, "pasteurization": "ON", "unitHandling": "VERIFY",
        "nanHandling": "ON", "language": "FASTEXPR", "visualization": False,
    },
    "regular": "group_rank(ts_rank(operating_income/equity, 126), subindustry)",
}
resp = session.post("https://api.worldquantbrain.com/simulations", json=payload)
sim_id = resp.headers["Location"].rstrip("/").split("/")[-1]

while True:
    data = session.get(f"https://api.worldquantbrain.com/simulations/{sim_id}").json()
    if data.get("status") == "COMPLETE":
        alpha_id = data["alpha"]
        break
    time.sleep(8)

alpha = session.get(f"https://api.worldquantbrain.com/alphas/{alpha_id}").json()
```

### 7.4 Submit and Monitor

```python
# Submit
sub = session.post(f"https://api.worldquantbrain.com/alphas/{alpha_id}/submit")
print(sub.status_code)  # 201 means the request was accepted

# Monitor SELF_CORRELATION
for _ in range(30):
    alpha = session.get(f"https://api.worldquantbrain.com/alphas/{alpha_id}").json()
    sc = next((c for c in alpha.get("is", {}).get("checks", []) if c["name"] == "SELF_CORRELATION"), {})
    if sc.get("result") in ("PASS", "FAIL"):
        break
    time.sleep(60)
```

### 7.5 Automated Submission Template

```python
import numpy as np

def simulate_and_submit(expression, settings, existing_pnls=None):
    """
    existing_pnls: {alpha_id: [cum_pnl_values]}, cumulative PnL series for already-live factors.
    Returns: {"alpha_id": ..., "decision": "submitted|skip|high_corr|verify_failed", ...}
    """
    payload = {"type": "REGULAR", "settings": settings, "regular": expression}
    resp = session.post("https://api.worldquantbrain.com/simulations", json=payload)
    if resp.status_code != 201:
        return {"error": "simulate_failed"}
    sim_id = resp.headers["Location"].rstrip("/").split("/")[-1]
    while True:
        data = session.get(f"https://api.worldquantbrain.com/simulations/{sim_id}").json()
        if data.get("status") == "COMPLETE":
            alpha_id = data["alpha"]
            break
        if data.get("status") in ("ERROR", "FAILED"):
            return {"error": "simulation_error"}
        time.sleep(8)
    alpha = session.get(f"https://api.worldquantbrain.com/alphas/{alpha_id}").json()
    is_ = alpha.get("is", {})

    # 1. Basic metric filter.
    if is_.get("fitness", 0) < 1.1 or is_.get("sharpe", 0) < 1.3 or is_.get("turnover", 1) > 0.20:
        return {"alpha_id": alpha_id, "decision": "skip", "reason": "metrics", "metrics": is_}

    # 2. Correlation check (based on daily returns).
    def daily_rets(cum):
        return [cum[i+1] - cum[i] for i in range(len(cum) - 1)]

    if existing_pnls:
        new_pnl = fetch_pnl(session, alpha_id)
        new_ret = daily_rets(new_pnl)
        for old_id, old_pnl in existing_pnls.items():
            old_ret = daily_rets(old_pnl)
            if len(new_ret) == len(old_ret) and len(new_ret) > 20:
                corr = abs(float(np.corrcoef(new_ret, old_ret)[0, 1]))
                if corr >= 0.7:
                    # Exception: if the new Sharpe is more than 10% higher, submission can still be justified.
                    old_sharpe = None  # provide from upstream cache or metadata
                    if old_sharpe is None or is_.get("sharpe", 0) < old_sharpe * 1.1:
                        return {"alpha_id": alpha_id, "decision": "high_corr", "corr_with": old_id, "corr": corr}

    # 3. Submit.
    sub = session.post(f"https://api.worldquantbrain.com/alphas/{alpha_id}/submit")
    if sub.status_code not in (200, 201):
        return {"alpha_id": alpha_id, "decision": "submit_failed", "status": sub.status_code}

    # 4. Verify that it is actually live (BRAIN can keep an alpha UNSUBMITTED because of SELF_CORRELATION).
    for _ in range(20):
        time.sleep(10)
        alpha = session.get(f"https://api.worldquantbrain.com/alphas/{alpha_id}").json()
        if alpha.get("status") == "ACTIVE":
            return {"alpha_id": alpha_id, "decision": "submitted", "status": "ACTIVE"}
        sc = next((c for c in alpha.get("is", {}).get("checks", []) if c["name"] == "SELF_CORRELATION"), {})
        if sc.get("result") == "FAIL":
            return {"alpha_id": alpha_id, "decision": "self_correlation_fail", "status": alpha.get("status")}

    return {"alpha_id": alpha_id, "decision": "verify_failed", "status": alpha.get("status")}
```

### 7.6 Rate Limiting

- Sleep 2-5 seconds between simulations and submissions.
- On 429 responses, read `Retry-After` and back off exponentially.
- For batches, prefer a single thread or at most two concurrent workers.

### 7.7 Post-Submission Verification (`201` does not mean live)

`POST /alphas/{id}/submit` returning 201 only means the request was accepted, **not** that the alpha is now ACTIVE. Common outcomes in practice:

- the alpha still remains `UNSUBMITTED` because SELF_CORRELATION failed or it is still under review;
- a new alpha generated from the same signal with different parameters is treated as a duplicate and never becomes truly live.

**Always confirm twice**:

```python
alpha = session.get(f"{API_BASE}/alphas/{alpha_id}").json()
print(alpha.get("status"))  # ACTIVE is the only true success state

# If status == UNSUBMITTED, inspect SELF_CORRELATION in the checks list.
for c in alpha.get("is", {}).get("checks", []):
    print(c["name"], c.get("result"), c.get("value"))
```

**Fetch all alphas and count ACTIVE ones**:

```python
def get_all_alphas(session, limit=100):
    all_alphas = []
    offset = 0
    while True:
        data = session.get(f"{API_BASE}/users/self/alphas", params={"limit": limit, "offset": offset}).json()
        batch = data.get("results", data.get("alphas", []))
        if not batch:
            break
        all_alphas.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return all_alphas

all_alphas = get_all_alphas(session)
active = [a for a in all_alphas if a.get("status") == "ACTIVE"]
print(f"total={len(all_alphas)}, ACTIVE={len(active)}")
```

## 8. Portfolio Construction Rules

### 8.1 Diversified Portfolio Example

| Cluster | Representative expression |
|----|------------|
| Profitability | `group_rank(ts_rank(operating_income/equity, 126), subindustry)` |
| Analyst | `group_rank(ts_rank(est_eps/close, 252), subindustry)` |
| FCF | `group_rank(ts_rank(free_cash_flow_reported_value/equity, 126), industry)` |
| Low-correlation blend | `0.5*rank(-(close/open-1)) + 0.5*rank(ts_rank(operating_income/equity, 126))` |
| Quality basket | `0.5*group_rank(ts_rank(oi/equity,126),subindustry) + 0.5*group_rank(ts_rank(est_eps/close,126),industry)` |

### 8.2 Submission Priority

1. High Fitness (>= 1.5) and low TO (< 15%).
2. Signals from different clusters.
3. If SELF_CORRELATION conflicts, keep the higher-Fitness version.

### 8.3 The Reality of Correlation

Daily-return correlation across ACTIVE alphas shows that:

- **Within the same signal cluster, correlation is usually very high**:
  - two open-close reversal plus OI/Equity blends with different weights had daily-return correlation of **0.84**;
  - two analyst EPS signals had correlation of **0.74**;
  - two leverage/quality factors (`-equity/assets` vs `liabilities/assets`) had correlation of **0.84**.
- **Cross-cluster differences do not guarantee diversification**: a sentiment alpha based on `scl12_buzz` and an analyst alpha based on `est_eps/close` still reached **0.59-0.67**.
- **Cumulative PnL correlation is heavily distorted**: pairwise cumulative-PnL correlations are often above **0.90**, which makes unrelated factors look identical.

**Conclusion**:

- Changing windows, weights, or neutralization does **not** create true low correlation.
- True low correlation comes from **different data sources or different economic logic** such as macro events, options flow, cross-border data, or alternative data.
- In the standard USA TOP3000 fundamental / price-volume / analyst pool, "low correlation" usually means **0.3-0.6 daily-return correlation**; do not chase zero.

## 9. Pre-Submission Checklist

- [ ] Have I pulled **all** alphas, including ACTIVE and UNSUBMITTED, not just the latest simulation?
- [ ] Is the new factor's **daily-return** correlation with existing ACTIVE alphas below 0.7, or is the new Sharpe at least 10% better than the old one?
- [ ] Is correlation computed from **daily returns**, not cumulative PnL?
- [ ] Has the field been verified?
- [ ] Did the simulation complete without errors?
- [ ] Is Sharpe >= 1.3, ideally >= 1.5?
- [ ] Is Fitness >= 1.1?
- [ ] Is Turnover between 1% and 20%, with room to relax up to 35% if needed?
- [ ] Is Drawdown below 15%?
- [ ] Do all IS checks pass?
- [ ] Are the long and short counts reasonable?
- [ ] After submission, did I re-check that status == ACTIVE? A 201 response alone is not enough.

## 10. Core Lessons

1. Pull the full ACTIVE alpha PnL set before generating a new factor, or you will repeat high-correlation ideas.
2. Correlation must be computed on daily returns; cumulative PnL makes almost everything look the same.
3. A 201 response does not mean submission succeeded; always confirm `status == ACTIVE`.
4. Fundamental > mixed > technical: `operating_income/equity`, `est_eps/close`, and `free_cash_flow_reported_value/equity` are the most reliable starting points.
5. `group_rank + ts_rank` is the golden combination.
6. `SUBINDUSTRY` neutralization has the highest pass rate.
7. Decay is the main lever for controlling turnover: 0 for fundamentals, 10-30 for technical signals.
8. A 50/50 orthogonal blend may reduce turnover, but it does not necessarily reduce correlation; correlation depends on signal source, not just weights.
9. Verify fields first; invalid fields fail immediately.
10. In USA TOP3000, true low correlation is hard to achieve; different expressions from the same data pool are often still highly correlated.

## 11. Self-Evolution Loop

After each BRAIN interaction, such as a submission, query, or analysis, the AI should write the useful findings back into this skill so that it keeps improving with practice.

### 11.1 Triggers

Run `scripts/evolve_skill.py` after any of the following:

- one or more new alphas were submitted;
- a batch of alphas was backtested;
- alpha status changed, for example UNSUBMITTED -> ACTIVE or rejected;
- a new field becomes usable or a known field starts failing.

### 11.2 How to Run

**Prerequisite**: set `WQ_BRAIN_USERNAME` and `WQ_BRAIN_PASSWORD`, or place an untracked `credential.txt` in the skill directory with the BRAIN credentials as a JSON array:

```json
["your_username", "your_password"]
```

```bash
# 1. Preview: generate a Markdown snippet without changing any files.
pyenv exec python scripts/evolve_skill.py

# 2. Apply: append the snippet to SKILL.md and update alpha_db.json.
pyenv exec python scripts/evolve_skill.py --apply
```

> Note: **preview mode without `--apply` does not modify `alpha_db.json` or `SKILL.md`**. Review the output first, then apply it if it is correct. The script only depends on `requests` and `numpy`; it does **not** require the `wq-bus` project code. The data files are shipped with the skill.

The script will:

1. Fetch `/users/self/alphas` with pagination to obtain every alpha.
2. Compare the results with the local `alpha_db.json` and find **new** or **changed** alphas.
3. Fetch `recordsets/pnl` for new alphas and compute **daily-return correlation** against existing ACTIVE alphas.
4. Generate a lesson entry automatically, including metric evaluation, correlation evaluation, and an expression summary.
5. Emit a **bulk snapshot** on the first run, then **incremental entries** on later runs.
6. In `--apply` mode, append the entry to `## 12. Empirical Record (Auto-Updated)` and save the local `alpha_db.json` file.

### 11.3 How the AI Should Distill Lessons

After the script outputs a report, the AI should decide manually which entries deserve to be written permanently into the skill:

- **Keep**: successful high-Fitness, low-turnover cases, new low-correlation signal clusters, and unexpected failure modes.
- **Compress**: repeated entries from the same signal cluster should be merged into a single rule.
- **Update templates and thresholds**: if a field or template repeatedly fails, revise Sections 4, 5, and 6.

### 11.4 Data Structures

- `alpha_db.json`: local alpha snapshot store containing status, metrics, expressions, and PnL. It contains personal research records and is ignored by default through `.gitignore`; it should not be published to a public repository.
- `SKILL.md`: the final human-readable playbook. Section 12 keeps only sanitized, general lessons.

## 12. Empirical Record (Auto-Updated)

> This section only documents the mechanism. Real alpha IDs, expressions, PnL series, submission statuses, and correlation records generated during actual runs may be linked to a personal account and research assets. They are written to local `alpha_db.json` by default and are not published with the repository.
>
> If you want to preserve general lessons, summarize them into sanitized rules and write them back into Sections 4, 5, 6, 8, and 10.
