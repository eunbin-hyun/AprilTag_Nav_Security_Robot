"""계정 창구 (로그인 설계 확정본 2026-08-01 §3 · 지라 -349). **전부 관리자 전용**이다.

    GET   /api/users                 목록
    POST  /api/users                 생성
    PATCH /api/users/{id}            역할·활성 수정
    POST  /api/users/{id}/password   비밀번호 다시 정하기
    GET   /api/users/{id}/audit      그 계정에 벌어진 변경 기록 읽기

⚠ 응답에 `password_hash`를 절대 안 싣는다. API 키·DB 접속 문자열을 돌려주는 창구도 만들지
않는다 — "키는 서버 설정에만 존재한다"가 범위 정본 §1이다.

## 마지막 관리자 보호는 A의 함수 하나뿐이다

`auth.assert_not_last_admin`이 활성 관리자 행을 잠그고 개수를 센다. 이 파일은 그 함수를
부르고 **같은 트랜잭션에서** 강등·해제 UPDATE까지 커밋한다 — 잠그고 커밋해 버린 뒤에
UPDATE를 따로 내면 그 사이가 다시 열린다(그 함수 docstring의 경고다).

⚠ 대상 계정 행을 `FOR UPDATE`로 미리 잠그지 **않는다.** 잠그면 잠금 순서가 둘로 갈려
(대상 행 → 관리자 행 집합) 관리자 둘을 서로 강등하는 두 요청이 교착에 빠진다.
`assert_not_last_admin`이 `ORDER BY id`로 한 줄 세우는 걸 깨지 않으려고 그 함수 하나에만
잠금을 맡긴다. 대상 계정을 잠금 없이 읽어도 안전한 이유는 그 함수가 잠금을 잡은 뒤
관리자 집합을 **다시 읽기** 때문이다(READ COMMITTED 잠금 재평가).

## 계정 변경도 `staff_audit`에 남는다

남기는 자리는 셋이다 — 생성(`create`), 역할·활성 수정(`update`, 해제는 `deactivate`),
비밀번호 다시 정하기(`update` + 본문 `password_reset`). 조회는 안 남긴다. 낱말이 셋뿐인 건
§2.4 action enum 그대로이고, 그 안에서 무슨 일이었나는 기록 본문이 말한다.

지시받은 계약이라 명부 감사 테이블을 같이 쓴다. `staff_id`는 `ACCOUNT_AUDIT_STAFF_ID`(0)로
"명부 행이 아니다"를 표시하고, 진짜 대상은 기록 본문에 싣는다. 전용 테이블을 팔지 여부는
보고의 결정 필요에 올렸다(`app/routers/staff.py`의 그 상수 주석에 근거가 있다).

읽는 자리는 `GET /api/users/{id}/audit` 하나다. 명부 쪽 `GET /api/staff/{id}/audit`과 같은
모양·같은 급(창구 전체가 관리자)이고, 쓰기·삭제 창구는 여기도 안 만든다 — append-only는
창구 부재로 지킨다(§4). 0번 자리에 계정 기록과 신원 삭제 기록(`identity_clear`,
`app/routers/query.py`)이 같이 쌓이므로, 조회는 `staff_id`만 보면 안 되고 본문의
`target`까지 봐야 갈래가 안 섞인다.

## ⭐ `AUTH_REQUIRE_LOGIN=false` 구간에도 이 창구들은 닫혀 있다

다섯 창구 전부 `require_admin_always`라 플래그와 무관하게 늘 판정한다. 근거는
`app/routers/staff.py` 모듈 머리와 같고, 계정 생성 창구라 폭이 더 넓어서 더 중요한
자리다 — 열어 두면 플래그를 켜기 전 구간에 아무나 관리자 계정을 만들 수 있다. 계정
심기는 창구가 아니라 `tools/seed_users.py`로 하는 게 §6.2 2단계다.

⛔ 쓰기 셋(생성·수정·비밀번호)은 거기에 **급 사다리 판정**(`auth.assert_can_write_account`)
을 더 탄다(2026-08-08 백지검토). `require_admin_always`만으로는 관리자가 자기를 `owner`로
올리고 총괄 비밀번호를 갈아 끼우는 길이 열려 있었다 — 명부가 `assert_can_write` 여덟
자리로 막는 그 일을 계정 쪽만 안 막고 있었다.

못박아 둔 시험은 `tests/test_account_endpoints.py`의
`test_플래그가_꺼져도_계정_창구는_익명을_막는다`와
`test_플래그가_꺼져도_계정_감사_조회는_익명을_막는다` 둘이다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.db import get_db
from app.models import AppUser, StaffAudit
from app.routers.staff import (
    ACCOUNT_AUDIT_STAFF_ID,
    ACTION_CREATE,
    ACTION_DEACTIVATE,
    ACTION_UPDATE,
    DEFAULT_AUDIT_LIMIT,
    MAX_AUDIT_LIMIT,
    add_audit,
)
from app.schemas import (
    AppUserCreateIn,
    AppUserOut,
    AppUserPatchIn,
    PasswordResetIn,
    StaffAuditOut,
)

logger = logging.getLogger("c207.account")

router = APIRouter(prefix="/api/users", tags=["account"])

DEFAULT_USER_LIMIT = 100
MAX_USER_LIMIT = 500

# 계정 감사 행의 `after.target` 낱말. 심는 자리(account_snapshot)와 읽는 자리(read_user_audit)가
# 같은 값을 봐야 해서 상수로 뺐다 — 문자열을 두 자리에 손으로 적으면 한쪽만 고쳐진다.
ACCOUNT_AUDIT_TARGET = "app_user"

_NOT_FOUND = "계정을 찾을 수 없습니다."
_BAD_USERNAME = "아이디는 영문 소문자와 숫자 3~20자로 지어 주세요."
_DUP_USERNAME = "이미 있는 아이디입니다."


def _user_out(user: AppUser) -> AppUserOut:
    """계정 행 → 응답. 계정을 내보내는 자리는 전부 이 함수 하나를 탄다.

    `password_hash`가 여기 없다는 게 계약이다 — 갈래마다 손으로 칸을 채우면 언젠가 한
    자리가 해시를 같이 싣는다.
    """
    return AppUserOut(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        is_active=user.is_active,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def account_snapshot(user: AppUser) -> dict:
    """감사 기록에 실을 계정 사본. 명부 사본과 섞이지 않게 `target`을 같이 적는다.

    비밀번호 해시는 안 담는다 — 감사 기록은 나중에 읽히는 자료라 거기 해시를 흘리면
    응답에서 막은 걸 기록으로 새게 하는 셈이다.
    """
    return {
        "target": ACCOUNT_AUDIT_TARGET,
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
    }


async def _get_user(db: AsyncSession, user_id: int) -> AppUser:
    """계정 행을 가져온다. 없으면 404. **잠그지 않는다**(모듈 머리의 교착 설명)."""
    user = await db.get(AppUser, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND)
    return user


@router.get("", response_model=list[AppUserOut])
async def list_users(
    limit: int = Query(DEFAULT_USER_LIMIT, ge=1, le=MAX_USER_LIMIT),
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> list[AppUserOut]:
    """계정 목록. 해제된 계정도 같이 나온다 — 안 보이면 되살릴 방법이 없다."""
    rows = (
        await db.execute(select(AppUser).order_by(AppUser.id).limit(limit))
    ).scalars().all()
    return [_user_out(u) for u in rows]


@router.get("/{user_id}/audit", response_model=list[StaffAuditOut])
async def read_user_audit(
    user_id: int,
    limit: int = Query(DEFAULT_AUDIT_LIMIT, ge=1, le=MAX_AUDIT_LIMIT),
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> list[StaffAuditOut]:
    """그 계정에 벌어진 변경 기록. **읽기만이다** — 고치거나 지우는 창구는 없다(§4).

    여기 실려 나오는 사건은 셋이다 — 계정 생성(`create`), 역할·활성 수정(`update`,
    해제만 `deactivate`), 비밀번호 다시 정하기(`update` + `after.password_reset=true`).
    비밀번호는 원문도 해시도 기록에 없다.

    경로·모양·정렬을 명부 쪽 `GET /api/staff/{id}/audit`에 맞췄다. 응답 스키마도 같은
    `StaffAuditOut`을 그대로 쓴다 — 한 테이블 한 줄을 두 창구가 다른 모양으로 내보내면
    화면이 갈래마다 다른 파서를 든다.

    급은 계정 창구와 같은 **관리자**다(`require_admin_always`). 누가 누구를 강등했나가 곧
    권한 정본의 이력이라 계정 목록과 같은 자리에서 본다.

    ⚠ 거르는 조건이 셋이다. `staff_id=0`은 "명부 행이 아니다" 표시일 뿐이라 그 자리에
    신원 삭제 기록(`action="identity_clear"`, `after.target="alert_identity"`)도 같이 쌓인다.
    그래서 `target`이 계정인 것만 고르고, 그 안에서 `after.user_id`로 대상을 좁힌다.
    `before`가 아니라 `after`를 보는 이유는 그 칸이 NOT NULL이라 어느 행에도 반드시 있어서다.

    없는 계정 번호면 404다. 감사 행은 계정이 지워져도 남지만(`actor_user_id`만 SET NULL로
    끊긴다), 이 창구는 "그 계정의 이력"을 읽는 자리라 대상이 실재해야 뜻이 선다 — 명부 쪽과
    같은 판단이다.

    최근 것부터 내려온다.
    """
    await _get_user(db, user_id)
    rows = (
        await db.execute(
            select(StaffAudit)
            .where(
                StaffAudit.staff_id == ACCOUNT_AUDIT_STAFF_ID,
                StaffAudit.after["target"].astext == ACCOUNT_AUDIT_TARGET,
                StaffAudit.after["user_id"].astext == str(user_id),
            )
            .order_by(StaffAudit.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [StaffAuditOut.model_validate(r, from_attributes=True) for r in rows]


@router.post("", response_model=AppUserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: AppUserCreateIn,
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> AppUserOut:
    """계정 생성.

    아이디 모양은 `auth.USERNAME_RE` 하나로 판정한다(정본 한 자리 — 스키마에 같은 규칙을
    또 적으면 한쪽만 고쳐진다). **계정을 만드는** 창구라서 여기서는 규칙 밖 아이디를 422로
    막는 게 맞다 — 로그인 창구가 같은 걸 401로 흘리는 건 계정 열거를 막으려는 반대 이유다.

    아이디는 대소문자를 접어 저장한다. 규칙이 소문자라 접어도 남의 계정에 안 닿고, 안 접으면
    `Admin`과 `admin`이 다른 계정으로 서서 로그인 잠금까지 갈라진다.

    감사 기록은 같은 커밋에 들어간다. `flush`로 번호를 먼저 받아야 기록에 그 번호를 실을 수
    있고(`after.user_id`가 조회 창구의 거르는 조건이다), 그 사이에 커밋을 끼우면 계정만 서고
    기록이 빠지는 갈라짐이 생긴다 — 명부 등록(`create_staff`)과 같은 모양이다.

    ⚠ 비밀번호는 어디에도 안 찍는다. 로그에 남기는 건 아이디와 역할까지고, 감사 사본에도
    원문·해시가 안 실린다(`account_snapshot`).

    ⭐ **중복 아이디는 두 겹으로 막는다.** 미리 보는 SELECT는 흔한 실수에 친절한 문구를
    주려는 것이고, 진짜 정본은 DB의 유니크 제약이다. 같은 아이디로 두 요청이 동시에 오면
    둘 다 선검사를 지나고 뒤엣것이 INSERT에서 걸리는데, 그걸 안 잡으면 **500**이 나간다 —
    같은 사건에 응답이 409와 500 둘로 갈리면 화면이 재시도할지 문구를 띄울지 못 정한다.
    그래서 `IntegrityError`를 같은 409로 옮긴다(문구도 선검사와 같은 값이다).

    ⚠ 비밀번호 해시는 `hash_password_async`로 만든다. 동기판은 16MiB scrypt를 이벤트 루프
    위에서 돌려 서버 전체를 수백 밀리초 세운다(로봇 하트비트·출동 대기 창까지 같이 멈춘다).
    """
    # 급 판정이 아이디 검사보다 먼저다 — 권한 없는 요청에는 422·409로 계정 존재 여부를
    # 흘리지 않고 403 하나로 끝낸다.
    auth.assert_can_write_account(actor, new_role=body.role.value)
    username = auth.normalize_username(body.username)
    if not auth.USERNAME_RE.match(username):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=_BAD_USERNAME
        )
    if (
        await db.execute(select(AppUser.id).where(AppUser.username == username))
    ).first() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DUP_USERNAME)

    user = AppUser(
        username=username,
        password_hash=await auth.hash_password_async(body.password),
        display_name=body.display_name,
        role=body.role.value,
        # server_default가 있어도 명시한다 — 안 그러면 커밋 뒤 이 객체의 칸이 None이다.
        is_active=True,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        # 선검사와 INSERT 사이에 딴 요청이 같은 아이디를 먼저 넣었다. 유니크 제약이 잡아
        # 준 자리라 500이 아니라 409다. 되돌려야 뒤이은 조회가 죽은 트랜잭션을 안 만난다.
        await db.rollback()
        logger.info("계정 %r 생성이 동시 요청과 부딪혀 409로 돌려보낸다", username)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=_DUP_USERNAME
        ) from None
    await db.refresh(user)  # created_at 같은 server_default를 실물로 읽는다
    add_audit(
        db,
        staff_id=ACCOUNT_AUDIT_STAFF_ID,
        action=ACTION_CREATE,
        actor=actor,
        before=None,
        after=account_snapshot(user),
    )
    await db.commit()

    logger.info("계정 %r을 만들었다(역할 %s)", username, user.role)
    return _user_out(user)


@router.patch("/{user_id}", response_model=AppUserOut)
async def update_user(
    user_id: int,
    body: AppUserPatchIn,
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> AppUserOut:
    """계정 역할·활성 수정. 권한 판정의 정본을 바꾸는 자리라 감사 기록이 같이 남는다.

    **마지막 관리자 보호** — 활성 관리자를 강등하거나 해제할 때만 검사가 돈다. 잠금과 UPDATE가
    같은 커밋이라야 뜻이 있어서 그 사이에 커밋을 안 끼운다(§4).

    관리자가 자기를 강등하는 건 막지 않는다 — 정본은 "마지막 관리자"만 막는다(§8 결정 4).

    해제하면 그 사람의 살아 있는 세션을 전부 끊는다. `resolve_session`이 매 요청 계정을 같이
    보기 때문에 안 끊어도 다음 요청에서 죽지만, 세션 행에 "언제 왜 끊겼나"를 남기는 게 감사
    자료다(§1.4). 세션 끊기는 **커밋 뒤에** 한다 — 그 함수가 자기 커밋을 내기 때문에 앞에
    두면 마지막 관리자 잠금이 그 자리에서 풀린다.

    ⚠ **감사 사본은 잠금을 잡은 뒤에 뜬다.** 대상 행을 처음 읽는 자리(`_get_user`)는 잠금이
    없어서(모듈 머리의 교착 설명) 그 사이에 딴 요청이 같은 계정을 강등·해제하고 커밋할 수
    있다. 그때 메모리에 남은 옛 값으로 `before`를 뜨면 감사 행이 DB 실물과 다른 거짓을
    남긴다 — 권한 이력을 되짚는 유일한 자료가 거짓이면 그 표가 통째로 못 믿을 값이 된다.
    """
    user = await _get_user(db, user_id)
    sent = body.model_fields_set

    demoting = "role" in sent and body.role.value != auth.ROLE_ADMIN
    deactivating = "is_active" in sent and body.is_active is False

    # 지금 급과 새 급을 **둘 다** 대조한다(auth.assert_can_write_account docstring ④).
    # 본인 계정은 내려가기만 통과다(docstring ③ — §8 결정 4의 자기 강등 허용과
    # 마지막 관리자 보호는 그대로 두고, 자기 승격만 여기서 막는다).
    auth.assert_can_write_account(
        actor,
        target_role=user.role,
        new_role=body.role.value if "role" in sent else None,
        target_user_id=user.id,
        deactivate=deactivating,
    )
    # ⚠ **대상이 지금 관리자인지를 여기서 안 본다**(2026-08-02 백지 검토 3차 X20). 예전에는
    # `user.role == ADMIN and user.is_active`를 앞세워 갈래를 갈랐는데, 그 값은 잠금 없는 첫
    # SELECT 것이라 그 사이에 관리자로 승격된 계정이 검사를 통째로 건너뛰었다 — 그 갈래로
    # 강등이 겹치면 관리자가 0이 된다. `assert_not_last_admin`은 활성 관리자 행을 잠근 뒤
    # **다시 읽어** 대상이 그 집합에 있는지 보므로, 관리자가 아닌 계정에는 그냥 no-op이다.
    # 판정을 그 함수 하나에 맡기면 "읽고 → 판정하고 → 쓰는" 사이의 창이 사라진다.
    #
    # 대상 행을 `FOR UPDATE`로 미리 잠그는 쪽은 여전히 안 쓴다 — 잠금 순서가 둘로 갈려
    # 교착이 열린다(모듈 머리). 잠그는 자리는 지금도 그 함수 하나뿐이다.
    if demoting or deactivating:
        await auth.assert_not_last_admin(db, user.id)

    # 잠금을 잡은 뒤 대상 행을 다시 읽는다. READ COMMITTED라 이 SELECT는 앞 트랜잭션이
    # 커밋한 실물을 본다 — 사본도 뒤이은 UPDATE도 같은 값을 딛는다. 잠금을 안 잡은 갈래
    # (강등·해제가 아닌 수정)에서도 한 번 더 읽어 두는 게 손해가 아니다: 사본이 오래된
    # 값일 창이 그만큼 좁아지고, 아직 아무 칸도 안 바꿨으니 지울 변경도 없다.
    await db.refresh(user)

    # ⭐ 08-08 교차 검증 — **실물을 다시 읽었으니 같은 판정을 한 번 더 돈다.** 위 첫 호출의
    # target_role은 잠금 없는 SELECT 값이라, 그 사이에 대상이 승격됐으면 낡은 급으로 통과한
    # 채 여기까지 온다(X20 주석이 못박은 그 창과 같은 모양이다). 첫 호출은 흔한 갈래의 이른
    # 403용으로 남긴다 — 잠금·refresh 비용을 아낀다.
    auth.assert_can_write_account(
        actor,
        target_role=user.role,
        new_role=body.role.value if "role" in sent else None,
        target_user_id=user.id,
        deactivate=deactivating,
    )

    before = account_snapshot(user)
    if "role" in sent:
        user.role = body.role.value
    if "is_active" in sent:
        user.is_active = body.is_active

    add_audit(
        db,
        staff_id=ACCOUNT_AUDIT_STAFF_ID,
        # 해제는 되돌리기 어려운 조작이라 낱말을 갈라 둔다. 역할만 바뀌면 update다.
        action=ACTION_DEACTIVATE if deactivating else ACTION_UPDATE,
        actor=actor,
        before=before,
        after=account_snapshot(user),
    )
    await db.commit()
    await db.refresh(user)

    if deactivating:
        await auth.revoke_user_sessions(db, user.id)

    logger.info("계정 %s를 수정했다(바뀐 칸 %s)", user.id, sorted(sent))
    return _user_out(user)


@router.post("/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    user_id: int,
    body: PasswordResetIn,
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """비밀번호를 다시 정하고 그 계정의 세션을 전부 끊는다.

    끊는 이유 — 비밀번호를 바꾸는 상황은 대개 "샜을지 모른다"라서, 옛 비밀번호로 열려 있던
    세션이 살아 있으면 바꾼 뜻이 없다. ⚠ 본인은 이 창구를 못 쓴다(08-08) — 지금 비밀번호를
    확인하는 전용 창구(`POST /api/auth/password`)로 간다. 아래 본문 첫 가드가 그 못이다.

    204라 본문이 없다. 응답 객체를 직접 만들어 돌려주는 건 로그아웃 창구와 같은 이유다.

    감사 기록이 같은 커밋에 남는다(`action="update"`). 남의 비밀번호를 갈아 끼우는 건 계정
    생성과 함께 권한 이력에서 제일 무거운 사건이라, 안 남기면 "누가 언제 남의 계정을 열었나"가
    어디에도 없다. 낱말을 새로 만들지 않고 `update`를 쓰는 이유는 §2.4 action enum이
    create·update·deactivate 셋이라서고, 무엇을 바꿨나는 본문의 `password_reset` 표시가 말한다.

    ⚠ 비밀번호를 로그에도 응답에도 **감사 기록에도** 안 싣는다 — 원문도 해시도다. 감사는
    나중에 읽히는 자료라 거기 해시를 흘리면 응답에서 막은 걸 기록으로 새게 하는 셈이다.

    ⭐ **해시 교체와 세션 끊기가 한 트랜잭션이다.** 예전에는 해시를 먼저 커밋하고 그 뒤에
    `revoke_user_sessions`를 따로 불렀다. 커밋 둘 사이에 프로세스가 죽으면 비밀번호만
    바뀌고 옛 세션은 살아 있다 — "샜을지 모른다"라서 바꾸는 자리인데 정작 새어 나간 세션이
    그대로 남는, 이 창구가 막으려던 바로 그 상태다. 그래서 무효화 UPDATE를 커밋 **앞으로**
    옮겨 같은 트랜잭션에 얹었다. 인증 코어가 `commit=False` 판을 열어 줘서 지금은 그 인자를
    **명시로** 넘기고 커밋을 이 함수가 낸다 — 그 한 커밋이 셋(해시·감사 행·세션 무효화)을
    한꺼번에 확정한다. 인자를 안 적고 기본값에 기대면, 기본값이 바뀌는 날 이 자리가 조용히
    두 커밋으로 갈라진다.

    ⚠ 순서를 되돌리지 마라 — 무효화를 커밋 뒤로 옮기면 갈라짐이 다시 열린다. 같은 파일의
    **계정 해제** 갈래는 반대로 커밋 뒤가 맞다. 거긴 `assert_not_last_admin`의 행 잠금이
    커밋까지 살아 있어야 해서다.

    ⚠ 해시는 `hash_password_async`로 만든다(계정 생성과 같은 이유 — 동기판은 16MiB scrypt를
    이벤트 루프 위에서 돌려 서버 전체를 수백 밀리초 세운다).
    """
    user = await _get_user(db, user_id)
    # 본인은 이 창구를 못 쓴다 — 여긴 지금 비밀번호를 안 묻는 "잊었을 때" 창구라, 본인까지
    # 열면 세션을 훔친 쪽이 지금 비밀번호 없이 계정을 통째로 가져간다(08-08 교차 검증).
    # 본인은 지금 비밀번호를 확인하는 전용 창구(POST /api/auth/password)로 간다.
    if actor is not None and actor.id == user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="본인 비밀번호는 설정의 비밀번호 변경에서 바꿔 주세요. 이 창구는 아랫급 계정 전용입니다.",
        )
    # 남의 비밀번호는 아랫급 것만 갈 수 있다 — 관리자가 총괄·동급 관리자 비밀번호를 갈아
    # 끼우면 그 계정 자체를 넘겨받는 길이다.
    # ⚠ 여기 판정은 잠금 없는 최신값으로 돈다 — update_user와 달리 "판정 창"을 못으로
    #   막지 못한다. 진짜 창은 판정 → hash_password_async(scrypt 수백 ms) → flush 사이이고
    #   거기서 대상이 승격되면 낡은 급으로 통과한다. 이 창구는 대상 행을 안 잠그고(교착
    #   회피, 모듈 머리) hash가 무거워 창이 넓다 — 완전 차단이 필요하면 대상 행 잠금이나
    #   hash 뒤 재판정을 얹어야 한다(2026-08-08 2차 교차 검증에서 근거를 바로잡았다).
    #   지금은 그 위험을 감수한다 — 관리자 급 이상만 닿는 창구라 공격 표면이 좁다.
    auth.assert_can_write_account(actor, target_role=user.role, target_user_id=user.id)
    # 스냅샷 칸(역할·활성)은 이 조작으로 안 바뀐다. 그래도 before를 같이 남기는 이유는 기록
    # 모양을 다른 계정 감사 행과 같게 두려는 것이다 — 갈래마다 모양이 다르면 화면이 파서를
    # 여럿 든다. 바뀐 사실은 after의 `password_reset` 한 칸이 말한다.
    before = account_snapshot(user)
    user.password_hash = await auth.hash_password_async(body.password)
    add_audit(
        db,
        staff_id=ACCOUNT_AUDIT_STAFF_ID,
        action=ACTION_UPDATE,
        actor=actor,
        before=before,
        after={**account_snapshot(user), "password_reset": True},
    )
    # 해시 UPDATE와 감사 INSERT를 트랜잭션에 밀어 넣고, 세션 무효화 UPDATE까지 같은
    # 트랜잭션에 얹은 뒤 한 번에 커밋한다(위 ⭐ 문단).
    await db.flush()
    await auth.revoke_user_sessions(db, user.id, commit=False)
    await db.commit()

    logger.info("계정 %s의 비밀번호를 다시 정하고 세션을 끊었다", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
