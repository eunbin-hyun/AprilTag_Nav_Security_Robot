"""크레딧에 신원을 실어 통과 행까지 잇는다 (S15P11C207-241 · 프론트 요구 N1).

## 수리 전에 뭐가 새고 있었나

크레딧이 익명이었다. 태깅이 오면 그 게이트에 크레딧 한 장이 쌓이고, A→B 통과가 오면
**수량만** 한 장 줄었다. 그래서 A가 찍고 B가 지나가도 서버는 "정상 통과 한 건"까지만 알고,
통과 행에서 카드 UID로 가는 길이 아예 없었다(`gate_pass_event`에 그 칸이 없었다). 화면은
"정상 통과"라는 글자밖에 못 그렸고, 나중에 되짚을 때도 누구 크레딧이 탔는지 못 댔다.

## 수리 뒤 계약

소비 규칙은 **한 글자도 안 바꾼다.** 여전히 그 게이트의 유효 크레딧 중 먼저 발급된 한 장을
FIFO로 태운다. 달라진 건 태우는 그 UPDATE의 RETURNING으로 UID를 같이 받아
`gate_pass_event.tag_id`에 적는 것뿐이다(마이그레이션 0007, nullable 추가형).

그래서 이 칸은 **"누가 지나갔나"가 아니라 "어느 크레딧을 태웠나"**다. 빔은 사람을 못
알아보니 그 이상은 못 준다 — 줄이 엉키면 적히는 이름도 같이 엉킨다. 이 파일이 지키는 건
셋이다.
  ① 순서대로 찍고 순서대로 지나가면 UID도 그 순서로 붙는다.
  ② 크레딧을 안 태운 판정(미태깅·퇴장·보류)은 NULL이다 — 남의 이름을 빌려 쓰지 않는다.
     ⚠ F22에서 예외가 하나 붙었다. **기기가 인입에 실어 보낸 관측 UID**는 그 판정에도 남는다
     (7번). 서버가 크레딧에서 이름을 지어내는 게 아니라 기기가 읽은 값을 안 잃는 것이라
     "빌려 쓰지 않는다"는 규칙 자체는 그대로다 — 인입에 UID가 없으면 여전히 NULL이다.
  ③ 만료된 크레딧의 UID는 안 붙는다. 태깅 행은 같은 게이트에 멀쩡히 남아 있어서, UID를
     "소비"가 아니라 "최근 태깅 조회"로 채우면 미태깅 통과에 방금 만료된 사람 이름이 붙는다.

인입(WS)·조회(`GET /api/events`) 두 창구까지 같이 본다. DB 컬럼만 차고 창구가 안 실으면
화면 입장에서는 수리가 안 된 것과 같다.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.credit.state_machine import Verdict, evaluate_pass
from app.db import get_session
from app.models import GatePassEvent
from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 만료를 확실히 지난 고정 과거 시각. 판정은 lazy expiry라 서버 현재와 무관하게 재현된다.
PAST = dt.datetime(2026, 7, 24, 3, 0, 0, tzinfo=dt.timezone.utc)


def _live() -> dt.datetime:
    """지금 발급하면 살아 있는 크레딧을 만들 기준 시각(TTL 3초 안에 케이스가 끝난다)."""
    return dt.datetime.now(dt.timezone.utc)


def _iso(base: dt.datetime, offset_sec: float) -> str:
    return (base + dt.timedelta(seconds=offset_sec)).isoformat()


def _tagging(event_id: str, tag_id: str, base: dt.datetime, offset: float) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 1,
        "tag_id": tag_id,
        "observed_at": _iso(base, offset),
    }


def _pass(event_id: str, base: dt.datetime, offset: float, direction: str = "A_TO_B") -> dict:
    """실기기가 만드는 모양(빔 A 먼저·빔 B 나중)으로 채운다.

    빔 칸을 대충 비우면 방향 정합 검사(S15P11C207-243)가 어긋난 주장으로 보고 크레딧을
    아예 안 태운다 — 그러면 이 파일이 재려는 UID 축이 통째로 안 열린다.
    """
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 1,
        "direction": direction,
        "status": "complete",
        "beam_a_ts": _iso(base, offset),
        "beam_b_ts": _iso(base, offset + 0.2),
        "observed_at": _iso(base, offset),
    }


async def _pass_row(event_id: str) -> GatePassEvent:
    async with get_session() as s:
        return (
            await s.execute(select(GatePassEvent).where(GatePassEvent.event_id == event_id))
        ).scalar_one()


@pytest.fixture
def sent(monkeypatch):
    """manager.broadcast를 가로채 WS 메시지를 모은다(test_dashboard_ws.py와 같은 방식)."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


