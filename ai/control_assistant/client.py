"""GMS(OpenAI 호환) chat.completions 호출 한 겹.

openai SDK를 안 쓰고 httpx로 직접 친다 — 백엔드가 이미 httpx를 물고 있어서 의존성이
안 늘고, 우리가 쓰는 건 /chat/completions 하나뿐이라 SDK가 주는 이득이 없다.

⚠ reasoning_effort는 이 프록시가 400으로 거부한다(1학기 실측). 보내지 않는다.
⚠ gpt-5·o시리즈는 max_tokens를 400으로 거부하고(max_completion_tokens만 받음)
   temperature도 기본값 외엔 거부한다(7/29 실측). 모델 이름 보고 페이로드를 가른다.
⚠ 실패는 전부 LLMUnavailable 하나로 모은다 — 부르는 쪽이 예외 종류를 안 가리고
   폴백 한 갈래로만 내려가게 하려는 거다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import httpx

from .config import AssistantConfig

logger = logging.getLogger("c207.assistant")

# 연결이 이보다 오래 걸리면 서버가 느린 게 아니라 안 닿는 것이다. 읽기 상한과 따로 잡는다.
CONNECT_TIMEOUT_SEC = 3.0

# 이력을 몇 메시지까지 싣나. 두 턴(질문·답 두 쌍)이다.
# ⚠ 넉넉히 잡으면 프리필이 늘어 지연을 도로 까먹는다. 짧게 시작해 재고 늘리는 편이 낫다.
MAX_HISTORY_MESSAGES = 4
_ROLES = ("user", "assistant")

# 창구 하나를 몇 번 태우나. llama-server 슬롯마다 캐시가 따로라 `--parallel` 수와 맞춘다.
# ⚠ `ai/ops/llm_env.sh` 의 `LLM_PARALLEL` 이 정본이다. 거기를 올리면 여기도 올려야 한다.
WARM_ROUNDS = 2

# ⛔ 데우기는 읽기 상한을 따로 길게 준다(2026-08-07 실측으로 잡음).
#
# 데우기를 동시에 던지면 찬 슬롯 둘이 **같은 GPU 를 나눠 쓰며 서로를 늦춘다.** 한 개면
# 7초에 끝날 프리필이 둘이 겹치면 12초를 넘어, 평소 상한(12초)으로는 **데우기 자체가
# 전부 타임아웃**했다. 실측에서 `warmed: 0` 이 나왔다.
#
# ⭐ 데우기는 사람이 기다리는 호출이 아니다. 기동 때 한 번 도는 것이라 넉넉히 줘도
# 손해가 없다. 반대로 여기서 실패하면 **요원의 첫 질문이 그 값을 대신 문다.**
WARM_TIMEOUT_SEC = 45.0


def _clean_history(history: list | None) -> list[dict]:
    """이력을 믿을 수 있는 모양으로 만든다.

    ⛔ **이력에도 인젝션 방어를 태운다.** 요원이 친 질문이 그대로 이력에 남으므로,
    한 턴 전에 심은 지시문이 다음 턴에 살아나는 길이 생긴다.

    ⚠ 역할은 user·assistant 만 받는다. system 이 섞이면 우리 규칙을 덮어쓴다.
    """
    if not history:
        return []

    from . import injection          # 순환 임포트를 피해 안에서 부른다

    out = []
    for msg in history[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role not in _ROLES or not isinstance(content, str) or not content.strip():
            continue
        safe, _ = injection.neutralize(content)
        out.append({"role": role, "content": safe})
    return out


def _is_reasoning_family(model: str) -> bool:
    """max_tokens·temperature를 거부하는 추론 계열인가 (gpt-5.x·o시리즈)."""
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _is_local_endpoint(base_url: str) -> bool:
    """GMS 프록시가 아니면 우리가 띄운 llama-server로 본다.

    ⭐ 판별을 base_url로 하는 이유 — 전환 스위치가 `backend.env` 한 곳이라
    코드를 다시 안 건드리고 켜고 끌 수 있다. 모델 이름으로 가르면 모델을 바꿀 때마다
    여기를 같이 고쳐야 한다.
    """
    return "gms.ssafy.io" not in base_url


class LLMUnavailable(RuntimeError):
    """LLM 응답을 못 받았다. reason은 폴백 응답에 실려 원인 추적에 쓰인다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def complete(
    cfg: AssistantConfig, *, system: str, user: str, history: list | None = None
) -> str:
    """system·user 두 메시지로 한 번 호출하고 본문 텍스트만 돌려준다.

    JSON 강제(response_format=json_object)나 function calling도 이 프록시에서 되지만
    관제 보조 3종은 결과가 한국어 문단이라 안 쓴다. 형식을 JSON으로 받아 다시 풀면
    실패 지점만 하나 늘어난다.
    """
    if not cfg.enabled:
        raise LLMUnavailable("no_api_key")

    # ⭐ 이력은 system 다음, 지금 질문 앞에 넣는다. 이 순서라야 **system 몫이 프롬프트
    # 캐시에 그대로 남는다** — 이력을 맨 앞에 두면 턴이 바뀔 때마다 캐시가 통째로 깨진다
    # (2026-08-07 실측 — 같은 system 으로 두 번 부르면 프리필이 2,562 → 577토큰이다).
    messages = [{"role": "system", "content": system}]
    messages.extend(_clean_history(history))
    messages.append({"role": "user", "content": user})

    payload = {"model": cfg.model, "messages": messages}
    if _is_reasoning_family(cfg.model):
        # 이 계열은 내부 추론 토큰까지 이 상한에서 까먹는다. gpt-5.4-mini는 기본 추론이
        # 0이라 400으로 충분한 걸 실측했고, 무거운 모델로 갈아탈 땐 GMS_MAX_TOKENS를 올려라.
        payload["max_completion_tokens"] = cfg.max_tokens
    else:
        # 관제 문장이라 매번 다른 표현이 나올 이유가 없다. 낮게 고정한다.
        payload["temperature"] = 0.2
        payload["max_tokens"] = cfg.max_tokens

    if _is_local_endpoint(cfg.base_url):
        # ⛔ Qwen3.6 같은 추론 모델은 이걸 안 끄면 상한을 생각에 다 태우고 content가 빈다.
        # 2026-08-06 V100 실측 — 켠 채로 1200토큰을 주니 36.5초를 쓰고 본문이 빈 문자열이었고
        # (생각만 영어로 3,026자), 끄니 6.6초에 170토큰짜리 한국어 브리핑이 나왔다.
        # 빈 문자열은 아래에서 `empty_response`로 잡혀 폴백으로 가니 화면은 안 비지만,
        # 그러면 로컬 LLM을 붙여 놓고 규칙 문장만 보게 된다.
        # ⚠ GMS 프록시에는 안 보낸다 — 모르는 필드를 400으로 거부한 전례가 있다(reasoning_effort).
        payload["chat_template_kwargs"] = {"enable_thinking": False}

    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    url = f"{cfg.base_url.rstrip('/')}/chat/completions"

    # ⛔⛔ **연결과 읽기를 갈라야 한다**(2026-08-07 시연 리스크 점검에서 잡음).
    # 예전에는 `timeout=cfg.timeout_sec` 하나만 줘서 **연결 시도에도 같은 상한**이 걸렸다.
    # 역터널이 끊긴 상태를 흉내내 재 보니 첫 실패에 **20초를 꽉 채웠다** — 시연 중이면
    # 요원이 20초 동안 멈춘 화면을 본다.
    #
    # ⭐ 서버가 살아 있으면 연결은 같은 호스트라 순식간이다. 3초를 넘기면 그건 느린 게
    # 아니라 안 닿는 것이다. 읽기 상한은 그대로 두어 생성이 느린 판을 성급히 안 끊는다.
    timeout = httpx.Timeout(cfg.timeout_sec, connect=CONNECT_TIMEOUT_SEC)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        logger.warning("LLM 타임아웃 — 폴백으로 간다: %s", exc)
        raise LLMUnavailable("timeout") from exc
    except httpx.HTTPError as exc:
        logger.warning("LLM 전송 실패 — 폴백으로 간다: %s", exc)
        raise LLMUnavailable("transport_error") from exc

    if resp.status_code != 200:
        # 본문을 통째로 로그에 담지 않는다(키가 실린 에러 본문이 그대로 남는 걸 막는다).
        logger.warning("LLM 비정상 응답 %s — 폴백으로 간다", resp.status_code)
        raise LLMUnavailable(f"http_{resp.status_code}")

    try:
        choice = resp.json()["choices"][0]
        text = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        logger.warning("LLM 응답 모양이 계약과 다르다 — 폴백으로 간다: %s", exc)
        raise LLMUnavailable("bad_response_shape") from exc

    # ⛔ **잘린 답도 200으로 온다.** 상한에 걸려 문장이 중간에 끊겨도 HTTP는 정상이고
    # `content`도 비어 있지 않아서, 위 검사만으로는 그대로 화면에 나간다.
    # "…무단 통과가 3건이고 경고는" 처럼 끊긴 문장이 요원에게 보인다.
    #
    # ⚠ 대화 기록 3턴을 붙이면 400토큰이 확실히 모자라진다. 붙이기 전에 이 못을 먼저 박는다
    # (2026-08-07 교차 조사에서 잡힘).
    finish = choice.get("finish_reason")
    if finish and finish not in ("stop", "eos"):
        logger.warning("LLM 답이 잘렸다(finish_reason=%s) — 폴백으로 간다", finish)
        raise LLMUnavailable("truncated")

    text = (text or "").strip()
    if not text:
        # 빈 문자열을 화면에 그대로 내보내면 "LLM이 답했는데 칸이 비었다"가 된다.
        raise LLMUnavailable("empty_response")
    return text


