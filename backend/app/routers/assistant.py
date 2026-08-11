"""관제 보조 API 3종 (P1 #7 "LLM 관제 보조" — 준비패키지 §2 AI 활용 지도).

- GET  /api/assistant/daily-summary        하루치 이벤트·경고 → 한국어 브리핑 문단
- GET  /api/assistant/alert-brief/{id}     경고 한 건의 발생 맥락 → 한 문장 브리핑
- POST /api/assistant/chat                 조회 데이터만 근거로 답하는 단순 QA

라우터는 얇게 둔다 — 사실을 모으는 건 app.assistant_data, 문장을 만드는 건 리포 /ai다.
여기서는 요청을 받아 그 둘을 잇고 응답 모양만 맞춘다.

정직 라인 — 이 계열은 전부 읽기 전용이다. 로봇 명령을 내보내는 갈래가 없고 툴콜링도 안
붙인다. LLM이 부를 수 있는 함수가 아예 없으니 "LLM이 로봇을 움직였다"가 성립하지 않는다.

⚠ 조회 계열이지만 인증(X-API-Key)을 건다 — query.py의 목록 API와 다른 대접이다. 이 셋은
읽기만 해도 GMS 크레딧을 태우기 때문이다(팀 공용 10만 크레딧). 인터넷에 열어 두면 아무나
반복 호출로 팀 크레딧을 다 빨아낼 수 있고, 그건 조회 부하가 아니라 소진 사고다.
"""
from __future__ import annotations

import collections
import dataclasses
import datetime as dt
import json
import logging
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import ai_bridge, assistant_data
from app.config import get_settings
from app.db import get_db
from app.models import Alert, AssistantDailyBrief, ChatConversation, ChatMessage
from app.schemas import (
    AlertBriefOut,
    AssistantMeta,
    ChatIn,
    ChatMessageOut,
    ChatOut,
    ConversationDetailOut,
    ConversationOut,
    DailySummaryOut,
)
from app.auth import require_agent
from app.security import key_gate_or, require_api_key

# ⭐ AI 탭 세션 기반 전환 (로그인 설계 확정본 §3). 세 창구 전부 요원이다(§8 확정 C —
# 요원부터 전부 노출).
#
# 키와 세션을 **둘 다 걸지 않는다.** 둘 다면 브라우저가 여전히 기기 키를 들어야 해서
# -238("브라우저가 든 키는 개발자 도구에 그대로 보인다")이 그대로 남는다. 그래서 갈아
# 끼우되, 갈아 끼우는 순간을 `AUTH_REQUIRE_LOGIN`이 쥔다 — 꺼진 구간엔 옛 키 게이트가
# 그대로 살아서 off 구간이 무인증으로 열리지 않는다(설계 검토 P1 반영).
#
# 크레딧 소진 방어(모듈 머리)는 켠 뒤에도 남는다 — 키가 아니라 nginx `c207_ai` 존과
# 세션 단위 제한이 맡는다. GMS 키는 서버 안쪽 ai_bridge가 붙이므로 브라우저엔 안 내려간다.
logger = logging.getLogger("c207.assistant")

# 게이트 함수를 **한 번만 만든다.** `key_gate_or`는 부를 때마다 새 함수를 내주는데, 두 번
# 만들어 걸면 FastAPI가 서로 다른 의존성으로 보고 인증을 두 번 태운다.
_gate = key_gate_or(require_agent, require_api_key)

# ── 세션 단위 호출 제한 (2026-08-02 백지 검토 3차 X47) ─────────────────────
#
# 모듈 머리와 로그인 설계초안 §5가 "세션 단위 제한"을 계약으로 적어 놨는데 그 자리가 비어
# 있었다. 앱 안 카운터가 0건이고 nginx `c207_ai` 존은 `$binary_remote_addr`, 곧 **IP만**
# 센다 — 학내 NAT면 여럿이 한 버킷을 나눠 쓰고, 반대로 세션 하나가 IP를 갈면 벗어난다.
#
# 세는 방식은 출동 창구(`app/dispatch.check_command_throttle`)와 같은 결이다. 창 안에 찍힌
# 눈금을 세고 넘으면 429 + Retry-After다.
#
# ⚠ **"1초에 한 번"이 아니라 "60초에 60번"이다.** 평균은 nginx 존(1r/s)과 같지만 몰아치기가
# 되는 모양이라, 창 하나 안에서 60연발이 그대로 지나간다(2026-08-04 A3 — 예전 주석이
# "nginx 존과 같은 1r/s로 맞췄다"고만 적어서 순간 폭주를 막는 것처럼 읽혔다). nginx를 지나면
# burst 5에 눌리지만, 컨테이너 8000을 직접 부르는 경로는 그 앞단이 없다. 화면이 탭 하나에서
# 브리핑·경고·챗봇을 한꺼번에 그리는 흔한 모양을 막지 않으려고 창을 넓게 뒀고, 크레딧
# 소진을 진짜로 막는 건 아래 **하루 예산**이다.
#
# ⚠ 인메모리다(워커 1개 전제, `app/auth.py` 잠금 표와 같은 자리). 창이 빈 항목은 지워서
# 표가 안 자라고, 인증 게이트를 지난 요청만 여기 닿는다.
_CALL_WINDOW_SEC = 60.0
_CALL_MAX_IN_WINDOW = 60
_calls: dict[str, collections.deque[float]] = {}


