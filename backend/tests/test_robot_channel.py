"""로봇 outbound WS 상태 수신·알림 중계 (S15P11C207-156).

실 WebSocket 왕복은 test_robot_ws.py가 본다. 여기선 매니저에 가짜 소켓을 꽂아 "어떤 메시지가
나가고 어떤 행이 남나"를 검증한다. 세션 이벤트 루프 하나에서 돌아야 asyncpg 엔진이 갈리지
않아서(1단계 함정) 실 소켓 대신 가짜를 쓴다.
"""
import datetime as dt
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from app import device_sound
from app.config import get_settings
from app.db import get_session
from app.models import Alert, Robot, ShuttleArrival
from app.robot_channel import (
    ARRIVAL_EXPIRED_ALERT_TYPE,
    ROBOT_ARRIVAL_ALERT_TYPE,
    handle_robot_frame,
    reset_robot_channel_state,
)
from app.ws import manager, robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _no_dispatch_hold(monkeypatch):
    """이 파일에서는 출동 대기 창을 꺼서 셔틀 신호가 예전처럼 즉시 발사되게 둔다.

    2026-07-31 셔틀출동 설계가 셔틀 도착과 목적지 명령 사이에 5초 대기 창을 새로 끼웠다
    (app/dispatch.py). 이 파일이 보는 건 **명령이 나간 뒤**의 배선이다 — 짝짓기·ETA 표본·
    명령 봉투 모양·재전송 멱등. 그 배선은 대기 창이 있든 없든 같은 함수
    (`notify_shuttle_arrival`)를 타므로, 여기서 5초를 기다리면 시험만 느려지고 잡는 건
    똑같다. 대기 창 자체의 시나리오(자동 발사·수동 선택·대기·즉시 이동 우선)는
    `tests/test_shuttle_dispatch.py`가 본다. 창을 거쳐 자동 발사된 신호에도 짝짓기가 붙는지도
    거기서 못박는다.

    끄는 방법은 `recommand_edge`와 같다 — 설정 인스턴스에 속성을 직접 대입하면 뒤이어
    `get_settings.cache_clear()`를 부르는 픽스처가 새 인스턴스를 만들면서 이 값이 조용히
    사라진다(실측: 이 파일의 재출동 케이스가 그렇게 깨졌다). env를 바꾸고 캐시를 비운다.
    """
    monkeypatch.setenv("SHUTTLE_DISPATCH_HOLD_SEC", "0")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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


@pytest.fixture
def recommand_edge(monkeypatch):
    """같은 값 ARRIVED 재보고를 재출동 도착으로 치는 길을 이 케이스에서만 연다.

    기본값이 꺼짐이라(가짜 재도착 방지) 재출동 판정을 보려면 켜야 한다. 설정 인스턴스는
    lru_cache로 하나뿐이라 속성을 직접 대입하면 xdist 병렬에서 서로 밟는다. env를
    monkeypatch로 바꾸고 cache_clear로 재구성을 강제해서 끝나고 되돌린다(검사 위생).
    """
    monkeypatch.setenv("ARRIVAL_EDGE_ON_RECOMMAND", "true")
    get_settings.cache_clear()
    try:
        yield get_settings()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest_asyncio.fixture(loop_scope="session")
async def robot2():
    """두 번째 로봇 커넥션. 신호 오귀속을 보려면 로봇이 둘 있어야 한다."""
    sock = FakeSocket()
    await robot_manager.connect(sock)
    try:
        yield sock
    finally:
        robot_manager.disconnect(sock)


# ── 상태 수신·중계 ────────────────────────────────────────────────────────

async def test_robot_state_relayed_to_dashboard(dash, robot):
    frame = state_frame(
        robot_id="jetson01",
        status_summary={
            "battery": 77,
            "mode": "INDOOR_TAGGING",
            "comm_status": "WS_OK",
            "position": {"x": 1.5, "y": 2.0, "theta": 0.0},
            "led_status": "GREEN",
        },
    )
    await handle_robot_frame(robot, frame)

    relayed = dash.first("robot_state")
    assert relayed is not None, dash.types()
    assert relayed["robot_id"] == "jetson01"
    assert relayed["data"]["status_summary"]["battery"] == 77
    assert relayed["data"]["status_summary"]["position"]["x"] == 1.5
    # 로봇에게는 state_ack로 답한다.
    assert robot.first("state_ack") is not None


async def test_status_summary_updates_robot_row(dash, robot):
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01",
            status_summary={"battery": 80, "mode": "OUTDOOR_TAGGING", "comm_status": "WS_OK"},
        ),
    )
    async with get_session() as s:
        row = (await s.execute(select(Robot).where(Robot.name == "jetson01"))).scalars().one()
        assert row.battery == 80
        assert row.mode == "OUTDOOR_TAGGING"
        assert row.network_status == "WS_OK"


async def test_partial_summary_keeps_previous_values(dash, robot):
    """status_summary는 절대 상태다 — 안 실려 온 필드는 '보고 안 함'이라 덮지 않는다."""
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
    async with get_session() as s:
        row = (await s.execute(select(Robot).where(Robot.name == "jetson01"))).scalars().one()
        assert row.battery == 64
        assert row.mode == "INDOOR_TAGGING"
        assert row.network_status == "POLLING_GRACE"


# ── mission_status → 알림 변환 ────────────────────────────────────────────

