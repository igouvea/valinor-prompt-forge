"""
forge.proposer

The "scientist" of the autoresearch loop. Given the journal of past
experiments, the current champion prompts, and the latest failure context, it
proposes ONE targeted mutation to the three-prompt bundle.

Implementation: rather than asking the model to emit three 4-9 KB prompts as
escaped JSON (fragile), the proposer is a tool-using claude agent whose working
directory is an isolated CANDIDATE dir seeded with copies of the champion
prompts + context files. It edits planner.md / generator.md / validator.md in
place and writes RATIONALE.md. The orchestrator reads those files back. This
mirrors Karpathy's pattern (the agent edits the file) and avoids escaping bugs.

Always runs on the claude CLI at CONFIG.researcher_model (Opus), regardless of
which CLI drives the inner-loop agents.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG, ROLES
from .agent_cli import run_agent
from .experiment import ChampionPrompts, ExperimentResult


@dataclass
class Proposal:
    prompts: ChampionPrompts
    rationale: str    # full RATIONALE.md text
    hypothesis: str   # extracted ## Hypothesis (best-effort)
    cost_usd: float
    tokens_out: int = 0
    error: str | None = None


_SYSTEM_PROMPT = """\
You are the lead researcher in an autoresearch loop (Karpathy's pattern). You are
optimizing the SYSTEM PROMPTS of three coding agents — planner, generator,
validator — that run as a pipeline: planner writes a spec, generator implements
it and writes/runs tests, validator independently re-runs tests and emits a
VERDICT.

You maximize a single score, averaged over benchmarks:

    score = 0.5 * held_out_test_pass + 0.2 * time_efficiency + 0.3 * rubric_score

test_pass_rate is measured against HELD-OUT golden tests the agents never see — so
CONTEXT.md's failing-test names tell you EXACTLY which behaviours the generated code
gets wrong. time_efficiency rewards reaching a correct result in less wall-clock time.

Your working directory contains:
  - planner.md, generator.md, validator.md  → the CURRENT champion prompts. EDIT THESE.
  - program.md                              → your research policy. READ IT. Do NOT edit it.
  - JOURNAL.md                              → past experiments: scores, mutations, rationales.
  - CONTEXT.md                              → the latest experiment's failures + judge feedback.

Your job each turn: propose exactly ONE targeted mutation to the prompt bundle
that you hypothesize will raise the score, then APPLY it by editing the three
prompt files in place with your file tools.

Rules (from program.md — obey them):
  - Small, targeted edits beat full rewrites. Change one or two specific things.
  - Your hypothesis MUST cite a specific signal from CONTEXT.md — a named failing test, a
    ⚠️ low rubric criterion, or the cycles/time — and the exact prompt + section you change
    to address it, and which agent owns the failure. Generic "make it clearer" is rejected.
  - Do NOT repeat a mutation the journal shows was already tried and discarded.
  - Keep prompts BENCHMARK-AGNOSTIC: never name or hard-code a specific benchmark.
  - The three prompts are coupled — consider the whole pipeline, not one role in isolation.
  - You may edit one, two, or all three files, but the change must be coherent.

After editing, WRITE a file named RATIONALE.md with exactly these sections:
  ## Hypothesis
  (one paragraph)
  ## Changes
  (bullet list: which file, what changed)
  ## Why
  (one paragraph: why this should raise the score)

Do not edit anything other than the three prompt files and RATIONALE.md.
"""

_KICKOFF = (
    "Read program.md, JOURNAL.md, CONTEXT.md, and the three prompt files "
    "(planner.md, generator.md, validator.md). Then apply ONE targeted mutation by "
    "editing the prompt files in place, and write RATIONALE.md. When done, your final "
    "message should be a one-line summary of the mutation."
)


def build_context_md(exp: ExperimentResult, rubrics: list[Any], score: Any) -> str:
    """Human/agent-readable digest of the latest experiment's failures + grades.
    `rubrics` is list[judge.RubricResult]; `score` is scorer.Score (duck-typed to
    avoid an import cycle)."""
    lines: list[str] = [f"# Latest experiment: {exp.exp_id}", ""]
    lines.append(
        f"Score total={getattr(score, 'total', 0):.3f} "
        f"(tests={getattr(score, 'tests', 0):.2f}, "
        f"speed={getattr(score, 'speed', 0):.2f} @ {getattr(score, 'raw_wall_seconds', 0):.0f}s/bench, "
        f"rubric={getattr(score, 'rubric', 0):.2f})"
    )
    lines.append(
        "The speed channel rewards reaching a CORRECT result in less wall-clock time."
    )
    lines.append("")
    lines.append(
        "HOW TO READ THIS: trace every failure to its owning agent before proposing —\n"
        "  • a failing held-out test, or low GENERATOR rubric → the generator under-implemented it,\n"
        "    OR the PLANNER never specified that behaviour as an acceptance criterion;\n"
        "  • a wrong VERDICT or low VALIDATOR rubric → the validator wasn't skeptical enough;\n"
        "  • 0/0 tests → the implementation didn't honour the module contract at all;\n"
        "  • high cycles / time → the pipeline wandered.\n"
        "Your hypothesis must cite the SPECIFIC signal below and the exact prompt+section you change."
    )
    lines.append("")
    rub_by_bench = {r.benchmark: r for r in rubrics}
    for b in exp.benchmarks:
        lines.append(f"## Benchmark: {b.benchmark}")
        lines.append(
            f"- verdict: **{b.verdict}** · cycles: {b.cycles} · "
            f"held-out tests: **{b.test.passed}/{b.test.total} passed**"
        )
        # WHICH held-out behaviours the generated code got wrong (most actionable).
        failed_names = getattr(b.test, "failed_names", None) or []
        if failed_names:
            lines.append("\nFailing held-out tests — these exact behaviours are WRONG in the code:")
            for name in failed_names[:20]:
                lines.append(f"  - {name}")
        elif b.test.total == 0:
            lines.append(
                "\n**0 tests ran** — the implementation didn't even load against the contract "
                "(missing/wrongly-exported `src/index.js`, syntax error, or wrong module shape). "
                "The first fix is making the generator honour the EXACT module contract from the spec."
            )
            snippet = (b.test.raw_stderr or b.test.raw_stdout or "")[-700:]
            if snippet.strip():
                lines.append("```\n" + snippet.strip() + "\n```")
        # FULL rubric per role (all criteria), weak ones flagged.
        rub = rub_by_bench.get(b.benchmark)
        if rub:
            lines.append(f"\nJudge overall {rub.overall:.2f} — {rub.rationale}")
            for role_score in rub.roles:
                lines.append(f"\n**{role_score.role}** rubric:")
                for c in role_score.criteria:
                    flag = " ⚠️ WEAK" if c.score <= 1 else ""
                    lines.append(f"  - `{c.key}` {c.score}/3{flag}: {c.rationale}")
        lines.append("")
    return "\n".join(lines)


def build_journal_md(journal_entries: list[dict]) -> str:
    """Render the last N journal entries for the proposer to learn from."""
    if not journal_entries:
        return "# Journal\n\n(empty — this is an early experiment)\n"
    lines = ["# Journal (most recent last)", ""]
    for e in journal_entries:
        adopted = "ADOPTED ✓" if e.get("adopted") else "discarded ✗"
        lines.append(
            f"## {e.get('exp_id')} — score {e.get('score', 0):.3f} [{adopted}]"
        )
        if e.get("hypothesis"):
            lines.append(f"- hypothesis: {e['hypothesis']}")
        if e.get("changes_summary"):
            lines.append(f"- changes: {e['changes_summary']}")
        b = e.get("breakdown") or {}
        if b:
            lines.append(
                f"- breakdown: tests={b.get('tests', 0):.2f} "
                f"cycles={b.get('cycles', 0):.2f} rubric={b.get('rubric', 0):.2f}"
            )
        lines.append("")
    return "\n".join(lines)


def seed_candidate_dir(
    candidate_dir: Path,
    champion: ChampionPrompts,
    journal_entries: list[dict],
    context_md: str,
) -> None:
    """Lay out the proposer's sandbox: champion prompt copies + context files."""
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "planner.md").write_text(champion.planner, encoding="utf-8")
    (candidate_dir / "generator.md").write_text(champion.generator, encoding="utf-8")
    (candidate_dir / "validator.md").write_text(champion.validator, encoding="utf-8")
    program = CONFIG.repo_root / "program.md"
    if program.exists():
        (candidate_dir / "program.md").write_text(program.read_text(encoding="utf-8"), encoding="utf-8")
    (candidate_dir / "JOURNAL.md").write_text(build_journal_md(journal_entries), encoding="utf-8")
    (candidate_dir / "CONTEXT.md").write_text(context_md, encoding="utf-8")
    # Remove any stale rationale from a previous attempt in this dir.
    (candidate_dir / "RATIONALE.md").unlink(missing_ok=True)


