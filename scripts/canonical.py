"""Canonicalization of BRAIN simulation requests.

Two requests that would make BRAIN do the same work must hash to the same key,
so the local store can serve one of them from cache and never simulate it twice.

Canonical rules (all are provably semantics-preserving — nothing here decides that
two *different* expressions are equivalent):

    whitespace      collapsed to single spaces, then tightened around ``( ) ,``
    operator case   ``TS_MEAN(...)`` -> ``ts_mean(...)`` for names in the operator
                    reference, so only real operator identifiers are touched
    numbers         ``0.50`` / ``1e-1`` / ``20.0`` -> ``0.5`` / ``0.1`` / ``20``
    commutative     argument order of ``add`` / ``multiply`` / ``max`` / ``min``
                    with exactly two positional arguments is sorted
    settings        defaults filled in, types coerced, enums upper-cased, and
                    ``visualization`` dropped (it does not change the result)

The last rule is the only structural rewrite; it is limited to operators whose
result provably does not depend on argument order, and skipped when an argument
carries a keyword (``plot=``, ``filter=``, ...).

Usage:
    from canonical import canonical_key, normalize_expression, normalize_settings

    key = canonical_key("rank(TS_MEAN(close,20))", {"region": "usa"})

Near-duplicates: ``skeleton_hash`` maps expressions that differ only in numeric
parameters (``ts_mean(close, 20)`` vs ``ts_mean(close, 60)``) to the same value, and
``fields_of`` lists the data fields an expression touches. Both are *flags* for
priority/diversity, never an automatic merge.
"""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATORS_PATH = REPO_ROOT / "references" / "wq_operators.json"

# BRAIN settings that define what a simulation actually computes. Keep in sync with
# legacy/wq_brain/batch_simulate.build_settings (which delegates to normalize_settings).
DEFAULT_SETTINGS: dict[str, Any] = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 6,
    "neutralization": "SUBINDUSTRY",
    "truncation": 0.1,
    "pasteurization": "ON",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
}

# Order used inside the canonical payload (never rely on dict ordering).
KEY_SETTINGS_FIELDS: tuple[str, ...] = (
    "instrumentType",
    "region",
    "universe",
    "delay",
    "decay",
    "neutralization",
    "truncation",
    "pasteurization",
    "unitHandling",
    "nanHandling",
    "language",
)

# Accepted settings that do not change simulation results.
IGNORED_SETTINGS = frozenset({"visualization"})

_INT_SETTINGS = frozenset({"delay", "decay"})
_FLOAT_SETTINGS = frozenset({"truncation"})
_UPPER_SETTINGS = frozenset(
    {"instrumentType", "region", "universe", "neutralization", "pasteurization", "unitHandling", "nanHandling", "language"}
)

# Used only when references/wq_operators.json is missing (fresh clone without the
# snapshot): enough coverage that operator case folding still works for common ops.
_FALLBACK_OPERATOR_NAMES = frozenset(
    """
    abs add and bucket days_from_last_change densify divide equal greater greater_equal
    group_backfill group_mean group_neutralize group_rank group_scale group_zscore hump
    if_else inverse is_nan kth_element last_diff_value less less_equal log max min multiply
    normalize not not_equal or power quantile rank reverse scale sign signed_power sqrt
    subtract trade_when ts_arg_max ts_arg_min ts_av_diff ts_backfill ts_corr ts_count_nans
    ts_covariance ts_decay_linear ts_delay ts_delta ts_mean ts_product ts_quantile ts_rank
    ts_regression ts_scale ts_std_dev ts_step ts_sum ts_zscore vec_avg vec_sum winsorize
    zscore
    """.split()
)

_NUMBER_RE = re.compile(r"(?<![\w.])(\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?(?![\w.])")
_BARE_NUMBER_RE = re.compile(r"\d+\.\d*|\.\d+|\d+")
_IDENT_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMMUTATIVE_OPS = ("add", "max", "min", "multiply")
_COMMUTATIVE_CALL_RE = re.compile(r"\b(" + "|".join(_COMMUTATIVE_OPS) + r")\(", re.IGNORECASE)
_LITERAL_KEYWORDS = frozenset({"true", "false", "nan", "inf", "null", "none"})


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def operator_names() -> frozenset[str]:
    """Lower-cased operator names from references/wq_operators.json (fallback if absent)."""
    try:
        raw = json.loads(OPERATORS_PATH.read_text(encoding="utf-8"))
        names = {str(op["name"]).lower() for op in raw if isinstance(op, dict) and op.get("name")}
    except (OSError, ValueError, TypeError, KeyError):
        return _FALLBACK_OPERATOR_NAMES
    return frozenset(names) if names else _FALLBACK_OPERATOR_NAMES


