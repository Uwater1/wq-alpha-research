"""Static pre-screening of candidates before they can spend a BRAIN slot.

BRAIN charges 1-3 minutes of platform time for a request it will reject anyway, so the
cheapest capacity win is refusing provably-broken work locally. This module checks an
expression and its settings against the local references:

    references/wq_operators.json                    66 operators with signatures
    references/wq_usa_top3000_delay1_data_fields.json 4,367 fields, USA TOP3000 delay 1

Severity split (errors reject, warnings only flag):

    errors    malformed structure, an operator that does not exist, wrong argument
              count, invalid settings values, and a field that is unknown *inside the
              scope the catalog actually covers*
    warnings  everything that depends on catalog freshness or on judgement: a field
              used outside the catalog scope, a vector field that was not aggregated
              with vec_avg/vec_sum, an unknown keyword, deep nesting. Warnings lower a
               candidate's priority instead of rejecting it, exactly as designed.

Field checks only fire when the requested region/universe/delay is the scope the
snapshot covers (USA/TOP3000/delay 1); for any other scope unknown names are warnings,
so a valid CHN alpha is never rejected by a USA catalog.

Usage:
    from validate import validate

    report = validate("group_rank(ts_rank(operating_incom, 126), subindustry)")
    report.ok        # False
    report.errors    # ["unknown field 'operating_incom' (did you mean 'operating_income'?)"]
    report.features  # categories, operators, depth, windows, groups
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import canonical

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATORS_PATH = REPO_ROOT / "references" / "wq_operators.json"
CATALOG_PATH = REPO_ROOT / "references" / "wq_usa_top3000_delay1_data_fields.json"

# Scope the local field snapshot covers; anything else downgrades field checks to warnings.
CATALOG_SCOPE = {"region": "USA", "universe": "TOP3000", "delay": 1}

# Settings enums documented in SKILL.md / the platform payload examples.
NEUTRALIZATIONS = frozenset({"NONE", "MARKET", "SECTOR", "INDUSTRY", "SUBINDUSTRY", "COUNTRY", "STATISTICAL"})
NAN_HANDLING = frozenset({"ON", "OFF"})
UNIT_HANDLING = frozenset({"VERIFY", "NONE"})
PASTEURIZATION = frozenset({"ON", "OFF"})
LANGUAGES = frozenset({"FASTEXPR", "PYTHON"})
INSTRUMENT_TYPES = frozenset({"EQUITY"})
DELAYS = frozenset({0, 1})
DECAY_RANGE = (0, 512)
LITERAL_KEYWORDS = frozenset({"true", "false", "nan", "inf", "none", "null"})

#: Operators that consume vector fields directly; a VECTOR field outside one of these
#: is a warning, because BRAIN requires aggregation before most operators.
VECTOR_OPERATORS = frozenset({"vec_avg", "vec_sum"})

#: Operators whose `group` argument must be a GROUP-type field.
GROUP_OPERATORS = frozenset({"group_rank", "group_neutralize", "group_zscore", "group_scale", "group_backfill", "group_mean"})

DEEP_DEPTH_WARNING = 8
QUOTES = "\"'“”‘’"
_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\(")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d*)?(?:[eE][+-]?\d+)?(?![\w.])")


@dataclass(frozen=True)
class OperatorSpec:
    """Signature parsed out of the operator reference's `definition`."""

    name: str
    required: int
    optional: int
    varargs: bool
    symbolic: bool

    @property
    def maximum(self) -> float:
        return float("inf") if self.varargs else self.required + self.optional


@dataclass
class ValidationReport:
    """Outcome of a static check, plus the structural features worth storing."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)
    scope_checked: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def penalty(self) -> float:
        """Priority penalty for the ranking step: flagged work is queued behind clean work."""
        return min(0.05 * len(self.warnings), 0.35)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "scope_checked": self.scope_checked,
            "features": self.features,
        }


def _split_args(inner: str) -> list[str]:
    """Split a call's arguments on top-level commas, ignoring commas inside quotes."""
    args: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in inner:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in QUOTES:
            quote = char
            current.append(char)
            continue
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


