"""판정 로직 네 갈래 굳히기 시험 (코덱스 확정 C1·C13·C15·C16·C17 + 조건부 K1·K2).

수리 전 코드에서 여기 케이스가 실제로 실패하는 걸 먼저 확인해서 반증을 잡았다
(worklog/2026-07-30_수리_A-판정로직.md ① 참고). 갈래는 넷이다.

1. 쿨다운이 Alert 행까지 삼키던 자리 — 사건은 언제나 남고, 쿨다운은 외부 알림만 억제한다.
2. 시각 가드 — 미래 태깅이 과거 통과를 정상화하지 못하고, 늦게 온 옛 태깅이 최신 크레딧을
   만료시키지 못한다.
3. 퇴장 회수 — 남의 크레딧을 먹어 정상 입장을 미태깅으로 뒤집던 자리.
4. 멱등 부수효과 — 중복 요청이 원래 판정을 돌려주고, 본문이 다르면 409로 거절한다.
"""
import datetime as dt
import logging

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.credit.state_machine import (
    CreditState,
    Verdict,
    evaluate_pass,
    issue_credit,
)
from app.db import get_session
from app.models import Alert, GatePassEvent, TaggingEvent
from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 30, 3, 0, 0, tzinfo=dt.timezone.utc)


def _at(offset_sec: float) -> dt.datetime:
    return BASE + dt.timedelta(seconds=offset_sec)


def _iso(offset_sec: float) -> str:
    return _at(offset_sec).isoformat()


def _now(offset_sec: float = 0.0) -> dt.datetime:
    """서버 현재 시각 기준. 회수·청소가 벽시계를 같이 보므로 그쪽 갈래는 이 축을 쓴다."""
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_sec)


async def _tag(event_id: str, tag_id: str, at: dt.datetime, gate_no: int = 1):
    async with get_session() as s:
        return await issue_credit(
            s, event_id=event_id, device_id="raspberry01", gate_no=gate_no,
            tag_id=tag_id, observed_at=at,
        )


async def _pass(
    event_id: str,
    at: dt.datetime,
    gate_no: int = 1,
    direction: str = "A_TO_B",
    status: str = "complete",
):
    # 빔 B 시각은 빔 A보다 0.2초 뒤다. 예전엔 `beam_b_ts=None`이었는데, 그 모양(빔 A만 있는
    # complete)은 라파가 못 만드는 자료다 — `emit`은 complete면 두 칸을 다 채우고
    # `emit_tagged_entry`는 빔 B만 채운다. 서버 정합 검사(S15P11C207-243)가 그걸 어긋난
    # 주장으로 보게 되면서 fixture를 실기기 모양으로 맞췄다. 판정 창은 그대로 beam_a_ts라
    # 이 시험들이 재던 축(만료·쿨다운·회수)은 한 글자도 안 바뀐다.
    beam_b = at + dt.timedelta(seconds=0.2) if status == "complete" else None
    async with get_session() as s:
        return await evaluate_pass(
            s, event_id=event_id, device_id="raspberry01", gate_no=gate_no,
            direction=direction, status=status,
            beam_a_ts=at, beam_b_ts=beam_b, observed_at=at,
        )


async def _count(model) -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _credit_state(event_id: str) -> str:
    async with get_session() as s:
        return (
            await s.execute(
                select(TaggingEvent.credit_state).where(TaggingEvent.event_id == event_id)
            )
        ).scalar_one()


