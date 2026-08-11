"""인입 스키마 검증 실패는 422, 인증 실패는 401."""
import datetime as dt

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def test_tagging_missing_field_422(client, auth_headers):
    # tag_id 누락 → pydantic 검증 실패
    body = {"event_id": "x-1-0", "gate_no": 1, "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_tagging_bad_datetime_422(client, auth_headers):
    body = {
        "event_id": "x-1-1",
        "tag_id": "20250001",
        "observed_at": "not-a-datetime",
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_gate_pass_bad_enum_422(client, auth_headers):
    body = {
        "event_id": "x-2-0",
        "direction": "SIDEWAYS",  # enum 밖
        "status": "complete",
        "observed_at": _now(),
    }
    resp = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_missing_api_key_401(client):
    body = {"event_id": "x-3-0", "tag_id": "20250001", "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body)
    assert resp.status_code == 401, resp.text


async def test_wrong_api_key_401(client):
    body = {"event_id": "x-3-1", "tag_id": "20250001", "observed_at": _now()}
    resp = await client.post(
        "/api/tagging-events", json=body, headers={"X-API-Key": "nope"}
    )
    assert resp.status_code == 401, resp.text
