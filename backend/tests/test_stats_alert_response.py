"""경고 대응 통계 API `/api/stats/alert-response` (프론트 요구 N2).

경고를 직접 심어서 확인·종결 소요시간 집계를 본다. 검사의 핵심은 넷이다.

1. **빈 갈래에서 0으로 안 나눈다** — 확인이 한 건도 없어도 200이고, 평균·중앙값은 0이 아니라
   null이다. "0초 만에 확인"과 "확인한 게 없다"는 다른 사실이다.
2. **미확인은 total에 남고 표본에서만 빠진다** — 둘의 차가 곧 "아직 안 본 건"이다.
3. **구간 경계** — 시작 이상 끝 미만이고, 자르는 축은 `created_at`이다.
4. **파라미터 검증** — 범위 밖은 조용히 자르지 않고 422(`/api/stats/untagged`와 같은 정책).

기준 시각은 형제 시험(test_stats_untagged.py)과 같은 2026-07-28 05:00 UTC = 같은 날 14:00 KST다.
days=7이면 구간은 KST 7월 22일 0시부터 7월 29일 0시 직전까지다.
"""
import datetime as dt

import pytest

from app.db import get_session
from app.models import Alert

pytestmark = pytest.mark.asyncio(loop_scope="session")

NOW_UTC = dt.datetime(2026, 7, 28, 5, 0, tzinfo=dt.timezone.utc)
KST = dt.timezone(dt.timedelta(hours=9))

FIRST_DAY = dt.date(2026, 7, 22)
LAST_DAY = dt.date(2026, 7, 28)

EMPTY_SUMMARY = {"count": 0, "avg_seconds": None, "median_seconds": None}


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """집계 기준 시각을 고정한다. 안 고정하면 구간이 실행하는 날마다 밀린다."""
    from app import stats

    monkeypatch.setattr(stats, "_now_utc", lambda: NOW_UTC)


def _kst(date: dt.date, hour: int = 12, minute: int = 0, **kw) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=KST, **kw)


async def _alert(
    alert_type: str,
    created: dt.datetime,
    *,
    ack_after: int | None = None,
    resolve_after: int | None = None,
    severity: str = "high",
) -> None:
    """경고 한 건. 확인·종결은 "생긴 지 몇 초 뒤"로 적어 소요시간을 못박는다."""
    async with get_session() as s:
        s.add(
            Alert(
                type=alert_type,
                severity=severity,
                created_at=created,
                ack=ack_after is not None,
                acked_at=(
                    None if ack_after is None else created + dt.timedelta(seconds=ack_after)
                ),
                resolved_at=(
                    None
                    if resolve_after is None
                    else created + dt.timedelta(seconds=resolve_after)
                ),
            )
        )
        await s.commit()


async def _get(client, **params) -> dict:
    r = await client.get("/api/stats/alert-response", params=params)
    assert r.status_code == 200, r.text
    return r.json()


async def test_빈_구간도_200에_평균이_null(client):
    """경고가 없어도 200이다. 평균·중앙값은 0이 아니라 null이고 by_type은 빈 배열이다."""
    body = await _get(client)

    assert body["range"] == {
        "days": 7,
        "start_date": "2026-07-22",
        "end_date": "2026-07-28",
        "timezone": "Asia/Seoul",
    }
    assert body["overall"] == {
        "total": 0,
        "ack": EMPTY_SUMMARY,
        "resolve": EMPTY_SUMMARY,
    }
    assert body["by_type"] == []


async def test_평균과_중앙값(client):
    """평균은 산술평균이고 중앙값은 가운데 값이다. 둘이 갈리는 표본으로 확인한다."""
    # 확인까지 10·20·60초 → 평균 30, 중앙값 20. 평균만 보면 못 잡는 치우침이다.
    for i, seconds in enumerate((10, 20, 60)):
        await _alert("untagged", _kst(LAST_DAY, 9, i), ack_after=seconds,
                     resolve_after=seconds * 2)

    body = await _get(client)
    assert body["overall"]["total"] == 3
    assert body["overall"]["ack"] == {
        "count": 3, "avg_seconds": 30.0, "median_seconds": 20.0,
    }
    assert body["overall"]["resolve"] == {
        "count": 3, "avg_seconds": 60.0, "median_seconds": 40.0,
    }
    assert len(body["by_type"]) == 1
    assert body["by_type"][0]["type"] == "untagged"
    assert body["by_type"][0]["ack"]["median_seconds"] == 20.0


