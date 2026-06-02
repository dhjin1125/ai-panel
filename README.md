# ai-panel-cli

Claude, Gemini, Codex CLI를 API 키 없이 로컬 headless 실행으로 호출해 같은 논제를 비교하고 짧은 토론 결과를 저장하는 도구입니다.

## 전제

- Python 3와 `curl`이 필요합니다.
- 실제 비교/토론 실행에는 `claude`, `gemini`, `codex` CLI가 설치되어 있고 로그인되어 있어야 합니다.
- v1은 텍스트 논제만 다룹니다.
- 모델에게 로컬 코드 읽기나 파일 수정을 요청하지 않습니다.

## 빠른 실행

macOS에서는 클론한 폴더 안의 `AI Panel.command`를 더블클릭하면 로컬 서버가 켜지고 브라우저가 열립니다.

```bash
git clone https://github.com/dhjin1125/ai-panel.git
cd ai-panel
./open-ai-panel
./ai-panel ask examples/topic.md
./ai-panel debate examples/topic.md
./ai-panel serve
```

결과는 `runs/<timestamp>/` 아래에 저장됩니다.

브라우저에서 쓰려면 아래 명령만 실행하면 됩니다. 서버가 켜지고 브라우저가 자동으로 열립니다.

```bash
./open-ai-panel
```

화면에서는 `논제`를 입력하고 `실행 방식`에서 `비교` 또는 `토론`을 고른 뒤, 프리셋/최종 정리 모델/각 CLI 모델을 선택하고 `실행`을 누르면 됩니다. 기본 결과 화면은 Claude/Gemini/Codex 답변을 3컬럼으로 보여주고, 개별 md/json 탭을 누르면 원본 파일만 따로 볼 수 있습니다.

특정 CLI가 실패하면 결과 화면에 실패한 모델과 직접 확인할 명령이 표시됩니다. 터미널에서 해당 CLI 로그인/세션을 확인한 뒤 `같은 토픽 다시 실행`을 누르면 됩니다.

왼쪽 `CLI 연동` 영역의 `연동` 버튼을 누르면 해당 CLI의 로그인/세션 확인 터미널이 열립니다. Claude는 `claude auth login`, Gemini는 `gemini`, Codex는 `codex login`을 실행합니다.

각 CLI 연동 영역에서 사용할 모델을 반드시 선택합니다. Claude는 Haiku/Sonnet/Opus, Gemini는 2.5/3 preview 계열, Codex는 GPT-5.5/GPT-5.4/GPT-5.4 Mini 중 하나를 고르며 실행 명령에 `--model <모델>` 옵션을 붙입니다.

연동 버튼 옆 상태 배지는 `정상`, `최근 성공`, `오류`, `연동 필요`, `확인 필요`, `미설치`로 표시됩니다. Claude/Codex는 로그인 상태 명령을 우선 확인하고, Gemini는 별도 상태 명령이 없어 최근 실행 결과를 기준으로 표시합니다.

## 명령

```bash
./ai-panel ask topic.md
./ai-panel debate topic.md
./ai-panel debate topic.md --preset deep
./ai-panel debate topic.md --judge claude --model claude=opus --model gemini=gemini-2.5-pro
./ai-panel show <run-id>
./ai-panel doctor
./ai-panel serve
```

- `ask`: Claude/Gemini/Codex에 독립 답변을 병렬 요청합니다.
- `debate`: 독립 답변, 상호 비판 1회, 최종 요약까지 실행합니다.
- `show`: 저장된 run의 파일 경로를 출력합니다.
- `doctor`: 설정 파일과 CLI 설치 여부를 점검합니다.
- `serve`: 로컬 웹 UI를 실행합니다.

## 개발

테스트는 레포 루트에서 아래 명령으로 실행합니다.

```bash
python3 -m unittest discover
```

## 설정

기본 설정은 `agents.yaml`에 있습니다. 이 파일은 YAML 파일명이지만 JSON-compatible YAML 문법을 사용합니다.

UI 디자인 기준은 `DESIGN.md`에 있습니다. Mintlify의 읽기 중심 문서 UI, Notion의 따뜻한 중립 표면, Claude의 모델 비교 카드 스타일을 섞은 방향입니다.

각 agent command에서 `{prompt}`가 있으면 해당 위치에 프롬프트를 인자로 넣고, 없으면 프롬프트를 stdin으로 전달합니다. 긴 토론 프롬프트가 OS 인자 길이 제한에 걸리지 않도록 기본 설정은 stdin 방식을 우선합니다.

```json
{
  "timeout_seconds": 900,
  "judge": "codex",
  "agents": [
    {
      "id": "claude",
      "command": ["claude", "--print", "--output-format", "text", "--permission-mode", "plan"]
    }
  ]
}
```

## 산출물

```text
runs/<timestamp>/
  topic.md
  meta.json
  round1/
    claude.md
    gemini.md
    codex.md
  round2/
    claude_critique.md
    gemini_critique.md
    codex_critique.md
  summary.md
```

특정 CLI가 실패해도 가능한 결과는 저장하고, 실패 사유는 `meta.json`과 해당 출력 파일에 남깁니다.