_HYP_RE = re.compile(r"##\s*Hypothesis\s*\n(.+?)(?:\n##\s|\Z)", re.DOTALL | re.IGNORECASE)


def _extract_hypothesis(rationale: str) -> str:
    m = _HYP_RE.search(rationale)
    return (m.group(1).strip() if m else rationale.strip())[:500]


def propose(
    candidate_dir: Path,
    champion: ChampionPrompts,
    journal_entries: list[dict],
    context_md: str,
    log_dir: Path,
) -> Proposal:
    """Run the proposer agent against an isolated candidate dir. Returns the
    mutated prompts + rationale. On failure, returns the champion unchanged with
    .error set so the orchestrator can skip the experiment gracefully."""
    seed_candidate_dir(candidate_dir, champion, journal_entries, context_md)

    sp_file = log_dir / "proposer.system.txt"
    log_path = log_dir / "proposer.log"
    run = run_agent(
        system_prompt=_SYSTEM_PROMPT,
        user_message=_KICKOFF,
        work_dir=candidate_dir,
        sys_prompt_file=sp_file,
        log_path=log_path,
        cli="claude",                       # researcher is always claude/Opus
        model=CONFIG.researcher_model,
    )
    if run.is_error and not (candidate_dir / "RATIONALE.md").exists():
        return Proposal(champion, "", "", run.cost_usd, error=run.error or "proposer failed")

    try:
        mutated = ChampionPrompts.load(candidate_dir)
    except Exception as e:
        return Proposal(champion, "", "", run.cost_usd, error=f"could not read mutated prompts: {e}")

    rationale_path = candidate_dir / "RATIONALE.md"
    rationale = rationale_path.read_text(encoding="utf-8") if rationale_path.exists() else (run.final_text or "")
    return Proposal(
        prompts=mutated,
        rationale=rationale,
        hypothesis=_extract_hypothesis(rationale),
        cost_usd=run.cost_usd,
        tokens_out=run.tokens_out,
    )
