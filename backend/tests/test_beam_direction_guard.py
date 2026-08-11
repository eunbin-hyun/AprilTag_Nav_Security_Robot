"""방향 주장 대 빔 시각 정합 시험 (지라 S15P11C207-243).

기기가 보낸 `direction`은 **주장**이고, 같은 이벤트에 실려 온 빔 시각이 그 주장의 실물이다.
라파는 빔 두 개의 선후로 방향을 만드는데(`hardware/rpi/gate_server.py` — `A_TO_B if
beam_a_ts < beam_b_ts else B_TO_A`), 수리 전 서버는 그 주장만 보고 A_TO_B면 크레딧을 태웠다.

수리 전 실측 반증(이 워크트리, 2026-07-31 — 아래 두 케이스가 실제로 이 값을 냈다).
- `A_TO_B` + 빔 시각 없음   → verdict=normal, 크레딧 consumed  (기대: beam_incomplete·issued 유지)
- `A_TO_B` + 빔 역순(a>b)   → verdict=normal, 크레딧 consumed  (기대: beam_incomplete·issued 유지)

정합 검사는 **이벤트 한 건 안에서만** 본다. 벽시계와 견주는 축은 안 늘린다 — 만료 청소·퇴장
회수가 쓰는 시계 두 축 체계(S15P11C207-242)를 건드리면 그 수리가 흔들린다.

⚠ 판정을 응답 코드로 안 한다. `GatePassEventIn`이 pydantic 기본 `extra=ignore`라 칸 이름을
틀려도 200이 나온다. 그래서 저장된 행(verdict·matched_tagging_event_id)과 크레딧 상태를
DB에서 직접 읽어 대조한다.
"""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.credit.state_machine import (
    BEAM_CONFLICT_B_MISSING,
    BEAM_CONFLICT_NO_TS,
    BEAM_CONFLICT_ORDER,
    BEAM_CONFLICT_SAME_TS,
    CreditState,
    Verdict,
    evaluate_pass,
)
from app.db import get_session
from app.models import Alert, GatePassEvent, TaggingEvent
from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 판정 창은 서버 현재와 무관한 lazy expiry라 고정 시각으로 재현한다. 만료 청소가 쓰는
# 이른 쪽(min) 기준으로도 TTL 3초 안이라 유효 크레딧이 안 닫힌다(test_credit_state_machine과 같은 축).
BASE = dt.datetime(2026, 7, 31, 3, 0, 0, tzinfo=dt.timezone.utc)


def _at(offset_sec: float) -> dt.datetime:
    return BASE + dt.timedelta(seconds=offset_sec)


def _iso(offset_sec: float) -> str:
    return _at(offset_sec).isoformat()


async def _tag(client, headers, event_id: str, *, gate_no: int = 1,
               tag_id: str = "20250001", offset: float = 0.0) -> None:
    r = await client.post(
        "/api/tagging-events",
        json={
            "event_id": event_id,
            "device_id": "raspberry01",
            "gate_no": gate_no,
            "tag_id": tag_id,
            "observed_at": _iso(offset),
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text


async def _post_pass(
    client,
    headers,
    event_id: str,
    *,
    direction: str | None,
    status: str | None = "complete",
    beam_a: float | None = None,
    beam_b: float | None = None,
    observed: float = 1.0,
    gate_no: int = 1,
    result: str | None = None,
):
    body = {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": gate_no,
        "direction": direction,
        "status": status,
        "beam_a_ts": _iso(beam_a) if beam_a is not None else None,
        "beam_b_ts": _iso(beam_b) if beam_b is not None else None,
        "observed_at": _iso(observed),
        "result": result,
    }
    return await client.post("/api/gate-pass-events", json=body, headers=headers)


async def _pass_row(event_id: str) -> GatePassEvent:
    async with get_session() as s:
        return (
            await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == event_id))
        ).scalar_one()


async def _credit_state(event_id: str) -> str:
    async with get_session() as s:
        return (
            await s.execute(
                select(TaggingEvent.credit_state).where(TaggingEvent.event_id == event_id)
            )
        ).scalar_one()


