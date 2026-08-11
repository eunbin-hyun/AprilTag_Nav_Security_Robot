"""크레딧 상태머신 판정 (아키텍처 2026-07-23 §03) 시나리오 시험.

일곱 갈래를 실 DB로 관통 검증한다.
1. 정상 통과 — 유효 크레딧 소비
2. 미태깅 — 크레딧 없이 통과
3. 만료 뒤 통과 — 크레딧이 있어도 만료라 소비 안 됨
4. 재태깅 덮어쓰기 — 같은 카드 재태깅이 이전 크레딧을 만료로 덮음
5. 퇴장 회수 — B→A 퇴장이 가장 오래된 미소비 크레딧을 회수
6. 재시도 중복 인입 — 같은 event_id 재전송이 크레딧을 두 번 안 먹음
7. 동시 통과 — 통과 두 건이 같은 크레딧을 못 집음
8. 저신뢰 플래그 — 미소비 유효 크레딧 과다
9. 쿨다운 — 같은 게이트 반복 미태깅은 **알림**만 한 번(Alert 행은 매 건 남는다)
10~11. 만료 크레딧 회수 — 뒤늦은 퇴장이 옛 만료분을 회수하지 않음
12. 기기 시계가 미래로 튀어도 유효 크레딧이 전량 만료되지 않음(회수도 안 된다)
13. 입장 경로도 만료분을 닫지만 유효 통과 판정은 안 뒤집음
14. 지연 인입(백로그·배속) — 뒤늦게 밀려온 통과가 남의 유효 크레딧을 닫지 않음
15. 기기 시계가 과거로 튄 퇴장이 벽시계로 만료된 크레딧을 회수하지 않음

판정 창은 서버 현재와 무관하게 관측 시각 기준이라(lazy expiry) 고정 시각 BASE로 재현한다.
**만료 청소는 이벤트 시각과 서버 수신 시각 중 이른 쪽, 퇴장 회수는 늦은 쪽으로 돈다**(시계 둘의
합의) — 그래서 "아직 살아 있는 크레딧"이 필요한 케이스는 BASE로 못 만든다(서버 시계로 보면
한참 지난 시각이라 청소가 닫고 회수도 안 잡는다). 그런 케이스만 `_live()`로 서버 현재를 기준
시각으로 잡는다.

2026-08-01부터 케이스마다 **UID 짝 단정**을 같이 든다(S15P11C207-241). 통과 행의
`tag_id`는 그 통과가 태운 크레딧의 카드 UID고, `matched_tagging_event_id`와 늘 같이 차거나
같이 빈다. 소비 규칙(FIFO·창·청소)은 한 글자도 안 바뀌었으니 여기 판정 단정은 그대로다 —
새로 붙은 건 "그 소비가 누구 크레딧이었나"를 통과 행이 잃지 않는가 하나다. 신원 축만
따로 보는 갈래(순서대로 두 명·미태깅 null·만료분 안 붙음·조회·WS·재전송)는
`tests/test_credit_identity.py`에 있다.

⚠ 퇴장 크레딧 회수는 2026-07-30부터 기본으로 꺼졌다. 5·10·11·12·15번은 팀이 정본 468행을
되살릴 때를 대비해 회수 규칙을 계속 지키는 자리라 `exit_recovery_on` 픽스처로 켜고 돈다.
기본 배치 거동(회수 안 함)은 `test_verdict_hardening.py`가 짚는다.
"""
import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.credit.state_machine import CreditState, Verdict, evaluate_pass
from app.db import get_session
from app.models import Alert, GatePassEvent, TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 24, 3, 0, 0, tzinfo=dt.timezone.utc)


def _live() -> dt.datetime:
    """지금 발급해도 살아 있는 크레딧을 만들 기준 시각(TTL 3초 안에 케이스가 끝난다)."""
    return dt.datetime.now(dt.timezone.utc)


