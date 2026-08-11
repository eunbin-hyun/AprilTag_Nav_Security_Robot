"""관제 보조 폴백·프라이버시·파이프라인 갈래 (LLM 실호출 없음).

여기 케이스는 DB도 네트워크도 안 탄다 — 리포 /ai가 딕셔너리만 보고 도는 계층인지
확인하는 자리다. 못박는 것.
- 키가 없으면 호출 자체를 안 하고 폴백으로 간다(fallback_reason="no_api_key").
- 호출이 터져도 예외가 밖으로 안 새고 폴백 문장이 나간다.
- 태그 UID 원문은 프롬프트에 안 실린다.
- 폴백 문장은 집계한 수치를 실제로 담는다(빈 문자열·자리표시자 금지).
"""
from __future__ import annotations

import datetime as dt

import httpx
import pytest

from app.ai_bridge import AssistantConfig, mask_tag_id
from app.config import get_settings
from app.schemas import CommStatus, RobotMode
from ai.control_assistant import fallback, pipeline, prompts
from tests.assistant_stub import ok, stub_llm

ASYNCIO = pytest.mark.asyncio(loop_scope="session")

DAILY_FACTS = {
    "date": "2026-07-28",
    "events": {"tagging": 12, "gate_pass": 15, "shuttle_arrival": 3, "total": 30},
    "verdicts": {"normal": 10, "untagged": 3, "beam_incomplete": 2},
    "alerts": {"total": 5, "active": 2, "by_type": {"untagged": 5}},
    "gates": [
        {"gate_no": 1, "passes": 9, "untagged": 3},
        {"gate_no": 2, "passes": 6, "untagged": 0},
    ],
    "peak_hour": {"hour": 14, "events": 7},
    "robots": {"total": 2, "online": 1},
}

ALERT_FACTS = {
    "alert": {
        "id": 7,
        "type": "untagged",
        "severity": "high",
        "ack": False,
        "created_at": "2026-07-28T14:32:05+09:00",
    },
    "gate_no": 1,
    "source": {
        "type": "gate_pass",
        "id": 41,
        "event_id": "gp-0041",
        "gate_no": 1,
        "observed_at": "2026-07-28T14:32:00+09:00",
        "direction": "A_TO_B",
        "verdict": "untagged",
        "tag_id": None,
    },
    "recent_context": {
        "window_min": 60,
        "alerts_all_gates": 2,
        "passes": 9,
        "untagged": 3,
        "gate_scoped_passes": True,
    },
    "prior_alerts": [],
}

# ⛔ 예전 재료가 `network_status`에 `"online"`·`"offline"`을, `mode`에 `"AUTO"`를 심고
# 있었다. **셋 다 계약에 없는 값이다** — `CommStatus`는 `WS_OK`·`POLLING_GRACE`·
# `SERVER_DOWN` 셋이고 `RobotMode`는 `OUTDOOR_TAGGING`·`INDOOR_TAGGING`·
# `CHARGING_STATION`·`MANUAL` 넷이다. 그래서 폴백의 죽은 비교(`== "online"`)가
# **영원히 초록으로 통과**했다. 실물 값으로 돌리면 늘 0대였다(2026-08-07 교차 검증에서 잡힘).
#
# ⭐ 참과 거짓을 나란히 심는다 — 붙은 것 하나(`WS_OK`)와 안 붙은 것 하나(`SERVER_DOWN`)를
# 같이 두면 셈이 죽었을 때 "1대"가 "0대"로 드러난다. 한쪽만 있으면 못이 헛돈다.
CHAT_FACTS = {
    "robots": [
        {"name": "RB1-01", "mode": "OUTDOOR_TAGGING", "battery": 87,
         "network_status": CommStatus.WS_OK.value},
        {"name": "RB1-02", "mode": "MANUAL", "battery": 40,
         "network_status": CommStatus.SERVER_DOWN.value},
    ],
    "alerts": {"active": 2, "recent": []},
    "events": {
        "recent": [
            {"kind": "gate_pass", "gate_no": 1, "observed_at": "2026-07-28T14:32:00+09:00"}
        ]
    },
}

