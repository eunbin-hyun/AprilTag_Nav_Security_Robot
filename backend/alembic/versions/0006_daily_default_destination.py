"""그날의 기본 목적지 표 (셔틀출동 설계 2026-07-31 §2.1)

Revision ID: d15p0006dest
Revises: c10life0005
Create Date: 2026-07-31

## 왜 표를 새로 파나

아침에 날씨로 정한 기본 목적지가 인메모리면 서버가 한 번만 다시 떠도 그날의 결정이
사라진다. 요원이 손으로 고른 값도 같이 날아가서, 재기동 뒤 첫 셔틀이 엉뚱한 곳으로
나간다. 설계 §2.1-4가 "정한 값은 서버가 다시 떠도 살아남아야 한다(DB에 둔다)"로 못박은
자리다.

## 칸 뜻

- `service_date` — **KST 날짜**다. UNIQUE라 하루에 한 행뿐이고, 이 제약이 곧 "자동 조회는
  행이 없을 때만 만든다"는 규칙의 잠금 장치다(ON CONFLICT DO NOTHING이 이 인덱스를 탄다).
- `source` — `auto`(날씨로 정함) / `manual`(요원이 고름). 요원 선택이 그날 안에는 이긴다.
- `weather_*` — 무슨 근거로 정했나. `weather_ok=false`면 조회에 실패해 마지막 성공값이나
  UNKNOWN으로 갔다는 뜻이고 화면이 그 사실을 표시한다.

## downgrade

표 하나를 통째로 지운다. 다른 표를 안 건드리므로 왕복이 손실 없이 닫힌다 — 지워지는 건
그날 기본 목적지 기록뿐이고, 다시 올리면 그날 첫 조회가 자동으로 새로 채운다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd15p0006dest'
down_revision: Union[str, None] = 'c10life0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'daily_default_destination'
_DATE_INDEX = 'ix_daily_default_destination_service_date'


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('service_date', sa.Date(), nullable=False),
        sa.Column('destination', sa.String(length=32), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('weather_status', sa.String(length=16), nullable=True),
        sa.Column('weather_desc', sa.String(length=64), nullable=True),
        sa.Column('weather_ok', sa.Boolean(), nullable=True),
        sa.Column(
            'decided_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    # UNIQUE 인덱스다. 자동 조회의 "행이 없을 때만 만든다"가 이 인덱스 위에서 원자로 돈다.
    op.create_index(_DATE_INDEX, _TABLE, ['service_date'], unique=True)


def downgrade() -> None:
    op.drop_index(_DATE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
