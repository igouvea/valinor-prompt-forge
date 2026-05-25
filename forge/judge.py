"""
forge.judge

Rubric scoring via the authed `claude` CLI (no API key). Given an
ExperimentResult's artifacts, ask Opus to grade each role on a fixed set of
criteria, then return a normalized [0,1] score for the rubric channel of the
multi-metric score.

The rubric is operator-visible in this file. Edit RUBRIC to change what good
looks like — this is the "what's the proposer optimizing for" knob beyond the
hard test-pass-rate signal. Per-criterion rationale is preserved in the journal
so future proposer calls can learn from it.

This calls the model (Opus by default — CONFIG.researcher_model) once per
benchmark. Skip it during dry-runs with call_judge=False — it returns a neutral
zero rubric with a placeholder rationale.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

from .config import CONFIG, ROLES, Role
from .agent_cli import run_researcher
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
    cost_usd: float = 0.0
    tokens_out: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "benchmark": self.benchmark,
            "overall": self.overall,
            "rationale": self.rationale,
            "cost_usd": self.cost_usd,
            "roles": [
                {"role": r.role, "average": r.average,
                 "criteria": [asdict(c) for c in r.criteria]}
                for r in self.roles
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Prompt construction
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
        role_data = next((r for r in bench.roles if r.role == role), None)
        artifact = (role_data.artifact if role_data and role_data.artifact else "(empty / not written)")
        final = (role_data.final_message if role_data and role_data.final_message else "(no final message)")
        artifacts_block.append(
            f"### {role.upper()} artifact\n```markdown\n{artifact}\n```\n\n"
            f"### {role.upper()} final assistant message\n```\n{final}\n```"
        )

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
    """Used when the judge is skipped or fails — gives a neutral zero score."""
    roles = [
        RoleScore(
            role=role,
            criteria=[CriterionScore(c.key, 0, reason) for c in RUBRIC[role]],
            average=0.0,
        )
        for role in ROLES
    ]
    return RubricResult(benchmark=bench.benchmark, roles=roles, overall=0.0, rationale=reason)


def _assemble(bench: BenchmarkResult, data: dict[str, Any], cost: float, tokens: int = 0) -> RubricResult:
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
        avg = (sum(c.score for c in criterion_scores) / (3 * len(crit_defs))) if crit_defs else 0.0
        role_scores.append(RoleScore(role=role, criteria=criterion_scores, average=avg))

    overall = sum(r.average for r in role_scores) / len(role_scores) if role_scores else 0.0
    return RubricResult(
        benchmark=bench.benchmark,
        roles=role_scores,
        overall=overall,
        rationale=str(data.get("overall_rationale", "")).strip(),
        cost_usd=cost,
        tokens_out=tokens,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public
# ─────────────────────────────────────────────────────────────────────────────


def score_benchmark(bench: BenchmarkResult, *, log_dir: Path, call_judge: bool = True) -> RubricResult:
    """Grade one benchmark's artifacts. Returns a normalized [0,1] overall."""
    if not call_judge:
        return _empty_rubric(bench, "judge skipped (call_judge=False)")

    sp_file = log_dir / f"judge.{bench.benchmark}.system.txt"
    log_path = log_dir / f"judge.{bench.benchmark}.log"
    run = run_researcher(
        system_prompt=_SYSTEM_PROMPT,
        user_message=_build_user_payload(bench),
        sys_prompt_file=sp_file,
        log_path=log_path,
    )
    if run.is_error or not run.final_text:
        return _empty_rubric(bench, f"judge call failed: {run.error or 'empty response'}")
    try:
        data = _parse_judge_response(run.final_text)
    except Exception as e:
        return _empty_rubric(bench, f"judge response unparseable: {e}")
    return _assemble(bench, data, run.cost_usd, run.tokens_out)


def score_experiment(
    exp: ExperimentResult, *, log_dir: Path | None = None, call_judge: bool = True,
    on_judge: "callable | None" = None,
) -> tuple[float, list[RubricResult]]:
    """Score every benchmark. Returns (mean_overall, per_benchmark_rubrics).
    `on_judge(benchmark, phase)` is an optional progress hook ("start"/"done")."""
    log_dir = log_dir or (CONFIG.runs_dir / exp.exp_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    rubrics: list[RubricResult] = []
    for b in exp.benchmarks:
        if on_judge:
            on_judge(b.benchmark, "start", 0)
        r = score_benchmark(b, log_dir=log_dir, call_judge=call_judge)
        rubrics.append(r)
        if on_judge:
            on_judge(b.benchmark, "done", r.tokens_out)
    if not rubrics:
        return 0.0, []
    mean = sum(r.overall for r in rubrics) / len(rubrics)
    return mean, rubrics
