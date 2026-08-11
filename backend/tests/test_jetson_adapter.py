"""젯슨 web_bridge 프레임 어댑터 (S15P11C207-338).

젯슨은 서버 WS 계약과 다른 모양으로 올린다 — type이 `status_summary`·`odom`·`imu`로 갈리고
본문이 `payload`가 아니라 `data`이며 robot_id는 최상위에만 있다. 그래서 어댑터를 붙이기 전엔
젯슨 프레임이 전부 `unknown_frame_type`으로 되돌아갔다.

여기서 못박는 계약은 넷이다.
  1. 세 종류가 각각 서버 `robot_state` payload로 접힌다(값 매핑까지).
  2. 최상위 robot_id가 payload 안으로 복사된다 — 이게 빠지면 `RobotStateIn` 검증에서
     프레임이 통째로 버려진다.
  3. 깨진 프레임은 `error` 봉투로 돌려주되, **세 종류가 `unknown_frame_type`으로 빠지는
     길은 없다.** 그게 이 작업의 목적이라 사유 문자열까지 본다.
  4. 기존 `robot_state` 경로와 모르는 type의 거동은 안 바뀐다(무회귀).

접기 규칙은 순수 함수라 DB를 안 타지만, 프레임을 실제로 넣어 보는 케이스는
`test_robot_channel.py`와 같은 방식으로 매니저에 가짜 소켓을 꽂아 본다(세션 이벤트 루프
하나에서 돌아야 asyncpg 엔진이 안 갈린다).
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import get_session
from app.jetson_adapter import (
    INVALID_JETSON_FRAME,
    JETSON_FRAME_TYPES,
    MAX_TRACKED_ROBOTS,
    adapt_jetson_frame,
    is_jetson_frame,
    reset_jetson_adapter_state,
)
from app.models import Robot
from app.robot_channel import (
    ROBOT_ARRIVAL_ALERT_TYPE,
    get_presentation,
    handle_robot_frame,
)
from app.ws import manager, robot_manager

# 순수 함수 케이스가 섞여 있어 모듈 전체에 asyncio 표를 안 붙인다(동기 함수에 붙으면
# pytest-asyncio가 경고를 낸다). 세션 루프 하나를 같이 쓰는 건 async 케이스만이라
# 표를 별칭으로 묶어 그 케이스에만 단다.
session_loop = pytest.mark.asyncio(loop_scope="session")

ROBOT_ID = "RB1-01"

# ⛔ **"서버가 모르는 상태값" 표본.** 여기 있는 두 시험(F24 계열)이 쓴다.
#
# ⚠ 2026-08-06에 이 자리에서 사고가 났다. 예전 표본이 `EN_ROUTE`였는데 그날 프론트와 젯슨
# 요청으로 **그 값을 enum에 넣었고**, 그러자 못이 아는 값을 재게 돼서 조용히 헛돌았다.
# 그래서 표본을 상수로 뽑고 **enum 안에 들어가면 곧장 터지게** 아래 시험을 같이 건다.
_UNKNOWN_STATUS = "SERVER_DOES_NOT_KNOW_THIS"


def test_unknown_status_sample_is_really_unknown():
    """⛔ 표본이 enum에 들어가면 위 못 둘이 헛돈다 — 그 순간을 여기서 잡는다."""
    from app.schemas import MissionStatus

    assert _UNKNOWN_STATUS not in {s.value for s in MissionStatus}, (
        f"{_UNKNOWN_STATUS}가 계약에 들어왔다 — F24 못 둘이 아는 값을 재고 있다. 표본을 바꿔라"
    )


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


class FakeSocket:
    """매니저에 직접 꽂는 가짜 WebSocket. 받은 메시지만 모아 둔다."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]

    def last(self, type_: str) -> dict[str, Any] | None:
        found = [m for m in self.sent if m.get("type") == type_]
        return found[-1] if found else None


def jetson_frame(type_: str, data: Any, robot_id: str | None = ROBOT_ID, seq: int = 1) -> dict:
    """젯슨 messages.envelope()와 같은 모양(hardware/.../web_bridge/messages.py)."""
    frame: dict[str, Any] = {"type": type_, "timestamp": _now(), "seq": seq, "data": data}
    if robot_id is not None:
        frame["robot_id"] = robot_id
    return frame


def status_data(**overrides: Any) -> dict[str, Any]:
    """젯슨 messages.status_summary()가 싣는 data 그대로."""
    data = {
        "battery": None,
        "operation_mode": "unknown",
        "drive_state": "READY",
        "comm_status": "connected",
        "operation_state": {
            "checkpoint": "UNKNOWN",
            "segment": "UNKNOWN",
            "direction": "UNKNOWN",
            "phase": "UNKNOWN",
            "description": "경로 상태 미연동",
        },
        "camera_status": "unknown",
    }
    data.update(overrides)
    return data


def odom_data(**overrides: Any) -> dict[str, Any]:
    """젯슨 messages.odom_message()가 싣는 data 그대로."""
    data = {
        "frame": "odom",
        "position_type": "relative_estimate",
        "origin": "robot_boot",
        "x": 1.25,
        "y": -3.5,
        "yaw": 0.75,
        "linear_x": 0.2,
        "angular_z": 0.05,
        "valid": True,
    }
    data.update(overrides)
    return data


IMU_DATA = {"yaw_rate": 0.01, "linear_accel_x": 0.3, "valid": True}


def mission_data(status: str = "ARRIVED", **overrides: Any) -> dict[str, Any]:
    """젯슨 mission_status 프레임의 data (계약 jetson_to_web_interface_v2.md §9).

    ⚠ 상태 칸 이름이 서버와 다르다 — 젯슨은 `status`, 서버 payload는 `mission_status`다.
    """
    data = {"status": status, "checkpoint": "C"}
    data.update(overrides)
    return data


