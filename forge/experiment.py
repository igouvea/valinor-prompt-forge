"""
forge.experiment

One experiment = run the current champion prompts against the configured
benchmarks, end to end (planner → generator → validator per benchmark, then
vitest), and record everything to state/runs/exp-NNNN/.

This module is the inner loop. It does NOT mutate or grade prompts; it only
drives an authed agentic CLI (claude/codex via forge.agent_cli) + vitest. The
proposer (mutate) and judge (grade) are the separate outer loop.

Public API:
    run_experiment(prompts: ChampionPrompts, exp_id: str) -> ExperimentResult
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .config import CONFIG, ROLES, Role
from .agent_cli import run_agent


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
    cost_usd: float = 0.0
    num_turns: int = 0
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

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.roles)

    @property
    def wall_seconds(self) -> float:
        """Total agent wall-clock time spent on this benchmark (all roles)."""
        return sum(r.wall_seconds for r in self.roles)

    def to_dict(self) -> dict:
        return {
            "benchmark": self.benchmark,
            "cycles": self.cycles,
            "verdict": self.verdict,
            "cost_usd": self.cost_usd,
            "wall_seconds": self.wall_seconds,
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

    @property
    def total_cost_usd(self) -> float:
        return sum(b.cost_usd for b in self.benchmarks)

    @property
    def total_agent_wall_seconds(self) -> float:
        """Sum of agent wall time across all benchmarks (all roles)."""
        return sum(b.wall_seconds for b in self.benchmarks)

    @property
    def mean_benchmark_wall_seconds(self) -> float:
        """Average agent wall time per benchmark — the input to the speed score."""
        if not self.benchmarks:
            return 0.0
        return self.total_agent_wall_seconds / len(self.benchmarks)

    def to_dict(self) -> dict:
        return {
            "exp_id": self.exp_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "wall_seconds": self.finished_at - self.started_at,
            "total_agent_wall_seconds": self.total_agent_wall_seconds,
            "mean_benchmark_wall_seconds": self.mean_benchmark_wall_seconds,
            "total_cycles": self.total_cycles,
            "aggregate_pass_rate": self.aggregate_pass_rate,
            "total_cost_usd": self.total_cost_usd,
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
            if entry.is_file() and (entry.name.endswith(".test.js") or entry.name.endswith(".test.ts")):
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
# Valinor's buildAgentPrompt does (codexHarness.ts).
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


# Short user kick-off message per role. The role contract lives in the system
# prompt; this is just the cue to start.
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
    """Run one role against the benchmark via the authed CLI. Returns outputs."""
    system_prompt = _build_system_prompt(role, prompts)
    user_message = ROLE_USER_KICKOFF[role]
    log_path = exp_run_dir / f"stdout.{role}.log"
    sp_file = exp_run_dir / f"system.{role}.txt"

    run = run_agent(
        system_prompt=system_prompt,
        user_message=user_message,
        work_dir=bench_dir,
        sys_prompt_file=sp_file,
        log_path=log_path,
        label=role,
    )

    artifact_path = bench_dir / ROLE_PRIMARY_ARTIFACT[role]
    artifact = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else None

    return RoleResult(
        role=role,
        session_id=run.session_id,
        exit_code=run.exit_code,
        wall_seconds=run.wall_seconds,
        final_message=run.final_text,
        artifact=artifact,
        cost_usd=run.cost_usd,
        num_turns=run.num_turns,
        error=run.error,
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


# Golden test files are copied in with this filename marker so the test runner
# can be filtered to ONLY them. This isolates scoring from any tests the agent
# wrote (in tests/, src/, *.spec.js, anywhere) — those never count.
GOLDEN_MARKER = "__forge_golden__"


def _inject_golden_tests(bench_dir: Path) -> bool:
    """If a held-out golden suite exists at golden/<benchmark>/, copy it into the
    benchmark's tests/ dir (marked, so the runner targets only it) so scoring
    measures true correctness against edge cases the agent never saw. Returns
    True if injected.

    The golden suite lives OUTSIDE the agent's working dir and is copied in only
    here, after the agents have finished — so they cannot read or overfit to it.
    Kept in tests/ (not a subdir) so its `../src/index.js` import still resolves.
    The next experiment's reset_benchmark() wipes the benchmark scratch."""
    golden_dir = CONFIG.repo_root / "golden" / bench_dir.name
    golden_files = sorted(golden_dir.glob("*.test.js")) if golden_dir.exists() else []
    if not golden_files:
        return False
    tests_dir = bench_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    for f in tests_dir.glob(f"{GOLDEN_MARKER}*"):
        f.unlink()
    for gf in golden_files:
        shutil.copy2(gf, tests_dir / f"{GOLDEN_MARKER}{gf.name}")
    return True