def _throttle_key(request: Request, actor: object | None) -> str:
    """무엇으로 세나. 로그인이 켜졌으면 **사람**, 꺼져 있으면 부른 자리(IP)다.

    켜진 뒤에는 세션을 쥔 사람이 단위라 IP를 갈아도 안 벗어난다. 꺼져 있는 구간에는 세션이
    없으므로(게이트가 기기 키를 본다) IP로 떨어진다.
    """
    user_id = getattr(actor, "id", None)
    if user_id is not None:
        return f"user:{user_id}"
    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


def _check_call_quota(key: str) -> float:
    """창 안 호출 수를 센다. 넘으면 남은 초(>0), 아니면 0이고 눈금을 하나 찍는다."""
    now = time.monotonic()
    marks = _calls.setdefault(key, collections.deque())
    while marks and now - marks[0] >= _CALL_WINDOW_SEC:
        marks.popleft()
    if len(marks) >= _CALL_MAX_IN_WINDOW:
        return _CALL_WINDOW_SEC - (now - marks[0])
    marks.append(now)
    # 창이 빈 항목은 들고 있을 이유가 없다(열쇠가 IP일 수 있어 표가 자라는 걸 여기서 막는다).
    for stale in [k for k, v in _calls.items() if not v]:
        del _calls[stale]
    return 0.0


def reset_call_quota() -> None:
    """호출 눈금을 비운다. 시험 위생용(DB 밖 상태라 TRUNCATE로 안 지워진다)."""
    global _llm_day, _llm_calls_today, _no_key_warned
    # ⚠ 일일 요약 지문(아래 `_brief_facts_*`)도 같이 비운다 — 이것도 DB 밖 상태라 시험이
    #    표를 지워도 안 지워지고, 남아 있으면 다음 케이스가 남의 지문으로 캐시를 판정한다.
    global _brief_facts_day, _brief_facts_seen
    _calls.clear()
    _llm_day = ""
    _llm_calls_today = 0
    _no_key_warned = False
    _brief_facts_day = ""
    _brief_facts_seen = ""


# ── 하루 LLM 예산 (2026-08-04 수리 A1 — 크레딧 상한이 코드에 한 줄도 없었다) ──────
#
# 모듈 머리가 "읽기만 해도 GMS 크레딧을 태운다"고 적어 뒀는데, 정작 있는 건 연타 제한뿐이라
# **하루 총량 방어가 0이었다.** 화면이 브리핑을 타이머로 갱신하거나 탭을 열어 둔 채 두면 팀
# 공용 10만 크레딧이 조용히 준다(브리핑 한 번이 14크레딧이다).
#
# ⭐ 다 쓰면 **429가 아니라 폴백 문장**이다. 크레딧이 떨어졌다고 화면이 통째로 깨지면 안
# 된다 — 문장 품질만 내려가고 창구는 200 그대로다. 폴백이라는 사실은 응답
# `meta.fallback_reason`이 `daily_budget`으로 말한다.
#
# ⚠ 인메모리다(워커 1개 전제, 위 연타 제한과 같은 자리). 재기동하면 그날 몫이 다시 찬다 —
# 시연 규모에서는 그게 맞는 쪽이다(진짜 정산은 GMS 대시보드가 한다).
_DAILY_LLM_CALLS = 300

# 오늘이 며칠인지(KST)와 오늘 쓴 수.
_llm_day = ""
_llm_calls_today = 0
_no_key_warned = False


