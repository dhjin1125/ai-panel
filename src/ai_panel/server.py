from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import platform
from pathlib import Path
import secrets
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
from urllib.parse import unquote, urlparse
from uuid import uuid4
import webbrowser

from ai_panel.config import PanelConfig
from ai_panel.panel import run_ask, run_debate


MAX_TOPIC_BYTES = 1_000_000
MAX_ACTIVE_JOBS = 3
MAX_JOBS_IN_MEMORY = 100
STATUS_TIMEOUT_SECONDS = 8

CONNECT_COMMANDS = {
    "claude": ["claude", "auth", "login"],
    "gemini": ["gemini"],
    "codex": ["codex", "login"],
}

CONNECT_PATHS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
    str(Path.home() / ".local/bin"),
    "/Applications/cmux.app/Contents/Resources/bin",
]

STATUS_COMMANDS = {
    "claude": ["claude", "auth", "status"],
    "codex": ["codex", "login", "status"],
}


@dataclass
class Job:
    id: str
    mode: str
    models: dict[str, str]
    judge: str
    preset_id: str | None
    preset_label: str | None
    status: str
    created_at: str
    updated_at: str
    steps: list[dict]
    run_id: str | None = None
    run_path: str | None = None
    error: str | None = None


class ServerState:
    def __init__(self, config: PanelConfig, runs_dir: Path, token: str, host: str, port: int):
        self.config = config
        self.runs_dir = runs_dir
        self.token = token
        self.allowed_origins = _allowed_origins(host, port)
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()

    def create_job(
        self,
        mode: str | None,
        topic: str,
        model_by_agent: dict[str, str] | None = None,
        judge_id: str | None = None,
        preset_id: str | None = None,
    ) -> Job:
        preset = _preset_by_id(self.config, preset_id)
        selected_mode = mode or (preset["mode"] if preset else "debate")
        if selected_mode not in {"ask", "debate"}:
            raise ValueError("mode는 ask 또는 debate여야 합니다.")
        selected_models = _selected_models(self.config, model_by_agent, preset)
        selected_judge = _selected_judge(self.config, judge_id, preset)
        preset_label = preset["label"] if preset else None
        steps = _initial_steps(self.config, selected_mode, selected_models, selected_judge)
        job = Job(
            id=uuid4().hex[:12],
            mode=selected_mode,
            models=selected_models,
            judge=selected_judge,
            preset_id=preset["id"] if preset else None,
            preset_label=preset_label,
            status="queued",
            created_at=_now(),
            updated_at=_now(),
            steps=steps,
        )
        with self.lock:
            self._prune_jobs_locked()
            if self._active_job_count_locked() >= MAX_ACTIVE_JOBS:
                raise RuntimeError("동시에 실행 중인 작업이 너무 많습니다. 잠시 후 다시 실행하세요.")
            self.jobs[job.id] = job
        thread = threading.Thread(
            target=self._run_job,
            args=(job.id, selected_mode, topic, selected_models, selected_judge, job.preset_id, preset_label),
            daemon=True,
        )
        thread.start()
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self.lock:
            return self.jobs.get(job_id)

    def _run_job(
        self,
        job_id: str,
        mode: str,
        topic: str,
        model_by_agent: dict[str, str],
        judge_id: str,
        preset_id: str | None,
        preset_label: str | None,
    ) -> None:
        self._update_job(job_id, status="running")
        try:
            def status_callback(stage: str, agent_id: str, status: str, payload: dict) -> None:
                self._update_step(job_id, stage, agent_id, status, payload)

            if mode == "ask":
                panel_run = run_ask(
                    self.config,
                    topic,
                    self.runs_dir,
                    "web input",
                    model_by_agent,
                    judge_id,
                    preset_id,
                    status_callback,
                )
            elif mode == "debate":
                panel_run = run_debate(
                    self.config,
                    topic,
                    self.runs_dir,
                    "web input",
                    model_by_agent,
                    judge_id,
                    preset_id,
                    status_callback,
                )
            else:
                raise ValueError(f"지원하지 않는 mode입니다: {mode}")

            status = "done" if panel_run.exit_code == 0 else "done_with_errors"
            self._update_job(
                job_id,
                status=status,
                run_id=panel_run.run_dir.name,
                run_path=str(panel_run.run_dir),
            )
        except Exception as exc:  # noqa: BLE001
            self._update_job(
                job_id,
                status="failed",
                error=str(exc),
            )

    def _update_job(self, job_id: str, **updates) -> None:
        with self.lock:
            job = self.jobs[job_id]
            for key, value in updates.items():
                setattr(job, key, value)
            job.updated_at = _now()

    def _update_step(
        self,
        job_id: str,
        stage: str,
        agent_id: str,
        status: str,
        payload: dict,
    ) -> None:
        with self.lock:
            job = self.jobs[job_id]
            step = next(
                (
                    item
                    for item in job.steps
                    if item.get("stage") == stage and item.get("agent_id") == agent_id
                ),
                None,
            )
            if step is None:
                step = {"stage": stage, "agent_id": agent_id}
                job.steps.append(step)
            step.update(payload)
            step["status"] = status
            step["updated_at"] = _now()
            if status == "running":
                step.setdefault("started_at", _now())
            job.updated_at = _now()

    def _active_job_count_locked(self) -> int:
        return sum(1 for job in self.jobs.values() if job.status in {"queued", "running"})

    def _prune_jobs_locked(self) -> None:
        if len(self.jobs) < MAX_JOBS_IN_MEMORY:
            return
        removable = sorted(
            (
                job
                for job in self.jobs.values()
                if job.status not in {"queued", "running"}
            ),
            key=lambda job: job.updated_at,
        )
        for job in removable[: max(0, len(self.jobs) - MAX_JOBS_IN_MEMORY + 1)]:
            self.jobs.pop(job.id, None)


def serve(
    config: PanelConfig,
    runs_dir: Path,
    host: str,
    port: int,
    open_browser: bool = True,
) -> None:
    token = secrets.token_urlsafe(24)
    state = ServerState(config, runs_dir, token, host, port)

    class Handler(PanelRequestHandler):
        server_state = state

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}"
    print(f"ai-panel web UI: {url}")
    print("웹 UI 토큰은 현재 서버 프로세스 안에서만 사용됩니다.")
    print("중단하려면 Ctrl+C를 누르세요.")
    if open_browser:
        webbrowser.open(url)
    httpd.serve_forever()


