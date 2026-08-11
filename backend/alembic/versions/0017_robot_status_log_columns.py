"""주행 기록을 남기려고 robot_status_log 에 칸 넷을 더한다

## 왜 지금 거나

2026-08-06 에 로봇이 처음 서버에 붙었고 **그날 주행 실패가 5건 났는데 사유도 그때 위치도
안 남아 원인을 못 봤다.** 반대로 같은 날 빔 판정 문제는 `gate_pass_event` 가 남아 있어서
"짝이 안 맞는 게 아니라 한쪽만 온다"까지 좁힐 수 있었다. **한 시스템 안에서 기록의 유무가
진단 가능 여부를 갈랐다.** 사용자 확정 — "주행 관련 기록이 남아야 우리가 참고해서 프론트나
서버를 수정하지?"

## 표는 이미 있었다

`robot_status_log` 는 0001 이 만든 뒤로 **INSERT 도 SELECT 도 한 자리가 없는 빈 표**였다
(모델 주석이 `[뼈대·미배선]` 로 못 박고 있었다). 젯슨이 올린 `odom`·`imu` 는 대시보드로
중계만 되고 사라졌다. 그래서 새 표를 만들지 않고 **이 표를 살린다.**

## 칸 넷

| 칸 | 왜 |
| --- | --- |
| `mission_status` | 주행 성공·실패·날씨 차단을 시각과 함께 남긴다 |
| `position` | 서버가 재부팅 오프셋을 보정한 **지도 좌표**. 원본 `odom` 과 나란히 둔다 |
| `log` | ⭐ **실패 사유.** 계약(`RobotStateIn.log`)에 처음부터 있었는데 서버가 안 받아 적었다 |
| `sensor_health` | 센서가 죽어서 멈춘 건지 가른다 |

⚠ `odom` 과 `position` 을 **둘 다** 남긴다. 앞은 젯슨 원본(로봇이 켜진 자리가 원점인 상대
좌표)이고 뒤는 서버 보정값이라 값이 다르다 — 하나만 남기면 보정이 맞았는지 나중에 못 가린다.

## 인덱스

`(robot_id, ts)` 복합 하나다. 되짚는 질의가 "그 로봇의 그 시각 앞뒤"뿐이라 그 축이면 된다.
`ts` 단독은 안 만든다 — 로봇이 한두 대라 `robot_id` 단독은 선택도가 없고, 어느 질의든
`robot_id` 가 먼저 걸린다.

## 부하

`robot.updated_at` 을 실측하니 **2초에 한 번**이다(계약 주석의 "1Hz 다운샘플"과 맞는다).
하루 8시간이면 만 사천 행쯤이라 부담이 아니다.

## 되돌리기

`downgrade` 가 인덱스와 칸 넷을 지운다. 0001 이 만든 원래 칸(`imu`·`odom`·`comm_status`·`ts`)은
안 건드린다.

Revision ID: q1drvlog0017
Revises: p1stdidx0016
Create Date: 2026-08-06
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "q1drvlog0017"
down_revision = "p1stdidx0016"
branch_labels = None
depends_on = None

_TABLE = "robot_status_log"
_INDEX = "ix_robot_status_log_robot_ts"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("mission_status", sa.String(32), nullable=True))
    op.add_column(_TABLE, sa.Column("position", JSONB, nullable=True))
    op.add_column(_TABLE, sa.Column("log", sa.Text, nullable=True))
    op.add_column(_TABLE, sa.Column("sensor_health", JSONB, nullable=True))
    op.create_index(_INDEX, _TABLE, ["robot_id", "ts"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_column(_TABLE, "sensor_health")
    op.drop_column(_TABLE, "log")
    op.drop_column(_TABLE, "position")
    op.drop_column(_TABLE, "mission_status")
