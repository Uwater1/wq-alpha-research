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
11. Below ~12.5% turnover, Fitness ignores turnover entirely (`returns / max(TO, 0.125)`): a very stable signal is scored purely on its return, so low-turnover Fitness failures mean the return is too small — raise returns instead of lowering turnover further.
12. Correlation is a property of the *field family*, not of the transform: `ts_rank(ROE,126)` vs `ts_zscore(ROE,252)` measured +0.76, and ROE vs ROA +0.79. Changing window/transform/denominator inside a family does not create a new alpha; switching family (for example accrual profitability to realized cash flow, +0.21) does.
13. Cash-flow fundamentals are the cheapest orthogonal block next to profitability within the same `fundamental` dataset: they clear the same Sharpe range while correlating only 0.2-0.3 with accrual-based quality alphas.

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

### 2026-09-18 15:36 UTC — Bulk Initialization Snapshot

- Total alphas: 19 | ACTIVE: 1 | non-ACTIVE: 18
- Signal-cluster distribution: {'other': 9, 'technical': 9, 'analyst': 1}

**Top ACTIVE alphas by Fitness** (IDs and expressions sanitized by `scripts/evolve_skill.py`; raw records stay in the local `alpha_db.json`):
- `alpha-9a458f31` (analyst, `group_rank+ts_rank` on an EPS-yield field): Sharpe≈1.85, Fitness≈1.02, TO≈0.24 — passes the IS gate but sits above the 20% turnover comfort zone.

**High-correlation ACTIVE daily-return pairs**: none >= 0.7 (or insufficient PnL)

**Clear failures (Fitness < 0.5, 10 total)**:
- Cluster distribution: {'technical': 6, 'other': 4}

**High turnover (TO > 50%, 9 total)**:
- Cluster distribution: {'technical': 7, 'other': 2}

---


### 2026-09-18 21:13 UTC — 15-Alpha Fundamental Batch (Compressed)

15 expressions were simulated in one batch (USA TOP3000 delay=1, 3 concurrent, ~15 min wall clock).
3 passed every IS check and became ACTIVE. Per-alpha rows were compressed into rules per §11.3.

**Kept — the three ACTIVE survivors (all low-frequency fundamental quality / cash-flow signals):**

| Cluster | Operator shape | Decay | Sharpe | Fitness | TO | Daily-return corr vs existing ACTIVE |
|---|---|---:|---:|---:|---:|---:|
| cash-flow estimate yield | `group_rank+ts_rank` | 4 | 1.85 | 1.45 | 10.8% | 0.66 pre-submit, 0.68 in pool |
| profitability + investment blend | `group_rank+ts_rank+ts_delta` | 4 | 1.84 | 1.35 | 6.2% | 0.06 (near-orthogonal) |
| profitability z-score | `group_rank+ts_zscore` | 2 | 1.79 | 1.25 | 4.7% | -0.01 (near-orthogonal) |

**Rules distilled from this batch:**

1. **Low-turnover signals fail on Fitness, not Sharpe.** Sharpe 1.2-1.65 at ~5-6% turnover repeatedly landed at
   Fitness 0.5-1.0. The Fitness formula divides returns by `max(TO, 0.125)`, so turnover below 12.5% buys no
   credit at all — the annualized return itself must clear roughly 6%. Fix the return profile, not the turnover.
2. **A different transform/window on the same field is NOT a correlation escape hatch.** Directly measured
   on the same pool: `ts_rank(ROE,126)` vs `ts_zscore(ROE,252)` = **+0.76**, and `ROE` vs `ROA` = **+0.79**.
   Transform, window and denominator tweaks stay inside the same bet. Orthogonality has to come from the
   *field family*: `cashflow_op/equity` vs `ROE` = +0.21, vs an accrual profitability z-score = +0.32, vs an
   estimate-yield alpha = +0.22 — realized cash flow is a genuinely different bet from accrual profitability.
3. **Blending two slow fundamentals raises Fitness without raising turnover.** The 50/50 quality + asset-growth
   blend was the only single expression to reach Fitness 1.35 at 6% turnover; the blend's weights matter less
   than the fact that two return streams are added.
4. **Analyst *estimate* yields are one cluster regardless of field name.** An EBITDA-estimate-yield expression hit
   0.73 daily-return correlation with the existing EPS-estimate-yield alpha and was rejected. Do not re-mine the
   analyst estimate family by swapping one estimate field for another.
5. **Alternative data at high decay was the worst cohort.** Social buzz and implied-volatility skew at decay 6
   produced Sharpe <= 0.33 with 43-44% turnover (HIGH_TURNOVER *and* LOW_SHARPE). Their low correlation is real
   but worthless without a return profile: control turnover before chasing correlation.