async def _alert_count() -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(Alert))).scalar_one()


# ── ① 반증 자리 — 수리 전엔 여기서 크레딧이 탔다 ────────────────────────────

async def test_빔_시각이_하나도_없는_A_TO_B는_크레딧을_안_태운다(client, auth_headers):
    """수리 전 실측 — verdict=normal, 크레딧 consumed. 주장만 있고 실물이 없는데 정상이었다.

    크레딧이 **있는** 자리라 판정은 보수(`beam_incomplete`)다. 크레딧이 없으면 미태깅으로
    남는다(아래 ①-2 — 1차 수리가 여기서 경보를 통째로 없앤 자리다).
    """
    await _tag(client, auth_headers, "T-NOTS")
    r = await _post_pass(
        client, auth_headers, "P-NOTS",
        direction="A_TO_B", beam_a=None, beam_b=None, observed=1.0,
    )
    assert r.status_code == 200, r.text

    row = await _pass_row("P-NOTS")
    assert row.verdict == Verdict.BEAM_INCOMPLETE
    assert row.matched_tagging_event_id is None
    # 크레딧을 안 태웠으니 UID도 비어야 한다. 지금은 소비 자리에서만 UID가 차서 자동으로
    # 만족하는데, 이 갈래에 나중에 소비가 붙으면 조용히 남의 이름이 실릴 수 있다(S15P11C207-241).
    assert row.tag_id is None
    assert await _credit_state("T-NOTS") == CreditState.ISSUED
    # 보수 판정이라 미태깅 경고도 아니다(사건 기록이 부풀면 안 된다).
    assert await _alert_count() == 0


async def test_빔_순서가_주장과_거꾸로면_크레딧을_안_태운다(client, auth_headers):
    """빔 A가 빔 B보다 늦게 끊겼는데 A_TO_B를 주장한 이벤트. 수리 전엔 normal이었다."""
    await _tag(client, auth_headers, "T-REV")
    r = await _post_pass(
        client, auth_headers, "P-REV",
        direction="A_TO_B", beam_a=1.4, beam_b=1.0, observed=1.4,
    )
    assert r.status_code == 200, r.text

    row = await _pass_row("P-REV")
    assert row.verdict == Verdict.BEAM_INCOMPLETE
    assert row.matched_tagging_event_id is None
    assert await _credit_state("T-REV") == CreditState.ISSUED
    assert await _alert_count() == 0


async def test_빔_두_개가_같은_눈금이면_크레딧을_안_태운다(client, auth_headers):
    """a == b도 어긋남이다 — 라파 판별식이면 그 값은 **B_TO_A로 적힌다**.

    1차 수리는 "굵은 시계가 접은 모양"이라며 통과시켰는데, 그 근거였던 자료는 실기기가 아니라
    `tools/dummy_publisher.py`가 빔 두 칸에 `now_iso()`를 각각 불러 만든 **이동 시간 0초짜리
    통과**였다. 시늉 도구 쪽을 고쳤다(`BEAM_TRANSIT_SEC`). 열어 두면 빔 두 칸에 같은 값만
    넣어도 남의 크레딧이 타는 우회로가 된다(수리 1차 검증 P1).
    """
    await _tag(client, auth_headers, "T-EQ")
    r = await _post_pass(
        client, auth_headers, "P-EQ",
        direction="A_TO_B", beam_a=1.0, beam_b=1.0, observed=1.0,
    )
    assert r.json()["beam_conflict"] == BEAM_CONFLICT_SAME_TS
    row = await _pass_row("P-EQ")
    assert row.verdict == Verdict.BEAM_INCOMPLETE
    assert await _credit_state("T-EQ") == CreditState.ISSUED


