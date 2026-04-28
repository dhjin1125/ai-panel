from pathlib import Path
import unittest

from ai_panel.server import _allowed_origins, _write_connect_script


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


if __name__ == "__main__":
    unittest.main()