NO_KEY = AssistantConfig(api_key="")
WITH_KEY = AssistantConfig(api_key="dummy-not-a-real-key")


# ── 폴백 문장이 수치를 실제로 담나 ────────────────────────────────────────

def test_daily_fallback_carries_real_numbers():
    text = fallback.daily_summary(DAILY_FACTS)
    assert "2026-07-28" in text
    assert "30건" in text          # 전체 이벤트
    # ⚠ 문자열 리터럴로 둔다. fallback.VERDICT_LABEL을 import해서 비교하면 표를 바꿔도
    # 시험이 같이 따라가서 회귀 못이 헛돈다(2026-08-07 — 이 낱말이 화면과 어긋난 채
    # 통과하고 있었다).
    assert "무단 통과가 3건" in text   # 판정 분포
    assert "1번 게이트" in text        # 무단 통과 최다 게이트
    assert "14시" in text          # 최다 시간대
    assert "***" not in text       # 폴백에도 태그 자리가 안 샌다


def test_daily_fallback_says_none_when_quiet():
    quiet = {
        "date": "2026-07-28",
        "events": {"tagging": 0, "gate_pass": 0, "shuttle_arrival": 0, "total": 0},
        "verdicts": {},
        "alerts": {"total": 0, "active": 0, "by_type": {}},
        "gates": [],
        "peak_hour": None,
        "robots": {"total": 0, "online": 0},
    }
    text = fallback.daily_summary(quiet)
    assert "무단 통과로 판정된 통과는 없었습니다" in text
    assert text.strip()


def test_alert_fallback_has_time_gate_and_history():
    text = fallback.alert_brief(ALERT_FACTS)
    assert "14:32" in text
    assert "1번 게이트" in text
    assert "무단 통과 경고가 발생했습니다" in text
    assert "60분" in text


def test_alert_fallback_translates_robot_alert_types():
    """⭐ 알림 종류는 판정과 **다른 축**이다.

    `Alert.type`에는 `untagged`처럼 판정 표에도 있는 값이 오지만 `robot_mission_failed`처럼
    판정 표에 없는 값도 온다. 예전에는 판정 표(`VERDICT_LABEL`)만 봐서 그런 값이 **영어
    그대로** 요원 화면에 나갔다(2026-08-07 정정).

    ⚠ 참과 거짓을 나란히 둔다 — 판정 표에도 있는 값 하나만 재면 사슬이 안 돌아도 통과한다.
    위 케이스가 `untagged`(양쪽 표에 다 있음)라서 그것만으로는 못이 헛돈다.
    """
    for alert_type, expected in (
        ("robot_mission_failed", "로봇 주행 실패"),   # 판정 표에 없다 — 알림 표만 안다
        ("robot_weather_blocked", "날씨로 출동 보류"),
        ("shuttle_arrival_expired", "셔틀 신호 만료"),
    ):
        facts = {**ALERT_FACTS, "alert": {**ALERT_FACTS["alert"], "type": alert_type}}
        text = fallback.alert_brief(facts)
        assert expected in text, f"{alert_type}이 한국어로 안 옮겨졌다: {text}"
        assert alert_type not in text, f"영어 값이 그대로 샜다: {text}"


def test_alert_fallback_keeps_unknown_type_as_is():
    """⛔ 모르는 값이 오면 **지어내지 않고 그대로 적는다**(프론트 용어 정본 §0).

    그럴듯한 한국어를 만들면 요원이 없는 사실을 믿는다.
    """
    facts = {**ALERT_FACTS, "alert": {**ALERT_FACTS["alert"], "type": "some_new_kind"}}
    assert "some_new_kind" in fallback.alert_brief(facts)


def test_chat_fallback_reads_current_state():
    text = fallback.chat_answer(CHAT_FACTS)
    assert "2대" in text and "1대가 서버에 붙어" in text, text
    assert "확인하지 않은 경고는 2건" in text


