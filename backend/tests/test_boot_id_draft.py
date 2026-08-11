"""[초안·팀 확정 대기] 안건② event_id 안 boot_id 검증.

핵심은 4번이다. 재기동으로 seq가 1로 돌아가고 unix_ms까지 겹치면 예전 형식은
두 번째 이벤트를 조용히 버린다. boot_id를 넣으면 두 건 다 살아남는다.
"""
import contextlib
import datetime as dt

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.eventid import format_event_id, parse_event_id
from app.db import get_session
from app.models import TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 27, 3, 0, 0, tzinfo=dt.timezone.utc)
UNIX_MS = 1785294000000


@contextlib.contextmanager
def require_boot_id(enabled: bool):
    """event_id_require_boot_id를 잠깐 바꾼다.

    get_settings()가 lru_cache라 인스턴스가 하나뿐인데, 속성을 직접 대입하면 다른
    프로세스(xdist 워커)의 env는 그대로라 재구성 시점마다 값이 갈릴 수 있다. 그래서
    env 변수를 monkeypatch로 바꾸고 cache_clear로 재구성을 강제한다 — 되돌릴 때도
    env를 되돌리고 cache_clear로 다시 강제한다.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("EVENT_ID_REQUIRE_BOOT_ID", "true" if enabled else "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


def _tagging(event_id: str, tag_id: str = "20250001", device_id: str = "raspberry01") -> dict:
    return {
        "event_id": event_id,
        "device_id": device_id,
        "gate_no": 1,
        "tag_id": tag_id,
        "observed_at": BASE.isoformat(),
    }


# 1. 조용한 중복 — 예전 형식은 재기동 뒤 같은 seq를 버리고, boot_id를 넣으면 살린다.
async def test_boot_id_prevents_silent_duplicate(client, auth_headers):
    legacy = format_event_id("raspberry01", None, UNIX_MS, 1)
    r1 = await client.post("/api/tagging-events", json=_tagging(legacy, "20250001"), headers=auth_headers)
    r2 = await client.post("/api/tagging-events", json=_tagging(legacy, "20250002"), headers=auth_headers)
    # 서로 다른 카드인데 두 번째가 "중복"으로 조용히 버려진다 — 이게 지금 위험이다.
    assert r1.json()["stored"] is True
    assert r2.json()["stored"] is False and r2.json()["duplicate"] is True

    boot_a = format_event_id("raspberry02", "aaaaaaaa", UNIX_MS, 1)
    boot_b = format_event_id("raspberry02", "bbbbbbbb", UNIX_MS, 1)
    r3 = await client.post(
        "/api/tagging-events", json=_tagging(boot_a, "20250003", "raspberry02"), headers=auth_headers
    )
    r4 = await client.post(
        "/api/tagging-events", json=_tagging(boot_b, "20250004", "raspberry02"), headers=auth_headers
    )
    assert r3.json()["stored"] is True and r4.json()["stored"] is True

    async with get_session() as s:
        boots = (
            await s.execute(
                select(TaggingEvent.boot_id).where(TaggingEvent.device_id == "raspberry02")
            )
        ).scalars().all()
    assert sorted(boots) == ["aaaaaaaa", "bbbbbbbb"]  # 본문에 안 실어도 event_id에서 뽑힌다


# 2. 멱등은 그대로 — 새 형식도 같은 event_id 재전송이면 한 번만 들어간다.
async def test_boot_format_still_idempotent(client, auth_headers):
    eid = format_event_id("raspberry01", "1a2f3c4d", UNIX_MS, 5)
    r1 = await client.post("/api/tagging-events", json=_tagging(eid), headers=auth_headers)
    r2 = await client.post("/api/tagging-events", json=_tagging(eid), headers=auth_headers)
    assert r1.json()["stored"] is True
    assert r2.json()["stored"] is False and r2.json()["duplicate"] is True


# 3. 기본값(플래그 꺼짐) — 예전 형식이 그대로 200이다(하위 호환).
async def test_legacy_accepted_by_default(client, auth_headers):
    assert get_settings().event_id_require_boot_id is False
    r = await client.post(
        "/api/tagging-events", json=_tagging("raspberry01-1785294000000-1"), headers=auth_headers
    )
    assert r.status_code == 200, r.text


# 4. 플래그 켜짐 — 예전 형식은 422, 새 형식은 200.
async def test_flag_on_rejects_legacy_event_id(client, auth_headers):
    with require_boot_id(True):
        bad = await client.post(
            "/api/tagging-events", json=_tagging(f"raspberry01-{UNIX_MS}-1"), headers=auth_headers
        )
        good = await client.post(
            "/api/tagging-events",
            json=_tagging(format_event_id("raspberry01", "1a2f3c4d", UNIX_MS, 1)),
            headers=auth_headers,
        )
    assert bad.status_code == 422
    assert good.status_code == 200, good.text


# 5. 본문 boot_id가 event_id보다 우선한다(퍼블리셔가 명시로 보내는 길도 열어 둔다).
async def test_explicit_boot_id_field_wins(client, auth_headers):
    body = _tagging(
        format_event_id("raspberry03", "1a2f3c4d", UNIX_MS, 1), device_id="raspberry03"
    )
    body["boot_id"] = "ffffffff"
    await client.post("/api/tagging-events", json=body, headers=auth_headers)
    async with get_session() as s:
        stored = (
            await s.execute(
                select(TaggingEvent.boot_id).where(TaggingEvent.device_id == "raspberry03")
            )
        ).scalar_one()
    assert stored == "ffffffff"