@pytest.fixture(autouse=True)
def fresh_adapter_state():
    """어댑터는 로봇별 최신값을 인메모리로 든다(세 프레임을 한 장으로 합치려고).

    케이스 사이에 그 값이 새면 앞 케이스의 위치·배터리가 다음 케이스 payload에 딸려 들어와
    "안 실렸다"를 보는 시험이 통째로 거짓 초록이 된다. 앞뒤로 비운다(검사 위생).
    """
    reset_jetson_adapter_state()
    try:
        yield
    finally:
        reset_jetson_adapter_state()


@pytest_asyncio.fixture(loop_scope="session")
async def dash():
    """대시보드 쪽 수신자. 검사 위생 — 끝나면 반드시 목록에서 뺀다."""
    sock = FakeSocket()
    await manager.connect(sock)
    try:
        yield sock
    finally:
        manager.disconnect(sock)


@pytest_asyncio.fixture(loop_scope="session")
async def robot():
    sock = FakeSocket()
    await robot_manager.connect(sock)
    try:
        yield sock
    finally:
        robot_manager.disconnect(sock)


# ── 순수 함수: 세 종류 접기 ───────────────────────────────────────────────

def test_is_jetson_frame_covers_four_types():
    """⚠ 셋에서 넷으로 늘렸다(F23). mission_status가 빠져 있어서 도착 알림·ETA가 안 살았다."""
    assert JETSON_FRAME_TYPES == {"status_summary", "odom", "imu", "mission_status"}
    for type_ in JETSON_FRAME_TYPES:
        assert is_jetson_frame(jetson_frame(type_, {}))
    assert not is_jetson_frame({"type": "robot_state", "data": {}})
    assert not is_jetson_frame("이건 dict가 아니다")


def test_status_summary_folds_into_payload():
    result = adapt_jetson_frame(jetson_frame("status_summary", status_data(battery=77)))
    assert result.error is None
    payload = result.payload

    # 최상위 robot_id가 payload 안으로 복사된다(서버 필수 칸).
    assert payload["robot_id"] == ROBOT_ID
    assert payload["status_summary"]["battery"] == 77
    # connected → WS_OK. 젯슨 2단을 서버 3단 enum에 맞춘다.
    assert payload["status_summary"]["comm_status"] == "WS_OK"
    # 서버 스키마에 자리 없는 칸은 sensor_health로 흐른다.
    health = payload["sensor_health"]
    assert health["drive_state"] == "READY"
    assert health["camera_status"] == "unknown"
    assert health["operation_state"]["description"] == "경로 상태 미연동"
    assert health["source"] == "jetson"
    assert health["frame_type"] == "status_summary"
    assert health["seq"] == 1


def test_jetson_comm_status_no_longer_becomes_server_down():
    """⛔ **계약이 2026-08-06에 바뀌었다.** 예전에는 젯슨 `disconnected`를 `SERVER_DOWN`으로
    접었는데, 그 둘은 뜻이 달랐다 — 젯슨은 STM32 시리얼 링크, 서버는 로봇↔서버 통신이다.

    그대로 받던 동안 **UART 케이블이 빠지면 화면이 "통신 정지"라 말하고 명령 버튼까지
    잠겼다.** 자세한 것과 나머지 못은 `tests/test_comm_status_meaning.py`에 있다.
    """
    result = adapt_jetson_frame(
        jetson_frame("status_summary", status_data(comm_status="disconnected"))
    )
    assert result.payload["status_summary"]["comm_status"] == "WS_OK"
    assert result.payload["sensor_health"]["stm32_link"] == "disconnected"


def test_unknown_operation_mode_is_not_reported():
    """젯슨 operation_mode 기본값은 자유 문자열 'unknown'이다.

    status_summary는 절대 상태라 **안 실린 칸은 '보고 안 함'**이다. enum에 없는 값을 억지로
    실으면 pydantic이 프레임 전체를 거절해 배터리·통신까지 같이 버려진다. 그래서 mode는
    빼고, 원본은 sensor_health에 남겨 화면에서 되짚을 수 있게 한다.
    """
    result = adapt_jetson_frame(jetson_frame("status_summary", status_data()))
    assert "mode" not in result.payload["status_summary"]
    assert result.payload["sensor_health"]["operation_mode_raw"] == "unknown"

    known = adapt_jetson_frame(
        jetson_frame("status_summary", status_data(operation_mode="outdoor_tagging"))
    )
    assert known.payload["status_summary"]["mode"] == "OUTDOOR_TAGGING"
    assert "operation_mode_raw" not in known.payload["sensor_health"]


def test_battery_out_of_contract_is_absorbed():
    """서버 battery는 int 0~100이다. 소수점·범위 밖이 오면 프레임 전체가 깨지면 안 된다.

    ⚠ 로봇을 하나씩 갈라 본다. 어댑터는 로봇별 최신값을 누적해서, 같은 로봇으로 이어 보내면
    앞 프레임의 배터리가 그대로 남는다(그게 절대 상태 규칙이고 아래 시험이 따로 못박는다).
    """
    rounded = adapt_jetson_frame(
        jetson_frame("status_summary", status_data(battery=87.6), robot_id="RB-round")
    )
    assert rounded.payload["status_summary"]["battery"] == 88

    over = adapt_jetson_frame(
        jetson_frame("status_summary", status_data(battery=140), robot_id="RB-over")
    )
    assert "battery" not in over.payload.get("status_summary", {})
    assert over.payload["sensor_health"]["battery_raw"] == 140

    # 젯슨이 지금 실제로 보내는 값(STM32 연동 전)은 None이다 — raw로도 안 남긴다.
    empty = adapt_jetson_frame(
        jetson_frame("status_summary", status_data(), robot_id="RB-empty")
    )
    assert "battery" not in empty.payload.get("status_summary", {})
    assert "battery_raw" not in empty.payload["sensor_health"]


