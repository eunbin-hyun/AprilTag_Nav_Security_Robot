"""`GET /api/events`의 계열 축 — `?kind=` (프론트 요구 N8).

## 왜 이 시험이 따로 있나

`fetch_events`는 태깅·통과·셔틀 세 계열을 UNION해서 최근순 `limit`으로 자른다. 계열을 고를
축이 없던 동안 통과 이벤트 표는 **통과 100건을 보려고 합본 500건을 받아 화면에서 걸러야**
했다. 상한이 계열 사이에서 나뉘니까 바쁜 계열이 조용한 계열의 자리를 먹었다.

여기서 못박는 건 "필터가 붙었다"가 아니라 **"그 계열에서 최근 limit건"**이다 —
`test_필터에_limit을_얹으면_그_계열에서_최근_limit건이다`가 그 줄이다.

## 무엇을 못박나

- 계열마다 딱 그 계열만 온다(셋 다).
- 필터 + `limit`이면 다른 계열이 자리를 안 차지한다.
- 셔틀 라벨은 `shuttle`이 아니라 `SHUTTLE_SOURCE_TYPE`(`shuttle_arrival`)이다.
- 모르는 `kind`는 `422`(빈 글자 포함). `/api/alerts`의 `source_type`이 빈 `200`으로 여는 것과
  일부러 다르다 — 저쪽은 자유 문자열이고 `robot` 출처가 실재한다.
- 걸러도 한 줄의 칸 모양은 합본과 같다(가지 하나만 subquery로 읽는 자리의 라벨 회귀).
- 필터를 안 주면 예전 응답 그대로다(기존 계약 불변).

⚠ 셔틀 라벨은 손으로 적지 않고 `SHUTTLE_SOURCE_TYPE`을 쓴다. 예전에 이벤트 쪽만 `shuttle`로
적혀 경고 `source_type`과 갈라졌던 자리라, 시험이 손글자를 들고 있으면 같은 사고가 시험을
통과한 채로 재발한다.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.db import get_session
from app.models import GatePassEvent, ShuttleArrival, TaggingEvent
from app.robot_channel import SHUTTLE_SOURCE_TYPE

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 8, 1, 3, 0, 0, tzinfo=dt.timezone.utc)

TAGGING_KIND = "tagging"
GATE_PASS_KIND = "gate_pass"
ALL_KINDS = (TAGGING_KIND, GATE_PASS_KIND, SHUTTLE_SOURCE_TYPE)


def _at(offset_sec: float) -> dt.datetime:
    return BASE + dt.timedelta(seconds=offset_sec)


async def _seed_one_each(prefix: str, *, offset: float = 0.0) -> None:
    """계열마다 한 줄씩 심는다. 받은 시각은 셋 다 다르게 둬 정렬이 결정적이게 한다."""
    async with get_session() as s:
        s.add(
            TaggingEvent(
                event_id=f"{prefix}-tag",
                tag_id="TAG-KIND",
                gate_no=1,
                observed_at=_at(offset),
                received_at=_at(offset),
            )
        )
        s.add(
            GatePassEvent(
                event_id=f"{prefix}-gate",
                gate_no=1,
                direction="A_TO_B",
                verdict="normal",
                tag_id="TAG-KIND",
                observed_at=_at(offset + 1),
                received_at=_at(offset + 1),
            )
        )
        s.add(
            ShuttleArrival(
                event_id=f"{prefix}-shuttle",
                gate_no=1,
                shuttle_no="S-1",
                signal_ts=_at(offset + 2),
                received_at=_at(offset + 2),
            )
        )
        await s.commit()


def _kinds(rows) -> set[str]:
    return {r["kind"] for r in rows}


def _event_ids(rows) -> list[str]:
    return [r["event_id"] for r in rows]


# ── 계열별로 딱 그 계열만 ──────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ALL_KINDS)
async def test_계열을_주면_그_계열만_온다(client, kind):
    await _seed_one_each("only")

    res = await client.get(f"/api/events?kind={kind}")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert rows, f"{kind} 계열이 한 줄도 안 왔다 — 심은 행을 못 찾는다"
    assert _kinds(rows) == {kind}, f"다른 계열이 섞였다 — 실제 {_kinds(rows)}"


async def test_셔틀_라벨은_shuttle이_아니라_shuttle_arrival이다(client):
    """이벤트 쪽만 `shuttle`로 적혀 경고 `source_type`과 갈라졌던 자리의 회귀 못."""
    await _seed_one_each("label")

    assert SHUTTLE_SOURCE_TYPE == "shuttle_arrival"
    ok = await client.get(f"/api/events?kind={SHUTTLE_SOURCE_TYPE}")
    assert ok.status_code == 200, ok.text
    assert _kinds(ok.json()) == {SHUTTLE_SOURCE_TYPE}

    # 예전 라벨은 아는 글자가 아니다 — 조용히 빈 목록이 되면 화면이 "셔틀은 없다"로 그린다.
    old = await client.get("/api/events?kind=shuttle")
    assert old.status_code == 422, old.text


async def test_걸러도_한_줄의_칸_모양은_합본과_같다(client):
    """가지 하나만 subquery로 읽는 자리라 칸 라벨이 빠지면 여기서 터진다."""
    await _seed_one_each("shape")

    merged = (await client.get("/api/events")).json()
    for kind in ALL_KINDS:
        filtered = (await client.get(f"/api/events?kind={kind}")).json()
        same_row = [r for r in merged if r["kind"] == kind][0]
        assert filtered[0] == same_row, f"{kind} 한 줄이 합본과 다르다"

    gate_row = [r for r in merged if r["kind"] == GATE_PASS_KIND][0]
    assert gate_row["direction"] == "A_TO_B" and gate_row["verdict"] == "normal"
    shuttle_row = [r for r in merged if r["kind"] == SHUTTLE_SOURCE_TYPE][0]
    assert shuttle_row["shuttle_no"] == "S-1"
    assert shuttle_row["observed_at"] is not None, "셔틀 signal_ts가 observed_at 자리에 안 실렸다"


# ── 필터 + limit — 다른 계열이 자리를 안 차지한다 ──────────────────────────

async def test_필터에_limit을_얹으면_그_계열에서_최근_limit건이다(client):
    """N8이 고치는 결함 그대로 — 상한을 계열 사이에서 나눠 먹던 자리다.

    통과를 3건 심고 그 **뒤에** 태깅·셔틀을 더 최근 시각으로 심는다. 필터가 없으면
    `limit=3`은 최근 세 줄(태깅·셔틀 쪽)을 가져가 통과가 한 줄도 안 남는다.
    """
    async with get_session() as s:
        for n in range(3):
            s.add(
                GatePassEvent(
                    event_id=f"win-gate-{n}",
                    gate_no=2,
                    direction="A_TO_B",
                    verdict="normal",
                    observed_at=_at(n),
                    received_at=_at(n),
                )
            )
        # 통과보다 나중에 들어온 다른 계열 — 필터 없는 상한을 통째로 먹는 쪽이다.
        for n in range(3):
            s.add(
                TaggingEvent(
                    event_id=f"win-tag-{n}",
                    tag_id="TAG-WIN",
                    gate_no=2,
                    observed_at=_at(100 + n),
                    received_at=_at(100 + n),
                )
            )
        await s.commit()

    # 결함이 나던 조건 — 필터 없이 상한 3이면 통과가 한 줄도 안 남는다.
    merged = (await client.get("/api/events?limit=3")).json()
    assert GATE_PASS_KIND not in _kinds(merged), (
        "다른 계열이 상한을 안 먹었다 — 이 케이스가 아무것도 안 재고 있다"
    )

    # 같은 상한에서도 계열을 주면 그 계열의 최근 3건이 온전히 온다.
    only_gate = (await client.get(f"/api/events?kind={GATE_PASS_KIND}&limit=3")).json()
    assert _kinds(only_gate) == {GATE_PASS_KIND}
    assert _event_ids(only_gate) == ["win-gate-2", "win-gate-1", "win-gate-0"]


async def test_필터를_걸어도_정렬은_received_at_desc_id_desc다(client):
    """상한 안에서 자르는 쪽은 여전히 오래된 끝이고, 같은 시각이면 id 내림차순이다."""
    async with get_session() as s:
        for suffix in ("a", "b", "c"):
            s.add(
                TaggingEvent(
                    event_id=f"ord-tie-{suffix}",
                    tag_id="TAG-ORD",
                    gate_no=3,
                    observed_at=_at(0),
                    received_at=_at(0),  # 셋 다 같은 시각
                )
            )
        await s.commit()

    rows = (await client.get(f"/api/events?kind={TAGGING_KIND}")).json()
    assert _event_ids(rows) == ["ord-tie-c", "ord-tie-b", "ord-tie-a"]

    narrowed = (await client.get(f"/api/events?kind={TAGGING_KIND}&limit=2")).json()
    assert _event_ids(narrowed) == ["ord-tie-c", "ord-tie-b"]


# ── 모르는 kind는 422 ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["robot", "nope", "TAGGING", "gate", ""])
async def test_모르는_kind는_422다(client, bad):
    """UNION 가지가 딱 셋이라 그 밖의 값은 답이 있을 수 없는 질의다.

    빈 목록으로 조용히 돌려주면 화면이 오타를 "그 계열은 오늘 없다"로 그린다.
    `robot`이 여기 끼어 있는 게 `/api/alerts`와 갈리는 지점이다 — 경고 `source_type`에는
    `robot`이 실재하지만 이벤트에는 그런 가지가 없다.
    """
    res = await client.get(f"/api/events?kind={bad}")
    assert res.status_code == 422, f"{bad!r}가 422가 아니다 — {res.status_code} {res.text}"


async def test_알림_쪽_source_type은_그대로_빈_200이다(client):
    """일부러 다르게 간 자리라 반대쪽이 안 딸려 바뀌었는지 같이 잰다(기존 계약 불변)."""
    res = await client.get("/api/alerts?source_type=nope")
    assert res.status_code == 200, res.text
    assert res.json() == []


# ── 기존 계약 불변 ─────────────────────────────────────────────────────────

async def test_kind를_안_주면_예전_그대로_3계열_합본이다(client):
    await _seed_one_each("keep")

    res = await client.get("/api/events")
    assert res.status_code == 200, res.text
    rows = res.json()
    assert _kinds(rows) == set(ALL_KINDS), f"합본에서 계열이 빠졌다 — 실제 {_kinds(rows)}"
    assert _event_ids(rows) == ["keep-shuttle", "keep-gate", "keep-tag"]


async def test_계열별_결과를_합치면_합본과_같다(client):
    """필터가 행을 잃거나 만들지 않는다 — 축이 붙어도 자료는 같은 한 벌이다."""
    await _seed_one_each("sum", offset=0)
    await _seed_one_each("sum2", offset=10)

    merged = _event_ids((await client.get("/api/events")).json())
    per_kind: list[str] = []
    for kind in ALL_KINDS:
        per_kind += _event_ids((await client.get(f"/api/events?kind={kind}")).json())

    assert sorted(per_kind) == sorted(merged)
