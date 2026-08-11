"""[초안·팀 확정 대기] 안건③ mission_status EN_ROUTE 검증.

핵심은 2번과 3번을 나란히 보는 거다. 플래그를 끄면 연속 재출동 도착이 한 번밖에
안 잡히고(지금 결함), 켜면 두 번 다 잡힌다.
"""
import contextlib
import json
import pathlib

import pytest

from app.config import get_settings
from app.robot.mission_state import (
    ALLOWED_TRANSITIONS,
    LEGACY_STATUSES,
    MissionStatus,
    MissionTracker,
)

SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[1] / "schemas" / "robot_state.schema.json"


@contextlib.contextmanager
def en_route(enabled: bool):
    """mission_status_en_route_enabled를 env+cache_clear로 잠깐 바꾸고 반드시 되돌린다.

    get_settings()가 lru_cache 싱글턴이라 속성을 직접 대입하면 xdist 병렬에서 서로
    밟는다. env를 monkeypatch로 바꾸고 cache_clear로 재구성을 강제하는 쪽이 안전하다.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("MISSION_STATUS_EN_ROUTE_ENABLED", "true" if enabled else "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


def _arrivals(statuses: list[str], robot_id: str = "robot01") -> int:
    tracker = MissionTracker()
    return sum(tracker.observe(robot_id, s).arrival_edge for s in statuses)


# 1. 공용 스키마 enum이 예전 셋을 그대로 두고 EN_ROUTE만 늘렸다(하위 호환).
def test_schema_enum_extends_without_removing():
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    enum = schema["properties"]["mission_status"]["enum"]
    assert LEGACY_STATUSES <= set(enum)
    assert MissionStatus.EN_ROUTE in enum
    assert None in enum


# 2. 플래그 꺼짐 — 연속 재출동 도착이 한 번밖에 안 잡힌다(지금 결함 재현).
def test_flag_off_misses_consecutive_redispatch_arrival():
    with en_route(False):
        assert _arrivals(["ARRIVED", "EN_ROUTE", "ARRIVED"]) == 1
        # 현장 로봇이 실제로 올리는 값은 EN_ROUTE 없이 ARRIVED 두 번이다 — 도착은 여전히 1회.
        assert _arrivals(["ARRIVED", "ARRIVED"]) == 1


# 3. 플래그 켜짐 — 사이에 EN_ROUTE가 들어가 두 번째 도착이 살아난다.
def test_flag_on_catches_second_arrival():
    with en_route(True):
        assert _arrivals(["ARRIVED", "EN_ROUTE", "ARRIVED"]) == 2


# 4. 플래그 켜짐 — 같은 ARRIVED 재전송은 여전히 도착으로 안 센다(하트비트 오검출 방지).
def test_repeated_arrived_is_not_a_new_edge():
    with en_route(True):
        assert _arrivals(["ARRIVED", "ARRIVED", "ARRIVED"]) == 1


# 5. 플래그 켜짐 — 도착 뒤 스스로 나선 순간을 redispatch로 집어낸다.
def test_redispatch_flag_marks_self_dispatch():
    with en_route(True):
        tracker = MissionTracker()
        tracker.observe("robot01", MissionStatus.ARRIVED)
        obs = tracker.observe("robot01", MissionStatus.EN_ROUTE)
    assert obs.accepted is True
    assert obs.redispatch is True
    assert obs.unexpected is False


# 6. 플래그 꺼짐 — EN_ROUTE는 낯선 값으로 흘리고 기존 상태를 안 건드린다.
def test_flag_off_ignores_en_route_without_losing_state():
    with en_route(False):
        tracker = MissionTracker()
        tracker.observe("robot01", MissionStatus.ARRIVED)
        obs = tracker.observe("robot01", MissionStatus.EN_ROUTE)
    assert obs.accepted is False
    assert obs.reason == "unknown_status"
    assert obs.current == MissionStatus.ARRIVED  # 상태가 안 바뀐다


# 7. 플래그 꺼짐 — 예전 셋은 예전 그대로 동작한다(하위 호환).
def test_flag_off_legacy_statuses_unchanged():
    with en_route(False):
        tracker = MissionTracker()
        for status in ("ARRIVED", "RETURN_COMPLETE", "WEATHER_BLOCKED"):
            assert tracker.observe("robot01", status).accepted is True
        assert tracker.last("robot01") == MissionStatus.WEATHER_BLOCKED


# 8. 로봇별로 상태가 갈린다(셔틀·로봇 여러 대 대비).
def test_state_is_per_robot():
    with en_route(True):
        tracker = MissionTracker()
        tracker.observe("robot01", MissionStatus.ARRIVED)
        obs = tracker.observe("robot02", MissionStatus.ARRIVED)
    assert obs.arrival_edge is True
    assert tracker.last("robot01") == MissionStatus.ARRIVED


# 9. 계약에 없는 전이는 표시만 하고 상태는 받아 적는다(로봇이랑 그림이 안 어긋나게).
def test_unexpected_transition_is_flagged_but_recorded():
    with en_route(True):
        tracker = MissionTracker()
        tracker.observe("robot01", MissionStatus.RETURN_COMPLETE)
        obs = tracker.observe("robot01", MissionStatus.ARRIVED)
    assert obs.unexpected is True
    assert obs.accepted is True
    assert tracker.last("robot01") == MissionStatus.ARRIVED
    assert MissionStatus.ARRIVED not in ALLOWED_TRANSITIONS[MissionStatus.RETURN_COMPLETE]


# 10. mission_status가 없는 프레임(null)은 상태를 안 건드린다.
def test_null_status_is_noop():
    with en_route(True):
        tracker = MissionTracker()
        tracker.observe("robot01", MissionStatus.EN_ROUTE)
        obs = tracker.observe("robot01", None)
    assert obs.accepted is False and obs.reason == "no_status"
    assert tracker.last("robot01") == MissionStatus.EN_ROUTE