def test_unmappable_value_does_not_erase_last_known_state():
    """못 읽는 값이 왔다고 직전 보고를 지우면 안 된다(status_summary는 절대 상태).

    배터리가 한 번 왔다가 다음 프레임에서 범위 밖으로 오면 "모른다"지 "0%"가 아니다.
    """
    adapt_jetson_frame(jetson_frame("status_summary", status_data(battery=64)))
    later = adapt_jetson_frame(jetson_frame("status_summary", status_data(battery=140)))
    assert later.payload["status_summary"]["battery"] == 64
    assert later.payload["sensor_health"]["battery_raw"] == 140


def test_odom_folds_into_odom_and_position():
    """odom은 개발자 칸(payload.odom)과 지도 칸(status_summary.position) 둘 다로 접힌다.

    서버가 지도에 점을 찍는 값은 status_summary.position 하나뿐이라, odom만 실으면 개발자
    화면엔 뜨는데 관제 지도엔 로봇이 안 나타난다.
    """
    result = adapt_jetson_frame(jetson_frame("odom", odom_data()))
    assert result.error is None
    odom = result.payload["odom"]
    # 젯슨 원본 이름은 그대로 남는다(개발자 화면 원문 대조).
    for key, value in odom_data().items():
        assert odom[key] == value
    position = result.payload["status_summary"]["position"]
    assert position == {"x": 1.25, "y": -3.5, "theta": 0.75}
    assert result.payload["robot_id"] == ROBOT_ID


def test_odom_carries_frontend_field_names_too():
    """odom에 theta·linear_vel·angular_vel 별칭을 같이 싣는다.

    젯슨은 같은 값을 yaw·linear_x·angular_z로 부른다(`messages.odom_message`). 예전 React
    화면이 theta 계열 이름으로 읽어서 별칭을 붙였다.

    ⚠ 2026-08-04 기준 이 별칭을 읽는 소비자가 0건이다 — 새 관제 화면(`frontend/index.html`)은
    젯슨 이름(linear_x·angular_z)을 그대로 읽고, 개발자 페이지도 별칭을 안 쓴다. 계약은
    유지하되(빼면 옛 소비자가 생겼을 때 조용히 0이 뜬다) 지우려면 소비자 0건을 먼저 다시
    재라.
    """
    odom = adapt_jetson_frame(jetson_frame("odom", odom_data())).payload["odom"]
    assert (odom["theta"], odom["linear_vel"], odom["angular_vel"]) == (0.75, 0.2, 0.05)

    imu = adapt_jetson_frame(jetson_frame("imu", IMU_DATA)).payload["imu"]
    # 젯슨 yaw_rate는 각속도라 화면 imu.yaw(방위각)에 못 붙인다 — 가속도 칸만 짝이 맞다.
    assert imu["acc_x"] == 0.3
    assert "yaw" not in imu


def test_invalid_odom_keeps_raw_but_drops_position():
    """valid=false 좌표는 지도에 안 찍는다. 위치는 인메모리 최신값이라 한 번 박히면 남는다."""
    result = adapt_jetson_frame(jetson_frame("odom", odom_data(valid=False)))
    assert result.payload["odom"]["valid"] is False       # 개발자 화면엔 그대로 남는다
    assert "status_summary" not in result.payload

    missing = adapt_jetson_frame(jetson_frame("odom", odom_data(x=None)))
    assert "status_summary" not in missing.payload


def test_imu_folds_into_payload():
    result = adapt_jetson_frame(jetson_frame("imu", IMU_DATA))
    assert result.error is None
    for key, value in IMU_DATA.items():
        assert result.payload["imu"][key] == value
    assert result.payload["robot_id"] == ROBOT_ID


def test_adapted_payloads_match_robot_state_json_schema():
    """접은 payload가 정본 `schemas/robot_state.schema.json`을 통과해야 한다.

    pydantic `RobotStateIn`은 그 파일의 사본이고 둘이 어긋나면 **JSON Schema 파일을 따른다**
    (프론트 TS 타입도 같은 파일을 본다). pydantic만 보면 못 잡는 어긋남이 실제로 있다 —
    `position.theta`가 정본에선 `"type": "number"`라 null이 안 되는데 pydantic은
    `float | None`이라 조용히 통과한다. 명령 쪽(test_robot_channel의
    `test_command_envelope_matches_json_schema`)과 같은 잣대를 수신 쪽에도 세운다.
    """
    import json
    import pathlib

    import jsonschema

    schema = json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "schemas" / "robot_state.schema.json")
        .read_text(encoding="utf-8")
    )
    # 로봇을 갈라 본다 — 같은 로봇으로 이으면 앞 프레임 값이 누적돼서 "칸이 빈 판"을 못 만든다.
    frames = [
        jetson_frame("status_summary", status_data(battery=55, operation_mode="MANUAL")),
        jetson_frame("status_summary", status_data(), robot_id="RB-blank"),
        jetson_frame("odom", odom_data(), robot_id="RB-odom"),
        jetson_frame("odom", odom_data(yaw=None), robot_id="RB-noyaw"),  # theta를 못 읽는 판
        jetson_frame("imu", IMU_DATA, robot_id="RB-imu"),
    ]
    for frame in frames:
        result = adapt_jetson_frame(frame)
        assert result.error is None, frame["type"]
        jsonschema.validate(result.payload, schema)


