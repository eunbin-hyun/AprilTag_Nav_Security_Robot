"""로봇 outbound WS 상태 수신·알림 중계 (지라 S15P11C207-156).

로봇 상태는 POST가 아니라 이 채널로만 받는다(아키텍처 2026-07-23 M-4). 하는 일이 셋이다.

1. **수신** — 로봇이 올린 RobotState를 검증하고 Robot 행에 반영한다. status_summary는
   절대 상태라 값이 실려 온 필드만 덮는다(안 실린 필드는 "보고 안 함"이지 "없음"이 아니다).
2. **중계** — 보고 한 건에 대시보드 WS 메시지를 **두 장** 밀어낸다. `robot_state`는 로봇이 올린
   원본 프레임 그대로이고(odom·imu·sensor_health·mission_status까지), `robot_status`는 그
   보고로 **로봇 카드가 어떻게 됐나**다(칸이 스냅샷 `robots[]` 한 줄과 같다). 화면은 카드를
   `robot_status`로 갈아끼우고, 카드에 없는 칸(odom·imu·sensor_health)만 `robot_state`에서
   읽는다 — 관제 화면(`frontend/index.html`)과 개발자 텔레메트리 페이지
   (`app/static/telemetry.html`) 둘 다 이 갈래다(-157, S15P11C207-81).
   ⚠ 미션 상태는 2026-08-01부터 카드에도 실린다(프론트 요구 N10) — 화면을 새로 고치면
   개요 탭 타일이 비던 자리다. `robot_state`에도 그대로 흘러서 예전 갈래는 안 깨진다.
3. **알림 변환** — mission_status(ARRIVED·RETURN_COMPLETE·WEATHER_BLOCKED)는 사람이 봐야
   하는 사건이라 Alert 행 + 전용 메시지로 바꾼다.

⚠ mission_status는 **에지 트리거**다. RobotState는 절대 상태라 주기 보고마다 같은 값이 계속
실려 온다 — 값이 바뀐 순간(직전 상태 ≠ 이번 상태)만 사건으로 치지 않으면 도착 한 번에 알림이
N개 뜨고 셔틀 신호도 N개 소진되고 ETA 표본도 N개 들어간다. 직전 상태는 인메모리로 들고
(`reset_robot_channel_state()`로 초기화), ARRIVED에서 다른 상태로 넘어가는 에지는 게이트
출발(`robot_departure`)로 도착과 대칭으로 남긴다. 이 상태를 버리는 길은 TTL 청소 하나뿐이다 —
커넥션이 끊겼다고 버리면 같은 자리에 서 있는 로봇의 재접속 첫 보고가 가짜 도착이 된다. 수명
기준(`seen_at`)은 mission_status가 실린 보고만이 아니라 그 로봇의 모든 상태 보고가 갱신한다 —
주행 중엔 mission_status를 비우고 올리는 로봇이 있어서, 실린 보고만 세면 달려가는 동안 상태가
걷히고 도착해서 올린 ARRIVED가 가짜 도착이 된다.

값이 안 바뀐 ARRIVED 재보고를 새 도착으로 치는 길은 **기본으로 닫아 뒀다**
(`ARRIVAL_EDGE_ON_RECOMMAND=false`). 게이트1에 도착한 로봇이 복귀 보고 없이 게이트2로
재출동하면 mission_status는 ARRIVED 그대로라 값 비교로는 두 번째 도착을 못 잡는데, 그렇다고
"새 명령이 나갔으면 새 도착"으로 치면 더 큰 구멍이 난다 — 명령은 붙어 있는 로봇 전부에게
브로드캐스트라, 게이트에 서 있는 로봇도 새 신호마다 CommandLink를 받아 다음 주기 보고가
가짜 도착이 된다(0.5초 보고 주기가 ETA 표본으로 들어가 구간 기록을 오염시킨다). 그래서
기본 동작은 상태 전이 에지 하나뿐이고, 재출동 판정은 설정으로 켤 때만 돈다. 연속 재출동을
제대로 잡는 정공법은 mission_status enum에 중간 상태를 넣는 것이고 월요일 팀 안건이다.

ARRIVED를 어느 셔틀 신호에 묶느냐는 짐작으로 하지 않는다. 목적지 명령을 보낼 때 "이 로봇에
이 신호(shuttle_arrival_id·gate_no)로 명령했다"는 상관관계를 남기고, 도착이 오면 그 로봇이
받은 명령의 신호와 짝짓는다. 인메모리 상관은 재기동으로 날아가니 그다음은 DB에 "이 로봇
앞으로 통지됨"으로 남은 신호(`notified_robot_id`)를 본다. 그래도 없으면 "**어느 로봇에도
명령 안 된**" 신호만 잡는다 — 딴 로봇이 받아 출동 중인 신호를 늦게 접속한 로봇의 도착이
가로채면 게이트가 뒤바뀐다. 명령했던 게이트를 알면 그 게이트 안에서만 찾는다(CommandLink의
gate_no가 이 자리에 쓰인다). TTL을 넘긴 미짝 신호는 도착이 올 때마다 만료 알림으로 닫는다.

반대 방향으로, 셔틀 도착 인입(POST /api/shuttle-arrivals)이 오면 이 채널로 목적지 명령을
밀어낸다(역할상세 §3의 "신호 수신 → 로봇 통지" 배선). 명령은 상대 변화가 아니라 절대
상태로 싣고 command_id·TTL을 붙인다.

DB 커밋 뒤에 브로드캐스트한다 — 워커가 1개라 send가 트랜잭션을 붙잡으면 전체가 선다(§05).
스키마 변경은 없다. mission_status는 Alert의 type 값으로만 표현해 마이그레이션을 안 건드렸다.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from collections import deque
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.device_sound import (
    KIND_ROBOT_ARRIVAL,
    KIND_ROBOT_RETURN_COMPLETE,
    push_sound,
)
from app.jetson_adapter import (
    adapt_jetson_frame,
    is_jetson_frame,
    reset_jetson_adapter_state,
)
from app.command_log import CHANNEL_ROBOT, record_ack, record_issued
from app.models import Alert, Robot, RobotStatusLog, ShuttleArrival
from app.schemas import (
    CommandOut,
    CommandType,
    Destination,
    MissionStatus,
    RobotPosition,
    RobotStateIn,
    RobotStatusSummary,
    TagTargetIn,
)
from app.ws import make_envelope, make_robot_envelope, manager, robot_manager

logger = logging.getLogger("c207.robot")

# 로봇 도착 알림을 셔틀 신호에 묶는 키. ETA(-158)가 이 짝을 읽어 구간 소요시간을 뽑는다.
ROBOT_ARRIVAL_ALERT_TYPE = "robot_arrival"
ROBOT_DEPARTURE_ALERT_TYPE = "robot_departure"
# 복귀 완료. 상수로 뺀 이유는 아래 소리 표가 같은 문자열을 한 번 더 쓰기 때문이다 — 두 자리에
# 손으로 적어 두면 한쪽만 고쳤을 때 알림은 나가는데 스피커만 조용해진다.
ROBOT_RETURN_COMPLETE_ALERT_TYPE = "robot_return_complete"
# ⭐ 주행 실패(2026-08-05 젯슨 §6-5). ⚠ 새 종류라 **화면 표에 넣어 달라고 알려야 한다** —
#   프론트 23차가 "표에 없는 종류는 소리가 안 난다"고 못 박았다.
ROBOT_MISSION_FAILED_ALERT_TYPE = "robot_mission_failed"
SHUTTLE_SOURCE_TYPE = "shuttle_arrival"
# 짝을 못 찾고 TTL까지 넘긴 셔틀 신호를 닫는 표식. Alert 행으로 남겨 마이그레이션을 안 건드린다.
ARRIVAL_EXPIRED_ALERT_TYPE = "shuttle_arrival_expired"
# 이 종류들이 붙은 셔틀 신호는 "처리 끝"이라 미짝 목록에서 뺀다.
ARRIVAL_RESOLVED_TYPES = (ROBOT_ARRIVAL_ALERT_TYPE, ARRIVAL_EXPIRED_ALERT_TYPE)

# mission_status → (알림 메시지·Alert type, severity, 화면 문구).
# 문구는 대시보드에 그대로 뜨는 제품 문구라 합니다체로 적는다.
# ⛔ **알림을 안 만드는 상태.** 나머지는 전부 "끝났다" 계열이라 사건인데, 이건 진행 중이다.
#
# ⚠ 아래 `MISSION_NOTICES`는 **없는 키를 조회하면 그대로 터진다**(`MISSION_NOTICES[x]`).
# 그래서 `MissionStatus`에 값을 늘리면 **둘 중 하나에는 반드시 적어야 한다** — 여기 아니면
# 저기다. 한쪽만 늘리면 그 상태가 처음 올라오는 순간 프레임 처리가 통째로 죽는다.
_NO_NOTICE_STATUSES = frozenset({MissionStatus.EN_ROUTE.value})

MISSION_NOTICES: dict[str, tuple[str, str, str]] = {
    MissionStatus.ARRIVED.value: (
        ROBOT_ARRIVAL_ALERT_TYPE,
        "info",
        "로봇이 게이트 자리에 도착했습니다.",
    ),
    MissionStatus.RETURN_COMPLETE.value: (
        ROBOT_RETURN_COMPLETE_ALERT_TYPE,
        "info",
        "로봇이 게이트를 떠나 복귀를 마쳤습니다.",
    ),
    MissionStatus.WEATHER_BLOCKED.value: (
        "robot_weather_blocked",
        "warning",
        "날씨 때문에 로봇이 출동하지 못했습니다.",
    ),
    # ⭐ 주행 실패. ⚠ 급이 `warning`이다 — 도착·복귀는 `info`지만 이건 **사람이 가서
    # 봐야 하는** 상태다. 로봇이 길 위에 멈춰 있다.
    MissionStatus.FAILED.value: (
        ROBOT_MISSION_FAILED_ALERT_TYPE,
        "warning",
        "로봇이 주행을 마치지 못하고 멈췄습니다.",
    ),
}

# ⭐ 알림 종류 → 라파이 스피커에 낼 소리(2026-08-06 사용자 배선). **둘뿐이다** — 태깅 위치
# 도착과 충전 복귀 완료다. 판단은 젯슨이 하고 서버는 넘기기만 한다.
#
# ⚠ **출발(`ROBOT_DEPARTURE_ALERT_TYPE`)은 일부러 뺐다.** 사용자 배선표에 없고, 출발 소리는
# "젯슨이 실제로 움직였다고 알릴 때"라야 하는데 이 알림은 ARRIVED에서 벗어나는 전이라
# 복귀·실패로 빠질 때도 같이 난다. 실패로 멈춘 로봇 옆에서 "출발합니다"가 울린다.
#
# ⚠ 주행 실패(`ROBOT_MISSION_FAILED_ALERT_TYPE`)도 뺐다. 게이트에 선 사람에게 알릴 내용이
# 아니라 관제 요원이 봐야 하는 상태다 — 그쪽은 화면 알림(`warning`)이 맡는다.
MISSION_SOUND_KINDS: dict[str, str] = {
    ROBOT_ARRIVAL_ALERT_TYPE: KIND_ROBOT_ARRIVAL,
    ROBOT_RETURN_COMPLETE_ALERT_TYPE: KIND_ROBOT_RETURN_COMPLETE,
}

# 도착 상태에서 벗어나는 에지 = 게이트 출발. mission_status 값이 아니라 전이로만 잡힌다
# (robot_state.schema.json의 mission_status enum은 손대지 않는다).
DEPARTURE_NOTICE: tuple[str, str, str] = (
    ROBOT_DEPARTURE_ALERT_TYPE,
    "info",
    "로봇이 게이트 자리에서 출발했습니다.",
)


@dataclass
class MissionNotice:
    alert_type: str
    alert_id: int
    severity: str
    message: str
    # 이 알림이 "무슨 상태에 대한" 알림인가. 출발은 mission_status enum에 없는 사건이라 None이고,
    # 대신 from_status·to_status로 전이를 그대로 싣는다(출발 메시지가 복귀완료로 읽히는 걸 막는다).
    mission_status: str | None
    from_status: str | None
    to_status: str | None
    arrival_id: int | None   # 셔틀 신호에 묶였을 때만 채워진다


@dataclass
class RobotStateResult:
    robot_pk: int
    notices: list[MissionNotice] = field(default_factory=list)

    @property
    def notice(self) -> MissionNotice | None:
        """이번 프레임에서 마지막으로 난 알림. 없으면 None."""
        return self.notices[-1] if self.notices else None


# ── 인메모리 상태 (에지 추적 · 명령 상관관계) ──────────────────────────────
# 워커 1개 전제라 인메모리로 든다(크레딧 쿨다운과 같은 방식). 서버가 재기동되면 비는데,
# 짝짓기는 DB에 남는 `ShuttleArrival.notified_robot_id`가 이어받는다(_match_arrival_for_robot
# 2순위). 에지 추적은 이어받을 자리가 없어서, 재기동 뒤 첫 보고가 다시 전이로 잡힌다.

@dataclass
class MissionState:
    """로봇별 직전 mission_status와, 그 도착이 묶인 셔틀 신호."""
    status: str | None = None
    arrival_id: int | None = None
    # 이 상태를 기록한 시각. "직전 도착 뒤에 새 명령이 나갔나"를 가르는 기준이다.
    at: dt.datetime | None = None
    # 이 로봇의 마지막 보고 시각. 상태 수명은 이 값 하나로만 정해진다 — `at`으로 지우거나
    # 커넥션 종료로 버리면, 한 자리에 오래 서 있던 로봇의 다음 보고가 가짜 도착이 된다.
    # ⚠ mission_status가 실린 보고만이 아니라 **그 로봇의 모든 상태 보고**가 이 값을 갱신한다
    # (`touch_mission_state`). 주행 중엔 mission_status 없이 올리는 로봇이 있어서, 실린 보고만
    # 세면 살아서 달리는 로봇의 상태가 TTL에 걷히고 다음 ARRIVED가 가짜 도착이 된다.
    seen_at: dt.datetime | None = None


@dataclass
class CommandLink:
    """어느 로봇에 어느 셔틀 신호로 목적지 명령을 보냈나(도착 짝짓기의 근거)."""
    command_id: str
    arrival_id: int
    gate_no: int | None
    issued_at: dt.datetime


@dataclass
class RobotPresentation:
    """화면에만 쓰는 최신값(위치·LED). Robot 테이블에 컬럼이 없는 칸이다.

    ⚠ **서버가 재기동되면 사라진다 — 의도된 거동이다.** DB 컬럼을 안 만든 건 마이그레이션을
    안 건드리기로 한 결정이고(0002·0003 초안이 물려 있어 alembic이 두 갈래로 갈릴 위험),
    위치·LED는 다음 상태 보고가 곧바로 다시 채우는 값이라 잃어도 복구가 저절로 된다.
    없는 동안에는 null로 나가서 화면 계약이 안 깨진다.

    로봇 수만큼만 자라고 키는 Robot 행 이름과 같은 robot_id다(행이 이미 그만큼 생긴다).
    """
    position: dict[str, Any] | None = None
    led_status: str | None = None
    # 마지막으로 보고된 mission_status 문자열(프론트 요구 N10). 화면에 그 값을 보여주려고만 든다.
    #
    # ⚠ `MissionState.status`와 값이 같아 보여도 **같은 자리가 아니다.** 저쪽은 알림 에지를
    # 가르는 상태라 TTL(`prune_mission_states`)로 걷히고, 걷히는 게 안전장치다 — 한참 사라졌다
    # 돌아온 로봇의 ARRIVED를 진짜 도착으로 다시 세야 하니까. 화면 값을 저기서 읽으면 ①표시가
    # TTL이라는 알림용 손잡이에 딸려 흔들리고 ②조회(GET)가 청소라는 상태 변경을 밟아야 값이
    # 맞아떨어진다. 그래서 위치·LED와 같은 표시 전용 자리에 따로 든다. 둘이 갈리는 순간은
    # 하나뿐이다 — TTL을 넘겨 조용한 로봇. 그때 에지 상태는 비고 표시는 마지막 보고값을 든다.
    mission_status: str | None = None


_mission_states: dict[str, MissionState] = {}
_command_links: dict[str, deque[CommandLink]] = {}
_presentations: dict[str, RobotPresentation] = {}

# ⭐ ACK 대조 **전용** 목록 (2026-08-06 · §26-4).
#
# ⛔ 왜 `_command_links`를 안 쓰나 — 그 자료형은 **도착 짝짓기의 근거**이고 `arrival_id`가
# 필수 칸이다. 셔틀 신호가 없는 단독 명령(복귀·즉시이동·긴급정지)은 그 칸을 비워야 하는데,
# `_match_arrival_for_robot`이 그 목록을 FIFO로 읽어 셔틀을 고른다 — **빈 항목을 섞으면
# 짝짓기가 엉킨다.** 그래서 명령 이름만 짧게 기억하는 목록을 따로 둔다.
#
# ⚠ 값은 등록 시각(단조)이다. TTL을 넘긴 항목은 대조할 때 걷는다.
_ack_only_commands: dict[str, float] = {}
# ⚠ 같은 명령의 **벽시계** 시각. 위 단조 시계와 짝이다.
#
# 둘을 나눠 드는 까닭 — TTL 판정은 시각이 뒤로 튀어도 안 뒤집히게 단조 시계로 해야 하고,
# "직전 상태 뒤에 명령이 나갔나"(`_has_command_link_since`)는 DB 시각과 견줘야 해서 벽시계가
# 필요하다. 한 시계로 둘 다 하면 한쪽이 반드시 틀린다.
_ack_only_issued_at: dict[str, dt.datetime] = {}
# 상한. 넘으면 오래된 것부터 버린다 — 어차피 TTL이 지난 것들이라 대조에서 어떻게든 빠진다.
_ACK_ONLY_MAX = 512


def _mono_now() -> float:
    """단조 시계. 벽시계를 안 쓰는 이유는 시각이 뒤로 튀어도 TTL이 안 뒤집히게 하려는 거다.

    함수로 빼 두는 건 시험이 갈아끼우는 자리라서다(`device_sound._mono`와 같은 방식).
    """
    return time.monotonic()


def reset_robot_channel_state() -> None:
    """인메모리 상태를 비운다(시험 위생 — 케이스 사이에 에지·상관관계가 새면 안 된다).

    위치·LED도 같이 비운다. 이 호출이 곧 "서버 재기동"과 같은 자리다.
    젯슨 어댑터의 로봇별 누적 상태도 같은 성격이라 여기서 같이 비운다 — 훅이 둘로 갈라지면
    한쪽만 부르는 시험에서 앞 케이스 값이 샌다.
    """
    _mission_states.clear()
    _command_links.clear()
    # ⚠ 단독 명령 대조 목록도 같이 비운다. 안 비우면 앞 케이스가 낸 명령 이름이 남아
    # 다음 케이스의 "모르는 번호" 검사가 조용히 통과한다.
    _ack_only_commands.clear()
    _ack_only_issued_at.clear()
    _presentations.clear()
    reset_jetson_adapter_state()


def _position_json(position: RobotPosition) -> dict[str, Any]:
    """position을 정본 계약대로 편다. 값이 없는 칸은 null로 채우지 않고 **뺀다**.

    정본 `schemas/robot_state.schema.json`은 position의 세 칸을 `{"type": "number"}`로 못박아
    null을 안 받는데, pydantic `RobotPosition`은 `float | None`이라 조용히 통과한다
    (schemas.py는 수정 금지라 pydantic 쪽을 못 좁힌다). 게다가 `model_dump()`는 안 실린 칸을
    **null로 되채워서**, 어댑터가 theta를 빼도 중계 단계에서 `theta: null`이 되살아난다 —
    어댑터 출력은 JSON Schema 통과인데 브로드캐스트는 실패였다(실측 결함 2).
    """
    return position.model_dump(mode="json", exclude_none=True)


def _state_json(state: RobotStateIn) -> dict[str, Any]:
    """중계용 robot_state payload. position의 빈 칸만 정본 계약대로 걷어낸다."""
    payload = state.model_dump(mode="json")
    if state.status_summary is not None and state.status_summary.position is not None:
        payload["status_summary"]["position"] = (
            _position_json(state.status_summary.position) or None
        )
    return payload


def record_presentation(
    robot_id: str,
    summary: RobotStatusSummary | None,
    mission_status: MissionStatus | None = None,
) -> None:
    """보고에 실려 온 위치·LED·임무 상태만 최신값으로 덮는다.

    status_summary는 절대 상태라 안 실린 필드는 "보고 안 함"이지 "없음"이 아니다 — Robot 행
    갱신(`_upsert_robot`)과 같은 규칙이라야 화면 카드에서 한쪽만 사라지는 일이 없다.

    `mission_status`는 status_summary 밖(RobotState 최상위)이라 따로 받는데, 덮는 규칙은
    똑같다. 주행 중엔 이 칸을 비우고 올리는 로봇이 있어서, null을 "없음"으로 받아 지우면
    게이트로 달려가는 동안 화면 타일이 깜빡인다. 개발자 화면도 같은 규칙으로 그린다
    (`static/telemetry.html`의 `if (p.mission_status)`).
    """
    position = summary.position if summary is not None else None
    led_status = summary.led_status if summary is not None else None
    if position is None and led_status is None and mission_status is None:
        return
    current = _presentations.setdefault(robot_id, RobotPresentation())
    if position is not None:
        current.position = _position_json(position)
    if led_status is not None:
        current.led_status = led_status
    if mission_status is not None:
        current.mission_status = mission_status.value


def clear_mission_presentation(robot_id: str | None = None) -> None:
    """이동 명령을 **실제로 보낸 순간** 임무 상태를 비운다(2026-08-06 · 프론트 38차 §1-1).

    ⛔ **왜 필요한가** — 위 `record_presentation`이 null을 일부러 안 지운다(주행 중 이 칸을
    비우고 올리는 로봇이 있어서, 지우면 화면 타일이 깜빡인다). 그 규칙 자체는 옳은데 부작용이
    하나 있었다 — 도착해서 찍힌 `ARRIVED`가 다음 도착까지 안 지워져서, 로봇이 다시 출발해도
    카드가 계속 "도착"을 실었다.

    ⛔ 표시로만 끝나지 않았다. 화면이 그 값으로 태깅·ToF 판정을 켜서 **복귀 중에도 태깅이
    켜져 있고 주행 중에도 ToF가 켜져 있었다**(사용자가 실기기 시연에서 짚은 자리다).

    ⭐ **서버는 이 순간을 정확히 안다.** 이동 명령을 로봇에 실제로 보냈다면 앞 사이클의 도착은
    무효다. 그래서 젯슨이 `EN_ROUTE`를 보내 주기를 기다리지 않고 여기서 끊는다
    (`mission_status_en_route_enabled`는 켜도 젯슨이 주행 중 그 칸을 아예 안 실어 소용이 없다).

    ⚠ **명령이 아무에게도 안 갔으면 부르지 마라.** 붙은 로봇이 0대인데 비우면 화면이 멀쩡한
    카드를 잃는다 — 부르는 쪽이 그 판정을 하고 온다.

    ⚠ 위치·LED는 안 건드린다. 그 둘은 움직이는 동안에도 유효한 값이다.
    ⚠ `robot_id`를 안 주면 전부 비운다 — 목적지 명령은 붙은 로봇 전부에게 나가서 대상이
    하나로 안 좁혀진다.
    """
    targets = [robot_id] if robot_id is not None else list(_presentations)
    for name in targets:
        shown = _presentations.get(name)
        if shown is not None:
            shown.mission_status = None


def get_presentation(robot_id: str) -> RobotPresentation:
    """그 로봇의 위치·LED·임무 상태 최신값. 보고가 없었으면 세 칸 다 None인 빈 값."""
    return _presentations.get(robot_id, RobotPresentation())


def register_command_link(robot_id: str, link: CommandLink) -> None:
    _command_links.setdefault(robot_id, deque()).append(link)


def register_ack_only_command(command_id: str) -> None:
    """셔틀 신호와 무관한 단독 명령의 이름을 ACK 대조용으로만 기억한다(§26-4).

    ⛔ **여기 등록한 이름은 도착 짝짓기에 안 쓴다.** 짝짓기는 `_command_links`가 맡고 그쪽은
    `arrival_id`가 필수다 — 단독 명령에는 그 값이 없어서 섞으면 셔틀 고르기가 엉킨다.

    ⚠ 이게 없던 동안 복귀·즉시이동·긴급정지 ACK가 **전부 "모르는 번호"로 찍혔다.** 로그가
    시끄러운 것으로 끝나지 않았다 — `_log_command_ack`가 거기서 조기 반환하고 있어서
    **젯슨이 명령을 거절해도 그 사실이 화면까지 안 갔다.**
    """
    now = _mono_now()
    if len(_ack_only_commands) >= _ACK_ONLY_MAX:
        # 오래된 것부터 버린다. TTL이 지난 것들이라 대조에서 어차피 빠질 이름이다.
        for stale in sorted(_ack_only_commands, key=_ack_only_commands.get)[
            : len(_ack_only_commands) - _ACK_ONLY_MAX + 1
        ]:
            _ack_only_commands.pop(stale, None)
            # ⚠ 짝을 같이 버린다. 한쪽만 지우면 벽시계 목록이 상한 없이 자란다.
            _ack_only_issued_at.pop(stale, None)
    _ack_only_commands[command_id] = now
    _ack_only_issued_at[command_id] = dt.datetime.now(dt.timezone.utc)


def _ack_only_known(command_id: str) -> bool:
    """단독 명령 목록에 있고 아직 TTL 안인가. 지난 항목은 이 자리에서 걷는다."""
    issued = _ack_only_commands.get(command_id)
    if issued is None:
        return False
    ttl_sec = get_settings().arrival_match_ttl_sec
    if _mono_now() - issued > ttl_sec:
        _ack_only_commands.pop(command_id, None)
        return False
    return True


def on_robot_disconnect(robot_id: str | None) -> int:
    """커넥션이 끊겼을 때의 정리. 에지 추적 상태는 **버리지 않는다**.

    로봇은 Wi-Fi가 한 번 끊겼다 붙어도 같은 자리에 그대로 서 있다. 상태를 버리면 다시 붙어
    올린 첫 ARRIVED가 도착 에지로 잡혀 ①가짜 도착 알림 ②미짝 신호 오귀속 ③가짜 ETA 표본이
    한꺼번에 난다 — 재접속 한 번이 주행 기록을 만드는 셈이다.

    상태 수명은 TTL 청소가 맡는다(`prune_mission_states`, 기준은 마지막 보고 시각). 다시 안
    붙는 로봇 몫도 TTL을 넘기면 걷힌다. 여기서도 한 번 훑어, 보고가 아예 안 오는 동안에도
    청소가 도는 자리를 하나 더 둔다. 걷어낸 수를 돌려준다.
    """
    pruned = prune_mission_states()
    logger.info("로봇 %s 커넥션 종료 — 에지 상태 유지, TTL 청소 %d건", robot_id, pruned)
    return pruned


def touch_mission_state(robot_id: str, now: dt.datetime | None = None) -> None:
    """상태 보고 한 건이 왔다고 표시한다(에지 추적 상태의 수명 갱신).

    보고에 mission_status가 실렸는지는 안 본다. 주행 중엔 mission_status를 비우고 올리는 로봇이
    있어서, 실린 보고만 수명으로 세면 게이트로 달려가는 동안 상태가 TTL에 걷힌다 — 그러면
    도착해서 올린 ARRIVED가 "직전 상태 없음"에서 오는 전이로 잡혀 가짜 도착이 된다.

    청소를 먼저 밟고 나서 갱신한다. 순서를 뒤집으면 TTL을 넘겨 이미 못 믿을 상태가 보고 한 건에
    되살아나, 한참 사라졌다 돌아온 로봇의 진짜 도착이 조용히 묻힌다.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    prune_mission_states(now)
    state = _mission_states.get(robot_id)
    if state is None:
        state = MissionState()
        _mission_states[robot_id] = state
    state.seen_at = now


