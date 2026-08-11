"""학번으로 경고를 찾는 창구가 생겨서 인덱스를 건다

## 왜 0015 에서 안 걸고 지금 거나

0015 를 넣을 때는 **쓰는 자리가 0건**이었다. 조회 창구가 없는데 인덱스를 걸면 신원을 적을
때마다 값을 무는 값만 치른다. 그래서 "창구를 내는 날 같이 건다"고 적어 두었고, 그날이다
(`GET /api/alerts/by-student-no/{student_no}` · 프론트 27차 §3-2).

## 부분 인덱스인 까닭

`identified_student_no` 는 **거의 다 NULL 이다** — 신원을 적은 경고만 값이 있고, 그것도
학번을 안 적은 갈래가 있다. 전체 인덱스는 NULL 행까지 담아 쓸데없이 커진다.

⚠ `WHERE identified_student_no IS NOT NULL` 조건이 **조회 쿼리와 맞아야** 플래너가 이 인덱스를
쓴다. 창구는 `= :key` 로 찾는데 그건 NULL 을 못 만나므로 조건이 함의된다(Postgres 가 그걸
안다). 나중에 `IS NULL` 로 찾는 자리가 생기면 이 인덱스는 안 타니 그때 따로 봐라.

## 되돌리기

`downgrade` 가 인덱스만 지운다. 값은 안 건드린다.

Revision ID: p1stdidx0016
Revises: n1stud3nt0015
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "p1stdidx0016"
down_revision = "n1stud3nt0015"
branch_labels = None
depends_on = None

_INDEX = "ix_alert_identified_student_no"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "alert",
        ["identified_student_no"],
        postgresql_where=sa.text("identified_student_no IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="alert")