def test_broken_frames_are_rejected():
    no_id = adapt_jetson_frame(jetson_frame("status_summary", status_data(), robot_id=None))
    assert no_id.payload is None
    assert no_id.error["reason"] == INVALID_JETSON_FRAME

    blank_id = adapt_jetson_frame(jetson_frame("odom", odom_data(), robot_id="   "))
    assert blank_id.error["reason"] == INVALID_JETSON_FRAME

    bad_data = adapt_jetson_frame(jetson_frame("imu", "data가 객체가 아니다"))
    assert bad_data.error["reason"] == INVALID_JETSON_FRAME
    assert bad_data.error["type"] == "imu"


# ── 채널 관통: 프레임이 실제로 안 버려지나 ────────────────────────────────

@session_loop
async def test_jetson_status_summary_reaches_dashboard_and_row(dash, robot):
    await handle_robot_frame(
        robot, jetson_frame("status_summary", status_data(battery=64))
    )

    # 로봇에게는 error가 아니라 state_ack가 간다.
    assert robot.last("state_ack") is not None, robot.types()
    assert "error" not in robot.types()

    relayed = dash.last("robot_state")
    assert relayed is not None, dash.types()
    assert relayed["robot_id"] == ROBOT_ID
    assert relayed["data"]["status_summary"]["comm_status"] == "WS_OK"
    assert relayed["data"]["sensor_health"]["drive_state"] == "READY"

    async with get_session() as s:
        row = (
            await s.execute(select(Robot).where(Robot.name == ROBOT_ID))
        ).scalars().one()
        assert row.battery == 64
        assert row.network_status == "WS_OK"


@session_loop
async def test_jetson_odom_puts_robot_on_the_map(dash, robot):
    await handle_robot_frame(robot, jetson_frame("odom", odom_data()))

    assert robot.last("state_ack") is not None, robot.types()
    card = dash.last("robot_status")
    assert card is not None, dash.types()
    # 지도 점은 카드의 position에서 나온다.
    assert card["data"]["position"] == {"x": 1.25, "y": -3.5, "theta": 0.75}
    assert dash.last("robot_state")["data"]["odom"]["frame"] == "odom"


@session_loop
async def test_jetson_imu_is_not_dropped(dash, robot):
    await handle_robot_frame(robot, jetson_frame("imu", IMU_DATA))

    assert robot.last("state_ack") is not None, robot.types()
    relayed_imu = dash.last("robot_state")["data"]["imu"]
    for key, value in IMU_DATA.items():
        assert relayed_imu[key] == value


@session_loop
async def test_four_jetson_types_never_hit_unknown_frame_type(dash, robot):
    """이 시험이 이번 작업의 본체다 — 네 종류가 다시 버려지면 여기가 빨개진다."""
    for type_, data in (
        ("status_summary", status_data()),
        ("odom", odom_data()),
        ("imu", IMU_DATA),
        ("mission_status", mission_data()),
    ):
        await handle_robot_frame(robot, jetson_frame(type_, data))

    reasons = [m["data"].get("reason") for m in robot.sent if m["type"] == "error"]
    assert reasons == [], reasons
    assert robot.types().count("state_ack") == 4


# ── mission_status 접기 (F23) · 모르는 값 완화 (F24) ──────────────────────
# 젯슨 계약(`hardware/jetson/ros2_ws/docs/jetson_to_web_interface_v2.md` §9)에는 처음부터
# 있던 프레임인데 JETSON_FRAME_TYPES에 없어서 unknown_frame_type으로 되돌아갔다. 그래서
# 도착 알림도 ETA도 영영 안 살았다.


def test_mission_status_folds_into_payload():
    """젯슨 `data.status` → 서버 `payload.mission_status`. 칸 이름이 갈리는 경계다."""
    for value in ("ARRIVED", "WEATHER_BLOCKED", "RETURN_COMPLETE"):
        result = adapt_jetson_frame(jetson_frame("mission_status", mission_data(value)))
        assert result.error is None, result.error
        assert result.payload["mission_status"] == value


def test_mission_status_is_not_carried_into_other_frames():
    """다른 셋과 반대로 **누적 안 한다** — 절대 상태가 아니라 에지라서 그렇다.

    odom·imu 프레임마다 마지막 ARRIVED를 다시 실으면 에지 판정 질의가 프레임마다 한 번씩
    더 돈다. 안 싣는다고 값이 지워지지도 않는다("안 실린 칸은 보고 안 함").
    """
    adapt_jetson_frame(jetson_frame("mission_status", mission_data("ARRIVED")))
    later = adapt_jetson_frame(jetson_frame("odom", odom_data()))
    assert "mission_status" not in later.payload, later.payload


def test_unknown_mission_status_keeps_the_rest_of_the_state():
    """⭐ F24 — 값 하나가 배터리·위치까지 통째로 못 버린다.

    안 걸러 내면 `RobotStateIn` 검증이 프레임을 통째로 거절해서 같이 실린 상태가 다 사라진다.

    ⚠ **표본이 2026-08-06에 바뀌었다.** 예전에는 `EN_ROUTE`를 "모르는 값"으로 썼는데 그날
    프론트와 젯슨 요청으로 **그 값을 enum에 넣었다.** 그러자 이 못이 아는 값을 재게 돼서
    조용히 헛돌았다 — 전량에서 빨개져 잡혔다. 표본은 **enum 밖인 게 확실한 글자**여야 한다
    ([[feedback_boundary_test_imports_constant]] 계열).
    """
    adapt_jetson_frame(jetson_frame("status_summary", status_data(battery=64)))
    adapt_jetson_frame(jetson_frame("odom", odom_data()))

    result = adapt_jetson_frame(jetson_frame("mission_status", mission_data(_UNKNOWN_STATUS)))
    assert result.error is None, result.error
    # 모르는 값은 그 칸만 빠진다.
    assert "mission_status" not in result.payload, result.payload
    # 나머지 상태는 그대로 살아 올라간다.
    assert result.payload["status_summary"]["battery"] == 64
    assert result.payload["status_summary"]["position"]["x"] == 1.25


