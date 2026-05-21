"""
forge.judge

Cloud-side rubric scoring. Given an ExperimentResult's artifacts, ask Opus to
grade each role on a fixed set of criteria, then return a normalized [0,1]
score for the rubric channel of the multi-metric score.

The rubric is operator-visible in this file. Edit RUBRIC to change what good
looks like — this is the "what's the proposer optimizing for" knob beyond the
hard test-pass-rate signal. The rationale per criterion is preserved in the
experiment journal so future proposer calls can learn from it.

This module DOES call Anthropic (Opus by default — see CONFIG). Tokens cost
roughly $0.20 per experiment (input ~10K artifact tokens + ~1K reasoning
output). Skip it during dry-runs by passing call_anthropic=False to score()
— it'll return rubric_score=0.5 with a placeholder rationale.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from typing import Any

from anthropic import Anthropic

from .config import CONFIG, ROLES, Role
from .experiment import BenchmarkResult, ExperimentResult


# ─────────────────────────────────────────────────────────────────────────────
# The rubric — edit me to change what "good" looks like
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Criterion:
    key: str          # short stable id used in JSON output
    description: str  # what the judge looks for


PLANNER_CRITERIA: tuple[Criterion, ...] = (
    Criterion("acceptance_specificity",
              "Every success criterion is observable and testable — names a concrete "
              "behaviour, command, or test artifact. No vague 'works correctly' criteria."),
    Criterion("scope_decomposition",
              "Spec breaks the work into individually verifiable features. Each feature "
              "has its own success criteria block."),
    Criterion("constraint_respect",
              "Spec respects the benchmark brief's stated boundaries (e.g., out-of-scope "
              "items are explicitly deferred, not silently included)."),
    Criterion("exec_summary_present",
              "Spec opens with a complete EXEC-SUMMARY block (OBJECTIVE/STATUS/HEADLINE/"
              "IMPLEMENTED/VALIDATED/TRADEOFFS/OBS/BLOCKERS) per the role contract."),
)

GENERATOR_CRITERIA: tuple[Criterion, ...] = (
    Criterion("implementation_completeness",
              "build-report.md credibly maps every acceptance criterion to specific code "
              "and a passing test. No 'TODO' or 'partial' admissions."),
    Criterion("test_coverage",
              "Generator wrote tests covering each criterion, not just the happy path. "
              "Tests can be re-run from the report."),
    Criterion("artifact_discipline",
              "Generator stayed inside its file scope (src/, bin/, tests/) and did not "
              "modify fixtures or the brief."),
    Criterion("evidence_quality",
              "build-report includes the actual test command + its passing output, not "
              "just a claim 'tests pass'."),
)

VALIDATOR_CRITERIA: tuple[Criterion, ...] = (
    Criterion("independent_verification",
              "Validator re-ran the tests itself; validation.md cites independent evidence, "
              "not just the generator's build-report claims."),
    Criterion("verdict_correctness",
              "Final VERDICT (PASS/FAIL) matches the actual test/spec reality. A PASS "
              "with failing tests is the worst possible outcome — score zero on this."),
    Criterion("criterion_table",
              "validation.md contains a per-criterion table (criterion → PASS/FAIL → evidence) "
              "with concrete evidence per row."),
    Criterion("gap_actionability",
              "FAIL verdicts cite specific gaps with reproduction steps and code locations, "
              "not vague concerns."),
)

RUBRIC: dict[Role, tuple[Criterion, ...]] = {
    "planner": PLANNER_CRITERIA,
    "generator": GENERATOR_CRITERIA,
    "validator": VALIDATOR_CRITERIA,
}


# ─────────────────────────────────────────────────────────────────────────────
# Output schema
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CriterionScore:
    key: str
    score: int       # 0..3 (0=missing, 1=weak, 2=adequate, 3=strong)
    rationale: str   # one-line judge commentary


@dataclass
class RoleScore:
    role: Role
    criteria: list[CriterionScore]
    average: float  # 0..1


@dataclass
class RubricResult:
    benchmark: str
    roles: list[RoleScore]
    overall: float  # 0..1, mean of role averages
    rationale: str  # judge's overall paragraph

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "overall": self.overall,
            "rationale": self.rationale,
            "roles": [
                {"role": r.role, "average": r.average,
                 "criteria": [asdict(c) for c in r.criteria]}
                for r in self.roles
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic call
# ─────────────────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are a strict but fair rubric grader for autoresearch experiments. You will
be shown the artifacts produced by three agents (planner, generator, validator)
running against a benchmark project. Your job is to score each agent on a small
set of fixed criteria, on a 0-3 scale:

  0 = missing or fundamentally wrong
  1 = weak / partial
  2 = adequate
  3 = strong

For EACH criterion you must:
  - Return an integer score 0..3.
  - Return a one-sentence rationale (under 30 words) citing the evidence that
    drove your score. Be specific — quote file names or excerpts if possible.

Reward hacking is the worst failure mode. If a Validator returned VERDICT: PASS
but the tests obviously did not pass, score `verdict_correctness` as 0. Do not
give credit for prose that papers over missing implementation.

Output ONLY a single JSON object matching the schema in the user message. No
prose outside the JSON. No code fences.
"""


