"""Import paths for the skill scripts and the legacy BRAIN tooling.

Nothing here touches the network or credentials: the modules under test only
resolve credentials inside their session constructors.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

for _rel in ("scripts", "legacy/wq_brain"):
    _path = str(REPO_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)
