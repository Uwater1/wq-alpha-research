# Generator V3 TODO

**Goal:** diversity-first symbolic alpha generation while preserving validation, canonical deduplication, lineage, the permanent trial ledger, staged search, scheduler safety, and point-in-time replay.

**Status:** V3 is substantially implemented but **P0–P15 are not yet spec-complete** after the latest audit. Completed infrastructure is compressed below; remaining gaps are intentionally left open with the required fix beside each item.

**Live default:** unchanged. V2 remains the default unless `--strategy`, `--motif`, or `--dry-plan` explicitly selects V3.

---

# P0 — Diversity identities and source metadata

## P0.1 Child source profile

- [x] `derive_source_profile()` returns field IDs, datasets, categories, types, primary family, and cross-dataset flag.
- [x] Single-dataset children use that dataset as `primary_family`.
- [x] Multi-dataset children use a stable composite family such as `multi:analyst4+fundamental2`.
- [x] Normal mutation children recompute family/source metadata from the child.
- [ ] **Fix crossover family persistence.** `_crossover_proposal()` currently derives a child profile but can keep the first parent's `signal_family`.  
  **Do:** derive the profile once, set `Proposal.family = profile["primary_family"]`, and add a regression asserting queued candidate + trial family match the child profile.

## P0.2 Structural identities

- [x] Keep `canonical_key`.
- [x] Keep existing `skeleton_hash` behavior.
- [x] Add `grammar_skeleton` / `grammar_skeleton_hash`.
- [x] Add `semantic_skeleton` / `semantic_skeleton_hash`.
- [x] Same topology + different fields collapse under grammar hash.
- [x] Dataset/category source changes remain visible under semantic hash.
- [x] Numeric-only variants collapse under grammar/semantic hashes.

## P0.3 Archive niche identity

- [x] Archive niches use topology-preserving grammar/semantic hashes.
- [x] Niche metadata includes family, dataset/category sets, motif, depth, field count, cross-dataset flag, turnover bucket, mutation type, and generation mode.
- [x] Operator sets are descriptive metadata only.

---

# P1 — Typed expression grammar

- [x] `FieldNode`, `LiteralNode`, `CallNode`, `ExprNode`.
- [x] Reuse `scripts/compatibility.py`; no second type system.
- [x] Validate known arity/type constraints before generated calls are accepted.
- [x] Deterministic FASTEXPR rendering.
- [x] Complexity budgets for depth, nodes, fields, and binary operators.
- [x] Grammar tests require no BRAIN calls.
- [x] Tolerant parsing covers real stored shapes such as `trade_when(a>b,...)` with fallback hashing for unmodelled legacy syntax.

---

# P2 — Motif registry

- [x] `Motif` registry replaces a large flat template catalogue as the V3 search primitive.
- [x] Single-source motifs: level, momentum/reversion/change/acceleration, smoothing, volatility adjustment, group transforms, ranked level.
- [x] Two-source motifs: spread, ratio, rank difference, normalized difference, confirmation/contrarian, cross-dataset composite.
- [x] Event/expectation motifs exist and are gated by type/dataset metadata.
- [ ] **Make event/expectation eligibility role-aware.** Dataset allow-lists alone do not prove that one field is an “actual” and another an “expectation”.  
  **Do:** attach/infer semantic roles or tags (actual, estimate, revision, event/surprise, etc.) and require compatible roles in `motif_eligible()`; add positive and negative tests.

---

# P3 — Independent deterministic recipes

- [x] Per-proposal RNG seed includes campaign, global seed, field IDs, motif, recipe index, and parents.
- [x] Unrelated proposals do not perturb an existing proposal's recipe.
- [x] Independently sample lookback, smoothing, decay, neutralization, group, truncation, normalization, winsorization, rank/zscore, and sign.
- [x] Persist recipe index + full recipe metadata.
- [x] One field can produce multiple reproducible recipes.

---

# P4 — Generation strategy layer

## P4.1 Modes

- [x] `explore`
- [x] `exploit`
- [x] `mutate`
- [x] `crossover`
- [x] `mixed`
- [x] Every realized V3 proposal records its generation mode; unavailable lineage modes fall back honestly.

## P4.2 Explore

