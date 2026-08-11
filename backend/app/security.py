"""인입 API·WebSocket 공용 인증.

키는 두 갈래다. **기기 키**(`API_KEY`)는 젯슨·라파이·인입 HTTP가 쓰고, **대시보드 키**
(`DASHBOARD_API_KEY`)는 브라우저 화면이 쓴다. 두 값을 나눈 이유는 카드 원문(S15P11C207-238)
그대로다 — "브라우저 화면이 기기 키를 들면 개발자 도구에서 그대로 보여서 실질 인증이 되지
않습니다. 기기용 키와 사람용 인증을 갈라야 합니다." 그래서 대시보드 채널은 기기 키를 **안
받는다**. 화면 키가 새더라도 인입·명령 창구까지 같이 열리지는 않게 하는 게 목적이다.

⚠ 대시보드 키는 "사람용 인증"이 아니라 그 자리를 메우는 임시 자물쇠다. 브라우저가 들고 있는
값은 무엇이든 개발자 도구에 보인다 — 진짜 답은 세션·역할 구분(M-6)이고, 그건 팀 결정 자리다.

WebSocket은 붙는 쪽마다 실을 수 있는 자리가 달라서 세 창구를 **동급**으로 받는다 —
X-API-Key 헤더 / `Authorization: Bearer <token>` / `?api_key=` 쿼리. 젯슨 web_bridge는
Bearer로 실어 보내고(하드웨어 쪽 인터페이스 v2 계약), 브라우저 WebSocket API는 핸드셰이크에
임의 헤더를 못 실어 쿼리 말고 길이 없다.

"동급"을 말로만 두면 조용히 갈라진다. 실측으로 잡힌 갈라짐 둘을 여기서 막는다.
  1. **인코딩** — 헤더는 starlette이 latin-1로 디코드해 올리고 쿼리는 UTF-8로 올라온다.
     한글 키를 전선에 UTF-8로 실으면 헤더 쪽 str이 모지바케라, 그대로 UTF-8로 인코드하면
     전선 바이트와 다른 값이 된다. 실 uvicorn 실측으로 `API_KEY=열쇠키한글`이면 쿼리만
     통과하고 헤더 둘은 잘렸다 — 젯슨이 통째로 잠긴다. `_wire_str`이 전선 바이트를 되살린다.
  2. **둘레 공백** — HTTP 헤더 값은 파서가 앞뒤 공백을 떼고 올린다. 쿼리는 안 뗀다. 그래서
     `API_KEY=" sp key "`면 쿼리만 통과했다. 양쪽을 다 strip해서 세 창구를 같게 맞춘다.

값 대조는 hmac.compare_digest로 한다. 다만 compare_digest는 **str을 받으면 두 값이 다
ASCII일 때만** 동작하고 아니면 TypeError를 던진다 — 비ASCII 키를 실은 접속 하나가 500을
내며 핸드셰이크 경로를 터뜨린다. 그래서 대조 전에 양쪽을 바이트로 바꾼다.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import Awaitable, Callable
from typing import Literal
from urllib.parse import urlsplit

from fastapi import Depends, Header, HTTPException, Request, WebSocket, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from app import auth
from app.config import INSECURE_DEFAULT_API_KEY, get_settings
from app.db import get_db, get_session

logger = logging.getLogger("c207.security")

# WS 인증 갈래는 채널 이름 하나로만 갈린다. 갈래를 늘릴 땐 여기와 _ws_auth_required,
# _ws_expected_key 셋을 같이 고친다(계약 목록이 한 자리에 모여 있어야 한 곳만 고쳐지지 않는다).
WsChannel = Literal["robot", "dashboard"]


def _wire_str(value: str | None) -> str | None:
    """헤더 값에서 전선 바이트를 되살려 UTF-8로 다시 읽는다.

    starlette은 헤더 바이트를 latin-1로 디코드해 올린다(바이트 하나 = 글자 하나). 그래서
    UTF-8로 실린 한글 키가 여기서는 모지바케 str이다. latin-1로 되감아 원래 바이트를 얻고
    UTF-8로 다시 읽으면 쿼리 쪽 값과 같은 문자열이 된다.

    UTF-8이 아닌 바이트가 와도 surrogateescape로 흘려보낸다 — 그 조합은 `_as_bytes`가 다시
    같은 바이트로 되돌려 놓아서, 대조가 전선 바이트 그대로 이뤄진다.
    """
    if value is None:
        return None
    try:
        raw = value.encode("latin-1")
    except UnicodeEncodeError:
        # latin-1 밖 글자 = 전선을 안 탄 값(ASGI scope에 직접 넣은 시험 등). 그대로 쓴다.
        return value
    return raw.decode("utf-8", errors="surrogateescape")


def _as_bytes(value: str) -> bytes:
    """대조용 바이트. 어떤 문자가 와도 예외를 안 낸다.

    surrogateescape를 쓰는 이유는 `_wire_str`이 흘려보낸 비UTF-8 바이트를 원래 값 그대로
    되돌리기 위해서다 — 'replace'로 접으면 서로 다른 키가 같은 '?'로 뭉쳐 대조가 헐거워진다.
    """
    return value.encode("utf-8", errors="surrogateescape")


def _normalize(value: str) -> str:
    """둘레 공백을 뗀 대조용 값. 창구 셋을 같은 잣대로 맞추는 자리다.

    헤더 파서가 이미 떼는 공백을 쿼리·기대값 쪽에서도 떼서, 같은 키가 창구마다 다른 결과를
    내지 않게 한다. 키 안쪽 공백은 안 건드린다.
    """
    return value.strip()


def keys_match(supplied: str | None, expected: str) -> bool:
    """키가 맞나. 길이가 달라도, 비ASCII가 와도 예외 없이 False로 떨어진다.

    빈 값은 곧장 False다 — API_KEY를 비워 둔 배포에서 키를 안 실은 접속이 통과하는 게
    제일 나쁜 경우라, 기대값이 비면 아무도 못 들어오는 쪽으로 닫는다.
    """
    if not supplied or not expected:
        return False
    left, right = _normalize(supplied), _normalize(expected)
    if not left or not right:
        return False
    return hmac.compare_digest(_as_bytes(left), _as_bytes(right))


def secret_is_usable(expected: str, label: str) -> bool:
    """이 기대값이 실제로 자물쇠 구실을 하나.

    두 가지를 닫는다.
      - 빈 값. 안 넣고 스위치만 켠 배포다.
      - 저장소에 그대로 적혀 있는 기본키(`dev-local-key`). 값이 공개돼 있으니 켜 봐야
        보안극장이다 — 실측으로 API_KEY를 안 넣고 플래그만 켜면 그 기본키로 통과했다.
    둘 다 "닫고 크게 남긴다". 조용히 열어 두면 켠 사람이 잠긴 줄 안다.
    """
    key = _normalize(expected or "")
    if not key:
        logger.error("%s가 비어 있다 — 인증을 켠 채 키가 없어 접속을 전부 닫는다.", label)
        return False
    if key == INSECURE_DEFAULT_API_KEY:
        logger.error(
            "%s가 저장소 기본값(%r) 그대로다 — 공개된 값이라 자물쇠가 아니다. 접속을 닫는다.",
            label,
            INSECURE_DEFAULT_API_KEY,
        )
        return False
    return True


def device_key_ok(x_api_key: str | None, *, require_usable_secret: bool = False) -> bool:
    """이 헤더 값이 기기 키(`API_KEY`)와 맞나. **판정은 여기 한 자리다.**

    `require_api_key`가 그대로 이 함수를 쓰고, 401을 안 던지고 bool만 필요한 자리
    (`key_gate_or`의 기기 키 폴백)도 같은 함수를 부른다. 판정을 두 벌 적으면 한쪽만
    고쳐지고 그 한쪽이 곧 인증이 새는 구멍이다 — 이 파일이 이미 두 번 겪은 자리다
    (모듈 머리 "동급"·`routers/dispatch.require_dispatch_key` 주석).

    ⭐ **`require_usable_secret`은 대조가 아니라 기대값 쪽 잣대다.** 켜면 대조에 앞서
    `secret_is_usable`을 먼저 본다(빈 값·저장소 기본키 `dev-local-key` 거절). 대조 자체는
    아래 `keys_match` 한 줄 그대로라 이 인자를 더해도 판정이 두 벌이 되지 않는다.

    부르는 자리 둘의 잣대가 **일부러 다르다.**
      - 인입 게이트(`require_api_key`, 기본값 False)는 기본키를 그대로 받는다. 로컬 개발이
        `.env.example` 값으로 도는 자리라 막으면 개발이 선다(그 함수 docstring). 배포에서
        덮어쓰는 규칙은 문서 몫이다.
      - 로그인을 켠 뒤의 기기 키 폴백(`key_gate_or`, True)은 안 받는다. 그 갈래는 세션
        게이트를 **건너뛰는** 자리라, 저장소에 그대로 적혀 있는 값으로 로봇 복귀가 열리면
        로그인을 켠 뜻이 이 창구에서 통째로 사라진다. 같은 겹을 WS 로봇 채널이 이미 쓴다
        (`ws_expected_key` — 그쪽도 `secret_is_usable`을 앞세운다).

    ⚠ 새로 부르는 자리를 만들면 이 인자를 반드시 정하고 넘긴다. 기본값이 옛 거동(False)이라
    안 적으면 기본키가 통과한다 — 세션·로그인을 건너뛰는 갈래면 True다.
    """
    expected = get_settings().api_key
    if require_usable_secret and not secret_is_usable(expected, "기기 키 폴백 API_KEY"):
        return False
    return keys_match(_wire_str(x_api_key), expected)


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """인입·상태변경 HTTP 창구 공용 게이트(기기 키).

    ⚠ 예전에는 `x_api_key != settings.api_key` 한 줄이었다. 그 모습은 `API_KEY=""`로 뜬
    배포에서 **빈 `X-API-Key:` 헤더가 통과**했다(실 uvicorn 실측 — 헤더 없으면 401인데 빈
    헤더는 422까지 들어갔다). keys_match는 기대값이 비면 아무도 통과 못 시킨다.
    상수시간 대조라 키 값이 응답 시간으로 새지도 않는다.

    여기서는 기본키(dev-local-key)를 막지 않는다 — 인입 창구는 이 카드 밖이고, 로컬 개발이
    .env.example 값 그대로 도는 자리라 막으면 개발이 선다. 배포에서 반드시 덮어쓰는 규칙은
    문서(backend/README.md)에 있다.
    """
    if not device_key_ok(x_api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="유효한 X-API-Key 헤더가 필요합니다.",
        )


def bearer_token(authorization: str | None) -> str | None:
    """`Authorization: Bearer <token>`에서 토큰만. 형식이 아니면 None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = token.strip()
    return token or None


