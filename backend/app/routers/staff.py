"""N14 명부 창구 (로그인 설계 확정본 2026-08-01 §3 · 지라 -347).

    GET    /api/staff/by-tag/{tag_id}   요원   이벤트 문맥 신원 한 건
    GET    /api/staff                   팀장   목록
    GET    /api/staff/{id}              팀장   단건
    GET    /api/staff/{id}/audit        팀장   감사 기록 읽기
    POST   /api/staff                   팀장   등록
    PATCH  /api/staff/{id}              팀장   수정
    POST   /api/staff/{id}/deactivate   팀장   일시정지 (깃발만 내린다)
    POST   /api/staff/{id}/reactivate   팀장   복구
    DELETE /api/staff/{id}              팀장   영구 삭제 (사건에 엮이면 409)

## 명부에서 내리는 길이 둘이다 (2026-08-05 사용자 결정)

사용자가 관리 버튼을 **수정·일시정지·해제(영구 삭제)** 셋으로 나누라고 정했다.

- **일시정지**(`deactivate`) — 행을 안 지우고 `is_active`만 내린다. 기록이 다 살아 있고
  `reactivate`로 되돌린다. **평소에 쓰는 길이다.**
- **영구 삭제**(`DELETE`) — 행을 아주 지운다. ⛔ **이 사람에게 연결된 경고가 하나라도 있으면
  409다.** 그래서 실제로 지울 수 있는 것은 **오타로 만든 행**뿐이다.

⚠ DB는 삭제를 안 막는다(`alert.staff_id`가 `ondelete="SET NULL"`이다). **막는 것은 그 창구의
판정 한 줄뿐이라**, 그 줄을 지우면 경고가 조용히 주인을 잃는다.

## 판정은 전부 `app/auth.py` 한 자리다

역할 게이트(`require_leader`·`require_agent`)와 쓰기 방어(`assert_can_write`)를 여기서 다시
구현하지 않는다. 창구 코드에 `if role ==`을 흩으면 창구가 늘 때 한쪽만 고쳐진다(§4).
등록·수정·해제 셋이 같은 함수 하나를 부른다 — 그래서 "등록 직급 상한"이 따로 없이도
우회 승격 길이 닫힌다.

⚠ 직급을 **바꾸는** 수정은 `assert_can_write`를 두 번 부른다. 옛 급 한 번, 새 급 한 번이다.
한 번만 보면 요원 행을 팀장으로 올리는 길이 샌다(그 함수 docstring이 계약을 적어 뒀다).

## ⭐ `AUTH_REQUIRE_LOGIN=false` 구간에도 이 창구들은 **닫혀 있다**

기본 역할 게이트(`require_leader`)는 플래그가 꺼져 있으면 판정을 아예 안 한다. 제1 불변이
"기존 창구의 거동을 글자 그대로 보존한다"라서 그렇게 만든 자리다. 그런데 명부·계정은
로그인과 함께 **새로 생긴** 창구라 보존할 기존 거동이 없다 — 그 게이트를 그대로 쓰면
롤아웃 1~4단계 내내 익명이 명부를 읽고 관리자 직급 행까지 만들 수 있다(검증 F1 실측).

그래서 이 파일은 `require_*_always` 쪽을 쓴다. 플래그와 무관하게 늘 판정하므로 지금도
세션 없는 요청은 401이다. 기존 창구에는 절대 안 쓴다(`app/auth.py`의 always 주석).

못박아 둔 시험은 둘이다 — `tests/test_staff_endpoints.py`의
`test_플래그가_꺼져도_명부_창구는_익명을_막는다`와 `tests/test_account_endpoints.py`의
`test_플래그가_꺼져도_계정_창구는_익명을_막는다`(감사 조회 쪽은 같은 파일의
`test_플래그가_꺼져도_계정_감사_조회는_익명을_막는다`).

## 감사 기록은 변경과 **같은 트랜잭션**이다

`add_audit`은 커밋을 안 한다. 명부 변경과 기록 INSERT가 한 커밋에 같이 들어가서, 변경만
남고 기록이 빠지는 갈라짐이 구조에서 사라진다(§4). 수정·삭제 창구는 만들지 않는다 —
append-only는 창구 부재로 지킨다.
"""
from __future__ import annotations

import logging
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select, union_all
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.db import get_db
from app.models import (
    Alert,
    AppUser,
    GatePassEvent,
    Staff,
    StaffAccessLog,
    StaffAudit,
    TaggingEvent,
)
from app.routers.query import EventKind
from app.schemas import (
    StaffAccessLogOut,
    StaffAuditOut,
    StaffCreateIn,
    StaffOut,
    StaffPatchIn,
    StaffTagOut,
)

logger = logging.getLogger("c207.staff")

router = APIRouter(prefix="/api/staff", tags=["staff"])

