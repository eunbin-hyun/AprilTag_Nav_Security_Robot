"""명부 카드 UID 정규화 (2026-08-06 · 팀원 교차 검증).

⛔ **왜 필요한가** — 명부 대조가 정확 비교(`Staff.tag_id == tag_id`)뿐이라, **대소문자 하나만
달라도 조용히 "명부 밖 카드"가 된다.** 통과 이벤트에 이름이 영영 안 뜨는데 에러도 경고도
안 난다. 사람이 손으로 넣는 값이라 실제로 갈릴 자리다.

⭐ **대문자로 맞추는 까닭** — 기기가 그렇게 보낸다. 8/6 실서버 태깅 11건이 전부 대문자였다.
받는 값을 손대는 대신 적는 값을 그쪽에 맞춘다 — 기기 원본은 증거라 안 건드리는 게 맞다.

⚠ **지금이 넣기 제일 좋은 때다.** 명부에 카드가 심긴 행이 0건이라 옮길 값이 없다.
카드를 실제로 지정하기 시작하면 그때는 마이그레이션이 붙는다.
"""
import pytest

from app.schemas import StaffCreateIn, StaffPatchIn

pytestmark = pytest.mark.asyncio(loop_scope="session")


def test_lowercase_input_becomes_uppercase():
    """⛔ 이 시험이 이 파일의 존재 이유다 — 소문자로 심으면 대문자 태깅과 안 맞는다."""
    body = StaffCreateIn(name="손세욱", tag_id="a1b2c3d4")

    assert body.tag_id == "A1B2C3D4", (
        "소문자로 심으면 기기가 보내는 대문자와 안 맞아 조용히 '명부 밖 카드'가 된다"
    )


def test_surrounding_space_is_trimmed():
    """⚠ 손입력에서 제일 흔한 오염이다."""
    assert StaffCreateIn(name="손세욱", tag_id="  a1b2  ").tag_id == "A1B2"


def test_already_uppercase_stays():
    """기기가 보내는 모양 그대로 넣으면 안 바뀐다."""
    assert StaffCreateIn(name="손세욱", tag_id="DEADBEEF").tag_id == "DEADBEEF"


def test_none_stays_none():
    """⚠ 카드를 아직 안 지정한 사람이 정상이다 — 빈 값을 글자로 만들면 안 된다."""
    assert StaffCreateIn(name="손세욱").tag_id is None


def test_patch_normalizes_too():
    """⚠ **수정 창구도 같이 타야 한다.** 등록만 고치면 나중에 바꾼 값이 다시 어긋난다."""
    assert StaffPatchIn(tag_id="c0ffee").tag_id == "C0FFEE"


def test_patch_without_tag_is_untouched():
    """보낸 칸만 바뀌는 규칙을 깨지 않는다."""
    body = StaffPatchIn(name="이름만")

    assert "tag_id" not in body.model_fields_set
