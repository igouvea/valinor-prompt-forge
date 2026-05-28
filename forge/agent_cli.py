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
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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
    tokens_out: int = 0   # generated tokens (output + reasoning) — for tok/s
    error: str | None = None


# opencode emits per-turn usage in its --format json stream; sum output+reasoning.
_OPENCODE_TOK_RE = re.compile(r'"tokens":\{"total":\d+,"input":\d+,"output":(\d+),"reasoning":(\d+)')


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


def isolated_agent_env(work_dir: Path) -> dict[str, str]:
    """Environment for benchmark-scoped agent subprocesses.

    opencode discovers a workspace by walking up to the nearest Git root. The
    benchmark dirs are nested inside the forge repo, so without a ceiling it can
    promote the workspace to valinor-prompt-forge and read the forge-level
    `.valinor/` state instead of the benchmark-local one. The ceiling keeps the
    agent rooted at the requested benchmark dir.
    """
    env = dict(os.environ)
    env["VALINOR_PROJECT_ROOT"] = str(work_dir)
    env["VALINOR_FORGE_MODE"] = "1"
    env["VALINOR_FORGE_BENCHMARK_ROOT"] = str(work_dir)
    env["GIT_CEILING_DIRECTORIES"] = str(work_dir.parent)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("NO_COLOR", "1")
    return env


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
    stdin_text: str | None = None, env: dict[str, str] | None = None,
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
            env=env,
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


def _stop_requested() -> bool:
    try:
        from . import state
        return state.stop_requested()
    except Exception:
        return False


