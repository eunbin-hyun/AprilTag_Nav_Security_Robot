"""시연으로 만든 행에 표시 칸을 붙인다 (프론트 22차 · 사용자 확정)

## 왜 필요한가

시연 도구가 만든 게이트 판정·알림이 **화면에만 남고 서버에는 안 갔다.** 새로고침하면
통째로 사라져서, 리허설에서 장면을 쌓아 두고 다시 열면 비어 있었다. 사용자가 "기록이
남아야 한다"고 두 번 짚은 자리다(프론트 22차 §0-2).

서버에 넣기로 하면서 **진짜 자료와 갈라 볼 표시**가 필요해졌다.

## 왜 `device_id` 로 안 가르나

`device_id="demo-console"` 같은 값으로도 고를 수는 있다. 그런데 그건 **"어느 기기가
보냈나"라는 다른 뜻의 칸**이라, 나중에 시연 도구를 여러 대로 늘리거나 기기 이름을
바꾸는 순간 조용히 갈라진다. 지우는 기준이 이름 규칙에 매이는 것도 약하다.

⭐ **불리언 한 칸이 뜻을 정확히 담고 인덱스도 싸다.**

## 되돌리기

`downgrade`가 칸을 지운다. 시연 행 자체는 안 지운다 — 그건 자료 삭제라 마이그레이션이
할 일이 아니다(`purge-demo-data` 창구 몫이다).

Revision ID: m1demo0f0014
Revises: k1ck5tat0013
Create Date: 2026-08-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'm1demo0f0014'
down_revision: Union[str, None] = 'k1ck5tat0013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 칸을 붙일 표. 시연 도구가 만들 수 있는 것만이다.
#
# ⚠ `alert`도 넣는다 — 무단 통과 시연이 경고를 같이 만들기 때문이다. 통과 행만 표시하면
#   "시연 통과는 지웠는데 그 경고는 남는" 갈래가 생긴다.
_TABLES = ("gate_pass_event", "tagging_event", "alert")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "is_demo",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
        # ⭐ 부분 인덱스다. 시연 행은 전체의 아주 일부라 `WHERE is_demo` 로 좁히면
        #    인덱스가 그 몇 줄만 담는다. 지우는 질의(`DELETE ... WHERE is_demo`)와
        #    골라 보는 질의가 이 하나를 같이 쓴다.
        op.create_index(
            f"ix_{table}_is_demo",
            table,
            ["is_demo"],
            postgresql_where=sa.text("is_demo"),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_is_demo", table_name=table)
        op.drop_column(table, "is_demo")
