"""요약 파이프라인 3종. 사실 딕셔너리를 받아 문장과 생성 주체를 돌려준다.

계약이 딱 하나다 — **백엔드는 DB에서 사실을 모아 딕셔너리로 넘기고, 여기서는 DB를 모른다.**
그래서 이 모듈은 SQLAlchemy도 FastAPI도 임포트하지 않고, 시험도 딕셔너리만으로 돈다.

어느 갈래로 가든 text는 항상 채워진다. generated_by가 "llm"이면 LLM이 쓴 문장이고
"fallback"이면 규칙 기반 문장이며, 이때 fallback_reason에 원인이 실린다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import fallback, guards, prompts
from .client import LLMUnavailable, complete, warm_up as _warm_up
from .config import AssistantConfig

# 프로세스 하나에 하나. 세 창구가 같은 서버를 보므로 차단기도 하나여야 뜻이 맞는다 —
# 일일 요약이 세 번 연달아 죽었으면 챗봇도 안 될 것이 뻔하다.
_BREAKER = guards.CircuitBreaker()

# ⭐ 서버가 살아 있다는 뜻인 실패들. 차단기가 세지 않는다.
#   truncated          — 답이 길어 상한에서 잘렸다. 서버는 정상으로 200을 줬다
#   empty_response     — 내용이 비었다. 추론을 못 끄면 나는 자리라 서버 문제가 아니다
#   bad_response_shape — 응답 모양이 계약과 다르다. 계약 문제지 서버 죽음이 아니다
SERVER_ALIVE_REASONS = frozenset({"truncated", "empty_response", "bad_response_shape"})


@dataclass(frozen=True)
class AssistantText:
    text: str
    generated_by: str  # "llm" | "fallback"
    fallback_reason: str | None = None


def reset_breaker() -> None:
    """차단기를 닫는다. 시험 위생용 — 케이스 사이에 상태가 새면 뒤 케이스가 가짜로 실패한다."""
    _BREAKER.reset()


async def _run(
    cfg: AssistantConfig,
    *,
    system: str,
    user: str,
    fallback_text: str,
    facts: dict,
    history: list | None = None,
) -> AssistantText:
    """LLM 한 번 → 실패면 규칙 문장. 폴백 문장은 호출 전에 이미 만들어 둔다.

    미리 만드는 이유는 폴백 자체가 또 터지는 걸 막기 위해서다 — LLM이 실패한 뒤에야
    폴백 문장을 만들면, 사실 딕셔너리가 이상할 때 응답이 통째로 500이 된다.

    ⭐ 갈래가 셋이다.
      ① 차단기가 열려 있으면 **부르지도 않는다**(`circuit_open`).
      ② 호출이 터지면 폴백(`timeout`·`http_error`…).
      ③ 답은 왔는데 사후 검사에 걸리면 폴백(`hanja_leak`·`number_mismatch`…).
    셋 다 사유를 남긴다 — 안 남기면 "LLM이 붙었는데 왜 규칙 문장이지"를 사람이 추측하게 된다.
    """
    now = time.monotonic()

    # ⛔⛔ **키가 없는 것을 차단기가 실패로 세고 있었다**(2026-08-07 전량 시험이 잡음).
    # 차단기는 "LLM 서버가 죽었나"를 재는 물건인데, 키가 없는 건 서버 문제가 아니다.
    # 키 없는 배포에서 세 번째 호출부터 사유가 `no_api_key` 가 아니라 `circuit_open` 으로
    # 바뀌어 나갔다 — **화면이 요원에게 틀린 원인을 적어 준다.**
    # ⚠ 단독으로 돌리면 안 걸리고 전량에서만 걸린다. 앞 케이스가 차단기를 열어 두기 때문이다.
    if not cfg.enabled:
        return AssistantText(fallback_text, "fallback", "no_api_key")

    if _BREAKER.is_open(now):
        return AssistantText(fallback_text, "fallback", "circuit_open")

    try:
        text = await complete(cfg, system=system, user=user, history=history)
    except LLMUnavailable as exc:
        # ⛔⛔ **서버가 200을 돌려준 실패는 차단기가 안 센다**(2026-08-07 반대 심문이 잡음).
        # 차단기는 "LLM 서버가 죽었나"를 재는 물건이다. 답이 길어 잘렸거나(`truncated`)
        # 모양이 이상한 것은 **서버가 살아서 답을 준 것**이라 뜻이 다르다.
        #
        # ⚠ 이 구분이 없으면 되먹임이 생긴다 — 답을 길게 쓰라고 완화할수록 `truncated`가
        # 늘고, 세 번 연달으면 차단기가 열려 **30초 동안 LLM 을 아예 안 부른다.**
        # 게다가 차단기가 세 창구 공용이라 일일 요약이 세 번 잘리면 챗봇도 같이 죽는다.
        #
        # ⭐ 오늘 아침 `no_api_key` 를 뺀 것과 같은 계열이다. 그때 이 자리를 안 봤다.
        if exc.reason not in SERVER_ALIVE_REASONS:
            _BREAKER.record_failure(now)
        return AssistantText(fallback_text, "fallback", exc.reason)

    # 호출 자체는 됐으니 차단기는 닫는다. 내용이 나빠도 서버는 살아 있다.
    _BREAKER.record_success()

    reason = guards.check(text, facts)
    if reason:
        return AssistantText(fallback_text, "fallback", reason)
    return AssistantText(text, "llm")


async def daily_summary(cfg: AssistantConfig, facts: dict) -> AssistantText:
    """하루치 집계 → 한국어 브리핑 문단."""
    return await _run(
        cfg,
        system=prompts.DAILY_SUMMARY_SYSTEM,
        user=prompts.daily_summary_user(facts),
        fallback_text=fallback.daily_summary(facts),
        facts=facts,
    )


async def alert_brief(cfg: AssistantConfig, facts: dict) -> AssistantText:
    """경고 한 건 + 주변 맥락 → 한 문장 브리핑."""
    return await _run(
        cfg,
        system=prompts.ALERT_BRIEF_SYSTEM,
        user=prompts.alert_brief_user(facts),
        fallback_text=fallback.alert_brief(facts),
        facts=facts,
    )


async def chat(
    cfg: AssistantConfig, question: str, facts: dict, history: list | None = None
) -> AssistantText:
    """질문 + 조회 데이터 → 단순 QA 답변.

    툴콜링을 안 붙인다. LLM이 부를 수 있는 함수가 없으면 로봇 제어 갈래도 원천적으로 없다
    (정직 라인 — 조사원문 "LLM 활용처" ③·다른 팀 비교 절).

    ⭐ `history`는 최근 대화다. `[{"role": "user"|"assistant", "content": str}, …]` 모양이고
    안 주면 예전과 똑같이 돈다. 안 주면 이어지는 질문("그럼 어제는?")을 못 받는다.

    ⚠ 몇 개를 싣든 `client.MAX_HISTORY_MESSAGES`에서 잘린다. 역할이 이상하거나 빈 내용은
    거기서 걸러지고, 지시문 무늬도 거기서 중화된다 — **한 턴 전에 심은 지시가 다음 턴에
    살아나는 길**을 막는 자리다.
    """
    return await _run(
        cfg,
        system=prompts.CHAT_SYSTEM,
        user=prompts.chat_user(question, facts),
        # ⭐ 질문을 같이 넘긴다 — 폴백이 질문 갈래에 맞는 문장을 앞에 놓는다.
        #    예전에는 무단 통과를 물어도 로봇 대수부터 답했다(2026-08-07 실사용).
        fallback_text=fallback.chat_answer(facts, question),
        facts=facts,
        history=history,
    )


async def warm_up(cfg: AssistantConfig) -> dict:
    """세 창구의 시스템 프롬프트를 미리 태운다. 서버가 뜬 뒤 한 번 부르면 된다.

    ⛔ 캐시가 없는 첫 호출이 **7.36초**다(2026-08-07 실측). 읽기 상한이 8초라
    시연에서 요원의 첫 질문이 규칙 문장으로 떨어질 수 있다. 자세한 근거는
    `client.warm_up` 머리말에 있다.

    ⚠ 실패해도 예외를 안 던진다 — 데우기가 안 됐다고 서비스가 안 뜨면 안 된다.

    ⛔ **순서가 곧 무엇이 데워진 채 남나다 — 마지막 하나가 두 슬롯을 전부 먹는다.**
    `client.warm_up` 은 창구 하나마다 WARM_ROUNDS(2)개를 gather 로 **동시에** 던지고
    슬롯도 둘(LLM_PARALLEL=2)이라, 창구가 바뀔 때마다 두 칸이 통째로 갈린다. "마지막
    둘이 남는다"가 아니다 — 끝에서 둘째 창구도 안 남는다(2026-08-08 교차 검증 정정).
    실서버 `/slots` 두 칸이 둘 다 alert_brief 였던 관측(옛 순서의 마지막 하나)이 그
    증거인데, 실사용(경고 카드마다 alert_brief 자동 호출)이 덮었을 수도 있어 정황이다.
    순서를 정하는 진짜 근거는 **접두 길이**다 — 셋 다 _COMMON_RULES(2,439자)로 시작해
    alert·daily 는 꼬리 259·823자만 다시 태우면 되는데, chat 은 _CHAT_CONTEXT 까지
    고유 접두 12,065자를 다시 문다(찬 호출 7초대). 그래서 **접두가 제일 긴 chat 을 맨
    끝에** 둔다. 창구를 늘리거나 순서를 바꿀 때 이 문단부터 다시 읽어라.
    ⚠ 이 순서는 기동부터 첫 경고 전까지만 산다 — 시연 중 경고가 오면 화면이 부르는
    alert_brief 가 슬롯을 도로 덮는다. 근본 후보는 --parallel 3 또는 주기 재데우기다.
    """
    return await _warm_up(cfg, [
        prompts.ALERT_BRIEF_SYSTEM,
        prompts.DAILY_SUMMARY_SYSTEM,
        prompts.CHAT_SYSTEM,
    ])
