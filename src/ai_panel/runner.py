from __future__ import annotations

import asyncio
from dataclasses import dataclass
import shutil
import time
from typing import Callable

from ai_panel.config import AgentConfig


PROMPT_PLACEHOLDER = "{prompt}"
StatusCallback = Callable[[str, str, str, dict], None]


@dataclass(frozen=True)
class RunResult:
    agent_id: str
    model: str
    command: list[str]
    stdout: str
    stderr: str
    exit_code: int | None
    duration_ms: int
    timed_out: bool
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.error is None


async def run_agent(
    agent: AgentConfig,
    prompt: str,
    timeout_seconds: int,
    model: str | None = None,
    stage: str = "run",
    status_callback: StatusCallback | None = None,
) -> RunResult:
    started = time.monotonic()
    selected_model = model if model is not None else agent.default_model
    command, stdin = _build_invocation(_command_with_model(agent, selected_model), prompt)
    _emit_status(
        status_callback,
        stage,
        agent.id,
        "running",
        {"model": selected_model},
    )
    executable = command[0]
    if shutil.which(executable) is None:
        return _finish(
            status_callback,
            stage,
            RunResult(
                agent_id=agent.id,
                model=selected_model,
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=_duration_ms(started),
                timed_out=False,
                error=f"실행 파일을 찾을 수 없습니다: {executable}",
            ),
        )

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return _finish(
            status_callback,
            stage,
            RunResult(
                agent_id=agent.id,
                model=selected_model,
                command=command,
                stdout="",
                stderr="",
                exit_code=None,
                duration_ms=_duration_ms(started),
                timed_out=False,
                error=str(exc),
            ),
        )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(stdin.encode("utf-8") if stdin is not None else None),
            timeout=timeout_seconds,
        )
        return _finish(
            status_callback,
            stage,
            RunResult(
                agent_id=agent.id,
                model=selected_model,
                command=command,
                stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
                stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
                exit_code=process.returncode,
                duration_ms=_duration_ms(started),
                timed_out=False,
            ),
        )
    except asyncio.TimeoutError:
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()
        return _finish(
            status_callback,
            stage,
            RunResult(
                agent_id=agent.id,
                model=selected_model,
                command=command,
                stdout=stdout_bytes.decode("utf-8", errors="replace").strip(),
                stderr=stderr_bytes.decode("utf-8", errors="replace").strip(),
                exit_code=process.returncode,
                duration_ms=_duration_ms(started),
                timed_out=True,
                error=f"{timeout_seconds}초 timeout",
            ),
        )


async def run_many(
    agents: list[AgentConfig],
    prompt_by_agent: dict[str, str],
    timeout_seconds: int,
    model_by_agent: dict[str, str] | None = None,
    stage: str = "run",
    status_callback: StatusCallback | None = None,
) -> list[RunResult]:
    tasks = [
        run_agent(
            agent,
            prompt_by_agent[agent.id],
            timeout_seconds,
            (model_by_agent or {}).get(agent.id),
            stage,
            status_callback,
        )
        for agent in agents
        if agent.id in prompt_by_agent
    ]
    if not tasks:
        return []
    return await asyncio.gather(*tasks)


def _build_invocation(command: list[str], prompt: str) -> tuple[list[str], str | None]:
    if PROMPT_PLACEHOLDER in command:
        return [prompt if part == PROMPT_PLACEHOLDER else part for part in command], None
    return command, prompt


def _command_with_model(agent: AgentConfig, model: str) -> list[str]:
    if not model:
        return agent.command
    rendered_arg = [model if part == "{model}" else part for part in agent.model_arg]
    if not rendered_arg:
        return agent.command
    for marker in (PROMPT_PLACEHOLDER, "--prompt", "-p", "-"):
        if marker in agent.command:
            index = agent.command.index(marker)
            return [*agent.command[:index], *rendered_arg, *agent.command[index:]]
    return [*agent.command, *rendered_arg]


def _duration_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _finish(
    status_callback: StatusCallback | None,
    stage: str,
    result: RunResult,
) -> RunResult:
    _emit_status(
        status_callback,
        stage,
        result.agent_id,
        "done" if result.ok else "failed",
        {
            "model": result.model,
            "duration_ms": result.duration_ms,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "error": result.error or result.stderr or None,
        },
    )
    return result


def _emit_status(
    status_callback: StatusCallback | None,
    stage: str,
    agent_id: str,
    status: str,
    payload: dict,
) -> None:
    if status_callback is not None:
        status_callback(stage, agent_id, status, payload)