def test_chat_fallback_answers_what_was_asked():
    """⛔ **폴백이 질문을 아예 안 보고 있었다**(2026-08-07 밤, 사용자가 실사용에서 잡음).

        요원 — "가장 최근에 누가 무단통과했어?"
        폴백 — "로봇은 1대가 등록되어 있고 그 가운데 0대가 서버에 붙어 있습니다…"

    사실은 다 맞는데 질문과 무관하다. 요원이 보기에는 못 알아들은 것과 같다.

    ⭐ 지어내는 것이 아니라 **있는 값의 순서를 바꾸고, 셀 수 있는 것을 센다.**
    `events.recent` 안의 `verdict` 를 세는 것은 조회 결과 요약이지 새 사실이 아니다.
    """
    facts = {
        "robots": [{"name": "RB1-01", "network_status": "WS_DISCONNECTED",
                    "updated_at": "2026-08-07T18:00:00+09:00"}],
        "alerts": {"active": 133, "recent": []},
        "events": {"recent": [
            {"observed_at": "2026-08-07T18:00:00+09:00", "kind": "gate_pass", "verdict": "untagged"},
            {"observed_at": "2026-08-07T17:52:00+09:00", "kind": "gate_pass", "verdict": "normal"},
            {"observed_at": "2026-08-07T17:40:00+09:00", "kind": "gate_pass", "verdict": "untagged"},
        ]},
    }

    asked = fallback.chat_answer(facts, "가장 최근에 누가 무단통과했어?")
    assert "무단 통과 2건" in asked, asked
    # ⭐ 물어본 것이 로봇 대수보다 앞에 온다.
    assert asked.index("경고는 133건") < asked.index("로봇은 1대"), asked

    # 로봇을 물으면 로봇이 앞이다.
    robot = fallback.chat_answer(facts, "로봇 어디 있어?")
    assert robot.index("로봇은 1대") < robot.index("경고는 133건"), robot

    # ⚠ 질문이 없으면 예전 그대로다 — 좁히다 빠뜨리는 쪽이 더 나쁘다.
    none_q = fallback.chat_answer(facts)
    assert none_q.index("로봇은 1대") < none_q.index("경고는 133건")
    assert "무단 통과 2건" not in none_q

    # ⛔ 첫 문장은 여전히 사과문이 아니다(모듈 머리 계약).
    for t in (asked, robot, none_q):
        assert t.startswith("질문하신 시점의 관제 상태를"), t[:60]


def test_chat_fallback_does_not_invent_verdicts():
    """⚠ 판정 칸이 없는 이벤트만 있으면 세지 않는다. 없는 것을 지어내면 안 된다."""
    facts = {
        "robots": [], "alerts": {"active": 5, "recent": []},
        "events": {"recent": [{"observed_at": "2026-08-07T18:00:00+09:00",
                               "kind": "shuttle_arrival"}]},
    }
    text = fallback.chat_answer(facts, "최근 무단 통과 있었어?")
    assert "판정은" not in text, text


def test_chat_fallback_counts_online_by_real_contract_value():
    """⛔ 붙은 로봇을 셀 때 쓰는 낱말이 **계약에 있는 값**인가.

    예전에는 `"online"`과 비교해서 로봇이 붙어 있어도 늘 0대였다. 시험 재료도 같은 가짜
    낱말을 심고 있어서 죽은 비교가 영원히 통과했다(2026-08-07).

    ⚠ 여기서만 enum을 import해 대조한다. `ai/` 패키지는 백엔드를 모르는 게 계약이라
    그쪽에 리터럴로 두고, **값이 바뀌면 이 시험이 빨개진다.**
    """
    assert CommStatus.WS_OK.value in fallback.ONLINE_STATUS
    assert CommStatus.SERVER_DOWN.value not in fallback.ONLINE_STATUS
    assert CommStatus.POLLING_GRACE.value not in fallback.ONLINE_STATUS
    # ⚠ 짧은 옛 이름도 받는다. 화면도 둘 다 받는다(index.html:13093).
    assert "OK" in fallback.ONLINE_STATUS

    # 참·거짓을 나란히 — 붙은 것 하나, 안 붙은 것 하나.
    both = fallback.chat_answer(CHAT_FACTS)
    assert "1대가 서버에 붙어" in both, both

    # 전부 안 붙은 판은 0대로 나와야 한다. 셈이 죽으면 위 케이스와 같은 값이 나온다.
    none_up = {
        **CHAT_FACTS,
        "robots": [
            {**r, "network_status": CommStatus.SERVER_DOWN.value}
            for r in CHAT_FACTS["robots"]
        ],
    }
    assert "0대가 서버에 붙어" in fallback.chat_answer(none_up)


