"""
forge.agent_cli

The single choke-point for spawning an authed agentic CLI. Two kinds of call:

  run_agent(...)      → an inner-loop coding agent (planner/generator/validator)
                        with full tool access inside a benchmark scratch dir.
  run_researcher(...) → a single-shot reasoning call (proposer / judge) with no
                        tools; returns the model's text.

Both go through `claude` (OAuth/subscription — NO ANTHROPIC_API_KEY) by
default, or `codex` when CONFIG.agent_cli == "codex". No HTTP, no SDK, no key.

Key implementation facts (verified against claude 2.1.150 on Windows):
  - `claude` resolves to a real .exe → shell=False works directly.
  - System prompts are large (4–9 KB) → pass via `--system-prompt-file`, never
    inline (Windows command line caps at ~32 KB).
  - `--strict-mcp-config --mcp-config {empty}` drops the user's global MCP
    servers (Gmail/Slack/Drive/…), cutting ~58 KB of token overhead AND removing
    a confound — the agents only get coding tools.
  - `--output-format json` prints ONE JSON object at exit with the final text in
    `result`, plus `num_turns`, `total_cost_usd`, `usage`, `is_error`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .config import CONFIG


# ─────────────────────────────────────────────────────────────────────────────
# Result type
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class AgentRun:
    final_text: str
    num_turns: int
    cost_usd: float
    is_error: bool
    session_id: str | None
    exit_code: int | None
    wall_seconds: float
    error: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Executable + shared config resolution (cached)
# ─────────────────────────────────────────────────────────────────────────────


def _which(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise FileNotFoundError(
            f"'{name}' CLI not found on PATH. The forge inner loop needs an "
            f"authed agentic CLI. Install it and ensure you've logged in."
        )
    return exe


_NOMCP_PATH: Path | None = None


def _nomcp_config() -> Path:
    """Path to a JSON file declaring zero MCP servers. Combined with
    --strict-mcp-config this guarantees no MCP tools leak into an agent."""
    global _NOMCP_PATH
    if _NOMCP_PATH is None:
        CONFIG.state_dir.mkdir(parents=True, exist_ok=True)
        p = CONFIG.state_dir / "nomcp.json"
        p.write_text('{"mcpServers": {}}', encoding="utf-8")
        _NOMCP_PATH = p
    return _NOMCP_PATH


# Tools to explicitly deny for researcher (no-tool reasoning) calls. Listing the
# built-ins keeps the prompt self-contained and avoids wasted denied turns.
_RESEARCHER_DISALLOW = (
    "Bash", "Edit", "Write", "Read", "Glob", "Grep",
    "WebSearch", "WebFetch", "Task", "NotebookEdit",
)


# ─────────────────────────────────────────────────────────────────────────────
# Subprocess + parsing
# ─────────────────────────────────────────────────────────────────────────────


def _run(
    argv: list[str], *, cwd: Path | None, log_path: Path, timeout_s: int,
    stdin_text: str | None = None,
) -> tuple[int | None, str]:
    """Run argv (shell=False), tee stdout+stderr to log_path, return (exit, stdout).

    Large prompts go via `stdin_text` (piped to the CLI), NOT on the command
    line — Windows caps the command line at ~32 KB and a packed judge payload
    (artifacts ≈ 30 KB+) overflows it (WinError 206)."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            argv,
            input=stdin_text,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            shell=False,
        )
    except subprocess.TimeoutExpired as e:
        body = (e.stdout or "") if isinstance(e.stdout, str) else ""
        log_path.write_text(body + f"\n[forge] TIMEOUT after {timeout_s}s\n", encoding="utf-8")
        return None, body
    log_path.write_text(
        (proc.stdout or "") + ("\n----- stderr -----\n" + proc.stderr if proc.stderr else ""),
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout or ""


def _parse_claude_json(stdout: str) -> dict:
    """claude --output-format json prints one JSON object. Tolerate a preamble."""
    brace = stdout.find("{")
    if brace < 0:
        raise ValueError("no JSON object in claude output")
    # The result object is the last top-level {...}; find the matching close by
    # decoding from the first brace (claude emits exactly one object at the end).
    return json.loads(stdout[brace:])


def _parse_codex_jsonl_final(stdout: str) -> str:
    """codex exec --json emits JSONL events. Return the last agent/assistant text."""
    last_text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        # codex event shapes vary across versions; look for assistant message text.
        msg = ev.get("msg") or ev
        t = msg.get("type") or ev.get("type")
        if t in ("agent_message", "assistant_message", "message", "item.completed"):
            text = (
                msg.get("text")
                or msg.get("message")
                or (msg.get("item") or {}).get("text")
                or ""
            )
            if isinstance(text, str) and text.strip():
                last_text = text
    return last_text


# ─────────────────────────────────────────────────────────────────────────────
# Public: inner-loop coding agent
# ─────────────────────────────────────────────────────────────────────────────


def run_agent(
    *,
    system_prompt: str,
    user_message: str,
    work_dir: Path,
    sys_prompt_file: Path,
    log_path: Path,
    cli: str | None = None,
    model: str | None = None,
    timeout_s: int | None = None,
) -> AgentRun:
    """Run a tool-using coding agent inside `work_dir`. Returns its final text +
    turn/cost stats. The role's system prompt is written to `sys_prompt_file`."""
    cli = cli or CONFIG.agent_cli
    model = model or CONFIG.agent_model()
    timeout_s = timeout_s or CONFIG.role_timeout_s

    sys_prompt_file.parent.mkdir(parents=True, exist_ok=True)
    sys_prompt_file.write_text(system_prompt, encoding="utf-8")

    started = time.time()
    if cli == "codex":
        run = _run_codex_agent(user_message, system_prompt, work_dir, model, log_path, timeout_s)
    else:
        run = _run_claude_agent(user_message, sys_prompt_file, work_dir, model, log_path, timeout_s)
    run.wall_seconds = time.time() - started
    return run


def _run_claude_agent(
    user_message: str, sys_prompt_file: Path, work_dir: Path, model: str,
    log_path: Path, timeout_s: int,
) -> AgentRun:
    argv = [
        _which("claude"), "-p", user_message,
        "--system-prompt-file", str(sys_prompt_file),
        "--model", model,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--add-dir", str(work_dir),
        "--strict-mcp-config", "--mcp-config", str(_nomcp_config()),
        "--allowedTools", " ".join(CONFIG.agent_allowed_tools),
        "--max-budget-usd", str(CONFIG.agent_max_budget_usd),
    ]
    exit_code, stdout = _run(argv, cwd=work_dir, log_path=log_path, timeout_s=timeout_s)
    if exit_code is None:
        return AgentRun("", 0, 0.0, True, None, None, 0.0, error=f"timeout after {timeout_s}s")
    try:
        data = _parse_claude_json(stdout)
    except Exception as e:
        return AgentRun("", 0, 0.0, True, None, exit_code, 0.0, error=f"unparseable claude output: {e}")
    return AgentRun(
        final_text=str(data.get("result", "")),
        num_turns=int(data.get("num_turns", 0) or 0),
        cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
        is_error=bool(data.get("is_error", False)),
        session_id=data.get("session_id"),
        exit_code=exit_code,
        wall_seconds=0.0,
    )


def _run_codex_agent(
    user_message: str, system_prompt: str, work_dir: Path, model: str,
    log_path: Path, timeout_s: int,
) -> AgentRun:
    # codex exec has no clean system/user split, so prepend the role prompt to
    # the kickoff. --full-auto allows autonomous file edits inside the cwd.
    # NOTE: unverified against codex 0.130.0 — first codex run should confirm the
    # event shape parsed by _parse_codex_jsonl_final.
    combined = f"{system_prompt}\n\n---\n\n{user_message}"
    # codex reads instructions from stdin when the prompt arg is omitted — keep
    # the (large) combined prompt off the command line.
    argv = [
        _which("codex"), "exec",
        "-m", model,
        "--cd", str(work_dir),
        "--full-auto",
        "--json",
    ]
    exit_code, stdout = _run(argv, cwd=work_dir, log_path=log_path, timeout_s=timeout_s, stdin_text=combined)
    if exit_code is None:
        return AgentRun("", 0, 0.0, True, None, None, 0.0, error=f"timeout after {timeout_s}s")
    text = _parse_codex_jsonl_final(stdout)
    return AgentRun(
        final_text=text,
        num_turns=0,
        cost_usd=0.0,
        is_error=(exit_code != 0 and not text),
        session_id=None,
        exit_code=exit_code,
        wall_seconds=0.0,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public: outer-loop researcher (proposer / judge) — no tools, single answer
# ─────────────────────────────────────────────────────────────────────────────


def run_researcher(
    *,
    system_prompt: str,
    user_message: str,
    sys_prompt_file: Path,
    log_path: Path,
    model: str | None = None,
    timeout_s: int = 600,
) -> AgentRun:
    """Single-shot reasoning call (proposer/judge). Always claude CLI, no tools.
    Returns the model's text in .final_text. Failure-safe: on any error returns
    is_error=True with .error set and empty text (callers degrade gracefully)."""
    model = model or CONFIG.researcher_model
    sys_prompt_file.parent.mkdir(parents=True, exist_ok=True)
    sys_prompt_file.write_text(system_prompt, encoding="utf-8")

    started = time.time()
    try:
        # The prompt (packed artifacts) is large → pipe via stdin, not argv.
        argv = [
            _which("claude"), "-p",
            "--system-prompt-file", str(sys_prompt_file),
            "--model", model,
            "--output-format", "json",
            "--strict-mcp-config", "--mcp-config", str(_nomcp_config()),
            "--disallowedTools", " ".join(_RESEARCHER_DISALLOW),
        ]
    except FileNotFoundError as e:
        return AgentRun("", 0, 0.0, True, None, None, time.time() - started, error=str(e))

    exit_code, stdout = _run(argv, cwd=None, log_path=log_path, timeout_s=timeout_s, stdin_text=user_message)
    wall = time.time() - started
    if exit_code is None:
        return AgentRun("", 0, 0.0, True, None, None, wall, error=f"timeout after {timeout_s}s")
    try:
        data = _parse_claude_json(stdout)
    except Exception as e:
        return AgentRun("", 0, 0.0, True, None, exit_code, wall, error=f"unparseable: {e}")
    return AgentRun(
        final_text=str(data.get("result", "")),
        num_turns=int(data.get("num_turns", 0) or 0),
        cost_usd=float(data.get("total_cost_usd", 0.0) or 0.0),
        is_error=bool(data.get("is_error", False)),
        session_id=data.get("session_id"),
        exit_code=exit_code,
        wall_seconds=wall,
    )
