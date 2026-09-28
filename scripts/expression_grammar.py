"""Typed expression grammar for Generator V3 (P1) plus structural identities (P0.2).

This module is the *grammar* layer of the diversity-first generator. It owns:

* a small typed AST (:class:`FieldNode`, :class:`LiteralNode`, :class:`CallNode`);
* operator signature checks delegated to :mod:`compatibility` (never a second type system);
* a complexity budget (``max_depth``/``max_nodes``/``max_fields``/``max_binary_ops``);
* deterministic rendering to FASTEXPR, only after the tree is valid;
* ``grammar_skeleton`` / ``semantic_skeleton`` (and their hashes) that separate
  "same topology, different fields" from "same topology, different economic source";
* a weighted :func:`grammar_distance`; and
* the motif registry (:data:`MOTIFS`) that turns economic hypotheses into typed ASTs.

Nothing here calls BRAIN, reads credentials, or touches the database, so grammar tests
stay offline and deterministic.

Vocabulary
----------

``grammar_skeleton`` preserves operator topology and field *type* while masking exact
field ids and every numeric/string literal::

    group_rank(ts_rank(<FIELD:MATRIX>, #), <GROUP>)

``semantic_skeleton`` replaces each source field with ``<dataset:category:type>``::

    subtract(rank(<analyst4:analyst:MATRIX>), rank(<fundamental2:fundamental:MATRIX>))

They are deliberately additive: the pre-existing ``canonical.skeleton_hash`` is left
untouched and keeps collapsing numeric parameter grids around the same exact fields.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field as dataclass_field, replace
from typing import Any, Callable, Iterable, Mapping, Sequence

import compatibility

GRAMMAR_VERSION = "expression-grammar-v1"
MOTIF_REGISTRY_VERSION = "motif-registry-v1"

MATRIX = compatibility.MATRIX
VECTOR = compatibility.VECTOR
GROUP = compatibility.GROUP
UNKNOWN = "UNKNOWN"

#: Field types that represent an economic *source* (as opposed to a structural argument
#: such as a group literal). Only these count toward source profiles and dataset sets.
SOURCE_TYPES = (MATRIX, VECTOR)

_BINARY_OPERATORS = frozenset({"add", "subtract", "multiply", "divide", "max", "min", "power"})
_NUMBER_RE = re.compile(r"\d+\.\d*|\.\d+|\d+(?:[eE][+-]?\d+)?")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class GrammarError(ValueError):
    """Raised when a node would be a known deterministic type/arity error."""


# ---------------------------------------------------------------------------
# AST
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldNode:
    field_id: str
    value_type: str = MATRIX
    dataset: str = "unknown"
    category: str = "unknown"

    @property
    def is_source(self) -> bool:
        return self.value_type in SOURCE_TYPES


@dataclass(frozen=True)
class LiteralNode:
    value: Any
    literal_type: str = "number"


@dataclass(frozen=True)
class CallNode:
    operator: str
    args: tuple["ExprNode", ...]
    output_type: str = MATRIX
    #: Keyword arguments such as ``hump=0.005`` or ``std=4``, in written order.
    keywords: tuple[tuple[str, "ExprNode"], ...] = ()


ExprNode = FieldNode | LiteralNode | CallNode


# ---------------------------------------------------------------------------
# Complexity budget
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ComplexityLimits:
    """Generator limits — not platform-validity assumptions."""

    max_depth: int = 5
    max_nodes: int = 16
    max_fields: int = 2
    max_binary_ops: int = 2


DEFAULT_LIMITS = ComplexityLimits()


def _walk(node: ExprNode) -> Iterable[ExprNode]:
    yield node
    if isinstance(node, CallNode):
        for argument in node.args:
            yield from _walk(argument)
        for _name, value in node.keywords:
            yield from _walk(value)


def node_depth(node: ExprNode) -> int:
    if not isinstance(node, CallNode):
        return 1
    children = list(node.args) + [value for _name, value in node.keywords]
    return 1 + (max((node_depth(child) for child in children), default=0))


def node_count(node: ExprNode) -> int:
    return sum(1 for _ in _walk(node))


def source_fields(node: ExprNode) -> tuple[FieldNode, ...]:
    """Distinct economic source fields, first-seen order (structural GROUP args excluded)."""
    seen: dict[str, FieldNode] = {}
    for item in _walk(node):
        if isinstance(item, FieldNode) and item.is_source and item.field_id not in seen:
            seen[item.field_id] = item
    return tuple(seen.values())


def binary_op_count(node: ExprNode) -> int:
    return sum(1 for item in _walk(node) if isinstance(item, CallNode) and item.operator in _BINARY_OPERATORS)


def measure(node: ExprNode) -> dict[str, int]:
    return {
        "depth": node_depth(node),
        "nodes": node_count(node),
        "fields": len(source_fields(node)),
        "binary_ops": binary_op_count(node),
    }


def check_complexity(node: ExprNode, limits: ComplexityLimits = DEFAULT_LIMITS) -> list[str]:
    """Return the budget violations for ``node`` (empty means the node fits)."""
    measured = measure(node)
    violations: list[str] = []
    if measured["depth"] > limits.max_depth:
        violations.append(f"depth {measured['depth']} > {limits.max_depth}")
    if measured["nodes"] > limits.max_nodes:
        violations.append(f"nodes {measured['nodes']} > {limits.max_nodes}")
    if measured["fields"] > limits.max_fields:
        violations.append(f"fields {measured['fields']} > {limits.max_fields}")
    if measured["binary_ops"] > limits.max_binary_ops:
        violations.append(f"binary_ops {measured['binary_ops']} > {limits.max_binary_ops}")
    return violations


# ---------------------------------------------------------------------------
# Rendering and construction
# ---------------------------------------------------------------------------


def _format_number(value: Any) -> str:
    number = float(value)
    if number.is_integer() and abs(number) < 1e16:
        return str(int(number))
    return repr(number)


def render(node: ExprNode) -> str:
    """Render a valid AST to FASTEXPR. Callers must have built it through :func:`make_call`."""
    if isinstance(node, FieldNode):
        return node.field_id
    if isinstance(node, LiteralNode):
        if node.literal_type == "bool":
            return "true" if node.value else "false"
        if node.literal_type == "string":
            return f'"{node.value}"' if _needs_quotes(str(node.value)) else str(node.value)
        return _format_number(node.value)
    if isinstance(node, CallNode):
        if node.operator in COMPARISON_INFIX and len(node.args) == 2 and not node.keywords:
            # Comparisons are parsed from infix form, so render them back the same way.
            return f"({render(node.args[0])}{COMPARISON_INFIX[node.operator]}{render(node.args[1])})"
        parts = [render(argument) for argument in node.args]
        parts.extend(f"{name}={render(value)}" for name, value in node.keywords)
        return f"{node.operator}({','.join(parts)})"
    raise TypeError(f"not an expression node: {node!r}")


def _needs_quotes(text: str) -> bool:
    return not _IDENT_RE.fullmatch(text)


#: Bare identifiers and numeric literals, used only by the skeleton fallback path.
_IDENT_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NUMBER_TOKEN_RE = re.compile(r"\d+\.?\d*(?:[eE][+-]?\d+)?")


def literal(value: Any, literal_type: str | None = None) -> LiteralNode:
    if literal_type is not None:
        return LiteralNode(value, literal_type)
    if isinstance(value, bool):
        return LiteralNode(value, "bool")
    if isinstance(value, str):
        return LiteralNode(value, "string")
    return LiteralNode(value, "number")


def _argument_fields(args: Sequence[ExprNode]) -> list[tuple[str, str] | None]:
    fields: list[tuple[str, str] | None] = []
    for argument in args:
        if isinstance(argument, FieldNode):
            fields.append((argument.field_id, argument.value_type))
        else:
            fields.append(None)
    return fields


def validate_call(operator: str, args: Sequence[ExprNode]) -> None:
    """Reject a call that is a known deterministic type/arity error.

    Arity is checked against the published signature; type mismatches are delegated to
    :mod:`compatibility` so the grammar and the queue agree on what is impossible.
    """
    constraint = compatibility.constraint_for(operator)
    if constraint is None:
        raise GrammarError(f"unknown operator {operator!r}")
    positional = len(args)
    if constraint.min_args is not None and positional < constraint.min_args:
        raise GrammarError(f"{operator}() needs at least {constraint.min_args} args, got {positional}")
    if constraint.max_args is not None and positional > constraint.max_args:
        raise GrammarError(f"{operator}() accepts at most {constraint.max_args} args, got {positional}")
    findings = compatibility.type_findings(operator, _argument_fields(args))
    if findings:
        raise GrammarError("; ".join(message for _code, message in findings))


def make_call(
    operator: str,
    args: Sequence[ExprNode],
    *,
    keywords: Mapping[str, ExprNode] | Sequence[tuple[str, ExprNode]] = (),
    validate: bool = True,
) -> CallNode:
    """Create a validated :class:`CallNode`; the AST is only built when the call is legal."""
    name = str(operator).lower()
    keyword_items = tuple(keywords.items()) if isinstance(keywords, Mapping) else tuple(keywords)
    positional = tuple(args)
    if validate:
        valid_names = set(compatibility.named_optional_arguments(name))
        for key, _value in keyword_items:
            if valid_names and key not in valid_names:
                raise GrammarError(f"{name}() has no keyword argument {key!r}")
        validate_call(name, positional)
    constraint = compatibility.constraint_for(name)
    output_type = constraint.output_type if constraint else MATRIX
    return CallNode(name, positional, output_type, keyword_items)


def vector_value(node: FieldNode) -> ExprNode:
    """Wrap a VECTOR field in ``vec_avg`` so it may feed matrix operators."""
    if node.value_type != VECTOR:
        return node
    return make_call("vec_avg", [node])


# ---------------------------------------------------------------------------
# Parsing FASTEXPR into an AST (tolerant: used for skeletons and diagnostics)
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?:(?P<number>\d+\.\d*|\.\d+|\d+(?:[eE][+-]?\d+)?)"
    r"|(?P<string>\"[^\"]*\"|'[^']*')"
    r"|(?P<ident>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>>=|<=|==|!=|<>|[()+\-*/,=<>=!]))"
)

#: Comparison spellings -> the catalog operator that carries the same meaning. The skeleton
#: only needs a stable, comparable name; rendering turns them back into infix form.
COMPARISON_OPERATORS: dict[str, str] = {
    ">": "greater", "<": "less", ">=": "greater_equal", "<=": "less_equal",
    "==": "equal", "=": "equal", "!=": "not_equal", "<>": "not_equal",
}
#: Infix spelling for the comparison operators, used when rendering an AST back to FASTEXPR.
COMPARISON_INFIX: dict[str, str] = {
    "greater": ">", "less": "<", "greater_equal": ">=", "less_equal": "<=",
    "equal": "==", "not_equal": "!=",
}


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        match = _TOKEN_RE.match(text, position)
        if match is None:
            remainder = text[position:].strip()
            if not remainder:
                break
            # Unknown character: keep it as an opaque identifier so parsing never crashes
            # on an expression shape this tolerance layer does not model.
            tokens.append(("ident", remainder[0]))
            position += text[position:].index(remainder[0]) + 1
            continue
        position = match.end()
        kind = match.lastgroup or "ident"
        tokens.append((kind, match.group(kind)))
    return tokens


class _Parser:
    def __init__(self, tokens: Sequence[tuple[str, str]], fields: Mapping[str, Any] | None) -> None:
        self.tokens = list(tokens)
        self.fields = fields or {}
        self.index = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        if self.index >= len(self.tokens):
            raise GrammarError("unexpected end of expression")
        token = self.tokens[self.index]
        self.index += 1
        return token

    def expect(self, kind: str, value: str | None = None) -> tuple[str, str]:
        token = self.next()
        if token[0] != kind or (value is not None and token[1] != value):
            raise GrammarError(f"expected {value or kind}, got {token[1]!r}")
        return token

    # -- grammar -----------------------------------------------------------
    def parse(self) -> ExprNode:
        node = self.comparison()
        if self.peek() is not None:
            raise GrammarError(f"trailing tokens at {self.peek()!r}")
        return node

    def comparison(self) -> ExprNode:
        """Comparison/equality level.

        Existing expressions legitimately contain ``a > b`` (``trade_when(volume>ts_mean(volume,60),...)``),
        and the tolerant parser must hash those rather than refuse them. These map to the
        published comparison operators so the skeleton keeps the real topology; they are only
        ever produced by parsing, never chosen as a generation motif.
        """
        node = self.expression()
        token = self.peek()
        if token is None:
            return node
        if token[1] in {"<", ">", "<=", ">=", "==", "!=", "<>", "="}:
            self.next()
            operator = COMPARISON_OPERATORS[token[1]]
            return make_call(operator, [node, self.expression()], validate=False)
        return node

    def expression(self) -> ExprNode:
        node = self.term()
        while True:
            token = self.peek()
            if token is not None and token[1] in {"+", "-"}:
                self.next()
                keyword = "add" if token[1] == "+" else "subtract"
                node = make_call(keyword, [node, self.term()], validate=False)
            else:
                return node

    def term(self) -> ExprNode:
        node = self.factor()
        while True:
            token = self.peek()
            if token is not None and token[1] in {"*", "/"}:
                self.next()
                keyword = "multiply" if token[1] == "*" else "divide"
                node = make_call(keyword, [node, self.factor()], validate=False)
            else:
                return node

    def factor(self) -> ExprNode:
        token = self.peek()
        if token is None:
            raise GrammarError("unexpected end of expression")
        kind, value = token
        if value == "(":
            self.next()
            node = self.comparison()
            self.expect("op", ")")
            return node
        if value in {"-", "+"}:
            self.next()
            node = self.factor()
            if value == "-":
                return make_call("reverse", [node], validate=False)
            return node
        if kind == "number":
            self.next()
            return LiteralNode(float(value), "number")
        if kind == "string":
            self.next()
            return LiteralNode(value[1:-1], "string")
        self.next()
        lowered = value.lower()
        if lowered in {"true", "false"}:
            return LiteralNode(lowered == "true", "bool")
        if self.peek() is not None and self.peek()[1] == "(":
            self.next()
            args: list[ExprNode] = []
            keywords: list[tuple[str, ExprNode]] = []
            if self.peek() is not None and self.peek()[1] != ")":
                while True:
                    if (self.peek() is not None and self.peek()[0] == "ident"
                            and self.index + 1 < len(self.tokens)
                            and self.tokens[self.index + 1][1] == "="):
                        name = self.next()[1]
                        self.expect("op", "=")
                        keywords.append((name.lower(), self.comparison()))
                    else:
                        # Arguments may contain a comparison: trade_when(volume>adv20,...).
                        args.append(self.comparison())
                    if self.peek() is not None and self.peek()[1] == ",":
                        self.next()
                        continue
                    break
            self.expect("op", ")")
            return make_call(lowered, args, keywords=keywords, validate=False)
        return self._field(value)

    def _field(self, name: str) -> FieldNode:
        info = self.fields.get(name) or self.fields.get(name.lower())
        if info is None:
            return FieldNode(name, UNKNOWN, "unknown", "unknown")
        return field_node_from(info, fallback_id=name)


def parse_expression(expression: str, fields: Mapping[str, Any] | None = None) -> ExprNode:
    """Parse FASTEXPR into a tolerant AST.

    The parser never validates types (existing expressions may predate the catalog) and
    never raises for an unknown operator; it only raises :class:`GrammarError` on a
    genuinely malformed token stream.
    """
    return _Parser(_tokenize(str(expression or "")), fields).parse()


# ---------------------------------------------------------------------------
# Structural identities
# ---------------------------------------------------------------------------


def _attr(info: Any, *names: str, default: Any = None) -> Any:
    """Read an attribute from an object or a mapping, tolerating either naming convention."""
    if isinstance(info, Mapping):
        for name in names:
            if name in info:
                return info[name]
        return default
    for name in names:
        if hasattr(info, name):
            return getattr(info, name)
    return default


def _identifier(value: Any) -> str:
    """Normalize a dataset/category value that may be a bare id or an ``{id, name}`` mapping."""
    if value is None:
        return "unknown"
    if isinstance(value, Mapping):
        return str(value.get("id") or "unknown")
    return str(value)


def field_node_from(info: Any, *, fallback_id: str | None = None) -> FieldNode:
    """Build a :class:`FieldNode` from any field metadata object/mapping.

    Accepts both the generator ``Field`` naming (``name``/``field_type``) and the grammar
    naming (``field_id``/``value_type``) so the catalog does not need adapting.
    """
    field_id = str(_attr(info, "field_id", "name", "id", default=fallback_id or ""))
    value_type = str(_attr(info, "value_type", "field_type", "type", default=MATRIX)).upper()
    return FieldNode(
        field_id,
        value_type,
        _identifier(_attr(info, "dataset", default="unknown")),
        _identifier(_attr(info, "category", default="unknown")),
    )


def _masked_tokens(node: ExprNode, *, semantic: bool) -> str:
    if isinstance(node, FieldNode):
        if node.value_type == GROUP:
            return "<GROUP>"
        if semantic:
            return f"<{node.dataset}:{node.category}:{node.value_type}>"
        return f"<FIELD:{node.value_type}>"
    if isinstance(node, LiteralNode):
        return "#"
    if isinstance(node, CallNode):
        parts = [_masked_tokens(argument, semantic=semantic) for argument in node.args]
        parts.extend(f"{name}={_masked_tokens(value, semantic=semantic)}" for name, value in node.keywords)
        return f"{node.operator}({','.join(parts)})"
    raise TypeError(f"not an expression node: {node!r}")


def _fallback_skeleton(expression: str, fields: Mapping[str, Any] | None, *, semantic: bool) -> str:
    """Masked-token skeleton for an expression the tolerant parser cannot model.

    These helpers hash *everything already in the database*, including hand-typed CSV rows and
    legacy expressions, so one unmodelled shape must never break a campaign plan. The fallback
    keeps operator calls and token order verbatim, masks literals, and masks source names using
    the catalog when the name is known. It is coarser than the AST skeleton — it cannot see
    nesting — but it is still deterministic and comparable, and it only runs for a shape the
    parser already refused.
    """
    metadata = fields or {}

    def mask(match: "re.Match[str]") -> str:
        token = match.group(0)
        if str(expression or "")[match.end():match.end() + 1].lstrip().startswith("("):
            return token  # operator name: part of the topology, keep it
        info = metadata.get(token) or metadata.get(token.lower())
        if info is None:
            return "<FIELD>"
        node = field_node_from(info, fallback_id=token)
        if node.value_type == GROUP:
            return "<GROUP>"
        if semantic:
            return f"<{node.dataset}:{node.category}:{node.value_type}>"
        return f"<FIELD:{node.value_type}>"

    text = _IDENT_TOKEN_RE.sub(mask, str(expression or ""))
    return _NUMBER_TOKEN_RE.sub("#", text)


def _node_or_fallback(expression: str | ExprNode, fields: Mapping[str, Any] | None, *, semantic: bool) -> str:
    if isinstance(expression, (FieldNode, LiteralNode, CallNode)):
        return _masked_tokens(expression, semantic=semantic)
    try:
        return _masked_tokens(parse_expression(str(expression), fields), semantic=semantic)
    except GrammarError:
        return _fallback_skeleton(str(expression), fields, semantic=semantic)


def grammar_skeleton(expression: str | ExprNode, fields: Mapping[str, Any] | None = None) -> str:
    """Operator topology + field type, with exact fields and literals masked."""
    return _node_or_fallback(expression, fields, semantic=False)


def semantic_skeleton(expression: str | ExprNode, fields: Mapping[str, Any] | None = None) -> str:
    """Operator topology + ``<dataset:category:type>`` source identity, literals masked."""
    return _node_or_fallback(expression, fields, semantic=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def grammar_skeleton_hash(expression: str | ExprNode, fields: Mapping[str, Any] | None = None) -> str:
    return _sha256(grammar_skeleton(expression, fields))


def semantic_skeleton_hash(expression: str | ExprNode, fields: Mapping[str, Any] | None = None) -> str:
    return _sha256(semantic_skeleton(expression, fields))


def _operator_sequence(node: ExprNode) -> list[str]:
    if isinstance(node, CallNode):
        return [node.operator] + [op for child in node.args for op in _operator_sequence(child)]
    return []


def _levenshtein(left: Sequence[str], right: Sequence[str]) -> int:
    previous = list(range(len(right) + 1))
    for i, left_item in enumerate(left, start=1):
        current = [i]
        for j, right_item in enumerate(right, start=1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (left_item != right_item),
            ))
        previous = current
    return previous[-1]


def _jaccard_distance(left: Iterable[str], right: Iterable[str]) -> float:
    left_set, right_set = set(left), set(right)
    if not left_set and not right_set:
        return 0.0
    return 1.0 - len(left_set & right_set) / len(left_set | right_set)


#: Component weights, summing to 1.0 so the distance lands in ``[0, 1]``.
DISTANCE_WEIGHTS: dict[str, float] = {
    "operators": 0.40,
    "datasets": 0.25,
    "categories": 0.15,
    "fields": 0.10,
    "depth": 0.10,
}


def grammar_distance(
    left: str | ExprNode,
    right: str | ExprNode,
    *,
    fields: Mapping[str, Any] | None = None,
    motif_left: str | None = None,
    motif_right: str | None = None,
) -> float:
    """Weighted, deterministic structural distance in ``[0, 1]``.

    Components: operator-tree edit distance, dataset-set Jaccard, category-set Jaccard,
    source-field-count difference, depth difference, and (when both motif ids are known)
    a motif mismatch bonus. No tree-edit-distance dependency is required.
    """
    left_node = left if isinstance(left, (FieldNode, LiteralNode, CallNode)) else parse_expression(str(left), fields)
    right_node = right if isinstance(right, (FieldNode, LiteralNode, CallNode)) else parse_expression(str(right), fields)
    left_ops, right_ops = _operator_sequence(left_node), _operator_sequence(right_node)
    longest = max(len(left_ops), len(right_ops), 1)
    operator_distance = _levenshtein(left_ops, right_ops) / longest
    dataset_distance = _jaccard_distance(_datasets(left_node), _datasets(right_node))
    category_distance = _jaccard_distance(_categories(left_node), _categories(right_node))
    field_distance = min(1.0, abs(len(source_fields(left_node)) - len(source_fields(right_node))) / 2.0)
    depth_distance = min(1.0, abs(node_depth(left_node) - node_depth(right_node)) / 4.0)
    distance = (
        DISTANCE_WEIGHTS["operators"] * operator_distance
        + DISTANCE_WEIGHTS["datasets"] * dataset_distance
        + DISTANCE_WEIGHTS["categories"] * category_distance
        + DISTANCE_WEIGHTS["fields"] * field_distance
        + DISTANCE_WEIGHTS["depth"] * depth_distance
    )
    if motif_left is not None and motif_right is not None and motif_left != motif_right:
        distance = min(1.0, distance + 0.20)
    return round(min(1.0, max(0.0, distance)), 6)


def _datasets(node: ExprNode) -> tuple[str, ...]:
    return tuple(sorted({item.dataset for item in source_fields(node)}))


def _categories(node: ExprNode) -> tuple[str, ...]:
    return tuple(sorted({item.category for item in source_fields(node)}))


# ---------------------------------------------------------------------------
# Recipes (sampled in generation_policy, consumed by motif builders)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipe:
    """Independent, per-proposal sampling of the recipe dimensions."""

    lookback: int = 60
    smoothing_window: int = 10
    decay: int = 6
    neutralization: str = "SUBINDUSTRY"
    group_level: str = "subindustry"
    truncation: float = 0.1
    normalization: str = "rank"
    winsorization: bool = False
    rank_or_zscore: str = "rank"
    sign: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "lookback": self.lookback,
            "smoothing_window": self.smoothing_window,
            "decay": self.decay,
            "neutralization": self.neutralization,
            "group_level": self.group_level,
            "truncation": self.truncation,
            "normalization": self.normalization,
            "winsorization": self.winsorization,
            "rank_or_zscore": self.rank_or_zscore,
            "sign": self.sign,
        }


def group_node(name: str, fields: Mapping[str, Any] | None = None) -> FieldNode:
    """A GROUP literal node (``subindustry``, ``industry``, ...)."""
    info = (fields or {}).get(name) or (fields or {}).get(name.lower())
    if info is not None and str(_attr(info, "value_type", "field_type", default="")).upper() == GROUP:
        return field_node_from(info, fallback_id=name)
    return FieldNode(name, GROUP, "unknown", "group")


# ---------------------------------------------------------------------------
# Motif registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Motif:
    """An economically interpretable grammar recipe.

    ``builder`` receives the resolved source fields (already vector-aggregated), the
    sampled :class:`Recipe`, and the resolved group node (``None`` when unused).
    """

    id: str
    description: str
    input_roles: tuple[str, ...]
    allowed_field_types: tuple[str, ...]
    builder: Callable[[Sequence[FieldNode], Recipe, FieldNode | None], ExprNode]
    tags: tuple[str, ...] = ()
    needs_group: bool = False
    #: When set, only fields whose dataset is in this set are eligible (event motifs).
    allowed_datasets: tuple[str, ...] = ()


def _apply_sign(node: ExprNode, recipe: Recipe) -> ExprNode:
    if recipe.sign >= 0:
        return node
    return make_call("reverse", [node])


#: Datasets whose metadata supports the event/expectation motifs.
EVENT_DATASETS = ("analyst4", "model16", "model51", "model51_1", "news12", "news18",
                  "option8", "option9", "socialmedia8", "socialmedia12", "sentiment")

_SOURCE_TYPES = (MATRIX, VECTOR)


def _build_cross_sectional_level(fields, recipe, group):
    return _apply_sign(make_call(recipe.rank_or_zscore, [vector_value(fields[0])]), recipe)


def _build_time_series_level(fields, recipe, group):
    return make_call("ts_mean", [vector_value(fields[0]), literal(recipe.lookback)])


def _build_momentum(fields, recipe, group):
    return make_call("rank", [make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])])


def _build_mean_reversion(fields, recipe, group):
    revert = make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])
    return make_call("reverse", [make_call("zscore", [revert])])


def _build_change(fields, recipe, group):
    return make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])


def _build_acceleration(fields, recipe, group):
    delta = make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])
    return make_call("ts_delta", [delta, literal(max(2, recipe.lookback // 2))])


def _build_smoothed_change(fields, recipe, group):
    delta = make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])
    return make_call("ts_mean", [delta, literal(recipe.smoothing_window)])


def _build_volatility_adjusted(fields, recipe, group):
    delta = make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])
    volatility = make_call("ts_std_dev", [vector_value(fields[0]), literal(max(5, recipe.lookback * 2))])
    return make_call("divide", [delta, volatility])


def _build_group_relative(fields, recipe, group):
    return make_call("group_rank", [vector_value(fields[0]), group or group_node(recipe.group_level)])


def _build_group_neutralized(fields, recipe, group):
    return make_call("group_neutralize", [make_call("zscore", [vector_value(fields[0])]),
                                          group or group_node(recipe.group_level)])


def _build_ranked_level(fields, recipe, group):
    node = vector_value(fields[0])
    if recipe.winsorization:
        node = make_call("winsorize", [node], keywords={"std": literal(4)})
    return make_call("ts_rank", [node, literal(recipe.lookback)])


def _build_spread(fields, recipe, group):
    return make_call("subtract", [make_call("zscore", [vector_value(fields[0])]),
                                  make_call("zscore", [vector_value(fields[1])])])


def _build_ratio(fields, recipe, group):
    return make_call("divide", [vector_value(fields[0]), vector_value(fields[1])])


def _build_difference_of_ranks(fields, recipe, group):
    return make_call("subtract", [make_call("rank", [vector_value(fields[0])]),
                                  make_call("rank", [vector_value(fields[1])])])


def _build_normalized_difference(fields, recipe, group):
    # The subtraction already carries direction, so this motif does not consume the recipe
    # ``sign`` dimension (an extra wrapper would exceed the depth budget for no gain).
    left, right = vector_value(fields[0]), vector_value(fields[1])
    numerator = make_call("subtract", [left, right])
    denominator = make_call("add", [make_call("abs", [left]), make_call("abs", [right])])
    return make_call("divide", [numerator, denominator])


def _build_confirming_signals(fields, recipe, group):
    return make_call("add", [
        make_call("ts_rank", [vector_value(fields[0]), literal(recipe.lookback)]),
        make_call("ts_rank", [vector_value(fields[1]), literal(recipe.lookback)]),
    ])


def _build_contrarian_pair(fields, recipe, group):
    return make_call("subtract", [
        make_call("ts_rank", [vector_value(fields[0]), literal(recipe.lookback)]),
        make_call("ts_rank", [vector_value(fields[1]), literal(recipe.lookback)]),
    ])


def _build_cross_dataset_composite(fields, recipe, group):
    group_ref = group or group_node(recipe.group_level)
    return make_call("add", [
        make_call("group_zscore", [vector_value(fields[0]), group_ref]),
        make_call("group_zscore", [vector_value(fields[1]), group_ref]),
    ])


def _build_actual_vs_expectation(fields, recipe, group):
    actual, expected = vector_value(fields[0]), vector_value(fields[1])
    return make_call("divide", [make_call("subtract", [actual, expected]),
                                make_call("abs", [expected])])


def _build_estimate_revision(fields, recipe, group):
    return make_call("ts_delta", [vector_value(fields[0]), literal(recipe.lookback)])


def _build_event_decay(fields, recipe, group):
    return make_call("ts_decay_linear", [vector_value(fields[0]), literal(recipe.lookback)])


def _build_surprise_normalization(fields, recipe, group):
    surprise = make_call("subtract", [vector_value(fields[0]), vector_value(fields[1])])
    return make_call("divide", [surprise, make_call("ts_std_dev", [surprise, literal(recipe.lookback)])])


MOTIFS: tuple[Motif, ...] = (
    Motif("cross_sectional_level", "Cross-sectional level of one source", ("value",), _SOURCE_TYPES,
          _build_cross_sectional_level, ("single", "level")),
    Motif("time_series_level", "Smoothed level of one source", ("value",), _SOURCE_TYPES,
          _build_time_series_level, ("single", "level")),
    Motif("momentum", "Change over a lookback, ranked cross-sectionally", ("value",), _SOURCE_TYPES,
          _build_momentum, ("single", "trend")),
    Motif("mean_reversion", "Contrarian reversal of a change", ("value",), _SOURCE_TYPES,
          _build_mean_reversion, ("single", "reversion")),
    Motif("change", "Raw first difference over a lookback", ("value",), _SOURCE_TYPES,
          _build_change, ("single", "difference")),
    Motif("acceleration", "Second difference: change in the change", ("value",), _SOURCE_TYPES,
          _build_acceleration, ("single", "difference")),
    Motif("smoothed_change", "Average change over a smoothing window", ("value",), _SOURCE_TYPES,
          _build_smoothed_change, ("single", "difference")),
    Motif("volatility_adjusted", "Change scaled by its own volatility", ("value",), _SOURCE_TYPES,
          _build_volatility_adjusted, ("single", "risk")),
    Motif("group_relative", "Source ranked inside a group", ("value",), _SOURCE_TYPES,
          _build_group_relative, ("single", "group"), needs_group=True),
    Motif("group_neutralized", "Source neutralized inside a group", ("value",), _SOURCE_TYPES,
          _build_group_neutralized, ("single", "group"), needs_group=True),
    Motif("ranked_level", "Time-series rank of a (optionally winsorized) level", ("value",), _SOURCE_TYPES,
          _build_ranked_level, ("single", "level")),
    Motif("spread", "Standardized difference between two sources", ("left", "right"), _SOURCE_TYPES,
          _build_spread, ("two_source", "difference")),
    Motif("ratio", "Ratio of two economic sources", ("left", "right"), _SOURCE_TYPES,
          _build_ratio, ("two_source", "ratio")),
    Motif("difference_of_ranks", "Difference between two cross-sectional ranks", ("left", "right"), _SOURCE_TYPES,
          _build_difference_of_ranks, ("two_source", "difference")),
    Motif("normalized_difference", "Signed scale-free difference of two sources", ("left", "right"), _SOURCE_TYPES,
          _build_normalized_difference, ("two_source", "difference")),
    Motif("confirming_signals", "Agreement of two time-series ranks", ("left", "right"), _SOURCE_TYPES,
          _build_confirming_signals, ("two_source", "agreement")),
    Motif("contrarian_pair", "Disagreement between two time-series ranks", ("left", "right"), _SOURCE_TYPES,
          _build_contrarian_pair, ("two_source", "reversion")),
    Motif("cross_dataset_composite", "Two group-neutralized sources from different datasets", ("left", "right"), _SOURCE_TYPES,
          _build_cross_dataset_composite, ("two_source", "composite"), needs_group=True),
    Motif("actual_vs_expectation", "Reported value relative to an expectation", ("actual", "expected"), _SOURCE_TYPES,
          _build_actual_vs_expectation, ("event",), allowed_datasets=EVENT_DATASETS),
    Motif("estimate_revision", "Revision of an estimate/expectation", ("value",), _SOURCE_TYPES,
          _build_estimate_revision, ("event",), allowed_datasets=EVENT_DATASETS),
    Motif("event_decay", "Event effect decayed linearly", ("value",), _SOURCE_TYPES,
          _build_event_decay, ("event",), allowed_datasets=EVENT_DATASETS),
    Motif("surprise_normalization", "Expectation surprise scaled by its own volatility", ("actual", "expected"), _SOURCE_TYPES,
          _build_surprise_normalization, ("event",), allowed_datasets=EVENT_DATASETS),
)

MOTIF_BY_ID: dict[str, Motif] = {motif.id: motif for motif in MOTIFS}

#: Motifs that need two distinct economic sources.
TWO_SOURCE_MOTIFS = tuple(motif.id for motif in MOTIFS if len(motif.input_roles) == 2)
SINGLE_SOURCE_MOTIFS = tuple(motif.id for motif in MOTIFS if len(motif.input_roles) == 1)


def motif_by_id(motif_id: str) -> Motif:
    try:
        return MOTIF_BY_ID[motif_id]
    except KeyError as exc:  # pragma: no cover - defensive
        raise GrammarError(f"unknown motif {motif_id!r}") from exc


def motif_eligible(motif: Motif, fields: Sequence[FieldNode], *, distinct_datasets: bool = False) -> bool:
    """Metadata/type-driven eligibility; unsupported semantic combinations are refused."""
    if len(fields) < len(motif.input_roles):
        return False
    used = fields[: len(motif.input_roles)]
    if any(field.value_type not in motif.allowed_field_types for field in used):
        return False
    if motif.allowed_datasets and not all(field.dataset in motif.allowed_datasets for field in used):
        return False
    if distinct_datasets and len({field.dataset for field in used}) < 2:
        return False
    return True


def eligible_motifs(
    fields: Sequence[FieldNode],
    *,
    include_cross_dataset: bool = True,
) -> tuple[Motif, ...]:
    """Every motif the supplied sources can legally realize."""
    if not fields:
        return ()
    result: list[Motif] = []
    for motif in MOTIFS:
        distinct = motif.id == "cross_dataset_composite"
        if distinct and not include_cross_dataset:
            continue
        if motif_eligible(motif, fields, distinct_datasets=distinct):
            result.append(motif)
    return tuple(result)


def build_motif(
    motif_id: str,
    fields: Sequence[FieldNode],
    recipe: Recipe,
    *,
    group: FieldNode | None = None,
    limits: ComplexityLimits = DEFAULT_LIMITS,
) -> CallNode:
    """Realize one motif into a validated, complexity-bounded AST."""
    motif = motif_by_id(motif_id)
    resolved = tuple(fields[: len(motif.input_roles)])
    if not motif_eligible(motif, resolved, distinct_datasets=motif.id == "cross_dataset_composite"):
        raise GrammarError(f"motif {motif_id!r} is not eligible for the supplied fields")
    group_ref = group
    if motif.needs_group and group_ref is None:
        group_ref = group_node(recipe.group_level)
    node = motif.builder(resolved, recipe, group_ref)
    if not isinstance(node, CallNode):
        raise GrammarError(f"motif {motif_id!r} did not build a call node")
    # Re-validate every nested call and enforce the complexity budget before returning.
    for item in _walk(node):
        if isinstance(item, CallNode):
            validate_call(item.operator, item.args)
    violations = check_complexity(node, limits)
    if violations:
        raise GrammarError("complexity budget exceeded: " + "; ".join(violations))
    return node


def compatible_recipe(node: ExprNode, recipe: Recipe) -> Recipe:
    """Return the recipe trimmed to what the expression can express (diagnostics only)."""
    return replace(recipe)
