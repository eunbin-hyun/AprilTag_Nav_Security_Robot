"""빔 통과 인입에 실려 오는 기기 1차 판단(`result`) 수용·노출.

하드웨어 쪽이 라파이에서 자기 나름의 1차 판단을 같이 올리고 싶다고 요청했다(7/28). 아직
값 enum이 안 정해져서 자유 문자열 32자 상한으로만 받는다.

⚠ 이 값은 **서버 판정을 대체하지 않는다**. DB 컬럼도 안 만든다(마이그레이션을 안 건드린다).
받은 값은 대시보드 메시지 `data`의 `device_result`로 흘려보내 화면에서 눈으로 대조만 한다.
서버 verdict는 크레딧 상태머신이 내는 값 그대로다 — 여기 케이스가 그걸 못박는다.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 28, 3, 0, 0, tzinfo=dt.timezone.utc)
GATE_PASS_PAYLOAD_KEYS = {
    "event_id", "gate_no", "direction", "status",
    # tag_id는 태운 크레딧의 카드 UID다(S15P11C207-241). 늘 실리고 소비가 없으면 null이라
    # 이 파일이 재는 device_result 축과 겹치지 않는다.
    "verdict", "matched_tagging_event_id", "tag_id", "low_confidence", "device_result",
    # pass_id는 통과 행 PK다. untagged_alert의 source_id와 잇는 열쇠라 늘 실린다
    # (계약 못은 tests/test_ws_pass_id_link.py).
    "pass_id",
    # ⭐ 카드 주인. 명부에 없으면 둘 다 null이고 **늘 실린다**(프론트 11차 §4-2, 사용자 확정).
    "staff_name", "staff_student_no",
}
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "detection_event.schema.json"


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


@pytest.fixture
def sent(monkeypatch):
    """manager.broadcast를 가로채 메시지를 모은다(test_dashboard_ws.py와 같은 방식)."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


def _pass_body(event_id: str, **extra) -> dict:
    body = {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 1,
        "direction": "A_TO_B",
        "status": "complete",
        "beam_a_ts": _iso(1),
        "beam_b_ts": _iso(1.2),
        "observed_at": _iso(1),
    }
    body.update(extra)
    return body


async def _tag(client, auth_headers, event_id: str) -> None:
    resp = await client.post(
        "/api/tagging-events",
        json={"event_id": event_id, "device_id": "raspberry01", "gate_no": 1,
              "tag_id": "UID-1", "observed_at": _iso(0)},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text


# ── 수용 ──────────────────────────────────────────────────────────────────

async def test_result_실은_인입이_200이고_메시지에_device_result가_실린다(
    client, auth_headers, sent
):
    await _tag(client, auth_headers, "t-dr-1")
    sent.clear()
    resp = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-dr-1", result="PASS"), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text

    assert [m["type"] for m in sent] == ["gate_pass_event"]
    payload = sent[0]["data"]
    assert set(payload) == GATE_PASS_PAYLOAD_KEYS
    assert payload["device_result"] == "PASS"


async def test_result_안_실은_인입은_기존과_같고_device_result가_null이다(
    client, auth_headers, sent
):
    """옵션 칸이라 안 보내는 기기의 메시지는 값만 null이고 나머지는 그대로다."""
    await _tag(client, auth_headers, "t-dr-2")
    sent.clear()
    resp = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-dr-2"), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text

    payload = sent[0]["data"]
    assert set(payload) == GATE_PASS_PAYLOAD_KEYS
    assert payload["device_result"] is None
    assert payload["verdict"] == "normal"
    assert payload["matched_tagging_event_id"] is not None


async def test_길이_상한_32자까지_받고_넘으면_422(client, auth_headers):
    ok = await client.post(
        "/api/gate-pass-events",
        json=_pass_body("p-dr-3", result="X" * 32),
        headers=auth_headers,
    )
    assert ok.status_code == 200, ok.text

    too_long = await client.post(
        "/api/gate-pass-events",
        json=_pass_body("p-dr-4", result="X" * 33),
        headers=auth_headers,
    )
    assert too_long.status_code == 422, too_long.text


async def test_값_enum은_아직_미정이라_자유_문자열을_받는다(client, auth_headers, sent):
    """검증은 길이만이다. 하드웨어가 쓰는 낱말이 정해지기 전에 서버가 먼저 막으면 안 된다."""
    resp = await client.post(
        "/api/gate-pass-events",
        json=_pass_body("p-dr-5", result="아직 안 정한 값"),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert sent[0]["data"]["device_result"] == "아직 안 정한 값"


# ── 서버 판정 불변 ─────────────────────────────────────────────────────────

async def test_verdict는_result와_무관하다(client, auth_headers, sent):
    """기기가 뭐라고 올리든 서버 판정은 크레딧 상태머신 결과 그대로다.

    태깅이 없는 통과는 result="PASS"가 실려 와도 untagged다 — 기기 값이 판정을 못 덮는다.
    """
    resp = await client.post(
        "/api/gate-pass-events",
        json=_pass_body("p-dr-6", result="PASS"),
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["verdict"] == "untagged"

    # 미태깅이라 경고 메시지까지 두 장. result가 알림 흐름도 안 건드린다.
    assert [m["type"] for m in sent] == ["gate_pass_event", "untagged_alert"]
    assert sent[0]["data"]["device_result"] == "PASS"
    assert sent[0]["data"]["verdict"] == "untagged"


async def test_result_유무가_verdict를_안_가른다(client, auth_headers):
    """같은 시나리오를 result만 넣고 빼고 돌려 판정이 같은지 본다."""
    await _tag(client, auth_headers, "t-dr-7")
    with_result = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-dr-7", result="FAIL"), headers=auth_headers
    )
    await _tag(client, auth_headers, "t-dr-8")
    without = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-dr-8"), headers=auth_headers
    )
    assert with_result.json()["verdict"] == without.json()["verdict"] == "normal"


# ── 공용 스키마(정본 JSON) ────────────────────────────────────────────────

async def test_공용_스키마에_result가_옵션으로_들어있다():
    """프론트 TS 타입도 이 파일을 본다. 서버 pydantic만 고치면 계약이 갈라진다."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    prop = schema["properties"]["result"]
    assert prop["type"] == ["string", "null"]
    assert prop["maxLength"] == 32
    # result를 덧붙였다고 필수 칸이 늘면 안 된다. 목록 자체는 2026-08-04에
    # ["event_id","kind","observed_at"] → ["event_id"]로 바뀌었다(느슨한 쪽 확정).
    # 창구별 필수 칸과 코드의 대조는 test_ingest_contract_required.py가 맡는다.
    assert schema["required"] == ["event_id"]
    # additionalProperties를 새로 걸면 기존 기기 프레임이 통째로 막힌다.
    assert "additionalProperties" not in schema