DEFAULT_STAFF_LIMIT = 200
MAX_STAFF_LIMIT = 500
DEFAULT_AUDIT_LIMIT = 100
MAX_AUDIT_LIMIT = 500

# RFID UID 상한. DB 칸(String(64))과 같은 값이다.
MAX_TAG_LEN = 64

_NOT_FOUND = "명부 항목을 찾을 수 없습니다."
# 태그가 겹쳤을 때 나가는 문장. 미리 보는 검사(`_assert_tag_free`)와 유니크 제약이 잡은
# 자리가 **같은 문장**을 써야 한다 — 같은 사건에 응답이 둘로 갈리면 화면이 재시도할지
# 문구를 띄울지 못 정한다(계정 창구가 이미 쓰는 모양이다).
_DUP_TAG = "이미 다른 항목이 쓰고 있는 태그입니다."
# by-tag는 **모든 실패가 같은 문장**이다. "그 태그는 명부에 있는데 지금은 못 본다"를
# 알려 주면 그게 곧 열거라, 이벤트 창구의 UID 평문과 붙여 명부를 통째로 긁을 수 있다(§8 결정 6).
_TAG_NOT_FOUND = "해당 태그의 신원을 찾을 수 없습니다."

# 감사 기록 action 낱말 셋(§2.4). String(16)이라 이 밖으로 늘리면 칸 폭부터 본다.
ACTION_CREATE = "create"
ACTION_UPDATE = "update"
ACTION_DEACTIVATE = "deactivate"
# ⭐ 해제의 짝. `PATCH`에 `is_active`를 받지 않고 창구를 따로 둔 이유는 `deactivate`와 같다 —
# **이름을 고치려다 실수로 활성 상태가 뒤집히는 갈래를 아예 안 만든다.** 감사 기록에서도
# "누가 되살렸나"가 `update`에 묻히지 않고 따로 보인다.
ACTION_REACTIVATE = "reactivate"
# ⭐ 행을 아주 지운 사실. 지운 뒤에는 대상 행이 없으므로 감사 행이 **유일한 흔적**이다.
# `before`에 지운 행의 스냅샷을 통째로 담는 이유가 그것이다.
ACTION_DELETE = "delete"

# 계정 감사 행이 쓰는 명부 번호 자리.
#
# `staff_audit.staff_id`는 NOT NULL인데 계정 역할 변경에는 대응하는 명부 행이 없다. FK가
# 없는 느슨한 정수 칸이라(§2.4 — 기록 보존이 무결성보다 위인 테이블) 0을 "명부 행이 아니다"
# 표시로 쓴다. 명부 시퀀스는 1부터라 실재 행과 안 겹치고, `GET /api/staff/{id}/audit`은
# 실재하는 명부 행만 물어서 이 행이 명부 감사 목록에 섞여 나가지 않는다.
# 진짜 대상은 기록 본문(`after.target`·`after.user_id`)에 있다.
ACCOUNT_AUDIT_STAFF_ID = 0