# 1. 두 명이 순서대로 태깅·통과하면 각자 UID가 순서대로 붙는다
async def test_two_people_in_order_get_their_own_uid(client, auth_headers):
    """수리 전에는 둘 다 "정상 통과"였고 이름 칸이 없었다. 이제 줄 순서대로 이름이 붙는다.

    ⚠ 이게 성립하는 건 **찍은 순서와 지나간 순서가 같을 때**다. 소비가 FIFO라 그 이상은
    보장 못 한다(빔은 사람을 못 알아본다). 이 케이스가 재는 건 "수량만 세던 소비가
    UID를 잃지 않고 통과 행까지 나른다"이지 사람 식별이 아니다.
    """
    live = _live()
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "UID-A", live, 0.0), headers=auth_headers
    )
    await client.post(
        "/api/tagging-events", json=_tagging("T-B", "UID-B", live, 0.1), headers=auth_headers
    )

    first = await client.post(
        "/api/gate-pass-events", json=_pass("P-1", live, 0.5), headers=auth_headers
    )
    second = await client.post(
        "/api/gate-pass-events", json=_pass("P-2", live, 0.7), headers=auth_headers
    )
    assert first.json()["verdict"] == Verdict.NORMAL, first.text
    assert second.json()["verdict"] == Verdict.NORMAL, second.text

    p1 = await _pass_row("P-1")
    p2 = await _pass_row("P-2")
    assert (p1.tag_id, p2.tag_id) == ("UID-A", "UID-B"), (p1.tag_id, p2.tag_id)
    # 이름 칸과 행 id 칸은 늘 짝이다. 한쪽만 차면 화면과 되짚기가 서로 다른 답을 낸다.
    assert p1.matched_tagging_event_id != p2.matched_tagging_event_id
    assert p1.matched_tagging_event_id is not None and p2.matched_tagging_event_id is not None


# 2. 미태깅 통과는 UID가 없다(null)
async def test_untagged_pass_has_null_uid(client, auth_headers):
    """지킬 크레딧이 없는 통과에 이름이 붙으면 그건 지어낸 신원이다.

    딴 게이트에 **딴 사람의 살아 있는 크레딧**을 깔아 두고 본다. 크레딧이 아예 없는
    빈 DB에서는 "빌려 쓸 이름"조차 없어 이 케이스가 아무것도 안 재기 때문이다.

    ⚠ 다만 게이트 범위로 조회하는 구현이면 이 배치로는 "최근 태깅 조회" 함정을 못 잡는다
    (그쪽도 NULL을 낸다). 같은 게이트에서 그 함정을 실제로 잡는 건 아래 3번(만료)이다.
    """
    live = _live()
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "UID-A", live, 0.0), headers=auth_headers
    )
    body = _pass("P-9", live, 0.5)
    body["gate_no"] = 9  # 크레딧이 없는 딴 게이트
    r = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert r.json()["verdict"] == Verdict.UNTAGGED, r.text

    row = await _pass_row("P-9")
    assert row.tag_id is None, row.tag_id
    assert row.matched_tagging_event_id is None
    # 옆 게이트 태깅 행은 그대로 남아 있다(소비도 변조도 없었다).
    other = await client.get("/api/events")
    tagging_rows = [r for r in other.json() if r["kind"] == "tagging"]
    assert [r["tag_id"] for r in tagging_rows] == ["UID-A"]


