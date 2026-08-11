"""질문에 맞는 사실만 골라 싣는다. 프리필 시간을 줄이는 자리다.

## 왜 필요한가 (2026-08-07 실측)

llama-server 가 응답에 `prompt eval`(프리필)과 `eval`(디코딩)을 나눠 찍어 준다. 실제 크기
프롬프트 한 번을 재니 이렇다.

    벽시계 6.41초 = 프리필 2,562토큰 3.47초 + 디코딩 85토큰 2.58초

⭐ **프리필이 디코딩보다 크다.** 그런데 그 2,562토큰 가운데 사실 딕셔너리가 절반을 넘고,
**질문이 무엇이든 늘 같은 묶음을 통째로 싣고 있었다.** "로봇 어디 있어"에 하루치 게이트별
집계까지 실어 보낸다.

⚠ 시스템 프롬프트 몫은 이미 캐시가 먹는다(같은 접두사라 두 번째 호출부터 프리필이
2,562 → 577토큰으로 준다). 캐시가 못 먹는 건 **매번 값이 달라지는 사실 딕셔너리**다.
그래서 줄일 수 있는 자리가 여기뿐이다.

## 설계

⛔ **LLM 을 한 번 더 불러 의도를 분류하지 않는다.** 그러면 호출이 두 배가 되어 지금 제일
아픈 지연과 방향이 반대다. 낱말 규칙으로 가른다.

⭐ **안전판 — 애매하면 전부 싣는다.** 걸러 냈는데 필요한 칸이 빠지면 답이 나빠진다.
줄이는 이득보다 못 답하는 손해가 크다.

⭐ **어느 갈래든 늘 싣는 것이 있다**(`_ALWAYS`). 인사나 "지금 상태 어때" 같은 물음에
쓰이는 작은 값들이라, 빠지면 답이 통째로 비어 버린다.
"""
from __future__ import annotations

import re

# 갈래마다 실을 최상위 칸. 값이 비면 그 갈래를 안 좁힌다.
_GROUPS: dict[str, tuple[str, ...]] = {
    "집계": ("today", "yesterday"),
    "로봇": ("robots",),
    "경고": ("alerts",),
    "이벤트": ("events",),
    "날씨": ("weather",),
    "셔틀": ("recent_shuttles",),
}

# ⚠ 어느 갈래로 좁히든 늘 남긴다. 없으면 인사 답과 "지금 어때"가 빈다.
# ⭐ `viewer` 도 늘 남긴다 — 누가 묻는지는 어느 물음에서든 쓰이고 값이 아주 작다.
_ALWAYS: tuple[str, ...] = ("robots", "alerts", "viewer")

# 낱말이 겹치는 자리가 있다. 겹치면 그 갈래를 **둘 다** 싣는다 — 좁히다 빠뜨리는 쪽이 더 나쁘다.
_PATTERNS: dict[str, re.Pattern] = {
    "집계": re.compile(
        r"몇\s*건|건수|얼마나|통계|집계|합계|전체|총|오늘|어제|하루|시간대|몰[린렸]|"
        r"많[았이]|무단\s*통과|정상\s*통과|퇴장|한쪽\s*감지|판정|게이트별|추이"
    ),
    "로봇": re.compile(r"로봇|RB1|배터리|어디|위치|주행|충전|복귀|출동|이동|연결|통신|붙어"),
    "경고": re.compile(r"경고|알림|미확인|확인\s*처리|심각|주의|셔틀|무단|이상|처리"),
    "이벤트": re.compile(r"최근|방금|직전|이벤트|태깅|통과\s*기록|들어온|찍[힌었]"),
    "날씨": re.compile(r"날씨|비\s|비가|눈이|맑|흐림|출동|나갈\s*수|실외|기상"),
    "셔틀": re.compile(r"셔틀|언제\s*왔|도착"),
}

# ⛔ 이 낱말이 보이면 사실이 거의 필요 없다. 계약(시스템 지식)만으로 답하는 물음이다.
_HOWTO = re.compile(
    r"어떻게\s*(하|쓰|해|보|누르|사용)|무엇을\s*해|뭐\s*해야|방법|절차|뜻이\s*뭐|"
    r"무슨\s*뜻|이란|이라는\s*게|설명해|알려\s*줘\s*$|권한|직급|역할|누가\s*볼"
)

# 인사·감사. 사실은 작은 것만 있으면 된다.
_GREETING = re.compile(r"^\s*(안녕|반가|수고|고마|감사|하이|헬로|좋은\s*(아침|저녁))")

