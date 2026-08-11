"""LLM이 낸 문장을 요원에게 내보내기 전에 거르는 사후 검사.

⭐ **LLM을 다시 안 부른다.** 문자열과 사실 딕셔너리만 보고 판정하므로 지연이 0이다.
검사에 걸리면 규칙 기반 폴백 문장으로 떨어뜨리고, 왜 떨어뜨렸는지 `fallback_reason`에 남긴다.

왜 사후 검사인가 —
`json_schema` 강제 출력으로 모양을 묶는 길도 있는데 **접었다.** 우리 기록에
"긴 한국어 분석은 schema 강제 출력이 자주 실패한다, 자유 텍스트 + 끝에 판정 한 줄이 안정"이
있고(워크플로우 실측에서 한 갈래는 전부 실패했다), 우리 모델이 그 기록과 같은 Qwen 계열이다.
게다가 스키마를 안 지킨 응답이 그대로 나가면 화면에 JSON 조각이 뜬다.
"""
from __future__ import annotations

import re

# ── ① 한자 누출 ──────────────────────────────────────────────────────────
#
# ⛔ 2026-08-07 실서버에서 잡혔다. 챗봇이 이렇게 답했다 —
#    "로그인한 사용자의 身份信息나 역할 정보가 포함되어 있지 않습니다"
# Qwen 계열이 중국어 자료로 배운 모델이라 한국어를 쓰다가 한자어 자리에서 중국어가 샌다.
# 요원 화면에 중국어가 뜨면 "이 시스템 뭐지" 하는 자리라 반드시 막는다.
#
# ⚠ 한자를 **전부** 막지는 않는다. 우리 관제 문구에 한자가 나올 일이 없어서 한 자만 나와도
# 누출로 보지만, 혹시 정당한 쓰임이 생기면 이 문턱을 올린다.
_HANJA = re.compile(r"[一-鿿㐀-䶿]")
# 한글이 거의 없으면 애초에 한국어 답이 아니다(영어로 통째로 답한 판).
_HANGUL = re.compile(r"[가-힣]")

# ── ② 숫자 조작 ──────────────────────────────────────────────────────────
#
# 사실에 없는 숫자를 모델이 지어내거나, 있는 값을 더하고 나눠서 새 수를 만드는 것을 막는다.
# 프롬프트로도 금지하지만 프롬프트는 지켜질 때만 지켜진다.
_NUM = re.compile(r"\d[\d,]*")

# 통째로 시각인 문자열. `_collect_numbers` 가 이 값의 숫자를 근거로 안 삼는다(아래 까닭).
#   2026-08-07 · 2026-08-07T16:32:00+09:00 · 2026-08-07 16:32 · 16:32:00
_TIMESTAMP_STR = re.compile(
    r"\s*\d{4}-\d{2}-\d{2}([T ]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?\s*"
)
_CLOCK_STR = re.compile(r"\s*\d{1,2}:\d{2}(:\d{2})?\s*")

# 숫자로 읽되 대조에서 빼는 것들. 시각·날짜·비율은 사실에서 그대로 온 게 아니라
# 모델이 형식을 바꿔 적는 자리라, 여기까지 잡으면 정상 문장이 다 걸린다.
#   2026-08-07 · 2026년 8월 7일 · 14:32 · 14시 · 17시 25분 · 90%
#
# ⛔⛔ **창(窓)으로 재던 것을 붙어 있는 문맥만 보게 좁혔다**(2026-08-09 백지검토).
#
# 예전에는 숫자 앞뒤 6자를 잘라 위 무늬를 `search` 했다. 그러면 창 안에 시각이 하나만
# 있어도 **옆에 있는 딴 숫자까지 통째로 면제**됐다. 하필 프롬프트가 못박은 문장이
# 정확히 그 꼴이다 —
#
#     "이벤트가 가장 몰린 시간대는 15시로 999건입니다."   →  옛 검사는 통과(None)
#
# 일일 요약에서 제일 자주 나오는 숫자가 한 번도 대조를 안 탔다는 뜻이다.
#
# ⭐ 그래서 **바로 붙어 있는 글자만** 본다. 뒤가 날짜·시각 단위(년월일시분초)이거나
# 백분율이면 건너뛰고, `14:32`·`2026-08-07`·`1.5` 처럼 구분자 양옆이면 건너뛴다.
# `15시`의 15는 여전히 면제이고 `71건`의 71은 이제 대조를 탄다.
_AFTER_NUM_SKIP = re.compile(r"[년월일시분초]|\s*%|[:\-./]\d")
_BEFORE_NUM_SKIP = ":-./"


def has_hanja(text: str) -> bool:
    """중국어(한자)가 섞였나."""
    return bool(_HANJA.search(text))