async def test_mission_arrived_links_shuttle_and_alerts(dash, robot, client, auth_headers):
    r = await client.post(
        "/api/shuttle-arrivals",
        json={
            "event_id": "gate2-1800000000000-0",
            "gate_no": 2,
            "shuttle_no": "SHUTTLE-A",
            "signal_ts": _now(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="ARRIVED")
    )

    notice = dash.first("robot_arrival")
    assert notice is not None, dash.types()
    assert notice["data"]["message"] == "로봇이 게이트 자리에 도착했습니다."
    assert notice["data"]["mission_status"] == "ARRIVED"

    async with get_session() as s:
        arrival_id = (await s.execute(select(ShuttleArrival.id))).scalar_one()
        alert = (
            await s.execute(select(Alert).where(Alert.type == "robot_arrival"))
        ).scalars().one()
    # 셔틀 신호에 묶여야 ETA가 구간 소요시간을 뽑을 수 있다.
    assert notice["data"]["shuttle_arrival_id"] == arrival_id
    assert alert.source_type == "shuttle_arrival"
    assert alert.source_id == arrival_id
    assert alert.severity == "info"


async def test_mission_weather_blocked_is_warning(dash, robot):
    """우천 미출동은 셔틀 신호 짝짓기 없이 경고 하나로만 남는다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="WEATHER_BLOCKED")
    )
    notice = dash.first("robot_weather_blocked")
    assert notice is not None, dash.types()
    assert notice["data"]["severity"] == "warning"
    assert notice["data"]["shuttle_arrival_id"] is None

    async with get_session() as s:
        alert = (
            await s.execute(select(Alert).where(Alert.type == "robot_weather_blocked"))
        ).scalars().one()
        assert alert.source_type == "robot"


async def test_mission_return_complete_notice(dash, robot):
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="RETURN_COMPLETE")
    )
    notice = dash.first("robot_return_complete")
    assert notice is not None, dash.types()
    assert notice["data"]["message"] == "로봇이 게이트를 떠나 복귀를 마쳤습니다."


# ── 로봇 사건 → 게이트 스피커 (2026-08-06 사용자 배선) ─────────────────────
#
# 젯슨과 라파는 서로 직접 못 붙는다. 젯슨이 서버로 올린 `mission_status`를 서버가 라파 소리
# 큐로 넘기는 그 한 줄을 여기서 지킨다. 실기기가 없어도 큐까지는 실측할 수 있다.


@pytest.fixture
def sound_queue():
    """소리 큐 위생. 인메모리라 앞 케이스가 넣은 신호가 샌다(`test_device_sound.py`와 같은 짝)."""
    device_sound.reset_sound_queue()
    try:
        yield
    finally:
        device_sound.reset_sound_queue()


async def test_도착과_복귀완료가_게이트_스피커로_나간다(dash, robot, sound_queue):
    """⭐ 이 파일에서 소리를 보는 본체. 이 한 줄이 빠지면 라파 스피커가 조용해진다.

    ⚠ 종류 이름을 상수가 아니라 **글자로 박는다.** 이 값은 서버 안에서만 쓰는 이름이 아니라
    라파이 폴러가 음원을 고르는 열쇠라(창구가 `type=kind`로 그대로 내보낸다) 바뀌면 시험이
    깨져야 맞다. 상수를 import해 비교하면 이름을 바꿔도 초록이라 계약을 못 지킨다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="ARRIVED")
    )
    signals = device_sound.take_sounds("raspberry-sound-1")
    assert [s.kind for s in signals] == ["robot_arrival"], "도착이 스피커로 안 갔다"

    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="RETURN_COMPLETE")
    )
    # ⭐ 이 전이는 알림을 **둘** 낸다(출발 + 복귀완료). 그런데 소리는 하나여야 한다.
    signals = device_sound.take_sounds("raspberry-sound-2")
    assert [s.kind for s in signals] == ["robot_arrival", "robot_return_complete"], (
        "출발 알림까지 소리가 났거나 복귀완료가 안 갔다"
    )


async def test_주행실패는_게이트_스피커로_안_나간다(dash, robot, sound_queue):
    """게이트에 선 사람에게 알릴 내용이 아니다 — 관제 요원이 봐야 하는 상태다.

    화면 알림은 그대로 나가는지도 같이 잰다. 소리만 빼는 것이지 알림을 지운 게 아니다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="FAILED")
    )
    assert dash.first("robot_mission_failed") is not None, dash.types()
    assert device_sound.take_sounds("raspberry-sound-3") == []


async def test_스피커_신호에_알림_번호가_실린다(dash, robot, sound_queue):
    """라파이 ack 로그에서 "어느 사건이 울렸나"를 되짚는 유일한 열쇠다.

    ⚠ `gate_no`는 비어 있어야 한다 — 어느 게이트에 선 로봇인지 서버가 모른다. 값을 지어
    넣으면 게이트가 둘이 되는 날 엉뚱한 쪽만 울린다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="ARRIVED")
    )
    signal = device_sound.take_sounds("raspberry-sound-4")[0]
    assert signal.gate_no is None
    assert signal.event_id is not None and signal.event_id.startswith("alert-")

    async with get_session() as s:
        alert_id = (
            await s.execute(
                select(Alert.id).where(Alert.type == ROBOT_ARRIVAL_ALERT_TYPE)
            )
        ).scalars().one()
    assert signal.event_id == f"alert-{alert_id}", "되짚을 수 없는 번호가 실렸다"


# ── 셔틀 도착 → 로봇 통지 ─────────────────────────────────────────────────