@session_loop
async def test_mission_status_frame_reaches_server_state(dash, robot):
    """접히는 것만으로는 안 된다 — 도착 판정·중계·화면 값까지 닿아야 F23이 산 거다."""
    await handle_robot_frame(robot, jetson_frame("status_summary", status_data()))
    await handle_robot_frame(robot, jetson_frame("mission_status", mission_data("ARRIVED")))

    ack = robot.last("state_ack")
    assert ack is not None, robot.types()
    # 도착 에지가 실제로 잡혀 알림이 났다 = 서버 안쪽 상태에 닿았다.
    assert ack["data"]["notices"] == [ROBOT_ARRIVAL_ALERT_TYPE], ack
    assert dash.last(ROBOT_ARRIVAL_ALERT_TYPE) is not None, dash.types()
    assert dash.last("robot_state")["data"]["mission_status"] == "ARRIVED"
    # 화면이 새로고침 뒤에 읽는 자리(RobotOut.mission_status)까지 찬다.
    assert get_presentation(ROBOT_ID).mission_status == "ARRIVED"


@session_loop
async def test_unknown_mission_status_frame_is_not_dropped_on_the_channel(dash, robot):
    """F24를 채널 관통으로 다시 잰다 — 어댑터 출력만 보면 못 잡히는 갈래가 있었다(결함 2)."""
    await handle_robot_frame(robot, jetson_frame("status_summary", status_data(battery=42)))
    dash.sent.clear()
    robot.sent.clear()

    await handle_robot_frame(
        robot, jetson_frame("mission_status", mission_data(_UNKNOWN_STATUS))
    )

    assert robot.last("error") is None, robot.types()
    assert robot.last("state_ack") is not None, robot.types()
    relayed = dash.last("robot_state")["data"]
    assert relayed["mission_status"] is None, relayed
    assert relayed["status_summary"]["battery"] == 42, relayed


@session_loop
async def test_broken_jetson_frame_answers_error_not_unknown_type(dash, robot):
    """깨진 젯슨 프레임도 연결을 안 끊고 error로 답한다. 사유는 unknown_frame_type이 아니다."""
    await handle_robot_frame(
        robot, jetson_frame("status_summary", status_data(), robot_id=None)
    )
    err = robot.last("error")
    assert err is not None, robot.types()
    # 로봇 전선 봉투의 본문 칸은 `data`다(젯슨 공통 envelope · 2026-08-01 확정).
    assert err["data"]["reason"] == INVALID_JETSON_FRAME
    assert dash.last("robot_state") is None


# ── 무회귀: 기존 경로 ─────────────────────────────────────────────────────

@session_loop
async def test_existing_robot_state_path_unchanged(dash, robot):
    """어댑터를 붙였다고 기존 robot_state 프레임의 거동이 달라지면 안 된다."""
    await handle_robot_frame(
        robot,
        {
            "type": "robot_state",
            "version": "1",
            "robot_id": "jetson01",
            "timestamp": _now(),
            "data": {
                "robot_id": "jetson01",
                "status_summary": {
                    "battery": 80,
                    "mode": "INDOOR_TAGGING",
                    "comm_status": "POLLING_GRACE",
                    "position": {"x": 5.0, "y": 6.0, "theta": 0.0},
                },
            },
        },
    )
    assert robot.last("state_ack") is not None, robot.types()
    relayed = dash.last("robot_state")
    assert relayed["data"]["status_summary"]["battery"] == 80
    assert relayed["data"]["status_summary"]["position"]["x"] == 5.0

    async with get_session() as s:
        row = (
            await s.execute(select(Robot).where(Robot.name == "jetson01"))
        ).scalars().one()
        assert row.mode == "INDOOR_TAGGING"
        assert row.network_status == "POLLING_GRACE"


@session_loop
async def test_unknown_type_still_answers_unknown_frame_type(dash, robot):
    await handle_robot_frame(robot, {"type": "no_such_type", "data": {}})
    assert robot.last("error")["data"]["reason"] == "unknown_frame_type"

    await handle_robot_frame(robot, 42)
    assert robot.last("error")["data"]["reason"] == "frame_not_object"


# ── 교차 검증(2026-07-31)에서 잡힌 결함의 재발 방지 ────────────────────────
# 아래 케이스는 하나하나가 실측으로 재현된 결함 하나를 붙잡는다. 시험 이름 뒤 번호가
# 검증 보고의 결함 번호다. 어댑터 출력만 보면 못 잡히는 갈래가 많아서(결함 2가 그랬다)
# 채널을 관통시켜 **브로드캐스트에 실제로 실린 값**으로 판정하는 케이스를 같이 둔다.


def robot_state_schema() -> dict:
    """정본 `schemas/robot_state.schema.json`. 프론트 TS 타입도 같은 파일을 본다."""
    import json
    import pathlib

    return json.loads(
        (pathlib.Path(__file__).resolve().parents[1] / "schemas" / "robot_state.schema.json")
        .read_text(encoding="utf-8")
    )