def ws_supplied_keys(websocket: WebSocket) -> list[str]:
    """이 핸드셰이크가 실어 온 키 후보 전부. 셋 다 같은 자리에 실린 동급 창구다.

    "먼저 찾은 하나"가 아니라 전부 모아서 하나라도 맞으면 통과시킨다. 프록시가 헤더를
    덧붙이거나 클라이언트가 두 자리에 다 실었을 때, 먼저 잡힌 자리가 빈 값이라는 이유로
    정상 접속이 잘리는 걸 막는다.

    헤더 둘은 `_wire_str`을 지나 쿼리와 같은 문자열 모습이 된다.

    ⚠ **쿼리 창구는 값이 nginx 액세스 로그에 통째로 남는다.** 없애지 않는 이유는 브라우저
    WebSocket API가 핸드셰이크에 임의 헤더를 못 실어서, 빼면 화면이 키 인증 구간에서
    통째로 막히기 때문이다(모듈 머리). 없애는 건 로그인 롤아웃 5단계가 한다 —
    `AUTH_REQUIRE_LOGIN`을 켜면 대시보드 판정이 쿠키로 갈아타서(`ws_dashboard_allowed`)
    이 함수를 아예 안 지난다. 그때까지는 이 창구가 로그에 남는 걸 아는 채로 둔다.
    """
    candidates = (
        _wire_str(websocket.headers.get("x-api-key")),
        bearer_token(_wire_str(websocket.headers.get("authorization"))),
        websocket.query_params.get("api_key"),
    )
    return [value for value in candidates if value]


