"""[초안·팀 확정 대기] 안건② 인입 3테이블에 boot_id 옵션 컬럼

Revision ID: b007id000003
Revises: a1r00m1d0002
Create Date: 2026-07-27

event_id는 그대로 UNIQUE 키다. boot_id는 조회·역추적용 파생 컬럼이라 인덱스도 안 건다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'b007id000003'
down_revision: Union[str, None] = 'a1r00m1d0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('tagging_event', 'gate_pass_event', 'shuttle_arrival')


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column('boot_id', sa.String(length=16), nullable=True))


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, 'boot_id')