@session_loop
async def test_frame_arrival_counts_as_heartbeat_1(robot):
    """[결함 1] 프레임이 올라오는 커넥션은 pong이 없어도 살아 있다.

    젯슨 브리지는 소켓을 **읽지 않아서**(`web_bridge_node.WebSocketClient._connect_forever`에
    recv가 없다) 서버 ping에 답을 못 한다. pong만 세면 15초 × 미스 2번마다 `drop_stale`이
    1001로 끊어 재접속 루프가 돈다 — 어댑터가 프레임을 받아 준다는 목표가 45초 창에서만
    성립하는 자리였다.
    """
    assert robot_manager.record_ping(robot) == 1
    assert robot_manager.record_ping(robot) == 2      # 한 번 더 쌓이면 스테일로 끊긴다

    await handle_robot_frame(robot, jetson_frame("odom", odom_data()))
    assert robot_manager.record_ping(robot) == 1      # 상태 프레임이 미스를 되돌렸다

    # 모양이 깨진 프레임도 "소켓은 살아 있다"는 증거다.
    robot_manager.record_ping(robot)
    await handle_robot_frame(robot, jetson_frame("imu", IMU_DATA, robot_id=None))
    assert robot.last("error")["data"]["reason"] == INVALID_JETSON_FRAME
    assert robot_manager.record_ping(robot) == 1


@session_loop
async def test_broadcast_payload_matches_json_schema_2(dash, robot):
    """[결함 2] 어댑터 출력이 아니라 **중계된 payload**가 정본 스키마를 지켜야 한다.

    어댑터가 theta를 안 실어도 중계는 `state.model_dump(mode="json")`을 타서 pydantic이
    `theta: null`을 도로 채웠다(정본은 `"type": "number"`라 null 불가). 실측으로 어댑터 출력은
    PASS, 브로드캐스트는 FAIL("None is not of type 'number'")이었다 — 어댑터 출력만 보는
    시험으로는 구조적으로 못 잡는다.
    """
    import json

    import jsonschema

    schema = robot_state_schema()
    frames = [
        jetson_frame("odom", odom_data(yaw=None)),          # theta를 못 읽는 판
        jetson_frame("status_summary", status_data(battery=30)),
        jetson_frame("imu", IMU_DATA),
        jetson_frame("odom", odom_data()),
    ]
    for frame in frames:
        await handle_robot_frame(robot, frame)
        payload = dash.last("robot_state")["data"]
        jsonschema.validate(payload, schema)             # 계약 대조
        json.dumps(payload, allow_nan=False)             # 직렬화도 실제로 되나

    # 첫 판(yaw 없음)에서 지도 칸이 어떻게 나갔나를 따로 못박는다.
    first = dash.sent[0]["data"] if dash.sent[0]["type"] == "robot_state" else None
    assert first is not None
    assert "theta" not in first["status_summary"]["position"]

    # 카드(robot_status)에도 null 좌표가 안 실린다 — 같은 값이 인메모리 최신값으로 흐른다.
    card_position = dash.last("robot_status")["data"]["position"]
    assert None not in card_position.values()


@session_loop
async def test_split_frames_merge_into_one_state_3(dash, robot):
    """[결함 3] odom·imu가 서로를 지우면 안 된다.

    화면(`dataSlice.upsertTelemetry`)은 받은 프레임으로 로봇 칸을 통째로 갈아끼운다. 젯슨이
    odom·imu를 각자 1Hz로 따로 보내니, 접은 프레임을 그대로 올리면 odom이 IMU를 0으로,
    imu가 위치를 (0,0)으로 매초 번갈아 지웠다. 어댑터가 한 상태로 합쳐서 보내야 한다.
    """
    await handle_robot_frame(robot, jetson_frame("status_summary", status_data(battery=40)))
    await handle_robot_frame(robot, jetson_frame("odom", odom_data(), seq=2))
    await handle_robot_frame(robot, jetson_frame("imu", IMU_DATA, seq=3))
    # 젯슨 1Hz 두 바퀴 — 어느 프레임 뒤에 화면이 그려도 값이 다 살아 있어야 한다.
    for seq in (4, 5, 6, 7):
        type_, data = ("odom", odom_data()) if seq % 2 == 0 else ("imu", IMU_DATA)
        await handle_robot_frame(robot, jetson_frame(type_, data, seq=seq))
        payload = dash.last("robot_state")["data"]
        assert payload["odom"]["x"] == 1.25, f"seq {seq}에서 odom이 지워졌다"
        assert payload["imu"]["yaw_rate"] == 0.01, f"seq {seq}에서 imu가 지워졌다"
        assert payload["status_summary"]["position"]["x"] == 1.25
        assert payload["status_summary"]["battery"] == 40


@session_loop
async def test_status_summary_frame_carries_telemetry_fields_4(dash, robot):
    """[결함 4] comm_status가 실린 유일한 프레임이 텔레메트리에 닿아야 한다.

    화면 `robotStateToTelemetry`는 odom·imu·position이 하나도 없으면 **null을 돌려준다**.
    status_summary 프레임엔 그 셋이 없어서, comm_status를 실은 프레임만 통째로 버려지고
    신호 세기가 0%로 고정됐다. 반대로 odom·imu 프레임엔 comm_status가 없었다.
    """
    await handle_robot_frame(robot, jetson_frame("odom", odom_data()))
    await handle_robot_frame(robot, jetson_frame("imu", IMU_DATA))
    await handle_robot_frame(robot, jetson_frame("status_summary", status_data(battery=40)))

    payload = dash.last("robot_state")["data"]
    # 텔레메트리를 만드는 조건(odom·imu·position 중 하나라도 있나)을 status_summary 판이 넘는다.
    assert payload["odom"] and payload["imu"] and payload["status_summary"]["position"]
    assert payload["status_summary"]["comm_status"] == "WS_OK"


