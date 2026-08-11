"""미태깅 통계 API (조사 정본 2026-07-16 — 집계만).

시각을 고정해 놓고 미태깅 통과를 직접 심어서 본다. 구간 경계·KST 버킷·이동평균·게이트
필터·빈 구간·파라미터 검증이 대상이다.

기준 시각은 2026-07-28 05:00 UTC = 같은 날 14:00 KST다. days=7이면 구간은 KST로
7월 22일(수) 0시부터 7월 29일 0시 직전까지고, 마지막 날이 오늘(7월 28일 화)이다.

⚠ 시각을 UTC로 자르면 이 시험 여럿이 조용히 통과한다 — 그래서 일부러 UTC 날짜와 KST
날짜가 갈라지는 시각(전날 15시 UTC 이후)을 골라 심는다.
"""
import datetime as dt

import pytest

from app.credit.state_machine import Verdict
from app.db import get_session
from app.models import GatePassEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 2026-07-28 14:00 KST. 이 시각을 "지금"으로 고정한다.
NOW_UTC = dt.datetime(2026, 7, 28, 5, 0, tzinfo=dt.timezone.utc)
KST = dt.timezone(dt.timedelta(hours=9))

# days=7 구간의 첫날·끝날(KST 로컬 날짜)
FIRST_DAY = dt.date(2026, 7, 22)   # 수요일
LAST_DAY = dt.date(2026, 7, 28)    # 화요일

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """집계 기준 시각을 고정한다. 안 고정하면 구간이 실행하는 날마다 밀린다."""
    from app import stats

    monkeypatch.setattr(stats, "_now_utc", lambda: NOW_UTC)


async def _untagged(event_id: str, local: dt.datetime, *, gate_no: int = 2) -> None:
    """KST 로컬 시각으로 미태깅 통과 한 건을 심는다."""
    await _pass(event_id, local, gate_no=gate_no, verdict=Verdict.UNTAGGED)


async def _pass(
    event_id: str, local: dt.datetime, *, gate_no: int, verdict: str
) -> None:
    observed_at = local.replace(tzinfo=KST)
    async with get_session() as s:
        s.add(
            GatePassEvent(
                event_id=event_id,
                device_id="gate-sim",
                gate_no=gate_no,
                direction="A_TO_B",
                status="complete",
                beam_a_ts=observed_at,
                beam_b_ts=observed_at,
                observed_at=observed_at,
                verdict=verdict,
            )
        )
        await s.commit()


def _kst(date: dt.date, hour: int = 12, minute: int = 0, **kw) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hour, minute, **kw)


async def test_untagged_stats_empty_range(client):
    """미태깅이 하나도 없어도 200이고, 날짜·히트맵 칸은 0으로 꽉 차 있다."""
    r = await client.get("/api/stats/untagged")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["total"] == 0
    assert body["peak"] is None
    assert body["range"] == {
        "days": 7,
        "start_date": "2026-07-22",
        "end_date": "2026-07-28",
        "timezone": "Asia/Seoul",
    }
    assert [d["date"] for d in body["daily"]] == [
        "2026-07-22", "2026-07-23", "2026-07-24",
        "2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28",
    ]
    assert all(d["count"] == 0 for d in body["daily"])
    # 창(3)이 덜 찬 앞 두 날만 null이고, 그 뒤는 0.0이다 — null과 0은 다른 뜻이다.
    assert [d["moving_avg"] for d in body["daily"]] == [None, None, 0.0, 0.0, 0.0, 0.0, 0.0]

    heatmap = body["heatmap"]
    assert heatmap["weekday_labels"] == ["월", "화", "수", "목", "금", "토", "일"]
    assert len(heatmap["matrix"]) == 7
    assert all(len(row) == 24 for row in heatmap["matrix"])
    assert heatmap["max_count"] == 0


async def test_untagged_range_boundaries(client):
    """경계는 시작 이상 끝 미만이다 — 첫날 0시 정각은 들고, 그 1초 전은 안 든다."""
    # 구간 밖: 첫날 0시 1초 전 (= 7월 21일 23:59:59 KST)
    await _untagged("b-before", _kst(FIRST_DAY, 0) - dt.timedelta(seconds=1))
    # 구간 안 양 끝: 첫날 0시 정각, 마지막 날 23:59:59
    await _untagged("b-first", _kst(FIRST_DAY, 0))
    await _untagged("b-last", _kst(LAST_DAY, 23, 59, second=59))
    # 구간 밖: 다음 날 0시 정각 (끝 경계 자체)
    await _untagged("b-after", _kst(LAST_DAY + dt.timedelta(days=1), 0))

    body = (await client.get("/api/stats/untagged")).json()
    assert body["total"] == 2
    counts = {d["date"]: d["count"] for d in body["daily"]}
    assert counts["2026-07-22"] == 1
    assert counts["2026-07-28"] == 1
    assert sum(counts.values()) == 2


