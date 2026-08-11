"""출동 창구 (정본: 셔틀출동_설계·셔틀출동_계약 2026-07-31 — 팀장 작업공간 보관, 리포에는 안 둔다).

관제 화면이 누르는 버튼이 여기로 들어온다. 여섯 갈래다.

1. 그날 기본 목적지 조회 — `GET /api/dispatch/default-destination`
2. 기본 목적지 수동 지정 — `POST /api/dispatch/default-destination`
3. 대기 창 선택(실내·실외·대기) — `POST /api/dispatch/pending/{arrival_id}/choose`
4. 즉시 이동(실내·실외) — `POST /api/dispatch/commands/move`
5. 복귀 — `POST /api/dispatch/commands/return`
6. 긴급정지 세우기·풀기 — `POST /api/dispatch/commands/emergency-stop`

거기에 화면 새로고침 복구용 `GET /api/dispatch/state` 하나를 더 뒀다. 대기 창은 서버
메모리에만 있어서, 창이 떠 있는 5초 사이에 화면을 새로 열면 WS 알림을 이미 놓친 뒤다 —
이 창구가 없으면 카운트다운을 못 되살린다.

## ⚠ 인증 — 기본은 키가 없고, 켜는 스위치가 하나 있다

`POST /api/shuttle-calls`와 같은 자리다. 화면이 X-API-Key를 들면 브라우저에 키가 그대로
노출되고, 그 키 하나로 태깅·통과 인입과 신원 입력까지 전부 열린다(routers/query.py 모듈
머리 "인증 경계"). 그래서 화면이 누르는 버튼은 기본값이 키 없음이다.

**여는 폭이 로봇 조작이라 셔틀 호출보다 넓다는 건 사실이다.** 공개 도메인에 그대로 두면
아무나 로봇을 보내거나 세울 수 있다. 그래서 두 겹을 뒀다(2026-07-31 검증 P2).

1. **`DISPATCH_REQUIRE_API_KEY`** — 켜면 이 라우터 전체가 X-API-Key를 요구한다. 기본은
   False다(켜는 순간 화면이 키를 실어야 붙으니 현장 배선이 같이 움직인다). 화면 경계를
   프록시 인증(auth_basic)이나 세션 쿠키로 옮기는 진짜 결정은 여전히 팀·사용자 자리다.
2. **연타 유량 제한** — 이동·복귀는 `DISPATCH_COMMAND_MIN_INTERVAL_SEC`(기본 1초) 안에
   다시 오면 429이고 `Retry-After`가 실린다. 셔틀 호출 쿨다운과 같은 결이다.
   ⚠ **긴급정지 창구는 제한을 안 받는다.** 세우는 것도 푸는 것도 막히면 안 되는 자리다.

`AUTH_REQUIRE_LOGIN`을 켜면 이 창구들이 세션 쿠키를 받는다. **딱 하나 예외가 복귀
(`POST /commands/return`)**다 — 라파이 게이트 단말이 세션을 못 쥔 채 그 창구를 직접
부르기 때문에, 켠 뒤에도 유효한 `X-API-Key`를 같이 받는다(2026-08-04 사용자 확정).
그 갈래만 `device_router`에 붙어 있고, 이유와 방어는 아래 라우터 절에 적었다.

## ⚠ 긴급정지 중에는 이동 창구가 409다

긴급정지 상태에서 이동·복귀·창 안 선택(실내·실외)을 부르면 409다. 세운 로봇을 다시
움직이는 갈래를 서버가 막는다(app/dispatch.py 모듈 머리 표). `HOLD` 선택과 긴급정지
해제는 그대로 받는다.

## 새 스키마를 왜 여기 두나

`app/schemas.py`는 이번 사이클에 여러 조가 같이 만지는 파일이라 요청·응답 모델을 이
파일 안에 뒀다. 낱말(Destination·CommandType)만 schemas.py 것을 그대로 쓴다 — 계약
정본은 `schemas/command.schema.json` 하나다.
"""
from __future__ import annotations

import logging
import math
from enum import Enum

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_agent
from app.config import get_settings
from app.db import get_db
from app.dispatch import (
    DispatchOutcome,
    EmergencyStopEngaged,
    WindowChoice,
    WindowNotOpen,
    check_command_throttle,
    choose_in_window,
    dispatch_emergency_stop,
    dispatch_immediate_move,
    dispatch_return_to_charge,
    emergency_stopped,
    ensure_today_default,
    open_windows,
    peek_today_default,
    rollback_command_throttle,
    set_manual_default,
)
from app.schemas import Destination
from app.security import key_gate_or, require_api_key

