"""더미 퍼블리셔(tools/dummy_publisher.py)로 실 이벤트를 흘려 끝까지 검증한다.

더미 퍼블리셔는 진짜 HTTP로만 쏘는 CLI라(httpx.Client + 실 소켓), 인입부터 크레딧
판정·소비·적재까지 관통 시나리오 시험 1건이다. 앱을 실제 uvicorn 서버로 별도 포트에
띄우고(같은 이벤트 루프에 백그라운드 태스크로 붙임), 블로킹 퍼블리셔 호출은
asyncio.to_thread로 스레드풀에 넘겨 서버 코루틴을 안 막게 한다.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest
import uvicorn
from sqlalchemy import select

# tools/는 __init__.py가 없는 네임스페이스 패키지라 backend/ 가 sys.path에 있어야 임포트된다.
_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app.credit.state_machine import CreditState, Verdict  # noqa: E402
from app.db import get_session  # noqa: E402
from app.main import app  # noqa: E402
from app.models import GatePassEvent, ShuttleArrival, TaggingEvent  # noqa: E402
from tools.dummy_publisher import Publisher  # noqa: E402

pytestmark = pytest.mark.asyncio(loop_scope="session")


# 기동·종료가 막히면 시험이 영원히 매달린다. 상한을 걸어 실패로 떨어뜨린다.
_STARTUP_TIMEOUT_SEC = 20.0
_SHUTDOWN_TIMEOUT_SEC = 20.0


async def _wait_started(server: uvicorn.Server) -> None:
    while not server.started:
        await asyncio.sleep(0.02)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def test_dummy_publisher_normal_scenario_end_to_end(auth_headers):
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    try:
        await asyncio.wait_for(_wait_started(server), timeout=_STARTUP_TIMEOUT_SEC)

        pub = Publisher(f"http://127.0.0.1:{port}", auth_headers["X-API-Key"])
        try:
            assert await asyncio.to_thread(
                pub.send_tagging, "e2edev", 9, "e2e-tag-1", 0
            ) is True
            assert await asyncio.to_thread(
                pub.send_gate_pass, "e2edev", 9, "A_TO_B", "complete", 0
            ) is True
            assert await asyncio.to_thread(
                pub.send_shuttle, "e2edev", 9, "SHUTTLE-E2E", 0
            ) is True
        finally:
            pub.close()

        async with get_session() as s:
            tag = (
                await s.execute(select(TaggingEvent).where(TaggingEvent.gate_no == 9))
            ).scalar_one()
            gp = (
                await s.execute(select(GatePassEvent).where(GatePassEvent.gate_no == 9))
            ).scalar_one()
            shuttle = (
                await s.execute(select(ShuttleArrival).where(ShuttleArrival.gate_no == 9))
            ).scalar_one()

        assert tag.credit_state == CreditState.CONSUMED
        assert gp.verdict == Verdict.NORMAL
        assert gp.matched_tagging_event_id == tag.id
        assert shuttle.shuttle_no == "SHUTTLE-E2E"
    finally:
        server.should_exit = True
        # 종료가 안 끝나도 매달리지 않게 상한을 건다. 넘기면 태스크를 취소해 루프를 비운다
        # (asyncio.wait_for가 타임아웃에 대상 태스크를 취소해준다).
        try:
            await asyncio.wait_for(server_task, timeout=_SHUTDOWN_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            pytest.fail("uvicorn 서버가 시간 안에 안 내려갔다")
