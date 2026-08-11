"""/api/events 정렬 결정성 (S15P11C207-191).

시각 단일 키만 쓰면 한 트랜잭션에 들어온 행들의 반환 순서가 DB 스캔 순서에 맡겨져
잠재 flaky가 된다. 알림 쪽과 같은 꼴(시각 desc + id desc)로 맞춘다.
"""
import datetime as dt

import pytest

from app.db import get_session
from app.models import TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = dt.datetime(2026, 7, 28, 9, 0, tzinfo=dt.timezone.utc)


async def test_events_order_has_id_tiebreaker(client):
    """received_at이 같은 행이 여럿이면 id 내림차순으로 순서가 정해진다."""
    async with get_session() as s:
        for suffix in ("a", "b", "c"):
            s.add(
                TaggingEvent(
                    event_id=f"tie-{suffix}",
                    tag_id="TAG-TIE",
                    gate_no=1,
                    observed_at=T0,
                    received_at=T0,  # 셋 다 같은 시각 — tiebreaker 없이는 순서가 안 정해진다
                )
            )
        await s.commit()

    r = await client.get("/api/events")
    assert r.status_code == 200, r.text
    order = [row["event_id"] for row in r.json()]
    assert order == ["tie-c", "tie-b", "tie-a"], f"id 내림차순 tiebreaker가 없다 — 실제 {order}"

    again = await client.get("/api/events")
    assert [row["event_id"] for row in again.json()] == order, "같은 자료에 순서가 흔들린다"


async def test_events_time_key_still_wins(client):
    """tiebreaker를 붙여도 1차 키는 received_at 내림차순 그대로다."""
    async with get_session() as s:
        s.add(
            TaggingEvent(
                event_id="older-but-smaller-id",
                tag_id="TAG-A",
                gate_no=1,
                observed_at=T0,
                received_at=T0,
            )
        )
        await s.flush()
        s.add(
            TaggingEvent(
                event_id="newer-bigger-id",
                tag_id="TAG-B",
                gate_no=1,
                observed_at=T0,
                received_at=T0 + dt.timedelta(seconds=60),
            )
        )
        await s.commit()

    order = [row["event_id"] for row in (await client.get("/api/events")).json()]
    assert order == ["newer-bigger-id", "older-but-smaller-id"]


async def test_events_limit_takes_newest_side(client):
    """limit이 걸려도 잘리는 쪽은 오래된 끝이다(정렬 키를 바꿔도 이건 유지)."""
    async with get_session() as s:
        for n in range(5):
            s.add(
                TaggingEvent(
                    event_id=f"lim-{n}",
                    tag_id="TAG-LIM",
                    gate_no=1,
                    observed_at=T0,
                    received_at=T0 + dt.timedelta(seconds=n),
                )
            )
        await s.commit()

    order = [row["event_id"] for row in (await client.get("/api/events?limit=2")).json()]
    assert order == ["lim-4", "lim-3"]
