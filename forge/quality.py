"""Quality gates for deciding whether an experiment is optimizer signal.

The forge should learn from implementations that exercised the held-out tests.
Runs with a zero test grade are usually no-output / wrong-workspace failures;
they are preserved on disk for debugging, but excluded from ratcheting and
proposer context.
"""

from __future__ import annotations

from typing import Iterable


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def experiment_has_positive_tests(exp: object) -> bool:
    """True when the experiment has a positive held-out test pass signal."""
    return _float(getattr(exp, "aggregate_pass_rate", 0.0)) > 0.0


def score_has_positive_tests(score: object) -> bool:
    return _float(getattr(score, "tests", 0.0)) > 0.0


def entry_has_positive_tests(entry: dict) -> bool:
    breakdown = entry.get("breakdown") or {}
    return _float(breakdown.get("tests", 0.0)) > 0.0


def usable_journal_entries(entries: Iterable[dict]) -> list[dict]:
    """Drop zero-test entries before they affect champions or proposer context."""
    return [entry for entry in entries if entry_has_positive_tests(entry)]
