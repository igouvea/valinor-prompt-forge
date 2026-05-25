"""
Forge configuration.

Operator-tunable knobs live here. Edit values directly. The proposer is
forbidden from editing this file (`program.md` enforces that boundary).

Auth model: everything runs through an already-authenticated agentic CLI
(`claude` by default, OAuth/subscription — NO ANTHROPIC_API_KEY needed; or
`codex` for GPT models). The inner-loop agents and the outer-loop researcher
(proposer + judge) are all CLI subprocesses. See forge/agent_cli.py.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


Role = Literal["planner", "generator", "validator"]
ROLES: tuple[Role, ...] = ("planner", "generator", "validator")

# What drives the inner-loop agents. Toggle one per run.
#   "lmstudio" → a LOCAL model via opencode + LM Studio (default). Zero cloud
#               tokens for the agents, and the prompts get tuned for what a
#               small model can do — which is the deployment target.
#   "claude"   → Anthropic models via OAuth (strong, cloud).
#   "codex"    → OpenAI/GPT via the ChatGPT subscription.
# The researcher (proposer + judge) is ALWAYS Opus regardless of this.
AgentCli = Literal["lmstudio", "claude", "codex"]


REPO_ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class ScoreWeights:
    """Multi-metric weighted score: w_tests*test_pass + w_speed*time_efficiency + w_rubric*rubric.

    The efficiency channel is TIME-based (wall-clock seconds the agents spent),
    not iteration count — what matters is how fast the agents reach a correct
    result, not how many rework rounds they nominally took (which is ~always 1
    on single-pass tasks). A prompt that gets the agent there with less wandering
    is faster on both cloud and local models.

    NOTE: at the Top/Opus tier a strong model produces decent output even from a
    mediocre prompt, compressing the test-pass channel. If the loop stalls for
    lack of gradient, lean toward the rubric (e.g. tests=0.4, speed=0.1,
    rubric=0.5). These weights must sum to 1.0.
    """

    tests: float = 0.5
    speed: float = 0.2
    rubric: float = 0.3

    def __post_init__(self) -> None:
        total = self.tests + self.speed + self.rubric
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
    progress_md: Path = REPO_ROOT / "progress.md"

    # Valinor checkout (sibling dir) — promote target. The three BASE_ROLE
    # prompts live inside this TS file.
    valinor_repo: Path = REPO_ROOT.parent / "valinor"
    valinor_harness_file: Path = REPO_ROOT.parent / "valinor" / "src" / "runtime" / "agents" / "codexHarness.ts"

    # ───── inner-loop agent driver
    # Default: local gpt-oss-20b via LM Studio — we iterate on the small model
    # we deploy to, with no agent token cost.
    agent_cli: AgentCli = field(default_factory=lambda: _env("FORGE_AGENT_CLI", "lmstudio"))  # type: ignore[assignment]
    # Local model id as opencode addresses it (provider/model). qwen3.5-9b fits
    # a 32K context comfortably in 16GB (gpt-oss-20b spills to shared mem at 32K
    # → ~3x slower). Loaded automatically with a large context (LM Studio resets
    # to 4096 on reload). Override via env.
    agent_model_lmstudio: str = field(default_factory=lambda: _env("FORGE_LMSTUDIO_MODEL", "lmstudio/qwen/qwen3.5-9b"))
    # Context window to load the local model with (LM Studio resets to 4096 on
    # reload; our prompts are ~5-9K tokens so it needs to be generous).
    lmstudio_context_length: int = field(default_factory=lambda: int(_env("FORGE_LMSTUDIO_CTX", "32768")))
    # Specific quant variant to load (LM Studio "model@quant" form). Q4_K_M is
    # ~5.6GB and ~2x faster than the Q8 default while leaving 16GB headroom at
    # 32K. It's loaded under the base identifier so opencode addresses it
    # unchanged. Empty → load LM Studio's default variant.
    lmstudio_load_variant: str = field(default_factory=lambda: _env("FORGE_LMSTUDIO_VARIANT", "qwen/qwen3.5-9b@q4_k_m"))
    agent_model_claude: str = field(default_factory=lambda: _env("FORGE_CLAUDE_MODEL", "opus"))
    # codex's top coding model. VERIFY on first codex run; override via env.
    agent_model_codex: str = field(default_factory=lambda: _env("FORGE_CODEX_MODEL", "gpt-5.1-codex"))
    # Tools the inner-loop agents may use (claude --allowedTools). Coding only —
    # MCP servers are stripped entirely (see agent_cli.py).
    agent_allowed_tools: tuple[str, ...] = ("Bash", "Edit", "Write", "Read", "Glob", "Grep")
    # USD-equivalent failsafe per role-run, in case an agent runs away. On a
    # subscription this just caps the equivalent spend; the wall-clock timeout
    # is the real backstop.
    agent_max_budget_usd: float = 4.0

    # ───── outer-loop researcher (proposer + judge)
    # Always the claude CLI at the strongest reasoning model. This is the
    # "scientist" — mutating prompts and grading artifacts.
    researcher_model: str = field(default_factory=lambda: _env("FORGE_RESEARCHER_MODEL", "opus"))

    # ───── experiment shape
    # The hard slate: held-out golden tests (golden/<name>/) measure true
    # correctness, so even a strong model lands well below 1.0 — leaving the
    # optimizer real headroom. git-log-summary is retired (self-tests saturate
    # it to 1.0); keep it available via FORGE_BENCHMARKS override if needed.
    benchmarks: tuple[str, ...] = field(default_factory=lambda: tuple(
        os.environ.get("FORGE_BENCHMARKS", "json-patch,expr-eval,task-api").split(",")
    ))

    # How the loop sweeps benchmarks:
    #   "rotate" — each experiment runs ONE benchmark (round-robin), proposes a
    #             mutation from its failures, and ratchets on THAT benchmark's
    #             score. ~3x more iterations + faster, but can chase a single
    #             benchmark's optimum (local maxima) — guarded by global
    #             checkpoints below.
    #   "sweep"  — each experiment runs all benchmarks and ratchets on the mean
    #             (globally balanced, slower).
    benchmark_mode: str = field(default_factory=lambda: _env("FORGE_BENCHMARK_MODE", "rotate"))
    # In rotate mode, every N experiments re-run the champion on ALL benchmarks
    # to measure the true aggregate and detect local-maxima drift.
    global_checkpoint_every: int = field(default_factory=lambda: int(_env("FORGE_GLOBAL_CHECKPOINT_EVERY", "6")))

    # Per-role wall-clock timeout for the agent subprocess (seconds). Failsafe
    # against an agent that gets stuck and never exits.
    role_timeout_s: int = 30 * 60  # 30 min per role

    # Max validator→generator rework rounds per benchmark. 1 means: planner once,
    # generator once, validator once. If validator FAILs, the cycle is over.
    max_rework_rounds: int = 1

    # ───── scoring
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    # Time reference for the speed channel (per-benchmark agent wall seconds).
    # time_efficiency = ref / (ref + mean_benchmark_seconds): equals 0.5 when a
    # benchmark takes `ref` seconds, → 1.0 as it gets faster, → 0 as it slows.
    # Calibrate to the observed baseline so the champion sits near 0.5 and has
    # headroom in BOTH directions. Override via FORGE_TIME_REF_SECONDS.
    # Local pace: gpt-oss-20b/qwen took ~250-300s/benchmark in smokes, so the
    # speed channel sits near 0.5 with headroom. Re-tune via FORGE_TIME_REF_SECONDS
    # once a clean local baseline lands. (Was 540 for the Opus agent baseline.)
    time_ref_seconds: float = field(default_factory=lambda: float(_env("FORGE_TIME_REF_SECONDS", "300")))

    # ───── stop condition (forge run)
    stop_score_threshold: float = 0.95
    stop_plateau_experiments: int = 20  # no improvement in this many → stop

    # ───── dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 7777

    def agent_model(self) -> str:
        """Model id for the currently-toggled inner-loop driver."""
        if self.agent_cli == "lmstudio":
            return self.agent_model_lmstudio
        if self.agent_cli == "codex":
            return self.agent_model_codex
        return self.agent_model_claude


CONFIG = ForgeConfig()