async def test_빔_A만_있는_complete는_크레딧을_안_태운다(client, auth_headers):
    """라파는 이 모양을 못 만든다 — `emit`은 complete면 두 칸을 다 채우고(`gate_server.py`
    250-266행), `emit_tagged_entry`는 빔 B만 채운다. 그래서 빔 A만 실린 complete는 계약 밖이다.

    1차 수리는 dev 시험 자산이 이 모양을 정상 fixture로 쓰고 있다는 이유로 열어 뒀는데,
    그건 계약을 시험에 맞춰 푼 것이었다(수리 1차 검증 P1). fixture 쪽을 실기기 모양으로 고쳤다.
    """
    await _tag(client, auth_headers, "T-AONLY")
    await _post_pass(
        client, auth_headers, "P-AONLY",
        direction="A_TO_B", beam_a=1.0, beam_b=None, observed=1.0,
    )
    row = await _pass_row("P-AONLY")
    assert row.verdict == Verdict.BEAM_INCOMPLETE
    assert await _credit_state("T-AONLY") == CreditState.ISSUED


async def test_부정합_이벤트도_행은_그대로_남는다(client, auth_headers):
    """판정만 바꾸고 기록은 안 버린다 — 어긋난 주장을 나중에 되짚어야 한다."""
    await _tag(client, auth_headers, "T-KEEP")
    await _post_pass(
        client, auth_headers, "P-KEEP",
        direction="A_TO_B", beam_a=2.0, beam_b=1.0, observed=2.0, result="normal",
    )
    row = await _pass_row("P-KEEP")
    assert row.direction == "A_TO_B"      # 기기 주장은 지우지 않는다
    assert row.status == "complete"
    assert row.beam_a_ts is not None and row.beam_b_ts is not None
    assert row.verdict == Verdict.BEAM_INCOMPLETE


# ── ①-2 크레딧이 없으면 미태깅으로 남긴다 (수리 1차 검증 P1) ────────────────
# 1차 수리는 어긋난 주장을 **무조건** `beam_incomplete`로 접었다. 그러면 빔 칸만 비우고
# 들어오는 쪽이 Alert도·팝업도·매터모스트 카드도 안 받고 untagged 통계에서도 빠진다 —
# 미태깅을 잡으려고 넣은 검사가 미태깅 탐지에 구멍을 냈다. 크레딧이 없을 땐 지킬 게 없으니
# dev와 같은 미태깅 판정으로 남긴다("모든 미태깅 사건은 Alert 행을 남긴다" 하드닝).

async def test_크레딧_없이_빔_칸만_비운_A_TO_B는_미태깅_경보를_남긴다(client, auth_headers):
    """1차 수리 실측 반증 — verdict=beam_incomplete·Alert 0행이라 경보가 통째로 사라졌다."""
    r = await _post_pass(
        client, auth_headers, "P-NC-NOTS", gate_no=21,
        direction="A_TO_B", beam_a=None, beam_b=None, observed=1.0,
    )
    assert r.status_code == 200, r.text

    row = await _pass_row("P-NC-NOTS")
    assert row.verdict == Verdict.UNTAGGED, "빔 칸을 비우면 경보가 빠지는 우회로가 열렸다"
    assert row.matched_tagging_event_id is None
    assert await _alert_count() == 1
    # 사유는 그대로 실린다 — 판정이 미태깅이어도 "기기 주장이 이상했다"는 사실은 남는다.
    assert r.json()["beam_conflict"] == BEAM_CONFLICT_NO_TS


async def test_크레딧_없는_역순_주장도_미태깅_경보를_남긴다(client, auth_headers):
    """배선이 뒤집힌 기기로 위장해도 경보를 못 피한다."""
    await _post_pass(
        client, auth_headers, "P-NC-REV", gate_no=22,
        direction="A_TO_B", beam_a=1.4, beam_b=1.0, observed=1.4,
    )
    row = await _pass_row("P-NC-REV")
    assert row.verdict == Verdict.UNTAGGED
    assert await _alert_count() == 1


async def test_크레딧이_있으면_보수_판정으로_지킨다(client, auth_headers):
    """대조군 — 크레딧이 있는 자리에서는 안 태우고 보수 판정으로 남긴다(경보도 아니다).

    두 갈래를 가르는 축이 **크레딧 유무 하나**라는 걸 못박는 자리다.
    """
    await _tag(client, auth_headers, "T-WC", gate_no=23)
    await _post_pass(
        client, auth_headers, "P-WC", gate_no=23,
        direction="A_TO_B", beam_a=None, beam_b=None, observed=1.0,
    )
    row = await _pass_row("P-WC")
    assert row.verdict == Verdict.BEAM_INCOMPLETE
    assert await _credit_state("T-WC") == CreditState.ISSUED, "부정합 주장이 크레딧을 건드렸다"
    assert await _alert_count() == 0


