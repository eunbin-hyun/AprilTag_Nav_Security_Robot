"""로그인 창구 넷 (로그인 설계 확정본 2026-08-01 §7.1 · 비밀번호는 2026-08-08 설정 창).

    POST /api/auth/login     200 + Set-Cookie / 401 / 429
    POST /api/auth/logout    204 + 만료된 Set-Cookie (세션이 없어도 204 — 멱등)
    GET  /api/auth/me        200 / 401
    POST /api/auth/password  204 / 400(지금 비밀번호 틀림) / 401 / 422 / 429 — 본인 전용

라우터는 얇다 — 해시·세션·잠금은 전부 `app/auth.py`가 한다. 여기서는 HTTP 모양만 맞춘다.

## ⚠ 이 셋은 `AUTH_REQUIRE_LOGIN`과 무관하게 늘 산다

플래그는 "다른 창구가 로그인을 요구하느냐"를 가르는 스위치지 로그인 자체의 스위치가
아니다. 꺼진 채로 배포해서 계정을 심고 관리자로 실제 로그인해 보는 게 롤아웃 3단계다
(§6.2). 그러니 이 창구가 플래그에 묶이면 켜기 전에 아무것도 검증 못 한다.

## 401과 403을 가르는 이유 (§7.2)

여기 나가는 실패는 전부 401이다 — "세션이 없다·틀렸다"라서 화면이 로그인으로 보내야
한다. 급이 모자란 건 403이고 그건 각 창구의 역할 게이트가 낸다. 403을 401처럼 다루면
요원이 관리자 창구를 한 번 건드릴 때마다 로그아웃된다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.config import get_settings
from app.db import get_db
from app.models import AppUser
from app.routers.account import account_snapshot
from app.routers.staff import ACCOUNT_AUDIT_STAFF_ID, ACTION_UPDATE, add_audit
from app.schemas import AuthUserOut, LoginIn, OwnPasswordChangeIn

logger = logging.getLogger("c207.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# 아이디·비밀번호 중 무엇이 틀렸는지 **구분하지 않는다.** 구분하면 그게 곧 계정 열거다.
_BAD_CREDENTIALS = "아이디 또는 비밀번호가 올바르지 않습니다."
_LOCKED = "잠시 후 다시 시도해 주세요."
_NEED_LOGIN = "로그인이 필요합니다."


def _set_session_cookie(response: Response, token: str) -> None:
    """세션 쿠키를 심는다. 속성 넷이 계약이다(§1.2).

    - `HttpOnly` — JS가 못 읽는다. 토큰이 스크립트로 새는 길을 닫는다.
    - `Secure` — 배포는 443+certbot이라 기본으로 붙고, http로 도는 로컬만 설정으로 끈다.
    - `SameSite=Lax` — 화면과 창구가 **같은 오리진**이라(nginx가 `/`에 SPA, `/api/`에
      백엔드) `None`도 CORS도 필요 없다. 상태변경 창구가 전부 POST·DELETE라 Lax만으로
      교차 사이트 위조가 실질 차단된다.
    - `Path=/` — WS 핸드셰이크(`/ws/dashboard`)에도 같이 실려야 한다.

    `max_age`는 절대 만료와 같은 값이다. 브라우저 쪽 수명이 서버 쪽보다 길면 죽은 토큰을
    계속 실어 보내 401을 맞는다.

    ⭐ 이름은 `auth.session_cookie_name()`이 정한다 — https면 `__Host-c207_session`이다.
    위 속성 셋(Secure·Path=/·Domain 없음)이 그 접두의 필수 조건이라 여기서 이미 맞는다.
    """
    settings = get_settings()
    response.set_cookie(
        key=auth.session_cookie_name(),
        value=token,
        max_age=int(settings.session_absolute_ttl_sec),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    """쿠키를 만료시킨다. **심을 때와 같은 속성**을 줘야 브라우저가 같은 쿠키로 알아본다.

    ⚠ 지금 이름만 지우면 롤아웃 중에 옛 이름 쿠키가 브라우저에 남아, 로그아웃한 사람이
    다음 요청에 그 토큰을 다시 실어 보낸다(세션은 이미 끊겨서 401이지만 화면이 로그인
    상태로 착각한다). 그래서 **두 이름을 다 지운다.**
    """
    settings = get_settings()
    for name in (auth.SESSION_COOKIE_NAME, auth.SESSION_COOKIE_NAME_SECURE):
        response.delete_cookie(
            key=name,
            httponly=True,
            # `__Host-` 접두 쿠키는 Secure가 아니면 브라우저가 아예 안 받는다 — 지우는
            # 응답도 마찬가지라, 그 이름에는 설정과 무관하게 Secure를 붙인다.
            secure=settings.session_cookie_secure or name == auth.SESSION_COOKIE_NAME_SECURE,
            samesite="lax",
            path="/",
        )


@router.post("/login", response_model=AuthUserOut)
async def login(
    body: LoginIn,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> AuthUserOut:
    """아이디·비밀번호 → 세션 쿠키.

    실패 갈래가 셋인데 **밖에서 보이는 모습은 둘**이다(429 잠금, 401 나머지 전부).
    아이디가 없든 비밀번호가 틀렸든 계정이 해제됐든 같은 401·같은 문장이다.

    아이디가 없을 때도 더미 해시를 한 번 검증하고 간다 — 안 그러면 응답 시간이 "그 아이디는
    있다"를 알려 준다(§2.6).

    ⭐ 잠금 검사는 **진행 중 시도까지 같이 본다**(`login_attempt_blocked`). 검사와
    `record_login_failure` 사이에 scrypt 한 번이 놓여서, 그 창에 병렬로 밀어 넣은 요청은
    예전 검사로는 전부 통과했다 — 5회 잠금이 한 번에 N회 추측을 허용하던 자리다(3차 X77).
    검사 **직후** `login_attempt`를 여는 순서가 계약이다. 그 사이에 await을 끼우면 두 요청이
    같은 자리를 잡아 원자성이 깨진다(워커 1개·이벤트 루프 하나 전제).
    """
    username = auth.normalize_username(body.username)

    remaining = auth.login_attempt_blocked(username)
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_LOCKED,
            headers={"Retry-After": str(max(1, int(remaining) + 1))},
        )

    with auth.login_attempt(username):
        user = (
            await db.execute(select(AppUser).where(AppUser.username == username))
        ).scalars().first()

        if user is None:
            await auth.verify_password_async(
                body.password, await auth.dummy_password_hash_async()
            )
            auth.record_login_failure(username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CREDENTIALS
            )

        if not await auth.verify_password_async(body.password, user.password_hash):
            auth.record_login_failure(username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CREDENTIALS
            )

        if not user.is_active:
            # 해제된 계정. 여기서도 **실패로 센다**(2026-08-02 백지 검토 F14).
            #
            # 안 세면 이 갈래가 타이밍이 아니라 **상태 코드**로 답을 흘린다. 비밀번호가 틀리면
            # 세고 맞으면 안 세니까, 공격자가 후보 하나를 넣고 다음 요청이 429로 갈리는지만
            # 보면 그 후보의 정오를 그대로 읽는다 — 해제된 계정의 비밀번호가 통째로 새고,
            # 그 사람을 다시 활성화하는 순간 열린다.
            #
            # 예전 주석이 걱정한 "해제된 계정으로 그 아이디를 영구 잠금"은 성립하지 않는다.
            # 잠금은 60초짜리고 로그인이 성공하면 풀리는데, 해제된 계정은 애초에 못 들어온다.
            # 응답 코드·본문은 그대로 401·같은 문장이다.
            auth.record_login_failure(username)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_CREDENTIALS
            )

        auth.clear_login_failures(username)
        # 만료 뒤 보관 기간이 지난 행 청소. 별도 크론 없이 여기서 한 번 훑는다(§1.4 간소화).
        await auth.purge_expired_sessions(db)
        token = await auth.create_session(
            db, user, user_agent=request.headers.get("user-agent")
        )
        _set_session_cookie(response, token)
        return AuthUserOut(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
        )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """로그아웃. 세션이 없어도 204다 — 멱등이라 화면이 상태를 몰라도 그냥 부르면 된다.

    쿠키 만료는 세션 무효화가 실패해도 **반드시** 나가야 한다. 브라우저에 죽은 토큰이
    남으면 화면이 계속 로그인 상태로 착각한다.

    응답 객체를 주입받지 않고 직접 만들어 돌려준다 — 204는 본문이 없는 응답이라, 주입받은
    응답에 쿠키만 얹고 None을 돌려주면 헤더가 어디로 합쳐지는지가 프레임워크 판본에 달린다.

    ⭐ **만료 응답을 먼저 만들고 무효화를 감싼다**(2026-08-02 백지 검토 3차 X18). 위 계약을
    적어 놓고도 본문은 직선이라, `revoke_session`(자기 커밋이다)이 DB 흔들림에 터지면 그
    자리에서 500이 나가고 만료 쿠키가 안 실렸다. 브라우저에 죽은 토큰이 남으면 화면이 계속
    로그인 상태로 착각한다 — 세션 행은 어차피 절대 만료로 죽으니, 둘 중 반드시 나가야 하는
    쪽은 쿠키 만료다.
    """
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _clear_session_cookie(response)

    token = auth.read_session_token(request.cookies)
    if token:
        try:
            await auth.revoke_session(db, token)
        except Exception:
            # 여기서 갈래를 안 가리는 이유 — 무엇이 터졌든 사용자한테 줄 답은 하나다(204 +
            # 만료 쿠키). 삼킨 사실은 로그에 통째로 남긴다. 토큰 원문은 안 찍는다.
            logger.exception("로그아웃 세션 무효화가 실패했다 — 쿠키 만료는 그대로 내보낸다")
    return response


@router.get("/me", response_model=AuthUserOut)
async def me(request: Request, db: AsyncSession = Depends(get_db)) -> AuthUserOut:
    """지금 누구인가. 화면이 뜰 때 한 번 불러 로그인 상태와 역할을 받는다.

    역할을 쿠키나 localStorage에 안 담는 이유가 이 창구다 — 담으면 화면이 고친 값으로
    탭이 열린다(§7.1).

    ⚠ 플래그가 꺼져 있어도 세션이 없으면 401이다. 이 창구의 뜻이 "지금 로그인돼 있나"라서,
    플래그를 봐서 200을 주면 화면이 "누군지 모르는데 로그인됨"을 받는다.
    """
    actor = await auth.resolve_request_actor(request, db)
    if actor is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_NEED_LOGIN
        )
    return AuthUserOut(
        id=actor.id,
        username=actor.username,
        display_name=actor.display_name,
        role=actor.role,
    )


_BAD_CURRENT_PASSWORD = "현재 비밀번호가 맞지 않습니다."


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_own_password(
    body: OwnPasswordChangeIn,
    request: Request,
    actor: auth.AuthActor | None = Depends(auth.require_trainee_always),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """본인 비밀번호 변경(2026-08-08 설정 창). 급과 무관하게 **로그인만 돼 있으면** 쓴다.

    관리자 재설정(`POST /api/users/{id}/password`)과 갈래가 다르다 — 그쪽은 지금 비밀번호를
    안 묻는 "잊었을 때" 창구라 아랫급 전용이고, 여긴 **지금 비밀번호를 확인**하는 본인
    전용이다. 세션 쿠키를 훔친 쪽이 비밀번호까지 갈아 계정을 영구히 가져가는 길을 지금
    비밀번호 확인이 막는다.

    ⭐ **다른 기기 세션만 끊고 이 요청을 실어 온 세션은 남긴다.** "샜을지 모른다"라서
    바꾸는 건 같은데, 바꾼 본인이 그 자리에서 쫓겨나면 설정 창이 로그인 화면으로 떨어진다 —
    본인 확인은 방금 지금 비밀번호로 했다.

    ⭐ 틀린 지금 비밀번호는 **로그인 잠금과 같은 계좌로 센다**(record_login_failure).
    안 세면 세션을 훔친 쪽이 이 창구로 지금 비밀번호를 무한정 추측한다 — 로그인 창구를
    5회 잠금으로 막아 놓고 옆문을 열어 두는 셈이다. 같은 이유로 잠겨 있으면 429다.
    ⛔ **잠금 검사 직후 `login_attempt`로 자리를 잡는다**(2026-08-08 2차 교차 검증). 로그인
    창구가 못박은 계약이다 — 검사와 `record_login_failure` 사이에 scrypt 한 번(verify)이
    놓여서, 그 창에 병렬로 밀어 넣은 요청은 전부 옛 검사(inflight=0)를 통과한다. 5회 상한에
    대고 N회 추측이 나가는 X77 병렬 우회가 이 창구에도 그대로 열려 있었다. 검사 **직후**
    자리를 여는 순서가 계약이고, 그 사이에 await을 끼우면 원자성이 깨진다(워커 1개 전제).

    해시 교체·감사 행·세션 무효화가 **한 커밋**이다(관리자 재설정의 F19와 같은 계약).
    비밀번호는 원문도 해시도 로그·감사 어디에도 안 싣는다.
    """
    remaining = auth.login_attempt_blocked(actor.username)
    if remaining > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=_LOCKED,
            headers={"Retry-After": str(max(1, int(remaining) + 1))},
        )

    with auth.login_attempt(actor.username):
        user = await db.get(AppUser, actor.id)
        if user is None or not user.is_active:
            # 세션은 살았는데 계정이 사라졌거나 해제된 갈래 — resolve가 매 요청 계정을 같이
            # 보므로 사실상 안 오는 값인데, 오면 로그인부터 다시다.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail=_NEED_LOGIN
            )

        if not await auth.verify_password_async(
            body.current_password, user.password_hash
        ):
            auth.record_login_failure(actor.username)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=_BAD_CURRENT_PASSWORD
            )
        auth.clear_login_failures(actor.username)

        before = account_snapshot(user)
        user.password_hash = await auth.hash_password_async(body.new_password)
        add_audit(
            db,
            staff_id=ACCOUNT_AUDIT_STAFF_ID,
            action=ACTION_UPDATE,
            actor=actor,
            before=before,
            # 관리자 재설정과 갈라 읽히게 own 표시를 따로 둔다 — 감사 표에서 "누가 남의 것을
            # 갈았나"와 "본인이 바꿨나"는 무게가 다르다.
            after={**account_snapshot(user), "password_change_own": True},
        )
        await db.flush()
        token = auth.read_session_token(request.cookies)
    await auth.revoke_user_sessions(
        db,
        user.id,
        commit=False,
        except_token_hash=auth.token_digest(token) if token else None,
    )
    await db.commit()

    logger.info("계정 %s가 자기 비밀번호를 바꿨다 — 다른 세션은 끊었다", user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
