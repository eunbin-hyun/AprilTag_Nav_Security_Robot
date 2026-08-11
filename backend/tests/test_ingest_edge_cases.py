"""인입 경계값·판정 분기 시험. 정찰 "안 덮인 구멍" 중 코드로 채울 수 있는 부분.

- beam_incomplete 두 경로(status=incomplete / complete인데 direction 없음)
- 셔틀 인입의 422·401(태깅·빔통과만 있고 셔틀은 안 덮여 있었음)
- 필드 길이 경계(min_length=1, max_length=128/64)
"""
import datetime as dt

import pytest

from app.credit.state_machine import Verdict

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ── 판정 분기: beam_incomplete ──────────────────────────────────────────────

async def test_status_incomplete_is_beam_incomplete_no_alert(client, auth_headers):
    r = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": "BI1", "gate_no": 1, "direction": None, "status": "incomplete",
            "observed_at": _now(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == Verdict.BEAM_INCOMPLETE

    alerts = await client.get("/api/alerts")
    assert alerts.json() == []


async def test_complete_without_direction_is_beam_incomplete(client, auth_headers):
    # 계약 밖 조합(complete인데 direction null) — 판정 없이 기록만, 500이 아니다.
    r = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": "BI2", "gate_no": 1, "direction": None, "status": "complete",
            "beam_a_ts": _now(), "observed_at": _now(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == Verdict.BEAM_INCOMPLETE


# ── 셔틀 인입: 스키마·인증 (여태 태깅/빔통과만 덮여 있었음) ───────────────────

async def test_shuttle_missing_signal_ts_422(client, auth_headers):
    body = {"event_id": "S-422-1", "gate_no": 2, "shuttle_no": "SH-1"}
    resp = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_shuttle_missing_api_key_401(client):
    body = {"event_id": "S-401-1", "gate_no": 2, "shuttle_no": "SH-1", "signal_ts": _now()}
    resp = await client.post("/api/shuttle-arrivals", json=body)
    assert resp.status_code == 401, resp.text


# ── 필드 길이 경계 ───────────────────────────────────────────────────────────

async def test_event_id_empty_string_422(client, auth_headers):
    body = {"event_id": "", "tag_id": "20250001", "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_tag_id_empty_string_422(client, auth_headers):
    body = {"event_id": "L1", "tag_id": "", "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_event_id_over_max_length_422(client, auth_headers):
    body = {"event_id": "x" * 129, "tag_id": "20250001", "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_event_id_exactly_max_length_ok(client, auth_headers):
    body = {"event_id": "x" * 128, "tag_id": "20250001", "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True


async def test_tag_id_over_max_length_422(client, auth_headers):
    body = {"event_id": "L2", "tag_id": "y" * 65, "observed_at": _now()}
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


# ── device_id·gate_no 상한 (전체검토 D16) ────────────────────────────────────
# 왜 이 묶음이 생겼나 — 두 칸에만 상한이 없어서, 65자 device_id는 asyncpg
# StringDataRightTruncation으로, int32를 넘는 gate_no는 DataError로 **500**이 났다.
# 500은 5xx라 라파 sender_worker(`hardware/rpi/gate_server.py:686~706`)의 백오프 재시도에
# 걸리고, 전송 스레드가 하나라 그 뒤 이벤트 큐가 통째로 밀린다. 422면 그 자리에서 버린다.
#
# ⚠ 상한을 넣되 **필수/선택은 안 건드렸다.** 두 칸 다 여전히 선택이고, 실기기가 지금 보내는
# 값(device_id="raspberry01" 11자 / gate_no=5)은 아래 통과 시험이 지킨다.

_OVER_INT32 = 99999999999  # DataError: value out of int32 range를 냈던 값


async def test_tagging_device_id_over_max_length_422(client, auth_headers):
    body = {
        "event_id": "D16-T1", "device_id": "d" * 65, "gate_no": 5,
        "tag_id": "20250001", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_gate_pass_device_id_over_max_length_422(client, auth_headers):
    body = {
        "event_id": "D16-G1", "device_id": "d" * 65, "gate_no": 5,
        "direction": "A_TO_B", "status": "complete", "observed_at": _now(),
    }
    resp = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_device_id_exactly_max_length_ok(client, auth_headers):
    # DB 컬럼(String(64))과 같은 폭까지는 그대로 들어간다 — 상한이 컬럼을 넘지 않는지 재는 자리다.
    body = {
        "event_id": "D16-T2", "device_id": "d" * 64, "gate_no": 5,
        "tag_id": "20250002", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True


async def test_tagging_gate_no_over_int32_422(client, auth_headers):
    body = {
        "event_id": "D16-T3", "device_id": "raspberry01", "gate_no": _OVER_INT32,
        "tag_id": "20250003", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_gate_pass_gate_no_over_int32_422(client, auth_headers):
    body = {
        "event_id": "D16-G2", "device_id": "raspberry01", "gate_no": _OVER_INT32,
        "direction": "A_TO_B", "status": "complete", "observed_at": _now(),
    }
    resp = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_shuttle_gate_no_over_int32_422(client, auth_headers):
    body = {
        "event_id": "D16-S1", "gate_no": _OVER_INT32,
        "shuttle_no": "SH-1", "signal_ts": _now(),
    }
    resp = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


# ⚠ 아래 두 못은 **리터럴 9999/10000**을 쓴다. `GATE_NO_MAX`를 import해서 쓰면 상수를 99로
# 줄여도 시험이 따라 내려가 그대로 초록이라, 정작 재려던 "상한 값 자체"를 안 지킨다.
# 저장소 안 gate_no 리터럴 최댓값이 59뿐이라 아무 시험도 그 축소를 못 잡았다.
# `_OVER_INT32` 한 값만 재면 상한이 9999든 int32 최댓값이든 구분이 안 된다는 게 D16 검증의 지적이다.
# 상한을 일부러 옮기려면 이 두 줄을 같이 고쳐라 — 그게 이 못의 값어치다.
_GATE_NO_MAX_EXPECTED = 9999


async def test_gate_no_max_constant_is_pinned():
    """상한 값이 조용히 움직이지 않게 못을 박는다(위 주석 참고)."""
    from app.schemas import GATE_NO_MAX

    assert GATE_NO_MAX == _GATE_NO_MAX_EXPECTED


async def test_gate_no_exactly_max_ok(client, auth_headers):
    # device_id는 `test_device_id_exactly_max_length_ok`가 경계를 잡는데 gate_no만 비어 있었다.
    body = {
        "event_id": "D16-T5", "device_id": "raspberry01", "gate_no": 9999,
        "tag_id": "20250006", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True


async def test_gate_no_over_max_422(client, auth_headers):
    """상한 +1은 422다. int32 안이라 DB는 받아 주는 값이고, 막는 건 계약이다."""
    body = {
        "event_id": "D16-T6", "device_id": "raspberry01", "gate_no": 10000,
        "tag_id": "20250007", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


async def test_gate_no_zero_422(client, auth_headers):
    # 0·음수는 없는 게이트다. 화면 셔틀 호출(`ShuttleCallIn`)이 이미 같은 범위로 막고 있어
    # 기기 인입만 열려 있으면 같은 사실의 계약이 창구마다 갈린다.
    body = {
        "event_id": "D16-T4", "gate_no": 0,
        "tag_id": "20250004", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 422, resp.text


# ── 실기기가 지금 보내는 프레임은 그대로 통과해야 한다 ─────────────────────────
# 값은 `hardware/rpi/gate_server.py:40-41` 기본값(DEVICE_ID="raspberry01" / GATE_NO=5)이다.

async def test_real_device_tagging_frame_still_ok(client, auth_headers):
    body = {
        "event_id": "raspberry01-1754300000000-0001", "device_id": "raspberry01",
        "gate_no": 5, "tag_id": "20250005", "observed_at": _now(),
    }
    resp = await client.post("/api/tagging-events", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True


async def test_real_device_gate_pass_frame_still_ok(client, auth_headers):
    # ⚠ `result`는 라파의 두 emit 갈래가 **늘 싣는** 칸이다(`gate_server.py:288`·`:305`).
    #    status=complete·direction=A_TO_B면 "untagged"나 "normal"이다. 이 칸을 빼고 재면
    #    실기기가 실제로 보내는 프레임이 아니라 그 부분집합만 지키게 된다.
    body = {
        "event_id": "raspberry01-1754300000001-0002", "device_id": "raspberry01",
        "gate_no": 5, "direction": "A_TO_B", "status": "complete",
        "beam_a_ts": _now(), "beam_b_ts": _now(), "observed_at": _now(),
        "result": "untagged",
    }
    resp = await client.post("/api/gate-pass-events", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True


async def test_shuttle_caller_frame_still_ok(client, auth_headers):
    """셔틀 인입을 실제로 부르는 쪽 프레임. **실기기가 아니다.**

    셔틀은 실기기를 안 만들기로 확정됐고(`query.py:603`), `hardware/` 어디에도
    `/api/shuttle-arrivals`를 부르는 자리가 없다. 지금 이 창구를 부르는 건 개발자 화면
    (`static/telemetry.html:1106`)과 더미 퍼블리셔(`tools/dummy_publisher.py:153`) 둘이다.
    아래 본문은 더미 퍼블리셔가 보내는 칸 그대로다(event_id 형식도 `make_event_id`와 같다).
    """
    body = {
        "event_id": "raspberry01-1754300000002-0003", "gate_no": 5,
        "shuttle_no": "SH-1", "signal_ts": _now(),
    }
    resp = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] is True
