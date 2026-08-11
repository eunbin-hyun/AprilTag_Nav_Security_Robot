"""시험 자료 정리 도구 — 시연 전에 쌓인 가짜 행을 백업 뜨고 지운다.

## 왜 필요한가

2026-07-30 실서버에 통과 494건·태깅 12건·경고 18건이 있는데, 통과 494건 중 297건이 미태깅
판정이고 그 대부분이 ToF 고착(지라 S15P11C207-227)으로 생긴 가짜다. 시연 첫 화면에
미처리 사고 18건이 그대로 보이고, 통계·집계도 센서 오류량을 보안 위험으로 센다.

## 안전선 (파괴 도구라 촘촘히 걸었다)

1. **미리보기가 기본값이다.** `--apply`를 안 주면 무엇을 지울지 세어서 보여주고 끝난다.
2. **범위 옵션이 없으면 아무것도 안 지운다**(exit 2). "전부 지우기"를 실수로 부르는 걸
   막는다. 진짜 전부 지우려면 `--all`을 명시한다.
3. **지우기 전에 자동 백업**한다. 지울 행 전부를 JSONL로 떠서 세어 보고, 파일 행 수가
   지울 행 수와 다르면 삭제를 아예 안 한다. 백업 파일은 **덮지 않고**(같은 이름이면 멈춘다)
   권한 0600으로 만든다 — 안에 카드 UID와 사후 신원 문자열이 원문으로 들어간다.
   기본 자리는 배포 자리 밖(`~/c207-purge-backups`)이다. 배포 폴더 안에 두면 다음 배포의
   `rsync --delete`가 지운다.
4. **시험 DB가 아니면 확인 문구를 손으로 타이핑**해야 한다. DB 이름이 `_test`로 안 끝나면
   운영으로 보고 `DELETE <db이름>`을 그대로 입력하라고 요구한다. 파이프·리다이렉트로는
   못 지난다(터미널이 아니면 거절).
5. **지운 뒤 남은 행 수를 표로 보고**한다.
6. **로그인·감사 계열은 아예 대상이 아니다**(아래).
7. **지우는 사이에 새 경고가 끼어들면 통째로 되돌린다.** 지우는 건 백업에 담긴 id뿐이라,
   조회 뒤에 들어온 경고가 지워질 이벤트를 가리키면 그 경고만 아무것도 안 가리키는 채로
   남는다. 삭제 트랜잭션 안에서 그걸 다시 훑어 하나라도 있으면 롤백하고 종료 코드 5로 선다
   (같이 지우면 백업에 없어서 되돌릴 길이 사라진다). 인입을 멈추고 다시 돌리면 된다.

## ⚠ `--all`이 지우는 것과 안 지우는 것

`--all`은 "이 도구가 아는 표 전부"지 "DB 전부"가 아니다.

- 지운다 — `alert` · `gate_pass_event` · `tagging_event` · `shuttle_arrival` ·
  `daily_default_destination` · `robot_status_log` · `device_command_log` ·
  `chat_message` · `chat_conversation`
- **안 지운다(시연 자산)** — `app_user` · `auth_session` · `staff_audit` · `staff` ·
  `staff_access_log`
- **안 지운다(범위 밖)** — `robot` · `sticker_inspection` · `temporary_pass` · `kiosk`.
  지우면 안 되는 게 아니라 **지울 게 없는** 표다(근거는 `OUT_OF_SCOPE` 주석). 뒤 셋은
  INSERT하는 코드가 한 줄도 없고, `robot`은 지워도 로봇이 다시 붙는 순간 같은 이름으로
  되살아난다.

⚠ **이 세 줄은 아래 튜플과 손으로 맞춘 사본이라 조용히 낡는다.** 닫힘 시험은 튜플만 보고
이 글은 안 본다 — 2026-08-07에 실제로 어긋난 채 발견됐다(`device_command_log`가 여기에만
빠져 있었고, "INSERT하는 코드가 한 줄도 없다"가 `robot_status_log`엔 이미 거짓이었다).
**튜플을 고치면 이 줄도 같이 고쳐라.**

세 목록은 `Base.metadata`와 대조해 **닫힌 집합**이다. 모델에 표가 하나 늘고 어디에도 안
적히면 `tests/test_purge_test_data.py`의 분류 닫힘 시험이 터진다 — 손으로 적은 표 목록이
조용히 새는 걸 막는 자리다(`tests/conftest.py:_assert_truncate_list_is_closed`와 같은 잣대).

계정·세션은 시험 자료가 아니라 시연 자산이다. 날리면 시연 계정으로 로그인할 길이 없어진다.
감사 기록(`staff_audit`)은 append-only 계약이라(로그인 설계 §4) 지우는 창구를 안 만든다.
계정을 다시 심을 거면 이 도구가 아니라 `tools/seed_users.py`가 그 자리다.

## 되돌리기 (undo)

백업 파일 하나로 되돌린다. 삭제할 때 화면에 찍히는 명령을 그대로 붙여 넣으면 된다.

    python -m tools.purge_test_data --restore ~/c207-purge-backups/purge_20260730-101500-4213.jsonl

되돌리기 규칙.
- `INSERT ... ON CONFLICT (event_id) DO NOTHING`이라 이미 있는 행은 건너뛴다(여러 번 돌려도
  같은 결과다). 경고(alert)는 event_id가 없어서 PK(id)로 같은 판정을 한다.
- 원래 id를 그대로 되돌린다. 그래서 경고의 `source_id` → 이벤트 id 역추적이 살아난다.
- 되돌린 뒤 시퀀스를 최대 id로 다시 맞춘다. 안 맞추면 다음 INSERT가 이미 있는 id를 잡아
  중복 키로 터진다.
- ⚠ 지운 뒤에 새 행이 그 id로 들어갔으면 그 행은 안 덮고 건너뛴다. 되돌릴 거면 지운 직후에
  하는 게 안전하다.
- ⚠ **건너뛴 행이 있으면 되돌린 경고가 엉뚱한 데를 가리킬 수 있다.** 이벤트 쪽이 유일 제약
  (PK나 `event_id`)에 걸려 건너뛰었는데 그 경고는 그대로 들어가면, `source_id`가 지금 그
  자리를 차지한 다른 행을 가리키거나 아무것도 안 가리킨다. 끝에 찍히는 "건너뛴 행(이미 있음)"
  표가 0이 아니면 그 표들부터 눈으로 확인해라.

## 앞으로 시험 자료를 갈라 쌓는 방법 (제안 — 팀 결정 대기)

지금은 시험 행과 진짜 행이 같은 테이블에 같은 모양으로 섞여서, 지울 때 시각으로만 가른다.
device_id 접두로 갈라두면 범위를 정확히 집을 수 있다.

- 실기기는 `gate1`·`gate9`처럼 지금 쓰는 이름 그대로 둔다.
- 손시험·시뮬·부하시험은 `test-` 접두를 쓴다 (`test-gate1`, `test-sim-a`).
- 화면 셔틀 가상 호출은 이미 `webcall-` 접두가 붙는다(app/shuttle_call.py가 만든다).
- 그러면 정리가 `--device-prefix test- --device-prefix webcall-` 한 줄로 끝나고, 통계도
  나중에 같은 축으로 시험 자료를 뺄 수 있다.

⚠ `shuttle_arrival` 테이블엔 device_id 칸이 없다. 셔틀은 event_id 접두로 가른다(도구가
그렇게 한다). 통계에서 시험 자료를 빼는 건 이 도구 범위 밖이고, `device_id LIKE 'test-%'`를
집계 쿼리에서 제외하는 건 통계 담당 조 몫이다.

## 사용 예

    # 무엇을 지울지만 본다(기본값)
    python -m tools.purge_test_data --before 2026-07-30

    # 실제로 지운다 (백업 자동)
    python -m tools.purge_test_data --before 2026-07-30 --apply

    # 발표 직전 — 전부 비우고 번호까지 1번으로 되돌린다
    python -m tools.purge_test_data --all --apply --reset-sequences

    # 시험 기기 것만
    python -m tools.purge_test_data --device-prefix test- --device-prefix webcall- --apply

    # 되돌리기
    python -m tools.purge_test_data --restore ~/c207-purge-backups/purge_20260730-101500-4213.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import pathlib
import sys
from functools import lru_cache

import asyncpg

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
# ⚠ 기본 백업 자리는 **배포 자리 밖**이다(홈). 예전 기본값 `backend/tools/_purge_backups`는
# 배포 대상 폴더($APP_DIR) 안이라, 다음 배포의 `rsync -a --delete`가 통째로 지웠다 —
# 되돌릴 길이 백업 파일 하나뿐인 도구에서 그 파일이 배포 한 번에 사라지는 자리였다.
# 배포 영수증을 `$HOME/c207-deploy-state`에 적는 것과 같은 이유다(deploy/jenkins-deploy.sh).
# 컨테이너 안에서 돌리면 홈도 컨테이너와 같이 사라지니 `--backup-dir`로 호스트 자리를 주거나
# 끝난 뒤 `docker cp`로 빼라.
DEFAULT_BACKUP_DIR = pathlib.Path.home() / "c207-purge-backups"

# 지우는 순서. 경고가 이벤트를 (source_type, source_id)로 가리키니 경고를 먼저 지운다 —
# 이벤트만 지우면 아무것도 안 가리키는 경고가 남아 화면이 빈 사건을 그린다.
#
# `daily_default_destination`은 0006이 만든 표다. 그날 기본 목적지 한 줄이라 시험 자료
# 성격이고, 남아 있으면 시연 날 아침 조회가 시험 때 정한 값을 그대로 물려받는다.
#
# ⭐ **주행 기록·명령 기록은 맨 뒤다**(2026-08-07에 `OUT_OF_SCOPE`에서 옮겼다). 둘 다 아무도
# 가리키지 않아서(경고의 `source_type`은 빔 통과·태깅·카메라만 가리킨다) 순서에 걸리는 게
# 없다. `robot_status_log.robot_id`가 `robot`을 FK로 물지만 `robot`은 이 도구가 안 지운다.
PURGE_ORDER = (
    "alert",
    "gate_pass_event",
    "tagging_event",
    "shuttle_arrival",
    "daily_default_destination",
    "robot_status_log",
    "device_command_log",
    # ⭐ 관제 챗봇 대화(0019). 리허설로 쌓인 질문·답변이 발표 화면 목록에 그대로 선다.
    # ⚠ 메시지가 대화를 FK 로 무니 **메시지가 앞**이다.
    "chat_message",
    "chat_conversation",
    # ⭐ 일일 요약 캐시(0020). 리허설로 만든 요약이 남아 있으면 발표 당일 개요 탭이 **어제
    # 이야기**를 한다 — 오늘치 수명이 지나기 전까지 그 문장이 그대로 나간다.
    "assistant_daily_brief",
)
# 되돌릴 때는 반대 순서다(이벤트를 먼저 넣어야 경고의 역추적이 곧바로 맞는다).
RESTORE_ORDER = tuple(reversed(PURGE_ORDER))

# ⛔ **이 도구가 절대 안 지우는 표.** `--all`을 줘도 그대로 남는다.
#
# 0008이 만든 로그인 계열(`app_user`·`auth_session`)과 감사 기록(`staff_audit`)·명부(`staff`)다.
# 시험 자료가 아니라 **시연 자산**이라, 한 번 날리면 시연 계정으로 로그인할 길이 없어지고
# 감사 기록은 append-only 계약(로그인 설계 §4)이라 지우는 창구 자체를 안 만든다.
# 계정을 정말 다시 심을 거면 이 도구가 아니라 `tools/seed_users.py`가 그 자리다.
NEVER_PURGED = (
    "app_user",
    "auth_session",
    "staff_audit",
    "staff",
    # 명부 조회 이력(0012). `staff_audit`과 같은 잣대다 — append-only 계약이라 지우는 창구를
    # 안 만든다. 조회 기록은 "누가 명부를 봤나"라서 시험 자료가 아니라 감사 자산이다.
    #
    # ⚠ 시험·리허설로 쌓인 조회 기록이 발표 화면의 "조회 이력" 탭에 그대로 보인다. 그게
    # 거슬리면 DB 에 직접 붙어 지워야 한다 — 이 도구로 여는 순간 "감사 기록을 지우는 창구가
    # 있다"가 되어 계약이 무너진다. 감사 기록은 지울 수 있으면 감사가 아니다.
    "staff_access_log",
)

# 범위 밖 — "지우면 안 되는 것"이 아니라 **지울 게 없어서 안 고르는 표**다.
#
# ⚠ 이 목록이 있는 이유는 `PURGE_ORDER + NEVER_PURGED + OUT_OF_SCOPE`를 `Base.metadata`와
# 대조해 **닫힌 집합**으로 만들기 위해서다(`tests/test_purge_test_data.py`의 분류 닫힘 시험).
# 손으로 적은 표 목록은 조용히 샌다 — 같은 저장소에서 이미 한 번 났다. `tests/conftest.py`의
# `_TRUNCATE_TABLES`에서 바로 이 다섯 중 넷이 빠져 케이스 사이로 행이 샜고, 그래서 거기에
# `_assert_truncate_list_is_closed`가 붙었다. 여기도 같은 잣대다 — 모델에 표를 하나 붙이고
# 세 목록 어디에도 안 적으면 시험이 곧장 터져서 "이건 지울 것인가"를 그 자리에서 정하게 된다.
#
# 표마다 근거(2026-08-04 실측 — `app/`·`tools/`·`hardware/` 전체에서 쓰는 자리를 세었다).
#  - robot              지우지 않는다. 행을 만드는 자리가 둘인데(`robot_channel._upsert_robot`·
#                       `_ensure_robot_pk`) 둘 다 **이름으로 찾고 없으면 만든다**. 실기기가
#                       붙는 순간 같은 행에 값이 덮이고, 값이 하나도 없는 행은 조회 쪽
#                       (`routers/query.py:_has_nothing_to_show`)이 화면에서 이미 가린다.
#                       지워 봐야 다음 접속에 그대로 되살아나는 표라 지울 이득이 없다.
#                       ⚠ 남는 틈 하나는 문서에 적었다(시험자료_비우기 문서 "로봇 카드" 절) —
#                       `--robot-id`를 기본값(`jetson01`) 말고 딴 이름으로 돌린 시험이 있으면
#                       그 이름 행은 아무도 안 덮어서 발표 화면에 낡은 카드로 선다.
#  - sticker_inspection 쓰는 자리가 한 곳도 없다(뼈대 3종 — models.py "P1 후순위, 마이그레이션
#                       범위만 확보").
#  - temporary_pass     같다.
#  - kiosk              같다.
#
# ✅ **`robot_status_log`·`device_command_log`는 2026-08-07에 `PURGE_ORDER`로 옮겼다.**
# 예전 주석이 "나중에 실제로 행을 쌓기 시작하면 옮길 자리는 PURGE_ORDER"라고 예고해 뒀고
# 2026-08-06에 그날이 왔다(0017·0018). ⛔ **여기로 되돌리지 마라** — 안 지우면 시험·리허설로
# 쌓인 주행 기록과 명령 기록이 발표 자료에 그대로 남는다.
OUT_OF_SCOPE = (
    "robot",
    "sticker_inspection",
    "temporary_pass",
    "kiosk",
)

# 경고는 `alert` 하나뿐이고 나머지는 자기 범위로 직접 고른다(따라 지우기의 기준 표들).
NON_ALERT_TABLES = tuple(t for t in PURGE_ORDER if t != "alert")

# 테이블마다 시각 축이 다르다. 서버 수신 시각이 아니라 "기기가 본 시각"을 쓴다 —
# received_at으로 자르면 백로그로 늦게 올라온 어제 이벤트가 오늘로 붙는다.
TIME_COLUMN = {
    "tagging_event": "observed_at",
    "gate_pass_event": "observed_at",
    "shuttle_arrival": "signal_ts",
    "alert": "created_at",
    # 그날 값을 정한 시각. `service_date`(KST 날짜)가 아니라 이쪽을 쓴다 — 다른 표와 같은
    # 시각 축(timestamptz)이라야 `--before`에 준 한 값이 표마다 같은 순간을 가리킨다.
    "daily_default_destination": "decided_at",
    # 로봇이 그 보고를 올린 시각(0017).
    "robot_status_log": "ts",
    # ⛔ **`issued_at` 단독을 쓰면 게이트 기록이 통째로 안 지워진다.** `device_command_log`엔
    # 두 채널이 사는데(`channel`), 게이트 스피커 행은 **발행 기록 없이 ack만 난다** —
    # 소리 큐가 인메모리 동기 함수라 발행 자리에서 DB를 못 만져서다(`command_log.py:121`이
    # `channel`·`command_id`·`target`만 넣어 행을 만들고 `issued_at`은 안 채운다).
    # SQL에서 `NULL >= $1`은 false라 `--before`·`--after`를 주면 그 행들이 통째로 빠진다.
    # 그래서 **발행 시각이 없으면 ack 시각으로 떨어뜨린다.**
    # ⚠ 여기 값은 `build_where`가 f-string으로 SQL에 그대로 박는다 — 칸 이름만이 아니라
    # 식도 된다. 쓰는 자리는 `build_where` 두 줄뿐이라 식이어도 안전하다.
    "device_command_log": "COALESCE(issued_at, ack_at)",
    # 대화가 마지막으로 움직인 시각(0019). `created_at` 이 아니라 이쪽을 쓴다 — 며칠 전에 연
    # 대화에 오늘 말을 붙이면 그건 오늘 자료다.
    "chat_conversation": "updated_at",
    "chat_message": "created_at",
    # 만든 시각이 아니라 마지막으로 다시 만든 시각이다(0020). 오늘치는 수명이 지날 때마다
    # 갱신되므로 그쪽이 "이 자료가 언제 것인가"에 맞다.
    "assistant_daily_brief": "updated_at",
}

# device_id 칸이 있는 테이블. 셔틀·경고엔 없다(셔틀은 event_id 접두로 가른다).
HAS_DEVICE_ID = ("tagging_event", "gate_pass_event")
# event_id 칸이 있는 테이블(멱등 키). 경고엔 없어서 되돌릴 때 PK로 충돌을 판정한다.
HAS_EVENT_ID = ("tagging_event", "gate_pass_event", "shuttle_arrival")
# gate_no 칸이 있는 테이블. 없는 표에 `--gate`를 걸면 SQL이 그 자리에서 터진다.
HAS_GATE_NO = ("tagging_event", "gate_pass_event", "shuttle_arrival")

CONFIRM_TEMPLATE = "DELETE {db}"


# ── 안전 판정 (순수 함수 — 시험이 직접 부른다) ──────────────────────────────

def is_test_database(db_name: str) -> bool:
    """이름만 보고 시험 DB인지 본다. `_test`로 끝나거나 `_test_`가 들어가면 시험이다.

    이름 규칙 하나로 판정하는 게 얄팍해 보이지만, 접속 정보로는 운영·시험을 못 가른다
    (둘 다 같은 호스트에 있을 수 있다). 그리고 판정이 틀리는 방향이 안전한 쪽이다 —
    시험 DB를 운영으로 잘못 보면 확인 문구를 한 번 더 타이핑할 뿐이다.
    """
    return db_name.endswith("_test") or "_test_" in db_name


def has_scope(args: argparse.Namespace) -> bool:
    """범위 옵션이 하나라도 붙었나. 없으면 아무것도 안 지운다."""
    return bool(
        args.before
        or args.after
        or args.device_prefix
        or args.gate
        or args.all
    )


# ── DSN 다루기 ─────────────────────────────────────────────────────────────

def to_asyncpg_dsn(url: str) -> str:
    """SQLAlchemy URL(`postgresql+asyncpg://`)을 asyncpg가 아는 꼴로 바꾼다."""
    return url.replace("postgresql+asyncpg://", "postgresql://")


def db_name_of(dsn: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(dsn).path.lstrip("/")


def resolve_dsn(explicit: str | None) -> str:
    """--database-url이 없으면 앱 설정(.env·환경변수)에서 가져온다."""
    if explicit:
        return to_asyncpg_dsn(explicit)
    sys.path.insert(0, str(BACKEND_DIR))
    from app.config import get_settings  # noqa: PLC0415 - CLI에서만 늦게 부른다

    return to_asyncpg_dsn(get_settings().database_url)


# ── 범위 → SQL ─────────────────────────────────────────────────────────────

def build_where(table: str, args: argparse.Namespace) -> tuple[str, list]:
    """테이블별 WHERE 절과 파라미터를 만든다. 조건이 없으면 ("", [])다.

    조건은 AND로 묶는다 — 여러 축을 주면 교집합이라 범위가 좁아진다. OR로 묶으면
    "기간을 좁히려고 준 옵션이 오히려 대상을 늘리는" 방향이라 파괴 도구에선 위험하다.
    """
    clauses: list[str] = []
    params: list = []

    if args.after:
        params.append(args.after)
        clauses.append(f"{TIME_COLUMN[table]} >= ${len(params)}")
    if args.before:
        params.append(args.before)
        clauses.append(f"{TIME_COLUMN[table]} < ${len(params)}")

    if args.device_prefix:
        # 테이블마다 "기기"를 가리키는 칸이 다르다. device_id가 있으면 그걸 보고,
        # 없으면 event_id 접두를 본다(event_id 첫 토막이 device_id니까 같은 축이다).
        column = "device_id" if table in HAS_DEVICE_ID else (
            "event_id" if table in HAS_EVENT_ID else None
        )
        if column is None:
            # 경고엔 기기 축이 아예 없다. 기기로 좁히는 실행에선 경고를 직접 안 고르고,
            # 지워진 이벤트를 가리키는 경고만 따라 지운다(collect_orphan_alert_ids).
            clauses.append("false")
        else:
            ors = []
            for prefix in args.device_prefix:
                params.append(f"{prefix}%")
                ors.append(f"{column} LIKE ${len(params)}")
            clauses.append("(" + " OR ".join(ors) + ")")

    if args.gate:
        if table not in HAS_GATE_NO:
            # 경고·그날 기본 목적지엔 gate_no가 없다. 기기 축과 같은 이유로 안 고른다
            # (경고는 따라 지우기가 받는다). 칸 없는 표에 조건을 걸면 SQL이 터진다.
            clauses.append("false")
        else:
            params.append(args.gate)
            clauses.append(f"gate_no = ANY(${len(params)}::int[])")

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


async def count_rows(conn, table: str, where: str, params: list) -> int:
    return await conn.fetchval(f"SELECT count(*) FROM {table}{where}", *params)


async def fetch_rows(conn, table: str, where: str, params: list) -> list[dict]:
    rows = await conn.fetch(f"SELECT * FROM {table}{where} ORDER BY id", *params)
    return [dict(r) for r in rows]


async def reset_sequences(conn, tables=PURGE_ORDER) -> dict[str, int]:
    """지운 표의 id 시퀀스를 **남은 행에 맞춰** 되돌리고 `{표: 다음 id}`를 돌려준다.

    지우기만 하면 시퀀스는 안 내려간다(실측 — 8101을 지워도 다음 행이 8102다). 그래서
    비운 뒤 발표 화면 첫 통과가 "8102번"으로 뜬다.

    ⚠ **무조건 1로 되돌리지 않는다.** 범위를 좁혀 지웠으면(`--before` 등) 행이 남아 있고,
    그 상태에서 1로 내리면 다음 INSERT가 살아 있는 id를 잡아 중복 키로 터진다. 그래서
    남은 최대 id에 맞춘다 — 표가 비었을 때만 1부터 다시 시작한다(되돌리기가 쓰는 셈법과 같다).

    ⚠ **삭제 트랜잭션 밖에서 불러야 한다.** Postgres 시퀀스는 트랜잭션을 안 탄다 — 안에서
    부르면 롤백돼도 `setval`만 남아서, 아무것도 안 지웠는데 번호만 내려간 상태가 된다.
    """
    out: dict[str, int] = {}
    for table in tables:
        seq = await conn.fetchval("SELECT pg_get_serial_sequence($1, 'id')", table)
        if seq is None:
            continue  # id가 시퀀스가 아닌 표는 건드릴 게 없다
        max_id = await conn.fetchval(f"SELECT max(id) FROM {table}")
        if max_id is None:
            # is_called=false라 다음 nextval이 1을 준다(1을 건너뛰지 않는다).
            await conn.execute("SELECT setval($1, 1, false)", seq)
            out[table] = 1
        else:
            await conn.execute("SELECT setval($1, $2, true)", seq, max_id)
            out[table] = max_id + 1
    return out


async def collect_orphan_alert_ids(conn, doomed: dict[str, list[dict]]) -> list[int]:
    """지워질 이벤트를 가리키는 경고 id.

    이벤트만 지우고 경고를 남기면 아무것도 안 가리키는 경고가 남는다 — 화면이 빈 사건을
    그리고, 신원 입력 창구는 발화 이벤트에서 게이트 번호를 못 되짚는다.
    """
    source_type_of = {
        "gate_pass_event": "gate_pass",
        "tagging_event": "tagging",
        "shuttle_arrival": "shuttle_arrival",
    }
    ids: set[int] = set()
    for table, source_type in source_type_of.items():
        source_ids = [r["id"] for r in doomed.get(table, [])]
        if not source_ids:
            continue
        found = await conn.fetch(
            "SELECT id FROM alert WHERE source_type = $1 AND source_id = ANY($2::bigint[])",
            source_type,
            source_ids,
        )
        ids.update(r["id"] for r in found)
    return sorted(ids)


# ── JSONL 백업 ─────────────────────────────────────────────────────────────

def _json_default(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    raise TypeError(f"JSON으로 못 바꾸는 값: {type(value)!r}")


def write_backup(path: pathlib.Path, doomed: dict[str, list[dict]]) -> int:
    """지울 행을 JSONL로 떠서 쓴 줄 수를 돌려준다. 한 줄이 {table, row} 한 벌이다.

    ⚠ **덮어쓰지 않는다**(`O_EXCL`). 이름이 초 단위+PID라 부딪힐 일이 거의 없지만, 부딪히면
    조용히 앞 백업을 덮는 대신 `FileExistsError`로 시끄럽게 선다 — 되돌릴 길이 이 파일
    하나뿐이라 조용한 손실이 제일 나쁘다.
    ⚠ 파일 권한은 0600이다. 안에 카드 UID(`tagging_event.tag_id`)와 사후 신원 문자열
    (`alert.identified_person`·**`alert.identified_student_no`**)이 원문으로 실린다.
    윈도우에서는 mode가 사실상 안 먹는다.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fp:
        for table in PURGE_ORDER:
            for row in doomed.get(table, []):
                fp.write(
                    json.dumps(
                        {"table": table, "row": row},
                        ensure_ascii=False,
                        default=_json_default,
                    )
                    + "\n"
                )
                written += 1
    return written