async def test_미확인은_total에_남고_표본에서만_빠진다(client):
    """확인 안 한 경고는 0초로 안 채운다. total과 count의 차가 "아직 안 본 건"이다."""
    await _alert("untagged", _kst(LAST_DAY, 9), ack_after=30, resolve_after=90)
    await _alert("untagged", _kst(LAST_DAY, 10))            # 확인도 종결도 없다
    await _alert("untagged", _kst(LAST_DAY, 11), ack_after=50)  # 확인만 했다

    body = await _get(client)
    assert body["overall"]["total"] == 3
    # 확인 표본은 둘(30·50)이고, 미확인 한 건이 평균을 0쪽으로 안 끌어내린다.
    assert body["overall"]["ack"] == {
        "count": 2, "avg_seconds": 40.0, "median_seconds": 40.0,
    }
    # 종결은 한 건뿐이라 평균과 중앙값이 같다.
    assert body["overall"]["resolve"] == {
        "count": 1, "avg_seconds": 90.0, "median_seconds": 90.0,
    }


async def test_아무도_확인_안_한_종류도_200(client):
    """확인이 0건인 종류에서 0으로 나누지 않는다. count=0에 평균·중앙값은 null이다."""
    await _alert("beam_incomplete", _kst(LAST_DAY, 9))
    await _alert("beam_incomplete", _kst(LAST_DAY, 10))

    body = await _get(client)
    assert body["overall"]["total"] == 2
    assert body["overall"]["ack"] == EMPTY_SUMMARY
    assert body["overall"]["resolve"] == EMPTY_SUMMARY
    assert body["by_type"] == [
        {"type": "beam_incomplete", "total": 2,
         "ack": EMPTY_SUMMARY, "resolve": EMPTY_SUMMARY}
    ]


async def test_종류별로_가르고_건수_많은_순(client):
    """by_type은 건수 많은 순, 같으면 종류 이름 오름차순이다(동점 순서를 못박는다)."""
    for i in range(3):
        await _alert("untagged", _kst(LAST_DAY, 9, i), ack_after=10)
    await _alert("robot_offline", _kst(LAST_DAY, 10), ack_after=100)
    await _alert("weather_blocked", _kst(LAST_DAY, 11), ack_after=200)

    body = await _get(client)
    assert [t["type"] for t in body["by_type"]] == [
        "untagged",        # 3건
        "robot_offline",   # 1건, 이름 오름차순으로 앞
        "weather_blocked",  # 1건
    ]
    assert [t["total"] for t in body["by_type"]] == [3, 1, 1]
    # 종류를 갈라도 전체 줄은 전체 표본으로 다시 센다(중앙값은 부분에서 못 만든다).
    assert body["overall"]["total"] == 5
    assert body["overall"]["ack"]["median_seconds"] == 10.0


async def test_구간_경계는_생성_시각_기준(client):
    """경계는 시작 이상 끝 미만이고, 자르는 축은 created_at이다."""
    await _alert("untagged", _kst(FIRST_DAY, 0) - dt.timedelta(seconds=1), ack_after=1)
    await _alert("untagged", _kst(FIRST_DAY, 0), ack_after=1)
    await _alert("untagged", _kst(LAST_DAY, 23, 59, second=59), ack_after=1)
    await _alert("untagged", _kst(LAST_DAY + dt.timedelta(days=1), 0), ack_after=1)

    body = await _get(client)
    assert body["overall"]["total"] == 2


async def test_어제_난_경고를_오늘_확인해도_어제로_센다(client):
    """확인 시각으로 자르면 안 된다 — 자르는 축은 생성 시각 하나뿐이다."""
    # 구간 밖(첫날 하루 전)에 나서 구간 안에서 확인된 경고. 세면 안 된다.
    await _alert(
        "untagged", _kst(FIRST_DAY - dt.timedelta(days=1), 23), ack_after=7200
    )
    body = await _get(client)
    assert body["overall"]["total"] == 0
    assert body["overall"]["ack"] == EMPTY_SUMMARY


async def test_days가_구간_길이를_바꾼다(client):
    """days는 구간을 늘리고 줄인다. 오늘은 늘 마지막 날이다."""
    await _alert("untagged", _kst(FIRST_DAY, 9), ack_after=10)
    await _alert("untagged", _kst(LAST_DAY, 9), ack_after=30)

    one = await _get(client, days=1)
    assert one["range"]["start_date"] == "2026-07-28"
    assert one["range"]["end_date"] == "2026-07-28"
    assert one["overall"]["total"] == 1
    assert one["overall"]["ack"]["avg_seconds"] == 30.0

    wide = await _get(client, days=30)
    assert wide["range"]["start_date"] == "2026-06-29"
    assert wide["overall"]["total"] == 2
    assert wide["overall"]["ack"]["avg_seconds"] == 20.0


