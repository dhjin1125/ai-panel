from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_NAME = "agents.yaml"


@dataclass(frozen=True)
class ModelOption:
    id: str
    label: str


@dataclass(frozen=True)
class AgentConfig:
    id: str
    command: list[str]
    model_arg: list[str]
    models: list[ModelOption]
    default_model: str


@dataclass(frozen=True)
class PresetConfig:
    id: str
    label: str
    mode: str
    judge: str
    models: dict[str, str]


@dataclass(frozen=True)
class PanelConfig:
    agents: list[AgentConfig]
    judge: str
    timeout_seconds: int
    presets: list[PresetConfig]


class ConfigError(ValueError):
    pass


def load_config(path: Path) -> PanelConfig:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path}는 JSON-compatible YAML 형식이어야 합니다: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise ConfigError("설정 최상위 값은 object여야 합니다.")

    timeout = _optional_int(raw, "timeout_seconds", default=900)
    judge = _required_str(raw, "judge")
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, list) or not agents_raw:
        raise ConfigError("agents는 비어 있지 않은 list여야 합니다.")

    agents = [_parse_agent(item) for item in agents_raw]
    ids = [agent.id for agent in agents]
    if len(ids) != len(set(ids)):
        raise ConfigError("agent id는 중복될 수 없습니다.")
    if judge not in ids:
        raise ConfigError(f"judge '{judge}'가 agents 목록에 없습니다.")
    presets = _parse_presets(raw.get("presets"), agents, judge)

    return PanelConfig(
        agents=agents,
        judge=judge,
        timeout_seconds=timeout,
        presets=presets,
    )


def default_config_path(cwd: Path | None = None) -> Path:
    base = cwd or Path.cwd()
    return base / DEFAULT_CONFIG_NAME


def preset_by_id(
    config: PanelConfig,
    preset_id: str | None,
    required: bool = False,
) -> PresetConfig | None:
    if not preset_id:
        return None
    for preset in config.presets:
        if preset.id == preset_id:
            return preset
    if required:
        raise ValueError("preset_id를 찾을 수 없습니다.")
    return None


def selected_models(
    config: PanelConfig,
    model_by_agent: dict[str, str] | None,
    preset: PresetConfig | None = None,
) -> dict[str, str]:
    requested = {**(preset.models if preset else {}), **(model_by_agent or {})}
    selected = {}
    for agent in config.agents:
        model = requested.get(agent.id, agent.default_model)
        allowed = {option.id for option in agent.models}
        selected[agent.id] = model if isinstance(model, str) and model in allowed else agent.default_model
    return selected


def selected_judge(
    config: PanelConfig,
    judge_id: str | None,
    preset: PresetConfig | None = None,
    required: bool = False,
) -> str:
    requested = judge_id or (preset.judge if preset else config.judge)
    agent_ids = {agent.id for agent in config.agents}
    if requested not in agent_ids:
        if required:
            raise ValueError("judge가 agents 목록에 없습니다.")
        return config.judge
    return requested


def _parse_agent(raw: Any) -> AgentConfig:
    if not isinstance(raw, dict):
        raise ConfigError("agent 항목은 object여야 합니다.")
    agent_id = _required_str(raw, "id")
    command_raw = raw.get("command")
    if not isinstance(command_raw, list) or not command_raw:
        raise ConfigError(f"{agent_id}.command는 비어 있지 않은 list여야 합니다.")
    command = []
    for index, part in enumerate(command_raw):
        if not isinstance(part, str):
            raise ConfigError(f"{agent_id}.command에는 string만 허용됩니다.")
        if index == 0 and not part:
            raise ConfigError(f"{agent_id}.command의 실행 파일 이름은 비어 있을 수 없습니다.")
        command.append(part)
    model_arg = _optional_str_list(raw, "model_arg", default=["--model", "{model}"])
    models = _parse_models(raw.get("models"))
    default_model = raw.get("default_model")
    if not isinstance(default_model, str) or not default_model:
        raise ConfigError(f"{agent_id}.default_model은 비어 있지 않은 string이어야 합니다.")
    model_ids = [model.id for model in models]
    if len(model_ids) != len(set(model_ids)):
        raise ConfigError(f"{agent_id}.models의 id는 중복될 수 없습니다.")
    if default_model not in model_ids:
        raise ConfigError(f"{agent_id}.default_model이 models 목록에 없습니다.")
    return AgentConfig(
        id=agent_id,
        command=command,
        model_arg=model_arg,
        models=models,
        default_model=default_model,
    )


