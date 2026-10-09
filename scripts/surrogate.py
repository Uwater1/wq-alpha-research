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
#: Binary gate-failure targets (P24.2): a refused candidate usually fails one specific check,
#: and "will BRAIN refuse this for LOW_SHARPE" is a more learnable question than "will it pass".
FAILURE_TARGET_CHECKS = {"low_sharpe": "LOW_SHARPE", "low_fitness": "LOW_FITNESS"}
#: Targets that are probabilities and must be bounded like one wherever they are consumed.
BINARY_TARGETS = ("is_pass", "low_sharpe", "low_fitness")
TARGETS = ("is_pass", "low_sharpe", "low_fitness", "sharpe", "fitness", "turnover")


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
    """Attach only parent outcomes that existed before the child entered the queue.

    Event IDs are the causal clock. Timestamps are second-granularity and can tie,
    so current parent state alone must never retroactively enrich an older child.
    """
    enriched: list[dict[str, Any]] = []
    parent_cache: dict[int, dict[str, Any]] = {}
    child_queue_events: dict[int, int | None] = {}
    for row in rows:
        item = dict(row)
        parent_id = row.get("parent_id")
        if parent_id:
            try:
                pid = int(parent_id)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                if pid not in parent_cache:
                    try:
                        parent = db.get_candidate(pid)
                    except (AttributeError, KeyError):
                        parent = None
                    payload = dict(parent) if parent else {}
                    result_event = db.query(
                        "SELECT MIN(id) AS event_id FROM events "
                        "WHERE entity='simulation' AND CAST(entity_id AS INTEGER)=? "
                        "AND event='result' AND to_status='DONE'",
                        (pid,),
                    )
                    payload["_outcome_event_id"] = (
                        result_event[0].get("event_id") if result_event else None
                    )
                    parent_cache[pid] = payload
                parent = parent_cache[pid]

                child_id = int(row.get("id") or 0)
                if child_id not in child_queue_events:
                    queued = db.query(
                        "SELECT MIN(id) AS event_id FROM events "
                        "WHERE entity='candidate' AND CAST(entity_id AS INTEGER)=? AND event='queued'",
                        (child_id,),
                    )
                    child_queue_events[child_id] = queued[0].get("event_id") if queued else None
                parent_event = parent.get("_outcome_event_id")
                child_event = child_queue_events[child_id]
                available_at_queue = (
                    isinstance(parent_event, int)
                    and isinstance(child_event, int)
                    and parent_event < child_event
                )
                if parent and available_at_queue:
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
            SELECT c.*, s.completed_at AS outcome_at, s.checks_json AS checks_json,
                   (
                       SELECT MIN(e.id) FROM events e
                       WHERE e.entity='simulation'
                         AND CAST(e.entity_id AS INTEGER)=c.id
                         AND e.event='result'
                         AND e.to_status='DONE'
                   ) AS outcome_event_id
            FROM candidates c
            JOIN simulations s ON s.canonical_key=c.canonical_key
            WHERE c.status IN ('IS_PASS','CORR_PASS','SUBMISSION_READY','SUBMITTING','ACTIVE','REJECTED')
              AND c.attempt_count > 0
              AND s.completed_at IS NOT NULL
            ORDER BY s.completed_at, outcome_event_id, c.id
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


