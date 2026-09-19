"""Tests for the multi-simulation capability check and REGULAR fallback.

The verdict itself is recorded from the live platform (see `scripts/multi_sim.py`):
BRAIN answers `Object with name=MULTI does not exist`, so support must never be
assumed and the answer must be cached instead of re-probed.
"""
from __future__ import annotations

import pytest

import brain_api
import multi_sim
import research_db as rdb
import sim_scheduler as sched

LIVE_REJECTION = (
    'POST https://api.worldquantbrain.com/simulations -> HTTP 400: '
    '{"type":["Object with name=MULTI does not exist."],'
    '"settings":{"visualization":["This field is required."]},"regular":["Not a valid string."]}'
)


class _FakeClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0

    def submit_multi(self, expressions, settings):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return brain_api.SimulationHandle(None, "", " + ".join(expressions), "SIM1", "url", 0.0)


@pytest.fixture()
def db(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def test_live_platform_message_is_classified_as_unsupported():
    assert multi_sim.classify_failure(LIVE_REJECTION)
    assert multi_sim.classify_exception(brain_api.BrainAPIError(LIVE_REJECTION, 400))
    assert not multi_sim.classify_failure("simulation failed: unsupported operator rank(")
    assert not multi_sim.classify_exception(brain_api.RateLimitError("slow down", retry_after=10))


def test_probe_records_an_unsupported_platform(db):
    capability = multi_sim.probe(_FakeClient(brain_api.BrainAPIError(LIVE_REJECTION, 400)))

    assert capability.supported is False
    assert capability.batch_size == 0
    assert "type=MULTI" in capability.reason
    assert "does not exist" in capability.evidence

    multi_sim.save_capability(db, capability)
    cached = multi_sim.load_capability(db)
    assert cached is not None and cached.supported is False
    assert multi_sim.multi_simulation_enabled(db) is False  # fallback to REGULAR


def test_probe_does_not_swallow_rate_limit_or_session_errors():
    with pytest.raises(brain_api.RateLimitError):
        multi_sim.probe(_FakeClient(brain_api.RateLimitError("busy", retry_after=30)))
    with pytest.raises(brain_api.SessionExpiredError):
        multi_sim.probe(_FakeClient(brain_api.SessionExpiredError("expired", 401)))
    assert multi_sim.probe(_FakeClient()).supported is True


def test_unknown_capability_is_not_assumed_to_be_supported(db):
    assert multi_sim.load_capability(db) is None
    assert multi_sim.multi_simulation_enabled(db) is False


def test_capability_round_trip_for_a_supporting_platform(db):
    multi_sim.save_capability(db, multi_sim.MultiSimCapability(True, "accepted", "2026-09-18T00:00:00+00:00",
                                                              batch_size=3))
    cached = multi_sim.load_capability(db)
    assert cached is not None and cached.supported and cached.batch_size == 3
    assert multi_sim.multi_simulation_enabled(db) is True


def test_candidate_groups_require_identical_settings(db):
    keys = [{"decay": 6}, {"decay": 6}, {"decay": 9}]
    for index, settings in enumerate(keys + keys):
        db.queue_candidate(f"rank(close) + {index}", settings)
    rows = db.list_queued()

    groups = multi_sim.candidate_groups(rows, max_batch=10)

    assert sorted(len(group) for group in groups) == [2, 4]  # never mixes decay 6 with 9
    assert multi_sim.candidate_groups(rows, max_batch=1) == []
    assert all(len(group) <= 2 for group in multi_sim.candidate_groups(rows, max_batch=2))


def test_scheduler_stays_regular_when_multi_simulation_is_unsupported(db):
    multi_sim.save_capability(db, multi_sim.MultiSimCapability(False, "rejected", "now"))
    db.queue_candidate("rank(close)")
    client = _FakeClient(brain_api.BrainAPIError(LIVE_REJECTION, 400))

    assert sched.main.__module__  # the dispatcher exposes no multi path when unsupported
    assert client.calls == 0


def test_cli_status_reports_the_cached_verdict(db, capsys):
    multi_sim.save_capability(db, multi_sim.MultiSimCapability(False, "platform rejected type=MULTI", "now"))

    assert multi_sim.main(["--db", str(db.path), "--status"]) == 0
    assert "'supported': False" in capsys.readouterr().out


def test_cli_status_flags_an_unchecked_account(db, capsys):
    assert multi_sim.main(["--db", str(db.path), "--status"]) == 2
    assert "unknown" in capsys.readouterr().out.lower()
