"""
forge.state

Append-only journal + atomic live-state snapshot + stop sentinel. Shared by the
orchestrator (writer) and the dashboard (reader). All writes are atomic so the
dashboard never reads a half-written file.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import CONFIG


# ───── journal (append-only, one JSON line per experiment) ──────────────────


def append_journal(entry: dict) -> None:
    CONFIG.experiments_jsonl.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG.experiments_jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def read_journal(limit: int | None = None) -> list[dict]:
    if not CONFIG.experiments_jsonl.exists():
        return []
    out: list[dict] = []
    for ln in CONFIG.experiments_jsonl.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out[-limit:] if limit else out


# ───── live snapshot (single JSON the dashboard polls) ──────────────────────


def write_live(state: dict) -> None:
    state["updated_at"] = time.time()
    CONFIG.live_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.live_json.with_name(CONFIG.live_json.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(CONFIG.live_json)  # atomic on the same filesystem


def read_live() -> dict | None:
    if not CONFIG.live_json.exists():
        return None
    try:
        return json.loads(CONFIG.live_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


# ───── stop sentinel (forge stop creates it; the loop polls it) ─────────────


_STOP_FILE: Path = CONFIG.state_dir / "stop"


def request_stop() -> None:
    _STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STOP_FILE.write_text("stop", encoding="utf-8")


def stop_requested() -> bool:
    return _STOP_FILE.exists()


def clear_stop() -> None:
    _STOP_FILE.unlink(missing_ok=True)
