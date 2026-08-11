"""실 WebSocket 왕복 (S15P11C207-156).

httpx는 WebSocket을 못 다뤄서 Starlette `TestClient.websocket_connect`를 쓴다. TestClient는
동기라 자기 이벤트 루프를 따로 돌리는데, 세션 루프에 붙은 asyncpg 엔진을 두 루프가 같이
쓰면 깨진다. 그래서 여기 케이스는 **DB를 안 타는 경로만** 고르고, TestClient를 별 스레드로
밀어 세션 루프를 비워 둔다. DB를 타는 판정·중계는 test_robot_channel.py가 본다.
"""
import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.ws import robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _dashboard_hello() -> dict[str, Any]:
    with TestClient(app) as client:
        with client.websocket_connect("/ws/dashboard") as ws:
            return ws.receive_json()


def _robot_roundtrip(frames: list[Any]) -> list[dict[str, Any]]:
    """registered 메시지를 받고, 준 프레임을 차례로 보내며 응답을 모은다."""
    received: list[dict[str, Any]] = []
    with TestClient(app) as client:
        with client.websocket_connect("/ws/robot") as ws:
            received.append(ws.receive_json())
            for frame in frames:
                if isinstance(frame, str):
                    ws.send_text(frame)
                else:
                    ws.send_json(frame)
                received.append(ws.receive_json())
    return received


async def test_dashboard_sends_hello():
    msg = await asyncio.to_thread(_dashboard_hello)
    assert msg["type"] == "hello"
    assert msg["version"] == "1"
    assert msg["data"]["channel"] == "dashboard"


async def test_robot_handshake_reports_heartbeat():
    received = await asyncio.to_thread(_robot_roundtrip, [])
    assert received[0]["type"] == "registered"
    # 본문 칸은 `data`다. 두 전선을 통일한 뒤에도(2026-08-04) 예전 이름이 되살아나지
    # 않는지 여기서 지킨다 — 봉투를 두 벌로 되돌리는 회귀가 가장 먼저 걸리는 자리다.
    assert "payload" not in received[0], received[0]
    assert received[0]["data"]["channel"] == "robot"
    # 계약은 하트비트 10~20초다.
    assert 10 <= received[0]["data"]["heartbeat_sec"] <= 20


async def test_bad_frames_do_not_close_connection():
    """잘못된 프레임 넷을 연달아 보내도 연결이 살아 있어야 한다(로봇 재접속 루프 방지)."""
    frames = [
        "이건 JSON이 아니다",
        {"type": "no_such_type", "data": {}},
        {"type": "robot_state", "data": {"status_summary": {"battery": 10}}},  # robot_id 없음
        42,  # 객체가 아닌 프레임
    ]
    received = await asyncio.to_thread(_robot_roundtrip, frames)
    reasons = [m["data"].get("reason") for m in received[1:]]
    assert [m["type"] for m in received[1:]] == ["error"] * 4
    assert reasons == [
        "invalid_json",
        "unknown_frame_type",
        "invalid_robot_state",
        "frame_not_object",
    ]


async def test_robot_disconnect_clears_manager():
    """연결이 끝나면 커넥션 목록에서 빠져야 한다(finally 정리 확인)."""
    before = robot_manager.robot_count
    await asyncio.to_thread(_robot_roundtrip, [])
    assert robot_manager.robot_count == before