def _mask_quotes(text: str) -> str:
    """Blank out quoted content so no scan matches inside a string literal.

    The filler is '#' rather than a letter: a letter would look like another field.
    """
    out: list[str] = []
    quote: str | None = None
    for char in text:
        if quote is not None:
            if char == quote:
                quote = None
                out.append(char)
            else:
                out.append("#")
            continue
        if char in QUOTES:
            quote = char
        out.append(char)
    return "".join(out)


def _parse_signature(name: str, definition: str) -> OperatorSpec:
    """`ts_regression(y, x, d, lag = 0, rettype = 0)` -> required 3, optional 2."""
    text = definition.split("\n")[0].split("\r")[0].strip()
    head = text.split("(")[0].strip()
    symbolic = name not in head
    varargs = "..." in text or ".." in text
    if symbolic or "(" not in text or ")" not in text:
        return OperatorSpec(name, 0, 0, varargs, symbolic)
    inner = text[text.find("(") + 1 : text.rfind(")")]
    required = optional = 0
    for argument in _split_args(inner):
        argument = argument.strip()
        if ".." in argument:  # `max(x, y, ..)` / `min(x, y ..)`: not a real argument
            varargs = True
            argument = argument.replace("..", "").strip()
        if not argument:
            continue
        if "=" in argument:
            optional += 1
        else:
            required += 1
    return OperatorSpec(name, required, optional, varargs, symbolic)


@lru_cache(maxsize=1)
def operator_specs() -> dict[str, OperatorSpec]:
    try:
        raw = json.loads(OPERATORS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(op["name"]).lower(): _parse_signature(str(op["name"]).lower(), str(op.get("definition", "")))
        for op in raw
        if isinstance(op, dict) and op.get("name")
    }


@dataclass(frozen=True)
class FieldInfo:
    id: str
    type: str
    category: str
    dataset: str


@lru_cache(maxsize=1)
def field_catalog() -> dict[str, FieldInfo]:
    """USA TOP3000 delay=1 field snapshot (empty when the reference file is missing)."""
    try:
        raw = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    catalog: dict[str, FieldInfo] = {}
    for entry in raw:
        if not isinstance(entry, Mapping) or not entry.get("id"):
            continue
        catalog[str(entry["id"]).lower()] = FieldInfo(
            id=str(entry["id"]),
            type=str(entry.get("type") or "").upper(),
            category=str((entry.get("category") or {}).get("id") or ""),
            dataset=str((entry.get("dataset") or {}).get("id") or ""),
        )
    return catalog


def catalog_covers(settings: Mapping[str, Any] | None) -> bool:
    """True when the requested scope is the one the local field snapshot describes."""
    normalized = canonical.normalize_settings(settings)
    return (
        str(normalized["region"]).upper() == CATALOG_SCOPE["region"]
        and str(normalized["universe"]).upper() == CATALOG_SCOPE["universe"]
        and int(normalized["delay"]) == CATALOG_SCOPE["delay"]
    )


def _closest(field: str, known: Iterable[str]) -> str | None:
    """Cheap typo hint: same length class and a long common prefix/suffix."""
    best: tuple[int, str] | None = None
    for candidate in known:
        if abs(len(candidate) - len(field)) > 3:
            continue
        prefix = 0
        for left, right in zip(field, candidate):
            if left != right:
                break
            prefix += 1
        score = prefix + (2 if candidate.endswith(field[-2:]) else 0)
        if prefix >= 4 and (best is None or score > best[0]):
            best = (score, candidate)
    return best[1] if best else None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def validate_settings(settings: Mapping[str, Any] | None) -> list[str]:
    """Errors for settings BRAIN cannot accept ('impossible settings')."""
    errors: list[str] = []
    try:
        normalized = canonical.normalize_settings(settings)
    except ValueError as exc:
        return [str(exc)]

    if str(normalized["region"]).upper() not in {"USA", "CHN", "EUR", "ASI", "GLB", "JPN", "KOR", "TWN", "HKG", "IND"}:
        errors.append(f"unknown region {normalized['region']!r}")
    if not str(normalized["universe"]).strip():
        errors.append("universe is empty")
    if int(normalized["delay"]) not in DELAYS:
        errors.append(f"delay {normalized['delay']!r} is not one of {sorted(DELAYS)}")
    decay = int(normalized["decay"])
    if not (DECAY_RANGE[0] <= decay <= DECAY_RANGE[1]):
        errors.append(f"decay {decay} is outside {DECAY_RANGE[0]}..{DECAY_RANGE[1]}")
    truncation = float(normalized["truncation"])
    if not (0.0 < truncation <= 1.0):
        errors.append(f"truncation {truncation} is not in (0, 1]")
    for field_name, allowed in (
        ("neutralization", NEUTRALIZATIONS),
        ("nanHandling", NAN_HANDLING),
        ("unitHandling", UNIT_HANDLING),
        ("pasteurization", PASTEURIZATION),
        ("language", LANGUAGES),
        ("instrumentType", INSTRUMENT_TYPES),
    ):
        value = str(normalized[field_name]).upper()
        if value not in allowed:
            errors.append(f"{field_name} {value!r} is not one of {sorted(allowed)}")
    return errors


