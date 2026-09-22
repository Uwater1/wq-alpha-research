from __future__ import annotations

import pytest
import field_intelligence
import research_db


@pytest.fixture
def db(tmp_path):
    with research_db.ResearchDB.open(tmp_path / "research.db") as store:
        yield store


def test_operator_compatibility_is_machine_readable():
    items = field_intelligence.operator_compatibility()
    vec = next(item for item in items if item["operator_name"] == "vec_avg")
    group = next(item for item in items if item["operator_name"] == "group_rank")
    assert vec["requires_vector"] == 1
    assert vec["input_type"] == "VECTOR"
    assert group["requires_group"] == 1
    assert group["min_args"] == 2


def test_refresh_tracks_catalog_and_empirical_field_outcomes(db):
    outcome = db.queue_candidate("rank(close)", {"decay": 6}, signal_family="technical")
    result = field_intelligence.refresh(db)
    assert result["fields"] == 4367
    row = db.query("SELECT * FROM field_coverage WHERE field_id='close' AND catalog_version=?", (result["catalog_version"],))[0]
    assert row["cataloged"] == 1
    assert row["validated"] == 1
    assert row["attempts"] == 1
    assert db.query("SELECT COUNT(*) AS n FROM operator_compatibility")[0]["n"] == 66


def test_under_tested_order_prefers_unseen_fields(db):
    db.queue_candidate("rank(close)", {"decay": 6}, signal_family="technical")
    catalog = field_intelligence.Catalog()
    fields = [field for field in catalog.fields if field.name in {"close", "open", "high"}]
    ordered = field_intelligence.under_tested(db, fields)
    assert ordered[0].name != "close"