6. **Correlation drifts upward as your own pool grows.** The same alpha measured 0.66 against a 1-alpha pool
   and 0.68 against the 2-alpha pool after the first submission. Keep ~0.1 of headroom below the 0.7 limit
   rather than submitting right at the boundary.
7. **Operationally:** 15 expressions cost ~15 minutes (BRAIN runs 3 simulations concurrently, ~2 min each).
   Use `--skip-done` for reruns so finished expressions are never re-simulated, and trust
   `submit_from_csv.py`'s ACTIVE confirmation instead of re-simulating to double-check.

---


### 2026-09-18 22:07 UTC

- **alpha-3af05a89** (UNSUBMITTED, cashflow): Sharpe=2.29, Fitness=1.86, TO=0.133, DD=0.033. high Fitness and low turnover, a strong candidate；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-97e0834e** (UNSUBMITTED, cashflow): Sharpe=1.59, Fitness=1.01, TO=0.059, DD=0.054. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-26701717** (UNSUBMITTED, cashflow): Sharpe=1.55, Fitness=0.88, TO=0.059, DD=0.042. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-9c2e18f2** (ACTIVE, profitability+cashflow): Sharpe=2.14, Fitness=1.59, TO=0.062, DD=0.046. high Fitness and low turnover, a strong candidate；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-3d5fec4c** (UNSUBMITTED, cashflow): Sharpe=1.84, Fitness=1.29, TO=0.062, DD=0.047. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-70ab4e69** (UNSUBMITTED, cashflow): Sharpe=1.60, Fitness=1.01, TO=0.058, DD=0.050. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_delta`
- **alpha-a7477118** (UNSUBMITTED, cashflow): Sharpe=1.82, Fitness=1.27, TO=0.061, DD=0.039. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-52d76672** (UNSUBMITTED, cashflow): Sharpe=1.68, Fitness=1.38, TO=0.097, DD=0.049. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_zscore`
- **alpha-252d8f37** (UNSUBMITTED, other): Sharpe=1.30, Fitness=1.01, TO=0.081, DD=0.070. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_zscore`
- **alpha-fd146b85** (UNSUBMITTED, cashflow): Sharpe=1.83, Fitness=1.34, TO=0.057, DD=0.051. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-6ff79ebe** (UNSUBMITTED, cashflow): Sharpe=1.69, Fitness=1.08, TO=0.059, DD=0.039. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-39ab409d** (UNSUBMITTED, cashflow): Sharpe=1.74, Fitness=1.19, TO=0.059, DD=0.050. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+winsorize+ts_backfill`
- **alpha-6ab8e736** (UNSUBMITTED, other): Sharpe=1.01, Fitness=0.58, TO=0.154, DD=0.066. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-24b06044** (UNSUBMITTED, other): Sharpe=1.33, Fitness=1.04, TO=0.100, DD=0.070. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_zscore`
- **alpha-99379724** (UNSUBMITTED, other): Sharpe=1.36, Fitness=0.69, TO=0.280, DD=0.059. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-a10bf5b5** (UNSUBMITTED, cashflow+technical): Sharpe=0.67, Fitness=0.30, TO=0.117, DD=0.062. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_std_dev`
- **alpha-51e47611** (UNSUBMITTED, cashflow): Sharpe=0.72, Fitness=0.45, TO=0.125, DD=0.098. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+rank+ts_delta`
- **alpha-98281a42** (UNSUBMITTED, cashflow): Sharpe=1.71, Fitness=1.37, TO=0.098, DD=0.039. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_delta`
- **alpha-0340a097** (UNSUBMITTED, cashflow): Sharpe=1.61, Fitness=1.29, TO=0.103, DD=0.049. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_delta`
- **alpha-3e6fa5eb** (ACTIVE, cashflow): Sharpe=1.63, Fitness=1.32, TO=0.126, DD=0.047. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-435395ea** (ACTIVE, cashflow): Sharpe=2.18, Fitness=1.79, TO=0.101, DD=0.033. high Fitness and low turnover, a strong candidate；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-798cec17** (UNSUBMITTED, cashflow): Sharpe=1.91, Fitness=1.32, TO=0.062, DD=0.037. meets the basic submission threshold；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-dee38b98** (UNSUBMITTED, cashflow): Sharpe=1.46, Fitness=0.96, TO=0.060, DD=0.052. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_delta`
- **alpha-b1098918** (ACTIVE, cashflow): Sharpe=1.58, Fitness=1.00, TO=0.056, DD=0.056. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-9a19b59e** (UNSUBMITTED, cashflow): Sharpe=1.40, Fitness=0.76, TO=0.057, DD=0.054. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-0073370b** (UNSUBMITTED, cashflow): Sharpe=1.24, Fitness=0.74, TO=0.044, DD=0.062. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+abs`
- **alpha-e4ac8c0e** (UNSUBMITTED, other): Sharpe=1.31, Fitness=0.71, TO=0.055, DD=0.047. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-67e11734** (UNSUBMITTED, other): Sharpe=1.02, Fitness=0.49, TO=0.047, DD=0.049. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-0506bdfc** (UNSUBMITTED, other): Sharpe=0.32, Fitness=0.08, TO=0.054, DD=0.048. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-f36121ed** (UNSUBMITTED, cashflow): Sharpe=1.47, Fitness=0.83, TO=0.056, DD=0.041. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-c37561ab** (UNSUBMITTED, cashflow): Sharpe=0.82, Fitness=0.36, TO=0.054, DD=0.073. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-01510adc** (UNSUBMITTED, technical): Sharpe=1.36, Fitness=0.54, TO=0.282, DD=0.056. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `trade_when+ts_arg_max+group_rank+ts_rank`
- **alpha-db7aa674** (UNSUBMITTED, technical): Sharpe=1.07, Fitness=0.50, TO=0.262, DD=0.042. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `trade_when+group_rank+ts_rank+ts_delta`
- **alpha-6e701b22** (UNSUBMITTED, other): Sharpe=0.69, Fitness=0.32, TO=0.026, DD=0.079. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+winsorize+ts_backfill`
- **alpha-655700cf** (UNSUBMITTED, other): Sharpe=0.84, Fitness=0.44, TO=0.027, DD=0.085. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+winsorize+ts_backfill`
- **alpha-c55146bf** (UNSUBMITTED, other): Sharpe=0.99, Fitness=0.63, TO=0.034, DD=0.061. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+winsorize+ts_backfill`
- **alpha-ed23df0f** (UNSUBMITTED, other): Sharpe=0.83, Fitness=0.40, TO=0.026, DD=0.069. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+winsorize+ts_backfill`
- **alpha-93f620b8** (UNSUBMITTED, technical): Sharpe=0.02, Fitness=0.00, TO=0.216, DD=0.101. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-63f1b808** (UNSUBMITTED, other): Sharpe=0.39, Fitness=0.08, TO=0.341, DD=0.059. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-238972ca** (UNSUBMITTED, other): Sharpe=0.02, Fitness=0.00, TO=0.219, DD=0.066. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-eea9259a** (UNSUBMITTED, other): Sharpe=-0.05, Fitness=-0.00, TO=0.254, DD=0.117. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_zscore`
- **alpha-907f7737** (UNSUBMITTED, other): Sharpe=1.04, Fitness=0.47, TO=0.172, DD=0.046. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-0a4b8569** (UNSUBMITTED, other): Sharpe=0.13, Fitness=0.03, TO=0.138, DD=0.129. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-8f4349ec** (UNSUBMITTED, other): Sharpe=1.00, Fitness=0.41, TO=0.227, DD=0.058. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_zscore`
- **alpha-36fdec21** (UNSUBMITTED, other): Sharpe=0.28, Fitness=0.07, TO=0.092, DD=0.053. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-7c9c434f** (UNSUBMITTED, technical): Sharpe=0.10, Fitness=0.02, TO=0.084, DD=0.117. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_corr+rank`
- **alpha-d648bc13** (UNSUBMITTED, other): Sharpe=1.24, Fitness=0.56, TO=0.245, DD=0.056. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-e3b0a4e0** (UNSUBMITTED, technical): Sharpe=0.23, Fitness=0.07, TO=0.117, DD=0.073. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_std_dev`
- **alpha-d2fb738c** (UNSUBMITTED, technical): Sharpe=-0.07, Fitness=-0.01, TO=0.363, DD=0.102. turnover is high; increase decay or blend in more stable signals；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_zscore`
- **alpha-3d7552bd** (UNSUBMITTED, other): Sharpe=1.04, Fitness=0.70, TO=0.159, DD=0.073. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_delta`
- **alpha-1ed8a1a2** (UNSUBMITTED, technical): Sharpe=0.40, Fitness=0.18, TO=0.107, DD=0.105. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+ts_std_dev`
- **alpha-d80393f1** (UNSUBMITTED, technical): Sharpe=0.12, Fitness=0.02, TO=0.498, DD=0.160. turnover is high; increase decay or blend in more stable signals；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank+abs`
- **alpha-8f72bccb** (UNSUBMITTED, technical): Sharpe=-0.06, Fitness=-0.01, TO=0.053, DD=0.104. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_zscore+ts_std_dev`
- **alpha-f85b2daf** (UNSUBMITTED, other): Sharpe=0.35, Fitness=0.13, TO=0.149, DD=0.131. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-444fb842** (UNSUBMITTED, other): Sharpe=1.25, Fitness=0.95, TO=0.116, DD=0.069. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-ad6cbc19** (UNSUBMITTED, other): Sharpe=0.70, Fitness=0.36, TO=0.148, DD=0.074. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`
- **alpha-448a8bbe** (UNSUBMITTED, other): Sharpe=0.05, Fitness=0.01, TO=0.090, DD=0.294. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank`
- **alpha-412146d1** (UNSUBMITTED, other): Sharpe=-0.38, Fitness=-0.13, TO=0.145, DD=0.154. metrics are average and need more work；no ACTIVE alpha available for comparison
  - Expression: `group_rank+ts_rank`

