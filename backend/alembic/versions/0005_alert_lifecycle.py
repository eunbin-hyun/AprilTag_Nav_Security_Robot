"""경고에 사건 수명주기 칸 추가 + 예전 행 backfill

Revision ID: c10life0005
Revises: 1de4711d0004
Create Date: 2026-07-30

`ack` 불리언과 신원 칸이 서로 독립인 불리언 둘뿐이라 "어디까지 진행됐나"를 가리키는 값이
아예 없었다(실서버 경고 18건 전부 ack=false, 그중 id 18은 신원까지 적힌 채 활성). 사건이
열렸다가 닫히는 축을 넣는다. 상태 낱말·전이 규칙은 app/alert_lifecycle.py에 있다.

## 예전 행을 어느 상태로 읽나

resolved_at이 새 칸이라 예전 행은 절대 RESOLVED가 아니다. 그래서 이 순서다.

- identified_at IS NOT NULL → 'IDENTIFIED'  (실서버 id 18)
- 위가 아니고 ack = true    → 'ACKED'
- 나머지                     → 'OPEN'       (실서버 나머지 17건)

**`ack` 값은 한 행도 안 바꾼다.** ack는 "관제가 확인을 눌렀나"만 가리키는 값으로 그대로
두기로 했고(alert_lifecycle 모듈 머리 참고), 예전 조작 기록을 마이그레이션이 사후에 고쳐
쓰면 감사 기록이 거짓이 된다.

acked_at·resolved_at도 NULL로 둔다. 예전 행이 언제 확인됐는지 서버가 모른다 —
created_at으로 채우면 감사 기록에 없는 시각을 지어내는 셈이다.

## downgrade

칸 여섯과 인덱스를 지운다. 예전 칸(ack·identified_*)은 upgrade가 안 건드렸으니 **그쪽은**
되돌릴 게 없다.

⚠ **하지만 왕복이 손실 없이 닫히지는 않는다.** 0005 뒤에 쌓인 대응 기록
(`status`·`acked_at`·`acked_by`·`resolved_at`·`resolved_by`·`resolution`)이 컬럼째
사라진다. 누가 언제 확인했고 왜 닫았나는 감사 자료라 다시 만들 수 없다.
**내리기 전에 덤프를 떠라** — 0008이 거는 경고와 같은 급이다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c10life0005'
down_revision: Union[str, None] = '1de4711d0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (컬럼 이름, 타입, server_default). Column 객체를 모듈 상수로 들면 upgrade를 두 번 부를 때
# 이미 테이블에 붙은 객체를 다시 쓰게 되므로 0004와 같이 이름·타입만 들고 다닌다.
_COLUMNS = (
    ('status', sa.String(length=16), 'OPEN'),
    ('acked_at', sa.DateTime(timezone=True), None),
    ('acked_by', sa.String(length=64), None),
    ('resolved_at', sa.DateTime(timezone=True), None),
    ('resolved_by', sa.String(length=64), None),
    ('resolution', sa.String(length=16), None),
)

_STATUS_INDEX = 'ix_alert_status'


def upgrade() -> None:
    for name, type_, default in _COLUMNS:
        op.add_column(
            'alert',
            sa.Column(
                name,
                type_,
                nullable=default is None,
                server_default=default,
            ),
        )
    # 예전 행 backfill. 새 칸이 server_default로 이미 'OPEN'이라 나머지 둘만 올린다.
    op.execute(
        "UPDATE alert SET status = 'IDENTIFIED' WHERE identified_at IS NOT NULL"
    )
    op.execute(
        "UPDATE alert SET status = 'ACKED'"
        " WHERE identified_at IS NULL AND ack = true"
    )
    op.create_index(_STATUS_INDEX, 'alert', ['status'])


def downgrade() -> None:
    op.drop_index(_STATUS_INDEX, table_name='alert')
    for name, _type, _default in reversed(_COLUMNS):
        op.drop_column('alert', name)
