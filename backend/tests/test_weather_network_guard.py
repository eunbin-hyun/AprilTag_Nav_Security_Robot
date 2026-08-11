"""시험이 바깥 날씨 서버를 안 타는지 못박는다 (2026-07-31 검증 P3).

## 왜 이 파일이 따로 있나

날씨 스텁이 `tests/test_shuttle_dispatch.py` 안에만 있던 시절에는, **다른 파일**이 연 5초
대기 창이 지연으로 터지면 그 자리에서 wttr.in을 진짜로 불렀다(`test_dashboard_ws.py`·
`test_shuttle_call.py`가 그 갈래다). 바깥이 느린 날엔 시험이 느려지고 죽은 날엔 통째로
빨개진다.

그래서 차단을 `conftest.py`의 autouse 픽스처로 올렸는데, 그게 **전역인지**는 그 파일
안에서 증명할 수 없다 — 같은 파일에 스텁이 있으면 스텁이 통과시킨 건지 차단이 통과시킨
건지 못 가른다. 이 파일은 자기 스텁을 하나도 안 들고 있어서, 여기서 통과하면 차단이
파일 밖에서도 산다는 뜻이다.
"""
from __future__ import annotations

import pytest

from app import weather as weather_mod
from app.weather import WeatherStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_스텁이_없는_파일에서도_바깥으로_안_나간다():
    """이 파일에는 날씨 픽스처가 없다. 그런데도 조회가 즉시 성공해야 한다."""
    weather_mod.reset_weather_cache()
    reading = await weather_mod.fetch_current_weather()

    assert reading.ok is True, "차단이 안 걸려 바깥 호출이 실패했거나 폴백을 탔다"
    assert reading.stale is False
    assert reading.status is WeatherStatus.CLEAR
    assert reading.description == "Sunny"


async def test_차단이_httpx_경계에_걸려_있다():
    """막는 자리가 `fetch_current_weather`가 아니라 그 아래 HTTP 경계여야 한다.

    `app/dispatch.py`가 조회 함수를 이름으로 가져가서(`from app.weather import ...`),
    모듈 한 곳만 갈아 끼우면 갈래가 샌다. 경계 한 자리를 막아야 어느 경로로 들어와도
    못 나간다.
    """
    import httpx

    assert weather_mod.httpx.AsyncClient is not httpx.AsyncClient or issubclass(
        weather_mod.httpx.AsyncClient, httpx.AsyncClient
    )
    assert weather_mod.httpx.AsyncClient.__name__ == "_WeatherGuardedClient"


async def test_셔틀_자동_발사도_바깥을_안_탄다(client):
    """대기 창을 실제로 열어 보는 자리. 이 파일에는 날씨 스텁이 없다."""
    from app.config import get_settings
    from app.dispatch import destination_without_fetch, ensure_today_default
    from app.db import get_session

    assert get_settings().weather_api_url.startswith("http"), "차단은 URL을 보고 건다"
    async with get_session() as session:
        stored = await ensure_today_default(session)
        # 맑음이 돌아오니 실외로 정해진다(강수가 아니다).
        assert (stored.destination, stored.weather_status) == ("OUTDOOR_TAGGING", "CLEAR")
        assert stored.weather_ok is True
        # 발사 경로는 이 값을 네트워크 없이 그대로 읽는다.
        picked = await destination_without_fetch(session)
    assert picked.value == "OUTDOOR_TAGGING"