@pytest.fixture
def exit_recovery_on(monkeypatch):
    """퇴장 크레딧 회수를 켠다(5·10·11·12·15번 케이스용).

    2026-07-30부터 회수는 기본으로 꺼졌다 — 퇴장은 안쪽에서 나오는 몸이라 바깥에서 기다리는
    사람의 크레딧만 먹는 구조였고, 실측에서 정상 입장이 미태깅으로 뒤집혔다
    (`app/credit/state_machine.py`의 `EXIT_RECOVERY_ENABLED` 주석에 물리 근거가 있다).

    정본(`docs/아키텍처_2026-07-23.html:468`)이 회수를 명시했으니 되살릴지는 팀 결정이고,
    그날을 대비해 회수 선택·시각 가드 규칙을 지키는 케이스들은 스위치를 켜고 돈다.
    """
    from app.credit import state_machine

    monkeypatch.setattr(state_machine, "EXIT_RECOVERY_ENABLED", True)


def _iso(offset_sec: float, base: dt.datetime | None = None) -> str:
    return ((base or BASE) + dt.timedelta(seconds=offset_sec)).isoformat()


async def _tagging(
    event_id: str,
    tag_id: str,
    observed_offset: float,
    gate_no: int = 1,
    base: dt.datetime | None = None,
) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": gate_no,
        "tag_id": tag_id,
        "observed_at": _iso(observed_offset, base),
    }


async def _pass(event_id: str, direction: str | None, status: str,
                beam_a_offset: float, gate_no: int = 1,
                base: dt.datetime | None = None) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": gate_no,
        "direction": direction,
        "status": status,
        "beam_a_ts": _iso(beam_a_offset, base),
        "beam_b_ts": _iso(beam_a_offset + 0.2, base) if status == "complete" else None,
        "observed_at": _iso(beam_a_offset, base),
    }


async def _get_tagging(event_id: str) -> TaggingEvent:
    async with get_session() as s:
        return (
            await s.execute(select(TaggingEvent).where(TaggingEvent.event_id == event_id))
        ).scalar_one()


async def _count(model) -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


# 1. 정상 통과 — 유효 크레딧 소비
async def test_normal_pass_consumes_credit(client, auth_headers):
    await client.post("/api/tagging-events", json=await _tagging("T1", "20250001", 0), headers=auth_headers)
    r = await client.post(
        "/api/gate-pass-events", json=await _pass("P1", "A_TO_B", "complete", 1.0), headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == Verdict.NORMAL

    tag = await _get_tagging("T1")
    assert tag.credit_state == CreditState.CONSUMED
    assert tag.consumed_by_pass_id is not None

    async with get_session() as s:
        gp = (await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == "P1"))).scalar_one()
    assert gp.verdict == Verdict.NORMAL
    assert gp.matched_tagging_event_id == tag.id
    # 통과 행이 태운 크레딧의 카드 UID까지 들고 있다(S15P11C207-241). 행 id와 UID는 짝이다.
    assert gp.tag_id == tag.tag_id == "20250001"


# 2. 미태깅 — 크레딧 없이 통과
async def test_untagged_no_credit(client, auth_headers):
    r = await client.post(
        "/api/gate-pass-events", json=await _pass("P1", "A_TO_B", "complete", 1.0), headers=auth_headers
    )
    assert r.json()["verdict"] == Verdict.UNTAGGED

    assert await _count(Alert) == 1
    async with get_session() as s:
        alert = (await s.execute(select(Alert))).scalar_one()
        gp = (await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == "P1"))).scalar_one()
    assert alert.type == "untagged"
    assert alert.source_type == "gate_pass"
    assert alert.source_id == gp.id  # 발화 이벤트 역추적
    # 태운 크레딧이 없으니 UID 칸도 비어 있다 — 미태깅에 남의 이름이 붙으면 안 된다(-241).
    assert gp.tag_id is None and gp.matched_tagging_event_id is None