def prune_mission_states(now: dt.datetime | None = None) -> int:
    """마지막 보고가 TTL을 넘긴 로봇의 에지 추적 상태를 걷어내고 걷어낸 수를 돌려준다.

    상태를 버리는 길은 이 청소 하나뿐이다(커넥션 종료로는 안 버린다 — `on_robot_disconnect`).
    기준 시각은 `seen_at`이고, 그 값은 mission_status가 실렸는지와 무관하게 모든 상태 보고가
    갱신한다(`touch_mission_state`). 상태가 자라는 자리는 보고 처리 하나뿐이라 거기서 같이
    훑고, 커넥션 종료 때도 한 번 훑어 보고가 아예 끊긴 로봇 몫이 남지 않게 한다.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    ttl_sec = get_settings().mission_state_ttl_sec
    stale = [
        robot_id
        for robot_id, state in _mission_states.items()
        if state.seen_at is None or (now - state.seen_at).total_seconds() > ttl_sec
    ]
    for robot_id in stale:
        del _mission_states[robot_id]
    return len(stale)


def _link_is_live(link: CommandLink, now: dt.datetime, ttl_sec: float) -> bool:
    return (now - link.issued_at).total_seconds() <= ttl_sec


def prune_command_links(now: dt.datetime | None = None) -> int:
    """TTL을 넘긴 명령 상관을 전 로봇에서 걷어내고 걷어낸 수를 돌려준다.

    청소를 도착 경로에만 두면, 도착을 한 번도 안 올리는 로봇(전원 차단·통신 이탈·명령 무시)의
    큐가 무한히 쌓인다. 큐가 자라는 자리는 명령 발신 하나뿐이라 거기서 같이 훑는다.

    ⚠ **큐를 새 객체로 갈아끼우지 않고 제자리에서 고친다.** 짝짓기(`_match_arrival_for_robot`)는
    `_command_links.get(robot_id)`로 deque를 **한 번 집어** 놓고 `await`를 낀 채 popleft로
    돈다. 그 사이에 여기서 dict 값을 새 deque로 바꾸면 짝짓기는 아무도 안 보는 고아 큐를
    계속 파먹고, 그 뒤에 등록된 명령 상관은 새 큐에 쌓여 영영 안 읽힌다 — 로봇 도착이 엉뚱한
    셔틀 신호에 붙는 갈래다. 버릴 게 없으면 아예 손을 안 대는 것도 같은 이유다.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    ttl_sec = get_settings().arrival_match_ttl_sec
    dropped = 0
    for robot_id, links in list(_command_links.items()):
        kept = [link for link in links if _link_is_live(link, now, ttl_sec)]
        if len(kept) == len(links):
            continue
        dropped += len(links) - len(kept)
        # 같은 객체를 비우고 다시 채운다. 짝짓기가 들고 있는 참조가 그대로 살아 있다.
        links.clear()
        links.extend(kept)
        if not links:
            del _command_links[robot_id]
    return dropped


