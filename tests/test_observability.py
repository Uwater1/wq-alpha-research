"""Offline Priority 3 observability tests."""
from __future__ import annotations

import json
import sqlite3

import brain_api
import research_db as rdb


class Response:
    status_code = 200
    headers = {}
    text = "{}"
    content = b"{}"

    def json(self):
        return {}


class Session:
    def request(self, method, url, **kwargs):
        return Response()

    def close(self):
        pass


def test_brain_client_exposes_non_sensitive_request_telemetry():
    client = brain_api.BrainClient(session=Session())
    client.get("https://api.worldquantbrain.com/health")
    assert client.last_request["method"] == "GET"
    assert client.last_request["http_status"] == 200
    assert client.last_request["retry_count"] == 0
    assert client.last_request["latency_ms"] >= 0
    assert "password" not in json.dumps(client.last_request).lower()


def test_existing_pre_observability_events_table_is_upgraded_before_indexing(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
        "entity TEXT NOT NULL, entity_id TEXT, event TEXT NOT NULL, from_status TEXT, "
        "to_status TEXT, payload_json TEXT)"
    )
    connection.commit()
    connection.close()

    with rdb.ResearchDB.open(path) as db:
        columns = {row["name"] for row in db.query("PRAGMA table_info(events)")}
        assert {"operation", "http_status", "latency_ms", "result_class"} <= columns
        db.log_event("transport", "legacy", "http", operation="migration", result_class="ok")
        assert db.query("SELECT operation FROM events WHERE entity_id='legacy'")[0]["operation"] == "migration"


def test_events_store_transport_dimensions_and_stats_rates(tmp_path):
    with rdb.ResearchDB.open(tmp_path / "research.db") as db:
        db.log_event("transport", "1", "http", operation="simulation.submit", candidate_id=1,
                     simulation_id="SIM-LOCAL", http_status=201, retry_count=1,
                     latency_ms=12.5, rate_limit_seconds=0, result_class="accepted")
        db.log_event("transport", "1", "http", operation="simulation.poll", candidate_id=1,
                     simulation_id="SIM-LOCAL", http_status=200, latency_ms=4.0, result_class="done")
        event = db.query("SELECT * FROM events WHERE operation='simulation.submit'")[0]
        assert event["http_status"] == 201
        assert event["retry_count"] == 1
        assert event["latency_ms"] == 12.5
        assert event["result_class"] == "accepted"
        payload = db.stats().as_dict()
        for key in (
            "cache_hit_rate", "simulation_success_rate", "is_pass_rate",
            "correlation_pass_rate", "submission_success_rate", "active_alphas_per_week",
            "simulations_per_is_pass", "simulations_per_active_alpha",
        ):
            assert key in payload
