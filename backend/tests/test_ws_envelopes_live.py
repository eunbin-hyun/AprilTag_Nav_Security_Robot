"""새 메시지 2종이 **실 브로드캐스트 경로**로 대시보드까지 가는지 본다 (S15P11C207-81).

가로채기(monkeypatch broadcast)나 가짜 소켓은 "브로드캐스트를 불렀나"까지만 본다. 실제로
대시보드 소켓까지 나가는지는 여기서 확인한다 — 라우터가 브로드캐스트를 안 부르거나 메시지가
직렬화에서 깨지면 앞엣것들은 전부 초록인데 화면만 조용하다.

⚠ 여기는 **전선(TCP)이 아니다.** starlette TestClient의 WebSocket은 ASGI 앱을 인프로세스로
부르는 통이라 HTTP 파서·헤더 인코딩·핸드셰이크 거절 코드처럼 전선에서만 드러나는 것은 못
본다. 그 층은 실 uvicorn을 띄우는 `test_ws_auth_live.py`가 따로 맡는다. 이 파일이 맡는 건
"인입 → 브로드캐스트 → 대시보드 소켓" 배선 하나다(층 가르기, 2026-08-02 백지 검토 C7).

test_ws_channels.py와 같은 규칙을 따른다.
- httpx(ASGITransport)는 WebSocket을 못 여니 starlette TestClient를 쓴다. HTTP 인입도 같은
  TestClient로 보내야 한 루프 안에서 브로드캐스트가 붙는다.
- TestClient는 자기 스레드·루프를 새로 판다. 세션 루프에 묶인 asyncpg 커넥션을 그 루프에서
  재사용하면 "attached to a different loop"로 죽으니, DB를 타는 케이스 앞뒤로 풀을 비운다.
- 이 파일 케이스는 전부 **동기**다. async 케이스 안에서 asyncio.run(dispose)을 부르면
  "cannot be called from a running event loop"로 죽는다.
"""
import asyncio
import datetime as dt
import json
from typing import Any

from starlette.testclient import TestClient

from app.config import get_settings
from app.db import engine
from app.main import app