# ---------------------------------------------------------------------------
# Expression normalization
# ---------------------------------------------------------------------------


def _format_number(value: float) -> str:
    """One spelling per numeric value: 0.50 -> 0.5, 1e-1 -> 0.1, 20.0 -> 20."""
    if value != value or value in (float("inf"), float("-inf")):
        return repr(value)
    if value.is_integer() and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


def _normalize_numbers(text: str) -> str:
    return _NUMBER_RE.sub(lambda m: _format_number(float(m.group(0))), text)


# Operators with no unary form: whitespace around them can never change the parse.
# ``+`` and ``-`` are deliberately absent (unary minus/plus).
_TIGHT_OPERATOR_CHARS = "*/=!<>"


def _tighten_punctuation(text: str) -> str:
    """Drop only the whitespace that can never matter.

    That is whitespace before ``( ) ,``, after ``( ,``, and around the operators in
    ``_TIGHT_OPERATOR_CHARS``. Whitespace after a closing paren is left alone, so
    ``a) - b`` never becomes ``a)-b``, and ``+``/``-`` keep their spacing.
    """
    text = re.sub(r"\s*([(),])", r"\1", text)
    text = re.sub(r"([(,])\s*", r"\1", text)
    return re.sub(rf"\s*([{re.escape(_TIGHT_OPERATOR_CHARS)}])\s*", r"\1", text)


