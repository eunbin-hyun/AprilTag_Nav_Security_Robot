"""WS 채널(/ws/dashboard·/ws/robot) 시험. 정찰 구멍 — 지금까지 WS 자체가 안 덮여 있었다.

httpx AsyncClient(ASGITransport)는 WebSocket을 못 여니 starlette TestClient를 쓴다.
HTTP 인입도 같은 TestClient로 보내야 한 이벤트 루프 안에서 브로드캐스트가 붙는다
(딴 루프에서 보낸 요청이 이 WebSocket 소켓으로 브로드캐스트되면 루프가 갈려 조용히 삼켜진다).

TestClient는 자기 전용 스레드·이벤트 루프를 새로 판다. app.db.engine의 커넥션 풀은
pytest-asyncio 세션 루프에 묶인 커넥션을 이미 들고 있을 수 있어서, 그 커넥션을 이
루프에서 재사용하면 "attached to a different loop"로 죽는다. DB를 건드리는 시험
앞뒤로 풀을 비워서(dispose) 커넥션이 항상 그 시점 루프에서 새로 열리게 한다.

로봇 채널(/ws/robot) 프로토콜 왕복은 여기서 안 본다 — tests/test_robot_ws.py가
실 소켓으로, tests/test_robot_channel.py가 가짜 소켓으로 이미 덮는다.
"""
import asyncio
import datetime as dt

from starlette.testclient import TestClient

from app.config import get_settings
from app.db import engine
from app.main import app

API_KEY = get_settings().api_key


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _dispose_pool() -> None:
    """커넥션 풀을 비운다. 세션 루프를 지우지 않고.

    ⚠ `asyncio.run`은 끝나면서 MainThread의 현재 이벤트 루프를 None으로 지운다.
    pytest-asyncio가 세션 루프를 그 자리에서 찾기 때문에, 지워 두면 이 파일 뒤에
    오는 async 시험이 통째로 "There is no current event loop"로 깨진다(동기 시험이라
    깨지는 자리가 여기가 아니라 뒷 파일이다 — 원인을 찾기 어려운 오염이다).
    그래서 원래 루프를 되돌려 놓는다.
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


def test_ws_dashboard_hello_envelope():
    with TestClient(app) as tc, tc.websocket_connect("/ws/dashboard") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["data"]["channel"] == "dashboard"
        assert hello["data"]["clients"] == 1


def test_ws_dashboard_receives_tagging_broadcast():
    _dispose_pool()
    try:
        with TestClient(app) as tc, tc.websocket_connect("/ws/dashboard") as ws:
            ws.receive_json()  # hello

            resp = tc.post(
                "/api/tagging-events",
                json={
                    "event_id": "WS1", "gate_no": 3, "tag_id": "20250099",
                    "observed_at": _now(),
                },
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 200, resp.text

            msg = ws.receive_json()
            assert msg["type"] == "tagging_event"
            assert msg["data"]["event_id"] == "WS1"
    finally:
        _dispose_pool()


def test_ws_dashboard_receives_untagged_alert_broadcast():
    _dispose_pool()
    try:
        with TestClient(app) as tc, tc.websocket_connect("/ws/dashboard") as ws:
            ws.receive_json()  # hello

            resp = tc.post(
                "/api/gate-pass-events",
                json={
                    "event_id": "WS2", "gate_no": 4, "direction": "A_TO_B", "status": "complete",
                    "beam_a_ts": _now(), "observed_at": _now(),
                },
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["verdict"] == "untagged"

            gate_pass_msg = ws.receive_json()
            assert gate_pass_msg["type"] == "gate_pass_event"
            alert_msg = ws.receive_json()
            assert alert_msg["type"] == "untagged_alert"
            assert alert_msg["data"]["gate_no"] == 4
    finally:
        _dispose_pool()
