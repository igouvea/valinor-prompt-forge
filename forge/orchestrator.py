"""
forge.orchestrator

The outer autoresearch loop (Karpathy's pattern):

    baseline (seed champion) → score → set champion
    loop:
        propose mutation (Opus) → candidate prompts
        run experiment (agents on the toggled CLI) → artifacts + tests + cycles
        judge (Opus) → rubric → score
        ratchet: candidate beats champion ? adopt : discard
        append journal, regen progress.md, update live.json
        until champion ≥ threshold & plateau, or `forge stop`

Each experiment is wrapped in try/except so a single failure can't kill an
overnight run. Live state is written continuously for the dashboard.
"""

from __future__ import annotations

import shutil
import sys
import threading
import time
import traceback

from .config import CONFIG, ROLES
from . import state
from .experiment import ChampionPrompts, ExperimentResult, run_experiment, _next_exp_id
from .judge import score_experiment, RubricResult
from .scorer import Score, compute_score, beats
from .proposer import propose, build_context_md
from .progress import ProgressTracker


# ─────────────────────────────────────────────────────────────────────────────
# Champion management
# ─────────────────────────────────────────────────────────────────────────────


def _ensure_champion() -> None:
    """If champion prompts are missing, seed them from prompts/seed/."""
    CONFIG.prompts_champion_dir.mkdir(parents=True, exist_ok=True)
    for role in ROLES:
        champ = CONFIG.prompts_champion_dir / f"{role}.md"
        if not champ.exists():
            seed = CONFIG.prompts_seed_dir / f"{role}.md"
            if not seed.exists():
                raise FileNotFoundError(f"no seed prompt for {role}: {seed}")
            shutil.copy2(seed, champ)


def _adopt(prompts: ChampionPrompts) -> None:
    """Write candidate prompts into prompts/champion/ (the new champion)."""
    (CONFIG.prompts_champion_dir / "planner.md").write_text(prompts.planner, encoding="utf-8")
    (CONFIG.prompts_champion_dir / "generator.md").write_text(prompts.generator, encoding="utf-8")
    (CONFIG.prompts_champion_dir / "validator.md").write_text(prompts.validator, encoding="utf-8")