async def test_부정합_미태깅도_외부_알림_채널을_그대로_탄다(client, auth_headers):
    """Alert 행만이 아니라 팝업·매터모스트 판정까지 dev와 같아야 한다."""
    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P-NC-NOTIFY", device_id="raspberry01", gate_no=24,
            direction="A_TO_B", status="complete",
            beam_a_ts=None, beam_b_ts=None, observed_at=_at(1.0),
        )
    assert ev.verdict == Verdict.UNTAGGED
    assert ev.alert_id is not None
    assert ev.notify_dashboard is True and ev.notify_chat is True
    assert ev.beam_conflict == BEAM_CONFLICT_NO_TS


# ── ② 라파 정상 페이로드 회귀 (hardware/rpi/gate_server.py) ─────────────────

async def test_라파_태깅_입장_페이로드는_그대로_정상이다(client, auth_headers):
    """`emit_tagged_entry` 모양 — 태깅 직후엔 빔 A를 안 읽어서 beam_a_ts가 null로 올라온다.

    빔 A가 없다는 사실만으로 막으면 이 정상 입장 경로가 통째로 죽는다(시연 ③ 본 갈래).
    """
    await _tag(client, auth_headers, "T-RPI1")
    r = await _post_pass(
        client, auth_headers, "P-RPI1",
        direction="A_TO_B", status="complete",
        beam_a=None, beam_b=1.0, observed=1.0, result="normal",
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == Verdict.NORMAL

    row = await _pass_row("P-RPI1")
    assert row.verdict == Verdict.NORMAL
    assert row.matched_tagging_event_id is not None
    assert await _credit_state("T-RPI1") == CreditState.CONSUMED


async def test_라파_양빔_정순_페이로드는_그대로_정상이다(client, auth_headers):
    """`emit(..., 'complete')` 모양 — 빔 A가 먼저 끊기고 빔 B가 나중이면 A_TO_B다."""
    await _tag(client, auth_headers, "T-RPI2")
    r = await _post_pass(
        client, auth_headers, "P-RPI2",
        direction="A_TO_B", status="complete",
        beam_a=1.0, beam_b=1.2, observed=1.2, result="untagged",
    )
    assert r.json()["verdict"] == Verdict.NORMAL
    assert await _credit_state("T-RPI2") == CreditState.CONSUMED


async def test_라파_퇴장_페이로드는_그대로_퇴장이다(client, auth_headers):
    """`emit`이 만든 B→A. 퇴장은 크레딧을 안 태우고 기록만 남는다(회수는 기본 꺼짐)."""
    await _tag(client, auth_headers, "T-RPI3")
    r = await _post_pass(
        client, auth_headers, "P-RPI3",
        direction="B_TO_A", status="complete",
        beam_a=1.2, beam_b=1.0, observed=1.2, result="exit",
    )
    assert r.json()["verdict"] == Verdict.EXIT
    assert await _credit_state("T-RPI3") == CreditState.ISSUED


async def test_라파_한쪽_빔_미완_페이로드는_그대로_보수판정이다(client, auth_headers):
    """`emit(..., 'incomplete')` 모양 — direction=null·status=incomplete."""
    r = await _post_pass(
        client, auth_headers, "P-RPI4",
        direction=None, status="incomplete", beam_a=1.0, beam_b=None, observed=1.0,
    )
    assert r.json()["verdict"] == Verdict.BEAM_INCOMPLETE


# ── ③ 조합 매트릭스: 방향 셋 × 빔 시각 넷 ──────────────────────────────────
# 빔 모양 이름과 값. status는 전부 complete로 두고 **주장 대 실물**만 본다
# (status=incomplete는 지금도 맨 앞에서 보수 판정이라 아래 ④에서 따로 짚는다).
_BEAM_SHAPES = {
    "both_fwd": (1.0, 1.2),   # a < b — 라파 `emit(complete)`가 A_TO_B로 적는 유일한 모양
    "both_rev": (1.4, 1.0),   # a > b — 라파라면 B_TO_A로 적었을 값
    "both_eq": (1.0, 1.0),    # a == b — 라파 판별식이면 이것도 B_TO_A다
    "a_only": (1.0, None),    # complete인데 나가는 쪽 확인이 없다 — 라파가 못 내는 모양
    "b_only": (None, 1.0),    # 라파 `emit_tagged_entry` — 정상 태깅 입장
    "none": (None, None),
}

# 기대 판정. A_TO_B 줄만 이번 수리로 바뀐다. 통과는 라파가 실제로 만드는 두 모양뿐이다
# (`emit(complete)`의 both_fwd, `emit_tagged_entry`의 b_only). 나머지 넷은 계약 밖이라
# 크레딧을 안 태운다 — 이 매트릭스는 **크레딧을 깔고** 도니까 보수 판정으로 떨어진다.
# 크레딧 없는 줄은 아래 별도 매트릭스에서 미태깅으로 남는 걸 본다.
_EXPECTED = {
    ("A_TO_B", "both_fwd"): Verdict.NORMAL,
    ("A_TO_B", "both_rev"): Verdict.BEAM_INCOMPLETE,
    ("A_TO_B", "both_eq"): Verdict.BEAM_INCOMPLETE,
    ("A_TO_B", "a_only"): Verdict.BEAM_INCOMPLETE,
    ("A_TO_B", "b_only"): Verdict.NORMAL,
    ("A_TO_B", "none"): Verdict.BEAM_INCOMPLETE,
    # 퇴장은 크레딧을 안 태우는 갈래라 이번 검사 범위 밖이다. 거동이 안 바뀌는 걸 굳힌다.
    ("B_TO_A", "both_fwd"): Verdict.EXIT,
    ("B_TO_A", "both_rev"): Verdict.EXIT,
    ("B_TO_A", "both_eq"): Verdict.EXIT,
    ("B_TO_A", "a_only"): Verdict.EXIT,
    ("B_TO_A", "b_only"): Verdict.EXIT,
    ("B_TO_A", "none"): Verdict.EXIT,
    # 방향이 없으면 예전부터 보수 판정이다.
    (None, "both_fwd"): Verdict.BEAM_INCOMPLETE,
    (None, "both_rev"): Verdict.BEAM_INCOMPLETE,
    (None, "both_eq"): Verdict.BEAM_INCOMPLETE,
    (None, "a_only"): Verdict.BEAM_INCOMPLETE,
    (None, "b_only"): Verdict.BEAM_INCOMPLETE,
    (None, "none"): Verdict.BEAM_INCOMPLETE,
}


@pytest.mark.parametrize("direction", ["A_TO_B", "B_TO_A", None])
@pytest.mark.parametrize("shape", list(_BEAM_SHAPES))
async def test_방향_빔_조합_매트릭스(client, auth_headers, direction, shape):
    """조합마다 크레딧 한 장을 깔아 두고 판정과 크레딧 상태를 같이 본다."""
    beam_a, beam_b = _BEAM_SHAPES[shape]
    key = f"{direction or 'NONE'}-{shape}"
    await _tag(client, auth_headers, f"T-{key}")
    observed = max(v for v in (beam_a, beam_b, 1.0) if v is not None)

    r = await _post_pass(
        client, auth_headers, f"P-{key}",
        direction=direction, status="complete",
        beam_a=beam_a, beam_b=beam_b, observed=observed,
    )
    assert r.status_code == 200, r.text

    expected = _EXPECTED[(direction, shape)]
    row = await _pass_row(f"P-{key}")
    assert row.verdict == expected

    # 크레딧이 탄 건 normal 판정뿐이어야 한다(저장 행 실물로 대조).
    credit = await _credit_state(f"T-{key}")
    if expected == Verdict.NORMAL:
        assert credit == CreditState.CONSUMED
        assert row.matched_tagging_event_id is not None
    else:
        assert credit == CreditState.ISSUED
        assert row.matched_tagging_event_id is None


# 같은 매트릭스를 **크레딧 없이** 한 번 더 돈다. 1차 수리의 매트릭스는 18칸이 전부 크레딧을
# 깔고 시작해서, 경보가 사라지는 자리(크레딧 없는 A_TO_B 부정합)를 한 칸도 안 쟀다
# (수리 1차 검증이 짚은 시험 구멍). 여기서는 판정과 **Alert 행 수**를 같이 본다.
_EXPECTED_NO_CREDIT = {
    # 크레딧이 없으니 통과 모양도 미태깅이다(dev와 같다).
    ("A_TO_B", "both_fwd"): Verdict.UNTAGGED,
    ("A_TO_B", "b_only"): Verdict.UNTAGGED,
    # 어긋난 주장도 미태깅으로 남는다 — 빔 칸을 비워 경보를 피하는 우회로를 막는다.
    ("A_TO_B", "both_rev"): Verdict.UNTAGGED,
    ("A_TO_B", "both_eq"): Verdict.UNTAGGED,
    ("A_TO_B", "a_only"): Verdict.UNTAGGED,
    ("A_TO_B", "none"): Verdict.UNTAGGED,
}


@pytest.mark.parametrize("shape", list(_BEAM_SHAPES))
async def test_크레딧_없는_A_TO_B_매트릭스는_전부_미태깅이다(client, auth_headers, shape):
    """빔 모양이 뭐든 A_TO_B 주장이 왔는데 쓸 크레딧이 없으면 경보가 남아야 한다."""
    beam_a, beam_b = _BEAM_SHAPES[shape]
    observed = max(v for v in (beam_a, beam_b, 1.0) if v is not None)
    r = await _post_pass(
        client, auth_headers, f"P-NC-{shape}", gate_no=31,
        direction="A_TO_B", status="complete",
        beam_a=beam_a, beam_b=beam_b, observed=observed,
    )
    assert r.status_code == 200, r.text

    row = await _pass_row(f"P-NC-{shape}")
    assert row.verdict == _EXPECTED_NO_CREDIT[("A_TO_B", shape)]
    assert await _alert_count() == 1, "미태깅 사건인데 Alert 행이 안 남았다"


# ── ④ 기존 체계와 안 어긋나는지 ────────────────────────────────────────────

async def test_status_incomplete가_방향_주장보다_먼저다(client, auth_headers):
    """한쪽 빔만 끊긴 사건은 방향 주장이 뭐든 예전처럼 맨 앞에서 보수 판정이다."""
    await _tag(client, auth_headers, "T-ST")
    await _post_pass(
        client, auth_headers, "P-ST",
        direction="A_TO_B", status="incomplete", beam_a=1.0, beam_b=None, observed=1.0,
    )
    row = await _pass_row("P-ST")
    assert row.verdict == Verdict.BEAM_INCOMPLETE
    assert await _credit_state("T-ST") == CreditState.ISSUED


async def test_보수_판정은_쿨다운_눈금을_안_먹는다(client, auth_headers):
    """크레딧을 지킨 보수 판정은 경고가 아니라서 창을 안 찍는다 — 뒤에 온 진짜 미태깅이 울린다.

    예전 쿨다운 사고(경고가 조용히 삼켜지던 자리)와 같은 계열이라 여기서 같이 지킨다.
    크레딧을 하나 깔아 보수 판정 갈래로 보낸다(크레딧이 없으면 그 자체가 미태깅이다).
    """
    await _tag(client, auth_headers, "T-CD", gate_no=7)
    await _post_pass(
        client, auth_headers, "P-CD1", gate_no=7,
        direction="A_TO_B", beam_a=None, beam_b=None, observed=1.0,
    )
    assert await _alert_count() == 0

    # 크레딧을 안 태웠으니 다음 정상 통과가 그 크레딧을 그대로 쓴다.
    await _post_pass(
        client, auth_headers, "P-CD2", gate_no=7,
        direction="A_TO_B", beam_a=1.0, beam_b=1.2, observed=1.2,
    )
    row = await _pass_row("P-CD2")
    assert row.verdict == Verdict.NORMAL
    assert await _credit_state("T-CD") == CreditState.CONSUMED
    assert await _alert_count() == 0


async def test_부정합_미태깅_뒤_진짜_미태깅도_사건이_남는다(client, auth_headers):
    """부정합이 미태깅으로 남는 갈래에서도 쿨다운은 **외부 알림만** 억제한다.

    사건 기록(Alert 행)까지 삼키면 예전 사고(미태깅 297건 중 279건이 사라진 자리)가 돌아온다.
    """
    first = await _post_pass(
        client, auth_headers, "P-CDX1", gate_no=17,
        direction="A_TO_B", beam_a=None, beam_b=None, observed=1.0,
    )
    second = await _post_pass(
        client, auth_headers, "P-CDX2", gate_no=17,
        direction="A_TO_B", beam_a=1.0, beam_b=1.2, observed=1.2,
    )
    assert first.status_code == 200 and second.status_code == 200
    assert (await _pass_row("P-CDX1")).verdict == Verdict.UNTAGGED
    assert (await _pass_row("P-CDX2")).verdict == Verdict.UNTAGGED
    assert await _alert_count() == 2, "쿨다운이 사건 기록까지 삼켰다"


async def test_부정합이어도_저신뢰_집계는_그대로_돈다(client, auth_headers):
    """low_confidence는 판정과 별개 축이다. 보수 판정으로 떨어져도 계속 세야 한다.

    응답(IngestAck)에 안 실리는 값이라 판정 함수를 직접 부른다.
    """
    for i in range(4):  # low_confidence_credit_threshold=3 → 4장이면 초과
        await _tag(client, auth_headers, f"T-LC{i}", gate_no=8, tag_id=f"2025000{i}",
                   offset=0.0)

    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P-LC", device_id="raspberry01", gate_no=8,
            direction="A_TO_B", status="complete",
            beam_a_ts=None, beam_b_ts=None, observed_at=_at(1.0),
        )
    assert ev.verdict == Verdict.BEAM_INCOMPLETE
    assert ev.beam_conflict == BEAM_CONFLICT_NO_TS
    assert ev.low_confidence is True
    assert ev.alert_id is None
    assert ev.notify_dashboard is False and ev.notify_chat is False