# 3. 만료 뒤 통과 — TTL 3.0초를 넘긴 빔은 크레딧을 못 쓴다
async def test_expired_credit_untagged(client, auth_headers):
    await client.post("/api/tagging-events", json=await _tagging("T1", "20250001", 0), headers=auth_headers)
    # 만료 3.0초 → beam_a_ts를 4.0초 뒤로 두면 now > expires_at
    r = await client.post(
        "/api/gate-pass-events", json=await _pass("P1", "A_TO_B", "complete", 4.0), headers=auth_headers
    )
    assert r.json()["verdict"] == Verdict.UNTAGGED

    tag = await _get_tagging("T1")
    # 판정은 소비 시점 비교로 하고(lazy expiry), 판정 뒤 청소가 만료분을 expired로 닫는다.
    # 입장 경로에도 청소가 붙어서 issued로 남지 않는다(3차 수리 4번).
    assert tag.credit_state == CreditState.EXPIRED
    assert tag.consumed_by_pass_id is None
    assert tag.consumed_at is None

    # 만료된 크레딧의 UID는 통과 행에 안 붙는다(-241). 여기가 새는 자리다 — 태깅 행은
    # 같은 게이트에 멀쩡히 있어서, UID를 "소비"가 아니라 "최근 태깅 조회"로 채우면
    # 미태깅 통과에 방금 만료된 사람 이름이 붙는다.
    async with get_session() as s:
        gp = (await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == "P1"))).scalar_one()
    assert gp.verdict == Verdict.UNTAGGED
    assert gp.tag_id is None, gp.tag_id


# 4. 재태깅 덮어쓰기 — 같은 카드가 다시 찍으면 이전 크레딧이 만료로 덮인다
async def test_retag_supersedes_previous(client, auth_headers):
    await client.post("/api/tagging-events", json=await _tagging("T1", "20250001", 0.0), headers=auth_headers)
    await client.post("/api/tagging-events", json=await _tagging("T2", "20250001", 0.5), headers=auth_headers)

    t1 = await _get_tagging("T1")
    t2 = await _get_tagging("T2")
    assert t1.credit_state == CreditState.EXPIRED
    assert t2.credit_state == CreditState.ISSUED

    # 통과는 살아 있는 최신 크레딧(T2)을 소비해야 한다
    r = await client.post(
        "/api/gate-pass-events", json=await _pass("P1", "A_TO_B", "complete", 1.0), headers=auth_headers
    )
    assert r.json()["verdict"] == Verdict.NORMAL
    t2 = await _get_tagging("T2")
    assert t2.credit_state == CreditState.CONSUMED
    async with get_session() as s:
        gp = (await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == "P1"))).scalar_one()
    assert gp.matched_tagging_event_id == t2.id
    # 덮인 크레딧(T1)이 아니라 살아남은 크레딧(T2)의 UID가 붙는다. 같은 카드라 값은 같고,
    # 어느 행을 태웠나는 matched_tagging_event_id가 가른다.
    assert gp.tag_id == t2.tag_id


# 5. 퇴장 회수 — B→A는 경고 없이 가장 오래된 미소비 크레딧을 회수
async def test_exit_recovers_oldest(client, auth_headers, exit_recovery_on):
    """회수는 살아 있는 크레딧이 대상이라 기준 시각을 서버 현재로 잡는다.

    고정 과거 시각으로 잡으면 만료 청소(서버 수신 시각 기준)가 먼저 닫아서 회수할 게 없다 —
    그 상황은 10번·11번 케이스가 따로 본다.

    ⚠ 2026-07-30부터 회수는 기본으로 꺼졌다(퇴장이 구조적으로 남의 크레딧만 먹는다 —
    `state_machine.EXIT_RECOVERY_ENABLED` 주석). 이 케이스는 팀이 정본 468행을 되살리기로
    정할 때를 대비해 회수 선택 규칙을 계속 지키는 자리라, 스위치를 켜고 돈다. 기본 배치의
    거동(회수 안 함)은 `test_verdict_hardening.py`가 짚는다.
    """
    live = _live()
    await client.post(
        "/api/tagging-events",
        json=await _tagging("T1", "20250001", 0, base=live),
        headers=auth_headers,
    )
    r = await client.post(
        "/api/gate-pass-events",
        json=await _pass("P1", "B_TO_A", "complete", 1.0, base=live),
        headers=auth_headers,
    )
    assert r.json()["verdict"] == Verdict.EXIT

    tag = await _get_tagging("T1")
    assert tag.credit_state == CreditState.RECOVERED
    assert await _count(Alert) == 0  # 퇴장은 경고 대상이 아니다

    # 회수는 소비가 아니라 되돌리기라 통과 행에 UID를 안 적는다(-241). 퇴장은 안쪽에서
    # 나오는 몸이고 회수 대상은 구조적으로 남의 크레딧이라, 여기 이름을 적으면 그 UID가
    # "이 사람이 나갔다"로 읽힌다.
    async with get_session() as s:
        gp = (await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == "P1"))).scalar_one()
    assert gp.tag_id is None and gp.matched_tagging_event_id is None