def _score_from_entry(entry: dict) -> Score:
    bd = entry.get("breakdown") or {}
    # tolerate the older "cycles" key from pre-time-metric journals
    speed = bd.get("speed", bd.get("cycles", 0.0))
    return Score(
        total=float(entry.get("score", 0.0)),
        tests=float(bd.get("tests", 0.0)),
        speed=float(speed),
        rubric=float(bd.get("rubric", 0.0)),
        raw_wall_seconds=float(bd.get("raw_wall_seconds", 0.0)),
        raw_total_cycles=int(bd.get("raw_total_cycles", 0)),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Live state
# ─────────────────────────────────────────────────────────────────────────────


class Live:
    """In-memory mirror of state/live.json, flushed atomically on every change."""

    def __init__(self) -> None:
        self.d: dict = {
            "status": "starting",
            "status_detail": "",
            "config": {
                "agent_cli": CONFIG.agent_cli,
                "agent_model": CONFIG.agent_model(),
                "researcher_model": CONFIG.researcher_model,
                "weights": {"tests": CONFIG.weights.tests, "speed": CONFIG.weights.speed,
                            "rubric": CONFIG.weights.rubric},
                "benchmarks": list(CONFIG.benchmarks),
                "stop_threshold": CONFIG.stop_score_threshold,
                "stop_plateau": CONFIG.stop_plateau_experiments,
            },
            "champion": None,
            "current_exp": None,
            "totals": {"experiments": 0, "adopted": 0, "cost_usd": 0.0},
            "best_score": 0.0,
            "plateau_count": 0,
            "history": [],
            "progress": None,
        }
        self.tracker = None  # current ProgressTracker; the heartbeat refreshes it

    def set(self, **kw) -> None:
        self.d.update(kw)
        state.write_live(self.d)

    def status(self, status: str, detail: str = "") -> None:
        self.d["status"] = status
        self.d["status_detail"] = detail
        state.write_live(self.d)

    def hydrate_from_journal(self, journal: list[dict]) -> None:
        adopted = [e for e in journal if e.get("adopted")]
        if adopted:
            last = adopted[-1]
            self.d["champion"] = {
                "exp_id": last.get("exp_id"),
                "score": last.get("score", 0.0),
                "breakdown": last.get("breakdown", {}),
            }
        self.d["totals"]["experiments"] = len(journal)
        self.d["totals"]["adopted"] = len(adopted)
        self.d["totals"]["cost_usd"] = round(sum(e.get("cost_usd", 0.0) for e in journal), 4)
        self.d["best_score"] = max((e.get("score", 0.0) for e in journal), default=0.0)
        self.d["history"] = [
            {k: e.get(k) for k in ("exp_id", "score", "adopted", "hypothesis",
                                   "breakdown", "cost_usd", "finished_at")}
            for e in journal[-200:]
        ]
        state.write_live(self.d)


# ─────────────────────────────────────────────────────────────────────────────
# One iteration
# ─────────────────────────────────────────────────────────────────────────────


def _run_and_score(prompts: ChampionPrompts, exp_id: str, live: Live,
                   tracker: "ProgressTracker | None" = None) -> tuple[ExperimentResult, list[RubricResult], Score]:
    acc = {"cost": 0.0, "roles": 0}

    def on_role(bench: str, role: str, phase: str, result=None) -> None:
        if tracker is not None:
            if phase == "start":
                tracker.start(f"{bench}/{role}")
            elif phase == "done":
                tracker.done(f"{bench}/{role}", tokens=getattr(result, "tokens_out", 0) or 0)
        if phase == "done" and result is not None:
            acc["cost"] += getattr(result, "cost_usd", 0.0) or 0.0
            acc["roles"] += 1
        live.d["current_exp"] = {
            "exp_id": exp_id,
            "phase": f"{role} on {bench} ({phase})",
            "cost_usd": round(acc["cost"], 4),
            "roles_done": acc["roles"],
        }
        if tracker is not None:
            live.d["progress"] = tracker.to_dict()
        live.status("running", f"{exp_id}: {role} on {bench} ({phase})")

    exp = run_experiment(prompts, exp_id, on_role=on_role)

    def on_judge(bench: str, phase: str, tokens: int = 0) -> None:
        if tracker is not None:
            if phase == "start":
                tracker.start(f"{bench}/judge")
            elif phase == "done":
                tracker.done(f"{bench}/judge", tokens=tokens)
            live.d["progress"] = tracker.to_dict()
        live.status("judging", f"{exp_id}: judging {bench}")

    rubric_mean, rubrics = score_experiment(exp, on_judge=on_judge)
    score = compute_score(exp, rubric_mean)
    live.tracker = None
    live.d["current_exp"] = None
    return exp, rubrics, score


def _journal_entry(
    exp: ExperimentResult, score: Score, adopted: bool,
    hypothesis: str, changes_summary: str, proposer_cost: float, judge_cost: float,
) -> dict:
    return {
        "exp_id": exp.exp_id,
        "finished_at": exp.finished_at,
        "score": round(score.total, 4),
        "adopted": adopted,
        "breakdown": {
            "tests": round(score.tests, 4),
            "speed": round(score.speed, 4),
            "rubric": round(score.rubric, 4),
            "raw_wall_seconds": round(score.raw_wall_seconds, 1),
            "raw_total_cycles": score.raw_total_cycles,
        },
        "verdicts": {b.benchmark: b.verdict for b in exp.benchmarks},
        "tests": {b.benchmark: f"{b.test.passed}/{b.test.total}" for b in exp.benchmarks},
        "hypothesis": hypothesis,
        "changes_summary": changes_summary,
        "cost_usd": round(exp.total_cost_usd + proposer_cost + judge_cost, 4),
    }


def _record(live: Live, entry: dict, journal: list[dict]) -> None:
    state.append_journal(entry)
    journal.append(entry)
    live.hydrate_from_journal(journal)
    _write_progress(journal, live)


# ─────────────────────────────────────────────────────────────────────────────
# progress.md
# ─────────────────────────────────────────────────────────────────────────────


def _write_progress(journal: list[dict], live: Live) -> None:
    champ = live.d.get("champion") or {}
    lines = [
        "# forge progress",
        "",
        f"_Updated {time.strftime('%Y-%m-%d %H:%M:%S')}_",
        "",
        f"- Agent CLI: **{CONFIG.agent_cli}** · model **{CONFIG.agent_model()}** · "
        f"researcher **{CONFIG.researcher_model}**",
        f"- Weights: tests {CONFIG.weights.tests} / speed {CONFIG.weights.speed} / rubric {CONFIG.weights.rubric}"
        f" · time-ref {CONFIG.time_ref_seconds:.0f}s/bench",
        f"- Benchmarks: {', '.join(CONFIG.benchmarks)}",
        f"- Experiments: {live.d['totals']['experiments']} · adopted: {live.d['totals']['adopted']} · "
        f"cost ≈ ${live.d['totals']['cost_usd']:.2f}",
        f"- Champion: **{champ.get('exp_id', '—')}** @ score **{champ.get('score', 0):.3f}**",
        f"- Best score: **{live.d['best_score']:.3f}** · plateau: {live.d['plateau_count']}",
        "",
        "## Experiments (most recent first)",
        "",
        "| exp | score | tests | speed | rubric | time/bench | adopted | hypothesis |",
        "| --- | ----- | ----- | ----- | ------ | ---------- | ------- | ---------- |",
    ]
    for e in reversed(journal[-50:]):
        bd = e.get("breakdown", {})
        hyp = (e.get("hypothesis") or "").replace("\n", " ").replace("|", "/")[:80]
        speed = bd.get("speed", bd.get("cycles", 0))
        secs = bd.get("raw_wall_seconds")
        secs_str = f"{secs:.0f}s" if secs is not None else "—"
        lines.append(
            f"| {e.get('exp_id')} | {e.get('score', 0):.3f} | {bd.get('tests', 0):.2f} | "
            f"{speed:.2f} | {bd.get('rubric', 0):.2f} | {secs_str} | "
            f"{'✓' if e.get('adopted') else '·'} | {hyp} |"
        )
    CONFIG.progress_md.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Main loop
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    max_experiments: int | None = None
    if "--max" in argv:
        i = argv.index("--max")
        try:
            max_experiments = int(argv[i + 1])
        except (IndexError, ValueError):
            print("[forge] --max needs an integer", file=sys.stderr)
            return 2

    # Single-instance guard: refuse to start if another loop is already live
    # (two loops race on the same state and load the model twice → GPU overflow).
    if not state.acquire_run_lock():
        print(f"[forge] another forge run loop is already active (PID {state.lock_holder()}); "
              f"refusing to start a second. Stop it first (forge stop / the Forge Stop button).",
              flush=True)
        return 0
    import atexit
    atexit.register(state.release_run_lock)

    _ensure_champion()
    state.clear_stop()
    journal = state.read_journal()

    live = Live()
    live.hydrate_from_journal(journal)

    # Heartbeat: refresh live.json every 15s so the app/dashboard keep seeing the
    # loop as RUNNING during a long agent role (which fires no phase events for
    # minutes). Without it, live.json goes stale and the app flips to IDLE.
    def _heartbeat() -> None:
        while True:
            time.sleep(10)
            try:
                if live.tracker is not None:
                    live.d["progress"] = live.tracker.to_dict()  # refresh elapsed/ETA
                state.write_live(live.d)
            except Exception:
                pass

    threading.Thread(target=_heartbeat, daemon=True).start()

    champion_prompts = ChampionPrompts.load(CONFIG.prompts_champion_dir)
    adopted_entries = [e for e in journal if e.get("adopted")]
    champion_score: Score | None = _score_from_entry(adopted_entries[-1]) if adopted_entries else None
    best_total = live.d["best_score"]
    plateau = 0
    done = 0

    print(f"[forge] orchestrator: cli={CONFIG.agent_cli} model={CONFIG.agent_model()} "
          f"researcher={CONFIG.researcher_model}", flush=True)

    # Auto-launch LM Studio for local runs so the Valinor "Start" button just
    # works — start the server and load the model if they aren't already up.
    if CONFIG.agent_cli == "lmstudio":
        live.status("starting", "ensuring LM Studio is up + model loaded…")
        from .agent_cli import ensure_lmstudio_ready
        ok, msg = ensure_lmstudio_ready(
            CONFIG.agent_model(), CONFIG.lmstudio_context_length, CONFIG.lmstudio_load_variant
        )
        print(f"[forge] LM Studio: {msg}", flush=True)
        if not ok:
            live.status("error", f"LM Studio not ready: {msg}")
            print(f"[forge] ABORT — {msg}", flush=True)
            return 1

    # ── baseline: if we've never scored the seed champion, do that first.
    if champion_score is None:
        exp_id = _next_exp_id()
        print(f"[forge] baseline {exp_id} (seed champion, no mutation)", flush=True)
        tracker = ProgressTracker(exp_id, CONFIG.benchmarks, include_propose=False)
        live.tracker = tracker
        live.status("running", f"{exp_id}: baseline")
        try:
            exp, rubrics, score = _run_and_score(champion_prompts, exp_id, live, tracker)
        except Exception as e:
            traceback.print_exc()
            live.status("error", f"baseline failed: {e}")
            return 1
        judge_cost = sum(r.cost_usd for r in rubrics)
        champion_score = score
        best_total = score.total
        entry = _journal_entry(exp, score, adopted=True,
                               hypothesis="baseline (seed prompts, unchanged)",
                               changes_summary="—", proposer_cost=0.0, judge_cost=judge_cost)
        _record(live, entry, journal)
        done += 1
        print(f"[forge] baseline score={score.total:.3f} "
              f"(tests={score.tests:.2f} speed={score.speed:.2f} rubric={score.rubric:.2f} "
              f"@ {score.raw_wall_seconds:.0f}s/bench)", flush=True)

    latest_exp: ExperimentResult | None = None
    latest_rubrics: list[RubricResult] = []
    latest_score: Score | None = champion_score

    # ── optimization loop
    while True:
        if state.stop_requested():
            print("[forge] stop requested — exiting loop.", flush=True)
            break
        if max_experiments is not None and done >= max_experiments:
            print(f"[forge] reached --max {max_experiments} — stopping.", flush=True)
            break
        if (champion_score and champion_score.total >= CONFIG.stop_score_threshold
                and plateau >= CONFIG.stop_plateau_experiments):
            print(f"[forge] goal reached: champion {champion_score.total:.3f} ≥ "
                  f"{CONFIG.stop_score_threshold} and plateau {plateau}. Stopping.", flush=True)
            break

        exp_id = _next_exp_id()
        run_dir = CONFIG.runs_dir / exp_id
        run_dir.mkdir(parents=True, exist_ok=True)
        tracker = ProgressTracker(exp_id, CONFIG.benchmarks, include_propose=True)
        live.tracker = tracker

        # 1. propose
        tracker.start("propose")
        live.d["progress"] = tracker.to_dict()
        live.status("proposing", f"{exp_id}: proposing mutation")
        context_md = (build_context_md(latest_exp, latest_rubrics, latest_score)
                      if latest_exp is not None else "# No prior experiment context.\n")
        proposal = propose(
            candidate_dir=run_dir / "candidate",
            champion=champion_prompts,
            journal_entries=state.read_journal(limit=12),
            context_md=context_md,
            log_dir=run_dir,
        )
        tracker.done("propose", tokens=proposal.tokens_out)
        live.d["progress"] = tracker.to_dict()
        if proposal.error:
            print(f"[forge] {exp_id}: proposer failed ({proposal.error}); skipping.", flush=True)
            live.status("error", f"{exp_id}: proposer failed: {proposal.error}")
            time.sleep(5)
            continue

        # 2. run + 3. judge + score
        try:
            exp, rubrics, score = _run_and_score(proposal.prompts, exp_id, live, tracker)
        except Exception as e:
            traceback.print_exc()
            print(f"[forge] {exp_id}: experiment failed ({e}); skipping.", flush=True)
            live.status("error", f"{exp_id}: experiment failed: {e}")
            time.sleep(5)
            continue

        judge_cost = sum(r.cost_usd for r in rubrics)
        latest_exp, latest_rubrics, latest_score = exp, rubrics, score

        # 4. ratchet
        won = beats(score, champion_score)
        if won:
            _adopt(proposal.prompts)
            champion_prompts = proposal.prompts
            champion_score = score
        if score.total > best_total:
            best_total = score.total
            plateau = 0
        else:
            plateau += 1
        live.d["plateau_count"] = plateau

        entry = _journal_entry(exp, score, adopted=won, hypothesis=proposal.hypothesis,
                               changes_summary=proposal.hypothesis[:120],
                               proposer_cost=proposal.cost_usd, judge_cost=judge_cost)
        _record(live, entry, journal)
        done += 1

        verdicts = " ".join(f"{b.benchmark}={b.verdict}" for b in exp.benchmarks)
        print(f"[forge] {exp_id}: score={score.total:.3f} "
              f"(tests={score.tests:.2f} speed={score.speed:.2f} rubric={score.rubric:.2f} "
              f"@ {score.raw_wall_seconds:.0f}s/bench) "
              f"{'ADOPTED ✓' if won else 'discarded ✗'} | {verdicts}", flush=True)

    live.status("stopped", f"done after {done} experiment(s)")
    print(f"[forge] champion: {live.d.get('champion')}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
