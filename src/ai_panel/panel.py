from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ai_panel.config import PanelConfig, PresetConfig
from ai_panel.prompts import check_format, critique_prompt, independent_prompt, summary_prompt
from ai_panel.runner import RunResult, run_agent, run_many
from ai_panel.storage import (
    failure_summaries,
    make_run_dir,
    read_success_outputs,
    result_meta,
    write_meta,
    write_result,
    write_text,
)


StatusCallback = Callable[[str, str, str, dict], None]


@dataclass(frozen=True)
class PanelRun:
    run_dir: Path
    exit_code: int
    meta: dict


def run_ask(
    config: PanelConfig,
    topic: str,
    runs_dir: Path,
    topic_source: str,
    model_by_agent: dict[str, str] | None = None,
    judge_id: str | None = None,
    preset_id: str | None = None,
    status_callback: StatusCallback | None = None,
) -> PanelRun:
    run_dir = make_run_dir(runs_dir)
    write_text(run_dir / "topic.md", topic)

    preset = _preset_by_id(config, preset_id)
    selected_models = _selected_models(config, model_by_agent, preset)
    judge = _selected_judge(config, judge_id, preset)
    results = asyncio.run(_run_round1(config, topic, selected_models, status_callback))
    for result in results:
        write_result(run_dir / "round1" / f"{result.agent_id}.md", result)

    results_by_stage = {"round1": results}
    meta = {
        "mode": "ask",
        "topic": topic_source,
        "timeout_seconds": config.timeout_seconds,
        "preset_id": preset.id if preset else None,
        "preset_label": preset.label if preset else None,
        "judge": judge,
        "models": selected_models,
        "round1": result_meta(results),
        "steps": steps_meta(results_by_stage),
        "format_checks": format_checks(results_by_stage),
    }
    write_meta(run_dir / "meta.json", meta)
    return PanelRun(run_dir=run_dir, exit_code=_exit_code_for_results(results), meta=meta)


def run_debate(
    config: PanelConfig,
    topic: str,
    runs_dir: Path,
    topic_source: str,
    model_by_agent: dict[str, str] | None = None,
    judge_id: str | None = None,
    preset_id: str | None = None,
    status_callback: StatusCallback | None = None,
) -> PanelRun:
    run_dir = make_run_dir(runs_dir)
    write_text(run_dir / "topic.md", topic)

    preset = _preset_by_id(config, preset_id)
    selected_models = _selected_models(config, model_by_agent, preset)
    judge = _selected_judge(config, judge_id, preset)
    round1_results = asyncio.run(_run_round1(config, topic, selected_models, status_callback))
    for result in round1_results:
        write_result(run_dir / "round1" / f"{result.agent_id}.md", result)

    answers = read_success_outputs(round1_results)
    round2_results: list[RunResult] = []
    if len(answers) >= 2:
        prompt_by_agent = {
            agent.id: critique_prompt(topic, agent.id, answers)
            for agent in config.agents
            if agent.id in answers
        }
        round2_results = asyncio.run(
            run_many(
                config.agents,
                prompt_by_agent,
                config.timeout_seconds,
                selected_models,
                "round2",
                status_callback,
            )
        )
        for result in round2_results:
            write_result(run_dir / "round2" / f"{result.agent_id}_critique.md", result)
    else:
        for agent in config.agents:
            _emit_status(
                status_callback,
                "round2",
                agent.id,
                "skipped",
                {
                    "model": selected_models.get(agent.id, ""),
                    "error": "성공한 Round 1 답변이 2개 미만입니다.",
                },
            )
        write_text(
            run_dir / "round2" / "skipped.md",
            "성공한 Round 1 답변이 2개 미만이라 상호 비판을 건너뜁니다.",
        )

    critiques = read_success_outputs(round2_results)
    failures = failure_summaries(round1_results) + failure_summaries(round2_results)
    summary_result = asyncio.run(
        _run_summary(
            config,
            judge,
            topic,
            answers,
            critiques,
            failures,
            selected_models,
            status_callback,
        )
    )
    write_result(run_dir / "summary.md", summary_result)

    all_results = round1_results + round2_results + [summary_result]
    results_by_stage = {
        "round1": round1_results,
        "round2": round2_results,
        "summary": [summary_result],
    }
    meta = {
        "mode": "debate",
        "topic": topic_source,
        "timeout_seconds": config.timeout_seconds,
        "preset_id": preset.id if preset else None,
        "preset_label": preset.label if preset else None,
        "judge": judge,
        "models": selected_models,
        "round1": result_meta(round1_results),
        "round2": result_meta(round2_results),
        "summary": result_meta([summary_result])[0],
        "steps": steps_meta(results_by_stage),
        "format_checks": format_checks(results_by_stage),
    }
    write_meta(run_dir / "meta.json", meta)
    return PanelRun(run_dir=run_dir, exit_code=_exit_code_for_results(all_results), meta=meta)


