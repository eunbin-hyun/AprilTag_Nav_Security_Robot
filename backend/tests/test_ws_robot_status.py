"""로봇 상태 DB 반영을 대시보드에 알리는 `robot_status` 메시지 (S15P11C207-81).

`robot_state`와 헷갈리면 안 된다. 둘은 같은 사건에서 나오지만 싣는 게 다르다.

- `robot_state` — 로봇이 올린 프레임 원본 중계다(odom·imu·sensor_health·position·led 포함).
  DB에 안 남는 값까지 그대로 흐른다.
- `robot_status` — 그 보고로 **로봇 카드가 어떻게 됐나**다. 칸이 스냅샷 `robots[]` 한 줄과
  똑같아서(`id · name · mode · battery · network_status · updated_at · position · led_status ·
  mission_status`) 화면이 로봇 카드를 스냅샷 다시 읽지 않고 그 자리에서 갈아끼울 수 있다.
  앞 여섯은 DB 행 값이고 뒤 셋은 인메모리 최신값이다(tests/test_robot_presentation.py).

앞엣것만 있으면 화면이 원본 프레임에서 카드 값을 스스로 뽑아야 하는데, 그 규칙(안 실린
필드는 안 덮는다)이 서버에 이미 있어서 두 벌이 갈라진다.

여기 케이스는 가짜 소켓으로 본다(세션 루프 하나 — test_robot_channel.py와 같은 이유).
진짜 소켓으로 받는지는 tests/test_ws_envelopes_live.py가 따로 덮는다.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import get_session
from app.models import Robot
from app.robot_channel import handle_robot_frame
from app.ws import manager, robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

ENVELOPE_KEYS = {"type", "version", "robot_id", "timestamp", "data"}
CARD_KEYS = {
    "id", "name", "mode", "battery", "network_status", "updated_at",
    # 위치·LED·임무 상태는 DB 컬럼이 아니라 인메모리 최신값이다
    # (tests/test_robot_presentation.py).
    "position", "led_status", "mission_status",
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]

    def first(self, type_: str) -> dict[str, Any] | None:
        for m in self.sent:
            if m.get("type") == type_:
                return m
        return None

    def count(self, type_: str) -> int:
        return sum(1 for m in self.sent if m.get("type") == type_)


def state_frame(**payload: Any) -> dict[str, Any]:
    return {
        "type": "robot_state",
        "version": "1",
        "robot_id": payload.get("robot_id"),
        "timestamp": _now(),
        "data": payload,
    }


@pytest_asyncio.fixture(loop_scope="session")
async def dash():
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


async def test_상태_보고가_robot_status_메시지를_낸다(dash, robot):
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 55, "mode": "OUTDOOR_TAGGING", "comm_status": "WS_OK"},
        ),
    )

    card = dash.first("robot_status")
    assert card is not None, dash.types()
    assert set(card) == ENVELOPE_KEYS
    assert card["robot_id"] == "jetson01"
    assert set(card["data"]) == CARD_KEYS
    assert card["data"]["name"] == "jetson01"
    assert card["data"]["battery"] == 55
    assert card["data"]["mode"] == "OUTDOOR_TAGGING"
    assert card["data"]["network_status"] == "WS_OK"


async def test_메시지_값이_DB_행과_같다(dash, robot):
    """화면이 이 메시지로 카드를 갈아끼운 뒤 스냅샷을 다시 읽어도 같은 그림이어야 한다."""
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 41, "mode": "INDOOR_TAGGING", "comm_status": "WS_OK"},
        ),
    )
    payload = dash.first("robot_status")["data"]

    async with get_session() as s:
        row = (await s.execute(select(Robot).where(Robot.name == "jetson01"))).scalars().one()
    assert payload["id"] == row.id
    assert (payload["battery"], payload["mode"], payload["network_status"]) == (
        row.battery, row.mode, row.network_status
    )
    assert dt.datetime.fromisoformat(payload["updated_at"]).tzinfo is not None


async def test_안_실린_필드는_직전_값_그대로_실린다(dash, robot):
    """status_summary는 절대 상태라 안 실린 필드는 '보고 안 함'이다.

    메시지가 그 자리를 null로 실으면 화면 카드에서 배터리가 사라진다 — DB는 안 지웠는데
    화면만 빈다.
    """
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 64, "mode": "INDOOR_TAGGING", "comm_status": "WS_OK"},
        ),
    )
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson01", status_summary={"comm_status": "POLLING_GRACE"}),
    )

    last = [m for m in dash.sent if m["type"] == "robot_status"][-1]["data"]
    assert last["battery"] == 64
    assert last["mode"] == "INDOOR_TAGGING"
    assert last["network_status"] == "POLLING_GRACE"


async def test_summary_없는_보고도_메시지를_낸다(dash, robot):
    """주행 중엔 status_summary 없이 올리는 로봇이 있다. updated_at은 그래도 갱신된다."""
    await handle_robot_frame(robot, state_frame(robot_id="jetson02"))
    card = dash.first("robot_status")
    assert card is not None, dash.types()
    assert card["data"]["name"] == "jetson02"
    assert card["data"]["updated_at"] is not None


async def test_보고_한_건에_메시지_한_장씩(dash, robot):
    """중계(robot_state)와 카드(robot_status)는 짝이다 — 한쪽만 두 번 나면 안 된다."""
    for _ in range(3):
        await handle_robot_frame(robot, state_frame(robot_id="jetson01"))
    assert dash.count("robot_status") == 3
    assert dash.count("robot_state") == 3