def _ws_auth_required(channel: str) -> bool:
    """이 채널이 키를 요구하나. 모르는 채널이면 닫는 쪽(True)으로 간다."""
    settings = get_settings()
    if channel == "robot":
        return settings.ws_auth_required_robot
    if channel == "dashboard":
        return settings.ws_auth_required_dashboard
    logger.error("알 수 없는 WS 채널 이름 %r — 인증을 요구하는 쪽으로 닫는다", channel)
    return True


def ws_expected_key(channel: str) -> str | None:
    """이 채널이 받아 주는 키. 쓸 수 없는 설정이면 None(= 전부 닫음)이다.

    채널마다 기대값이 **다르다**. 로봇 채널은 기기 키(API_KEY), 대시보드는 대시보드 키
    (DASHBOARD_API_KEY)다. 대시보드에서 기기 키를 안 받는 게 이 카드의 핵심이라, 여기서
    fallback으로 API_KEY를 끌어다 쓰면 안 된다 — 그러면 갈라놓은 게 도로 합쳐진다.
    """
    settings = get_settings()
    if channel == "robot":
        return settings.api_key if secret_is_usable(settings.api_key, "로봇 채널 키 API_KEY") else None
    if channel == "dashboard":
        return (
            settings.dashboard_api_key
            if secret_is_usable(settings.dashboard_api_key, "대시보드 채널 키 DASHBOARD_API_KEY")
            else None
        )
    logger.error("알 수 없는 WS 채널 이름 %r — 기대 키가 없어 닫는다", channel)
    return None


async def ws_api_key_ok(websocket: WebSocket, channel: WsChannel) -> bool:
    """WebSocket 핸드셰이크가 키를 통과했나. 그 채널 플래그가 꺼져 있으면 늘 True.

    채널별로 갈리는 건 "요구하느냐"와 "어느 키를 받느냐" 둘이고, 키를 어느 창구로 받고
    어떻게 대조하는지는 두 채널이 똑같다.

    대시보드가 쿼리로 키를 보내면 nginx 액세스 로그에 통째로 남는다는 점은 그대로다 —
    화면 경계의 최종 답은 프록시 인증이나 세션 쿠키다(M-6).
    """
    if not _ws_auth_required(channel):
        return True
    expected = ws_expected_key(channel)
    if expected is None:
        return False
    return any(keys_match(supplied, expected) for supplied in ws_supplied_keys(websocket))


# ── 로그인 이관 게이트 (로그인 설계 확정본 2026-08-01 §3·§5.1) ───────────────
#
# ⭐ 제1 불변 — `AUTH_REQUIRE_LOGIN=false`인 동안 **옛 경로가 글자 그대로 산다.** 아래 둘은
# 갈아 끼우는 게 아니라 갈림길이다. 꺼져 있으면 예전 키 게이트를 그대로 부르고, 켜져 있을
# 때만 세션 게이트로 넘어간다. off 구간을 무인증으로 여는 갈래는 여기에 없다(설계 P1 반영).
#
# 갈림길을 **한 함수에 모은 이유**가 있다. 창구가 서른 개 넘고 query·assistant·dispatch
# 셋이 같은 갈림을 탄다 — 파일마다 `if get_settings().auth_require_login:`을 흩으면 창구가
# 늘 때 한 자리만 고쳐지고, 그 한 자리가 곧 인증이 새는 구멍이다.


