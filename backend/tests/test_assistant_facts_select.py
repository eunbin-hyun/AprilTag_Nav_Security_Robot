"""질문에 맞는 사실만 싣는지 (2026-08-07 신설).

⚠ 여기서 못박는 것은 **줄이는 것보다 안 빠뜨리는 것**이다. 걸러 냈는데 필요한 칸이
없으면 챗봇이 "자료에 없습니다"로 답한다 — 그게 우리가 오늘 고친 결함 자체다.
"""
from __future__ import annotations

from ai.control_assistant import facts_select, prompts

FACTS = {
    "today": {"date": "2026-08-07", "events": {"total": 153},
              "verdicts": {"normal": 7, "untagged": 15},
              "alerts": {"total": 84, "active": 69, "by_type": {"untagged": 15}},
              "gates": [{"gate_no": 1, "passes": 114, "untagged": 13}],
              "peak_hour": {"hour": 16, "events": 88}},
    "yesterday": {"date": "2026-08-06", "events": {"total": 12}},
    "robots": [{"name": "RB1-01", "network_status": "WS_OK", "battery": 87}],
    "alerts": {"active": 74, "recent": [{"id": 94, "type": "untagged"}]},
    "events": {"recent": [{"kind": "gate_pass", "gate_no": 1}]},
}


def _keys(question):
    return set(facts_select.pick(question, FACTS))


# ── ① 좁히는가 ───────────────────────────────────────────────────────────

def test_robot_question_drops_daily_totals():
    """로봇을 물었는데 하루치 게이트별 집계까지 실을 이유가 없다."""
    k = _keys("로봇이 지금 어디 있어?")
    assert "robots" in k
    assert "today" not in k, k


def test_count_question_keeps_totals():
    k = _keys("오늘 무단 통과가 몇 건이야?")
    assert "today" in k and "yesterday" in k, k


def test_greeting_carries_only_the_small_ones():
    """⚠ 인사에도 사실이 아주 없으면 안 된다 — '지금 경고 74건입니다'를 못 적는다."""
    k = _keys("안녕")
    assert k == {"robots", "alerts"}, k


def test_howto_question_does_not_need_daily_facts():
    """'어떻게 쓰나'는 계약(시스템 지식)으로 답한다."""
    k = _keys("이 시스템 어떻게 쓰는 거야?")
    assert "today" not in k, k


# ── ② ⭐ 안 빠뜨리는가 (이쪽이 더 중요하다) ──────────────────────────────

def test_unknown_question_keeps_everything():
    """⭐ 안전판 — 갈래가 안 잡히면 전부 싣는다. 좁히다 빠뜨리는 쪽이 더 나쁘다."""
    assert _keys("음 그러니까 아까 그거 있잖아") == set(FACTS)


def test_always_present_keys_survive_every_branch():
    """⛔ 어느 갈래로 좁혀도 robots·alerts 는 남는다."""
    for q in ["오늘 몇 건이야", "안녕", "로봇 어디", "최근 이벤트 뭐야",
              "경고 확인하려면", "이 시스템 어떻게 써", "권한이 뭐야"]:
        k = _keys(q)
        assert "robots" in k and "alerts" in k, (q, k)


def test_mixed_question_keeps_both_groups():
    """낱말이 두 갈래에 걸리면 둘 다 싣는다."""
    k = _keys("오늘 경고 몇 건이고 로봇은 붙어 있어?")
    assert {"today", "robots", "alerts"} <= k, k


def test_original_facts_are_not_mutated():
    """⚠ 사후 검사기가 **원본 숫자**로 대조한다. 걸러 낸 사본으로 검사하면 가짜 실패가 난다."""
    before = dict(FACTS)
    facts_select.pick("로봇 어디 있어", FACTS)
    assert FACTS == before
    assert set(FACTS) == set(before)


def test_non_dict_facts_pass_through():
    assert facts_select.pick("q", None) is None
    assert facts_select.pick("", FACTS) is FACTS


# ── ③ 프롬프트에 실제로 걸리는가 ─────────────────────────────────────────

def test_prompt_actually_shrinks():
    """⭐ 배선 검사 — 골라 놓고 프롬프트가 안 쓰면 헛것이다."""
    big = prompts.chat_user("음 그러니까 아까 그거", FACTS)
    small = prompts.chat_user("로봇 어디 있어?", FACTS)
    assert len(small) < len(big), (len(small), len(big))
    assert "peak_hour" not in small, "로봇 질문에 하루치 집계가 실렸다"
    assert "RB1-01" in small, "로봇 질문인데 로봇이 빠졌다"


# ── ④ 백엔드가 새로 실은 칸 셋 (2026-08-07 저녁) ────────────────────────