def _matching_paren(text: str, open_index: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at open_index, or None if unbalanced."""
    depth = 0
    for index in range(open_index, len(text)):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _split_top_level(inner: str) -> list[str]:
    """Split a call's argument list on commas that are not inside nested calls."""
    args: list[str] = []
    depth = 0
    current: list[str] = []
    for char in inner:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            args.append("".join(current))
            current = []
            continue
        current.append(char)
    args.append("".join(current))
    return args


def _lowercase_operators(text: str) -> str:
    """Fold the case of identifiers that are known operator names and are being called."""
    known = operator_names()

    def repl(match: re.Match) -> str:
        name = match.group(1)
        return (name.lower() if name.lower() in known else name) + "("

    return _IDENT_CALL_RE.sub(repl, text)


def _sort_commutative_args_once(text: str) -> str:
    """One left-to-right pass; leaves calls that carry keywords for the next pass."""
    out: list[str] = []
    cursor = 0
    position = 0
    while position < len(text):
        match = _COMMUTATIVE_CALL_RE.search(text, position)
        if match is None:
            break
        open_index = match.end() - 1
        close_index = _matching_paren(text, open_index)
        if close_index is None:
            position = match.end()
            continue
        args = _split_top_level(text[open_index + 1 : close_index])
        sortable = len(args) == 2 and all(arg.strip() and "=" not in arg for arg in args)
        out.append(text[cursor : open_index + 1])
        if sortable:
            out.append(",".join(sorted(arg.strip() for arg in args)))
            out.append(")")
            cursor = close_index + 1
            position = close_index + 1
        else:
            # Scan inside the call now; the outer call is retried on a later pass.
            cursor = open_index + 1
            position = open_index + 1
    out.append(text[cursor:])
    return "".join(out)


def _sort_commutative_args(text: str, passes: int = 10) -> str:
    """Sort argument order of commutative calls until the expression stops changing."""
    for _ in range(passes):
        updated = _sort_commutative_args_once(text)
        if updated == text:
            break
        text = updated
    return text


def normalize_expression(expression: Any) -> str:
    """Canonical spelling of a FASTEXPR expression.

    Raises ValueError for a non-string or empty expression: silently stringifying a
    number or a dict would queue a request nobody asked for.
    """
    if not isinstance(expression, str):
        raise ValueError(f"expression must be a string, got {type(expression).__name__}")
    if not expression.strip():
        raise ValueError("expression is empty")

    text = " ".join(expression.split())
    text = _tighten_punctuation(text)
    text = _normalize_numbers(text)
    text = _lowercase_operators(text)
    text = _sort_commutative_args(text)
    return _tighten_punctuation(text).strip()


# ---------------------------------------------------------------------------
# Settings normalization
# ---------------------------------------------------------------------------


def _coerce_setting(field: str, value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(f"{field}={value!r} is not a valid setting value")
    if field in _INT_SETTINGS:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}={value!r} is not a valid int") from exc
    if field in _FLOAT_SETTINGS:
        try:
            return float(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field}={value!r} is not a valid float") from exc
    if field in _UPPER_SETTINGS:
        return str(value).strip().upper()
    return value


def normalize_settings(settings: Mapping[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """Fill defaults, coerce types, upper-case enums; drop result-neutral keys.

    Unknown keys are kept (sorted, JSON-safe) so their hash contribution stays
    deterministic instead of silently colliding with a default request.
    """
    raw: dict[str, Any] = {}
    if settings:
        if not isinstance(settings, Mapping):
            raise ValueError("settings must be a mapping")
        raw.update(settings)
    raw.update(overrides)

    normalized: dict[str, Any] = dict(DEFAULT_SETTINGS)
    extras: dict[str, Any] = {}
    for field, value in raw.items():
        if field in IGNORED_SETTINGS or value is None or (isinstance(value, str) and not value.strip()):
            continue
        if field in DEFAULT_SETTINGS:
            normalized[field] = _coerce_setting(field, value)
        else:
            extras[str(field)] = value
    normalized.update({key: extras[key] for key in sorted(extras)})
    return normalized


def _value_token(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(float(value))
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return str(value).strip()


def settings_payload(settings: Mapping[str, Any] | None = None, **overrides: Any) -> str:
    """Deterministic string form of the settings that affect a simulation result."""
    normalized = normalize_settings(settings, **overrides)
    parts = [f"{field}={_value_token(normalized[field])}" for field in KEY_SETTINGS_FIELDS]
    extras = sorted(set(normalized) - set(DEFAULT_SETTINGS))
    parts.extend(f"{field}={_value_token(normalized[field])}" for field in extras)
    return "|".join(parts)


def expression_hash(expression: Any) -> str:
    return _sha256(normalize_expression(expression))


def settings_hash(settings: Mapping[str, Any] | None = None, **overrides: Any) -> str:
    return _sha256(settings_payload(settings, **overrides))


def canonical_payload(expression: Any, settings: Mapping[str, Any] | None = None, **overrides: Any) -> str:
    normalized = normalize_expression(expression)
    return f"{normalized}\x1f{settings_payload(settings, **overrides)}"


def canonical_key(expression: Any, settings: Mapping[str, Any] | None = None, **overrides: Any) -> str:
    """SHA256 over normalized expression + every result-affecting setting."""
    expression_part = expression_hash(expression)
    settings_part = settings_hash(settings, **overrides)
    return _sha256(f"{expression_part}|{settings_part}")


# ---------------------------------------------------------------------------
# Near-duplicate / structure helpers (flags only — never an automatic merge)
# ---------------------------------------------------------------------------


def skeleton(expression: Any) -> str:
    """Expression with numeric parameters masked, so parameter grids collapse."""
    text = _BARE_NUMBER_RE.sub("#", str(expression or "").lower())
    return re.sub(r"\s+", "", text)


def skeleton_hash(expression: Any) -> str:
    return _sha256(skeleton(expression))


def fields_of(expression: Any) -> tuple[str, ...]:
    """Data fields an expression reads (identifiers that are not a call or a literal)."""
    text = str(expression or "")
    called = {match.group(1) for match in _IDENT_CALL_RE.finditer(text)}
    found = {
        name
        for name in _IDENT_RE.findall(text)
        if name not in called and name.lower() not in _LITERAL_KEYWORDS and name.lower() not in operator_names()
    }
    return tuple(sorted(found))


def operators_of(expression: Any) -> tuple[str, ...]:
    """Operator names used by an expression, lower-cased, sorted."""
    return tuple(sorted({match.group(1).lower() for match in _IDENT_CALL_RE.finditer(str(expression or ""))}))


def structural_features(expression: Any) -> dict[str, Any]:
    """Cheap structural features (recorded per candidate for validation and ranking)."""
    text = str(expression or "")
    return {
        "fields": list(fields_of(text)),
        "operators": list(operators_of(text)),
        "depth": expression_depth(text),
        "literals": [m.group(0) for m in _BARE_NUMBER_RE.finditer(text)],
    }


def expression_depth(text: str) -> int:
    """Maximum call nesting depth of an expression."""
    return _expression_depth(text)


def _expression_depth(text: str) -> int:
    depth = 0
    maximum = 0
    for char in text:
        if char == "(":
            depth += 1
            maximum = max(maximum, depth)
        elif char == ")":
            depth = max(0, depth - 1)
    return maximum
