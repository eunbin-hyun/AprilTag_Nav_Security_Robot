"""예상 도착 시간 (S15P11C207-158).

구간은 "셔틀 도착 신호 → 로봇 도착 알림" 하나다. 기록을 직접 심어 EWMA 계산·이상치 제외·
게이트 필터·빈 응답 계약을 본다.
"""
import datetime as dt

import pytest

from app.db import get_session
from app.models import Alert, ShuttleArrival

pytestmark = pytest.mark.asyncio(loop_scope="session")

T0 = dt.datetime(2026, 7, 26, 9, 0, tzinfo=dt.timezone.utc)


async def _record(
    event_id: str, *, gate_no: int, signal_offset_sec: int, took_sec: float | None
) -> int:
    """셔틀 신호 한 건과 (있으면) 그 신호에 묶인 로봇 도착 알림을 심는다."""
    signal_ts = T0 + dt.timedelta(seconds=signal_offset_sec)
    async with get_session() as s:
        arrival = ShuttleArrival(
            event_id=event_id, gate_no=gate_no, shuttle_no="SHUTTLE-A", signal_ts=signal_ts
        )
        s.add(arrival)
        await s.flush()
        if took_sec is not None:
            s.add(
                Alert(
                    type="robot_arrival",
                    severity="info",
                    source_type="shuttle_arrival",
                    source_id=arrival.id,
                    created_at=signal_ts + dt.timedelta(seconds=took_sec),
                )
            )
        await s.commit()
        return arrival.id


async def test_eta_empty_response(client):
    """기록이 없으면 200에 빈 응답이다 — 404도 500도 아니다."""
    r = await client.get("/api/eta")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["method"] == "none"
    assert body["samples"] == 0
    assert body["eta_sec"] is None
    assert body["eta_at"] is None
    assert body["alpha"] is None
    assert body["measured_sec"] == []
    assert body["message"] == "아직 주행 기록이 없어 예상 도착 시간을 계산하지 못합니다."


async def test_eta_ewma_from_records(client):
    """오래된 것부터 60·90·30초면 alpha=0.3 EWMA는 57.3초다."""
    await _record("gate2-eta-0", gate_no=2, signal_offset_sec=0, took_sec=60)
    await _record("gate2-eta-1", gate_no=2, signal_offset_sec=300, took_sec=90)
    await _record("gate2-eta-2", gate_no=2, signal_offset_sec=600, took_sec=30)

    body = (await client.get("/api/eta")).json()
    assert body["method"] == "ewma"
    assert body["samples"] == 3
    assert body["alpha"] == 0.3
    assert body["measured_sec"] == [60.0, 90.0, 30.0]
    assert body["eta_sec"] == pytest.approx(57.3, abs=0.05)
    assert "57초" in body["message"]


async def test_eta_drops_outliers(client):
    """음수(시계 어긋남)와 지나치게 큰 값은 표본에서 뺀다."""
    await _record("gate2-out-0", gate_no=2, signal_offset_sec=0, took_sec=-30)
    await _record("gate2-out-1", gate_no=2, signal_offset_sec=300, took_sec=99999)
    await _record("gate2-out-2", gate_no=2, signal_offset_sec=600, took_sec=45)

    body = (await client.get("/api/eta")).json()
    assert body["samples"] == 1
    assert body["measured_sec"] == [45.0]
    assert body["eta_sec"] == pytest.approx(45.0, abs=0.05)


async def test_eta_gate_filter(client):
    await _record("gate2-f-0", gate_no=2, signal_offset_sec=0, took_sec=60)
    await _record("gate5-f-0", gate_no=5, signal_offset_sec=300, took_sec=120)

    g2 = (await client.get("/api/eta", params={"gate_no": 2})).json()
    g5 = (await client.get("/api/eta", params={"gate_no": 5})).json()
    g9 = (await client.get("/api/eta", params={"gate_no": 9})).json()
    assert g2["measured_sec"] == [60.0]
    assert g5["measured_sec"] == [120.0]
    assert g9["method"] == "none"


async def test_eta_at_uses_pending_arrival(client):
    """아직 도착이 안 찍힌 셔틀 신호가 있으면 그 신호 시각 + 예상 소요시간이 eta_at이다."""
    await _record("gate2-p-0", gate_no=2, signal_offset_sec=0, took_sec=50)
    await _record("gate2-p-1", gate_no=2, signal_offset_sec=900, took_sec=None)

    body = (await client.get("/api/eta")).json()
    assert body["eta_sec"] == pytest.approx(50.0, abs=0.05)
    expected = T0 + dt.timedelta(seconds=950)
    assert dt.datetime.fromisoformat(body["eta_at"]) == expected


async def test_eta_sample_limit(client):
    """samples 파라미터가 표본 수를 자른다(최근 것부터)."""
    await _record("gate2-l-0", gate_no=2, signal_offset_sec=0, took_sec=10)
    await _record("gate2-l-1", gate_no=2, signal_offset_sec=300, took_sec=20)
    await _record("gate2-l-2", gate_no=2, signal_offset_sec=600, took_sec=30)

    body = (await client.get("/api/eta", params={"samples": 2})).json()
    assert body["measured_sec"] == [20.0, 30.0]


async def test_eta_rejects_out_of_range_samples(client):
    """samples는 1 이상 상한 이하다 — 범위를 벗어나면 조용히 자르지 않고 422로 돌려준다."""
    from app.eta import SAMPLE_LIMIT_MAX

    for bad in (0, -5, SAMPLE_LIMIT_MAX + 1):
        r = await client.get("/api/eta", params={"samples": bad})
        assert r.status_code == 422, (bad, r.text)

    r = await client.get("/api/eta", params={"samples": SAMPLE_LIMIT_MAX})
    assert r.status_code == 200, r.text


async def test_eta_rejects_out_of_range_gate_no(client):
    """gate_no도 인입 3종과 같은 범위다 — 상한이 없으면 500이 났다.

    이 값은 `app/eta.py`의 `ShuttleArrival.gate_no == gate_no`로 그대로 들어가고 컬럼이
    Integer라, int32를 넘는 값은 검증을 지나 asyncpg DataError로 500이 됐다(실측). 인입만
    막고 조회를 열어 두면 같은 결함이 창구만 바꿔 남는다.

    ⚠ 상한이 빠지면 이 시험은 깨끗한 assert 실패가 아니라 DBAPIError로 죽는다 —
    conftest의 client가 `ASGITransport(app=app)`라 `raise_app_exceptions`가 기본 True다.
    어느 쪽이든 빨개지니 되돌리기 감지기 노릇은 한다.
    """
    from app.schemas import GATE_NO_MAX, GATE_NO_MIN

    for bad in (0, -1, GATE_NO_MAX + 1, 99999999999):
        r = await client.get("/api/eta", params={"gate_no": bad})
        assert r.status_code == 422, (bad, r.status_code, r.text)

    for ok in (GATE_NO_MIN, GATE_NO_MAX):
        r = await client.get("/api/eta", params={"gate_no": ok})
        assert r.status_code == 200, (ok, r.text)