def looks_korean(text: str) -> bool:
    """한국어 문장인가. 한글이 전체의 10%도 안 되면 아니라고 본다."""
    if not text.strip():
        return False
    return len(_HANGUL.findall(text)) >= max(3, len(text) * 0.1)


def _collect_numbers(node, out: set[str]) -> None:
    """사실 딕셔너리 안의 모든 수를 문자열로 모은다. 중첩·목록을 다 훑는다."""
    if isinstance(node, dict):
        for v in node.values():
            _collect_numbers(v, out)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect_numbers(v, out)
    elif isinstance(node, bool):
        pass                      # True/False 는 1/0 으로 읽히면 안 된다
    elif isinstance(node, int):
        out.add(str(node))
    elif isinstance(node, float):
        out.add(str(node))
        if node.is_integer():
            out.add(str(int(node)))
    elif isinstance(node, str):
        # 사실에 문자열로 실린 수(id 같은 것)도 근거로 인정한다.
        #
        # ⛔⛔ **시각 문자열만 뺀다**(2026-08-09 백지검토). 사실 딕셔너리에는 ISO 시각이
        # 잔뜩 실린다(`observed_at`·`received_at`·`created_at`·`updated_at`·`date`).
        # 그 값의 분·초·연·월·일이 통째로 "아는 수"가 되어서, 모델이 지어낸 건수가
        # 어느 시각의 두 자리와 겹치기만 하면 `number_mismatch` 를 그냥 지났다.
        # 챗봇 갈래 상한(경고 6·이벤트 6·셔틀 3)대로 재료를 만들어 프로브를 돌리니
        # 2~99 가운데 서른네 개가 통과했다.
        #
        # ⚠ **문자열 전체를 근거에서 빼지는 않는다.** `event_id`·로봇 이름처럼 숫자가
        # 박힌 값을 모델이 그대로 옮겨 적는 자리가 있어서, 그것까지 빼면 멀쩡한 답이
        # 규칙 문장으로 떨어진다. 통째로 시각인 값만 건너뛴다.
        # ⚠ 답에 적히는 시각(`14:32`·`2026-08-07`·`17시 25분`)은 텍스트 쪽에서 이미
        # 건너뛰므로, 여기서 빼도 정상 문장이 걸리지 않는다.
        if _TIMESTAMP_STR.fullmatch(node) or _CLOCK_STR.fullmatch(node):
            return
        for m in _NUM.finditer(node):
            out.add(m.group().replace(",", ""))


def ungrounded_numbers(text: str, facts: dict) -> list[str]:
    """문장에 있는데 사실에는 없는 수를 돌려준다.

    ⚠ 0과 1은 안 센다 — "한 대"·"하나"처럼 세는 말로 자연스럽게 나오고, 사실에 없어도
    틀린 말이 아닌 자리가 많다. 그것까지 잡으면 정상 문장이 폴백으로 떨어진다.
    """
    known = set()
    _collect_numbers(facts, known)

    bad = []
    for m in _NUM.finditer(text):
        raw = m.group()
        # 시각·날짜·비율 문맥이면 건너뛴다. **바로 붙어 있는 글자만 본다** — 창으로 재면
        # 옆 숫자까지 같이 면제된다(`_AFTER_NUM_SKIP` 머리말).
        if _AFTER_NUM_SKIP.match(text, m.end()):
            continue
        if m.start() > 0 and text[m.start() - 1] in _BEFORE_NUM_SKIP:
            continue
        n = raw.replace(",", "")
        if n in ("0", "1") or n in known:
            continue
        bad.append(n)
    return bad


# ⛔⛔ **계약 지식의 숫자도 "아는 수"다**(2026-08-07 실측으로 잡음).
#
# 프롬프트에 설비 구성을 넣었더니 모델이 답에 그것을 적었다.
#
#     "게이트 1번은 장비 오류로 쌓인 값이며, 실제 설비는 게이트 5번과 2번만 있습니다"
#
# 그런데 5·2 가 사실 딕셔너리에 없어서 `number_mismatch` 로 잡혔고, **우리가 넣은 지식이
# 우리 검사에 걸려 폴백으로 떨어졌다.** 여섯 판에 두 번 그랬다.
#
# ⭐ 계약 지식은 늘 참인 값이라 대조 집합에 함께 넣는다. 여기 없는 수를 지어내는 것은
# 그대로 잡힌다 — 넓히는 게 아니라 **빠져 있던 것을 채우는 것**이다.
#
# ⚠ 프롬프트의 계약 지식이 바뀌면 여기도 같이 고쳐야 한다. 두 자리가 갈리면 조용히
# 폴백이 늘거나 지어낸 수가 통과한다.
CONTRACT_NUMBERS = frozenset({
    "5", "2",      # 게이트 번호 — 로봇이 판정하는 5번, 셔틀이 들어오는 2번
    # ⚠ 역할은 2026-08-08에 교육생(trainee)이 붙어 **다섯**이 됐다(프롬프트도 2026-08-09에
    #   고쳤다). 그래도 이 값은 남긴다 — 경고 한 건이 닫히기까지의 단계가 아직 넷이다
    #   (OPEN·ACKED·IDENTIFIED·RESOLVED, `prompts.py` "경고 하나가 닫히기까지").
    "4",           # 경고 수명주기 넷
    "6", "10",     # 날씨 여섯 · 알림 열 종류
    "12",          # 폴백 사유 열둘
})


