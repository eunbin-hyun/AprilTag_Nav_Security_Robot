"""경고에 사후 신원 확인 칸 4개 추가

Revision ID: 1de4711d0004
Revises: b007id000003
Create Date: 2026-07-30

미태깅 통과는 태그를 안 댔으니 시스템에 신원 근거가 없다. 게이트에서 막지 않고 흘려보낸 뒤
안쪽 보안 요원이 붙잡아 확인한 신원을 관제 화면에서 적어 넣는 흐름이라, 그 입력을 담을 칸이
경고 행에 필요하다.

전부 nullable이라 이미 쌓인 경고 행은 NULL 그대로 남고 기존 조회·판정 경로는 안 바뀐다.

⚠ **downgrade는 손실이다.** 컬럼 4개를 drop하니 요원이 손으로 적어 넣은
`identified_person`·`identified_by`·`identified_at`·`identify_note`가 통째로 사라진다.
그 값들은 시스템이 다시 만들 수 없는 자료라(사람이 붙잡아 확인한 신원이다) 되돌릴 길이
없다. **내리기 전에 덤프를 떠라** — 0008이 같은 이유로 거는 경고와 같은 급이다.

⚠ identified_person은 자유 문자열이다. 실명·학번을 넣을지는 팀 결정이고 시연에서는 가명을
권한다. 인덱스도 안 건다 — 이 칸으로 사람을 검색하는 기능을 만들 계획이 없다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '1de4711d0004'
down_revision: Union[str, None] = 'b007id000003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (컬럼 이름, 타입). Column 객체를 모듈 상수로 들면 upgrade를 두 번 부를 때 이미 테이블에
# 붙은 객체를 다시 쓰게 되므로, 이름·타입만 들고 add_column 자리에서 새로 만든다.
_COLUMNS = (
    ('identified_person', sa.String(length=64)),
    ('identified_by', sa.String(length=64)),
    ('identified_at', sa.DateTime(timezone=True)),
    ('identify_note', sa.String(length=200)),
)


def upgrade() -> None:
    for name, type_ in _COLUMNS:
        op.add_column('alert', sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    for name, _type in reversed(_COLUMNS):
        op.drop_column('alert', name)