async def test_범위_밖_파라미터는_422(client):
    """범위 밖 days는 조용히 자르지 않고 422다(/api/stats/untagged와 같은 정책)."""
    from app.stats import MAX_DAYS

    for params in ({"days": 0}, {"days": -1}, {"days": MAX_DAYS + 1}, {"days": "일주일"}):
        r = await client.get("/api/stats/alert-response", params=params)
        assert r.status_code == 422, (params, r.text)

    for params in ({}, {"days": 1}, {"days": MAX_DAYS}):
        r = await client.get("/api/stats/alert-response", params=params)
        assert r.status_code == 200, (params, r.text)


# ── 종류 필터 (2026-08-07 사용자 결정 · 화면 58차) ─────────────────────────


async def test_종류를_안_주면_안_가린다(client):
    """⛔ **이 창구를 두 화면이 같이 쓴다.** 기본값을 좁히면 안 물어본 쪽이 조용히 바뀐다.

    교대 결산은 무단 통과만 세야 하고, AI 탭 대응 통계는 "요원이 알림에 얼마나 빨리
    응답하나"라 종류를 안 가리는 것이 맞다.
    """
    day = _kst(LAST_DAY)
    await _alert("untagged", day, ack_after=10)
    await _alert("robot_arrival", day, ack_after=20)
    await _alert("shuttle_arrival_expired", day, ack_after=30)

    body = await _get(client, days=1)

    assert body["overall"]["total"] == 3, "기본값이 좁혀졌다 — AI 탭 숫자가 같이 바뀐다"
    assert body["types"] is None
    assert {r["type"] for r in body["by_type"]} == {
        "untagged", "robot_arrival", "shuttle_arrival_expired"
    }


async def test_종류를_주면_그것만_센다(client):
    """⭐ 교대 결산이 쓰는 갈래 — `?days=1&types=untagged`."""
    day = _kst(LAST_DAY)
    await _alert("untagged", day, ack_after=10)
    await _alert("untagged", day, ack_after=30)
    await _alert("robot_arrival", day, ack_after=20)
    await _alert("shuttle_arrival_expired", day, ack_after=40)

    body = await _get(client, days=1, types="untagged")

    assert body["overall"]["total"] == 2, "무단 통과만 세야 하는데 다른 종류가 섞였다"
    assert body["types"] == ["untagged"]
    assert [r["type"] for r in body["by_type"]] == ["untagged"]
    # 중앙값도 좁힌 표본에서 나와야 한다 — 전체에서 자르면 값이 달라진다.
    assert body["overall"]["ack"]["median_seconds"] == 20


async def test_쉼표로_여럿_줄_수_있다(client):
    """⚠ 종류가 늘어날 자리라 하나만 받게 두면 다음에 계약을 또 고쳐야 한다."""
    day = _kst(LAST_DAY)
    await _alert("untagged", day, ack_after=10)
    await _alert("robot_arrival", day, ack_after=20)
    await _alert("shuttle_arrival_expired", day, ack_after=30)

    body = await _get(client, days=1, types="untagged,robot_arrival")

    assert body["overall"]["total"] == 2
    assert body["types"] == ["untagged", "robot_arrival"]


async def test_모르는_종류는_422가_아니라_0건(client):
    """⚠ 닫힌 집합으로 막으면 종류를 새로 만든 날 이 창구가 먼저 깨진다."""
    await _alert("untagged", _kst(LAST_DAY), ack_after=10)

    body = await _get(client, days=1, types="없는종류")

    assert body["overall"]["total"] == 0
    assert body["by_type"] == []


async def test_남음도_같은_잣대로_센다(client):
    """⛔ **여기가 이 건의 본체다.** 교대 결산이 "남음"에 스냅샷 값을 쓰고 있었다.

    스냅샷 `active_alerts` 는 **종류도 기간도 안 가린다** — 종 아이콘이 "요원이 확인할
    전부"를 세는 자리라 그쪽은 그게 맞다. 그래서 그 함수를 고치는 대신 여기서 따로 센다.
    **발생·처리·남음이 같은 모집단이어야 한다.**
    """
    day = _kst(LAST_DAY)
    await _alert("untagged", day, ack_after=10)     # 확인함
    await _alert("untagged", day)                   # 안 함 → 남음 1
    await _alert("robot_arrival", day)              # 안 함 — 다른 종류라 안 세야 한다
    # 구간 밖 무단 통과. 기간도 가려야 한다.
    await _alert("untagged", _kst(FIRST_DAY - dt.timedelta(days=1)))

    body = await _get(client, days=1, types="untagged")

    assert body["overall"]["total"] == 2
    assert body["remaining"] == 1, (
        "남음이 종류나 기간을 안 가렸다 — 발생·처리와 다른 모집단을 세고 있다"
    )
