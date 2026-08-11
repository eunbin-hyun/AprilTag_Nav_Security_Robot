"""관제 보조 LLM 정본(P1 #7 "LLM 관제 보조" — 준비패키지 §2 AI 활용 지도).

기능 3종.
- daily_summary — 하루치 이벤트·경고 집계를 한국어 브리핑 문단으로.
- alert_brief — 경고 한 건의 발생 맥락을 한 문장 브리핑으로.
- chat — 조회 데이터만 컨텍스트로 삼는 단순 QA.

정직 라인 — 여기 LLM은 읽기 전용 관제 보조다. 툴콜링도 로봇 제어도 안 붙인다.
백엔드는 DB에서 사실을 모아 딕셔너리로 넘기고, 이 패키지는 DB를 모른다.

⭐ 한국어 표기 정본은 **화면**이다 —
`docs/AI전달_2026-08-06_프론트발_LLM용_관제용어정본.md`.
`fallback.py`의 표 셋과 `prompts.py`의 용어집이 그 문서를 옮겨 적은 것이라,
낱말이 바뀌면 **두 파일을 같이** 고쳐야 한다. 한쪽만 고치면 LLM이 붙은 판과
안 붙은 판(폴백)이 서로 다른 낱말을 쓴다.

⚠ 아래 __all__ 아홉은 백엔드 `app/ai_bridge.py`가 그대로 임포트하는 **계약**이다.
이름이나 시그니처를 바꾸면 백엔드가 기동 자체를 못 한다(2026-08-07 백엔드 확인).
"""
from .client import LLMUnavailable
from .config import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SEC,
    AssistantConfig,
)
from .health import snapshot as health_snapshot
from .pipeline import AssistantText, alert_brief, chat, daily_summary, warm_up
from .privacy import mask_tag_id

__all__ = [
    "AssistantConfig",
    "AssistantText",
    "LLMUnavailable",
    "DEFAULT_BASE_URL",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT_SEC",
    "alert_brief",
    "chat",
    "daily_summary",
    "warm_up",
    "health_snapshot",
    "mask_tag_id",
]
