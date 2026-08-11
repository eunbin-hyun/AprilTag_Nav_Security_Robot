"""바깥으로 나가는 POST 차단 그물 자체를 잰다 (2026-08-02 백지 검토 F58).

`conftest.block_outbound_post`가 매 케이스 앞에 `httpx.AsyncClient.post`를 감싼다. 그물이
조용히 벗겨지면(픽스처 순서가 뒤집히거나 누가 `httpx.AsyncClient`를 다시 갈아 끼우면) 미태깅을
만드는 케이스 수십 개가 **팀 보안 채널에 진짜 카드를 쏜다.** 시험을 한 번 돌릴 때마다 도배되고,
그걸 알아채는 길은 채널을 눈으로 보는 것뿐이다.

그물에 시험이 없으면 그물이 죽은 걸 못 본다. 날씨 쪽은 `test_weather_network_guard.py`가 그
자리를 맡고 있어서, 여기는 POST 쪽 같은 짝이다.

⚠ 이 파일은 바깥으로 아무것도 안 보낸다. 잣대 함수를 직접 부르고, 실제 발사는 "막혔나"만 본다.
"""
from __future__ import annotations

import httpx
import pytest

from app.config import get_settings
from app.notify import send_untagged_alert
from tests.conftest import OutboundPostBlocked, _post_target_allowed

# 판정표 케이스는 순수 함수라 sync다. 모듈 전체에 asyncio 마커를 걸면 그쪽이 경고를 뱉어서
# 케이스마다 붙인다.
_asyncio = pytest.mark.asyncio(loop_scope="session")

# 실제로 존재하지 않는 바깥 주소. 그물이 살아 있으면 여기까지 못 간다.
_OUTSIDE_WEBHOOK = "https://meeting.ssafy.com/hooks/should-never-fire"


@pytest.mark.parametrize(
    ("url", "allowed"),
    [
        pytest.param("http://127.0.0.1:8123/hooks/mock", True, id="loopback-ipv4"),
        pytest.param("http://localhost:8123/hooks/mock", True, id="loopback-이름"),
        pytest.param("http://test/api/alerts", True, id="ASGI-시험-호스트"),
        pytest.param("/api/alerts", True, id="상대경로"),
        pytest.param(_OUTSIDE_WEBHOOK, False, id="팀-채널"),
        pytest.param("https://hooks.slack.com/x", False, id="아무-바깥"),
        # ⚠ loopback "같아 보이는" 이름은 loopback이 아니다. 여기가 뚫리면
        #   `127.0.0.1.evil.example` 한 줄로 그물을 통째로 우회한다.
        pytest.param("http://127.0.0.1.evil.example/x", False, id="loopback-흉내"),
    ],
)
def test_바깥_주소_판정표(url, allowed):
    assert _post_target_allowed(url) is allowed


@_asyncio
async def test_바깥으로_POST를_내면_가드가_던진다():
    """가드가 실제로 붙어 있나. 이름이 아니라 **거동**으로 확인한다."""
    async with httpx.AsyncClient() as client:
        with pytest.raises(OutboundPostBlocked):
            await client.post(_OUTSIDE_WEBHOOK, json={"text": "안 나가야 한다"})


@_asyncio
async def test_매터모스트_웹훅이_바깥이어도_카드가_안_나간다(monkeypatch):
    """`.env`에 진짜 웹훅이 남아 있는 판을 그대로 재현한다.

    설정을 켜 놓고 발사 함수를 불러도 카드가 안 나가고(`False`), 예외가 호출자로 새지도
    않는다 — `notify._post_card`가 삼켜서 "발사 실패"로 떨어지는 게 맞는 모습이다. 예외가
    새면 미태깅 인입이 통째로 500이 된다.
    """
    settings = get_settings()
    before = settings.mattermost_webhook_url
    monkeypatch.setattr(settings, "mattermost_webhook_url", _OUTSIDE_WEBHOOK, raising=False)
    try:
        assert settings.mattermost_active is True, "이 케이스는 켜진 상태를 재는 자리다"
        sent = await send_untagged_alert(
            gate_no=1, alert_id=1, source_id=1, low_confidence=False
        )
    finally:
        monkeypatch.setattr(settings, "mattermost_webhook_url", before, raising=False)

    assert sent is False, "시험이 팀 채널로 카드를 쐈다 — 바깥 POST 그물이 벗겨졌다"
