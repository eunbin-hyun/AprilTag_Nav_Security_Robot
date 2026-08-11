"""이동 중 상태(EN_ROUTE)와 도착 표시 비우기 (2026-08-06 · 프론트 38차 §1-1 · 젯슨 회신 §7).

⛔ **왜 있나** — `MissionStatus` 넷이 전부 "끝났다" 계열이라 **출발을 담을 낱말이 없었다.**
그래서 도착해서 찍힌 `ARRIVED`가 다음 도착까지 안 지워졌고, 화면이 그 값으로 태깅·ToF 판정을
켜서 **복귀 중에도 태깅이 켜져 있고 주행 중에도 ToF가 켜져 있었다**(사용자가 실기기 시연에서
짚은 자리다). 프론트와 젯슨이 같은 요청을 따로 보내왔다.

⚠ 고친 자리가 둘이라 시험도 둘이다.
  ① 젯슨이 EN_ROUTE를 보내면 서버가 받는다 (이 파일 앞쪽)
  ② 서버가 이동 명령을 보낸 순간 도착 표시를 비운다 (뒤쪽) — 젯슨이 EN_ROUTE를 안 보내도
     화면이 안 얼어붙게 하는 안전망이다
"""
import pytest

from app.db import get_session
from app.robot_channel import (
    MISSION_NOTICES,
    _NO_NOTICE_STATUSES,
    clear_mission_presentation,
    get_presentation,
    handle_robot_state,
    record_presentation,
    reset_robot_channel_state,
)
from app.schemas import MissionStatus, RobotStateIn, RobotStatusSummary

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ── ① 계약 — EN_ROUTE를 받는다 ──────────────────────────────────────────


def test_en_route_is_in_enum():
    """젯슨이 보낼 낱말이 계약에 있다."""
    assert MissionStatus.EN_ROUTE.value == "EN_ROUTE"


def test_every_status_is_either_notice_or_silent():
    """⛔ **닫힌 집합 검사.** `MISSION_NOTICES[x]`는 없는 키에 그대로 터진다.

    상태를 하나 늘리고 두 표 어디에도 안 적으면, 그 값이 처음 올라오는 순간 프레임 처리가
    통째로 죽는다. 그 사고를 여기서 미리 잡는다.
    """
    for status in MissionStatus:
        assert (
            status.value in MISSION_NOTICES or status.value in _NO_NOTICE_STATUSES
        ), f"{status.value}가 알림 표에도 조용한 상태 표에도 없다 — 올라오는 순간 터진다"


async def test_en_route_makes_no_alert_but_keeps_card():
    """⭐ 이동 중은 사건이 아니라 알림을 안 만든다. 다만 화면 카드에는 남는다."""
    reset_robot_channel_state()
    result = await handle_robot_state(
        RobotStateIn(robot_id="enroute-1", mission_status=MissionStatus.EN_ROUTE)
    )

    assert result.notices == [], "이동 중은 진행 상태라 알림을 만들면 안 된다"
    assert get_presentation("enroute-1").mission_status == "EN_ROUTE", (
        "화면이 '이동 중'을 그리려면 카드에는 남아야 한다"
    )


async def test_leaving_arrived_still_announces_departure():
    """⚠ 알림을 안 만든다고 **출발 알림까지 사라지면 안 된다.**

    출발은 `ARRIVED에서 벗어나는 전이`가 내는 별도 사건이다. EN_ROUTE를 조용히 처리하면서
    그 갈래까지 같이 죽이면 게이트 안내가 끊긴다.
    """
    reset_robot_channel_state()
    await handle_robot_state(
        RobotStateIn(robot_id="enroute-2", mission_status=MissionStatus.ARRIVED)
    )
    result = await handle_robot_state(
        RobotStateIn(robot_id="enroute-2", mission_status=MissionStatus.EN_ROUTE)
    )

    assert len(result.notices) == 1, "도착에서 벗어나는 출발 알림이 사라졌다"
    assert result.notices[0].to_status == "EN_ROUTE"


# ── ② 안전망 — 명령을 보낸 순간 도착 표시를 비운다 ────────────────────


def test_clear_wipes_only_mission_status():
    """⚠ 위치·LED는 안 건드린다 — 움직이는 동안에도 유효한 값이다."""
    reset_robot_channel_state()
    record_presentation(
        "clear-1",
        RobotStatusSummary(position={"x": 1.0, "y": 2.0}, led_status="GREEN"),
        MissionStatus.ARRIVED,
    )

    clear_mission_presentation("clear-1")

    shown = get_presentation("clear-1")
    assert shown.mission_status is None, "도착 표시가 안 지워졌다 — 화면이 계속 도착으로 읽는다"
    assert shown.position == {"x": 1.0, "y": 2.0}, "위치까지 지우면 지도에서 로봇이 사라진다"
    assert shown.led_status == "GREEN"


def test_clear_all_when_no_robot_id():
    """목적지 명령은 붙은 로봇 전부에게 나가서 대상이 하나로 안 좁혀진다."""
    reset_robot_channel_state()
    for name in ("clear-a", "clear-b"):
        record_presentation(name, None, MissionStatus.ARRIVED)

    clear_mission_presentation()

    assert get_presentation("clear-a").mission_status is None
    assert get_presentation("clear-b").mission_status is None


def test_clear_on_unknown_robot_is_safe():
    """⚠ 보고가 한 번도 없던 로봇에 불러도 빈 카드를 만들지 않는다."""
    reset_robot_channel_state()
    clear_mission_presentation("never-seen")

    assert get_presentation("never-seen").mission_status is None