class PanelRequestHandler(BaseHTTPRequestHandler):
    server_state: ServerState
    server_version = "AIPanelHTTP/0.1"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(_index_html(self.server_state.token))
            return
        if path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "agents": [agent.id for agent in self.server_state.config.agents],
                    "judge": self.server_state.config.judge,
                    "runs_dir": str(self.server_state.runs_dir),
                }
            )
            return
        if path == "/api/config":
            self._send_json(self._config_payload())
            return
        if path == "/api/runs":
            self._send_json({"runs": self._list_runs()})
            return
        if path == "/api/agents":
            self._send_json({"agents": self._agent_statuses()})
            return
        if path.startswith("/api/jobs/"):
            job_id = unquote(path.removeprefix("/api/jobs/"))
            job = self.server_state.get_job(job_id)
            if job is None:
                self._send_error(HTTPStatus.NOT_FOUND, "job을 찾을 수 없습니다.")
                return
            self._send_json({"job": asdict(job)})
            return
        if path.startswith("/api/runs/"):
            run_id = unquote(path.removeprefix("/api/runs/"))
            run_dir = self._run_dir(run_id)
            if run_dir is None:
                self._send_error(HTTPStatus.NOT_FOUND, "run을 찾을 수 없습니다.")
                return
            self._send_json({"run": self._read_run(run_dir)})
            return
        self._send_error(HTTPStatus.NOT_FOUND, "요청 경로를 찾을 수 없습니다.")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not self._valid_mutation_request():
            self._send_error(HTTPStatus.FORBIDDEN, "허용되지 않은 요청입니다.")
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/rerun"):
            self._rerun(parsed.path)
            return
        if parsed.path.startswith("/api/agents/") and parsed.path.endswith("/connect"):
            self._connect_agent(parsed.path)
            return
        if parsed.path != "/api/jobs":
            self._send_error(HTTPStatus.NOT_FOUND, "요청 경로를 찾을 수 없습니다.")
            return
        try:
            payload = self._read_json()
            mode = payload.get("mode")
            topic = payload.get("topic")
            model_by_agent = payload.get("models", {})
            judge_id = payload.get("judge")
            preset_id = payload.get("preset_id")
            if mode is not None and mode not in {"ask", "debate"}:
                raise ValueError("mode는 ask 또는 debate여야 합니다.")
            if not isinstance(topic, str) or not topic.strip():
                raise ValueError("논제를 입력해야 합니다.")
            if not isinstance(model_by_agent, dict):
                raise ValueError("models는 object여야 합니다.")
            if judge_id is not None and not isinstance(judge_id, str):
                raise ValueError("judge는 string이어야 합니다.")
            if preset_id is not None and not isinstance(preset_id, str):
                raise ValueError("preset_id는 string이어야 합니다.")
            job = self.server_state.create_job(
                mode,
                topic.strip(),
                model_by_agent,
                judge_id,
                preset_id,
            )
            self._send_json({"job": asdict(job)}, status=HTTPStatus.ACCEPTED)
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, str(exc))

    def _connect_agent(self, path: str) -> None:
        agent_id = unquote(path.removeprefix("/api/agents/").removesuffix("/connect"))
        agent_ids = {agent.id for agent in self.server_state.config.agents}
        if agent_id not in agent_ids:
            self._send_error(HTTPStatus.NOT_FOUND, "agent를 찾을 수 없습니다.")
            return
        command = CONNECT_COMMANDS.get(agent_id)
        if command is None:
            self._send_error(HTTPStatus.BAD_REQUEST, "이 agent는 연동 명령이 없습니다.")
            return
        resolved_command = _resolve_connect_command(command)
        if resolved_command is None:
            self._send_error(HTTPStatus.BAD_REQUEST, f"실행 파일을 찾을 수 없습니다: {command[0]}")
            return
        try:
            script_path = _write_connect_script(agent_id, resolved_command, command)
            if platform.system() == "Darwin":
                subprocess.Popen(["open", str(script_path)])
            else:
                self._send_error(
                    HTTPStatus.BAD_REQUEST,
                    f"자동 터미널 열기는 macOS만 지원합니다. 직접 실행하세요: {shlex.join(command)}",
                )
                return
            self._send_json({"ok": True, "agent": agent_id, "command": shlex.join(command)})
        except OSError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def _rerun(self, path: str) -> None:
        run_id = unquote(path.removeprefix("/api/runs/").removesuffix("/rerun"))
        run_dir = self._run_dir(run_id)
        if run_dir is None:
            self._send_error(HTTPStatus.NOT_FOUND, "run을 찾을 수 없습니다.")
            return
        try:
            topic = (run_dir / "topic.md").read_text(encoding="utf-8").strip()
            meta = _read_json_file(run_dir / "meta.json")
            mode = meta.get("mode", "debate")
            model_by_agent = meta.get("models", {})
            judge_id = meta.get("judge")
            preset_id = meta.get("preset_id")
            if mode not in {"ask", "debate"}:
                mode = "debate"
            if not isinstance(model_by_agent, dict):
                model_by_agent = {}
            if not isinstance(judge_id, str):
                judge_id = None
            if not isinstance(preset_id, str):
                preset_id = None
            if not topic:
                raise ValueError("저장된 topic이 비어 있습니다.")
            job = self.server_state.create_job(mode, topic, model_by_agent, judge_id, preset_id)
            self._send_json({"job": asdict(job)}, status=HTTPStatus.ACCEPTED)
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except RuntimeError as exc:
            self._send_error(HTTPStatus.TOO_MANY_REQUESTS, str(exc))
        except OSError as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def log_message(self, format: str, *args) -> None:
        print(f"{self.address_string()} - {format % args}")

    def _valid_mutation_request(self) -> bool:
        token = self.headers.get("X-AI-Panel-Token", "")
        if not secrets.compare_digest(token, self.server_state.token):
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in self.server_state.allowed_origins:
            return False
        return True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            raise ValueError("요청 본문이 비어 있습니다.")
        if length > MAX_TOPIC_BYTES:
            raise ValueError("요청 본문이 너무 큽니다.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("JSON 요청만 지원합니다.") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON object를 보내야 합니다.")
        return payload

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status=status)

    def _list_runs(self) -> list[dict]:
        runs_dir = self.server_state.runs_dir
        if not runs_dir.exists():
            return []
        runs = []
        for path in sorted(runs_dir.iterdir(), reverse=True):
            if path.is_dir():
                meta_path = path / "meta.json"
                topic_path = path / "topic.md"
                mode = None
                preset_label = None
                topic = path.name
                if meta_path.exists():
                    try:
                        meta = json.loads(meta_path.read_text(encoding="utf-8"))
                        mode = meta.get("mode")
                        preset_label = meta.get("preset_label")
                    except json.JSONDecodeError:
                        mode = None
                        preset_label = None
                if topic_path.exists():
                    topic = _topic_label(topic_path.read_text(encoding="utf-8", errors="replace"))
                runs.append(
                    {
                        "id": path.name,
                        "path": str(path),
                        "mode": mode,
                        "preset_label": preset_label,
                        "topic": topic,
                    }
                )
        return runs

    def _agent_statuses(self) -> list[dict]:
        statuses = []
        for agent in self.server_state.config.agents:
            executable = agent.command[0]
            connect_command = CONNECT_COMMANDS.get(agent.id)
            health = _agent_health(agent.id, executable, self.server_state.runs_dir)
            statuses.append(
                {
                    "id": agent.id,
                    "installed": shutil.which(executable) is not None,
                    "executable": executable,
                    "connect_command": shlex.join(connect_command) if connect_command else None,
                    "default_model": agent.default_model,
                    "models": [asdict(model) for model in agent.models],
                    "status": health["status"],
                    "status_label": health["label"],
                    "status_detail": health["detail"],
                }
            )
        return statuses

    def _config_payload(self) -> dict:
        return {
            "agents": [agent.id for agent in self.server_state.config.agents],
            "judge": self.server_state.config.judge,
            "judges": [agent.id for agent in self.server_state.config.agents],
            "presets": [asdict(preset) for preset in self.server_state.config.presets],
        }

    def _run_dir(self, run_id: str) -> Path | None:
        if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
            return None
        run_dir = self.server_state.runs_dir / run_id
        if not run_dir.is_dir():
            return None
        return run_dir

    def _read_run(self, run_dir: Path) -> dict:
        files = []
        for path in sorted(run_dir.rglob("*")):
            if path.is_file() and path.suffix in {".md", ".json"}:
                files.append(
                    {
                        "path": str(path.relative_to(run_dir)),
                        "content": path.read_text(encoding="utf-8", errors="replace"),
                    }
                )
        topic = ""
        topic_path = run_dir / "topic.md"
        if topic_path.exists():
            topic = topic_path.read_text(encoding="utf-8", errors="replace").strip()
        meta = _read_json_file(run_dir / "meta.json")
        return {
            "id": run_dir.name,
            "path": str(run_dir),
            "mode": meta.get("mode"),
            "preset_id": meta.get("preset_id"),
            "preset_label": meta.get("preset_label"),
            "judge": meta.get("judge"),
            "models": meta.get("models", {}),
            "topic": topic,
            "topic_label": _topic_label(topic),
            "failures": _extract_failures(meta),
            "steps": meta.get("steps", []),
            "format_checks": meta.get("format_checks", {}),
            "files": files,
        }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _selected_models(
    config: PanelConfig,
    model_by_agent: dict[str, str] | None,
    preset: dict | None = None,
) -> dict[str, str]:
    requested = {**((preset or {}).get("models", {})), **(model_by_agent or {})}
    selected = {}
    for agent in config.agents:
        value = requested.get(agent.id, agent.default_model)
        model_ids = {model.id for model in agent.models}
        selected[agent.id] = value if isinstance(value, str) and value in model_ids else agent.default_model
    return selected


