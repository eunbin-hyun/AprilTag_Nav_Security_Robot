"""관제 보조 API 3종 — 실DB 집계 + 라우터 계약 (LLM은 목).

⚠ **실호출은 절대 안 한다.** 아래 autouse 픽스처가 매 케이스에서 LLM 설정을 키 없는 값으로
덮어써서, 개발자 .env에 실키가 들어 있어도 시험이 GMS를 안 부른다(크레딧 보호 + 결정성).
LLM이 답한 갈래를 볼 때는 tests/assistant_stub.py의 목을 끼운다 — 갈아 끼우는 자리를
LLM 클라이언트가 보는 httpx 이름 하나로 좁혀서 시험 클라이언트 요청은 안 건드린다.

못박는 것.
- 집계 수치가 실제 인입 건수와 맞나(LLM이 아니라 SQL이 세는지).
- 키가 없어도 200에 폴백 문장이 나가나(화면이 안 비나).
- generated_by가 갈래를 정확히 가리키나.
- 태그 UID 원문이 응답 어디에도 안 실리나.
"""
from __future__ import annotations

import datetime as dt
import logging

import pytest

from app import ai_bridge
from app.config import get_settings
from app.routers import assistant as assistant_router
from app.schemas import CommStatus, RobotMode
from tests import assistant_stub
from tests.assistant_stub import stub_llm

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 관제 보조 3종은 GMS 크레딧을 태우므로 인증이 걸렸다. 키 없이 부르면 401이다.
HEADERS = {"X-API-Key": get_settings().api_key}

RAW_TAG = "AABBCCDD11223344"


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """모든 케이스의 기본값 — 키 없음. 실호출 갈래를 원천 차단한다."""
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key=""),
    )


def _mock_llm(monkeypatch, answer: str):
    """LLM이 answer를 돌려주게 만든다. 실제로 나간 요청은 돌려받은 객체의 .calls에 쌓인다."""
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key="mock-key", model="gpt-4o-mini"),
    )
    return stub_llm(monkeypatch, assistant_stub.ok(answer))


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


async def _seed_untagged(client, auth_headers, *, event_id: str, gate_no: int) -> dict:
    """태깅 없이 통과 → 미태깅 판정 + 경고 1건. 경고 ID를 돌려준다."""
    r = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": event_id,
            "device_id": "raspberry01",
            "gate_no": gate_no,
            "direction": "A_TO_B",
            "status": "complete",
            "beam_a_ts": _now_iso(),
            "observed_at": _now_iso(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "untagged"
    alerts = (await client.get("/api/alerts")).json()
    return alerts[0]


def _kst_date_of(iso: str) -> str:
    """서버가 찍은 시각을 KST 날짜로. 시험이 자정 근처에서도 안 흔들리게 실제 값을 쓴다."""
    return (
        dt.datetime.fromisoformat(iso)
        .astimezone(dt.timezone(dt.timedelta(hours=9)))
        .date()
        .isoformat()
    )


# ── ① 일일 요약 ──────────────────────────────────────────────────────────

async def test_daily_summary_empty_day_still_answers(client):
    """이벤트가 없는 날도 200이고 문장이 비지 않는다."""
    resp = await client.get("/api/assistant/daily-summary", params={"date": "2020-01-01"}, headers=HEADERS)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["date"] == "2020-01-01"
    assert body["summary"].strip()
    assert body["meta"]["generated_by"] == "fallback"
    assert body["meta"]["fallback_reason"] == "no_api_key"
    assert body["meta"]["model"] is None
    assert body["facts"]["events"]["total"] == 0


async def test_daily_summary_counts_match_ingested_rows(client, auth_headers):
    alert = await _seed_untagged(client, auth_headers, event_id="AS1", gate_no=3)
    await client.post(
        "/api/tagging-events",
        json={
            "event_id": "AS2",
            "device_id": "reader01",
            "gate_no": 3,
            "tag_id": RAW_TAG,
            "observed_at": _now_iso(),
        },
        headers=auth_headers,
    )
    await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "AS3", "gate_no": 3, "shuttle_no": "S-1", "signal_ts": _now_iso()},
        headers=auth_headers,
    )

    day = _kst_date_of(alert["created_at"])
    body = (
        await client.get("/api/assistant/daily-summary", params={"date": day}, headers=HEADERS)
    ).json()
    facts = body["facts"]

    assert facts["events"] == {
        "tagging": 1,
        "gate_pass": 1,
        "shuttle_arrival": 1,
        "total": 3,
    }
    assert facts["verdicts"]["untagged"] == 1
    assert facts["alerts"]["total"] == 1
    assert facts["alerts"]["active"] == 1
    assert facts["alerts"]["by_type"]["untagged"] == 1
    assert facts["gates"] == [{"gate_no": 3, "passes": 1, "untagged": 1}]
    assert facts["peak_hour"]["events"] == 3
    # 폴백 문장이 그 수치를 실제로 담고 있다.
    assert "3건" in body["summary"] and "무단 통과가 1건" in body["summary"]