- [x] Prefer unseen motifs and under-tested fields/families.
- [x] Post-materialization novelty screening sees grammar/semantic history.
- [ ] **Plan grammar/semantic novelty before materialization.** Current planning does not explicitly budget unseen grammar skeletons, semantic structures, or sparse archive cells.  
  **Do:** feed grammar/semantic counts + archive occupancy into slot selection and reserve explore slots for unseen/sparse structures.
- [ ] **Make cross-dataset motifs reachable in ordinary planning.** The current partner pool is generally family-local, so `cross_dataset_composite` can be impossible while tests still pass conditionally.  
  **Do:** for motifs requiring distinct datasets, select the partner from another compatible dataset/global source pool; add a deterministic test that at least one cross-dataset proposal is produced.

## P4.3 Exploit

- [x] Prefer proven motifs within observed family evidence.
- [ ] **Support proven-structure transfer to new datasets/sources.**  
  **Do:** allow proven motif/semantic evidence to seed compatible new datasets rather than keying all exploit evidence to the current family; explicitly test “proven structure → new source”.

## P4.4 Mutate

Existing concrete edits include field swap, template change, window change, decay change, neutralization change, group transform, component removal, and signal combination.

- [x] Keep failure-directed repair.
- [ ] Implement real V3 `dataset_swap`.
- [ ] Implement real V3 `motif_change`.
- [ ] Implement real V3 `normalization_change`.
- [ ] Implement real V3 `group_change`.
- [ ] Implement typed `subtree_replace`.
- [ ] Normalize `add_component` / structural-combine naming as a stable concrete operation.  
  **Do for all mutation items:** perform AST/type/complexity validation, persist a stable `operation`, and add one regression per operation showing it can actually be generated.

---

# P5 — Archive → generator loop

- [x] `plan_campaign()` produces explicit slots with mode, family, motif, parents, recipe index, and reason.
- [x] Consume `archive.allocate_families()`.
- [x] Enforce bounded family share.
- [x] Reserve under-tested-family exploration.
- [x] Consume `archive.parents()`.
- [x] Parent selection rotates across archive families/niches.
- [x] Planned budget equals requested budget.
- [x] Same DB snapshot + seed reproduces the same plan.
- [x] Archive state affects actual generation.

---

# P6 — Crossover

## P6.1 Structural distance

- [x] `grammar_distance(A, B) -> [0,1]`.
- [x] Operator-tree difference.
- [x] Dataset-set Jaccard distance.
- [x] Category-set Jaccard distance.
- [x] Field-count difference.
- [x] Depth difference.
- [x] Optional motif mismatch component.

## P6.2 Parent selection

- [x] Reject same-grammar/near-identical pairs when better alternatives exist.
- [ ] **Use distance as a real selection objective.** Current logic mainly filters bad pairs then randomly chooses among survivors.  
  **Do:** score eligible pairs with `grammar_distance()` (including motif IDs when known) and choose or weight toward structurally distant pairs deterministically.

## P6.3 Crossover forms and complexity

- [x] `add(rank(A), rank(B))`.
- [x] `subtract(rank(A), rank(B))`.
- [x] `add(zscore(A), zscore(B))`.
- [x] `multiply(rank(A), rank(B))`.
- [x] Complexity budget scales to evolved parents under a hard ceiling.
- [x] Persist both parent IDs.
- [ ] Child family must be derived from final child sources.  
  **Do:** same fix as P0.1; test a genuinely cross-family parent pair.

---

# P7 — Novelty-aware generation

Implemented novelty facts:

- [x] exact-candidate novelty
- [x] current `skeleton_hash` novelty
- [x] grammar-skeleton novelty
- [x] semantic-skeleton novelty
- [x] dataset novelty
- [x] motif novelty

Outcomes:

- [x] `KEEP`
- [x] `DOWNWEIGHT`
- [x] `SKIP_REDUNDANT`
- [x] Skips consume no simulation capacity and remain in `research_trials`.

Remaining dimensions:

- [ ] **Category novelty.**  
  **Do:** track category history separately from dataset history and expose the component in `NoveltyReport`.
- [ ] **Archive niche sparsity.**  
  **Do:** score actual archive-cell occupancy/member count rather than proxying with grammar frequency.