# 3. 만료된 크레딧의 UID는 안 붙는다
async def test_expired_credit_uid_not_attached(client, auth_headers):
    """TTL이 지난 뒤 지나간 사람은 미태깅이고, 만료된 크레딧의 이름을 물려받지 않는다.

    태깅 행은 지워지지 않고 같은 게이트에 남는다(상태만 expired로 닫힌다). 그래서 UID를
    소비 결과가 아니라 "이 게이트 최근 태깅"으로 채우는 구현이면 여기서 이름이 샌다.
    """
    await client.post(
        "/api/tagging-events", json=_tagging("T-OLD", "UID-OLD", PAST, 0.0), headers=auth_headers
    )
    # TTL 3.0초를 넘긴 4.0초 뒤 통과.
    r = await client.post(
        "/api/gate-pass-events", json=_pass("P-LATE", PAST, 4.0), headers=auth_headers
    )
    assert r.json()["verdict"] == Verdict.UNTAGGED, r.text

    row = await _pass_row("P-LATE")
    assert row.tag_id is None, row.tag_id

    # 조회 창구에서도 통과 줄만 비어 있고 태깅 줄의 UID는 그대로 보인다.
    listed = (await client.get("/api/events")).json()
    by_kind = {row["kind"]: row for row in listed}
    assert by_kind["gate_pass"]["tag_id"] is None
    assert by_kind["tagging"]["tag_id"] == "UID-OLD"


# 4. 조회 창구(GET /api/events)가 정상 통과의 UID를 싣는다 — 프론트 요구 N1
async def test_events_feed_carries_uid_on_normal_pass(client, auth_headers):
    """DB 컬럼만 차고 창구가 안 실으면 화면 입장에서는 수리가 안 된 것과 같다.

    UNION 자리 맞춤이 이 칸을 `_null_str()`로 채우고 있어서(계열마다 안 쓰는 칸을 NULL로
    맞추는 자리), 컬럼을 붙여도 여기를 안 고치면 응답은 예전 그대로 null이다.
    """
    live = _live()
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "UID-A", live, 0.0), headers=auth_headers
    )
    await client.post(
        "/api/gate-pass-events", json=_pass("P-1", live, 0.5), headers=auth_headers
    )

    rows = (await client.get("/api/events")).json()
    gate = [r for r in rows if r["kind"] == "gate_pass"]
    assert len(gate) == 1
    assert gate[0]["verdict"] == Verdict.NORMAL
    assert gate[0]["tag_id"] == "UID-A", gate[0]
    # 태깅 줄의 뜻(찍은 카드)은 그대로다. 두 계열이 같은 칸을 다른 뜻으로 쓴다.
    tagging = [r for r in rows if r["kind"] == "tagging"]
    assert tagging[0]["tag_id"] == "UID-A"


# 5. WS 판정 메시지도 UID를 싣는다(소비가 없으면 null 칸으로)
async def test_ws_gate_pass_payload_carries_uid(client, auth_headers, sent):
    """칸은 **늘** 싣고 값만 갈린다. 있을 때만 붙이면 화면이 "안 실림"과 "미태깅"을 못 가른다.

    room_id·beam_conflict는 반대 규칙(있을 때만)이라 헷갈리기 쉬운 자리다 — 그 둘은 dev
    계약을 안 흔들려는 임시 가드고, 이 칸은 화면이 상시로 읽는 값이다.
    """
    live = _live()
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "UID-A", live, 0.0), headers=auth_headers
    )
    sent.clear()
    await client.post(
        "/api/gate-pass-events", json=_pass("P-1", live, 0.5), headers=auth_headers
    )
    normal = [m for m in sent if m["type"] == "gate_pass_event"][0]["data"]
    assert normal["tag_id"] == "UID-A", normal

    sent.clear()
    untagged_body = _pass("P-9", live, 0.6)
    untagged_body["gate_no"] = 9
    await client.post("/api/gate-pass-events", json=untagged_body, headers=auth_headers)
    untagged = [m for m in sent if m["type"] == "gate_pass_event"][0]["data"]
    assert "tag_id" in untagged and untagged["tag_id"] is None, untagged