def key_gate_or(
    session_gate: Callable[[Request, AsyncSession], Awaitable[object]],
    legacy_key_gate: Callable[[str | None], Awaitable[object]],
    *,
    device_key_fallback: bool = False,
):
    """플래그가 켜지면 `session_gate`, 꺼져 있으면 `legacy_key_gate`를 태우는 의존성을 만든다.

    `legacy_key_gate`는 `X-API-Key` 헤더 하나만 받는 기존 게이트다(`require_api_key`·
    `require_dispatch_key`). 헤더 파라미터를 여기서 선언하니 플래그와 무관하게 OpenAPI
    모양이 예전 그대로 남는다 — 옛 클라이언트가 보는 계약이 안 흔들린다.

    돌려주는 값은 켜졌을 때만 `AuthActor`이고 꺼졌을 땐 None이다. `dependencies=[...]`로
    걸면 값이 버려지고, 창구가 행위자를 알아야 하면 인자로 받는다(`actor=Depends(...)`).
    두 경우 다 "None일 수 있다"를 창구가 그대로 다루면 된다.

    ## `device_key_fallback` — 기기가 부르는 창구 전용 갈래 (2026-08-04 사용자 확정)

    켠 뒤에도 **유효한 기기 키면 통과**시킨다. 세션을 못 쥐는 기기가 부르는 창구가 그
    자리다 — 라파이 게이트 단말의 복귀 요청(`POST /api/dispatch/commands/return`,
    `hardware/rpi/gate_server.py call_return_service`)이 지금 유일한 갈래다. 이 갈래가
    없으면 플래그를 켜는 순간 그 버튼이 401로 죽는다.

    ⭐ **`require_dispatch_key`가 아니라 `device_key_ok`를 본다.** 두 값이 다르다 —
    `require_dispatch_key`는 `DISPATCH_REQUIRE_API_KEY`가 꺼져 있으면(기본값) **아무것도
    검사하지 않고 통과**시킨다. 그걸 폴백으로 쓰면 "키를 계속 받는다"가 실제로는 "익명도
    통과"가 되어, 로그인을 켠 뜻이 이 창구에서만 통째로 사라진다. 여기서는 스위치와 무관하게
    키를 **실제로** 대조하고, 그래서 `API_KEY=""`인 배포에서는 아무도 못 지난다
    (`keys_match`가 기대값이 비면 False다).

    ⭐ **`require_usable_secret=True`로 부른다** — 빈 값만이 아니라 저장소 기본키
    (`dev-local-key`)도 거절한다. `.env.example`에 공개돼 있는 값이라, 그걸로 로봇 복귀가
    열리면 로그인을 켠 뜻이 그 자리에서 사라진다. 지금 EC2는 실키를 따로 넣어 뒀지만
    서버를 새로 세우거나 `backend.env`를 다시 만들면 기본값으로 돌아갈 수 있고, 같은 겹을
    WS 로봇 채널은 이미 쓴다(`ws_expected_key`). 인입 게이트(`require_api_key`)는 로컬
    개발 때문에 일부러 기본키를 받는 자리라 그대로 둔다 — 잣대가 갈리는 이유는
    `device_key_ok` docstring에 있다.

    ⚠ 키 갈래로 들어온 요청은 **누가 눌렀는지 모른다.** 공용 기기 키라 사람을 못 가리고,
    이 의존성이 돌려주는 값도 None이라 창구가 행위자를 적을 자리가 없다. 그 사실을 로그에
    남긴다 — 안 남기면 "세션 기록이 없는 명령"이 왜 생겼는지 나중에 못 짚는다.

    ⚠ 그 감사 줄을 **`logger.warning`으로 적는다.** `logger.info`면 운영에서 한 줄도 안
    남는다 — 앱이 `basicConfig`·`dictConfig`를 안 부르고 컨테이너도 `--log-level` 없이 떠서
    (`backend/Dockerfile` CMD), `c207.security`의 유효 레벨이 root 기본값 WARNING이다.
    핸들러가 0개라 WARNING부터는 `logging.lastResort`가 stderr로 흘리고 docker logs에
    남는다(2026-08-04 uvicorn `LOGGING_CONFIG`로 실측 — INFO는 안 나오고 WARNING만 났다).

    ⭐ 제1 불변 — 플래그가 꺼져 있으면 이 인자가 True여도 갈림이 **먼저** `legacy_key_gate`로
    빠진다. off 구간 거동은 한 글자도 안 바뀐다.
    """

    async def dependency(
        request: Request,
        x_api_key: str | None = Header(default=None),
        db: AsyncSession = Depends(get_db),
    ) -> object | None:
        if not get_settings().auth_require_login:
            return await legacy_key_gate(x_api_key)
        if device_key_fallback and device_key_ok(x_api_key, require_usable_secret=True):
            logger.warning(
                "기기 키로 %s %s를 통과시켰다 — 사람 행위자는 남지 않는다",
                request.method,
                request.url.path,
            )
            return None
        return await session_gate(request, db)

    return dependency


