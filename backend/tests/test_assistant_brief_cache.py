"""일일 요약을 만들어 두고 다시 쓰나 (0020 · 2026-08-07 사용자 지시).

⭐ **왜 캐시하나** — 요원이 개요 탭을 열 때마다 LLM 을 부르고 있었다. 실측 7.4초라 그동안
그 칸이 비어 있고, 화면을 다시 열면 같은 값을 또 만든다. 같은 날 같은 자료면 답도 같다.

⛔ **이 파일의 핵심 못은 "폴백을 안 앉힌다"이다.** LLM 이 죽어서 나온 규칙 문장을 캐시에
저장하면 그 문장이 수명 동안 계속 나가고, 그 사이 LLM 이 살아나도 요원은 규칙 문장을 본다.
**고장이 캐시에 굳는 자리라 조용하다** — 응답은 200이고 문장도 그럴듯하다.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app import ai_bridge
from app.config import get_settings
from app.db import get_session
from app.models import AssistantDailyBrief
from app.routers import assistant as assistant_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

# ⚠ 인입 키 헤더가 없으면 401이라 LLM 까지 아예 안 간다 — 캐시를 잴 수가 없다.
HEADERS = {"X-API-Key": get_settings().api_key}


def _stub_llm(monkeypatch, text: str = "오늘은 조용했습니다."):
    """LLM 이 성공한 것처럼 만든다. 부른 횟수를 센다."""
    calls = {"n": 0}

    async def _fake(cfg, facts):
        calls["n"] += 1
        return ai_bridge.AssistantText(
            text=f"{text} ({calls['n']}번째)", generated_by="llm", fallback_reason=None
        )

    monkeypatch.setattr(assistant_router.ai_bridge, "daily_summary", _fake)
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key="dummy-not-a-real-key"),
    )
    return calls


async def _rows() -> int:
    async with get_session() as s:
        return (
            await s.execute(select(func.count()).select_from(AssistantDailyBrief))
        ).scalar_one()


async def test_두_번째_호출은_LLM을_안_부른다(client, monkeypatch):
    """⭐ 본체 — 같은 날을 다시 물으면 만들어 둔 것을 준다."""
    calls = _stub_llm(monkeypatch)

    first = await client.get("/api/assistant/daily-summary", headers=HEADERS)
    assert first.status_code == 200
    assert calls["n"] == 1
    assert await _rows() == 1

    second = await client.get("/api/assistant/daily-summary", headers=HEADERS)
    assert second.status_code == 200
    assert calls["n"] == 1, "두 번째에도 LLM 을 불렀다 — 캐시가 안 먹는다"
    assert second.json()["summary"] == first.json()["summary"]
    assert second.json()["meta"]["generated_by"] == "llm"


async def test_폴백_문장은_저장하지_않는다(client, monkeypatch):
    """⛔ 고장을 캐시에 굳히면 LLM 이 살아나도 요원은 규칙 문장을 계속 본다."""

    async def _fallback(cfg, facts):
        return ai_bridge.AssistantText(
            text="규칙 문장입니다.", generated_by="fallback", fallback_reason="no_api_key"
        )

    monkeypatch.setattr(assistant_router.ai_bridge, "daily_summary", _fallback)
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key=""),
    )

    resp = await client.get("/api/assistant/daily-summary", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["meta"]["generated_by"] == "fallback"
    assert await _rows() == 0, "폴백이 캐시에 앉았다 — 고장이 수명 동안 굳는다"


async def test_수명이_지나면_다시_만든다(client, monkeypatch):
    """⚠ 오늘치는 자료가 계속 는다. 아침 요약이 저녁에도 맞을 리 없다."""
    calls = _stub_llm(monkeypatch)

    await client.get("/api/assistant/daily-summary", headers=HEADERS)
    assert calls["n"] == 1

    ttl = get_settings().assistant_brief_ttl_sec
    async with get_session() as s:
        row = (await s.execute(select(AssistantDailyBrief))).scalar_one()
        row.updated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ttl + 60)
        await s.commit()

    await client.get("/api/assistant/daily-summary", headers=HEADERS)
    assert calls["n"] == 2, "수명이 지났는데 낡은 요약을 그대로 줬다"
    assert await _rows() == 1, "다시 만들 때 행이 늘면 어느 것이 최신인지 갈린다"


async def test_지난_날짜는_수명과_무관하다(client, monkeypatch):
    """⭐ 그날 자료가 더 안 바뀌니 영구히 재사용한다."""
    calls = _stub_llm(monkeypatch)
    yday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date().isoformat()

    await client.get("/api/assistant/daily-summary", params={"date": yday}, headers=HEADERS)
    assert calls["n"] == 1

    ttl = get_settings().assistant_brief_ttl_sec
    async with get_session() as s:
        row = (await s.execute(select(AssistantDailyBrief))).scalar_one()
        row.updated_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ttl * 10)
        await s.commit()

    await client.get("/api/assistant/daily-summary", params={"date": yday}, headers=HEADERS)
    assert calls["n"] == 1, "지난 날짜인데 수명 때문에 다시 만들었다"


async def test_날짜마다_따로_만든다(client, monkeypatch):
    """⚠ 하루치가 옆날로 새면 개요 탭이 남의 날 이야기를 한다."""
    calls = _stub_llm(monkeypatch)
    yday = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).date().isoformat()

    today_resp = await client.get("/api/assistant/daily-summary", headers=HEADERS)
    yday_resp = await client.get("/api/assistant/daily-summary", params={"date": yday}, headers=HEADERS)

    assert calls["n"] == 2, "다른 날인데 앞날 요약을 그대로 줬다"
    assert today_resp.json()["summary"] != yday_resp.json()["summary"]
    assert await _rows() == 2