# 6. 재시도 중복 인입 — 같은 event_id 재전송이 크레딧을 두 번 소비하지 않는다
async def test_retry_duplicate_no_double_consume(client, auth_headers):
    await client.post("/api/tagging-events", json=await _tagging("T1", "20250001", 0), headers=auth_headers)
    body = await _pass("P1", "A_TO_B", "complete", 1.0)

    r1 = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    r2 = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)  # 같은 event_id 재전송

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["stored"] is True and r1.json()["verdict"] == Verdict.NORMAL
    assert r2.json()["stored"] is False  # 멱등 — 새 행 안 생김

    assert await _count(GatePassEvent) == 1
    async with get_session() as s:
        consumed = (
            await s.execute(
                select(func.count())
                .select_from(TaggingEvent)
                .where(TaggingEvent.credit_state == CreditState.CONSUMED)
            )
        ).scalar_one()
    assert consumed == 1  # 두 번째 재전송이 또 먹지 않았다


# 7. 동시 통과 — 크레딧 한 장에 통과 두 건이 동시에 와도 한 건만 소비
async def test_concurrent_passes_one_credit(client, auth_headers):
    await client.post("/api/tagging-events", json=await _tagging("T1", "20250001", 0), headers=auth_headers)

    body_a = await _pass("PA", "A_TO_B", "complete", 1.0)
    body_b = await _pass("PB", "A_TO_B", "complete", 1.0)
    r_a, r_b = await asyncio.gather(
        client.post("/api/gate-pass-events", json=body_a, headers=auth_headers),
        client.post("/api/gate-pass-events", json=body_b, headers=auth_headers),
    )

    verdicts = sorted([r_a.json()["verdict"], r_b.json()["verdict"]])
    assert verdicts == [Verdict.NORMAL, Verdict.UNTAGGED]  # 정확히 하나만 크레딧을 집었다

    async with get_session() as s:
        consumed = (
            await s.execute(
                select(func.count())
                .select_from(TaggingEvent)
                .where(TaggingEvent.credit_state == CreditState.CONSUMED)
            )
        ).scalar_one()
    assert consumed == 1
    assert await _count(Alert) == 1  # 미태깅 한 건만 경고

    # UID도 정확히 한 줄에만 붙는다(-241). 크레딧 한 장을 둘이 나눠 갖는 일이 없다.
    async with get_session() as s:
        rows = (await s.execute(select(GatePassEvent).order_by(GatePassEvent.id))).scalars().all()
    tagged = [r.tag_id for r in rows if r.tag_id is not None]
    assert tagged == ["20250001"], [(r.event_id, r.verdict, r.tag_id) for r in rows]
    for r in rows:
        # 짝 불변식 — 행 id와 UID는 같이 차거나 같이 빈다.
        assert (r.tag_id is None) == (r.matched_tagging_event_id is None)


# 8. 저신뢰 플래그 — 미소비 유효 크레딧이 임계(3)를 넘으면 신뢰도 낮음
async def test_low_confidence_when_unconsumed_piles_up(client, auth_headers):
    # 서로 다른 카드로 유효 크레딧 5장 발급(줄줄이 찍고 안 지나간 상황).
    # 남은 크레딧이 살아 있어야 저신뢰 집계에 들어가니 기준 시각은 서버 현재다.
    live = _live()
    for i in range(5):
        await client.post(
            "/api/tagging-events",
            json=await _tagging(f"T{i}", f"2025000{i}", 0.0, base=live),
            headers=auth_headers,
        )
    # 통과 한 건이 1장을 소비해도 남은 4장 > 임계 3 → 저신뢰
    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P1", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            # 빔 두 칸을 실기기 모양(a < b)으로 채운다 — 빔 A만 있는 complete는 라파가 못
            # 만드는 자료라 정합 검사(S15P11C207-243)가 어긋난 주장으로 본다. 판정 창은
            # 그대로 beam_a_ts라 이 시험이 재는 저신뢰 집계 축은 안 바뀐다.
            beam_a_ts=live + dt.timedelta(seconds=1),
            beam_b_ts=live + dt.timedelta(seconds=1.2),
            observed_at=live + dt.timedelta(seconds=1),
        )
    assert ev.verdict == Verdict.NORMAL
    assert ev.low_confidence is True
    # 크레딧이 다섯 장 열려 있어도 태운 건 FIFO로 한 장이라, 실리는 UID도 그 한 장 것이다.
    assert ev.tag_id == "20250000", ev.tag_id


