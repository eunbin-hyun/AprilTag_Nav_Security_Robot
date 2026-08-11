"""로그인 시스템 — 계정·세션 테이블 + 명부 확장 + 감사 기록 (S15P11C207-349·-347)

Revision ID: f10g1n000008
Revises: e241tag00007
Create Date: 2026-08-01

계약 정본은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §2·§6.1이다.

## 담는 것 넷

1. `app_user` — 로그인 계정. 권한 판정의 유일한 정본이다.
2. `auth_session` — 불투명 세션 토큰. **원문이 아니라 sha256을 저장한다.**
3. `staff` 칸 추가 — 명부에 직급(`rank`)·본인 링크(`app_user_id`)·태그·학번·활성·시각.
4. `staff_audit` — 명부 변경 감사 기록(append-only).

## 왜 배포 중에 안 깨지나

**전부 추가형이다.** 기존 칸을 지우지도 이름을 바꾸지도 않는다. `staff`에 붙는 칸은
nullable이거나 server_default가 있어서 이미 쌓인 행이 0건이어도 있어도 안 깨진다
(실측으로 `staff`는 지금 0행이고 행을 넣는 코드도 조회 창구도 없다).

⚠ `staff.role` 기존 칸은 **안 건드린다.** 뜻이 `rank`와 겹치지만 `alert.staff_id`가 이
테이블을 물고 있어서, 지우면 손댈 자리가 번진다. 폐기 여부는 설계 §8 결정 필요 1번이고
별건이다.

⚠ `staff.tag_id`에 유니크 인덱스를 건다. 기존 행이 전부 NULL이라도 안 걸린다 —
Postgres 유니크 인덱스는 NULL을 여러 개 허용한다.

## staff_audit.staff_id에 FK를 안 거는 이유

명부 행이 사라지면 감사 기록이 같이 끌려가거나(CASCADE) INSERT가 막히는데(RESTRICT),
둘 다 감사가 아니다. 무결성보다 기록 보존이 위인 테이블이라 느슨한 정수로 둔다.
반대로 `actor_user_id`는 FK지만 `ON DELETE SET NULL`이다 — 계정이 지워져도 기록은
남아야 하고, 진짜 증거는 같이 복사해 둔 문자열 셋(`actor_username`·`actor_display_name`
·`actor_role`)이다.

## downgrade

역순으로 닫는다. `staff_audit` → `staff` 칸 → `auth_session` → `app_user` 순이다.
순서가 중요하다 — `staff.app_user_id`와 `auth_session.user_id`가 `app_user`를 물고 있어서
`app_user`를 먼저 지우려 하면 FK가 막는다.

왕복이 손실 없이 닫히지는 **않는다.** 내리면 계정·세션·감사 기록이 통째로 사라진다.
추가형 왕복(0004·0007)과 다른 점이라 여기 적어 둔다 — 되돌리기 전에 덤프를 뜬다.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'f10g1n000008'
down_revision: Union[str, None] = 'e241tag00007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# staff에 붙는 칸. (이름, 타입, nullable, server_default) — Column 객체를 모듈 상수로 들면
# upgrade를 두 번 부를 때 이미 테이블에 붙은 객체를 다시 쓰게 되므로 0005와 같이 재료만 든다.
_STAFF_COLUMNS = (
    ('tag_id', lambda: sa.String(length=64), True, None),
    ('student_no', lambda: sa.String(length=32), True, None),
    ('rank', lambda: sa.String(length=16), True, None),
    ('is_active', lambda: sa.Boolean(), False, 'true'),
    ('app_user_id', lambda: sa.Integer(), True, None),
    ('created_at', lambda: sa.DateTime(timezone=True), False, sa.text('now()')),
    ('updated_at', lambda: sa.DateTime(timezone=True), True, None),
)

_STAFF_TAG_INDEX = 'ix_staff_tag_id'
_STAFF_USER_FK = 'fk_staff_app_user_id'


def upgrade() -> None:
    op.create_table(
        'app_user',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=32), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('display_name', sa.String(length=64), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('is_active', sa.Boolean(), server_default='true', nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_app_user_username'), 'app_user', ['username'], unique=True)

    op.create_table(
        'auth_session',
        sa.Column('id', sa.BigInteger(), nullable=False),
        # sha256 hex = 정확히 64자. 원문 토큰은 여기 안 들어온다.
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('user_agent', sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['app_user.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_auth_session_token_hash'), 'auth_session', ['token_hash'], unique=True
    )

    for name, type_factory, nullable, default in _STAFF_COLUMNS:
        op.add_column(
            'staff',
            sa.Column(name, type_factory(), nullable=nullable, server_default=default),
        )
    op.create_index(_STAFF_TAG_INDEX, 'staff', ['tag_id'], unique=True)
    op.create_foreign_key(
        _STAFF_USER_FK, 'staff', 'app_user', ['app_user_id'], ['id'], ondelete='SET NULL'
    )

    op.create_table(
        'staff_audit',
        sa.Column('id', sa.BigInteger(), nullable=False),
        # FK를 안 건다(모듈 머리 참고). 기록 보존이 무결성보다 위인 테이블이다.
        sa.Column('staff_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=16), nullable=False),
        sa.Column('actor_user_id', sa.Integer(), nullable=True),
        sa.Column('actor_username', sa.String(length=32), nullable=True),
        sa.Column('actor_display_name', sa.String(length=64), nullable=True),
        sa.Column('actor_role', sa.String(length=16), nullable=True),
        sa.Column('before', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('after', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['actor_user_id'], ['app_user.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_staff_audit_staff_id'), 'staff_audit', ['staff_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_staff_audit_staff_id'), table_name='staff_audit')
    op.drop_table('staff_audit')

    op.drop_constraint(_STAFF_USER_FK, 'staff', type_='foreignkey')
    op.drop_index(_STAFF_TAG_INDEX, table_name='staff')
    for name, _type_factory, _nullable, _default in reversed(_STAFF_COLUMNS):
        op.drop_column('staff', name)

    op.drop_index(op.f('ix_auth_session_token_hash'), table_name='auth_session')
    op.drop_table('auth_session')
    op.drop_index(op.f('ix_app_user_username'), table_name='app_user')
    op.drop_table('app_user')