def _staff_out(row: Staff) -> StaffOut:
    """명부 행 → 응답. 명부를 내보내는 자리는 전부 이 함수 하나를 탄다.

    갈래마다 손으로 칸을 채우면 새 칸이 붙을 때 한 자리를 빠뜨린다(`query._alert_out`과
    같은 이유). 기존 `role` 칸은 일부러 안 싣는다(StaffOut docstring).
    """
    return StaffOut(
        id=row.id,
        name=row.name,
        tag_id=row.tag_id,
        student_no=row.student_no,
        rank=row.rank,
        is_active=row.is_active,
        app_user_id=row.app_user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def audit_snapshot(row: Staff) -> dict:
    """감사 기록에 실을 명부 행 사본. **JSON에 담을 수 있는 칸만** 담는다.

    시각 칸을 넣으면 JSONB 직렬화가 datetime에서 터진다(기본 직렬화기가 `json.dumps`다).
    "언제"는 감사 행의 `created_at`이 답하니 여기 넣을 이유가 없다.
    """
    return {
        "name": row.name,
        "tag_id": row.tag_id,
        "student_no": row.student_no,
        "rank": row.rank,
        "is_active": row.is_active,
        "app_user_id": row.app_user_id,
    }


def add_audit(
    db: AsyncSession,
    *,
    staff_id: int,
    action: str,
    actor: auth.AuthActor | None,
    before: dict | None,
    after: dict,
) -> None:
    """감사 행 하나를 **같은 트랜잭션에** 얹는다. 커밋은 부르는 쪽이 한다.

    여기서 커밋하면 변경만 남고 기록이 빠지는(또는 그 반대인) 갈라짐이 생긴다(§4).

    행위자 이름을 FK만이 아니라 **문자열로도 복사한다.** 계정이 나중에 바뀌거나 지워져도
    기록이 그때 그대로 남아야 감사다(§2.4). `actor`가 None인 건 플래그 off 구간이라
    네 칸이 전부 NULL로 남는다 — 그 상태 자체가 "로그인 전에 벌어진 일"이라는 기록이다.
    """
    db.add(
        StaffAudit(
            staff_id=staff_id,
            action=action,
            actor_user_id=actor.id if actor else None,
            actor_username=actor.username if actor else None,
            actor_display_name=actor.display_name if actor else None,
            actor_role=actor.role if actor else None,
            before=before,
            after=after,
        )
    )


async def add_access_log(
    db: AsyncSession,
    *,
    action: str,
    actor: auth.AuthActor | None,
    target_staff_id: int | None = None,
    tag_id: str | None = None,
    result_count: int | None = None,
) -> None:
    """명부를 **누가 봤나**를 남긴다. `add_audit`과 달리 **여기서 커밋한다.**

    조회 창구는 자기 트랜잭션에 쓸 게 없어서, 커밋을 부르는 쪽에 맡기면 기록이 그냥
    사라진다. 변경 창구는 반대로 변경과 한 커밋에 묶여야 해서 `add_audit`이 커밋을 안 한다 —
    두 헬퍼의 커밋 규칙이 다른 이유가 이것이다.

    ⛔ **태그 UID 원문을 안 담는다.** 앞 네 글자만 남기고 뒤를 가린다. 원문을 담으면 UID
    유출을 막으려고 액세스 로그를 끈 것이 헛일이 된다(§3 · 2026-08-02 실측).

    ⚠ **실패한 조회는 안 남긴다.** 404 갈래는 정보가 한 줄도 안 나갔으니 "봤다"가 아니다.
    열거 시도까지 잡으려면 실패도 남겨야 하는데, 그러면 존재하지 않는 태그를 넣은 요청이
    그대로 표에 쌓여 열거 공격의 저장소가 된다 — 남기는 쪽이 더 나쁘다고 봤다.
    """
    prefix = None
    if tag_id:
        head = tag_id[:4]
        prefix = f"{head}…" if len(tag_id) > 4 else head
    db.add(
        StaffAccessLog(
            action=action,
            target_staff_id=target_staff_id,
            tag_prefix=prefix,
            result_count=result_count,
            actor_user_id=actor.id if actor else None,
            actor_username=actor.username if actor else None,
            actor_display_name=actor.display_name if actor else None,
            actor_role=actor.role if actor else None,
        )
    )
    await db.commit()


async def _get_staff(db: AsyncSession, staff_id: int, *, lock: bool = False) -> Staff:
    """명부 행을 가져온다. 없으면 404.

    쓰기 갈래(수정·해제)는 `lock=True`로 부른다 — `SELECT ... FOR UPDATE`라 같은 행을
    동시에 만지는 요청이 앞 요청 커밋까지 선다. 잠금 없이 "읽고 → 판정하고 → 쓰면"
    그 사이가 방어를 뚫는 경합 창이 된다(`query._lock_alert`와 같은 수법).
    """
    stmt = select(Staff).where(Staff.id == staff_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return row


async def _assert_tag_free(
    db: AsyncSession, tag_id: str | None, *, exclude_id: int | None = None
) -> None:
    """그 태그를 이미 쓰는 행이 있으면 409.

    DB에 유니크 인덱스가 있어서 없어도 결국 막히는데, 그러면 IntegrityError가 500으로
    나간다. 미리 보고 "누구 것인지는 안 알려주는" 409 문장을 준다.

    ⚠ 이 검사와 INSERT 사이는 잠기지 않는다. 같은 순간 같은 UID를 등록하는 두 요청은
    유니크 인덱스가 막는다 — 명부 등록이 사람 손 조작이라 그 창을 잠그는 값이 더 비싸다고
    봤다. 대신 **잡히는 자리에서 같은 409로 옮긴다**(`_conflict_or_raise`). 예전에는 그
    갈래가 500으로 나가서 같은 사건에 응답이 둘로 갈렸다(2026-08-02 백지 검토 3차 X79).
    """
    if not tag_id:
        return
    stmt = select(Staff.id).where(Staff.tag_id == tag_id)
    if exclude_id is not None:
        stmt = stmt.where(Staff.id != exclude_id)
    if (await db.execute(stmt.limit(1))).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUP_TAG)


async def _conflict_or_raise(db: AsyncSession, exc: IntegrityError) -> NoReturn:
    """유니크 제약에 걸린 쓰기를 409로 옮긴다. 되돌리는 것까지 여기서 한다.

    명부 행에 걸린 유니크는 `tag_id` 하나뿐이라(models.py) 문구도 선검사와 같은 값이다.
    되돌려야 뒤이은 조회가 죽은 트랜잭션을 안 만난다 — 계정 창구(`account.create_user`)가
    이미 쓰는 모양 그대로다.
    """
    await db.rollback()
    logger.info("명부 쓰기가 유니크 제약에 걸려 409로 돌려보낸다: %s", exc.orig)
    raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUP_TAG) from None