---

### 2026-09-18 (same day) — Cash-Flow Campaign, Compressed Rules

> The 55 raw rows above are the mechanism's output. Per §11.3 they are compressed here into
the rules that should drive the next run; read this section, not the rows.

Three batches were run after the first three submissions: 28 alternative-data expressions, 19 cash-flow
expressions, then 12 targeted fixes. Six alphas reached ACTIVE.

**What worked — realized cash-flow fundamentals and cross-family blends:**

| Cluster | Shape | Sharpe | Fitness | TO |
|---|---|---:|---:|---:|
| cash-flow yield + estimate yield (blend) | `group_rank+ts_rank` | 2.18 | 1.79 | 10.1% |
| ROE + cash-flow yield (blend) | `group_rank+ts_rank` | 2.14 | 1.59 | 6.2% |
| cash-flow yield + market-correlation (blend) | `group_rank+ts_rank` | 1.63 | 1.32 | 12.6% |
| cash-flow yield, industry grouping | `group_rank+ts_rank` | 1.58 | 1.00 | 5.6% |
| cash-flow yield + receivables (queued) | `group_rank+ts_rank` | 1.91 | 1.32 | 6.2% |

**Rules distilled from this campaign:**

1. **Cash flow is the productive orthogonal block.** `cashflow_op/equity` measured +0.21 daily-return
   correlation against ROE, +0.22 against an estimate-yield alpha and +0.32 against an accrual
   profitability z-score, while its own Sharpe sits in the same 1.5-1.9 band. When the profitability and
   analyst-estimate space is exhausted, mine realized cash flow next — not another ratio or window.
