"""시간 버킷 집계 API (프론트 요구 N12).

두 창구를 본다 — `/api/stats/gate-pass-buckets`(15분 칸 × 판정 4갈래)와
`/api/stats/alert-buckets`(24시간 칸 × 심각도 3갈래). 검사의 핵심은 셋이다.

1. **칸 경계** — 시작 이상 끝 미만이다. 칸의 첫 초·마지막 초를 일부러 심어 한 건이 두 칸에
   겹치거나 어디에도 안 세어지는 갈래를 잡는다.
2. **격자 정렬** — 15분 칸은 KST 벽시계 눈금(:00·:15·:30·:45)에 붙고, 24시간 칸은 KST 로컬
   날짜(00:00 KST)에 붙는다. 24시간을 지금부터 거꾸로 세면 칸이 자정에 안 맞는다.
3. **빈 칸도 실린다** — 조용하던 시간대가 그래프에서 사라지면 안 된다.

기준 시각을 **눈금에서 7분 지난 자리**(2026-07-28 05:07 UTC = 14:07 KST)로 잡았다. 눈금
정각으로 잡으면 "진행 중인 칸이 마지막 자리에 들어오나"가 우연히 통과한다.
"""
import datetime as dt

import pytest

from app.credit.state_machine import Verdict
from app.db import get_session
from app.models import Alert, GatePassEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 2026-07-28 14:07 KST. 15분 눈금(14:00)에서 7분 지난 자리다.
NOW_UTC = dt.datetime(2026, 7, 28, 5, 7, tzinfo=dt.timezone.utc)
KST = dt.timezone(dt.timedelta(hours=9))

BUCKET = dt.timedelta(minutes=15)
# hours=24(기본)일 때 창은 [지금이 든 칸의 다음 눈금 − 24시간, 그 눈금)이다.
WINDOW_END = dt.datetime(2026, 7, 28, 5, 15, tzinfo=dt.timezone.utc)
WINDOW_START = WINDOW_END - dt.timedelta(hours=24)

FIRST_DAY = dt.date(2026, 7, 22)
LAST_DAY = dt.date(2026, 7, 28)

EMPTY_GATE_PASS = {
    "total": 0, "normal": 0, "untagged": 0, "exit": 0, "beam_incomplete": 0, "other": 0,
}
EMPTY_SEVERITY = {"total": 0, "info": 0, "warning": 0, "high": 0, "other": 0}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """집계 기준 시각을 고정한다. 안 고정하면 창이 실행하는 순간마다 밀린다."""
    from app import stats

    monkeypatch.setattr(stats, "_now_utc", lambda: NOW_UTC)


def _ts(value: str) -> dt.datetime:
    """응답의 ISO 문자열을 datetime으로. 끝의 Z도 그대로 받는다."""
    return dt.datetime.fromisoformat(value)


def _kst(date: dt.date, hour: int = 12, minute: int = 0, **kw) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=KST, **kw)


async def _pass(event_id: str, observed: dt.datetime, *, verdict=Verdict.NORMAL) -> None:
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


async def _alert(created: dt.datetime, *, severity: str | None = "high") -> None:
    async with get_session() as s:
        s.add(Alert(type="untagged", severity=severity, created_at=created))
        await s.commit()