def _has_command_link_since(
    robot_id: str, since: dt.datetime | None, now: dt.datetime
) -> bool:
    """그 로봇 앞으로 `since` 뒤에 나간 유효한 명령 상관이 있나.

    같은 ARRIVED 재보고를 "새 도착"으로 볼지 가르는 판정이다. 기준을 직전 도착 시각으로
    잡아야, 도착 전에 이미 큐에 쌓여 있던 명령이 주기 보고를 가짜 도착으로 만들지 않는다.
    """
    ttl_sec = get_settings().arrival_match_ttl_sec
    links = _command_links.get(robot_id) or ()
    if any(
        _link_is_live(link, now, ttl_sec) and (since is None or link.issued_at > since)
        for link in links
    ):
        return True

    # ⭐ **단독 명령도 본다**(2026-08-06). 위 목록은 셔틀 출동만 담아서, 복귀·즉시이동 뒤에
    # 난 일은 "직전 뒤에 명령이 나갔나"를 물어도 늘 거짓이었다. 연속 실패 알림이 그 판정에
    # 걸려 있어서, 복귀 도중 두 번 멈추면 두 번째가 조용해진다.
    #
    # ⚠ 목록이 둘인 까닭은 `CommandLink`가 도착 짝짓기 근거라 `arrival_id`가 필수여서다
    # (`register_ack_only_command` 설명). 여기서만 둘을 합쳐 본다.
    return any(
        (now - issued).total_seconds() <= ttl_sec and (since is None or issued > since)
        for issued in _ack_only_issued_at.values()
    )


