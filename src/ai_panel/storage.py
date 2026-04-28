from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from ai_panel.runner import RunResult


def make_run_dir(runs_dir: Path) -> Path:
    timestamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%dT%H%M%S")
    candidate = runs_dir / timestamp
    suffix = 1
    while candidate.exists():
        candidate = runs_dir / f"{timestamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


def write_result(path: Path, result: RunResult) -> None:
    if result.ok:
        body = result.stdout or "(빈 응답)"
    else:
        body = "\n".join(
            part
            for part in [
                "# 실행 실패",
                "",
                f"- agent: {result.agent_id}",
                f"- exit_code: {result.exit_code}",
                f"- timed_out: {result.timed_out}",
                f"- error: {result.error or '없음'}",
                "",
                "## stdout",
                result.stdout or "(없음)",
                "",
                "## stderr",
                result.stderr or "(없음)",
            ]
        )
    write_text(path, body)


def read_success_outputs(results: list[RunResult]) -> dict[str, str]:
    return {result.agent_id: result.stdout for result in results if result.ok}


def failure_summaries(results: list[RunResult]) -> list[str]:
    failures = []
    for result in results:
        if result.ok:
            continue
        reason = result.error or f"exit_code={result.exit_code}"
        if result.stderr:
            reason = f"{reason}; stderr={result.stderr[:300]}"
        failures.append(f"{result.agent_id}: {reason}")
    return failures


def write_meta(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def result_meta(results: list[RunResult]) -> list[dict]:
    return [asdict(result) for result in results]