def test_reboot_does_not_teleport_robot_to_origin_5():
    """[결함 5] 젯슨 좌표는 부팅 자리가 (0,0)인 상대값이다.

    재부팅하면 x·y가 0으로 되돌아가 지도의 로봇이 원점으로 순간이동했다. 부팅 세션이 바뀐 걸
    seq 되돌림으로 알아채고, 그 순간의 지도 좌표를 새 원점으로 잡는다.
    """
    before = adapt_jetson_frame(
        jetson_frame("odom", odom_data(x=10.0, y=5.0, yaw=0.5), seq=40)
    ).payload["status_summary"]["position"]
    assert (before["x"], before["y"]) == (10.0, 5.0)

    # 재부팅 — seq가 0으로 돌아가고 좌표도 원점부터 다시 센다.
    rebooted = adapt_jetson_frame(
        jetson_frame("odom", odom_data(x=0.0, y=0.0, yaw=0.0), seq=0)
    ).payload["status_summary"]["position"]
    assert (rebooted["x"], rebooted["y"]) == (10.0, 5.0)
    assert rebooted["theta"] == pytest.approx(0.5)

    moved = adapt_jetson_frame(
        jetson_frame("odom", odom_data(x=1.0, y=0.0, yaw=0.0), seq=1)
    )
    assert (moved.payload["status_summary"]["position"]["x"],
            moved.payload["status_summary"]["position"]["y"]) == (11.0, 5.0)
    # 원본은 젯슨이 보낸 값 그대로다(보정은 지도 칸에만 건다).
    assert moved.payload["odom"]["x"] == 1.0


def test_absolute_frames_are_not_reanchored_5():
    """[결함 5] 젯슨이 공통 원점을 쓴다고 밝히면(position_type이 relative가 아님) 보정을 안 건다."""
    absolute = odom_data(position_type="absolute_map", origin="site_survey")
    adapt_jetson_frame(jetson_frame("odom", dict(absolute, x=10.0, y=5.0), seq=40))
    after = adapt_jetson_frame(
        jetson_frame("odom", dict(absolute, x=0.0, y=0.0), seq=0)
    ).payload["status_summary"]["position"]
    assert (after["x"], after["y"]) == (0.0, 0.0)


@session_loop
async def test_odom_imu_frame_updates_row_with_real_state_6(dash, robot):
    """[결함 6] 상태값이 하나도 없는 프레임이 `updated_at`만 밀면 "마지막 상태 보고 시각"이 흐려진다.

    합쳐 보내면 imu 프레임 한 장도 그 로봇의 상태 한 벌을 실제로 보고한다 — 카드·행이
    빈 값으로 덮이지 않고 그대로 유지되는지까지 본다.
    """
    await handle_robot_frame(robot, jetson_frame("status_summary", status_data(battery=52)))
    await handle_robot_frame(robot, jetson_frame("odom", odom_data()))
    await handle_robot_frame(robot, jetson_frame("imu", IMU_DATA))

    card = dash.last("robot_status")["data"]
    assert card["battery"] == 52
    assert card["network_status"] == "WS_OK"
    assert card["position"]["x"] == 1.25
    assert card["updated_at"] is not None

    async with get_session() as s:
        row = (await s.execute(select(Robot).where(Robot.name == ROBOT_ID))).scalars().one()
        assert (row.battery, row.network_status) == (52, "WS_OK")


def test_robot_id_is_stored_trimmed_7():
    """[결함 7] robot_id를 `strip()`으로 검사만 하고 원본을 실으면 행이 갈라진다."""
    result = adapt_jetson_frame(jetson_frame("imu", IMU_DATA, robot_id="  RB1-01  "))
    assert result.payload["robot_id"] == "RB1-01"


@session_loop
async def test_padded_robot_id_does_not_create_second_row_7(dash, robot):
    await handle_robot_frame(robot, jetson_frame("status_summary", status_data(battery=51)))
    await handle_robot_frame(
        robot,
        jetson_frame("status_summary", status_data(battery=53), robot_id=f"  {ROBOT_ID}  "),
    )
    async with get_session() as s:
        rows = (
            await s.execute(select(Robot).where(Robot.name.like(f"%{ROBOT_ID}%")))
        ).scalars().all()
    assert [r.name for r in rows] == [ROBOT_ID]
    assert rows[0].battery == 53


def test_unknown_keys_are_never_relayed_8():
    """[결함 8] 젯슨 원본 dict를 그대로 중계하면 보낸 쪽이 정한 칸이 대시보드 전원에게 간다.

    `/ws/robot`은 `ws_require_api_key`가 기본 꺼짐이라 인증 없는 peer도 같은 길을 탄다.
    실측으로 `odom.robot_id="EVIL"`이 화면까지 살아 나갔다 — 계약 칸만 다시 조립한다.
    """
    evil = odom_data(robot_id="EVIL", type="command", payload={"cmd": "stop"}, note="긴 " * 200)
    odom = adapt_jetson_frame(jetson_frame("odom", evil)).payload["odom"]
    assert {"robot_id", "type", "payload", "note"}.isdisjoint(odom)

    imu = adapt_jetson_frame(
        jetson_frame("imu", dict(IMU_DATA, robot_id="EVIL", script="<img onerror=1>"))
    ).payload["imu"]
    assert set(imu) <= {"yaw_rate", "linear_accel_x", "valid", "acc_x"}

    # 자유 문자열 칸은 길이를 자른다(화면 카드 한 줄 폭).
    long_text = adapt_jetson_frame(
        jetson_frame("odom", odom_data(frame="f" * 300), robot_id="RB-long")
    ).payload["odom"]
    assert len(long_text["frame"]) == 64

    # 중첩 dict(operation_state)도 계약 칸만 남는다.
    health = adapt_jetson_frame(
        jetson_frame(
            "status_summary",
            status_data(operation_state={"checkpoint": "A", "evil": "x"}, drive_state=["배열"]),
            robot_id="RB-nested",
        )
    ).payload["sensor_health"]
    assert health["operation_state"] == {"checkpoint": "A"}
    assert "drive_state" not in health