def _checks_rows(row: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """The settled BRAIN checks of one row, or ``None`` when there are none to read."""
    raw = row.get("checks_json")
    if raw is None:
        return None
    try:
        value = json.loads(str(raw))
    except (TypeError, ValueError):
        return None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else None


def _target(row: Mapping[str, Any], target: str) -> float | None:
    if target in FAILURE_TARGET_CHECKS:
        checks = _checks_rows(row)
        if checks is None:
            return None
        wanted = FAILURE_TARGET_CHECKS[target]
        refused = any(
            str(check.get("name") or "").upper() == wanted
            and str(check.get("result") or "").upper() not in ("", "PASS")
            for check in checks
        )
        return 1.0 if refused else 0.0
    if target == "is_pass":
        value = row.get("is_pass")
    else:
        value = row.get(target)
    if isinstance(value, bool):
        return float(value)
    return float(value) if isinstance(value, (int, float)) else None


def training_rows(
    db: Any,
    *,
    before_event_id: int | None = None,
    before_timestamp: str | None = None,
) -> list[dict[str, Any]]:
    """Settled rows usable to train an **as-of** model.

    A caller replaying history must pass ``before_event_id`` so the model can only learn from
    outcomes that had actually settled before the decision it is scoring. ``outcome_event_id``
    is the monotonic ``events.id`` of the terminal simulation result, so the filter is exact;
    timestamps are second-granularity and tie, and are only used as an optional extra cap.
    """
    rows = _enrich_with_parents(db, _rows(db, training=True))
    if before_event_id is not None:
        rows = [
            row for row in rows
            if isinstance(row.get("outcome_event_id"), int) and row["outcome_event_id"] < int(before_event_id)
        ]
    if before_timestamp is not None:
        rows = [row for row in rows if str(row.get("outcome_at") or "") <= str(before_timestamp)]
    return rows


def train_model(rows: list[Mapping[str, Any]], *, penalty: float = 1.0, min_samples: int = 5) -> dict[str, Any]:
    """Fit all targets from the given rows and return the model dict (no persistence).

    Split out from :func:`fit` so an offline replay can build a model from a point-in-time
    slice of history without overwriting the live model on disk.
    """
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
        if target in BINARY_TARGETS:
            predictions = np.clip(predictions, 0.0, 1.0)
        models[target] = {"weights": weights, "samples": len(y)}
        diagnostics[target] = {
            "samples": len(y),
            "mae": round(float(np.mean(np.abs(predictions - y))), 6),
            "mean": round(float(np.mean(y)), 6),
        }
    return {
        "version": 1, "trained_at": datetime.now(timezone.utc).isoformat(),
        "samples": len(rows), "features": feature_names, "penalty": penalty,
        "targets": models, "diagnostics": diagnostics,
    }


def fit(db: Any, *, penalty: float = 1.0, min_samples: int = 5) -> dict[str, Any]:
    """Train all targets on the full settled history and persist the model."""
    model = train_model([dict(row) for row in training_rows(db)], penalty=penalty, min_samples=min_samples)
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
        result[target] = max(0.0, min(1.0, value)) if target in BINARY_TARGETS else value
    return result


# ---------------------------------------------------------------------------
# P24.3: calibration and fixed-budget capture
# ---------------------------------------------------------------------------


def brier_score(actual: Iterable[float], predicted: Iterable[float]) -> float:
    """Mean squared error of a probability forecast (lower is better)."""
    pairs = [(float(a), float(p)) for a, p in zip(actual, predicted)]
    if not pairs:
        return 0.0
    return float(np.mean([(p - a) ** 2 for a, p in pairs]))


def log_loss(actual: Iterable[float], predicted: Iterable[float], *, epsilon: float = 1e-6) -> float:
    """Binary cross-entropy with the prediction clipped away from 0/1 (lower is better)."""
    pairs = [(float(a), float(p)) for a, p in zip(actual, predicted)]
    if not pairs:
        return 0.0
    total = 0.0
    for a, p in pairs:
        q = max(epsilon, min(1.0 - epsilon, p))
        total += a * math.log(q) + (1.0 - a) * math.log(1.0 - q)
    return float(-total / len(pairs))


def reliability_table(actual: Iterable[float], predicted: Iterable[float], *,
                      bins: int = 10) -> list[dict[str, Any]]:
    """Prediction bins with their observed frequency, so calibration is inspectable."""
    pairs = [(float(a), float(p)) for a, p in zip(actual, predicted)]
    if not pairs:
        return []
    width = 1.0 / max(1, int(bins))
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(max(1, int(bins)))]
    for a, p in pairs:
        index = min(len(buckets) - 1, max(0, int(p / width)))
        buckets[index].append((a, p))
    table: list[dict[str, Any]] = []
    for index, members in enumerate(buckets):
        if not members:
            continue
        table.append({
            "bin": index,
            "low": round(index * width, 4),
            "high": round((index + 1) * width, 4),
            "count": len(members),
            "mean_predicted": round(float(np.mean([p for _, p in members])), 6),
            "observed_rate": round(float(np.mean([a for a, _ in members])), 6),
        })
    return table


def calibration_report(actual: Iterable[float], predicted: Iterable[float], *,
                       bins: int = 10) -> dict[str, Any]:
    """Brier / log loss / reliability plus the base rate the model must beat (P24.3)."""
    actual_list = [float(value) for value in actual]
    predicted_list = [float(value) for value in predicted]
    if not actual_list:
        return {"samples": 0}
    base_rate = float(np.mean(actual_list))
    return {
        "samples": len(actual_list),
        "base_rate": round(base_rate, 6),
        # A constant base-rate forecast is the simple prior a complex model has to beat.
        "base_rate_brier": round(base_rate * (1.0 - base_rate), 6),
        "brier": round(brier_score(actual_list, predicted_list), 6),
        "log_loss": round(log_loss(actual_list, predicted_list), 6),
        "reliability": reliability_table(actual_list, predicted_list, bins=bins),
    }


def _capture(order: Sequence[int], actual: Sequence[bool], budget: int) -> int:
    """Actual IS passes among the first ``budget`` rows of one ordering (P24.3)."""
    return sum(1 for index in list(order)[: max(0, int(budget))] if actual[index])


