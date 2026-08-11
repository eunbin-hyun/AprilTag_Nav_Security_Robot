"""관제 보조 LLM 설정. GMS(싸피 OpenAI 호환 프록시) 계약을 그대로 담는다.

키는 코드에 절대 박지 않는다 — 환경변수 GMS_API_KEY로만 받는다(.env는 커밋 제외).
백엔드에서 부를 때는 백엔드 Settings에서 읽은 값을 넘겨 주고, 이 모듈 단독으로 쓸 때는
from_env()가 환경변수를 직접 읽는다(스모크 스크립트가 이 길을 쓴다).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 1학기 실측 정본. OpenAI 호환 프록시라 경로는 /chat/completions가 그대로 붙는다.
DEFAULT_BASE_URL = "https://gms.ssafy.io/gmsapi/api.openai.com/v1"
# 7/29 가이드·실측 근거로 채택 — 14크레딧에 브리핑 1.3초로 품질·속도·크레딧 균형이 제일 좋다.
# 기본 추론 토큰 0이라 max_tokens 상한 400 그대로 안전한 것도 실측 확인.
DEFAULT_MODEL = "gpt-5.4-mini"
# ⭐ **읽기 상한이다. 연결 상한은 따로 3초로 잡는다**(`client.CONNECT_TIMEOUT_SEC`).
# 안 닿는 경우는 3초에 끝나므로, 이 값은 "닿았는데 생성이 느린" 경우만 잰다.
#
# 2026-08-07 실측 — 답이 길어지는 질문 열 개를 상한 없이 재니 이렇다.
#   중앙값 4.68초 · 최대 9.82초
#   상한  8초면 2/10 이 넘쳐 규칙 문장으로 떨어진다
#   상한 10초면 0/10 인데 최대값에 0.18초밖에 안 남는다
#   상한 12초면 0/10 이고 2.2초 여유가 남는다
#
# ⚠ 사용자가 "긴 글도 답변할 수 있게 시간 조절하라"고 정했다.
# ⚠ 실서버는 환경변수 `GMS_TIMEOUT_SEC` 이 이 기본값을 덮는다 — 거기도 같이 올려야 한다.
#
# ⛔⛔ 12초로 잡았다가 **20초로 올렸다**(2026-08-07 밤). 프론트가 실사용에서 규칙 문장이
# 뜨는 것을 보고 재 준 값이 13.96초였고, 우리가 실경로로 다시 재니 최악이 25.84초였다.
#
#     자르기 전   첫 호출 25.84초 · 동시 둘 16.94초
#     목록 자른 뒤 첫 호출 13.12초 · 동시 둘  6.18초
#
# ⭐ **느린 것은 디코딩이 아니라 프리필이다.** 답은 60~80토큰뿐이라 `max_tokens` 를 줄여도
# 안 빨라진다. 시스템 프롬프트 4,870톡에 근거 묶음이 붙어 7,000톡을 넘는 것이 원인이고,
# 그래서 `facts_select` 가 목록을 여섯 건으로 자른다.
#
# ⚠⚠ **"데우기가 첫 호출을 못 구한다"가 여기 적혀 있었는데 다른 파일과 정면으로 부딪힌다**
# (2026-08-09 백지검토). 지연을 조정하는 사람이 어느 쪽을 근거로 삼느냐로 판단이 통째로
# 갈리는 자리라, 잰 것과 못 잰 것을 갈라 적는다.
#
#   잰 것   — 위 25.84초·13.12초는 실제 벽시계다. 첫 호출이 느린 것은 사실이다.
#   못 잰 것 — 그 원인이 "llama-server 가 접두사 유사도로 슬롯을 고를 때 데워 둔 슬롯을
#             안 고른다"인지는 **확인 못 했다.** `client.warm_up`(200줄)과
#             `pipeline.warm_up`(157줄)은 반대로 적혀 있다 — 데우면 구해지고, 그래서
#             창구 데우는 순서까지 접두 길이로 정해 뒀다(2026-08-08 교차 검증).
#             같은 주장이 `facts_select`(77줄)에도 한 벌 더 있다.
#
# ⛔ 갈림은 llama-server `/slots` 를 눈으로 봐야 닫힌다. 그 전까지 **이 문단을 상한 조정의
# 근거로 쓰지 마라.** 아래 20초는 실측 벽시계로 잡은 값이라 이 다툼과 무관하다.
#
# ⚠ 여기서 상한을 넘기면 **답이 다 만들어진 뒤에 버려진다.** GPU 는 이미 다 썼고 요원은
# 규칙 문장을 본다 — 제일 손해가 큰 실패다. 20초는 최악 13.12초에 여유를 더한 값이다.
DEFAULT_TIMEOUT_SEC = 20.0
# 브리핑은 짧은 문단이라 상한을 낮게 둔다(비용·지연 둘 다 줄인다).
DEFAULT_MAX_TOKENS = 400


@dataclass(frozen=True)
class AssistantConfig:
    """LLM 호출에 필요한 값 묶음.

    api_key가 비면 호출 자체를 안 하고 폴백으로 간다(키 없는 배포에서도 화면이 안 빈다).
    """

    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout_sec: float = DEFAULT_TIMEOUT_SEC
    max_tokens: int = DEFAULT_MAX_TOKENS

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "AssistantConfig":
        return cls(
            api_key=os.environ.get("GMS_API_KEY", ""),
            base_url=os.environ.get("GMS_BASE_URL") or DEFAULT_BASE_URL,
            model=os.environ.get("GMS_MODEL") or DEFAULT_MODEL,
            timeout_sec=float(os.environ.get("GMS_TIMEOUT_SEC") or DEFAULT_TIMEOUT_SEC),
            max_tokens=int(os.environ.get("GMS_MAX_TOKENS") or DEFAULT_MAX_TOKENS),
        )
