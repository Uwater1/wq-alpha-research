"""One structured field/operator type-compatibility model.

The generator, the static validator, and the persisted ``operator_compatibility``
table all read their rules from here, so a combination can never be "provably
impossible" for one caller and perfectly acceptable for another. Only deterministic
type rules live in this module; anything that depends on catalog freshness stays a
warning in the validator.

Vocabulary
----------

The field snapshot exposes MATRIX, VECTOR and GROUP (plus a handful of UNIVERSE and
SYMBOL) fields, and the operator snapshot declares the argument layout of every
operator. From those two references this module derives:

``VECTOR_AGGREGATORS``
    the only operators that may consume a VECTOR field directly (``vec_avg``/``vec_sum``);
``GROUP_ARGUMENTS``
    the position of the ``group`` argument for each group_* operator, read out of its
    published definition instead of being guessed;
``constraints()``
    one :class:`OperatorConstraint` per operator: input/output type, group/vector
    requirement, and arity.

The rule functions are deliberately free of database and of the field catalog so the
validator can call them without the catalog the store was built against.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
OPERATORS_PATH = REPO_ROOT / "references" / "wq_operators.json"

MATRIX = "MATRIX"
VECTOR = "VECTOR"
GROUP = "GROUP"

#: Operators that turn a VECTOR field into a MATRIX. A VECTOR field is only usable as
#: an argument of one of these, so every other use is a known impossibility.
VECTOR_AGGREGATORS = frozenset({"vec_avg", "vec_sum"})

#: A bare field type -> the set of types it may be consumed as. ``None`` means "not a
#: bare catalog field" (a nested expression), which is never a type error.
_TYPE_ARGUMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*$")


@dataclass(frozen=True)
class OperatorConstraint:
    """Machine-readable signature of one BRAIN operator."""

    name: str
    category: str
    input_type: str
    output_type: str
    requires_group: int
    requires_vector: int
    min_args: int | None
    max_args: int | None
    varargs: bool
    group_argument: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "operator_name": self.name,
            "category": self.category,
            "input_type": self.input_type,
            "output_type": self.output_type,
            "requires_group": self.requires_group,
            "requires_vector": self.requires_vector,
            "min_args": self.min_args,
            "max_args": self.max_args,
            "varargs": self.varargs,
            "group_argument": self.group_argument,
        }


def reference_version(path: str | Path) -> str:
    """Short content hash of a reference snapshot, used as its catalog version."""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return "missing"


def _split_definition_arguments(definition: str) -> list[str]:
    """Argument names written in an operator definition, e.g. ``group_backfill``."""
    text = definition.split("\n")[0].split("\r")[0].strip()
    if "(" not in text or ")" not in text:
        return []
    inner = text[text.find("(") + 1 : text.rfind(")")]
    arguments: list[str] = []
    current: list[str] = []
    depth = 0
    for char in inner:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            arguments.append("".join(current))
            current = []
            continue
        current.append(char)
    arguments.append("".join(current))
    return [argument.strip() for argument in arguments if argument.strip()]


def _arity(definition: str) -> tuple[int | None, int | None, bool]:
    """``(min_args, max_args, varargs)`` read out of the published signature."""
    text = definition.split("\n")[0].split("\r")[0].strip()
    if "(" not in text or ")" not in text:
        return None, None, False
    varargs = "..." in text or ".." in text
    arguments = _split_definition_arguments(definition)
    required = 0
    optional = 0
    for argument in arguments:
        if ".." in argument:
            continue
        if "=" in argument:
            optional += 1
        else:
            required += 1
    maximum = None if varargs else required + optional
    return required, maximum, varargs


@lru_cache(maxsize=4)
def operator_definitions(path: str | Path = OPERATORS_PATH) -> tuple[Mapping[str, Any], ...]:
    """Raw operator snapshot; empty when the reference file is missing or unreadable."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping) and item.get("name"))


@lru_cache(maxsize=4)
def constraints(path: str | Path = OPERATORS_PATH) -> dict[str, OperatorConstraint]:
    """One :class:`OperatorConstraint` per operator name (lowercased)."""
    result: dict[str, OperatorConstraint] = {}
    for item in operator_definitions(path):
        name = str(item["name"]).lower()
        category = str(item.get("category") or "").lower()
        definition = str(item.get("definition") or "")
        minimum, maximum, varargs = _arity(definition)
        requires_vector = int(category == "vector")
        argument_names = [argument.split("=")[0].strip() for argument in _split_definition_arguments(definition)]
        group_argument = argument_names.index("group") if "group" in argument_names else None
        requires_group = int(category == "group" or name.startswith("group_"))
        result[name] = OperatorConstraint(
            name=name,
            category=category,
            input_type=VECTOR if requires_vector else (GROUP if requires_group else MATRIX),
            output_type=MATRIX,
            requires_group=requires_group,
            requires_vector=requires_vector,
            min_args=minimum,
            max_args=maximum,
            varargs=varargs,
            group_argument=group_argument,
        )
    return result


