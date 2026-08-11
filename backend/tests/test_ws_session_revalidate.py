"""열린 대시보드 WS가 주기마다 세션을 다시 보나 (백지검토 F3 / 코덱스 X8).

## 왜 이 파일이 있나

설계 §1.1이 토큰이 아니라 세션을 고른 유일한 근거가 "강등·해제가 **다음 요청부터** 먹힌다"인데,
WS만 그 근거의 예외였다. 요청이 핸드셰이크 한 번뿐이라, 해제·강등된 계정이 열어 둔 탭이 계속
push를 받았다. `app.security._ws_dashboard_recheck_loop`가 그 자리를 메운다.

## 왜 TestClient가 아니라 판정 함수를 직접 부르나

세션 조회가 DB를 타는데 TestClient는 자기 이벤트 루프를 따로 돌린다 — 거기서 DB를 타면
커넥션 풀이 두 루프에 걸쳐 갈라진다(`got Future attached to a different loop`,
test_auth_gates.py `_fake_ws` 주석의 실측). 그래서 시험 루프 안에서 루프 함수를 직접 돌리고,
소켓은 보낸 메시지를 받아 적는 최소 ASGI WebSocket으로 세운다.

⚠ 주기는 인자로 넣어 밀리초로 줄인다. 실제 기본값(60초)을 기다리면 시험이 선다.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import select, update
from starlette.websockets import WebSocket

from app import auth, security
from app.config import get_settings
from app.db import get_session
from app.models import AppUser, AuthSession
from app.security import (
    WS_SESSION_REVOKED_CODE,
    _ws_dashboard_recheck_loop,
    start_ws_dashboard_recheck,
    ws_dashboard_allowed,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PASSWORD = "recheck-pw-1234"
_TICK = 0.02


@pytest.fixture
def flag_on(monkeypatch):
    """`AUTH_REQUIRE_LOGIN=true` 구간. 설정이 lru_cache라 캐시를 앞뒤로 비운다."""
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


class _SpyWebSocket(WebSocket):
    """보낸 ASGI 메시지를 받아 적는 소켓. 재검증 경로는 close 하나만 쓴다."""

    def __init__(self, cookie: str | None) -> None:
        headers = []
        if cookie is not None:
            headers.append((b"cookie", cookie.encode("ascii")))
        self.sent: list[dict] = []

        async def _receive():  # pragma: no cover - 재검증 경로가 안 읽는다
            return {"type": "websocket.receive"}

        async def _send(message):
            self.sent.append(message)

        super().__init__(
            {
                "type": "websocket",
                "path": "/ws/dashboard",
                "headers": headers,
                "query_string": b"",
            },
            receive=_receive,
            send=_send,
        )

    @property
    def close_code(self) -> int | None:
        for message in self.sent:
            if message.get("type") == "websocket.close":
                return message.get("code")
        return None


async def _make_session(username: str, role: str) -> tuple[AppUser, str]:
    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password(_PASSWORD),
            display_name=f"이름-{username}",
            role=role,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    async with get_session() as session:
        token = await auth.create_session(session, user)
    return user, token


def _socket_for(token: str) -> _SpyWebSocket:
    return _SpyWebSocket(f"{auth.SESSION_COOKIE_NAME}={token}")


async def _last_seen_at(token: str) -> dt.datetime:
    async with get_session() as session:
        return (
            await session.execute(
                select(AuthSession.last_seen_at).where(
                    AuthSession.token_hash == auth.token_digest(token)
                )
            )
        ).scalar_one()


async def _rewind_last_seen(token: str, seconds: float) -> dt.datetime:
    """`last_seen_at`을 과거로 민다. 갱신 문턱(`SESSION_TOUCH_MIN_INTERVAL_SEC`=60초)을
    넘겨 놔야 touch 갈래가 실제로 열려서, "안 밀었다"가 문턱 덕이 아니라는 게 증명된다.
    """
    moved = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)
    async with get_session() as session:
        await session.execute(
            update(AuthSession)
            .where(AuthSession.token_hash == auth.token_digest(token))
            .values(last_seen_at=moved)
        )
        await session.commit()
    return await _last_seen_at(token)


async def _run_until_closed(sock: _SpyWebSocket, ticks: int = 40) -> None:
    """루프를 돌리다 소켓이 닫히면 멈춘다. 안 닫히면 취소하고 그대로 둔다."""
    task = asyncio.create_task(_ws_dashboard_recheck_loop(sock, _TICK))
    try:
        for _ in range(ticks):
            if sock.close_code is not None:
                return
            await asyncio.sleep(_TICK)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── 1. 제1 불변 — 플래그가 꺼져 있으면 아무 일도 안 일어난다 ────────────────


async def test_recheck_task_is_not_started_when_flag_off():
    """⭐ 꺼진 구간에는 태스크도 주기 DB 조회도 끊김도 없다.

    여기가 뒤집히면 롤아웃 1~4단계에서 아무 이유 없이 DB 조회가 탭 수만큼 늘고, 판정 실수
    하나가 곧 살아 있는 화면을 끊는다.
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    assert start_ws_dashboard_recheck(_SpyWebSocket(None)) is None