def test_chat_fallback_does_not_trust_stale_ws_ok():
    """⛔⛔ 값이 `WS_OK`라도 **마지막 보고가 오래됐으면** 붙어 있는 게 아니다.

    2026-08-07 프론트 실측 — 로봇이 8시간 51분째 안 붙었는데 `network_status`가 `WS_OK`로
    남아 있었고, 그 판에서 브리핑이 "1대가 온라인입니다"라고 적었다. 같은 시각 화면은
    "신호 지연"이라 적었다. **두 곳이 갈렸다.**

    ⚠ 문턱은 화면과 같은 25초다(`index.html:6287` `HEARTBEAT_MAX_S + 5`).
    우리만 다른 값을 쓰면 또 갈린다.
    """
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8, minutes=51)
    stale = {
        **CHAT_FACTS,
        "robots": [{**CHAT_FACTS["robots"][0], "updated_at": old.isoformat()}],
    }
    text = fallback.chat_answer(stale)
    assert "0대가 서버에 붙어" in text, f"오래된 WS_OK 를 붙어 있다고 셌다: {text}"

    # 방금 보고한 판은 그대로 세야 한다. 문턱이 너무 빡빡하면 정상도 0대가 된다.
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    fresh = {**CHAT_FACTS,
             "robots": [{**CHAT_FACTS["robots"][0], "updated_at": now}]}
    assert "1대가 서버에 붙어" in fallback.chat_answer(fresh)

    # `updated_at`이 아예 없으면 낱말만 믿는다 — 전부 0대로 떨어뜨리는 것도 틀린 사실이다.
    assert "1대가 서버에 붙어" in fallback.chat_answer(
        {**CHAT_FACTS, "robots": [CHAT_FACTS["robots"][0]]}
    )


def test_stale_threshold_gap_with_server_is_recorded():
    """✅ 폴백과 서버 집계가 **같은 문턱**을 쓴다. 갈리면 여기서 빨개진다.

    ⛔ **한때 갈려 있었다**(2026-08-07). 서버가 `ws_heartbeat_sec + 5` = 20초를 썼고
    폴백은 25초였다. 주석에는 "화면과 같은 식"이라 적혀 있었지만 실제로는 **방향이 반대인
    설정**이었다.

        ws_heartbeat_sec   서버 → 로봇   서버가 얼마나 자주 찌르나
        robot_stale_sec    로봇 → 서버   로봇 보고가 얼마나 뜸하면 끊긴 걸로 보나

    그래서 20~25초 구간에서 일일 요약과 챗봇이 **다른 대수**를 말했다. 갈림을 없애려던
    수리가 갈림을 한 칸 늘린 셈이다.

    ⭐ 지금은 `robot_stale_sec`(25초) 하나를 본다. 화면 `STALE_S = HEARTBEAT_MAX_S(20) + 5`
    와 같은 값이다. **"같은 식"이 아니라 "같은 값"이어야 한다** — 식이 같아 보여도 넣는
    설정이 다르면 결과가 갈린다.
    """
    server_sec = get_settings().robot_stale_sec
    assert fallback.STALE_SEC == 25, "화면 문턱(HEARTBEAT_MAX_S + 5)에서 벗어났다"
    assert server_sec == fallback.STALE_SEC, (
        f"서버 문턱이 {server_sec}초인데 폴백은 {fallback.STALE_SEC}초다 — "
        "둘이 갈리면 20~25초 구간에서 일일 요약과 챗봇이 다른 대수를 말한다"
    )


