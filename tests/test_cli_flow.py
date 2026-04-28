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
                                "command": [sys.executable, "-c", ARG_AGENT, "{prompt}"],
                            },
                            {
                                "id": "stdin_agent",
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
                        models=[ModelOption(id="", label="Default")],
                        default_model="",
                    ),
                    AgentConfig(
                        id="judge_b",
                        command=[sys.executable, "-c", "print('B')"],
                        model_arg=["--model", "{model}"],
                        models=[ModelOption(id="", label="Default")],
                        default_model="",
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


if __name__ == "__main__":
    unittest.main()
