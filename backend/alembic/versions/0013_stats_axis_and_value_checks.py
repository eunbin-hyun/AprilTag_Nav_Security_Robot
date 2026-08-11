"""오늘 통계 시각 축 인덱스 둘 + 낱말 CHECK 셋 (전체검토 L29·L34)

Revision ID: k1ck5tat0013
Revises: j1acce5s0012
Create Date: 2026-08-05

## 1. 인덱스 둘 — `/api/stats/today`의 시각 축을 맞춘다 (L29)

`stats.today_counts`는 계열 셋을 같은 구간으로 자르는데 축 이름만 다르다 — 태깅·통과는
`observed_at`, 셔틀은 `signal_ts`다. 그런데 0009가 깐 건 `gate_pass_event.observed_at`
하나뿐이라, **같은 집계 안에서 표 하나만 Index Only Scan이고 나머지 둘은 Seq Scan**이었다
(전체검토 실측 29ms 대 56ms·45ms).

⚠ `tagging_event`에 이미 `ix_tagging_event_credit_window`(`credit_state, expires_at,
observed_at`)가 있는데 그걸로 안 된다. `observed_at`이 **셋째 칸**이라 앞칸에 술어가
없으면 btree를 못 탄다. 그래서 단일 축을 따로 깐다.

⚠ `shuttle_arrival`의 `gate_no`·`notified_robot_id`는 여기서도 안 깐다. 0009가 미룬 이유
(짝짓기 질의가 `id NOT IN (…)` 반조인이라 어차피 표를 한 바퀴 돈다)가 그대로 유효하다 —
이번에 붙는 건 통계가 구간을 자르는 `signal_ts` 하나다.

## 2. CHECK 셋 — 낱말을 DB에도 못박는다 (L34)

`pg_constraint` contype='c'가 스키마 전체에서 0건이었다. 값 검증이 전부 Pydantic 층에만
있어서, 그 층을 안 거치는 길(마이그레이션 backfill·손 UPDATE·psql)로 들어온 값은 아무도
안 막았다.

**셋만 건다.** 검토가 지목한 여섯 칸 중 둘은 일부러 뺐다.
  - `gate_pass_event.verdict` — `tests/test_stats_today.py:123`이 `verdict="quarantined"`를
    **일부러** 심어서 모르는 낱말이 어느 칸으로 접히나를 잰다.
  - `app_user.role` — `tests/test_ws_session_revalidate.py:234`가 `role="none"`으로 강등을
    흉내 내서 WS가 끊기나를 잰다.
둘 다 시험이 닫힌 집합 밖 값을 계약처럼 쓰고 있어서, CHECK를 걸면 시험이 빨개진다. 시험을
같이 고칠지는 팀 결정이라 여기서 임의로 안 정한다.

### ⚠ `NOT VALID`로 거는 이유

# 간소화: `NOT VALID`라 **이미 쌓인 행은 안 훑는다.** 새 INSERT·UPDATE만 막힌다.
검증 스캔을 켜면 실서버에 낱말 밖 행이 한 줄이라도 있을 때 `alembic upgrade head`가 그
자리에서 실패하고, 젠킨스 배포가 통째로 선다. 지금은 배포를 안 세우는 쪽을 고른다.
쌓인 행까지 보증하려면 나중에 한 줄 더 뜬다 —
`ALTER TABLE tagging_event VALIDATE CONSTRAINT ck_tagging_event_credit_state;`
(표를 잠그지 않는 가벼운 스캔이다. 걸리는 행이 나오면 그 행부터 정리해야 한다.)

⚠ 시험은 `Base.metadata.create_all`로 빈 DB를 만들어서 CHECK가 **처음부터 유효**하다.
그래서 `app/models.py` 쪽에는 `NOT VALID`가 없다 — 두 자리가 어긋난 게 아니라, 빈 표에
거는 것과 쌓인 표에 거는 것의 차이다.

⚠ 낱말 정본은 코드에 있다(`app/alert_lifecycle.py`의 `AlertStatus`·`AlertResolution`,
`app/credit/state_machine.py`의 `CreditState`). 늘리거나 바꾸면 **여기와 `app/models.py`
셋을 같이** 고쳐야 한다. autogenerate는 CHECK 제약을 비교 대상으로 안 봐서
`tests/test_schema_drift.py`가 이 어긋남은 못 잡는다.

## downgrade

인덱스 둘·제약 셋을 지운다. 자료를 한 줄도 안 바꾸므로 왕복에 손실이 없다.
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'k1ck5tat0013'
down_revision: Union[str, None] = 'j1acce5s0012'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (인덱스 이름, 표, 칸들). 이름·칸 순서가 app/models.py의 `index=True`가 만드는 이름과
# 한 글자도 같아야 한다 — 어긋나면 tests/test_schema_drift.py가 빨개진다.
_INDEXES = (
    ('ix_tagging_event_observed_at', 'tagging_event', ['observed_at']),
    ('ix_shuttle_arrival_signal_ts', 'shuttle_arrival', ['signal_ts']),
)

# (제약 이름, 표, 조건). 조건 문자열은 app/models.py의 `CheckConstraint`와 같은 글자다.
_CHECKS = (
    (
        'ck_tagging_event_credit_state',
        'tagging_event',
        "credit_state IN ('issued', 'consumed', 'expired', 'recovered')",
    ),
    (
        'ck_alert_status',
        'alert',
        "status IN ('OPEN', 'ACKED', 'IDENTIFIED', 'RESOLVED')",
    ),
    (
        'ck_alert_resolution',
        'alert',
        "resolution IN ('confirmed', 'false_positive', 'duplicate', 'other')",
    ),
)


def upgrade() -> None:
    for name, table, columns in _INDEXES:
        op.create_index(name, table, columns)
    for name, table, condition in _CHECKS:
        op.create_check_constraint(name, table, condition, postgresql_not_valid=True)


def downgrade() -> None:
    for name, table, _condition in reversed(_CHECKS):
        op.drop_constraint(name, table, type_='check')
    for name, table, _columns in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
