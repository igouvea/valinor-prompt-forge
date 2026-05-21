"""
forge.experiment

One experiment = run the current champion prompts against the configured
benchmarks, end to end (planner → generator → validator per benchmark, then
vitest), and record everything to state/runs/exp-NNNN/.

This module is the inner loop. It does NOT call Anthropic; it only drives
opencode + LM Studio + vitest. The proposer (Opus) and judge (Opus) are
separate.

Public API:
    run_experiment(prompts: ChampionPrompts, exp_id: str) -> ExperimentResult
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterator

from .config import CONFIG, ROLES, Role


# Resolve opencode's actual Node entrypoint so we can invoke node directly
# without going through cmd.exe's 8K command-line limit on Windows. The npm
# shim path is stable across opencode-ai installs.
def _find_opencode_entry() -> tuple[str, str] | None:
    """Return (node_exe, opencode-js-path) or None if not findable."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    candidates = [
        Path(appdata) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode",
    ]
    for cand in candidates:
        if cand.is_file():
            # Prefer a bundled node next to the npm shim if present, else PATH node.
            local_node = cand.parent.parent.parent.parent / "node.exe"
            node = str(local_node) if local_node.is_file() else "node"
            return node, str(cand)
    return None


_OPENCODE_ENTRY = _find_opencode_entry()


# ─────────────────────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ChampionPrompts:
    planner: str
    generator: str
    validator: str

    @classmethod
    def load(cls, directory: Path) -> "ChampionPrompts":
        return cls(
            planner=(directory / "planner.md").read_text(encoding="utf-8"),
            generator=(directory / "generator.md").read_text(encoding="utf-8"),
            validator=(directory / "validator.md").read_text(encoding="utf-8"),
        )

    def for_role(self, role: Role) -> str:
        return getattr(self, role)


@dataclass
class RoleResult:
    role: Role
    session_id: str | None
    exit_code: int | None
    wall_seconds: float
    final_message: str
    artifact: str | None  # the role's primary handoff artifact (spec/build-report/validation)
    error: str | None = None


@dataclass
class TestResult:
    passed: int
    failed: int
    total: int
    raw_stdout: str
    raw_stderr: str

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total > 0 else 0.0


@dataclass
class BenchmarkResult:
    benchmark: str
    cycles: int  # planner + (generator+validator) rounds. 1 = single-pass success/fail.
    verdict: str  # "pass" | "fail" | "unknown"
    test: TestResult
    roles: list[RoleResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "cycles": self.cycles,
            "verdict": self.verdict,
            "test": asdict(self.test),
            "roles": [asdict(r) for r in self.roles],
        }


@dataclass
class ExperimentResult:
    exp_id: str
    started_at: float
    finished_at: float
    benchmarks: list[BenchmarkResult] = field(default_factory=list)

    @property
    def total_cycles(self) -> int:
        return sum(b.cycles for b in self.benchmarks)

    @property
    def aggregate_pass_rate(self) -> float:
        if not self.benchmarks:
            return 0.0
        return sum(b.test.pass_rate for b in self.benchmarks) / len(self.benchmarks)

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_seconds": self.finished_at - self.started_at,
            "total_cycles": self.total_cycles,
            "aggregate_pass_rate": self.aggregate_pass_rate,
            "benchmarks": [b.to_dict() for b in self.benchmarks],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark scratch management
# ─────────────────────────────────────────────────────────────────────────────


# Files inside a benchmark that the agent generates. Wiped on reset.
GENERATED_PATTERNS = ["src", "bin", ".valinor", ".opencode"]


def reset_benchmark(bench_dir: Path) -> None:
    """Delete agent-generated artifacts but preserve brief, fixtures, deps."""
    for name in GENERATED_PATTERNS:
        target = bench_dir / name
        if target.exists():
            shutil.rmtree(target)
    # Wipe any agent-written test files (keep fixtures + the dir itself).
    tests_dir = bench_dir / "tests"
    if tests_dir.exists():
        for entry in tests_dir.iterdir():
            if entry.is_file() and entry.name.endswith(".test.js"):
                entry.unlink()
            elif entry.is_file() and entry.name.endswith(".test.ts"):
                entry.unlink()


