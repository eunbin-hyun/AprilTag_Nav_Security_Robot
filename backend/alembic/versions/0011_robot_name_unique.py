"""robot.name 에 UNIQUE 인덱스 (전체검토 L5)

Revision ID: i1r0b0t00011
Revises: h1feed000010
Create Date: 2026-08-05

## 무엇을 고치나

`robot` 표의 `name` 이 사실상 이 표의 열쇠인데 제약이 없었다. 로봇이 붙으면 이름으로 찾고
없으면 만드는 자리가 둘이다 — `robot_channel._upsert_robot`(상태 보고를 받을 때)와
`_ensure_robot_pk`(명령 통지 기록을 남길 때). 그 "찾고 없으면 만들기"는 **두 요청 사이에서
원자가 아니다.**

그래서 같은 이름으로 두 요청이 겹치면 행이 둘 생기고, 화면에 같은 로봇 카드가 두 장 뜬다.
지금 로봇이 한 대뿐이라 안 밟혔을 뿐이고, 젯슨이 붙었다 끊겼다 하는 시연 자리가 정확히
그 경합이 나기 쉬운 판이다.

## 왜 지금 넣나 — 중복이 0인 것을 실측했다

제약을 거는 마이그레이션은 **이미 중복이 있으면 그 자리에서 막힌다.** 실서버(EC2 `c207`)에서
세어 보고 넣었다.

```sql
SELECT count(*) FROM (SELECT name FROM robot GROUP BY name HAVING count(*)>1) t;  -- 0
SELECT count(*) FROM robot;                                                       -- 0행
```

⚠ **다른 DB 에 올릴 때는 다시 세라.** 시연 리허설로 로봇이 여러 이름으로 붙은 판이면 값이
0이 아닐 수 있다. 그때는 남길 행을 고르고 나머지를 지운 뒤에 올려야 한다 — 어느 행을
남길지는 사람이 정할 자리라 이 마이그레이션이 알아서 지우지 않는다.

## 이름을 왜 `ix_robot_name` 으로 잡았나

`unique=True, index=True` 를 준 SQLAlchemy 칸이 만드는 기본 이름과 같다(`AppUser.username`
이 같은 모양이다). `tests/test_schema_drift.py` 가 모델과 마이그레이션 스키마를
`compare_metadata` 로 견주므로, 이름이 한 글자라도 어긋나면 그 시험이 빨개진다.

## 되돌리기

`DROP INDEX` 하나다. 자료를 한 줄도 안 바꾸므로 손실이 없다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'i1r0b0t00011'
down_revision: Union[str, None] = 'h1feed000010'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = 'ix_robot_name'
_TABLE = 'robot'


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ['name'], unique=True)


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
