"""관제 챗봇 대화를 계정별로 남길 표 둘을 만든다

## 왜 지금 거나

그전까지 챗봇 대화가 **브라우저 메모리에만** 쌓였다. 새로고침하면 통째로 사라지고 다른
기기에서 로그인하면 안 보였다. 그런데 화면에는 "새 채팅" 버튼과 대화 목록이 이미 있어서
겉보기에는 되는 것처럼 읽혔다.

사용자 확정(2026-08-07) — "서버로 해야지."

## ⛔ 브라우저 저장소로 안 하는 까닭

`localStorage` 는 **공용 PC 에서 다음 사람에게 그대로 넘어간다.** 관제 PC 는 요원이 교대로
쓰는 자리다. 같은 판단을 화면 쪽에서 이미 한 번 했다 — API 키를 `sessionStorage` 에 두는
이유가 그것이다. 대화 내용은 "요원이 무엇을 물었나"라서 키보다 덜 민감하지도 않다.

`sessionStorage` 면 새로고침은 버티지만 탭을 닫으면 사라지고 기기를 넘나들지 못한다.
"새 채팅으로 목록을 관리한다"는 요구 자체가 계정 개념이라 서버가 맞다.

## 표를 둘로 가르는 까닭

한 표에 대화와 메시지를 같이 담으면 목록 질의가 매번 본문을 통째로 읽는다. 목록은 자주
열리고 본문은 한 대화를 열 때만 필요하다.

## ⚠ 이 표의 보안 계약

조회 창구는 **반드시 `user_id == actor.id`** 를 건다. 계정마다 갈리는 것이 요구사항의
본체라, 그 조건이 빠지면 기능이 아니라 사고가 된다.

## ⚠ 새 표를 만들 때 같이 손댈 자리 둘 (닫힌 집합이 둘이다)

- `backend/tests/conftest.py` 의 `_TRUNCATE_TABLES`
- `backend/tools/purge_test_data.py` 의 `PURGE_ORDER` / `TIME_COLUMN`

둘 다 손으로 적은 목록이고 각자 닫힘 검사가 붙어 있다. 하나만 고치면 다른 쪽이 터진다.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "s1chatlog0019"
down_revision = "r1cmdlog0018"
branch_labels = None
depends_on = None

_CONV = "chat_conversation"
_MSG = "chat_message"


def upgrade() -> None:
    op.create_table(
        _CONV,
        sa.Column("id", sa.BigInteger, primary_key=True),
        # ⚠ 계정을 지우면 대화도 같이 지운다. 감사 기록과 달리 개인 자료라 남길 근거가 없다.
        sa.Column(
            "user_id",
            sa.Integer,
            sa.ForeignKey("app_user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_conversation_user_id", _CONV, ["user_id"])
    # 목록 질의가 "내 것을 최근 순으로"라 복합이다. updated_at 단독은 안 만든다.
    op.create_index("ix_chat_conversation_user_updated", _CONV, ["user_id", "updated_at"])

    op.create_table(
        _MSG,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.BigInteger,
            sa.ForeignKey("chat_conversation.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # `user` 또는 `assistant`. 값이 둘뿐이고 늘 서버가 적어서 enum 을 안 쓴다.
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        # 응답 줄에만 찬다. `llm` 또는 `fallback`.
        sa.Column("generated_by", sa.String(16), nullable=True),
        # 폴백일 때 그 사유(`daily_budget`·`empty_response` 등).
        sa.Column("fallback_reason", sa.String(32), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_chat_message_conversation_id", _MSG, ["conversation_id", "id"])


def downgrade() -> None:
    # ⚠ 메시지를 먼저 내린다. 대화를 먼저 지우면 FK 가 걸린다.
    op.drop_index("ix_chat_message_conversation_id", table_name=_MSG)
    op.drop_table(_MSG)
    op.drop_index("ix_chat_conversation_user_updated", table_name=_CONV)
    op.drop_index("ix_chat_conversation_user_id", table_name=_CONV)
    op.drop_table(_CONV)
