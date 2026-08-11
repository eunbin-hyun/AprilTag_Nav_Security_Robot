"""[초안·팀 확정 대기] 안건① 인입 3테이블에 room_id 옵션 컬럼

Revision ID: a1r00m1d0002
Revises: bd476bba124d
Create Date: 2026-07-27

전부 nullable이라 기존 행은 NULL로 남고, 판정 범위는 credit_scope_room 플래그가
켜질 때만 바뀐다. 되돌리려면 downgrade 한 방(컬럼 drop)이면 끝난다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1r00m1d0002'
down_revision: Union[str, None] = 'bd476bba124d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('tagging_event', 'gate_pass_event', 'shuttle_arrival')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column('room_id', sa.String(length=32), nullable=True))
        op.create_index(op.f(f'ix_{table}_room_id'), table, ['room_id'], unique=False)


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(op.f(f'ix_{table}_room_id'), table_name=table)
        op.drop_column(table, 'room_id')
