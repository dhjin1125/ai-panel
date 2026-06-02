from pathlib import Path
import json
import sys
import tempfile
import unittest

from ai_panel.cli import main
from ai_panel.config import AgentConfig, ModelOption, PanelConfig
from ai_panel.panel import run_debate


ARG_AGENT = "import sys; print('ARG:' + sys.argv[1][:200])"
STDIN_AGENT = "import sys; print('STDIN:' + sys.stdin.read()[:200])"


class CliFlowTest(unittest.TestCase):
    def test_debate_writes_expected_outputs_without_real_model_cli(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "agents.yaml"
            topic_path = root / "topic.md"
            runs_dir = root / "runs"
            config_path.write_text(
                json.dumps(
                    {
                        "timeout_seconds": 10,
                        "judge": "arg_agent",
                        "agents": [
                            {
                                "id": "arg_agent",
                                "default_model": "test-model",
                                "models": [{"id": "test-model", "label": "Test Model"}],
                                "model_arg": [],
                                "command": [sys.executable, "-c", ARG_AGENT, "{prompt}"],
                            },
                            {
                                "id": "stdin_agent",
                                "default_model": "test-model",
                                "models": [{"id": "test-model", "label": "Test Model"}],
                                "model_arg": [],
                                "command": [sys.executable, "-c", STDIN_AGENT],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            topic_path.write_text("테스트 논제", encoding="utf-8")

            exit_code = main(
                [
                    "--config",
                    str(config_path),
                    "--runs-dir",
                    str(runs_dir),
                    "debate",
                    str(topic_path),
                ]
            )

            self.assertEqual(exit_code, 0)
            run_dirs = list(runs_dir.iterdir())
            self.assertEqual(len(run_dirs), 1)
            run_dir = run_dirs[0]
            self.assertTrue((run_dir / "round1" / "arg_agent.md").exists())
            self.assertTrue((run_dir / "round1" / "stdin_agent.md").exists())
            self.assertTrue((run_dir / "round2" / "arg_agent_critique.md").exists())
            self.assertTrue((run_dir / "round2" / "stdin_agent_critique.md").exists())
            self.assertTrue((run_dir / "summary.md").exists())
            self.assertTrue((run_dir / "meta.json").exists())

            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertIn("steps", meta)
            self.assertIn("format_checks", meta)
            self.assertNotIn("stdout", meta["round1"][0])
            self.assertNotIn("stderr", meta["round1"][0])
            self.assertIn("stdout_chars", meta["round1"][0])

    def test_cli_applies_preset_and_model_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = root / "agents.yaml"
            topic_path = root / "topic.md"
            runs_dir = root / "runs"
            config_path.write_text(
                json.dumps(
                    {
                        "timeout_seconds": 10,
                        "judge": "agent_a",
                        "presets": [
                            {
                                "id": "deep",
                                "label": "Deep",
                                "mode": "ask",
                                "judge": "agent_b",
                                "models": {
                                    "agent_a": "model_a_deep",
                                    "agent_b": "model_b_deep",
                                },
                            }
                        ],
                        "agents": [
                            {
                                "id": "agent_a",
                                "default_model": "model_a_default",
                                "models": [
                                    {"id": "model_a_default", "label": "A Default"},
                                    {"id": "model_a_deep", "label": "A Deep"},
                                    {"id": "model_a_manual", "label": "A Manual"},
                                ],
                                "model_arg": [],
                                "command": [sys.executable, "-c", "print('A')"],
                            },
                            {
                                "id": "agent_b",
                                "default_model": "model_b_default",
                                "models": [
                                    {"id": "model_b_default", "label": "B Default"},
                                    {"id": "model_b_deep", "label": "B Deep"},
                                ],
                                "model_arg": [],
                                "command": [sys.executable, "-c", "print('B')"],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            topic_path.write_text("테스트 논제", encoding="utf-8")

            exit_code = main(
                [
                    "--config",
                    str(config_path),
                    "--runs-dir",
                    str(runs_dir),
                    "ask",
                    str(topic_path),
                    "--preset",
                    "deep",
                    "--model",
                    "agent_a=model_a_manual",
                ]
            )

            self.assertEqual(exit_code, 0)
            run_dir = next(runs_dir.iterdir())
            meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["preset_id"], "deep")
            self.assertEqual(meta["judge"], "agent_b")
            self.assertEqual(meta["models"]["agent_a"], "model_a_manual")
            self.assertEqual(meta["models"]["agent_b"], "model_b_deep")

    def test_debate_uses_run_level_judge_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_dir = root / "runs"
            config = PanelConfig(
                agents=[
                    AgentConfig(
                        id="judge_a",
                        command=[sys.executable, "-c", "print('A')"],
                        model_arg=["--model", "{model}"],
                        models=[ModelOption(id="test-model", label="Test Model")],
                        default_model="test-model",
                    ),
                    AgentConfig(
                        id="judge_b",
                        command=[sys.executable, "-c", "print('B')"],
                        model_arg=["--model", "{model}"],
                        models=[ModelOption(id="test-model", label="Test Model")],
                        default_model="test-model",
                    ),
                ],
                judge="judge_a",
                timeout_seconds=10,
                presets=[],
            )

            panel_run = run_debate(
                config,
                "테스트 논제",
                runs_dir,
                "test",
                judge_id="judge_b",
            )

        self.assertEqual(panel_run.meta["judge"], "judge_b")
        self.assertEqual(panel_run.meta["summary"]["agent_id"], "judge_b")
        self.assertNotIn("stdout", panel_run.meta["summary"])

    def test_successful_stderr_is_not_step_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_dir = root / "runs"
            config = PanelConfig(
                agents=[
                    AgentConfig(
                        id="noisy",
                        command=[
                            sys.executable,
                            "-c",
                            "import sys; print('ok'); print('debug log', file=sys.stderr)",
                        ],
                        model_arg=[],
                        models=[ModelOption(id="test-model", label="Test Model")],
                        default_model="test-model",
                    )
                ],
                judge="noisy",
                timeout_seconds=10,
                presets=[],
            )

            panel_run = run_debate(config, "테스트 논제", runs_dir, "test")

        self.assertEqual(panel_run.exit_code, 0)
        self.assertIsNone(panel_run.meta["steps"][0]["error"])
        self.assertEqual(panel_run.meta["round1"][0]["stderr_chars"], len("debug log"))

    def test_invalid_preset_does_not_create_run_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_dir = root / "runs"
            config = PanelConfig(
                agents=[
                    AgentConfig(
                        id="judge",
                        command=[sys.executable, "-c", "print('ok')"],
                        model_arg=[],
                        models=[ModelOption(id="test-model", label="Test Model")],
                        default_model="test-model",
                    )
                ],
                judge="judge",
                timeout_seconds=10,
                presets=[],
            )

            with self.assertRaises(ValueError):
                run_debate(config, "테스트 논제", runs_dir, "test", preset_id="missing")

        self.assertFalse(runs_dir.exists())


if __name__ == "__main__":
    unittest.main()
