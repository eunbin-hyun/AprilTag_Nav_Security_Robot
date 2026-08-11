"""멱등 인입: 같은 event_id로 2회 POST해도 DB 행은 1개, 두 응답 다 2xx."""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.db import get_session
from app.models import GatePassEvent, ShuttleArrival, TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def _count(model) -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def test_tagging_idempotent(client, auth_headers):
    body = {
        "event_id": "raspberry01-1700000000000-0",
        "device_id": "raspberry01",
        "gate_no": 1,
        "tag_id": "20250001",
        "observed_at": _now(),
    }
    r1 = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    r2 = await client.post("/api/tagging-events", json=body, headers=auth_headers)

    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["stored"] is True
    assert r1.json()["duplicate"] is False
    assert r2.json()["stored"] is False
    assert r2.json()["duplicate"] is True
    assert await _count(TaggingEvent) == 1


async def test_gate_pass_idempotent(client, auth_headers):
    body = {
        "event_id": "raspberry01-1700000000001-0",
        "device_id": "raspberry01",
        "gate_no": 1,
        "direction": "A_TO_B",
        "status": "complete",
        "beam_a_ts": _now(),
        "beam_b_ts": _now(),
        "observed_at": _now(),
    }
    r1 = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    r2 = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert await _count(GatePassEvent) == 1


async def test_shuttle_idempotent(client, auth_headers):
    body = {
        "event_id": "gate2-1700000000002-0",
        "gate_no": 2,
        "shuttle_no": "SHUTTLE-A",
        "signal_ts": _now(),
    }
    r1 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    r2 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert r1.status_code == 200 and r2.status_code == 200
    assert await _count(ShuttleArrival) == 1
