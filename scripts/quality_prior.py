"""Conditional quality prior over the trial ledger (P21) and the P20.2 scoring rule.

A global "which motif works" count is too coarse to steer a campaign: the P19/P22 evidence shows
the same structure pays in one source/recipe cell and not in another. This module estimates
``P(IS_PASS | context)`` over a bounded hierarchy

    motif x dataset x recipe bucket
        -> motif x dataset -> motif x category -> motif x role signature
        -> motif x mutation operation -> motif
        -> mutation operation x parent quality -> global

with two disciplines that the roadmap requires explicitly:

* **hierarchical backoff** — a narrow bucket is only trusted once it has minimum evidence;
  otherwise the answer comes from the next coarser level, and the level that answered is
  reported alongside the specific evidence so a thin bucket is never silently authoritative;
* **counts stay separate** — attempts, simulations, IS passes, correlation passes and skipped
  duplicates are distinct. A skipped rediscovery was never simulated and is not a negative
  performance outcome.

The score itself (P20.2) is ``bounded quality prior x novelty x uncertainty / cost``: every term
is bounded, the quality term has an explicit floor so strong negative evidence cannot zero a
region out of the search forever, and uncertainty re-enters as a UCB bonus rather than a reason
to trust a two-sample bucket.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field as dataclass_field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))

import canonical  # noqa: E402
import diversity  # noqa: E402
import expression_grammar as grammar  # noqa: E402
import quality_diagnostics as diagnostics  # noqa: E402
import research_db  # noqa: E402

QUALITY_PRIOR_VERSION = "quality-prior-v1"
#: Beta prior. ``1, 19`` is a 5% prior pass rate: the campaign's own measured baseline, so an
#: unobserved cell starts where the evidence says a fresh hypothesis starts.
DEFAULT_PRIOR_ALPHA = 1.0
DEFAULT_PRIOR_BETA = 19.0
#: Narrow levels below this many simulations never answer a lookup.
DEFAULT_MIN_EVIDENCE = 3
#: UCB exploration coefficient on the uncertainty term.
DEFAULT_EXPLORATION = 0.6
#: Bounds of the quality term. The floor is the exploration reserve: strongly negative evidence
#: downweights a region, it never deletes it from the search.
QUALITY_FLOOR = 0.01
QUALITY_CAP = 1.0
#: Bounds of the novelty term, so novelty alone can never dominate quality.
NOVELTY_FLOOR = 0.25
#: Bounds of the cost term's divisor.
COST_FLOOR = 0.25

#: Context attribute -> the ledger row field it is read from. The diagnostic corpus names the
#: source fields ``primary_dataset`` / ``primary_category`` / ``semantic_role_signature``; the
#: context uses the shorter names, and this mapping is the single place they meet.
CONTEXT_ROW_FIELDS: dict[str, str] = {
    "motif_id": "motif_id",
    "mutation_operation": "mutation_operation",
    "dataset": "primary_dataset",
    "category": "primary_category",
    "role_signature": "semantic_role_signature",
    "recipe_bucket": "recipe_bucket",
    "parent_quality_bucket": "parent_quality_bucket",
    "outer_operator": "outer_operator",
}

#: Lookup levels, most specific first. Each entry is ``(name, context attributes)``.
HIERARCHY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("motif+dataset+recipe", ("motif_id", "dataset", "recipe_bucket")),
    ("motif+dataset", ("motif_id", "dataset")),
    ("motif+category", ("motif_id", "category")),
    ("motif+role_signature", ("motif_id", "role_signature")),
    ("motif+operation", ("motif_id", "mutation_operation")),
    ("motif", ("motif_id",)),
    ("operation+parent_quality", ("mutation_operation", "parent_quality_bucket")),
    ("global", ()),
)


def _key(parts: Iterable[str]) -> str:
    return "|".join(str(part) for part in parts)


def _row_value(row: Mapping[str, Any], attribute: str) -> str:
    """Read one context attribute from a ledger row, accepting either spelling."""
    field = CONTEXT_ROW_FIELDS.get(attribute, attribute)
    value = row.get(field)
    if value is None and field != attribute:
        value = row.get(attribute)
    return str(value or "unknown")


@dataclass(frozen=True)
class Evidence:
    """Separated counts for one context cell (P21.2)."""

    attempts: int = 0
    simulations: int = 0
    is_pass: int = 0
    corr_pass: int = 0
    skipped: int = 0
    #: Sharpe sum, so a cell can report its mean quality without keeping every row.
    sharpe_sum: float = 0.0

    def plus(self, other: "Evidence") -> "Evidence":
        return Evidence(
            attempts=self.attempts + other.attempts,
            simulations=self.simulations + other.simulations,
            is_pass=self.is_pass + other.is_pass,
            corr_pass=self.corr_pass + other.corr_pass,
            skipped=self.skipped + other.skipped,
            sharpe_sum=self.sharpe_sum + other.sharpe_sum,
        )

    @property
    def mean_sharpe(self) -> float | None:
        return round(self.sharpe_sum / self.simulations, 4) if self.simulations else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "simulations": self.simulations,
            "is_pass": self.is_pass,
            "corr_pass": self.corr_pass,
            "skipped": self.skipped,
            "mean_sharpe": self.mean_sharpe,
        }


@dataclass(frozen=True)
class Context:
    """The pre-simulation facts a lookup is conditioned on."""

    motif_id: str = ""
    mutation_operation: str = ""
    dataset: str = ""
    category: str = ""
    role_signature: str = ""
    recipe_bucket: str = ""
    parent_quality_bucket: str = ""
    outer_operator: str = ""

    def level_key(self, attributes: Sequence[str]) -> str:
        return _key(getattr(self, name, "") or "unknown" for name in attributes)

    def as_dict(self) -> dict[str, str]:
        return {
            name: str(getattr(self, name) or "")
            for name in (
                "motif_id", "mutation_operation", "dataset", "category", "role_signature",
                "recipe_bucket", "parent_quality_bucket", "outer_operator",
            )
        }


@dataclass(frozen=True)
class Look:
    """What the prior knows about one context, and which level answered."""

    mean: float
    lower: float
    upper: float
    simulations: int
    level: str
    #: The narrowest cell of this context that has any evidence, however thin it is.
    specific: Evidence
    #: Which hierarchy level ``specific`` came from (may be coarser than the hypothesis).
    specific_level: str = "none"
    global_prior: float = 0.0
    #: True when no level met the minimum evidence and the global prior answered.
    backed_off: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "mean": round(self.mean, 6),
            "lower": round(self.lower, 6),
            "upper": round(self.upper, 6),
            "simulations": self.simulations,
            "level": self.level,
            "specific": self.specific.as_dict(),
            "specific_level": self.specific_level,
            "global_prior": round(self.global_prior, 6),
            "backed_off": self.backed_off,
        }


def _beta_interval(passes: int, simulations: int, alpha: float, beta: float,
                   *, z: float = 1.0) -> tuple[float, float, float]:
    """Beta-binomial posterior mean and a bounded credible interval.

    ``z`` scales an approximate normal interval on the posterior, which is what keeps a
    two-sample bucket from looking as authoritative as a fifty-sample one.
    """
    alpha_post = alpha + passes
    beta_post = beta + max(0, simulations - passes)
    total = alpha_post + beta_post
    mean = alpha_post / total
    variance = (alpha_post * beta_post) / (total * total * (total + 1.0))
    deviation = z * math.sqrt(max(0.0, variance))
    return mean, max(0.0, mean - deviation), min(1.0, mean + deviation)


class QualityPrior:
    """Bounded, point-in-time conditional statistics over the trial ledger."""

    def __init__(
        self,
        tables: Mapping[str, Mapping[str, Evidence]] | None = None,
        *,
        as_of: str = "",
        alpha: float = DEFAULT_PRIOR_ALPHA,
        beta: float = DEFAULT_PRIOR_BETA,
        min_evidence: int = DEFAULT_MIN_EVIDENCE,
        exploration: float = DEFAULT_EXPLORATION,
    ) -> None:
        self.tables: dict[str, dict[str, Evidence]] = {
            name: dict(table) for name, table in (tables or {}).items()
        }
        self.as_of = str(as_of or "")
        self.alpha = float(alpha)
        self.beta = float(beta)
        self.min_evidence = int(min_evidence)
        self.exploration = float(exploration)

    # -- construction -------------------------------------------------------

    @classmethod
    def from_rows(
        cls,
        rows: Sequence[Mapping[str, Any]],
        *,
        as_of: str = "",
        min_evidence: int = DEFAULT_MIN_EVIDENCE,
        exploration: float = DEFAULT_EXPLORATION,
    ) -> "QualityPrior":
        tables: dict[str, dict[str, Evidence]] = {name: {} for name, _ in HIERARCHY}
        for row in rows:
            skipped = str(row.get("decision") or "") == "SKIP_REDUNDANT" or not row.get("simulated")
            simulated = bool(row.get("simulated")) and not skipped
            evidence = Evidence(
                attempts=1,
                simulations=1 if simulated else 0,
                is_pass=1 if simulated and row.get("is_pass") else 0,
                corr_pass=1 if simulated and row.get("corr_pass") else 0,
                skipped=1 if skipped else 0,
                sharpe_sum=float(row.get("sharpe") or 0.0) if simulated else 0.0,
            )
            for name, attributes in HIERARCHY:
                key = _key(_row_value(row, attribute) for attribute in attributes) or "global"
                tables[name][key] = tables[name].get(key, Evidence()).plus(evidence)
        return cls(tables, as_of=as_of, min_evidence=min_evidence, exploration=exploration)

    @classmethod
    def build(
        cls,
        db: research_db.ResearchDB,
        *,
        as_of: str | None = None,
        generator_versions: Sequence[str] | None = None,
        min_evidence: int = DEFAULT_MIN_EVIDENCE,
        exploration: float = DEFAULT_EXPLORATION,
    ) -> "QualityPrior":
        """Build the prior from evidence settled at or before ``as_of`` (point-in-time safe)."""
        rows = diagnostics.load_evidence(db, as_of=as_of, generator_versions=generator_versions)
        clock = as_of or max((str(row.get("settled_at") or "") for row in rows), default="")
        return cls.from_rows(
            rows, as_of=clock, min_evidence=min_evidence, exploration=exploration,
        )

    # -- lookup -------------------------------------------------------------

    @property
    def global_evidence(self) -> Evidence:
        return self.tables.get("global", {}).get("global", Evidence())

    @property
    def global_prior(self) -> float:
        """The Beta posterior mean over everything observed so far."""
        evidence = self.global_evidence
        mean, _, _ = _beta_interval(
            evidence.simulations and evidence.is_pass, evidence.simulations or 0, self.alpha, self.beta,
        )
        return mean

    def lookup(self, context: Context) -> Look:
        """Hierarchical backoff: the most specific level with minimum evidence answers.

        The specific cell's own counts are always reported, so a caller can see that its answer
        came from a coarser level because the narrow one had two samples.
        """
        # ``specific`` is the narrowest cell of *this* context that has any evidence at all,
        # so a caller can always see how thin the narrow answer is even when the returned
        # estimate came from a coarser level (P21.2).
        specific = Evidence()
        specific_level = "none"
        for name, attributes in HIERARCHY:
            key = context.level_key(attributes) or "global"
            cell = self.tables.get(name, {}).get(key)
            if cell is not None and specific_level == "none":
                specific = cell
                specific_level = name
            if cell is None or cell.simulations < self.min_evidence:
                continue
            mean, lower, upper = _beta_interval(
                cell.is_pass, cell.simulations, self.alpha, self.beta,
            )
            return Look(
                mean=mean, lower=lower, upper=upper, simulations=cell.simulations,
                level=name, specific=specific, specific_level=specific_level,
                global_prior=self.global_prior, backed_off=False,
            )
        # Nothing met the bar: answer from everything at the coarsest level and say so.
        evidence = self.global_evidence
        mean, lower, upper = _beta_interval(
            evidence.is_pass, evidence.simulations, self.alpha, self.beta,
        )
        return Look(
            mean=mean, lower=lower, upper=upper, simulations=evidence.simulations,
            level="global", specific=specific, specific_level=specific_level,
            global_prior=mean, backed_off=True,
        )

    def score(
        self,
        context: Context,
        *,
        novelty: float = 1.0,
        cost: float = 1.0,
        uncertainty: float | None = None,
    ) -> tuple[float, Look]:
        """``bounded quality x novelty x uncertainty / cost`` for one hypothesis (P20.2)."""
        look = self.lookup(context)
        score = quality_conditioned_score(
            look, novelty=novelty, cost=cost, uncertainty=uncertainty,
            exploration=self.exploration,
        )
        return score, look

    def rank(
        self,
        contexts: Sequence[Context],
        *,
        novelty: Mapping[int, float] | None = None,
        cost: Mapping[int, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Score a list of hypotheses; ties break on the more specific evidence, then index."""
        ranked: list[dict[str, Any]] = []
        for index, context in enumerate(contexts):
            score, look = self.score(
                context,
                novelty=float((novelty or {}).get(index, 1.0)),
                cost=float((cost or {}).get(index, 1.0)),
            )
            ranked.append({"index": index, "score": round(score, 8), "look": look.as_dict(),
                           "context": context.as_dict()})
        ranked.sort(key=lambda row: (-row["score"], -row["look"]["simulations"], row["index"]))
        return ranked

    def table_summary(self, level: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """The best-evidenced cells of one level, for reporting and for tests."""
        cells = []
        for key, cell in self.tables.get(level, {}).items():
            mean, lower, upper = _beta_interval(cell.is_pass, cell.simulations, self.alpha, self.beta)
            cells.append({
                "key": key, **cell.as_dict(), "mean": round(mean, 6),
                "lower": round(lower, 6), "upper": round(upper, 6),
                "enough_evidence": cell.simulations >= self.min_evidence,
            })
        cells.sort(key=lambda row: (-row["mean"], -row["simulations"], row["key"]))
        return cells[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": QUALITY_PRIOR_VERSION,
            "as_of": self.as_of,
            "min_evidence": self.min_evidence,
            "exploration": self.exploration,
            "global_prior": round(self.global_prior, 6),
            "global": self.global_evidence.as_dict(),
            "levels": {name: len(table) for name, table in self.tables.items()},
        }


def context_from_row(row: Mapping[str, Any]) -> Context:
    """The pre-simulation context of one *candidate* row, for ranking and scheduling.

    Derived only from what the row itself says, so it is cheap enough to call per queued
    candidate. ``parent_quality_bucket`` is left empty: resolving a parent's outcome needs the
    full ledger row map, which is what the diagnostic corpus (not the scheduler) is for — and
    an unset attribute simply reads as ``unknown`` at every level that uses it.
    """
    expression = str(row.get("normalized_expression") or "")
    parameters = row.get("mutation_parameters_json")
    if isinstance(parameters, str):
        try:
            parameters = json.loads(parameters)
        except ValueError:
            parameters = {}
    parameters = parameters if isinstance(parameters, Mapping) else {}
    settings = row.get("settings_json")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except ValueError:
            settings = {}
    settings = settings if isinstance(settings, Mapping) else {}
    profile = diversity.derive_source_profile(expression) or {}
    fields = [str(name) for name in (profile.get("field_ids") or canonical.fields_of(expression))]
    roles = sorted({role for field in fields for role in grammar.infer_field_roles(field)})
    recipe = row.get("recipe_json")
    if isinstance(recipe, str):
        try:
            recipe = json.loads(recipe)
        except ValueError:
            recipe = None
    if not isinstance(recipe, Mapping) or not recipe:
        recipe = diagnostic_recipe(expression, settings)
    return Context(
        motif_id=str(row.get("motif_id") or parameters.get("motif_id") or "none"),
        mutation_operation=canonical.normalize_mutation_operation(parameters.get("operation")) or "none",
        dataset=(list(profile.get("datasets") or []) or ["unknown"])[0],
        category=(list(profile.get("categories") or []) or ["unknown"])[0],
        role_signature="+".join(roles) or "generic",
        recipe_bucket=diagnostics.recipe_bucket(recipe),
        outer_operator=diagnostics.outer_operator(expression),
    )


def diagnostic_recipe(expression: str, settings: Mapping[str, Any]) -> dict[str, Any]:
    """The stored recipe, or one reconstructed from settings plus the first window (P21.3)."""
    import seed_bank  # local: keeps the module import graph acyclic

    return seed_bank.recipe_from_settings(expression, settings)


def quality_conditioned_score(
    look: Look,
    *,
    novelty: float = 1.0,
    cost: float = 1.0,
    uncertainty: float | None = None,
    exploration: float = DEFAULT_EXPLORATION,
) -> float:
    """The bounded objective replacing "novelty -> simulate" (P20.2).

    ``quality x novelty x uncertainty / cost``, with every term bounded:

    * quality uses the posterior's **upper** bound, so genuine uncertainty can pay, but it is
      clamped to ``[QUALITY_FLOOR, QUALITY_CAP]`` — a region with strongly negative evidence is
      downweighted, never deleted;
    * novelty has a floor of :data:`NOVELTY_FLOOR` so it cannot compensate for absent quality
      but also cannot be switched off;
    * uncertainty is the UCB bonus ``1 + exploration * sqrt(u)`` where ``u`` is the gap between
      the posterior's upper and lower bounds;
    * cost divides the whole thing with a floor, and is where a caller expresses the price of a
      simulation for a band or a mutation distance.
    """
    quality = max(QUALITY_FLOOR, min(QUALITY_CAP, float(look.upper)))
    novelty_term = NOVELTY_FLOOR + (1.0 - NOVELTY_FLOOR) * max(0.0, min(1.0, float(novelty)))
    if uncertainty is None:
        uncertainty = max(0.0, float(look.upper) - float(look.lower))
    uncertainty_term = 1.0 + max(0.0, float(exploration)) * math.sqrt(max(0.0, float(uncertainty)))
    cost_term = max(COST_FLOOR, float(cost))
    return quality * novelty_term * uncertainty_term / cost_term


#: Distance bands priced as cost multipliers relative to a parameter-only edit (P22.1): a D4
#: semantic change is a bigger bet than a D1 recipe move, so it must earn a better prior.
BAND_COST: dict[str, float] = {"D0": 0.5, "D1": 1.0, "D2": 1.1, "D3": 1.35, "D4": 1.6}

#: Mutation ladder, cheapest and most structure-preserving first (P22.1). Crossover is last
#: because its marginal value has to be demonstrated before it earns budget.
MUTATION_LADDER: tuple[str, ...] = (
    "recipe_adjustment",
    "normalization_change",
    "group_change",
    "field_swap",
    "dataset_swap",
    "add_component",
    "motif_change",
    "crossover",
)


def ladder_prior(
    prior: QualityPrior,
    *,
    motif_id: str = "",
    dataset: str = "",
    category: str = "",
    role_signature: str = "",
    recipe_bucket: str = "",
    parent_quality_bucket: str = "",
) -> list[dict[str, Any]]:
    """Rank the mutation ladder for one context, cheapest justified step first (P22.1)."""
    rows: list[dict[str, Any]] = []
    for index, operation in enumerate(MUTATION_LADDER):
        context = Context(
            motif_id=motif_id, mutation_operation=operation, dataset=dataset,
            category=category, role_signature=role_signature, recipe_bucket=recipe_bucket,
            parent_quality_bucket=parent_quality_bucket,
        )
        score, look = prior.score(context, novelty=1.0, cost=BAND_COST.get(
            {0: "D0", 1: "D1", 2: "D2", 3: "D3", 4: "D4"}.get(min(index, 4), "D4"), 1.0,
        ))
        rows.append({"operation": operation, "rank": index, "score": round(score, 8),
                     "look": look.as_dict()})
    rows.sort(key=lambda row: (-row["score"], row["rank"]))
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Conditional quality prior over the trial ledger (P21)")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--as-of", dest="as_of")
    parser.add_argument("--min-evidence", type=int, default=DEFAULT_MIN_EVIDENCE)
    parser.add_argument("--level", default="motif+dataset+recipe")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    with research_db.ResearchDB.open(args.db) as db:
        prior = QualityPrior.build(db, as_of=args.as_of, min_evidence=args.min_evidence)
    report = {
        "prior": prior.as_dict(),
        "cells": prior.table_summary(args.level, limit=args.limit),
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
