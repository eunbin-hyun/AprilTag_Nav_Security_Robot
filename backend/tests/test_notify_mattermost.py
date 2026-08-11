"""무단 통과 → 매터모스트 Incoming Webhook 발사 (S15P11C207-82).

⚠ **실제 팀 채널로는 절대 안 보낸다.** 여기 케이스는 전부 127.0.0.1에 띄운 목 수신 서버로만
간다. 웹훅 URL은 픽스처가 그 목 서버 주소로 덮어쓰고 케이스가 끝나면 되돌린다(검사 위생).

무엇을 못박나.
- URL이 비면 조용히 스킵이 기본이다(설정 안 한 배포가 로그를 어지럽히면 안 된다).
- URL을 넣으면 그때부터 실제로 POST가 나간다. 예전엔 MATTERMOST_ENABLED까지 켜야 했는데,
  URL만 넣고 아무 일도 안 일어나는 쪽이 배포에서 더 위험한 함정이라 URL 유무를 기본 스위치로
  삼았다. 명시적으로 끈 배포(false)는 URL이 있어도 그대로 조용하다.
- 웹훅이 느리거나 4xx를 뱉어도 무단 통과 판정·경고 행·대시보드 메시지는 그대로 간다.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import func, select

from app import notify
from app.config import get_settings
from app.db import get_session
from app.models import Alert
from app.notify import send_untagged_alert

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 28, 3, 0, 0, tzinfo=dt.timezone.utc)


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


class _Recorder(BaseHTTPRequestHandler):
    """받은 JSON 본문을 모아 두는 최소 수신기. 응답 코드·지연은 서버가 들고 있다."""

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 계약
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        if self.server.delay:
            time.sleep(self.server.delay)
        try:
            self.server.received.append(json.loads(raw))
        except ValueError:
            self.server.received.append({"_raw": raw.decode("utf-8", "replace")})
        try:
            self.send_response(self.server.status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            # 클라이언트가 타임아웃으로 먼저 끊은 경우다. 시험이 보려던 그림이라 삼킨다.
            pass

    def log_message(self, *args) -> None:  # 시험 출력이 지저분해지는 걸 막는다
        return None


class _MockMattermost(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Recorder)
        self.received: list[dict] = []
        self.status = 200
        self.delay = 0.0

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/hooks/mock"

    def handle_error(self, request, client_address) -> None:
        # 타임아웃 케이스에서 끊긴 소켓 traceback이 시험 출력에 쏟아지는 걸 막는다.
        return None


@pytest.fixture
def mm_server():
    """로컬 목 매터모스트. 반드시 여기서 닫는다 — 스레드가 새면 다음 케이스가 흔들린다."""
    server = _MockMattermost()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def mm_config():
    """설정을 케이스 안에서만 바꾼다. get_settings는 lru_cache라 인스턴스가 하나뿐이다."""
    settings = get_settings()
    before = (settings.mattermost_enabled, settings.mattermost_webhook_url)
    try:
        yield settings
    finally:
        settings.mattermost_enabled, settings.mattermost_webhook_url = before


async def test_웹훅_미설정이면_조용히_스킵(mm_config):
    """URL을 안 넣은 배포가 기본이다 — 아무 데도 안 쏘고 False."""
    mm_config.mattermost_webhook_url = ""
    sent = await send_untagged_alert(gate_no=1, alert_id=1, source_id=1, low_confidence=False)
    assert sent is False


async def test_URL만_넣으면_카드가_실제로_날아간다(mm_config, mm_server):
    mm_config.mattermost_webhook_url = mm_server.url
    sent = await send_untagged_alert(gate_no=3, alert_id=77, source_id=42, low_confidence=False)

    assert sent is True, "URL을 넣었는데 발사가 안 됐다"
    assert len(mm_server.received) == 1
    text = mm_server.received[0]["text"]
    assert "무단 통과" in text
    assert "3번" in text and "77" in text and "42" in text
    assert "신뢰도" not in text  # low_confidence=False면 그 줄은 없다


async def test_저신뢰_표시가_카드에_실린다(mm_config, mm_server):
    mm_config.mattermost_webhook_url = mm_server.url
    await send_untagged_alert(gate_no=1, alert_id=2, source_id=3, low_confidence=True)
    assert "신뢰도" in mm_server.received[0]["text"]


async def test_명시적으로_끄면_URL이_있어도_안_쏜다(mm_config, mm_server):
    """킬 스위치 — URL을 지우지 않고 발사만 멈추는 길이 배포에 필요하다."""
    mm_config.mattermost_webhook_url = mm_server.url
    mm_config.mattermost_enabled = False
    sent = await send_untagged_alert(gate_no=1, alert_id=1, source_id=1, low_confidence=False)
    assert sent is False
    assert mm_server.received == []


async def test_4xx_응답은_실패로_센다(mm_config, mm_server):
    """웹훅 URL이 폐기됐거나 본문이 거부되면 매터모스트는 200을 안 준다.

    예외가 안 나서 조용히 '보냈다'로 세면 아무도 못 받은 경고를 성공으로 기록한다.
    """
    mm_config.mattermost_webhook_url = mm_server.url
    mm_server.status = 400
    sent = await send_untagged_alert(gate_no=1, alert_id=9, source_id=9, low_confidence=False)
    assert sent is False
    assert len(mm_server.received) == 1  # 몸통은 갔고, 판정만 실패다


async def test_웹훅이_느려도_예외가_안_샌다(mm_config, mm_server, monkeypatch):
    """타임아웃이 호출자로 새면 미태깅 인입이 통째로 500이 된다.

    ⚠ **여유를 넉넉히 둔다**(2026-08-05). 예전에는 지연 1.0초에 문턱 0.9초라 여유가
    0.7초뿐이었고, 전량을 4워커로 돌리면 기계가 바빠 그 여유를 넘겨 **가짜로 빨개졌다**
    (두 판 연속 실측 — 단독으로는 늘 통과). 재는 것은 "1.0초를 다 안 기다렸나"이지
    정확한 시간이 아니므로, 지연을 늘려 갈림을 벌린다.
    """
    monkeypatch.setattr(notify, "MATTERMOST_TIMEOUT_S", 0.2)
    mm_config.mattermost_webhook_url = mm_server.url
    mm_server.delay = 3.0

    started = time.monotonic()
    sent = await send_untagged_alert(gate_no=1, alert_id=5, source_id=5, low_confidence=False)
    elapsed = time.monotonic() - started

    assert sent is False
    # 타임아웃이 안 걸리면 3초를 꼬박 기다린다. 2.0초 문턱은 그 갈림을 잡으면서
    # 부하로 늘어지는 정리 시간(0.2초 뒤 소켓 닫기)을 넉넉히 덮는다.
    assert elapsed < 2.0, f"타임아웃이 안 걸렸다({elapsed:.2f}초)"


async def test_미태깅_인입이_웹훅까지_관통한다(client, auth_headers, mm_config, mm_server):
    """지라 -82 본체 — 경고가 생기는 순간 카드가 나간다(목 서버로만)."""
    mm_config.mattermost_webhook_url = mm_server.url

    r = await client.post(
        "/api/gate-pass-events",
        json={"event_id": "p-mm-1", "device_id": "raspberry01", "gate_no": 5,
              "direction": "A_TO_B", "status": "complete",
              "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "untagged"

    assert len(mm_server.received) == 1, "미태깅 경고가 났는데 카드가 안 나갔다"
    assert "5번" in mm_server.received[0]["text"]


async def test_웹훅이_죽어도_경고는_남는다(client, auth_headers, mm_config, monkeypatch):
    """알림 발사 실패가 판정·경고 행·응답을 되돌리면 안 된다(커밋 뒤 호출)."""
    monkeypatch.setattr(notify, "MATTERMOST_TIMEOUT_S", 0.2)
    # 아무도 안 듣는 자리로 쏜다 — 연결 거부가 그대로 예외로 올라오는 경로다.
    mm_config.mattermost_webhook_url = "http://127.0.0.1:9/hooks/dead"

    r = await client.post(
        "/api/gate-pass-events",
        json={"event_id": "p-mm-2", "device_id": "raspberry01", "gate_no": 6,
              "direction": "A_TO_B", "status": "complete",
              "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "untagged"

    async with get_session() as s:
        count = (
            await s.execute(
                select(func.count()).select_from(Alert).where(Alert.type == "untagged")
            )
        ).scalar_one()
    assert count == 1, "웹훅 실패가 경고 행까지 되돌렸다"
