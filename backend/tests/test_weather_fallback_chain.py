"""1차(wttr.in)가 죽으면 대체 도메인(wttr.is)이 받는 사슬을 세운다.

근거 — `docs/조사_백엔드5과제_웹검증_2026-07-31.md` §T2. wttr.in 단독 의존은 취약하고
(2026-02 실제 다운 이슈), 제작자 README가 스크립트 용도엔 대체 도메인 wttr.is를 직접
권한다. wttr.is가 같은 j1 본문을 주는 건 2026-07-31 실측으로 확인했다.

autouse 가드(`stub_outbound_weather`)는 wttr.* 전부를 맑음으로 답하므로, 여기서는
케이스 안에서 클라이언트를 한 겹 더 갈아 끼워 "어느 도메인이 죽었나"를 정한다.
"""
from __future__ import annotations

import httpx
import pytest

from app import weather as weather_mod
from app.config import get_settings
from app.weather import WeatherStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 1차와 구별되는 본문(비)을 준다 — 폴백을 탔다는 사실이 값으로 드러나게.
_RAINY_J1 = {
    "current_condition": [
        {
            "weatherCode": "296",
            "weatherDesc": [{"value": "Light rain"}],
            "precipMM": "1.2",
            "temp_C": "24",
        }
    ]
}


def _chain_client(dead: frozenset[str]):
    """지정한 도메인만 죽는 가짜 클라이언트. 나머지는 비 오는 본문을 준다."""

    class _ChainClient(httpx.AsyncClient):
        async def get(self, url, *args, **kwargs):
            target = str(url)
            if any(marker in target for marker in dead):
                raise httpx.ConnectError("죽은 도메인", request=httpx.Request("GET", target))
            return httpx.Response(200, json=_RAINY_J1, request=httpx.Request("GET", target))

    return _ChainClient


async def test_1차가_죽으면_대체_도메인이_받는다(monkeypatch):
    monkeypatch.setattr(
        weather_mod.httpx, "AsyncClient", _chain_client(frozenset({"wttr.in"}))
    )
    weather_mod.reset_weather_cache()

    reading = await weather_mod.fetch_current_weather()

    # 폴백이 성공하면 신선한 성공이다 — stale이 아니다.
    assert reading.ok is True
    assert reading.stale is False
    assert reading.status is WeatherStatus.LIGHT_RAIN


async def test_사슬이_전부_죽으면_마지막_성공값_규칙으로_넘어간다(monkeypatch):
    monkeypatch.setattr(
        weather_mod.httpx,
        "AsyncClient",
        _chain_client(frozenset({"wttr.in", "wttr.is"})),
    )
    weather_mod.reset_weather_cache()

    reading = await weather_mod.fetch_current_weather()

    # 성공 이력이 없으니 UNKNOWN. (마지막 성공값 폴백 자체는 기존 시험이 본다.)
    assert reading.ok is False
    assert reading.status is WeatherStatus.UNKNOWN


async def test_폴백_주소가_설정에_실려_있다():
    """사슬의 둘째 고리가 설정에서 빠지면 이 파일 전체가 헛돈다 — 못박는다."""
    assert "wttr.is" in get_settings().weather_api_fallback_url