def test_chat_facts_use_only_contract_values():
    """⛔ 시험 재료가 계약 밖 낱말을 심으면 못이 헛돈다.

    이 파일이 그 사고를 냈다 — `network_status="online"`·`mode="AUTO"`가 실물에 없는
    값이라, 그 값을 읽는 코드가 죽어 있어도 시험이 초록이었다.
    """
    comm = {c.value for c in CommStatus}
    modes = {m.value for m in RobotMode}
    for r in CHAT_FACTS["robots"]:
        assert r["network_status"] in comm, f"계약 밖 통신 상태: {r['network_status']}"
        assert r["mode"] in modes, f"계약 밖 로봇 목적지: {r['mode']}"


def test_chat_fallback_does_not_apologize():
    """⭐ 폴백 문장은 사과문이 아니다(모듈 머리 계약).

    브리핑·경고 폴백은 집계 문장으로 시작하는데 챗봇만 "지금은 자동 요약을 만들지 못해"로
    시작했다. GMS 키가 없는 판(지금 EC2가 그 상태다)에서 챗봇을 열면 첫 화면이 실패
    고백이 된다. 폴백이라는 사실은 화면이 `meta.generated_by`로 읽는다.
    """
    failed = {**ALERT_FACTS, "alert": {**ALERT_FACTS["alert"], "type": "robot_mission_failed"}}
    for text in (
        fallback.chat_answer(CHAT_FACTS),
        fallback.daily_summary(DAILY_FACTS),
        fallback.alert_brief(ALERT_FACTS),
        fallback.alert_brief(failed),   # ⭐ "로봇 주행 실패"가 들어가는 판도 같이 잰다
    ):
        assert "못해" not in text and "못했" not in text, text
        assert "죄송" not in text, text
        # ⚠ 예전에는 `"실패" not in text`였는데, 알림 종류에 `robot_mission_failed`가 있고
        # 화면 표기가 "로봇 주행 실패"라 그 낱말을 통째로 막으면 **사실 서술까지** 막힌다.
        # 그래서 낱말이 아니라 **어미로** 좁혔다 — 사과문("…생성에 실패하여", "…실패했습니다")은
        # 다시 걸리고 명사형 사실 서술("로봇 주행 실패 경고가")은 안 걸린다(2026-08-07).
        assert "실패하여" not in text and "실패했습니다" not in text, text


def test_prompt_and_fallback_use_the_same_words():
    """⛔ 낱말이 **두 자리**에 있다. 한쪽만 고치면 조용히 갈라진다.

    LLM이 붙은 판은 `prompts.py` 용어집을 쓰고, 안 붙은 판은 `fallback.py` 표를 쓴다.
    실제 배포에서 요원 눈에 닿는 건 프롬프트 쪽인데 거기엔 못이 하나도 없었다(2026-08-07).

    ⚠ 낱말 자체가 맞는지는 위 리터럴 케이스들이 지킨다. 여기서 지키는 건 "두 벌이 같다"다.
    """
    rules = prompts._COMMON_RULES
    for table in (fallback.VERDICT_LABEL, fallback.KIND_LABEL, fallback.ALERT_LABEL):
        for key, word in table.items():
            assert f"{key}={word}" in rules, f"프롬프트 용어집에 없거나 다르다: {key}={word}"


# ── 프라이버시 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [("A1B2C3D4", "A1***"), ("AB", "***"), ("A", "***"), ("", None), (None, None)],
)
def test_mask_tag_id(raw, expected):
    assert mask_tag_id(raw) == expected


def test_prompt_never_carries_raw_tag_id():
    """마스킹된 값만 사실 딕셔너리에 담기므로 프롬프트에도 원문이 안 나온다."""
    facts = dict(ALERT_FACTS)
    facts["source"] = dict(ALERT_FACTS["source"], tag_id=mask_tag_id("A1B2C3D4"))
    user = prompts.alert_brief_user(facts)
    assert "A1B2C3D4" not in user
    assert "A1***" in user


def test_system_prompts_forbid_robot_control():
    for system in (
        prompts.DAILY_SUMMARY_SYSTEM,
        prompts.ALERT_BRIEF_SYSTEM,
        prompts.CHAT_SYSTEM,
    ):
        assert "로봇 제어" in system
        assert "지어내지 않습니다" in system