async def test_daily_summary_uses_llm_when_key_present(client, monkeypatch):
    captured = _mock_llm(monkeypatch, "오늘은 미태깅 1건이 있었습니다.")
    body = (
        await client.get("/api/assistant/daily-summary", params={"date": "2026-07-28"}, headers=HEADERS)
    ).json()

    assert body["summary"] == "오늘은 미태깅 1건이 있었습니다."
    assert body["meta"]["generated_by"] == "llm"
    assert body["meta"]["fallback_reason"] is None
    assert body["meta"]["model"] == "gpt-4o-mini"
    # 집계된 사실이 프롬프트에 실려 나갔나
    assert "2026-07-28" in captured.calls[0]["json"]["messages"][1]["content"]


async def test_daily_summary_rejects_bad_date(client):
    resp = await client.get("/api/assistant/daily-summary", params={"date": "어제"}, headers=HEADERS)
    assert resp.status_code == 422


async def test_daily_summary_rejects_overflowing_date(client):
    """⭐ `9999-12-31`은 검증을 지나 **500**이 되던 자리다(축④ ④).

    집계 창을 만드는 `_kst_window`가 그 날에 하루를 더하는데 파이썬 `date` 최댓값이 바로
    그 날이라 `OverflowError`다. 날짜 칸에서 연도를 손으로 치면 닿는다.
    """
    resp = await client.get(
        "/api/assistant/daily-summary", params={"date": "9999-12-31"}, headers=HEADERS
    )
    assert resp.status_code == 422, resp.text
    # 하루를 더할 수 있는 마지막 날은 그대로 지난다(상한을 필요 이상으로 좁히지 않았나).
    ok = await client.get(
        "/api/assistant/daily-summary", params={"date": "9999-12-30"}, headers=HEADERS
    )
    assert ok.status_code == 200, ok.text


async def test_daily_llm_budget_falls_back_instead_of_breaking(client, monkeypatch):
    """⭐ 하루 LLM 예산을 다 쓰면 **429가 아니라 폴백 문장**이다(A1).

    크레딧 상한이 코드에 한 줄도 없어서, 화면이 브리핑을 타이머로 갱신하면 팀 공용 10만
    크레딧이 조용히 줄던 자리다. 다 썼을 때 창구가 깨지면 시연이 멈추니 문장 품질만 내린다.
    """
    _mock_llm(monkeypatch, "예산 안에서 답한 문장입니다.")
    monkeypatch.setattr(assistant_router, "_daily_llm_limit", lambda: 1)
    assistant_router.reset_call_quota()

    # ⚠ **날짜를 갈라 부른다**(2026-08-07 · 0020 캐시가 생긴 뒤). 같은 날을 두 번 부르면
    #    두 번째가 만들어 둔 요약을 꺼내 써서 LLM 을 아예 안 부르고, 그러면 예산 갈래가
    #    안 돈다. 여기서 재는 것은 "예산이 끝나면 폴백"이지 캐시가 아니다.
    first = (
        await client.get("/api/assistant/daily-summary", params={"date": "2026-07-28"}, headers=HEADERS)
    ).json()
    second = (
        await client.get("/api/assistant/daily-summary", params={"date": "2026-07-27"}, headers=HEADERS)
    ).json()

    assert first["meta"]["generated_by"] == "llm"
    assert second["meta"]["generated_by"] == "fallback"
    # 사유가 `no_api_key`로 나가면 로그를 보는 사람이 "키가 빠졌다"로 잘못 읽는다.
    assert second["meta"]["fallback_reason"] == "daily_budget"
    assert second["summary"].strip(), "예산이 끝났다고 문장이 비면 안 된다"
    assistant_router.reset_call_quota()


