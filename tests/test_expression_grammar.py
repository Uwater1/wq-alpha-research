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


def test_field_roles_are_inferred_deterministically():
    assert grammar.infer_field_roles("est_ptp") == (grammar.ROLE_ESTIMATE,)
    assert grammar.infer_field_roles("actual_earnings_per_share_2") == (grammar.ROLE_ACTUAL,)
    assert grammar.infer_field_roles("eps_revision_1m") == (grammar.ROLE_REVISION,)
    assert grammar.infer_field_roles("assets") == (grammar.ROLE_GENERIC,)
    assert grammar.infer_field_roles("close") == (grammar.ROLE_GENERIC,)
    # An explicit override beats inference.
    grammar.FIELD_ROLE_OVERRIDES["custom_x"] = (grammar.ROLE_SURPRISE,)
    try:
        assert grammar.infer_field_roles("custom_x") == (grammar.ROLE_SURPRISE,)
    finally:
        grammar.FIELD_ROLE_OVERRIDES.pop("custom_x", None)


def test_event_expectation_motifs_are_role_aware_not_dataset_aware():
    """P2: 'actual' vs 'expectation' is a role relation, not two dataset memberships."""
    actual = grammar.FieldNode("actual_earnings_per_share_2", MATRIX, "news12", "news")
    estimate = grammar.FieldNode("est_eps", MATRIX, "analyst4", "analyst")
    other_estimate = grammar.FieldNode("est_ptp", MATRIX, "analyst4", "analyst")
    generic = grammar.FieldNode("close", MATRIX, "pv1", "pv")
    motif = grammar.motif_by_id("actual_vs_expectation")

    # Positive: an actual paired with an estimate is the promised semantic pair.
    assert grammar.motif_eligible(motif, [actual, estimate])
    node = grammar.build_motif("actual_vs_expectation", [actual, estimate], grammar.Recipe())
    assert grammar.render(node).startswith("divide(subtract(")

    # Negative: two event-dataset estimates are not an actual-vs-expectation pair.
    assert not grammar.motif_eligible(motif, [estimate, other_estimate])
    with pytest.raises(grammar.GrammarError):
        grammar.build_motif("actual_vs_expectation", [estimate, other_estimate], grammar.Recipe())

    # Negative: an actual paired with a role-less event-dataset field is refused — the
    # dataset allow-list alone is not proof that the pair is actual-vs-expectation.
    roleless = grammar.FieldNode("volume_rank_60", MATRIX, "analyst4", "analyst")
    assert not grammar.motif_eligible(grammar.motif_by_id("surprise_normalization"), [actual, roleless])

    # Positive/negative for the single-source event motifs.
    assert grammar.motif_eligible(grammar.motif_by_id("estimate_revision"), [estimate])
    assert not grammar.motif_eligible(grammar.motif_by_id("estimate_revision"), [actual])
    assert grammar.motif_eligible(grammar.motif_by_id("event_decay"), [estimate])
    assert not grammar.motif_eligible(grammar.motif_by_id("event_decay"), [generic])


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


# ---------------------------------------------------------------------------
# Real-world tolerance: comparisons inside call arguments (live database shapes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("expression", [
    "trade_when(volume>ts_mean(volume,60),rank(ts_rank(est_ptp/close,126)),-1)",
    "trade_when(ts_std_dev(returns,20)>ts_mean(ts_std_dev(returns,20),120),rank(close),-1)",
    "trade_when(returns<0,rank(close),-1)",
    "trade_when(volume<=adv20,rank(close),-1)",
    "trade_when(volume!=0,rank(close),-1)",
])
def test_comparison_arguments_parse_and_hash(expression):
    """`trade_when(a>b, ...)` is a real BRAIN shape; skeleton hashing must not refuse it."""
    node = grammar.parse_expression(expression)
    skeleton = grammar.grammar_skeleton(node)
    semantic = grammar.semantic_skeleton(node)
    assert skeleton.startswith("trade_when(")
    assert ">" in skeleton or "<" in skeleton or "greater" in skeleton or "less" in skeleton
    assert semantic  # never empty, never raises
    assert grammar.render(node)  # renderable back to FASTEXPR
    assert grammar.grammar_skeleton_hash(expression) == grammar.grammar_skeleton_hash(node)


def test_comparison_topology_is_preserved_but_field_identity_is_not():
    greater = grammar.grammar_skeleton_hash(
        "trade_when(ts_rank(close,60)>ts_mean(close,120),rank(close),-1)"
    )
    same_shape_other_field = grammar.grammar_skeleton_hash(
        "trade_when(ts_rank(open,60)>ts_mean(open,120),rank(open),-1)"
    )
    less_not_greater = grammar.grammar_skeleton_hash(
        "trade_when(ts_rank(close,60)<ts_mean(close,120),rank(close),-1)"
    )
    assert greater == same_shape_other_field  # fields masked
    assert greater != less_not_greater  # direction is part of the topology


def test_malformed_expression_falls_back_instead_of_raising():
    """A single unmodelled row in the database must not break a campaign plan."""
    broken = "trade_when(volume>adv20,"
    skeleton = grammar.grammar_skeleton(broken)
    assert skeleton
    assert grammar.grammar_skeleton_hash(broken)  # deterministic, no exception
    assert grammar.semantic_skeleton(broken)
    assert grammar.grammar_skeleton_hash(broken) == grammar.grammar_skeleton_hash(broken)


def test_fallback_skeleton_keeps_operator_names_and_masks_sources():
    broken = "trade_when(volume>adv20,"
    skeleton = grammar.grammar_skeleton(broken)
    assert "trade_when" in skeleton
    assert "volume" not in skeleton and "adv20" not in skeleton
