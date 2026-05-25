"""
forge.state

Append-only journal + atomic live-state snapshot + stop sentinel. Shared by the
orchestrator (writer) and the dashboard (reader). All writes are atomic so the
dashboard never reads a half-written file.
"""

from __future__ import annotations

import json
import os
import subprocess
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


# ───── single-instance run lock (prevent two concurrent loops) ──────────────

_LOCK_FILE: Path = CONFIG.state_dir / "lock"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=10,
            )
            return str(pid) in (out.stdout or "")
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def acquire_run_lock() -> bool:
    """True if this process took the lock; False if another LIVE loop holds it.
    A stale lock (holder PID dead) is taken over. Prevents two `forge run`
    loops racing on the same state (which loads the model twice and overflows
    the GPU)."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _LOCK_FILE.exists():
        try:
            holder = int(_LOCK_FILE.read_text(encoding="utf-8").split()[0])
        except Exception:
            holder = -1
        if holder != os.getpid() and _pid_alive(holder):
            return False
    _LOCK_FILE.write_text(f"{os.getpid()} {time.time()}", encoding="utf-8")
    return True


def release_run_lock() -> None:
    try:
        if _LOCK_FILE.exists():
            holder = int(_LOCK_FILE.read_text(encoding="utf-8").split()[0])
            if holder == os.getpid():
                _LOCK_FILE.unlink()
    except Exception:
        pass


def lock_holder() -> int | None:
    """PID of the live lock holder, or None."""
    if not _LOCK_FILE.exists():
        return None
    try:
        pid = int(_LOCK_FILE.read_text(encoding="utf-8").split()[0])
    except Exception:
        return None
    return pid if _pid_alive(pid) else None