API_KEY = get_settings().api_key
CARD_KEYS = {
    "id", "name", "mode", "battery", "network_status", "updated_at",
    "position", "led_status", "mission_status",
}
ACK_KEYS = {"alert_id", "type", "severity", "ack"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _dispose_pool() -> None:
    """커넥션 풀을 비운다. 세션 루프를 지우지 않고.

    ⚠ `asyncio.run`은 끝나면서 MainThread의 현재 이벤트 루프를 None으로 지운다.
    pytest-asyncio가 세션 루프를 그 자리에서 찾기 때문에, 지워 두면 이 파일 뒤에 오는
    async 시험이 통째로 깨진다(test_ws_channels.py가 먼저 물린 함정).
    """
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    try:
        asyncio.run(engine.dispose())
    finally:
        if previous is not None:
            asyncio.set_event_loop(previous)


def _recv(ws, timeout: float = 5.0) -> dict[str, Any]:
    """메시지 하나를 기다리되 무한정은 아니다.

    starlette `receive_json()`은 큐를 timeout 없이 기다린다 — 메시지가 안 나오는 회귀에서
    시험이 빨강이 아니라 **멈춤**이 된다(수리 전 빨강을 볼 수가 없고, 배치 전체가 선다).
    그래서 내부 큐를 timeout으로 집는다.
    """
    message = ws._send_queue.get(timeout=timeout)
    if isinstance(message, BaseException):
        raise message
    assert message["type"] == "websocket.send", f"서버가 소켓을 닫았다: {message}"
    return json.loads(message["text"])


def _robot_frame(robot_id: str, **summary: Any) -> dict[str, Any]:
    return {
        "type": "robot_state",
        "version": "1",
        "robot_id": robot_id,
        "timestamp": _now(),
        "data": {"robot_id": robot_id, "status_summary": summary},
    }


def test_브로드캐스트_로봇_보고가_robot_status까지_간다():
    _dispose_pool()
    try:
        with TestClient(app) as tc, tc.websocket_connect("/ws/dashboard") as dash:
            _recv(dash)  # hello
            with tc.websocket_connect("/ws/robot") as robot:
                _recv(robot)  # registered
                robot.send_json(
                    _robot_frame(
                        "jetson-live", battery=33, mode="OUTDOOR_TAGGING", comm_status="WS_OK"
                    )
                )
                ack = _recv(robot)
                # ⚠ 이 확인이 없으면 프레임 검증 실패(error 메시지)가 조용히 통과하고,
                # 대시보드가 아무것도 못 받은 이유를 "메시지 미구현"으로 잘못 읽는다.
                assert ack["type"] == "state_ack", ack
                got = [_recv(dash), _recv(dash)]
    finally:
        _dispose_pool()

    types = [m["type"] for m in got]
    assert "robot_state" in types, types
    assert "robot_status" in types, types
    card = got[types.index("robot_status")]
    assert card["robot_id"] == "jetson-live"
    assert set(card["data"]) == CARD_KEYS
    assert card["data"]["battery"] == 33
    assert card["data"]["network_status"] == "WS_OK"


def test_브로드캐스트_위치LED가_메시지와_조회응답까지_간다():
    """위치·LED는 DB 컬럼이 아니라 인메모리다 — 가짜 소켓만 보면 '메모리엔 들었는데
    소켓·HTTP로는 안 나가는' 회귀를 못 잡는다. 브로드캐스트로 나간 메시지와 /api/robots를 같이 본다.
    """
    _dispose_pool()
    pos = {"x": 7.5, "y": 8.25, "theta": -1.0}
    try:
        with TestClient(app) as tc, tc.websocket_connect("/ws/dashboard") as dash:
            _recv(dash)  # hello
            with tc.websocket_connect("/ws/robot") as robot:
                _recv(robot)  # registered
                frame = _robot_frame("jetson-pos", battery=61, comm_status="WS_OK")
                frame["data"]["status_summary"]["position"] = pos
                frame["data"]["status_summary"]["led_status"] = "AMBER"
                robot.send_json(frame)
                assert _recv(robot)["type"] == "state_ack"
                got = [_recv(dash), _recv(dash)]
            listed = tc.get("/api/robots")
            snap = tc.get("/api/dashboard/snapshot")
    finally:
        _dispose_pool()

    types = [m["type"] for m in got]
    card = got[types.index("robot_status")]["data"]
    assert set(card) == CARD_KEYS
    assert card["position"] == pos
    assert card["led_status"] == "AMBER"

    assert listed.status_code == 200, listed.text
    row = [r for r in listed.json() if r["name"] == "jetson-pos"][0]
    assert row["position"] == pos
    assert row["led_status"] == "AMBER"

    assert snap.status_code == 200, snap.text
    snap_row = [r for r in snap.json()["robots"] if r["name"] == "jetson-pos"][0]
    assert snap_row["position"] == pos
    assert set(snap_row) == CARD_KEYS


def test_브로드캐스트_경고_확인이_alert_ack까지_간다():
    """대시보드 두 대 중 한쪽이 확인하면 다른 쪽 화면도 그 자리에서 지워져야 한다."""
    _dispose_pool()
    try:
        with TestClient(app) as tc, tc.websocket_connect("/ws/dashboard") as dash:
            _recv(dash)  # hello
            resp = tc.post(
                "/api/gate-pass-events",
                json={"event_id": "WS-ACK-1", "gate_no": 7, "direction": "A_TO_B",
                      "status": "complete", "beam_a_ts": _now(), "observed_at": _now()},
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["verdict"] == "untagged"

            _recv(dash)  # gate_pass_event
            alert_env = _recv(dash)
            assert alert_env["type"] == "untagged_alert"
            alert_id = alert_env["data"]["alert_id"]

            acked = tc.post(f"/api/alerts/{alert_id}/ack", headers={"X-API-Key": API_KEY})
            assert acked.status_code == 200, acked.text
            ack_env = _recv(dash)
    finally:
        _dispose_pool()

    assert ack_env["type"] == "alert_ack", ack_env
    assert set(ack_env["data"]) == ACK_KEYS
    assert ack_env["data"]["alert_id"] == alert_id
    assert ack_env["data"]["type"] == "untagged"
    assert ack_env["data"]["ack"] is True
