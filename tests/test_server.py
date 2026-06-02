from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_panel.config import AgentConfig, ModelOption, PanelConfig, PresetConfig
from ai_panel.server import (
    PanelRequestHandler,
    ServerState,
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

    def test_jobs_api_runs_fake_agent_and_exposes_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runs_dir = root / "runs"
            config = PanelConfig(
                agents=[
                    AgentConfig(
                        id="agent_a",
                        command=[
                            sys.executable,
                            "-c",
                            (
                                "print('## 결론\\nOK\\n\\n## 근거\\n테스트\\n\\n"
                                "## 불확실한 점\\n없음\\n\\n## 실행 제안\\n계속')"
                            ),
                        ],
                        model_arg=[],
                        models=[
                            ModelOption(id="default", label="Default"),
                            ModelOption(id="fast-model", label="Fast"),
                        ],
                        default_model="default",
                    )
                ],
                judge="agent_a",
                timeout_seconds=10,
                presets=[
                    PresetConfig(
                        id="fast",
                        label="Fast",
                        mode="ask",
                        judge="agent_a",
                        models={"agent_a": "fast-model"},
                    )
                ],
            )
            state = ServerState(config, runs_dir, "test-token", "127.0.0.1", 8765)

            status, health = _dispatch(state, "GET", "/api/health")
            self.assertEqual(status, 200)
            self.assertTrue(health["ok"])

            status, forbidden = _dispatch(
                state,
                "POST",
                "/api/jobs",
                {"topic": "테스트"},
                token="",
            )
            self.assertEqual(status, 403)
            self.assertIn("error", forbidden)

            status, created = _dispatch(
                state,
                "POST",
                "/api/jobs",
                {"topic": "테스트", "preset_id": "fast"},
                token="test-token",
            )
            self.assertEqual(status, 202)
            job = _wait_for_job(state, created["job"]["id"])

            self.assertEqual(job["status"], "done")
            self.assertEqual(job["preset_id"], "fast")
            self.assertEqual(job["models"]["agent_a"], "fast-model")

            status, runs = _dispatch(state, "GET", "/api/runs")
            self.assertEqual(status, 200)
            self.assertEqual(len(runs["runs"]), 1)
            status, run = _dispatch(state, "GET", f"/api/runs/{runs['runs'][0]['id']}")
            self.assertEqual(status, 200)
            self.assertEqual(run["run"]["preset_id"], "fast")
            self.assertEqual(run["run"]["models"]["agent_a"], "fast-model")
            self.assertIn("round1/agent_a.md", {item["path"] for item in run["run"]["files"]})


def _dispatch(
    state: ServerState,
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
) -> tuple[int, dict]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
    }
    if token is not None:
        headers["X-AI-Panel-Token"] = token

    class Handler(PanelRequestHandler):
        server_state = state

        def __init__(self) -> None:
            self.path = path
            self.headers = headers
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.status = 0
            self.response_headers = {}

        def send_response(self, code, message=None) -> None:
            self.status = int(code)

        def send_header(self, key: str, value: str) -> None:
            self.response_headers[key] = value

        def end_headers(self) -> None:
            pass

        def log_message(self, format: str, *args) -> None:
            pass

    handler = Handler()
    if method == "GET":
        handler.do_GET()
    elif method == "POST":
        handler.do_POST()
    else:
        raise ValueError(f"unsupported method: {method}")
    return handler.status, json.loads(handler.wfile.getvalue().decode("utf-8"))


def _wait_for_job(state: ServerState, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = state.get_job(job_id)
        if job is not None and job.status not in {"queued", "running"}:
            return {
                "status": job.status,
                "preset_id": job.preset_id,
                "models": job.models,
            }
        time.sleep(0.05)
    raise AssertionError("job did not finish")


if __name__ == "__main__":
    unittest.main()
