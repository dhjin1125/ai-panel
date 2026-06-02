import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from ai_panel.server import (
    _allowed_origins,
    _auth_status,
    _index_html,
    _resolve_connect_command,
    _write_connect_script,
)


class ServerHelperTest(unittest.TestCase):
    def test_allowed_origins_include_loopback_aliases(self):
        origins = _allowed_origins("127.0.0.1", 8765)

        self.assertIn("http://127.0.0.1:8765", origins)
        self.assertIn("http://localhost:8765", origins)

    def test_connect_script_uses_unique_temp_path(self):
        first = _write_connect_script("codex", ["echo", "one"])
        second = _write_connect_script("codex", ["echo", "two"])

        try:
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith("ai-panel-connect-codex-"))
            self.assertTrue(Path(first).exists())
            self.assertTrue(Path(second).exists())
        finally:
            Path(first).unlink(missing_ok=True)
            Path(second).unlink(missing_ok=True)

    def test_connect_script_executes_resolved_command_but_displays_friendly_command(self):
        script = _write_connect_script(
            "claude",
            ["/tmp/fake cli/claude", "auth", "login"],
            ["claude", "auth", "login"],
        )

        try:
            content = Path(script).read_text(encoding="utf-8")

            self.assertIn("export PATH=", content)
            self.assertIn("printf '%s\\n' '  claude auth login'", content)
            self.assertIn("'/tmp/fake cli/claude' auth login", content)
        finally:
            Path(script).unlink(missing_ok=True)

    def test_resolve_connect_command_uses_real_path_for_symlinked_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bin_dir = tmp_path / "bin"
            real_dir = tmp_path / "real"
            bin_dir.mkdir()
            real_dir.mkdir()
            target = real_dir / "gemini.js"
            target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            target.chmod(0o755)
            link = bin_dir / "gemini"
            link.symlink_to(target)

            with patch.dict(os.environ, {"PATH": str(bin_dir)}):
                command = _resolve_connect_command(["gemini", "--version"])

            self.assertEqual([str(target.resolve()), "--version"], command)

    def test_codex_auth_status_falls_back_to_auth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            codex_dir = home / ".codex"
            codex_dir.mkdir()
            (codex_dir / "auth.json").write_text(
                '{"auth_mode": "chatgpt", "tokens": {"id_token": "present"}}',
                encoding="utf-8",
            )

            with (
                patch("ai_panel.server.Path.home", return_value=home),
                patch("ai_panel.server.subprocess.run", side_effect=subprocess.TimeoutExpired("codex", 8)),
            ):
                self.assertEqual("ok", _auth_status("codex"))

    def test_gemini_auth_status_falls_back_to_oauth_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            gemini_dir = home / ".gemini"
            gemini_dir.mkdir()
            (gemini_dir / "oauth_creds.json").write_text(
                '{"refresh_token": "present", "access_token": "present"}',
                encoding="utf-8",
            )

            with patch("ai_panel.server.Path.home", return_value=home):
                self.assertEqual("ok", _auth_status("gemini"))

    def test_index_exposes_preset_and_judge_controls(self):
        html = _index_html("test-token")

        self.assertIn('id="presetSelect"', html)
        self.assertIn('id="judgeSelect"', html)
        self.assertIn("preset_id: selectedPresetId()", html)


if __name__ == "__main__":
    unittest.main()
