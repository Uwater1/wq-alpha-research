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
    """Build stable sparse-ish features from a candidate without using account IDs."""
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
    features["attempt_count"] = float(row.get("attempt_count") or 0)
    features["near_duplicate"] = 1.0 if row.get("near_duplicate_of") else 0.0
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


def _ridge_fit(x: np.ndarray, y: np.ndarray, penalty: float) -> list[float]:
    if len(x) == 0:
        return []
    identity = np.eye(x.shape[1], dtype=float)
    identity[0, 0] = 0.0  # do not penalize the bias
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
    rows = _rows(db, training=True)
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
        weights = _ridge_fit(x[indexes], y, penalty)
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
    rows = _rows(db, training=False)
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


def evaluate(db: Any, *, top_k: int = 10) -> dict[str, Any]:
    model = load(db)
    rows = _rows(db, training=True)
    if model is None or not rows:
        return {"available": False, "samples": len(rows)}
    scored = []
    for row in rows:
        prediction = predict(model, row)
        scored.append((prediction.get("is_pass", 0.0), bool(row.get("is_pass"))))
    scored.sort(reverse=True, key=lambda item: item[0])
    top = scored[:top_k]
    return {
        "available": True, "samples": len(rows), "top_k": min(top_k, len(scored)),
        "top_pass_recall": round(sum(actual for _, actual in top) / max(sum(actual for _, actual in scored), 1), 6),
        "top_pass_rate": round(sum(actual for _, actual in top) / max(len(top), 1), 6),
        "model": model.get("diagnostics", {}),
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
