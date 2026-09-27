"""Unit tests for the typed expression grammar (Generator V3 P1/P2).

No BRAIN calls, no database, no credentials: the grammar is a pure function of the local
operator snapshot and the supplied field metadata.
"""
from __future__ import annotations

import pytest

import diversity
import expression_grammar as grammar

MATRIX = grammar.MATRIX
VECTOR = grammar.VECTOR
GROUP = grammar.GROUP


def _field(field_id, value_type=MATRIX, dataset="ds_a", category="cat_a"):
    return grammar.FieldNode(field_id, value_type, dataset, category)


def test_make_call_rejects_known_type_mismatch():
    with pytest.raises(grammar.GrammarError):
        grammar.make_call("vec_avg", [_field("close", MATRIX)])  # vec_avg needs a VECTOR
    with pytest.raises(grammar.GrammarError):
        grammar.make_call("group_rank", [_field("close", MATRIX), _field("open", MATRIX)])  # group arg must be GROUP


def test_make_call_enforces_arity_and_keywords():
    with pytest.raises(grammar.GrammarError):
        grammar.make_call("ts_mean", [_field("close")])  # ts_mean(x, d) needs two args
    with pytest.raises(grammar.GrammarError):
        grammar.make_call("winsorize", [_field("close")], keywords={"nope": grammar.literal(1)})


def test_valid_call_renders_deterministically():
    node = grammar.make_call("ts_mean", [_field("close"), grammar.literal(60)])
    assert grammar.render(node) == "ts_mean(close,60)"
    assert grammar.render(node) == grammar.render(node)


def test_complexity_budget_is_enforced():
    shallow = grammar.ComplexityLimits(max_depth=1, max_nodes=2, max_fields=1, max_binary_ops=0)
    node = grammar.make_call("ts_mean", [_field("close"), grammar.literal(60)])
    assert grammar.check_complexity(node, shallow)  # depth 2 > 1
    assert grammar.check_complexity(node, grammar.DEFAULT_LIMITS) == []
    with pytest.raises(grammar.GrammarError):
        grammar.build_motif("group_relative", [_field("close")], grammar.Recipe(), limits=shallow)


def test_motif_eligibility_is_metadata_driven():
    single = grammar.eligible_motifs([_field("close")])
    assert "cross_sectional_level" in {motif.id for motif in single}
    assert "spread" not in {motif.id for motif in single}  # two-source motif needs a partner

    same_dataset = grammar.eligible_motifs([_field("a"), _field("b", dataset="ds_a")])
    assert "cross_dataset_composite" not in {motif.id for motif in same_dataset}

    distinct = grammar.eligible_motifs([_field("a"), _field("b", dataset="ds_b")])
    assert "cross_dataset_composite" in {motif.id for motif in distinct}


def test_event_motifs_require_supporting_metadata():
    with pytest.raises(grammar.GrammarError):
        grammar.build_motif("event_decay", [_field("close", dataset="pv1")], grammar.Recipe())
    node = grammar.build_motif("event_decay", [_field("est_eps", dataset="analyst4")], grammar.Recipe())
    assert grammar.render(node).startswith("ts_decay_linear(")


def test_same_topology_different_fields_shares_grammar_hash():
    fields = diversity.load_field_metadata()
    left = grammar.build_motif("change", [grammar.field_node_from(fields["close"])], grammar.Recipe())
    right = grammar.build_motif("change", [grammar.field_node_from(fields["return_assets"])], grammar.Recipe())
    assert grammar.grammar_skeleton_hash(left, fields) == grammar.grammar_skeleton_hash(right, fields)


def test_different_datasets_change_semantic_hash_not_grammar_hash():
    fields = diversity.load_field_metadata()
    left = grammar.build_motif("ranked_level", [grammar.field_node_from(fields["close"])], grammar.Recipe())
    other = next(
        item for item in fields.values()
        if item.value_type == MATRIX and item.dataset not in ("pv1", "unknown")
    )
    right = grammar.build_motif("ranked_level", [grammar.field_node_from(other)], grammar.Recipe())
    assert grammar.grammar_skeleton_hash(left, fields) == grammar.grammar_skeleton_hash(right, fields)
    assert grammar.semantic_skeleton_hash(left, fields) != grammar.semantic_skeleton_hash(right, fields)


def test_numeric_only_variants_share_grammar_and_semantic_hashes():
    fields = diversity.load_field_metadata()
    node = grammar.field_node_from(fields["close"])
    fast = grammar.build_motif("change", [node], grammar.Recipe(lookback=20))
    slow = grammar.build_motif("change", [node], grammar.Recipe(lookback=252))
    assert grammar.grammar_skeleton("change", fields) is not None  # smoke: parse path exists
    assert grammar.grammar_skeleton_hash(fast, fields) == grammar.grammar_skeleton_hash(slow, fields)
    assert grammar.semantic_skeleton_hash(fast, fields) == grammar.semantic_skeleton_hash(slow, fields)


def test_existing_skeleton_hash_behavior_is_unchanged():
    # The grammar adds identities; it must not alter canonical.skeleton_hash semantics.
    import canonical

    left = canonical.skeleton_hash("ts_mean(close, 20)")
    right = canonical.skeleton_hash("ts_mean(close, 60)")
    other = canonical.skeleton_hash("ts_mean(open, 20)")
    assert left == right
    assert left != other


def test_grammar_distance_is_bounded_and_motif_sensitive():
    fields = diversity.load_field_metadata()
    node = grammar.field_node_from(fields["close"])
    other = grammar.field_node_from(fields["return_assets"])
    base = grammar.build_motif("change", [node], grammar.Recipe(), group=grammar.group_node("subindustry"))
    assert grammar.grammar_distance(base, base, fields=fields) == 0.0
    same_topo = grammar.build_motif("change", [other], grammar.Recipe(), group=grammar.group_node("subindustry"))
    assert 0.0 < grammar.grammar_distance(base, same_topo, fields=fields) <= 1.0
    other_motif = grammar.build_motif("smoothed_change", [node], grammar.Recipe())
    assert 0.0 < grammar.grammar_distance(base, other_motif, fields=fields) <= 1.0
    # A different topology on the same source and a different source on the same topology
    # are both non-zero and the score stays bounded.
    assert grammar.grammar_distance(other_motif, other, fields=fields) <= 1.0


def test_parser_round_trips_a_nested_expression():
    fields = diversity.load_field_metadata()
    text = "group_rank(ts_rank(free_cash_flow_reported_value,60),industry)"
    node = grammar.parse_expression(text, fields)
    assert grammar.render(node) == text.replace("industry", "industry")
    assert grammar.node_depth(node) == 3


def test_parser_masks_literals_in_skeletons():
    fields = diversity.load_field_metadata()
    a = grammar.grammar_skeleton("ts_mean(close, 20)", fields)
    b = grammar.grammar_skeleton("ts_mean(return_assets, 252)", fields)
    assert a == b == "ts_mean(<FIELD:MATRIX>,#)"