def _selected_judge(
    config: PanelConfig,
    judge_id: str | None,
    preset: dict | None = None,
) -> str:
    requested = judge_id or ((preset or {}).get("judge")) or config.judge
    agent_ids = {agent.id for agent in config.agents}
    if requested not in agent_ids:
        raise ValueError("judge가 agents 목록에 없습니다.")
    return requested


def _preset_by_id(config: PanelConfig, preset_id: str | None) -> dict | None:
    if not preset_id:
        return None
    for preset in config.presets:
        if preset.id == preset_id:
            return asdict(preset)
    raise ValueError("preset_id를 찾을 수 없습니다.")


def _initial_steps(
    config: PanelConfig,
    mode: str,
    models: dict[str, str],
    judge: str,
) -> list[dict]:
    steps = [
        {
            "stage": "round1",
            "agent_id": agent.id,
            "model": models.get(agent.id, ""),
            "status": "pending",
        }
        for agent in config.agents
    ]
    if mode == "debate":
        steps.extend(
            {
                "stage": "round2",
                "agent_id": agent.id,
                "model": models.get(agent.id, ""),
                "status": "pending",
            }
            for agent in config.agents
        )
        steps.append(
            {
                "stage": "summary",
                "agent_id": judge,
                "model": models.get(judge, ""),
                "status": "pending",
            }
        )
    return steps


def _allowed_origins(host: str, port: int) -> set[str]:
    origins = {f"http://{host}:{port}"}
    if host in {"127.0.0.1", "localhost"}:
        origins.update({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})
    return origins


def _agent_health(agent_id: str, executable: str, runs_dir: Path) -> dict[str, str]:
    if shutil.which(executable) is None:
        return {
            "status": "missing",
            "label": "미설치",
            "detail": f"실행 파일을 찾을 수 없습니다: {executable}",
        }

    auth = _auth_status(agent_id)
    recent = _recent_agent_result(runs_dir, agent_id)
    if auth == "ok":
        return {"status": "ok", "label": "정상", "detail": "CLI 로그인 상태 확인됨"}
    if recent == "ok":
        return {"status": "ok", "label": "최근 성공", "detail": "최근 실행이 성공했습니다"}
    if recent == "error":
        return {"status": "error", "label": "오류", "detail": "최근 실행이 실패했습니다"}
    if auth == "error":
        return {"status": "error", "label": "연동 필요", "detail": "CLI 로그인 상태 확인 실패"}
    return {"status": "unknown", "label": "확인 필요", "detail": "아직 실행 기록이 없거나 상태 명령이 없습니다"}


