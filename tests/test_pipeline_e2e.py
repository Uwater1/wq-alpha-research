"""Mocked end-to-end Priority 4 pipeline; no credentials or network."""
from __future__ import annotations

import json

import brain_api
import research_db as rdb
import sim_scheduler
import submission_worker
from test_scheduler import FakeBrain, FakeClock


class Response:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)
        self.content = self.text.encode()

    def json(self):
        return self._payload


class PipelineClient:
    def __init__(self, alpha_id: str):
        self.alpha_id = alpha_id
        self.posts: list[str] = []
        self.last_request = {}

    def list_active_alphas(self, *, page_size=100, max_pages=200):
        return []

    def get(self, url, **kwargs):
        self.last_request = {"http_status": 200, "retry_count": 0, "latency_ms": 1.0}
        values = []
        total = 0.0
        for index in range(80):
            total += 1.0 if index % 2 == 0 else -1.0
            values.append(total)
        return Response({
            "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
            "records": [[f"d{index:03d}", value] for index, value in enumerate(values)],
        })

    def submit_alpha(self, alpha_id):
        self.posts.append(alpha_id)
        return {"outcome": "submitted", "detail": ""}

    def submit_checks(self, alpha_id):
        return [{"name": "SELF_CORRELATION", "result": "PASS"}]

    def alpha_status(self, alpha_id):
        return "ACTIVE"

    def close(self):
        pass


def test_generate_queue_simulate_correlate_submit_active_learn(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as db:
        outcome = db.queue_candidate(
            "group_rank(ts_rank(operating_income, 60), subindustry)",
            {"decay": 6}, source="mock-generator", signal_family="fundamental",
        )
        assert outcome.action == "queued"

        fake_brain = FakeBrain(polls_to_finish=1)
        clock = FakeClock()
        scheduler = sim_scheduler.SimulationScheduler(
            db, fake_brain, slots=1, max_runtime=None, clock=clock,
            sleep=clock.sleep, staged_search=False,
        )
        scheduler.run()
        candidate = db.get_candidate(outcome.candidate_id)
        assert candidate["status"] == "SUBMISSION_READY"
        assert candidate["brain_alpha_id"]

        # Empty ACTIVE book is a valid, explicit correlation pass; later checks remain
        # local and the platform SELF_CORRELATION check is still required.
        client = PipelineClient(candidate["brain_alpha_id"])
        submit_clock = FakeClock()
        worker = submission_worker.SubmissionWorker(
            db, client, max_submissions=1, require_correlation=True, max_runtime=None,
            poll_interval=0.0, max_polls=2, clock=submit_clock, sleep=submit_clock.sleep,
        )
        worker.run()
        assert client.posts == [candidate["brain_alpha_id"]]
        assert db.get_candidate(outcome.candidate_id)["status"] == "ACTIVE"
        assert db.counts("submissions") == {"ACTIVE": 1}

        # Learn only after the simulation event exists: this remains an observation, not
        # an automatic global rule.
        materialized = db.materialize_event_observations()
        assert materialized["created"] == 1
        observation = db.observations(claim="simulation_outcome")[0]
        rule_id = db.propose_rule(
            title="Validated baseline structures",
            body="Retain validated structures only after independent review.",
            scope={"region": "USA", "universe": "TOP3000"},
            evidence=[(observation["id"], "support")],
            provenance={"source": "mocked-e2e"}, privacy_class="SANITIZED",
        )
        evaluation = db.evaluate_rule(rule_id, min_support=1, min_independent_groups=1)
        assert evaluation["passed"] is True
        assert db.transition_rule(rule_id, "active", expected_version=1)["state"] == "active"
