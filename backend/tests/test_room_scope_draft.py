"""[초안·팀 확정 대기] 안건① 룸 단위 크레딧 검증.

증명할 두 가지.
1. 플래그가 꺼진 기본 상태에서는 기존 동작이 그대로다 — 룸이 달라도 크레딧을 교차 소비한다.
   (지금 시뮬 멀티룸에서 나는 실측 결함을 시험으로 못박아 회의 자료로 쓴다.)
2. 플래그를 켜면 룸이 갈려 A룸 태깅을 B룸 통과가 못 먹는다.

플래그는 env 변수를 monkeypatch로 바꾸고 get_settings.cache_clear()로 재구성해서 켠다.
get_settings()가 lru_cache라 인스턴스가 하나뿐인데, 속성을 직접 대입하면 재구성 시점마다
값이 갈릴 수 있어 xdist 병렬에 안전하지 않다 — env+cache_clear면 항상 env가 정본이라
안전하다. 상태 오염이 다음 케이스로 새지 않게 되돌릴 때도 env를 되돌리고 cache_clear로
다시 강제한다.
"""
import contextlib
import datetime as dt

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.credit.state_machine import CreditState, Verdict
from app.db import get_session
from app.models import TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 27, 3, 0, 0, tzinfo=dt.timezone.utc)


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


@contextlib.contextmanager
def room_scope(enabled: bool):
    """credit_scope_room 플래그를 env+cache_clear로 잠깐 바꾼다. 빠져나갈 때 되돌린다."""
    mp = pytest.MonkeyPatch()
    mp.setenv("CREDIT_SCOPE_ROOM", "true" if enabled else "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


def _tagging(event_id: str, tag_id: str, offset: float, room_id: str | None) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 1,
        "room_id": room_id,
        "tag_id": tag_id,
        "observed_at": _iso(offset),
    }


def _pass(event_id: str, offset: float, room_id: str | None, direction: str = "A_TO_B") -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 1,
        "room_id": room_id,
        "direction": direction,
        "status": "complete",
        "beam_a_ts": _iso(offset),
        "beam_b_ts": _iso(offset + 0.2),
        "observed_at": _iso(offset),
    }


async def _alert_count() -> int:
    from app.models import Alert

    async with get_session() as s:
        return len((await s.execute(select(Alert))).scalars().all())


@pytest.fixture
def popups(monkeypatch):
    """WS `untagged_alert` 팝업이 몇 번 나갔나를 센다.

    쿨다운이 억제하는 건 **외부 알림**이고 Alert 행은 이제 미태깅마다 무조건 남는다
    (2026-07-30 판정 로직 수리 — 쿨다운 창에 삼켜진 사건 279건이 실서버에서 통째로
    사라진 자리를 고쳤다). 그래서 "쿨다운 키가 제대로 갈리나"를 Alert 행 수로 세면
    이제 아무것도 안 짚는다 — 울린 횟수를 직접 세야 한다.
    """
    from app.ws import manager

    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


def _popup_count(box: list[dict]) -> int:
    return len([m for m in box if m["type"] == "untagged_alert"])


async def _state(event_id: str) -> str:
    async with get_session() as s:
        return (
            await s.execute(
                select(TaggingEvent.credit_state).where(TaggingEvent.event_id == event_id)
            )
        ).scalar_one()


# 1. 기본값(플래그 꺼짐) — room_id를 실어 보내도 판정이 안 바뀐다(하위 호환).
async def test_flag_off_keeps_cross_room_consumption(client, auth_headers):
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "20250001", 0, "roomA"), headers=auth_headers
    )
    r = await client.post(
        "/api/gate-pass-events", json=_pass("P-B", 1.0, "roomB"), headers=auth_headers
    )
    # 룸이 다른데도 A룸 크레딧을 먹는다 — 이게 지금 실측되는 교차 소비다.
    assert r.json()["verdict"] == Verdict.NORMAL
    assert await _state("T-A") == CreditState.CONSUMED