def _daily_llm_limit() -> int:
    """`Settings`의 `assistant_daily_llm_calls`를 읽는다. 0 이하면 LLM을 아예 안 부른다.

    ⭐ **2026-08-07에 그 필드가 실제로 생겼다.** 그전까지는 `Settings`에 이름이 **없어서**
    `getattr` 기본값 300으로 늘 떨어졌다 — 오타가 아니라 없는 이름이라 로그도 안 남았고,
    `.env`에 값을 적어도 안 먹었다(AI 갈래 3차 ③이 잡았다).

    ⚠ `getattr`을 그대로 두는 까닭 — `app/config.py`는 여러 조가 같이 만지는 파일이라
    누가 필드를 지워도 여기가 죽지 않게 한다. **다만 이제는 있는 이름이라 `.env`가 먹는다.**
    """
    try:
        return int(getattr(get_settings(), "assistant_daily_llm_calls", _DAILY_LLM_CALLS))
    except (TypeError, ValueError):
        return _DAILY_LLM_CALLS


def _take_llm_budget() -> bool:
    """오늘 몫이 남았으면 하나 쓰고 True. 다 썼으면 False다."""
    global _llm_day, _llm_calls_today
    today = assistant_data.today_kst().isoformat()
    if today != _llm_day:
        _llm_day = today
        _llm_calls_today = 0
    limit = _daily_llm_limit()
    if _llm_calls_today >= limit:
        return False
    _llm_calls_today += 1
    if _llm_calls_today == limit:
        logger.warning(
            "관제 보조 하루 LLM 예산 %d회를 다 썼다 — 오늘 남은 요청은 규칙 기반 문장으로"
            " 나간다(응답 meta.fallback_reason=daily_budget).",
            limit,
        )
    return True


def _llm_config() -> tuple[ai_bridge.AssistantConfig, bool]:
    """이번 요청이 쓸 LLM 설정과 "예산 때문에 내렸나" 표시.

    ⚠ 키가 비어 있으면 **한 번은 시끄럽게 남긴다.** 그 판에서는 LLM을 아예 안 부르고 규칙
    문장이 200으로 나가는데, 응답만 봐서는 그게 안 드러난다(`meta.generated_by`를 그리는
    화면이 아직 없다). 지금 EC2가 정확히 그 상태라, 로그 한 줄이 배포 상태를 말해 준다.
    """
    global _no_key_warned
    cfg = ai_bridge.build_config()
    if not cfg.api_key:
        if not _no_key_warned:
            _no_key_warned = True
            logger.warning(
                "GMS_API_KEY가 비어 있다 — 관제 보조 3종이 LLM을 안 부르고 규칙 기반"
                " 문장으로 답한다(응답 meta.generated_by=fallback)."
            )
        return cfg, False
    if not _take_llm_budget():
        return dataclasses.replace(cfg, api_key=""), True
    return cfg, False


async def _quota_gate(
    request: Request, actor: object | None = Depends(_gate)
) -> object | None:
    """인증을 지난 뒤 호출 수를 센다. 게이트가 먼저 도니 익명 요청은 이 표에 안 닿는다."""
    remain = _check_call_quota(_throttle_key(request, actor))
    if remain > 0:
        logger.warning(
            "관제 보조 호출이 창 상한(%d회/%.0f초)에 닿았다", _CALL_MAX_IN_WINDOW, _CALL_WINDOW_SEC
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
            headers={"Retry-After": str(max(1, math.ceil(remain)))},
        )
    return actor


router = APIRouter(
    prefix="/api/assistant",
    tags=["assistant"],
    dependencies=[Depends(_quota_gate)],
)


def _meta(
    result: ai_bridge.AssistantText,
    cfg: ai_bridge.AssistantConfig,
    *,
    budget_spent: bool = False,
) -> AssistantMeta:
    """응답에 실을 "어떻게 만든 문장인가".

    ⚠ 하루 예산 때문에 내린 판은 사유를 `daily_budget`으로 바꿔 적는다. 그대로 두면 키를
    지운 방식이라 `no_api_key`로 나가서, 로그를 보는 사람이 "키가 빠졌다"로 잘못 읽는다.
    """
    reason = result.fallback_reason
    if budget_spent and result.generated_by == "fallback":
        reason = "daily_budget"
    return AssistantMeta(
        generated_by=result.generated_by,
        fallback_reason=reason,
        model=cfg.model if result.generated_by == "llm" else None,
    )