# ⛔⛔ **긴 목록을 자른다**(2026-08-07 밤 실측으로 잡음).
#
# 칸만 골라도 목록이 길면 소용이 없다. "무단 통과" 물음 하나에 `alerts` 와 `events` 가
# 둘 다 걸리는데, 그 둘의 최근 목록이 각각 열다섯 건이라 user 가 **5,290자**가 됐다.
#
#     첫 호출   25.84초  (프리필 8,232톡 18.52초)
#     두 번째    2.64초  (프리필     4톡)
#
# ⛔ **데우기가 이 자리를 못 구한다.** `warm_up` 은 system 만 태우는데, user 가 3,000톡을
# 넘으면 llama-server 가 접두사 유사도로 슬롯을 고를 때 데워 둔 슬롯을 안 고른다.
# 그래서 8,232톡을 통째로 다시 프리필한다.
#
# ⭐ 답에 실제로 쓰이는 것은 **최근 몇 건**이다. 열다섯 번째 경고를 근거로 삼는 답은 없다.
# 집계는 `today`·`yesterday` 가 따로 들고 있어서, 목록을 잘라도 "몇 건이야"는 안 흔들린다.
#
# ⚠ 자르는 것은 **모델에게 보내는 사본**뿐이다. 원본은 그대로라 사후 검사기가 대조하는
# 숫자 집합도 그대로다 — 자른 값을 근거로 답하면 여전히 잡힌다.
_LIST_CAPS: dict[str, tuple[str, int]] = {
    "alerts": ("recent", 6),
    "events": ("recent", 6),
    "recent_shuttles": ("", 3),      # 빈 이름이면 그 칸 자체가 목록이다
}


def _cap_lists(picked: dict) -> dict:
    """긴 목록의 뒤를 자른다. 최근이 앞에 온다는 계약을 그대로 믿는다."""
    out = {}
    for key, value in picked.items():
        cap = _LIST_CAPS.get(key)
        if not cap:
            out[key] = value
            continue
        inner, limit = cap
        if not inner:
            out[key] = value[:limit] if isinstance(value, list) else value
        elif isinstance(value, dict) and isinstance(value.get(inner), list):
            out[key] = {**value, inner: value[inner][:limit]}
        else:
            out[key] = value
    return out


def pick(question: str, facts: dict) -> dict:
    """질문에 맞는 칸만 남긴 사본을 돌려준다.

    ⚠ 원본을 안 건드린다 — 사후 검사기가 **원본 숫자**로 대조하고 폴백도 원본을 본다.
    걸러 낸 사본으로 검사하면 "사실에 있는데 없다고 잡는" 가짜 실패가 난다.
    """
    if not isinstance(facts, dict) or not question:
        return facts

    keys = set(_ALWAYS)

    hit = {name for name, pat in _PATTERNS.items() if pat.search(question)}

    # ⛔ 2026-08-09 백지검토 — **인사말이 앞에 붙으면 뒤 물음의 재료가 통째로 빠졌다.**
    # `_GREETING`은 `^`로 시작해 `.match`로 재는지라 문장 **머리**만 본다. 그래서
    # "안녕하세요. 오늘 게이트 통과가 몇 건인가요?"가 인사 갈래로 떨어져 today·yesterday가
    # 안 실렸고, 모델은 답할 재료가 없어 "지금 화면에 안 실려 옵니다"로 답했다.
    # 실서버 실측 — 인사말을 떼면 "0건입니다"로 정확히 답하고, 붙이면 못 답한다.
    # 요원이 화면을 열고 처음 말을 거는 자리라 시연에서 그대로 밟는다.
    #
    # ⭐ 고침은 갈래 판정 순서만 바꾼 것이다. 물음 패턴에 하나라도 걸리면 그것은 물음이지
    #    인사가 아니다. 순수 인사("안녕하세요")는 어느 패턴에도 안 걸려 그대로 인사 갈래다.
    if not hit and _GREETING.match(question):
        # 인사에는 늘 싣는 것만. 하루치 집계까지 보낼 이유가 없다.
        return _cap_lists({k: facts[k] for k in facts if k in keys})

    if not hit:
        # ⭐ 안 걸리면 전부 싣는다. 모르는 물음을 좁히면 답이 나빠진다.
        # 다만 "어떻게 쓰나"류는 계약만으로 답하니 작은 것만 남긴다.
        if _HOWTO.search(question):
            return _cap_lists({k: facts[k] for k in facts if k in keys})
        # ⚠ 칸은 다 싣더라도 **목록 길이는 자른다.** 갈래를 모르는 것과 열다섯 건을
        # 통째로 보내는 것은 다른 이야기다 — 그 길이가 25초를 만든다.
        return _cap_lists(facts)

    for name in hit:
        keys.update(_GROUPS.get(name, ()))

    picked = {k: facts[k] for k in facts if k in keys}
    # 다 걸러져 아무것도 안 남으면 원본을 준다(있을 수 없지만 값이 비는 것보다 낫다).
    return _cap_lists(picked or facts)


def summarize(question: str, facts: dict) -> str:
    """무엇을 왜 골랐나. 로그·시험용이다."""
    before = len(str(facts))
    after = len(str(pick(question, facts)))
    cut = 100 - round(after / before * 100) if before else 0
    return f"{before}자 → {after}자 ({cut}% 줄임)"
