"""
forge.scorer

Multi-metric weighted score for an ExperimentResult.

Formula (weights live in CONFIG.weights, edit program.md to change the
research-org policy, edit config.py to change the math itself):

    score = w_tests  * aggregate_pass_rate
          + w_cycles * cycle_efficiency
          + w_rubric * rubric_score

where:
    aggregate_pass_rate  ∈ [0,1] — fraction of benchmark tests passing
                                   (averaged across benchmarks)
    cycle_efficiency     ∈ [0,1] — 1 / max(1, total_cycles)
                                   (1.0 means single-pass clear on every bench)
    rubric_score         ∈ [0,1] — Opus judge's normalized rubric average

A perfect run is 1.0. The seed/baseline score is whatever the current Valinor
prompts achieve against the benchmarks on the local model.

This module is pure functions. No I/O, no LLM calls. judge.py supplies the
rubric_score input.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CONFIG
from .experiment import ExperimentResult


@dataclass(frozen=True)
class Score:
    """The full breakdown so the proposer and dashboard can read each metric."""

    total: float
    tests: float           # aggregate test pass rate
    cycles: float          # cycle efficiency (1/cycles)
    rubric: float          # judge's rubric score
    raw_total_cycles: int  # for display


def cycle_efficiency(total_cycles: int) -> float:
    """1.0 for single-pass-success-everywhere, decreasing as rework piles on."""
    return 1.0 / max(1, total_cycles)


def compute_score(experiment: ExperimentResult, rubric_score: float) -> Score:
    """Combine the three signals into one scalar.

    `rubric_score` must already be normalized to [0, 1]. judge.py is
    responsible for that normalization.
    """
    rubric_score = max(0.0, min(1.0, rubric_score))
    test_rate = experiment.aggregate_pass_rate
    cyc = cycle_efficiency(experiment.total_cycles)
    w = CONFIG.weights
    total = w.tests * test_rate + w.cycles * cyc + w.rubric * rubric_score
    return Score(
        total=total,
        tests=test_rate,
        cycles=cyc,
        rubric=rubric_score,
        raw_total_cycles=experiment.total_cycles,
    )


def beats(candidate: Score, champion: Score | None) -> bool:
    """Strict-improvement ratchet (program.md choice). The candidate must
    score STRICTLY higher than the champion to be adopted."""
    if champion is None:
        return True
    return candidate.total > champion.total