async def _assert_user_exists(db: AsyncSession, app_user_id: int | None) -> None:
    """계정 링크가 실재하는 계정을 가리키나. 아니면 422.

    FK에만 맡기면 없는 번호가 IntegrityError로 500이 된다. 이 링크는 "본인 항목" 판정
    열쇠라(확정 D) 조용히 틀린 값이 들어가면 방어가 엉뚱한 행에 걸린다.
    """
    if app_user_id is None:
        return
    if (
        await db.execute(select(AppUser.id).where(AppUser.id == app_user_id))
    ).first() is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="그런 계정이 없습니다. 계정 번호를 확인해 주세요.",
        )


async def _tag_is_on_active_alert(db: AsyncSession, tag_id: str) -> bool:
    """이 태그가 **활성 경고**에 붙어 있나(§8 결정 6).

    "활성"의 뜻은 이 저장소가 이미 쓰던 잣대 그대로 `ack = false`다
    (`query.count_active_alerts`·`/api/alerts?active=true`·스냅샷 `active_alerts`).
    관제가 확인을 누르면 그 태그로는 더 못 캔다.

    태그와 경고를 잇는 길은 역추적 규칙 하나다 — 경고의 `(source_type, source_id)`가
    이벤트 한 줄의 `(kind, id)`고(S15P11C207-192), 그 이벤트 행이 `tag_id`를 들고 있다.
    라벨을 손으로 안 적고 `EventKind`를 그대로 쓴다(예전에 셔틀만 글자가 갈라졌던 자리다).
    셔틀 도착에는 태그 칸이 없어서 가지가 둘뿐이다.
    """
    tagging = (
        select(Alert.id)
        .join(TaggingEvent, Alert.source_id == TaggingEvent.id)
        .where(
            Alert.source_type == EventKind.tagging.value,
            Alert.ack.is_(False),
            TaggingEvent.tag_id == tag_id,
        )
    )
    gate = (
        select(Alert.id)
        .join(GatePassEvent, Alert.source_id == GatePassEvent.id)
        .where(
            Alert.source_type == EventKind.gate_pass.value,
            Alert.ack.is_(False),
            GatePassEvent.tag_id == tag_id,
        )
    )
    return (await db.execute(union_all(tagging, gate).limit(1))).first() is not None


# ── 요원 창구 ──────────────────────────────────────────────────────────────
# ⚠ `/by-tag/{tag_id}`를 `/{staff_id}` 계열보다 **먼저** 선언한다. 라우팅은 선언 순서대로
# 맞춰 보므로 뒤에 두면 `/api/staff/by-tag/audit` 같은 경로가 `/{staff_id}/audit`에 먼저
# 걸려 422로 나간다.


