from pathlib import Path
import tempfile
import unittest

from ai_panel.config import ConfigError, load_config


class ConfigTest(unittest.TestCase):
    def test_loads_json_compatible_yaml(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "agents.yaml"
            config_path.write_text(
                """
{
  "timeout_seconds": 12,
  "judge": "codex",
  "agents": [
    {"id": "codex", "command": ["codex", "exec", "-"]}
  ]
}
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.timeout_seconds, 12)
        self.assertEqual(config.judge, "codex")
        self.assertEqual(config.agents[0].command, ["codex", "exec", "-"])
        self.assertEqual(config.presets[0].id, "balanced")

    def test_rejects_unknown_judge(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "agents.yaml"
            config_path.write_text(
                """
{
  "judge": "missing",
  "agents": [
    {"id": "codex", "command": ["codex", "exec", "-"]}
  ]
}
""",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_allows_empty_non_executable_argument(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "agents.yaml"
            config_path.write_text(
                """
{
  "judge": "gemini",
  "agents": [
    {"id": "gemini", "command": ["gemini", "--prompt", ""]}
  ]
}
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.agents[0].command, ["gemini", "--prompt", ""])

    def test_loads_presets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "agents.yaml"
            config_path.write_text(
                """
{
  "judge": "codex",
  "presets": [
    {
      "id": "deep",
      "label": "Deep",
      "mode": "debate",
      "judge": "claude",
      "models": {"claude": "opus", "codex": ""}
    }
  ],
  "agents": [
    {
      "id": "claude",
      "default_model": "",
      "models": [{"id": "", "label": "Default"}, {"id": "opus", "label": "Opus"}],
      "command": ["claude", "--print"]
    },
    {"id": "codex", "command": ["codex", "exec", "-"]}
  ]
}
""",
                encoding="utf-8",
            )

            config = load_config(config_path)

        self.assertEqual(config.presets[0].id, "deep")
        self.assertEqual(config.presets[0].judge, "claude")
        self.assertEqual(config.presets[0].models["claude"], "opus")

    def test_rejects_unknown_preset_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "agents.yaml"
            config_path.write_text(
                """
{
  "judge": "codex",
  "presets": [
    {"id": "bad", "models": {"codex": "missing"}}
  ],
  "agents": [
    {"id": "codex", "command": ["codex", "exec", "-"]}
  ]
}
""",
                encoding="utf-8",
            )

            with self.assertRaises(ConfigError):
                load_config(config_path)


if __name__ == "__main__":
    unittest.main()
