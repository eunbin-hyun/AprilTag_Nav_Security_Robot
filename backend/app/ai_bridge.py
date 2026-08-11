"""백엔드 ↔ 리포 /ai 배선. 백엔드가 AI 코드를 임포트하는 자리는 여기 하나뿐이다.

**왜 이 방식인가.** 팀 규칙상 AI 코드 자리는 리포 /ai인데 백엔드 실행·시험은 backend/를
루트로 돈다. 셋 중에 골랐다.

1. `pip install -e ../ai` — 정석이지만 팀원 전원이 설치 한 단계를 더 밟아야 하고,
   requirements.txt에 로컬 경로가 들어가면 CI·배포에서 깨진다.
2. PYTHONPATH 환경변수 — 실행하는 자리마다(uvicorn·pytest·도커) 따로 잡아야 해서
   한 자리만 빠뜨려도 임포트 에러가 난다.
3. **여기서 리포 루트를 sys.path에 한 번 꽂기(채택)** — 팀원은 아무것도 안 해도 되고,
   나중에 1번으로 갈아탈 때 고칠 파일도 이 하나다.

⚠ 리포 루트를 sys.path[0]에 꽂으므로 최상위 이름 `ai`는 우리 폴더가 이긴다. 같은 이름의
   외부 패키지를 설치하면 그쪽이 아니라 이 폴더가 잡힌다는 뜻이다(의도한 거다).
"""
from __future__ import annotations

import sys
from pathlib import Path

# backend/app/ai_bridge.py → parents[0]=app, [1]=backend, [2]=리포 루트
_REPO_ROOT = Path(__file__).resolve().parents[2]

if (_REPO_ROOT / "ai" / "__init__.py").exists():
    _root_str = str(_REPO_ROOT)
    if _root_str not in sys.path:
        sys.path.insert(0, _root_str)

from ai.control_assistant import (  # noqa: E402 - sys.path를 잡은 뒤라야 임포트된다
    AssistantConfig,
    AssistantText,
    LLMUnavailable,
    alert_brief,
    chat,
    daily_summary,
    health_snapshot,
    mask_tag_id,
    warm_up,
)
from ai.control_assistant.config import DEFAULT_BASE_URL, DEFAULT_MODEL  # noqa: E402

__all__ = [
    "AssistantConfig",
    "AssistantText",
    "LLMUnavailable",
    "alert_brief",
    "chat",
    "daily_summary",
    "mask_tag_id",
    "build_config",
]


def build_config() -> AssistantConfig:
    """백엔드 Settings 값으로 LLM 설정을 만든다. 키가 비면 폴백 전용으로 돈다."""
    from app.config import get_settings

    s = get_settings()
    return AssistantConfig(
        api_key=s.gms_api_key,
        # base_url·model 기본값 정본은 ai/control_assistant/config.py 한 곳 — 백엔드엔 복사본을 안 둔다.
        base_url=s.gms_base_url or DEFAULT_BASE_URL,
        model=s.gms_model or DEFAULT_MODEL,
        timeout_sec=s.gms_timeout_sec,
        max_tokens=s.gms_max_tokens,
    )