async def test_untagged_buckets_use_kst_not_utc(client):
    """버킷은 KST 로컬 날짜·시각이다. UTC로 자르면 날짜와 시각이 둘 다 밀린다."""
    # KST 7월 27일(월) 07:00 = UTC 7월 26일 22:00 — 날짜도 요일도 UTC와 갈린다.
    await _untagged("kst-morning", _kst(dt.date(2026, 7, 27), 7))
    # KST 7월 27일(월) 23:30 = UTC 7월 27일 14:30 — 시각만 갈린다.
    await _untagged("kst-night", _kst(dt.date(2026, 7, 27), 23, 30))

    body = (await client.get("/api/stats/untagged")).json()
    counts = {d["date"]: d["count"] for d in body["daily"]}
    assert counts["2026-07-27"] == 2
    assert counts["2026-07-26"] == 0

    matrix = body["heatmap"]["matrix"]
    assert matrix[MON][7] == 1     # UTC로 세면 일요일 22시로 간다
    assert matrix[MON][23] == 1    # UTC로 세면 월요일 14시로 간다
    assert matrix[SUN][22] == 0
    assert matrix[MON][14] == 0
    assert sum(sum(row) for row in matrix) == 2


async def test_untagged_moving_average(client):
    """이동평균은 뒤쪽 창 단순평균이고, 창이 덜 찬 앞머리는 null이다."""
    plan = {0: 3, 1: 0, 2: 6, 3: 3, 4: 0, 5: 0, 6: 3}  # 첫날부터 7일치 건수
    for offset, count in plan.items():
        for i in range(count):
            await _untagged(f"ma-{offset}-{i}", _kst(FIRST_DAY + dt.timedelta(days=offset), 9))

    body = (await client.get("/api/stats/untagged", params={"window": 3})).json()
    assert [d["count"] for d in body["daily"]] == [3, 0, 6, 3, 0, 0, 3]
    assert [d["moving_avg"] for d in body["daily"]] == [None, None, 3.0, 3.0, 3.0, 1.0, 1.0]
    assert body["total"] == 15
    assert body["window"] == 3

    # 창 크기를 바꾸면 앞머리 null 개수와 값이 같이 바뀐다.
    wide = (await client.get("/api/stats/untagged", params={"window": 7})).json()
    assert [d["moving_avg"] for d in wide["daily"]] == [None] * 6 + [round(15 / 7, 2)]


async def test_untagged_gate_filter_and_peak(client):
    """gate_no를 주면 그 게이트만 센다. peak도 필터를 따라 움직인다."""
    for i in range(3):
        await _untagged(f"g2-{i}", _kst(LAST_DAY, 8), gate_no=2)
    await _untagged("g5-0", _kst(LAST_DAY, 17), gate_no=5)

    whole = (await client.get("/api/stats/untagged")).json()
    assert whole["total"] == 4
    assert whole["gate_no"] is None
    assert whole["peak"] == {"weekday": TUE, "hour": 8, "count": 3}
    assert whole["heatmap"]["max_count"] == 3

    g5 = (await client.get("/api/stats/untagged", params={"gate_no": 5})).json()
    assert g5["total"] == 1
    assert g5["gate_no"] == 5
    assert g5["peak"] == {"weekday": TUE, "hour": 17, "count": 1}

    g9 = (await client.get("/api/stats/untagged", params={"gate_no": 9})).json()
    assert g9["total"] == 0
    assert g9["peak"] is None


async def test_untagged_counts_only_untagged_verdict(client):
    """정상·퇴장·빔 미완 통과는 안 센다. 세는 건 미태깅 판정이 붙은 통과뿐이다."""
    await _untagged("v-untagged", _kst(LAST_DAY, 10))
    for name, verdict in (
        ("v-normal", Verdict.NORMAL),
        ("v-exit", Verdict.EXIT),
        ("v-incomplete", Verdict.BEAM_INCOMPLETE),
    ):
        await _pass(name, _kst(LAST_DAY, 10), gate_no=2, verdict=verdict)

    body = (await client.get("/api/stats/untagged")).json()
    assert body["total"] == 1
    assert body["heatmap"]["matrix"][TUE][10] == 1