# ---------------------------------------------------------------------------
# Expressions
# ---------------------------------------------------------------------------


def validate(
    expression: Any,
    settings: Mapping[str, Any] | None = None,
    *,
    check_fields: bool = True,
    catalog: Mapping[str, FieldInfo] | None = None,
    operators: Mapping[str, OperatorSpec] | None = None,
) -> ValidationReport:
    """Statically screen one candidate; never raises for a bad expression."""
    report = ValidationReport()
    try:
        normalized = canonical.normalize_expression(expression)
    except ValueError as exc:
        report.errors.append(f"malformed: {exc}")
        return report

    catalog = field_catalog() if catalog is None else catalog
    operators = operator_specs() if operators is None else operators
    report.scope_checked = check_fields and catalog_covers(settings)
    masked = _mask_quotes(normalized)
    report.errors.extend(validate_settings(settings))

    # 1. structure
    if masked.count("(") != masked.count(")"):
        report.errors.append("malformed: unbalanced parentheses")
    if re.search(r"\(\s*,", masked) or re.search(r",\s*\)", masked) or re.search(r",\s*,", masked):
        report.errors.append("malformed: empty argument in a call")

    # 2. calls: known operator, argument count, keyword arguments
    call_spans: list[tuple[int, int]] = []
    for match in _CALL_RE.finditer(masked):
        name = match.group(1)
        lower = name.lower()
        open_index = match.end() - 1
        close_index = _matching_paren(masked, open_index)
        if close_index is None:
            report.errors.append(f"malformed: unbalanced parentheses in {name}(")
            continue
        call_spans.append((open_index, close_index))
        arguments = _split_args(masked[open_index + 1 : close_index])
        positional = [a.strip() for a in arguments if a.strip() and "=" not in a]
        spec = operators.get(lower)
        if spec is None:
            report.errors.append(f"unknown operator {name!r}")
            continue
        if not spec.symbolic:
            if len(positional) < spec.required:
                report.errors.append(f"{lower} needs at least {spec.required} argument(s), got {len(positional)}")
            elif len(positional) > spec.maximum:
                report.errors.append(f"{lower} accepts at most {int(spec.maximum)} argument(s), got {len(positional)}")
        known_keywords = {"filter", "rate", "std", "constant", "driver", "sigma", "lag", "rettype", "dense",
                          "lookback", "k", "ignore", "hump", "range", "buckets", "skipBoth", "NaNGroup",
                          "useStd", "limit", "scale", "longscale", "shortscale", "group", "weight"}
        for argument in arguments:
            if "=" in argument:
                keyword = argument.split("=", 1)[0].strip()
                if keyword and keyword not in known_keywords:
                    report.warnings.append(f"unknown keyword argument {keyword!r} on {lower}()")

    # 3. fields: unknown names, vector misuse, group arguments
    called = {match.group(1).lower() for match in _CALL_RE.finditer(masked)}
    keywords = {match.group(1) for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)\s*=", masked)}
    # Keyword values are settings, not fields: the reference writes `driver = gaussian`.
    keyword_values = {match.group(1) for match in re.finditer(r"=\s*([A-Za-z_][A-Za-z0-9_]*)", masked)}
    bare = [
        token for token in _IDENT_RE.findall(masked)
        if token.lower() not in called
        and token.lower() not in LITERAL_KEYWORDS
        and token.lower() not in operators
        and token not in keywords
        and token not in keyword_values
    ]
    fields_used: dict[str, FieldInfo] = {}
    for token in bare:
        info = catalog.get(token.lower())
        if info is None:
            if report.scope_checked:
                hint = _closest(token.lower(), catalog)
                report.errors.append(f"unknown field {token!r}" + (f" (did you mean {hint!r}?)" if hint else ""))
            else:
                report.warnings.append(f"field {token!r} is not in the local snapshot (outside its scope)")
            continue
        fields_used[info.id] = info

    if fields_used:
        for info in fields_used.values():
            if info.type == "VECTOR" and not _inside_vector_operator(masked, info.id, call_spans):
                report.warnings.append(
                    f"vector field {info.id!r} should be aggregated with vec_avg/vec_sum before use"
                )
        for call in GROUP_OPERATORS & called:
            for group_argument in _group_arguments(masked, call):
                info = catalog.get(group_argument.lower())
                if info is not None and info.type != "GROUP":
                    report.warnings.append(f"{call}(..., {group_argument}) expects a GROUP field, got a {info.type} field")

    depth = canonical.expression_depth(normalized)
    if depth > DEEP_DEPTH_WARNING:
        report.warnings.append(f"expression depth {depth} is deep (>{DEEP_DEPTH_WARNING})")

    report.features = {
        "fields": sorted(fields_used),
        "field_types": _count(items.type for items in fields_used.values()),
        "categories": _count(items.category for items in fields_used.values() if items.category),
        "datasets": sorted({items.dataset for items in fields_used.values() if items.dataset}),
        "operators": sorted(called),
        "operator_counts": _count(called),
        "groups": sorted({arg.lower() for call in GROUP_OPERATORS for arg in _group_arguments(masked, call)}),
        "depth": depth,
        "windows": _windows_of(masked),
        "warnings": list(report.warnings),
    }
    return report


