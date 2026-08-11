"""서버가 낸 명령과 기기 응답을 남길 표를 만든다

## 왜 지금 거나

2026-08-06 에 로봇이 처음 붙었고, 그날 확인해 보니 **서버가 낸 명령이 DB 에 한 줄도 안 남고
있었다.** 출동·복귀·긴급정지·즉시이동도, 젯슨이 그걸 받았는지 거절했는지도, 게이트 스피커가
소리를 실제로 냈는지도 전부 로그 파일에만 있었다. 로그는 컨테이너를 갈면 사라지고 질의도
안 된다 — "아까 그 복귀 명령이 로봇에 닿긴 했나"를 되짚을 수가 없었다.

사용자 확정 — "라파, 젯슨 관련 기록이 안 남는 게 가장 문제네."

## 두 채널이 한 표에 산다

`channel` 로 가른다.

| 값 | 무엇 | 행이 나는 시점 |
| --- | --- | --- |
| `robot` | 서버 → 젯슨 명령 | 발행 때 나고, `command_ack` 가 오면 그 행이 갱신된다 |
| `gate` | 게이트 스피커 소리 | ⚠ **ack 때만 난다** |

⚠ 게이트에 발행 행이 없는 까닭 — 소리 큐가 인메모리 **동기** 함수라(`device_sound.push_sound`)
그 자리에서 DB 를 못 만진다. 대신 라파이가 "울렸다"고 보내는 ack 를 남긴다. **큐에 넣은 것보다
실제로 울린 것이 더 값진 기록이다.**

## UNIQUE 를 안 거는 까닭

기기가 여러 대면 같은 소리 신호에 각자 ack 를 보내 같은 `command_id` 로 행이 여럿 난다.
지금은 라파이 한 대지만 설계가 그렇다. 인덱스만 건다.

## 되돌리기

`downgrade` 가 표를 통째로 지운다. 이 표를 참조하는 표가 없어 딸린 것이 없다.

Revision ID: r1cmdlog0018
Revises: q1drvlog0017
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "r1cmdlog0018"
down_revision = "q1drvlog0017"
branch_labels = None
depends_on = None

_TABLE = "device_command_log"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("command_id", sa.String(64), nullable=False),
        sa.Column("command_type", sa.String(32), nullable=True),
        sa.Column("target", sa.String(64), nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ack_result", sa.String(16), nullable=True),
        sa.Column("ack_detail", sa.Text, nullable=True),
    )
    op.create_index("ix_device_command_log_command_id", _TABLE, ["command_id"])
    op.create_index("ix_device_command_log_issued_at", _TABLE, ["issued_at"])


def downgrade() -> None:
    op.drop_index("ix_device_command_log_issued_at", table_name=_TABLE)
    op.drop_index("ix_device_command_log_command_id", table_name=_TABLE)
    op.drop_table(_TABLE)
