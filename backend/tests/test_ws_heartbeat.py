"""서버 쪽 하트비트 미스 판정 (교차검증 8번).

소켓이 끊긴 걸 OS가 늘 알려주지는 않는다(전원 차단·Wi-Fi 이탈). pong이 연속으로 안 오면
서버가 스테일로 보고 커넥션을 닫아야 robot_count가 실제와 맞는다. DB를 안 타는 경로라
가짜 소켓으로 본다.
"""
import asyncio
from typing import Any

import pytest

from app.ws import robot_heartbeat, robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")


class SilentSocket:
    """ping을 받아도 pong을 안 보내는 로봇."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed_with: int | None = None

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


class PongingSocket(SilentSocket):
    """ping을 받으면 바로 pong으로 답하는 로봇."""

    async def send_json(self, message: dict[str, Any]) -> None:
        await super().send_json(message)
        if message.get("type") == "ping":
            robot_manager.note_pong(self)


class DeadSocket(SilentSocket):
    """send 자체가 터지는 소켓(이미 닫힌 커넥션)."""

    async def send_json(self, message: dict[str, Any]) -> None:
        raise RuntimeError("소켓이 이미 닫혔다")


async def test_silent_connection_is_dropped_as_stale():
    before = robot_manager.robot_count
    sock = SilentSocket()
    await robot_manager.connect(sock)
    try:
        # 미스 1까지 허용 → ping 두 번을 못 받으면 닫힌다.
        await asyncio.wait_for(robot_heartbeat(sock, 0.01, 1), timeout=2.0)
        assert sock.closed_with == 1001
        assert robot_manager.robot_count == before
    finally:
        robot_manager.disconnect(sock)


async def test_ponging_connection_survives():
    before = robot_manager.robot_count
    sock = PongingSocket()
    await robot_manager.connect(sock)
    task = asyncio.create_task(robot_heartbeat(sock, 0.01, 1))
    try:
        await asyncio.sleep(0.15)
        assert not task.done(), "pong을 답했는데 하트비트가 끝났다"
        assert len([m for m in sock.sent if m["type"] == "ping"]) >= 3
        assert robot_manager.robot_count == before + 1
        assert sock.closed_with is None
    finally:
        task.cancel()
        robot_manager.disconnect(sock)


async def test_send_failure_drops_connection():
    """ping send가 터지면 태스크가 남지 않고 커넥션이 목록에서 빠진다(9번 try/except)."""
    before = robot_manager.robot_count
    sock = DeadSocket()
    await robot_manager.connect(sock)
    try:
        await asyncio.wait_for(robot_heartbeat(sock, 0.01, 5), timeout=2.0)
        assert robot_manager.robot_count == before
    finally:
        robot_manager.disconnect(sock)


async def test_frame_traffic_keeps_silent_connection_alive():
    """상태 프레임이 올라오는 동안은 pong이 없어도 안 끊는다(젯슨 브리지 거동).

    젯슨 `WebSocketClient`는 소켓을 **읽지 않아서** ping을 보고도 답을 못 한다. pong만 세면
    상태를 초당 몇 장씩 올리는 로봇이 15초 × 미스 2번마다 1001로 끊겨 재접속 루프를 돈다.
    프레임 도착을 살아 있다는 증거로 세면(`note_activity`, 수신부가 부른다) 그 루프가 멎는다.

    ⚠ 스테일 판정을 없앤 게 아니다 — 프레임이 끊기면 그대로 닫혀야 한다. 한 케이스에서 두
    방향을 다 본다(한 방향만 보면 "안 끊긴다"를 통과시키려고 판정을 죽여도 초록이다).
    """
    before = robot_manager.robot_count
    sock = SilentSocket()
    await robot_manager.connect(sock)
    task = asyncio.create_task(robot_heartbeat(sock, 0.01, 1))
    try:
        for _ in range(15):
            await asyncio.sleep(0.01)
            robot_manager.note_activity(sock)   # 수신부가 프레임 한 장을 받은 자리
        assert not task.done(), "프레임이 올라오는데 하트비트가 커넥션을 끊었다"
        assert sock.closed_with is None
        assert robot_manager.robot_count == before + 1

        # 프레임이 끊기면 옛 거동 그대로 스테일로 닫힌다.
        await asyncio.wait_for(task, timeout=2.0)
        assert sock.closed_with == 1001
        assert robot_manager.robot_count == before
    finally:
        task.cancel()
        robot_manager.disconnect(sock)