async def test_shuttle_arrival_pushes_command_to_robot(dash, robot, client, auth_headers):
    # 먼저 상태를 한 번 올려 이 커넥션이 어느 로봇인지 등록한다.
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )

    r = await client.post(
        "/api/shuttle-arrivals",
        json={
            "event_id": "gate2-1800000000001-0",
            "gate_no": 2,
            "shuttle_no": "SHUTTLE-A",
            "signal_ts": _now(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    command = robot.first("command")
    assert command is not None, robot.types()
    # 봉투의 본문 칸은 `data`다(2026-08-04부터 대시보드도 같은 이름이다).
    assert command["data"]["type"] == "cmd_destination"
    assert command["data"]["cmd_destination"] == "OUTDOOR_TAGGING"
    assert command["data"]["command_id"].startswith("shuttle-")
    assert command["data"]["ttl_ms"] == 30000

    broadcast = dash.first("shuttle_arrival")
    assert broadcast["data"]["notified_robot_count"] == 1

    async with get_session() as s:
        arrival = (await s.execute(select(ShuttleArrival))).scalars().one()
        robot_row = (
            await s.execute(select(Robot).where(Robot.name == "jetson01"))
        ).scalars().one()
        assert arrival.notified_robot_id == robot_row.id


async def test_shuttle_retry_does_not_push_command_twice(dash, robot, client, auth_headers):
    """멱등 규칙 — 같은 event_id 재전송이 로봇을 두 번 출동시키면 안 된다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    body = {
        "event_id": "gate2-1800000000002-0",
        "gate_no": 2,
        "shuttle_no": "SHUTTLE-A",
        "signal_ts": _now(),
    }
    r1 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    r2 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert r1.json()["stored"] is True
    assert r2.json()["duplicate"] is True
    assert robot.count("command") == 1


async def test_shuttle_arrival_without_robot_reports_zero(dash, client, auth_headers):
    """로봇이 안 붙어 있으면 명령이 못 간다 — 그 사실이 메시지에 드러나야 한다."""
    r = await client.post(
        "/api/shuttle-arrivals",
        json={
            "event_id": "gate2-1800000000003-0",
            "gate_no": 2,
            "shuttle_no": "SHUTTLE-A",
            "signal_ts": _now(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert dash.first("shuttle_arrival")["data"]["notified_robot_count"] == 0
    async with get_session() as s:
        arrival = (await s.execute(select(ShuttleArrival))).scalars().one()
        assert arrival.notified_robot_id is None


# ── 잘못된 프레임 ─────────────────────────────────────────────────────────

async def test_invalid_robot_state_replies_error(dash, robot):
    """검증에 걸리는 상태는 연결을 끊지 않고 error 메시지로 돌려준다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 500})
    )
    err = robot.first("error")
    assert err is not None, robot.types()
    assert err["data"]["reason"] == "invalid_robot_state"
    # 상태가 반영되지도, 중계되지도 않는다.
    assert dash.first("robot_state") is None
    async with get_session() as s:
        assert (await s.execute(select(Robot))).scalars().first() is None


async def test_unknown_frame_type_replies_error(robot):
    await handle_robot_frame(robot, {"type": "no_such_type", "data": {}})
    err = robot.first("error")
    assert err is not None
    assert err["data"]["reason"] == "unknown_frame_type"


async def test_command_ack_frame_is_accepted(robot):
    """ACK는 로그만 남기고 답을 안 보낸다. 본문 칸은 `data`다."""
    await handle_robot_frame(
        robot, {"type": "command_ack", "data": {"command_id": "shuttle-1", "accepted": True}}
    )
    assert robot.sent == []


async def test_robot_status_post_not_exposed(client, auth_headers):
    """계약 가드 M-4 — 로봇 상태 POST는 열지 않는다(WS 전용)."""
    r = await client.post(
        "/api/robots/1/status", json={"battery": 50}, headers=auth_headers
    )
    assert r.status_code in (404, 405), r.text


# ── mission_status 에지 트리거 ─────────────────────────────────────────────

async def _post_arrival(client, auth_headers, event_id: str, *, gate_no: int = 2) -> int:
    # signal_ts를 1초 과거로 찍는다. ETA 표본은 Alert.created_at(DB now())−signal_ts가
    # 양수여야 잡히는데, 시험 DB가 딴 기계면 시계가 수십 ms 어긋나 신호→도착이 빠른
    # 시험만 음수로 떨어진다(게이밍PC 실측 −85ms에서 4건 재현). 짝짓기 TTL은 900초라
    # 1초 과거는 여유 안이고, 단정은 표본 개수만 봐서 표본 값 크기는 무관하다.
    signal_ts = (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    ).isoformat()
    r = await client.post(
        "/api/shuttle-arrivals",
        json={
            "event_id": event_id,
            "gate_no": gate_no,
            "shuttle_no": "SHUTTLE-A",
            "signal_ts": signal_ts,
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    async with get_session() as s:
        return (
            await s.execute(
                select(ShuttleArrival.id).where(ShuttleArrival.event_id == event_id)
            )
        ).scalar_one()


async def _alerts_of(type_: str) -> list[Alert]:
    async with get_session() as s:
        return list((await s.execute(select(Alert).where(Alert.type == type_))).scalars().all())


async def test_repeated_arrived_report_notifies_once(dash, robot, client, auth_headers):
    """RobotState는 절대 상태다 — ARRIVED가 실린 주기 보고 5번은 도착 한 번이다.

    알림·Alert 행·ETA 표본이 각각 1건이어야 한다. 예전 구현은 보고 수만큼 늘어났다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    arrival_id = await _post_arrival(client, auth_headers, "gate2-edge-0")

    for seq in range(5):
        await handle_robot_frame(
            robot,
            state_frame(
                robot_id="jetson01",
                status_summary={"battery": 90 - seq, "comm_status": "WS_OK"},
                mission_status="ARRIVED",
            ),
        )

    assert dash.count("robot_arrival") == 1, dash.types()
    alerts = await _alerts_of("robot_arrival")
    assert len(alerts) == 1
    assert alerts[0].source_id == arrival_id
    # 상태 중계는 보고마다 그대로 간다(도착만 에지로 잡는다).
    assert dash.count("robot_state") == 6
    # ETA 표본도 한 건이다 — 같은 도착이 구간 기록을 5개로 부풀리면 안 된다.
    body = (await client.get("/api/eta")).json()
    assert body["samples"] == 1, body


async def test_arrived_again_after_departure_is_a_new_event(dash, robot, client, auth_headers):
    """도착 → 복귀 → 다시 도착은 서로 다른 사건이라 도착 알림이 두 번 난다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    first = await _post_arrival(client, auth_headers, "gate2-edge-1")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="RETURN_COMPLETE")
    )
    second = await _post_arrival(client, auth_headers, "gate2-edge-2")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    assert dash.count("robot_arrival") == 2, dash.types()
    linked = [
        m["data"]["shuttle_arrival_id"] for m in dash.sent if m["type"] == "robot_arrival"
    ]
    assert linked == [first, second]