def read_backup(path: pathlib.Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            out.setdefault(item["table"], []).append(item["row"])
    return out


# ── 실행 ───────────────────────────────────────────────────────────────────

def confirm_interactively(db_name: str) -> bool:
    """운영으로 보이는 DB면 확인 문구를 손으로 타이핑하게 한다.

    터미널이 아니면(파이프·리다이렉트·CI) 거절한다 — `echo "DELETE c207" | python ...`으로
    지날 수 있으면 이 게이트가 있으나 마나다.
    """
    if not sys.stdin.isatty():
        print(
            "[중단] 시험 DB가 아닌데 입력이 터미널이 아닙니다."
            " 확인 문구는 손으로 입력해야 합니다.",
            file=sys.stderr,
        )
        return False
    expected = CONFIRM_TEMPLATE.format(db=db_name)
    print(f"⚠ '{db_name}'은(는) 시험 DB로 안 보입니다. 지우려면 다음을 그대로 입력하세요.")
    print(f"   {expected}")
    try:
        typed = input("> ").strip()
    except EOFError:
        typed = ""
    if typed != expected:
        print("[중단] 확인 문구가 다릅니다. 아무것도 지우지 않았습니다.", file=sys.stderr)
        return False
    return True


def print_counts(title: str, counts: dict[str, int]) -> None:
    print(f"\n{title}")
    width = max(len(t) for t in counts) if counts else 10
    for table, n in counts.items():
        print(f"  {table.ljust(width)}  {n:>7,}건")
    print(f"  {'합계'.ljust(width)}  {sum(counts.values()):>7,}건")


async def run_purge(args: argparse.Namespace, dsn: str) -> int:
    db_name = db_name_of(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        # 1) 지울 행을 실물로 모은다(세기만 하고 지우면 그 사이 들어온 행이 같이 날아간다).
        doomed: dict[str, list[dict]] = {}
        for table in NON_ALERT_TABLES:
            where, params = build_where(table, args)
            doomed[table] = await fetch_rows(conn, table, where, params)

        alert_where, alert_params = build_where("alert", args)
        direct_alerts = await fetch_rows(conn, "alert", alert_where, alert_params)
        orphan_ids = await collect_orphan_alert_ids(conn, doomed)
        seen = {r["id"] for r in direct_alerts}
        extra_ids = [i for i in orphan_ids if i not in seen]
        if extra_ids:
            extra = await conn.fetch(
                "SELECT * FROM alert WHERE id = ANY($1::bigint[]) ORDER BY id", extra_ids
            )
            direct_alerts = direct_alerts + [dict(r) for r in extra]
        doomed["alert"] = direct_alerts

        counts = {t: len(doomed.get(t, [])) for t in PURGE_ORDER}
        total = sum(counts.values())
        print(f"대상 DB — {db_name}")
        print_counts("지울 대상", counts)
        if extra_ids:
            print(f"  (경고 {len(extra_ids)}건은 지워질 이벤트를 가리켜서 따라 지운다)")

        if total == 0:
            print("\n지울 행이 없습니다.")
            return 0

        if not args.apply:
            print("\n[미리보기] 아무것도 지우지 않았습니다. 실제로 지우려면 --apply를 붙이세요.")
            return 0

        if not is_test_database(db_name) and not confirm_interactively(db_name):
            return 3

        # 2) 백업. 파일 줄 수가 지울 행 수와 다르면 삭제를 아예 안 한다.
        # 이름에 PID를 붙인다 — 초 단위 이름만 쓰면 두 사람이 같은 초에 돌릴 때 뒤가 앞을
        # 덮는다. 그래도 부딪히면 write_backup이 O_EXCL로 선다(조용히 안 덮는다).
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = pathlib.Path(args.backup_dir) if args.backup_dir else DEFAULT_BACKUP_DIR
        backup_path = backup_dir / f"purge_{stamp}-{os.getpid()}.jsonl"
        try:
            written = write_backup(backup_path, doomed)
        except FileExistsError:
            print(
                f"[중단] 백업 파일이 이미 있습니다 — {backup_path}."
                " 덮지 않았고 아무것도 지우지 않았습니다.",
                file=sys.stderr,
            )
            return 4
        if written != total:
            print(
                f"[중단] 백업이 {written}줄인데 지울 행은 {total}건입니다."
                " 아무것도 지우지 않았습니다.",
                file=sys.stderr,
            )
            return 4
        print(f"\n백업 — {backup_path} ({written:,}줄)")

        # 3) 삭제. 한 트랜잭션이라 중간에 터지면 통째로 되돌아간다.
        #
        # ⚠ 지우는 건 1)에서 모아 백업까지 뜬 id뿐이다. 그래서 "백업에 없는데 지워지는" 갈래는
        # 없지만, 조회~삭제 사이(확인 문구 타이핑·백업 쓰기)에 새 경고가 들어와 지워질 이벤트를
        # 가리키면 그 경고는 아무것도 안 가리키는 채로 남는다(화면이 빈 사건을 그린다).
        # 그 경고를 같이 지울 수는 없다 — 백업에 없어서 되돌릴 길이 사라진다. 그래서 삭제한
        # 뒤 같은 트랜잭션 안에서 다시 훑어 하나라도 있으면 **통째로 되돌리고** 다시 돌리라고
        # 알린다. 재조회는 이 트랜잭션(read committed)이라 그 사이 커밋된 새 경고도 보인다.
        deleted: dict[str, int] = {}
        stranded: list[int] = []

        class _Stranded(Exception):
            """새로 들어온 경고가 지워질 이벤트를 가리킨다 — 롤백 신호."""

        try:
            async with conn.transaction():
                for table in PURGE_ORDER:
                    ids = [r["id"] for r in doomed.get(table, [])]
                    if not ids:
                        deleted[table] = 0
                        continue
                    await conn.execute(
                        f"DELETE FROM {table} WHERE id = ANY($1::bigint[])", ids
                    )
                    deleted[table] = len(ids)
                stranded = await collect_orphan_alert_ids(conn, doomed)
                if stranded:
                    raise _Stranded
        except _Stranded:
            print(
                f"[중단] 지우는 사이에 새 경고 {len(stranded)}건이 들어와 지울 이벤트를"
                f" 가리킵니다(id {', '.join(str(i) for i in stranded[:10])}"
                f"{' …' if len(stranded) > 10 else ''}).\n"
                "        같이 지우면 백업에 없어서 되돌릴 길이 없으므로 삭제를 통째로"
                " 되돌렸습니다. 인입을 멈추고 다시 돌리세요.\n"
                f"        (백업 파일 {backup_path}은 남아 있지만 지운 게 없으니 버려도 됩니다.)",
                file=sys.stderr,
            )
            return 5
        print_counts("지웠다", deleted)

        # 3-b) 시퀀스 되돌리기(옵션). ⚠ 트랜잭션이 **커밋된 뒤**다 — 시퀀스는 롤백을 안 타서
        # 위 블록 안에 넣으면 되돌림이 일어난 판에서도 번호만 내려간다.
        if args.reset_sequences:
            nexts = await reset_sequences(conn)
            print("\n시퀀스를 되돌렸다 (다음 id)")
            width = max(len(t) for t in nexts) if nexts else 10
            for table, nid in nexts.items():
                print(f"  {table.ljust(width)}  {nid:>7,}")
            print(
                "  ⚠ 이 백업으로 되돌릴 거면 그 전에 새 행을 만들지 마라 — 새 행이 옛 id를"
                " 차지하면 되돌리기가 그 행을 건너뛴다."
            )

        # 4) 남은 행 수 보고.
        remaining = {}
        for table in PURGE_ORDER:
            remaining[table] = await conn.fetchval(f"SELECT count(*) FROM {table}")
        print_counts("남은 행", remaining)
        print(f"\n안 지운 표(시연 자산) — {' · '.join(NEVER_PURGED)}")
        print(f"안 지운 표(범위 밖·빈 뼈대) — {' · '.join(OUT_OF_SCOPE)}")

        print("\n되돌리려면 이 명령을 그대로 쓰세요.")
        print(f"  python -m tools.purge_test_data --restore {backup_path}")
        return 0
    finally:
        await conn.close()


async def run_restore(path: pathlib.Path, dsn: str) -> int:
    data = read_backup(path)
    conn = await asyncpg.connect(dsn)
    try:
        print(f"대상 DB — {db_name_of(dsn)}")
        print(f"백업 — {path}")
        inserted: dict[str, int] = {}
        skipped: dict[str, int] = {}
        async with conn.transaction():
            for table in RESTORE_ORDER:
                rows = data.get(table, [])
                inserted[table] = 0
                skipped[table] = 0
                for row in rows:
                    cols = list(row)
                    values = [_coerce(table, c, row[c]) for c in cols]
                    placeholders = ", ".join(f"${i + 1}" for i in range(len(cols)))
                    # 대상 없는 ON CONFLICT DO NOTHING을 쓴다. `(event_id)`로 좁히면 지운 뒤
                    # 새 행이 그 id를 차지한 경우 PK 충돌이 잡히지 않아 되돌리기가 통째로
                    # 터진다 — 대상을 안 적으면 유일 제약 전부(PK·event_id)를 다 덮는다.
                    done = await conn.fetchval(
                        f"INSERT INTO {table} ({', '.join(cols)})"
                        f" VALUES ({placeholders})"
                        f" ON CONFLICT DO NOTHING RETURNING id",
                        *values,
                    )
                    if done is None:
                        skipped[table] += 1
                    else:
                        inserted[table] += 1
                # 원래 id를 그대로 넣었으니 시퀀스를 최대 id로 맞춘다.
                # 안 맞추면 다음 INSERT가 이미 있는 id를 잡아 중복 키로 터진다.
                if inserted[table]:
                    await conn.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'),"
                        f" GREATEST((SELECT max(id) FROM {table}), 1))"
                    )
        print_counts("되돌렸다", inserted)
        if sum(skipped.values()):
            print_counts("건너뛴 행(이미 있음)", skipped)
        return 0
    finally:
        await conn.close()


@lru_cache(maxsize=1)
def _time_columns() -> dict[str, dict[str, type]]:
    """표별 시각 칸을 **모델 metadata에서 뽑는다** — `{표: {칸: date|datetime}}`.

    ⭐ 손으로 관리하던 칸 이름 튜플을 없앤 자리다. 그 목록은 스키마가 자랄 때마다 어긋났고,
    어긋나면 되돌리기가 시각 칸을 **문자열 그대로** 넣어 asyncpg가 그 자리에서 터진다 —
    되돌릴 길이 백업 파일 하나뿐인 도구에서 제일 나쁜 실패다. 지금은 표를 하나 더해도
    `models.py`만 맞으면 자동으로 따라온다.

    metadata를 못 읽으면 빈 dict를 준다. 그래도 되돌리기가 통째로 죽지는 않고, 시각 칸이
    문자열로 남은 행에서만 asyncpg가 알려 준다.
    """
    try:
        sys.path.insert(0, str(BACKEND_DIR))
        from sqlalchemy import Date, DateTime  # noqa: PLC0415 - CLI에서만 늦게 부른다
        from app.models import Base  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 도구라 여기서 죽이지 않는다
        print(f"[경고] 모델 metadata를 못 읽었다({exc}) — 시각 칸 변환을 건너뛴다", file=sys.stderr)
        return {}

    out: dict[str, dict[str, type]] = {}
    for name, table in Base.metadata.tables.items():
        cols = {}
        for column in table.columns:
            if isinstance(column.type, DateTime):
                cols[column.name] = dt.datetime
            elif isinstance(column.type, Date):
                cols[column.name] = dt.date
        if cols:
            out[name] = cols
    return out


def _coerce(table: str, column: str, value):
    """JSONL에서 읽은 값을 asyncpg가 받는 꼴로 되돌린다(시각 문자열 → date·datetime)."""
    if value is None or not isinstance(value, str):
        return value
    kind = _time_columns().get(table, {}).get(column)
    if kind is None:
        return value
    return kind.fromisoformat(value)


def _iso_datetime(raw: str) -> dt.datetime:
    """`2026-07-30`이나 `2026-07-30T09:00:00+09:00`을 받는다.

    오프셋을 안 적으면 UTC로 본다 — 로컬로 보면 도구를 돌리는 기계 시간대에 따라 지우는
    범위가 달라진다(같은 명령이 기계마다 다른 걸 지우면 안 된다).
    """
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="purge_test_data",
        description="시험 자료 정리 (기본은 미리보기. 지우려면 --apply)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--database-url", help="안 주면 앱 설정(.env·환경변수)을 쓴다")
    p.add_argument(
        "--apply", action="store_true",
        help="실제로 지운다. 안 주면 무엇을 지울지만 보여준다",
    )
    scope = p.add_argument_group("범위 (하나 이상 필수)")
    scope.add_argument("--before", type=_iso_datetime, help="이 시각보다 앞(미포함)")
    scope.add_argument("--after", type=_iso_datetime, help="이 시각부터(포함)")
    scope.add_argument(
        "--device-prefix", action="append", default=[],
        help="기기 접두로 좁힌다. 여러 번 붙일 수 있다 (예: --device-prefix test-)",
    )
    scope.add_argument(
        "--gate", type=int, action="append", default=[],
        help="게이트 번호로 좁힌다. 여러 번 붙일 수 있다",
    )
    scope.add_argument(
        "--all", action="store_true",
        help=(
            "범위 없이 전부. 실수로 부르면 안 되니 명시해야 한다."
            " ⚠ 계정·세션·감사·명부(" + " · ".join(NEVER_PURGED) + ")는 이 옵션으로도 안 지운다"
        ),
    )
    p.add_argument(
        "--reset-sequences", action="store_true",
        help=(
            "지운 뒤 id 시퀀스를 남은 최대 id로 되돌린다(빈 표는 1부터)."
            " 발표 화면에 '8102번 통과' 같은 큰 번호가 안 뜨게 한다."
            " ⚠ 되돌리기(--restore)를 쓸 생각이면 붙이지 마라"
        ),
    )
    p.add_argument("--backup-dir", help=f"기본값 {DEFAULT_BACKUP_DIR}")
    p.add_argument(
        "--restore", metavar="JSONL",
        help="백업 파일로 되돌린다(다른 옵션은 무시한다)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dsn = resolve_dsn(args.database_url)

    if args.restore:
        path = pathlib.Path(args.restore)
        if not path.exists():
            print(f"[중단] 백업 파일이 없습니다 — {path}", file=sys.stderr)
            return 2
        return asyncio.run(run_restore(path, dsn))

    if not has_scope(args):
        print(
            "[중단] 범위 옵션이 없습니다. --before/--after/--device-prefix/--gate 중"
            " 하나 이상을 주거나, 정말 전부 지울 거면 --all을 명시하세요.",
            file=sys.stderr,
        )
        return 2

    return asyncio.run(run_purge(args, dsn))


if __name__ == "__main__":
    raise SystemExit(main())