@pytest.fixture
def sent(monkeypatch):
    """manager.broadcast를 가로채 메시지를 모은다(실 소켓을 붙이면 루프가 갈린다)."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


# ── ① 쿨다운은 알림만 억제하고 사건은 남긴다 (C1) ──────────────────────────

async def test_쿨다운_창_안_미태깅도_알림_행이_남는다():
    """반증 자리 — 수리 전엔 5건 중 Alert가 1건만 생겼다(검증 R1 재현)."""
    results = [await _pass(f"CD{i}", _at(i * 0.2)) for i in range(5)]

    assert [r.verdict for r in results] == [Verdict.UNTAGGED] * 5
    assert all(r.alert_id is not None for r in results), "쿨다운이 Alert 행을 안 만들었다"
    assert len({r.alert_id for r in results}) == 5, "다섯 사건이 Alert를 공유했다"
    assert await _count(Alert) == 5


async def test_쿨다운은_외부_알림만_억제한다():
    first = await _pass("CDN0", _at(0))
    second = await _pass("CDN1", _at(0.2))

    assert first.notify_dashboard is True and first.notify_chat is True
    assert second.notify_dashboard is False and second.notify_chat is False
    # 억제됐어도 사건 자체는 남아 신원 입력 대상이 된다.
    assert second.alert_id is not None


async def test_억제된_사건도_신원_입력_대상이_된다(client, auth_headers):
    """진짜 미태깅자가 가짜 폭주에 묻혀도 요원이 신원을 적을 대상이 있어야 한다(검증 R5)."""
    await _pass("ID-fake", _at(0))
    real = await _pass("ID-real", _at(3.0))

    r = await client.post(
        f"/api/alerts/{real.alert_id}/identify",
        json={"person": "가명-검증"},
        headers=auth_headers,
    )
    assert r.status_code == 200, "억제된 사건에 신원을 붙일 Alert 행이 없다"


async def test_팝업과_매터모스트_판정이_갈라져_있다():
    """ingest.py가 한 불리언으로 둘을 같이 껐던 자리. 판정을 따로 들고 있어야 한다."""
    ev = await _pass("SPLIT0", _at(0))
    assert hasattr(ev, "notify_dashboard") and hasattr(ev, "notify_chat")
    # 편의 property는 "하나라도 나가나"다.
    assert ev.notify is (ev.notify_dashboard or ev.notify_chat)


async def test_억제된_미태깅은_팝업_메시지를_안_보낸다(client, auth_headers, sent):
    body = {"device_id": "raspberry01", "gate_no": 1, "direction": "A_TO_B",
            "status": "complete", "beam_b_ts": None}
    for i, off in enumerate((0.0, 0.2)):
        await client.post(
            "/api/gate-pass-events",
            json={**body, "event_id": f"WS-CD{i}", "beam_a_ts": _iso(off),
                  "observed_at": _iso(off)},
            headers=auth_headers,
        )
    types = [m["type"] for m in sent]
    assert types == ["gate_pass_event", "untagged_alert", "gate_pass_event"]
    # 화면은 안 도배됐는데 DB엔 두 건 다 남았다.
    assert await _count(Alert) == 2


# ── ② 시각 가드 (K1·K2) ────────────────────────────────────────────────────

async def test_미래_태깅은_과거_통과를_정상화하지_못한다():
    """반증 자리 — 수리 전엔 1시간 미래 태깅이 과거 통과를 normal로 만들었다(P02A)."""
    await _tag("FUT-T", "UID-FUT", _at(3600))
    ev = await _pass("FUT-P", _at(0))

    assert ev.verdict == Verdict.UNTAGGED, "미래 태깅이 과거 통과를 정상화했다"
    assert ev.matched_tagging_event_id is None
    assert await _credit_state("FUT-T") == CreditState.ISSUED


async def test_스큐가_TTL을_늘리지_못한다():
    """태깅 +60초, 통과 +50초. TTL이 3초인데 수리 전엔 normal이 나왔다(P02A2)."""
    await _tag("SKW-T", "UID-SKW", _at(60))
    ev = await _pass("SKW-P", _at(50))
    assert ev.verdict == Verdict.UNTAGGED, "TTL이 시계 스큐만큼 늘어났다"


async def test_정상_순서는_그대로_소비된다():
    """가드가 정상 경로를 안 깨는지 — 대조군."""
    await _tag("OK-T", "UID-OK", _at(0))
    ev = await _pass("OK-P", _at(1.0))
    assert ev.verdict == Verdict.NORMAL and ev.matched_tagging_event_id is not None


async def test_태깅과_통과가_같은_시각이면_소비된다():
    """경계 — issued_at <= pass_time 이라 같은 시각은 소비 대상이다."""
    await _tag("EQ-T", "UID-EQ", _at(0))
    ev = await _pass("EQ-P", _at(0))
    assert ev.verdict == Verdict.NORMAL


async def test_TTL_경계는_그대로다():
    """expires_at > compare_ts 라 TTL 정확히 지난 통과는 미태깅이다(기존 계약 유지)."""
    ttl = get_settings().credit_ttl_sec
    await _tag("TTL-T", "UID-TTL", _at(0))
    ev = await _pass("TTL-P", _at(ttl))
    assert ev.verdict == Verdict.UNTAGGED


async def test_늦게_온_옛_태깅이_최신_크레딧을_안_만료시킨다():
    """반증 자리 — 수리 전엔 새 크레딧이 expired가 되고 정상 태깅자가 경고를 맞았다(P02B)."""
    await _tag("OOO-NEW", "UID-OOO", _at(10))
    await _tag("OOO-OLD", "UID-OOO", _at(0))  # 10초 전 사건이 나중에 도착

    assert await _credit_state("OOO-NEW") == CreditState.ISSUED, "최신 크레딧이 만료됐다"
    # 같은 카드가 두 장을 쌓지 못한다는 규칙은 유지 — 늦게 온 옛 사건이 자기를 접는다.
    assert await _credit_state("OOO-OLD") == CreditState.EXPIRED

    ev = await _pass("OOO-P", _at(11))
    assert ev.verdict == Verdict.NORMAL, "정상 태깅자가 미태깅으로 찍혔다"


async def test_재태깅은_이전_크레딧을_그대로_덮는다():
    """정상 순서 재태깅은 기존 계약 그대로 — 대조군."""
    await _tag("RT-1", "UID-RT", _at(0))
    await _tag("RT-2", "UID-RT", _at(1))
    assert await _credit_state("RT-1") == CreditState.EXPIRED
    assert await _credit_state("RT-2") == CreditState.ISSUED


@pytest.fixture
def credit_logger_alive():
    """`c207.credit` 로거가 살아 있게 되돌린다.

    ⚠ `alembic/env.py:16`의 `fileConfig(config.config_file_name)`이 기본값
    `disable_existing_loggers=True`로 돌아서, `test_migrations_roundtrip.py`가 한 번 돌면
    그 뒤로 **모든 `c207.*` 로거가 `disabled=True`가 된다.** 순서에 따라 로그 검사가 조용히
    뜻을 잃는 자리다(같은 함정이 `test_alert_identify.py`의 신원 로그 검사에도 걸린다).
    고칠 자리는 env.py 한 줄인데 우리 조 소유가 아니라 보고에 올렸다 — 그때까지 이 픽스처가
    케이스를 순서에 안 흔들리게 잡아 준다.
    """
    lg = logging.getLogger("c207.credit")
    was = lg.disabled
    lg.disabled = False
    yield lg
    lg.disabled = was


async def test_미래_스큐가_한도를_넘으면_경고_로그가_남는다(caplog, credit_logger_alive):
    """거절도 절대 하지 않는다 — 라파 sender_worker가 4xx를 무한 재시도해서 큐가 막힌다."""
    with caplog.at_level(logging.WARNING, logger="c207.credit"):
        result = await _tag("FSK-T", "UID-FSK", _now(600))
    assert result.stored is True, "미래 스큐를 422로 막으면 기기 큐가 통째로 막힌다"
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "c207.credit")
    assert ours, "로그가 아예 안 잡혔다(검사가 헛돌고 있다)"
    assert "스큐" in ours


async def test_스큐가_한도_안이면_경고가_안_남는다(caplog, credit_logger_alive):
    """대조군 — 실기기 스큐(-11.9~-18.9ms)로는 절대 안 울려야 한다."""
    with caplog.at_level(logging.WARNING, logger="c207.credit"):
        await _tag("FSK-OK", "UID-FSK-OK", _now(-0.02))
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "c207.credit")
    assert "스큐" not in ours


# ── ③ 퇴장 회수 (C13) ─────────────────────────────────────────────────────

async def test_제3자_퇴장이_남의_크레딧을_안_먹는다():
    """반증 자리 — 수리 전엔 A 크레딧이 recovered로 회수돼 A 입장이 untagged가 됐다(S2)."""
    await _tag("EX-T", "UID-EX", _now(0))
    exit_ev = await _pass("EX-X", _now(0.3), direction="B_TO_A")
    entry = await _pass("EX-A", _now(0.6))

    assert exit_ev.verdict == Verdict.EXIT
    assert await _credit_state("EX-T") == CreditState.CONSUMED, "퇴장이 남의 크레딧을 회수했다"
    assert entry.verdict == Verdict.NORMAL, "정상 입장이 미태깅으로 뒤집혔다"
    assert await _count(Alert) == 0


async def test_회수를_되살리면_예전_거동이_돌아온다(monkeypatch):
    """정본 468행을 되살릴 팀 결정이 나면 상수 하나로 돌아가는지 — 되돌림 경로를 지킨다."""
    from app.credit import state_machine

    monkeypatch.setattr(state_machine, "EXIT_RECOVERY_ENABLED", True)
    await _tag("RV-T", "UID-RV", _now(0))
    await _pass("RV-X", _now(0.3), direction="B_TO_A")
    assert await _credit_state("RV-T") == CreditState.RECOVERED


async def test_퇴장은_여전히_기록으로_남는다():
    """회수를 끊어도 퇴장 판정 자체는 정본 계약대로 남는다."""
    ev = await _pass("EX-ONLY", _now(0), direction="B_TO_A")
    assert ev.verdict == Verdict.EXIT and ev.alert_id is None
    async with get_session() as s:
        stored = (
            await s.execute(
                select(GatePassEvent.verdict).where(GatePassEvent.event_id == "EX-ONLY")
            )
        ).scalar_one()
    assert stored == Verdict.EXIT


# ── ④ 멱등 부수효과 (C16·C17·C15) ─────────────────────────────────────────

async def test_중복_통과는_원래_판정을_돌려준다(client, auth_headers):
    """반증 자리 — 수리 전 2차 응답은 verdict=null이었다(P04A)."""
    # 빔 두 칸을 실기기 모양(a < b)으로 채운다 — 빔 A만 있는 complete는 라파가 못 만드는
    # 자료라 서버 정합 검사(S15P11C207-243)가 어긋난 주장으로 본다. `_pass` 헬퍼 주석 참고.
    body = {"event_id": "DUP-1", "device_id": "raspberry01", "gate_no": 1,
            "direction": "A_TO_B", "status": "complete",
            "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)}
    first = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    second = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)

    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["verdict"] == "untagged"
    assert second.json() == {
        "event_id": "DUP-1", "stored": False, "duplicate": True, "verdict": "untagged",
    }, "중복 응답이 원래 판정을 안 돌려줬다"


async def test_중복_통과가_크레딧을_두_번_먹지_않는다(client, auth_headers):
    """멱등 원래 계약은 유지 — 대조군."""
    await _tag("DUP-T", "UID-DUP", _at(0))
    body = {"event_id": "DUP-2", "device_id": "raspberry01", "gate_no": 1,
            "direction": "A_TO_B", "status": "complete",
            "beam_a_ts": _iso(1), "beam_b_ts": _iso(1.2), "observed_at": _iso(1)}
    r1 = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    r2 = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert r1.json()["verdict"] == "normal" and r2.json()["verdict"] == "normal"
    assert await _count(GatePassEvent) == 1
    assert await _credit_state("DUP-T") == CreditState.CONSUMED


async def test_중복_통과는_알림을_다시_안_보낸다(client, auth_headers, sent):
    body = {"event_id": "DUP-3", "device_id": "raspberry01", "gate_no": 1,
            "direction": "A_TO_B", "status": "complete",
            "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)}
    await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert [m["type"] for m in sent] == ["gate_pass_event", "untagged_alert"]
    assert await _count(Alert) == 1


async def test_같은_event_id_다른_본문은_409(client, auth_headers):
    """반증 자리 — 수리 전엔 200 duplicate로 조용히 버렸다(P04B)."""
    first = {"event_id": "CFL-1", "device_id": "raspberry01", "gate_no": 1,
             "direction": "A_TO_B", "status": "complete",
             "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)}
    await client.post("/api/gate-pass-events", json=first, headers=auth_headers)

    conflict = {**first, "gate_no": 9, "direction": "B_TO_A"}
    r = await client.post("/api/gate-pass-events", json=conflict, headers=auth_headers)
    assert r.status_code == 409, "다른 본문을 조용히 버렸다"
    detail = r.json()["detail"]
    assert "gate_no" in str(detail) and "direction" in str(detail)

    # 저장된 행은 1차 본문 그대로다(2차가 덮지 않는다).
    async with get_session() as s:
        row = (
            await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == "CFL-1"))
        ).scalars().one()
    assert row.gate_no == 1 and row.direction == "A_TO_B"


async def test_셔틀도_다른_본문이면_409(client, auth_headers):
    body = {"event_id": "CFS-1", "gate_no": 2, "shuttle_no": "SH-07", "signal_ts": _iso(0)}
    r1 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert r1.status_code == 200
    r2 = await client.post(
        "/api/shuttle-arrivals", json={**body, "shuttle_no": "SH-99"}, headers=auth_headers
    )
    assert r2.status_code == 409
    assert "shuttle_no" in str(r2.json()["detail"])


async def test_셔틀_중복은_본문이_같으면_200(client, auth_headers):
    body = {"event_id": "CFS-2", "gate_no": 2, "shuttle_no": "SH-07", "signal_ts": _iso(0)}
    await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    r = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert r.status_code == 200 and r.json()["duplicate"] is True


async def test_셔틀_통지_실패는_재전송으로_복구된다(
    client, auth_headers, monkeypatch, seed_robot, no_hold
):
    """반증 자리 — 수리 전엔 재전송해도 명령이 다시 안 나갔다(P04C).

    ⚠ `no_hold`가 붙은 이유(2026-07-31). 재전송 복구는 "명령이 이미 나갔어야 하는데
    유실됐다"는 갈래다. 5초 대기 창이 아직 도는 중이면 명령은 유실된 게 아니라 아직 안
    나간 것이라, 지금 서버는 그때 재전송을 안 태우고 창 마감을 기다린다. 창을 끄고
    즉시 발사시킨 뒤에야 진짜 복구 갈래를 볼 수 있다.
    """
    from app.ws import robot_manager

    # 통지가 실제로 나간 사실은 ShuttleArrival.notified_robot_id에 남는데, 그 값을 채우려면
    # 같은 이름의 Robot 행이 있어야 한다(robot_channel.notify_shuttle_arrival).
    await seed_robot(name="jetson01")
    delivered: list[dict] = []

    async def _dead(command, robot_id=None):
        return []

    async def _alive(command, robot_id=None):
        delivered.append(command)
        return ["jetson01"]

    body = {"event_id": "SHF-1", "gate_no": 3, "shuttle_no": "SH-01", "signal_ts": _iso(0)}

    monkeypatch.setattr(robot_manager, "send_command", _dead)
    r1 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert r1.status_code == 200
    assert r1.headers.get("X-Shuttle-Notified-Robots") == "0", "통지 실패가 응답에 안 드러난다"

    monkeypatch.setattr(robot_manager, "send_command", _alive)
    r2 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert r2.status_code == 200 and r2.json()["duplicate"] is True
    assert len(delivered) == 1, "같은 요청 재전송이 명령을 다시 안 냈다"
    assert r2.headers.get("X-Shuttle-Notified-Robots") == "1"


async def test_이미_통지된_셔틀은_재전송에서_두_번_안_나간다(
    client, auth_headers, monkeypatch, seed_robot, no_hold
):
    """재전송 복구가 로봇을 두 번 출동시키면 안 된다 — 대조군.

    `no_hold`는 위 반증 시험과 같은 이유다(창이 도는 중이면 재전송을 안 태운다).
    """
    from app.ws import robot_manager

    await seed_robot(name="jetson01")
    calls: list[dict] = []

    async def _alive(command, robot_id=None):
        calls.append(command)
        return ["jetson01"]

    monkeypatch.setattr(robot_manager, "send_command", _alive)
    body = {"event_id": "SHF-2", "gate_no": 3, "shuttle_no": "SH-02", "signal_ts": _iso(0)}
    await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert len(calls) == 1, "이미 통지된 신호가 재전송으로 또 나갔다"
