"""`GET /api/alerts`의 발화 이벤트 축 — `?source_type=`·`?source_id=` (프론트 요구 N13).

## 왜 이 시험이 따로 있나

이벤트 표의 "처리" 열(미확인/확인/종결)은 이벤트 한 줄에서 경고를 **역참조해서** 그린다.
짝짓는 규칙은 이벤트의 `(kind, id)` ↔ 경고의 `(source_type, source_id)`다(S15P11C207-192).

이 축이 없던 동안 화면은 경고 목록을 최근순 상한(`limit`, 최대 500)만큼 받아 스스로 뒤졌다.
그래서 **그 창 밖으로 밀린 경고는 존재하는데도 "기록 없음"으로 그려졌다.** 여기서 못박는 건
"필터가 붙었다"가 아니라 **"상한 밖으로 밀린 경고가 이 축으로는 집힌다"**다 —
`test_상한_밖으로_밀린_경고도_짝으로_부르면_온다`가 그 줄이다.

## 무엇을 못박나

- 짝으로 걸면 그 이벤트가 낸 경고만 온다(다른 이벤트가 낸 건 안 섞인다).
- 계열이 다르면 **행 id가 같아도** 안 섞인다 — 이게 `source_id`만 받으면 안 되는 이유다.
- 없는 짝이면 빈 목록에 200(404가 아니다).
- `source_type`만 주면 그 종류 전체, `source_id`만 주면 422.
- 범위 밖 값(0 이하 id·32자 넘는 종류)은 조용히 안 자르고 422.
- 축을 안 주면 예전 응답 그대로다(기존 계약 불변).

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

BASE = dt.datetime(2026, 7, 31, 6, 0, 0, tzinfo=dt.timezone.utc)
HEADERS = {"X-API-Key": get_settings().api_key}


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


async def _make_untagged_alert(client, *, gate_no: int, tag: str) -> tuple[int, int]:
    """태깅 없이 통과를 하나 넣어 미태깅 경고를 만든다.

    돌려주는 건 `(경고 id, 그 경고를 낸 통과 행 id)`다. 뒤엣값이 이벤트 한 줄의 `id`이고
    화면이 `(kind, id)`로 들고 오는 바로 그 값이다.
    """
    res = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": f"p-src-{tag}",
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
        assert row.source_type == "gate_pass", row.source_type
        assert row.source_id is not None
        return row.id, row.source_id


def _ids(rows) -> set[int]:
    return {r["id"] for r in rows}


# ── 화면이 실제로 거는 질의 ────────────────────────────────────────────────

async def test_짝으로_걸면_그_이벤트가_낸_경고만_온다(client):
    """이벤트 표 한 줄이 자기 처리 상태를 되짚는 흐름 그대로다."""
    first_alert, first_src = await _make_untagged_alert(client, gate_no=1, tag="one")
    second_alert, second_src = await _make_untagged_alert(client, gate_no=2, tag="two")
    assert first_src != second_src

    rows = (
        await client.get(f"/api/alerts?source_type=gate_pass&source_id={first_src}")
    ).json()
    assert _ids(rows) == {first_alert}
    assert second_alert not in _ids(rows)
    # 응답 한 줄에 짝이 그대로 실려 있어야 화면이 되짚은 값을 검산할 수 있다.
    assert rows[0]["source_type"] == "gate_pass"
    assert rows[0]["source_id"] == first_src


async def test_이벤트_목록의_kind_id를_그대로_넣어도_붙는다(client):
    """화면은 `(kind, id)`를 `/api/events`에서 받는다. 두 창구의 글자가 갈리면 안 붙는다.

    셔틀 라벨이 예전에 `shuttle`(이벤트)과 `shuttle_arrival`(경고)로 갈려 있던 자리라
    글자를 손으로 적지 않고 목록에서 받은 값을 그대로 되먹인다.
    """
    alert_id, src_id = await _make_untagged_alert(client, gate_no=3, tag="feed")

    events = (await client.get("/api/events")).json()
    row = [e for e in events if e["kind"] == "gate_pass" and e["id"] == src_id][0]

    got = (
        await client.get(
            f"/api/alerts?source_type={row['kind']}&source_id={row['id']}"
        )
    ).json()
    assert _ids(got) == {alert_id}


async def test_상한_밖으로_밀린_경고도_짝으로_부르면_온다(client):
    """N13이 고치는 결함 그대로 — 최근순 상한 밖이면 화면이 "기록 없음"을 그렸다."""
    old_alert, old_src = await _make_untagged_alert(client, gate_no=4, tag="old")
    await _make_untagged_alert(client, gate_no=5, tag="new")

    # 상한을 1로 좁히면 옛 경고는 창 밖으로 밀린다(결함이 나던 조건).
    narrow = _ids((await client.get("/api/alerts?limit=1")).json())
    assert old_alert not in narrow, "상한 밖으로 안 밀렸다 — 이 케이스가 아무것도 안 재고 있다"

    # 같은 상한에서도 짝으로 부르면 집힌다.
    got = _ids(
        (
            await client.get(
                f"/api/alerts?source_type=gate_pass&source_id={old_src}&limit=1"
            )
        ).json()
    )
    assert got == {old_alert}


async def test_계열이_다르면_행_id가_같아도_안_섞인다(client):
    """`source_id`만으로는 행이 안 정해진다는 걸 실물로 잰다.

    계열마다 시퀀스가 따로라 통과 12번과 셔틀 12번이 동시에 존재한다. 짝 강제(422)가
    지키는 게 바로 이 자리다 — id만 받으면 화면이 남의 사건 처리 상태를 그린다.

    셔틀 쪽은 경고 행을 직접 심는다. 그래야 `source_id`를 통과 행과 **같은 숫자**로
    맞출 수 있다(인입으로 만들면 id를 고를 수 없다).
    """
    gate_alert, src_id = await _make_untagged_alert(client, gate_no=6, tag="clash")
    async with get_session() as session:
        twin = Alert(
            type="robot_arrival",
            severity="info",
            source_type="shuttle_arrival",
            source_id=src_id,  # 일부러 같은 숫자
            created_at=BASE,
        )
        session.add(twin)
        await session.commit()
        twin_id = twin.id

    gate_rows = (
        await client.get(f"/api/alerts?source_type=gate_pass&source_id={src_id}")
    ).json()
    shuttle_rows = (
        await client.get(f"/api/alerts?source_type=shuttle_arrival&source_id={src_id}")
    ).json()

    assert _ids(gate_rows) == {gate_alert}
    assert _ids(shuttle_rows) == {twin_id}
    assert gate_alert != twin_id


async def test_없는_짝이면_빈_목록에_200이다(client):
    """"그 이벤트는 경고를 안 냈다"가 정상 답이라 404가 아니다."""
    await _make_untagged_alert(client, gate_no=7, tag="none")
    res = await client.get("/api/alerts?source_type=gate_pass&source_id=987654")
    assert res.status_code == 200, res.text
    assert res.json() == []


# ── 축 하나만 줬을 때 ──────────────────────────────────────────────────────

async def test_source_type만_주면_그_종류_전체다(client):
    a1, _ = await _make_untagged_alert(client, gate_no=1, tag="t1")
    a2, _ = await _make_untagged_alert(client, gate_no=2, tag="t2")
    async with get_session() as session:
        other = Alert(
            type="robot_arrival",
            severity="info",
            source_type="shuttle_arrival",
            source_id=1,
            created_at=BASE,
        )
        session.add(other)
        await session.commit()
        other_id = other.id

    got = _ids((await client.get("/api/alerts?source_type=gate_pass")).json())
    assert {a1, a2} <= got
    assert other_id not in got


async def test_source_id만_주면_422다(client):
    res = await client.get("/api/alerts?source_id=1")
    assert res.status_code == 422, res.text


async def test_source_type이_빈_글자면_짝이_안_선_것으로_본다(client):
    """`?source_type=&source_id=1`도 id만 준 것과 같다 — 빈 글자로 게이트를 못 지나간다."""
    res = await client.get("/api/alerts?source_type=&source_id=1")
    assert res.status_code == 422, res.text


async def test_모르는_source_type은_빈_목록이다(client):
    """아는 글자 목록으로 안 좁힌다. 좁히면 `robot` 출처 경고를 뽑는 질의가 422가 된다."""
    await _make_untagged_alert(client, gate_no=8, tag="unknown")
    res = await client.get("/api/alerts?source_type=nope")
    assert res.status_code == 200, res.text
    assert res.json() == []


# ── 범위 밖 값은 조용히 안 자른다 ──────────────────────────────────────────

@pytest.mark.parametrize("bad", ["0", "-1", "abc"])
async def test_잘못된_source_id는_422다(client, bad):
    res = await client.get(f"/api/alerts?source_type=gate_pass&source_id={bad}")
    assert res.status_code == 422, res.text


async def test_32자를_넘는_source_type은_422다(client):
    """DB 컬럼이 String(32)라 그 위는 애초에 안 맞는 값이다."""
    ok = await client.get(f"/api/alerts?source_type={'a' * 32}")
    assert ok.status_code == 200, ok.text
    too_long = await client.get(f"/api/alerts?source_type={'a' * 33}")
    assert too_long.status_code == 422, too_long.text


# ── 기존 계약 불변 ─────────────────────────────────────────────────────────

async def test_축을_안_주면_예전_그대로다(client):
    """추가만 있고 변경은 없다 — 기본 목록에 걸러지는 게 생기면 안 된다."""
    a1, _ = await _make_untagged_alert(client, gate_no=1, tag="keep1")
    a2, _ = await _make_untagged_alert(client, gate_no=2, tag="keep2")
    got = _ids((await client.get("/api/alerts")).json())
    assert {a1, a2} <= got


async def test_다른_축과_같이_주면_교집합이다(client):
    """화면 질의는 `type`·`active`·`status`에 이 축이 얹힌 모양이다."""
    alert_id, src_id = await _make_untagged_alert(client, gate_no=9, tag="mix")
    pair = f"source_type=gate_pass&source_id={src_id}"

    assert _ids((await client.get(f"/api/alerts?type=untagged&{pair}")).json()) == {alert_id}
    assert _ids((await client.get(f"/api/alerts?active=true&{pair}")).json()) == {alert_id}
    assert _ids((await client.get(f"/api/alerts?status=OPEN&{pair}")).json()) == {alert_id}

    # 종류가 안 맞으면 짝이 맞아도 안 온다.
    assert (await client.get(f"/api/alerts?type=robot_arrival&{pair}")).json() == []

    # 확인을 누르면 ack 축에서 빠진다(짝은 그대로 맞는데도).
    await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)
    assert (await client.get(f"/api/alerts?active=true&{pair}")).json() == []
    assert _ids((await client.get(f"/api/alerts?{pair}")).json()) == {alert_id}