NEW_FACTS = {**FACTS,
             "viewer": {"role": "owner", "role_label": "총괄 관리자",
                        "display_name": "손세욱", "can_manage_staff": True},
             "weather": {"status": "CLEAR", "description": "맑음", "dispatch_ok": True},
             "recent_shuttles": [{"shuttle_no": "12-3", "gate_no": 5}]}


def test_viewer_always_rides_along():
    """⭐ 누가 묻는지는 어느 물음에서든 쓰인다. 값이 작아 늘 실어도 싸다."""
    for q in ["안녕", "오늘 몇 건이야", "로봇 어디", "이 시스템 어떻게 써", "제 직급이 뭐야"]:
        assert "viewer" in facts_select.pick(q, NEW_FACTS), q


def test_weather_question_carries_weather():
    for q in ["오늘 날씨 어때?", "비 오면 로봇 나가?", "지금 출동 되나?"]:
        assert "weather" in facts_select.pick(q, NEW_FACTS), q


def test_shuttle_question_carries_shuttle_times():
    """⚠ recent_shuttles 는 **시각**이고 today.events.shuttle_arrival 은 **건수**다."""
    k = facts_select.pick("셔틀 언제 왔어?", NEW_FACTS)
    assert "recent_shuttles" in k, k


def test_robot_question_still_drops_weather_and_shuttle():
    """⚠ 새 칸이 늘었다고 좁히기가 헐거워지면 안 된다."""
    k = set(facts_select.pick("로봇이 지금 어디 있어?", NEW_FACTS))
    assert "recent_shuttles" not in k, k
    assert "today" not in k, k


# ── 긴 목록 자르기 ────────────────────────────────────────────────────────

LONG_FACTS = {
    "robots": [{"name": "RB1-01"}],
    "alerts": {"active": 133, "recent": [{"id": i, "type": "untagged"} for i in range(15)]},
    "events": {"recent": [{"id": i, "kind": "gate_pass"} for i in range(15)]},
    "recent_shuttles": [{"at": f"1{i}:00"} for i in range(5)],
    "today": {"events": {"total": 200}},
    "viewer": {"role": "agent"},
}


def test_long_lists_are_capped():
    """⛔⛔ **칸만 골라도 목록이 길면 소용이 없다**(2026-08-07 밤 실측).

    "무단 통과" 물음 하나에 `alerts` 와 `events` 가 둘 다 걸리는데, 그 둘의 최근 목록이
    각각 열다섯 건이라 user 가 **5,290자**가 됐다.

        첫 호출   25.84초  (프리필 8,232톡 18.52초)
        두 번째    2.64초  (프리필     4톡)

    ⛔ 데우기가 이 자리를 못 구한다. `warm_up` 은 system 만 태우는데 user 가 3,000톡을
    넘으면 llama-server 가 데워 둔 슬롯을 안 골라 통째로 다시 프리필한다.
    """
    picked = facts_select.pick("최근 무단 통과자 처리된 것 있나?", LONG_FACTS)
    assert len(picked["alerts"]["recent"]) == 6, picked["alerts"]["recent"]
    assert len(picked["events"]["recent"]) == 6, picked["events"]["recent"]


def test_capping_does_not_touch_the_original():
    """⚠ 자르는 것은 **모델에게 보내는 사본**뿐이다.

    원본이 잘리면 사후 검사기가 대조하는 숫자 집합이 줄어, 잘려 나간 값을 근거로 답해도
    "사실에 있다"로 통과한다.
    """
    facts_select.pick("최근 무단 통과 있어?", LONG_FACTS)
    assert len(LONG_FACTS["alerts"]["recent"]) == 15
    assert len(LONG_FACTS["events"]["recent"]) == 15


def test_unknown_question_still_caps_lists():
    """⚠ 갈래를 모르는 것과 열다섯 건을 통째로 보내는 것은 다른 이야기다.

    안 걸리는 물음은 칸을 다 싣지만, 그 길이가 25초를 만든다.
    """
    picked = facts_select.pick("이건 대체 무슨 소리야", LONG_FACTS)
    assert set(picked) == set(LONG_FACTS), "칸은 그대로 다 실어야 한다"
    assert len(picked["events"]["recent"]) == 6
    assert len(picked["recent_shuttles"]) == 3


def test_capping_survives_odd_shapes():
    """⚠ 폴백이 마지막 자리라 여기서 터지면 화면이 통째로 빈다."""
    odd = {"alerts": "문자열", "events": {"recent": None}, "recent_shuttles": 3,
           "robots": [], "viewer": {}}
    # ⚠ 셔틀까지 실리게 물어야 세 칸을 다 본다 — "경고"만 물으면 셔틀이 안 실린다.
    picked = facts_select.pick("최근 경고랑 셔틀 도착 알려줘", odd)
    assert picked["alerts"] == "문자열"
    assert picked["events"] == {"recent": None}
    assert picked["recent_shuttles"] == 3
