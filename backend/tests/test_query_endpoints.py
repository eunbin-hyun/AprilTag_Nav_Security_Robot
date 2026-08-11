"""조회 API 스텁(로봇·이벤트·알림·ack) 시험. 지금까지 정찰에서 빠져 있던 구멍."""
import datetime as dt

import pytest
from sqlalchemy import insert

from app.credit.state_machine import evaluate_pass
from app.db import get_session
from app.models import ShuttleArrival

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 25, 3, 0, 0, tzinfo=dt.timezone.utc)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def test_list_robots_empty(client):
    resp = await client.get("/api/robots")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_list_alerts_returns_created_alert(client, auth_headers):
    r = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": "Q1",
            "device_id": "raspberry01",
            "gate_no": 5,
            "direction": "A_TO_B",
            "status": "complete",
            "beam_a_ts": _now(),
            "observed_at": _now(),
        },
        headers=auth_headers,
    )
    assert r.json()["verdict"] == "untagged"

    resp = await client.get("/api/alerts")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["type"] == "untagged"
    assert body[0]["ack"] is False


async def test_ack_alert_success(client, auth_headers):
    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="Q2", device_id="raspberry01", gate_no=6,
            direction="A_TO_B", status="complete",
            beam_a_ts=BASE, beam_b_ts=None, observed_at=BASE,
        )
    assert ev.alert_id is not None

    resp = await client.post(f"/api/alerts/{ev.alert_id}/ack", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == ev.alert_id
    assert body["ack"] is True

    listed = await client.get("/api/alerts")
    assert listed.json()[0]["ack"] is True


async def test_ack_alert_not_found_404(client, auth_headers):
    resp = await client.post("/api/alerts/999999/ack", headers=auth_headers)
    assert resp.status_code == 404


async def test_list_events_merges_three_kinds(client, auth_headers):
    await client.post(
        "/api/tagging-events",
        json={"event_id": "E1", "gate_no": 7, "tag_id": "20259999", "observed_at": _now()},
        headers=auth_headers,
    )
    await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": "E2", "gate_no": 7, "direction": "A_TO_B", "status": "complete",
            "beam_a_ts": _now(), "observed_at": _now(),
        },
        headers=auth_headers,
    )
    await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "E3", "gate_no": 7, "shuttle_no": "SH-1", "signal_ts": _now()},
        headers=auth_headers,
    )

    resp = await client.get("/api/events")
    assert resp.status_code == 200
    kinds = {row["kind"] for row in resp.json()}
    assert kinds == {"tagging", "gate_pass", "shuttle_arrival"}


async def test_list_events_limit_out_of_range_is_422(client):
    """창 밖 limit은 조용히 깎지 않고 422로 막는다(사용자 확정 정책).

    창 안쪽은 돌아온 행 수로 잰다 — 200만 보면 아무것도 안 본 것이다.
    테이블이 비어 있으면 상한이 안 걸려도 200이 나오니까,
    상한 500이 진짜 500행에서 멈추는지 보려고 501행을 한 방 벌크 인서트로 넣는다.
    """
    async with get_session() as s:
        await s.execute(
            insert(ShuttleArrival),
            [
                {
                    "event_id": f"CLAMP{i}",
                    "gate_no": 8,
                    "shuttle_no": "SH-CLAMP",
                    "signal_ts": BASE + dt.timedelta(seconds=i),
                }
                for i in range(501)
            ],
        )
        await s.commit()

    over = await client.get("/api/events", params={"limit": 10000})
    assert over.status_code == 422, "상한 500을 넘는 limit이 422로 안 막혔다"

    under = await client.get("/api/events", params={"limit": 0})
    assert under.status_code == 422, "하한 1 미만인 limit이 422로 안 막혔다"

    # 경계값 500은 창 안이라 그대로 통과하고, 501행 중 500행에서 멈춘다.
    edge = await client.get("/api/events", params={"limit": 500})
    assert edge.status_code == 200
    assert len(edge.json()) == 500, "limit 상한 500이 500행에서 안 멈췄다"

    # 창 안쪽 값은 그대로 통과한다(상한 검사가 정상 요청까지 깎지 않는다).
    inside = await client.get("/api/events", params={"limit": 7})
    assert inside.status_code == 200
    assert len(inside.json()) == 7