@router.get("/by-tag/{tag_id}", response_model=StaffTagOut)
async def read_staff_by_tag(
    tag_id: str,
    actor: auth.AuthActor | None = Depends(auth.require_agent_always),
    db: AsyncSession = Depends(get_db),
) -> StaffTagOut:
    """이벤트에 찍힌 UID로 신원 한 건을 읽는다. **요원**에게 열린 유일한 명부 창구다(§3).

    ⚠ **UID가 경로에 실린다 — 접근 로그에 평문으로 쌓이는 자리다.** nginx 기본 로그 형식의
    `$request`가 요청 줄을 통째로 적기 때문이고, **쿼리스트링으로 옮겨도 같다**(쿼리도
    `$request`에 들어간다 — 2026-08-02 실측). 계약을 안 바꾸고 닫는 길이라 로그를 끄는 쪽을
    골랐다.

    ⛔ **막는 자리가 둘이고, 한쪽만 고치면 조용히 다시 샌다.**
      ① 프록시 — `deploy/nginx-c207-api.conf`의 `location ~ ^/api/staff/by-tag/`가
         `access_log off`다. 조각은 **젠킨스 배포 밖 손 배포**라, EC2에 깔기 전까지는
         그쪽 로그가 안 막혀 있다(`deploy/README.md` 손 배포 ②).
      ② 앱 — uvicorn 액세스 로그. `deploy/docker-compose.backend.yml`의 기동 줄에
         `--no-access-log`가 **있어야** 막힌다. 이 로그는 프록시와 따로 컨테이너 stdout에
         찍혀서 `docker logs`와 json-file 회전분에 그대로 남는다.
         확인은 `docker logs c207-backend | grep by-tag` — UID가 나오면 안 막힌 것이다.
    ⚠ **경로 모양을 바꾸면 ①의 정규식도 같이 고쳐야 한다.** 안 고치면 새는 걸 알려 주는
    신호가 아무 데도 없다 — 두 자리를 재는 시험도 아직 없다.

    ✅ **조회 이력을 남긴다(2026-08-05 사용자 결정).** 성공한 조회 한 건이
    `staff_access_log`에 한 줄로 남는다 — `action="by_tag"`, 명부 행 번호,
    **UID 앞 네 글자만**(`tag_prefix`). 원문을 담으면 액세스 로그를 끈 것이 헛일이 된다.
    404 갈래는 안 남긴다(정보가 안 나갔고, 남기면 그 표가 열거 시도의 저장소가 된다).
    보는 창구는 `GET /api/staff/access-log`이고 팀장 이상이다.

    목록 열람이 아니라 이벤트 문맥 조회라서 요원 급이다 — 요원 화면에는 "이벤트 문맥의
    신원만" 뜬다는 게 범위 정본 §3이다.

    ⚠ **활성 경고에 붙은 태그만** 통과한다(§8 결정 6). 이 제한이 없으면 요원이
    `/api/events`의 UID 평문을 한 건씩 순회해 명부 전체를 긁을 수 있다.

    실패는 갈래를 안 가리고 전부 같은 404다. "그 태그는 명부에 있는데 지금은 못 본다"를
    알려 주면 제한이 열거 방어 구실을 못 한다. 그래서
      ①없는 태그 ②명부에 없는 태그 ③활성 경고가 없는 태그
    셋이 밖에서 똑같이 보인다.

    해제된(`is_active=false`) 행도 돌려준다. 해제는 삭제가 아니라 "더 이상 등록 인원이
    아니다"라서, 그 사람 태그로 경고가 났을 때야말로 관제가 이름을 알아야 한다(§2.3).
    """
    tag = tag_id.strip()
    if not tag or len(tag) > MAX_TAG_LEN:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_TAG_NOT_FOUND)

    if not await _tag_is_on_active_alert(db, tag):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_TAG_NOT_FOUND)

    row = (
        await db.execute(select(Staff).where(Staff.tag_id == tag))
    ).scalars().first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_TAG_NOT_FOUND)

    # ⚠ 로그에 이름·학번·UID를 찍지 마라. 남기는 건 명부 행 번호까지다.
    logger.info("명부 %s를 태그 문맥으로 조회했다", row.id)
    await add_access_log(
        db, action="by_tag", actor=actor, target_staff_id=row.id, tag_id=tag
    )
    return StaffTagOut(tag_id=tag, name=row.name, student_no=row.student_no)


# ── 팀장 창구 — 조회 ───────────────────────────────────────────────────────