def _windows_of(masked: str) -> list[int]:
    """Integer literals >= 2 — the lookback-style parameters worth tracking."""
    windows: set[int] = set()
    for match in _NUMBER_RE.finditer(masked):
        try:
            value = float(match.group(0))
        except ValueError:
            continue
        if value >= 2 and value.is_integer() and value < 1e9:
            windows.add(int(value))
    return sorted(windows)


def _count(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _matching_paren(text: str, open_index: int) -> int | None:
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _inside_vector_operator(masked: str, field_id: str, call_spans: list[tuple[int, int]]) -> bool:
    """True when every use of the field sits inside a vec_* call."""
    pattern = re.compile(rf"\b{re.escape(field_id)}\b", re.IGNORECASE)
    occurrences = [match.start() for match in pattern.finditer(masked)]
    if not occurrences:
        return True
    vector_calls = [
        (open_index, close_index)
        for match in _CALL_RE.finditer(masked)
        if match.group(1).lower() in VECTOR_OPERATORS
        for open_index, close_index in [(match.end() - 1, _matching_paren(masked, match.end() - 1))]
        if close_index is not None
    ]
    return all(any(start < index < end for start, end in vector_calls) for index in occurrences)


def _group_arguments(masked: str, operator: str) -> list[str]:
    """The `group` argument of a group_* call, as written."""
    found: list[str] = []
    for match in re.finditer(rf"\b{operator}\(", masked):
        close_index = _matching_paren(masked, match.end() - 1)
        if close_index is None:
            continue
        arguments = [a.strip() for a in _split_args(masked[match.end() : close_index])]
        positional = [a for a in arguments if a and "=" not in a and "(" not in a]
        if operator in ("group_backfill", "group_mean"):
            if len(positional) >= 3:
                found.append(positional[2])
        elif positional:
            found.append(positional[-1])
    return found