# ── 캐시한 문장과 지금 숫자가 어긋나는 것 막기 (2026-08-09 백지검토 P1) ──────
#
# ⛔ **문장은 캐시에서, 숫자는 새로 뽑아 같이 나갔다.** `daily_summary`는 facts를 요청마다
# 새로 모으는데 summary는 수명(`assistant_brief_ttl_sec`, 기본 30분) 안이면 만들어 둔 것을
# 그대로 준다. 그래서 시연 중 이벤트가 쌓이면 "무단 통과 3건"이라 적힌 문단과 화면 집계가
# 서로 다른 수를 말한다. 만들 때 도는 검사(ai/control_assistant/guards.py "답의 숫자가
# 사실에 있나")는 **생성 시점에 한 번**이라 캐시 재사용은 그 검사를 아예 안 지나간다.
#
# ⭐ 그래서 **만들 때 본 사실**을 들고 있다가, 지금 사실과 다르면 캐시를 버리고 새로 만든다.
#
# ⚠ **DB 열이 아니라 메모리다**(위 연타 제한·하루 예산과 같은 자리, 워커 1개 전제). 열을
#   더하면 마이그레이션이 붙는데 마감이 하루 반이고 실서버가 살아 있다 — 스키마를 건드릴
#   자리가 아니다.
# ⚠ **들고 있는 지문이 없으면(재기동 직후) 캐시를 그대로 쓴다.** 모르는 것을 "달라졌다"로
#   보면 모델 서버가 느린 판에서 요청마다 20초를 물고, 멀쩡히 저장해 둔 문장까지 못 쓴다.
#   그 판은 수명이 알아서 씻는다.
# ⚠ 지난 날짜는 애초에 안 본다 — 그날 자료가 더 안 바뀌기 때문이다(`_cached_brief` 머리).
# ⚠ `robots`는 견주는 값에서 뺀다 — 그날 집계가 아니라 **지금 붙어 있는 대수**라
#   `robot_stale_sec`(25초)마다 뒤집힌다. 넣으면 아무 일도 안 일어난 날에도 자꾸 새로 만든다.
# ⭐ 새로 만드는 값이 크지 않다 — 화면은 이 창구를 **탭을 열 때 한 번** 부르고 성공하면
#   잠근다(frontend/index.html `loadAiSummary` `aiCardTried`). 주기 조회가 아니다.
_BRIEF_FACT_KEYS = ("date", "events", "verdicts", "alerts", "gates", "peak_hour")
_brief_facts_day = ""
_brief_facts_seen = ""


