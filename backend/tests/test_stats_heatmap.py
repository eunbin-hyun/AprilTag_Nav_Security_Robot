"""요일 × 시간대 통과 히트맵 (`GET /api/stats/gate-pass-heatmap`).

프론트 2차가 부탁한 창구다. 관제 개요의 히트맵 카드가 7×24 격자를 그리는데
`/api/stats/gate-pass-buckets`는 15분 버킷이라 그 격자를 못 만든다.

⭐ **미태깅 히트맵과 세는 것이 다르다.** `/api/stats/untagged`의 히트맵은 미태깅만 세지만
이쪽은 **판정을 안 가린 통과 전체**다. "언제 사람이 몰리나"를 보는 카드라 정상 통과까지
들어가야 뜻이 선다. 이 파일의 본체가 그 갈림을 못 박는 케이스다.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.credit.state_machine import Verdict
from app.db import get_session
from app.models import GatePassEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

KST = dt.timezone(dt.timedelta(hours=9))


async def _pass(event_id: str, local: dt.datetime, *, verdict: str = Verdict.NORMAL) -> None:
    """통과 한 건. `local`은 KST 벽시계 시각이다(격자가 로컬 기준이라 그렇게 준다)."""
    observed = local.replace(tzinfo=KST).astimezone(dt.timezone.utc)
    async with get_session() as s:
        s.add(
            GatePassEvent(
                event_id=event_id, device_id="gate-sim", gate_no=2,
                direction="A_TO_B", status="complete",
                beam_a_ts=observed, beam_b_ts=observed, observed_at=observed,
                verdict=verdict,
            )
        )
        await s.commit()


def _today_at(hour: int) -> dt.datetime:
    """오늘 KST 그 시각. 구간이 '오늘 포함 최근 며칠'이라 오늘에 심어야 창 안이다."""
    now = dt.datetime.now(KST)
    return dt.datetime(now.year, now.month, now.day, hour, 30)


async def test_격자가_늘_7행_24열로_꽉_찬다(client):
    """건수 0인 칸을 빼면 화면이 격자를 못 그린다. 자료가 하나도 없어도 모양은 같다."""
    res = await client.get("/api/stats/gate-pass-heatmap?days=7")
    assert res.status_code == 200, res.text
    body = res.json()

    assert len(body["weekday_labels"]) == 7
    assert len(body["matrix"]) == 7
    assert all(len(row) == 24 for row in body["matrix"]), "24칸이 안 되는 줄이 있다"
    assert body["range"]["days"] == 7
    assert body["range"]["timezone"]


async def test_판정을_안_가리고_통과_전체를_센다(client):
    """⭐ 이 파일의 본체 — 미태깅 히트맵과 세는 것이 다르다.

    같은 시각에 정상 하나와 미태깅 하나를 심으면 이 창구는 **둘 다** 세고,
    `/api/stats/untagged`는 미태깅 하나만 센다.
    """
    at = _today_at(9)
    await _pass("hm-normal", at, verdict=Verdict.NORMAL)
    await _pass("hm-untagged", at, verdict=Verdict.UNTAGGED)

    weekday = at.weekday()  # 0=월 … 6=일. 격자 행 순서와 같다.

    res = await client.get("/api/stats/gate-pass-heatmap?days=1")
    assert res.status_code == 200, res.text
    cell = res.json()["matrix"][weekday][9]
    assert cell == 2, f"통과 전체를 세야 하는데 {cell}건이다(미태깅만 센 것 같다)"

    # ⚠ `window`를 같이 준다 — 기본 창(7일)이 days보다 크면 그 창구는 422다.
    untagged = await client.get("/api/stats/untagged?days=1&window=1")
    assert untagged.status_code == 200, untagged.text
    assert untagged.json()["heatmap"]["matrix"][weekday][9] == 1, (
        "미태깅 히트맵까지 통과 전체를 세게 바뀌었다 — 두 카드가 같은 값을 그린다"
    )


async def test_최고_칸과_색_기준을_같이_준다(client):
    """`max_count`는 색 농도 기준이고 `peak`는 가장 몰린 칸이다."""
    at = _today_at(14)
    for i in range(3):
        await _pass(f"hm-peak-{i}", at)
    await _pass("hm-other", _today_at(20))

    res = await client.get("/api/stats/gate-pass-heatmap?days=1")
    body = res.json()

    assert body["max_count"] == 3
    assert body["peak"] == {"weekday": at.weekday(), "hour": 14, "count": 3}


async def test_자료가_없으면_peak_가_null_이고_max_가_0_이다(client):
    """화면이 "아직 자료가 없다"를 그릴 수 있어야 한다. 0으로 꽉 찬 격자는 그대로 온다."""
    res = await client.get("/api/stats/gate-pass-heatmap?days=1")
    body = res.json()

    assert body["peak"] is None
    assert body["max_count"] == 0
    assert sum(sum(row) for row in body["matrix"]) == 0


async def test_범위_밖_days_는_422_다(client):
    """`/api/stats/untagged`와 같은 정책이다 — 조용히 자르지 않는다."""
    assert (await client.get("/api/stats/gate-pass-heatmap?days=0")).status_code == 422
    assert (await client.get("/api/stats/gate-pass-heatmap?days=9999")).status_code == 422


async def test_게이트로_거를_수_있다(client):
    """`gate_no`를 주면 그 게이트만 센다. 안 주면 전체 합이다."""
    at = _today_at(11)
    await _pass("hm-gate2", at)  # 헬퍼가 gate_no=2로 심는다
    weekday = at.weekday()

    both = await client.get("/api/stats/gate-pass-heatmap?days=1")
    assert both.json()["matrix"][weekday][11] == 1

    other = await client.get("/api/stats/gate-pass-heatmap?days=1&gate_no=1")
    assert other.json()["matrix"][weekday][11] == 0, "다른 게이트 것까지 셌다"
    assert other.json()["gate_no"] == 1