async def test_missing_key_is_logged_once(client, caplog):
    """⚠ 키가 비면 LLM을 아예 안 부르는데 응답만 봐서는 안 드러난다 — 로그로 남긴다.

    지금 EC2가 정확히 그 상태다(`GMS_API_KEY` 없음). `meta.generated_by`를 그리는 화면이
    아직 없어서, 배포 상태를 말해 주는 자리가 이 한 줄뿐이다.
    """
    assistant_router.reset_call_quota()
    with caplog.at_level(logging.WARNING, logger="c207.assistant"):
        for _ in range(3):
            await client.get(
                "/api/assistant/daily-summary", params={"date": "2020-01-01"}, headers=HEADERS
            )

    hits = [r for r in caplog.records if "GMS_API_KEY" in r.getMessage()]
    assert len(hits) == 1, f"한 번만 남겨야 한다 — {len(hits)}줄"
    assistant_router.reset_call_quota()


# ── ② 이상 상황 브리핑 ───────────────────────────────────────────────────

async def test_alert_brief_not_found(client):
    resp = await client.get("/api/assistant/alert-brief/999999", headers=HEADERS)
    assert resp.status_code == 404


async def test_alert_brief_huge_id_is_422_not_500(client):
    """⭐ int64를 넘는 번호가 검증을 지나 asyncpg DataError로 **500**이 되던 자리다.

    `alert.id`가 `BigInteger`다. "SELECT라 안전하다"는 이 저장소가 `/api/eta`에서 이미
    반증해 뒀다(`tests/test_eta.py`). 주소창 손입력·낡은 링크가 밟는다(축④ ①).
    """
    resp = await client.get(
        "/api/assistant/alert-brief/9223372036854775808", headers=HEADERS
    )
    assert resp.status_code == 422, resp.text
    # 경계값은 그대로 지나 404다(상한을 필요 이상으로 좁히지 않았나).
    edge = await client.get(
        "/api/assistant/alert-brief/9223372036854775807", headers=HEADERS
    )
    assert edge.status_code == 404, edge.text


async def test_alert_brief_carries_source_and_gate(client, auth_headers):
    alert = await _seed_untagged(client, auth_headers, event_id="AB1", gate_no=7)
    body = (await client.get(f"/api/assistant/alert-brief/{alert['id']}", headers=HEADERS)).json()

    assert body["alert_id"] == alert["id"]
    assert body["meta"]["generated_by"] == "fallback"
    facts = body["facts"]
    assert facts["gate_no"] == 7
    assert facts["source"]["event_id"] == "AB1"
    assert facts["source"]["verdict"] == "untagged"
    assert facts["recent_context"]["window_min"] == 60
    assert facts["recent_context"]["passes"] == 1
    assert facts["recent_context"]["untagged"] == 1
    assert facts["recent_context"]["gate_scoped_passes"] is True
    assert "7번 게이트" in body["brief"]


async def test_alert_brief_counts_prior_alerts_across_gates(client, auth_headers):
    """경고 수는 관제 전체 기준이다 — Alert엔 게이트 컬럼이 없어 게이트로 못 좁힌다.

    딴 게이트 경고도 세어지는 게 의도된 거동이고, 문장도 "관제 전체"로 말해야
    요원한테 거짓 사실("같은 게이트에서 N건")이 안 나간다. 쿨다운을 피하려 게이트를 나눠 쓴다.
    """
    await _seed_untagged(client, auth_headers, event_id="AB2", gate_no=11)
    later = await _seed_untagged(client, auth_headers, event_id="AB3", gate_no=12)

    body = (await client.get(f"/api/assistant/alert-brief/{later['id']}", headers=HEADERS)).json()
    assert body["facts"]["recent_context"]["alerts_all_gates"] == 1
    assert body["facts"]["prior_alerts"][0]["type"] == "untagged"
    assert "관제 전체에서 경고가 1건 더 있었습니다" in body["brief"]
    assert "같은 게이트에서 경고" not in body["brief"]


