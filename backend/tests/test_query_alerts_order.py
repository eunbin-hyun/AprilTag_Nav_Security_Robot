"""/api/alerts 정렬 결정성 (S15P11C207-193).

-191과 같은 계열: created_at 단일 키만 쓰면 한 트랜잭션에 들어온 알림들의
반환 순서가 DB 스캔 순서에 맡겨져 잠재 flaky가 된다. 이벤트 쪽과 같은 꼴
(시각 desc + id desc)로 맞춘다.
"""
import datetime as dt

import pytest

from app.db import get_session
from app.models import Alert

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = dt.datetime(2026, 7, 28, 9, 0, tzinfo=dt.timezone.utc)


async def test_alerts_order_has_id_tiebreaker(client):
    """created_at이 같은 알림이 여럿이면 id 내림차순으로 순서가 정해진다."""
    async with get_session() as s:
        for n in range(3):
            s.add(
                Alert(
                    type="untagged",
                    severity="warning",
                    source_type="gate_pass",
                    source_id=n + 1,
                    created_at=T0,  # 셋 다 같은 시각 — tiebreaker 없이는 순서가 안 정해진다
                )
            )
        await s.commit()

    r = await client.get("/api/alerts")
    assert r.status_code == 200, r.text
    ids = [row["id"] for row in r.json()]
    assert ids == sorted(ids, reverse=True), f"id 내림차순 tiebreaker가 없다 — 실제 {ids}"

    again = await client.get("/api/alerts")
    assert [row["id"] for row in again.json()] == ids, "같은 자료에 순서가 흔들린다"


async def test_alerts_time_key_still_wins(client):
    """tiebreaker를 붙여도 1차 키는 created_at 내림차순 그대로다."""
    async with get_session() as s:
        s.add(
            Alert(
                type="untagged",
                severity="warning",
                source_type="gate_pass",
                source_id=101,
                created_at=T0,
            )
        )
        await s.flush()
        s.add(
            Alert(
                type="untagged",
                severity="warning",
                source_type="gate_pass",
                source_id=102,
                created_at=T0 + dt.timedelta(seconds=60),
            )
        )
        await s.commit()

    rows = (await client.get("/api/alerts")).json()
    assert [row["source_id"] for row in rows] == [102, 101]