def setup_valinor_dir(bench_dir: Path, readme_text: str) -> None:
    """Pre-create .valinor/handoff/ and write a minimal brief.json from README."""
    valinor = bench_dir / ".valinor"
    handoff = valinor / "handoff"
    handoff.mkdir(parents=True, exist_ok=True)

    brief = {
        "goal": readme_text,
        "audience": "the operator running this benchmark in valinor-prompt-forge",
        "boundaries": {"noGo": []},
        "source": "benchmark README.md (verbatim)",
    }
    (valinor / "brief.json").write_text(json.dumps(brief, indent=2), encoding="utf-8")
    # Empty steer + tasks files so the prompts that reference them don't error.
    (handoff / "steer.md").write_text("", encoding="utf-8")
    (valinor / "tasks.jsonl").write_text("", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Role prompt assembly
# ─────────────────────────────────────────────────────────────────────────────


# Handoff context that gets appended to each role's system prompt. Mirrors what
# Valinor's buildAgentPrompt does (codexHarness.ts:214-240).
HANDOFF_INPUTS: dict[Role, list[str]] = {
    "planner": [
        ".valinor/brief.json",
        ".valinor/tasks.jsonl",
        ".valinor/handoff/steer.md",
        ".valinor/handoff/backlog.md",
        ".valinor/handoff/validation.md",
    ],
    "generator": [
        ".valinor/handoff/spec.md",
        ".valinor/handoff/acceptance.md",
        ".valinor/handoff/validation.md",
    ],
    "validator": [
        ".valinor/handoff/spec.md",
        ".valinor/handoff/acceptance.md",
        ".valinor/handoff/build-report.md",
    ],
}

HANDOFF_OUTPUTS: dict[Role, list[str]] = {
    "planner": [
        ".valinor/handoff/spec.md",
        ".valinor/handoff/acceptance.md",
        ".valinor/handoff/backlog.md",
    ],
    "generator": [".valinor/handoff/build-report.md"],
    "validator": [".valinor/handoff/validation.md"],
}

ROLE_FINAL_INSTRUCTION: dict[Role, str] = {
    "planner": "Your final assistant message MUST be a one-paragraph summary of what you produced and the single most important next step.",
    "generator": "Your final assistant message MUST be a one-paragraph summary of what you produced and the single most important next step.",
    "validator": "Remember: your final message's first line must be `VERDICT: PASS` or `VERDICT: FAIL`.",
}


def _build_system_prompt(role: Role, prompts: ChampionPrompts) -> str:
    """Full system-prompt body for a role: champion prompt + handoff context."""
    inputs = "\n".join(f"- {p}" for p in HANDOFF_INPUTS[role])
    outputs = "\n".join(f"- {p}" for p in HANDOFF_OUTPUTS[role])
    body = prompts.for_role(role)
    final = ROLE_FINAL_INSTRUCTION[role]
    return (
        f"{body}\n\n"
        f"Incoming handoff files to read first (skip any that do not exist yet):\n"
        f"{inputs}\n\n"
        f"Output artifact(s) you must write:\n"
        f"{outputs}\n\n"
        f"{final}"
    )


# Short user kick-off message per role. The heavy lifting is in the system
# prompt (the agent definition); this is just the cue to start.
ROLE_USER_KICKOFF: dict[Role, str] = {
    "planner": (
        "Begin your role. Read .valinor/brief.json for the goal, then read the README.md "
        "in the project root for the benchmark spec. Produce .valinor/handoff/spec.md and "
        ".valinor/handoff/acceptance.md per your role instructions."
    ),
    "generator": (
        "Begin your role. Read .valinor/handoff/spec.md and .valinor/handoff/acceptance.md, "
        "then implement the plan in this repository. Write code, write tests, run them yourself "
        "with `npm test`, and only emit .valinor/handoff/build-report.md when all acceptance "
        "criteria are met and the tests pass."
    ),
    "validator": (
        "Begin your role. Read .valinor/handoff/spec.md, .valinor/handoff/acceptance.md, "
        ".valinor/handoff/build-report.md, and the actual source files + tests. Independently "
        "run the tests yourself with `npm test`. Emit .valinor/handoff/validation.md and start "
        "your final assistant message with exactly `VERDICT: PASS` or `VERDICT: FAIL`."
    ),
}


def _write_agent_definitions(bench_dir: Path, prompts: ChampionPrompts) -> None:
    """
    Write per-experiment opencode agent definition files to the benchmark's
    .opencode/agent/ directory. opencode discovers project-scoped agents there.
    Each agent's frontmatter sets mode=primary + permissive tool access, and
    the body is the full role system prompt (champion + handoff context).
    """
    agent_dir = bench_dir / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        body = _build_system_prompt(role, prompts)
        frontmatter = (
            "---\n"
            f"description: Valinor {role} for prompt-forge experiments\n"
            "mode: primary\n"
            "temperature: 0.2\n"
            "permission:\n"
            "  bash: allow\n"
            "  edit: allow\n"
            "  write: allow\n"
            "  read: allow\n"
            "  webfetch: deny\n"
            "  websearch: deny\n"
            "---\n"
        )
        (agent_dir / f"forge-{role}.md").write_text(frontmatter + body, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# opencode subprocess driver
# ─────────────────────────────────────────────────────────────────────────────


_SESSION_ID_RE = re.compile(r'"sessionID":"(ses_[A-Za-z0-9]+)"')


def _opencode_argv(*extra: str) -> list[str]:
    """
    Build a command argv that invokes opencode WITHOUT going through cmd.exe.

    On Windows, the `opencode` PATH entry is a .cmd shim that runs through
    cmd.exe (8K char limit). We bypass it by invoking `node <opencode-bin>`
    directly. CreateProcess on Windows allows ~32K-char command lines, which
    fits even our largest prompts.
    """
    if _OPENCODE_ENTRY is not None:
        node, entry = _OPENCODE_ENTRY
        return [node, entry, *extra]
    # Fallback: rely on PATH. shell=False; on Windows this requires a full
    # path to a .exe, which opencode isn't, so this path is unlikely to work
    # but we leave it for non-Windows platforms.
    return ["opencode", *extra]


def _spawn_opencode_with_agent(
    agent_name: str,
    user_message: str,
    bench_dir: Path,
    model: str,
    log_path: Path,
) -> tuple[int | None, str | None]:
    """
    Run `opencode run --agent <name> ...` with our role as a registered agent
    definition (so its system prompt is the role instructions, not the user
    message). Streams stdout to log_path, returns (exit_code, session_id).
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = _opencode_argv(
        "run",
        "--dangerously-skip-permissions",
        "--dir", str(bench_dir),
        "--agent", agent_name,
        "-m", model,
        "--format", "json",
        user_message,
    )
    session_id: str | None = None
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(bench_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if session_id is None:
                m = _SESSION_ID_RE.search(line)
                if m:
                    session_id = m.group(1)
        proc.wait(timeout=CONFIG.role_timeout_s)
    return proc.returncode, session_id


def _opencode_export_last_text(session_id: str) -> str:
    """`opencode export <sid>` → last assistant message's concatenated text parts."""
    try:
        out = subprocess.run(
            _opencode_argv("export", session_id),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
        )
    except subprocess.SubprocessError as e:
        return f"[opencode export failed: {e}]"
    if out.returncode != 0:
        return f"[opencode export exit {out.returncode}: {out.stderr.strip()}]"
    body = out.stdout
    brace = body.find("{")
    if brace < 0:
        return ""
    try:
        data = json.loads(body[brace:])
    except json.JSONDecodeError as e:
        return f"[opencode export parse failed: {e}]"
    messages = data.get("messages") or []
    last_assistant = None
    for m in reversed(messages):
        if (m.get("info") or {}).get("role") == "assistant":
            last_assistant = m
            break
    if not last_assistant:
        return ""
    parts = last_assistant.get("parts") or []
    texts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    return "\n".join(t for t in texts if t)


# Valinor's primary artifact filename per role.
ROLE_PRIMARY_ARTIFACT: dict[Role, str] = {
    "planner": ".valinor/handoff/spec.md",
    "generator": ".valinor/handoff/build-report.md",
    "validator": ".valinor/handoff/validation.md",
}


def run_role(
    role: Role,
    prompts: ChampionPrompts,
    bench_dir: Path,
    exp_run_dir: Path,
) -> RoleResult:
    """Run one role against the benchmark. Returns the role's outputs."""
    started = time.time()
    agent_name = f"forge-{role}"
    user_message = ROLE_USER_KICKOFF[role]
    log_path = exp_run_dir / f"stdout.{role}.log"

    exit_code, session_id = _spawn_opencode_with_agent(
        agent_name, user_message, bench_dir, CONFIG.experiment_model, log_path
    )
    wall = time.time() - started

    final_message = ""
    if session_id:
        final_message = _opencode_export_last_text(session_id)
        # Persist the full session export for the dashboard / proposer.
        try:
            export = subprocess.run(
                _opencode_argv("export", session_id),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                shell=False,
            )
            (exp_run_dir / f"session.{role}.json").write_text(export.stdout, encoding="utf-8")
        except subprocess.SubprocessError:
            pass

    artifact_path = bench_dir / ROLE_PRIMARY_ARTIFACT[role]
    artifact = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else None

    return RoleResult(
        role=role,
        session_id=session_id,
        exit_code=exit_code,
        wall_seconds=wall,
        final_message=final_message,
        artifact=artifact,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Verdict + test parsing
# ─────────────────────────────────────────────────────────────────────────────


def parse_verdict(final_message: str) -> str:
    if not final_message:
        return "unknown"
    first = final_message.lstrip().upper()[:32]
    if first.startswith("VERDICT: PASS"):
        return "pass"
    if first.startswith("VERDICT: FAIL"):
        return "fail"
    return "unknown"


def run_tests(bench_dir: Path) -> TestResult:
    """Run `npm test -- --reporter=json` in the benchmark dir; parse vitest output."""
    try:
        proc = subprocess.run(
            ["npm", "test", "--silent", "--", "--reporter=json"],
            cwd=str(bench_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            shell=(sys.platform == "win32"),
        )
    except subprocess.SubprocessError as e:
        return TestResult(0, 0, 0, raw_stdout="", raw_stderr=f"npm test failed to start: {e}")

    # Vitest --reporter=json emits the JSON object on stdout, sometimes preceded
    # by npm's own preamble. Find the first { and parse from there.
    body = proc.stdout
    brace = body.find("{")
    if brace < 0:
        # No tests written / vitest crashed before producing JSON
        return TestResult(passed=0, failed=0, total=0, raw_stdout=body, raw_stderr=proc.stderr)
    try:
        data = json.loads(body[brace:])
    except json.JSONDecodeError:
        return TestResult(passed=0, failed=0, total=0, raw_stdout=body, raw_stderr=proc.stderr)

    # Vitest JSON shape: {"numPassedTests": N, "numFailedTests": M, "numTotalTests": T, ...}
    passed = int(data.get("numPassedTests", 0))
    failed = int(data.get("numFailedTests", 0))
    total = int(data.get("numTotalTests", passed + failed))
    return TestResult(passed=passed, failed=failed, total=total, raw_stdout=body, raw_stderr=proc.stderr)


# ─────────────────────────────────────────────────────────────────────────────
# Public: one experiment
# ─────────────────────────────────────────────────────────────────────────────


def run_one_benchmark(
    benchmark: str,
    prompts: ChampionPrompts,
    exp_dir: Path,
) -> BenchmarkResult:
    bench_dir = CONFIG.benchmarks_dir / benchmark
    if not bench_dir.exists():
        raise FileNotFoundError(f"benchmark not found: {bench_dir}")

    readme_path = bench_dir / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    bench_run_dir = exp_dir / benchmark
    bench_run_dir.mkdir(parents=True, exist_ok=True)

    reset_benchmark(bench_dir)
    setup_valinor_dir(bench_dir, readme)
    _write_agent_definitions(bench_dir, prompts)

    roles: list[RoleResult] = []
    verdict = "unknown"
    cycles = 0

    # planner once, then generator+validator up to max_rework_rounds times.
    planner_res = run_role("planner", prompts, bench_dir, bench_run_dir)
    roles.append(planner_res)

    for round_idx in range(1, CONFIG.max_rework_rounds + 1):
        cycles += 1
        gen_res = run_role("generator", prompts, bench_dir, bench_run_dir)
        roles.append(gen_res)
        val_res = run_role("validator", prompts, bench_dir, bench_run_dir)
        roles.append(val_res)
        verdict = parse_verdict(val_res.final_message)
        if verdict == "pass":
            break

    test = run_tests(bench_dir)

    # Snapshot the benchmark's final state for the dashboard / proposer.
    snapshot_dir = bench_run_dir / "final-state"
    snapshot_dir.mkdir(exist_ok=True)
    for name in [".valinor", "src", "bin", "tests"]:
        src = bench_dir / name
        if src.exists():
            dst = snapshot_dir / name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)

    return BenchmarkResult(
        benchmark=benchmark,
        cycles=cycles,
        verdict=verdict,
        test=test,
        roles=roles,
    )


def run_experiment(prompts: ChampionPrompts, exp_id: str) -> ExperimentResult:
    """Run all configured benchmarks, return aggregate result."""
    exp_dir = CONFIG.runs_dir / exp_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the prompts that drove this experiment (for the proposer + audit).
    snap = exp_dir / "champion"
    snap.mkdir(exist_ok=True)
    (snap / "planner.md").write_text(prompts.planner, encoding="utf-8")
    (snap / "generator.md").write_text(prompts.generator, encoding="utf-8")
    (snap / "validator.md").write_text(prompts.validator, encoding="utf-8")

    started = time.time()
    bench_results: list[BenchmarkResult] = []
    for bench in CONFIG.benchmarks:
        print(f"[forge] running benchmark: {bench}", flush=True)
        result = run_one_benchmark(bench, prompts, exp_dir)
        bench_results.append(result)
        print(
            f"[forge]   {bench}: verdict={result.verdict} cycles={result.cycles} "
            f"tests={result.test.passed}/{result.test.total}",
            flush=True,
        )

    finished = time.time()
    exp = ExperimentResult(
        exp_id=exp_id,
        started_at=started,
        finished_at=finished,
        benchmarks=bench_results,
    )
    (exp_dir / "result.json").write_text(json.dumps(exp.to_dict(), indent=2), encoding="utf-8")
    return exp


# ─────────────────────────────────────────────────────────────────────────────
# CLI: uv run python -m forge.experiment <benchmark?>
# ─────────────────────────────────────────────────────────────────────────────


def _next_exp_id() -> str:
    CONFIG.runs_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(d.name for d in CONFIG.runs_dir.iterdir() if d.is_dir() and d.name.startswith("exp-"))
    if not existing:
        return "exp-0001"
    last = int(existing[-1].split("-")[1])
    return f"exp-{last + 1:04d}"


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    prompts = ChampionPrompts.load(CONFIG.prompts_champion_dir)
    exp_id = _next_exp_id()
    print(f"[forge] starting {exp_id} with experiment-model={CONFIG.experiment_model}", flush=True)
    result = run_experiment(prompts, exp_id)
    print(json.dumps(result.to_dict(), indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