- [ ] **Numeric parent→child grammar distance.**  
  **Do:** pass parent grammar/source metadata into `screen_novelty()` and score `grammar_distance(parent, child)`; equality-only penalty is insufficient.
- [ ] **Full skip provenance.**  
  **Do:** complete P10's skip-lineage work so skipped mutations/crossovers retain their parents and operation.

---

# P8 — Ranking integration

- [x] Expected quality, exact novelty, grammar novelty, semantic novelty, information gain, family diversity, failure risk, duplicate penalty.
- [x] Correlated novelty contribution is bounded/capped.
- [x] Ranking remains advisory.
- [ ] **Make `archive_sparsity` a real archive signal.** It currently mirrors grammar novelty.  
  **Do:** derive it from archive niche occupancy/member counts and add a test where grammar frequency is equal but archive sparsity differs.
- [ ] **Clarify `portfolio_diversification` stage.** It is computed for candidate scoring but materially applied in submission ranking.  
  **Do:** either include it in simulation priority as documented, or explicitly document/rename it as submission-only and test that contract.

---

# P9 — Adaptive motif / mutation allocation

## P9.1 Motifs

- [x] Persist motif outcome counters.
- [x] Bounded Thompson-style motif allocation.
- [x] Reserve exploration for under-tested motifs.
- [x] Successful motifs can earn more budget.
- [x] No motif can monopolize the campaign.

## P9.2 Mutation/source transitions

- [ ] **Record correct source→target families.** Current aggregation can write the same family for both sides.  
  **Do:** source family = parent/source lineage; target family = final child-derived profile. Add a cross-family mutation test.
- [ ] **Record concrete mutation operation, not only broad repair class.**  
  **Do:** aggregate `mutation_parameters["operation"]` (or equivalent normalized field) separately from `mutation_type`.
- [ ] **Implement adaptive mutation allocation.**  
  **Do:** allocate bounded exploration/exploitation budget across concrete mutation operations from corrected historical outcomes, analogous to motif allocation; add tests showing a successful operation gains budget while an untested operation retains exploration.

---

# P10 — Database / provenance

- [x] Candidate fields expose strategy, generation mode, motif, recipe ID/index, grammar hash, semantic hash, source profile, policy/grammar versions.
- [x] `research_trials` records campaign/candidate IDs, V3 mode/version/motif/recipe, hashes, source profile, decision, and reason.
- [x] Queued crossover lineage persists both parents.
- [x] Historical rows are migrated in place; no destructive rewrite.
- [ ] **Preserve complete lineage for novelty-skipped proposals.**  
  **Do:** extend `record_generation_decision()` + caller to persist `parent_ids`, generation, mutation type, and mutation parameters. Add skipped-mutation and skipped-crossover tests.

---

# P11 — Versioning

- [x] V3 identity: `catalog-generator-v3`.
- [x] V2 path remains honestly labelled `catalog-generator-v2`.
- [x] `GRAMMAR_VERSION`.
- [x] `MOTIF_REGISTRY_VERSION`.
- [x] `GENERATION_POLICY_VERSION`.

---

# P12 — CLI

- [x] V2-compatible `python -m wq generate`.
- [x] `python -m wq mutate`.
- [x] V3 `--strategy`.
- [x] V3 `--motif`.
- [x] Explicit `crossover` command.
- [x] `--dry-plan` performs no BRAIN calls and no queue writes.
- [x] Dry plan reports mode/family/motif/dataset/grammar/semantic distribution.

---

# P13 — Diversity report

- [x] trial count
- [x] unique exact candidates
- [x] unique fields/datasets/categories
- [x] current/grammar/semantic skeleton counts
- [x] motif count
- [x] entropy-based effective motif/dataset/grammar/semantic/family counts
- [x] cross-dataset share
- [x] duplicate / near-duplicate rates
- [x] median parent→child grammar distance
- [x] median pairwise PnL correlation when cached data permits
- [x] IS_PASS / CORR_PASS / ACTIVE breakdowns by motif

---

# P14 — Point-in-time policy replay