@router.get("/access-log", response_model=list[StaffAccessLogOut])
async def read_access_log(
    limit: int = Query(DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> list[StaffAccessLogOut]:
    """명부를 누가 봤나. 최근 것부터 내려온다. **읽기만이다**(append-only, §4).

    ⚠ **이 창구를 `/{staff_id}` 위에 둔 이유** — 아래 단건 창구가 `int`라 `access-log`는
    어차피 안 걸리지만, 나중에 그 칸이 문자열이 되면 조용히 이쪽이 가려진다. 순서로
    못박아 두는 편이 싸다.

    급은 팀장 이상이다. 요원이 서로의 조회를 보는 건 다른 얘기라 여기 안 연다 — 요원한테
    열린 명부 창구는 `by-tag` 하나뿐이라는 범위 정본 §3과 같은 잣대다.

    ⭐ **이 창구 자체는 조회 이력을 안 남긴다.** 남기면 이력을 볼 때마다 이력이 늘어서
    "누가 명부를 봤나"가 자기 조회에 파묻힌다. 감사 대상은 명부이지 감사 화면이 아니다.
    """
    rows = (
        await db.execute(
            select(StaffAccessLog).order_by(StaffAccessLog.id.desc()).limit(limit)
        )
    ).scalars().all()
    return [StaffAccessLogOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("", response_model=list[StaffOut])
async def list_staff(
    limit: int = Query(DEFAULT_STAFF_LIMIT, ge=1, le=MAX_STAFF_LIMIT),
    bg: bool = Query(
        False,
        description="화면이 배경으로 미리 받는 호출이면 true. 조회 이력에 안 남는다.",
    ),
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> list[StaffOut]:
    """명부 목록. 요원은 못 본다 — 신원 목록은 명부 권한자만이다(범위 정본 §3).

    조회에는 급 제한이 없다(§3). 팀장은 자기 윗급 행도 **보이지만** 못 고친다 — 안 보이면
    누가 있는지도 모른 채 등록하다 태그가 겹친다.

    ## ⭐ `bg=1`이면 조회 이력을 안 남긴다 (2026-08-05 프론트 17차)

    화면이 **로그인 직후 명부를 배경으로 한 번 받아 온다.** 그래서 팀장·관리자가 로그인할
    때마다 `action="list"` 가 한 줄씩 쌓였다 — 사용자가 "로그인할 때마다 명부 이력 조회
    뜨는 거 고쳤나"라고 짚은 자리다.

    ⚠ **요원이 명부 탭을 직접 여는 갈래에는 이 표식이 안 붙는다.** 그건 사람이 신원 목록을
    실제로 본 것이라 **이력에 남아야 한다.** 표식은 화면이 스스로 미리 받는 호출에만 붙는다.

    ⚠ **이 값을 믿는 것이 계약이다.** 화면이 거짓으로 `bg=1`을 붙이면 이력이 빈다. 그래도
    막지 않는 까닭은, 이 창구를 부를 수 있는 사람은 이미 명부 권한자이고 **이력의 목적이
    "권한 없는 열람 잡기"가 아니라 "누가 명부를 봤나 남기기"**여서다. 권한 자체는 위
    `require_leader_always`가 쥔다.
    """
    rows = (
        await db.execute(select(Staff).order_by(Staff.id).limit(limit))
    ).scalars().all()
    if not bg:
        await add_access_log(db, action="list", actor=actor, result_count=len(rows))
    return [_staff_out(r) for r in rows]


@router.get("/{staff_id}", response_model=StaffOut)
async def read_staff(
    staff_id: int,
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> StaffOut:
    """명부 단건. 목록과 같은 급이고 급 제한도 같다(윗급 행도 보인다)."""
    row = await _get_staff(db, staff_id)
    await add_access_log(db, action="detail", actor=actor, target_staff_id=row.id)
    return _staff_out(row)


@router.get("/{staff_id}/audit", response_model=list[StaffAuditOut])
async def read_staff_audit(
    staff_id: int,
    limit: int = Query(DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> list[StaffAuditOut]:
    """그 명부 행의 변경 기록. **읽기만이다** — 고치거나 지우는 창구는 만들지 않는다(§4).

    없는 명부 번호면 404다. 감사 행은 명부 행이 사라져도 남지만(FK가 없다), 이 창구는
    "그 명부 행의 이력"을 읽는 자리라 대상이 실재해야 뜻이 선다.

    최근 것부터 내려온다.
    """
    await _get_staff(db, staff_id)
    rows = (
        await db.execute(
            select(StaffAudit)
            .where(StaffAudit.staff_id == staff_id)
            .order_by(StaffAudit.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [StaffAuditOut.model_validate(r, from_attributes=True) for r in rows]


# ── 팀장 창구 — 쓰기 ───────────────────────────────────────────────────────


@router.post("", response_model=StaffOut, status_code=status.HTTP_201_CREATED)
async def create_staff(
    body: StaffCreateIn,
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> StaffOut:
    """명부 등록. **등록 직급 상한**이 여기서 걸린다(§4).

    `assert_can_write(actor, 새 직급)`이 그 상한이다 — 수정·해제만 막으면 "관리자 직급으로
    신규 등록"이라는 우회 승격 길이 남는다(범위 정본 §5-1). 대상 행이 아직 없어서
    `target_row` 없이 부른다.

    감사 기록은 같은 커밋에 들어간다. `flush`로 번호를 먼저 받아야 기록에 그 번호를 실을 수
    있고, 그 사이에 커밋을 끼우면 변경과 기록이 갈라진다.

    ⭐ 새로 거는 계정 링크가 자기 계정이면 403이다(`assert_not_self_link`). `assert_can_write`의
    본인 항목 차단은 **대상 행의 지금 값**만 보므로, 등록에는 그 검사가 안 걸린다 — 그대로
    두면 팀장이 자기 계정을 링크한 행을 새로 만들어 본인 항목 방어를 우회한다(3차 X32).
    """
    rank = body.rank.value if body.rank else None
    auth.assert_can_write(actor, rank)
    auth.assert_not_self_link(actor, body.app_user_id)
    await _assert_tag_free(db, body.tag_id)
    await _assert_user_exists(db, body.app_user_id)

    row = Staff(
        name=body.name,
        tag_id=body.tag_id,
        student_no=body.student_no,
        rank=rank,
        # server_default가 있어도 파이썬 쪽 값을 명시한다 — 안 그러면 커밋 뒤 이 객체의
        # 칸이 None이라 응답과 감사 사본이 거짓을 싣는다(expire_on_commit=False).
        is_active=True,
        app_user_id=body.app_user_id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        # 선검사와 INSERT 사이에 딴 요청이 같은 태그를 먼저 넣었다. 유니크 제약이 잡아 준
        # 자리라 500이 아니라 409다.
        await _conflict_or_raise(db, exc)
    await db.refresh(row)  # created_at 같은 server_default를 실물로 읽는다
    add_audit(
        db,
        staff_id=row.id,
        action=ACTION_CREATE,
        actor=actor,
        before=None,
        after=audit_snapshot(row),
    )
    await db.commit()

    logger.info("명부 %s를 등록했다(직급 %s)", row.id, rank)
    return _staff_out(row)


@router.patch("/{staff_id}", response_model=StaffOut)
async def update_staff(
    staff_id: int,
    body: StaffPatchIn,
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> StaffOut:
    """명부 수정. 자기보다 **아랫급 행만** 고칠 수 있다(범위 정본 §2).

    ⚠ **관리자는 그 상한을 안 탄다**(`auth.assert_can_write` 갈래 ②). 동급·본인 항목까지
    통과다 — 그 예외가 없으면 `admin` 급 행은 아무도 못 고치는 영구 잠금이 된다(§2.5).
    2026-08-05에 프론트가 "docstring은 아랫급만이라는데 관리자로 동급·본인이 200이 난다"고
    짚어 여기에 명시한다. **서버가 열린 게 아니라 문서가 좁게 적혀 있었다.**

    방어를 **두 번** 부르는 게 계약이다.
      ① 옛 급 + 대상 행 — 동급·윗급 행인가, 본인 항목인가.
      ② 새 급(직급을 바꿀 때만) — 아랫급 사람을 자기 위로 올리는 길을 막는다.
    한 번만 보면 요원 행을 팀장으로 올리는 우회 승격이 통과한다.

    보낸 칸만 바뀐다 — 이름만 고치려던 요청이 태그·학번을 같이 지우면 안 된다.

    ⭐ 새로 거는 계정 링크가 자기 계정이면 403이다(`assert_not_self_link`). 위 방어 둘은 대상
    행의 **지금** 값만 보므로, 남의 행을 자기 링크로 갈아 끼우는 길이 그 밖에 있었다(3차 X32).
    """
    row = await _get_staff(db, staff_id, lock=True)
    sent = body.model_fields_set

    # ⭐ 고치기라 `state_change`가 거짓이다 — 총괄은 자기 행을 **고칠 수는** 있다.
    auth.assert_can_write(actor, row.rank, row)
    if "rank" in sent:
        auth.assert_can_write(actor, body.rank.value if body.rank else None, row)
    if "app_user_id" in sent:
        auth.assert_not_self_link(actor, body.app_user_id)

    if "tag_id" in sent:
        await _assert_tag_free(db, body.tag_id, exclude_id=row.id)
    if "app_user_id" in sent:
        await _assert_user_exists(db, body.app_user_id)

    before = audit_snapshot(row)
    for field in ("name", "tag_id", "student_no", "app_user_id"):
        if field in sent:
            setattr(row, field, getattr(body, field))
    if "rank" in sent:
        row.rank = body.rank.value if body.rank else None

    add_audit(
        db,
        staff_id=row.id,
        action=ACTION_UPDATE,
        actor=actor,
        before=before,
        after=audit_snapshot(row),
    )
    try:
        # 여기 flush가 따로 없어서 유니크 위반은 이 커밋에서 올라온다(등록 쪽은 flush 자리다).
        await db.commit()
    except IntegrityError as exc:
        await _conflict_or_raise(db, exc)
    # updated_at은 DB 쪽 onupdate라 커밋 뒤에 다시 읽어야 실물이 나온다.
    await db.refresh(row)

    logger.info("명부 %s를 수정했다(바뀐 칸 %s)", row.id, sorted(sent))
    return _staff_out(row)


@router.post("/{staff_id}/deactivate", response_model=StaffOut)
async def deactivate_staff(
    staff_id: int,
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> StaffOut:
    """명부 해제. 행을 **안 지우고** `is_active`를 내린다(§2.3).

    지우면 `alert.staff_id`가 끊겨 예전 사건의 신원 연결이 같이 사라진다. DELETE 창구를
    안 만든 이유가 그것이다.

    이미 해제된 행에 다시 보내도 200이다(멱등). 그때는 감사 행을 안 남긴다 — 아무것도
    안 바뀐 조작까지 적으면 기록이 눌러 본 횟수로 채워진다. 권한 판정은 그 경우에도 먼저
    돈다(권한 없는 사람은 이미 해제된 행에서도 403이다).
    """
    row = await _get_staff(db, staff_id, lock=True)
    # ⭐ 상태를 바꾸는 창구다 — 총괄도 자기 계정은 못 내린다(2026-08-05 owner 도입).
    auth.assert_can_write(actor, row.rank, row, state_change=True)

    if not row.is_active:
        return _staff_out(row)

    before = audit_snapshot(row)
    row.is_active = False
    add_audit(
        db,
        staff_id=row.id,
        action=ACTION_DEACTIVATE,
        actor=actor,
        before=before,
        after=audit_snapshot(row),
    )
    await db.commit()
    await db.refresh(row)

    logger.info("명부 %s를 해제했다", row.id)
    return _staff_out(row)


@router.post("/{staff_id}/reactivate", response_model=StaffOut)
async def reactivate_staff(
    staff_id: int,
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> StaffOut:
    """해제한 명부 행을 다시 살린다. `deactivate`의 짝이다.

    **왜 `PATCH`에 `is_active`를 안 받고 창구를 따로 뒀나** — 해제와 같은 이유다. 수정 창구가
    활성 상태까지 받으면 **이름 한 칸만 고치려던 요청이 사람을 되살리거나 지우는 갈래**가 열린다.
    감사 기록에서도 "누가 되살렸나"가 `update` 더미에 묻히지 않고 따로 보인다.

    권한은 해제와 **같은 함수**(`assert_can_write`)가 판정한다. 되살리는 쪽만 느슨하면
    "못 고치는 사람이 되살릴 수는 있다"는 구멍이 난다.

    이미 활성인 행에 다시 보내도 200이다(멱등). 그때는 감사 행을 안 남긴다 — `deactivate`와
    같은 규칙이라 두 창구의 기록 밀도가 갈리지 않는다.

    ⚠ 되살려도 **태그 UID·학번은 해제 때 그대로 남아 있던 값**이다. 해제는 행을 지우는 게
    아니라 깃발만 내리기 때문이다(§2.3).
    """
    row = await _get_staff(db, staff_id, lock=True)
    # ⭐ 상태를 바꾸는 창구다 — 총괄도 자기 계정은 못 내린다(2026-08-05 owner 도입).
    auth.assert_can_write(actor, row.rank, row, state_change=True)

    if row.is_active:
        return _staff_out(row)

    before = audit_snapshot(row)
    row.is_active = True
    add_audit(
        db,
        staff_id=row.id,
        action=ACTION_REACTIVATE,
        actor=actor,
        before=before,
        after=audit_snapshot(row),
    )
    await db.commit()
    await db.refresh(row)

    logger.info("명부 %s를 다시 살렸다", row.id)
    return _staff_out(row)


@router.delete("/{staff_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_staff(
    staff_id: int,
    actor: auth.AuthActor | None = Depends(auth.require_leader_always),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """명부 행을 아주 지운다. ⛔ **사건에 엮인 행은 못 지운다**(409).

    **왜 만들었나** — 사용자가 명부 관리 버튼을 **수정·일시정지·해제(영구 삭제)** 셋으로
    나누라고 정했다(2026-08-05). 예전에는 `deactivate` 하나뿐이라 **오타로 만든 행을 지울
    길이 없었다.**

    ⭐ **"사건 연결이 끊긴다"는 걱정은 조건으로 막았다.** 이 행을 가리키는 경고
    (`alert.staff_id`)가 하나라도 있으면 **409로 거절하고 일시정지를 안내한다.** 그래서
    지울 수 있는 것은 **아무 사건에도 안 엮인 행**뿐이다 — 잘못 등록한 행이 정확히 그 갈래다.

    ⚠ 조건 없이 지워도 DB가 막지는 않는다(`alert.staff_id`가 `ondelete="SET NULL"`이다).
    **막는 것은 이 창구의 판정 한 줄뿐이라** 지우면 그 경고가 조용히 주인을 잃는다. 그래서
    DB 제약이 아니라 **여기서 세우는 것이 계약**이다.

    ⚠ 감사 기록(`staff_audit`)은 안 사라진다 — 그 표는 일부러 FK를 안 걸어 뒀다(models.py
    §StaffAudit). 지운 행의 스냅샷을 `before`에 통째로 담으므로 **감사 행이 유일한 흔적**이다.

    ⚠ 경고에 적힌 이름(`alert.identified_person`)은 문자열 사본이라 그대로 남는다. 사라지는
    것은 "그 사람이 명부의 몇 번이었나"라는 연결뿐이다.
    """
    row = await _get_staff(db, staff_id, lock=True)
    # ⭐ 상태를 바꾸는 창구다 — 총괄도 자기 계정은 못 내린다(2026-08-05 owner 도입).
    auth.assert_can_write(actor, row.rank, row, state_change=True)

    linked = (
        await db.execute(
            select(func.count()).select_from(Alert).where(Alert.staff_id == row.id)
        )
    ).scalar_one()
    if linked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "reason": "staff_linked_to_alerts",
                "linked_alerts": linked,
                "message": (
                    f"이 사람에게 연결된 경고가 {linked}건 있어 지울 수 없습니다. "
                    "일시정지를 쓰면 기록을 지키면서 명단에서 내릴 수 있습니다."
                ),
            },
        )

    add_audit(
        db,
        staff_id=row.id,
        action=ACTION_DELETE,
        actor=actor,
        before=audit_snapshot(row),
        after={},
    )
    await db.delete(row)
    await db.commit()

    logger.warning("명부 %s를 지웠다 — 행위자 %s", staff_id, actor.username if actor else "-")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