logger = logging.getLogger("c207.dispatch")


async def require_dispatch_key(x_api_key: str | None = Header(default=None)) -> None:
    """`DISPATCH_REQUIRE_API_KEY`가 켜져 있을 때만 X-API-Key를 요구한다.

    기본은 꺼짐이라 지금 거동이 한 글자도 안 바뀐다. 켜는 순간 관제 화면도 키를 실어야
    하니, 언제 켤지는 현장 배선과 같이 움직이는 팀 결정이다(모듈 머리 "인증").

    ⚠ 스위치가 켜졌을 때의 대조는 **`security.require_api_key`에 그대로 넘긴다.** 예전에는
    여기서 `x_api_key != settings.api_key` 한 줄로 따로 봤는데, 그 모습이 인입 창구에서
    이미 고친 결함 셋(`API_KEY=""` 배포에서 빈 헤더 통과 · 상수시간이 아닌 대조 · 헤더
    파서가 떼는 둘레 공백 때문에 창구마다 갈리는 판정)을 이 라우터에만 되살려 뒀다.
    같은 키(`API_KEY`)를 보는 게이트가 둘로 갈려 있으면 한쪽만 고쳐지므로, 판정은 한
    함수에만 둔다. 401 문장도 그 함수 것과 글자까지 같다.
    """
    if not get_settings().dispatch_require_api_key:
        return
    await require_api_key(x_api_key)


def _throttle(kind: str) -> None:
    """즉시 명령 연타를 429로 삼킨다. 남은 초는 Retry-After에 싣는다.

    ⚠ 눈금이 **명령 종류당 하나**다(`app/dispatch._last_command_at`). 요원 A가 이동을 누른
    직후 요원 B가 누르면 B도 막히고, 라파이 게이트 단말이 부른 복귀와 화면 복귀 버튼도 같은
    통을 쓴다. 그래서 문구를 "당신이 자주 보냈다"가 아니라 사실대로 적는다 — 처음 누른
    사람한테 "너무 자주 보냈습니다"가 뜨면 화면이 거짓말을 한다(2026-08-04 D2).
    사람 단위로 가를지는 팀 결정 자리다(로봇 하나를 여럿이 조종하는 판이라 지금은 전역이다).
    """
    remain = check_command_throttle(kind)
    if remain <= 0:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"방금 같은 명령이 나갔습니다. {remain:.1f}초 뒤에 다시 시도해 주세요.",
        headers={"Retry-After": str(max(1, math.ceil(remain)))},
    )


def _stopped_conflict(exc: EmergencyStopEngaged, kind: str | None = None) -> HTTPException:
    """긴급정지 중 이동 요청 → 409. 화면은 해제 버튼을 강조한다.

    명령이 서버 밖으로 안 나갔으니 유량 제한 눈금도 돌려준다 — 안 그러면 긴급정지를 풀고
    바로 누른 이동이 429로 한 번 더 막힌다.
    """
    if kind is not None:
        rollback_command_throttle(kind)
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


# 이 라우터에 붙은 창구 여섯은 켠 뒤 **요원 세션만** 받는다(로그인 설계 확정본 §3).
# 일곱 번째 창구인 복귀는 아래 `device_router`로 갈라져서 키도 같이 받는다 — 그 예외의
# 이유와 방어는 그쪽 절에 있다. `DISPATCH_REQUIRE_API_KEY`는 세션이 켜지면 뜻이 없어져서
# 켜지 않는다 — 그래도 스위치를 안 지우는 이유는 플래그를 되돌리는 롤백 구간(§6.2 5단계
# 역행)에서 그게 유일한 자물쇠라서다(두 라우터가 같은 `require_dispatch_key`를 쓴다).
#
# ⭐ 제1 불변 — `AUTH_REQUIRE_LOGIN=false`면 옛 `require_dispatch_key`가 그대로 돈다
# (그 스위치도 기본 꺼짐이라 지금 거동이 한 글자도 안 바뀐다).
router = APIRouter(
    prefix="/api/dispatch",
    tags=["dispatch"],
    dependencies=[Depends(key_gate_or(require_agent, require_dispatch_key))],
)

