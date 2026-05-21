"""
Forge configuration.

Operator-tunable knobs live here. Edit values directly. The proposer is
forbidden from editing this file (`program.md` enforces that boundary).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Role = Literal["planner", "generator", "validator"]
ROLES: tuple[Role, ...] = ("planner", "generator", "validator")


REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ScoreWeights:
    """Multi-metric weighted score: w_tests*test_pass + w_cycles*(1/cycles) + w_rubric*rubric."""

    tests: float = 0.5
    cycles: float = 0.2
    rubric: float = 0.3

    def __post_init__(self) -> None:
        total = self.tests + self.cycles + self.rubric
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"ScoreWeights must sum to 1.0, got {total}")


@dataclass(frozen=True)
class ForgeConfig:
    # ───── paths
    repo_root: Path = REPO_ROOT
    prompts_seed_dir: Path = REPO_ROOT / "prompts" / "seed"
    prompts_champion_dir: Path = REPO_ROOT / "prompts" / "champion"
    benchmarks_dir: Path = REPO_ROOT / "benchmarks"
    state_dir: Path = REPO_ROOT / "state"
    runs_dir: Path = REPO_ROOT / "state" / "runs"
    experiments_jsonl: Path = REPO_ROOT / "state" / "experiments.jsonl"
    live_json: Path = REPO_ROOT / "state" / "live.json"

    # ───── models
    # The local agent that drives planner/generator/validator. opencode receives
    # this as the -m argument (provider/model form). Must be loaded in LM Studio.
    experiment_model: str = "lmstudio/openai/gpt-oss-20b"

    # Cloud model for the proposer (mutation generator) and judge (rubric scorer).
    anthropic_model: str = "claude-opus-4-7"
    anthropic_thinking_budget: int = 10000  # "xhigh" extended thinking

    # ───── experiment shape
    benchmarks: tuple[str, ...] = ("git-log-summary",)
    # Once react-form-validation + csv-to-json benchmarks exist, expand:
    # benchmarks = ("git-log-summary", "react-form-validation", "csv-to-json")

    # Per-role timeout for the opencode subprocess (wall clock seconds). Failsafe
    # against an agent that gets stuck and never exits cleanly.
    role_timeout_s: int = 30 * 60  # 30 min per role

    # Max validator→generator rework rounds per benchmark. 1 means: planner runs
    # once, generator runs once, validator runs once. If validator FAILs, the
    # cycle is over (counts toward score via cycles). Raise to allow retries.
    max_rework_rounds: int = 1

    # ───── scoring
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    # ───── stop condition (forge run)
    stop_score_threshold: float = 0.95
    stop_plateau_experiments: int = 20  # no improvement in this many → stop

    # ───── dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 7777


CONFIG = ForgeConfig()