def walk_forward(db: Any, *, folds: int = 4, min_train: int = 5,
                 penalty: float = 1.0, seed: int = 0) -> dict[str, Any]:
    """Expanding-window out-of-sample evaluation of ``is_pass`` (P24.3).

    The chronological 70/30 holdout in :func:`evaluate` is a single split. This refits on every
    prefix and scores the next window, so a favourable split cannot masquerade as a calibrated
    model, and reports the pooled Brier / log loss / reliability across the folds.
    """
    rows = sorted(
        training_rows(db),
        key=lambda row: (str(row.get("outcome_at") or ""), int(row.get("outcome_event_id") or 0),
                         int(row.get("id") or 0)),
    )
    total = len(rows)
    if total < int(min_train) + 1:
        return {"available": False, "reason": "insufficient_history", "samples": total}
    matrix, feature_names = _matrix(rows)
    fold_count = max(1, min(int(folds), total - int(min_train)))
    chunk = max(1, (total - int(min_train)) // fold_count)
    actual: list[bool] = []
    predicted: list[float] = []
    per_fold: list[dict[str, Any]] = []
    for fold in range(fold_count):
        start = int(min_train) + fold * chunk
        stop = total if fold == fold_count - 1 else min(start + chunk, total)
        test_indexes = list(range(start, stop))
        if not test_indexes:
            continue
        usable = [(index, _target(rows[index], "is_pass")) for index in range(0, start)]
        usable = [(index, value) for index, value in usable
                  if value is not None and math.isfinite(value)]
        if len(usable) < int(min_train):
            per_fold.append({"fold": fold, "skipped": True, "train": start})
            continue
        train_indexes = [index for index, _ in usable]
        y_train = np.asarray([value for _, value in usable], dtype=float)
        weights = np.asarray(_ridge_fit(matrix[train_indexes], y_train, penalty, feature_names),
                             dtype=float)
        if len(weights) == matrix.shape[1]:
            fold_pred = np.clip(matrix[test_indexes] @ weights, 0.0, 1.0)
        else:  # pragma: no cover - feature set is derived from the full matrix above
            fold_pred = np.full(len(test_indexes), float(y_train.mean()))
        fold_actual = [bool(rows[index].get("is_pass")) for index in test_indexes]
        actual.extend(fold_actual)
        predicted.extend(float(value) for value in fold_pred)
        per_fold.append({
            "fold": fold,
            "train": len(train_indexes),
            "test": len(test_indexes),
            "pass_rate": round(sum(fold_actual) / len(fold_actual), 6),
            "mean_prediction": round(float(np.mean(fold_pred)), 6),
        })
    if not actual:
        return {"available": False, "reason": "insufficient_history", "samples": total,
                "folds": per_fold}
    return {
        "available": True,
        "samples": total,
        "folds": per_fold,
        "seed": int(seed),
        "calibration": calibration_report(actual, predicted),
    }


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
    rows = sorted(
        _enrich_with_parents(db, _rows(db, training=True)),
        key=lambda r: (
            str(r.get("outcome_at") or ""),
            int(r.get("outcome_event_id") or 0),
            int(r.get("id") or 0),
        ),
    )
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
    # P24.3: calibration of the held-out probability, and the passes a fixed simulation budget
    # captures under this ordering versus the naive alternatives a complex model must beat.
    actual = [bool(value) for value in test_actual]
    predicted = [float(value) for value in test_pred]
    budget = min(20, len(test_rows))
    shuffled = np.random.default_rng(0).permutation(len(test_rows)).tolist()
    capture = {
        "budget": budget,
        "test_size": len(test_rows),
        "available_passes": sum(1 for value in actual if value),
        "surrogate": _capture(order, actual, budget),
        "fifo": _capture(list(range(len(test_rows))), actual, budget),
        "random": _capture(shuffled, actual, budget),
    }
    capture["lift_over_fifo"] = (
        round(capture["surrogate"] / capture["fifo"], 6) if capture["fifo"] else None
    )
    return {
        "available": True, "oos": True, "samples": total,
        "train_samples": len(train_rows), "test_samples": len(test_rows),
        "split": {
            "train_ids": [int(r["id"]) for r in train_rows],
            "test_ids": [int(r["id"]) for r in test_rows],
            "train_max_completed_at": train_rows[-1].get("outcome_at"),
            "test_min_completed_at": test_rows[0].get("outcome_at"),
            "train_max_outcome_event_id": int(train_rows[-1].get("outcome_event_id") or 0),
            "test_min_outcome_event_id": int(test_rows[0].get("outcome_event_id") or 0),
            "train_last_id": int(train_rows[-1]["id"]),
            "test_first_id": int(test_rows[0]["id"]),
        },
        "top_k": len(top),
        "top_pass_recall": round(sum(1 for i in top if test_actual[i]) / max(sum(1 for v in test_actual if v), 1), 6),
        "top_pass_rate": round(sum(1 for i in top if test_actual[i]) / max(len(top), 1), 6),
        "calibration": calibration_report(actual, predicted),
        "passes_per_budget": capture,
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
    walk = sub.add_parser("walk-forward")
    walk.add_argument("--folds", type=int, default=4)
    walk.add_argument("--min-train", dest="min_train", type=int, default=5)
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
        elif args.command == "walk-forward":
            print(json.dumps(walk_forward(db, folds=args.folds, min_train=args.min_train),
                             indent=2, sort_keys=True))
        else:
            print(json.dumps(rank_advisory(db, limit=args.limit), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