# 9. 쿨다운 — 같은 게이트·같은 종류 미태깅이 연달아 와도 경고는 첫 건만
async def test_untagged_cooldown_suppresses_repeat(client, auth_headers):
    async with get_session() as s:
        ev1 = await evaluate_pass(
            s, event_id="P1", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            beam_a_ts=BASE + dt.timedelta(seconds=1), beam_b_ts=None,
            observed_at=BASE + dt.timedelta(seconds=1),
        )
    async with get_session() as s:
        ev2 = await evaluate_pass(
            s, event_id="P2", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            beam_a_ts=BASE + dt.timedelta(seconds=1.5), beam_b_ts=None,
            observed_at=BASE + dt.timedelta(seconds=1.5),
        )
    assert ev1.verdict == Verdict.UNTAGGED and ev1.notify is True and ev1.alert_id is not None
    # 두 번째는 **알림만** 삼켜진다. 2026-07-30 수리 전엔 Alert 행 자체가 안 생겨서 사건이
    # 통째로 사라졌다(실서버 미태깅 297건 중 279건). 지금은 기록은 남고 울림만 빠진다.
    assert ev2.verdict == Verdict.UNTAGGED and ev2.notify is False
    assert ev2.alert_id is not None and ev2.alert_id != ev1.alert_id
    # 미태깅이라 둘 다 UID가 없다. 쿨다운은 알림만 건드리고 신원 칸엔 영향이 없다(-241).
    assert ev1.tag_id is None and ev2.tag_id is None
    # 판정도 경고 행도 둘 다 남는다. 차이는 외부 알림 판정뿐이다.
    assert await _count(GatePassEvent) == 2
    assert await _count(Alert) == 2


# 10. 만료 크레딧은 한참 뒤 무관한 퇴장에 회수되지 않는다
async def test_expired_credit_not_recovered_by_later_exit(client, auth_headers, exit_recovery_on):
    await client.post("/api/tagging-events", json=await _tagging("T1", "20250001", 0), headers=auth_headers)
    # TTL 3.0초를 한참 넘긴 뒤(60초) 이 태깅과 무관한 퇴장이 온다.
    r = await client.post(
        "/api/gate-pass-events", json=await _pass("P1", "B_TO_A", "complete", 60.0), headers=auth_headers
    )
    assert r.json()["verdict"] == Verdict.EXIT

    tag = await _get_tagging("T1")
    assert tag.credit_state == CreditState.EXPIRED   # 회수가 아니라 만료로 닫힌다
    assert tag.consumed_by_pass_id is None           # 엉뚱한 통과를 가리키지 않는다
    assert tag.consumed_at is None


# 11. 만료분과 유효분이 섞여 있으면 유효분만 회수한다
#     (5번과 같은 이유로 회수 스위치를 켜고 돈다 — 5번 docstring 참고)
async def test_exit_recovers_valid_credit_not_expired_one(
    client, auth_headers, exit_recovery_on
):
    live = _live()
    # T1은 60초 전 태깅이라 이미 만료, T2는 방금 찍어 살아 있다.
    await client.post(
        "/api/tagging-events",
        json=await _tagging("T1", "20250001", -60.0, base=live),
        headers=auth_headers,
    )
    await client.post(
        "/api/tagging-events",
        json=await _tagging("T2", "20250002", 0.0, base=live),
        headers=auth_headers,
    )
    r = await client.post(
        "/api/gate-pass-events",
        json=await _pass("P1", "B_TO_A", "complete", 0.5, base=live),
        headers=auth_headers,
    )
    assert r.json()["verdict"] == Verdict.EXIT

    t1 = await _get_tagging("T1")
    t2 = await _get_tagging("T2")
    assert t1.credit_state == CreditState.EXPIRED and t1.consumed_by_pass_id is None
    assert t2.credit_state == CreditState.RECOVERED and t2.consumed_by_pass_id is not None