# 6. 재전송 중복도 같은 UID를 돌려준다
async def test_duplicate_resend_reports_same_uid(client, auth_headers):
    """라파 sender는 2xx가 나올 때까지 무한 재전송이라, 운영에서 보이는 응답 대부분이 재전송분이다.

    중복 갈래가 저장 행에서 UID를 다시 안 뽑으면 그 칸은 첫 요청 한 번만 차고 나머지는 늘
    비어 보인다(`beam_conflict`가 같은 이유로 이미 밟은 자리다). 크레딧은 물론 두 번 안 탄다.
    """
    live = _live()
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "UID-A", live, 0.0), headers=auth_headers
    )
    async with get_session() as s:
        first = await evaluate_pass(
            s, event_id="P-1", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            beam_a_ts=live + dt.timedelta(seconds=0.5),
            beam_b_ts=live + dt.timedelta(seconds=0.7),
            observed_at=live + dt.timedelta(seconds=0.5),
        )
    async with get_session() as s:
        again = await evaluate_pass(
            s, event_id="P-1", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            beam_a_ts=live + dt.timedelta(seconds=0.5),
            beam_b_ts=live + dt.timedelta(seconds=0.7),
            observed_at=live + dt.timedelta(seconds=0.5),
        )
    assert first.stored is True and first.tag_id == "UID-A"
    assert again.stored is False, "재전송이 새 행을 만들었다"
    assert again.tag_id == first.tag_id
    assert again.matched_tagging_event_id == first.matched_tagging_event_id


# 7. 기기가 읽은 UID는 미태깅 통과에도 남는다 (F22)
async def test_untagged_pass_keeps_device_observed_uid():
    """미태깅 경고에서 신원 조회로 가는 길을 여는 자리다.

    `/api/staff/by-tag/{tag_id}`는 **활성 경고에 붙은 태그만** 통과시키는데(로그인 설계 §8
    결정 6), 경고를 내는 유일한 통과 갈래가 미태깅이고 그 행의 UID 칸이 늘 NULL이라 그
    창구가 구조적으로 늘 404였다. 크레딧을 안 태운 판정에도 **기기가 실제로 읽은** UID는
    남게 고쳤다.

    ⚠ 위 2·3번과 안 부딪힌다. 저기는 "서버가 크레딧에서 이름을 지어내지 말라"는 계약이고
    (`인입에 UID가 없으면 여전히 NULL`), 여기는 기기가 보낸 관측값을 안 잃는다는 계약이다.
    지금 라파 `gate_server.py`는 이 칸을 아직 안 싣는다 — 서버 쪽 길만 먼저 뚫어 둔다.
    """
    async with get_session() as s:
        result = await evaluate_pass(
            s, event_id="P-OBS", device_id="raspberry01", gate_no=7,
            direction="A_TO_B", status="complete",
            beam_a_ts=PAST, beam_b_ts=PAST + dt.timedelta(seconds=0.2),
            observed_at=PAST,
            tag_id="UID-OBSERVED",
        )
    # 크레딧이 한 장도 없으니 판정은 그대로 미태깅이다 — UID가 판정을 안 바꾼다.
    assert result.verdict == Verdict.UNTAGGED
    assert result.matched_tagging_event_id is None
    assert result.tag_id == "UID-OBSERVED"

    row = await _pass_row("P-OBS")
    assert row.tag_id == "UID-OBSERVED", row.tag_id
    assert row.matched_tagging_event_id is None


# 8. 크레딧 UID가 기기 관측값을 이긴다 (F22)
async def test_consumed_credit_uid_wins_over_device_observed(client, auth_headers):
    """둘이 어긋나면 서버가 직접 만든 사실(어느 크레딧을 태웠나)이 이긴다."""
    live = _live()
    await client.post(
        "/api/tagging-events", json=_tagging("T-A", "UID-A", live, 0.0), headers=auth_headers
    )
    async with get_session() as s:
        result = await evaluate_pass(
            s, event_id="P-BOTH", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            beam_a_ts=live + dt.timedelta(seconds=0.5),
            beam_b_ts=live + dt.timedelta(seconds=0.7),
            observed_at=live + dt.timedelta(seconds=0.5),
            tag_id="UID-MISREAD",
        )
    assert result.verdict == Verdict.NORMAL
    assert result.tag_id == "UID-A"
    assert (await _pass_row("P-BOTH")).tag_id == "UID-A"
