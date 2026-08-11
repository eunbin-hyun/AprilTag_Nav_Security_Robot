"""대시보드 WS(/ws/dashboard) 구독 계약 시험.

README "대시보드 WebSocket 이벤트 목록" 절이 적어둔 종류·페이로드를 코드로 못박는다.
문서와 코드가 갈라지면 여기서 깨지게 하는 게 목적이다.

브로드캐스트를 가로채서 본다 — 실 소켓을 붙이면 테스트가 자기 이벤트 루프를 새로 열어
asyncpg 커넥션 풀이 다른 루프에 묶이는 사고가 난다. 인입에서 메시지가 나가는지까지는
가로채기로 충분히 관통 검증된다.
"""
import datetime as dt

import pytest

from app.ws import ConnectionManager, RobotConnectionManager, make_envelope, manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 27, 3, 0, 0, tzinfo=dt.timezone.utc)
ENVELOPE_KEYS = {"type", "version", "robot_id", "timestamp", "data"}


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


@pytest.fixture
def sent(monkeypatch):
    """manager.broadcast를 가로채 메시지를 모은다. monkeypatch가 케이스 끝에 원복한다."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


def _types(box: list[dict]) -> list[str]:
    return [m["type"] for m in box]


async def test_메시지는_다섯_칸_고정(sent):
    """아키텍처 06절 — {type, version, robot_id, timestamp, data} 한 겹 메시지."""
    env = make_envelope("hello", {"channel": "dashboard", "clients": 1})
    assert set(env) == ENVELOPE_KEYS
    assert env["type"] == "hello"
    assert env["robot_id"] is None
    assert dt.datetime.fromisoformat(env["timestamp"]).tzinfo is not None
    assert set(env["data"]) == {"channel", "clients"}


async def test_태깅_인입이_메시지로_나간다(client, auth_headers, sent):
    await client.post(
        "/api/tagging-events",
        json={"event_id": "t-ws", "device_id": "raspberry01", "gate_no": 1,
              "tag_id": "UID-1", "observed_at": _iso(0)},
        headers=auth_headers,
    )
    assert _types(sent) == ["tagging_event"]
    env = sent[0]
    assert set(env) == ENVELOPE_KEYS
    assert set(env["data"]) == {"event_id", "gate_no", "tag_id", "observed_at"}


async def test_정상_통과_메시지(client, auth_headers, sent):
    await client.post(
        "/api/tagging-events",
        json={"event_id": "t-ok", "device_id": "raspberry01", "gate_no": 1,
              "tag_id": "UID-1", "observed_at": _iso(0)},
        headers=auth_headers,
    )
    sent.clear()
    await client.post(
        "/api/gate-pass-events",
        json={"event_id": "p-ok", "device_id": "raspberry01", "gate_no": 1,
              "direction": "A_TO_B", "status": "complete",
              "beam_a_ts": _iso(1), "beam_b_ts": _iso(1.2), "observed_at": _iso(1)},
        headers=auth_headers,
    )
    # 정상 통과는 판정 메시지 한 장만. 경고는 안 나간다.
    assert _types(sent) == ["gate_pass_event"]
    payload = sent[0]["data"]
    assert set(payload) == {
        "event_id", "gate_no", "direction", "status",
        "verdict", "matched_tagging_event_id", "low_confidence",
        # 통과 행 PK. untagged_alert의 source_id와 잇는 열쇠다 — 값이 같다는 계약은
        # tests/test_ws_pass_id_link.py가 못박는다.
        "pass_id",
        # 태운 크레딧의 카드 UID(S15P11C207-241). 늘 실리고 소비가 없으면 null이다 —
        # 갈래별 값은 tests/test_credit_identity.py.
        "tag_id",
        # 기기 1차 판단(참고값). 안 보내는 기기면 null로 실린다 — 자세한 건
        # tests/test_device_result_field.py.
        "device_result",
        # ⭐ 카드 주인. `tag_id`를 명부에서 찾아 채우고 명부에 없으면 둘 다 null이다.
        # **늘 싣는다** — 있고 없고로 갈리면 화면이 칸의 유무를 먼저 검사해야 한다
        # (`pass_id`·`tag_id`와 같은 규칙). 프론트 11차 §4-2, 사용자 확정.
        "staff_name", "staff_student_no",
    }
    assert payload["verdict"] == "normal"
    assert payload["matched_tagging_event_id"] is not None
    # 판정 메시지 한 장으로 "누구 크레딧이 탔나"까지 온다(-241). 태깅 UID와 같은 값이다.
    assert payload["tag_id"] == "UID-1"


async def test_미태깅은_메시지_두_장(client, auth_headers, sent):
    """시연 ④ — 판정 메시지에 이어 경고 팝업 메시지가 따라 나간다."""
    await client.post(
        "/api/gate-pass-events",
        json={"event_id": "p-untagged", "device_id": "raspberry01", "gate_no": 1,
              "direction": "A_TO_B", "status": "complete",
              "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)},
        headers=auth_headers,
    )
    assert _types(sent) == ["gate_pass_event", "untagged_alert"]
    assert sent[0]["data"]["verdict"] == "untagged"
    alert = sent[1]["data"]
    assert set(alert) == {"alert_id", "gate_no", "source_type", "source_id", "low_confidence"}
    assert alert["source_type"] == "gate_pass"
    assert alert["alert_id"] is not None


async def test_셔틀_도착_메시지(client, auth_headers, sent):
    await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "s-ws", "gate_no": 2, "shuttle_no": "SH-07", "signal_ts": _iso(0)},
        headers=auth_headers,
    )
    assert _types(sent) == ["shuttle_arrival"]
    # notified_robot_count·command_id는 로봇 목적지 명령 통지가 붙으면서 늘어난 칸이다
    # (S15P11C207-156). 붙은 로봇이 없으면 통지 수는 0, 명령 id는 None으로 온다.
    # pending은 출동 대기 창이 붙으면서 늘어난 칸이다(2026-07-31 셔틀출동 설계 §2.2) —
    # 마감 시각·남은 초·버튼 셋이 들어 있고, 창을 안 열었으면 null이다. 이 알림 한 장으로
    # 화면이 카운트다운까지 그린다. 창 자체의 시나리오는 tests/test_shuttle_dispatch.py.
    assert set(sent[0]["data"]) == {
        "event_id", "gate_no", "shuttle_no", "notified_robot_count", "command_id",
        "pending",
        # 도착 행 PK. 셔틀 계열 경고의 source_id와 shuttle_dispatch_result의 arrival_id가
        # 같은 값이라, 창을 안 연 갈래(pending=null)에서도 화면이 두 메시지를 묶는다.
        "arrival_id",
    }
    assert sent[0]["data"]["arrival_id"] == sent[0]["data"]["pending"]["arrival_id"]
    # 명령은 아직 안 나갔다 — 5초 창이 열렸을 뿐이다.
    assert sent[0]["data"]["notified_robot_count"] == 0
    assert sent[0]["data"]["pending"]["deadline"] > sent[0]["data"]["pending"]["opened_at"]


async def test_중복_인입은_두_번_안_밀어낸다(client, auth_headers, sent):
    """멱등 — 라파이 재전송이 화면에 같은 줄을 두 번 그리면 안 된다."""
    body = {"event_id": "t-dup", "device_id": "raspberry01", "gate_no": 1,
            "tag_id": "UID-1", "observed_at": _iso(0)}
    await client.post("/api/tagging-events", json=body, headers=auth_headers)
    await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert _types(sent) == ["tagging_event"]


async def test_확인_처리가_alert_ack_메시지로_퍼진다(client, auth_headers, sent):
    """대시보드 두 대 중 한쪽이 확인하면 다른 쪽 화면도 그 자리에서 지워져야 한다.

    예전엔 이게 알려진 구멍이었다(한쪽에서 확인한 경고가 다른 쪽엔 그대로 남았다).
    S15P11C207-81에서 `alert_ack` 메시지로 메웠다.
    """
    await client.post(
        "/api/gate-pass-events",
        json={"event_id": "p-ack-ws", "device_id": "raspberry01", "gate_no": 1,
              "direction": "A_TO_B", "status": "complete",
              "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)},
        headers=auth_headers,
    )
    alert_id = sent[1]["data"]["alert_id"]
    sent.clear()

    assert (await client.post(f"/api/alerts/{alert_id}/ack", headers=auth_headers)).status_code == 200
    assert _types(sent) == ["alert_ack"]
    payload = sent[0]["data"]
    assert set(payload) == {"alert_id", "type", "severity", "ack"}
    assert payload["alert_id"] == alert_id
    assert payload["type"] == "untagged"
    assert payload["severity"] == "high"
    assert payload["ack"] is True


async def test_같은_경고를_두_번_확인해도_메시지는_한_장(client, auth_headers, sent):
    """이미 확인한 경고를 또 확인하는 건 사건이 아니다(멱등 인입과 같은 규칙).

    두 번째도 밀어내면 대시보드 두 대가 서로의 재확인을 주고받으며 같은 줄을 계속 다시 그린다.
    """
    await client.post(
        "/api/gate-pass-events",
        json={"event_id": "p-ack-twice", "device_id": "raspberry01", "gate_no": 1,
              "direction": "A_TO_B", "status": "complete",
              "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)},
        headers=auth_headers,
    )
    alert_id = sent[1]["data"]["alert_id"]
    sent.clear()

    first = await client.post(f"/api/alerts/{alert_id}/ack", headers=auth_headers)
    second = await client.post(f"/api/alerts/{alert_id}/ack", headers=auth_headers)
    # 응답은 둘 다 200에 ack=true다 — 화면이 두 번 눌러도 오류를 안 본다.
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["ack"] is True
    assert _types(sent) == ["alert_ack"], "재확인이 메시지를 한 장 더 냈다"


async def test_없는_경고_확인은_메시지를_안_낸다(client, auth_headers, sent):
    assert (await client.post("/api/alerts/999999/ack", headers=auth_headers)).status_code == 404
    assert sent == []


async def test_죽은_커넥션은_잘라내고_나머지는_간다():
    """한 화면이 탭을 닫아도 나머지 화면은 계속 받아야 한다."""
    class _Sock:
        def __init__(self, alive: bool) -> None:
            self.alive, self.got = alive, []

        async def accept(self) -> None:
            return None

        async def send_json(self, message: dict) -> None:
            if not self.alive:
                raise RuntimeError("closed")
            self.got.append(message)

    mgr = ConnectionManager()
    alive, dead = _Sock(True), _Sock(False)
    await mgr.connect(alive)
    await mgr.connect(dead)
    assert mgr.dashboard_count == 2

    await mgr.broadcast(make_envelope("tagging_event", {"event_id": "t-1"}))
    assert len(alive.got) == 1
    assert mgr.dashboard_count == 1  # 죽은 쪽만 빠진다


class _CloseSpySock:
    """보내기가 실패하고 닫힘을 기록하는 소켓. 라우트 루프 대신 `closed`로 판정한다."""

    def __init__(self, alive: bool) -> None:
        self.alive, self.got, self.closed = alive, [], None

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict) -> None:
        if not self.alive:
            raise RuntimeError("closed")
        self.got.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed = code


async def test_보내기_실패한_대시보드_소켓은_닫힌다():
    """반증 자리 — 목록에서 빼기만 하면 라우트 루프가 죽은 화면을 계속 붙잡는다."""
    mgr = ConnectionManager()
    alive, dead = _CloseSpySock(True), _CloseSpySock(False)
    await mgr.connect(alive)
    await mgr.connect(dead)

    await mgr.broadcast(make_envelope("tagging_event", {"event_id": "t-2"}))

    assert dead.closed is not None, "보내기가 실패했는데 소켓을 안 닫았다"
    assert alive.closed is None
    assert mgr.dashboard_count == 1


async def test_명령_보내기가_실패한_로봇_소켓은_닫힌다():
    """⭐ 블랙홀 반증 자리.

    목록에서만 빼면 라우트 루프가 계속 돌고, 로봇이 상태를 다시 올려도 `register`가 없는
    키에 쓰려다 아무 일도 안 한다(no-op). 하트비트도 못 잡는다 — 프레임이 올라올 때마다
    미스가 0으로 되돌아가기 때문이다. 그러면 그 로봇은 붙어 있는데 명령을 영영 못 받는다.
    소켓까지 닫아야 라우트가 `finally`로 빠지고 재접속으로 되살아난다.
    """
    mgr = RobotConnectionManager()
    dead = _CloseSpySock(False)
    await mgr.connect(dead)
    mgr.register(dead, "jetson01")
    assert mgr.robot_count == 1

    delivered = await mgr.send_command({"command_id": "c-1", "type": "cmd_destination"})

    assert delivered == []
    assert dead.closed is not None, "명령 전송이 실패했는데 소켓을 안 닫았다"
    assert mgr.robot_count == 0

    # 닫은 뒤 같은 소켓이 다시 자기를 밝혀도 목록에 안 들어온다(재등록은 no-op이다).
    mgr.register(dead, "jetson01")
    assert mgr.robot_ids() == []
