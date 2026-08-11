"""오늘 집계 API `/api/stats/today` (개발자 텔레메트리 화면 카드).

화면이 목록을 받아 세던 값을 서버가 세게 옮긴 자리라, 검사의 핵심은 둘이다.

1. **날짜 경계** — 하루를 KST로 자르나. UTC로 자르면 경계가 오전 9시에 놓여 어제 저녁
   아홉 시간이 오늘에 섞인다. 그래서 UTC 날짜와 KST 날짜가 갈라지는 시각(전날 15시 UTC
   이후)에 일부러 심는다. 자정 직전·직후 한 초 차이로 날이 갈리는지도 같이 본다.
2. **상한 없음** — 목록 조회 상한(500건)을 넘겨도 총계가 안 잘리나. 이 API를 만든 이유
   자체라 실제로 501건을 심어 확인한다.

기준 시각은 test_stats_untagged.py와 같은 2026-07-28 05:00 UTC = 같은 날 14:00 KST다.
"""
import datetime as dt

import pytest

from app.credit.state_machine import Verdict
from app.db import get_session
from app.models import Alert, GatePassEvent, ShuttleArrival, TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW_UTC = dt.datetime(2026, 7, 28, 5, 0, tzinfo=dt.timezone.utc)
KST = dt.timezone(dt.timedelta(hours=9))

TODAY = dt.date(2026, 7, 28)        # 화요일
YESTERDAY = dt.date(2026, 7, 27)
TOMORROW = dt.date(2026, 7, 29)

EMPTY_GATE_PASS = {
    "total": 0, "normal": 0, "untagged": 0, "exit": 0, "beam_incomplete": 0, "other": 0,
}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """집계 기준 시각을 고정한다. 안 고정하면 "오늘"이 실행하는 날마다 밀린다."""
    from app import stats

    monkeypatch.setattr(stats, "_now_utc", lambda: NOW_UTC)


def _kst(date: dt.date, hour: int = 12, minute: int = 0, **kw) -> dt.datetime:
    """KST 로컬 시각(tz 붙은 값). 이대로 심으면 DB엔 UTC로 들어간다."""
    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=KST, **kw)


async def _add(*rows) -> None:
    async with get_session() as s:
        for row in rows:
            s.add(row)
        await s.commit()


def _tagging(event_id: str, local: dt.datetime, *, gate_no: int = 2) -> TaggingEvent:
    return TaggingEvent(
        event_id=event_id, device_id="gate-sim", gate_no=gate_no,
        tag_id="T-0001", observed_at=local,
    )


def _pass(
    event_id: str, local: dt.datetime, *, verdict: str | None = Verdict.NORMAL,
    gate_no: int = 2,
) -> GatePassEvent:
    return GatePassEvent(
        event_id=event_id, device_id="gate-sim", gate_no=gate_no,
        direction="A_TO_B", status="complete",
        beam_a_ts=local, beam_b_ts=local, observed_at=local, verdict=verdict,
    )


def _shuttle(event_id: str, local: dt.datetime, *, gate_no: int = 2) -> ShuttleArrival:
    return ShuttleArrival(
        event_id=event_id, gate_no=gate_no, shuttle_no="S-1", signal_ts=local,
    )


async def _today(client) -> dict:
    r = await client.get("/api/stats/today")
    assert r.status_code == 200, r.text
    return r.json()


async def test_빈_하루도_200에_전부_0(client):
    """오늘 아무 일도 없으면 200에 칸이 전부 0이다. 404도 빈 응답도 아니다."""
    body = await _today(client)
    assert body["date"] == "2026-07-28"
    assert body["timezone"] == "Asia/Seoul"
    assert body["tagging"] == 0
    assert body["shuttle_arrival"] == 0
    assert body["active_alerts"] == 0
    assert body["gate_pass"] == EMPTY_GATE_PASS


async def test_종류별로_따로_센다(client):
    """태깅·통과·셔틀은 계열이 갈린다. 한 계열 건수가 딴 계열에 새면 안 된다."""
    await _add(
        _tagging("t-1", _kst(TODAY, 9)),
        _tagging("t-2", _kst(TODAY, 10)),
        _pass("p-1", _kst(TODAY, 9)),
        _shuttle("s-1", _kst(TODAY, 9)),
        _shuttle("s-2", _kst(TODAY, 10)),
        _shuttle("s-3", _kst(TODAY, 11)),
    )

    body = await _today(client)
    assert body["tagging"] == 2
    assert body["gate_pass"]["total"] == 1
    assert body["shuttle_arrival"] == 3


async def test_통과는_판정별로_가르고_합이_total(client):
    """판정 네 가지 + 사전 밖(null·새 판정)은 other. 칸 합이 total과 같아야 한다."""
    await _add(
        _pass("v-n1", _kst(TODAY, 8), verdict=Verdict.NORMAL),
        _pass("v-n2", _kst(TODAY, 8), verdict=Verdict.NORMAL),
        _pass("v-u1", _kst(TODAY, 9), verdict=Verdict.UNTAGGED),
        _pass("v-e1", _kst(TODAY, 10), verdict=Verdict.EXIT),
        _pass("v-b1", _kst(TODAY, 11), verdict=Verdict.BEAM_INCOMPLETE),
        # 아직 판정이 안 붙은 행(2단계 전 적재)과 사전에 없는 새 판정은 other로 모인다.
        _pass("v-null", _kst(TODAY, 12), verdict=None),
        _pass("v-new", _kst(TODAY, 12), verdict="quarantined"),
    )

    gp = (await _today(client))["gate_pass"]
    assert gp == {
        "total": 7, "normal": 2, "untagged": 1, "exit": 1,
        "beam_incomplete": 1, "other": 2,
    }
    assert gp["total"] == (
        gp["normal"] + gp["untagged"] + gp["exit"] + gp["beam_incomplete"] + gp["other"]
    )


