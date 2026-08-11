"""빔 통과 행에 소비한 크레딧의 카드 UID 칸 추가 (S15P11C207-241)

Revision ID: e241tag00007
Revises: d15p0006dest
Create Date: 2026-08-01

## 왜 칸을 붙이나

크레딧이 익명이었다. 태깅이 오면 그 게이트에 크레딧 한 장이 쌓이고, A→B 통과가 오면
**수량만** 한 장 줄었다. 그래서 A가 찍고 B가 지나가도 시스템은 "정상 통과 한 건"까지만
알고 누구 크레딧이 탔는지는 아무 데도 안 남았다. 통과 행에서 UID로 가는 길이 없으니
화면도 "정상 통과"라는 글자밖에 못 그린다.

`tagging_event.tag_id`는 원래 있었다. 없던 건 **소비된 크레딧과 통과 행을 잇는 자리**다.
상태머신이 크레딧을 태우는 그 UPDATE의 RETURNING으로 UID를 받아 이 칸에 적는다.

## 계약

- **nullable이다.** 소비가 없는 판정(미태깅·퇴장·보류)은 NULL 그대로다. 이미 쌓인 통과 행도
  전부 NULL로 남는다 — 소급해서 채우지 않는다. `matched_tagging_event_id`로 조인하면 소비된
  크레딧의 UID는 실제로 되살릴 수 있다(`tagging_event.tag_id`는 INSERT에서만 값이 들어가고
  뒤에 고치는 자리가 없다 — 재태깅은 그 행을 고치는 게 아니라 새 행을 만든다). 안 하는 이유는
  값어치다. 옛 행은 시연 전에 `tools/purge_test_data.py`로 비울 대상이고, 조인으로 메워도
  **소비 경로 값만** 차서 기기가 직접 읽은 UID(F22의 두 번째 출처, 미태깅 행에 남는 값)는
  여전히 NULL이라 절반만 찬 칸이 된다.
- **파생 사본이다.** 정본은 `matched_tagging_event_id`가 가리키는 태깅 행이고, 이 칸은 조인
  없이 통과 한 줄만 읽어도 UID가 보이게 하려고 값을 복사한 것이다. 둘은 늘 짝으로 찬다.
- **인덱스를 안 건다.** 이 칸으로 통과를 검색하는 기능이 아직 없다(0004가 신원 칸에 인덱스를
  안 건 것과 같은 판단). UID로 사람의 동선을 훑는 조회가 생기면 그때 따로 붙인다.

## downgrade

칸 하나를 떨어뜨린다. 추가형이라 왕복이 손실 없이 닫히고, 지워지는 건 통과 행의 UID 사본뿐이다
(정본인 `matched_tagging_event_id`는 안 건드리므로 소비 기록 자체는 남는다).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e241tag00007'
down_revision: Union[str, None] = 'd15p0006dest'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = 'gate_pass_event'
_COLUMN = 'tag_id'
# tagging_event.tag_id와 같은 폭이다. 값을 그대로 복사하는 칸이라 폭이 갈리면 긴 UID가 잘린다.
_LENGTH = 64


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=_LENGTH), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