async def ws_dashboard_allowed(websocket: WebSocket, *, touch: bool = True) -> bool:
    """대시보드 핸드셰이크를 받아 줄까. 플래그가 갈림길이다(§5.1).

    켜지면 **세션 쿠키만** 본다. `DASHBOARD_API_KEY` 경로를 폴백으로 안 남기는 게 계약이다 —
    남기면 그게 세션을 우회하는 뒷문이고, 그 값은 어차피 쿼리로 실려 nginx 액세스 로그에
    통째로 남는다(모듈 머리 참고). 꺼져 있으면 예전 키 판정(기본 꺼짐) 그대로다.

    쿠키는 같은 오리진 WS 핸드셰이크에 브라우저가 자동으로 실어 준다 — 임의 헤더를 못 싣는
    WebSocket API 제약을 우회하려고 쿼리스트링 키를 쓰던 자리가 이걸로 사라진다.

    실패를 여기서 안 닫고 bool만 돌려주는 이유는 닫는 방식이 계약이라서다(accept 뒤
    close(1008)). main.py `reject_unauthorized` 주석에 실측 근거가 있다.

    ⭐ **오리진 판정이 플래그 갈림보다 앞이다.** WS에는 CORS가 없고 배포 도메인이 형제 팀과
    같은 site라, 남의 팀 페이지가 이 스트림을 구독하는 갈래는 `AUTH_REQUIRE_LOGIN`이 꺼져
    있는 지금 그대로 열려 있다 — 갈림 안쪽에 넣으면 켜기 전까지 아무것도 안 막는다.
    로봇 채널(`ws_api_key_ok`)은 안 건드린다. 젯슨은 Origin을 안 싣고, 싣는 갈래도 없다.

    ⭐ `touch=False`는 **사람이 안 보낸 조회**라는 표시다(`auth.resolve_session` 참고).
    핸드셰이크는 사람이 탭을 여는 순간이라 기본값 그대로 `last_seen_at`을 밀고, 배경 재검증
    (`_ws_dashboard_recheck_loop`)만 끄고 부른다.
    """
    if not ws_origin_allowed(websocket):
        return False
    if not get_settings().auth_require_login:
        return await ws_api_key_ok(websocket, "dashboard")
    # ⚠ 쿠키 이름을 직접 읽지 않는다. 배포(secure)는 `__Host-` 접두를 심고 로컬 개발은
    # 옛 이름을 심어서, 한쪽 이름만 보면 배포에서 대시보드가 통째로 거절된다.
    token = auth.read_session_token(websocket.cookies)
    if not token:
        return False
    async with get_session() as session:
        actor = await auth.resolve_session(session, token, touch=touch)
    if actor is None:
        return False
    return auth.RANK.get(actor.role, 0) >= auth.RANK[auth.ROLE_AGENT]


# ── 열린 대시보드 WS의 세션 재검증 (백지검토 F3 / 코덱스 X8) ──────────────────
#
# ⭐ **왜 필요한가.** 설계 §1.1이 토큰이 아니라 세션을 고른 유일한 근거가 "강등·해제가 다음
# 요청부터 먹힌다"인데, WS만 그 근거의 예외였다. 핸드셰이크에서 한 번 판정하고 그 뒤로는
# 다시 안 봐서, 해제·강등된 계정이 열어 둔 탭이 push를 계속 받았다. HTTP 창구는 요청마다
# `resolve_session`을 타서 저절로 닫히는데 WS는 요청이 한 번뿐이라 그 자리가 비어 있다.
#
# 재검증은 **핸드셰이크와 같은 함수**(`ws_dashboard_allowed`)를 다시 부른다. 판정을 두 벌
# 적으면 한쪽만 고쳐지고, 그 한쪽이 곧 인증이 새는 구멍이다(이 파일 갈림길 절과 같은 이유).

# 재검증 주기(초). 강등·해제를 열린 탭이 얼마나 늦게 알아채나가 이 값이다.
#
# 60초로 잡은 근거 — 세션 유휴 만료가 2시간(`SESSION_IDLE_TTL_SEC`)이라 분 단위면 그 계약
# 안에서 충분히 촘촘하고, 짧게 잡으면 열린 탭 수 × (1/주기)만큼 DB **조회**가 그대로 는다.
#
# ⚠ 이 주기가 유휴 만료를 끄지 않는 근거는 값이 아니라 `touch=False`다. 재검증이 세션을
# 볼 때 `last_seen_at`을 같이 밀면 관제 화면을 켜 둔 것만으로 유휴 만료가 영영 안 와서,
# 주기를 어떤 값으로 잡아도 § 1.4 계약이 WS에서만 깨진다(2026-08-02 2차 검토 G1).
# 그래서 아래 루프는 `ws_dashboard_allowed(..., touch=False)`로 부른다 — 조회만 늘고
# UPDATE·COMMIT은 안 붙는다.
#
# config.Settings에 안 넣은 이유는 ws.py `WS_SEND_TIMEOUT_SEC`과 같다 — 이번 사이클에 여러
# 조가 config.py를 같이 만져서 줄을 더하면 텍스트 충돌이 난다.
WS_SESSION_RECHECK_SEC = 60.0

# 1008 = policy violation. main.py `WS_UNAUTHORIZED_CODE`와 같은 값을 **일부러** 쓴다 —
# 화면(telemetry.html)이 1008이면 재접속을 멈추고 안내를 띄우게 돼 있어서(코덱스 X66),
# 핸드셰이크 거절과 세션 만료가 같은 갈래로 다뤄져야 로그인 안내가 뜬다. 여기서 값을
# 가져다 쓰지 않고 다시 적는 이유는 import 방향이다(main → security 한 방향).
WS_SESSION_REVOKED_CODE = 1008


async def _ws_dashboard_recheck_loop(websocket: WebSocket, interval_sec: float) -> None:
    """주기마다 세션을 다시 보고, 더는 유효하지 않으면 소켓을 1008로 닫는다.

    ⚠ **잠들고 나서 판정한다.** 핸드셰이크 직후에 한 번 더 재면 방금 통과한 판정을 그대로
    다시 도는 군더더기 DB 조회다.

    ⚠ **조회가 터지면 안 끊는다.** DB가 잠깐 흔들릴 때 열린 화면을 전부 떨어뜨리면 관제가
    통째로 눈을 잃는데, 그 순간은 어차피 앱 전체가 서 있는 시점이라 얻는 게 없다. 다음
    주기가 다시 잰다.

    ⭐ **`touch=False`로 본다.** 여기서 `last_seen_at`을 밀면 사람이 아무것도 안 해도 유휴
    만료(2시간)가 영영 안 온다 — 서버가 자기 배경 태스크를 사람의 활동으로 세는 셈이다.
    재검증은 "아직 유효한가"만 묻고 세션 수명은 안 건드린다(2026-08-02 2차 검토 G1).

    닫기는 `receive`를 돌고 있는 라우트 루프를 깨우는 방식이다 — close 프레임이 나가면
    그쪽 `receive_text`가 WebSocketDisconnect로 터져 `finally`가 정리를 마친다.
    """
    while True:
        await asyncio.sleep(interval_sec)
        try:
            allowed = await ws_dashboard_allowed(websocket, touch=False)
        except Exception:
            logger.exception("대시보드 WS 세션 재검증이 실패했다 — 연결은 그대로 둔다")
            continue
        if allowed:
            continue
        logger.info("대시보드 WS 세션이 더는 유효하지 않다 — %d로 닫는다", WS_SESSION_REVOKED_CODE)
        # 라우트 finally가 취소하는 순간과 겹치면 이미 닫힌 소켓일 수 있다. 어느 쪽이든
        # 더 할 게 없다(ws.py `_close_quietly`와 같은 판단).
        with contextlib.suppress(Exception):
            await websocket.close(code=WS_SESSION_REVOKED_CODE, reason="session_revoked")
        return