async def test_KST_자정_경계에서_날이_갈린다(client):
    """⚠ 이 시험이 이 API의 핵심이다 — 하루 경계는 KST 자정이다.

    자정 1초 전은 어제, 자정 정각은 오늘, 내일 자정 정각은 내일이다. 경계는 시작 이상
    끝 미만이라 오늘 0시 정각은 들고 내일 0시 정각은 안 든다.

    심는 시각을 일부러 UTC 날짜와 갈라 둔다 — 오늘 00:00 KST는 UTC로 어제 15:00이고,
    오늘 23:59:59 KST는 UTC로 오늘 14:59:59다. UTC로 자르면 세 계열이 다 어긋난다.
    """
    just_before = _kst(TODAY, 0) - dt.timedelta(seconds=1)   # 07-27 23:59:59 KST
    midnight = _kst(TODAY, 0)                                # 07-28 00:00:00 KST
    last_second = _kst(TODAY, 23, 59, second=59)             # 07-28 23:59:59 KST
    next_midnight = _kst(TOMORROW, 0)                        # 07-29 00:00:00 KST

    await _add(
        _tagging("edge-t-before", just_before),
        _tagging("edge-t-in-first", midnight),
        _tagging("edge-t-in-last", last_second),
        _tagging("edge-t-after", next_midnight),
        _pass("edge-p-before", just_before, verdict=Verdict.UNTAGGED),
        _pass("edge-p-in", midnight, verdict=Verdict.UNTAGGED),
        _pass("edge-p-after", next_midnight, verdict=Verdict.UNTAGGED),
        _shuttle("edge-s-before", just_before),
        _shuttle("edge-s-in", last_second),
        _shuttle("edge-s-after", next_midnight),
    )

    body = await _today(client)
    assert body["date"] == "2026-07-28"
    assert body["tagging"] == 2            # 자정 정각 + 23:59:59만
    assert body["gate_pass"]["total"] == 1
    assert body["gate_pass"]["untagged"] == 1
    assert body["shuttle_arrival"] == 1


async def test_UTC로_자르면_틀리는_시각대(client):
    """UTC 버킷이면 조용히 통과하지 못하게 못 박는 자리.

    KST 오늘 00:30은 UTC로 어제 15:30이고, KST 어제 23:30은 UTC로 어제 14:30이다.
    UTC 날짜로 자르면 앞엣것이 빠지고 뒤엣것이 들어와 수가 그대로 1이 된다 — 그러면
    "몇 건이냐"만 보는 시험은 통과한다. 그래서 두 건을 심고 **둘 다** 확인한다.
    """
    await _add(
        _tagging("tz-today-early", _kst(TODAY, 0, 30)),
        _tagging("tz-yesterday-late", _kst(YESTERDAY, 23, 30)),
    )

    assert (await _today(client))["tagging"] == 1

    # 어제 것만 지워도 수가 그대로 1이면 오늘 것을 세고 있다는 뜻이다(UTC면 0이 된다).
    async with get_session() as s:
        row = await s.get(TaggingEvent, 2)
        await s.delete(row)
        await s.commit()
    assert (await _today(client))["tagging"] == 1


async def test_조회_상한_500을_넘겨도_안_잘린다(client):
    """이 API를 만든 이유. /api/events는 500에서 잘리는데 집계는 안 잘려야 한다."""
    rows = [_pass(f"bulk-{i}", _kst(TODAY, 9), verdict=Verdict.UNTAGGED) for i in range(501)]
    await _add(*rows)

    body = await _today(client)
    assert body["gate_pass"]["total"] == 501
    assert body["gate_pass"]["untagged"] == 501

    # 목록 API는 같은 자료를 500건에서 자른다 — 화면이 이걸 세면 501이 안 나온다.
    listed = (await client.get("/api/events?limit=500")).json()
    assert len(listed) == 500


async def test_확인_안_한_경고는_오늘_범위가_아니다(client):
    """active_alerts는 "지금 몇 건 남았나"다. 어제 난 미확인 경고도 들어간다.

    스냅샷 counts.active_alerts와 같은 값이라야 화면 두 자리가 안 갈라진다.
    """
    await _add(
        Alert(type="untagged", severity="high", source_type="gate_pass", source_id=1, ack=False),
        Alert(type="untagged", severity="high", source_type="gate_pass", source_id=2, ack=False),
        Alert(type="untagged", severity="high", source_type="gate_pass", source_id=3, ack=True),
    )

    body = await _today(client)
    assert body["active_alerts"] == 2

    snapshot = (await client.get("/api/dashboard/snapshot")).json()
    assert snapshot["counts"]["active_alerts"] == body["active_alerts"]


async def test_수신_시각이_아니라_관측_시각으로_자른다(client):
    """백로그로 늦게 올라온 어제 이벤트는 어제 몫이다.

    received_at은 서버가 받은 순간(지금)이라, 그걸로 자르면 어제 관측이 오늘로 붙는다.
    """
    await _add(_tagging("late-1", _kst(YESTERDAY, 22)))

    async with get_session() as s:
        row = await s.get(TaggingEvent, 1)
        assert row.received_at is not None       # 서버 수신 시각은 오늘 찍혔다
        assert row.observed_at.astimezone(KST).date() == YESTERDAY

    assert (await _today(client))["tagging"] == 0