# ── 파이프라인 갈래 ───────────────────────────────────────────────────────

@ASYNCIO
async def test_no_api_key_goes_fallback_without_calling(monkeypatch):
    """키가 없으면 네트워크를 아예 안 탄다. 요청이 한 건이라도 나가면 여기서 잡힌다."""

    def _boom():
        raise AssertionError("키가 없는데 LLM을 불렀다")

    shim = stub_llm(monkeypatch, _boom)
    result = await pipeline.daily_summary(NO_KEY, DAILY_FACTS)
    assert shim.calls == []
    assert result.generated_by == "fallback"
    assert result.fallback_reason == "no_api_key"
    assert "2026-07-28" in result.text


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (httpx.ConnectTimeout("느림"), "timeout"),
        (httpx.ConnectError("끊김"), "transport_error"),
    ],
)
@ASYNCIO
async def test_transport_failures_fall_back(monkeypatch, raised, reason):
    def _fail():
        raise raised

    stub_llm(monkeypatch, _fail)
    result = await pipeline.alert_brief(WITH_KEY, ALERT_FACTS)
    assert result.generated_by == "fallback"
    assert result.fallback_reason == reason
    assert "1번 게이트" in result.text


@pytest.mark.parametrize(
    ("status_code", "payload", "reason"),
    [
        (403, {"error": "forbidden"}, "http_403"),
        (400, {"error": "bad"}, "http_400"),
        (200, {"choices": []}, "bad_response_shape"),
        (200, {"choices": [{"message": {"content": "   "}}]}, "empty_response"),
    ],
)
@ASYNCIO
async def test_bad_responses_fall_back(monkeypatch, status_code, payload, reason):
    stub_llm(monkeypatch, lambda: httpx.Response(status_code, json=payload))
    result = await pipeline.chat(WITH_KEY, "지금 상황 어때요", CHAT_FACTS)
    assert result.generated_by == "fallback"
    assert result.fallback_reason == reason
    assert result.text.strip()


@ASYNCIO
async def test_llm_success_marks_generated_by_llm(monkeypatch):
    shim = stub_llm(monkeypatch, ok("  오늘은 조용했습니다.  "))
    result = await pipeline.daily_summary(WITH_KEY, DAILY_FACTS)

    assert result.generated_by == "llm"
    assert result.fallback_reason is None
    assert result.text == "오늘은 조용했습니다."  # 앞뒤 공백은 걷어낸다

    sent = shim.calls[0]
    assert sent["url"] == (
        "https://gms.ssafy.io/gmsapi/api.openai.com/v1/chat/completions"
    )
    assert sent["json"]["model"] == "gpt-5.4-mini"
    # 이 프록시는 reasoning_effort를 400으로 거부한다(1학기 실측) — 절대 안 실린다.
    assert "reasoning_effort" not in sent["json"]
    assert sent["headers"]["Authorization"] == "Bearer dummy-not-a-real-key"


@ASYNCIO
async def test_reasoning_family_payload_shape(monkeypatch):
    """gpt-5 계열엔 max_completion_tokens만 실리고 max_tokens·temperature는 안 실린다(7/29 실측 계약)."""
    shim = stub_llm(monkeypatch, ok("보고입니다."))
    await pipeline.daily_summary(WITH_KEY, DAILY_FACTS)

    sent = shim.calls[0]["json"]
    assert sent["max_completion_tokens"] == WITH_KEY.max_tokens
    assert "max_tokens" not in sent
    assert "temperature" not in sent


@ASYNCIO
async def test_non_reasoning_model_keeps_legacy_payload(monkeypatch):
    """gpt-4 계열로 돌리면 예전 그대로 max_tokens·temperature가 실린다."""
    shim = stub_llm(monkeypatch, ok("보고입니다."))
    legacy = AssistantConfig(api_key="dummy-not-a-real-key", model="gpt-4o-mini")
    await pipeline.daily_summary(legacy, DAILY_FACTS)

    sent = shim.calls[0]["json"]
    assert sent["max_tokens"] == legacy.max_tokens
    assert sent["temperature"] == 0.2
    assert "max_completion_tokens" not in sent