# 2. 플래그 켜짐 — 룸이 다르면 크레딧을 못 먹고 미태깅으로 잡힌다.
async def test_flag_on_splits_credit_by_room(client, auth_headers):
    with room_scope(True):
        await client.post(
            "/api/tagging-events", json=_tagging("T-A", "20250001", 0, "roomA"), headers=auth_headers
        )
        r = await client.post(
            "/api/gate-pass-events", json=_pass("P-B", 1.0, "roomB"), headers=auth_headers
        )
        assert r.json()["verdict"] == Verdict.UNTAGGED
        assert await _state("T-A") == CreditState.ISSUED  # A룸 크레딧은 그대로 살아 있다


# 3. 플래그 켜짐 — 같은 룸이면 예전처럼 정상 소비한다.
async def test_flag_on_same_room_still_consumes(client, auth_headers):
    with room_scope(True):
        await client.post(
            "/api/tagging-events", json=_tagging("T-A", "20250001", 0, "roomA"), headers=auth_headers
        )
        r = await client.post(
            "/api/gate-pass-events", json=_pass("P-A", 1.0, "roomA"), headers=auth_headers
        )
        assert r.json()["verdict"] == Verdict.NORMAL
        assert await _state("T-A") == CreditState.CONSUMED


# 4. 플래그 켜짐 — room_id를 아예 안 보내는 예전 퍼블리셔끼리도 서로 맞물린다(NULL 그룹).
async def test_flag_on_null_room_matches_null_room(client, auth_headers):
    with room_scope(True):
        await client.post(
            "/api/tagging-events", json=_tagging("T-N", "20250001", 0, None), headers=auth_headers
        )
        r = await client.post(
            "/api/gate-pass-events", json=_pass("P-N", 1.0, None), headers=auth_headers
        )
        assert r.json()["verdict"] == Verdict.NORMAL


# 5. 플래그 켜짐 — 쿨다운도 룸별로 갈려 B룸 경고가 A룸 경고에 안 먹힌다.
async def test_flag_on_cooldown_is_per_room(client, auth_headers, popups):
    with room_scope(True):
        r1 = await client.post(
            "/api/gate-pass-events", json=_pass("P1", 1.0, "roomA"), headers=auth_headers
        )
        r2 = await client.post(
            "/api/gate-pass-events", json=_pass("P2", 1.2, "roomB"), headers=auth_headers
        )
        assert r1.json()["verdict"] == Verdict.UNTAGGED
        assert r2.json()["verdict"] == Verdict.UNTAGGED

    assert _popup_count(popups) == 2  # 룸이 다르니 쿨다운에 안 묶인다
    assert await _alert_count() == 2  # 사건 기록은 어차피 둘 다 남는다


# 5-b. 회의 대조군 — 같은 시나리오인데 플래그가 꺼지면 두 룸 경고가 하나로 묶인다.
# 5번이랑 나란히 보여주는 자리다. 지금 현장에선 B룸 미태깅이 A룸 쿨다운에 삼켜진다.
async def test_flag_off_cooldown_merges_rooms(client, auth_headers, popups):
    r1 = await client.post(
        "/api/gate-pass-events", json=_pass("P1", 1.0, "roomA"), headers=auth_headers
    )
    r2 = await client.post(
        "/api/gate-pass-events", json=_pass("P2", 1.2, "roomB"), headers=auth_headers
    )
    assert r1.json()["verdict"] == Verdict.UNTAGGED
    assert r2.json()["verdict"] == Verdict.UNTAGGED

    # 룸이 달라도 게이트 키 하나로 묶여 두 번째 **알림**이 사라진다.
    assert _popup_count(popups) == 1
    # 다만 사건 기록은 둘 다 남는다 — 삼켜지는 건 울림뿐이다.
    assert await _alert_count() == 2