async def _gate_buckets(client, **params) -> dict:
    r = await client.get("/api/stats/gate-pass-buckets", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def _alert_buckets(client, **params) -> dict:
    r = await client.get("/api/stats/alert-buckets", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ── 15분 버킷 (게이트 판정) ────────────────────────────────────────────────

async def test_빈_창도_200에_칸이_전부_0(client):
    """통과가 없어도 200이고, 96칸이 0으로 꽉 차 있다. 빈 배열이 아니다."""
    body = await _gate_buckets(client)

    assert body["range"]["bucket_seconds"] == 900
    assert body["range"]["bucket_count"] == 96
    assert body["range"]["timezone"] == "Asia/Seoul"
    assert _ts(body["range"]["start"]) == WINDOW_START
    assert _ts(body["range"]["end"]) == WINDOW_END
    assert body["total"] == 0
    assert len(body["buckets"]) == 96
    assert all(b["counts"] == EMPTY_GATE_PASS for b in body["buckets"])


async def test_칸이_KST_15분_눈금에_붙는다(client):
    """칸 시작은 KST 벽시계 :00·:15·:30·:45이다. 지금 시각(14:07)에서 안 밀린다."""
    body = await _gate_buckets(client)
    starts = [_ts(b["start"]).astimezone(KST) for b in body["buckets"]]

    assert all(s.minute in (0, 15, 30, 45) and s.second == 0 for s in starts)
    assert starts[0] == _kst(dt.date(2026, 7, 27), 14, 15)
    # 마지막 칸은 "지금"(14:07)이 든 진행 중인 칸이다 — 끝나지 않았다고 빼면 차트가 늘 우하향한다.
    assert starts[-1] == _kst(LAST_DAY, 14, 0)


async def test_칸_경계는_첫_초_이상_다음_눈금_미만(client):
    """칸의 첫 초는 그 칸, 마지막 초도 그 칸, 다음 눈금 정각은 다음 칸이다."""
    await _pass("edge-before", WINDOW_START - dt.timedelta(seconds=1))  # 창 밖
    await _pass("edge-first", WINDOW_START)                             # 0번 칸 첫 초
    await _pass("edge-last", WINDOW_START + BUCKET - dt.timedelta(seconds=1))  # 0번 칸 끝 초
    await _pass("edge-next", WINDOW_START + BUCKET)                     # 1번 칸 첫 초
    await _pass("edge-tail", WINDOW_END - dt.timedelta(seconds=1))      # 95번 칸 끝 초
    await _pass("edge-out", WINDOW_END)                                 # 창 끝 경계 = 밖

    body = await _gate_buckets(client)
    totals = [b["counts"]["total"] for b in body["buckets"]]
    assert totals[0] == 2
    assert totals[1] == 1
    assert totals[95] == 1
    assert sum(totals) == 4
    assert body["total"] == 4      # total은 칸 합과 같다(어디에도 안 세어지는 건이 없다)


async def test_판정_4갈래와_other(client):
    """판정 넷은 제 칸으로 가고, 사전 밖 판정과 미판정(null)은 other로 모인다."""
    base = WINDOW_START + BUCKET * 10
    await _pass("v-normal", base, verdict=Verdict.NORMAL)
    await _pass("v-untagged", base + dt.timedelta(seconds=1), verdict=Verdict.UNTAGGED)
    await _pass("v-exit", base + dt.timedelta(seconds=2), verdict=Verdict.EXIT)
    await _pass("v-beam", base + dt.timedelta(seconds=3), verdict=Verdict.BEAM_INCOMPLETE)
    await _pass("v-none", base + dt.timedelta(seconds=4), verdict=None)
    await _pass("v-new", base + dt.timedelta(seconds=5), verdict="새판정")

    body = await _gate_buckets(client)
    assert body["buckets"][10]["counts"] == {
        "total": 6, "normal": 1, "untagged": 1, "exit": 1, "beam_incomplete": 1, "other": 2,
    }
    assert body["total"] == 6


async def test_hours가_칸_수를_바꾼다(client):
    """hours는 창 길이를 바꾼다. 끝은 그대로고 시작만 당겨진다."""
    await _pass("h-old", WINDOW_END - dt.timedelta(hours=3))
    await _pass("h-recent", WINDOW_END - dt.timedelta(minutes=30))

    one = await _gate_buckets(client, hours=1)
    assert one["range"]["bucket_count"] == 4
    assert len(one["buckets"]) == 4
    assert _ts(one["range"]["end"]) == WINDOW_END
    assert _ts(one["range"]["start"]) == WINDOW_END - dt.timedelta(hours=1)
    assert one["total"] == 1                      # 3시간 전 건은 창 밖

    wide = await _gate_buckets(client, hours=6)
    assert wide["range"]["bucket_count"] == 24
    assert wide["total"] == 2


async def test_gate_pass_buckets_범위_밖_파라미터는_422(client):
    """범위 밖 hours는 조용히 자르지 않고 422다(/api/stats/untagged와 같은 정책)."""
    from app.stats import MAX_BUCKET_HOURS

    for params in (
        {"hours": 0}, {"hours": -1}, {"hours": MAX_BUCKET_HOURS + 1}, {"hours": "하루"},
    ):
        r = await client.get("/api/stats/gate-pass-buckets", params=params)
        assert r.status_code == 422, (params, r.text)

    for params in ({}, {"hours": 1}, {"hours": MAX_BUCKET_HOURS}):
        r = await client.get("/api/stats/gate-pass-buckets", params=params)
        assert r.status_code == 200, (params, r.text)


# ── 24시간 버킷 (경고 심각도) ──────────────────────────────────────────────

async def test_빈_구간도_200에_날짜가_다_실린다(client):
    """경고가 없어도 200이고, 0인 날도 빠짐없이 days개 들어온다."""
    body = await _alert_buckets(client)

    assert body["range"] == {
        "days": 7,
        "start_date": "2026-07-22",
        "end_date": "2026-07-28",
        "timezone": "Asia/Seoul",
    }
    assert body["total"] == 0
    assert [b["date"] for b in body["buckets"]] == [
        "2026-07-22", "2026-07-23", "2026-07-24",
        "2026-07-25", "2026-07-26", "2026-07-27", "2026-07-28",
    ]
    assert all(b["counts"] == EMPTY_SEVERITY for b in body["buckets"])


async def test_심각도_3갈래와_other(client):
    """info·warning·high는 제 칸으로, 사전 밖 값과 null은 other로 모인다."""
    await _alert(_kst(LAST_DAY, 9), severity="info")
    await _alert(_kst(LAST_DAY, 10), severity="warning")
    await _alert(_kst(LAST_DAY, 11), severity="high")
    await _alert(_kst(LAST_DAY, 12), severity="high")
    await _alert(_kst(LAST_DAY, 13), severity=None)
    await _alert(_kst(LAST_DAY, 14), severity="치명")

    body = await _alert_buckets(client)
    counts = {b["date"]: b["counts"] for b in body["buckets"]}
    assert counts["2026-07-28"] == {
        "total": 6, "info": 1, "warning": 1, "high": 2, "other": 2,
    }
    assert body["total"] == 6


async def test_24시간_칸은_KST_자정에_붙는다(client):
    """칸 경계는 KST 00:00이다. UTC로 자르면 경계가 오전 9시에 놓여 날이 밀린다."""
    # KST 7월 27일 07:00 = UTC 7월 26일 22:00 — UTC로 자르면 26일로 간다.
    await _alert(_kst(dt.date(2026, 7, 27), 7))
    # KST 7월 27일 23:30 = UTC 7월 27일 14:30 — 날짜는 안 갈리지만 같은 칸이어야 한다.
    await _alert(_kst(dt.date(2026, 7, 27), 23, 30))

    body = await _alert_buckets(client)
    counts = {b["date"]: b["counts"]["total"] for b in body["buckets"]}
    assert counts["2026-07-27"] == 2
    assert counts["2026-07-26"] == 0
    assert body["total"] == 2


async def test_24시간_칸_경계값(client):
    """하루의 첫 초는 그 날, 마지막 초도 그 날, 다음 날 0시 정각은 다음 칸이다."""
    await _alert(_kst(FIRST_DAY, 0) - dt.timedelta(seconds=1))          # 구간 밖
    await _alert(_kst(FIRST_DAY, 0))                                     # 첫날 첫 초
    await _alert(_kst(FIRST_DAY, 23, 59, second=59))                     # 첫날 끝 초
    await _alert(_kst(FIRST_DAY + dt.timedelta(days=1), 0))              # 다음 칸 첫 초
    await _alert(_kst(LAST_DAY, 23, 59, second=59))                      # 마지막 날 끝 초
    await _alert(_kst(LAST_DAY + dt.timedelta(days=1), 0))               # 구간 밖

    body = await _alert_buckets(client)
    counts = {b["date"]: b["counts"]["total"] for b in body["buckets"]}
    assert counts["2026-07-22"] == 2
    assert counts["2026-07-23"] == 1
    assert counts["2026-07-28"] == 1
    assert body["total"] == 4
    assert sum(counts.values()) == body["total"]


async def test_alert_buckets_범위_밖_파라미터는_422(client):
    """범위 밖 days는 조용히 자르지 않고 422다."""
    from app.stats import MAX_DAYS

    for params in ({"days": 0}, {"days": -1}, {"days": MAX_DAYS + 1}, {"days": "이틀"}):
        r = await client.get("/api/stats/alert-buckets", params=params)
        assert r.status_code == 422, (params, r.text)

    for params in ({}, {"days": 1}, {"days": MAX_DAYS}):
        r = await client.get("/api/stats/alert-buckets", params=params)
        assert r.status_code == 200, (params, r.text)
