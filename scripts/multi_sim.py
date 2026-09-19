"""BRAIN multi-simulation capability check (TODO P3).

P3 asks for automatic detection, packing, and a REGULAR fallback. Detection was run
against the live API and the answer for this platform/account is **no**:

    POST /simulations  {"type": "MULTI", "settings": {...}, "regular": ["rank(close)", "rank(open)"]}
    -> HTTP 400 {"type": ["Object with name=MULTI does not exist."],
                 "settings": {"visualization": ["This field is required."]},
                 "regular": ["Not a valid string."]}

So the only simulation type is REGULAR with a single expression string, the three-slot
REGULAR scheduler in `scripts/sim_scheduler.py` is the whole capacity story, and no
packing code is shipped for an endpoint that rejects it. This module keeps the check
reproducible (and cached in `research.db.meta`) because platform tiers and features do
change: if `--probe` ever reports support, P3 packing becomes a real project again.

Usage:
    ./.venv/bin/python scripts/multi_sim.py --status   # cached result, no network
    ./.venv/bin/python scripts/multi_sim.py --probe    # real request; spends a simulation if supported
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import brain_api  # noqa: E402
import canonical  # noqa: E402
import research_db  # noqa: E402

META_SUPPORTED = "multi_sim_supported"
META_CHECKED_AT = "multi_sim_checked_at"
META_REASON = "multi_sim_reason"
META_BATCH_SIZE = "multi_sim_batch_size"

#: Batch size to try when a platform does support MULTI. The real maximum is unknown
#: until a supporting platform answers, so start at the three-slot concurrency limit.
MULTI_SIM_BATCH_SIZE = 3

#: Everything BRAIN can say when it simply has no multi-simulation endpoint.
UNSUPPORTED_PATTERNS = (
    re.compile(r"name=MULTI does not exist", re.IGNORECASE),
    re.compile(r"not a valid string", re.IGNORECASE),
    re.compile(r"(multi[- ]?simulation|type)\D{0,40}(not (available|supported|enabled)|unsupported)", re.IGNORECASE),
    re.compile(r"unknown (simulation )?type", re.IGNORECASE),
)

PROBE_EXPRESSIONS = ("rank(close)", "rank(open)")


@dataclass
class MultiSimCapability:
    """Result of a capability check: usable batches, or why not."""

    supported: bool
    reason: str
    checked_at: str
    batch_size: int = 0
    evidence: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "reason": self.reason,
            "checked_at": self.checked_at,
            "batch_size": self.batch_size,
            "evidence": self.evidence,
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_failure(message: str) -> bool:
    """True when BRAIN's refusal means 'this platform has no MULTI type' (not a bug)."""
    return any(pattern.search(message or "") for pattern in UNSUPPORTED_PATTERNS)


def classify_exception(exc: BaseException) -> bool:
    """A capability answer, as opposed to a transport/rate-limit problem worth retrying."""
    if isinstance(exc, (brain_api.RateLimitError, brain_api.SessionExpiredError)):
        return False
    return classify_failure(str(exc))


def probe(
    client: brain_api.BrainClient,
    settings: Mapping[str, Any] | None = None,
    *,
    expressions: tuple[str, ...] = PROBE_EXPRESSIONS,
) -> MultiSimCapability:
    """Ask BRAIN whether MULTI works. If it does, this starts a real simulation."""
    payload_settings = canonical.normalize_settings(settings)
    try:
        handle = client.submit_multi(list(expressions), payload_settings)
    except brain_api.BrainAPIError as exc:
        if classify_exception(exc):
            return MultiSimCapability(False, "platform rejected type=MULTI", _now(), evidence=str(exc)[:300])
        raise
    return MultiSimCapability(
        True,
        "platform accepted a MULTI request",
        _now(),
        batch_size=MULTI_SIM_BATCH_SIZE,
        evidence=f"{len(expressions)} expressions accepted, simulation handle stored",
    ) if handle else MultiSimCapability(False, "no simulation handle returned", _now())


def load_capability(db: research_db.ResearchDB) -> MultiSimCapability | None:
    """Cached capability, or None when the platform has never been asked."""
    supported = db.get_meta(META_SUPPORTED)
    if supported is None:
        return None
    batch_size = db.get_meta(META_BATCH_SIZE)
    return MultiSimCapability(
        supported=supported == "1",
        reason=db.get_meta(META_REASON) or "",
        checked_at=db.get_meta(META_CHECKED_AT) or "",
        batch_size=int(batch_size) if batch_size and batch_size.isdigit() else 0,
    )


def save_capability(db: research_db.ResearchDB, capability: MultiSimCapability) -> None:
    """Persist the answer so the pipeline never probes twice for the same account state."""
    db.set_meta(META_SUPPORTED, "1" if capability.supported else "0")
    db.set_meta(META_CHECKED_AT, capability.checked_at)
    db.set_meta(META_REASON, capability.reason[:500])
    db.set_meta(META_BATCH_SIZE, str(capability.batch_size))


def multi_simulation_enabled(db: research_db.ResearchDB) -> bool:
    """Report, never assume: packing is only on when a probe actually proved support."""
    capability = load_capability(db)
    return bool(capability and capability.supported and capability.batch_size >= 2)


def candidate_groups(candidates: list[Mapping[str, Any]], max_batch: int = MULTI_SIM_BATCH_SIZE) -> list[list[Mapping[str, Any]]]:
    """Group packable candidates: multi-simulation children share one settings block.

    Only identical settings qualify, which is stricter than the minimum the platform
    documents and therefore safe: a batch can never mix region/universe/decay.
    """
    if max_batch < 2:
        return []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for candidate in candidates:
        groups.setdefault(str(candidate.get("settings_json") or ""), []).append(candidate)
    return [group[:max_batch] for group in groups.values() if len(group) >= 2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", help=f"path to research.db (default: ${research_db.DB_ENV_VAR} or repo root)")
    parser.add_argument("--probe", action="store_true", help="make a real MULTI request and cache the answer")
    parser.add_argument("--status", action="store_true", help="print the cached answer without touching the network")
    args = parser.parse_args(argv)

    with research_db.ResearchDB.open(args.db) as db:
        if args.probe:
            capability = probe(brain_api.BrainClient())
            save_capability(db, capability)
            print(capability.as_dict())
            return 0
        cached = load_capability(db)
        if cached is None:
            print("Multi-simulation capability unknown for this account. Run with --probe to check.")
            return 2
        print(cached.as_dict())
        return 0


if __name__ == "__main__":
    sys.exit(main())