def _build_user_payload(bench: BenchmarkResult) -> str:
    """Pack the artifacts + objective grading hooks into a single user message."""
    rubric_schema: dict[str, Any] = {"roles": {}, "overall_rationale": "<string>"}
    artifacts_block: list[str] = []

    for role in ROLES:
        crit = RUBRIC[role]
        rubric_schema["roles"][role] = {
            "criteria": [
                {"key": c.key, "score": "<int 0-3>", "rationale": "<string>"}
                for c in crit
            ],
        }
        # Find the role's artifact + final message in the benchmark result.
        role_data = next((r for r in bench.roles if r.role == role), None)
        artifact = (role_data.artifact if role_data and role_data.artifact else "(empty / not written)")
        final = (role_data.final_message if role_data and role_data.final_message else "(no final message)")
        artifacts_block.append(
            f"### {role.upper()} artifact\n```markdown\n{artifact}\n```\n\n"
            f"### {role.upper()} final assistant message\n```\n{final}\n```"
        )

    # Objective grounding for verdict_correctness: include the actual test outcome.
    test_summary = (
        f"Benchmark: {bench.benchmark}\n"
        f"Cycles: {bench.cycles}\n"
        f"Validator's VERDICT: {bench.verdict}\n"
        f"Tests: passed={bench.test.passed} / failed={bench.test.failed} / total={bench.test.total}\n"
        f"Test pass rate: {bench.test.pass_rate:.2f}"
    )

    criteria_table = "\n".join(
        f"- {role}/{c.key}: {c.description}"
        for role, crits in RUBRIC.items()
        for c in crits
    )

    return (
        "## Benchmark facts (ground truth)\n"
        f"{test_summary}\n\n"
        "## Criteria you must score\n"
        f"{criteria_table}\n\n"
        "## Agent artifacts\n"
        + "\n\n".join(artifacts_block)
        + "\n\n## Required output schema\n"
        "Return a JSON object exactly matching this shape (replace placeholders):\n"
        "```json\n" + json.dumps(rubric_schema, indent=2) + "\n```\n"
        "Output ONLY the JSON object, no prose."
    )


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse_judge_response(text: str) -> dict[str, Any]:
    """Extract the first JSON object from the response. Tolerates surrounding
    prose if the model didn't fully obey 'JSON only'."""
    m = _JSON_BLOCK_RE.search(text)
    if not m:
        raise ValueError(f"judge response had no JSON object: {text[:200]}")
    return json.loads(m.group(0))


def _empty_rubric(bench: BenchmarkResult, reason: str) -> RubricResult:
    """Used when judge is skipped or fails — gives a neutral 0.5 score."""
    roles = [
        RoleScore(
            role=role,
            criteria=[CriterionScore(c.key, 0, reason) for c in RUBRIC[role]],
            average=0.0,
        )
        for role in ROLES
    ]
    return RubricResult(benchmark=bench.benchmark, roles=roles, overall=0.0, rationale=reason)


def score_benchmark(bench: BenchmarkResult, *, call_anthropic: bool = True) -> RubricResult:
    """Grade one benchmark's artifacts. Returns a normalized [0,1] overall."""
    if not call_anthropic:
        return _empty_rubric(bench, "judge skipped (call_anthropic=False)")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _empty_rubric(bench, "ANTHROPIC_API_KEY not set; judge skipped")

    client = Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=CONFIG.anthropic_model,
            max_tokens=4096,
            thinking={"type": "enabled", "budget_tokens": CONFIG.anthropic_thinking_budget},
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_payload(bench)}],
        )
    except Exception as e:
        return _empty_rubric(bench, f"judge call failed: {e}")

    # Find the assistant text part (Opus returns thinking + text blocks).
    text_parts: list[str] = []
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text_parts.append(block.text)
    raw = "\n".join(text_parts)

    try:
        data = _parse_judge_response(raw)
    except Exception as e:
        return _empty_rubric(bench, f"judge response unparseable: {e}")

    roles_data = data.get("roles") or {}
    role_scores: list[RoleScore] = []
    for role in ROLES:
        crit_defs = RUBRIC[role]
        role_obj = roles_data.get(role) or {}
        crit_list_in = role_obj.get("criteria") or []
        by_key = {c.get("key"): c for c in crit_list_in if isinstance(c, dict)}
        criterion_scores: list[CriterionScore] = []
        for cdef in crit_defs:
            raw_c = by_key.get(cdef.key, {})
            try:
                score_val = int(raw_c.get("score", 0))
            except (TypeError, ValueError):
                score_val = 0
            score_val = max(0, min(3, score_val))
            criterion_scores.append(
                CriterionScore(
                    key=cdef.key,
                    score=score_val,
                    rationale=str(raw_c.get("rationale", "")).strip()[:280],
                )
            )
        # Per-role average normalized to [0,1].
        avg = (sum(c.score for c in criterion_scores) / (3 * len(crit_defs))) if crit_defs else 0.0
        role_scores.append(RoleScore(role=role, criteria=criterion_scores, average=avg))

    overall = sum(r.average for r in role_scores) / len(role_scores) if role_scores else 0.0
    return RubricResult(
        benchmark=bench.benchmark,
        roles=role_scores,
        overall=overall,
        rationale=str(data.get("overall_rationale", "")).strip(),
    )


def score_experiment(exp: ExperimentResult, *, call_anthropic: bool = True) -> tuple[float, list[RubricResult]]:
    """Score every benchmark in the experiment. Returns (mean_overall, per_benchmark_rubrics)."""
    rubrics = [score_benchmark(b, call_anthropic=call_anthropic) for b in exp.benchmarks]
    if not rubrics:
        return 0.0, []
    mean = sum(r.overall for r in rubrics) / len(rubrics)
    return mean, rubrics