async def test_untagged_days_param_resizes_range(client):
    """days는 구간 길이를 바꾼다. 오늘은 늘 마지막 날이다."""
    await _untagged("d-old", _kst(FIRST_DAY, 9))
    await _untagged("d-today", _kst(LAST_DAY, 9))

    one = (await client.get("/api/stats/untagged", params={"days": 1, "window": 1})).json()
    assert one["range"]["start_date"] == "2026-07-28"
    assert one["range"]["end_date"] == "2026-07-28"
    assert len(one["daily"]) == 1
    assert one["total"] == 1

    wide = (await client.get("/api/stats/untagged", params={"days": 30})).json()
    assert wide["range"]["start_date"] == "2026-06-29"
    assert wide["range"]["end_date"] == "2026-07-28"
    assert len(wide["daily"]) == 30
    assert wide["total"] == 2


async def test_untagged_rejects_out_of_range_params(client):
    """범위 밖 파라미터는 조용히 자르지 않고 422다(/api/eta samples와 같은 정책)."""
    from app.stats import MAX_DAYS, MAX_WINDOW

    for params in (
        {"days": 0},
        {"days": -1},
        {"days": MAX_DAYS + 1},
        {"window": 0},
        {"window": MAX_WINDOW + 1},
        {"days": 3, "window": 4},   # 창이 구간보다 길면 이동평균이 통째로 null이 된다
        {"days": "일주일"},
        {"gate_no": "정문"},
    ):
        r = await client.get("/api/stats/untagged", params=params)
        assert r.status_code == 422, (params, r.text)

    for params in (
        {"days": 1, "window": 1},
        {"days": MAX_DAYS, "window": MAX_WINDOW},
        {"days": 7, "window": 7},
    ):
        r = await client.get("/api/stats/untagged", params=params)
        assert r.status_code == 200, (params, r.text)


async def test_heatmap_sum_matches_total(client):
    """히트맵 칸 합이 total과 같아야 한다.

    counts·total은 날짜 창으로 거르는데 matrix가 안 거르면 끝 경계(정각) 한 건이
    히트맵에만 얹혀 유령 칸이 생긴다 — 재검증이 잡은 경계 비대칭을 이 검사가 지킨다.
    끝 정각(오늘 자정) 이벤트를 일부러 심어 경계 밖 한 건이 어디에도 안 세어지는 걸 본다.
    """
    today_kst = NOW_UTC.astimezone(KST).date()
    await _untagged("hm-in", _kst(today_kst - dt.timedelta(days=1), 9))
    await _untagged("hm-edge", _kst(today_kst + dt.timedelta(days=1), 0, 0))
    body = (await client.get("/api/stats/untagged?days=7")).json()
    matrix_sum = sum(sum(row) for row in body["heatmap"]["matrix"])
    assert matrix_sum == body["total"]


async def test_gate_no_out_of_range_is_422(client):
    """gate_no도 days·window처럼 범위 밖은 조용히 비우지 말고 422.

    아래(0·음수)만 막혀 있고 **위가 열려 있었다.** 이 값은 `app/stats.py`의
    `GatePassEvent.gate_no == gate_no`로 그대로 들어가고 컬럼이 Integer라, int32를 넘는
    값은 검증을 지나 asyncpg DataError로 500이 됐다(실측). 범위는 기기 인입 3종과 같은
    값을 쓴다.

    ⚠ 상한이 빠지면 이 시험은 깨끗한 assert 실패가 아니라 DBAPIError로 죽는다 —
    conftest의 client가 `ASGITransport(app=app)`라 `raise_app_exceptions`가 기본 True다.
    """
    from app.schemas import GATE_NO_MAX, GATE_NO_MIN

    for bad in (0, -5, GATE_NO_MAX + 1, 99999999999):
        resp = await client.get(f"/api/stats/untagged?gate_no={bad}")
        assert resp.status_code == 422, (bad, resp.status_code, resp.text)

    for ok in (GATE_NO_MIN, GATE_NO_MAX):
        resp = await client.get(f"/api/stats/untagged?gate_no={ok}")
        assert resp.status_code == 200, (ok, resp.text)