async def warm_up(cfg: AssistantConfig, systems: list[str]) -> dict:
    """시스템 프롬프트를 미리 한 번씩 태워 서버 캐시에 얹는다.

    ## ⛔ 왜 필요한가 (2026-08-07 실측)

    시스템 프롬프트에 계약 지식을 넣으면서 10,688자가 됐다. 캐시가 없는 첫 호출은
    그 4,870토큰을 통째로 프리필한다.

        캐시 없을 때 첫 호출  7.36초 = 프리필 4,870톡 6.72초 + 디코딩 19톡 0.57초
        두 번째(캐시 삶)      1.94초 = 프리필     4톡 0.25초 + 디코딩 53톡 1.63초

    ⛔ **읽기 상한이 8초라 첫 호출이 아슬아슬하다.** 실제로 실사용 질문 하나가 상한에
    걸려 규칙 문장으로 떨어졌다. **시연에서 요원의 첫 질문이 폴백으로 나갈 수 있다.**

    ⭐ 답은 상한을 늘리는 것이 아니라 **첫 호출을 미리 태워 두는 것**이다. 두 번째부터는
    프리필이 4토큰이라 사실상 공짜다.

    ## ⛔⛔ 순차로 태우면 슬롯 하나만 데워진다 (2026-08-07 실측으로 잡음)

    슬롯마다 캐시가 따로인데, **순차로 부르면 llama-server 가 방금 비운 같은 슬롯을
    다시 고른다.** 앞 요청이 끝나 슬롯이 놀고 있고 접두사까지 같으니 당연한 선택이다.
    그래서 `WARM_ROUNDS = 2` 로 두 번 태워도 **슬롯 1 은 한 번도 안 데워졌다.**

    증상이 고약하다. 데운 뒤에 요원 둘이 **동시에** 물으면 슬롯 0·1 을 하나씩 쓰는데,
    슬롯 1 이 차가우니 거기서 4,870토큰을 프리필한다. 게다가 두 요청이 GPU 를 나눠 쓰니
    서로를 늦춘다.

        순차로 데운 뒤 · 동시 두 질문   →  둘 다 12.0초 (읽기 상한 초과, 둘 다 폴백)
        동시에 데운 뒤 · 동시 두 질문   →  2.52초 · 2.81초

    ⭐ 그래서 **동시에** 태운다. 슬롯이 다 차 있어야 llama-server 가 빈 슬롯을 새로
    고르고, 그렇게 해야 슬롯 수만큼 캐시가 얹힌다.

    ⚠ 창구 사이는 여전히 순차다. 세 창구를 한꺼번에 던지면 슬롯 둘에 여섯이 몰려
    서로 밀어낸다. 한 창구씩 슬롯을 채우고 다음으로 넘어간다.
    ⚠ 실패해도 그냥 넘어간다 — 데우기가 안 됐다고 서비스가 안 뜨면 안 된다.
    """
    out = {"warmed": 0, "failed": 0}
    if not cfg.enabled:
        return out

    # ⛔ 평소 상한(12초)으로는 데우기가 통째로 타임아웃한다 — `WARM_TIMEOUT_SEC` 머리말.
    warm_cfg = replace(cfg, timeout_sec=WARM_TIMEOUT_SEC)

    for system in systems:
        results = await asyncio.gather(
            *(complete(warm_cfg, system=system, user="준비") for _ in range(WARM_ROUNDS)),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, BaseException):
                out["failed"] += 1
                reason = getattr(r, "reason", type(r).__name__)
                logger.info("캐시 데우기 건너뜀 — %s", reason)
            else:
                out["warmed"] += 1
    return out
