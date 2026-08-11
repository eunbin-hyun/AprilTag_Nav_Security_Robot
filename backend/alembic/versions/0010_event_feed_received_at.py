"""이벤트 피드 정렬 축에 인덱스 셋 (received_at, id)

Revision ID: h1feed000010
Revises: g1nd3x000009
Create Date: 2026-08-04

## 무엇을 고치나

`/api/events`와 `GET /api/dashboard/snapshot`이 인입 3표를 `UNION ALL`로 합친 뒤
`ORDER BY received_at DESC, id DESC LIMIT n`으로 자른다(`routers/query.py::fetch_events`).
그런데 **세 표 어디에도 `received_at` 인덱스가 없었다.** 0009가 넣은 넷은 축이 다르다 —
`ix_gate_pass_event_observed_at`은 기기 관측 시각이고 피드는 서버 수신 시각으로 줄을
세운다. `ix_tagging_event_credit_window`는 크레딧 축, 나머지 둘은 경고 표다. 즉 0009는
이 조회를 **하나도 안 덮었다.**

그래서 화면이 한 번 붙을 때마다 3표를 통째로 훑고 top-N 정렬을 돌았다. 12.4만 행을 심어
실측한 계획이 `Parallel Seq Scan` 셋 + `Sort`였다.

## 재 본 숫자 (Postgres 실물, tagging 6만·gate_pass 6만·shuttle 4천·alert 1.2만)

같은 DB·같은 자료에서 이 인덱스 셋만 뺐다 넣으며 짝지어 쟀다(15바퀴, 앞 3바퀴 버림).

| 잰 것 | 전 | 후 |
|---|---|---|
| `GET /api/dashboard/snapshot` 응답 p50 | 33.75 ms | 9.93 ms |
| 스냅샷 이벤트 SQL (`EXPLAIN ANALYZE`) | 26.11 ms | 0.10 ms |
| `/api/events` limit 50 SQL | 25.80 ms | 0.11 ms |
| `fetch_events` 파이썬 왕복 p50 | 25.20 ms | 1.57 ms |
| 스냅샷 4조회 합 파이썬 왕복 p50 | 31.38 ms | 7.77 ms |

계획이 `Sort ← Parallel Append(Seq Scan ×3)`에서 `Merge Append(Index Scan Backward ×3)`로
바뀌고, 읽는 블록이 1883에서 9로 준다.

⚠ 나머지 셋은 이 마이그레이션 전에도 이미 빨랐다. 활성 경고 조회 0.05 ms(0009의
`ix_alert_created_at_id`가 `Index Scan Backward`로 받는다), 미확인 경고 세기 1.4 ms
(`alert` Seq Scan인데 1.2만 행에서 이 값이라 손댈 자리가 아니다), 로봇 카드는 행이 몇 줄이다.
**느린 건 이벤트 피드 하나뿐이었다.**

## 축을 왜 `(received_at, id)`로 잡았나

`ix_alert_created_at_id`(0009)와 같은 이유다. 정렬이 `received_at DESC, id DESC` 두 칸이라
`received_at` 단독으로는 동시각 행의 순서가 인덱스 안에서 안 정해져 계획에 정렬이 남는다.
**오름차순으로 만든다** — Postgres가 btree를 거꾸로 훑어서 DESC 정렬이 그대로 붙는다
(실측에서 `Index Scan Backward`).

## 왜 셋 다인가 — 하나씩 빼고 재 봤다

⚠ 처음 적었던 "하나만 빠지면 Merge Append가 통째로 무너진다"는 **틀렸다.** 실측하니
계획은 `Merge Append`로 남고 인덱스가 없는 가지만 `Sort ← Seq Scan`으로 바뀐다. 그래서
손해는 계획 붕괴가 아니라 **그 가지의 행 수에 비례**한다. 인덱스를 하나씩 빼고 잰 값이다.

| 뺀 인덱스 | 그 표 행 수 | 스냅샷 이벤트 SQL |
|---|---|---|
| (없음, 셋 다) | — | 0.055 ms |
| shuttle_arrival | 4천 | 1.48 ms |
| tagging_event | 6만 | 25.4 ms |
| gate_pass_event | 6만 | 27.2 ms |

셋 다 넣는다. shuttle_arrival은 행이 제일 적어 이득도 제일 작지만(1.48 → 0.055 ms),
셔틀 행은 시연 중에도 계속 쌓이고 인덱스 하나 값이 싸다. 큰 둘은 빼면 그대로 예전 숫자다.

## 인입이 느려지지는 않나

인입 3표에 인덱스가 하나씩 는다. 넣는 쪽은 게이트 몇 대가 초당 몇 건이라 무시할 수준이고,
축이 `received_at` 오름차순이라 새 행이 늘 btree 오른쪽 끝에 붙어 쪼개기가 거의 없다.

## 되돌리기

전부 인덱스 CREATE뿐이라 자료를 한 줄도 안 바꾼다. downgrade는 `DROP INDEX` 셋이고
손실이 없다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'h1feed000010'
down_revision: Union[str, None] = 'g1nd3x000009'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (인덱스 이름, 표, 칸들). 이름·칸 순서가 app/models.py의 `__table_args__`와 한 글자도
# 같아야 한다 — 어긋나면 tests/test_schema_drift.py가 빨개진다.
_INDEXES = (
    ('ix_tagging_event_received_at_id', 'tagging_event', ['received_at', 'id']),
    ('ix_gate_pass_event_received_at_id', 'gate_pass_event', ['received_at', 'id']),
    ('ix_shuttle_arrival_received_at_id', 'shuttle_arrival', ['received_at', 'id']),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
