"""신원 입력에 학번 칸을 나눈다 (프론트 27차 · 사용자 확정)

## 왜 나누나

사용자가 **"통과자 신원 입력에 왜 학번이 없어?"**라고 짚었고, 이어서 **"학번 이름 따로따로
검색할 수 있어야지"**로 방향을 확정했다.

**학번이 진짜 신원 근거다.** 이름은 동명이인이 있고 명부 대조(`staff.student_no`)도 학번으로
한다. 한 칸에 이어 적으면 그 대조를 서버가 못 한다.

⚠ 그때까지 프론트는 `person` 한 칸에 `"이름 · 학번"` 꼴로 이어 보내고 **화면이 숫자를
되뽑아** 학번으로 봤다. 이름에 숫자가 섞이면 흔들리는 임시방편이라, 칸이 생기면 그 되뽑기를
걷는다.

## 왜 폭이 32인가

`staff.student_no`가 `String(32)`다. 나중에 둘을 맞대 볼 자리라 폭을 같게 둔다 — 한쪽이
좁으면 명부에 있는 값을 신원 칸이 못 받는 갈래가 생긴다.

## 왜 NULL 을 허용하나

⚠ **현장에서 이름만 알아낸 갈래를 막으면 입력이 통째로 멈춘다.** 신원 입력은 요원이 손으로
적는 값이라, 한 칸을 필수로 만들면 그 칸을 모르는 순간 아무것도 못 적는다. 같은 이유로
`identified_person` 말고는 전부 옵션이다.

## 인덱스를 안 거는 이유

"학번으로 경고 찾기"가 열리긴 하는데, 그 조회는 아직 창구가 없다. 지금 걸면 **쓰는 자리가
0건인 인덱스**가 쓰기마다 값을 문다. 조회 창구가 생기는 날 그 자리에서 같이 건다.

## 되돌리기

`downgrade`가 칸을 지운다. ⚠ 지우면 그 칸에 적힌 학번이 **같이 사라진다** — 되돌리기 전에
값이 있는지 보고, 있으면 `person` 쪽으로 옮겨 두고 지워라.

Revision ID: n1stud3nt0015
Revises: m1demo0f0014
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "n1stud3nt0015"
down_revision = "m1demo0f0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert",
        sa.Column("identified_student_no", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("alert", "identified_student_no")
