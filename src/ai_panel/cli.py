from __future__ import annotations

import argparse
from pathlib import Path
import sys

from ai_panel.config import ConfigError, PanelConfig, default_config_path, load_config
from ai_panel.panel import run_ask, run_debate
from ai_panel.server import serve


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"입력 오류: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("중단되었습니다.", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-panel",
        description="Claude/Gemini/Codex CLI 답변을 비교하고 토론 결과를 저장합니다.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="설정 파일 경로입니다. 기본값: ./agents.yaml",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs"),
        help="실행 결과 저장 디렉터리입니다. 기본값: ./runs",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="agent별 timeout 초입니다. 설정 파일 값을 덮어씁니다.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="독립 답변만 생성합니다.")
    ask_parser.add_argument("topic", type=Path)
    _add_run_options(ask_parser)
    ask_parser.set_defaults(func=ask_command)

    debate_parser = subparsers.add_parser("debate", help="독립 답변, 비판, 요약을 생성합니다.")
    debate_parser.add_argument("topic", type=Path)
    _add_run_options(debate_parser)
    debate_parser.set_defaults(func=debate_command)

    show_parser = subparsers.add_parser("show", help="저장된 run의 파일 경로를 출력합니다.")
    show_parser.add_argument("run_id")
    show_parser.set_defaults(func=show_command)

    doctor_parser = subparsers.add_parser("doctor", help="설정과 CLI 설치 여부를 점검합니다.")
    doctor_parser.set_defaults(func=doctor_command)

    serve_parser = subparsers.add_parser("serve", help="로컬 웹 UI를 실행합니다.")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument(
        "--no-open",
        action="store_true",
        help="서버만 실행하고 브라우저는 자동으로 열지 않습니다.",
    )
    serve_parser.set_defaults(func=serve_command)

    return parser


def ask_command(args: argparse.Namespace) -> int:
    config = _load_panel_config(args)
    topic = _read_topic(args.topic)
    panel_run = run_ask(
        config,
        topic,
        args.runs_dir,
        str(args.topic),
        _parse_model_overrides(args.model),
        args.judge,
        args.preset,
    )
    print(panel_run.run_dir)
    return panel_run.exit_code


def debate_command(args: argparse.Namespace) -> int:
    config = _load_panel_config(args)
    topic = _read_topic(args.topic)
    panel_run = run_debate(
        config,
        topic,
        args.runs_dir,
        str(args.topic),
        _parse_model_overrides(args.model),
        args.judge,
        args.preset,
    )
    print(panel_run.run_dir)
    return panel_run.exit_code


def show_command(args: argparse.Namespace) -> int:
    run_dir = args.runs_dir / args.run_id
    if not run_dir.exists():
        print(f"run을 찾을 수 없습니다: {run_dir}", file=sys.stderr)
        return 2
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            print(path)
    return 0


def doctor_command(args: argparse.Namespace) -> int:
    config = _load_panel_config(args)
    print(f"config: {_config_path(args)}")
    print(f"timeout_seconds: {config.timeout_seconds}")
    print(f"judge: {config.judge}")
    exit_code = 0
    for agent in config.agents:
        from shutil import which

        executable = agent.command[0]
        found = which(executable)
        status = "OK" if found else "MISSING"
        if not found:
            exit_code = 1
        print(f"{agent.id}: {status} ({executable})")
    return exit_code


def serve_command(args: argparse.Namespace) -> int:
    config = _load_panel_config(args)
    serve(config, args.runs_dir, args.host, args.port, open_browser=not args.no_open)
    return 0


def _add_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--preset",
        default=None,
        help="agents.yaml의 preset id를 적용합니다.",
    )
    parser.add_argument(
        "--judge",
        default=None,
        help="토론 최종 정리 agent id를 지정합니다.",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        metavar="AGENT=MODEL",
        help="agent별 모델을 지정합니다. 여러 번 사용할 수 있습니다.",
    )


def _load_panel_config(args: argparse.Namespace) -> PanelConfig:
    config = load_config(_config_path(args))
    if args.timeout is not None:
        if args.timeout <= 0:
            raise ValueError("--timeout은 양수여야 합니다.")
        return PanelConfig(
            agents=config.agents,
            judge=config.judge,
            timeout_seconds=args.timeout,
            presets=config.presets,
        )
    return config


def _config_path(args: argparse.Namespace) -> Path:
    return args.config or default_config_path()


def _parse_model_overrides(values: list[str]) -> dict[str, str]:
    models = {}
    for value in values:
        if "=" not in value:
            raise ValueError("--model은 AGENT=MODEL 형식이어야 합니다.")
        agent_id, model = value.split("=", 1)
        agent_id = agent_id.strip()
        model = model.strip()
        if not agent_id or not model:
            raise ValueError("--model은 AGENT=MODEL 형식이어야 합니다.")
        models[agent_id] = model
    return models


def _read_topic(path: Path) -> str:
    try:
        topic = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise ValueError(f"topic 파일을 찾을 수 없습니다: {path}") from exc
    if not topic:
        raise ValueError("topic 파일이 비어 있습니다.")
    return topic