#: 미션 알림이 아닌 로봇 메시지. 상태 보고 한 건마다 짝으로 나가는 것들이라 순서 비교에서 뺀다
#: (robot_state=원본 중계, robot_status=DB 카드 — S15P11C207-81).
_ROBOT_RELAY_TYPES = ("robot_state", "robot_status")


def _notice_types(dash) -> list[str]:
    """로봇 미션 알림 메시지만 온 순서대로(상태 중계·카드·셔틀 인입 메시지는 뺀다)."""
    return [
        m["type"]
        for m in dash.sent
        if m["type"].startswith("robot_") and m["type"] not in _ROBOT_RELAY_TYPES
    ]


async def test_consecutive_arrivals_without_return_report(
    dash, robot, client, auth_headers, recommand_edge
):
    """(설정 켰을 때) 게이트1 도착 → 복귀 보고 없이 게이트2 재출동 → 두 번째 도착도 잡힌다.

    mission_status enum에 "이동 중"이 없어 로봇은 ARRIVED를 그대로 다시 올린다. 값 비교만
    하면 두 번째 도착이 통째로 사라져서, `ARRIVAL_EDGE_ON_RECOMMAND`를 켜면 새 명령 상관을
    근거로 새 도착으로 친다. 기본값이 꺼짐인 이유는 바로 아래 케이스가 본다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    first = await _post_arrival(client, auth_headers, "gate1-relay-0", gate_no=1)
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    # 복귀 보고가 없다. 게이트2 신호로 재출동하고 도착을 다시 올린다.
    second = await _post_arrival(client, auth_headers, "gate2-relay-1", gate_no=2)
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    assert _notice_types(dash) == ["robot_arrival", "robot_departure", "robot_arrival"]
    linked = [
        m["data"]["shuttle_arrival_id"] for m in dash.sent if m["type"] == "robot_arrival"
    ]
    assert linked == [first, second]
    # 출발도 대칭으로 난다 — 게이트1 신호에 묶이고, 전이는 from/to로 실린다.
    departure = dash.first("robot_departure")
    assert departure["data"]["shuttle_arrival_id"] == first
    assert departure["data"]["from_status"] == "ARRIVED"
    assert departure["data"]["to_status"] == "ARRIVED"
    assert len(await _alerts_of("robot_arrival")) == 2


async def test_standing_robot_is_not_redispatched_by_new_signals(
    dash, robot, client, auth_headers
):
    """기본 동작 — 게이트에 서 있는 로봇은 새 신호가 와도 도착이 한 번뿐이다.

    명령은 붙어 있는 로봇 **전부**에게 브로드캐스트라, 서 있는 로봇도 새 신호마다
    CommandLink를 받는다. 그걸 재출동 근거로 쓰면 0.5초 주기 보고가 가짜 도착이 되고
    ETA 표본이 보고 주기로 오염된다. 그래서 기본은 상태 전이 에지만 본다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    first = await _post_arrival(client, auth_headers, "gate2-stand-0")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    # 로봇은 게이트에 서서 계속 보고만 한다. 그 사이 새 셔틀 신호 3건이 들어온다.
    for seq in range(3):
        await _post_arrival(client, auth_headers, f"gate2-stand-{seq + 1}")
        await handle_robot_frame(
            robot,
            state_frame(
                robot_id="jetson01",
                status_summary={"battery": 90 - seq},
                mission_status="ARRIVED",
            ),
        )

    assert _notice_types(dash) == ["robot_arrival"], dash.types()
    assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] == first
    assert len(await _alerts_of("robot_arrival")) == 1
    # ETA 표본도 하나다 — 보고 주기가 구간 기록으로 들어가면 예측이 0초대로 무너진다.
    body = (await client.get("/api/eta")).json()
    assert body["samples"] == 1, body


