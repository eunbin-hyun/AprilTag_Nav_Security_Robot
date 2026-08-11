"""자주 도는 질의 축에 인덱스 넷 (백지검토 보류 F47·F48·F49·F50)

Revision ID: g1nd3x000009
Revises: f10g1n000008
Create Date: 2026-08-02

보류 사유 정본은 `docs/백지검토_사이클_2026-08-02.md` §4다. 거기서 "인덱스 신설
마이그레이션이라 사용자 결정 몫"으로 미뤄 뒀던 넷을 한 판으로 묶었다.

## 왜 추가만인가

**전부 인덱스 CREATE뿐이다.** 칸도 표도 제약도 안 건드리고 자료를 한 줄도 안 바꾼다.
잘못 골랐어도 되돌리기가 `DROP INDEX` 한 줄이라 downgrade가 손실 없이 닫힌다.

## 축을 어떻게 골랐나 — 주석이 아니라 실측한 실행계획으로

⭐ 붙일 축은 **코드가 실제로 거는 where·order_by를 읽고, Postgres에 실물을 세워
EXPLAIN으로 확인해서** 정했다. 안 쓰는 인덱스는 조회를 안 도와주면서 INSERT만 느리게
한다. 실측에서 뒤집힌 게 둘 있다.

⚠ **`gate_no`는 축에서 뺐다.** 상태기계가 `gate_no IS NOT DISTINCT FROM ?`로 거는데
(NULL끼리도 매칭시키려고 그렇게 적었다 — `_scope_clauses`), Postgres는 이 연산자를
**인덱스 조건으로 못 쓴다.** `(gate_no, credit_state, expires_at, observed_at)`으로 걸어
보니 인덱스가 통째로 안 잡히고 Seq Scan이 그대로 나왔다. `gate_no`를 빼고
`credit_state`를 앞에 세우니 Index Scan으로 붙었고, `gate_no`는 heap 단계 Filter로
빠진다. 그래서 검토 지적이 적었던 축(`gate_no` 선두)이 아니라 아래 축이다.

⚠ **`alert.type` 단독 인덱스는 안 만들었다.** 경보 목록은 어느 필터를 걸어도 끝에
`ORDER BY created_at DESC, id DESC LIMIT n`이 붙어서, 아래 `ix_alert_created_at_id`를
거꾸로 훑는 계획을 옵티마이저가 늘 고른다 — `type` 인덱스를 같이 깔아 두고 재도 한 번도
안 뽑혔다. 쓰이지도 않는데 INSERT마다 비용만 붙는다.

## 넷의 근거

1. `ix_tagging_event_credit_window` — `(credit_state, expires_at, observed_at)`.
   크레딧 소비 FIFO(`_consume_fifo`)·미소비 개수(`_count_unconsumed`)·만료 일괄
   (`_expire_overdue`)·회수(`_recover_oldest`)가 전부 이 축이다. 살아 있는 창을
   `expires_at` 한 칸의 양쪽 경계로 적어 둔 덕에(`_live_credit_window`) 범위 두 개가
   같은 칸에 떨어져서 인덱스 하나로 끝난다.
   ⚠ **꼬리의 `observed_at`은 정렬을 못 준다.** 앞칸 `expires_at`에 범위 술어가 걸려 있어서
   btree가 그 뒤 칸까지 순서를 보장하지 못한다 — `_consume_fifo`의
   `ORDER BY observed_at ASC, id ASC`에는 계획에 Sort가 그대로 붙는다(tiebreaker `id`는
   축에 아예 없다). 꼬리 칸이 하는 일은 **필터를 인덱스 안에서 더 좁히는 데까지**이고,
   FIFO 한 장을 고르는 건 `LIMIT 1`이라 좁혀 온 행이 적으면 Sort가 싸다.

2. `ix_gate_pass_event_observed_at` — `(observed_at)`.
   통계 세 자리(시간대별 미태깅·판정 분포·구간 버킷)가 이 칸으로 구간을 자른다.
   `(verdict, observed_at)` 복합도 재 봤는데 미태깅 집계 하나만 받고 나머지 둘은
   `verdict`를 안 걸어서 그대로 Seq Scan이었다. 단일 칸이 셋 다 받는다.

3. `ix_alert_source` — `(source_type, source_id)`.
   역추적 규칙(경보의 `(source_type, source_id)` = 이벤트의 `(kind, id)`)을 쓰는 자리가
   둘이다 — 화면이 이벤트 표에 처리 상태를 그릴 때 거는 조회(`fetch_alerts`)와, 명부
   태그가 활성 경고에 붙어 있나 보는 조인(`_tag_is_on_active_alert`)이다.

4. `ix_alert_created_at_id` — `(created_at, id)`.
   경보 목록의 정렬 축이다. **오름차순으로 만든다** — Postgres는 btree를 거꾸로 훑을 수
   있어서 `ORDER BY created_at DESC, id DESC`가 이 인덱스를 그대로 탄다(실측에서
   `Index Scan Backward`). DESC 인덱스를 따로 만들면 얻는 게 없고 autogenerate 대조만
   까다로워진다.

## 안 넣은 것 (보류로 남긴 이유)

- `staff_audit`의 JSONB 경로(F51) — 행이 몇 줄이라 실익이 없다. GIN은 쓰기 비용이 큰데
  `->>` 등가 비교엔 잘 안 붙는다. ⚠ 예전 판이 근거로 들었던 "`staff_id=0` 인덱스가 먼저
  좁힌다"는 틀렸다 — `ACCOUNT_AUDIT_STAFF_ID = 0`(`routers/staff.py`)은 계정 감사 행
  **전부가 나눠 쓰는 sentinel**이라 그 부류를 통째로 고를 뿐 안에서 더 안 좁힌다.
- `shuttle_arrival`의 `gate_no`·`signal_ts`·`notified_robot_id` — 짝짓기 질의가
  `id NOT IN (…)` 반조인이라 어차피 표를 한 바퀴 돌고, 셔틀 신호는 인입 3표 중 행이
  제일 적다.
- `auth_session`의 `user_id`·`revoked_at`·`expires_at` — 계정 6개에 계정당 세션 상한이
  5라 살아 있는 행이 수십 줄이다. 이 규모에서는 옵티마이저가 인덱스를 안 고른다.
  뜨거운 조회(`token_hash`)는 0008에서 이미 유니크 인덱스다.

셋 다 자료가 실제로 쌓인 뒤 실측해서 다시 판단할 자리다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'g1nd3x000009'
down_revision: Union[str, None] = 'f10g1n000008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (인덱스 이름, 표, 칸들). 이름·칸 순서가 app/models.py의 `__table_args__`·`index=True`와
# 한 글자도 같아야 한다 — 어긋나면 tests/test_schema_drift.py가 빨개진다.
_INDEXES = (
    ('ix_tagging_event_credit_window', 'tagging_event',
     ['credit_state', 'expires_at', 'observed_at']),
    ('ix_gate_pass_event_observed_at', 'gate_pass_event', ['observed_at']),
    ('ix_alert_source', 'alert', ['source_type', 'source_id']),
    ('ix_alert_created_at_id', 'alert', ['created_at', 'id']),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
