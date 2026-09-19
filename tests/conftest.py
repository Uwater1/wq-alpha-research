"""Import paths for the skill scripts and the legacy BRAIN tooling.

Nothing here touches the network or credentials: the modules under test only
resolve credentials inside their session constructors. The research store is
redirected to a temp directory so no test can write the real research.db.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

for _rel in ("scripts", "legacy/wq_brain"):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)


@pytest.fixture(autouse=True, scope="session")
def isolated_research_db(tmp_path_factory):
    """Point WQ_RESEARCH_DB at a throwaway file for the whole test session."""
    previous = os.environ.get("WQ_RESEARCH_DB")
    os.environ["WQ_RESEARCH_DB"] = str(tmp_path_factory.mktemp("research-db") / "research.db")
    yield
    if previous is None:
        os.environ.pop("WQ_RESEARCH_DB", None)
    else:
        os.environ["WQ_RESEARCH_DB"] = previous