async def test_queued_command_before_arrival_is_not_a_second_arrival(
    dash, robot, client, auth_headers, recommand_edge
):
    """(설정 켰을 때) 도착 전에 이미 쌓여 있던 명령은 주기 보고를 가짜 도착으로 안 만든다.

    재출동 판정 기준이 "직전 도착 뒤에 나간 명령"이라서 걸러진다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    first = await _post_arrival(client, auth_headers, "gate2-queue-0")
    await _post_arrival(client, auth_headers, "gate2-queue-1")  # 도착 전에 두 번째 신호
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    assert _notice_types(dash) == ["robot_arrival"], dash.types()
    assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] == first


async def test_departure_edge_emits_symmetric_notice(dash, robot, client, auth_headers):
    """ARRIVED에서 벗어나는 전이는 출발 알림으로 남긴다(-156 카드의 '출발')."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    arrival_id = await _post_arrival(client, auth_headers, "gate2-dep-0")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="RETURN_COMPLETE")
    )

    types = dash.types()
    assert types.index("robot_departure") < types.index("robot_return_complete")
    departure = dash.first("robot_departure")
    assert departure["data"]["message"] == "로봇이 게이트 자리에서 출발했습니다."
    # 출발도 도착과 같은 셔틀 신호에 묶인다.
    assert departure["data"]["shuttle_arrival_id"] == arrival_id
    assert len(await _alerts_of("robot_departure")) == 1


async def test_departure_envelope_does_not_claim_new_status(dash, robot, client, auth_headers):
    """출발 메시지는 새 상태를 mission_status에 싣지 않는다 — 전이는 from/to로만 싣는다.

    예전 메시지는 mission_status에 RETURN_COMPLETE를 실어서 화면이 출발을 복귀완료로 읽었다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    await _post_arrival(client, auth_headers, "gate2-env-0")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", mission_status="RETURN_COMPLETE")
    )

    departure = dash.first("robot_departure")["data"]
    assert departure["mission_status"] is None, departure
    assert departure["from_status"] == "ARRIVED"
    assert departure["to_status"] == "RETURN_COMPLETE"
    # 복귀완료 메시지는 그대로 자기 상태를 싣는다.
    done = dash.first("robot_return_complete")["data"]
    assert done["mission_status"] == "RETURN_COMPLETE"
    assert done["from_status"] == "ARRIVED"
    assert done["to_status"] == "RETURN_COMPLETE"


# ── 도착 ↔ 셔틀 신호 짝짓기 ───────────────────────────────────────────────

async def test_arrival_pairs_with_commanded_signal(dash, robot, client, auth_headers):
    """도착은 '최신 미짝 신호'가 아니라 그 로봇이 명령받은 신호에 붙는다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    commanded = await _post_arrival(client, auth_headers, "gate2-corr-0")
    assert robot.first("command") is not None

    # 명령이 안 간 더 최신 신호(다른 게이트). 예전 구현은 이걸 잡아 게이트를 오귀속했다.
    async with get_session() as s:
        s.add(
            ShuttleArrival(
                event_id="gate5-corr-1",
                gate_no=5,
                shuttle_no="SHUTTLE-B",
                signal_ts=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60),
            )
        )
        await s.commit()

    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] == commanded


async def test_fallback_takes_oldest_unmatched_signal(dash, robot):
    """상관관계가 없으면 가장 오래된 유효 미짝 신호부터 채운다(옛 신호 영구 누락 방지)."""
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as s:
        s.add_all(
            [
                ShuttleArrival(
                    event_id="gate2-old", gate_no=2, signal_ts=now - dt.timedelta(seconds=300)
                ),
                ShuttleArrival(
                    event_id="gate2-new", gate_no=2, signal_ts=now - dt.timedelta(seconds=10)
                ),
            ]
        )
        await s.commit()
        oldest = (
            await s.execute(
                select(ShuttleArrival.id).where(ShuttleArrival.event_id == "gate2-old")
            )
        ).scalar_one()

    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] == oldest


async def test_fallback_does_not_steal_signal_commanded_to_other_robot(
    dash, robot, robot2, client, auth_headers
):
    """딴 로봇에 명령된 신호를 늦게 붙은 로봇의 도착이 가져가면 안 된다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    commanded = await _post_arrival(client, auth_headers, "gate2-own-0")
    assert robot.count("command") == 1

    # jetson03은 명령이 나간 뒤에 붙었다 — 이 신호로 명령받은 적이 없다.
    await handle_robot_frame(
        robot2, state_frame(robot_id="jetson03", status_summary={"battery": 70})
    )
    assert robot2.count("command") == 0
    await handle_robot_frame(robot2, state_frame(robot_id="jetson03", mission_status="ARRIVED"))

    late = dash.first("robot_arrival")
    assert late["robot_id"] == "jetson03"
    assert late["data"]["shuttle_arrival_id"] is None, late

    # jetson01의 도착은 여전히 자기 신호에 붙는다.
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    linked = [
        m["data"]["shuttle_arrival_id"] for m in dash.sent if m["type"] == "robot_arrival"
    ]
    assert linked == [None, commanded]


async def test_fallback_stays_in_commanded_gate(dash, robot, robot2, client, auth_headers):
    """명령받은 신호가 이미 닫혔으면 폴백도 그 게이트 안에서만 찾는다(CommandLink.gate_no)."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    await handle_robot_frame(
        robot2, state_frame(robot_id="jetson03", status_summary={"battery": 70})
    )
    # 명령 안 된 딴 게이트 신호가 더 오래된 채로 열려 있다(폴백이 여기로 새면 게이트 오귀속).
    async with get_session() as s:
        s.add(
            ShuttleArrival(
                event_id="gate5-hint-0",
                gate_no=5,
                signal_ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=120),
            )
        )
        await s.commit()

    # 로봇 둘이 같은 신호로 명령을 받는다(브로드캐스트). 먼저 도착한 쪽이 신호를 쓴다.
    commanded = await _post_arrival(client, auth_headers, "gate2-hint-1")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    await handle_robot_frame(robot2, state_frame(robot_id="jetson03", mission_status="ARRIVED"))

    linked = [
        m["data"]["shuttle_arrival_id"] for m in dash.sent if m["type"] == "robot_arrival"
    ]
    assert linked == [commanded, None], linked


