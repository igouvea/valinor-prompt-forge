"""
forge.scorer

Multi-metric weighted score for an ExperimentResult.

Formula (weights live in CONFIG.weights, edit program.md to change the
research-org policy, edit config.py to change the math itself):

    score = w_tests  * aggregate_pass_rate
          + w_speed  * time_efficiency
          + w_rubric * rubric_score

where:
    aggregate_pass_rate  ∈ [0,1] — fraction of held-out golden tests passing
                                   (averaged across benchmarks)
    time_efficiency      ∈ (0,1] — ref / (ref + mean_benchmark_wall_seconds);
                                   rewards agents that reach a correct result
                                   FASTER (less wandering), not fewer nominal
                                   rework cycles
    rubric_score         ∈ [0,1] — Opus judge's normalized rubric average

A perfect, instant run approaches 1.0. The baseline score is whatever the
current Valinor prompts achieve against the benchmarks on the chosen model.

This module is pure functions. No I/O, no LLM calls. judge.py supplies the
rubric_score input; experiment.py supplies the timing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CONFIG
from .experiment import ExperimentResult
from .quality import experiment_has_positive_tests


@dataclass(frozen=True)
class Score:
    """The full breakdown so the proposer and dashboard can read each metric."""

    total: float
    tests: float           # aggregate test pass rate
    speed: float           # time efficiency (ref/(ref+seconds))
    rubric: float          # judge's rubric score
    raw_wall_seconds: float  # mean per-benchmark agent wall seconds (display)
    raw_total_cycles: int    # nominal rework cycles (display only, not scored)


def time_efficiency(mean_benchmark_seconds: float, ref_seconds: float | None = None) -> float:
    """Smooth speed score in (0,1]. 0.5 when a benchmark takes `ref` seconds,
    → 1.0 as it gets faster, → 0 as it slows. Monotonic, so there is always
    pressure toward faster runs (unlike a hard cap)."""
    ref = ref_seconds if ref_seconds is not None else CONFIG.time_ref_seconds
    ref = max(1e-6, ref)
    seconds = max(0.0, mean_benchmark_seconds)
    return ref / (ref + seconds)


def compute_score(experiment: ExperimentResult, rubric_score: float) -> Score:
    """Combine the three signals into one scalar.

    `rubric_score` must already be normalized to [0, 1]. judge.py is
    responsible for that normalization.
    """
    test_rate = experiment.aggregate_pass_rate
    mean_seconds = experiment.mean_benchmark_wall_seconds
    if not experiment_has_positive_tests(experiment):
        return Score(
            total=0.0,
            tests=0.0,
            speed=0.0,
            rubric=0.0,
            raw_wall_seconds=mean_seconds,
            raw_total_cycles=experiment.total_cycles,
        )
    rubric_score = max(0.0, min(1.0, rubric_score))
    spd = time_efficiency(mean_seconds)
    w = CONFIG.weights
    total = w.tests * test_rate + w.speed * spd + w.rubric * rubric_score
    return Score(
        total=total,
        tests=test_rate,
        speed=spd,
        rubric=rubric_score,
        raw_wall_seconds=mean_seconds,
        raw_total_cycles=experiment.total_cycles,
    )


def beats(candidate: Score, champion: Score | None) -> bool:
    """Strict-improvement ratchet (program.md choice). The candidate must
    score STRICTLY higher than the champion to be adopted."""
    if champion is None:
        return True
    return candidate.total > champion.total