# 12. 기기 시계가 미래로 튀어도 유효 크레딧이 전량 만료되지 않는다(회수도 안 된다)
async def test_future_device_clock_does_not_wipe_valid_credits(client, auth_headers, exit_recovery_on):
    """청소 기준이 기기가 보낸 빔 시각이면, 시계가 한 번 튀는 순간 그 게이트가 통째로 비워진다.

    라파이 시계가 1시간 앞으로 튄 퇴장 한 건으로 방금 찍은 크레딧 두 장이 다 만료되면,
    뒤이어 정상으로 지나가는 사람들이 전부 미태깅으로 잡힌다. 청소는 이른 쪽 시계로 한다.

    회수도 한 장도 안 잡는다. 회수는 늦은 쪽 시계(=1시간 뒤)로 견주는데 그 시각에서 보면
    크레딧 둘이 다 만료라, 시계를 못 믿는 퇴장은 아무것도 못 가져간다. 대가는 "시계 튄 퇴장이
    회수를 못 한다"뿐이고(그 크레딧은 제 TTL대로 다음 청소에 닫힌다), 얻는 건 만료분이 엉뚱한
    퇴장에 회수돼 회수 기록이 딴 통과를 가리키는 걸 막는 것이다.
    """
    live = _live()
    for i in (1, 2):
        await client.post(
            "/api/tagging-events",
            json=await _tagging(f"T{i}", f"2025000{i}", 0.0, base=live),
            headers=auth_headers,
        )
    # 퇴장 한 건이 1시간 미래 빔 시각을 싣고 온다(시계 점프).
    r = await client.post(
        "/api/gate-pass-events",
        json=await _pass("P1", "B_TO_A", "complete", 3600.0, base=live),
        headers=auth_headers,
    )
    assert r.json()["verdict"] == Verdict.EXIT

    t1 = await _get_tagging("T1")
    t2 = await _get_tagging("T2")
    states = sorted([t1.credit_state, t2.credit_state])
    # 두 장이 다 유효(issued)로 살아 있다 — 만료도 회수도 안 났다.
    assert states == [CreditState.ISSUED, CreditState.ISSUED], states
    assert t1.consumed_by_pass_id is None and t2.consumed_by_pass_id is None
    async with get_session() as s:
        expired = (
            await s.execute(
                select(func.count())
                .select_from(TaggingEvent)
                .where(TaggingEvent.credit_state == CreditState.EXPIRED)
            )
        ).scalar_one()
    assert expired == 0


# 13. 입장 경로 청소 — 만료분은 닫지만 유효 통과 판정은 안 뒤집는다
async def test_entry_pass_closes_expired_credit_without_breaking_verdict(client, auth_headers):
    """입장(A→B)에도 퇴장과 같은 청소를 밟는다. 순서는 소비 판정 뒤다.

    청소가 소비보다 먼저 돌면, 이벤트 시각으로는 유효한 크레딧이 서버 시각 기준으로 닫혀
    정상 통과가 미태깅으로 뒤집힌다. 그래서 판정 먼저, 청소 나중이다.
    """
    live = _live()
    # 옛 만료분(고정 과거 시각) + 방금 찍은 유효분이 같은 게이트에 섞여 있다.
    await client.post(
        "/api/tagging-events", json=await _tagging("TOLD", "20250001", 0.0), headers=auth_headers
    )
    await client.post(
        "/api/tagging-events",
        json=await _tagging("TNEW", "20250002", 0.0, base=live),
        headers=auth_headers,
    )
    r = await client.post(
        "/api/gate-pass-events",
        json=await _pass("P1", "A_TO_B", "complete", 1.0, base=live),
        headers=auth_headers,
    )
    assert r.json()["verdict"] == Verdict.NORMAL, r.text

    old = await _get_tagging("TOLD")
    new = await _get_tagging("TNEW")
    assert new.credit_state == CreditState.CONSUMED
    # 옛 만료분이 issued로 남지 않는다(예전엔 퇴장이 와야 닫혔다).
    assert old.credit_state == CreditState.EXPIRED
    assert old.consumed_by_pass_id is None


