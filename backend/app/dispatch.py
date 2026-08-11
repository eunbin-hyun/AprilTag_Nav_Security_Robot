"""셔틀 도착 → 로봇 출동 (정본: docs/셔틀출동_설계_2026-07-31.md §2.1·§2.2·§2.4).

## 무엇이 달라졌나

예전에는 셔틀 신호가 들어오면 그 자리에서 목적지 명령을 쐈다. 이제는 **5초를 붙든다.**
그 사이 관제 화면이 실내·실외·대기 중 하나를 고르면 그대로 나가고, 아무도 안 고르면
그날 기본 목적지로 자동 발사한다. "대기"를 고르면 명령을 아예 안 보낸다.

완전 자동도 수동도 아닌 중간을 만드는 자리다(설계 §9).

## 이 모듈이 들고 있는 것 셋

1. **그날 기본 목적지** — 날씨로 정하고 DB(`daily_default_destination`)에 남긴다. 요원이
   손으로 고른 값은 그날 안에는 자동 조회가 못 덮는다. 날씨 조회는 `app/weather.py`에
   있고 여기서는 "비·눈이면 실내"만 판단한다.
2. **대기 창** — 신호마다 asyncio 태스크 하나. 인메모리라 서버가 다시 뜨면 사라진다
   (설계에 명시된 유실 허용 자리 — 아래 "재기동" 절).
3. **긴급정지 상태** — 세웠나 풀었나. 화면이 버튼 모양을 정하는 데 쓴다. 역시 인메모리다.

## 신호 하나가 어떻게 닫히나 — 여섯 중 하나다

| 출처 | 언제 | 명령 |
| --- | --- | --- |
| `auto` | 5초가 그냥 지남 | 그날 기본 목적지 |
| `manual` | 요원이 창 안에서 실내·실외를 고름 | 고른 목적지 |
| `immediate` | 즉시 이동 버튼(실내·실외·복귀·긴급정지) | 그 버튼의 명령 |
| `hold_cancelled` | 요원이 "대기"를 고름 | **안 나간다** |
| `emergency_blocked` | 긴급정지가 걸린 상태에서 셔틀이 옴 | **안 나간다**(창도 안 연다) |
| `hold_disabled` | `shuttle_dispatch_hold_sec <= 0`이라 창이 꺼져 있음 | 받은 자리에서 바로. 기본값 5.0이라 라이브에는 안 나온다 |

⚠ 아래 둘은 **창을 안 여는** 갈래다. 그래도 확정 결과 한 장은 똑같이 나간다 — 계약 §0이
"어느 갈래로 닫히든 결과 한 장"이라, 예전에 `hold_disabled`만 그걸 안 지켰다. 방송 순서는
`DispatchStart.outcome` docstring 참고(arrival이 먼저, 결과가 뒤).
`resend`(통지 유실 재전송 복구, `app/shuttle_call.py`)는 이 표 밖이다 — 창과 무관한 갈래다.

## ⚠ 긴급정지가 걸려 있으면 이동 명령이 한 줄도 안 나간다 (2026-07-31 검증 P1)

예전에는 긴급정지 상태를 `GET /api/dispatch/state` 응답에서만 읽었다. 그래서 세워 놓은
뒤에 들어온 셔틀이 대기 창을 열고 5초 뒤에 그대로 목적지 명령을 쐈다 — 세운 로봇이 다시
움직이는 갈래다. 지금은 **명령을 내는 자리 전부**가 `_require_not_stopped()`를 먼저 지난다.

| 자리 | 긴급정지 중이면 |
| --- | --- |
| 셔틀 도착(`begin_dispatch`) | 창을 안 연다. 신호를 버리고 `emergency_blocked`를 알린다 |
| 자동 발사(타이머) | 발사 직전에 다시 본다. 걸려 있으면 버린다 |
| 창 안 선택(실내·실외) | 409. `HOLD`는 명령을 안 내니 그대로 받는다 |
| 즉시 이동·복귀 | 409 |
| 재전송 복구 | 안 보낸다(기기 재시도가 세운 로봇을 깨우면 안 된다) |
| 긴급정지 풀기 | 늘 받는다. 이게 막히면 시연이 끝난다 |

## ⚠ 창이 여럿이면 전부 닫는다 (2026-07-31 검증 P1)

게이트가 다른 셔틀이 5초 안에 연달아 오면 창이 둘 이상 열린다. 예전에는 즉시 이동·복귀·
긴급정지가 **창 하나만** 닫아서, 긴급정지를 눌러도 남은 창이 2초 뒤에 목적지 명령을 냈다.
지금은 즉시 명령이 열린 창을 **전부** 닫는다. 즉시 이동은 가장 먼저 열린 창(마감이 제일
가까운 창)에 명령을 매고 나머지 신호는 버린다 — 짝짓기가 한 신호에만 붙게 하려는 거다.

⚠ **"대기"로 닫힌 셔틀 신호는 버린다. 나중에 되살리지 않는다**(설계 §6.1의 미결 항목을
"버린다"로 확정). 그 신호에는 만료 알림(`shuttle_arrival_expired`)을 찍어 짝짓기 목록에서
빼낸다 — 안 그러면 나중에 온 로봇 도착이 버려진 신호에 붙어 게이트가 뒤바뀐다. 복귀·
긴급정지로 창이 닫힐 때도 같다(로봇이 게이트로 안 가니까).

## 재기동 — 열려 있던 창은 잃는다 (문서화된 허용)

대기 창은 인메모리 asyncio 태스크라 서버가 그 5초 안에 다시 뜨면 창이 통째로 사라진다.
그 신호는 명령이 안 나간 채(`notified_robot_id IS NULL`) DB에 남고, 같은 신호가 다시
들어오면 재전송 복구가 즉시 발사한다(`app/shuttle_call.py`). 창을 DB로 옮기면 재기동을
견디지만 5초짜리 상태를 위해 표 하나와 청소 잡이 늘어서, 시연 폭에는 과하다고 봤다.

## ⚠ 발사 경로는 바깥 HTTP를 안 탄다 (2026-07-31 검증 P2)

날씨 조회는 최대 4초(`weather_timeout_sec`)가 걸린다. 예전에는 자동 발사가 그 조회를
그대로 태워서, 그날 첫 셔틀이 화면 조회보다 먼저 오면 5초 창이 닫힌 뒤에 4초를 더
기다렸다(KST 자정을 넘겨 만료되는 창도 같은 갈래였다). 지금은 창을 열 때 그날 기본값이
없으면 **미리 조회를 백그라운드로 띄우고**(`_spawn_default_prefetch`, await 안 한다),
발사 자리는 `destination_without_fetch`로 아는 값만 본다. 그래서 발사 시각이 날씨 서버
응답 속도에 안 매달린다.

## 쿨다운과 헷갈리지 마라 (설계 §6.1)

`SHUTTLE_CALL_COOLDOWN_SEC`(5.0)과 `shuttle_dispatch_hold_sec`(5.0)이 값이 같다. 하는
일은 정반대다 — 쿨다운은 **입구에서 두 번째 호출을 429로 삼키고**, 대기 창은 **이미
받아들인 신호 하나를 붙든다.** 연타 시험에서 무엇이 막았는지 헷갈리지 않게 로그 머리를
갈라 뒀다: 쿨다운은 `[게이트 연타 차단]`, 대기 창은 `[출동 대기 창]`.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.device_sound import KIND_ROBOT_DEPARTURE, push_sound
from app.models import Alert, DailyDefaultDestination, ShuttleArrival
from app.command_log import CHANNEL_ROBOT, record_issued
from app.robot_channel import (
    ARRIVAL_EXPIRED_ALERT_TYPE,
    SHUTTLE_SOURCE_TYPE,
    clear_mission_presentation,
    notify_shuttle_arrival,
    register_ack_only_command,
)
from app.schemas import CommandOut, CommandType, Destination
from app.weather import (
    WeatherReading,
    WeatherStatus,
    fetch_current_weather,
    last_success as last_weather_success,
    unknown_reading,
)
from app.ws import make_envelope, manager, robot_manager

logger = logging.getLogger("c207.dispatch")

# KST. 오프셋을 더한 tz 변환으로만 날짜를 만든다 — ISO 문자열의 Z를 +09:00으로 바꿔치는
# 방식은 시각을 9시간 어긋나게 만든다(같은 순간을 다른 시각으로 읽는다).
KST = dt.timezone(dt.timedelta(hours=9))

# 대기 창 로그 머리. 쿨다운(`[게이트 연타 차단]`)과 갈라 읽는 표식이다.
HOLD_LOG_TAG = "[출동 대기 창]"

# 대시보드 WS 메시지 종류. 확정 결과 한 장이 여기로 나간다.
DISPATCH_RESULT_MESSAGE = "shuttle_dispatch_result"

# 날씨가 바뀌었을 때 대시보드로 나가는 메시지. 화면 상단 기상 칩이 이걸로 갱신된다.
# ⚠ 브라우저가 기상청을 직접 부르면 안 된다 — 인증키가 화면으로 내려가고 호출이 화면 수만큼
# 는다. 서버가 한 번 받아 이 한 장으로 뿌리는 게 계약이다.
WEATHER_MESSAGE = "weather_update"

# 아침에 기본 목적지를 정하는 시각(KST). 주기 조회가 이 시각을 지나야 그날 행을 만든다.
# 그 전에는 새벽 날씨로 하루를 정해 버리기 때문에 안 만든다 — 그 시간대에 셔틀이 오면
# 예전처럼 `ensure_today_default`의 lazy 갱신이 그 자리에서 정한다.
WEATHER_MORNING_HOUR = 7
WEATHER_MORNING_MINUTE = 0

# 날씨를 못 받았을 때의 기본 목적지. 시연 경로가 실외 하나뿐이라 그쪽이 안전하다(설계 §3).
DEFAULT_WHEN_UNKNOWN = Destination.OUTDOOR_TAGGING


class DispatchSource(str, Enum):
    """무엇이 이 명령을 냈나. 화면이 결과 문구를 고르는 근거다."""

    AUTO = "auto"
    MANUAL = "manual"
    IMMEDIATE = "immediate"
    HOLD_CANCELLED = "hold_cancelled"
    # 긴급정지가 걸린 채로 셔틀이 왔다. 명령을 안 내고 그 신호를 버렸다는 뜻이다.
    EMERGENCY_BLOCKED = "emergency_blocked"
    # 대기 창이 꺼져 있어(`shuttle_dispatch_hold_sec <= 0`) 신호를 받은 자리에서 바로 쐈다.
    # 창을 거친 `auto`·`manual`과 갈라 두는 이유는 화면이 "카운트다운이 있었나"를 이 값으로
    # 알기 때문이다. 기본값이 5.0이라 지금 라이브에서는 안 나오지만, 계약 §0의 "어느 갈래로
    # 닫히든 결과 한 장"을 이 갈래만 안 지키던 구멍을 메운다.
    HOLD_DISABLED = "hold_disabled"
    # 대기 창과 무관한 갈래 — 통지 유실 재전송 복구(app/shuttle_call.py).
    RESEND = "resend"
    # ⛔ 요원이 비상 정지를 눌렀다. 예전에는 이 갈래가 `IMMEDIATE`를 실어서, 화면이 그것을
    # 사람 말로 옮기며 **"즉시 이동으로 대신 보냈습니다"**라고 적었다(2026-08-06 프론트 27차).
    # 누른 적 없는 버튼을 눌렀다고 말하는 셈이었다.
    #
    # ⚠ `EMERGENCY_BLOCKED`를 재활용하지 않은 이유는 뜻이 다르기 때문이다 — 그쪽은 "셔틀이
    # 왔는데 정지 중이라 못 보냈다"이고 이쪽은 "요원이 정지를 눌렀다"다. 한 낱말에 두 사건을
    # 담으면 화면이 원인을 못 가른다.
    EMERGENCY_STOP = "emergency_stop"


class DefaultSource(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class WindowChoice(str, Enum):
    """대기 창 버튼 셋. 실내·실외는 목적지이고 HOLD는 "보내지 마"다.

    창에서 고를 수 있는 목적지는 실내·실외뿐이다 — 충전소는 창의 선택지가 아니라 복귀
    버튼이 따로 낸다. 이 enum이 그 집합의 정본이고 화면 `choices`도 여기서 나간다.
    """

    INDOOR_TAGGING = "INDOOR_TAGGING"
    OUTDOOR_TAGGING = "OUTDOOR_TAGGING"
    HOLD = "HOLD"


class EmergencyStopEngaged(Exception):
    """긴급정지가 걸려 있어 이동 명령을 못 낸다.

    라우터가 409로 옮긴다. 화면은 이걸 받으면 "긴급정지를 먼저 해제해 주세요"로 그린다.
    ⚠ 긴급정지 해제 창구는 이 예외를 절대 안 던진다 — 막히면 시연이 그 자리에서 끝난다.
    """


class WindowNotOpen(Exception):
    """그 셔틀 신호의 대기 창이 이미 닫혔거나 아예 없다.

    라우터가 404로 옮긴다. 5초가 지나 자동 발사됐거나 딴 사람이 먼저 골랐다는 뜻이라,
    화면은 이걸 받으면 "이미 결정됐습니다"로 그린다.
    """


# 서버 → 로봇 명령 모델은 `schemas.CommandOut` 하나다. 예전에는 이 모듈이 `DispatchCommand`를
# 따로 들고 있었다 — CommandOut이 `cmd_destination`까지만 실어서 복귀·긴급정지를 못 냈고,
# 여러 조가 같이 만지는 파일이라 칸을 거기 더하는 대신 여기 우회를 팠던 자리다. 지금은
# CommandOut이 여섯 칸을 다 들고 있어 우회가 필요 없다(정본 `schemas/command.schema.json`).


# ── 시각 도우미 ────────────────────────────────────────────────────────────

def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def kst_moment(now: dt.datetime | None = None) -> dt.datetime:
    """지금을 KST 시각으로.

    ⚠ 오프셋을 **더해서** 만든다(`astimezone(KST)`). ISO 문자열의 `Z`를 `+09:00`으로
    바꿔치는 방식은 같은 순간을 9시간 다른 시각으로 읽어서 자정 근처 날짜가 통째로
    어긋난다. naive 시각이 들어오면 UTC로 읽는다.
    """
    moment = now or _utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(KST)


def kst_today(now: dt.datetime | None = None) -> dt.date:
    """지금이 KST로 며칠인가."""
    return kst_moment(now).date()


# ── 그날 기본 목적지 (설계 §2.1) ───────────────────────────────────────────

@dataclass
class DefaultDestination:
    """그날 기본 목적지 한 줄. DB 행을 화면 계약 모양으로 옮긴 값이다."""

    service_date: dt.date
    destination: str
    source: str
    weather_status: str | None
    weather_desc: str | None
    weather_ok: bool | None
    decided_at: dt.datetime
    updated_at: dt.datetime | None

    @classmethod
    def of(cls, row: DailyDefaultDestination) -> "DefaultDestination":
        return cls(
            service_date=row.service_date,
            destination=row.destination,
            source=row.source,
            weather_status=row.weather_status,
            weather_desc=row.weather_desc,
            weather_ok=row.weather_ok,
            decided_at=row.decided_at,
            updated_at=row.updated_at,
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "service_date": self.service_date.isoformat(),
            "destination": self.destination,
            "source": self.source,
            "weather": {
                "status": self.weather_status,
                "description": self.weather_desc,
                # False면 조회에 실패해 마지막 성공값이나 UNKNOWN으로 정했다는 뜻이다.
                # 화면은 이때 "날씨를 받지 못했습니다"를 띄운다(설계 §5.2).
                "ok": self.weather_ok,
            },
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def weather_auto_default_on() -> bool:
    """날씨가 기본 목적지를 정하게 둘까. **시연 기간에는 꺼져 있다**(2026-08-03 사용자 확정).

    꺼도 날씨 조회·화면 갱신·예보 창구는 그대로 돈다 — 목적지만 안 건드린다.

    왜 껐나 — 실내 태깅 도착 태그가 시연 배치에 없다(ID1 충전소·ID2/ID4 코너·ID3 외부뿐).
    그 상태에서 비가 오면 서버는 실내로 넘기는데 젯슨은 그 명령을 외부 경로로 태운다.
    화면과 로봇이 어긋나고, 전환이 한 방향 걸쇠라 아침 소나기 한 번이 그날을 통째로 잠근다.
    """
    try:
        return bool(get_settings().weather_auto_default_enabled)
    except Exception:  # noqa: BLE001 - 설정을 못 읽어도 날씨 갈래가 죽으면 안 된다
        logger.warning("날씨 자동 전환 스위치를 못 읽었다 — 끈 것으로 본다")
        return False


def destination_for(reading: WeatherReading) -> Destination:
    """비·눈이면 실내, 아니면 실외 (설계 §2.1-2).

    ⛔ 스위치가 꺼져 있으면 비·눈이어도 실내로 안 넘긴다(위 `weather_auto_default_on`).
    """
    if reading.precipitating and weather_auto_default_on():
        return Destination.INDOOR_TAGGING
    if reading.status is WeatherStatus.UNKNOWN:
        return DEFAULT_WHEN_UNKNOWN
    return Destination.OUTDOOR_TAGGING


async def peek_today_default(
    session: AsyncSession, *, today: dt.date | None = None
) -> DefaultDestination | None:
    """DB에 있는 그날 기본 목적지. **날씨를 조회하지 않는다.**

    조회를 안 하는 게 핵심이다 — 셔틀 신호를 받는 길목에서 부르는 자리라, 여기서 바깥
    HTTP를 타면 대기 창이 열리는 시각이 날씨 서버 응답 속도에 매달린다.
    """
    day = today or kst_today()
    row = (
        await session.execute(
            select(DailyDefaultDestination).where(
                DailyDefaultDestination.service_date == day
            )
        )
    ).scalars().one_or_none()
    return DefaultDestination.of(row) if row is not None else None


# ⛔ 그날 첫 기본 목적지 조회에 주는 **총 시간**(초). `weather_timeout_sec`(기본 4초)는 고리
# 하나 몫이지 요청당 총예산이 아니다 — 사슬이 기상청 실황(최대 두 슬롯) → 예보 → wttr 둘이라
# 상류가 전부 느리면 이 요청 하나가 **십수 초** 매달린다(`routers/dispatch.
# get_default_destination` 주석이 그 위험을 스스로 적어 뒀는데 막는 자리는 없었다).
#
# 8초인 이유와 값은 예보 창구(`routers/weather.FORECAST_TOTAL_BUDGET_SEC`)와 **같게 맞춘다** —
# 같은 상류를 같은 화면이 부르는데 예산이 둘로 갈리면 사람이 겪는 최악이 두 벌이 된다.
ENSURE_DEFAULT_WEATHER_BUDGET_SEC = 8.0

# 그날 첫 조회를 **한 번으로 접는 잠금**(단일비행). 아침에 관제 탭이 셋 열리면 셋이 각각 같은
# 사슬을 타서 상류를 세 번 두드리고 셋 다 느리게 답한다. 예보 쪽에는 이미 같은 장치가 있다.
#
# ⚠ 모듈 자리에서 `asyncio.Lock()`을 한 번 만들어 두면 **루프가 바뀌는 순간 터진다** —
# `asyncio.Lock`은 처음 경합할 때 그 루프에 자기를 매고, 다른 루프에서 경합하면 RuntimeError다.
# 경합이 없는 판에서는 조용히 지나가서 터질 때만 터진다. 그래서 루프가 바뀌면 새로 만든다
# (`weather._forecast_flight_lock`이 같은 이유로 같은 모양이다. 서버는 루프가 하나라 늘 같다).
_default_weather_lock: asyncio.Lock | None = None
_default_weather_lock_loop: asyncio.AbstractEventLoop | None = None


def _default_weather_flight_lock() -> asyncio.Lock:
    """지금 도는 루프에 매인 잠금 하나."""
    global _default_weather_lock, _default_weather_lock_loop
    loop = asyncio.get_running_loop()
    if _default_weather_lock is None or _default_weather_lock_loop is not loop:
        _default_weather_lock = asyncio.Lock()
        _default_weather_lock_loop = loop
    return _default_weather_lock


async def ensure_today_default(
    session: AsyncSession, *, today: dt.date | None = None
) -> DefaultDestination:
    """그날 기본 목적지를 보장한다. 없으면 **그때** 날씨를 조회해 만든다(lazy 갱신).

    스케줄러를 새로 안 만든 이유가 여기 있다(설계 §6.1 "날씨 조회 시각"이 미정이었다).
    그날 처음 이 값을 묻는 순간이 곧 아침 조회 시각이라, 관제 화면이 아침에 켜지면 그때
    갱신되고 화면을 아무도 안 켜면 첫 셔틀이 왔을 때 갱신된다.

    ⚠ 행이 이미 있으면 **무엇도 안 덮는다.** 요원이 손으로 고른 값이 그날 안에는 날씨를
    이기는 규칙(설계 §2.1-3)이 이 한 줄이다.
    """
    day = today or kst_today()
    existing = await peek_today_default(session, today=day)
    if existing is not None:
        return existing

    # ⭐ 여기서부터가 **바깥 HTTP를 타는 유일한 구간**이라 총예산과 단일비행을 둘 다 건다
    #    (2026-08-09 백지검토 ⑤). 같은 상류를 쓰는 예보 창구엔 둘 다 있는데 이 자리만
    #    맨몸이었다 — 아침 첫 대시보드 로드가 이 자리다.
    async with _default_weather_flight_lock():
        # ⚠ 잠금을 기다리는 사이에 앞선 요청이 그날 행을 이미 넣었을 수 있다. 다시 안 보면
        #    단일비행이 "줄만 세우고 결국 다들 조회하는" 모양이 된다. `insert_auto_default`가
        #    커밋까지 하고 나오므로 이 재조회에 그 행이 보인다.
        existing = await peek_today_default(session, today=day)
        if existing is not None:
            return existing

        try:
            reading = await asyncio.wait_for(
                fetch_current_weather(), ENSURE_DEFAULT_WEATHER_BUDGET_SEC
            )
        except asyncio.TimeoutError:
            # ⚠ `fetch_current_weather`는 예외를 밖으로 안 내보내므로 여기 오는 갈래는
            #    **예산 초과 하나뿐**이다.
            # ⚠ 행을 안 만들고 돌아가는 길은 없다 — 이 함수 계약이 "그날 값을 보장한다"라
            #    빈손으로 나가면 부르는 라우트가 500이 된다. 그래서 조회가 통째로 실패했을
            #    때와 **같은 모양**으로 만든다(`weather._fallback`: 마지막 성공값이 있으면
            #    stale로, 없으면 UNKNOWN). 비가 왔다면 5분 주기 조회가 그 뒤에 실내로 넘긴다.
            cached = last_weather_success()
            reading = (
                replace(cached, ok=False, stale=True)
                if cached is not None
                else unknown_reading(ok=False)
            )
            logger.warning(
                "%s 그날 기본 목적지 날씨 조회가 %.1f초를 넘겼다 — %s으로 행을 만든다",
                day.isoformat(),
                ENSURE_DEFAULT_WEATHER_BUDGET_SEC,
                "마지막 성공값" if cached is not None else "UNKNOWN",
            )

        stored, _inserted = await insert_auto_default(session, day, reading)
        return stored


async def insert_auto_default(
    session: AsyncSession, day: dt.date, reading: WeatherReading
) -> tuple[DefaultDestination, bool]:
    """받아 둔 날씨로 그날 행을 만든다. **이미 있으면 아무것도 안 덮는다.**

    `(지금 행, 이번에 내가 넣었나)`를 돌려준다.

    ⚠ 두 번째 칸이 계약의 핵심이다. `on_conflict_do_nothing`이라 경합에서 진 쪽은 **한 행도
    안 넣는데** 예전에는 결과와 무관하게 "바뀌었다"를 돌려줬다. 요원 수동 행이 먼저 들어간
    순간에 헛 방송이 한 장 나가는 자리였다(2026-08-03 교차 검증 W4). `rowcount`가 실제로
    넣었는지를 말해 준다.

    `ensure_today_default`(lazy 갱신)와 주기 조회(`apply_weather_default`)가 같은 자리를
    쓴다 — 두 벌로 적으면 한쪽만 고쳐질 자리다. 조회는 여기서 안 한다(부르는 쪽이 이미 값을
    들고 있다).
    """
    destination = destination_for(reading)
    result = await session.execute(
        pg_insert(DailyDefaultDestination)
        .values(
            service_date=day,
            destination=destination.value,
            source=DefaultSource.AUTO.value,
            weather_status=reading.status.value,
            weather_desc=reading.description[:64],
            weather_ok=reading.ok,
        )
        # 경합(두 요청이 같은 순간에 그날 첫 조회를 함)이면 먼저 넣은 쪽이 정본이다.
        .on_conflict_do_nothing(index_elements=["service_date"])
    )
    inserted = result.rowcount > 0
    await session.commit()
    stored = await peek_today_default(session, today=day)
    if stored is None:  # 방어 — UNIQUE 충돌도 아닌데 행이 없다면 자료 문제다.
        raise RuntimeError(f"기본 목적지 행을 만들지 못했다 (service_date={day})")
    if inserted:
        logger.info(
            "%s 그날 기본 목적지를 날씨로 정한다 — %s (%s/%s, 조회성공=%s)",
            day.isoformat(), destination.value, reading.status.value,
            reading.description, reading.ok,
        )
    else:
        logger.info(
            "%s 그날 기본 목적지 행이 이미 있어 안 덮었다 — 지금 값은 %s(%s)",
            day.isoformat(), stored.destination, stored.source,
        )
    return stored, inserted


async def set_manual_default(
    session: AsyncSession, destination: Destination, *, today: dt.date | None = None
) -> DefaultDestination:
    """요원이 손으로 고른 기본 목적지. 그날 행을 만들거나 덮는다.

    덮을 때 날씨 칸은 그대로 둔다 — 그날 무슨 날씨였는지는 여전히 사실이고, 요원 선택은
    `source=manual`로 드러난다.

    ⚠ **행을 새로 만드는 갈래에서는 날씨 칸을 비워 두지 않는다.** 그날 행이 이 창구로 먼저
    생기면 `ensure_today_default`가 "이미 있다"로 그대로 돌려주므로 날씨 조회가 영영 안 돈다
    — 세 칸이 null인 채로 화면까지 나가는데, 계약(§5.2)은 `weather.status`를 낱말(UNKNOWN
    포함)로, `ok`를 bool로 정했다. null이면 화면의 "날씨를 받지 못했습니다" 조건이 안 걸린다.
    그래서 만드는 갈래에만 UNKNOWN·False를 같이 넣는다(덮어쓰기 갈래는 기존 값 보존).
    """
    day = today or kst_today()
    stmt = pg_insert(DailyDefaultDestination).values(
        service_date=day,
        destination=destination.value,
        source=DefaultSource.MANUAL.value,
        # on_conflict_do_update가 이 셋을 안 건드리므로 **새 행에만** 실린다.
        weather_status=WeatherStatus.UNKNOWN.value,
        weather_ok=False,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["service_date"],
            set_={
                "destination": destination.value,
                "source": DefaultSource.MANUAL.value,
                # pg_insert는 ORM의 onupdate를 안 태운다. 여기서 직접 찍는다.
                "updated_at": func.now(),
            },
        )
    )
    await session.commit()
    stored = await peek_today_default(session, today=day)
    if stored is None:
        raise RuntimeError(f"기본 목적지를 저장하지 못했다 (service_date={day})")
    logger.info(
        "%s 기본 목적지를 요원이 %s로 고쳤다 — 그날 안에는 날씨 조회가 못 덮는다",
        day.isoformat(), destination.value,
    )
    return stored


# ── 날씨 주기 조회가 부르는 자리 (2026-08-03 사용자 확정 규칙 셋) ────────────
#
# 1. 아침 정해진 시각에 날씨로 기본 목적지를 **한 번** 정한다(비·눈이면 실내, 아니면 실외).
# 2. ⭐ **비나 눈이 오기 시작하면 그 순간 실내로 넘긴다.** 예전 규칙은 "아침에 한 번 정하면
#    그날은 안 바뀐다"였는데 2026-08-03에 사용자가 뒤집었다.
# 3. 요원이 화면에서 손으로 고른 값이 있으면 **그 값이 이긴다.** 그날 안에는 날씨가 못 덮는다.
#
# ⚠ 2번은 **한 방향 걸쇠**다. 비가 그쳤다고 실외로 되돌리지 않는다 — 사용자가 정한 건
# "오기 시작하면 실내로"까지고, 되돌리는 규칙은 없다. 젖은 노면이 바로 마르지도 않는다.

# 마지막으로 방송한 봉투 내용(`observed_at` 뺀 payload). 같은 값이면 다시 안 쏜다.
# 인메모리라 재기동하면 첫 바퀴가 무조건 한 장 쏜다 — 새로 붙은 화면이 값을 받는 자리라
# 그게 맞다. 시험 위생은 `reset_dispatch_state`가 같이 걷는다.
_last_weather_broadcast: dict[str, Any] | None = None

# 마지막으로 받은 날씨 한 벌. 초기 로드 스냅샷이 읽는 자리다(W2). 인메모리라 재기동하면
# 비고, 그때는 `weather.last_success()`로 물러섰다가 첫 바퀴가 곧 채운다.
_last_weather_reading: WeatherReading | None = None


async def refresh_weather_columns(
    session: AsyncSession, day: dt.date, reading: WeatherReading
) -> bool:
    """그날 행의 날씨 근거 세 칸을 지금 값으로 맞춘다. 실제로 바꿨으면 True.

    ⭐ **목적지 전환과 무관하게 돈다.** 예전에는 조기 반환 넷(요원 수동·비 안 옴·이미
    실내·경합) 때문에 목적지가 안 바뀌면 이 칸들이 아침 값에 영영 굳었다. 그래서 WS 한 장
    안에서 `payload.status`(지금 조회)와 `payload.default.weather.status`(DB)가 서로 다른
    값을 말하고, 화면이 어느 쪽을 칩에 쓸지 계약이 없었다(2026-08-03 교차 검증 W5).

    `source`는 안 본다 — 요원이 목적지를 손으로 골랐어도 "그때 날씨가 뭐였나"는 여전히
    사실이고, 두 자리가 같은 값을 말해야 한다는 계약은 그 갈래에도 그대로다.

    ⚠ `updated_at`은 안 민다. 그 칸은 화면이 "이 결정이 언제 바뀌었나"로 읽는 자리라,
    5분마다 도는 날씨 갱신으로 밀면 결정이 바뀐 것처럼 보인다. 얼마나 최신인지는 봉투의
    `observed_at`이 말한다.

    ⚠ 값이 실제로 다를 때만 UPDATE가 걸린다(`is_distinct_from`). 5분마다 같은 값을 다시
    쓰면 헛 write가 하루 288번이고, 돌려주는 bool도 뜻을 잃는다.
    """
    desc = reading.description[:64]
    result = await session.execute(
        update(DailyDefaultDestination)
        .where(
            DailyDefaultDestination.service_date == day,
            or_(
                DailyDefaultDestination.weather_status.is_distinct_from(
                    reading.status.value
                ),
                DailyDefaultDestination.weather_desc.is_distinct_from(desc),
                DailyDefaultDestination.weather_ok.is_distinct_from(reading.ok),
            ),
        )
        .values(
            weather_status=reading.status.value,
            weather_desc=desc,
            weather_ok=reading.ok,
        )
    )
    await session.commit()
    return result.rowcount > 0


async def apply_weather_default(
    session: AsyncSession,
    reading: WeatherReading,
    *,
    today: dt.date | None = None,
    now: dt.datetime | None = None,
) -> tuple[DefaultDestination | None, bool]:
    """받은 날씨를 그날 기본 목적지에 반영한다. `(지금 행, 이번에 바뀌었나)`를 돌려준다.

    조회를 안 한다 — 값은 부르는 쪽(주기 조회)이 이미 들고 있다.

    행이 없고 아침 시각 전이면 `(None, False)`다. 새벽 날씨로 하루를 정해 버리지 않으려는
    거고, 그 시간대에 셔틀이 오면 `ensure_today_default`가 그 자리에서 정한다.

    ⚠ `stale`(마지막 성공값 재사용)도 그대로 판정에 쓴다. 따로 안 가르는 이유는, 조회가
    죽은 사이에 새로 생긴 비는 어차피 아무도 모르고, 죽기 전 마지막 관측이 비였다면 이미
    그때 걸쇠가 걸렸기 때문이다 — 가짜 전환을 새로 만들지 않는다.
    """
    day = today or kst_today(now)
    row = await peek_today_default(session, today=day)

    if row is None:
        moment = kst_moment(now)
        hour = WEATHER_MORNING_HOUR
        minute = WEATHER_MORNING_MINUTE
        if (moment.hour, moment.minute) < (hour, minute):
            return None, False
        # ⚠ 경합에서 진 갈래(요원 수동 행이 먼저 들어감)는 한 행도 안 넣는다 — 그때 True를
        # 돌려주면 헛 방송이 한 장 나간다(W4).
        return await insert_auto_default(session, day, reading)

    # ⭐ 목적지가 바뀌든 말든 **날씨 근거부터** 지금 값으로 맞춘다(W5).
    if await refresh_weather_columns(session, day, reading):
        row = await peek_today_default(session, today=day) or row

    # 규칙 3 — 요원이 고른 값이 이긴다.
    if row.source == DefaultSource.MANUAL.value:
        return row, False

    # 규칙 2 — 비·눈이 아니면 아무것도 안 한다(되돌리는 규칙은 없다).
    # ⛔ 스위치가 꺼져 있으면 비·눈이어도 안 넘긴다(`weather_auto_default_on` 참고).
    #    날씨 근거(weather_status·weather_desc)는 위에서 이미 갱신했으므로, 화면은 지금
    #    날씨를 그대로 보고 목적지만 아침에 정해진 값으로 남는다.
    if not reading.precipitating or not weather_auto_default_on():
        return row, False
    if row.destination == Destination.INDOOR_TAGGING.value:
        return row, False

    # ⚠ 읽고-고치고-쓰기가 아니라 **조건이 붙은 UPDATE 한 장**이다. 여기 오는 사이에 요원이
    # 손으로 골랐으면 `source='auto'` 조건이 안 맞아 0행이 바뀌고, 요원 선택이 그대로 산다.
    result = await session.execute(
        update(DailyDefaultDestination)
        .where(
            DailyDefaultDestination.service_date == day,
            DailyDefaultDestination.source == DefaultSource.AUTO.value,
            DailyDefaultDestination.destination != Destination.INDOOR_TAGGING.value,
        )
        .values(
            destination=Destination.INDOOR_TAGGING.value,
            weather_status=reading.status.value,
            weather_desc=reading.description[:64],
            weather_ok=reading.ok,
            # pg 업데이트는 ORM의 onupdate를 안 태운다. 여기서 직접 찍는다.
            updated_at=func.now(),
        )
    )
    await session.commit()
    stored = await peek_today_default(session, today=day)
    if result.rowcount == 0:
        # 그 사이에 요원이 골랐다. 덮지 않은 게 정답이다.
        return stored, False
    logger.info(
        "%s 비·눈이 와서 기본 목적지를 실내로 넘긴다 — %s(%s), 예전 값 %s",
        day.isoformat(), reading.status.value, reading.description, row.destination,
    )
    return stored, True


def weather_payload(
    reading: WeatherReading, default: DefaultDestination | None
) -> dict[str, Any]:
    """`weather_update` 봉투의 payload 한 벌.

    ⚠ **초기 로드 스냅샷도 이 함수를 쓴다**(`weather_snapshot`). 두 벌로 적으면 화면이 WS
    메시지와 스냅샷을 서로 다른 모양으로 파싱해야 하고, 칸이 하나 늘 때 한쪽만 늘어난다
    (2026-08-03 교차 검증 W2).
    """
    return {
        "status": reading.status.value,
        "description": reading.description,
        "temp_c": reading.temp_c,
        "precip_mm": reading.precip_mm,
        # False면 이번 조회가 실패해 마지막 성공값이나 UNKNOWN으로 갔다는 뜻이다.
        # 화면은 이때 "날씨를 받지 못했습니다"를 띄운다(설계 §5.2).
        "ok": reading.ok,
        "stale": reading.stale,
        # ⭐ 값이 어디서 왔나 — `kma`·`wttr`·`unknown`(2026-08-06 · 프론트 31차 §4-1).
        # 화면은 `wttr`일 때만 대체 출처 표식을 단다. 평소에 "기상청"이라 적으면 글자만
        # 늘고 아무 말도 안 하는 칸이 된다.
        "source": reading.source,
        "observed_at": reading.observed_at.isoformat() if reading.observed_at else None,
        "default": default.as_payload() if default is not None else None,
    }


def current_weather() -> WeatherReading | None:
    """서버가 지금 아는 날씨 한 벌. 아직 한 바퀴도 안 돌았으면 None이다.

    마지막으로 방송한 값이 정본이고, 그게 없으면 마지막 성공값으로 물러선다(재기동 직후
    첫 바퀴 전 자리). 인메모리라 `reset_dispatch_state`가 같이 걷는다.
    """
    return _last_weather_reading or last_weather_success()


async def weather_snapshot(session: AsyncSession) -> dict[str, Any] | None:
    """대시보드 초기 로드에 실을 날씨 한 칸. `weather_update` payload와 **같은 모양**이다.

    ⚠ `weather_update`는 값이 바뀔 때만 나간다(`handle_weather_reading`의 억제). 그래서
    시연 중 탭을 새로 열면 다음 변화가 올 때까지 상단 기상 칩이 빈 채로 남았다 — 그 구멍을
    스냅샷이 메운다(2026-08-03 교차 검증 W2).

    ⚠ 여기서 날씨를 조회하지 않는다(peek). 화면이 붙을 때마다 바깥 HTTP를 타면 초기 로드가
    날씨 서버 응답 속도에 매달린다.
    """
    reading = current_weather()
    if reading is None:
        return None
    return weather_payload(reading, await peek_today_default(session))


async def broadcast_weather(payload: dict[str, Any]) -> None:
    """날씨 한 장을 대시보드 전체에 민다(상단 기상 칩).

    봉투는 `make_envelope` 그대로라 다른 대시보드 메시지와 칸이 나란하다. 기본 목적지를
    같이 싣는 이유는, 비가 와서 실내로 넘어간 순간에 화면이 **두 값을 한 장으로** 받아야
    칩과 목적지 표시가 어긋나지 않기 때문이다.

    ⚠ payload를 **받는다**(값에서 만들지 않는다). 부르는 쪽이 방송 억제를 재려고 이미 한 벌
    만들어 두기 때문이라, 여기서 다시 만들면 두 벌이 갈릴 자리가 생긴다.
    """
    await manager.broadcast(make_envelope(WEATHER_MESSAGE, payload))


def broadcast_signature(payload: dict[str, Any]) -> dict[str, Any]:
    """방송 억제의 잣대 — **`observed_at`만 뺀 봉투 전체**.

    ## 왜 넓혔나 (2026-08-03 재검증 F7)

    예전 잣대는 `(상태 낱말, 조회 성공)` 둘뿐이었다. 그래서 문구·기온·강수량이 바뀌어도
    방송이 안 나갔다 — 실측으로 방송은 한 장인데 DB 문구는 '흐림'이고 열려 있던 화면은
    '구름많음'이었다(SKY 3과 4가 둘 다 CLOUDY라 낱말은 안 바뀌었다). 봉투 **안**의 어긋남은
    W5가 닫았는데 **시간축**으로 같은 어긋남이 열려 있었다.

    잣대를 "봉투 내용"으로 두면 지킬 성질이 한 줄로 선다 — **열려 있는 화면이 마지막으로 받은
    봉투는 서버가 아는 지금 값과 같다.** 칸이 하나 늘어도 잣대를 따로 안 고친다.

    ## `observed_at`만 빼는 이유

    그 칸은 조회할 때마다 반드시 바뀐다(관측 시각이든 조회 시각이든). 넣으면 억제가 통째로
    무력해져서 5분마다 같은 그림이 나간다.

    ⚠ 대가는 안다 — 기온 한 칸이 바뀌면 방송이 나간다(하루 최대 288장, 폴링 주기와 같다).
    그건 **진짜로 바뀐 값**이지 중복이 아니고, 봉투 하나가 수백 바이트다. 반대로 기온을 빼면
    칩의 온도가 상태 낱말이 바뀔 때까지 몇 시간 굳는데, 그게 F7과 똑같은 계열의 결함이다.
    """
    return {key: value for key, value in payload.items() if key != "observed_at"}


async def handle_weather_reading(reading: WeatherReading) -> None:
    """주기 조회 한 바퀴가 끝날 때마다 부른다(`weather.start_weather_poller`의 콜백).

    ⚠ **안 바뀌었으면 안 쏜다.** 5분마다 똑같은 값을 쏘면 화면 로그가 도배되고, 붙어 있는
    화면 수만큼 헛 전송이 는다. "바뀜"의 잣대는 `broadcast_signature`에 있다.
    """
    global _last_weather_broadcast, _last_weather_reading

    async with get_session() as session:
        default, changed = await apply_weather_default(session, reading)

    # 스냅샷이 읽을 자리다(W2). 억제로 방송을 삼켜도 "서버가 아는 지금 날씨"는 갱신한다 —
    # 새로 붙는 탭이 봐야 하는 건 마지막 방송이 아니라 지금 값이다.
    _last_weather_reading = reading
    payload = weather_payload(reading, default)
    signature = broadcast_signature(payload)
    if not changed and signature == _last_weather_broadcast:
        return
    _last_weather_broadcast = signature
    await broadcast_weather(payload)


def _as_destination(value: str | None) -> Destination | None:
    """DB·창에 담긴 문자열을 목적지 낱말로. 계약 밖 값이면 None이다."""
    if value is None:
        return None
    try:
        return Destination(value)
    except ValueError:
        logger.warning("기본 목적지 값 %r이 계약 밖이다 — 다음 후보로 넘어간다", value)
        return None


async def destination_without_fetch(
    session: AsyncSession,
    *,
    service_date: dt.date | None = None,
    snapshot: str | None = None,
) -> Destination:
    """⚠ **바깥 HTTP를 안 타고** 지금 아는 기본 목적지를 고른다.

    명령을 실제로 내는 자리(자동 발사·재전송 복구)가 전부 이걸 쓴다. 여기서 날씨 서버를
    기다리면 5초 창이 닫힌 뒤에 최대 4초를 더 기다리게 된다(2026-07-31 검증 P2).

    순서는 넷이다.
    1. 그날 행(창이 열릴 때 띄운 미리 조회가 끝났으면 여기 있다)
    2. 창이 열릴 때 들고 있던 값(`snapshot`) — 화면에 이미 그 값이 보였다
    3. 마지막으로 성공한 날씨(인메모리) — 행은 아직 없어도 판단 근거는 같다
    4. 설정값(`shuttle_destination`)
    """
    row = await peek_today_default(session, today=service_date)
    picked = _as_destination(row.destination if row is not None else None)
    if picked is not None:
        return picked

    picked = _as_destination(snapshot)
    if picked is not None:
        return picked

    cached = last_weather_success()
    if cached is not None:
        logger.info(
            "그날 기본 목적지 행이 아직 없다 — 마지막 성공 날씨(%s)로 정한다",
            cached.status.value,
        )
        return destination_for(cached)
    return get_settings().shuttle_destination


async def _prefetch_default(day: dt.date) -> None:
    """그날 기본 목적지를 백그라운드로 만들어 둔다(여기서만 바깥 HTTP를 탄다)."""
    try:
        async with get_session() as session:
            await ensure_today_default(session, today=day)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 태스크 밖으로 새면 아무도 안 받는다
        logger.exception("%s 그날 기본 목적지 미리 조회에 실패했다", day.isoformat())


def _drop_prefetch(day: dt.date, done: asyncio.Task) -> None:
    """끝난 미리 조회를 목록에서 뺀다. **자기가 등록한 태스크일 때만** 뺀다.

    ⚠ 조건 없이 `pop(day)`을 하면 늦게 끝난 앞 태스크가 **뒤이어 새로 띄운 태스크의 자리를**
    지운다. 그러면 `_spawn_default_prefetch`가 그날 조회가 없는 줄 알고 하나 더 띄우고,
    `_join_prefetch`도 도는 조회를 못 찾아 그냥 지나간다 — 같은 날짜 조회가 여럿 겹치는
    갈래다(날짜당 하나가 이 함수 짝의 계약이다).
    """
    if _prefetch_tasks.get(day) is done:
        del _prefetch_tasks[day]


def _spawn_default_prefetch(day: dt.date) -> None:
    """미리 조회를 띄우기만 한다. **await 하지 않는다.**

    창이 열리는 시각이 날씨 서버 응답 속도에 매달리면 화면 카운트다운이 늦게 뜬다. 창이
    도는 동안 조회가 끝나면 자동 발사가 그 행을 그대로 쓰고, 못 끝내도 발사는 기다리지
    않는다(`PREFETCH_JOIN_SEC`까지만 봐주고 아는 값으로 나간다).

    날짜당 하나만 띄운다 — 셔틀이 연달아 오면 같은 조회를 여럿이 동시에 태우게 된다.
    """
    live = _prefetch_tasks.get(day)
    if live is not None and not live.done():
        return
    task = asyncio.create_task(_prefetch_default(day))
    _prefetch_tasks[day] = task
    task.add_done_callback(lambda done: _drop_prefetch(day, done))


async def _join_prefetch(day: dt.date) -> None:
    """도는 미리 조회를 **짧게만** 기다린다. 안 끝나면 그냥 두고 간다.

    ⚠ `shield`로 감싼다. `wait_for`는 시간이 지나면 기다리던 대상을 취소하는데, 여기서
    취소하면 다른 셔틀도 쓸 그날 조회가 통째로 날아간다.
    """
    task = _prefetch_tasks.get(day)
    if task is None or task.done():
        return
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=PREFETCH_JOIN_SEC)
    except asyncio.TimeoutError:
        logger.info(
            "%s 그날 기본 목적지 미리 조회가 %.1f초 안에 안 끝나 아는 값으로 발사한다",
            HOLD_LOG_TAG, PREFETCH_JOIN_SEC,
        )
    except Exception:  # noqa: BLE001 - 조회 실패는 폴백이 받는다
        logger.warning("%s 미리 조회가 실패했다 — 아는 값으로 발사한다", HOLD_LOG_TAG)


# ── 대기 창 (설계 §2.2) ────────────────────────────────────────────────────

@dataclass
class PendingWindow:
    """셔틀 신호 하나를 붙들고 있는 5초 창."""

    arrival_id: int
    event_id: str | None
    gate_no: int | None
    shuttle_no: str | None
    room_id: str | None
    command_id: str
    hold_sec: float
    opened_at: dt.datetime
    deadline: dt.datetime
    # 이 창이 열린 KST 날짜. 발사 시각에 그날 기본 목적지를 되찾는 열쇠다.
    # ⚠ 발사 시점의 `kst_today()`를 다시 부르면 안 된다 — 23:59:58에 열린 창은 자정을
    # 넘겨 만료되므로 그때는 아직 아무것도 없는 "다음 날" 행을 찾게 된다.
    service_date: dt.date
    # 창이 열릴 때 서버가 알고 있던 기본 목적지. 아직 그날 첫 조회 전이면 None이고,
    # 그때는 자동 발사 시점에 날씨를 조회해 정한다.
    default_destination: str | None
    task: asyncio.Task | None = None
    # 이미 누가 이 창을 처리했나. 검사와 표시 사이에 await가 없어서 asyncio에서 원자다.
    resolved: bool = False

    def remain_sec(self, now: dt.datetime | None = None) -> float:
        left = (self.deadline - (now or _utc_now())).total_seconds()
        return round(left, 3) if left > 0 else 0.0

    def as_payload(self, now: dt.datetime | None = None) -> dict[str, Any]:
        """화면이 카운트다운을 그리는 데 필요한 전부."""
        return {
            "arrival_id": self.arrival_id,
            "event_id": self.event_id,
            "gate_no": self.gate_no,
            "shuttle_no": self.shuttle_no,
            "command_id": self.command_id,
            "hold_sec": self.hold_sec,
            "opened_at": self.opened_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "remain_sec": self.remain_sec(now),
            # 화면 버튼 셋. 낱말을 서버가 실어 보내야 화면과 계약이 안 갈라진다.
            "choices": [c.value for c in WindowChoice],
            "default_destination": self.default_destination,
        }


def with_room(payload: dict[str, Any], room_id: str | None) -> dict[str, Any]:
    """[초안·팀 확정 대기] 안건① room_id 칸을 플래그가 켜졌을 때만 붙인다.

    ⚠ 정의 자리를 여기 하나로 뒀다. `app/shuttle_call.py`가 이 함수를 가져다 쓰고,
    `shuttle_arrival`과 `shuttle_dispatch_result`가 같은 규칙을 지난다 — 예전에는 알림에만
    room_id가 실리고 확정 결과엔 안 실려서, 플래그를 켠 화면이 두 메시지를 못 묶었다.
    (`app/routers/ingest.py`에도 같은 이름 헬퍼가 있는데 그 파일은 다른 조 소유라 그대로 뒀다.)
    """
    if get_settings().credit_scope_room:
        return {**payload, "room_id": room_id}
    return payload


@dataclass
class DispatchOutcome:
    """명령 한 번의 결과. 라우터 응답과 WS 메시지가 같은 값을 본다."""

    source: DispatchSource
    command: str | None            # cmd_destination / return_to_charge / emergency_stop
    destination: str | None
    emergency_stop: bool | None
    command_id: str | None
    notified_robot_count: int
    arrival_id: int | None = None
    event_id: str | None = None
    gate_no: int | None = None
    room_id: str | None = None
    decided_at: dt.datetime | None = None

    def as_payload(self) -> dict[str, Any]:
        return with_room(
            {
                "arrival_id": self.arrival_id,
                "event_id": self.event_id,
                "gate_no": self.gate_no,
                "source": self.source.value,
                "command": self.command,
                "destination": self.destination,
                "emergency_stop": self.emergency_stop,
                "command_id": self.command_id,
                # 0이면 붙은 로봇이 없어 명령이 못 갔다는 뜻이다(화면에서 확인 가능).
                "notified_robot_count": self.notified_robot_count,
                "decided_at": (self.decided_at or _utc_now()).isoformat(),
            },
            self.room_id,
        )


@dataclass
class DispatchStart:
    """셔틀 신호를 받은 직후의 상태. 창을 열었으면 `pending`이 차 있다.

    ⚠ `outcome`은 **아직 안 방송한 확정 결과**다. 창을 안 연 갈래(긴급정지 차단·대기 창
    꺼짐)는 신호를 받은 그 자리에서 이미 결론이 나는데, 그걸 여기서 바로 방송하면
    `shuttle_dispatch_result`가 `shuttle_arrival`보다 **먼저** 나간다 — 계약 §2.1이 "곧이어
    따라온다"라 화면은 arrival을 먼저 받는다고 짜여 있다. 그래서 방송을 부르는 쪽
    (`app/shuttle_call.record_shuttle_arrival`)에 맡기고 여기서는 실어만 보낸다.
    """

    pending: dict[str, Any] | None
    notified_robot_count: int
    command_id: str
    outcome: DispatchOutcome | None = None


_windows: dict[int, PendingWindow] = {}

# 긴급정지를 세운 상태인가. 인메모리이고, 화면 버튼 모양만이 아니라 **명령을 낼 수 있나**를
# 정하는 값이다(모듈 머리 "긴급정지" 표).
_emergency_stopped = False

# 창을 열 때 띄운 그날 기본 목적지 미리 조회. 날짜당 하나이고, 참조를 안 들고 있으면
# 파이썬이 도는 태스크를 걷어갈 수 있어서 여기 담아 둔다(끝나면 스스로 빠진다).
_prefetch_tasks: dict[dt.date, asyncio.Task] = {}

# 창을 이미 `_claim`으로 집어 발사 중인 태스크. `_claim`이 `_windows`에서 빼 버려서
# `reset_dispatch_state`가 `_windows`만 훑으면 **이 태스크들은 아예 안 보인다** — 시험
# 정리(TRUNCATE)와 겹쳐 돌면서 없는 신호로 명령·알림을 낸다. 여기 따로 모아 같이 취소한다.
_firing_tasks: set[asyncio.Task] = set()

# 발사 직전에 미리 조회를 기다려 주는 최대 시간(초). 창이 도는 내내(기본 5초) 돌던
# 태스크가 DB 왕복 몇 번을 못 끝냈을 때를 위한 여유일 뿐이다.
#
# ⚠ 이 값이 작아야 하는 이유 — 예전에는 발사 자리에서 날씨 조회를 통째로 기다려서 5초
# 창이 닫힌 뒤 최대 4초(`weather_timeout_sec`)를 더 썼다(2026-07-31 검증 P2). 지금은
# 바깥이 느리면 여기서 손을 떼고 아는 값으로 나간다.
PREFETCH_JOIN_SEC = 0.5

# 즉시 명령 유량 제한. 종류별 마지막 발사 시각(단조 시계)이다.
_last_command_at: dict[str, float] = {}

# 셔틀 신호와 무관한 단독 명령의 이름표. 같은 밀리초 연타를 가르는 tiebreaker까지 붙인다
# (shuttle_call.next_web_call_event_id와 같은 방식).
_seq_lock = threading.Lock()
_seq = 0


def _next_command_id(kind: str) -> str:
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    return f"{kind}-{int(time.time() * 1000)}-{seq}"


def reset_dispatch_state() -> list[asyncio.Task]:
    """대기 창 태스크를 전부 취소하고 인메모리 상태를 비운다. 취소한 태스크를 돌려준다.

    시험 위생용이고 conftest `_clean`이 매 케이스 앞에 부른다. **안 부르면 앞 케이스가
    연 5초 창이 다음 케이스가 DB를 기다리는 동안 터져서** 남의 시험에 명령·알림이 새어
    든다. 서버 재기동과 같은 자리이기도 하다(창을 잃는다).

    ⚠ 창 목록(`_windows`)만 훑으면 **이미 발사에 들어간 태스크를 못 본다.** `_claim`이 창을
    `_windows`에서 빼기 때문이다 — 그 태스크는 취소도 안 된 채 TRUNCATE와 겹쳐 돈다. 그래서
    `_firing_tasks`도 같이 취소한다.

    ⚠ 여기서는 취소가 **끝나기를 기다리지 못한다**(동기 함수라). 취소는 그 태스크가 다음
    await에서 깨어날 때 배달되므로, 정리와 DB 비우기 사이 순서를 확실히 하려면 부르는 쪽이
    돌려받은 태스크를 `await asyncio.gather(*tasks, return_exceptions=True)`로 기다려야 한다.
    """
    global _emergency_stopped, _last_weather_broadcast, _last_weather_reading
    # 날씨 방송 중복 억제 상태도 같이 걷는다 — 안 걷으면 앞 케이스가 쏜 값 때문에 다음
    # 케이스의 첫 방송이 "안 바뀌었다"로 삼켜진다. 스냅샷이 읽는 마지막 값도 같이 비운다.
    _last_weather_broadcast = None
    _last_weather_reading = None
    cancelled: list[asyncio.Task] = []
    for window in list(_windows.values()):
        window.resolved = True
        if window.task is not None:
            window.task.cancel()
            cancelled.append(window.task)
    _windows.clear()
    for task in list(_firing_tasks):
        task.cancel()
        cancelled.append(task)
    _firing_tasks.clear()
    for task in list(_prefetch_tasks.values()):
        task.cancel()
        cancelled.append(task)
    _prefetch_tasks.clear()
    _last_command_at.clear()
    _emergency_stopped = False
    return cancelled


def emergency_stopped() -> bool:
    return _emergency_stopped


def _require_not_stopped(what: str) -> None:
    """긴급정지가 걸려 있으면 이동 명령을 못 낸다(모듈 머리 표).

    ⚠ 명령을 내는 자리마다 이 한 줄을 지나야 한다. 상태를 화면 조회에서만 읽던 시절에는
    세워 놓은 로봇이 셔틀 한 번에 다시 움직였다(2026-07-31 검증 P1).
    """
    if _emergency_stopped:
        logger.warning("긴급정지 중이라 %s 명령을 막았다", what)
        raise EmergencyStopEngaged(
            "긴급정지 상태입니다. 긴급정지를 먼저 해제해 주세요."
        )


def check_command_throttle(kind: str) -> float:
    """즉시 명령 연타를 막는다. 창 안이면 남은 초, 아니면 0.0이고 눈금을 새로 찍는다.

    인증이 없는 창구라(routers/dispatch.py 모듈 머리) 이게 유일한 유량 제한이다. 셔틀 호출
    쿨다운과 같은 결이고, 창 안이면 **눈금을 안 민다** — 밀면 연타가 창을 계속 뒤로 끌어서
    누르는 걸 멈출 때까지 안 열린다.

    ⚠ 긴급정지 창구는 여기를 안 지난다. 세우는 것도 푸는 것도 막히면 안 되는 자리다.
    """
    interval = get_settings().dispatch_command_min_interval_sec
    if interval <= 0:
        return 0.0
    now = time.monotonic()
    last = _last_command_at.get(kind)
    if last is not None:
        remain = interval - (now - last)
        if remain > 0:
            logger.info("[출동 창구 연타 차단] %s 명령을 %.2f초 뒤에 다시 받는다", kind, remain)
            return remain
    _last_command_at[kind] = now
    return 0.0


def rollback_command_throttle(kind: str) -> None:
    """방금 찍은 눈금을 지운다. **명령이 실제로 안 나갔을 때만** 부른다.

    긴급정지 중 이동 요청(409)이 그 자리다. 서버 밖으로 나간 게 없는데 눈금이 남으면,
    요원이 긴급정지를 풀자마자 누른 이동이 429로 한 번 더 막힌다.
    """
    _last_command_at.pop(kind, None)


def pending_window_open(arrival_id: int) -> bool:
    """그 셔틀 신호의 대기 창이 아직 도나.

    기기 재시도가 "명령이 유실됐다"로 오해하고 재전송을 태우기 전에 보는 자리다
    (`app/shuttle_call.py`). 창이 살아 있으면 명령은 마감 때 나간다.
    """
    return arrival_id in _windows


def open_windows(now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """지금 열려 있는 대기 창 전부. 화면이 새로고침 뒤 카운트다운을 되살리는 자리다."""
    moment = now or _utc_now()
    return [w.as_payload(moment) for w in _windows.values()]


def _claim(window: PendingWindow) -> bool:
    """이 창을 내가 처리한다고 못 박는다.

    ⚠ 검사와 표시 사이에 `await`를 넣지 마라. 지금은 그 사이가 없어서 이벤트 루프가
    끼어들 자리가 없고(단일 스레드), 그래서 타이머와 창구가 같은 창을 두 번 못 쏜다.
    """
    if window.resolved:
        return False
    window.resolved = True
    _windows.pop(window.arrival_id, None)
    return True


def _take_open_window(arrival_id: int) -> PendingWindow | None:
    """그 셔틀 신호의 창을 집어 닫는다. 타이머도 같이 세운다."""
    window = _windows.get(arrival_id)
    if window is None or not _claim(window):
        return None
    if window.task is not None:
        window.task.cancel()
    return window


def _take_all_open_windows() -> list[PendingWindow]:
    """열린 창을 **전부** 집어 닫는다. 오래된 것부터 나온다.

    ⚠ 즉시 명령(이동·복귀·긴급정지)은 반드시 이걸 쓴다. 하나만 닫으면 게이트가 다른 셔틀이
    연달아 왔을 때 남은 창이 몇 초 뒤에 목적지 명령을 낸다 — 긴급정지에서는 세운 로봇이
    다시 움직이는 갈래였다(2026-07-31 검증 P1).
    """
    taken: list[PendingWindow] = []
    for window in list(_windows.values()):
        if _claim(window):
            if window.task is not None:
                window.task.cancel()
            taken.append(window)
    return taken


async def _arrival_alive(session: AsyncSession, arrival_id: int) -> bool:
    """그 셔틀 신호 행이 아직 있나.

    자동 발사 직전에 본다. 시험 정리(TRUNCATE)나 자료 정리 도구가 행을 지운 뒤에 타이머가
    터지면, 없는 신호로 로봇을 출동시키고 짝짓기도 못 하는 명령이 나간다.
    """
    found = (
        await session.execute(
            select(ShuttleArrival.id).where(ShuttleArrival.id == arrival_id)
        )
    ).scalar_one_or_none()
    return found is not None


async def begin_dispatch(
    session: AsyncSession,
    *,
    arrival_id: int,
    gate_no: int | None,
    event_id: str | None = None,
    shuttle_no: str | None = None,
    room_id: str | None = None,
) -> DispatchStart:
    """셔틀 신호를 받았다. 대기 창을 열고 명령은 아직 안 낸다.

    `shuttle_dispatch_hold_sec`가 0 이하면 창을 안 열고 예전처럼 즉시 발사한다 — 되돌릴
    자리이자, 명령 발사 자체를 보는 시험이 창을 안 거치고 갈 수 있는 문이다.

    ⚠ 긴급정지가 걸려 있으면 **창 자체를 안 연다.** 창을 열면 5초 뒤에 세운 로봇으로
    목적지 명령이 나간다. 그 신호는 버리고(짝짓기 목록에서 빼고) `emergency_blocked`를
    화면에 알린다 — 요원이 해제한 뒤 셔틀 호출을 다시 누르는 게 정상 흐름이다.

    ⚠ 창을 안 연 갈래 둘(긴급정지 차단·대기 창 꺼짐)은 확정 결과를 **여기서 방송하지
    않는다.** `DispatchStart.outcome`에 실어 돌려주고 부르는 쪽이 `shuttle_arrival` 뒤에
    한 번만 낸다(계약 §2.1 순서). 여기서 내면 결과가 도착 알림을 앞질러 화면이 아직
    모르는 신호의 결과를 먼저 받는다.
    """
    hold = get_settings().shuttle_dispatch_hold_sec
    command_id = f"shuttle-{arrival_id}"

    if _emergency_stopped:
        await _discard_signal(session, arrival_id, "긴급정지 중이라 창을 안 열었다")
        outcome = DispatchOutcome(
            source=DispatchSource.EMERGENCY_BLOCKED,
            command=None,
            destination=None,
            emergency_stop=True,
            command_id=None,
            notified_robot_count=0,
            arrival_id=arrival_id,
            event_id=event_id,
            gate_no=gate_no,
            room_id=room_id,
            decided_at=_utc_now(),
        )
        logger.warning(
            "%s 긴급정지 중이라 셔틀 %s(게이트 %s) 출동을 막았다",
            HOLD_LOG_TAG, arrival_id, gate_no,
        )
        return DispatchStart(
            pending=None,
            notified_robot_count=0,
            command_id=command_id,
            outcome=outcome,
        )

    if hold <= 0:
        delivered, command_id = await notify_shuttle_arrival(
            session, arrival_id=arrival_id, gate_no=gate_no
        )
        logger.info(
            "%s 꺼져 있어 셔틀 %s 명령을 즉시 냈다 (%d대)",
            HOLD_LOG_TAG, arrival_id, delivered,
        )
        # 이 갈래도 확정 결과를 한 장 낸다(계약 §0 "어느 갈래로 닫히든 한 장"). 목적지는
        # `notify_shuttle_arrival`이 인자 없이 쓰는 설정값 그대로다 — 여기서 다시 고르면
        # 실제로 나간 명령과 결과가 어긋난다.
        outcome = DispatchOutcome(
            source=DispatchSource.HOLD_DISABLED,
            command=CommandType.cmd_destination.value,
            destination=get_settings().shuttle_destination.value,
            emergency_stop=None,
            command_id=command_id,
            notified_robot_count=delivered,
            arrival_id=arrival_id,
            event_id=event_id,
            gate_no=gate_no,
            room_id=room_id,
            decided_at=_utc_now(),
        )
        return DispatchStart(
            pending=None,
            notified_robot_count=delivered,
            command_id=command_id,
            outcome=outcome,
        )

    # 같은 신호에 창이 이미 있으면(있을 리 없지만) 앞 창을 닫고 새로 연다.
    stale = _windows.get(arrival_id)
    if stale is not None and _claim(stale) and stale.task is not None:
        stale.task.cancel()

    # ⚠ 여기서는 날씨를 조회하지 않는다(peek). 창이 열리는 시각이 바깥 HTTP에 매달리면
    # 화면 카운트다운이 늦게 뜬다. 행이 아직 없으면 조회를 백그라운드로 띄우고(await 안
    # 한다) 창이 도는 5초 사이에 끝나기를 기대한다 — 발사 자리는 그걸 안 기다린다.
    now = _utc_now()
    day = kst_today(now)
    known_default = await peek_today_default(session, today=day)
    if known_default is None:
        _spawn_default_prefetch(day)
    window = PendingWindow(
        arrival_id=arrival_id,
        event_id=event_id,
        gate_no=gate_no,
        shuttle_no=shuttle_no,
        room_id=room_id,
        command_id=command_id,
        hold_sec=hold,
        opened_at=now,
        deadline=now + dt.timedelta(seconds=hold),
        service_date=day,
        default_destination=known_default.destination if known_default else None,
    )
    _windows[arrival_id] = window
    window.task = asyncio.create_task(_hold_then_auto_fire(window))
    logger.info(
        "%s 셔틀 %s(게이트 %s) 목적지 명령을 %.1f초 붙든다 — 그 사이 선택이 오면 그쪽으로,"
        " 안 오면 기본값(%s)으로 나간다",
        HOLD_LOG_TAG, arrival_id, gate_no, hold,
        window.default_destination or "그때 날씨로 정함",
    )
    return DispatchStart(
        pending=window.as_payload(now), notified_robot_count=0, command_id=command_id
    )


async def _hold_then_auto_fire(window: PendingWindow) -> None:
    """5초를 기다렸다가 아무도 안 골랐으면 기본 목적지로 쏜다.

    ⚠ 예외를 밖으로 안 던진다. 태스크에서 튄 예외는 아무도 안 받아서 조용히 사라진다 —
    로그로 남겨야 시연 중에 "왜 안 나갔지"를 되짚을 수 있다.
    """
    try:
        await asyncio.sleep(window.hold_sec)
    except asyncio.CancelledError:
        return

    if not _claim(window):
        return

    # `_claim`이 이 창을 `_windows`에서 빼 갔다 — 여기서부터는 `reset_dispatch_state`가
    # 창 목록으로 이 태스크를 못 찾는다. 발사가 끝날 때까지만 따로 손잡이를 남긴다.
    me = asyncio.current_task()
    if me is not None:
        _firing_tasks.add(me)
    # 명령을 내보내는 자리에 발을 들였나. 실패 뒷정리가 신호를 버려도 되는지를 가른다.
    fired = False
    try:
        async with get_session() as session:
            if not await _arrival_alive(session, window.arrival_id):
                logger.warning(
                    "%s 셔틀 %s 신호 행이 사라져 자동 발사를 접는다",
                    HOLD_LOG_TAG, window.arrival_id,
                )
                return
            # ⚠ 붙들고 있던 5초 사이에 요원이 긴급정지를 눌렀을 수 있다. 창을 여는 자리에서
            # 한 번 봤다고 끝이 아니라 **내보내기 직전에 다시 본다.**
            if _emergency_stopped:
                await _abort_auto_fire_stopped(session, window)
                return
            # ⚠ 여기서 날씨를 조회하면 안 된다(2026-07-31 검증 P2). 창이 열릴 때 띄운
            # 미리 조회를 아주 짧게만 봐주고, 그 다음엔 아는 값만 본다.
            await _join_prefetch(window.service_date)
            destination = await destination_without_fetch(
                session,
                service_date=window.service_date,
                snapshot=window.default_destination,
            )
            # ⚠ 위 검사와 여기 사이에 await가 둘 있다 — 미리 조회 join(최대 0.5초)과 DB
            # 왕복이다. 이 태스크는 이미 `_claim`으로 `_windows`에서 빠져서 긴급정지가
            # 취소하지도 못하니, 그 사이에 세운 요원의 정지 프레임 뒤로 목적지 명령이
            # 꽂힐 수 있다. 그래서 **await가 하나도 없는 이 자리에서** 한 번 더 본다.
            if _emergency_stopped:
                await _abort_auto_fire_stopped(session, window)
                return
            logger.info(
                "%s 셔틀 %s %.1f초 안에 선택이 없어 기본값 %s로 자동 발사한다",
                HOLD_LOG_TAG, window.arrival_id, window.hold_sec, destination.value,
            )
            fired = True
            await _fire_window(
                session,
                window=window,
                destination=destination,
                source=DispatchSource.AUTO,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 태스크 밖으로 새면 아무도 안 받는다
        logger.exception(
            "%s 자동 발사 중 예외 (arrival_id=%s)", HOLD_LOG_TAG, window.arrival_id
        )
        await _fail_auto_fire(window, discard=not fired)
    finally:
        if me is not None:
            _firing_tasks.discard(me)


async def _fail_auto_fire(window: PendingWindow, *, discard: bool) -> None:
    """자동 발사가 예외로 죽었다 — 신호를 버리고 화면에 "명령이 안 나갔다"를 알린다.

    ⚠ 이걸 안 하면 커밋된 셔틀 신호가 **창도 타이머도 결과 방송도 없이 조용히 사라진다.**
    화면은 카운트다운이 끝난 자리에서 아무 답도 못 받고, 그 신호는 짝짓기 목록에 열린 채로
    남아 나중에 온 로봇 도착이 거기 붙는다.

    ⚠ `discard`는 **명령을 내보내는 자리에 발을 들이기 전에 터졌을 때만** True다. 발사에
    들어간 뒤라면 프레임이 이미 로봇까지 갔을 수 있어서, 그때 신호를 버리면 곧 올 ARRIVED가
    짝을 잃고 게이트가 뒤바뀐다. 그 갈래는 결과 방송만 하고 신호는 살려 둔다(재전송 복구가
    아직 그 신호를 집을 수 있다).

    ⚠ 세션을 새로 연다. 위에서 터진 세션은 트랜잭션이 죽어 있을 수 있어서 그대로 쓰면
    정리까지 같이 실패한다.

    ⚠ `source`는 그대로 `auto`다. 결과 낱말을 새로 만들면 계약(`DispatchSource` 일곱)과 화면
    문구가 같이 늘어야 하는데, 시연을 코앞에 둔 지금 창구 계약을 넓힐 자리가 아니다.
    "명령이 안 나갔다"는 사실은 `command`·`destination`이 null이고
    `notified_robot_count`가 0인 것으로 드러난다.
    """
    if discard:
        try:
            async with get_session() as session:
                await _discard_signal(
                    session, window.arrival_id, "자동 발사가 예외로 죽었다"
                )
        except Exception:  # noqa: BLE001 - 뒷정리 실패가 결과 방송까지 삼키면 안 된다
            logger.exception(
                "%s 셔틀 %s 실패 뒷정리도 실패했다", HOLD_LOG_TAG, window.arrival_id
            )
    try:
        await broadcast_outcome(
            DispatchOutcome(
                source=DispatchSource.AUTO,
                command=None,
                destination=None,
                emergency_stop=None,
                command_id=None,
                notified_robot_count=0,
                arrival_id=window.arrival_id,
                event_id=window.event_id,
                gate_no=window.gate_no,
                room_id=window.room_id,
                decided_at=_utc_now(),
            )
        )
    except Exception:  # noqa: BLE001 - 태스크 밖으로 새면 아무도 안 받는다
        logger.exception(
            "%s 셔틀 %s 실패 결과 방송도 실패했다", HOLD_LOG_TAG, window.arrival_id
        )


async def _abort_auto_fire_stopped(
    session: AsyncSession, window: PendingWindow
) -> None:
    """자동 발사를 긴급정지로 접는다 — 신호를 버리고 화면에 `emergency_blocked`를 알린다.

    자동 발사 경로가 긴급정지를 **두 번** 본다(창 마감 직후, 목적지를 다 구한 뒤). 두 자리가
    같은 뒷정리를 해야 해서 함수 하나로 뒀다 — 손으로 두 벌 적으면 한쪽만 고쳐진다.
    """
    logger.warning(
        "%s 셔틀 %s 자동 발사 직전에 긴급정지가 걸려 있어 명령을 안 낸다",
        HOLD_LOG_TAG, window.arrival_id,
    )
    await _discard_signal(
        session, window.arrival_id, "긴급정지 중이라 자동 발사를 접었다"
    )
    await broadcast_outcome(
        DispatchOutcome(
            source=DispatchSource.EMERGENCY_BLOCKED,
            command=None,
            destination=None,
            emergency_stop=True,
            command_id=None,
            notified_robot_count=0,
            arrival_id=window.arrival_id,
            event_id=window.event_id,
            gate_no=window.gate_no,
            room_id=window.room_id,
            decided_at=_utc_now(),
        )
    )


async def _fire_window(
    session: AsyncSession,
    *,
    window: PendingWindow,
    destination: Destination,
    source: DispatchSource,
) -> DispatchOutcome:
    """창에 매인 셔틀 신호로 목적지 명령을 낸다.

    `notify_shuttle_arrival`을 그대로 탄다 — `notified_robot_id` 기록과 도착 짝짓기용
    상관관계(CommandLink)가 거기 한 자리에 있어서, 여기서 따로 쏘면 ETA 표본이 끊긴다.
    """
    delivered, command_id = await notify_shuttle_arrival(
        session,
        arrival_id=window.arrival_id,
        gate_no=window.gate_no,
        destination=destination,
    )
    if delivered == 0:
        logger.warning(
            "%s 셔틀 %s 목적지 명령이 로봇에 못 갔다 — 붙은 로봇이 없다",
            HOLD_LOG_TAG, window.arrival_id,
        )
    outcome = DispatchOutcome(
        source=source,
        command=CommandType.cmd_destination.value,
        destination=destination.value,
        emergency_stop=None,
        command_id=command_id,
        notified_robot_count=delivered,
        arrival_id=window.arrival_id,
        event_id=window.event_id,
        gate_no=window.gate_no,
        room_id=window.room_id,
        decided_at=_utc_now(),
    )
    await broadcast_outcome(outcome)
    return outcome


async def _discard_signal(session: AsyncSession, arrival_id: int, why: str) -> None:
    """이 셔틀 신호는 로봇이 안 간다 — 짝짓기 목록에서 빼낸다.

    ⚠ 안 빼면 나중에 온 로봇 도착이 이 버려진 신호에 붙어 게이트가 뒤바뀌고, TTL이 지날
    때까지 미짝 목록 맨 앞을 차지한다. `robot_channel`이 이미 쓰는 만료 표식을 그대로
    재사용한다(`ARRIVAL_RESOLVED_TYPES`가 이 종류를 "처리 끝"으로 읽는다).
    """
    await session.execute(
        pg_insert(Alert).values(
            type=ARRIVAL_EXPIRED_ALERT_TYPE,
            severity="info",
            source_type=SHUTTLE_SOURCE_TYPE,
            source_id=arrival_id,
        )
    )
    await session.commit()
    logger.info(
        "%s 셔틀 %s 신호를 버린다(%s) — 재활용하지 않는다",
        HOLD_LOG_TAG, arrival_id, why,
    )


async def _discard_signals(
    session: AsyncSession, windows: list[PendingWindow], why: str
) -> None:
    """창 여럿에 매인 신호를 한꺼번에 버린다. **하나가 터져도 나머지를 계속 뺀다.**

    ⚠ 즉시 명령은 창을 전부 `_claim`으로 집어 온 **뒤에** 이 정리를 돈다. 중간에서 한 번
    터지면 남은 창의 신호는 만료 표식 없이 사라진다 — 창은 이미 닫혔는데 짝짓기 목록엔
    열린 채로 남아, 나중에 온 로봇 도착이 그 유령 신호에 붙어 게이트가 뒤바뀐다.

    실패한 신호는 세션을 되돌리고 넘어간다. 되돌리지 않으면 그 트랜잭션이 죽은 채로 남아
    뒤따르는 신호까지 전부 같이 실패한다.
    """
    for window in windows:
        try:
            await _discard_signal(session, window.arrival_id, why)
        except Exception:  # noqa: BLE001 - 한 건 실패가 나머지를 못 삼키게 한다
            logger.exception(
                "%s 셔틀 %s 신호를 버리지 못했다(%s) — 나머지 창은 계속 정리한다",
                HOLD_LOG_TAG, window.arrival_id, why,
            )
            try:
                await session.rollback()
            except Exception:  # noqa: BLE001 - 되돌리기까지 실패하면 더 할 게 없다
                logger.exception("세션 되돌리기도 실패했다")


# 로봇을 실제로 움직이는 명령. 이 둘만 게이트 스피커에 출발 안내를 낸다.
#
# ⛔ **닫힌 집합이다.** 새 이동 명령이 생기면 여기 더해야 소리가 따라간다. `emergency_stop`은
# 일부러 뺐다 — 세우는 명령이라 "출발합니다"가 나가면 정반대를 말한다.
_DEPARTURE_COMMANDS = frozenset(
    {CommandType.cmd_destination.value, CommandType.return_to_charge.value}
)


def _push_departure_sound(outcome: DispatchOutcome) -> None:
    """로봇이 실제로 움직였으면 게이트 스피커에 출발 안내를 넣는다(2026-08-06 라파이 요청).

    ⭐ **A→C 출동과 C→A 복귀가 같은 낱말을 쓴다.** 라파이 음성 문구를 "로봇이 출발합니다"처럼
    방향 없이 잡기로 합의했다 — 방향을 말하려면 낱말이 둘로 갈리고 라파이 음원도 둘이 된다.

    ⚠ **시점이 상태 전이가 아니라 명령 발사다.** 8/6 새벽 배선에서는 젯슨 `mission_status`를
    보고 넣으려다 뺐다 — "도착에서 벗어난 전이"가 복귀로 빠질 때도 **실패로 멈출 때도** 같이
    나서, 길에 멈춘 로봇 옆에서 출발 안내가 울리기 때문이다. 명령을 낸 순간은 서버가 확실히
    아는 자리라 그 갈래가 없다.

    ⚠ **로봇에 실제로 닿았을 때만 넣는다**(`notified_robot_count > 0`). 붙은 로봇이 하나도
    없으면 아무것도 안 움직이는데 게이트에서 "출발합니다"가 울린다 — 지금처럼 로봇이 서버에
    안 붙은 동안 시연 도구를 누르면 매번 그 거짓말이 난다.
    """
    if outcome.command not in _DEPARTURE_COMMANDS:
        return
    if outcome.notified_robot_count <= 0:
        return
    push_sound(
        KIND_ROBOT_DEPARTURE,
        gate_no=outcome.gate_no,
        event_id=f"depart-{outcome.command_id}" if outcome.command_id else None,
    )


async def broadcast_outcome(outcome: DispatchOutcome) -> None:
    """확정 결과를 대시보드 전체에 민다(설계 §6-7).

    ⚠ 화면 방송과 **게이트 스피커 큐**를 한 자리에서 다룬다. 명령을 내는 갈래가 넷이라
    (셔틀 자동·대기 창 선택·즉시 이동·복귀) 갈래마다 소리를 넣으면 하나를 빠뜨린다 —
    결과가 한 벌로 모이는 이 자리가 유일한 공통 통로다.
    """
    _push_departure_sound(outcome)
    await manager.broadcast(
        make_envelope(DISPATCH_RESULT_MESSAGE, outcome.as_payload())
    )


# ── 창구가 부르는 자리 ─────────────────────────────────────────────────────

async def choose_in_window(
    session: AsyncSession, *, arrival_id: int, choice: WindowChoice
) -> DispatchOutcome:
    """대기 창 버튼 셋. 실내·실외면 그 목적지로 즉시 쏘고, 대기면 아예 안 쏜다.

    ⚠ 고른 순간 바로 나간다. 남은 초를 채우고 나가지 않는다 — 요원이 이미 정했는데 화면만
    숫자를 세는 건 시연에서 "왜 안 가지"로 보인다(설계 §2.2-5의 "고르면 그 목적지로").

    ⚠ 긴급정지 중에는 실내·실외가 409다. `HOLD`는 명령을 안 내니 그대로 받는다 — 세운
    상태에서도 신호를 정리할 길은 열어 둬야 한다. 창을 집기 **전에** 검사하는 것도 일부러다.
    거절된 선택이 창을 소비하면 요원이 해제한 뒤에 그 셔틀을 다시 못 고른다.
    """
    if choice is not WindowChoice.HOLD:
        _require_not_stopped(f"셔틀 {arrival_id} 대기 창 선택({choice.value})")

    window = _take_open_window(arrival_id)
    if window is None:
        raise WindowNotOpen(f"셔틀 {arrival_id}의 대기 창이 열려 있지 않다")

    if choice is WindowChoice.HOLD:
        await _discard_signal(session, window.arrival_id, "요원이 대기를 골랐다")
        outcome = DispatchOutcome(
            source=DispatchSource.HOLD_CANCELLED,
            command=None,
            destination=None,
            emergency_stop=None,
            command_id=None,
            notified_robot_count=0,
            arrival_id=window.arrival_id,
            event_id=window.event_id,
            gate_no=window.gate_no,
            room_id=window.room_id,
            decided_at=_utc_now(),
        )
        await broadcast_outcome(outcome)
        return outcome

    destination = Destination(choice.value)
    logger.info(
        "%s 셔틀 %s 요원이 %s를 골랐다 — 남은 시간을 안 기다리고 바로 낸다",
        HOLD_LOG_TAG, window.arrival_id, destination.value,
    )
    return await _fire_window(
        session, window=window, destination=destination, source=DispatchSource.MANUAL
    )


async def _send_standalone(command: CommandOut) -> int:
    """셔틀 신호와 무관한 단독 명령. 붙어 있는 로봇 수를 돌려준다.

    ⭐ **복귀·즉시이동·긴급정지가 다 이 통로를 지난다**(2026-08-06 · §26-4). 그래서 여기가
    ①ACK 대조 목록에 이름을 올리고 ②DB에 명령을 남기는 자리다.

    ⛔ 이 둘이 없던 동안 그 명령들의 ACK가 전부 "모르는 번호"로 찍혔고, `_log_command_ack`가
    거기서 조기 반환하고 있어서 **젯슨이 거절해도 화면에 안 떴다.**
    """
    # ⚠ 보내기 **전에** 등록한다. ACK가 먼저 돌아오는 경합을 막는다 — 로봇이 같은 랜에 있어
    # 왕복이 밀리초 단위다.
    register_ack_only_command(command.command_id)
    delivered = await robot_manager.send_command(command.as_frame())
    if not delivered:
        logger.warning(
            "즉시 명령 %s가 로봇에 못 갔다 — 붙은 로봇이 없다", command.command_id
        )
    elif command.type.value in _DEPARTURE_COMMANDS:
        # ⭐ 로봇이 움직이기 시작했으니 앞 사이클의 도착은 무효다(프론트 38차 §1-1).
        # ⚠ **닿았을 때만** 비운다 — 0대에 대고 비우면 화면이 멀쩡한 카드를 잃는다.
        # ⚠ 출발 소리와 **같은 집합**을 쓴다. 새 이동 명령이 생기면 한 곳만 고치면 되고,
        # 둘로 갈리면 "소리는 나는데 도착 표시는 안 지워지는" 갈래가 조용히 열린다.
        for name in delivered:
            clear_mission_presentation(name)
    await record_issued(
        channel=CHANNEL_ROBOT,
        command_id=command.command_id,
        command_type=command.type.value if command.type else None,
        # 목적지 명령은 붙은 로봇 전부에게 나가서 대상이 하나로 안 좁혀진다. 몇 대에 갔는지를
        # 대신 남긴다 — 0대면 "명령은 냈는데 아무도 없었다"가 기록으로 남는다.
        payload={"delivered": len(delivered)},
    )
    return len(delivered)


async def dispatch_immediate_move(
    session: AsyncSession, destination: Destination
) -> DispatchOutcome:
    """즉시 이동(실내·실외). **대기 창이 떠 있으면 그 창을 취소하고 이긴다**(사용자 확정).

    창이 있으면 그 셔틀 신호에 매인 명령으로 낸다 — 짝짓기와 ETA가 그대로 살아 있게
    하려는 거다. 창이 없으면 신호와 무관한 단독 명령이다.

    ⚠ 창이 여럿이면 **전부 닫는다.** 명령은 가장 먼저 열린 창(마감이 제일 가까운 창)에
    매고, 나머지 신호는 버린다 — 하나만 닫으면 남은 창이 몇 초 뒤에 다른 목적지 명령을
    내서 로봇이 방금 받은 지시를 뒤집는다(2026-07-31 검증 P1).
    """
    _require_not_stopped(f"즉시 이동({destination.value})")

    windows = _take_all_open_windows()
    if windows:
        primary, rest = windows[0], windows[1:]
        await _discard_signals(
            session,
            rest,
            "즉시 이동이 대기 창을 이겼다(명령은 먼저 열린 창에 맨다)",
        )
        logger.info(
            "%s 셔틀 %s 대기 창을 즉시 이동이 이겼다 — %s로 덮어쓴다(같이 닫은 창 %d개)",
            HOLD_LOG_TAG, primary.arrival_id, destination.value, len(rest),
        )
        return await _fire_window(
            session,
            window=primary,
            destination=destination,
            source=DispatchSource.IMMEDIATE,
        )

    command = CommandOut(
        command_id=_next_command_id("move"),
        type=CommandType.cmd_destination,
        ttl_ms=get_settings().command_ttl_ms,
        cmd_destination=destination,
    )
    delivered = await _send_standalone(command)
    outcome = DispatchOutcome(
        source=DispatchSource.IMMEDIATE,
        command=CommandType.cmd_destination.value,
        destination=destination.value,
        emergency_stop=None,
        command_id=command.command_id,
        notified_robot_count=delivered,
        decided_at=_utc_now(),
    )
    await broadcast_outcome(outcome)
    return outcome


async def _cancel_windows_for_immediate(
    session: AsyncSession, why: str
) -> PendingWindow | None:
    """즉시 명령이 대기 창을 이길 때 **열린 창을 전부** 닫고 신호를 버린다.

    복귀·긴급정지는 로봇을 게이트로 안 보내니, 창에 매여 있던 셔틀 신호는 짝을 못 만난다.
    돌려주는 값은 결과에 실을 대표 창(가장 먼저 열린 것)이다.
    """
    windows = _take_all_open_windows()
    await _discard_signals(session, windows, why)
    if not windows:
        return None
    if len(windows) > 1:
        logger.info(
            "%s 열린 대기 창 %d개를 한꺼번에 닫았다(%s)",
            HOLD_LOG_TAG, len(windows), why,
        )
    return windows[0]


async def dispatch_return_to_charge(session: AsyncSession) -> DispatchOutcome:
    """복귀 버튼. 충전 위치로 보낸다(설계 §2.3).

    ⚠ 복귀도 로봇을 움직이는 명령이라 긴급정지 중에는 409다.
    """
    _require_not_stopped("복귀")
    window = await _cancel_windows_for_immediate(session, "복귀 명령이 대기 창을 이겼다")
    command = CommandOut(
        command_id=_next_command_id("return"),
        type=CommandType.return_to_charge,
        ttl_ms=get_settings().command_ttl_ms,
        return_to_charge=True,
    )
    delivered = await _send_standalone(command)
    outcome = DispatchOutcome(
        source=DispatchSource.IMMEDIATE,
        command=CommandType.return_to_charge.value,
        destination=Destination.CHARGING_STATION.value,
        emergency_stop=None,
        command_id=command.command_id,
        notified_robot_count=delivered,
        arrival_id=window.arrival_id if window else None,
        event_id=window.event_id if window else None,
        gate_no=window.gate_no if window else None,
        room_id=window.room_id if window else None,
        decided_at=_utc_now(),
    )
    await broadcast_outcome(outcome)
    return outcome


async def dispatch_emergency_stop(session: AsyncSession, *, engage: bool) -> DispatchOutcome:
    """긴급정지 세우기(`engage=True`)와 풀기(`engage=False`).

    ⚠ 푸는 자리가 없으면 시연 중 한 번 누르고 끝난다(설계 §2.4). 계약에 해제 명령 종류가
    따로 없어서 같은 `emergency_stop` 명령에 `false`를 실어 푼다.

    세울 때는 **열려 있는 대기 창을 전부** 취소한다 — 세워 놓고 몇 초 뒤에 출동 명령이
    나가면 안 된다. 창을 하나만 닫던 시절에는 창이 둘일 때 남은 쪽이 그대로 쐈다.

    ⚠ 이 창구는 `_require_not_stopped`를 안 지난다. 세우는 것도 푸는 것도 늘 받아야 한다.

    ⭐ **플래그를 언제 뒤집나가 계약이다.** 세울 때는 창 정리·명령 전송을 시작하기 **전에**
    세운다 — 그 await 구간(DB 커밋·WS 송신)에 들어온 이동 요청이 `_require_not_stopped`를
    그냥 지나 정지 프레임 **뒤에** 목적지 명령으로 꽂히기 때문이다. 반대로 풀 때는 해제
    프레임이 나간 **뒤에** 내린다 — 먼저 내리면 그 틈에 들어온 이동 명령이 아직 세워져 있는
    로봇에게 해제보다 먼저 도착한다. 어느 쪽이든 "막는 구간을 넓게" 잡는 방향이다.
    """
    global _emergency_stopped

    window = None
    if engage:
        _emergency_stopped = True
        try:
            window = await _cancel_windows_for_immediate(session, "긴급정지가 대기 창을 이겼다")
        except Exception:  # noqa: BLE001 - 정지 프레임을 내보내는 게 먼저다
            # ⭐ 창 정리가 터져도 **로봇에는 정지가 나가야 한다.** 예전에는 여기서 예외가
            # 그대로 올라가 500이 되면서, 서버는 정지 상태(모든 이동 409)인데 로봇은 정지
            # 프레임을 못 받은 반쪽 상태가 됐다(2026-08-04 축⑤ D3). 세운 플래그는 그대로
            # 둔다 — 못 지운 창이 나중에 발사되는 갈래는 자동 발사가 긴급정지를 다시 보는
            # 검사가 막는다(`_fire_window`). 되돌리는 쪽이 더 위험하다.
            logger.exception(
                "긴급정지가 대기 창을 정리하다 터졌다 — 정지 프레임은 그대로 내보낸다"
            )

    command = CommandOut(
        command_id=_next_command_id("estop"),
        type=CommandType.emergency_stop,
        ttl_ms=get_settings().command_ttl_ms,
        emergency_stop=engage,
    )
    try:
        delivered = await _send_standalone(command)
    finally:
        # ⭐ 해제는 **송신이 터져도 내린다.** 순서(프레임 뒤에 내린다)는 그대로 두되 try/finally로
        # 감싼다 — 예전에는 `_send_standalone`이 터지면 이 줄을 못 밟아 플래그가 True로 굳었고,
        # 그러면 모든 이동 요청이 409인데 푸는 창구는 500만 돌려주는 잠김 상태가 됐다(해제
        # 요청을 다시 보내도 같은 자리에서 또 터진다). 세우는 쪽은 반대다 — 못 세웠으면
        # 세운 채로 두는 게 안전해서 여기서 안 건드린다.
        if not engage:
            _emergency_stopped = False
    logger.info(
        "긴급정지를 %s (%d대에 전달)", "세웠다" if engage else "풀었다", delivered
    )
    outcome = DispatchOutcome(
        # ⛔ `IMMEDIATE`가 아니다 — 셔틀을 어디로 보냈나를 말하는 값인데 비상 정지는 아무
        # 데도 안 보낸 것이다. 창을 닫았다는 사실은 아래 `arrival_id`·`gate_no`가 나른다.
        source=DispatchSource.EMERGENCY_STOP,
        command=CommandType.emergency_stop.value,
        destination=None,
        emergency_stop=engage,
        command_id=command.command_id,
        notified_robot_count=delivered,
        arrival_id=window.arrival_id if window else None,
        event_id=window.event_id if window else None,
        gate_no=window.gate_no if window else None,
        room_id=window.room_id if window else None,
        decided_at=_utc_now(),
    )
    await broadcast_outcome(outcome)
    return outcome


async def resend_dispatch(
    session: AsyncSession, *, arrival_id: int, gate_no: int | None
) -> tuple[int, str]:
    """통지 유실 재전송 복구가 부르는 자리(app/shuttle_call.py).

    **창을 다시 열지 않는다.** 재전송이 왔다는 건 "명령이 안 나갔다"는 뜻이지 "사람이 다시
    골라야 한다"는 뜻이 아니다.

    ⚠ **아직 열려 있는 창은 건드리지 않는다**(2026-07-31 검증 P1). 예전에는 창을 닫고 그
    자리에서 쐈는데, 그러면 기기가 같은 event_id로 재시도한 순간 화면은 카운트다운을 계속
    그리는데 로봇은 이미 떠난 상태가 됐다. 창이 살아 있다는 건 "명령이 아직 나갈 예정"이지
    "유실됐다"가 아니다. 재시도가 마감을 뒤로 밀지도 않으니(창을 새로 안 연다) 명령이 영영
    안 나가는 갈래도 안 생긴다.

    ⚠ 긴급정지 중이면 아무것도 안 보낸다. 기기 쪽 재시도가 세운 로봇을 깨우면 안 된다.
    """
    command_id = f"shuttle-{arrival_id}"

    window = _windows.get(arrival_id)
    if window is not None:
        logger.info(
            "%s 셔틀 %s 재전송이 왔지만 대기 창이 아직 열려 있다 — 창을 그대로 두고"
            " 마감 때 나가게 둔다(남은 %.1f초)",
            HOLD_LOG_TAG, arrival_id, window.remain_sec(),
        )
        return 0, window.command_id

    if _emergency_stopped:
        logger.warning(
            "긴급정지 중이라 셔틀 %s 재전송 명령을 막았다", arrival_id
        )
        await broadcast_outcome(
            DispatchOutcome(
                source=DispatchSource.EMERGENCY_BLOCKED,
                command=None,
                destination=None,
                emergency_stop=True,
                command_id=None,
                notified_robot_count=0,
                arrival_id=arrival_id,
                gate_no=gate_no,
                decided_at=_utc_now(),
            )
        )
        return 0, command_id

    destination = await destination_without_fetch(session)
    delivered, command_id = await notify_shuttle_arrival(
        session, arrival_id=arrival_id, gate_no=gate_no, destination=destination
    )
    outcome = DispatchOutcome(
        source=DispatchSource.RESEND,
        command=CommandType.cmd_destination.value,
        destination=destination.value,
        emergency_stop=None,
        command_id=command_id,
        notified_robot_count=delivered,
        arrival_id=arrival_id,
        gate_no=gate_no,
        decided_at=_utc_now(),
    )
    await broadcast_outcome(outcome)
    return delivered, command_id