def _brief_signature(facts: dict) -> str:
    """요약 문장이 인용하는 값만 골라 만든 지문. 같은 값이면 같은 글자다(`sort_keys`)."""
    return json.dumps(
        {k: facts.get(k) for k in _BRIEF_FACT_KEYS},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


async def _cached_brief(
    session: AsyncSession, day: dt.date, facts: dict
) -> AssistantDailyBrief | None:
    """만들어 둔 일일 요약. 없거나 낡았으면 `None`이다.

    ⚠ **지난 날짜는 영구히 재사용한다** — 그날 자료가 더 안 바뀌기 때문이다. 오늘치만
    수명을 본다.

    ⚠ 수명이 0 이하면 캐시를 통째로 끈다. 매번 새로 만들던 예전 거동으로 돌아가는 스위치다.

    ⚠ 오늘치는 **수명 안이어도 사실이 바뀌었으면 버린다**(위 지문 절). 그래서 `facts`를
    받는다 — 부르는 쪽이 이미 뽑아 둔 값이라 질의가 늘지 않는다.
    """
    ttl = get_settings().assistant_brief_ttl_sec
    if ttl <= 0:
        return None
    row = (
        await session.execute(
            select(AssistantDailyBrief).where(AssistantDailyBrief.service_date == day)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    if day < assistant_data.today_kst():
        return row
    age = (dt.datetime.now(dt.timezone.utc) - row.updated_at).total_seconds()
    if age >= ttl:
        return None
    # 지문을 들고 있는 날만 견준다. 다르면 문장이 낡은 것이라 새로 만든다.
    if _brief_facts_day == day.isoformat() and _brief_facts_seen != _brief_signature(facts):
        logger.info("일일 요약 캐시를 버렸다 — 그 뒤로 사실이 바뀌었다 day=%s", day)
        return None
    return row


async def _store_brief(
    session: AsyncSession, day: dt.date, summary: str, model: str | None, signature: str
) -> None:
    """만든 요약을 남긴다. **실패해도 답은 이미 나간다** — 캐시는 곁이다.

    ⛔ **부르는 쪽에서 LLM 문장만 넘겨야 한다.** 폴백을 앉히면 그 문장이 수명 동안 계속
    나가서, 그 사이 LLM 이 살아나도 요원은 규칙 문장을 본다.

    ⚠ `signature`는 이 문장을 쓸 때 본 사실의 지문이다(위 지문 절). **저장이 실제로 끝난
    뒤에만** 남긴다 — 실패한 판에 남기면 낡은 행을 "최신"으로 보고 계속 내보낸다.
    """
    global _brief_facts_day, _brief_facts_seen
    try:
        row = (
            await session.execute(
                select(AssistantDailyBrief).where(
                    AssistantDailyBrief.service_date == day
                )
            )
        ).scalar_one_or_none()
        if row is None:
            session.add(
                AssistantDailyBrief(service_date=day, summary=summary, model=model)
            )
        else:
            row.summary = summary
            row.model = model
        await session.commit()
        _brief_facts_day = day.isoformat()
        _brief_facts_seen = signature
    except Exception:
        logger.warning("일일 요약을 저장 못 했다 day=%s", day, exc_info=True)
        await session.rollback()


@router.get("/health")
async def assistant_health(
    _actor: object | None = Depends(_gate),
) -> dict:
    """관제 보조 모델이 어디서 어떻게 도는가 (2026-08-07 · 관제 보조 갈래 14차).

    ⭐ **왜 있나** — 이 프로젝트 차별점이 **온프레미스**인데 화면 어디에도 안 보였다.
    요원도 심사위원도 답만 보지 그 문장이 사내 GPU 에서 나왔는지 외부 API 에서 왔는지
    모른다. 이 창구가 그 사실을 드러내는 자리다.

    ⚠ **요원 등급이면 볼 수 있다.** 개인 정보가 한 글자도 안 실리고, 역터널 주소는 관제
    보조 갈래가 가려서 준다("우리 GPU 서버 (사내망)" 꼴).

    ⚠ **응답 모양을 스키마로 안 묶는다.** 진단용이라 칸이 늘어날 자리이고, 여기서 계약을
    좁히면 관제 보조 쪽이 값을 하나 더할 때마다 이 파일을 같이 고쳐야 한다. 화면이 모르는
    칸은 그냥 안 그리면 된다.

    ⚠ 모델 서버에 상태를 한 번 묻는데 그 한 번도 상한이 걸려 있다 — 안 닿아도 예외를
    안 던지고 `reachable: false` 로 돌아온다.
    """
    return await ai_bridge.health_snapshot(ai_bridge.build_config())


@router.get("/daily-summary", response_model=DailySummaryOut)
async def daily_summary(
    date: dt.date | None = Query(
        default=None,
        description="KST 기준 날짜(YYYY-MM-DD). 생략하면 오늘.",
        # ⚠ 상한이 없으면 `9999-12-31`이 검증을 그냥 지나 **500**이 된다. 집계 창을 만드는
        # `assistant_data._kst_window`가 그 날에 하루를 더하는데, 파이썬 `date`의 최댓값이
        # 바로 그 날이라 `OverflowError: date value out of range`다(2026-08-04 축④ ④).
        # 그래서 "하루를 더할 수 있는 마지막 날"이 상한이다 — 422로 돌려주는 자리다.
        le=dt.date(9999, 12, 30),
    ),
    session: AsyncSession = Depends(get_db),
) -> DailySummaryOut:
    """하루치 관제 브리핑. 이벤트가 하나도 없는 날도 200에 '없었습니다' 문장이 나간다.

    ⭐ **만들어 둔 것이 있으면 그것을 준다**(0020). LLM 한 번이 7초대라 화면을 열 때마다
    부르면 그만큼 개요 탭이 비어 있었다. 같은 날 같은 자료면 답도 같다.
    """
    day = date or assistant_data.today_kst()
    facts = await assistant_data.collect_daily_facts(session, day)

    cached = await _cached_brief(session, day, facts)
    if cached is not None:
        return DailySummaryOut(
            date=day,
            summary=cached.summary,
            # ⚠ 캐시에서 꺼낸 것도 `generated_by=llm`이다 — 만들 때 LLM 이 낸 문장만
            #   저장하기 때문이다(폴백은 안 앉힌다).
            meta=AssistantMeta(
                generated_by="llm", model=cached.model, fallback_reason=None
            ),
            facts=facts,
        )

    # ⛔ **모델을 부르기 전에 트랜잭션을 닫는다**(2026-08-09 백지검토 P2 — 이 파일 세 창구가
    #    같이 걸렸다). AsyncSession은 첫 조회에서 asyncpg 커넥션을 물고 commit·rollback
    #    전까지 안 놓는다. 그래서 위 집계 SELECT가 연 트랜잭션이 모델이 답할 때까지 열린
    #    채였고, 읽기 상한이 20초(`gms_timeout_sec`, app/config.py)라 요청 하나가 풀 슬롯을
    #    20초씩 물었다. 풀은 10+10에 대기 5초뿐이라(app/db.py) 몇 건만 겹쳐도 인입·대시보드
    #    요청이 pool timeout으로 500을 맞는다.
    #
    # ⚠ **읽기만 했는데 rollback이 아니라 commit인 까닭** — `SessionMaker`가
    #    `expire_on_commit=False`라(app/db.py) commit은 들고 있는 ORM 행을 안 비운다.
    #    rollback은 그 설정과 무관하게 세션 안 행을 전부 비워서, 뒤에서 그 행의 칸을 읽으면
    #    비동기 세션이 지연 적재를 못 해 터진다(MissingGreenlet). 읽기 트랜잭션에 COMMIT은
    #    DB 쪽에서 아무것도 안 바꾼다.
    await session.commit()
    cfg, budget_spent = _llm_config()
    result = await ai_bridge.daily_summary(cfg, facts)
    meta = _meta(result, cfg, budget_spent=budget_spent)
    if meta.generated_by == "llm":
        await _store_brief(session, day, result.text, meta.model, _brief_signature(facts))
    return DailySummaryOut(
        date=day,
        summary=result.text,
        meta=meta,
        facts=facts,
    )


@router.get("/alert-brief/{alert_id}", response_model=AlertBriefOut)
async def alert_brief(
    # ⚠ 상한이 없으면 **404여야 할 자리가 500**이다. `alert.id`가 `BigInteger`라 int64를
    # 넘는 번호가 검증을 그냥 지나 asyncpg `DataError: value out of range`로 터진다. 이
    # 저장소가 `/api/eta`에서 이미 실측해 둔 계열이다(`tests/test_eta.py` — "SELECT라
    # 안전하다"가 여기서 반증됐다). 2026-08-04 축④ ① 중 이 파일 몫.
    alert_id: int = Path(ge=1, le=9223372036854775807),
    session: AsyncSession = Depends(get_db),
) -> AlertBriefOut:
    """경고 한 건 브리핑. 없는 경고는 404다 — 없는 상황을 지어낸 문장이 나가면 안 된다."""
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다."
        )
    facts = await assistant_data.collect_alert_facts(session, alert)
    # ⛔ 모델을 부르기 전에 트랜잭션을 닫는다 — 까닭은 `daily_summary` 같은 자리 주석에 있다.
    # ⚠ 이 창구가 특히 아픈 자리다. 화면이 경고 카드마다 이 브리핑을 붙여서 여러 건이 한꺼번에
    #    뜨면, 그만큼의 커넥션이 20초씩 풀 밖에 나가 있었다.
    # ⚠ commit 뒤에도 아래 `alert.id`를 읽을 수 있는 것은 `expire_on_commit=False`라서다
    #    (app/db.py). rollback으로 바꾸면 그 줄이 지연 적재로 터진다.
    await session.commit()
    cfg, budget_spent = _llm_config()
    result = await ai_bridge.alert_brief(cfg, facts)
    return AlertBriefOut(
        alert_id=alert.id,
        brief=result.text,
        meta=_meta(result, cfg, budget_spent=budget_spent),
        facts=facts,
    )


# ── 대화 저장 (2026-08-07 사용자 확정 · 마이그레이션 0019) ──────────────────
#
# ⛔ **이 절의 유일한 보안 계약** — 대화는 **`user_id == actor.id` 로만** 연다. 계정마다
#    갈리는 것이 요구사항의 본체라 그 조건이 빠지면 기능이 아니라 사고다.
#
# ⚠ **없는 대화와 남의 대화를 같은 404 로 답한다.** 403 으로 가르면 번호를 훑어 "그 번호에
#    대화가 있다"를 알아낼 수 있다(존재 누출).
#
# ⚠ **저장이 실패해도 답변은 나간다.** 챗봇의 본체는 답이고 기록은 곁이다. 저장 자리에서
#    터져 500 이 나가면 "물었는데 아무 말도 안 한다"가 되는데 그게 더 나쁘다.

_CHAT_TITLE_MAX = 60
# 이어 묻기에 실어 보낼 지난 메시지 수. 두 턴(질문·답변 두 쌍)이다.
_HISTORY_MESSAGES = 4


def _conversation_title(question: str) -> str:
    """첫 질문에서 목록에 보일 이름을 뽑는다. 화면이 나중에 고칠 수 있다."""
    one_line = " ".join(question.split())
    return one_line[:_CHAT_TITLE_MAX]


async def _open_conversation(
    session: AsyncSession, actor: object | None, body: ChatIn
) -> ChatConversation | None:
    """이어 쓸 대화를 찾거나 새로 연다. 로그인 정보가 없으면 저장을 안 한다.

    ⚠ `actor.id` 가 없는 경로(API 키로 여는 시연 도구 등)에서는 **저장을 건너뛴다.**
    주인 없는 대화를 만들면 목록에서 아무에게도 안 보이는 행이 쌓인다.
    """
    user_id = getattr(actor, "id", None)
    if user_id is None:
        return None

    if body.conversation_id is not None:
        row = await session.get(ChatConversation, body.conversation_id)
        # ⛔ 소유자 확인. 남의 것이면 없는 것과 같게 답한다(위 주석).
        if row is None or row.user_id != user_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation_not_found")
        return row

    row = ChatConversation(user_id=user_id, title=_conversation_title(body.question))
    session.add(row)
    await session.flush()
    return row


@router.get("/conversations", response_model=list[ConversationOut])
async def list_conversations(
    session: AsyncSession = Depends(get_db),
    actor: object | None = Depends(_gate),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[ConversationOut]:
    """내 대화 목록. 최근에 움직인 것부터다.

    ⛔ **남의 대화는 애초에 안 고른다.** `where` 에 `user_id` 가 걸려 있고, 그것이 이 창구의
    전부다.
    """
    user_id = getattr(actor, "id", None)
    if user_id is None:
        return []

    counts = (
        select(ChatMessage.conversation_id, func.count().label("n"))
        .group_by(ChatMessage.conversation_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(ChatConversation, func.coalesce(counts.c.n, 0))
            .outerjoin(counts, counts.c.conversation_id == ChatConversation.id)
            .where(ChatConversation.user_id == user_id)
            .order_by(ChatConversation.updated_at.desc(), ChatConversation.id.desc())
            .limit(limit)
        )
    ).all()
    return [
        ConversationOut(
            id=c.id,
            title=c.title,
            created_at=c.created_at,
            updated_at=c.updated_at,
            message_count=int(n),
        )
        for c, n in rows
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailOut)
async def get_conversation(
    conversation_id: int = Path(ge=1),
    session: AsyncSession = Depends(get_db),
    actor: object | None = Depends(_gate),
) -> ConversationDetailOut:
    """대화 하나의 메시지 전부. 시간 순이다."""
    user_id = getattr(actor, "id", None)
    row = await session.get(ChatConversation, conversation_id)
    # ⛔ 없는 것과 남의 것을 같은 404로 답한다(존재 누출 방지).
    if row is None or user_id is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation_not_found")

    msgs = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.id)
        )
    ).scalars().all()
    return ConversationDetailOut(
        id=row.id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        messages=[
            ChatMessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                generated_by=m.generated_by,
                fallback_reason=m.fallback_reason,
                created_at=m.created_at,
            )
            for m in msgs
        ],
    )