# 6. 롤백 안전판 — 플래그를 되돌리면 교차 소비 동작으로 즉시 복귀한다.
async def test_flag_toggle_back_restores_legacy(client, auth_headers):
    with room_scope(True):
        pass
    assert get_settings().credit_scope_room is False
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "20250001", 0, "roomA"), headers=auth_headers
    )
    r = await client.post(
        "/api/gate-pass-events", json=_pass("P-B", 1.0, "roomB"), headers=auth_headers
    )
    assert r.json()["verdict"] == Verdict.NORMAL


# ── 병합 접점(2026-07-28 dev 현행화) ──────────────────────────────────────
# 쿨다운 키는 초안이 룸 축을 넣었고, 값은 dev 수리판이 (이벤트 시각, 벽시계) 두 축을
# 넣었다. 둘을 합치면서 "룸 키를 쓰는 길에서도 벽시계 축이 그대로 사나"라는 조합이
# 새로 생겼는데, 이 조합만 어느 쪽 시험도 안 짚고 있었다 — 돌연변이를 심어도 189건이
# 다 통과하는 걸 실측으로 확인하고 아래 두 건을 보강했다.


# 7. 플래그 꺼짐 — room_id를 실어 보내도 두 축 쿨다운이 dev와 똑같이 돈다.
async def test_flag_off_room_payload_keeps_two_axis_cooldown(
    client, auth_headers, wall_clock, popups
):
    cooldown = get_settings().alert_cooldown_sec
    r1 = await client.post(
        "/api/gate-pass-events", json=_pass("X1", 1.0, "roomA"), headers=auth_headers
    )
    # 룸이 달라도 플래그가 꺼져 있으면 게이트 키 하나다. 두 축 다 창 안이라 삼켜진다.
    r2 = await client.post(
        "/api/gate-pass-events", json=_pass("X2", 1.5, "roomB"), headers=auth_headers
    )
    # 이벤트 시각은 아직 창 안인데 실제 시간만 창을 넘겼다 — 벽시계 축이 살아 있으면 다시 울린다.
    wall_clock["t"] += cooldown + 1.0
    r3 = await client.post(
        "/api/gate-pass-events", json=_pass("X3", 2.0, "roomA"), headers=auth_headers
    )
    assert [r.json()["verdict"] for r in (r1, r2, r3)] == [Verdict.UNTAGGED] * 3
    assert _popup_count(popups) == 2, "room_id를 실었다고 두 축 쿨다운이 달라졌다"
    assert await _alert_count() == 3, "삼켜진 사건의 Alert 행이 안 남았다"


# 8. 플래그 켜짐 — 키가 룸별로 갈려도 벽시계 축은 그대로 산다.
async def test_flag_on_room_key_still_honors_wall_axis(
    client, auth_headers, wall_clock, popups
):
    cooldown = get_settings().alert_cooldown_sec
    with room_scope(True):
        r1 = await client.post(
            "/api/gate-pass-events", json=_pass("Y1", 1.0, "roomA"), headers=auth_headers
        )
        # 같은 룸이라 키가 같고 두 축 다 창 안 — 삼켜진다.
        r2 = await client.post(
            "/api/gate-pass-events", json=_pass("Y2", 1.5, "roomA"), headers=auth_headers
        )
        wall_clock["t"] += cooldown + 1.0
        # 벽시계 축만 창 밖 — 룸 키를 타고 와도 다시 울려야 한다.
        r3 = await client.post(
            "/api/gate-pass-events", json=_pass("Y3", 2.0, "roomA"), headers=auth_headers
        )
    assert [r.json()["verdict"] for r in (r1, r2, r3)] == [Verdict.UNTAGGED] * 3
    assert _popup_count(popups) == 2, "룸 키로 갈리면서 벽시계 축이 죽었다"
    assert await _alert_count() == 3, "삼켜진 사건의 Alert 행이 안 남았다"
