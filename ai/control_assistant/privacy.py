"""프롬프트에 실릴 값에서 개인정보를 줄인다.

정직 라인(준비패키지 §2)이 "미태깅 식별은 집계로 충분, 개인 특정은 안 한다"라서
태그 UID 원문은 외부 LLM으로 나가지 않는다. 화면·DB에는 원문이 그대로 남고 여기서
가리는 건 프롬프트에 실리는 사본뿐이다.
"""
from __future__ import annotations

MASK = "***"


def mask_tag_id(tag_id: str | None) -> str | None:
    """태그 UID를 앞 2글자만 남기고 가린다.

    앞 2글자를 남기는 건 같은 브리핑 안에서 "같은 태그인가 다른 태그인가"를 구분하기
    위해서다. 뒷자리까지 남기면 원문 복원 여지가 커져서 안 남긴다.
    빈 값은 그대로 None을 돌려준다 — 자리 표시자 문자열을 만들면 LLM이 그걸 실제
    태그로 읽는다.
    """
    if not tag_id:
        return None
    if len(tag_id) <= 2:
        return MASK
    return f"{tag_id[:2]}{MASK}"