def _required_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{key}는 비어 있지 않은 string이어야 합니다.")
    return value


def _optional_int(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{key}는 양의 integer여야 합니다.")
    return value


def _optional_str_list(raw: dict[str, Any], key: str, default: list[str]) -> list[str]:
    value = raw.get(key, default)
    if not isinstance(value, list):
        raise ConfigError(f"{key}는 list여야 합니다.")
    parsed = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{key}에는 string만 허용됩니다.")
        parsed.append(item)
    return parsed


def _parse_models(raw: Any) -> list[ModelOption]:
    if raw is None:
        raise ConfigError("models는 명시적으로 선택 가능한 모델 목록을 제공해야 합니다.")
    if not isinstance(raw, list) or not raw:
        raise ConfigError("models는 비어 있지 않은 list여야 합니다.")
    models = []
    for item in raw:
        if isinstance(item, str):
            if not item:
                raise ConfigError("models에는 빈 id를 사용할 수 없습니다.")
            models.append(ModelOption(id=item, label=item))
            continue
        if not isinstance(item, dict):
            raise ConfigError("models 항목은 string 또는 object여야 합니다.")
        model_id = item.get("id")
        label = item.get("label", model_id)
        if not isinstance(model_id, str) or not model_id:
            raise ConfigError("models.id는 비어 있지 않은 string이어야 합니다.")
        if not isinstance(label, str) or not label:
            raise ConfigError("models.label은 비어 있지 않은 string이어야 합니다.")
        models.append(ModelOption(id=model_id, label=label))
    return models


def _parse_presets(
    raw: Any,
    agents: list[AgentConfig],
    default_judge: str,
) -> list[PresetConfig]:
    if raw is None:
        return [
            PresetConfig(
                id="balanced",
                label="기본 토론",
                mode="debate",
                judge=default_judge,
                models={},
            )
        ]
    if not isinstance(raw, list) or not raw:
        raise ConfigError("presets는 비어 있지 않은 list여야 합니다.")

    agent_by_id = {agent.id: agent for agent in agents}
    presets = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("preset 항목은 object여야 합니다.")
        preset_id = _required_str(item, "id")
        label = item.get("label", preset_id)
        if not isinstance(label, str) or not label:
            raise ConfigError(f"{preset_id}.label은 비어 있지 않은 string이어야 합니다.")
        mode = item.get("mode", "debate")
        if mode not in {"ask", "debate"}:
            raise ConfigError(f"{preset_id}.mode는 ask 또는 debate여야 합니다.")
        judge = item.get("judge", default_judge)
        if not isinstance(judge, str) or judge not in agent_by_id:
            raise ConfigError(f"{preset_id}.judge가 agents 목록에 없습니다.")
        models = _parse_preset_models(preset_id, item.get("models", {}), agent_by_id)
        presets.append(
            PresetConfig(
                id=preset_id,
                label=label,
                mode=mode,
                judge=judge,
                models=models,
            )
        )

    ids = [preset.id for preset in presets]
    if len(ids) != len(set(ids)):
        raise ConfigError("preset id는 중복될 수 없습니다.")
    return presets


def _parse_preset_models(
    preset_id: str,
    raw: Any,
    agent_by_id: dict[str, AgentConfig],
) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ConfigError(f"{preset_id}.models는 object여야 합니다.")
    models = {}
    for agent_id, model_id in raw.items():
        if not isinstance(agent_id, str) or agent_id not in agent_by_id:
            raise ConfigError(f"{preset_id}.models에 알 수 없는 agent가 있습니다: {agent_id}")
        if not isinstance(model_id, str):
            raise ConfigError(f"{preset_id}.models.{agent_id}는 string이어야 합니다.")
        allowed = {model.id for model in agent_by_id[agent_id].models}
        if model_id not in allowed:
            raise ConfigError(f"{preset_id}.models.{agent_id}가 models 목록에 없습니다.")
        models[agent_id] = model_id
    return models