async def test_arrival_repairs_pairing_after_restart(dash, robot, client, auth_headers):
    """서버가 재기동돼 인메모리 상관이 비어도, 도착은 자기 신호에 다시 붙는다.

    재기동으로 CommandLink가 날아가면 남는 근거는 DB의 `notified_robot_id`뿐이다. 그런데
    폴백이 "명령된 신호"를 통째로 배제하면 자기 신호도 같이 빠져서 그 신호는 영영 미짝으로
    남고 ETA 구간 기록에서도 빠진다. 그래서 자기 앞으로 표시된 신호를 먼저 집는다.
    """
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    commanded = await _post_arrival(client, auth_headers, "gate2-restart-0")
    assert robot.first("command") is not None

    # 재기동 재현 — 인메모리(에지 추적·명령 상관)만 비운다. DB는 그대로다.
    reset_robot_channel_state()

    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 88})
    )
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    notice = dash.first("robot_arrival")
    assert notice is not None, dash.types()
    assert notice["data"]["shuttle_arrival_id"] == commanded, notice["data"]
    # ETA도 구간 기록을 다시 잡는다(짝이 붙었으니 표본 1건).
    body = (await client.get("/api/eta")).json()
    assert body["samples"] == 1, body


async def test_restart_pairing_does_not_take_other_robots_signal(
    dash, robot, robot2, client, auth_headers
):
    """재기동 뒤 복원은 자기 앞으로 표시된 신호만 집는다 — 딴 로봇 신호는 그대로 금지다."""
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    commanded = await _post_arrival(client, auth_headers, "gate2-restart-own-0")
    reset_robot_channel_state()

    # jetson03은 이 신호로 명령받은 적이 없다(DB 표시는 jetson01 앞으로 있다).
    await handle_robot_frame(
        robot2, state_frame(robot_id="jetson03", status_summary={"battery": 70})
    )
    await handle_robot_frame(robot2, state_frame(robot_id="jetson03", mission_status="ARRIVED"))
    late = dash.first("robot_arrival")
    assert late["robot_id"] == "jetson03"
    assert late["data"]["shuttle_arrival_id"] is None, late["data"]

    # jetson01이 올리면 그때 자기 신호에 붙는다.
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 88})
    )
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    linked = [
        m["data"]["shuttle_arrival_id"] for m in dash.sent if m["type"] == "robot_arrival"
    ]
    assert linked == [None, commanded], linked


async def test_expire_runs_even_when_link_matches(dash, robot, client, auth_headers):
    """만료 청소는 폴백 경로만이 아니라 도착 처리마다 돈다."""
    ttl = get_settings().arrival_match_ttl_sec
    async with get_session() as s:
        s.add(
            ShuttleArrival(
                event_id="gate9-stale",
                gate_no=9,
                signal_ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ttl + 60),
            )
        )
        await s.commit()
        stale_id = (
            await s.execute(
                select(ShuttleArrival.id).where(ShuttleArrival.event_id == "gate9-stale")
            )
        ).scalar_one()

    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    commanded = await _post_arrival(client, auth_headers, "gate2-sweep-0")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    # 상관관계로 짝이 맞았는데도(폴백 안 탔는데도) 옛 신호가 닫혔다.
    assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] == commanded
    expired = await _alerts_of(ARRIVAL_EXPIRED_ALERT_TYPE)
    assert [a.source_id for a in expired] == [stale_id]


async def test_stale_command_links_are_pruned_outside_arrival(dash, client, auth_headers):
    """도착을 한 번도 안 올리는 로봇의 명령 상관도 TTL로 걷힌다(무한 누적 방지)."""
    from app.robot_channel import CommandLink, _command_links, register_command_link

    ttl = get_settings().arrival_match_ttl_sec
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ttl + 60)
    for i in range(3):
        register_command_link(
            "jetson09",
            CommandLink(command_id=f"shuttle-{i}", arrival_id=i + 1, gate_no=2, issued_at=old),
        )
    assert len(_command_links["jetson09"]) == 3

    # 도착이 안 와도 명령을 새로 낼 때 청소가 돈다.
    await _post_arrival(client, auth_headers, "gate2-prune-0")
    assert "jetson09" not in _command_links


async def test_stale_signal_expires_instead_of_matching(dash, robot):
    """TTL을 넘긴 미짝 신호는 만료로 닫고, 새 도착을 거기 붙이지 않는다."""
    ttl = get_settings().arrival_match_ttl_sec
    stale_ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ttl + 60)
    async with get_session() as s:
        s.add(ShuttleArrival(event_id="gate2-stale", gate_no=2, signal_ts=stale_ts))
        await s.commit()
        stale_id = (
            await s.execute(
                select(ShuttleArrival.id).where(ShuttleArrival.event_id == "gate2-stale")
            )
        ).scalar_one()

    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] is None
    expired = await _alerts_of(ARRIVAL_EXPIRED_ALERT_TYPE)
    assert [a.source_id for a in expired] == [stale_id]
    assert expired[0].severity == "warning"
    # 만료된 신호는 ETA 기준 시각으로도 안 쓰인다.
    async with get_session() as s:
        from app.robot_channel import latest_unmatched_arrival

        assert await latest_unmatched_arrival(s) is None