@lru_cache(maxsize=4)
def optional_argument_names(path: str | Path = OPERATORS_PATH) -> dict[str, tuple[str, ...]]:
    """Named optional arguments per operator, e.g. ``hump -> ('hump',)``.

    BRAIN writes these as ``name = value`` in the published definition and requires them
    as keyword arguments: ``hump(x, 0.005)`` is refused with "Invalid number of inputs",
    while ``hump(x, hump=0.005)`` is accepted.
    """
    result: dict[str, tuple[str, ...]] = {}
    for item in operator_definitions(path):
        names: list[str] = []
        for argument in _split_definition_arguments(str(item.get("definition") or "")):
            head, separator, _value = argument.partition("=")
            if separator and head.strip():
                names.append(head.strip())
        result[str(item["name"]).lower()] = tuple(names)
    return result


def named_optional_arguments(operator: str) -> tuple[str, ...]:
    return optional_argument_names().get(str(operator).lower(), ())


def constraint_for(operator: str) -> OperatorConstraint | None:
    return constraints().get(str(operator).lower())


def is_vector_aggregator(operator: str) -> bool:
    return str(operator).lower() in VECTOR_AGGREGATORS


def group_argument_index(operator: str, positional_count: int | None = None) -> int | None:
    """Position of the ``group`` argument, defaulting to the last positional one."""
    constraint = constraint_for(operator)
    if constraint is None or not constraint.requires_group:
        return None
    if constraint.group_argument is not None:
        return constraint.group_argument
    if positional_count:
        return positional_count - 1
    return None


def bare_field_argument(argument: str, field_types: Mapping[str, str]) -> tuple[str, str] | None:
    """``(field_id, type)`` when the argument is a bare catalog field, else ``None``."""
    match = _TYPE_ARGUMENT_RE.match(argument or "")
    if match is None:
        return None
    token = match.group(1)
    field_type = field_types.get(token.lower())
    if field_type is None:
        return None
    return token, str(field_type).upper()


#: Stable finding codes. Callers persist and calibrate on these, so they are part of the
#: public contract and must not be renamed without a migration.
CODE_VECTOR_UNAGGREGATED = "TYPE_VECTOR_UNAGGREGATED"
CODE_VECTOR_OPERATOR_ARGUMENT = "TYPE_VECTOR_OPERATOR_ARGUMENT"
CODE_GROUP_ARGUMENT = "TYPE_GROUP_ARGUMENT"
TYPE_FINDING_CODES = frozenset(
    {CODE_VECTOR_UNAGGREGATED, CODE_VECTOR_OPERATOR_ARGUMENT, CODE_GROUP_ARGUMENT}
)


def type_findings(
    operator: str,
    argument_fields: Sequence[tuple[str, str] | None],
) -> list[tuple[str, str]]:
    """Known-impossible type mismatches for one call, as ``(code, message)`` pairs.

    ``argument_fields`` holds one entry per *positional* argument: ``(field_id, type)``
    for a bare catalog field and ``None`` for anything else (a nested expression, a
    literal, or a field the local snapshot does not know). Unknown fields are never
    reported here — the catalog-scope check in the validator owns that question.
    """
    constraint = constraint_for(operator)
    if constraint is None:
        return []
    findings: list[tuple[str, str]] = []
    first = argument_fields[0] if argument_fields else None
    if (constraint.requires_vector or is_vector_aggregator(operator)) and first is not None:
        if first[1] != VECTOR:
            findings.append((
                CODE_VECTOR_OPERATOR_ARGUMENT,
                f"{constraint.name}() requires a VECTOR field, got {first[1]} field {first[0]!r}",
            ))
    if constraint.requires_group:
        index = group_argument_index(operator, len(argument_fields))
        if index is not None and 0 <= index < len(argument_fields):
            argument = argument_fields[index]
            if argument is not None and argument[1] != GROUP:
                findings.append((
                    CODE_GROUP_ARGUMENT,
                    f"{constraint.name}() requires a GROUP field for its group argument, "
                    f"got {argument[1]} field {argument[0]!r}",
                ))
    return findings


def type_errors(
    operator: str,
    argument_fields: Sequence[tuple[str, str] | None],
) -> list[str]:
    """Messages only (see :func:`type_findings` for the structured form)."""
    return [message for _code, message in type_findings(operator, argument_fields)]


def vector_misuse(field_id: str, field_type: str, aggregated: bool) -> str | None:
    """Rule text for a VECTOR field used outside a vector aggregator."""
    if str(field_type).upper() != VECTOR or aggregated:
        return None
    return (
        f"vector field {field_id!r} must be aggregated with vec_avg/vec_sum "
        "before most operators can consume it"
    )


def vector_misuse_finding(field_id: str, field_type: str, aggregated: bool) -> tuple[str, str] | None:
    """Structured form of :func:`vector_misuse`."""
    message = vector_misuse(field_id, field_type, aggregated)
    return None if message is None else (CODE_VECTOR_UNAGGREGATED, message)


def group_argument_positions(
    operator: str, positional: Sequence[str], field_types: Mapping[str, str]
) -> list[str]:
    """The `group` argument of a group_* call, as written."""
    index = group_argument_index(operator, len(positional))
    if index is None or index >= len(positional):
        return []
    return [positional[index].strip()]
