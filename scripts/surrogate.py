"""Learned simulation surrogate for candidate ordering.

The surrogate is deliberately advisory: it predicts pass probability and IS metrics for
ranking, but never rejects a candidate. It trains from completed local research.db rows,
using structural fields/operators/settings/family/lineage features and a deterministic
ridge fit implemented with numpy (already a project dependency).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import research_db

MODEL_META_KEY = "surrogate_model_v1"
TARGETS = ("is_pass", "sharpe", "fitness", "turnover")


def _token_features(row: Mapping[str, Any]) -> dict[str, float]:
    """Build stable sparse-ish features from a candidate without using account IDs.

    Pre-simulation stage only: every feature here must be known *before* BRAIN
    runs. Post-outcome columns (sharpe/fitness/turnover/self_corr/is_pass,
    brain ids, checks) are deliberately NEVER featurized, so pre-simulation
    ranking cannot leak the result it is trying to predict. Submission-stage
    signals belong to a separate model, not this one.
    """
    expression = str(row.get("normalized_expression") or row.get("expression") or "")
    features: dict[str, float] = {"bias": 1.0}
    import canonical

    for field in canonical.fields_of(expression):
        features[f"field:{field}"] = 1.0
    for operator in canonical.operators_of(expression):
        features[f"op:{operator}"] = features.get(f"op:{operator}", 0.0) + 1.0
    features["depth"] = float(canonical.expression_depth(expression))
    settings = row.get("settings_json")
    try:
        settings = json.loads(str(settings or "{}"))
    except ValueError:
        settings = {}
    for key in ("region", "universe", "delay", "decay", "neutralization", "nanHandling", "language"):
        if key in settings:
            value = settings[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                features[f"setting:{key}"] = float(value)
            else:
                features[f"setting:{key}={value}"] = 1.0
    family = str(row.get("signal_family") or "unknown")
    features[f"family:{family}"] = 1.0
    features["generation"] = float(row.get("generation") or 0)
    # attempt_count is intentionally excluded: the final retry count of a settled
    # training row was not known at the equivalent pre-simulation prediction point.
    features["near_duplicate"] = 1.0 if row.get("near_duplicate_of") else 0.0
    # Lineage / mutation signals known at queue time (pre-simulation).
    mutation_type = row.get("mutation_type")
    if mutation_type:
        features[f"muttype:{mutation_type}"] = 1.0
    features["has_parent"] = 1.0 if row.get("parent_id") else 0.0
    for key in ("parent_is_pass", "parent_sharpe", "parent_fitness"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            features[key] = float(value)
    # Ranking outputs known before simulation (quality/novelty/risk priors).
    for key in ("expected_quality", "novelty_score", "failure_risk"):
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            features[f"rank:{key}"] = float(value)
    structural = row.get("structural_json")
    if structural:
        try:
            report = json.loads(str(structural))
            for category, count in (report.get("features", {}).get("categories", {}) or {}).items():
                features[f"category:{category}"] = float(count)
            for operator, count in (report.get("features", {}).get("operator_counts", {}) or {}).items():
                features[f"validated_op:{operator}"] = float(count)
        except (TypeError, ValueError):
            pass
    return features


def _enrich_with_parents(db: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach parent outcome metrics (pre-simulation knowledge) for lineage features."""
    enriched: list[dict[str, Any]] = []
    cache: dict[int, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        parent_id = row.get("parent_id")
        if parent_id:
            try:
                pid = int(parent_id)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                if pid not in cache:
                    try:
                        parent = db.get_candidate(pid)
                    except (AttributeError, KeyError):
                        parent = None
                    cache[pid] = dict(parent) if parent else {}
                parent = cache[pid]
                if parent:
                    is_pass = parent.get("is_pass")
                    if isinstance(is_pass, bool):
                        item["parent_is_pass"] = float(is_pass)
                    elif isinstance(is_pass, (int, float)):
                        item["parent_is_pass"] = float(is_pass)
                    for key in ("sharpe", "fitness"):
                        value = parent.get(key)
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            item[f"parent_{key}"] = float(value)
        enriched.append(item)
    return enriched


def _rows(db: Any, *, training: bool) -> list[dict[str, Any]]:
    if training:
        query = """
            SELECT * FROM candidates
            WHERE status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE','REJECTED')
              AND attempt_count > 0
            ORDER BY id
        """
    else:
        query = "SELECT * FROM candidates WHERE status IN ('QUEUED','RETRY','SIMULATING') ORDER BY id"
    return db.query(query)


def _matrix(rows: list[Mapping[str, Any]], feature_names: list[str] | None = None) -> tuple[np.ndarray, list[str]]:
    maps = [_token_features(row) for row in rows]
    names = feature_names or sorted({name for item in maps for name in item})
    matrix = np.zeros((len(rows), len(names)), dtype=float)
    indexes = {name: index for index, name in enumerate(names)}
    for row_index, item in enumerate(maps):
        for name, value in item.items():
            if name in indexes:
                matrix[row_index, indexes[name]] = float(value)
    return matrix, names


def _ridge_fit(x: np.ndarray, y: np.ndarray, penalty: float,
               feature_names: list[str] | None = None) -> list[float]:
    if len(x) == 0:
        return []
    identity = np.eye(x.shape[1], dtype=float)
    # Leave the bias term unpenalized: locate it by name instead of assuming
    # it is column 0 (feature names are sorted, so bias can sit anywhere).
    bias_idx = feature_names.index("bias") if feature_names and "bias" in feature_names else 0
    if 0 <= bias_idx < identity.shape[0]:
        identity[bias_idx, bias_idx] = 0.0
    try:
        weights = np.linalg.solve(x.T @ x + penalty * identity, x.T @ y)
    except np.linalg.LinAlgError:
        weights = np.linalg.pinv(x) @ y
    return [float(value) for value in weights]


def _target(row: Mapping[str, Any], target: str) -> float | None:
    if target == "is_pass":
        value = row.get("is_pass")
    else:
        value = row.get(target)
    if isinstance(value, bool):
        return float(value)
    return float(value) if isinstance(value, (int, float)) else None


def fit(db: Any, *, penalty: float = 1.0, min_samples: int = 5) -> dict[str, Any]:
    """Train all targets and persist coefficients plus quality diagnostics."""
    rows = _enrich_with_parents(db, _rows(db, training=True))
    if len(rows) < min_samples:
        raise ValueError(f"need at least {min_samples} settled candidates, found {len(rows)}")
    x, feature_names = _matrix(rows)
    models: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    for target in TARGETS:
        usable = [(index, _target(row, target)) for index, row in enumerate(rows)]
        usable = [(index, value) for index, value in usable if value is not None and math.isfinite(value)]
        if not usable:
            continue
        indexes = [index for index, _ in usable]
        y = np.asarray([value for _, value in usable], dtype=float)
        weights = _ridge_fit(x[indexes], y, penalty, feature_names)
        predictions = x[indexes] @ np.asarray(weights)
        if target == "is_pass":
            predictions = np.clip(predictions, 0.0, 1.0)
        models[target] = {"weights": weights, "samples": len(y)}
        diagnostics[target] = {
            "samples": len(y),
            "mae": round(float(np.mean(np.abs(predictions - y))), 6),
            "mean": round(float(np.mean(y)), 6),
        }
    model = {
        "version": 1, "trained_at": datetime.now(timezone.utc).isoformat(),
        "samples": len(rows), "features": feature_names, "penalty": penalty,
        "targets": models, "diagnostics": diagnostics,
    }
    db.set_meta(MODEL_META_KEY, json.dumps(model, sort_keys=True))
    return model


def load(db: Any) -> dict[str, Any] | None:
    raw = db.get_meta(MODEL_META_KEY)
    if not raw:
        return None
    try:
        model = json.loads(raw)
    except ValueError:
        return None
    return model if isinstance(model, dict) else None


def predict(model: Mapping[str, Any], row: Mapping[str, Any]) -> dict[str, float]:
    """Return advisory predictions; missing targets are simply omitted."""
    matrix, _ = _matrix([row], list(model.get("features") or []))
    result: dict[str, float] = {}
    for target, payload in (model.get("targets") or {}).items():
        weights = np.asarray(payload.get("weights") or [], dtype=float)
        if len(weights) != matrix.shape[1]:
            continue
        value = float(matrix[0] @ weights)
        result[target] = max(0.0, min(1.0, value)) if target == "is_pass" else value
    return result


def rank_advisory(db: Any, *, limit: int = 20) -> list[dict[str, Any]]:
    model = load(db)
    rows = _enrich_with_parents(db, _rows(db, training=False))
    if model is None:
        return [{"id": row["id"], "prediction": None, "priority": row.get("priority", 0.0)} for row in rows[:limit]]
    scored = []
    for row in rows:
        prediction = predict(model, row)
        # Advisory score only: blend prediction into ordering and keep every candidate.
        quality = prediction.get("is_pass", 0.0)
        scored.append({"id": row["id"], "prediction": prediction,
                       "priority": round(float(row.get("priority") or 0.0) + quality, 6)})
    scored.sort(key=lambda item: (-item["priority"], item["id"]))
    return scored[:limit]


def evaluate(db: Any, *, top_k: int = 10, penalty: float = 1.0) -> dict[str, Any]:
    """Deterministic chronological out-of-sample evaluation.

    The settled rows are split time-aware (earliest 70% train, latest 30%
    test); a temporary model is refit on train only and scored on the held-out
    test slice. Small samples report ``insufficient_oos_data`` instead of a
    misleading in-sample metric. Split boundaries persist with the report.
    """
    rows = sorted(_enrich_with_parents(db, _rows(db, training=True)), key=lambda r: int(r.get("id") or 0))
    total = len(rows)
    if not rows or load(db) is None:
        return {"available": False, "samples": total}
    if total < 8:
        return {"available": False, "reason": "insufficient_oos_data", "samples": total,
                "min_samples_for_oos": 8}
    split_idx = min(max(5, int(total * 0.7)), total - 2)
    train_rows, test_rows = rows[:split_idx], rows[split_idx:]
    train_x, feature_names = _matrix(train_rows)
    test_x, _ = _matrix(test_rows, feature_names)
    # Refit is_pass on train only so the test slice is truly held out.
    usable = [(index, _target(row, "is_pass")) for index, row in enumerate(train_rows)]
    usable = [(index, value) for index, value in usable if value is not None and math.isfinite(value)]
    if not usable:
        return {"available": False, "reason": "insufficient_oos_data", "samples": total,
                "train_samples": len(train_rows), "test_samples": len(test_rows)}
    train_indexes = [index for index, _ in usable]
    y_train = np.asarray([value for _, value in usable], dtype=float)
    weights = np.asarray(_ridge_fit(train_x[train_indexes], y_train, penalty, feature_names), dtype=float)
    test_actual = [bool(row.get("is_pass")) for row in test_rows]
    test_pred = np.clip(test_x @ weights, 0.0, 1.0) if len(weights) == test_x.shape[1] else np.zeros(len(test_rows))
    order = sorted(range(len(test_rows)), key=lambda i: test_pred[i], reverse=True)
    top = order[:min(top_k, len(test_rows))]
    return {
        "available": True, "oos": True, "samples": total,
        "train_samples": len(train_rows), "test_samples": len(test_rows),
        "split": {"train_ids": [int(r["id"]) for r in train_rows],
                  "test_ids": [int(r["id"]) for r in test_rows],
                  "train_max_id": int(train_rows[-1]["id"]),
                  "test_min_id": int(test_rows[0]["id"])},
        "top_k": len(top),
        "top_pass_recall": round(sum(1 for i in top if test_actual[i]) / max(sum(1 for v in test_actual if v), 1), 6),
        "top_pass_rate": round(sum(1 for i in top if test_actual[i]) / max(len(top), 1), 6),
        "model": (load(db) or {}).get("diagnostics", {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db")
    sub = parser.add_subparsers(dest="command", required=True)
    train = sub.add_parser("train")
    train.add_argument("--penalty", type=float, default=1.0)
    train.add_argument("--min-samples", type=int, default=5)
    sub.add_parser("status")
    sub.add_parser("evaluate")
    rank = sub.add_parser("rank")
    rank.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        if args.command == "train":
            print(json.dumps(fit(db, penalty=args.penalty, min_samples=args.min_samples), indent=2, sort_keys=True))
        elif args.command == "status":
            model = load(db)
            print(json.dumps(model or {"available": False}, indent=2, sort_keys=True))
        elif args.command == "evaluate":
            print(json.dumps(evaluate(db), indent=2, sort_keys=True))
        else:
            print(json.dumps(rank_advisory(db, limit=args.limit), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