async def _run_round1(
    config: PanelConfig,
    topic: str,
    model_by_agent: dict[str, str],
    status_callback: StatusCallback | None,
) -> list[RunResult]:
    prompt_by_agent = {agent.id: independent_prompt(topic) for agent in config.agents}
    return await run_many(
        config.agents,
        prompt_by_agent,
        config.timeout_seconds,
        model_by_agent,
        "round1",
        status_callback,
    )


async def _run_summary(
    config: PanelConfig,
    judge_id: str,
    topic: str,
    answers: dict[str, str],
    critiques: dict[str, str],
    failures: list[str],
    model_by_agent: dict[str, str],
    status_callback: StatusCallback | None,
) -> RunResult:
    judge = next(agent for agent in config.agents if agent.id == judge_id)
    prompt = summary_prompt(topic, answers, critiques, failures)
    return await run_agent(
        judge,
        prompt,
        config.timeout_seconds,
        model_by_agent.get(judge.id),
        "summary",
        status_callback,
    )


def _exit_code_for_results(results: list[RunResult]) -> int:
    return 0 if all(result.ok for result in results) else 1


def _selected_models(
    config: PanelConfig,
    model_by_agent: dict[str, str] | None,
    preset: PresetConfig | None = None,
) -> dict[str, str]:
    requested = {**(preset.models if preset else {}), **(model_by_agent or {})}
    selected = {}
    for agent in config.agents:
        model = requested.get(agent.id, agent.default_model)
        allowed = {option.id for option in agent.models}
        selected[agent.id] = model if model in allowed else agent.default_model
    return selected


def _selected_judge(
    config: PanelConfig,
    judge_id: str | None,
    preset: PresetConfig | None = None,
) -> str:
    requested = judge_id or (preset.judge if preset else config.judge)
    agent_ids = {agent.id for agent in config.agents}
    return requested if requested in agent_ids else config.judge


def _preset_by_id(config: PanelConfig, preset_id: str | None) -> PresetConfig | None:
    if not preset_id:
        return None
    return next((preset for preset in config.presets if preset.id == preset_id), None)


def steps_meta(results_by_stage: dict[str, list[RunResult]]) -> list[dict]:
    steps = []
    for stage, results in results_by_stage.items():
        for result in results:
            steps.append(
                {
                    "stage": stage,
                    "agent_id": result.agent_id,
                    "model": result.model,
                    "status": "done" if result.ok else "failed",
                    "duration_ms": result.duration_ms,
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "error": result.error or result.stderr or None,
                }
            )
    return steps


def format_checks(results_by_stage: dict[str, list[RunResult]]) -> dict:
    checks = {}
    for stage, results in results_by_stage.items():
        checks[stage] = {
            result.agent_id: check_format(stage, result.stdout)
            if result.ok
            else {"ok": False, "error": "실행 실패"}
            for result in results
        }
    return checks


def _emit_status(
    status_callback: StatusCallback | None,
    stage: str,
    agent_id: str,
    status: str,
    payload: dict,
) -> None:
    if status_callback is not None:
        status_callback(stage, agent_id, status, payload)