def _kill_process_tree(proc: subprocess.Popen) -> None:
    """Best-effort termination for a CLI plus its spawned tool children."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                shell=False,
            )
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


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
    label: str = "agent",
    on_progress: Callable[[int], None] | None = None,
) -> AgentRun:
    """Run a tool-using coding agent inside `work_dir`. Returns its final text +
    turn/cost stats. The role's system prompt is written to `sys_prompt_file`.
    `label` names the role (used for the opencode agent definition)."""
    cli = cli or CONFIG.agent_cli
    model = model or CONFIG.agent_model()
    timeout_s = timeout_s or CONFIG.role_timeout_s

    sys_prompt_file.parent.mkdir(parents=True, exist_ok=True)
    sys_prompt_file.write_text(system_prompt, encoding="utf-8")

    started = time.time()
    if cli == "lmstudio":
        run = _run_opencode_agent(
            system_prompt, user_message, work_dir, model, log_path, timeout_s,
            agent_name=f"forge-{label}", on_progress=on_progress,
        )
    elif cli == "codex":
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
    exit_code, stdout = _run(
        argv, cwd=work_dir, log_path=log_path, timeout_s=timeout_s,
        env=isolated_agent_env(work_dir),
    )
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
        tokens_out=int((data.get("usage") or {}).get("output_tokens", 0) or 0),
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
    exit_code, stdout = _run(
        argv, cwd=work_dir, log_path=log_path, timeout_s=timeout_s,
        stdin_text=combined, env=isolated_agent_env(work_dir),
    )
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
# opencode + LM Studio driver (local models — gpt-oss-20b etc.)
# ─────────────────────────────────────────────────────────────────────────────


def _opencode_entry() -> tuple[str, str] | None:
    """(node, opencode-js) so we invoke node directly and dodge cmd.exe's 8K
    command-line limit on Windows. None → fall back to `opencode` on PATH."""
    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = Path(appdata) / "npm" / "node_modules" / "opencode-ai" / "bin" / "opencode"
        if cand.is_file():
            local_node = cand.parent.parent.parent.parent / "node.exe"
            node = str(local_node) if local_node.is_file() else "node"
            return node, str(cand)
    return None


_OPENCODE_ENTRY = _opencode_entry()
_SESSION_RE = re.compile(r'"sessionID":"(ses_[A-Za-z0-9]+)"')


def _opencode_argv(*extra: str) -> list[str]:
    if _OPENCODE_ENTRY is not None:
        node, entry = _OPENCODE_ENTRY
        return [node, entry, *extra]
    return ["opencode", *extra]


def _write_opencode_agent(work_dir: Path, agent_name: str, system_prompt: str) -> None:
    """opencode discovers project agents in <cwd>/.opencode/agent/. Putting the
    role in the agent's SYSTEM prompt (not the user message) is what stops small
    models from roleplaying a greeting instead of doing the work."""
    agent_dir = work_dir / ".opencode" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = (
        "---\n"
        f"description: forge {agent_name}\n"
        "mode: primary\n"
        "temperature: 0.2\n"
        "permission:\n"
        "  bash: allow\n  edit: allow\n  write: allow\n  read: allow\n"
        "  webfetch: deny\n  websearch: deny\n"
        "---\n"
    )
    (agent_dir / f"{agent_name}.md").write_text(frontmatter + system_prompt, encoding="utf-8")


def _opencode_export_text(session_id: str) -> str:
    """`opencode export <id>` → last assistant message's text parts. (--format
    json streams tool/step events but not final text, so we recover it here.)"""
    try:
        out = subprocess.run(
            _opencode_argv("export", session_id),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, shell=False,
        )
    except subprocess.SubprocessError:
        return ""
    if out.returncode != 0:
        return ""
    brace = out.stdout.find("{")
    if brace < 0:
        return ""
    try:
        data = json.loads(out.stdout[brace:])
    except json.JSONDecodeError:
        return ""
    for m in reversed(data.get("messages") or []):
        if (m.get("info") or {}).get("role") == "assistant":
            parts = m.get("parts") or []
            return "\n".join(p.get("text", "") for p in parts if p.get("type") == "text")
    return ""


def _opencode_logged_error(log_path: Path) -> str | None:
    """Return a concise opencode error found in the JSON event log."""
    if not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if "Context size has been exceeded" in text:
        return "Context size has been exceeded"
    for line in reversed(text.splitlines()):
        if '"type":"error"' not in line and '"error":' not in line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        err = data.get("error") or {}
        if isinstance(err, dict):
            msg = (err.get("data") or {}).get("message") or err.get("message")
            if msg:
                return str(msg)
        if isinstance(err, str) and err:
            return err
    return None


def _run_opencode_agent(
    system_prompt: str, user_message: str, work_dir: Path, model: str,
    log_path: Path, timeout_s: int, agent_name: str,
    on_progress: Callable[[int], None] | None = None,
) -> AgentRun:
    _write_opencode_agent(work_dir, agent_name, system_prompt)
    cmd = _opencode_argv(
        "run", "--dangerously-skip-permissions",
        "--dir", str(work_dir), "--agent", agent_name,
        "-m", model, "--format", "json", user_message,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    session_id: str | None = None
    tokens_out = 0
    try:
        with log_path.open("w", encoding="utf-8") as log:
            proc = subprocess.Popen(
                cmd, cwd=str(work_dir), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", shell=False, env=isolated_agent_env(work_dir),
            )
            assert proc.stdout is not None
            stop_reason: dict[str, str | None] = {"value": None}
            last_output_at = {"value": time.time()}
            quiet_timeout_s = max(0, CONFIG.role_quiet_timeout_s)
            deadline = time.time() + timeout_s

            def _watchdog() -> None:
                while proc.poll() is None:
                    now = time.time()
                    reason: str | None = None
                    if _stop_requested():
                        reason = "stop requested"
                    elif now >= deadline:
                        reason = f"timeout after {timeout_s}s"
                    elif quiet_timeout_s and now - last_output_at["value"] >= quiet_timeout_s:
                        reason = f"quiet timeout after {quiet_timeout_s}s with no opencode output"
                    if reason:
                        stop_reason["value"] = reason
                        _kill_process_tree(proc)
                        return
                    time.sleep(1)

            watchdog = threading.Thread(target=_watchdog, daemon=True)
            watchdog.daemon = True
            watchdog.start()
            try:
                for line in proc.stdout:
                    last_output_at["value"] = time.time()
                    log.write(line)
                    log.flush()
                    if session_id is None:
                        match = _SESSION_RE.search(line)
                        if match:
                            session_id = match.group(1)
                    for m in _OPENCODE_TOK_RE.finditer(line):  # sum per-turn output+reasoning
                        tokens_out += int(m.group(1)) + int(m.group(2))
                    if on_progress:
                        on_progress(tokens_out)
                proc.wait()
            finally:
                pass
            if stop_reason["value"]:
                reason = stop_reason["value"] or "stopped"
                log.write(f"\n[forge] {reason} — killed agent process tree\n")
                log.flush()
                return AgentRun("", 0, 0.0, True, session_id, None, 0.0, tokens_out, reason)
    except FileNotFoundError as e:
        return AgentRun("", 0, 0.0, True, None, None, 0.0, 0, f"opencode not found: {e}")

    final = _opencode_export_text(session_id) if session_id else ""
    logged_error = _opencode_logged_error(log_path)
    return AgentRun(
        final_text=final,
        num_turns=0,
        cost_usd=0.0,  # local model — no cloud cost
        is_error=bool(logged_error) or (proc.returncode != 0 and not final),
        session_id=session_id,
        exit_code=proc.returncode,
        wall_seconds=0.0,
        tokens_out=tokens_out,
        error=logged_error,
    )


# ─────────────────────────────────────────────────────────────────────────────
# LM Studio auto-launch (so "Start" in Valinor just works for local runs)
# ─────────────────────────────────────────────────────────────────────────────

_LMSTUDIO_BASE = "http://localhost:1234/v1"


def _lmstudio_loaded_models() -> list[str] | None:
    """Loaded model ids, or None if the LM Studio server isn't reachable."""
    try:
        with urllib.request.urlopen(_LMSTUDIO_BASE + "/models", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return [m.get("id", "") for m in data.get("data", [])]
    except Exception:
        return None


def _run_lms(args: list[str], timeout: int) -> tuple[int, str]:
    exe = shutil.which("lms") or "lms"
    try:
        if os.name == "nt":
            # `lms` is a .cmd shim on Windows → go through the shell. Args are
            # trusted (model id + numbers from config), so no injection surface.
            proc = subprocess.run(
                '"' + exe + '" ' + " ".join(args),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, shell=True,
            )
        else:
            proc = subprocess.run(
                [exe, *args], capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, shell=False,
            )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:  # noqa: BLE001
        return 1, str(e)


def _lms_resident() -> list[dict]:
    """Models currently loaded in memory (from `lms ps --json`)."""
    rc, out = _run_lms(["ps", "--json"], 15)
    if rc != 0:
        return []
    start = out.find("[")
    if start < 0:
        return []
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return []


def _model_key(m: dict) -> str:
    return m.get("modelKey") or m.get("identifier") or ""


def ensure_lmstudio_ready(
    model_id: str, context_length: int = 32768, load_variant: str = ""
) -> tuple[bool, str]:
    """Ensure the LM Studio server is up and ONLY the target model is resident,
    at a large-enough context AND the right quant variant. Returns (ok, message).

    Gotchas this handles on a 16GB GPU:
      - A model resident at LM Studio's 4096 default truncates our ~11K-token
        prompts; /v1/models only says a model is *downloaded*, so we read the
        resident contextLength via `lms ps --json` and reload when too small.
      - The same model can be resident at the wrong quant (e.g. a heavy Q8 vs
        the intended Q4) — we compare selectedVariant and reload if it differs.
      - Two big models can't coexist in 16GB, so we unload any OTHER resident
        model before loading the target.
    `model_id` is opencode-form; `load_variant` (model@quant) pins the quant and
    is served under the base identifier so opencode addresses it unchanged."""
    lms_key = model_id.split("/", 1)[1] if model_id.startswith("lmstudio/") else model_id
    last = lms_key.rsplit("/", 1)[-1]

    # 1. server reachable?
    if _lmstudio_loaded_models() is None:
        rc, out = _run_lms(["server", "start"], 30)
        if rc != 0:
            return False, f"could not start LM Studio server (is `lms` installed?): {out.strip()[:200]}"
        for _ in range(20):
            time.sleep(1)
            if _lmstudio_loaded_models() is not None:
                break
        if _lmstudio_loaded_models() is None:
            return False, "LM Studio server did not become reachable on :1234"

    # 2. already exactly right? (single instance, our model, big ctx, right quant)
    resident = _lms_resident()
    matches = [m for m in resident if last in _model_key(m)]
    target = matches[0] if matches else None
    ctx = target.get("contextLength") if target else None
    variant_ok = (not load_variant) or (target is not None and target.get("selectedVariant") == load_variant)
    if (target is not None and len(matches) == 1 and len(resident) == 1
            and isinstance(ctx, (int, float)) and ctx >= context_length and variant_ok):
        return True, f"{lms_key} already loaded @ {int(ctx)} ctx ({target.get('selectedVariant')})"

    # 3. clean slate, then load. `--yes` picks the preferred variant for an
    #    ambiguous key (LM Studio prefers the smaller Q4) — the `@variant` form
    #    isn't a valid LOAD key, only a `get` key. unload --all also clears any
    #    duplicate instances and frees VRAM (16GB can't host two big models).
    _run_lms(["unload", "--all"], 60)
    rc, out = _run_lms(["load", lms_key, "-c", str(context_length), "--gpu", "max", "-y"], 300)
    if rc != 0:
        return False, f"`lms load {lms_key} -c {context_length}` failed: {out.strip()[:200]}"

    got = None
    after = next((m for m in _lms_resident() if last in _model_key(m)), None)
    if after is not None:
        got = after.get("selectedVariant")
    if load_variant and got and got != load_variant:
        return True, f"loaded {lms_key} @ {context_length} ctx (got {got}, wanted {load_variant})"
    return True, f"loaded {lms_key} @ {context_length} ctx ({got or 'default variant'})"


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
        tokens_out=int((data.get("usage") or {}).get("output_tokens", 0) or 0),
    )
