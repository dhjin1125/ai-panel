from __future__ import annotations


FORMAT_SECTIONS = {
    "round1": ["결론", "근거", "불확실한 점", "실행 제안"],
    "round2": ["동의하는 점", "문제점", "빠진 관점", "수정 제안"],
    "summary": ["한줄 결론", "모델별 차이", "가장 신뢰할 주장", "주의할 점", "추천 답변"],
}


def independent_prompt(topic: str) -> str:
    return f"""당신은 독립 평가자입니다.

아래 논제에 대해 다른 모델의 답변을 보지 않았다고 가정하고 답하세요.
한국어로 간결하게 답하세요.
불확실한 내용은 단정하지 말고 불확실하다고 표시하세요.
반드시 아래 Markdown 제목을 이 순서 그대로 사용하세요:

## 결론
## 근거
## 불확실한 점
## 실행 제안

<논제>
{topic}
</논제>
"""


def critique_prompt(topic: str, own_agent: str, answers: dict[str, str]) -> str:
    other_answers = "\n\n".join(
        f"<{agent} 답변>\n{answer}\n</{agent} 답변>"
        for agent, answer in answers.items()
        if agent != own_agent
    )
    return f"""당신은 비판 검토자입니다.

아래 논제와 다른 모델들의 답변을 보고, 오류 가능성, 약한 근거, 빠진 관점, 더 나은 결론을 한국어로 지적하세요.
새로운 최종 답변을 쓰지 말고 비판과 보완 제안에 집중하세요.
반드시 아래 Markdown 제목을 이 순서 그대로 사용하세요:

## 동의하는 점
## 문제점
## 빠진 관점
## 수정 제안

<논제>
{topic}
</논제>

{other_answers}
"""


def summary_prompt(
    topic: str,
    answers: dict[str, str],
    critiques: dict[str, str],
    failures: list[str],
) -> str:
    answer_block = "\n\n".join(
        f"<{agent} round1>\n{answer}\n</{agent} round1>"
        for agent, answer in answers.items()
    )
    critique_block = "\n\n".join(
        f"<{agent} critique>\n{critique}\n</{agent} critique>"
        for agent, critique in critiques.items()
    )
    failure_block = "\n".join(f"- {failure}" for failure in failures) or "- 없음"

    return f"""당신은 최종 정리자입니다.

아래 논제, 각 모델의 독립 답변, 상호 비판, 실패 정보를 바탕으로 한국어 최종 요약을 작성하세요.
사용자가 바로 읽고 결정할 수 있게 짧고 명확하게 정리하세요.
반드시 아래 Markdown 제목을 이 순서 그대로 사용하세요:

## 한줄 결론
## 모델별 차이
## 가장 신뢰할 주장
## 주의할 점
## 추천 답변

<논제>
{topic}
</논제>

<실패 정보>
{failure_block}
</실패 정보>

{answer_block}

{critique_block}
"""


def check_format(stage: str, text: str) -> dict:
    expected = FORMAT_SECTIONS.get(stage, [])
    headings = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        title = stripped.lstrip("#").strip()
        if title:
            headings.append(title)
    missing = [section for section in expected if section not in headings]
    positions = [headings.index(section) for section in expected if section in headings]
    ordered = positions == sorted(positions)
    return {
        "ok": not missing and ordered,
        "expected": expected,
        "found": headings,
        "missing": missing,
        "ordered": ordered,
    }
