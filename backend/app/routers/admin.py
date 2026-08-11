"""관리자 전용 창구 — 지금은 시연 자료 비우기 하나다.

## `POST /api/admin/purge-demo-data`

발표 직전에 시험·리허설로 쌓인 자료를 비운다. 화면(시연 도구 안 "이벤트·알림 초기화")이
이 창구를 부른다.

⛔ **되돌릴 수 없다.** 같은 일을 하는 스크립트(`tools/purge_test_data.py`)는 지우기 전에
JSONL 백업을 뜨는데 **이 창구는 못 뜬다** — 컨테이너 파일 시스템이라 컨테이너와 함께
사라지고, 응답으로 내려보내면 그 안에 카드 UID 가 평문으로 실린다. 그래서 안전선을
백업이 아니라 **권한과 확인 문구**로 세웠다.

1. **관리자 세션만.** 팀장도 안 된다 — 명부 수정보다 위다.
2. **본문에 `confirm: "PURGE"`** 가 없으면 400. 버튼 오조작을 막는 마지막 관문이다.
3. 화면이 두 단계로 한 번 더 묻는다(첫 클릭에 빨강으로 바뀌고 5초 뒤 풀림). 그건 화면
   쪽 안전선이고 서버는 이 둘로 막는다.

## 지우는 범위는 스크립트와 같다

`tools.purge_test_data.PURGE_ORDER` 를 그대로 쓴다. 목록을 여기 다시 적지 않는 이유는
두 자리가 갈라지는 것을 막으려는 것이다 — 스크립트에 표가 하나 늘고 여기가 안 늘면
"스크립트로 지운 것과 버튼으로 지운 것이 다른" 상태가 되고, 그건 발표 당일에야 드러난다.

계정·세션·명부·감사 기록(`staff_audit`·`staff_access_log`)은 안 지운다. 그쪽은 시험
자료가 아니라 시연 자산이고, 감사 기록은 append-only 계약이라 지우는 창구를 안 만든다.

## ⚠ 시퀀스를 되돌린다

지우기만 하면 번호가 안 내려간다 — `pass_id` 8101 을 지워도 다음 통과가 8102 로
들어온다. 발표 첫 통과가 "8102번"으로 뜨는 것을 막으려고 시퀀스를 1부터 다시 시작하게
한다. 스크립트의 `--reset-sequences` 와 같은 동작이다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.db import get_db
from app.models import StaffAudit
from app.routers.staff import ACCOUNT_AUDIT_STAFF_ID
from app.schemas import PurgeAccessLogOut, PurgeDemoDataIn, PurgeDemoDataOut
from tools.purge_test_data import PURGE_ORDER

logger = logging.getLogger("c207.admin")

router = APIRouter(prefix="/api/admin", tags=["admin"])

# 본문에 이 값이 그대로 실려야 지운다. 화면이 두 단계로 묻더라도 서버가 한 번 더 받는다 —
# 화면 판정만 믿으면 창구를 직접 부르는 자리에서 그냥 지워진다.
_CONFIRM_WORD = "PURGE"

# 조회 이력을 비운 사실을 감사에 남길 때 쓰는 낱말. 명부 조작 셋(create·update·deactivate·
# reactivate)과 갈래가 달라 이름을 따로 둔다 — 감사 표를 훑을 때 "표를 통째로 비운 일"이
# 개별 행 수정에 묻히면 안 된다.
ACTION_PURGE_ACCESS_LOG = "purge_access_log"
# ⚠ `staff_audit.action` 이 `String(16)` 이다. 이 값이 정확히 16자라 **한 글자만 길었으면
#   그 요청이 500이었다** (`purge_access_log` 도 같은 자리다).
ACTION_PURGE_DEMO_DATA = "purge_demo_data"


@router.post("/purge-demo-data", response_model=PurgeDemoDataOut)
async def purge_demo_data(
    body: PurgeDemoDataIn,
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> PurgeDemoDataOut:
    """시연 자료를 비운다. ⛔ 되돌릴 수 없다 — 위 모듈 머리를 먼저 읽어라.

    `require_admin_always` 를 쓰는 이유는 명부·계정 창구와 같다. 기본 역할 게이트는
    `AUTH_REQUIRE_LOGIN` 이 꺼져 있으면 판정을 아예 안 하는데, 이 창구는 그 플래그와
    무관하게 늘 막혀야 한다. 꺼진 판에서 익명이 실 자료를 지우는 갈래가 생기면 안 된다.

    지운 건수를 표별로 돌려준다. 화면이 `total` 을 읽어 "N건을 비웠습니다"로 알린다 —
    눌렀는데 아무 말이 없으면 됐는지 안 됐는지 모른다.
    """
    if body.confirm != _CONFIRM_WORD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"확인 문구가 다릅니다. confirm 에 {_CONFIRM_WORD} 를 실어 주세요.",
        )

    deleted: dict[str, int] = {}
    for table in PURGE_ORDER:
        # ⚠ 표 이름은 PURGE_ORDER 상수라 바깥 입력이 아니다. 그래도 f-string 으로 SQL 을
        #   짜는 자리라 목록을 바꿀 때 이 사실을 같이 봐라 — 사용자 입력이 여기 닿는 길이
        #   생기면 그 순간 주입 자리가 된다.
        res = await db.execute(text(f"DELETE FROM {table}"))  # noqa: S608
        deleted[table] = res.rowcount or 0

    total = sum(deleted.values())
    # ⭐ **지운 사실을 감사에 남긴다** (2026-08-06). 아래 `purge_access_log` 는 예전부터
    #   남기는데 **이쪽은 로그만 찍고 감사에 안 남겼다.** 훨씬 큰 조작인데 비대칭이었다.
    #
    #   ⛔ 그래서 실제로 잃은 것이 있다 — 사용자가 개발자 화면에서 보고 남겨 둔 **카드 UID
    #   태깅 셋이 사라졌는데, 누가 언제 지웠는지 되짚을 자리가 한 곳도 없었다.** 시퀀스가
    #   1로 리셋된 것만 흔적으로 남아 "이 창구가 돌았다"를 짐작했을 뿐이다.
    #
    #   ⚠ `staff_audit` 는 append-only 계약이고 `PURGE_ORDER` 에도 안 들어 있어서 이 기록은
    #   다음 비우기에도 안 지워진다. **표가 비어도 "누가 언제 몇 건을 지웠다"는 남는다.**
    db.add(
        StaffAudit(
            staff_id=ACCOUNT_AUDIT_STAFF_ID,
            action=ACTION_PURGE_DEMO_DATA,
            actor_user_id=actor.id if actor else None,
            actor_username=actor.username if actor else None,
            actor_display_name=actor.display_name if actor else None,
            actor_role=actor.role if actor else None,
            before=None,
            after={"deleted": deleted, "total": total, "sequences_reset": True},
        )
    )
    await db.commit()

    # ⛔ **시퀀스 되돌리기는 삭제 커밋 "뒤"다** (2026-08-09 백지검토 수리). 예전에는 위 DELETE
    #   루프와 같은 트랜잭션 안에 있었는데, **Postgres 시퀀스는 롤백을 안 탄다.** 그 판에서
    #   커밋이 어떤 이유로든 실패하면 행은 전부 살아 있는데 시퀀스만 1로 내려가, 다음 인입
    #   INSERT 가 살아 있는 id 를 잡아 duplicate key 로 터진다. `pass_id` 가 8000번대인 지금은
    #   그 고장이 8000건 넘게 이어진다.
    #
    #   같은 일을 하는 스크립트가 이미 같은 규칙을 못박아 뒀다 —
    #   `tools/purge_test_data.reset_sequences` docstring("삭제 트랜잭션 밖에서 불러야 한다")과
    #   그 호출 자리(커밋 뒤).
    #
    #   ⚠ 값은 그대로 `1, false` 다. 이 창구는 **표를 통째로** 비우므로 남은 행이 없어서
    #   스크립트의 `max(id)` 셈법이 필요 없다(스크립트는 `--before` 부분 삭제 갈래가 있어서
    #   그 셈법을 쓴다). 여기서 부분 삭제가 생기면 그때 같이 옮겨라.
    for table in PURGE_ORDER:
        await db.execute(
            text(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), 1, false)")  # noqa: S608
        )
    await db.commit()

    # ⚠ warning 인 이유는 `c207.*` 로거의 운영 유효 레벨이 WARNING 이라서다. info 로 적으면
    #   운영에서 한 줄도 안 남는다(2026-08-04에 `c207.security` 에서 밟은 자리다).
    logger.warning(
        "purge_demo_data actor=%s total=%s detail=%s",
        actor.username if actor else "-",
        total,
        deleted,
    )
    return PurgeDemoDataOut(deleted=deleted, total=total, sequences_reset=True)


@router.post("/purge-access-log", response_model=PurgeAccessLogOut)
async def purge_access_log(
    body: PurgeDemoDataIn,
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    db: AsyncSession = Depends(get_db),
) -> PurgeAccessLogOut:
    """명부 조회 이력(`staff_access_log`)을 비운다. ⛔ 되돌릴 수 없다.

    **왜 만들었나** — 사용자가 시연 도구에 "명부 이력 지우기" 버튼을 두라고 정했다. 리허설이
    남긴 조회 자국이 발표에서 "누가 명부를 봤나" 표를 못 쓰게 만들기 때문이다.

    ⭐ **지운 사실 자체는 `staff_audit`에 남긴다.** 프론트가 12차 §3에서 짚은 위험이
    "관리자가 자기 조회 흔적을 지울 수 있어 그 표가 감사 구실을 못 한다"였는데, 감사 표는
    append-only 계약이라(로그인 설계 §4) 지우는 창구가 없다. 그래서 **표는 비어도 "누가 언제
    몇 건을 지웠다"가 남는다.** 그 한 줄이 이 창구를 감사 가능하게 만든다.

    ⚠ `staff_access_log`는 `NEVER_PURGED`라 시연 자료 비우기(`purge-demo-data`)가 안 건드린다.
    성격이 달라서다 — 저쪽은 시연 흔적이고 이쪽은 감사 기록이다. 창구를 따로 둔 이유가 그것이다.

    ⚠ 시퀀스는 안 되돌린다. 번호가 이어져야 "지운 뒤에도 새 조회는 계속 센다"가 눈에 보인다.
    """
    if body.confirm != _CONFIRM_WORD:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"확인 문구가 다릅니다. confirm 에 {_CONFIRM_WORD} 를 실어 주세요.",
        )

    res = await db.execute(text("DELETE FROM staff_access_log"))
    deleted = res.rowcount or 0

    # ⭐ 지운 사실을 감사에 남긴다. `staff_id`는 계정 감사와 같은 sentinel을 쓴다 —
    #    특정 명부 행에 대한 조작이 아니라 표 전체에 대한 조작이라서다.
    audit = StaffAudit(
        staff_id=ACCOUNT_AUDIT_STAFF_ID,
        action=ACTION_PURGE_ACCESS_LOG,
        actor_user_id=actor.id if actor else None,
        actor_username=actor.username if actor else None,
        actor_display_name=actor.display_name if actor else None,
        actor_role=actor.role if actor else None,
        before=None,
        after={"deleted": deleted},
    )
    db.add(audit)
    await db.flush()
    audit_id = audit.id
    await db.commit()

    # ⚠ warning 인 이유는 위 창구와 같다 — `c207.*` 운영 유효 레벨이 WARNING 이다.
    logger.warning(
        "purge_access_log actor=%s deleted=%s audit_id=%s",
        actor.username if actor else "-",
        deleted,
        audit_id,
    )
    return PurgeAccessLogOut(deleted=deleted, audit_id=audit_id)