async def test_역순_부정합_사유가_결과에_실린다(client, auth_headers):
    """어긋난 사유를 호출자가 볼 수 있어야 운영에서 기기를 짚는다."""
    await _tag(client, auth_headers, "T-RSN", gate_no=9)
    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P-RSN", device_id="raspberry01", gate_no=9,
            direction="A_TO_B", status="complete",
            beam_a_ts=_at(2.0), beam_b_ts=_at(1.0), observed_at=_at(2.0),
        )
    assert ev.verdict == Verdict.BEAM_INCOMPLETE
    assert ev.beam_conflict == BEAM_CONFLICT_ORDER


async def test_중복_재전송은_원래_판정과_사유를_그대로_돌려준다(client, auth_headers):
    """멱등 계약은 안 바뀐다 — 두 번째 요청도 200에 같은 판정이다.

    사유 칸도 저장 행에서 다시 뽑아 채운다. 라파 sender가 2xx가 나올 때까지 무한 재전송이라
    운영에서 실제로 보이는 응답은 대부분 이 재전송분인데, 1차 수리는 여기서 None을 돌려줘서
    사유 칸이 사실상 늘 비어 있었다(수리 1차 검증 P3).
    """
    await _tag(client, auth_headers, "T-DUP")
    body = dict(direction="A_TO_B", beam_a=1.4, beam_b=1.0, observed=1.4)
    first = await _post_pass(client, auth_headers, "P-DUP", **body)
    second = await _post_pass(client, auth_headers, "P-DUP", **body)
    assert first.json()["verdict"] == Verdict.BEAM_INCOMPLETE
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True
    assert second.json()["verdict"] == Verdict.BEAM_INCOMPLETE
    assert second.json()["beam_conflict"] == BEAM_CONFLICT_ORDER, "재전송분에 사유가 안 실렸다"
    # 크레딧은 두 번 다 그대로다.
    assert await _credit_state("T-DUP") == CreditState.ISSUED


