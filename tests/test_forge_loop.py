import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from forge.agent_cli import AgentRun, _opencode_logged_error, isolated_agent_env
from forge.dashboard import _HTML, _presentable_live
from forge.experiment import (
    BenchmarkResult,
    ChampionPrompts,
    ExperimentResult,
    ROLE_HARD_GUARDRAILS,
    ROLE_USER_KICKOFF,
    RoleResult,
    TestResult,
    materialize_benchmark_workspace,
    cleanup_planner_scope_leaks,
    cap_auxiliary_handoff_files,
    required_outputs_missing,
    run_role,
    run_one_benchmark,
    setup_valinor_dir,
)
from forge.progress import ProgressTracker
from forge.quality import usable_journal_entries
from forge.scorer import compute_score


class ForgeLoopTests(unittest.TestCase):
    def test_agent_env_prevents_git_root_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "valinor-prompt-forge"
            bench = repo / "benchmarks" / "json-patch"
            bench.mkdir(parents=True)
            os.environ["VALINOR_PROJECT_ROOT"] = str(repo)

            env = isolated_agent_env(bench)

            self.assertEqual(env["GIT_CEILING_DIRECTORIES"], str(bench.parent))
            self.assertEqual(env["VALINOR_FORGE_BENCHMARK_ROOT"], str(bench))
            self.assertEqual(env["VALINOR_PROJECT_ROOT"], str(bench))

    def test_progress_tracker_reports_running_tokens(self) -> None:
        tracker = ProgressTracker("exp-test", ["json-patch"], include_propose=False)
        tracker.start("json-patch/generator")
        time.sleep(0.11)
        tracker.pulse("json-patch/generator", tokens=120)

        progress = tracker.to_dict()
        running = next(step for step in progress["steps"] if step["status"] == "running")

        self.assertEqual(running["tokens"], 120)
        self.assertIsNotNone(running["tps"])
        self.assertGreater(running["seconds"], 0)

    def test_required_outputs_missing_is_role_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = Path(tmp)
            setup_valinor_dir(bench, "benchmark brief")

            missing = required_outputs_missing("planner", bench)
            self.assertIn(".valinor/handoff/spec.md", missing)
            self.assertIn(".valinor/handoff/acceptance.md", missing)

            (bench / ".valinor" / "handoff" / "spec.md").write_text("spec", encoding="utf-8")
            (bench / ".valinor" / "handoff" / "acceptance.md").write_text("acceptance", encoding="utf-8")
            (bench / ".valinor" / "handoff" / "backlog.md").write_text("backlog", encoding="utf-8")

            self.assertEqual(required_outputs_missing("planner", bench), [])

    def test_workspace_materialization_excludes_agent_scratch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "benchmarks" / "sample"
            source.mkdir(parents=True)
            (source / "README.md").write_text("spec", encoding="utf-8")
            (source / "package.json").write_text('{"type":"module"}', encoding="utf-8")
            (source / ".valinor").mkdir()
            (source / ".valinor" / "brief.json").write_text("stale", encoding="utf-8")
            (source / ".opencode").mkdir()
            (source / "src").mkdir()
            (source / "src" / "index.js").write_text("stale generated code", encoding="utf-8")
            (source / "tests" / "fixtures").mkdir(parents=True)
            (source / "tests" / "generated.test.js").write_text("stale test", encoding="utf-8")
            (source / "tests" / "fixtures" / "input.log").write_text("fixture", encoding="utf-8")

            workspace = materialize_benchmark_workspace(source, root / "run" / "sample")

            self.assertTrue((workspace / ".git").exists())
            self.assertTrue((workspace / "README.md").exists())
            self.assertFalse((workspace / ".valinor").exists())
            self.assertFalse((workspace / ".opencode").exists())
            self.assertFalse((workspace / "src" / "index.js").exists())
            self.assertFalse((workspace / "tests" / "generated.test.js").exists())
            self.assertTrue((workspace / "tests" / "fixtures" / "input.log").exists())

    def test_zero_test_scores_are_not_optimization_signal(self) -> None:
        exp = ExperimentResult(
            exp_id="exp-zero",
            started_at=0.0,
            finished_at=10.0,
            benchmarks=[
                BenchmarkResult(
                    benchmark="sample",
                    cycles=1,
                    verdict="fail",
                    test=TestResult(0, 5, 5, raw_stdout="", raw_stderr="all failed"),
                    roles=[],
                )
            ],
        )

        score = compute_score(exp, rubric_score=1.0)

        self.assertEqual(score.total, 0.0)
        self.assertEqual(score.tests, 0.0)
        self.assertEqual(score.speed, 0.0)
        self.assertEqual(score.rubric, 0.0)

    def test_zero_test_entries_are_filtered_from_proposer_journal(self) -> None:
        entries = [
            {"exp_id": "bad", "breakdown": {"tests": 0.0}, "adopted": False},
            {"exp_id": "good", "breakdown": {"tests": 0.25}, "adopted": False},
        ]

        self.assertEqual([e["exp_id"] for e in usable_journal_entries(entries)], ["good"])

    def test_dashboard_keeps_expanded_experiment_across_poll(self) -> None:
        self.assertIn("let openExpId", _HTML)
        self.assertIn("detailCache", _HTML)
        self.assertIn("appendDetailRow", _HTML)
        self.assertNotIn("let openRow", _HTML)

    def test_dashboard_filters_zero_test_history_from_stale_live_json(self) -> None:
        live = _presentable_live({
            "history": [
                {"exp_id": "zero", "score": 0.9, "cost_usd": 1, "adopted": True, "breakdown": {"tests": 0}},
                {"exp_id": "signal", "score": 0.4, "cost_usd": 2, "adopted": False, "breakdown": {"tests": 0.2}},
            ],
            "totals": {},
        })

        self.assertEqual([e["exp_id"] for e in live["history"]], ["signal"])
        self.assertEqual(live["totals"]["experiments"], 1)
        self.assertEqual(live["totals"]["adopted"], 0)
        self.assertEqual(live["best_score"], 0.4)

    def test_dashboard_exposes_stop_button_and_endpoint(self) -> None:
        self.assertIn('id="stop-btn"', _HTML)
        self.assertIn("stopForge", _HTML)
        self.assertIn('fetch("/api/stop"', _HTML)

    def test_generator_and_validator_receive_original_readme_contract(self) -> None:
        self.assertIn("README.md", ROLE_USER_KICKOFF["generator"])
        self.assertIn("README.md", ROLE_USER_KICKOFF["validator"])
        self.assertIn("source of truth", ROLE_HARD_GUARDRAILS["generator"].lower())
        self.assertIn("source of truth", ROLE_HARD_GUARDRAILS["validator"].lower())

    def test_emulated_pass_runs_all_handoffs_and_scores_tests(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_roles: list[str] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, **_kwargs):
            seen_roles.append(role)
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner":
                (handoff / "spec.md").write_text("Implement task API module contract.", encoding="utf-8")
                (handoff / "acceptance.md").write_text("All CRUD, validation, and routing cases pass.", encoding="utf-8")
                (handoff / "backlog.md").write_text("T-1 task API implementation.", encoding="utf-8")
                artifact = "spec"
                final = "Planner wrote spec, acceptance, and backlog."
            elif role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text(TASK_API_PASSING_IMPLEMENTATION, encoding="utf-8")
                (handoff / "build-report.md").write_text("npm test passes for task-api.", encoding="utf-8")
                artifact = "build"
                final = "Generator implemented src/index.js and wrote build report."
            else:
                (handoff / "validation.md").write_text("VERDICT: PASS\nAll task-api checks pass.", encoding="utf-8")
                artifact = "validation"
                final = "VERDICT: PASS\nValidator independently accepted the implementation."
            if on_progress:
                on_progress(10 * len(seen_roles))
            return RoleResult(
                role=role,
                session_id=f"fake-{role}",
                exit_code=0,
                wall_seconds=0.01,
                final_message=final,
                artifact=artifact,
                tokens_out=10,
            )

        with tempfile.TemporaryDirectory() as tmp, patch("forge.experiment.run_role", fake_run_role):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-pass")

        self.assertEqual(seen_roles, ["planner", "generator", "validator"])
        self.assertEqual(result.verdict, "pass")
        self.assertGreater(result.test.total, 0)
        self.assertEqual(result.test.failed, 0)
        self.assertEqual(result.test.passed, result.test.total)

    def test_held_out_test_failures_override_validator_pass_verdict(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, **_kwargs):
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner":
                (handoff / "spec.md").write_text("spec", encoding="utf-8")
                (handoff / "acceptance.md").write_text("acceptance", encoding="utf-8")
                (handoff / "backlog.md").write_text("backlog", encoding="utf-8")
                return RoleResult(role=role, session_id="planner", exit_code=0, wall_seconds=0.01, final_message="", artifact="spec")
            if role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text("export function createStore(){}; export function handleRequest(){};", encoding="utf-8")
                (handoff / "build-report.md").write_text("build", encoding="utf-8")
                return RoleResult(role=role, session_id="generator", exit_code=0, wall_seconds=0.01, final_message="", artifact="build")
            (handoff / "validation.md").write_text("VERDICT: PASS\nstatic pass", encoding="utf-8")
            return RoleResult(role=role, session_id="validator", exit_code=0, wall_seconds=0.01, final_message="VERDICT: PASS\nstatic pass", artifact="validation")

        failing_test = TestResult(10, 11, 21, raw_stdout="", raw_stderr="held-out failures")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("forge.experiment.run_role", fake_run_role),
            patch("forge.experiment.run_tests", return_value=failing_test),
        ):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-hidden-fail")

        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.test.failed, 11)

    def test_missing_planner_backlog_gets_same_role_repair_before_blocking(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_calls: list[tuple[str, str | None]] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, attempt_label=None, user_message=None):
            seen_calls.append((role, attempt_label))
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner" and attempt_label is None:
                (handoff / "spec.md").write_text("spec", encoding="utf-8")
                (handoff / "acceptance.md").write_text("acceptance", encoding="utf-8")
                return RoleResult(role=role, session_id="planner-1", exit_code=0, wall_seconds=0.01, final_message="planner", artifact="spec")
            if role == "planner":
                self.assertIn(".valinor/handoff/backlog.md", user_message or "")
                (handoff / "backlog.md").write_text("backlog", encoding="utf-8")
                return RoleResult(role=role, session_id="planner-repair", exit_code=0, wall_seconds=0.01, final_message="planner repair", artifact="spec")
            if role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text(TASK_API_PASSING_IMPLEMENTATION, encoding="utf-8")
                (handoff / "build-report.md").write_text("build", encoding="utf-8")
                return RoleResult(role=role, session_id="generator", exit_code=0, wall_seconds=0.01, final_message="generator", artifact="build")
            (handoff / "validation.md").write_text("VERDICT: PASS\nok", encoding="utf-8")
            return RoleResult(role=role, session_id="validator", exit_code=0, wall_seconds=0.01, final_message="VERDICT: PASS\nok", artifact="validation")

        with tempfile.TemporaryDirectory() as tmp, patch("forge.experiment.run_role", fake_run_role):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-repair")

        self.assertEqual(seen_calls, [
            ("planner", None),
            ("planner", "repair1"),
            ("generator", None),
            ("validator", None),
        ])
        self.assertNotIn("missing required output", result.test.raw_stderr)
        self.assertEqual(result.verdict, "pass")

    def test_missing_generator_build_report_is_recovered_before_validator(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_calls: list[tuple[str, str | None]] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, attempt_label=None, user_message=None):
            seen_calls.append((role, attempt_label))
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner":
                (handoff / "spec.md").write_text("spec", encoding="utf-8")
                (handoff / "acceptance.md").write_text("acceptance", encoding="utf-8")
                (handoff / "backlog.md").write_text("backlog", encoding="utf-8")
                return RoleResult(role=role, session_id="planner", exit_code=0, wall_seconds=0.01, final_message="planner", artifact="spec")
            if role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text(TASK_API_PASSING_IMPLEMENTATION, encoding="utf-8")
                return RoleResult(role=role, session_id="generator", exit_code=0, wall_seconds=0.01, final_message="", artifact=None)
            build_report = (handoff / "build-report.md").read_text(encoding="utf-8")
            self.assertIn("Forge Recovery", build_report)
            (handoff / "validation.md").write_text("VERDICT: PASS\nok", encoding="utf-8")
            return RoleResult(role=role, session_id="validator", exit_code=0, wall_seconds=0.01, final_message="VERDICT: PASS\nok", artifact="validation")

        def fake_recover(bench_dir):
            report = bench_dir / ".valinor" / "handoff" / "build-report.md"
            report.write_text("# Build Report - Forge Recovery\n", encoding="utf-8")
            return RoleResult(
                role="generator",
                session_id=None,
                exit_code=0,
                wall_seconds=0.01,
                final_message="Forge recovery wrote build report.",
                artifact=report.read_text(encoding="utf-8"),
            )

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("forge.experiment.run_role", fake_run_role),
            patch("forge.experiment.recover_missing_generator_report", fake_recover),
        ):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-generator-recovery")

        self.assertEqual(seen_calls, [
            ("planner", None),
            ("generator", None),
            ("generator", "repair1"),
            ("validator", None),
        ])
        self.assertEqual(result.verdict, "pass")
        self.assertEqual(result.test.failed, 0)

    def test_failed_generator_recovery_skips_validator_and_scores_generated_code(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_roles: list[str] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, attempt_label=None, user_message=None):
            seen_roles.append(role)
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner":
                (handoff / "spec.md").write_text("spec", encoding="utf-8")
                (handoff / "acceptance.md").write_text("acceptance", encoding="utf-8")
                (handoff / "backlog.md").write_text("backlog", encoding="utf-8")
                return RoleResult(role=role, session_id="planner", exit_code=0, wall_seconds=0.01, final_message="", artifact="spec")
            if role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text("export function createStore(){}; export function handleRequest(){};", encoding="utf-8")
                return RoleResult(
                    role=role,
                    session_id="generator",
                    exit_code=None,
                    wall_seconds=0.01,
                    final_message="",
                    artifact=None,
                    error="timeout after 900s",
                )
            self.fail("validator should not run after failed generator recovery")

        recovery = RoleResult(
            role="generator",
            session_id=None,
            exit_code=1,
            wall_seconds=0.01,
            final_message="Forge recovery found failing visible tests.",
            artifact="recovery",
        )
        failing_test = TestResult(1, 2, 3, raw_stdout="", raw_stderr="generated code failed")
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch("forge.experiment.run_role", fake_run_role),
            patch("forge.experiment.recover_missing_generator_report", return_value=recovery),
            patch("forge.experiment.run_tests", return_value=failing_test),
        ):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-generator-fail")

        self.assertEqual(seen_roles, ["planner", "generator"])
        self.assertEqual(result.verdict, "fail")
        self.assertEqual(result.test.total, 3)

    def test_missing_validator_report_is_recovered_before_scoring(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_calls: list[str] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, attempt_label=None, user_message=None):
            seen_calls.append(role)
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner":
                (handoff / "spec.md").write_text("spec", encoding="utf-8")
                (handoff / "acceptance.md").write_text("acceptance", encoding="utf-8")
                (handoff / "backlog.md").write_text("backlog", encoding="utf-8")
                return RoleResult(role=role, session_id="planner", exit_code=0, wall_seconds=0.01, final_message="planner", artifact="spec")
            if role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text(TASK_API_PASSING_IMPLEMENTATION, encoding="utf-8")
                (handoff / "build-report.md").write_text("build", encoding="utf-8")
                return RoleResult(role=role, session_id="generator", exit_code=0, wall_seconds=0.01, final_message="generator", artifact="build")
            return RoleResult(
                role=role,
                session_id="validator",
                exit_code=0,
                wall_seconds=0.01,
                final_message="",
                artifact=None,
                error="Context size has been exceeded",
            )

        with tempfile.TemporaryDirectory() as tmp, patch("forge.experiment.run_role", fake_run_role):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-validator-recovery")

        self.assertEqual(seen_calls, ["planner", "generator", "validator", "validator"])
        self.assertEqual(result.test.failed, 0)
        self.assertTrue(any(r.role == "validator" and "Forge Recovery" in (r.artifact or "") for r in result.roles))

    def test_planner_scope_leaks_are_removed_before_generator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = Path(tmp)
            (bench / "src").mkdir()
            (bench / "src" / "index.js").write_text("planner source", encoding="utf-8")
            (bench / "tests").mkdir()
            (bench / "tests" / "planner.test.js").write_text("planner test", encoding="utf-8")
            (bench / "bin").mkdir()
            (bench / "bin" / "tool.js").write_text("planner bin", encoding="utf-8")
            setup_valinor_dir(bench, "brief")

            removed = cleanup_planner_scope_leaks(bench)

            self.assertEqual(set(removed), {"src", "tests", "bin"})
            self.assertFalse((bench / "src").exists())
            self.assertFalse((bench / "tests").exists())
            self.assertFalse((bench / "bin").exists())
            self.assertTrue((bench / ".valinor" / "handoff").exists())

    def test_runaway_auxiliary_handoff_files_are_capped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bench = Path(tmp)
            setup_valinor_dir(bench, "brief")
            summary = bench / ".valinor" / "handoff" / "summaries.md"
            summary.write_text("x" * 300_000 + "\nLATEST", encoding="utf-8")

            capped = cap_auxiliary_handoff_files(bench)

            self.assertEqual(capped, [".valinor/handoff/summaries.md"])
            self.assertLess(summary.stat().st_size, 200_000)
            self.assertIn("LATEST", summary.read_text(encoding="utf-8"))

    def test_runaway_auxiliary_handoff_cap_does_not_read_entire_file(self) -> None:
        original_read_text = Path.read_text
        with tempfile.TemporaryDirectory() as tmp:
            bench = Path(tmp)
            setup_valinor_dir(bench, "brief")
            summary = bench / ".valinor" / "handoff" / "summaries.md"
            summary.write_text("x" * 300_000 + "\nLATEST", encoding="utf-8")

            def fail_if_summary_read(path_self, *args, **kwargs):
                if Path(path_self).name == "summaries.md":
                    raise AssertionError("compaction must not read the whole runaway file")
                return original_read_text(path_self, *args, **kwargs)

            with patch.object(Path, "read_text", fail_if_summary_read):
                capped = cap_auxiliary_handoff_files(bench)

            self.assertEqual(capped, [".valinor/handoff/summaries.md"])
            self.assertLess(summary.stat().st_size, 200_000)
            self.assertIn("LATEST", summary.read_text(encoding="utf-8"))

    def test_run_role_compacts_auxiliary_handoff_before_agent_context(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        with tempfile.TemporaryDirectory() as tmp:
            bench = Path(tmp) / "bench"
            bench.mkdir()
            setup_valinor_dir(bench, "brief")
            summary = bench / ".valinor" / "handoff" / "summaries.md"
            summary.write_text("x" * 300_000 + "\nLATEST", encoding="utf-8")

            def fake_run_agent(**kwargs):
                live_summary = kwargs["work_dir"] / ".valinor" / "handoff" / "summaries.md"
                self.assertLess(live_summary.stat().st_size, 200_000)
                return AgentRun(
                    final_text="planner done",
                    num_turns=1,
                    cost_usd=0.0,
                    is_error=False,
                    session_id="fake",
                    exit_code=0,
                    wall_seconds=0.01,
                    tokens_out=1,
                )

            with patch("forge.experiment.run_agent", fake_run_agent):
                result = run_role("planner", prompts, bench, Path(tmp) / "run")

            self.assertIsNone(result.error)
            self.assertIn("LATEST", summary.read_text(encoding="utf-8"))

    def test_opencode_context_error_is_reported_from_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "agent.log"
            log.write_text('{"type":"error","error":{"data":{"message":"Context size has been exceeded."}}}', encoding="utf-8")

            self.assertEqual(_opencode_logged_error(log), "Context size has been exceeded")

    def test_context_overflow_triggers_compact_same_role_repair(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_calls: list[tuple[str, str | None]] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, attempt_label=None, user_message=None):
            seen_calls.append((role, attempt_label))
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner" and attempt_label is None:
                return RoleResult(
                    role=role,
                    session_id="planner",
                    exit_code=0,
                    wall_seconds=0.01,
                    final_message="",
                    artifact=None,
                    error="Context size has been exceeded",
                )
            if role == "planner":
                self.assertEqual(attempt_label, "compact1")
                self.assertIn("COMPACT REPAIR", user_message or "")
                (handoff / "spec.md").write_text("spec", encoding="utf-8")
                (handoff / "acceptance.md").write_text("acceptance", encoding="utf-8")
                (handoff / "backlog.md").write_text("backlog", encoding="utf-8")
                return RoleResult(role=role, session_id="planner-compact", exit_code=0, wall_seconds=0.01, final_message="planner compact", artifact="spec")
            if role == "generator":
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text(TASK_API_PASSING_IMPLEMENTATION, encoding="utf-8")
                (handoff / "build-report.md").write_text("build", encoding="utf-8")
                return RoleResult(role=role, session_id="generator", exit_code=0, wall_seconds=0.01, final_message="generator", artifact="build")
            (handoff / "validation.md").write_text("VERDICT: PASS\nok", encoding="utf-8")
            return RoleResult(role=role, session_id="validator", exit_code=0, wall_seconds=0.01, final_message="VERDICT: PASS\nok", artifact="validation")

        with tempfile.TemporaryDirectory() as tmp, patch("forge.experiment.run_role", fake_run_role):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-compact")

        self.assertEqual(seen_calls[:2], [("planner", None), ("planner", "compact1")])
        self.assertEqual(result.test.failed, 0)

    def test_context_overflow_after_complete_handoff_does_not_rewrite_outputs(self) -> None:
        prompts = ChampionPrompts(planner="planner", generator="generator", validator="validator")
        seen_calls: list[tuple[str, str | None]] = []

        def fake_run_role(role, prompts, bench_dir, exp_run_dir, on_progress=None, attempt_label=None, user_message=None):
            seen_calls.append((role, attempt_label))
            handoff = bench_dir / ".valinor" / "handoff"
            handoff.mkdir(parents=True, exist_ok=True)
            if role == "planner":
                self.assertIsNone(attempt_label)
                (handoff / "spec.md").write_text("task-api spec with req.path contract", encoding="utf-8")
                (handoff / "acceptance.md").write_text("task-api acceptance", encoding="utf-8")
                (handoff / "backlog.md").write_text("task-api backlog", encoding="utf-8")
                return RoleResult(
                    role=role,
                    session_id="planner",
                    exit_code=0,
                    wall_seconds=0.01,
                    final_message="",
                    artifact="spec",
                    error="Context size has been exceeded",
                )
            if role == "generator":
                self.assertIn("task-api spec", (handoff / "spec.md").read_text(encoding="utf-8"))
                src = bench_dir / "src"
                src.mkdir(exist_ok=True)
                (src / "index.js").write_text(TASK_API_PASSING_IMPLEMENTATION, encoding="utf-8")
                (handoff / "build-report.md").write_text("build", encoding="utf-8")
                return RoleResult(role=role, session_id="generator", exit_code=0, wall_seconds=0.01, final_message="", artifact="build")
            (handoff / "validation.md").write_text("VERDICT: PASS\nok", encoding="utf-8")
            return RoleResult(role=role, session_id="validator", exit_code=0, wall_seconds=0.01, final_message="VERDICT: PASS\nok", artifact="validation")

        with tempfile.TemporaryDirectory() as tmp, patch("forge.experiment.run_role", fake_run_role):
            result = run_one_benchmark("task-api", prompts, Path(tmp) / "exp-complete-overflow")

        self.assertEqual(seen_calls, [("planner", None), ("generator", None), ("validator", None)])
        self.assertEqual(result.test.failed, 0)


TASK_API_PASSING_IMPLEMENTATION = r"""
export function createStore() {
  return { tasks: [], nextId: 1 };
}

function response(status, body = null) {
  return { status, body };
}

function parseTaskId(path) {
  const match = /^\/tasks\/(\d+)$/.exec(path);
  return match ? Number(match[1]) : null;
}

function validTitle(title) {
  return typeof title === "string" && title.length > 0;
}

function validDone(done) {
  return done === undefined || typeof done === "boolean";
}

export function handleRequest(req, store) {
  const method = req?.method;
  const path = req?.path;
  const body = req?.body || {};

  if (path === "/tasks") {
    if (method === "POST") {
      if (!validTitle(body.title) || !validDone(body.done)) return response(400);
      const task = { id: store.nextId++, title: body.title, done: body.done ?? false };
      store.tasks.push(task);
      return response(201, { ...task });
    }
    if (method === "GET") {
      return response(200, store.tasks.map((task) => ({ ...task })));
    }
    return response(405);
  }

  const id = parseTaskId(path);
  if (id == null) return response(404);
  const idx = store.tasks.findIndex((task) => task.id === id);
  if (idx < 0) {
    return ["GET", "PUT", "DELETE"].includes(method) ? response(404) : response(405);
  }
  const task = store.tasks[idx];
  if (method === "GET") return response(200, { ...task });
  if (method === "PUT") {
    if (body.title !== undefined && !validTitle(body.title)) return response(400);
    if (!validDone(body.done)) return response(400);
    if (body.title !== undefined) task.title = body.title;
    if (body.done !== undefined) task.done = body.done;
    return response(200, { ...task });
  }
  if (method === "DELETE") {
    store.tasks.splice(idx, 1);
    return response(204);
  }
  return response(405);
}
"""