# ── 기기가 부르는 창구 (2026-08-04 사용자 확정 — "라파이는 키를 계속 받게 열어둔다") ──
#
# ⭐ **왜 라우터를 갈랐나.** 라파이 게이트 단말이 복귀를 직접 부른다
# (`hardware/rpi/gate_server.py call_return_service` — 두 번째 3초 터치). 그 기기는 세션을
# 못 쥐어서, `AUTH_REQUIRE_LOGIN`을 켜는 순간 그 버튼이 401로 죽는다. 그래서 **그 한 창구만**
# 켠 뒤에도 유효한 `X-API-Key`를 받는다(`device_key_fallback=True`). 나머지 여섯은 위
# `router`에 그대로 남아 켜지면 세션만 받는다.
#
# ⚠ 여기서 "유효한"은 **자물쇠 구실을 하는 키**다. 빈 값과 저장소 기본키(`dev-local-key`)는
# 거절이라, `API_KEY`를 안 덮은 배포에서는 이 갈래로 아무도 못 지난다(판정과 근거는
# `security.device_key_ok`의 `require_usable_secret`). 인입 게이트는 반대로 기본키를 받는다.
#
# ⭐ **창구마다 손으로 다는 꼴로 안 바꿨다.** 지금 구조의 값은 "새 창구를 추가해도 게이트가
# 자동으로 걸린다"는 것이라, 라우트 데코레이터마다 `dependencies=[...]`를 적기 시작하면
# 다음 사람이 한 줄을 빠뜨린 창구를 만든다. 그래서 라우터 레벨 의존성을 그대로 두고
# **라우터를 하나 더 둔다** — 어느 쪽에 붙여도 게이트는 자동으로 걸리고, 갈리는 건 어느
# 게이트냐뿐이다. 기본값(`@router`)이 좁은 쪽이라 실수는 "더 잠기는" 방향으로 난다.
#
# ⚠ **여기에 창구를 더 붙이지 마라.** 붙이는 순간 그 창구가 공용 기기 키로 열린다. 그게
# 조용히 일어나지 않게 `tests/test_dispatch_key_gate.py`가 이 라우터의 창구 목록을 닫힌
# 집합으로 못박는다 — 하나를 더 붙이면 그 시험이 빨개진다.
device_router = APIRouter(
    prefix="/api/dispatch",
    tags=["dispatch"],
    dependencies=[
        Depends(key_gate_or(require_agent, require_dispatch_key, device_key_fallback=True))
    ],
)


# ── 요청·응답 모델 ─────────────────────────────────────────────────────────

class MoveDestination(str, Enum):
    """즉시 이동·기본 목적지로 고를 수 있는 값. 충전소는 복귀 창구가 따로 있어 뺐다."""

    INDOOR_TAGGING = "INDOOR_TAGGING"
    OUTDOOR_TAGGING = "OUTDOOR_TAGGING"


class DefaultDestinationIn(BaseModel):
    """요원이 손으로 정하는 그날 기본 목적지."""

    destination: MoveDestination


class WeatherOut(BaseModel):
    status: str | None = None
    description: str | None = None
    # False면 조회에 실패해 마지막 성공값이나 UNKNOWN으로 정했다는 뜻이다. 화면은 이때
    # "날씨를 받지 못했습니다"를 띄운다.
    ok: bool | None = None


class DefaultDestinationOut(BaseModel):
    service_date: str
    destination: str
    source: str            # auto / manual
    weather: WeatherOut
    decided_at: str | None = None
    updated_at: str | None = None


class ChoiceIn(BaseModel):
    """대기 창 버튼 셋. HOLD면 명령을 아예 안 보낸다."""

    choice: WindowChoice


class MoveIn(BaseModel):
    destination: MoveDestination


class EmergencyStopIn(BaseModel):
    """세울까 풀까. 계약에 해제 명령이 따로 없어서 같은 명령에 false를 싣는다."""

    engage: bool = Field(default=True)