# ── ⑤ 만료 청소는 부정합 갈래에서도 돈다 (수리 1차 검증 P2) ─────────────────

async def test_부정합_갈래도_만료_크레딧을_청소한다(client, auth_headers):
    """1차 수리는 부정합이면 청소를 통째로 건너뛰어서, dev면 expired로 닫히는 크레딧이
    issued로 남았다. 청소는 기기 주장이 아니라 시계 둘의 합의(min)로만 도는 일이라 건너뛸
    이유가 없다 — 남겨 두면 저신뢰 집계와 퇴장 회수가 부풀어 보인다.

    시각 축은 서버 현재다(청소가 벽시계를 같이 본다). TTL 3초를 넘긴 크레딧을 깔아 둔다.
    """
    stale = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=30)
    r = await client.post(
        "/api/tagging-events",
        json={"event_id": "T-SWEEP", "device_id": "raspberry01", "gate_no": 41,
              "tag_id": "20250041", "observed_at": stale.isoformat()},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P-SWEEP", device_id="raspberry01", gate_no=41,
            direction="A_TO_B", status="complete",
            beam_a_ts=None, beam_b_ts=None,
            observed_at=dt.datetime.now(dt.timezone.utc),
        )
    assert ev.beam_conflict == BEAM_CONFLICT_NO_TS
    assert await _credit_state("T-SWEEP") == CreditState.EXPIRED, "부정합 갈래가 청소를 건너뛰었다"