def _auth_status(agent_id: str) -> str:
    command = STATUS_COMMANDS.get(agent_id)
    if command is None:
        return _auth_status_fallback(agent_id) or "unknown"
    try:
        resolved_command = _resolve_cli_command(command)
        if resolved_command is None:
            return _auth_status_fallback(agent_id) or "error"
        result = subprocess.run(
            resolved_command,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_SECONDS,
            check=False,
            env=_subprocess_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return _auth_status_fallback(agent_id) or "error"

    output = f"{result.stdout}\n{result.stderr}".lower()
    if agent_id == "claude" and '"loggedin":false' in output.replace(" ", ""):
        return "error"
    if result.returncode == 0:
        return "ok"
    fallback = _auth_status_fallback(agent_id)
    if fallback:
        return fallback
    return "error"


def _auth_status_fallback(agent_id: str) -> str | None:
    if agent_id == "gemini":
        return _gemini_auth_file_status()
    if agent_id == "codex":
        return _codex_auth_file_status()
    return None


def _gemini_auth_file_status() -> str | None:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "ok"

    auth_path = Path.home() / ".gemini" / "oauth_creds.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    has_refresh_token = bool(data.get("refresh_token"))
    has_access_token = bool(data.get("access_token"))
    return "ok" if has_refresh_token or has_access_token else None


def _codex_auth_file_status() -> str | None:
    auth_path = Path.home() / ".codex" / "auth.json"
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    has_api_key = bool(data.get("OPENAI_API_KEY"))
    has_tokens = isinstance(data.get("tokens"), dict) and bool(data["tokens"])
    return "ok" if has_api_key or has_tokens else None


def _recent_agent_result(runs_dir: Path, agent_id: str) -> str:
    if not runs_dir.exists():
        return "unknown"
    for run_dir in sorted((path for path in runs_dir.iterdir() if path.is_dir()), reverse=True):
        meta = _read_json_file(run_dir / "meta.json")
        for section in ("summary", "round2", "round1"):
            values = meta.get(section)
            if isinstance(values, dict):
                values = [values]
            if not isinstance(values, list):
                continue
            for item in values:
                if not isinstance(item, dict) or item.get("agent_id") != agent_id:
                    continue
                ok = item.get("exit_code") == 0 and not item.get("timed_out") and not item.get("error")
                return "ok" if ok else "error"
    return "unknown"


def _read_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _topic_label(topic: str) -> str:
    compact = " ".join(topic.strip().split())
    if not compact:
        return "(제목 없음)"
    return compact[:80] + ("..." if len(compact) > 80 else "")


def _extract_failures(meta: dict) -> list[dict]:
    failures = []
    for section in ("round1", "round2"):
        values = meta.get(section, [])
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            ok = item.get("exit_code") == 0 and not item.get("timed_out") and not item.get("error")
            if ok:
                continue
            failures.append(
                {
                    "agent": item.get("agent_id", "unknown"),
                    "section": section,
                    "exit_code": item.get("exit_code"),
                    "timed_out": item.get("timed_out"),
                    "error": item.get("error")
                    or item.get("stderr_preview")
                    or item.get("stderr")
                    or "실행 실패",
                }
            )
    summary = meta.get("summary")
    if isinstance(summary, dict):
        ok = summary.get("exit_code") == 0 and not summary.get("timed_out") and not summary.get("error")
        if not ok:
            failures.append(
                {
                    "agent": summary.get("agent_id", "summary"),
                    "section": "summary",
                    "exit_code": summary.get("exit_code"),
                    "timed_out": summary.get("timed_out"),
                    "error": summary.get("error")
                    or summary.get("stderr_preview")
                    or summary.get("stderr")
                    or "요약 실패",
                }
            )
    return failures


def _connect_path_env() -> str:
    paths = []
    for raw_path in [*os.environ.get("PATH", "").split(os.pathsep), *CONNECT_PATHS]:
        path = raw_path.strip()
        if path and path not in paths:
            paths.append(path)
    return os.pathsep.join(paths)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = _connect_path_env()
    return env


def _resolve_cli_command(command: list[str]) -> list[str] | None:
    executable_path = shutil.which(command[0], path=_connect_path_env())
    if executable_path is None:
        return None
    return [str(Path(executable_path).resolve()), *command[1:]]


def _resolve_connect_command(command: list[str]) -> list[str] | None:
    return _resolve_cli_command(command)


def _write_connect_script(
    agent_id: str,
    command: list[str],
    display_command: list[str] | None = None,
) -> Path:
    fd, raw_path = tempfile.mkstemp(
        prefix=f"ai-panel-connect-{agent_id}-",
        suffix=".command",
    )
    script_path = Path(raw_path)
    command_text = shlex.join(command)
    display_text = shlex.join(display_command or command)
    path_text = _connect_path_env()
    display_line = shlex.quote(f"  {display_text}")
    script = f"""#!/usr/bin/env bash
clear
export PATH={shlex.quote(path_text)}
echo "AI Panel - {agent_id} 연동"
echo
echo "실행할 명령:"
printf '%s\\n' {display_line}
echo
{command_text}
status=$?
echo
if [ $status -eq 0 ]; then
  echo "{agent_id} 연동 명령이 종료되었습니다."
else
  echo "{agent_id} 연동 명령이 실패했습니다. exit code: $status"
fi
echo
echo "이 창을 닫고 AI Panel에서 같은 토픽 다시 실행을 누르세요."
read -r -p "Enter를 누르면 창을 닫습니다..."
"""
    with open(fd, "w", encoding="utf-8") as handle:
        handle.write(script)
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR)
    return script_path


def _index_html(token: str) -> str:
    return INDEX_HTML.replace("__AI_PANEL_TOKEN__", token)