def start_ws_dashboard_recheck(
    websocket: WebSocket, *, interval_sec: float | None = None
) -> asyncio.Task | None:
    """재검증 태스크를 띄운다. **플래그가 꺼져 있으면 안 띄우고 None이다.**

    ⭐ 제1 불변 — `AUTH_REQUIRE_LOGIN=false` 구간에서는 태스크도, 주기 DB 조회도, 끊김도
    아예 없다. 꺼진 구간에는 판정할 세션이 없어서 재검증이 할 일도 없다.

    부르는 쪽이 태스크를 받아 `finally`에서 취소한다(main.py `ws_dashboard`).

    ⚠ 주기를 기본 인자로 안 굳힌다. 기본값 자리에 상수를 적으면 그 값이 **함수를 정의할 때**
    한 번 묶여서, 시험이 `WS_SESSION_RECHECK_SEC`를 갈아 끼워도 60초가 그대로 돈다.
    """
    if not get_settings().auth_require_login:
        return None
    if interval_sec is None:
        interval_sec = WS_SESSION_RECHECK_SEC
    return asyncio.create_task(_ws_dashboard_recheck_loop(websocket, interval_sec))


# ── 교차 오리진 상태변경 차단 (로그인 설계 확정본 §1.2 보강 한 겹) ────────────
#
# SameSite=Lax 쿠키가 이미 교차 사이트 요청에서 쿠키를 안 싣지만, 그 판정은 브라우저 몫이라
# 서버가 확인할 길이 없다. 여기서 한 겹을 더 건다 — **상태변경 메서드에 Origin이 실려 있고
# 그 값이 자기 오리진이 아니면 403**이다. 새 의존성은 0이고 판정 자리도 하나뿐이다.
#
# ⚠ **Origin이 없으면 통과한다.** 기기·서버가 부르는 갈래(젯슨 인입·curl·젠킨스)가 전부
# 여기고, 그쪽을 막으면 인입이 통째로 선다. 브라우저는 상태변경 요청에 Origin을 늘 싣기
# 때문에, "없으면 통과"가 브라우저 갈래를 여는 구멍이 되지 않는다.
#
# WebSocket 핸드셰이크는 **미들웨어가 안 탄다**(ASGI scope 타입으로 갈라 흘려보낸다). 대신
# 대시보드 채널이 아래 `ws_origin_allowed`로 **같은 판정 함수**(`origin_allowed`)를 부른다 —
# 계약을 두 벌 적으면 한쪽만 고쳐지므로, 미들웨어와 WS가 같은 함수를 보게 묶어 둔다.

# GET·HEAD·OPTIONS는 뺀다. 상태를 안 바꾸기 때문이다(부작용이 있는 GET 경로 몇은 예외다 —
# 바로 아래 `SIDE_EFFECT_GET_PATHS`).
#
# ⚠ OPTIONS 면제의 근거는 "프리플라이트를 자르면 화면이 막힌다"가 **아니다.** 이 앱에는
# CORSMiddleware가 아예 없어서(app/main.py) 교차 오리진 프리플라이트는 허용 헤더를 못 받고
# 브라우저가 그 자리에서 자른다 — 여기서 통과시켜도 본 요청이 나가지 못한다. 그래서 면제는
# 방어 구멍이 아니라 판정 밖이다(상태를 안 바꾸는 메서드라는 원래 잣대 그대로).
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# ⚠ **메서드만으로는 안 갈린다.** GET인데 실제로는 부작용이 있는 창구가 셋 있다
# (2026-08-02 백지 검토 3차 X10).
#   - `GET /api/dispatch/default-destination` — 그날 값이 없으면 **이때 바깥 날씨 서버를
#     타고 DB 행을 만든다**(lazy 갱신, routers/dispatch.py docstring 그대로).
#   - `GET /api/assistant/daily-summary`·`/alert-brief/{id}` — 팀 공용 GMS 크레딧을 태운다.
# 배포 도메인이 형제 팀과 같은 site라 남의 페이지가 이 둘을 부르게 만들 수 있어서, 판정을
# "상태를 안 바꾸는 메서드"가 아니라 **"부작용이 있느냐"**로 넓힌다. 판정 자리는 그대로
# 미들웨어 하나다.
#
# ⚠ 여기 적힌 건 경로 **정본이 아니라 사본**이다. 그 창구의 경로가 바뀌면 이 값도 같이
# 고쳐야 방어가 안 새고, 부작용 있는 GET 창구를 새로 만들면 여기 한 줄을 더한다.
SIDE_EFFECT_GET_PATHS = frozenset({"/api/dispatch/default-destination"})
SIDE_EFFECT_GET_PREFIXES = ("/api/assistant/",)

