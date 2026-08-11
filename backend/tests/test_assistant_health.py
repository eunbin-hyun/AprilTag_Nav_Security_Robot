"""AI 상태 스냅숏 — "이 답이 어디서 왔나"를 화면이 그릴 재료."""
from __future__ import annotations

import pytest

from ai.control_assistant import health
from ai.control_assistant.config import AssistantConfig
from app.config import get_settings

# ⛔ **이 줄이 없으면 전량이 무너진다**(2026-08-07 실측 · 128 failed · 905 errors).
#    auto 모드라 async 케이스가 저절로 돌긴 하는데, `loop_scope` 를 안 주면 **케이스마다
#    새 이벤트 루프**를 만든다. 세션 루프에 묶인 DB 커넥션 풀이 그 순간 깨져서 **뒤 파일들이
#    전부 `attached to a different loop` 로 죽는다.** 단독으로는 통과하니 원인을 찾기 어렵다.
pytestmark = pytest.mark.asyncio(loop_scope="session")

ASYNCIO = pytest.mark.asyncio(loop_scope="session")
LOCAL = AssistantConfig(api_key="k", base_url="http://172.18.0.1:18000/v1", model="qwen3.6-27b")
PROXY = AssistantConfig(api_key="k", base_url="https://gms.ssafy.io/gmsapi/api.openai.com/v1")


@ASYNCIO
async def test_snapshot_says_onprem_without_reaching_the_server(monkeypatch):
    """⭐ 서버에 못 닿아도 **설정은 우리가 아는 값**이라 그대로 쓸모가 있다."""
    import httpx

    class _Dead:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def get(self, url):
            raise httpx.ConnectError("안 닿는다")

    monkeypatch.setattr(health.httpx, "AsyncClient", _Dead)
    s = await health.snapshot(LOCAL)
    assert s["onprem"] is True
    assert s["reachable"] is False
    assert s["model"] == "qwen3.6-27b"
    assert s["probe_ms"] is not None


def test_endpoint_is_masked():
    """⛔ 역터널 주소를 화면에 그대로 내보내지 않는다. 요원이 알 필요가 없다."""
    assert "172.18" not in health._mask(LOCAL.base_url)
    assert health._mask(LOCAL.base_url) == "우리 GPU 서버 (사내망)"
    assert health._mask(PROXY.base_url) == "외부 프록시"


def test_onprem_uses_the_same_yardstick_as_the_client():
    """⚠ 판별 잣대가 client 와 갈리면 화면이 '온프레미스'라 적는데 실제로는 프록시일 수 있다."""
    from ai.control_assistant import client as llm_client

    for url in (LOCAL.base_url, PROXY.base_url, "http://127.0.0.1:8080/v1"):
        assert health._is_local(url) == llm_client._is_local_endpoint(url), url


@ASYNCIO
async def test_no_key_reports_unreachable():
    s = await health.snapshot(AssistantConfig(api_key=""))
    assert s["reachable"] is False


# ── 창구 (2026-08-07 · 백엔드가 붙인 자리) ─────────────────────────────────


async def test_창구가_스냅샷을_그대로_준다(client, monkeypatch):
    """⭐ 온프레미스라는 사실이 화면까지 가는 유일한 길이다."""
    from app.routers import assistant as router

    async def _fake(cfg):
        return {"onprem": True, "model": "qwen3.6-27b", "reachable": True}

    monkeypatch.setattr(router.ai_bridge, "health_snapshot", _fake)

    r = await client.get(
        "/api/assistant/health", headers={"X-API-Key": get_settings().api_key}
    )
    assert r.status_code == 200, r.text
    assert r.json()["onprem"] is True
    assert r.json()["model"] == "qwen3.6-27b"


async def test_모델서버가_죽어도_200이다(client, monkeypatch):
    """⚠ 진단 창구가 상류와 같이 죽으면 "왜 안 되나"를 볼 자리가 사라진다."""
    from app.routers import assistant as router

    async def _fake(cfg):
        return {"onprem": True, "reachable": False, "probe_ms": None}

    monkeypatch.setattr(router.ai_bridge, "health_snapshot", _fake)

    r = await client.get(
        "/api/assistant/health", headers={"X-API-Key": get_settings().api_key}
    )
    assert r.status_code == 200
    assert r.json()["reachable"] is False