class DispatchResultOut(BaseModel):
    """명령 한 번의 결과. WS `shuttle_dispatch_result` payload와 칸이 같다.

    같은 사실을 두 통로가 다른 모양으로 주면 화면이 두 그림을 그린다.
    """

    arrival_id: int | None = None
    event_id: str | None = None
    gate_no: int | None = None
    # credit_scope_room 플래그를 켰을 때만 실린다(app/dispatch.with_room).
    #
    # ⚠ 꺼져 있으면 **칸 자체가 빠진다.** WS `shuttle_dispatch_result`는 `with_room`이
    # 페이로드에 키를 아예 안 넣는 방식이라, REST만 `"room_id": null`로 내보내면 계약 1.9의
    # "두 통로가 같은 규칙"이 갈라진다. 그래서 이 창구들은 `response_model_exclude_unset`으로
    # 나가고, `_result`가 페이로드에 있는 칸만 채운다.
    room_id: str | None = None
    source: str
    command: str | None = None
    destination: str | None = None
    emergency_stop: bool | None = None
    command_id: str | None = None
    notified_robot_count: int
    decided_at: str


class PendingWindowOut(BaseModel):
    arrival_id: int
    event_id: str | None = None
    gate_no: int | None = None
    shuttle_no: str | None = None
    command_id: str
    hold_sec: float
    opened_at: str
    deadline: str
    remain_sec: float
    choices: list[str]
    default_destination: str | None = None


class DispatchStateOut(BaseModel):
    """화면이 새로고침 뒤 되살릴 상태 전부."""

    pending: list[PendingWindowOut]
    emergency_stop: bool
    default_destination: DefaultDestinationOut | None = None


def _result(outcome: DispatchOutcome) -> DispatchResultOut:
    return DispatchResultOut(**outcome.as_payload())


# ── 그날 기본 목적지 ───────────────────────────────────────────────────────

@router.get("/default-destination", response_model=DefaultDestinationOut)
async def get_default_destination(
    session: AsyncSession = Depends(get_db),
) -> DefaultDestinationOut:
    """그날 기본 목적지. **없으면 이때 날씨를 조회해 만든다**(lazy 갱신).

    스케줄러를 새로 안 만든 게 이 설계다(설계 §6.1의 "날씨 조회 시각"이 미정이었다).
    아침에 관제 화면이 켜지면서 이 창구를 부르는 순간이 곧 그날 첫 조회다.

    ⚠ 그날 첫 호출은 바깥 날씨 서버를 탄다. 실패해도 200이고 `weather.ok=false`로 그 사실이
    드러난다.

    ⚠ **`weather_timeout_sec`(기본 4초)는 요청당 총예산이 아니라 고리 하나 몫이다**
    (2026-08-04 W6 — 예전 문구가 "한 번 탄다(기본 4초)"라 총예산으로 읽혔다). 사슬은
    기상청 실황(최대 두 슬롯) → 기상청 예보 → wttr.in → 대체 도메인 넷이라, 상류가 전부
    느리면 이 요청 하나가 **십수 초** 매달린다. 아침 첫 대시보드 로드가 그 자리다.
    """
    return DefaultDestinationOut(**(await ensure_today_default(session)).as_payload())


@router.post("/default-destination", response_model=DefaultDestinationOut)
async def put_default_destination(
    body: DefaultDestinationIn, session: AsyncSession = Depends(get_db)
) -> DefaultDestinationOut:
    """요원이 그날 기본 목적지를 직접 고른다. **그날 안에는 날씨 조회가 이 값을 못 덮는다.**"""
    stored = await set_manual_default(session, Destination(body.destination.value))
    return DefaultDestinationOut(**stored.as_payload())


# ── 상태 조회 ──────────────────────────────────────────────────────────────

@router.get("/state", response_model=DispatchStateOut)
async def get_dispatch_state(
    session: AsyncSession = Depends(get_db),
) -> DispatchStateOut:
    """열린 대기 창·긴급정지 상태·그날 기본값.

    ⚠ 기본 목적지는 여기서 **조회를 안 한다**(peek). 화면이 새로고침할 때마다 날씨 서버를
    타면 안 되고, 이 창구의 목적은 "지금 서버가 아는 상태"를 그대로 보여주는 것이다.
    아직 그날 첫 조회 전이면 null이다.
    """
    known = await peek_today_default(session)
    return DispatchStateOut(
        pending=[PendingWindowOut(**w) for w in open_windows()],
        emergency_stop=emergency_stopped(),
        default_destination=(
            DefaultDestinationOut(**known.as_payload()) if known else None
        ),
    )