# 14. 지연 인입 — 뒤늦게 밀려온 통과가 남의 유효 크레딧을 닫지 않는다
async def test_delayed_backlog_ingest_keeps_credits_valid(client, auth_headers):
    """망 끊김 뒤 백로그·배속 재생처럼 인입이 늦어도 정상 통과 3건이 다 normal이어야 한다.

    청소 기준이 서버 수신 시각 하나면, 30초 전 사건이 지금 들어오는 순간 첫 통과가 청소를
    밟아 남은 크레딧 두 장이 만료로 닫힌다. 뒤 통과 둘이 미태깅으로 뒤집히고 경고까지 난다.
    이벤트 시각으로 보면 셋 다 TTL 안이라 청소는 아무것도 닫지 말아야 한다.
    """
    late = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
    for i in range(3):
        r = await client.post(
            "/api/tagging-events",
            json=await _tagging(f"T{i}", f"2025000{i}", i * 0.01, base=late),
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text

    verdicts = []
    for i in range(3):
        r = await client.post(
            "/api/gate-pass-events",
            json=await _pass(f"P{i}", "A_TO_B", "complete", 1.0 + i * 0.3, base=late),
            headers=auth_headers,
        )
        verdicts.append(r.json()["verdict"])

    assert verdicts == [Verdict.NORMAL] * 3, verdicts
    states = [(await _get_tagging(f"T{i}")).credit_state for i in range(3)]
    assert states == [CreditState.CONSUMED] * 3, states
    # 셋이 찍은 순서대로 지나가면 UID도 그 순서로 붙는다(-241). 소비가 FIFO라 나오는 결과다.
    async with get_session() as s:
        rows = (await s.execute(select(GatePassEvent).order_by(GatePassEvent.id))).scalars().all()
    assert [r.tag_id for r in rows] == ["20250000", "20250001", "20250002"], [
        (r.event_id, r.tag_id) for r in rows
    ]
    # 미태깅 경고가 한 건도 없다(뒤집힘이 나면 쿨다운 때문에 1건만 남는다 — 0건이어야 한다).
    assert await _count(Alert) == 0


# 15. 기기 시계가 과거로 튄 퇴장은 벽시계로 만료된 크레딧을 회수하지 않는다
async def test_past_device_clock_exit_does_not_recover_expired_credit(client, auth_headers, exit_recovery_on):
    """청소를 이른 쪽 시계로 옮기면서 열린 구멍을 회수 쪽 가드가 막는지 본다.

    RTC 없는 라파이가 부팅 직후 1970년대·어제 시각을 싣고 오는 자리다. 청소는 이른 쪽
    기준이라 그 퇴장에서 아무것도 안 닫는데, 회수가 만료를 안 따지면 벽시계로 이미 만료된
    크레딧을 집어 간다 — 회수 기록이 52초 전 태깅을 1시간 전 퇴장에 묶는다(F4 원건).

    회수는 늦은 쪽 시계로 견줘서 그 크레딧을 안 건드리고, 닫는 일은 청소 몫으로 남는다.
    시계가 제대로 붙은 다음 사건에서 expired로 닫힌다.
    """
    live = _live()
    # 52초 전 태깅 = TTL 3초를 벽시계로 이미 넘겼다.
    await client.post(
        "/api/tagging-events",
        json=await _tagging("T1", "20250001", -52.0, base=live),
        headers=auth_headers,
    )
    # 퇴장 한 건이 1시간 과거 빔 시각을 싣고 온다(시계가 과거로 튐).
    r = await client.post(
        "/api/gate-pass-events",
        json=await _pass("P1", "B_TO_A", "complete", -3600.0, base=live),
        headers=auth_headers,
    )
    assert r.json()["verdict"] == Verdict.EXIT

    tag = await _get_tagging("T1")
    assert tag.credit_state != CreditState.RECOVERED, tag.credit_state
    assert tag.consumed_by_pass_id is None   # 엉뚱한 퇴장을 가리키지 않는다
    assert tag.consumed_at is None

    # 시계가 정상인 사건이 오면 청소가 닫는다(회수가 아니라 만료로).
    r2 = await client.post(
        "/api/gate-pass-events",
        json=await _pass("P2", "B_TO_A", "complete", 0.5, base=live),
        headers=auth_headers,
    )
    assert r2.json()["verdict"] == Verdict.EXIT
    tag = await _get_tagging("T1")
    assert tag.credit_state == CreditState.EXPIRED
    assert tag.consumed_by_pass_id is None
    assert await _count(Alert) == 0  # 퇴장 둘 다 경고 대상이 아니다