def snapshot_edge_state(robot_id: str) -> tuple[MissionState | None, list[CommandLink]]:
    """그 로봇의 에지 상태와 명령 상관 큐를 **베낀다**(커밋 실패 되돌리기용).

    `MissionState`는 제자리에서 고쳐지고(`previous.seen_at = now`) 큐는 `popleft`로 소비되니
    참조만 들면 되돌릴 수가 없다. 그래서 둘 다 값 복사로 뜬다.
    """
    current = _mission_states.get(robot_id)
    links = _command_links.get(robot_id)
    return (
        dataclass_replace(current) if current is not None else None,
        list(links) if links is not None else [],
    )


def restore_edge_state(
    robot_id: str, snapshot: tuple[MissionState | None, list[CommandLink]]
) -> None:
    """`snapshot_edge_state`로 떠 둔 값으로 되돌린다.

    ⚠ 큐는 **같은 객체를 비우고 다시 채운다.** 새 deque로 갈아끼우면 짝짓기가 이미 집어 든
    참조가 고아 큐를 파먹는다(`prune_command_links` 주석과 같은 함정이다).
    """
    previous, links = snapshot
    if previous is None:
        _mission_states.pop(robot_id, None)
    else:
        _mission_states[robot_id] = previous
    if not links:
        _command_links.pop(robot_id, None)
        return
    queue = _command_links.setdefault(robot_id, deque())
    queue.clear()
    queue.extend(links)


def command_link_known(command_id: str, robot_id: str | None = None) -> bool:
    """서버가 실제로 낸 명령 이름인가(ACK 대조용).

    `robot_id`를 알면 그 로봇 큐를 먼저 보고, 없으면 전체를 훑는다 — 목적지 명령은 붙어 있는
    로봇 전부에게 나가서 상관이 여러 로봇 밑에 쌓이기 때문이다.

    ⚠ TTL로 걷힌 뒤에 온 늦은 ACK는 여기서 False가 된다. 그래서 부르는 쪽은 이 값으로
    연결을 끊거나 자료를 바꾸지 않는다 — 로그 갈래를 가르는 데만 쓴다.

    ⭐ **목록 둘을 본다**(2026-08-06 · §26-4). 셔틀 출동은 `_command_links`에 남고, 셔틀 신호가
    없는 단독 명령(복귀·즉시이동·긴급정지)은 `_ack_only_commands`에 남는다. 앞엣것만 보던
    동안 단독 명령 ACK가 전부 "모르는 번호"였다.
    """
    if _ack_only_known(command_id):
        return True
    if robot_id is not None:
        links = _command_links.get(robot_id)
        if links and any(link.command_id == command_id for link in links):
            return True
    return any(
        link.command_id == command_id
        for links in _command_links.values()
        for link in links
    )


def _commanded_arrival_ids(now: dt.datetime) -> set[int]:
    """아직 유효한 명령 상관이 가리키는 신호 id 모음. 폴백에서 뺄 대상이다."""
    ttl_sec = get_settings().arrival_match_ttl_sec
    return {
        link.arrival_id
        for links in _command_links.values()
        for link in links
        if _link_is_live(link, now, ttl_sec)
    }


# ── 셔틀 신호 ↔ 로봇 도착 짝짓기 ──────────────────────────────────────────

def _unmatched_arrivals(gate_no: int | None = None):
    """도착도 만료도 안 찍힌 셔틀 신호 질의(정렬은 호출자가 붙인다)."""
    resolved = select(Alert.source_id).where(
        Alert.source_type == SHUTTLE_SOURCE_TYPE,
        Alert.type.in_(ARRIVAL_RESOLVED_TYPES),
        Alert.source_id.is_not(None),  # NOT IN에 NULL이 섞이면 결과가 통째로 비어버린다
    )
    stmt = select(ShuttleArrival).where(ShuttleArrival.id.not_in(resolved))
    if gate_no is not None:
        stmt = stmt.where(ShuttleArrival.gate_no == gate_no)
    return stmt