def run_tests(bench_dir: Path) -> TestResult:
    """Run vitest in the benchmark dir and parse the JSON summary.

    If a held-out golden suite exists, it is injected and the runner is FILTERED
    to only the golden files — so the score reflects true correctness against
    our edge cases, never the generator's (gameable) self-tests, wherever it put
    them."""
    held_out = _inject_golden_tests(bench_dir)
    filter_args = [GOLDEN_MARKER] if held_out else []
    try:
        proc = subprocess.run(
            ["npm", "test", "--silent", "--", *filter_args, "--reporter=json"],
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
        return TestResult(passed=0, failed=0, total=0, raw_stdout=body, raw_stderr=proc.stderr)
    try:
        data = json.loads(body[brace:])
    except json.JSONDecodeError:
        return TestResult(passed=0, failed=0, total=0, raw_stdout=body, raw_stderr=proc.stderr)

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
    on_role: "callable | None" = None,
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

    roles: list[RoleResult] = []
    verdict = "unknown"
    cycles = 0

    def _emit(role: str, phase: str, result: "RoleResult | None" = None) -> None:
        if on_role:
            on_role(benchmark, role, phase, result)

    # planner once, then generator+validator up to max_rework_rounds times.
    _emit("planner", "start")
    planner_res = run_role("planner", prompts, bench_dir, bench_run_dir)
    roles.append(planner_res)
    _emit("planner", "done", planner_res)

    for _round_idx in range(1, CONFIG.max_rework_rounds + 1):
        cycles += 1
        _emit("generator", "start")
        gen_res = run_role("generator", prompts, bench_dir, bench_run_dir)
        roles.append(gen_res)
        _emit("generator", "done", gen_res)

        _emit("validator", "start")
        val_res = run_role("validator", prompts, bench_dir, bench_run_dir)
        roles.append(val_res)
        verdict = parse_verdict(val_res.final_message)
        _emit("validator", "done", val_res)
        if verdict == "pass":
            break

    # Snapshot the benchmark's final state FIRST — this captures the agent's own
    # code + tests before run_tests() swaps in the held-out golden suite.
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

    # Score on the held-out golden tests (true correctness, not self-consistency).
    test = run_tests(bench_dir)

    return BenchmarkResult(
        benchmark=benchmark,
        cycles=cycles,
        verdict=verdict,
        test=test,
        roles=roles,
    )


def run_experiment(prompts: ChampionPrompts, exp_id: str, on_role: "callable | None" = None) -> ExperimentResult:
    """Run all configured benchmarks, return aggregate result. `on_role(bench,
    role, phase)` is an optional progress callback for the live dashboard."""
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
        result = run_one_benchmark(bench, prompts, exp_dir, on_role=on_role)
        bench_results.append(result)
        print(
            f"[forge]   {bench}: verdict={result.verdict} cycles={result.cycles} "
            f"tests={result.test.passed}/{result.test.total} cost=${result.cost_usd:.2f}",
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
# CLI: forge eval
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
    print(
        f"[forge] starting {exp_id} via cli={CONFIG.agent_cli} model={CONFIG.agent_model()}",
        flush=True,
    )
    result = run_experiment(prompts, exp_id)
    print(json.dumps(result.to_dict(), indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