# ── 인메모리 상태 수명 ────────────────────────────────────────────────────

async def test_reconnect_does_not_forge_arrival(dash, client, auth_headers):
    """Wi-Fi가 한 번 끊겼다 붙어도 가짜 도착·가짜 ETA 표본·신호 오귀속이 안 난다.

    커넥션 종료로 에지 추적 상태를 버리면, 같은 자리에 그대로 서 있는 로봇이 다시 붙어 올린
    첫 ARRIVED가 도착 에지로 잡힌다. 재접속 한 번이 도착 알림 한 건과 주행 기록 한 건을
    만들어 ETA를 끌어내리고, 미짝 신호를 엉뚱하게 닫는다. 상태 수명은 TTL만 정한다.
    """
    from app.robot_channel import _mission_states, on_robot_disconnect

    sock1 = FakeSocket()
    await robot_manager.connect(sock1)
    try:
        await handle_robot_frame(sock1, state_frame(robot_id="jetson01", status_summary={"battery": 90}))
        arrival_id = await _post_arrival(client, auth_headers, "gate2-recon-0")
        await handle_robot_frame(sock1, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
        assert dash.count("robot_arrival") == 1, dash.types()
        assert dash.first("robot_arrival")["data"]["shuttle_arrival_id"] == arrival_id
        eta_before = (await client.get("/api/eta?gate_no=2")).json()
        assert eta_before["samples"] == 1, eta_before
    finally:
        robot_manager.disconnect(sock1)

    # 커넥션 종료 훅(main.py finally와 같은 자리) — 상태를 버리지 않는다.
    assert on_robot_disconnect("jetson01") == 0
    assert _mission_states["jetson01"].status == "ARRIVED"

    # 미짝 신호를 하나 둔다. 상태를 버렸다면 재접속 첫 보고가 여기로 오귀속된다.
    stray_id = await _post_arrival(client, auth_headers, "gate2-recon-1")

    sock2 = FakeSocket()
    await robot_manager.connect(sock2)
    try:
        await handle_robot_frame(sock2, state_frame(robot_id="jetson01", status_summary={"battery": 70}))
        await handle_robot_frame(sock2, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    finally:
        robot_manager.disconnect(sock2)

    assert dash.count("robot_arrival") == 1, dash.types()
    assert dash.count("robot_departure") == 0, dash.types()
    assert len(await _alerts_of(ROBOT_ARRIVAL_ALERT_TYPE)) == 1
    # 미짝 신호는 그대로 미짝이다.
    async with get_session() as s:
        stray = (
            await s.execute(select(ShuttleArrival).where(ShuttleArrival.id == stray_id))
        ).scalars().one()
    assert stray.id != arrival_id
    assert [a.source_id for a in await _alerts_of(ROBOT_ARRIVAL_ALERT_TYPE)] == [arrival_id]

    # ETA 표본이 안 늘었다(가짜 주행 기록 0건).
    eta_after = (await client.get("/api/eta?gate_no=2")).json()
    assert eta_after["samples"] == eta_before["samples"], eta_after
    assert eta_after["measured_sec"] == eta_before["measured_sec"], eta_after


async def test_mission_state_dropped_only_by_ttl_after_disconnect(dash, client, auth_headers):
    """커넥션이 다시 안 붙어도 상태가 영영 남지는 않는다 — TTL이 걷는다."""
    from app.robot_channel import _mission_states, on_robot_disconnect

    ttl = get_settings().mission_state_ttl_sec
    sock = FakeSocket()
    await robot_manager.connect(sock)
    try:
        await _post_arrival(client, auth_headers, "gate2-life-0")
        await handle_robot_frame(sock, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
        assert _mission_states["jetson01"].status == "ARRIVED"
    finally:
        robot_manager.disconnect(sock)

    assert on_robot_disconnect("jetson01") == 0
    _mission_states["jetson01"].seen_at -= dt.timedelta(seconds=ttl + 60)
    assert on_robot_disconnect("jetson01") == 1
    assert "jetson01" not in _mission_states


async def test_mission_state_pruned_by_ttl_but_kept_while_reporting(
    dash, robot, client, auth_headers
):
    """TTL 청소는 마지막 보고 시각을 본다 — 계속 보고하는 로봇은 안 걷힌다.

    청소 기준을 "도착을 기록한 시각"으로 잡으면 게이트에 오래 서 있는 로봇의 상태가 걷혀서,
    다음 주기 보고가 도착 에지로 다시 잡힌다(가짜 도착).
    """
    from app.robot_channel import _mission_states, prune_mission_states

    ttl = get_settings().mission_state_ttl_sec
    await _post_arrival(client, auth_headers, "gate2-life-1")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))

    # 도착을 기록한 시각(at)은 TTL을 넘겼는데, 방금까지 보고는 계속 오고 있다.
    _mission_states["jetson01"].at -= dt.timedelta(seconds=ttl + 60)
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert prune_mission_states() == 0
    assert "jetson01" in _mission_states
    # 가짜 도착도 안 났다.
    assert dash.count("robot_arrival") == 1, dash.types()

    # 보고가 TTL 넘게 끊기면 걷힌다.
    _mission_states["jetson01"].seen_at -= dt.timedelta(seconds=ttl + 60)
    assert prune_mission_states() == 1
    assert _mission_states == {}


async def test_driving_reports_without_mission_status_keep_state_alive(
    dash, robot, client, auth_headers
):
    """mission_status가 안 실린 주행 보고도 상태 수명을 갱신한다.

    로봇이 게이트로 달려가는 동안엔 mission_status를 비우고 상태만 올린다. 수명 기준을
    "mission_status 실린 보고"로 좁히면 그 주행 구간이 통째로 침묵으로 세어져, TTL을 넘긴
    순간 에지 상태가 걷힌다 — 도착해서 올린 ARRIVED가 "직전 상태 없음"에서 오는 전이로 잡혀
    가짜 도착 알림·미짝 신호 오귀속·가짜 ETA 표본이 한꺼번에 난다.
    """
    from app.robot_channel import _mission_states, prune_mission_states

    ttl = get_settings().mission_state_ttl_sec
    await _post_arrival(client, auth_headers, "gate2-drive-0")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert dash.count(ROBOT_ARRIVAL_ALERT_TYPE) == 1, dash.types()

    # 주행 보고만 올리며 TTL 창을 두 번 흘려보낸다(합치면 TTL 두 배 가까이).
    for battery in (80, 70):
        _mission_states["jetson01"].seen_at -= dt.timedelta(seconds=ttl - 1)
        await handle_robot_frame(
            robot, state_frame(robot_id="jetson01", status_summary={"battery": battery})
        )
        assert prune_mission_states() == 0, "주행 보고가 수명을 갱신하지 않았다"
        assert _mission_states["jetson01"].status == "ARRIVED"

    # 상태가 살아 있으니 같은 값 ARRIVED 재보고는 여전히 사건이 아니다(가짜 도착 0건).
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert dash.count(ROBOT_ARRIVAL_ALERT_TYPE) == 1, dash.types()
    assert len(await _alerts_of(ROBOT_ARRIVAL_ALERT_TYPE)) == 1


async def test_report_does_not_revive_state_past_ttl(dash, robot, client, auth_headers):
    """수명이 이미 TTL을 넘긴 상태는 보고 한 건으로 되살리지 않는다(청소 먼저, 갱신 나중).

    되살리면 반대쪽으로 샌다 — 한참 사라졌다 돌아온 로봇의 진짜 도착이 "값 안 바뀜"으로
    묻힌다. 없어진 로봇 몫은 걷어내고, 다음 ARRIVED를 새 도착으로 잡는 게 맞다.
    """
    from app.robot_channel import _mission_states

    ttl = get_settings().mission_state_ttl_sec
    await _post_arrival(client, auth_headers, "gate2-drive-1")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert dash.count(ROBOT_ARRIVAL_ALERT_TYPE) == 1, dash.types()

    # TTL을 넘겨 통신이 끊겼다. 그 뒤 주행 보고 한 건이 들어온다.
    _mission_states["jetson01"].seen_at -= dt.timedelta(seconds=ttl + 60)
    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 60})
    )
    assert _mission_states["jetson01"].status is None, "TTL 넘긴 상태가 되살아났다"

    # 다시 도착하면 새 도착으로 잡힌다.
    await _post_arrival(client, auth_headers, "gate2-drive-2")
    await handle_robot_frame(robot, state_frame(robot_id="jetson01", mission_status="ARRIVED"))
    assert dash.count(ROBOT_ARRIVAL_ALERT_TYPE) == 2, dash.types()


