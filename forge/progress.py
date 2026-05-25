"""
forge.progress

Intra-experiment step tracker for the dashboard. One experiment runs a fixed
pipeline — propose → (planner, generator, validator) per benchmark → judge per
benchmark — and this models that as an ordered list of steps, times each one,
and estimates time-to-complete for the whole iteration from rolling averages
(persisted in state/step_timing.json, so the ETA sharpens over runs).

The orchestrator drives it (start/done per step) and writes to_dict() into
live.json; the dashboard renders the step list + a progress bar + ETA.
"""

from __future__ import annotations

import json
import time

from .config import CONFIG

_TIMING_FILE = CONFIG.state_dir / "step_timing.json"

# Per-step-type seconds, seeded for qwen-9B-ish local runs and refined by EMA.
_DEFAULTS: dict[str, float] = {
    "propose": 120.0,
    "planner": 90.0,
    "generator": 200.0,
    "validator": 110.0,
    "judge": 35.0,
}


def _load_estimates() -> dict[str, float]:
    try:
        data = json.loads(_TIMING_FILE.read_text(encoding="utf-8"))
        return {**_DEFAULTS, **{k: float(v) for k, v in data.items()}}
    except Exception:
        return dict(_DEFAULTS)


def _save_estimates(est: dict[str, float]) -> None:
    try:
        CONFIG.state_dir.mkdir(parents=True, exist_ok=True)
        _TIMING_FILE.write_text(json.dumps(est), encoding="utf-8")
    except Exception:
        pass


class ProgressTracker:
    """Steps of ONE experiment, with timing + ETA. Thread-safe enough for the
    heartbeat to call to_dict() while the main thread calls start/done (field
    reassignments under the GIL; no key add/remove, so no iteration crash)."""

    def __init__(self, exp_id: str, benchmarks, include_propose: bool = True):
        self.exp_id = exp_id
        self.est = _load_estimates()
        self.started = time.time()
        self.steps: list[dict] = []
        if include_propose:
            self.steps.append(self._mk("propose", "propose mutation", "propose"))
        # roles run benchmark-by-benchmark, then all judges at the end —
        # mirror that execution order so "where we are" reads top-to-bottom.
        for b in benchmarks:
            for role in ("planner", "generator", "validator"):
                self.steps.append(self._mk(f"{b}/{role}", f"{b} · {role}", role))
        for b in benchmarks:
            self.steps.append(self._mk(f"{b}/judge", f"{b} · judge", "judge"))

    @staticmethod
    def _mk(key: str, label: str, typ: str) -> dict:
        return {"key": key, "label": label, "type": typ, "status": "pending", "seconds": 0.0, "_start": None}

    def start(self, key: str) -> None:
        for s in self.steps:
            if s["status"] == "running" and s["key"] != key:
                self._finish(s)
        for s in self.steps:
            if s["key"] == key:
                s["status"] = "running"
                s["_start"] = time.time()

    def done(self, key: str) -> None:
        for s in self.steps:
            if s["key"] == key and s["status"] == "running":
                self._finish(s)
        _save_estimates(self.est)

    def _finish(self, s: dict) -> None:
        if s["_start"]:
            s["seconds"] = round(time.time() - s["_start"], 1)
        s["status"] = "done"
        t = s["type"]
        prev = self.est.get(t, _DEFAULTS.get(t, 60.0))
        self.est[t] = round(0.6 * prev + 0.4 * s["seconds"], 1)  # exponential moving avg

    def to_dict(self) -> dict:
        now = time.time()
        done = sum(1 for s in self.steps if s["status"] == "done")
        cur = next((s for s in self.steps if s["status"] == "running"), None)
        cur_elapsed = round(now - cur["_start"], 1) if cur and cur.get("_start") else 0.0
        eta = 0.0
        for s in self.steps:
            if s["status"] == "pending":
                eta += self.est.get(s["type"], 60.0)
            elif s["status"] == "running":
                eta += max(0.0, self.est.get(s["type"], 60.0) - cur_elapsed)
        return {
            "exp_id": self.exp_id,
            "steps_total": len(self.steps),
            "steps_done": done,
            "current": cur["label"] if cur else None,
            "current_type": cur["type"] if cur else None,
            "current_elapsed": cur_elapsed,
            "elapsed": round(now - self.started, 1),
            "eta_seconds": round(eta, 1),
            "steps": [
                {"label": s["label"], "type": s["type"], "status": s["status"], "seconds": s["seconds"]}
                for s in self.steps
            ],
        }