async def _recent_history(
    session: AsyncSession, conversation: ChatConversation | None
) -> list[dict]:
    """이어 묻기용 최근 대화. **시간 순**으로 준다(오래된 것이 앞).

    ⭐ **서버가 자른다.** 관제 보조 쪽도 상한을 두지만 여기서 먼저 자르는 편이 낫다 —
    프롬프트 앞부분이 길어지면 프리필이 늘어 지연이 그만큼 붙는다(관제 보조 갈래 실측:
    지연의 절반 넘게가 프리필이다).

    ⚠ **두 턴(메시지 넷)으로 시작한다.** 넉넉히 잡았다가 지연을 도로 까먹기보다, 짧게
    시작해서 재고 늘리는 편이 낫다는 판단이다.

    ⚠ `role`은 저장된 원본을 그대로 싣는다. 여기서 새로 지어내면 `user`·`assistant`가
    뒤집혀도 아무도 모른다.
    """
    if conversation is None or conversation.id is None:
        return []
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.id.desc())
            .limit(_HISTORY_MESSAGES)
        )
    ).scalars().all()
    return [{"role": m.role, "content": m.content} for m in reversed(rows)]


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    # ⚠ 204는 본문이 없어야 한다. 이 줄이 없으면 FastAPI가 JSON 응답 클래스를 붙이고
    # 기동할 때 `Status code 204 must not have a response body`로 죽는다.
    response_class=Response,
)
async def delete_conversation(
    conversation_id: int = Path(ge=1),
    session: AsyncSession = Depends(get_db),
    actor: object | None = Depends(_gate),
) -> Response:
    """대화 하나를 지운다(2026-08-07 프론트 51차 요청).

    ⛔ **화면에서만 감추면 목록을 다시 받는 순간 되살아난다.** 그래서 실제로 지운다.

    ⚠ 메시지는 외래키가 `ondelete="CASCADE"`라 같이 사라진다 — 여기서 따로 안 지운다.
    지우는 자리가 둘이 되면 한쪽만 고쳐질 때 고아 행이 남는다.

    ⚠ 없는 것과 남의 것을 **같은 404**로 답한다. 조회와 규칙이 같아야 화면이 한 갈래로
    다루고, "권한 없음"으로 갈라 주면 번호를 훑어 남의 대화 존재를 알아낼 수 있다.
    """
    user_id = getattr(actor, "id", None)
    row = await session.get(ChatConversation, conversation_id)
    if row is None or user_id is None or row.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "conversation_not_found")

    await session.delete(row)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/chat", response_model=ChatOut)