_ORIGIN_REJECTED = "허용되지 않은 요청 출처입니다."


def origin_guard_applies(method: str | None, path: str) -> bool:
    """이 요청을 오리진 가드가 봐야 하나. 상태변경 메서드거나 부작용 있는 GET이면 본다."""
    if method in STATE_CHANGING_METHODS:
        return True
    if method != "GET":
        return False
    trimmed = path.rstrip("/") or "/"
    return trimmed in SIDE_EFFECT_GET_PATHS or path.startswith(SIDE_EFFECT_GET_PREFIXES)


def _normalize_origin(value: str) -> str:
    """대조용 오리진 한 모양. 둘레 공백·끝 슬래시를 떼고 대소문자를 접는다.

    스킴과 호스트는 규격상 대소문자를 안 가린다. 포트는 붙은 그대로 본다 —
    `http://a.com`과 `http://a.com:8000`은 브라우저가 다른 오리진으로 친다.
    """
    return value.strip().rstrip("/").lower()


def allowed_origins() -> set[str]:
    """설정에 적어 둔 **추가** 허용 오리진. 비어 있으면 자기 오리진 + 로컬 오리진뿐이다.

    배포는 SPA와 API가 같은 오리진이라(nginx 실측 — §1.2) 아무것도 안 적어도 돈다. 로컬
    개발(정적 서버를 몇 번 포트로 띄우든)도 `origin_allowed`가 로컬을 기본 허용해서 안 적어도
    된다 — 이 값이 필요한 건 로컬도 자기 오리진도 아닌 자리에서 화면을 띄울 때뿐이다.
    """
    raw = get_settings().auth_allowed_origins
    return {_normalize_origin(v) for v in raw.split(",") if v.strip()}


def request_self_origin(headers: Headers, scheme: str) -> str | None:
    """이 요청이 도착한 자리의 오리진. Host 헤더가 없으면 None이다.

    ⚠ 스킴은 `X-Forwarded-Proto`를 **먼저** 본다. nginx가 443에서 받아 컨테이너로는 http로
    넘기므로(deploy/nginx-c207-api.conf), ASGI scope의 스킴만 보면 자기 오리진이 늘
    `http://...`로 계산돼 브라우저가 보낸 `https://...`와 어긋난다 — 배포된 화면의 모든
    상태변경 요청이 403이 되는 자리다. nginx가 그 헤더를 프록시 location 아홉 곳 전부에
    싣는 걸 확인했다(그 파일의 `proxy_pass` 자리 수).

    ⚠ Host 쪽에도 같은 계열의 함정이 있다. nginx가 넘기는 `proxy_set_header Host $host`의
    `$host`는 **포트를 뗀 값**이라, 443·80처럼 브라우저 Origin에도 포트가 안 붙는 지금
    배포에서는 두 값이 맞는다. 하지만 포트가 붙는 창구(예: `https://api.example:8443`)를
    열면 자기 오리진이 `https://api.example`로 계산돼 브라우저가 보낸 값과 **전량** 어긋난다
    — 그때는 nginx 쪽을 `$http_host`(포트가 붙은 원본 Host)로 바꿔야 한다.
    """
    host = headers.get("host")
    if not host:
        return None
    forwarded = headers.get("x-forwarded-proto")
    # 프록시가 여럿이면 `https, http`처럼 쌓인다. 맨 앞이 바깥에서 들어온 스킴이다.
    if forwarded:
        scheme = forwarded.split(",")[0].strip() or scheme
    return _normalize_origin(f"{scheme}://{host}")


# 기본으로 열어 두는 로컬 호스트 이름. 포트도 스킴도 안 가린다.
_LOCAL_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1"})


def _is_local_origin(value: str) -> bool:
    """이 오리진의 호스트가 로컬 루프백인가(포트 무관, http·https 무관).

    호스트만 본다 — `urlsplit(...).port`는 `http://localhost:abc` 같은 값에서 ValueError를
    던지고, 판정에 포트가 필요 없으니 안 읽는다.

    ⚠ 그런데 `.hostname`을 안 읽어도 안전하지는 **않다**. `urlsplit("http://[")`는 파싱
    단계에서 `ValueError: Invalid IPv6 URL`을 던진다(실측). Origin 헤더 값은 아무나 넣는
    문자열이라, 안 잡으면 그 한 줄이 미들웨어를 뚫고 올라가 500이 된다 — 가드가 막아야 할
    요청이 오히려 서버 오류를 내는 자리다(2026-08-02 백지 검토 3차 X11). 못 읽는 값은
    로컬이 아닌 쪽(False)으로 닫는다.
    """
    try:
        return urlsplit(value).hostname in _LOCAL_ORIGIN_HOSTS
    except ValueError:
        logger.warning("오리진 문자열을 파싱하지 못했다 — 로컬이 아닌 쪽으로 닫는다: %r", value)
        return False


