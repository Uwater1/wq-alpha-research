"""Fetch the BRAIN operator reference (GET /operators, single call) to references/wq_operators.json.

Usage:
    ./.venv/bin/python scripts/fetch_operators.py

Output: list of {name, category, scope, definition, description, documentation, level}.
Refresh rarely — operators change far less often than the field catalog.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "legacy" / "wq_brain"))
sys.path.insert(0, str(REPO_ROOT))

from wq_session import API_BASE, api_get, get_session  # noqa: E402

OUT = REPO_ROOT / "references" / "wq_operators.json"


def main() -> int:
    ops = api_get(get_session(), f"{API_BASE}/operators").json()
    assert isinstance(ops, list) and ops and "name" in ops[0], "unexpected /operators shape"
    OUT.write_text(json.dumps(ops, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Saved {len(ops)} operators -> {OUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
