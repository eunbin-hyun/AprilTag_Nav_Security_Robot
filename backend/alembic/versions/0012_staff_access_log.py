"""명부 조회 이력 표 (사용자 결정 2026-08-05 · 결정 대기 7번)

Revision ID: j1acce5s0012
Revises: i1r0b0t00011
Create Date: 2026-08-05

## 왜 만드나

**"누가 명부를 봤나"가 지금 아무 데도 안 남는다.** 태그 UID 가 요청 줄에 평문으로 쌓이는
것을 막으려고 nginx·uvicorn 액세스 로그를 양쪽 다 껐는데(2026-08-02), 그러면서 조회 기록도
같이 사라졌다. 개인정보 쪽으로는 안 남는 게 안전하고 사고 추적 쪽으로는 남는 게 낫다 —
사용자가 **남기는 쪽**으로 정했다.

## 변경 감사(`staff_audit`)와 왜 표를 가르나

셋이 안 맞는다.

1. 목록 조회는 대상 `staff_id` 가 없는데 `staff_audit.staff_id` 는 필수다
2. 조회는 "이후 값"이 없는데 `staff_audit.after` 가 필수다
3. 조회는 화면을 열 때마다 나서 변경보다 훨씬 잦다 — 한 표에 섞으면 변경 이력이 조회
   기록에 파묻힌다

## ⛔ 태그 UID 원문은 안 담는다

`by_tag` 갈래에서 UID 앞 네 글자만 `tag_prefix` 에 남기고 뒤를 가린다. 원문을 담으면
액세스 로그를 끈 것이 헛일이 된다. 되짚을 때 "어느 태그 계열을 봤나"는 남고 값 자체는
안 남는 절충이다.

## 인덱스 둘

- `created_at` — 조회 이력 화면이 최신순으로 자른다. 이 표는 계속 자라기만 해서 인덱스
  없이는 시간이 갈수록 느려진다
- `target_staff_id` — "이 사람 명부를 누가 봤나"를 되짚는 축이다

## 되돌리기

`DROP TABLE` 하나다. 이 표는 append-only 기록이라 지우면 그 기록이 사라진다 — 되돌릴
일이 있으면 먼저 덤프를 떠라.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'j1acce5s0012'
down_revision: Union[str, None] = 'i1r0b0t00011'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'staff_access_log'


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('target_staff_id', sa.Integer(), nullable=True),
        sa.Column('tag_prefix', sa.String(length=16), nullable=True),
        sa.Column('result_count', sa.Integer(), nullable=True),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('actor_username', sa.String(length=32), nullable=True),
        sa.Column('actor_display_name', sa.String(length=64), nullable=True),
        sa.Column('actor_role', sa.String(length=16), nullable=True),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(['actor_user_id'], ['app_user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(f'ix_{_TABLE}_target_staff_id', _TABLE, ['target_staff_id'])
    op.create_index(f'ix_{_TABLE}_created_at', _TABLE, ['created_at'])


def downgrade() -> None:
    op.drop_index(f'ix_{_TABLE}_created_at', table_name=_TABLE)
    op.drop_index(f'ix_{_TABLE}_target_staff_id', table_name=_TABLE)
    op.drop_table(_TABLE)