# ── 대기 창 선택 ───────────────────────────────────────────────────────────

@router.post("/pending/{arrival_id}/choose", response_model=DispatchResultOut, response_model_exclude_unset=True)
async def choose_pending(
    arrival_id: int, body: ChoiceIn, session: AsyncSession = Depends(get_db)
) -> DispatchResultOut:
    """5초 창 안에서 실내·실외·대기를 고른다.

    고른 순간 바로 나간다(남은 초를 안 기다린다). 이미 닫힌 창이면 404다 — 5초가 지나
    자동 발사됐거나 딴 사람이 먼저 골랐다는 뜻이라, 화면은 "이미 결정됐습니다"로 그린다.

    긴급정지 중에 실내·실외를 고르면 409다. `HOLD`는 명령을 안 내니 그대로 받는다.
    """
    try:
        outcome = await choose_in_window(
            session, arrival_id=arrival_id, choice=body.choice
        )
    except EmergencyStopEngaged as exc:
        raise _stopped_conflict(exc)
    except WindowNotOpen:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="이미 결정된 셔틀입니다. 대기 창이 닫혔습니다.",
        )
    return _result(outcome)


# ── 즉시 이동 넷 ───────────────────────────────────────────────────────────

@router.post("/commands/move", response_model=DispatchResultOut, response_model_exclude_unset=True)
async def move_now(
    body: MoveIn, session: AsyncSession = Depends(get_db)
) -> DispatchResultOut:
    """즉시 이동(실내·실외). 대기 창이 떠 있으면 **전부** 취소하고 이긴다(사용자 확정).

    창이 여럿이면 명령은 가장 먼저 열린 창에 매이고 나머지 신호는 버린다.
    긴급정지 중이면 409, 너무 자주 부르면 429다.
    """
    _throttle("move")
    try:
        outcome = await dispatch_immediate_move(
            session, Destination(body.destination.value)
        )
    except EmergencyStopEngaged as exc:
        raise _stopped_conflict(exc, "move")
    return _result(outcome)


@device_router.post("/commands/return", response_model=DispatchResultOut, response_model_exclude_unset=True)
async def return_now(session: AsyncSession = Depends(get_db)) -> DispatchResultOut:
    """복귀. 충전 위치로 보낸다. 대기 창이 떠 있으면 전부 취소하고 그 셔틀 신호는 버린다.

    긴급정지 중이면 409, 너무 자주 부르면 429다.

    ⚠ **이 창구만 `device_router`에 붙어 있다**(위 라우터 절 참고). 관제 화면 버튼과 라파이
    게이트 단말이 같은 자리를 부르는 유일한 창구라, 로그인을 켠 뒤에도 기기 키 갈래가 살아
    있어야 현장 배선이 안 끊긴다.
    """
    _throttle("return")
    try:
        return _result(await dispatch_return_to_charge(session))
    except EmergencyStopEngaged as exc:
        raise _stopped_conflict(exc, "return")


@router.post("/commands/emergency-stop", response_model=DispatchResultOut, response_model_exclude_unset=True)
async def emergency_stop(
    body: EmergencyStopIn | None = None, session: AsyncSession = Depends(get_db)
) -> DispatchResultOut:
    """긴급정지 세우기(`engage=true`)와 풀기(`engage=false`).

    푸는 자리가 없으면 시연 중 한 번 누르고 끝난다(설계 §2.4). 세울 때는 열려 있는 대기
    창을 **전부** 취소한다 — 세워 놓고 몇 초 뒤에 출동 명령이 나가면 안 된다.

    ⚠ 이 창구만 유량 제한도 긴급정지 검사도 안 받는다. 안전 정지와 그 해제가 막히면
    시연이 그 자리에서 끝난다.

    ⚠ **본문 없이 불러도 세운다.** 칸이 `engage` 하나뿐이고 기본값이 True라 화면이 본문을
    안 실을 이유가 충분한데, 파라미터에 기본값이 없으면 FastAPI가 본문을 필수로 봐서
    `fetch(url, {method:'POST'})` 한 줄이 **422**였다(2026-08-04 축④ ⑨). 막히면 안 되는
    창구라고 적어 놓고 정작 부르는 가장 흔한 모양이 막히던 자리다.
    """
    engage = True if body is None else body.engage
    return _result(await dispatch_emergency_stop(session, engage=engage))
