"""목록 조회의 두 축 — `?active=`(ack)와 `?status=`(수명주기)가 서로 독립인가.

## 왜 이 시험이 따로 있나

관제 화면이 읽는 자리는 `GET /api/alerts?type=untagged` 하나다. 그 목록에서 닫힌 사건을
걷어내려면 화면이 `?status=`로 좁히거나, 다 받아서 화면에서 감추거나 둘 중 하나다. 어느
쪽을 고르든 **ack 축이 안 깨지는지**를 먼저 못박아야 한다.

`test_alert_lifecycle.py`가 이미 "상태로 추리면 그 단계만 온다"를 못박아 놨다. 여기는 그
다음 질문에 답한다 — **상태로 좁히면 무엇을 잃나.** 종결(RESOLVED)은 `ack`를 안 건드리므로
`ack=false`인 종결 건이 정상으로 존재하고, 그 줄은 종 배지가 세는 줄이다. 그래서 화면이
조회를 `?status=OPEN&status=ACKED&status=IDENTIFIED`로 좁히면 **종 배지에서 그 줄이 조용히
사라진다** — 서버 `count_active_alerts`는 계속 그 줄을 세는데 화면 숫자만 줄어든다.

이 시험이 그 갈림길을 숫자로 남긴다. 접점 4(화면 상태 축) 작업의 근거 자료다.

## 무엇을 못박나

- `?active=`가 질의 파라미터로 실제로 붙어 있고 `ack=false`만 걸러 준다(ack 축).
- 종결 건은 `ack`를 안 건드리므로 `?active=true`에 **그대로 남는다**(두 축이 독립이다).
- `?status=`로 종결을 걷어내도 ack 기준 집계(`snapshot.counts.active_alerts`,
  `/api/stats/today`의 `active_alerts`)는 **한 건도 안 변한다**.
- 두 축을 같이 주면 교집합이고, `type=untagged`까지 얹어도 같다(화면이 실제로 쓰는 모양).

⚠ 미태깅 경고는 같은 게이트에 10초 쿨다운이 걸려 있다. 한 케이스에서 여러 건이 필요하면
게이트 번호를 달리 준다(conftest가 케이스마다 쿨다운을 비우지만 케이스 안에서는 살아 있다).
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session
from app.models import Alert

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 30, 6, 0, 0, tzinfo=dt.timezone.utc)
HEADERS = {"X-API-Key": get_settings().api_key}

# 화면이 "닫히지 않은 사건"을 뽑을 때 쓸 질의. RESOLVED만 뺀 나머지 셋이다.
NOT_CLOSED_QUERY = "status=OPEN&status=ACKED&status=IDENTIFIED"


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


async def _make_untagged_alert(client, *, gate_no: int, tag: str) -> int:
    """태깅 없이 통과를 하나 넣어 미태깅 경고를 만들고 그 경고 id를 돌려준다."""
    res = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": f"p-axis-{tag}",
            "device_id": "raspberry01",
            "gate_no": gate_no,
            "direction": "A_TO_B",
            "status": "complete",
            "beam_a_ts": _iso(0),
            "beam_b_ts": _iso(0.2),
            "observed_at": _iso(0),
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    assert res.json()["verdict"] == "untagged", res.text
    async with get_session() as session:
        row = (
            await session.execute(select(Alert).order_by(Alert.id.desc()).limit(1))
        ).scalar_one()
        return row.id


def _ids(rows) -> set[int]:
    return {r["id"] for r in rows}


async def _four_stages(client) -> dict[str, int]:
    """네 단계를 하나씩 만든다. 게이트를 달리 줘서 쿨다운을 피한다."""
    ids = {
        "open": await _make_untagged_alert(client, gate_no=1, tag="open"),
        "acked": await _make_untagged_alert(client, gate_no=2, tag="acked"),
        "identified": await _make_untagged_alert(client, gate_no=3, tag="ident"),
        "resolved": await _make_untagged_alert(client, gate_no=4, tag="done"),
    }
    await client.post(f"/api/alerts/{ids['acked']}/ack", headers=HEADERS)
    await client.post(
        f"/api/alerts/{ids['identified']}/identify",
        json={"person": "가명-1"},
        headers=HEADERS,
    )
    await client.post(
        f"/api/alerts/{ids['resolved']}/status",
        json={"to": "RESOLVED", "resolution": "false_positive"},
        headers=HEADERS,
    )
    return ids


# ── ack 축이 질의 파라미터로 실제로 붙어 있나 ──────────────────────────────

async def test_active_파라미터가_ack_기준으로_걸러준다(client):
    """`?active=true`가 실제로 듣는가. 안 들으면 화면이 확인한 건까지 미확인으로 그린다."""
    ids = await _four_stages(client)
    rows = (await client.get("/api/alerts?active=true")).json()
    got = _ids(rows)

    # ack를 누른 건만 빠진다. 신원 입력과 종결은 ack를 안 건드린다.
    assert ids["acked"] not in got
    assert ids["open"] in got
    assert ids["identified"] in got
    assert all(r["ack"] is False for r in rows)


async def test_기본값은_전체다_active를_안_주면_확인한_건도_온다(client):
    """기본값이 조용히 바뀌면 화면이 "다 확인했다"고 잘못 읽는다."""
    ids = await _four_stages(client)
    got = _ids((await client.get("/api/alerts")).json())
    assert set(ids.values()) <= got


# ── 두 축이 독립인가 (여기가 핵심) ─────────────────────────────────────────

async def test_종결한_건은_ack를_안_건드려서_active_목록에_그대로_남는다(client):
    """종결이 ack를 찍지 않는다는 계약이 목록 API에서도 그대로 보이나.

    이게 화면이 조회를 상태로 좁히면 안 되는 이유다 — 이 줄은 "관제가 아직 안 본" 줄이라
    종 배지가 세는 줄인데, RESOLVED를 질의에서 빼면 화면에서 통째로 사라진다.
    """
    ids = await _four_stages(client)
    resolved = [
        r for r in (await client.get("/api/alerts")).json() if r["id"] == ids["resolved"]
    ][0]
    assert resolved["status"] == "RESOLVED"
    assert resolved["ack"] is False, "종결이 ack를 찍었다 — 두 축이 묶였다"
    assert resolved["acked_at"] is None

    # ack 축으로 물으면 종결 건이 그대로 온다.
    assert ids["resolved"] in _ids((await client.get("/api/alerts?active=true")).json())


async def test_상태로_종결을_걷어내면_그_줄만_사라진다(client):
    ids = await _four_stages(client)
    not_closed = _ids((await client.get(f"/api/alerts?{NOT_CLOSED_QUERY}")).json())
    assert ids["resolved"] not in not_closed
    assert {ids["open"], ids["acked"], ids["identified"]} <= not_closed


async def test_두_축이_고르는_집합이_실제로_다르다(client):
    """접점 4의 안전선. 한 축을 다른 축으로 갈아 쓰면 숫자가 **달라진다**를 실물로 잰다.

    두 질의를 앞뒤로 부르고 "안 변했다"만 보면 아무것도 안 재는 셈이다(사이에 상태를 바꾸는
    조작이 없으니 늘 통과한다). 그래서 두 축이 고른 집합을 직접 비교한다 — 크기는 같은데
    안에 든 줄이 다르다는 게 "묶으면 안 된다"의 증거다.
    """
    ids = await _four_stages(client)
    mine = set(ids.values())

    # ack 축 — 관제가 안 누른 것. 종결 건이 들어 있고 확인한 건이 빠진다.
    by_ack = _ids((await client.get("/api/alerts?active=true")).json()) & mine
    # status 축 — 안 닫힌 것. 확인한 건이 들어 있고 종결 건이 빠진다.
    by_status = _ids((await client.get(f"/api/alerts?{NOT_CLOSED_QUERY}")).json()) & mine

    assert by_ack == {ids["open"], ids["identified"], ids["resolved"]}
    assert by_status == {ids["open"], ids["acked"], ids["identified"]}
    assert len(by_ack) == len(by_status) == 3
    # 크기가 같아서 건수만 보면 같은 축처럼 보인다. 실제로는 서로 한 줄씩 다르다.
    assert by_ack != by_status
    assert by_ack - by_status == {ids["resolved"]}
    assert by_status - by_ack == {ids["acked"]}


async def test_ack_기준_집계는_종결_건을_그대로_센다(client):
    """종 배지·오늘 카드가 읽는 서버 집계가 status 필터와 무관한가.

    집계는 `count_active_alerts`(ack=false 전수)라 종결 건도 센다. 화면이 목록만 좁히면
    숫자가 맞고, 조회까지 좁히면 화면 숫자만 서버보다 작아진다.
    """
    ids = await _four_stages(client)

    snapshot = (await client.get("/api/dashboard/snapshot")).json()
    stats = (await client.get("/api/stats/today")).json()
    ack_false_total = len((await client.get("/api/alerts?active=true")).json())

    # 두 집계와 ack 축 목록 길이가 한 숫자로 맞는다(같은 잣대를 쓴다는 뜻).
    assert snapshot["counts"]["active_alerts"] == ack_false_total
    assert stats["active_alerts"] == ack_false_total

    # 그 숫자 안에 종결 건이 들어 있다 — 목록에서 감춰도 배지에는 남아야 하는 줄이다.
    resolved_row = [
        r for r in (await client.get("/api/alerts?active=true")).json()
        if r["id"] == ids["resolved"]
    ]
    assert len(resolved_row) == 1
    assert resolved_row[0]["status"] == "RESOLVED"

    # 화면이 조회를 status로 좁혔다면 잃었을 건수. 0이 아니라는 게 좁히면 안 되는 이유다.
    narrowed = _ids((await client.get(f"/api/alerts?{NOT_CLOSED_QUERY}")).json())
    lost = ack_false_total - len(
        [r for r in (await client.get("/api/alerts?active=true")).json()
         if r["id"] in narrowed]
    )
    assert lost == 1, f"종결·미확인 건이 {lost}건 — 조회를 좁히면 배지에서 그만큼 사라진다"


# ── 화면이 실제로 쓰는 모양 ────────────────────────────────────────────────

async def test_type과_두_축을_같이_주면_교집합이다(client):
    """관제 화면 질의는 `type=untagged`에 축 둘이 얹힌 모양이다."""
    ids = await _four_stages(client)

    # 신원까지 적혔지만 관제는 아직 안 누른 건 — 두 축이 갈리는 자리다.
    both = _ids(
        (await client.get("/api/alerts?type=untagged&active=true&status=IDENTIFIED")).json()
    )
    assert both == {ids["identified"]}

    # 종류가 다르면 상태가 맞아도 안 온다.
    assert (await client.get("/api/alerts?type=robot_arrival&status=OPEN")).json() == []