async def chat(
    body: ChatIn,
    session: AsyncSession = Depends(get_db),
    actor: object | None = Depends(_gate),
) -> ChatOut:
    """관제 챗봇. 서버가 조회한 데이터만 컨텍스트로 붙여 답한다.

    질문에 따라 조회 범위를 바꾸지 않는다 — 그러려면 LLM이 조회 함수를 고르는 툴콜링이
    필요한데, 읽기 전용 보조라는 자리에서 벗어난다.

    ⭐ **2026-08-07부터 문답을 계정별로 남긴다**(사용자 확정 · 0019). `conversation_id` 를
    비우면 새 대화를 열고, 주면 그 대화에 이어 붙인다.
    """
    conversation = await _open_conversation(session, actor, body)

    # ⭐ **묻는 사람을 같이 넘긴다**(2026-08-07). 자료를 등급으로 자르려던 것이 아니라,
    #    챗봇이 "총괄 관리자시니 개발자 화면까지 보실 수 있습니다"처럼 답하게 하려는 것이다.
    #    자르지 않는 근거는 `assistant_data._viewer` 머리에 적었다.
    facts = await assistant_data.collect_chat_facts(session, actor)
    cfg, budget_spent = _llm_config()
    # ⚠ **이력을 뽑는 자리가 저장보다 앞이라** 지금 질문은 안 섞인다(아래 `session.add_all`).
    #    순서를 뒤집으면 방금 한 질문이 이력으로도 한 번 더 들어간다.
    history = await _recent_history(session, conversation)
    # ⛔ 모델을 부르기 전에 트랜잭션을 닫는다 — 까닭은 `daily_summary` 같은 자리 주석에 있다.
    #
    # ⚠ **여기는 rollback이면 안 된다.** 위 `_open_conversation`이 새 대화를 `flush`까지만
    #    해 둔 판이라, 되돌리면 그 행이 사라지고 아래 저장이 없는 대화에 메시지를 붙인다.
    #    그래서 commit이고, 그 commit이 곧 "대화를 연다"는 뜻이다.
    # ⚠ 대신 모델 호출이 통째로 터지면 **메시지 없는 대화 한 줄이 목록에 남는다.** 예전에는
    #    요청이 끝날 때 같이 사라졌다. 커넥션을 20초 쥐는 쪽이 더 나쁘다고 봐서 이걸 택했고,
    #    빈 대화는 목록에서 지울 수 있다(DELETE /conversations/{id}).
    await session.commit()
    result = await ai_bridge.chat(cfg, body.question, facts, history=history)
    meta = _meta(result, cfg, budget_spent=budget_spent)

    conversation_id = conversation.id if conversation is not None else None
    if conversation is not None:
        # ⚠ 저장이 터져도 답은 나간다 — 챗봇의 본체는 답이고 기록은 곁이다.
        try:
            session.add_all(
                [
                    ChatMessage(
                        conversation_id=conversation.id, role="user", content=body.question
                    ),
                    ChatMessage(
                        conversation_id=conversation.id,
                        role="assistant",
                        content=result.text,
                        generated_by=meta.generated_by,
                        fallback_reason=meta.fallback_reason,
                    ),
                ]
            )
            conversation.updated_at = dt.datetime.now(dt.timezone.utc)
            await session.commit()
        except Exception:
            logger.warning("챗봇 대화를 못 남겼다 conversation_id=%s", conversation.id, exc_info=True)
            await session.rollback()
            conversation_id = None

    return ChatOut(
        question=body.question,
        answer=result.text,
        meta=meta,
        context=facts,
        conversation_id=conversation_id,
    )