async def test_recheck_task_starts_when_flag_on(flag_on):
    """켠 구간에서는 태스크가 뜬다. 뜬 걸 확인했으면 바로 걷는다."""
    _, token = await _make_session("recheck-start", auth.ROLE_AGENT)
    task = start_ws_dashboard_recheck(_socket_for(token), interval_sec=_TICK)
    assert task is not None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── 2. 살아 있는 세션은 안 끊는다 ──────────────────────────────────────────


async def test_live_session_stays_open(flag_on, monkeypatch):
    """주기가 여러 번 돌아도 멀쩡한 세션은 그대로다 — 대조군이 없으면 아래 셋이 무의미하다.

    ⚠ **바퀴 수를 세고 판정한다**(F68). 예전에는 `sleep(_TICK * 6)` 뒤에 취소했는데, 그건
    "여섯 바퀴가 돌았다"가 아니라 "여섯 틱만큼 시간이 흘렀다"일 뿐이다. DB 왕복이 틱보다
    느린 판에서는 **한 바퀴도 안 돈 채** 끝나고, 그러면 이 대조군이 아무것도 안 재고 초록이
    된다 — 대조군이 헛돌면 아래 셋(무효화·해제·강등)의 "닫혔다"도 뜻을 잃는다.

    이 파일이 5절에서 이미 같은 함정을 실측으로 잡아 뒀다(`_count_rounds` docstring —
    "고장 난 코드로 이 시험을 돌려도 통과했다"). 같은 손잡이를 여기서도 쓴다.
    """
    _, token = await _make_session("recheck-live", auth.ROLE_AGENT)
    sock = _socket_for(token)
    rounds = _count_rounds(monkeypatch)

    task = asyncio.create_task(_ws_dashboard_recheck_loop(sock, _TICK))
    try:
        await _wait_rounds(rounds, 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert sock.close_code is None, "살아 있는 세션이 끊겼다"
    assert all(rounds), f"판정이 통과가 아니었다 — {rounds}"


# ── 3. 죽은 세션은 1008로 닫는다 ───────────────────────────────────────────


async def test_revoked_session_is_closed(flag_on):
    """로그아웃(무효화)한 계정의 열린 탭이 닫힌다."""
    _, token = await _make_session("recheck-revoked", auth.ROLE_AGENT)
    sock = _socket_for(token)
    async with get_session() as session:
        await auth.revoke_session(session, token)

    await _run_until_closed(sock)
    assert sock.close_code == WS_SESSION_REVOKED_CODE, (
        "무효화된 세션의 소켓이 안 닫혔다 — 강등·해제가 다음 요청부터 먹힌다는 근거가 WS에서 깨진다"
    )


async def test_deactivated_account_is_closed(flag_on):
    """계정 해제(`is_active=false`)도 같은 자리에서 걸린다."""
    user, token = await _make_session("recheck-off", auth.ROLE_AGENT)
    sock = _socket_for(token)
    async with get_session() as session:
        await session.execute(
            update(AppUser).where(AppUser.id == user.id).values(is_active=False)
        )
        await session.commit()

    await _run_until_closed(sock)
    assert sock.close_code == WS_SESSION_REVOKED_CODE, "해제된 계정의 소켓이 안 닫혔다"


async def test_demoted_below_agent_is_closed(flag_on):
    """⭐ 강등 갈래. 세션은 살아 있고 급만 요원 아래로 떨어진 자리다.

    무효화·해제와 달리 `resolve_session`은 사람을 그대로 돌려주고, 급 판정(`RANK`)에서만
    떨어진다. 이 케이스가 없으면 "세션이 살아 있나"만 재는 시험이 되어 강등이 안 잡힌다.
    """
    user, token = await _make_session("recheck-demoted", auth.ROLE_AGENT)
    sock = _socket_for(token)
    async with get_session() as session:
        await session.execute(
            update(AppUser).where(AppUser.id == user.id).values(role="none")
        )
        await session.commit()

    await _run_until_closed(sock)
    assert sock.close_code == WS_SESSION_REVOKED_CODE, "강등된 계정의 소켓이 안 닫혔다"


# ── 4. 조회가 터져도 안 끊는다 ─────────────────────────────────────────────


async def test_transient_db_error_does_not_close(flag_on, monkeypatch):
    """DB가 잠깐 흔들리는 판에 열린 화면을 전부 떨어뜨리지 않는다.

    그 순간은 앱 전체가 서 있는 시점이라 끊어서 얻는 게 없고, 관제만 눈을 잃는다.

    ⚠ **터진 바퀴 수를 세고 판정한다**(F68). 예전에는 `sleep(_TICK * 5)` 뒤에 취소했는데,
    그러면 조회가 한 번도 안 터진 채 끝나도 "안 끊겼다"가 초록이다 — 재는 게 "실패를
    견뎠다"가 아니라 "실패가 아직 안 왔다"가 된다. 위 대조군과 같은 자리다.
    """
    _, token = await _make_session("recheck-dberr", auth.ROLE_AGENT)
    sock = _socket_for(token)

    blown: list[bool] = []

    async def _boom(_ws, **_kwargs):
        blown.append(True)
        raise RuntimeError("db down")

    monkeypatch.setattr("app.security.ws_dashboard_allowed", _boom)

    task = asyncio.create_task(_ws_dashboard_recheck_loop(sock, _TICK))
    try:
        await _wait_rounds(blown, 3)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert sock.close_code is None, "조회 실패 하나로 살아 있는 화면이 끊겼다"
    assert len(blown) >= 3, f"조회가 실제로 안 터졌다 — 견딤을 못 쟀다 ({len(blown)}회)"


# ── 5. 재검증이 유휴 만료를 밀지 않는다 (2026-08-02 2차 검토 G1) ────────────
#
# 여기가 이 파일에서 제일 늦게 생긴 축이다. 위 일곱 건은 전부 "닫나 / 안 닫나"만 재서,
# 재검증이 매 바퀴 `last_seen_at`을 밀어 **유휴 만료를 껐던** 실사고가 초록으로 지나갔다.
# 주기(60초)와 갱신 문턱(60초)이 같아서 재검증이 도는 내내 문턱을 넘고, 관제 화면을 켜 둔
# 탭은 사람이 아무것도 안 해도 절대 만료(12시간)까지 살았다.
#
# ⚠ 이 둘은 짝이다. "안 민다"만 재면 touch 갈래를 통째로 지워도 초록이라, 사람 요청이
# 미는 쪽까지 같이 재야 계약이 닫힌다.


def _count_rounds(monkeypatch) -> list[bool]:
    """판정 함수를 감싸 **끝난 바퀴 수**를 셀 수 있게 한다. 판정은 진짜 함수가 그대로 한다.

    ⚠ 시간으로 기다리면 안 된다. `sleep(_TICK * 6)` 뒤에 취소하는 방식은 DB 왕복이 그보다
    느린 판에서 첫 바퀴의 UPDATE가 날아가는 채로 끝나서, 아무것도 안 재고 초록이 된다
    (2026-08-02 실측 — 고장 난 코드로 이 시험을 돌려도 통과했다). 그래서 바퀴가 **끝났다는
    신호**를 세고, 그 신호를 보고 나서 판정한다.
    """
    real = security.ws_dashboard_allowed
    rounds: list[bool] = []

    async def _counted(ws, **kwargs):
        result = await real(ws, **kwargs)
        rounds.append(result)
        return result

    monkeypatch.setattr("app.security.ws_dashboard_allowed", _counted)
    return rounds


async def _wait_rounds(rounds: list[bool], want: int, timeout: float = 15.0) -> None:
    waited = 0.0
    while len(rounds) < want:
        if waited >= timeout:
            raise AssertionError(f"재검증이 {timeout}초 안에 {want}바퀴를 못 돌았다")
        await asyncio.sleep(_TICK)
        waited += _TICK


async def test_recheck_does_not_push_last_seen_at(flag_on, monkeypatch):
    """⭐ 재검증이 도는 동안 `last_seen_at`이 그대로여야 한다.

    설계 §1.4는 유휴 만료를 "`last_seen_at` + 2시간이고 **활동하면** 밀린다"로 못박았다.
    서버가 자기 배경 태스크를 사람의 활동으로 세면 그 계약이 WS에서만 깨진다.
    """
    _, token = await _make_session("recheck-noidle", auth.ROLE_AGENT)
    sock = _socket_for(token)
    # 10분 전으로 민다 — 갱신 문턱(60초)을 한참 넘긴 자리라, 안 밀렸으면 진짜 안 민 것이다.
    before = await _rewind_last_seen(token, 600)

    rounds = _count_rounds(monkeypatch)
    task = asyncio.create_task(_ws_dashboard_recheck_loop(sock, _TICK))
    try:
        await _wait_rounds(rounds, 2)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert rounds[:2] == [True, True], "사전 조건: 이 세션은 두 바퀴 다 통과해야 한다"
    assert sock.close_code is None, "사전 조건: 이 세션은 살아 있어야 한다"
    assert await _last_seen_at(token) == before, (
        "재검증이 last_seen_at을 밀었다 — 화면을 켜 둔 것만으로 유휴 만료가 영영 안 온다(§1.4)"
    )


async def test_human_handshake_still_pushes_last_seen_at(flag_on):
    """대조군 — 사람이 보내는 요청(핸드셰이크)은 여전히 밀어야 한다.

    이게 없으면 위 시험이 "touch를 통째로 지웠다"까지 초록으로 통과시켜, 유휴 만료가
    반대로 **로그인 직후부터** 2시간 뒤에 무조건 떨어진다.
    """
    _, token = await _make_session("recheck-touch", auth.ROLE_AGENT)
    before = await _rewind_last_seen(token, 600)

    assert await ws_dashboard_allowed(_socket_for(token)) is True

    assert await _last_seen_at(token) > before, (
        "핸드셰이크가 last_seen_at을 안 밀었다 — 활동해도 유휴 만료가 안 밀린다(§1.4)"
    )