# ── 미식별 커넥션 가드 ────────────────────────────────────────────────────

async def test_command_not_sent_to_unidentified_connection(dash, robot, client, auth_headers):
    """robot_state를 한 번도 안 보낸 커넥션엔 명령을 안 보낸다(정식 인증 M-6 전 최소 가드)."""
    await _post_arrival(client, auth_headers, "gate2-unident-0")
    assert robot.count("command") == 0, robot.types()
    assert dash.first("shuttle_arrival")["data"]["notified_robot_count"] == 0
    async with get_session() as s:
        arrival = (await s.execute(select(ShuttleArrival))).scalars().one()
        assert arrival.notified_robot_id is None


# ── led_status 길이 ───────────────────────────────────────────────────────

async def test_long_led_status_is_accepted(dash, robot):
    """정본 스키마에 led_status 길이 제한이 없다 — 긴 문구로 상태 전체를 버리면 안 된다."""
    led = "GREEN_BLINK_" + "X" * 60
    await handle_robot_frame(
        robot,
        state_frame(
            robot_id="jetson01", status_summary={"battery": 55, "led_status": led}
        ),
    )
    assert robot.first("error") is None, robot.sent
    relayed = dash.first("robot_state")
    assert relayed["data"]["status_summary"]["led_status"] == led
    async with get_session() as s:
        row = (await s.execute(select(Robot).where(Robot.name == "jetson01"))).scalars().one()
        assert row.battery == 55


# ── 명령 메시지 계약 ────────────────────────────────────────────────────────

async def test_command_envelope_matches_json_schema(dash, robot, client, auth_headers):
    """조립한 명령이 command.schema.json을 통과해야 한다(목적지 enum 강제 - 4번)."""
    import json
    import pathlib

    import jsonschema

    await handle_robot_frame(
        robot, state_frame(robot_id="jetson01", status_summary={"battery": 90})
    )
    await _post_arrival(client, auth_headers, "gate2-cmd-0")
    command = robot.first("command")
    assert command is not None, robot.types()

    schema_path = pathlib.Path(__file__).resolve().parents[1] / "schemas" / "command.schema.json"
    jsonschema.validate(command["data"], json.loads(schema_path.read_text(encoding="utf-8")))
    assert command["data"]["cmd_destination"] == "OUTDOOR_TAGGING"
