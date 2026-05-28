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
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable

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
    tokens_out: int = 0
    error: str | None = None


@dataclass
class TestResult:
    passed: int
    failed: int
    total: int
    raw_stdout: str
    raw_stderr: str
    failed_names: list[str] = field(default_factory=list)  # titles of failing held-out tests

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

# Template copy exclusions. The per-run workspace is built from benchmark
# source files only; prior agent artifacts stay out of the next experiment.
SCRATCH_DIR_NAMES = {
    ".git",
    ".opencode",
    ".valinor",
    "bin",
    "coverage",
    "node_modules",
    "src",
}


def _template_rel(source_dir: Path, path: Path) -> Path:
    return path.resolve().relative_to(source_dir.resolve())


def _is_generated_template_path(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    if any(part in SCRATCH_DIR_NAMES for part in parts):
        return True
    # Agents frequently create self-tests in tests/. Keep fixtures but drop
    # generated test files so the next run starts from the benchmark contract.
    if parts[0] == "tests" and rel.name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
        return True
    return False


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _git_tracked_template_files(source_dir: Path) -> list[tuple[Path, Path]]:
    """Return (repo-relative, source path) pairs tracked under source_dir."""
    try:
        rel = source_dir.resolve().relative_to(CONFIG.repo_root.resolve())
    except ValueError:
        return []
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--", str(rel).replace("\\", "/")],
            cwd=str(CONFIG.repo_root),
            capture_output=True,
            text=False,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0 or not proc.stdout:
        return []
    files: list[tuple[Path, Path]] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            repo_rel = Path(raw.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        src = CONFIG.repo_root / repo_rel
        if src.is_file():
            files.append((repo_rel, src))
    return files


def _copy_tracked_file_from_head(repo_rel: Path, dst: Path) -> bool:
    """Copy the committed template file, ignoring dirty scratch in the worktree."""
    git_path = str(repo_rel).replace("\\", "/")
    try:
        proc = subprocess.run(
            ["git", "show", f"HEAD:{git_path}"],
            cwd=str(CONFIG.repo_root),
            capture_output=True,
            text=False,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(proc.stdout)
    return True


def _copy_template_files(source_dir: Path, work_dir: Path) -> None:
    tracked = _git_tracked_template_files(source_dir)
    if tracked:
        for repo_rel, src in tracked:
            rel = _template_rel(source_dir, src)
            if not _is_generated_template_path(rel):
                dst = work_dir / rel
                if not _copy_tracked_file_from_head(repo_rel, dst):
                    _copy_file(src, dst)
        return

    for src in source_dir.rglob("*"):
        rel = _template_rel(source_dir, src)
        if _is_generated_template_path(rel):
            continue
        if src.is_file():
            _copy_file(src, work_dir / rel)


def _init_workspace_git(work_dir: Path) -> None:
    """Create a nested repo marker so agent tools stay in the run workspace."""
    if (work_dir / ".git").exists():
        return
    try:
        proc = subprocess.run(
            ["git", "init", "-q"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if proc.returncode == 0 and (work_dir / ".git").exists():
            return
    except (OSError, subprocess.SubprocessError):
        pass
    (work_dir / ".git").mkdir(exist_ok=True)


def materialize_benchmark_workspace(source_dir: Path, bench_run_dir: Path) -> Path:
    """Create a fresh per-run benchmark workspace and return it."""
    work_dir = bench_run_dir / "workspace"
    if work_dir.exists():
        shutil.rmtree(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    _copy_template_files(source_dir, work_dir)
    _init_workspace_git(work_dir)
    return work_dir


def _link_node_modules(source_dir: Path, work_dir: Path) -> bool:
    src = source_dir / "node_modules"
    dst = work_dir / "node_modules"
    if dst.exists():
        return True
    if not src.exists():
        return False
    try:
        os.symlink(src, dst, target_is_directory=True)
        return True
    except OSError:
        pass
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(dst), str(src)],
                capture_output=True,
                text=True,
                timeout=30,
                shell=False,
            )
            return proc.returncode == 0 and dst.exists()
        except (OSError, subprocess.SubprocessError):
            return False
    return False


def ensure_benchmark_dependencies(source_dir: Path, work_dir: Path) -> None:
    """Make `npm test` usable in the isolated workspace."""
    if not (work_dir / "package.json").exists():
        return
    if _link_node_modules(source_dir, work_dir):
        return
    try:
        subprocess.run(
            ["npm", "install", "--silent"],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            timeout=300,
            shell=(sys.platform == "win32"),
        )
    except subprocess.SubprocessError:
        # The later test command will produce the actionable failure.
        return


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

MAX_AUX_HANDOFF_BYTES = 256_000
AUX_HANDOFF_FILES = ("summaries.md", "planner-status.md")

ROLE_FINAL_INSTRUCTION: dict[Role, str] = {
    "planner": (
        "Before your final message, verify these exact files exist and are non-empty: "
        ".valinor/handoff/spec.md, .valinor/handoff/acceptance.md, "
        ".valinor/handoff/backlog.md. Your final assistant message MUST be a "
        "one-paragraph summary of those three handoff artifacts and the single "
        "most important next step. Do not write implementation source, tests, "
        "or binaries; Generator owns those files."
    ),
    "generator": "Your final assistant message MUST be a one-paragraph summary of what you produced and the single most important next step.",
    "validator": "Remember: your final message's first line must be `VERDICT: PASS` or `VERDICT: FAIL`.",
}


ROLE_HARD_GUARDRAILS: dict[Role, str] = {
    "planner": (
        "FORGE HARD GATES: Planner owns only planning handoff. Do not write "
        "implementation source, tests, binaries, or package files. backlog.md is "
        "mandatory; if there is only one task, write one backlog item for that task. "
        "Restate the user/benchmark contract precisely; do not invent implementation "
        "patterns, algorithms, or constraints that the brief did not require. All "
        "paths are relative to the current workspace; never search or write outside it."
    ),
    "generator": (
        "FORGE HARD GATES: Implementation comes before self-tests. Keep any "
        "agent-written tests short and focused; do not generate exhaustive or "
        "duplicate test suites. README.md is the source of truth for the public "
        "contract; if planner handoff conflicts with README.md, follow README.md "
        "and record the planner gap in build-report.md. Do not substitute a "
        "different API shape, request schema, module path, export name, or input "
        "format than the original benchmark contract. Do not rewrite tests to "
        "match flawed code. On Windows, use PowerShell-compatible commands and "
        "prefer tool file reads over shell cat/read. build-report.md is mandatory "
        "evidence every run: "
        "if tests pass, report ON_TRACK; if tests fail or context is low, still "
        "write build-report.md with STATUS AT_RISK or BLOCKED, exact gaps, and "
        "the command/output you reached. Never implement a default branch that "
        "treats an unsupported operation as a supported one; use the spec's error "
        "semantics for recognized-but-unsupported actions. Never exit after "
        "touching code without writing build-report.md. All paths are relative to "
        "the current workspace; never search or write outside it."
    ),
    "validator": (
        "FORGE HARD GATES: validation.md is mandatory every run. If checks fail, "
        "write VERDICT: FAIL with exact failing criteria; do not exit silently. "
        "README.md is the source of truth for the public contract; compare the "
        "planner handoff, generator output, and actual code against README.md. "
        "Static review alone is never enough for PASS when package tests can run; "
        "run the available test command or fail with the exact reason it could "
        "not run. "
        "All paths are relative to the current workspace; never search or write outside it."
    ),
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
        f"{final}\n\n"
        f"{ROLE_HARD_GUARDRAILS[role]}"
    )


# Short user kick-off message per role. The role contract lives in the system
# prompt; this is just the cue to start.
ROLE_USER_KICKOFF: dict[Role, str] = {
    "planner": (
        "Begin your role. Read .valinor/brief.json for the goal, then read the README.md "
        "in the current workspace root for the benchmark spec. Do not search outside the "
        "current workspace. Produce all required planner outputs: "
        ".valinor/handoff/spec.md, .valinor/handoff/acceptance.md, and "
        ".valinor/handoff/backlog.md. Backlog is mandatory even for a single benchmark task. "
        "Do not create or edit src/, tests/, bin/, or package files."
    ),
    "generator": (
        "Begin your role. Read README.md first as the original benchmark source of truth, "
        "then read .valinor/handoff/spec.md and .valinor/handoff/acceptance.md. "
        "Implement the exact README/module/request contract in this repository; if the "
        "handoff conflicts with README.md, follow README.md and note the planner gap. "
        "Write code, write focused tests from the README contract, run them yourself with "
        "`npm test`, and only emit .valinor/handoff/build-report.md when all acceptance "
        "criteria are met and the tests pass."
    ),
    "validator": (
        "Begin your role. Read README.md first as the original benchmark source of truth, "
        "then read .valinor/handoff/spec.md, .valinor/handoff/acceptance.md, "
        ".valinor/handoff/build-report.md, and the actual source files + tests. "
        "Independently run the tests yourself with `npm test`; do not PASS on static "
        "analysis alone. Emit .valinor/handoff/validation.md and start your final "
        "assistant message with exactly `VERDICT: PASS` or `VERDICT: FAIL`."
    ),
}


# Valinor's primary artifact filename per role.
ROLE_PRIMARY_ARTIFACT: dict[Role, str] = {
    "planner": ".valinor/handoff/spec.md",
    "generator": ".valinor/handoff/build-report.md",
    "validator": ".valinor/handoff/validation.md",
}


def required_outputs_missing(role: Role, bench_dir: Path) -> list[str]:
    """Required role outputs that are absent or empty in the benchmark dir."""
    missing: list[str] = []
    for rel in HANDOFF_OUTPUTS[role]:
        path = bench_dir / rel
        if not path.exists() or path.stat().st_size == 0:
            missing.append(rel)
    return missing


def is_context_overflow(message: str | None) -> bool:
    return bool(message and "context size has been exceeded" in message.lower())


def _compact_repair_user_message(role: Role, missing: list[str]) -> str:
    missing_lines = "\n".join(f"- {rel}" for rel in missing)
    if role == "planner":
        action = (
            "Read only README.md and .valinor/brief.json in the current workspace. "
            "Write compact spec.md, acceptance.md, and backlog.md if missing. Keep each "
            "file concise and directly tied to the brief. Do not inspect generated logs, "
            ".opencode, node_modules, or any parent directory."
        )
    elif role == "generator":
        action = (
            "Read only .valinor/handoff/spec.md, .valinor/handoff/acceptance.md, "
            "package.json, and src/index.js if it exists. Do not open generated tests. "
            "Write or repair only the missing implementation evidence artifact; if tests "
            "fail, write build-report.md with STATUS AT_RISK and exact gaps."
        )
    else:
        action = (
            "Read only .valinor/handoff/spec.md, .valinor/handoff/acceptance.md, "
            ".valinor/handoff/build-report.md, and src/index.js. Write validation.md "
            "with VERDICT PASS/FAIL. Do not inspect generated test suites or logs."
        )
    return (
        "COMPACT REPAIR AFTER CONTEXT OVERFLOW. Start fresh; do not continue the prior "
        "long context. Missing or empty required outputs:\n"
        f"{missing_lines}\n\n"
        f"{action}\n"
        "Before final response, verify every listed file exists and is non-empty."
    )


def recover_misdirected_handoff_outputs(role: Role, bench_dir: Path, bench_run_dir: Path) -> list[str]:
    """Copy role outputs accidentally written beside workspace back into it."""
    recovered: list[str] = []
    stray_root = bench_run_dir / ".valinor" / "handoff"
    if not stray_root.exists():
        return recovered
    for rel in HANDOFF_OUTPUTS[role]:
        if not rel.startswith(".valinor/handoff/"):
            continue
        name = rel.split(".valinor/handoff/", 1)[1]
        src = stray_root / name
        dst = bench_dir / rel
        if src.exists() and src.is_file() and src.stat().st_size > 0 and (not dst.exists() or dst.stat().st_size == 0):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            recovered.append(rel)
    return recovered


def cap_auxiliary_handoff_files(bench_dir: Path) -> list[str]:
    """Keep non-contract handoff files from growing into context/disk hazards."""
    capped: list[str] = []
    handoff = bench_dir / ".valinor" / "handoff"
    for name in AUX_HANDOFF_FILES:
        path = handoff / name
        if not path.exists() or not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size <= MAX_AUX_HANDOFF_BYTES:
            continue
        keep_bytes = b""
        try:
            with path.open("rb") as handle:
                handle.seek(max(size - (MAX_AUX_HANDOFF_BYTES // 2), 0))
                keep_bytes = handle.read(MAX_AUX_HANDOFF_BYTES // 2)
        except OSError:
            keep_bytes = b""
        keep = keep_bytes.decode("utf-8", errors="replace")
        path.write_text(
            "# Forge truncated runaway auxiliary handoff file\n\n"
            f"Original size: {size} bytes. Keeping the latest recoverable tail only.\n\n"
            + keep,
            encoding="utf-8",
        )
        capped.append(str(path.relative_to(bench_dir)).replace("\\", "/"))
    return capped


PLANNER_SCOPE_LEAKS = ("src", "bin", "tests")


def cleanup_planner_scope_leaks(bench_dir: Path) -> list[str]:
    """Remove files the planner is not allowed to produce before generator runs."""
    removed: list[str] = []
    base = bench_dir.resolve()
    for name in PLANNER_SCOPE_LEAKS:
        target = bench_dir / name
        if not target.exists():
            continue
        try:
            target.resolve().relative_to(base)
        except ValueError:
            continue
        if name == "tests" and target.is_dir():
            removed_any = False
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_file() and child.name.endswith((".test.js", ".test.ts", ".spec.js", ".spec.ts")):
                    child.unlink()
                    removed_any = True
            for child in sorted(target.rglob("*"), reverse=True):
                if child.is_dir():
                    try:
                        child.rmdir()
                    except OSError:
                        pass
            try:
                target.rmdir()
            except OSError:
                pass
            if removed_any:
                removed.append(name)
            continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(name)
    return removed


def _repair_user_message(role: Role, missing: list[str]) -> str:
    missing_lines = "\n".join(f"- {rel}" for rel in missing)
    role_guard = {
        "planner": (
            "Planner repair scope: write only missing planner handoff files. "
            "Do not create or edit implementation source, tests, binaries, or package files."
        ),
        "generator": (
            "Generator repair scope: write the missing build report. If src/index.js "
            "already exists, do not open generated self-test files and do not edit tests. "
            "Run npm test once if practical, then write .valinor/handoff/build-report.md. "
            "If tests fail or context is low, still write the report with STATUS AT_RISK "
            "or BLOCKED and include the exact gaps."
        ),
        "validator": (
            "Validator repair scope: independently inspect code/tests, run validation, and "
            "write the missing validation report."
        ),
    }[role]
    return (
        "REPAIR ONLY. Your previous run ended, but the harness found required output "
        "files missing or empty:\n"
        f"{missing_lines}\n\n"
        f"{role_guard}\n"
        "Read the existing handoff files, then write only the missing required artifact(s). "
        "Before your final message, verify every listed file exists and is non-empty."
    )


def detect_sandbox_drift(log_path: Path, work_dir: Path) -> str | None:
    """Detect agent tool use outside the per-run benchmark workspace."""
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    work = str(work_dir.resolve()).lower().replace("/", "\\")
    forbidden = [
        CONFIG.repo_root / ".valinor",
        CONFIG.benchmarks_dir,
    ]
    tokens = [(path, str(path.resolve()).lower().replace("/", "\\")) for path in forbidden]
    for raw_line in text.splitlines():
        line = raw_line.lower().replace("/", "\\")
        if "node_modules" in line:
            continue
        for path, token in tokens:
            if token in line and not token.startswith(work):
                return f"agent accessed outside benchmark workspace: {path}"
    return None


def run_role(
    role: Role,
    prompts: ChampionPrompts,
    bench_dir: Path,
    exp_run_dir: Path,
    on_progress: Callable[[int], None] | None = None,
    attempt_label: str | None = None,
    user_message: str | None = None,
) -> RoleResult:
    """Run one role against the benchmark via the authed CLI. Returns outputs."""
    system_prompt = _build_system_prompt(role, prompts)
    user_message = user_message or ROLE_USER_KICKOFF[role]
    suffix = f".{attempt_label}" if attempt_label else ""
    log_path = exp_run_dir / f"stdout.{role}{suffix}.log"
    sp_file = exp_run_dir / f"system.{role}{suffix}.txt"

    cap_auxiliary_handoff_files(bench_dir)
    run = run_agent(
        system_prompt=system_prompt,
        user_message=user_message,
        work_dir=bench_dir,
        sys_prompt_file=sp_file,
        log_path=log_path,
        label=role,
        on_progress=on_progress,
    )
    cap_auxiliary_handoff_files(bench_dir)

    artifact_path = bench_dir / ROLE_PRIMARY_ARTIFACT[role]
    artifact = artifact_path.read_text(encoding="utf-8") if artifact_path.exists() else None

    drift = detect_sandbox_drift(log_path, bench_dir)

    return RoleResult(
        role=role,
        session_id=run.session_id,
        exit_code=run.exit_code,
        wall_seconds=run.wall_seconds,
        final_message=run.final_text,
        artifact=artifact,
        cost_usd=run.cost_usd,
        num_turns=run.num_turns,
        tokens_out=run.tokens_out,
        error=run.error or drift,
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


def _inject_golden_tests(bench_dir: Path, benchmark_name: str | None = None) -> list[Path]:
    """If a held-out golden suite exists at golden/<benchmark>/, copy it into the
    benchmark's tests/ dir (marked, so the runner targets only it) so scoring
    measures true correctness against edge cases the agent never saw. Returns
    the copied files.

    The golden suite lives OUTSIDE the agent's working dir and is copied in only
    here, after the agents have finished — so they cannot read or overfit to it.
    Kept in tests/ (not a subdir) so its `../src/index.js` import still resolves.
    The next experiment's reset_benchmark() wipes the benchmark scratch."""
    golden_dir = CONFIG.repo_root / "golden" / (benchmark_name or bench_dir.name)
    golden_files = sorted(golden_dir.glob("*.test.js")) if golden_dir.exists() else []
    if not golden_files:
        return []
    tests_dir = bench_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    for f in tests_dir.glob(f"{GOLDEN_MARKER}*"):
        f.unlink()
    copied: list[Path] = []
    for gf in golden_files:
        dst = tests_dir / f"{GOLDEN_MARKER}{gf.name}"
        shutil.copy2(gf, dst)
        copied.append(dst)
    return copied


def run_tests(bench_dir: Path, benchmark_name: str | None = None) -> TestResult:
    """Run vitest in the benchmark dir and parse the JSON summary.

    If a held-out golden suite exists, it is injected and the runner is FILTERED
    to only the golden files — so the score reflects true correctness against
    our edge cases, never the generator's (gameable) self-tests, wherever it put
    them."""
    held_out = _inject_golden_tests(bench_dir, benchmark_name)
    if held_out:
        cmd = ["npx", "vitest", "run", *[str(p.relative_to(bench_dir)) for p in held_out], "--reporter=json"]
    else:
        cmd = ["npm", "test", "--silent", "--", "--reporter=json"]
    try:
        proc = subprocess.run(
            cmd,
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

    # Capture WHICH held-out tests failed — the exact behaviours the generated
    # code got wrong. (vitest's json reporter is Jest-shaped: testResults[] ->
    # assertionResults[] with fullName/title/status.) This is the proposer's
    # most actionable signal.
    failed_names: list[str] = []
    for file_res in (data.get("testResults") or []):
        for a in (file_res.get("assertionResults") or []):
            if a.get("status") == "failed":
                name = a.get("fullName") or a.get("title") or "(unnamed test)"
                failed_names.append(name.strip())

    return TestResult(
        passed=passed, failed=failed, total=total,
        raw_stdout=body, raw_stderr=proc.stderr,
        failed_names=failed_names[:30],
    )


def _snapshot_final_state(bench_dir: Path, bench_run_dir: Path) -> None:
    """Capture generated state before golden tests are injected."""
    cap_auxiliary_handoff_files(bench_dir)
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


def _blocked_result(
    benchmark: str,
    bench_dir: Path,
    bench_run_dir: Path,
    roles: list[RoleResult],
    cycles: int,
    reason: str,
) -> BenchmarkResult:
    _snapshot_final_state(bench_dir, bench_run_dir)
    return BenchmarkResult(
        benchmark=benchmark,
        cycles=cycles,
        verdict="unknown",
        test=TestResult(0, 0, 0, raw_stdout="", raw_stderr=reason, failed_names=[reason]),
        roles=roles,
    )


def _failed_result_with_tests(
    benchmark: str,
    bench_dir: Path,
    bench_run_dir: Path,
    roles: list[RoleResult],
    cycles: int,
    reason: str,
) -> BenchmarkResult:
    _snapshot_final_state(bench_dir, bench_run_dir)
    test = run_tests(bench_dir, benchmark)
    if test.total == 0:
        test.raw_stderr = reason
        test.failed_names = [reason]
    return BenchmarkResult(
        benchmark=benchmark,
        cycles=cycles,
        verdict="fail",
        test=test,
        roles=roles,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public: one experiment
# ─────────────────────────────────────────────────────────────────────────────


def _run_package_tests_raw(bench_dir: Path, timeout_s: int = 120) -> tuple[int | None, str, str]:
    """Run the benchmark's own visible test script without injecting golden tests."""
    if not (bench_dir / "package.json").exists():
        return 0, "", "No package.json; package test was not applicable."
    try:
        proc = subprocess.run(
            ["npm", "test"],
            cwd=str(bench_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=(sys.platform == "win32"),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return None, out, (err + f"\nPackage test timeout after {timeout_s}s").strip()
    except subprocess.SubprocessError as exc:
        return None, "", f"Package test failed to start: {exc}"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def recover_missing_generator_report(bench_dir: Path) -> RoleResult | None:
    """Write explicit Generator failure/evidence when build-report.md is omitted."""
    report_path = bench_dir / ".valinor" / "handoff" / "build-report.md"
    if report_path.exists() and report_path.stat().st_size > 0:
        return None
    src_dir = bench_dir / "src"
    if not src_dir.exists():
        return None

    started = time.time()
    code_files = sorted(str(p.relative_to(bench_dir)).replace("\\", "/") for p in src_dir.rglob("*") if p.is_file())
    rc, stdout, stderr = _run_package_tests_raw(bench_dir, timeout_s=120)
    status = "ON_TRACK" if rc == 0 else "AT_RISK"
    headline = (
        "Implementation evidence was recovered and visible tests pass."
        if rc == 0 else
        "Implementation evidence was recovered, but visible tests still need attention."
    )
    output = (stdout + ("\n" + stderr if stderr else "")).strip()
    if len(output) > 6000:
        output = output[:6000] + "\n... [truncated by forge recovery]"
    report = (
        "<!-- EXEC-SUMMARY\n"
        "OBJECTIVE: Complete the benchmark implementation handoff\n"
        f"STATUS: {status}\n"
        f"HEADLINE: {headline}\n"
        "IMPLEMENTED: Generator wrote implementation files but exited without its required evidence report.\n"
        "VALIDATED: Forge recovery ran the visible package test command and preserved the result for Validator.\n"
        "TRADEOFFS: This report is a recovery artifact, not a Validator verdict.\n"
        "OBS: Generator must still be judged by Validator and held-out scoring.\n"
        "BLOCKERS: none\n"
        "EXEC-SUMMARY -->\n\n"
        "# Build Report - Forge Recovery\n\n"
        "Generator-owned build-report.md was missing after Generator and one repair attempt. "
        "Forge wrote this recovery report so the handoff can continue with explicit evidence "
        "instead of silently blocking.\n\n"
        "## Implementation Files Detected\n\n"
        + "\n".join(f"- `{name}`" for name in code_files)
        + "\n\n## Visible Package Test\n\n"
        "- Command: `npm test`\n"
        f"- Exit code: `{rc if rc is not None else 'timeout/error'}`\n\n"
        "```text\n"
        f"{output or '(no output)'}\n"
        "```\n"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return RoleResult(
        role="generator",
        session_id=None,
        exit_code=0 if rc == 0 else 1,
        wall_seconds=time.time() - started,
        final_message="Forge recovery wrote missing generator build-report.md from existing implementation evidence.",
        artifact=report,
    )


def recover_missing_validator_report(bench_dir: Path, reason: str | None = None) -> RoleResult | None:
    """Write explicit Validator failure/evidence when validation.md is omitted."""
    report_path = bench_dir / ".valinor" / "handoff" / "validation.md"
    if report_path.exists() and report_path.stat().st_size > 0:
        return None
    if not (bench_dir / ".valinor" / "handoff" / "build-report.md").exists():
        return None

    started = time.time()
    rc, stdout, stderr = _run_package_tests_raw(bench_dir, timeout_s=120)
    output = (stdout + ("\n" + stderr if stderr else "")).strip()
    if len(output) > 6000:
        output = output[:6000] + "\n... [truncated by forge recovery]"
    verdict = "PASS" if rc == 0 and not reason else "FAIL"
    report = (
        f"VERDICT: {verdict}\n\n"
        "<!-- EXEC-SUMMARY\n"
        "OBJECTIVE: Validate the implementation handoff\n"
        f"STATUS: {'ON_TRACK' if verdict == 'PASS' else 'AT_RISK'}\n"
        f"HEADLINE: Validator evidence was recovered after validator omitted validation.md.\n"
        "IMPLEMENTED: No implementation changes were made by Validator recovery.\n"
        "VALIDATED: Forge recovery ran the visible package test command and recorded the outcome.\n"
        "TRADEOFFS: This report is a recovery artifact, not an independent human-quality review.\n"
        f"OBS: Original validator issue: {reason or 'missing validation.md'}.\n"
        "BLOCKERS: none\n"
        "EXEC-SUMMARY -->\n\n"
        "# Validation Report - Forge Recovery\n\n"
        "Validator-owned validation.md was missing after Validator. Forge wrote this recovery "
        "report so the run has explicit final evidence instead of a silent missing artifact.\n\n"
        "## Visible Package Test\n\n"
        "- Command: `npm test`\n"
        f"- Exit code: `{rc if rc is not None else 'timeout/error'}`\n\n"
        "```text\n"
        f"{output or '(no output)'}\n"
        "```\n"
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return RoleResult(
        role="validator",
        session_id=None,
        exit_code=0 if verdict == "PASS" else 1,
        wall_seconds=time.time() - started,
        final_message=f"VERDICT: {verdict}\nForge recovery wrote missing validator validation.md.",
        artifact=report,
    )


def run_one_benchmark(
    benchmark: str,
    prompts: ChampionPrompts,
    exp_dir: Path,
    on_role: "callable | None" = None,
) -> BenchmarkResult:
    source_dir = CONFIG.benchmarks_dir / benchmark
    if not source_dir.exists():
        raise FileNotFoundError(f"benchmark not found: {source_dir}")
    bench_run_dir = exp_dir / benchmark
    bench_run_dir.mkdir(parents=True, exist_ok=True)

    bench_dir = materialize_benchmark_workspace(source_dir, bench_run_dir)
    ensure_benchmark_dependencies(source_dir, bench_dir)

    readme_path = bench_dir / "README.md"
    readme = readme_path.read_text(encoding="utf-8") if readme_path.exists() else ""

    setup_valinor_dir(bench_dir, readme)
    cap_auxiliary_handoff_files(bench_dir)

    roles: list[RoleResult] = []
    verdict = "unknown"
    cycles = 0

    def _emit(role: str, phase: str, result: "RoleResult | None" = None) -> None:
        if on_role:
            on_role(benchmark, role, phase, result)

    def _repair_missing_outputs(role: Role, missing: list[str]) -> RoleResult:
        _emit(role, "repair_start")
        repair_res = run_role(
            role, prompts, bench_dir, bench_run_dir,
            on_progress=lambda tokens: _emit(role, "progress", tokens),
            attempt_label="repair1",
            user_message=_repair_user_message(role, missing),
        )
        roles.append(repair_res)
        _emit(role, "repair_done", repair_res)
        return repair_res

    def _compact_repair_outputs(role: Role, missing: list[str]) -> RoleResult:
        _emit(role, "compact_repair_start")
        repair_res = run_role(
            role, prompts, bench_dir, bench_run_dir,
            on_progress=lambda tokens: _emit(role, "progress", tokens),
            attempt_label="compact1",
            user_message=_compact_repair_user_message(role, missing),
        )
        roles.append(repair_res)
        _emit(role, "compact_repair_done", repair_res)
        return repair_res

    # planner once, then generator+validator up to max_rework_rounds times.
    _emit("planner", "start")
    planner_res = run_role(
        "planner", prompts, bench_dir, bench_run_dir,
        on_progress=lambda tokens: _emit("planner", "progress", tokens),
    )
    roles.append(planner_res)
    _emit("planner", "done", planner_res)
    recover_misdirected_handoff_outputs("planner", bench_dir, bench_run_dir)
    cap_auxiliary_handoff_files(bench_dir)
    missing = required_outputs_missing("planner", bench_dir)
    if planner_res.error and is_context_overflow(planner_res.error):
        if missing:
            compact_res = _compact_repair_outputs("planner", missing)
            recover_misdirected_handoff_outputs("planner", bench_dir, bench_run_dir)
            missing = required_outputs_missing("planner", bench_dir)
            if not missing and not compact_res.error:
                planner_res.error = None
        else:
            planner_res.error = None
    if not planner_res.error and missing:
        repair_res = _repair_missing_outputs("planner", missing)
        recover_misdirected_handoff_outputs("planner", bench_dir, bench_run_dir)
        missing = required_outputs_missing("planner", bench_dir)
        if repair_res.error:
            return _blocked_result(benchmark, bench_dir, bench_run_dir, roles, cycles, repair_res.error)
    cleanup_planner_scope_leaks(bench_dir)
    if planner_res.error or missing:
        reason = planner_res.error or f"planner missing required output(s): {', '.join(missing)}"
        return _blocked_result(benchmark, bench_dir, bench_run_dir, roles, cycles, reason)

    for _round_idx in range(1, CONFIG.max_rework_rounds + 1):
        cycles += 1
        _emit("generator", "start")
        gen_res = run_role(
            "generator", prompts, bench_dir, bench_run_dir,
            on_progress=lambda tokens: _emit("generator", "progress", tokens),
        )
        roles.append(gen_res)
        _emit("generator", "done", gen_res)
        recover_misdirected_handoff_outputs("generator", bench_dir, bench_run_dir)
        cap_auxiliary_handoff_files(bench_dir)
        missing = required_outputs_missing("generator", bench_dir)
        generator_had_error = bool(gen_res.error)
        if gen_res.error and is_context_overflow(gen_res.error):
            if missing:
                compact_res = _compact_repair_outputs("generator", missing)
                recover_misdirected_handoff_outputs("generator", bench_dir, bench_run_dir)
                missing = required_outputs_missing("generator", bench_dir)
                if not missing and not compact_res.error:
                    gen_res.error = None
                    generator_had_error = False
            else:
                gen_res.error = None
                generator_had_error = False
        if not gen_res.error and missing:
            repair_res = _repair_missing_outputs("generator", missing)
            recover_misdirected_handoff_outputs("generator", bench_dir, bench_run_dir)
            missing = required_outputs_missing("generator", bench_dir)
            if repair_res.error:
                generator_had_error = True
        if missing:
            recovery_res = recover_missing_generator_report(bench_dir)
            if recovery_res is not None:
                roles.append(recovery_res)
                _emit("generator", "recovered", recovery_res)
                if recovery_res.exit_code != 0:
                    return _failed_result_with_tests(
                        benchmark,
                        bench_dir,
                        bench_run_dir,
                        roles,
                        cycles,
                        recovery_res.error or recovery_res.final_message or "generator recovery failed",
                    )
                missing = required_outputs_missing("generator", bench_dir)
        if generator_had_error and not (bench_dir / ".valinor" / "handoff" / "build-report.md").exists():
            reason = gen_res.error or "generator failed before writing build-report.md"
            return _blocked_result(benchmark, bench_dir, bench_run_dir, roles, cycles, reason)
        if missing:
            reason = gen_res.error or f"generator missing required output(s): {', '.join(missing)}"
            return _blocked_result(benchmark, bench_dir, bench_run_dir, roles, cycles, reason)

        _emit("validator", "start")
        val_res = run_role(
            "validator", prompts, bench_dir, bench_run_dir,
            on_progress=lambda tokens: _emit("validator", "progress", tokens),
        )
        roles.append(val_res)
        verdict = parse_verdict(val_res.final_message)
        _emit("validator", "done", val_res)
        recover_misdirected_handoff_outputs("validator", bench_dir, bench_run_dir)
        cap_auxiliary_handoff_files(bench_dir)
        missing = required_outputs_missing("validator", bench_dir)
        if val_res.error and is_context_overflow(val_res.error):
            if missing:
                compact_res = _compact_repair_outputs("validator", missing)
                recover_misdirected_handoff_outputs("validator", bench_dir, bench_run_dir)
                verdict = parse_verdict(compact_res.final_message)
                missing = required_outputs_missing("validator", bench_dir)
                if not missing and not compact_res.error:
                    val_res.error = None
            else:
                val_res.error = None
        if not val_res.error and missing:
            repair_res = _repair_missing_outputs("validator", missing)
            verdict = parse_verdict(repair_res.final_message)
            recover_misdirected_handoff_outputs("validator", bench_dir, bench_run_dir)
            missing = required_outputs_missing("validator", bench_dir)
            if repair_res.error:
                verdict = "unknown"
        if val_res.error or missing:
            recovery_res = recover_missing_validator_report(bench_dir, val_res.error)
            if recovery_res is not None:
                roles.append(recovery_res)
                _emit("validator", "recovered", recovery_res)
                verdict = parse_verdict(recovery_res.final_message)
                missing = required_outputs_missing("validator", bench_dir)
        if missing:
            verdict = "unknown"
            break
        if val_res.error and verdict == "unknown":
            break
        if verdict == "pass":
            break

    # Snapshot the benchmark's final state FIRST — this captures the agent's own
    # code + tests before run_tests() swaps in the held-out golden suite.
    _snapshot_final_state(bench_dir, bench_run_dir)

    # Score on the held-out golden tests (true correctness, not self-consistency).
    test = run_tests(bench_dir, benchmark)
    if test.total > 0 and test.failed > 0:
        verdict = "fail"

    return BenchmarkResult(
        benchmark=benchmark,
        cycles=cycles,
        verdict=verdict,
        test=test,
        roles=roles,
    )


def run_experiment(prompts: ChampionPrompts, exp_id: str, on_role: "callable | None" = None,
                   benchmarks: "tuple | None" = None) -> ExperimentResult:
    """Run the given benchmarks (default: all configured), return aggregate
    result. `on_role(bench, role, phase, result)` is an optional progress
    callback. `benchmarks` lets rotate-mode run a single benchmark."""
    bench_list = tuple(benchmarks) if benchmarks is not None else CONFIG.benchmarks
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
    for bench in bench_list:
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
