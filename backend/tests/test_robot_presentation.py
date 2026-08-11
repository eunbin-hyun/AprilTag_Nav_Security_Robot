"""로봇 위치·LED·임무 상태를 화면 쪽으로 노출한다.

인바운드는 이미 받고 있었다(RobotStatusSummary.position·led_status). 그런데 Robot 테이블에
컬럼이 없어서 `_upsert_robot`이 그 둘을 버렸고, 원본 중계 메시지(`robot_state`)에만 남았다.
화면이 로봇 카드에서 위치·LED를 그리려면 카드 계열(스냅샷 `robots[]`·`/api/robots`·
`robot_status` 메시지)에도 값이 있어야 한다.

DB 컬럼은 안 만든다(마이그레이션 금지). 대신 서버 메모리에 로봇별 최신값을 든다 —
워커 1개 전제라 크레딧 쿨다운·미션 에지 추적과 같은 방식이다. **서버가 재기동되면 사라지고
다음 상태 보고가 다시 채운다.** 그게 의도된 거동이라 여기 케이스가 못박는다.

`mission_status`도 2026-08-01에 같은 자리로 들어왔다(프론트 요구 N10). 그 값은 WS
`robot_state`로만 흘러서, 화면을 새로 고치면 개요 탭 "로봇 상태" 타일이 비었다. 뒤쪽
"임무 상태" 절이 값 실리는 갈래와 아직 없을 때(null) 갈래를 둘 다 덮는다.
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import pytest_asyncio

from app.robot_channel import handle_robot_frame, reset_robot_channel_state
from app.ws import manager, robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

CARD_KEYS = {
    "id", "name", "mode", "battery", "network_status", "updated_at",
    "position", "led_status", "mission_status",
}
POS = {"x": 12.5, "y": 34.0, "theta": 1.57}


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

    def last(self, type_: str) -> dict[str, Any] | None:
        found = [m for m in self.sent if m.get("type") == type_]
        return found[-1] if found else None


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


def _card(dash: FakeSocket) -> dict[str, Any]:
    env = dash.last("robot_status")
    assert env is not None, dash.types()
    return env["data"]


# ── robot_status 메시지 ─────────────────────────────────────────────────────

async def test_위치와_LED가_robot_status_메시지에_실린다(dash, robot):
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 70, "comm_status": "WS_OK",
                            "position": POS, "led_status": "GREEN"},
        ),
    )
    card = _card(dash)
    assert set(card) == CARD_KEYS
    assert card["position"] == POS
    assert card["led_status"] == "GREEN"


async def test_안_실으면_null이라_기존_화면_계약이_안_깨진다(dash, robot):
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson02", status_summary={"battery": 20}),
    )
    card = _card(dash)
    assert set(card) == CARD_KEYS
    assert card["position"] is None
    assert card["led_status"] is None


async def test_부분_보고는_직전_값을_안_덮는다(dash, robot):
    """status_summary는 절대 상태다 — 위치만 올린 보고가 LED를 지우면 안 된다."""
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"position": POS, "led_status": "RED"},
        ),
    )
    moved = {"x": 99.0, "y": 1.0, "theta": 0.0}
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson01", status_summary={"position": moved}),
    )
    card = _card(dash)
    assert card["position"] == moved
    assert card["led_status"] == "RED"


# ── 조회 API·스냅샷 ───────────────────────────────────────────────────────

async def test_api_robots와_스냅샷에_같은_칸이_난다(dash, robot, client):
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 55, "position": POS, "led_status": "BLUE"},
        ),
    )

    listed = await client.get("/api/robots")
    assert listed.status_code == 200, listed.text
    row = [r for r in listed.json() if r["name"] == "jetson01"][0]
    assert row["position"] == POS
    assert row["led_status"] == "BLUE"

    snap = await client.get("/api/dashboard/snapshot")
    assert snap.status_code == 200, snap.text
    snap_row = [r for r in snap.json()["robots"] if r["name"] == "jetson01"][0]
    assert snap_row["position"] == POS
    assert snap_row["led_status"] == "BLUE"
    # 스냅샷 카드와 메시지 카드가 어긋나면 화면에 두 그림이 생긴다.
    assert set(snap_row) == CARD_KEYS


async def test_보고가_없는_로봇은_null이다(client, seed_robot):
    await seed_robot(name="patrol-9")
    listed = await client.get("/api/robots")
    row = [r for r in listed.json() if r["name"] == "patrol-9"][0]
    assert row["position"] is None
    assert row["led_status"] is None


# ── 재기동(메모리) 거동 ───────────────────────────────────────────────────

async def test_메모리라_재기동하면_사라진다(dash, robot, client):
    """DB 컬럼이 아니라 서버 메모리다. 값이 날아가도 로봇 행·나머지 칸은 그대로다."""
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 44, "position": POS, "led_status": "GREEN"},
        ),
    )
    reset_robot_channel_state()  # 서버 재기동과 같은 자리

    listed = await client.get("/api/robots")
    row = [r for r in listed.json() if r["name"] == "jetson01"][0]
    assert row["position"] is None
    assert row["led_status"] is None
    assert row["battery"] == 44  # DB에 남는 값은 그대로다

    # 다음 보고가 다시 채운다.
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson01", status_summary={"position": POS}),
    )
    again = await client.get("/api/robots")
    row2 = [r for r in again.json() if r["name"] == "jetson01"][0]
    assert row2["position"] == POS


# ── 임무 상태 (프론트 요구 N10) ────────────────────────────────────────────
# 값이 실리는 갈래와 아직 없을 때(null) 갈래를 나눠 못박는다. 인메모리라 "아직 없음"은
# 억지 상황이 아니라 컨테이너를 다시 띄울 때마다 지나가는 정상 상태다.

async def test_임무_상태가_스냅샷과_목록과_카드에_같이_실린다(dash, robot, client):
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 61, "comm_status": "WS_OK"},
            mission_status="ARRIVED",
        ),
    )

    card = _card(dash)
    assert set(card) == CARD_KEYS
    assert card["mission_status"] == "ARRIVED"

    listed = await client.get("/api/robots")
    assert listed.status_code == 200, listed.text
    row = [r for r in listed.json() if r["name"] == "jetson01"][0]
    assert row["mission_status"] == "ARRIVED"

    snap = await client.get("/api/dashboard/snapshot")
    assert snap.status_code == 200, snap.text
    snap_row = [r for r in snap.json()["robots"] if r["name"] == "jetson01"][0]
    assert snap_row["mission_status"] == "ARRIVED"
    # 스냅샷 카드와 메시지 카드가 어긋나면 화면에 두 그림이 생긴다.
    assert set(snap_row) == CARD_KEYS


async def test_임무_상태를_한_번도_안_올렸으면_null이다(dash, robot, client, seed_robot):
    """N10 계약의 null 갈래 — 칸은 늘 있고 값만 없다. 화면이 칸 없음으로 안 터진다."""
    await seed_robot(name="patrol-9")
    listed = await client.get("/api/robots")
    row = [r for r in listed.json() if r["name"] == "patrol-9"][0]
    assert "mission_status" in row
    assert row["mission_status"] is None

    # 임무 상태 없이 상태만 올린 로봇도 같다.
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson02", status_summary={"battery": 20}),
    )
    snap = await client.get("/api/dashboard/snapshot")
    snap_row = [r for r in snap.json()["robots"] if r["name"] == "jetson02"][0]
    assert snap_row["mission_status"] is None
    assert _card(dash)["mission_status"] is None


async def test_임무_상태는_재기동하면_null로_비고_다음_보고가_채운다(dash, robot, client):
    """인메모리라 컨테이너를 다시 띄우면 값이 없다 — 그게 정상이고 계약이다.

    DB에 남는 배터리는 그대로여서, 화면이 카드 자체를 못 그리는 일은 없다.
    """
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 33},
            mission_status="RETURN_COMPLETE",
        ),
    )
    reset_robot_channel_state()  # 서버 재기동과 같은 자리

    listed = await client.get("/api/robots")
    row = [r for r in listed.json() if r["name"] == "jetson01"][0]
    assert row["mission_status"] is None
    assert row["battery"] == 33  # DB에 남는 값은 그대로다

    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson01", mission_status="WEATHER_BLOCKED"),
    )
    again = await client.get("/api/robots")
    row2 = [r for r in again.json() if r["name"] == "jetson01"][0]
    assert row2["mission_status"] == "WEATHER_BLOCKED"


async def test_주행_보고는_직전_임무_상태를_안_덮는다(dash, robot, client):
    """주행 중엔 mission_status를 비우고 올리는 로봇이 있다. null은 "없음"이 아니라 "보고 안 함"이다.

    지우면 게이트로 달려가는 동안 개요 탭 타일이 깜빡인다. 개발자 화면도 같은 규칙으로
    그린다(`static/telemetry.html`의 `if (p.mission_status)`).
    """
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 70},
            mission_status="ARRIVED",
        ),
    )
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson01", status_summary={"battery": 69}),
    )

    card = _card(dash)
    assert card["battery"] == 69
    assert card["mission_status"] == "ARRIVED"

    listed = await client.get("/api/robots")
    row = [r for r in listed.json() if r["name"] == "jetson01"][0]
    assert row["mission_status"] == "ARRIVED"


async def test_임무_상태만_올린_로봇도_목록에_선다(dash, robot, client):
    """DB 칸이 전부 빈 로봇이라도 임무 상태가 있으면 카드가 선다.

    `_has_nothing_to_show`가 mission_status를 안 보면 이 로봇은 그릴 값이 있는데도
    통지 기록용 빈 행과 같이 가려진다 — 그 한 줄을 실제로 재는 회귀 케이스다.
    (다른 케이스들은 전부 status_summary에 배터리를 실어 DB 칸만으로 카드가 선다.)
    """
    await handle_robot_frame(
        robot,
        state_frame(robot_id="jetson07", mission_status="ARRIVED"),
    )
    listed = await client.get("/api/robots")
    assert listed.status_code == 200, listed.text
    rows = [r for r in listed.json() if r["name"] == "jetson07"]
    assert rows, "임무 상태만 올린 로봇이 목록에서 가려졌다"
    assert rows[0]["mission_status"] == "ARRIVED"
    assert rows[0]["battery"] is None
