"""알림 → 발화 이벤트 역추적 (S15P11C207-192).

AlertOut.source_id는 이벤트 행 PK인데 EventOut이 업무 키(event_id)만 내보내면
REST만으로는 "경고 클릭 → 통과 상세" 동선이 성립하지 않는다. 행 id를 덧붙여
(source_type, source_id) 짝을 (kind, id)로 맞춘다.
"""
import datetime as dt

import pytest

from app.db import get_session
from app.models import Alert, GatePassEvent, TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = dt.datetime(2026, 7, 28, 9, 0, tzinfo=dt.timezone.utc)


async def test_event_row_id_matches_alert_source_id(client):
    """AlertOut.source_id로 /api/events의 해당 행을 되짚을 수 있다."""
    async with get_session() as s:
        gp = GatePassEvent(
            event_id="gp-trace-1",
            gate_no=1,
            direction="A_TO_B",
            status="complete",
            observed_at=T0,
            received_at=T0,
        )
        s.add(gp)
        await s.flush()
        s.add(
            Alert(
                type="untagged_pass",
                severity="warning",
                source_type="gate_pass",
                source_id=gp.id,
            )
        )
        await s.commit()

    alerts = (await client.get("/api/alerts")).json()
    events = (await client.get("/api/events")).json()
    assert len(alerts) == 1 and len(events) == 1
    assert "id" in events[0], "EventOut에 행 id가 없어 알림 역추적이 끊긴다"

    alert = alerts[0]
    matched = [
        e for e in events if e["kind"] == alert["source_type"] and e["id"] == alert["source_id"]
    ]
    assert len(matched) == 1, f"source_id={alert['source_id']}로 되짚을 이벤트 행이 없다"
    assert matched[0]["event_id"] == "gp-trace-1"


async def test_event_row_id_is_per_kind_row_pk(client):
    """행 id는 계열마다 그 테이블의 PK다 — 같은 id가 계열이 다르면 다른 행이다."""
    async with get_session() as s:
        s.add(
            TaggingEvent(
                event_id="tag-1",
                tag_id="TAG-1",
                gate_no=1,
                observed_at=T0,
                received_at=T0,
            )
        )
        s.add(
            GatePassEvent(
                event_id="gp-1",
                gate_no=1,
                direction="A_TO_B",
                status="complete",
                observed_at=T0,
                received_at=T0,
            )
        )
        await s.commit()

    rows = {r["kind"]: r for r in (await client.get("/api/events")).json()}
    assert rows["tagging"]["id"] == 1
    assert rows["gate_pass"]["id"] == 1  # 시퀀스가 따로라 kind 없이는 id만으로 못 가른다


async def test_event_out_keeps_existing_fields(client):
    """행 id는 덧붙이기다 — 기존 필드 이름·형은 그대로 둔다."""
    async with get_session() as s:
        s.add(
            TaggingEvent(
                event_id="keep-shape-1",
                tag_id="TAG-KEEP",
                gate_no=3,
                observed_at=T0,
                received_at=T0,
            )
        )
        await s.commit()

    row = (await client.get("/api/events")).json()[0]
    assert row["kind"] == "tagging"
    assert row["event_id"] == "keep-shape-1"
    assert row["gate_no"] == 3
    assert row["observed_at"] is not None
    assert row["received_at"] is not None
    assert isinstance(row["id"], int)
