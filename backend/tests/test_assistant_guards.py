"""LLM이 낸 문장을 내보내기 전에 거르는 사후 검사 (2026-08-07 신설).

여기 케이스는 DB도 네트워크도 안 탄다. 못박는 것.
- 중국어(한자)가 섞이면 폴백으로 떨어뜨린다 — 실서버에서 실제로 샌 자리다.
- 사실에 없는 수를 지어내면 폴백으로 떨어뜨린다.
- 정상 문장은 안 걸린다 (문턱이 빡빡하면 쓸 만한 답이 다 규칙 문장으로 바뀐다).
- 연달아 실패하면 LLM을 아예 안 부른다.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.schemas import CommStatus
from ai.control_assistant import guards, pipeline, prompts
from ai.control_assistant.config import AssistantConfig
from ai.control_assistant.client import LLMUnavailable
# ⛔ `stub_llm` 의 둘째 인자는 **부를 수 있는 것**이다. 문자열을 그냥 넘기면
# `TypeError: 'str' object is not callable` 이 난다 — 정상 응답은 `ok()` 로 감싼다.
from tests.assistant_stub import ok, stub_llm

ASYNCIO = pytest.mark.asyncio(loop_scope="session")

FACTS = {
    "date": "2026-08-07",
    "events": {"tagging": 12, "gate_pass": 15, "shuttle_arrival": 3, "total": 30},
    "verdicts": {"normal": 10, "untagged": 3, "beam_incomplete": 2, "exit": 0},
    "alerts": {"total": 5, "active": 2, "by_type": {"untagged": 5}},
    "gates": [{"gate_no": 5, "passes": 15, "untagged": 3}],
    "peak_hour": {"hour": 14, "events": 7},
    "robots": {"total": 1, "online": 0},
}
WITH_KEY = AssistantConfig(api_key="dummy-not-a-real-key")


@pytest.fixture(autouse=True)
def _clean_breaker():
    """⚠ 차단기가 프로세스 상태라 케이스 사이에 새면 뒤 케이스가 가짜로 실패한다."""
    pipeline.reset_breaker()
    yield
    pipeline.reset_breaker()


# ── ① 한자 누출 ──────────────────────────────────────────────────────────

def test_hanja_leak_is_caught():
    """⛔ 2026-08-07 실서버에서 실제로 나온 문장이다.

    Qwen 계열이 중국어 자료로 배운 모델이라 한국어를 쓰다가 한자어 자리에서 중국어가 샌다.
    """
    real = "현재 제공된 자료에는 로그인한 사용자의 身份信息나 역할 정보가 포함되어 있지 않습니다."
    assert guards.has_hanja(real)
    assert guards.check(real, FACTS) == "hanja_leak"


@pytest.mark.parametrize("text", [
    "무단 통과가 3건 나왔습니다.",
    "로봇은 1대가 등록되어 있고 그 가운데 0대가 서버에 붙어 있습니다.",
    "2026-08-07 관제 요약입니다. 전체 이벤트는 30건입니다.",
    "14:32에 5번 게이트에서 무단 통과 경고가 발생했습니다.",
])
def test_normal_korean_passes(text):
    """⚠ 문턱이 빡빡하면 쓸 만한 답이 전부 규칙 문장으로 바뀐다. 정상은 안 걸려야 한다."""
    assert guards.check(text, FACTS) is None, text


def test_english_only_answer_is_caught():
    assert guards.check("There are 3 untagged passes today.", FACTS) == "not_korean"


def test_empty_answer_is_caught():
    assert guards.check("   ", FACTS) == "empty_response"


# ── ② 숫자 조작 ──────────────────────────────────────────────────────────

def test_invented_number_is_caught():
    """사실에 없는 수를 지어내면 잡는다."""
    bad = "무단 통과가 47건 나왔습니다."
    assert guards.ungrounded_numbers(bad, FACTS) == ["47"]
    assert guards.check(bad, FACTS) == "number_mismatch"


def test_computed_number_is_caught():
    """⛔ 직접 계산한 수도 잡는다 — 10 + 3 + 2 = 15 는 사실에 없는 값이다.

    ⚠ 15 는 `gate_pass`·`gates[0].passes` 로 사실에 있으니 다른 수를 쓴다.
    """
    assert guards.ungrounded_numbers("판정 합계는 20건입니다.", FACTS) == ["20"]


def test_numbers_from_facts_pass():
    """사실에 있는 수는 어디서 왔든 통과한다(중첩·목록 안까지 훑는다)."""
    ok = "게이트 통과 15건 가운데 무단 통과가 3건이고 경고는 5건입니다."
    assert guards.ungrounded_numbers(ok, FACTS) == []


def test_time_and_date_are_not_treated_as_facts():
    """⚠ 시각·날짜·비율은 대조에서 뺀다. 안 빼면 정상 문장이 다 걸린다."""
    for text in ("14:32에 발생했습니다.",
                 "2026년 8월 7일 요약입니다.",
                 "17시 25분에 마지막 보고가 있었습니다.",
                 "배터리는 90%입니다."):
        assert guards.ungrounded_numbers(text, FACTS) == [], text


def test_zero_and_one_are_not_counted():
    """0·1 은 세는 말로 자연스럽게 나온다. 그것까지 잡으면 정상 문장이 떨어진다."""
    assert guards.ungrounded_numbers("로봇 1대가 있고 경고는 0건입니다.", FACTS) == []


def test_bool_is_not_read_as_number():
    """⛔ True/False 가 1/0 으로 읽히면 사실에 없는 근거가 생긴다."""
    known = set()
    guards._collect_numbers({"ack": True, "n": 7}, known)
    assert known == {"7"}


# ── ③ 회로 차단기 ────────────────────────────────────────────────────────

def test_breaker_opens_after_threshold():
    b = guards.CircuitBreaker(threshold=3, open_sec=60)
    assert not b.is_open(0)
    b.record_failure(0)
    b.record_failure(1)
    assert not b.is_open(2), "문턱 전에 열렸다"
    b.record_failure(2)
    assert b.is_open(3), "문턱을 넘었는데 안 열렸다"


def test_breaker_closes_after_window():
    b = guards.CircuitBreaker(threshold=1, open_sec=60)
    b.record_failure(0)
    assert b.is_open(59)
    assert not b.is_open(61), "창이 지났는데 안 닫혔다"


def test_breaker_resets_on_success():
    b = guards.CircuitBreaker(threshold=2, open_sec=60)
    b.record_failure(0)
    b.record_success()
    b.record_failure(1)
    assert not b.is_open(2), "성공 뒤에 실패 수가 안 초기화됐다"


# ── ④ 파이프라인에 실제로 걸리나 ─────────────────────────────────────────

@ASYNCIO
async def test_pipeline_falls_back_on_hanja(monkeypatch):
    """⭐ 배선 검사 — 검사기가 있어도 파이프라인이 안 부르면 헛것이다."""
    stub_llm(monkeypatch, ok("로그인한 사용자의 身份信息를 확인할 수 없습니다."))
    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.generated_by == "fallback"
    assert r.fallback_reason == "hanja_leak"
    assert "身份" not in r.text, "폴백으로 갔는데 한자가 남았다"


@ASYNCIO
async def test_pipeline_falls_back_on_invented_number(monkeypatch):
    stub_llm(monkeypatch, ok("오늘 무단 통과가 47건 나왔습니다."))
    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.generated_by == "fallback"
    assert r.fallback_reason == "number_mismatch"


@ASYNCIO
async def test_pipeline_keeps_good_answer(monkeypatch):
    """⚠ 좋은 답까지 떨어뜨리면 로컬 LLM 을 붙인 뜻이 없다."""
    good = "2026-08-07 관제 요약입니다. 무단 통과가 3건 나왔습니다."
    stub_llm(monkeypatch, ok(good))
    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.generated_by == "llm"
    assert r.text == good


def test_stale_robot_is_annotated_for_the_model():
    """⛔⛔ **모델은 지금 몇 시인지 모른다.**

    `updated_at`이 "2026-08-06T17:25"라는 건 읽지만 그게 방금인지 열세 시간 전인지 알
    방법이 없다. 그런데 프롬프트로 "25초보다 오래됐으면 붙어 있다고 쓰지 마라"를 시켰다 —
    **애초에 못 지킬 규칙이었다.**

    2026-08-07 실사용에서 그대로 터졌다. 열세 시간 전 보고를 두고 이렇게 적었다.

        "현재 로봇 RB1-01의 통신 상태는 정상이며, 마지막 보고 시간은 8월 6일 17시 25분입니다."

    같은 순간 폴백은 "마지막 보고가 오래됐습니다"라고 옳게 말했다. 폴백은 코드가 계산하고
    LLM은 프롬프트가 시켰기 때문이다. 그래서 계산을 코드로 옮겼다.
    """
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=13)).isoformat()
    facts = {"robots": [{"name": "RB1-01", "network_status": CommStatus.WS_OK.value,
                         "updated_at": old}]}
    user = prompts.chat_user("로봇 상태 알려줘", facts)
    assert '"지금 상태": "서버에 붙어 있지 않습니다' in user, user[:300]
    assert "13시간 전" in user, user[:300]

    # ⚠ 원본은 안 건드린다 — 사후 검사기가 원본 숫자로 대조하고 폴백도 원본을 본다.
    assert "지금 상태" not in facts["robots"][0]

    now = dt.datetime.now(dt.timezone.utc).isoformat()
    fresh = {"robots": [{"name": "RB1-01", "network_status": CommStatus.WS_OK.value,
                         "updated_at": now}]}
    assert '"지금 상태": "서버에 붙어 있습니다"' in prompts.chat_user("q", fresh)


def test_robot_with_no_status_field_is_not_called_disconnected():
    """⛔ **안 실려 온 칸은 "아니다"가 아니다.**

    `_is_up` 은 `network_status` 가 비면 거짓을 돌려준다. 폴백에서는 그게 맞다 — 모를 때
    나쁜 쪽으로 기울어야 요원이 "괜찮다"고 읽고 넘어가지 않는다. 그런데 그 값을 그대로
    문장으로 옮기면 **모르는 것을 끊긴 것으로 단정**하게 된다.

    ⭐ 화면도 같은 잣대를 쓴다(프론트 11차). `reachable` 이 `null` 이면 회색으로 "지금
    닿는지는 확인하지 못했습니다"라 적는다. 화면이 "모른다"인데 챗봇이 "끊겼습니다"라
    하면 요원이 보는 두 자리가 서로 다른 말을 한다.

    ⚠ 세 갈래를 나란히 둔다 — 한쪽만 재면 오판이 정상으로 읽힌다.
    """
    now = dt.datetime.now(dt.timezone.utc).isoformat()

    # ① 칸이 아예 없다 → 모른다
    unknown = {"robots": [{"name": "RB1-01", "updated_at": now}]}
    got = prompts.chat_user("로봇 상태 알려줘", unknown)
    assert '"지금 상태": "지금 붙어 있는지는 확인하지 못했습니다"' in got, got[:300]
    assert "붙어 있지 않습니다" not in got, "모르는 것을 끊긴 것으로 단정했다"

    # ② 칸이 있고 나쁘다 → 끊겼다
    down = {"robots": [{"name": "RB1-01", "network_status": "WS_DISCONNECTED",
                        "updated_at": now}]}
    assert '"지금 상태": "서버에 붙어 있지 않습니다' in prompts.chat_user("q", down)

    # ③ 칸이 있고 좋다 → 붙어 있다
    up = {"robots": [{"name": "RB1-01", "network_status": CommStatus.WS_OK.value,
                      "updated_at": now}]}
    assert '"지금 상태": "서버에 붙어 있습니다"' in prompts.chat_user("q", up)


def test_top_verdict_is_computed_not_left_to_the_model():
    """⛔ 프롬프트로 "가장 많은 하나는 이름으로 적어라"를 시켰는데 판마다 갈렸다.

    200건이 전부 `beam_incomplete` 인 날에 모델이 "정상 통과와 퇴장은 없었습니다"라고
    **없는 것을 나열**했다. 요원이 읽으면 200건의 정체를 모른다.

    ⭐ 모델은 영어 열쇠를 한국어로 옮기고, 넷을 견줘 최댓값을 고르고, 문장으로 만드는
    세 가지를 매번 다시 해야 했다. 코드가 계산해 주면 옮겨 적기만 하면 된다.
    """
    facts = {"events": {"total": 200, "gate_pass": 200},
             "verdicts": {"normal": 0, "untagged": 0, "beam_incomplete": 200, "exit": 0}}
    user = prompts.daily_summary_user(facts)
    assert "한쪽 감지가 200건으로 가장 많았습니다" in user, user[:400]

    # ⚠ 원본은 안 건드린다 — 사후 검사기가 원본 수로 대조한다.
    assert "요약에 꼭 쓸 문장" not in facts["verdicts"]

    # 여럿이 섞인 날에는 최댓값 하나만 고른다.
    mixed = {"verdicts": {"normal": 120, "untagged": 3, "beam_incomplete": 8, "exit": 40}}
    assert "정상 통과가 120건으로 가장 많았습니다" in prompts.daily_summary_user(mixed)

    # 전부 0인 날에는 아무것도 안 붙인다 — 없는 것을 지어내면 안 된다.
    zero = {"verdicts": {"normal": 0, "untagged": 0, "beam_incomplete": 0, "exit": 0}}
    assert "가장 많았습니다" not in prompts.daily_summary_user(zero)


def test_chat_prompt_does_not_show_internal_words():
    """⛔ 규칙보다 **본보기**가 세다.

    규칙 줄에서 "조회 데이터 같은 내부 낱말을 쓰지 마라"고 시켜 놓고, 정작 사용자 메시지
    머리말이 "조회 데이터:"였다. 모델이 그대로 따라 "현재 조회 데이터에는…"으로 답했다.
    """
    user = prompts.chat_user("q", {"robots": []})
    assert "조회 데이터" not in user, user[:200]
    assert "지금 관제 상태" in user


@ASYNCIO
async def test_truncated_answer_is_caught(monkeypatch):
    """⛔ **잘린 답도 200으로 온다.**

    상한에 걸려 문장이 중간에 끊겨도 HTTP는 정상이고 `content`도 비어 있지 않다. 그래서
    "…무단 통과가 3건이고 경고는" 처럼 끊긴 문장이 그대로 요원에게 나간다.

    ⚠ 대화 기록 3턴을 붙이면 400토큰이 확실히 모자라진다. 붙이기 전에 박아 두는 못이다.
    """
    # ⛔ 예전에는 `llm_client.httpx.AsyncClient` 를 lambda 로 갈아 끼웠는데, lambda 안에서
    # 부르는 `httpx.AsyncClient` 가 **방금 갈아 끼운 자기 자신**이라 재귀로 들어가
    # `transport` 가 두 번 실렸다(TypeError). stub 을 쓰면 그 함정이 없다.
    stub_llm(monkeypatch, ok("무단 통과가 3건이고 경고는", finish_reason="length"))
    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.generated_by == "fallback"
    assert r.fallback_reason == "truncated", r.fallback_reason


@ASYNCIO
async def test_pipeline_opens_breaker_and_skips_call(monkeypatch):
    """⛔ 연달아 실패하면 **부르지도 않는다.**

    역터널이 끊기면 요청마다 타임아웃 20초를 꽉 채우는데, 어차피 결과가 폴백이면
    처음부터 폴백으로 가는 편이 낫다.
    """
    calls = {"n": 0}

    async def boom(cfg, *, system, user, history=None):
        calls["n"] += 1
        raise LLMUnavailable("timeout")

    monkeypatch.setattr(pipeline, "complete", boom)

    for _ in range(3):
        r = await pipeline.daily_summary(WITH_KEY, FACTS)
        assert r.fallback_reason == "timeout"
    assert calls["n"] == 3

    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.fallback_reason == "circuit_open"
    assert calls["n"] == 3, "차단기가 열렸는데도 LLM 을 불렀다"


@ASYNCIO
async def test_no_key_reports_no_api_key_even_when_breaker_is_open(monkeypatch):
    """⛔⛔ **키가 없는 것을 차단기가 실패로 세고 있었다** (2026-08-07 전량 시험이 잡음).

    차단기는 "LLM 서버가 죽었나"를 재는 물건이다. 키가 없는 건 서버 문제가 아닌데도
    `no_api_key`가 실패로 쌓여, 세 번째 호출부터 사유가 `circuit_open`으로 바뀌어 나갔다.
    **키 없는 배포에서 화면이 요원에게 틀린 원인을 적어 준다.**

    ⚠ 이 결함은 **단독으로 돌리면 안 걸린다.** 전량에서 앞 케이스가 차단기를 열어 둘 때만
    드러난다. 그래서 여기서 차단기를 일부러 열어 놓고 잰다.
    """
    async def boom(cfg, *, system, user, history=None):
        raise LLMUnavailable("timeout")

    monkeypatch.setattr(pipeline, "complete", boom)

    # 차단기를 연다.
    for _ in range(3):
        await pipeline.daily_summary(WITH_KEY, FACTS)
    assert (await pipeline.daily_summary(WITH_KEY, FACTS)).fallback_reason == "circuit_open"

    # ⭐ 키가 없으면 차단기와 무관하게 `no_api_key`다.
    no_key = AssistantConfig(api_key="")
    r = await pipeline.daily_summary(no_key, FACTS)
    assert r.fallback_reason == "no_api_key", r.fallback_reason
    assert r.text.strip()


@ASYNCIO
async def test_no_key_does_not_open_the_breaker(monkeypatch):
    """⚠ 키가 없는 호출은 차단기 계수를 올리지 않는다.

    올리면 키를 넣은 직후에도 60초 동안 LLM 을 안 부른다.
    """
    no_key = AssistantConfig(api_key="")
    for _ in range(5):
        assert (await pipeline.daily_summary(no_key, FACTS)).fallback_reason == "no_api_key"

    called = {"n": 0}

    async def fine(cfg, *, system, user, history=None):
        called["n"] += 1
        return "2026-08-07 관제 요약입니다. 무단 통과가 3건 나왔습니다."

    monkeypatch.setattr(pipeline, "complete", fine)
    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert called["n"] == 1, "키를 넣었는데 차단기가 열려 있어 안 불렀다"
    assert r.generated_by == "llm"


def test_breaker_window_is_short_enough_for_a_demo():
    """⛔ 60초는 시연에 길다 (2026-08-07 시연 리스크 점검).

    잠깐 끊겼다 돌아와도 요원이 그 시간 내내 규칙 문장만 본다. 실측으로 재현했다.
    ⚠ 이 못은 값이 조용히 다시 늘어나는 것을 막는다.
    """
    from ai.control_assistant import client as llm_client

    b = guards.CircuitBreaker()
    assert b.open_sec <= 30.0, f"차단 창이 {b.open_sec}초다 — 시연에 길다"
    assert llm_client.CONNECT_TIMEOUT_SEC <= 3.0, "연결 상한이 길면 죽은 서버를 찌를 때 화면이 멈춘다"


@ASYNCIO
async def test_unreachable_server_fails_fast(monkeypatch):
    """⛔⛔ 역터널이 끊긴 상태에서 **첫 실패에 20초를 꽉 채웠다**(2026-08-07 실측).

    연결과 읽기에 같은 상한을 걸어서다. 연결은 같은 호스트라 순식간이어야 하고,
    3초를 넘기면 느린 게 아니라 안 닿는 것이다.

    ⚠ 여기서는 실제 연결을 안 한다 — httpx 에 넘어가는 Timeout 모양만 본다.
    """
    import httpx

    from ai.control_assistant import client as llm_client

    seen = {}

    class _Client:
        def __init__(self, *a, **kw):
            seen.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *e):
            return False

        async def post(self, url, *, json, headers):
            raise httpx.ConnectTimeout("안 닿는다")

    # ⚠ 클라이언트가 `httpx.` 로 쓰는 이름을 전부 들고 있어야 한다(assistant_stub 와 같은 계약).
    shim = type("S", (), {"TimeoutException": httpx.TimeoutException,
                          "HTTPError": httpx.HTTPError, "Timeout": httpx.Timeout,
                          "AsyncClient": _Client})()
    monkeypatch.setattr(llm_client, "httpx", shim)

    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.fallback_reason == "timeout"

    t = seen.get("timeout")
    assert isinstance(t, httpx.Timeout), f"Timeout 객체가 안 넘어갔다: {t!r}"
    assert t.connect <= 3.0, f"연결 상한이 {t.connect}초다"
    assert t.read == WITH_KEY.timeout_sec, "읽기 상한이 설정값과 다르다"


@ASYNCIO
async def test_history_goes_after_system_not_before(monkeypatch):
    """⭐ 이력은 system 다음, 지금 질문 앞이다.

    이 순서라야 **system 몫이 프롬프트 캐시에 그대로 남는다.** 이력을 맨 앞에 두면
    턴이 바뀔 때마다 캐시가 통째로 깨져, 프리필을 줄이려던 수리가 헛것이 된다
    (2026-08-07 실측 — 같은 system 으로 두 번 부르면 프리필이 2,562 → 577토큰이다).
    """
    shim = stub_llm(monkeypatch, ok("오늘 무단 통과가 3건입니다."))
    await pipeline.chat(WITH_KEY, "그럼 어제는?", FACTS, history=[
        {"role": "user", "content": "오늘 몇 건이야"},
        {"role": "assistant", "content": "3건입니다"},
    ])

    roles = [m["role"] for m in shim.calls[0]["json"]["messages"]]
    assert roles == ["system", "user", "assistant", "user"], roles
    assert "그럼 어제는?" in shim.calls[0]["json"]["messages"][-1]["content"]


@ASYNCIO
async def test_chat_without_history_still_works(monkeypatch):
    """⚠ 이력을 안 주면 예전과 똑같이 돈다 — 백엔드가 아직 안 넘겨도 안 깨진다."""
    shim = stub_llm(monkeypatch, ok("오늘 무단 통과가 3건입니다."))
    r = await pipeline.chat(WITH_KEY, "오늘 몇 건이야", FACTS)
    assert r.generated_by == "llm"
    assert [m["role"] for m in shim.calls[0]["json"]["messages"]] == ["system", "user"]


@ASYNCIO
async def test_warm_up_touches_every_window(monkeypatch):
    """⛔ 캐시가 없는 첫 호출이 **7.36초**다(2026-08-07 실측). 읽기 상한이 8초라
    시연에서 요원의 첫 질문이 규칙 문장으로 떨어질 수 있다.

    ⭐ 답은 상한을 늘리는 것이 아니라 **첫 호출을 미리 태워 두는 것**이다.
    ⚠ 슬롯마다 캐시가 따로라 창구마다 두 번씩 태운다.
    """
    from ai.control_assistant import client as llm_client

    seen = []

    async def spy(cfg, *, system, user, history=None):
        seen.append(system)
        return "네"

    monkeypatch.setattr(llm_client, "complete", spy)
    monkeypatch.setattr(pipeline, "complete", spy)

    out = await pipeline.warm_up(WITH_KEY)
    assert out["warmed"] == 3 * llm_client.WARM_ROUNDS, out
    assert set(seen) == {prompts.CHAT_SYSTEM, prompts.DAILY_SUMMARY_SYSTEM,
                         prompts.ALERT_BRIEF_SYSTEM}, "창구 하나가 안 데워졌다"
    # ⭐ 08-08 — **순서도 계약이다.** 창구마다 두 요청을 동시에 태우고 슬롯이 둘이라,
    # 마지막 창구 하나가 두 슬롯을 전부 덮는다. chat이 마지막이 아니면 고유 접두
    # 12,065자짜리 챗봇이 늘 찬 호출로 떨어진다(2026-08-08 교차 검증 — set()만 보던
    # 이 시험이 그 순서를 안 지키고 있었다).
    assert seen[-llm_client.WARM_ROUNDS:] == (
        [prompts.CHAT_SYSTEM] * llm_client.WARM_ROUNDS
    ), "chat이 마지막이 아니다 — 데워 둔 슬롯을 다른 창구가 덮는다"


@ASYNCIO
async def test_warm_up_fills_every_slot_not_just_one(monkeypatch):
    """⛔⛔ **순차로 태우면 슬롯 하나만 데워진다**(2026-08-07 실서버 실측).

    앞 요청이 끝나 슬롯이 놀고 접두사까지 같으면 llama-server 는 **같은 슬롯을 다시
    고른다.** 그래서 두 번 태워도 슬롯 1 은 차가운 채로 남았다.

        순차로 데운 뒤 · 동시 두 질문   →  둘 다 12.0초 (상한 초과, 둘 다 폴백)
        동시에 데운 뒤 · 동시 두 질문   →  2.52초 · 2.81초

    ⭐ 그래서 한 창구의 `WARM_ROUNDS` 번을 **동시에** 던진다. 여기서 재는 것은
    "몇 번 불렀나"가 아니라 **"몇 개가 같은 순간에 떠 있었나"** 다 — 횟수만 세면
    순차 코드도 그대로 통과한다.
    """
    from ai.control_assistant import client as llm_client

    live = 0
    peak = 0

    async def spy(cfg, *, system, user, history=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)        # 다른 코루틴에 양보한다
        live -= 1
        return "네"

    monkeypatch.setattr(llm_client, "complete", spy)
    monkeypatch.setattr(pipeline, "complete", spy)

    await pipeline.warm_up(WITH_KEY)
    assert peak == llm_client.WARM_ROUNDS, (
        f"같은 순간에 {peak}개만 떴다. 순차로 태우면 슬롯 하나만 데워진다"
    )


@ASYNCIO
async def test_warm_up_uses_its_own_longer_timeout(monkeypatch):
    """⛔ 평소 상한(12초)으로 데우면 **데우기가 통째로 타임아웃한다**(2026-08-07 실측).

    찬 슬롯 둘을 동시에 채우면 같은 GPU 를 나눠 쓰며 서로를 늦춘다. 한 개면 7초에 끝날
    프리필이 겹치면 12초를 넘어 `warmed: 0` 이 나왔다. 데우기는 사람이 기다리는 호출이
    아니니 넉넉히 준다 — 여기서 실패하면 **요원의 첫 질문이 그 값을 대신 문다.**
    """
    from ai.control_assistant import client as llm_client

    seen = []

    async def spy(cfg, *, system, user, history=None):
        seen.append(cfg.timeout_sec)
        return "네"

    monkeypatch.setattr(llm_client, "complete", spy)
    monkeypatch.setattr(pipeline, "complete", spy)

    await pipeline.warm_up(WITH_KEY)
    assert seen, "데우기가 한 번도 안 불렸다"
    assert set(seen) == {llm_client.WARM_TIMEOUT_SEC}, (
        f"데우기가 평소 상한 {WITH_KEY.timeout_sec}초로 돌았다 — 겹치면 그 안에 못 끝난다"
    )
    assert llm_client.WARM_TIMEOUT_SEC > WITH_KEY.timeout_sec


@ASYNCIO
async def test_warm_up_does_not_flood_all_windows_at_once(monkeypatch):
    """⚠ 창구 사이는 순차로 남긴다.

    세 창구를 한꺼번에 던지면 슬롯 둘에 여섯이 몰려 서로 밀어낸다. 슬롯을 한 창구로
    채우고 다음으로 넘어가야 캐시가 남는다.
    """
    from ai.control_assistant import client as llm_client

    live = 0
    peak = 0

    async def spy(cfg, *, system, user, history=None):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0)
        live -= 1
        return "네"

    monkeypatch.setattr(llm_client, "complete", spy)
    monkeypatch.setattr(pipeline, "complete", spy)

    await pipeline.warm_up(WITH_KEY)
    assert peak <= llm_client.WARM_ROUNDS, (
        f"{peak}개가 한꺼번에 떴다. 슬롯 수보다 많이 던지면 서로 밀어낸다"
    )


@ASYNCIO
async def test_warm_up_survives_a_dead_server(monkeypatch):
    """⚠ 데우기가 안 됐다고 서비스가 안 뜨면 안 된다."""
    from ai.control_assistant import client as llm_client

    async def boom(cfg, *, system, user, history=None):
        raise LLMUnavailable("timeout")

    monkeypatch.setattr(llm_client, "complete", boom)
    out = await pipeline.warm_up(WITH_KEY)
    assert out["failed"] == 3 * llm_client.WARM_ROUNDS
    assert out["warmed"] == 0


@ASYNCIO
async def test_warm_up_without_key_does_nothing():
    out = await pipeline.warm_up(AssistantConfig(api_key=""))
    assert out == {"warmed": 0, "failed": 0}


def test_read_timeout_fits_the_longest_answers():
    """⭐ 사용자가 "긴 글도 답변할 수 있게" 정했다(2026-08-07).

    ⛔ **12초로 잡았다가 20초로 올렸다**(같은 날 밤). 답 길이가 아니라 **프리필**이
    문제였다 — 프론트가 실사용에서 규칙 문장이 뜨는 것을 보고 13.96초를 쟀고, 우리가
    실경로로 다시 재니 최악이 25.84초였다.

        자르기 전    첫 호출 25.84초 · 동시 둘 16.94초
        목록 자른 뒤  첫 호출 13.12초 · 동시 둘  6.18초

    ⚠ 여기서 상한을 넘기면 **답이 다 만들어진 뒤에 버려진다.** GPU 는 이미 다 썼고
    요원은 규칙 문장을 본다 — 제일 손해가 큰 실패다.
    ⚠ 연결 상한은 따로 3초라 "안 닿는 경우"가 이 값에 안 걸린다.
    """
    from ai.control_assistant import client as llm_client
    from ai.control_assistant.config import DEFAULT_TIMEOUT_SEC

    assert DEFAULT_TIMEOUT_SEC >= 20.0, (
        f"읽기 상한이 {DEFAULT_TIMEOUT_SEC}초다 — 첫 호출 13.12초에 여유가 안 남는다"
    )
    assert llm_client.CONNECT_TIMEOUT_SEC <= 3.0, "연결 상한이 길면 안 닿는 경우가 이 값을 먹는다"
    assert DEFAULT_TIMEOUT_SEC > llm_client.CONNECT_TIMEOUT_SEC * 3


@ASYNCIO
async def test_truncated_does_not_open_the_breaker(monkeypatch):
    """⛔⛔ 잘린 답은 **서버가 살아서 답을 준 것**이라 차단기가 세면 안 된다.

    안 그러면 되먹임이 생긴다 — 답을 길게 쓰라고 완화할수록 잘림이 늘고, 세 번 연달으면
    차단기가 열려 30초 동안 LLM 을 아예 안 부른다. 차단기가 세 창구 공용이라
    **일일 요약이 세 번 잘리면 챗봇도 같이 죽는다**(2026-08-07 반대 심문이 잡음).
    """
    calls = {"n": 0}

    async def cut(cfg, *, system, user, history=None):
        calls["n"] += 1
        raise LLMUnavailable("truncated")

    monkeypatch.setattr(pipeline, "complete", cut)

    for _ in range(5):
        r = await pipeline.daily_summary(WITH_KEY, FACTS)
        assert r.fallback_reason == "truncated", r.fallback_reason
    assert calls["n"] == 5, "차단기가 열려 LLM 을 안 불렀다"


@ASYNCIO
async def test_transport_failures_still_open_the_breaker(monkeypatch):
    """⚠ 반대로 **서버가 죽은 신호는 여전히 세야 한다.** 안 그러면 차단기가 무의미해진다."""
    calls = {"n": 0}

    async def dead(cfg, *, system, user, history=None):
        calls["n"] += 1
        raise LLMUnavailable("transport_error")

    monkeypatch.setattr(pipeline, "complete", dead)

    for _ in range(3):
        await pipeline.daily_summary(WITH_KEY, FACTS)
    r = await pipeline.daily_summary(WITH_KEY, FACTS)
    assert r.fallback_reason == "circuit_open", r.fallback_reason
    assert calls["n"] == 3, "차단기가 열렸는데도 불렀다"


def test_contract_numbers_are_not_treated_as_invented():
    """⛔⛔ **우리가 넣은 계약 지식이 우리 검사에 걸리고 있었다**(2026-08-07).

    프롬프트에 설비 구성을 넣었더니 모델이 답에 적었는데, 그 숫자가 사실 딕셔너리에
    없어서 `number_mismatch` 로 폴백에 떨어졌다. 여섯 판에 두 번 그랬다.

    ⚠ 계약 지식은 늘 참인 값이라 대조 집합에 함께 넣는다. 넓히는 게 아니라 채우는 것이다.
    """
    facts = {"alerts": {"active": 3}}
    real = ("최근 기록에는 게이트 1번에서 무단 통과가 발생했고 미확인 경고가 3건입니다. "
            "게이트 1번은 장비 오류로 쌓인 값이며 실제 설비는 게이트 5번과 2번만 있습니다.")
    assert guards.check(real, facts) is None, guards.check(real, facts)


def test_invented_numbers_still_caught_after_the_contract_allowance():
    """⚠ 계약 밖 수를 지어내는 것은 그대로 잡혀야 한다. 안 그러면 허용이 구멍이 된다."""
    facts = {"alerts": {"active": 3}}
    assert guards.check("미확인 경고가 47건입니다.", facts) == "number_mismatch"
    assert guards.check("게이트 9번에서 났습니다.", facts) == "number_mismatch"


def test_contract_numbers_match_the_prompt():
    """⛔ 두 자리가 갈리면 조용히 폴백이 늘거나 지어낸 수가 통과한다.

    프롬프트가 설비를 5번·2번이라 적으므로 검사도 그 둘을 알아야 한다.
    """
    assert "5번" in prompts.CHAT_SYSTEM and "2번" in prompts.CHAT_SYSTEM
    assert {"5", "2"} <= guards.CONTRACT_NUMBERS


def test_battery_rule_tells_the_number_not_just_the_caveat():
    """⛔ **못 믿을 값이라는 것과 안 알려 주는 것은 다르다**(2026-08-07 밤 실측).

    "배터리 값에 판단을 붙이지 마라"는 규칙이 셋으로 흩어져 있었는데 전부 금지만
    말했다. 그래서 모델이 과교정해 **값 자체를 안 적었다.**

        열두 판 중 3판  "배터리 값은 시연용 표시라 실제 잔량이 아닙니다"  ← 90 이 없다

    ⭐ 규칙에 **짝**을 달았다 — 수를 먼저 적고 그 다음에 시연용이라는 것을 덧붙인다.
    고친 뒤 열두 판에서 빠뜨림 0개, 판단어 누출 0개다.

    ⚠ 요원은 화면에서 그 수를 이미 보고 있다. 우리가 안 적으면 "왜 안 말해 주지"가 된다.
    """
    # 금지와 짝이 같은 자리에 있어야 한다 — 떨어져 있으면 한쪽만 지켜진다.
    rules = prompts._COMMON_RULES
    assert "판단을 붙이지 않습니다" in rules
    assert "수는 그대로 알려 드립니다" in rules, "금지만 있고 값을 적으라는 짝이 없다"
    assert "값을 아예 안 적으면 안 됩니다" in rules
