import os
import tempfile
import time
import unittest
from pathlib import Path

from forge.agent_cli import isolated_agent_env
from forge.experiment import required_outputs_missing, setup_valinor_dir
from forge.progress import ProgressTracker


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
            self.assertNotIn("VALINOR_PROJECT_ROOT", env)

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