# ── ⑥ 어긋난 사유를 밖으로 내보낸다 (수리 1차 검증 P3) ──────────────────────
# 1차 수리는 사유를 `PassEvaluation`에만 담고 WS·응답 어디에도 안 실었다. 읽는 자리가
# 시험뿐이라 운영에서 이상한 기기를 짚으려면 서버 로그 grep 말고 방법이 없었다.

async def test_어긋난_사유가_응답에_실린다(client, auth_headers):
    await _tag(client, auth_headers, "T-ACK", gate_no=42)
    r = await _post_pass(
        client, auth_headers, "P-ACK", gate_no=42,
        direction="A_TO_B", beam_a=1.0, beam_b=None, observed=1.0,
    )
    assert r.json()["beam_conflict"] == BEAM_CONFLICT_B_MISSING


async def test_평소_응답에는_사유_칸이_안_붙는다(client, auth_headers):
    """어긋남이 없으면 칸을 아예 안 싣는다 — dev ack 계약이 한 글자도 안 바뀐다."""
    await _tag(client, auth_headers, "T-ACK2", gate_no=43)
    r = await _post_pass(
        client, auth_headers, "P-ACK2", gate_no=43,
        direction="A_TO_B", beam_a=1.0, beam_b=1.2, observed=1.2,
    )
    assert r.json() == {
        "event_id": "P-ACK2", "stored": True, "duplicate": False, "verdict": "normal",
    }


async def test_어긋난_사유가_WS_메시지에도_실린다(client, auth_headers, monkeypatch):
    """화면이 "어느 기기가 이상한 주장을 보내나"를 실시간으로 볼 수 있어야 한다."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)

    await _tag(client, auth_headers, "T-WS", gate_no=44)
    await _post_pass(
        client, auth_headers, "P-WS", gate_no=44,
        direction="A_TO_B", beam_a=1.4, beam_b=1.0, observed=1.4,
    )
    passes = [m for m in box if m["type"] == "gate_pass_event"]
    assert len(passes) == 1
    assert passes[0]["data"]["beam_conflict"] == BEAM_CONFLICT_ORDER

    # 어긋남이 없는 메시지에는 칸이 안 붙는다(dev WS 계약 시험이 칸 집합을 통째로 대조한다).
    box.clear()
    await _tag(client, auth_headers, "T-WS2", gate_no=45)
    await _post_pass(
        client, auth_headers, "P-WS2", gate_no=45,
        direction="A_TO_B", beam_a=1.0, beam_b=1.2, observed=1.2,
    )
    normal = [m for m in box if m["type"] == "gate_pass_event"][0]
    assert "beam_conflict" not in normal["data"]