def test_mission_reason_keeps_longer_text_than_card_columns():
    """주행 실패 사유는 화면 카드 폭(64)이 아니라 되짚기 상한(200)으로 자른다.

    ⭐ **왜 갈랐나** — 젯슨이 2026-08-07부터 사유를 문장으로 조립해 보낸다
    ("태그를 한 번도 못 봤다 (단계 3/5 태그 4 코너 진입, 목표 태그 4, 주행 2.42 m)" = 53자).
    64로 자르면 태그 번호가 두 자리만 돼도 뒤가 소리 없이 날아가는데, 이 값은 화면 카드가
    아니라 `robot_status_log.log`(0017 · `sa.Text`)로 가서 나중에 되짚는 기록이다.

    ⛔ **`_TEXT_MAX_LEN`을 통째로 올려 맞추지 마라.** 저쪽은 `odom`의 `frame`처럼 카드에 한 줄로
    그려지는 칸이 쓴다 — 실제로 그렇게 고쳤다가 위 결함 8 시험이 잡았다. 아래 두 줄이 그
    **갈림**을 지킨다. 숫자를 상수에서 import해 오면 상한이 0이 돼도 통과하니 그대로 적는다.
    """
    real_example = "태그를 한 번도 못 봤다 (단계 3/5 태그 4 코너 진입, 목표 태그 4, 주행 2.42 m)"
    kept = adapt_jetson_frame(
        jetson_frame("mission_status", mission_data("FAILED", reason=real_example), robot_id="RB-r1")
    ).payload["log"]
    assert kept == real_example, "실제 젯슨 사유가 잘리면 안 된다"

    clipped = adapt_jetson_frame(
        jetson_frame("mission_status", mission_data("FAILED", reason="가" * 300), robot_id="RB-r2")
    ).payload["log"]
    assert len(clipped) == 200

    # ⚠ 짝 — 같은 이름이지만 `odom`의 `reason`은 카드 폭 그대로다. 한쪽만 재면 갈림이 무너진다.
    odom_reason = adapt_jetson_frame(
        jetson_frame("odom", odom_data(reason="가" * 300), robot_id="RB-r3")
    ).payload["odom"]
    assert len(odom_reason["reason"]) == 64


@session_loop
async def test_injected_keys_do_not_reach_dashboard_8(dash, robot):
    await handle_robot_frame(
        robot, jetson_frame("odom", odom_data(robot_id="EVIL", mission_status="ARRIVED"))
    )
    relayed = dash.last("robot_state")
    assert relayed["robot_id"] == ROBOT_ID
    assert "EVIL" not in str(relayed)
    assert relayed["data"]["mission_status"] is None   # 알림 위조 경로도 안 열린다


def test_bool_and_nonfinite_numbers_are_blocked_9():
    """[결함 9] docstring이 주장하던 bool·NaN 차단에 케이스가 없었다.

    `isinstance(True, int)`가 참이라 bool은 좌표 자리에 1.0으로 들어앉고, NaN·inf는
    브로드캐스트 JSON 직렬화를 통째로 터뜨린다.
    """
    import json

    flags = adapt_jetson_frame(jetson_frame("odom", odom_data(x=True, y=False)))
    assert "status_summary" not in flags.payload      # 지도에 안 찍는다
    assert "x" not in flags.payload["odom"] and "y" not in flags.payload["odom"]

    broken = adapt_jetson_frame(
        jetson_frame("odom", odom_data(x=float("nan"), y=float("inf")), robot_id="RB-nan")
    )
    assert "status_summary" not in broken.payload
    json.dumps(broken.payload, allow_nan=False)       # 중계가 안 터진다

    imu = adapt_jetson_frame(
        jetson_frame("imu", {"yaw_rate": float("nan"), "linear_accel_x": 0.3, "valid": True},
                     robot_id="RB-nan-imu")
    ).payload["imu"]
    assert "yaw_rate" not in imu and imu["linear_accel_x"] == 0.3

    battery = adapt_jetson_frame(
        jetson_frame("status_summary", status_data(battery=True), robot_id="RB-bool")
    ).payload
    assert "battery" not in battery.get("status_summary", {})


@session_loop
async def test_valid_flag_reaches_developer_page_9(dash, robot):
    """[결함 9] "valid 여부가 개발자 화면에 보인다"는 주장의 근거.

    보는 자리는 React 관제 화면이 아니라 개발자 텔레메트리 페이지
    `app/static/telemetry.html`이다 — 행마다 접힌 토글이 메시지 한 겹을 JSON 원문으로
    통째로 찍는다(`rawText`). 그 원문에 실려 나가는지를 여기서 못박는다.
    """
    await handle_robot_frame(robot, jetson_frame("odom", odom_data(valid=False)))
    payload = dash.last("robot_state")["data"]
    assert payload["odom"]["valid"] is False
    # 지도 칸은 안 채운다(못 믿을 좌표는 아예 안 싣는다).
    assert payload["status_summary"] is None


def test_tracked_robot_states_are_capped():
    """인증 없는 peer가 robot_id를 바꿔 가며 올려도 누적 상태가 무한히 안 자란다."""
    from app.jetson_adapter import _states

    for i in range(MAX_TRACKED_ROBOTS + 5):
        adapt_jetson_frame(jetson_frame("imu", IMU_DATA, robot_id=f"flood-{i}"))
    assert len(_states) <= MAX_TRACKED_ROBOTS
