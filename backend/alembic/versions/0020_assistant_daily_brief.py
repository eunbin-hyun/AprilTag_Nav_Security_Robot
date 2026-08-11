"""일일 요약을 만들어 두는 표를 만든다

## 왜 지금 거나

요원이 개요 탭을 열 때마다 LLM 을 부르고 있었다. 실측 **7.4초**다. 그동안 그 칸이
비어 있고, 화면을 다시 열면 같은 값을 또 만든다.

⭐ **이 브리핑은 누를 때 만들 이유가 없다.** 같은 날 같은 자료면 답도 같다. 실시간이
필요한 건 챗봇 하나뿐이다(관제 보조 갈래 8차 §3-1).

## 왜 표를 새로 만드나

`daily_default_destination` 에 얹는 쪽을 봤는데 접었다. 그 표는 "그날 목적지를 무엇으로
정했나"라 성격이 다르고, 브리핑 수명(다시 만드는 주기)이 목적지 결정과 엮이면 한쪽을
바꿀 때 다른 쪽이 조용히 따라 움직인다.

## 오늘과 지난 날짜를 다르게 다루는 까닭

**오늘치는 자료가 계속 는다.** 아침에 만든 요약이 저녁에도 맞을 리 없다. 그래서 오늘은
수명(`ASSISTANT_BRIEF_TTL_SEC`, 기본 1800초)을 두고 지나면 다시 만든다.

**지난 날짜는 자료가 안 바뀐다.** 한 번 만들면 영구히 재사용한다.

## ⛔ 폴백 문장은 저장하지 않는다

LLM 이 죽어서 나온 규칙 문장을 캐시에 앉히면 그 문장이 수명 동안 계속 나간다. 그 사이
LLM 이 살아나도 요원은 계속 규칙 문장을 본다 — **고장이 캐시에 굳는다.**

되돌리기(`downgrade`)는 표를 지우기만 한다. 캐시라 잃어도 다시 만들어진다.
"""
from alembic import op
import sqlalchemy as sa


revision = "t1brief0020"
down_revision = "s1chatlog0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_daily_brief",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    # 하루에 한 줄이다. 같은 날 두 줄이 생기면 어느 것이 최신인지 판정이 갈린다.
    op.create_index(
        "ix_assistant_daily_brief_service_date",
        "assistant_daily_brief",
        ["service_date"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_assistant_daily_brief_service_date", table_name="assistant_daily_brief"
    )
    op.drop_table("assistant_daily_brief")