async def latest_unmatched_arrival(
    session: AsyncSession, gate_no: int | None = None
) -> ShuttleArrival | None:
    """미짝 셔틀 신호 중 가장 최근 것. ETA(-158)가 예상 도착 시각의 기준 시각으로 쓴다."""
    stmt = _unmatched_arrivals(gate_no).order_by(ShuttleArrival.signal_ts.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def oldest_unmatched_arrival(
    session: AsyncSession, gate_no: int | None = None, *, uncommanded_only: bool = False
) -> ShuttleArrival | None:
    """미짝 셔틀 신호 중 가장 오래된 것.

    상관관계로 짝을 못 찾았을 때 여기서 채운다 — 최신부터 집으면 옛 신호가 영구히 미짝으로
    남아 ETA 표본에서 통째로 빠진다.

    `uncommanded_only`면 어느 로봇에도 명령 안 된 신호만 본다. 명령이 나간 신호는 그 로봇의
    도착이 올 자리라, 딴 로봇의 폴백이 가져가면 게이트가 뒤바뀐다. "명령됐다"는 근거는 둘이다
    — DB의 `notified_robot_id`(서버 재기동 뒤에도 남는다)와 아직 유효한 인메모리 상관.

    ⚠ 자기 앞으로 통지된 신호까지 여기서 빼면 재기동 뒤 짝짓기가 통째로 끊긴다. 그래서
    "이 로봇 앞으로 표시된 신호"는 `oldest_arrival_notified_to`가 먼저 집고, 여기는 그다음
    차례다(`_match_arrival_for_robot` 참고).
    """
    stmt = _unmatched_arrivals(gate_no)
    if uncommanded_only:
        stmt = stmt.where(ShuttleArrival.notified_robot_id.is_(None))
        commanded = _commanded_arrival_ids(dt.datetime.now(dt.timezone.utc))
        if commanded:
            stmt = stmt.where(ShuttleArrival.id.not_in(commanded))
    stmt = stmt.order_by(ShuttleArrival.signal_ts.asc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


async def oldest_arrival_notified_to(
    session: AsyncSession, robot_pk: int, gate_no: int | None = None
) -> ShuttleArrival | None:
    """DB에 이 로봇 앞으로 통지됐다고 표시된 미짝 신호 중 가장 오래된 것.

    인메모리 상관이 짝짓기의 1순위인데 서버가 재기동되면 그게 통째로 사라진다. 그때 이
    표시가 남아 있어야 로봇이 자기 신호에 다시 붙는다 — 안 그러면 명령받은 신호는
    "명령됨"이라 폴백에서도 빠져 영영 미짝으로 남고, ETA 구간 기록도 같이 빈다.
    딴 로봇 앞으로 표시된 신호는 robot_pk가 안 맞아 여기서도 안 잡힌다.
    """
    stmt = (
        _unmatched_arrivals(gate_no)
        .where(ShuttleArrival.notified_robot_id == robot_pk)
        .order_by(ShuttleArrival.signal_ts.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def _is_unmatched(session: AsyncSession, arrival_id: int) -> bool:
    stmt = _unmatched_arrivals().where(ShuttleArrival.id == arrival_id).limit(1)
    return (await session.execute(stmt)).scalars().first() is not None


async def expire_stale_arrivals(session: AsyncSession) -> list[int]:
    """TTL을 넘긴 미짝 셔틀 신호를 만료 알림으로 닫고 그 id 목록을 돌려준다.

    안 닫으면 며칠 전 신호가 미짝 목록 맨 앞에 남아, 오늘 온 도착이 옛 신호에 붙는다.
    """
    settings = get_settings()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=settings.arrival_match_ttl_sec
    )
    stale = (
        (await session.execute(_unmatched_arrivals().where(ShuttleArrival.signal_ts < cutoff)))
        .scalars()
        .all()
    )
    for arrival in stale:
        await session.execute(
            pg_insert(Alert).values(
                type=ARRIVAL_EXPIRED_ALERT_TYPE,
                severity="warning",
                source_type=SHUTTLE_SOURCE_TYPE,
                source_id=arrival.id,
            )
        )
        logger.info("셔틀 신호 %s 짝짓기 TTL 초과 — 만료 처리", arrival.id)
    return [arrival.id for arrival in stale]


async def _match_arrival_for_robot(
    session: AsyncSession, robot_id: str, robot_pk: int
) -> int | None:
    """이 로봇의 도착을 어느 셔틀 신호에 묶을지 고른다.

    만료 청소를 맨 앞에서 돌린다 — 상관관계로 짝이 맞아도 딴 게이트의 옛 미짝 신호는 닫혀야
    한다. 예전엔 폴백 경로에만 있어서, 명령을 잘 받는 로봇이 계속 도착하는 동안 옛 신호가
    영구히 열린 채로 남았다.

    그다음 순서는 셋이다.
    1. 이 로봇이 받은 목적지 명령의 인메모리 상관(FIFO). 명령 자체가 TTL을 넘겼거나 그
       신호가 이미 닫혔으면 버리고 다음을 본다.
    2. DB에 이 로봇 앞으로 통지됐다고 표시된 신호. 재기동으로 1번이 비어도 여기서 이어받는다.
    3. 어느 로봇에도 명령 안 된 신호 중 가장 오래된 것.

    2·3 모두 명령했던 게이트를 알면 그 게이트 안에서만 찾는다.
    """
    await expire_stale_arrivals(session)

    ttl_sec = get_settings().arrival_match_ttl_sec
    now = dt.datetime.now(dt.timezone.utc)
    links = _command_links.get(robot_id)
    gate_hint: int | None = None
    while links:
        link = links.popleft()
        if not _link_is_live(link, now, ttl_sec):
            continue
        if await _is_unmatched(session, link.arrival_id):
            return link.arrival_id
        # 그 신호는 이미 닫혔다(딴 로봇이 먼저 도착했거나 만료됐다). 게이트만 힌트로 남긴다.
        if gate_hint is None:
            gate_hint = link.gate_no

    own = await oldest_arrival_notified_to(session, robot_pk, gate_hint)
    if own is not None:
        return own.id

    fallback = await oldest_unmatched_arrival(session, gate_hint, uncommanded_only=True)
    return fallback.id if fallback is not None else None


# ── 상태 수신 ──────────────────────────────────────────────────────────────

def _robot_card(robot: Robot) -> dict[str, Any]:
    """`robot_status` 메시지에 실을 로봇 카드 한 줄.

    칸을 스냅샷 `robots[]`(RobotOut)과 **똑같이** 맞춘다. 화면이 스냅샷으로 채운 카드를 이
    메시지로 그대로 갈아끼우게 하는 게 목적이라, 칸이 하나라도 어긋나면 화면에 두 그림이 생긴다.

    position·led_status·mission_status는 DB 컬럼이 아니라 인메모리 최신값이다
    (`RobotPresentation`). 보고가 없었으면 null이라 기존 화면 계약은 그대로다.
    Robot.name이 곧 robot_id다.
    """
    shown = get_presentation(robot.name)
    return {
        "id": robot.id,
        "name": robot.name,
        "mode": robot.mode,
        "battery": robot.battery,
        "network_status": robot.network_status,
        "updated_at": robot.updated_at.isoformat() if robot.updated_at else None,
        "position": shown.position,
        "led_status": shown.led_status,
        "mission_status": shown.mission_status,
    }


async def _upsert_robot(
    session: AsyncSession,
    robot_id: str,
    summary: RobotStatusSummary | None,
    mission_status: MissionStatus | None = None,
) -> tuple[int, dict[str, Any]]:
    """robot_id(문자열)로 Robot 행을 찾거나 만들고, 보고된 필드만 갱신한다.

    RobotState의 robot_id는 "jetson01" 같은 기기 이름이고 Robot.id는 정수 PK라,
    이름으로 찾아 붙인다. position·led_status·mission_status는 Robot에 컬럼이 없어(스키마 유지)
    DB에 안 남기고 인메모리 최신값으로만 든다(`record_presentation`) — 거기서 카드·조회
    응답으로 흘러 나간다. 재기동으로 사라지는 건 의도된 거동이다.

    돌려주는 카드는 **커밋 전에** 떠 둔 값이다. 커밋 뒤에 ORM 객체를 다시 읽으면 만료된
    속성이라 세션이 또 질의하는데, 브로드캐스트는 세션 밖에서 돈다.
    """
    robot = (
        await session.execute(select(Robot).where(Robot.name == robot_id))
    ).scalars().first()
    if robot is None:
        robot = Robot(name=robot_id)
        session.add(robot)
        await session.flush()

    if summary is not None:
        if summary.mode is not None:
            robot.mode = summary.mode.value
        if summary.battery is not None:
            robot.battery = summary.battery
        if summary.comm_status is not None:
            robot.network_status = summary.comm_status.value
    # 카드를 뜨기 전에 넣어야 이번 보고의 위치·LED·임무 상태가 그 메시지에 실린다.
    record_presentation(robot_id, summary, mission_status)
    robot.updated_at = dt.datetime.now(dt.timezone.utc)
    await session.flush()
    return robot.id, _robot_card(robot)


async def _ensure_robot_pk(session: AsyncSession, robot_id: str) -> int:
    """robot_id(기기 이름)에 해당하는 `Robot` 행 PK. 없으면 만들어서라도 돌려준다.

    `_upsert_robot`과 같은 "이름으로 찾고 없으면 만든다" 규칙인데, 부르는 자리가 상태 보고가
    아니라 **명령 발신**이다. 명령을 실제로 받은 로봇은 행이 있어야 한다 — 그 행 없이는
    "이 신호는 이 로봇 앞으로 통지됐다"를 DB에 적을 자리가 없고, 그러면 통지가 나갔는데도
    `notified_robot_id`가 NULL로 남아 재전송 판정이 같은 신호를 다시 내보낸다(두 번 출동).

    행이 없는 상황은 억지 조건이 아니다. `robot_manager.register()`가 `robot_state` 프레임
    직후에 불리고 행 커밋은 그 뒤라, 그 틈에 셔틀 신호가 들어오면 명령은 나가는데 행이 아직
    없다. 정리 도구로 `robot` 행을 지운 뒤에도 붙어 있던 커넥션이 같은 자리다.

    ⚠ 음수·0 같은 표식으로 칸만 채우는 길은 안 골랐다. 값은 채워지지만 짝짓기
    (`oldest_arrival_notified_to`)가 그 pk를 영영 못 찾아 그 신호가 미짝으로 굳고 ETA 표본에서
    통째로 빠진다. 행을 만들면 그 로봇이 나중에 상태를 올릴 때 `_upsert_robot`이 같은 행을
    이름으로 찾아 붙어서 짝짓기가 그대로 이어진다.

    ⚠ `robot.name`엔 UNIQUE가 없다. 그래서 `scalar_one_or_none()`이 아니라 `first()`로 읽는다 —
    예전 코드는 어쩌다 같은 이름 행이 둘 생기면 통지 기록 자리에서 예외로 터졌다.

    ⚠⚠ **여기서 만든 행은 관제 화면에 뜨면 안 된다.** 모드·배터리·통신이 전부 NULL이라 그냥
    두면 목록(`GET /api/robots`)과 스냅샷에 빈 로봇 카드가 서고, 셔틀 통지 한 번마다 발표
    화면에 가짜 로봇이 늘어난다(운영 DB `robot` 테이블은 0행이라 전부 새로 생기는 카드다).
    그래서 조회 쪽에 짝이 되는 가드가 있다 — `routers/query.py:_has_nothing_to_show`가 그릴
    값이 하나도 없는 행을 목록에서 뺀다. **기록은 DB에 남고 화면에서만 가린다.** 그 로봇이
    상태를 올리는 순간 `_upsert_robot`이 같은 행에 값을 채워 카드가 그때 나타난다.
    한쪽만 고치면 안 된다 — 이 함수를 지우면 두 번 출동이 살아나고, 저 가드를 지우면 빈
    카드가 뜬다.
    """
    robot_pk = (
        await session.execute(select(Robot.id).where(Robot.name == robot_id))
    ).scalars().first()
    if robot_pk is not None:
        return robot_pk
    robot = Robot(name=robot_id)
    session.add(robot)
    await session.flush()
    logger.info("명령을 받은 로봇 %s의 행이 없어 새로 만들었다 (통지 기록용)", robot_id)
    return robot.id


async def _insert_notice(
    session: AsyncSession,
    spec: tuple[str, str, str],
    *,
    robot_pk: int,
    mission_status: str | None,
    from_status: str | None,
    to_status: str | None,
    arrival_id: int | None,
) -> MissionNotice:
    """알림 하나를 Alert 행으로 남긴다. 셔틀 신호에 묶였으면 그 신호를 출처로 적는다."""
    alert_type, severity, message = spec
    source_type = SHUTTLE_SOURCE_TYPE if arrival_id is not None else "robot"
    source_id = arrival_id if arrival_id is not None else robot_pk
    alert_id = (
        await session.execute(
            pg_insert(Alert)
            .values(
                type=alert_type,
                severity=severity,
                source_type=source_type,
                source_id=source_id,
            )
            .returning(Alert.id)
        )
    ).scalar_one()
    return MissionNotice(
        alert_type=alert_type,
        alert_id=alert_id,
        severity=severity,
        message=message,
        mission_status=mission_status,
        from_status=from_status,
        to_status=to_status,
        arrival_id=arrival_id,
    )


async def _apply_mission_edge(
    session: AsyncSession, *, robot_pk: int, robot_id: str, mission_status: str
) -> list[MissionNotice]:
    """mission_status 전이만 알림으로 바꾼다. 기본은 값이 바뀐 순간 하나뿐이다.

    ARRIVED에서 벗어나면 출발 알림을 먼저 내고(도착과 대칭), 그다음에 새 상태의 알림을 낸다.
    그래서 ARRIVED → RETURN_COMPLETE 한 번에 메시지가 둘 나갈 수 있다.

    같은 값 ARRIVED 재보고를 재출동 도착으로 치는 길은 `ARRIVAL_EDGE_ON_RECOMMAND`가
    켜졌을 때만 열린다. 기본으로 닫아 둔 이유는 명령이 브로드캐스트라서다 — 게이트에 서
    있는 로봇도 새 신호마다 명령을 받아, 켜 두면 주기 보고가 가짜 도착이 된다.
    """
    now = dt.datetime.now(dt.timezone.utc)
    prune_mission_states(now)
    previous = _mission_states.get(robot_id, MissionState())
    if mission_status == previous.status:
        # 같은 값 재보고는 사건이 아니다. 보고 시각만 갱신하고 끝낸다(TTL 청소 기준).
        previous.seen_at = now
        _mission_states[robot_id] = previous
        if mission_status == MissionStatus.FAILED.value:
            # ⛔ **연속 실패는 첫 건만 알림이 나던 자리**(2026-08-06 · 실기기 로그로 잡힘).
            # 그날 17분 안에 같은 사유로 네 번 멈췄는데(`ARM 차단 fault`) 화면에는 한 번만
            # 떴다. 상태 수명이 한 시간이고 보고마다 갱신돼서, 붙어 있는 로봇은 첫 FAILED가
            # 영영 "직전 상태"로 남는다.
            #
            # ⭐ **사유로는 못 가른다** — 그날 네 건이 글자까지 같았다. 가르는 것은 그 사이에
            # 새 명령이 나갔나 하나뿐이다(네 건 다 앞에 목적지 명령이 있었다).
            #
            # ⚠ 이 판정은 **로봇이 값이 바뀔 때만 올린다**는 젯슨 계약에 기대고 있다. 주기
            # 보고로 FAILED를 계속 올리는 기기가 붙으면 셔틀 신호마다 알림이 겹친다 —
            # 그때는 여기 대신 로봇 쪽 계약을 먼저 봐라.
            if not _has_command_link_since(robot_id, previous.at, now):
                return []
        elif mission_status != MissionStatus.ARRIVED.value:
            return []
        elif not get_settings().arrival_edge_on_recommand:
            return []
        # 켜졌을 때만: 그 로봇 앞으로 직전 도착 뒤에 새 명령이 나갔으면 재출동 도착으로 친다.
        elif not _has_command_link_since(robot_id, previous.at, now):
            return []

    notices: list[MissionNotice] = []
    if previous.status == MissionStatus.ARRIVED.value:
        notices.append(
            await _insert_notice(
                session,
                DEPARTURE_NOTICE,
                robot_pk=robot_pk,
                # 출발은 mission_status enum에 없는 사건이다. 여기에 새 상태를 실으면 화면이
                # 출발 메시지를 복귀완료로 읽는다 — 전이는 from/to로만 싣는다.
                mission_status=None,
                from_status=MissionStatus.ARRIVED.value,
                to_status=mission_status,
                arrival_id=previous.arrival_id,
            )
        )

    # ⭐ 진행 중 상태는 여기서 끝낸다(EN_ROUTE). 위 "ARRIVED에서 벗어나는 전이"가 이미 출발
    # 알림을 냈으니 알림이 사라지는 게 아니고, 이 값 자체로 사건을 하나 더 만들지 않을 뿐이다.
    # ⚠ 상태 저장은 아래에서 그대로 한다 — 화면 카드가 "이동 중"을 보려면 값이 남아야 한다.
    if mission_status in _NO_NOTICE_STATUSES:
        _mission_states[robot_id] = MissionState(
            status=mission_status, at=now, seen_at=now, arrival_id=None
        )
        return notices

    arrival_id: int | None = None
    if mission_status == MissionStatus.ARRIVED.value:
        arrival_id = await _match_arrival_for_robot(session, robot_id, robot_pk)
    notices.append(
        await _insert_notice(
            session,
            MISSION_NOTICES[mission_status],
            robot_pk=robot_pk,
            mission_status=mission_status,
            from_status=previous.status,
            to_status=mission_status,
            arrival_id=arrival_id,
        )
    )
    _mission_states[robot_id] = MissionState(
        status=mission_status, arrival_id=arrival_id, at=now, seen_at=now
    )
    return notices


def _status_log_row(state: RobotStateIn, robot_pk: int) -> RobotStatusLog | None:
    """주행 시계열 한 행을 만든다. 남길 게 없으면 None이다.

    ⭐ **왜 남기나** — 2026-08-06 첫 주행에서 실패 5건이 났는데 사유도 그때 위치도 안 남아
    원인을 못 봤다. 같은 날 빔 문제는 `gate_pass_event`가 남아서 원인을 좁혔다.

    ⚠ **모든 프레임을 남기지는 않는다.** 젯슨은 `status_summary`만 든 프레임도 올리는데
    (배터리·모드만 바뀐 보고) 그건 `robot` 행이 이미 들고 있어서 두 벌이 된다. 아래 여섯 칸
    중 하나라도 실려야 한 행을 쓴다.

    ⚠ **`odom`과 `position`을 둘 다 담는다.** 앞은 젯슨 원본(로봇을 켠 자리가 원점인 상대
    좌표)이고 뒤는 서버가 재부팅 오프셋을 보정한 지도 좌표라 값이 다르다 — 하나만 남기면
    보정이 맞았는지 나중에 못 가린다(`jetson_adapter` 머리 "상대 좌표 원점").
    """
    summary = state.status_summary
    position = summary.position.model_dump() if summary and summary.position else None
    comm_status = summary.comm_status.value if summary and summary.comm_status else None
    mission_status = state.mission_status.value if state.mission_status else None

    if not any(
        (state.odom, state.imu, state.sensor_health, state.log, position, mission_status)
    ):
        return None

    return RobotStatusLog(
        robot_id=robot_pk,
        imu=state.imu,
        odom=state.odom,
        comm_status=comm_status,
        # ⚠ 서버 수신 시각이다. 젯슨 관측 시각을 쓰려면 계약에 그 칸이 먼저 있어야 하는데
        # `RobotStateIn`에는 없다 — 있는 척하면 시계 이원화가 조용히 생긴다.
        ts=dt.datetime.now(dt.timezone.utc),
        mission_status=mission_status,
        position=position,
        log=state.log,
        sensor_health=state.sensor_health,
    )


async def handle_robot_state(state: RobotStateIn) -> RobotStateResult:
    """RobotState 한 건을 반영하고 대시보드로 중계한다.

    ⚠ 인메모리 에지 상태(`_mission_states`)와 짝짓기 큐(`_command_links`)는 커밋 **전에**
    바뀐다 — 에지 판정과 짝짓기가 알림 행을 만드는 근거라 순서를 뒤집을 수가 없다. 그래서
    커밋이 터지면 떠 둔 값으로 되돌린다. 안 되돌리면 재전송된 ARRIVED가 "직전 상태 =
    ARRIVED"에 걸려 전이가 아니게 되고, 소비된 CommandLink도 안 돌아와 그 도착의 알림과
    짝짓기가 영구히 사라진다.
    """
    snapshot = snapshot_edge_state(state.robot_id)
    try:
        async with get_session() as session:
            robot_pk, card = await _upsert_robot(
                session, state.robot_id, state.status_summary, state.mission_status
            )
            # ⭐ 주행 시계열 한 행(0017). 알림·중계와 **같은 트랜잭션**이라 커밋이 터지면
            # 같이 되돌아간다 — 알림은 남았는데 그때 위치가 없는 짝이 안 생긴다.
            log_row = _status_log_row(state, robot_pk)
            if log_row is not None:
                session.add(log_row)
            # 수명 갱신은 mission_status 유무와 무관하다 — 주행 보고(null)만 올리는 로봇의
            # 에지 상태가 TTL에 걷히면 도착해서 올린 ARRIVED가 가짜 도착이 된다.
            touch_mission_state(state.robot_id)
            notices: list[MissionNotice] = []
            if state.mission_status is not None:
                notices = await _apply_mission_edge(
                    session,
                    robot_pk=robot_pk,
                    robot_id=state.robot_id,
                    mission_status=state.mission_status.value,
                )
            await session.commit()
    except Exception:
        restore_edge_state(state.robot_id, snapshot)
        raise

    await manager.broadcast(
        make_envelope("robot_state", _state_json(state), robot_id=state.robot_id)
    )
    # 원본 중계(robot_state) 바로 뒤에 "DB 행이 어떻게 됐나"(robot_status)를 붙인다.
    # 화면이 원본 프레임에서 카드 값을 스스로 뽑으면 "안 실린 필드는 안 덮는다" 규칙이
    # 서버·화면 두 벌로 갈라진다 — 그 판정은 서버 한 곳에서만 한다(S15P11C207-81).
    await manager.broadcast(
        make_envelope("robot_status", card, robot_id=state.robot_id)
    )
    for notice in notices:
        # ⭐ 게이트 스피커에 넘기는 자리. **커밋 뒤라야 한다** — 위 `except`가 되돌리는 갈래로
        # 빠지면 알림 행이 안 남는데 소리만 울린 꼴이 된다. 큐가 인메모리라 되돌릴 수도 없다.
        #
        # ⚠ 화면 중계(`broadcast`)보다 **앞에 둔다.** 소리는 게이트에 선 사람용이고 중계는
        # 관제 화면용이라, 화면 쪽이 막혀도 현장 안내는 나가야 한다.
        #
        # `gate_no`는 안 싣는다 — 어느 게이트에 선 로봇인지 서버가 모른다(`device_sound`
        # 모듈 머리 "기기가 여럿일 때"). `event_id`에는 알림 번호를 실어, 라파이 ack 로그에서
        # 어느 사건이 울렸는지 되짚을 수 있게 한다.
        sound_kind = MISSION_SOUND_KINDS.get(notice.alert_type)
        if sound_kind is not None:
            push_sound(sound_kind, event_id=f"alert-{notice.alert_id}")
        await manager.broadcast(
            make_envelope(
                notice.alert_type,
                {
                    "alert_id": notice.alert_id,
                    # ⭐ 봉투 이름과 **같은 값을 본문에도 싣는다**(2026-08-06 · 프론트 38차 §1-2).
                    # 화면이 봉투 이름을 종류로 되짚어 쓰고 있었는데, 그러면 봉투 이름과 종류
                    # 이름이 갈리는 날 조용히 깨진다. 칸이 하나 늘 뿐이라 값이 싸다.
                    "type": notice.alert_type,
                    "severity": notice.severity,
                    "message": notice.message,
                    "mission_status": notice.mission_status,
                    "from_status": notice.from_status,
                    "to_status": notice.to_status,
                    "shuttle_arrival_id": notice.arrival_id,
                },
                robot_id=state.robot_id,
            )
        )
    return RobotStateResult(robot_pk=robot_pk, notices=notices)


# ── 프레임 처리 ────────────────────────────────────────────────────────────

def _drop_unknown_mission_status(data: Any) -> Any:
    """`mission_status` 값이 계약 밖이면 **그 칸만** 떼고 나머지를 살린다.

    ⚠ 안 떼면 낱말 하나가 프레임 **전체**를 `invalid_robot_state`로 만든다 — 같이 실려 온
    배터리·위치·LED까지 통째로 버려지고 화면 카드가 멈춘다. 로봇 쪽이 enum에 없는 중간
    상태(`MOVING` 같은)를 올리는 건 실제로 있는 갈래라, 여기서 값 하나 때문에 상태 보고가
    끊기면 손해가 훨씬 크다.

    젯슨은 어댑터가 앞에서 이미 걸러 주는데(`jetson_adapter`) 일반 `robot_state` 경로는 그
    완화를 안 탄다. 두 경로가 같은 규칙을 지나게 이 자리에 둔다.

    ⚠ 떼는 건 **모르는 값**뿐이다. 칸이 아예 없거나 null인 건 그대로 둔다 — 그건 "보고 안
    함"이라는 정상 갈래고, 다른 칸(robot_id 등)의 검증은 손대지 않는다.
    """
    if not isinstance(data, dict):
        return data
    raw = data.get("mission_status")
    if raw is None:
        return data
    try:
        MissionStatus(raw)
    except ValueError:
        logger.warning(
            "로봇 %s가 계약 밖 mission_status %r을 올렸다 — 그 칸만 버리고 나머지는 받는다",
            data.get("robot_id"), raw,
        )
        return {k: v for k, v in data.items() if k != "mission_status"}
    return data


# 로그 한 줄에 싣는 ACK 본문 길이 상한(글자). 넘으면 자른다.
_ACK_LOG_MAX = 200


# ACK 결과 낱말(2026-08-05 젯슨 §6-4). 젯슨이 "받았다"만이 아니라 **거절했다**를 말할 수
# 있어야 한다 — 이미 주행 중·FAULT·STM32 미준비·오도메트리 무효면 거절한다.
ACK_RESULT_ACCEPTED = "accepted"
ACK_RESULT_REJECTED = "rejected"
# 거절 사유 문구 상한(글자). 젯슨이 자기 서비스 응답 message 를 그대로 실어 보낸다.
_ACK_REASON_MAX = 200


async def _log_command_ack(ws: Any, data: Any) -> None:
    """로봇이 보낸 command_ack 한 장을 대조하고 로그로 남긴다.

    ## ⭐ 거절이면 화면까지 알린다 (2026-08-05 젯슨 §6-4)

    예전 계약은 `{"command_id": ...}` 하나뿐이라 **젯슨이 명령을 거절해도 알릴 방법이
    없었다.** 서버는 출발한 줄 알고 계속 기다리고, 화면은 5초 창을 띄운 채 굳는다.

    이제 `result`가 `rejected`면 대시보드로 `robot_command_rejected` 봉투를 흘린다.
    ⚠ **새 봉투 종류다** — 기존 봉투 칸을 안 늘렸다. dev의 WS 계약 시험이 종류마다 칸
    집합을 통째로 못박고 있어서, 있던 종류에 칸을 붙이면 평소 계약이 달라진다.

    ⚠ `result`가 없거나 모르는 값이면 **예전처럼 로그만** 남긴다. 젯슨 구판이 그대로
    붙어도 안 깨지는 자리다.

    ⚠ **본문을 원문 그대로 로그에 싣지 마라.** 로봇 채널 인증은 기본이 꺼짐
    (`ws_require_api_key_robot=False`)이라 아무나 붙어 프레임을 올릴 수 있고, 수신 루프는
    그걸 무한히 받는다(`main.py`). 개행이 든 긴 글자를 그대로 찍으면 로그가 여러 줄로 갈려
    가짜 항목을 심을 수 있고 파일도 부푼다. `repr`로 감싸 개행을 이스케이프하고 길이도 자른다.

    ⚠ `command_id`는 서버가 낸 명령 이름과 대조만 한다(`command_link_known`). 모르는 값이라도
    끊거나 버리지 않는다 — TTL이 지난 늦은 ACK도 여기로 오고, 그걸 끊으면 로봇이 재접속
    루프를 돈다. 대조 결과는 로그 갈래로만 드러낸다(젯슨 숙제 §3·`schemas.CommandOut` 주석).
    """
    body = repr(data)
    if len(body) > _ACK_LOG_MAX:
        body = body[:_ACK_LOG_MAX] + "…(잘림)"
    command_id = data.get("command_id") if isinstance(data, dict) else None
    robot_id = robot_manager.robot_id_of(ws)
    if not command_id:
        logger.warning("로봇 명령 ACK에 command_id가 없다 (robot=%s): %s", robot_id, body)
        return
    if not command_link_known(str(command_id), robot_id):
        # ⛔ **여기서 돌아가지 않는다.** 2026-08-06까지는 `return`이 있었고, 단독 명령이
        # 대조 목록에 아예 안 올라가던 것과 겹쳐 **젯슨이 복귀·즉시이동·긴급정지를 거절해도
        # 그 사실이 화면까지 안 갔다.** 요원은 버튼을 눌렀는데 아무 반응이 없었다.
        # 모르는 번호라는 사실은 로그로 남기고, 아래 거절 갈래는 그대로 태운다.
        logger.warning(
            "모르는 command_id의 ACK가 왔다 (robot=%s command_id=%s): %s",
            robot_id, command_id, body,
        )
    result = data.get("result") if isinstance(data, dict) else None
    # ⭐ 명령이 로봇에 닿았는지를 DB에 남긴다(0018). 로그 파일은 컨테이너를 갈면 사라지고
    # 질의도 안 돼서 "아까 그 복귀가 닿긴 했나"를 되짚을 수 없었다.
    await record_ack(
        channel=CHANNEL_ROBOT,
        command_id=str(command_id),
        result=str(result) if result else None,
        detail=data.get("reason") if isinstance(data, dict) else None,
        target=robot_id,
    )
    if result == ACK_RESULT_REJECTED:
        raw_reason = data.get("reason") if isinstance(data, dict) else None
        reason = str(raw_reason)[:_ACK_REASON_MAX] if raw_reason else None
        # ⚠ 거절은 info 가 아니라 warning 이다 — 사람이 봐야 하는 사건이다.
        logger.warning(
            "⛔ 로봇이 명령을 거절했다 (robot=%s command_id=%s 사유=%s): %s",
            robot_id, command_id, reason, body,
        )
        await manager.broadcast(
            make_envelope(
                "robot_command_rejected",
                {
                    "command_id": str(command_id),
                    "reason": reason,
                },
                robot_id=robot_id,
            )
        )
        return
    logger.info(
        "로봇 명령 ACK (robot=%s command_id=%s result=%s): %s",
        robot_id, command_id, result or "(없음)", body,
    )
    if result == ACK_RESULT_ACCEPTED:
        # ⭐ **수락도 화면에 보낸다**(2026-08-07 사용자 확정 · 프론트 49차 §4-2).
        #
        # ⚠ **2026-08-06까지는 반대였다.** "받아들인 명령까지 흘리면 소음만 는다"로 보고
        # 거절만 보냈다. 뒤집은 근거는 하나다 — **"거절이 안 왔으니 닿았겠지"는 안 닿은
        # 것과 구분이 안 된다.** 명령이 로봇에 실제로 닿았는지를 화면이 알 길이 아예
        # 없었고, 명령 칩이 "접수"(서버가 받았다)에서 멈춰 있었다.
        #
        # ⚠ 거절 봉투(`robot_command_rejected`)와 **대칭**으로 둔다. 한쪽만 있으면 화면이
        # 추측을 해야 하고, 그 추측이 위 문장 그대로 틀린다.
        await manager.broadcast(
            make_envelope(
                "robot_command_accepted",
                {"command_id": str(command_id)},
                robot_id=robot_id,
            )
        )


async def handle_robot_frame(ws: Any, frame: Any) -> None:
    """로봇이 올린 프레임 한 개를 처리한다.

    잘못된 프레임은 연결을 끊지 않고 `error` 메시지로 돌려준다 — 로봇이 재접속 루프에
    빠지면 상태 보고가 통째로 멈추기 때문이다.

    젯슨 web_bridge는 계약과 다른 모양으로 올린다(type이 status_summary·odom·imu로 갈린다).
    그 셋만 앞에서 robot_state payload로 접어 아래 경로에 그대로 태운다
    (`jetson_adapter`, S15P11C207-338). 접는 자리를 여기 하나로 둬야 검증·등록·ack·중계가
    한 갈래로 남는다 — 젯슨용 두 번째 처리 경로를 만들면 그때부터 규칙이 두 벌로 갈린다.

    ⚠ **내용물 칸은 `data`다.** 나가는 프레임은 전부 `make_robot_envelope`로 씌우고, 로봇이
    올리는 프레임(status_summary·odom·imu·robot_state·pong·command_ack)도 `data`에서 읽는다.
    대시보드로 나가는 중계도 2026-08-04부터 같은 이름이라(`make_envelope`) 한 로봇 보고가
    두 이름으로 갈라지지 않는다. 봉투 이름을 아는 서버 코드는 경계 셋뿐이다 — 이 함수,
    `jetson_adapter`, `ws.make_envelope`. 그 밖으로는 서버 dict만 흐른다.

    ⚠ 프레임이 왔다는 사실 자체가 **살아 있다는 증거다**(`note_activity`). pong만 세면, 소켓을
    안 읽어서 답을 못 하는 젯슨 브리지가 초당 두 장씩 올리는 동안에도 스테일로 잡혀
    45초마다 끊긴다(실측 결함 1). 깨진 프레임도 세는 게 맞다 — 모양이 틀렸을 뿐 소켓은 살아 있다.
    """
    robot_manager.note_activity(ws)

    if not isinstance(frame, dict):
        await ws.send_json(make_robot_envelope("error", {"reason": "frame_not_object"}))
        return

    frame_type = frame.get("type")

    if is_jetson_frame(frame):
        adapted = adapt_jetson_frame(frame)
        if adapted.error is not None:
            await ws.send_json(make_robot_envelope("error", adapted.error))
            return
        frame = {"type": "robot_state", "data": adapted.payload}
        frame_type = "robot_state"

    if frame_type == "robot_state":
        try:
            state = RobotStateIn.model_validate(
                _drop_unknown_mission_status(frame.get("data") or {})
            )
        except ValidationError as exc:
            await ws.send_json(
                make_robot_envelope(
                    "error",
                    {
                        "reason": "invalid_robot_state",
                        "detail": [e["msg"] for e in exc.errors()][:3],
                    },
                )
            )
            return
        # ⚠ 붙은 채로 남의 이름으로 갈아타는 프레임은 여기서 막는다. 연결은 안 끊는다 —
        # 진짜 로봇이 끊기면 상태 보고가 통째로 멈추고, 이 갈래는 애초에 진짜 로봇이 안 밟는다.
        owner = robot_manager.claim_conflict(ws, state.robot_id)
        if owner is not None:
            logger.warning(
                "로봇 %s로 등록된 커넥션이 %s 이름으로 상태를 올렸다 — 그 프레임을 버린다",
                owner, state.robot_id,
            )
            await ws.send_json(
                make_robot_envelope(
                    "error",
                    {"reason": "robot_id_mismatch", "registered_as": owner},
                    robot_id=owner,
                )
            )
            return
        # 같은 robot_id를 든 옛 소켓이 남아 있으면 여기서 밀려난다. 목록에서 빼는 것만으로는
        # 그 라우트 루프가 계속 돌아서, 소켓까지 닫아 줘야 `finally`로 빠진다(ws._close_quietly).
        for replaced in robot_manager.register(ws, state.robot_id):
            logger.info(
                "로봇 %s가 새 커넥션으로 붙었다 — 같은 ID를 들고 있던 옛 소켓을 닫는다",
                state.robot_id,
            )
            await robot_manager.drop_stale(replaced)
        try:
            result = await handle_robot_state(state)
        except Exception:
            # ⛔ **DB가 한 번 흔들려도 소켓은 살린다**(2026-08-09 백지검토 ①).
            #
            # `handle_robot_state`는 커밋이 터지면 인메모리 에지 상태를 되돌리고 예외를 **그대로
            # 올린다**(그쪽 `except Exception: restore_edge_state(...); raise`). 여기서 안 잡으면
            # 라우트(`main.ws_robot`)의 `except Exception`까지 올라가 **연결이 닫히고**, 로봇은
            # 재접속해서 첫 상태 프레임에 또 같은 자리에 걸린다 — DB가 회복될 때까지 재접속
            # 루프다. 그동안 붙은 로봇이 0이라 **긴급정지·복귀 명령도 같이 못 나간다.**
            # 반대로 소켓만 살아 있으면 DB 없이도 정지 프레임은 로봇에 닿는다.
            #
            # ⚠ 이 함수 머리의 계약("잘못된 프레임은 연결을 끊지 않고 `error`로 돌려준다")에서
            #    **DB 실패 갈래만 빠져 있었다.** 모양이 틀린 프레임보다 훨씬 무거운 사고인데
            #    끊는 쪽으로 굴고 있었다. 같은 규칙으로 맞춘다.
            #
            # ⚠ `state_ack`는 못 보낸다 — `robot_pk`가 없다. 젯슨 브리지는 이 답을 안 읽어서
            #    (소켓을 안 읽는 모양) 거동이 안 바뀌고, 읽는 클라이언트는 `error`로 안다.
            logger.exception(
                "로봇 상태를 저장하지 못했다 (robot=%s) — 연결은 살려 둔다", state.robot_id
            )
            await ws.send_json(
                make_robot_envelope(
                    "error",
                    {"reason": "state_store_failed"},
                    robot_id=state.robot_id,
                )
            )
            return
        await ws.send_json(
            make_robot_envelope(
                "state_ack",
                {
                    "robot_pk": result.robot_pk,
                    "notice": result.notice.alert_type if result.notice else None,
                    "notices": [n.alert_type for n in result.notices],
                },
                robot_id=state.robot_id,
            )
        )
    elif frame_type == "tag_target":
        # ⭐ 젯슨 에이프릴태그 검출값. **Robot 행을 안 건드리고 대시보드로만 흘린다** —
        # 로봇 상태가 아니라 지도 위치 보정값이라서다(`TagTargetIn` docstring).
        #
        # ⛔ 이 갈래가 없던 동안 서버는 초당 다섯 번 `unknown_frame_type` 에러를 되돌려
        #    보냈다. 젯슨은 `aee8ce4`(2026-08-04)부터 `/tag/target`을 5Hz로 올리고 있었고,
        #    화면은 그 값을 못 받아 로봇을 평면도에 절대 좌표로 못 그렸다.
        #
        # ⚠ 등록(`robot_manager.register`)은 여기서 안 한다. 그 자리는 `robot_state`가
        #    맡는 계약이고, 여기서도 하면 "어느 프레임이 로봇을 등록하나"가 두 벌이 된다.
        #    아직 등록 안 된 소켓이 이걸 먼저 올리면 `robot_id`가 None으로 나가는데,
        #    화면은 그때 어느 로봇인지 모르므로 그리지 않는다(젯슨은 상태를 먼저 올린다).
        try:
            target = TagTargetIn.model_validate(frame.get("data") or {})
        except ValidationError as exc:
            await ws.send_json(
                make_robot_envelope(
                    "error",
                    {
                        "reason": "invalid_tag_target",
                        "detail": [e["msg"] for e in exc.errors()][:3],
                    },
                )
            )
            return
        await manager.broadcast(
            make_envelope(
                "tag_target",
                target.model_dump(),
                robot_id=robot_manager.robot_id_of(ws),
            )
        )
    elif frame_type == "command_ack":
        # 전선 계약이 `data`다(젯슨 §8-4의 "command_ack도 data로 통일"). 젯슨엔 아직 송신
        # 코드가 없어서 `payload` 폴백은 안 둔다 — 두 이름을 다 받으면 계약이 두 벌로 굳는다.
        await _log_command_ack(ws, frame.get("data"))
    elif frame_type in ("pong", "ping"):
        # 하트비트 응답이 왔다 = 살아 있다. 미스 카운터를 되돌린다(스테일 오판 방지).
        # 본문은 안 읽는다 — 왔다는 사실만 세는 프레임이라 `data`가 비어도 그대로 통과다.
        robot_manager.note_pong(ws)
        return
    else:
        await ws.send_json(
            make_robot_envelope(
                "error", {"reason": "unknown_frame_type", "type": frame_type}
            )
        )


# ── 셔틀 도착 → 로봇 통지 ──────────────────────────────────────────────────

async def notify_shuttle_arrival(
    session: AsyncSession,
    *,
    arrival_id: int,
    gate_no: int | None = None,
    destination: Destination | None = None,
) -> tuple[int, str]:
    """셔틀 도착 신호를 목적지 명령으로 바꿔 로봇에 밀어낸다.

    `destination`을 주면 그 목적지로, 안 주면 설정값(`shuttle_destination`)으로 간다.
    출동 대기 창(app/dispatch.py)이 그날 기본값이나 요원이 고른 값을 여기로 실어 보낸다 —
    명령을 내는 자리를 이 함수 하나로 묶어 둬야 `notified_robot_id` 기록과 도착 짝짓기용
    상관관계(CommandLink)가 어느 갈래로 나가든 똑같이 남는다.

    돌려주는 값은 (명령을 받은 로봇 수, command_id)다. 0이면 (붙은 로봇이 없거나 아직 상태를
    한 번도 안 보낸 미식별 커넥션뿐이라) 명령이 못 갔다는 뜻이고, 그 사실을 대시보드 메시지에
    실어 화면에서 확인할 수 있게 한다.

    ⚠ 두 값의 뜻이 다르다. 배달 여부를 가리키는 건 **로봇 수 하나뿐이고**, `command_id`는
    0대일 때도 채워 준다 — 보내기 전에 `shuttle-{arrival_id}`로 정하는 "이 도착 신호에 매인
    명령 이름"이라서다. 0대일 때 비우면 재전송이 같은 이름을 다시 낼 근거가 사라지고
    (재전송분이 같은 이름이어야 로봇 쪽에서 접힌다), 부르는 쪽도 뒤에 올 ARRIVED를 되짚을
    이름을 잃는다. 계약 문장은 `schemas.ShuttleCallOut.command_id` 주석에 있다. 명령을 실제로 보냈으면 ShuttleArrival.notified_robot_id에
    그 로봇을 남기고, "이 로봇에 이 신호로 명령했다"는 상관관계를 인메모리에 적어 둔다 —
    나중에 온 ARRIVED를 이 신호에 정확히 묶는 근거다.

    ⚠ 그 로봇의 `Robot` 행이 아직 없으면 **만들어서라도** 기록을 남긴다(`_ensure_robot_pk`).
    예전엔 행이 있을 때만 적어서 "보냈는데 안 적힌" 상태가 났고, 그걸 재전송 판정
    (`routers/ingest.py`의 `notified_robot_id IS NULL`)이 미통지로 읽어 같은 신호를 다시 냈다 —
    로봇이 목적지 명령을 두 번 받는다.

    명령 메시지는 CommandOut(= command.schema.json의 pydantic 판)으로 조립한다. 손으로 dict를
    쌓으면 허용 밖 목적지가 그대로 로봇까지 내려간다.
    """
    # 명령을 새로 내는 자리가 큐가 자라는 유일한 자리다. 여기서 TTL 넘긴 상관을 걷어내면
    # 도착을 한 번도 안 올리는 로봇의 큐도 TTL 창 안으로 묶인다.
    prune_command_links()

    settings = get_settings()
    command_id = f"shuttle-{arrival_id}"
    # ⚠ dump 규칙을 손으로 적지 않는다. `as_frame()`이 전선 계약(`exclude_none`)을 쥐고 있어서,
    # 여기서 따로 적으면 규칙이 두 자리로 갈리고 한쪽만 고쳐진다.
    command = CommandOut(
        command_id=command_id,
        type=CommandType.cmd_destination,
        ttl_ms=settings.command_ttl_ms,
        cmd_destination=destination or settings.shuttle_destination,
    ).as_frame()
    # ⭐ **DB 표시가 송신보다 먼저다.** 예전에는 `send_command` 뒤에 `notified_robot_id`를
    # 적었는데, 그 사이(커밋 실패·워커 종료)에 끊기면 로봇은 명령을 받았는데 DB는 "미통지"로
    # 남는다 — 재전송 판정(`shuttle_call._recover_duplicate`의 `notified_robot_id IS NULL`)이
    # 그걸 유실로 읽어 같은 명령을 다시 내보내서 로봇이 두 번 출동한다.
    # 순서를 뒤집으면 반대 갈래(적었는데 못 보냄)가 생기는데 그쪽은 **되돌릴 수 있다** —
    # 아래에서 실제로 받은 로봇에 맞춰 다시 적고, 아무도 못 받았으면 표시를 지운다.
    expected = robot_manager.robot_ids()
    if expected:
        await _mark_notified(session, arrival_id, await _ensure_robot_pk(session, expected[0]))

    delivered_ids = await robot_manager.send_command(command)
    # ⭐ 셔틀 출동도 이동 명령이라 앞 사이클의 도착을 지운다(프론트 38차 §1-1).
    # ⚠ 단독 명령 통로(`dispatch._send_standalone`)와 **같은 규칙**이다 — 한쪽만 지우면
    # "게이트 출동은 표시가 지워지는데 즉시 이동은 안 지워지는" 갈래가 생긴다.
    for _name in delivered_ids:
        clear_mission_presentation(_name)

    issued_at = dt.datetime.now(dt.timezone.utc)
    for robot_id in delivered_ids:
        register_command_link(
            robot_id,
            CommandLink(
                command_id=command_id,
                arrival_id=arrival_id,
                gate_no=gate_no,
                issued_at=issued_at,
            ),
        )

    # ⭐ **낸 사실을 DB에도 남긴다.** 단독 명령 통로(`dispatch._send_standalone`)는 이미
    # 남기는데 여기만 빠져 있었다. 2026-08-07 실기기 주행에서 드러났다 — `shuttle-52` 행에
    # 발행 시각이 비고 ACK만 찍혀 있었다.
    #
    # ⛔ **ACK가 와야 행이 생기는 구조가 제일 나빴다.** 로봇이 응답을 안 하면 기록이 통째로
    # 없어서, 정작 알고 싶은 "명령은 냈는데 답이 없다"가 안 남는다. 게다가 통로마다 갈려서
    # 나중에 보는 사람이 "발행 기록이 없으니 명령을 안 냈구나"로 잘못 읽는다.
    await record_issued(
        channel=CHANNEL_ROBOT,
        command_id=command_id,
        command_type=CommandType.cmd_destination.value,
        # 목적지 명령은 붙은 로봇 전부에게 나가서 대상이 하나로 안 좁혀진다. 몇 대에 갔는지와
        # 어느 도착 신호로 낸 것인지를 대신 남긴다 — 0대면 "명령은 냈는데 아무도 없었다"다.
        payload={
            "delivered": len(delivered_ids),
            "arrival_id": arrival_id,
            "gate_no": gate_no,
        },
    )

    # 실제로 받은 대표 로봇이 미리 적어 둔 로봇과 다르면 맞춰 고친다(짝짓기가 이 pk로
    # 자기 신호를 되찾는다 — `oldest_arrival_notified_to`). 한 대도 못 받았으면 NULL로
    # 되돌려 재전송이 살아나게 한다.
    actual = delivered_ids[0] if delivered_ids else None
    head = expected[0] if expected else None
    if actual != head:
        robot_pk = await _ensure_robot_pk(session, actual) if actual is not None else None
        await _mark_notified(session, arrival_id, robot_pk)
    return len(delivered_ids), command_id


async def _mark_notified(
    session: AsyncSession, arrival_id: int, robot_pk: int | None
) -> None:
    """이 신호가 어느 로봇 앞으로 통지됐나를 DB에 적고 **바로 커밋한다**.

    커밋까지 여기서 하는 이유는 이 표시가 재전송 판정의 정본이라서다. 커밋 안 된 표시는
    딴 요청(재시도)에 안 보여서 아무것도 못 막는다.
    """
    await session.execute(
        update(ShuttleArrival)
        .where(ShuttleArrival.id == arrival_id)
        .values(notified_robot_id=robot_pk)
    )
    await session.commit()