- [x] `grammar_novelty`.
- [x] `semantic_novelty`.
- [x] `archive_v3`.
- [x] `mixed_v3`.
- [x] Decision cards exclude outcomes and raw hidden state.
- [x] Outcomes/ranking/surrogate evidence are resolved as-of the decision clock.
- [x] Leakage self-checks cover future candidates and future outcome stages.
- [x] Metrics include simulation efficiency, top-k recall, waste, effective V3 diversity, turnover/correlation failures, and robustness adjustment.
- [ ] **Add the explicit archive-V2 comparison requested by the benchmark matrix, or formally revise the benchmark.**  
  **Do:** add an `archive_v2` policy that reproduces the intended pre-V3 archive behavior, then include it in equal-budget comparisons; if another existing policy is intentionally equivalent, document and test that equivalence instead.

---

# P15 — Tests / regression

Existing coverage:

- [x] AST typing / arity.
- [x] motif eligibility and rendering.
- [x] recipe determinism/independence.
- [x] source-profile derivation.
- [x] grammar/semantic hash behavior.
- [x] structural distance.
- [x] archive niche identity.
- [x] family budget conservation.
- [x] crossover lineage/complexity.
- [x] deterministic plans.
- [x] novelty skip/no-capacity behavior.
- [x] V3 provenance columns.
- [x] generation-stat refresh.
- [x] legacy DB migration.
- [x] real comparison-expression parsing.
- [x] V2 canonical/dedup/queue/replay invariants remain covered.

Required regressions before P15 can be closed:

- [ ] Crossover `family == source_profile.primary_family`.
- [ ] Ordinary V3 planning produces at least one genuine cross-dataset motif under a deterministic fixture.
- [ ] Positive test for each promised V3 mutation operation.
- [ ] Negative/positive role-semantic tests for event/expectation motifs.
- [ ] Novelty report tests category novelty, archive sparsity, and numeric parent-child distance.
- [ ] Ranking test distinguishes archive sparsity from grammar novelty.
- [ ] Generation stats preserve `source_family != target_family` when appropriate.
- [ ] Generation stats learn concrete mutation operation separately from repair class.
- [ ] Skipped mutation preserves generation/parent/operation.
- [ ] Skipped crossover preserves both parents.
- [ ] Adaptive mutation allocation responds to evidence while reserving exploration.
- [ ] Archive-V2 replay policy/equivalence is benchmarked.

---

# P16 — Promotion order

Do not reinterpret P0–P15 as a reason to switch the live default. Close the reopened audit items first, then benchmark.

Recommended remaining implementation order:

```text
1. P0/P6 crossover family correctness
2. P4 cross-dataset planning + mutation vocabulary
3. P7 novelty completeness
4. P8 real archive sparsity
5. P9 source→target stats + adaptive mutation allocation
6. P10 skipped-lineage provenance
7. P2 semantic-role tightening
8. P14 archive-V2 benchmark
9. P15 regression closure
10. replay + small live V3 campaigns
```

- [ ] **Keep V2 as live default until comparative evidence exists.**
- [ ] Run equal-budget V2 vs V3 replay after the above fixes.
- [ ] Run small explicitly named live V3 campaigns only after replay is clean.

---

# P17 — Promotion gates

Initial V3 mix remains:

```text
explore   40%
exploit   25%
mutate    25%
crossover 10%
```

Recommended family cap:

```text
max_family_share = 0.35–0.50
```

Promote only if evidence shows improvement in:

- [ ] simulation efficiency
- [ ] grammar diversity
- [ ] semantic diversity
- [ ] correlation diversity

without material degradation in:

- [ ] IS_PASS rate
- [ ] CORR_PASS rate
- [ ] robustness-adjusted quality

---

# Final acceptance

Generator V3 is ready for promotion when:

- [ ] All open P0–P15 audit items above have regression coverage.
- [x] Exact, parameter, grammar, and semantic diversity are separately measurable.
- [x] Archive niches preserve expression topology.
- [x] One field can generate multiple deterministic recipes.
- [x] Multiple economic motifs and multi-field expressions exist.
- [x] Archive family/parent decisions affect generation.
- [x] Two-parent crossover works under bounded complexity.
- [x] V3 is auditable and reproducible.
- [x] V3 can be evaluated with point-in-time-safe replay.
- [ ] Equal-budget replay and small live campaigns justify promotion over V2.
- [x] Until then, V2 remains the default.

> **Core rule: search over hypotheses, not merely field names.**