2. **Blending across orthogonal families is the reliable Fitness lever.** Single factors repeatedly landed at
   Fitness 0.7-1.0 and died on LOW_FITNESS; the same factors blended 50/50 reached 1.3-1.8
   (cash flow + estimate 1.79, ROE + cash flow 1.59, cash flow + market-correlation 1.32). A blend raises
   returns without raising turnover — it is the cheapest fix for the Fitness gate.
3. **Alternative data is a dead end in USA TOP3000 delay=1 on this account.** 28 expressions spanning
   options (IV skew, term structure, put/call OI), news, social buzz, report footnotes, low-volatility,
   illiquidity and systematic-risk metrics peaked at Sharpe 1.25 / Fitness 0.95; most sat at Sharpe 0.0-1.0.
   Footnote fields preprocessed with `ts_backfill(120)+winsorize` were stable (TO 2.6-3.4%) but too weak
   (Sharpe 0.83-0.99). Do not spend another batch here without a new reason.
4. **`trade_when` gating trades Sharpe for turnover, not the other way round.** Gated reversal reached
   Sharpe 1.36 at 28% turnover (Fitness 0.54): gating preserved signal quality but never fixed the gate.
5. **Two checks cause nearly every near-miss, and both have mechanical fixes.** `LOW_SUB_UNIVERSE_SHARPE`:
   widen the grouping (`industry` instead of `subindustry`) or preprocess sparse fields with
   `ts_backfill(120)+winsorize`. `CONCENTRATED_WEIGHT`: lower truncation to 0.05. Read the failing check from
   `/alphas/{id}/check` before changing anything else — it is far cheaper than guessing.
6. **The scraper is stricter than the simulation response.** A simulation reports 7 checks, `/check` reports 8,
   so `passed == 7` from a batch does NOT mean submission-ready. Always gate on
   `scrape_submittable.py`, and re-run it a few minutes later: checks can still be `PENDING` right after a
   simulation finishes, so an early scrape under-reports winners.
7. **SELF_CORRELATION can PASS above its nominal 0.7 limit** (observed at 0.86) when the new alpha is
   materially stronger than the correlated incumbent. The PASS is legitimate, but it means the alpha is
   largely a duplicate — add it only when it brings better Sharpe or a genuinely new cluster.
8. **Submission is asynchronous.** After `POST /submit`, SELF_CORRELATION can stay `PENDING` for minutes and
   a repeat submit returns `403` — that is not a failure. Wait and re-check the status; do not discard or
   re-submit the alpha.

---