async def test_alert_brief_uses_llm_when_key_present(client, auth_headers, monkeypatch):
    alert = await _seed_untagged(client, auth_headers, event_id="AB4", gate_no=8)
    _mock_llm(monkeypatch, "8번 게이트에서 미태깅 통과가 감지되었습니다.")
    body = (await client.get(f"/api/assistant/alert-brief/{alert['id']}", headers=HEADERS)).json()
    assert body["brief"] == "8번 게이트에서 미태깅 통과가 감지되었습니다."
    assert body["meta"]["generated_by"] == "llm"


# ── ③ 관제 챗봇 ──────────────────────────────────────────────────────────

async def test_chat_answers_with_context(client, auth_headers, seed_robot):
    # ⛔ 예전에 `network_status="online"`을 심었는데 **계약에 없는 값이다**(`CommStatus`는
    # `WS_OK`·`POLLING_GRACE`·`SERVER_DOWN` 셋뿐). 그래서 폴백의 죽은 비교가 안 잡혔다
    # (2026-08-07 교차 검증). 실물 값으로 심고 아래에서 셈까지 잰다.
    await seed_robot(name="RB1-01", mode=RobotMode.OUTDOOR_TAGGING.value,
                     network_status=CommStatus.WS_OK.value)
    await _seed_untagged(client, auth_headers, event_id="CH1", gate_no=4)

    resp = await client.post(
        "/api/assistant/chat", json={"question": "지금 미확인 경고 몇 건인가요?"},
        headers=HEADERS,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["question"] == "지금 미확인 경고 몇 건인가요?"
    assert body["answer"].strip()
    assert body["meta"]["generated_by"] == "fallback"
    assert body["context"]["alerts"]["active"] == 1
    assert len(body["context"]["robots"]) == 1
    assert body["context"]["events"]["recent"][0]["kind"] == "gate_pass"
    assert "확인하지 않은 경고는 1건" in body["answer"]
    # ⭐ 붙은 로봇 셈이 창구를 지나서도 살아 있는가. 죽은 비교면 "0대"가 나온다.
    assert "1대가 서버에 붙어" in body["answer"], body["answer"]


async def test_chat_masks_tag_id_everywhere(client, auth_headers):
    """태그 UID 원문은 컨텍스트에도 답변에도 안 실린다(프롬프트로도 안 나간다)."""
    await client.post(
        "/api/tagging-events",
        json={
            "event_id": "CH2",
            "device_id": "reader01",
            "gate_no": 4,
            "tag_id": RAW_TAG,
            "observed_at": _now_iso(),
        },
        headers=auth_headers,
    )
    resp = await client.post("/api/assistant/chat", json={"question": "최근 태깅 알려주세요"}, headers=HEADERS)
    assert RAW_TAG not in resp.text
    tagging = [e for e in resp.json()["context"]["events"]["recent"] if e["kind"] == "tagging"]
    assert tagging and tagging[0]["tag_id"] == "AA***"


async def test_chat_uses_llm_and_sends_question(client, monkeypatch):
    captured = _mock_llm(monkeypatch, "현재 미확인 경고는 없습니다.")
    body = (
        await client.post("/api/assistant/chat", json={"question": "로봇 상태 어떤가요?"}, headers=HEADERS)
    ).json()

    assert body["answer"] == "현재 미확인 경고는 없습니다."
    assert body["meta"]["generated_by"] == "llm"
    assert "로봇 상태 어떤가요?" in captured.calls[0]["json"]["messages"][1]["content"]
    # 툴콜링은 절대 안 붙인다(정직 라인 — 읽기 전용 보조).
    assert "tools" not in captured.calls[0]["json"]
    assert "functions" not in captured.calls[0]["json"]


@pytest.mark.parametrize("question", ["", "x" * 501])
async def test_chat_rejects_bad_question(client, question):
    resp = await client.post("/api/assistant/chat", json={"question": question}, headers=HEADERS)
    assert resp.status_code == 422


async def test_chat_falls_back_when_llm_dies(client, monkeypatch):
    """LLM이 500을 뱉어도 화면은 안 빈다."""
    import httpx

    stub_llm(monkeypatch, lambda: httpx.Response(500, json={"error": "upstream"}))
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key="mock-key"),
    )
    body = (await client.post("/api/assistant/chat", json={"question": "상황"}, headers=HEADERS)).json()
    assert body["meta"]["generated_by"] == "fallback"
    assert body["meta"]["fallback_reason"] == "http_500"
    assert body["answer"].strip()