INDEX_HTML = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Panel</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f8f7f3;
      --panel: #fffdfa;
      --surface: #ffffff;
      --surface-muted: #faf9f5;
      --line: #e5e1d8;
      --line-strong: #d7d1c5;
      --text: #191816;
      --muted: #6f6a62;
      --faint: #9a9489;
      --accent: #c96442;
      --accent-strong: #ad5437;
      --danger: #b42318;
      --ok: #0f8f61;
      --warn: #a86413;
      --soft-warn: #fff7ed;
      --shadow: rgba(25, 24, 22, 0.04) 0 10px 28px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 12px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .brand {
      display: flex;
      flex-direction: column;
      gap: 2px;
      min-width: 0;
    }
    h1 {
      font-size: 18px;
      margin: 0;
      font-weight: 650;
      line-height: 1.1;
    }
    .brand-kicker {
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }
    main {
      display: grid;
      grid-template-columns: 372px minmax(0, 1fr);
      min-height: calc(100vh - 61px);
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      padding: 18px;
      overflow: auto;
    }
    section {
      padding: 22px;
      overflow: auto;
    }
    label {
      display: block;
      font-size: 13px;
      font-weight: 650;
      margin-bottom: 8px;
    }
    textarea {
      width: 100%;
      min-height: 240px;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--surface);
      box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.45);
    }
    textarea:focus {
      outline: 2px solid rgba(201, 100, 66, 0.16);
      border-color: var(--accent);
    }
    .control-label {
      margin-top: 12px;
    }
    .control-select {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      padding: 0 8px;
      font-size: 13px;
    }
    .control-select:focus {
      outline: 2px solid rgba(201, 100, 66, 0.16);
      border-color: var(--accent);
    }
    .mode-group {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 10px;
    }
    .mode-option {
      display: flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 10px;
      background: var(--surface);
      font-size: 13px;
      font-weight: 650;
      cursor: pointer;
    }
    .mode-option input { margin: 0; }
    button {
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      font-weight: 650;
      cursor: pointer;
    }
    button.primary {
      width: 100%;
      margin-top: 8px;
      background: var(--accent);
      color: #fff;
      border-color: var(--accent);
    }
    button:hover { border-color: var(--accent); }
    button.primary:hover { background: var(--accent-strong); }
    button:disabled {
      cursor: not-allowed;
      opacity: .55;
    }
    .status {
      margin-top: 12px;
      min-height: 20px;
      font-size: 13px;
      color: var(--muted);
    }
    .status.ok { color: var(--ok); }
    .status.warn { color: var(--warn); }
    .status.error { color: var(--danger); }
    .job-steps {
      display: grid;
      gap: 5px;
      margin-top: 8px;
    }
    .step-row {
      display: grid;
      grid-template-columns: 74px minmax(0, 1fr) auto;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 7px;
      background: var(--surface);
      font-size: 12px;
    }
    .step-row strong {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
    }
    .step-row span {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .runs-title {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 22px;
      margin-bottom: 8px;
      gap: 8px;
    }
    .runs-title h2 {
      font-size: 14px;
      margin: 0;
    }
    .connect-panel {
      margin-top: 16px;
      border-top: 1px solid var(--line);
      padding-top: 14px;
    }
    .connect-panel h2 {
      font-size: 14px;
      margin: 0 0 8px;
    }
    .agent-list {
      display: grid;
      gap: 6px;
    }
    .agent-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto auto;
      align-items: center;
      gap: 8px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: var(--surface-muted);
    }
    .agent-name {
      min-width: 0;
      font-size: 13px;
      font-weight: 650;
    }
    .agent-name small {
      display: block;
      color: var(--muted);
      margin-top: 2px;
      font-weight: 500;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .connect-button {
      width: auto;
      height: 30px;
      padding: 0 10px;
      font-size: 12px;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      height: 24px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: var(--surface);
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      white-space: nowrap;
    }
    .status-pill.ok {
      border-color: rgba(15, 143, 97, 0.25);
      background: #f0fbf6;
      color: var(--ok);
    }
    .status-pill.error,
    .status-pill.missing {
      border-color: rgba(180, 35, 24, 0.24);
      background: #fff1f0;
      color: var(--danger);
    }
    .status-pill.unknown {
      border-color: rgba(168, 100, 19, 0.24);
      background: #fff7ed;
      color: var(--warn);
    }
    .model-select {
      grid-column: 1 / -1;
      width: 100%;
      min-height: 34px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--surface);
      color: var(--text);
      padding: 0 8px;
      font-size: 12px;
    }
    .model-select:focus {
      outline: 2px solid rgba(201, 100, 66, 0.16);
      border-color: var(--accent);
    }
    .run-list {
      display: grid;
      gap: 6px;
    }
    .run-item {
      width: 100%;
      text-align: left;
      height: auto;
      min-height: 52px;
      padding: 10px 11px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 650;
      line-height: 1.35;
      background: var(--surface);
    }
    .run-item small {
      display: block;
      color: var(--muted);
      margin-top: 4px;
      font-weight: 500;
    }
    .file-tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin: 14px 0 12px;
    }
    .file-tab {
      width: auto;
      padding: 0 12px;
      font-size: 13px;
    }
    .file-tab.active {
      border-color: var(--accent);
      color: var(--accent);
      background: #fff7f1;
    }
    .result-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      margin-bottom: 14px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }
    .result-head h2 {
      font-size: 20px;
      margin: 0 0 4px;
      line-height: 1.3;
    }
    .path {
      color: var(--muted);
      font-size: 12px;
      word-break: break-all;
    }
    .plain-button {
      width: auto;
      padding: 0 12px;
      white-space: nowrap;
    }
    .failure-panel {
      border: 1px solid #fed7aa;
      border-radius: 8px;
      background: var(--soft-warn);
      padding: 13px 14px;
      margin: 12px 0;
      color: #7c2d12;
      font-size: 13px;
    }
    .failure-panel strong { display: block; margin-bottom: 6px; }
    .failure-panel code {
      display: inline-block;
      background: #fff;
      border: 1px solid #fed7aa;
      border-radius: 6px;
      padding: 2px 6px;
      margin: 2px 3px 2px 0;
      color: #111827;
    }
    .failure-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .summary-box {
      margin-bottom: 14px;
    }
    .summary-box h3,
    .compare-card h3 {
      font-size: 14px;
      margin: 0;
      color: var(--text);
      font-weight: 700;
    }
    .compare-card h3 small {
      display: block;
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 500;
    }
    .summary-box h3 {
      margin-bottom: 8px;
    }
    .compare-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(260px, 1fr));
      gap: 14px;
      align-items: stretch;
    }
    .compare-card {
      min-width: 0;
      display: flex;
      flex-direction: column;
      gap: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      overflow: hidden;
    }
    .card-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 12px 13px;
      border-bottom: 1px solid var(--line);
      background: var(--surface-muted);
    }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      padding: 0 8px;
      border-radius: 999px;
      border: 1px solid var(--line);
      color: var(--muted);
      background: var(--surface);
      font-size: 12px;
      font-weight: 650;
    }
    .badge.done,
    .badge.ok {
      border-color: rgba(15, 143, 97, 0.25);
      background: #f0fbf6;
      color: var(--ok);
    }
    .badge.running {
      border-color: rgba(168, 100, 19, 0.24);
      background: #fff7ed;
      color: var(--warn);
    }
    .badge.failed {
      border-color: rgba(180, 35, 24, 0.24);
      background: #fff1f0;
      color: var(--danger);
    }
    .badge.pending,
    .badge.skipped {
      color: var(--muted);
    }
    .markdown-body {
      margin: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      color: var(--text);
      font: 14px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      min-height: 220px;
      max-height: 62vh;
      overflow: auto;
    }
    .markdown-body h1,
    .markdown-body h2,
    .markdown-body h3,
    .markdown-body h4 {
      margin: 18px 0 8px;
      color: var(--text);
      line-height: 1.25;
      font-weight: 720;
    }
    .markdown-body h1:first-child,
    .markdown-body h2:first-child,
    .markdown-body h3:first-child,
    .markdown-body h4:first-child,
    .markdown-body p:first-child {
      margin-top: 0;
    }
    .markdown-body h1 { font-size: 19px; }
    .markdown-body h2 { font-size: 17px; }
    .markdown-body h3 { font-size: 15px; }
    .markdown-body h4 { font-size: 14px; }
    .markdown-body p {
      margin: 8px 0;
    }
    .markdown-body ul,
    .markdown-body ol {
      margin: 8px 0 10px;
      padding-left: 22px;
    }
    .markdown-body li {
      margin: 4px 0;
    }
    .markdown-body strong {
      font-weight: 750;
    }
    .markdown-body code {
      border: 1px solid var(--line);
      border-radius: 5px;
      background: var(--surface-muted);
      padding: 1px 5px;
      font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    }
    .markdown-body pre {
      margin: 10px 0;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-muted);
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-word;
      font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, monospace;
      max-height: 280px;
      overflow: auto;
    }
    .markdown-body table {
      width: 100%;
      border-collapse: collapse;
      margin: 10px 0;
      font-size: 13px;
    }
    .markdown-body th,
    .markdown-body td {
      border: 1px solid var(--line);
      padding: 7px 8px;
      text-align: left;
      vertical-align: top;
    }
    .markdown-body th {
      background: var(--surface-muted);
      font-weight: 700;
    }
    .markdown-body blockquote {
      margin: 10px 0;
      padding: 8px 12px;
      border-left: 3px solid var(--accent);
      background: var(--surface-muted);
      color: var(--muted);
    }
    .overview-doc {
      min-height: 180px;
      max-height: 280px;
      margin-top: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
    }
    .compare-card .markdown-body {
      flex: 1;
      min-height: 420px;
      border: 0;
      border-radius: 0;
      background: var(--surface);
      box-shadow: none;
    }
    .raw-pre {
      margin: 0;
      padding: 16px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-muted);
      color: var(--text);
      white-space: pre-wrap;
      word-break: break-word;
      font: 12px/1.6 ui-monospace, SFMono-Regular, Menlo, monospace;
      min-height: 220px;
      max-height: 62vh;
      overflow: auto;
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 26px;
      background: var(--surface);
      color: var(--muted);
    }
    @media (max-width: 1180px) {
      .compare-grid { grid-template-columns: 1fr; }
      .compare-card .markdown-body { min-height: 260px; }
    }
    @media (max-width: 860px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      textarea { min-height: 180px; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <h1>AI Panel</h1>
      <div class="brand-kicker">Compare, critique, and recover local AI CLI runs</div>
    </div>
    <div id="health" class="path">checking...</div>
  </header>
  <main>
    <aside>
      <label for="topic">논제</label>
      <textarea id="topic" placeholder="여기에 비교하거나 토론시킬 논제를 입력하세요."></textarea>
      <label class="control-label">실행 방식</label>
      <div class="mode-group" aria-label="실행 방식">
        <label class="mode-option"><input type="radio" name="mode" value="ask"> 비교</label>
        <label class="mode-option"><input type="radio" name="mode" value="debate" checked> 토론</label>
      </div>
      <label class="control-label" for="presetSelect">프리셋</label>
      <select id="presetSelect" class="control-select"></select>
      <label class="control-label" for="judgeSelect">최종 정리</label>
      <select id="judgeSelect" class="control-select"></select>
      <button id="runBtn" class="primary">실행</button>
      <div id="status" class="status">대기 중</div>
      <div id="jobSteps" class="job-steps"></div>
      <div class="connect-panel">
        <h2>CLI 연동</h2>
        <div id="agents" class="agent-list"></div>
      </div>
      <div class="runs-title">
        <h2>최근 결과</h2>
        <button id="refreshBtn" class="file-tab">새로고침</button>
      </div>
      <div id="runs" class="run-list"></div>
    </aside>
    <section>
      <div id="result" class="empty">아직 선택된 결과가 없습니다.</div>
    </section>
  </main>
  <script>
    const topic = document.querySelector("#topic");
    const runBtn = document.querySelector("#runBtn");
    const refreshBtn = document.querySelector("#refreshBtn");
    const presetSelect = document.querySelector("#presetSelect");
    const judgeSelect = document.querySelector("#judgeSelect");
    const statusEl = document.querySelector("#status");
    const jobStepsEl = document.querySelector("#jobSteps");
    const runsEl = document.querySelector("#runs");
    const agentsEl = document.querySelector("#agents");
    const resultEl = document.querySelector("#result");

    const apiToken = "__AI_PANEL_TOKEN__";
    let agentOrder = ["claude", "gemini", "codex"];
    let agentStatuses = [];
    let defaultJudge = "";
    let presets = [];
    let applyingPreset = false;

    let currentRun = null;
    let currentFiles = [];
    let activePath = "__overview";

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: {"Content-Type": "application/json", "X-AI-Panel-Token": apiToken},
        ...options
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "요청 실패");
      return data;
    }

    function selectedMode() {
      return document.querySelector('input[name="mode"]:checked').value;
    }

    function selectedJudge() {
      return judgeSelect.value || defaultJudge;
    }

    function setStatus(text, kind = "") {
      statusEl.textContent = text;
      statusEl.className = "status " + kind;
    }

    function setBusy(busy) {
      runBtn.disabled = busy;
      runBtn.textContent = busy ? "실행 중..." : "실행";
    }

    function selectedModels() {
      const values = {};
      for (const select of agentsEl.querySelectorAll(".model-select")) {
        values[select.dataset.agent] = select.value;
      }
      return values;
    }

    function selectedPresetId() {
      return presetSelect.value || null;
    }

    function activePreset() {
      return presets.find(preset => preset.id === selectedPresetId()) || null;
    }

    function markCustomSelection() {
      if (!applyingPreset) presetSelect.value = "";
    }

    function setMode(mode) {
      const input = document.querySelector(`input[name="mode"][value="${mode}"]`);
      if (input) input.checked = true;
    }

    function applyPreset(presetId) {
      const preset = presets.find(item => item.id === presetId);
      if (!preset) return;
      applyingPreset = true;
      setMode(preset.mode || "debate");
      if (preset.judge) judgeSelect.value = preset.judge;
      for (const select of agentsEl.querySelectorAll(".model-select")) {
        const model = preset.models && preset.models[select.dataset.agent];
        if (model && Array.from(select.options).some(option => option.value === model)) {
          select.value = model;
        }
      }
      applyingPreset = false;
    }

    async function start(mode, value) {
      const topicValue = (value || topic.value).trim();
      if (!topicValue) {
        setStatus("논제를 먼저 입력하세요.", "error");
        return;
      }
      const models = selectedModels();
      const missingModels = agentOrder.filter(agent => !models[agent]);
      if (missingModels.length) {
        setStatus(`모델을 선택하세요: ${missingModels.join(", ")}`, "error");
        return;
      }
      setBusy(true);
      setStatus(mode === "debate" ? "토론 실행 중..." : "비교 실행 중...", "warn");
      jobStepsEl.innerHTML = "";
      try {
        const data = await api("/api/jobs", {
          method: "POST",
          body: JSON.stringify({
            mode,
            topic: topicValue,
            models,
            judge: selectedJudge(),
            preset_id: selectedPresetId()
          })
        });
        pollJob(data.job.id);
      } catch (error) {
        setBusy(false);
        setStatus(error.message, "error");
      }
    }

    async function rerunCurrent() {
      if (!currentRun) return;
      setBusy(true);
      setStatus("같은 토픽으로 다시 실행 중...", "warn");
      try {
        const data = await api(`/api/runs/${encodeURIComponent(currentRun.id)}/rerun`, {
          method: "POST",
          body: "{}"
        });
        pollJob(data.job.id);
      } catch (error) {
        setBusy(false);
        setStatus(error.message, "error");
      }
    }

    async function pollJob(jobId) {
      try {
        const data = await api("/api/jobs/" + encodeURIComponent(jobId));
        const job = data.job;
        renderJobSteps(job);
        if (job.status === "queued" || job.status === "running") {
          setStatus("실행 중: " + job.status, "warn");
          setTimeout(() => pollJob(jobId), 2000);
          return;
        }
        setBusy(false);
        if (job.status === "failed") {
          setStatus("실패: " + (job.error || "unknown"), "error");
          return;
        }
        setStatus(job.status === "done" ? "완료" : "완료, 일부 실패 있음", job.status === "done" ? "ok" : "warn");
        await loadAgents();
        await loadRuns();
        if (job.run_id) await loadRun(job.run_id);
      } catch (error) {
        setBusy(false);
        setStatus(error.message, "error");
      }
    }

    async function loadRuns() {
      const data = await api("/api/runs");
      runsEl.innerHTML = "";
      for (const run of data.runs) {
        const button = document.createElement("button");
        button.className = "run-item";
        const runKind = run.preset_label || modeLabel(run.mode);
        button.innerHTML = `${escapeHtml(run.topic || run.id)}<small>${escapeHtml(runKind)} · ${escapeHtml(run.id)}</small>`;
        button.onclick = () => loadRun(run.id);
        runsEl.appendChild(button);
      }
    }

    async function loadConfig() {
      const data = await api("/api/config");
      defaultJudge = data.judge || "";
      agentOrder = data.agents && data.agents.length ? data.agents : agentOrder;
      presets = data.presets || [];
      renderPresetOptions();
      renderJudgeOptions(data.judges || data.agents || []);
      if (presets.length) {
        presetSelect.value = presets[0].id;
        applyPreset(presetSelect.value);
      }
    }

    async function loadAgents() {
      const data = await api("/api/agents");
      const previousModels = selectedModels();
      const preset = activePreset();
      agentStatuses = data.agents || [];
      agentOrder = agentStatuses.map(agent => agent.id);
      agentsEl.innerHTML = agentStatuses.map(agent => `
        <div class="agent-row">
          <div class="agent-name">
            ${escapeHtml(agent.id)}
            <small>${escapeHtml(agent.status_detail || (agent.installed ? agent.connect_command || agent.executable : "설치 안 됨"))}</small>
          </div>
          <span class="status-pill ${escapeAttr(agent.status || "unknown")}" title="${escapeAttr(agent.status_detail || "")}">${escapeHtml(agent.status_label || "확인 필요")}</span>
          <button class="connect-button" data-agent="${escapeAttr(agent.id)}" ${agent.installed ? "" : "disabled"}>연동</button>
          <select class="model-select" data-agent="${escapeAttr(agent.id)}">
            ${(agent.models || []).map(model => `
              <option value="${escapeAttr(model.id)}" ${model.id === (previousModels[agent.id] || (preset && preset.models && preset.models[agent.id]) || agent.default_model) ? "selected" : ""}>${escapeHtml(model.label)}</option>
            `).join("")}
          </select>
        </div>
      `).join("");
      for (const button of agentsEl.querySelectorAll(".connect-button")) {
        button.onclick = () => connectAgent(button.dataset.agent);
      }
      for (const select of agentsEl.querySelectorAll(".model-select")) {
        select.onchange = markCustomSelection;
      }
      if (preset) applyPreset(preset.id);
    }

    function renderPresetOptions() {
      presetSelect.innerHTML = [
        `<option value="">직접 선택</option>`,
        ...presets.map(preset => `<option value="${escapeAttr(preset.id)}">${escapeHtml(preset.label || preset.id)}</option>`)
      ].join("");
    }

    function renderJudgeOptions(judges) {
      judgeSelect.innerHTML = judges.map(judge => `
        <option value="${escapeAttr(judge)}" ${judge === defaultJudge ? "selected" : ""}>${escapeHtml(judge)}</option>
      `).join("");
    }

    async function connectAgent(agent) {
      try {
        const data = await api(`/api/agents/${encodeURIComponent(agent)}/connect`, {
          method: "POST",
          body: "{}"
        });
        setStatus(`${data.agent} 연동 터미널을 열었습니다.`, "ok");
      } catch (error) {
        setStatus(error.message, "error");
      }
    }

    function modeLabel(mode) {
      if (mode === "ask") return "비교";
      if (mode === "debate") return "토론";
      return mode || "unknown";
    }

    async function loadRun(runId) {
      const data = await api("/api/runs/" + encodeURIComponent(runId));
      currentRun = data.run;
      currentFiles = currentRun.files;
      activePath = "__overview";
      renderRun();
    }

    function fileByPath(path) {
      return currentFiles.find(file => file.path === path) || null;
    }

    function roundFile(agent, round) {
      const suffix = round === "round2" ? `${agent}_critique.md` : `${agent}.md`;
      return fileByPath(`${round}/${suffix}`);
    }

    function modelLabel(agentId) {
      const selected = (currentRun && currentRun.models && currentRun.models[agentId]) || "";
      const agent = agentStatuses.find(item => item.id === agentId);
      const option = agent && (agent.models || []).find(model => model.id === selected);
      return option ? option.label : selected || "모델 미기록";
    }

    function renderRun() {
      if (!currentRun) return;
      resultEl.className = "";
      const tabs = [
        `<button class="file-tab${activePath === "__overview" ? " active" : ""}" data-path="__overview">전체 비교</button>`,
        ...currentFiles.map(file => {
          const active = file.path === activePath ? " active" : "";
          return `<button class="file-tab${active}" data-path="${escapeAttr(file.path)}">${escapeHtml(file.path)}</button>`;
        })
      ].join("");
      const body = activePath === "__overview" ? renderOverview() : renderSingleFile();
      resultEl.innerHTML = `
        <div class="result-head">
          <div>
            <h2>${escapeHtml(currentRun.topic_label || currentRun.id)}</h2>
            <div class="path">${escapeHtml(runMetaLine())}</div>
            <div class="path">${escapeHtml(currentRun.path)}</div>
          </div>
          <button id="rerunBtn" class="plain-button">같은 토픽 다시 실행</button>
        </div>
        ${renderFailurePanel()}
        <div class="file-tabs">${tabs}</div>
        ${body}
      `;
      document.querySelector("#rerunBtn").onclick = rerunCurrent;
      for (const tab of resultEl.querySelectorAll(".file-tab")) {
        tab.onclick = () => {
          activePath = tab.dataset.path;
          renderRun();
        };
      }
      for (const button of resultEl.querySelectorAll(".failure-connect")) {
        button.onclick = () => connectAgent(button.dataset.agent);
      }
    }

    function runMetaLine() {
      const parts = [
        currentRun.preset_label || modeLabel(currentRun.mode),
        currentRun.mode === "debate" && currentRun.judge ? `최종 정리: ${currentRun.judge}` : null,
        currentRun.id
      ].filter(Boolean);
      return parts.join(" · ");
    }

    function renderFailurePanel() {
      const failures = currentRun.failures || [];
      if (!failures.length) return "";
      const agents = [...new Set(failures.map(item => item.agent))];
      const actions = agents.map(agent => `<button class="connect-button failure-connect" data-agent="${escapeAttr(agent)}">${escapeHtml(agent)} 연동</button>`).join("");
      const list = failures.map(item => `<li>${escapeHtml(item.agent)} · ${escapeHtml(item.section)} · ${escapeHtml(String(item.error).slice(0, 180))}</li>`).join("");
      return `
        <div class="failure-panel">
          <strong>일부 모델 실행이 실패했습니다.</strong>
          실패한 모델의 연동 버튼을 눌러 로그인/세션 확인 터미널을 열고, 완료 후 오른쪽 위의 같은 토픽 다시 실행을 누르세요.
          <div class="failure-actions">${actions}</div>
          <ul>${list}</ul>
        </div>
      `;
    }

    function renderOverview() {
      const summary = fileByPath("summary.md");
      const round1 = renderAgentGrid("Round 1 독립 답변", "round1");
      const round2Files = agentOrder.map(agent => roundFile(agent, "round2")).filter(Boolean);
      const round2 = round2Files.length ? renderAgentGrid("Round 2 상호 비판", "round2") : "";
      return `
        ${summary ? `<div class="summary-box"><h3>최종 요약 <small>${escapeHtml(currentRun.judge ? `최종 정리: ${currentRun.judge}` : "")}</small></h3><div class="markdown-body overview-doc">${renderMarkdown(summary.content)}</div></div>` : ""}
        ${round1}
        ${round2}
      `;
    }

    function renderAgentGrid(title, round) {
      const cards = agentOrder.map(agent => {
        const file = roundFile(agent, round);
        const content = file ? file.content : "(결과 없음)";
        const step = stepFor(round, agent);
        const state = step ? step.status : (content.startsWith("# 실행 실패") ? "failed" : "done");
        const stateLabel = statusLabel(state);
        return `
          <div class="compare-card">
            <div class="card-head">
              <h3>${escapeHtml(agent)} <small>${escapeHtml(modelLabel(agent))}</small></h3>
              <span class="badge ${escapeAttr(state)}">${escapeHtml(stateLabel)}</span>
            </div>
            <div class="markdown-body">${renderMarkdown(content)}</div>
          </div>
        `;
      }).join("");
      return `<div class="summary-box"><h3>${escapeHtml(title)}</h3></div><div class="compare-grid">${cards}</div>`;
    }

    function stepFor(stage, agent) {
      const steps = (currentRun && currentRun.steps) || [];
      return steps.find(step => step.stage === stage && step.agent_id === agent) || null;
    }

    function statusLabel(status) {
      return {
        pending: "대기",
        running: "실행 중",
        done: "완료",
        failed: "실패",
        skipped: "건너뜀"
      }[status] || status || "결과";
    }

    function renderJobSteps(job) {
      const steps = (job && job.steps) || [];
      if (!steps.length) {
        jobStepsEl.innerHTML = "";
        return;
      }
      jobStepsEl.innerHTML = steps.map(step => {
        const model = step.model ? ` · ${step.model}` : "";
        const detail = step.error ? ` · ${String(step.error).slice(0, 80)}` : "";
        return `
          <div class="step-row">
            <strong>${escapeHtml(step.stage || "run")}</strong>
            <span>${escapeHtml((step.agent_id || "agent") + model + detail)}</span>
            <span class="badge ${escapeAttr(step.status || "pending")}">${escapeHtml(statusLabel(step.status))}</span>
          </div>
        `;
      }).join("");
    }

    function renderSingleFile() {
      const file = fileByPath(activePath);
      if (!file) return `<div class="empty">표시할 파일이 없습니다.</div>`;
      if (file.path.endsWith(".md")) {
        return `<div class="markdown-body">${renderMarkdown(file.content)}</div>`;
      }
      return `<pre class="raw-pre">${escapeHtml(file.content)}</pre>`;
    }

    function renderMarkdown(markdown) {
      const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");
      const html = [];
      let paragraph = [];
      let listType = null;
      let inCode = false;
      let codeLines = [];

      const flushParagraph = () => {
        if (!paragraph.length) return;
        html.push(`<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`);
        paragraph = [];
      };
      const closeList = () => {
        if (!listType) return;
        html.push(`</${listType}>`);
        listType = null;
      };
      const flushBlocks = () => {
        flushParagraph();
        closeList();
      };
      const isTableSeparator = line => /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
      const isTableRow = line => line.includes("|") && line.trim().split("|").filter(Boolean).length >= 2;
      const parseTableRow = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());

      for (let index = 0; index < lines.length; index += 1) {
        const line = lines[index];
        const trimmed = line.trim();

        if (trimmed.startsWith("```")) {
          if (inCode) {
            html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
            codeLines = [];
            inCode = false;
          } else {
            flushBlocks();
            inCode = true;
          }
          continue;
        }
        if (inCode) {
          codeLines.push(line);
          continue;
        }
        if (!trimmed) {
          flushBlocks();
          continue;
        }
        if (/^---+$/.test(trimmed)) {
          flushBlocks();
          html.push("<hr>");
          continue;
        }

        const heading = trimmed.match(/^(#{1,4})\s+(.+)$/);
        if (heading) {
          flushBlocks();
          const level = Math.min(heading[1].length + 1, 4);
          html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
          continue;
        }

        if (trimmed.startsWith("> ")) {
          flushBlocks();
          html.push(`<blockquote>${inlineMarkdown(trimmed.slice(2))}</blockquote>`);
          continue;
        }

        if (isTableRow(line) && lines[index + 1] && isTableSeparator(lines[index + 1])) {
          flushBlocks();
          const headers = parseTableRow(line);
          const rows = [];
          index += 2;
          while (index < lines.length && isTableRow(lines[index])) {
            rows.push(parseTableRow(lines[index]));
            index += 1;
          }
          index -= 1;
          html.push(`<table><thead><tr>${headers.map(cell => `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${inlineMarkdown(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
          continue;
        }

        const unordered = trimmed.match(/^[-*]\s+(.+)$/);
        const ordered = trimmed.match(/^\d+[.)]\s+(.+)$/);
        if (unordered || ordered) {
          flushParagraph();
          const type = unordered ? "ul" : "ol";
          if (listType !== type) {
            closeList();
            html.push(`<${type}>`);
            listType = type;
          }
          html.push(`<li>${inlineMarkdown((unordered || ordered)[1])}</li>`);
          continue;
        }

        closeList();
        paragraph.push(line);
      }

      if (inCode) {
        html.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
      }
      flushBlocks();
      return html.join("");
    }

    function inlineMarkdown(value) {
      let output = escapeHtml(value);
      output = output.replace(/`([^`]+)`/g, "<code>$1</code>");
      output = output.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      output = output.replace(/__([^_]+)__/g, "<strong>$1</strong>");
      return output;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      })[ch]);
    }

    function escapeAttr(value) {
      return escapeHtml(value).replace(/`/g, "&#96;");
    }

    runBtn.onclick = () => start(selectedMode());
    refreshBtn.onclick = loadRuns;
    presetSelect.onchange = () => applyPreset(presetSelect.value);
    judgeSelect.onchange = markCustomSelection;
    for (const input of document.querySelectorAll('input[name="mode"]')) {
      input.onchange = markCustomSelection;
    }

    api("/api/health")
      .then(data => {
        document.querySelector("#health").textContent = `연동: ${data.agents.join(", ")}`;
      })
      .catch(error => {
        document.querySelector("#health").textContent = error.message;
      });
    loadConfig()
      .then(loadAgents)
      .catch(error => setStatus(error.message, "error"));
    loadRuns().catch(error => setStatus(error.message, "error"));
  </script>
</body>
</html>
"""