def origin_allowed(origin: str, self_origin: str | None) -> bool:
    """실려 온 Origin을 받아 줄까. 자기 오리진·로컬 오리진이거나 설정에 적힌 값이면 통과다.

    Host를 못 읽어 자기 오리진을 모르면(HTTP/1.0 등) **닫는 쪽**으로 간다 — 그때는 로컬
    오리진과 설정에 적힌 값만 통과한다. 모르는 상태에서 여는 쪽으로 가면 이 방어가 뜻을 잃는다.

    ⭐ **로컬 오리진(localhost·127.0.0.1)은 포트·스킴과 무관하게 기본 허용이다.** 안 그러면
    로컬 개발이 통째로 선다 — 화면을 로컬 서버로 띄우면 Origin 이 그 서버 주소
    (`http://localhost:<포트>`)로 실리는데 요청은 백엔드로 가서,
    자기 오리진 대조가 **모든 상태변경 요청**을 403으로 떨어뜨린다.

    이걸 열어도 이 가드가 막으려던 것은 그대로 막힌다. 이 가드의 방어 대상은 "사용자가 연
    남의 웹페이지가 브라우저를 시켜 우리 서버에 쓰기를 보내는 것"인데, 그 공격 페이지의
    오리진은 공격자 도메인이지 localhost일 수 없다. 반대로 사용자 기기 안에서 도는 악성
    프로세스는 브라우저를 안 거치고 직접 부르면 되고(Origin을 아예 안 실으면 그만이다),
    그 갈래는 애초에 이 가드가 막을 수 있는 자리가 아니다.

    ⚠ `Origin: null`(문자열)은 여기서 안 걸러져 **403이 된다.** 샌드박스 iframe·`data:`
    문서·일부 리다이렉트가 그 값을 싣는데, 호스트가 없어 자기 오리진과도 로컬과도 안 맞기
    때문이다. 그게 옳다 — "오리진을 잃은 문맥"이라 신뢰할 근거가 없고, 정말 열어야 하면
    `AUTH_ALLOWED_ORIGINS`에 적는 길이 남아 있다.
    """
    value = _normalize_origin(origin)
    if self_origin is not None and value == self_origin:
        return True
    if _is_local_origin(value):
        return True
    return value in allowed_origins()


# WS 스킴을 HTTP 스킴으로 되돌리는 표. 브라우저가 `wss://` 소켓에 싣는 Origin은 그 페이지가
# 뜬 자리라 `https://...`다 — scope 스킴(`wss`)을 그대로 쓰면 자기 오리진이 `wss://호스트`로
# 계산돼 **모든 핸드셰이크가 남으로 보인다.**
_WS_SCHEME_TO_HTTP = {"ws": "http", "wss": "https"}


def ws_origin_allowed(websocket: WebSocket) -> bool:
    """이 핸드셰이크의 Origin을 받아 줄까. HTTP 가드와 **같은 판정 함수**를 쓴다.

    WebSocket에는 CORS가 없다. 브라우저는 남의 페이지가 연 `new WebSocket(...)`도 그냥 붙여
    주고, 운영 환경에서는 대시보드와 API를 동일한 site 아래에 배치한다. 다른 도메인의
    주소는 오리진만 다르고 **같은 site**라 Lax 쿠키가 핸드셰이크에 그대로 실린다. 그래서
    쿠키 인증만으로는 남의 팀 페이지가 관제 이벤트 스트림을 통째로 구독할 수 있다.

    ⚠ **Origin이 없으면 통과한다.** 기기 갈래(젯슨 web_bridge·`websockets` 클라이언트·wscat)가
    전부 여기고, 그쪽을 막으면 로봇·시험이 통째로 선다. 브라우저는 WS 핸드셰이크에 Origin을
    늘 싣기 때문에 "없으면 통과"가 브라우저 갈래를 여는 구멍이 되지 않는다(HTTP 가드와 같은
    잣대다).

    자기 오리진 계산은 `request_self_origin`을 그대로 쓴다 — nginx가 `/ws/` location에도
    `X-Forwarded-Proto`를 싣는 걸 확인했다(deploy/nginx-c207-api.conf).
    """
    origin = websocket.headers.get("origin")
    if origin is None:
        return True
    scheme = _WS_SCHEME_TO_HTTP.get(websocket.scope.get("scheme", ""), "http")
    if origin_allowed(origin, request_self_origin(websocket.headers, scheme)):
        return True
    logger.warning("교차 오리진 WS 핸드셰이크를 막았다 — Origin %r, %s", origin, websocket.scope.get("path"))
    return False


class OriginGuardMiddleware:
    """상태를 바꾸는(또는 부작용이 있는) 요청에 실린 교차 오리진 Origin을 403으로 자르는
    ASGI 미들웨어(§1.2).

    무엇을 보나는 `origin_guard_applies` 한 함수가 정한다 — 상태변경 메서드 전부와, 부작용이
    있는 GET 경로 몇이다.

    `BaseHTTPMiddleware`가 아니라 맨 ASGI로 적은 이유 둘.
      ① `scope["type"]`을 직접 봐서 **WebSocket·lifespan을 손도 안 대고** 흘려보낸다.
      ② 본문을 안 읽으므로 인입 스트림·백그라운드 작업 흐름을 건드릴 여지가 없다.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not origin_guard_applies(
            scope.get("method"), scope.get("path", "")
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        origin = headers.get("origin")
        if origin is None or origin_allowed(
            origin, request_self_origin(headers, scope.get("scheme", "http"))
        ):
            await self.app(scope, receive, send)
            return

        # 오리진 값은 남긴다(공개 정보고, 막힌 이유를 못 짚으면 개발이 선다). 본문·쿠키는 안 찍는다.
        logger.warning(
            "교차 오리진 상태변경 요청을 막았다 — Origin %r, %s %s",
            origin,
            scope.get("method"),
            scope.get("path"),
        )
        await JSONResponse(status_code=status.HTTP_403_FORBIDDEN,
                           content={"detail": _ORIGIN_REJECTED})(scope, receive, send)