def check(text: str, facts: dict) -> str | None:
    """내보내도 되나. 괜찮으면 None, 걸리면 `fallback_reason`으로 쓸 사유.

    ⚠ 순서가 뜻을 갖는다 — 빈 답·비한국어를 먼저 보고 그 다음이 내용 검사다.
    """
    if not text or not text.strip():
        return "empty_response"
    if has_hanja(text):
        return "hanja_leak"
    if not looks_korean(text):
        return "not_korean"
    bad = [n for n in ungrounded_numbers(text, facts) if n not in CONTRACT_NUMBERS]
    if bad:
        return "number_mismatch"
    return None


# ── ③ 회로 차단기 ────────────────────────────────────────────────────────
#
# ⛔ 역터널이 끊기면 요청마다 읽기 상한을 꽉 채우고 폴백으로 간다. 어차피 결과가 폴백이면
# **처음부터 폴백으로 가는 편이 낫다.**
#
# ⚠ 이 주석이 "20초"라 적혀 있었는데 낡은 값이었다(2026-08-07 문서 검수에서 잡음).
# 지금은 읽기 12초·연결 3초다(`client.py`). 상한은 저기서만 정하니, 여기 숫자를 다시
# 적지 않는다 — 적으면 또 갈린다.
#
# ⚠ 프로세스 안 상태다(워커 1개 전제 — `routers/assistant.py`의 예산·연타 카운터와 같은 자리).
# 재기동하면 닫힌 채로 시작한다. 그게 맞는 쪽이다 — 재기동은 보통 뭔가 고친 뒤다.
_FAIL_THRESHOLD = 3       # 연달아 이만큼 실패하면 연다
# ⛔ 60초였는데 **시연에 너무 길다**(2026-08-07 시연 리스크 점검). 잠깐 끊겼다 돌아와도
# 요원이 1분 동안 규칙 문장만 본다. 실측으로 그 상태를 재현해 확인했다.
#
# ⭐ 짧게 잡아도 손해가 작아졌다 — 연결 타임아웃을 3초로 갈라 놔서(`client.CONNECT_TIMEOUT_SEC`)
# 죽은 서버를 다시 찔러도 3초면 폴백으로 돌아온다. 예전에는 그 자리에서 20초를 꽉 채웠다.
#
# ⚠ 지킴이가 llama-server 를 되살리는 데 40초쯤 걸린다(`guard.sh` 의 sleep 40). 그보다
# 짧게 잡으면 재기동 중에 한두 번 더 찌르는데, 3초짜리라 감당된다.
_OPEN_SEC = 30.0          # 열린 동안은 LLM을 아예 안 부른다


class CircuitBreaker:
    """연달아 실패하면 잠시 LLM 호출을 멈춘다.

    시각을 밖에서 주입받는다 — 시험이 실제 시간을 안 기다려도 되게.
    """

    def __init__(self, threshold: int = _FAIL_THRESHOLD, open_sec: float = _OPEN_SEC):
        self.threshold = threshold
        self.open_sec = open_sec
        self._fails = 0
        self._opened_at: float | None = None

    def is_open(self, now: float) -> bool:
        """지금 열려 있나(=LLM을 안 부르는 중인가)."""
        if self._opened_at is None:
            return False
        if now - self._opened_at >= self.open_sec:
            # 창이 지났으면 닫고 한 번 다시 시도한다(half-open).
            self._opened_at = None
            self._fails = 0
            return False
        return True

    def record_success(self) -> None:
        self._fails = 0
        self._opened_at = None

    def record_failure(self, now: float) -> None:
        self._fails += 1
        if self._fails >= self.threshold:
            self._opened_at = now

    def reset(self) -> None:
        """시험 위생용. 케이스 사이에 상태가 새면 뒤 케이스가 가짜로 실패한다."""
        self._fails = 0
        self._opened_at = None
