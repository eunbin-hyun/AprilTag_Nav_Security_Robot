"""실시간 카메라 프록시 (`app/routers/camera.py`).

## 젯슨은 절대 안 부른다

상류를 `httpx.AsyncBaseTransport` 가짜로 갈아 끼운다. 갈아 끼우는 자리는 `camera._new_client`
하나다 — 그 함수가 시험용 이음매로 열려 있다. 그래서 이 파일은 바깥 망을 한 번도 안 탄다.

## 앱을 따로 안 세운다 — 운영 앱을 그대로 태운다

⚠ 2026-08-04 전체검토 L17로 뒤집힌 자리다. 예전에는 `FastAPI()`를 새로 만들어 `camera.router`
하나만 붙였고 "재는 건 라우터의 거동이지 등록 여부가 아니다"라고 적어 뒀다. 그런데 그러면
`app/main.py`의 `include_router(camera.router)` 한 줄을 **지워도 이 파일이 통째로 초록이다.**
창구가 아무 데도 안 달린 채 계약만 초록인 자리라, 2026-08-02에 `GET /api/staff/by-tag`가 늘
404인데 초록이던 것과 같은 계열이다.

지금은 `app.main.app`을 그대로 쓴다. 미들웨어 변경에 흔들릴 여지를 감수하는 대신, 배선이
빠지면 이 파일이 곧장 404로 말한다.

## 자물쇠를 여는 자리와 진짜로 통과해 보는 자리

자물쇠(`require_agent_always`)가 세션을 조회하는 갈래는 `tests/test_auth_gates.py`가 계열별로
재고 있어서, 여기서 그 표를 다시 그리지 않는다. 대신 **이 창구 하나가 진짜 세션 쿠키로 열리는
갈래를 한 건**(`test_real_session_cookie_opens_stream`) 잰다 — 나머지를 전부
`dependency_overrides`로 열고 재면 게이트를 잘못 걸어도(예: 자물쇠 없는 의존성) 이 파일이
통째로 통과한다. 뒤쪽 거동(프레임·502·503·끊김)은 계속 자물쇠만 열고 본다.

그래서 이 파일은 **DB를 탄다.** conftest의 `_schema`·`_clean`을 예전에는 여기서 빈 픽스처로
덮었는데, 그러면 진짜 세션을 만들 자리가 없고 계정 행이 다음 파일로 샌다. 덮개를 걷었다.

## 끊김을 어떻게 재나 — 실물 ASGI로 태운다

`ASGITransport`는 앱을 **끝까지 돌려** 본문을 다 모은 뒤에 응답을 돌려준다(httpx 0.28 실측 —
`_transports/asgi.py`의 `body_parts`). 그래서 시험 클라이언트로는 "보다가 창을 닫는" 순간을
못 만든다.

예전에는 창구 함수를 직접 부르고 본문 제너레이터를 `aclose()`했는데, 그건 실물 순서가
**아니다.** starlette 0.41.3은 `http.disconnect`를 보면 task group을 취소하고(본문 제너레이터는
`yield`나 상류 `await`에 멈춘 채 남는다) **그 다음에** background를 부른다
(`starlette/responses.py` `StreamingResponse.__call__`). 그래서 여기서는 앱을 ASGI 규약대로
직접 태우고(`_drive_asgi`) 그 순서를 그대로 잰다.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import auth
from app.config import get_settings
from app.db import get_session
from app.main import app as main_app
from app.models import AppUser
from app.routers import camera

pytestmark = pytest.mark.asyncio(loop_scope="session")

_MJPEG = "multipart/x-mixed-replace; boundary=frameboundary"


def _without_stamp(data: bytes) -> bytes:
    """`X-Server-Frame-Time` 줄을 걷어낸 바이트.

    2026-08-06에 나가는 프레임마다 시각 헤더가 한 줄 붙었다. 프레임 **내용**을 재는
    케이스들이 그 한 줄에 걸려 깨지면 안 되므로, 비교 전에 그 줄만 뺀다. 헤더 자체가
    붙는지는 아래 "시각 헤더" 묶음이 따로 잰다 — 여기서 빼도 그 계약은 안 헐렁해진다.
    """
    return b"\r\n".join(
        line
        for line in data.split(b"\r\n")
        if not line.startswith(camera.FRAME_TIME_HEADER + b":")
    )


_FRAMES = [
    b"--frameboundary\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8frame-1\xff\xd9\r\n",
    b"--frameboundary\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8frame-2\xff\xd9\r\n",
]


# ── 가짜 젯슨 ───────────────────────────────────────────────────────────────

class _FakeStream(httpx.AsyncByteStream):
    """프레임을 흘리고, 닫혔는지를 기록한다.

    `gate`를 주면 프레임을 다 내보낸 뒤 그 자리에서 기다린다 — 스트림 하나를 "열린 채"로
    붙잡아 두는 방법이고, 동시 접속 상한을 재는 데 쓴다.

    `close_needs_await`를 주면 닫기가 **await를 한 번 탄다.** 실물 httpx 정리는 커넥션을
    반납하느라 await를 타므로, 취소 스코프 안에서 닫으면 그 자리에서 취소로 튄다 — 그
    갈래를 재려면 가짜도 await를 타야 한다(안 타는 코루틴은 취소가 전달될 자리가 없어서
    취소 중에도 그냥 성공해 버린다).
    """

    def __init__(
        self,
        frames: list[bytes],
        gate: asyncio.Event | None = None,
        close_needs_await: bool = False,
    ) -> None:
        self._frames = frames
        self._gate = gate
        self._close_needs_await = close_needs_await
        self.closed = False

    async def __aiter__(self):
        for frame in self._frames:
            yield frame
        if self._gate is not None:
            await self._gate.wait()

    async def aclose(self) -> None:
        if self._close_needs_await:
            await asyncio.sleep(0)
        self.closed = True


class _FakeJetson(httpx.AsyncBaseTransport):
    """상류 한 대. 200으로 프레임을 주거나, 다른 상태를 주거나, 아예 못 붙는다.

    `connect_gate`를 주면 **연결 수립 중에 멈춘다** — 응답 헤더를 만들기 전 자리라, 그때
    취소를 넣으면 창구가 `client.send`에 멈춘 채 끊기는 실물 순서가 된다. 어디까지 왔는지는
    `connecting`으로 알린다(고정 sleep을 안 쓰려고 둔다).

    `pool_closed`는 **클라이언트가 진짜로 닫혔는지**를 잰다. `AsyncClient.is_closed`는
    `_transport.aclose()`를 부르기 **전에** 서는 플래그라(httpx 0.28 `_client.py`, 이 저장소가
    `Response.is_closed`에서 이미 밟은 함정과 같은 성질) 취소 중에는 "닫혔다"고 거짓말한다.
    여기 `aclose`는 await를 한 번 타고 나서 표시하므로, 취소를 막아 내지 못하면 안 선다.
    """

    def __init__(
        self,
        *,
        status_code: int = 200,
        frames: list[bytes] | None = None,
        gate: asyncio.Event | None = None,
        error: Exception | None = None,
        close_needs_await: bool = False,
        connect_gate: asyncio.Event | None = None,
        content_type: str | None = _MJPEG,
    ) -> None:
        self.status_code = status_code
        self.frames = frames if frames is not None else list(_FRAMES)
        self.gate = gate
        self.error = error
        self.close_needs_await = close_needs_await
        self.connect_gate = connect_gate
        # None이면 Content-Type을 **아예 안 싣는다**(헤더 없는 상류를 재는 자리).
        self.content_type = content_type
        self.connecting = asyncio.Event()
        self.pool_closed = False
        self.streams: list[_FakeStream] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.connecting.set()
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        if self.error is not None:
            raise self.error
        stream = _FakeStream(list(self.frames), self.gate, self.close_needs_await)
        self.streams.append(stream)
        return httpx.Response(
            self.status_code,
            headers=({"content-type": self.content_type} if self.content_type else {}),
            stream=stream,
            request=request,
        )

    async def aclose(self) -> None:
        # 실물 정리는 커넥션을 반납하느라 await를 탄다 — 취소가 전달될 자리를 만들어 둔다.
        await asyncio.sleep(0)
        self.pool_closed = True


@pytest.fixture(autouse=True)
def _clean_camera():
    """앞 케이스가 남긴 자리 카운터를 비운다(DB 밖 상태라 TRUNCATE로 안 지워진다).

    ⚠ 공용 `conftest._clean`에는 아직 안 들어가 있다 — 그 파일은 이번 사이클에 여러 조가
    같이 만지는 자리라 여기서 든다. 옮기는 건 팀장 배선 몫이다.
    """
    camera.reset_camera_state()
    yield
    camera.reset_camera_state()


@pytest.fixture(autouse=True)
def _no_real_jetson(monkeypatch):
    """`jetson`을 안 문 케이스가 실기기 주소(사설 IP)로 나가는 걸 막는다.

    상류 주소는 아직 설정에 없어서 `camera.DEFAULT_STREAM_URL` 그대로 나간다 — 가짜를 안 깐
    케이스가 하나 생기면 시험이 조용히 바깥 망을 탄다. 공용 그물(`conftest`)이 카메라 GET을
    막아 주는 게 옳은 자리지만 그 파일은 이번 사이클에 여러 조가 같이 만지므로, 여기서 먼저
    막는다(옮기는 건 팀장 배선 몫).

    autouse라 `jetson`보다 먼저 깔리고, 케이스가 `jetson(...)`을 부르면 그게 이걸 덮는다.
    """

    def _refuse(timeout):
        raise AssertionError(
            "가짜 젯슨을 안 깔고 상류로 나가려 했다 — jetson 픽스처를 물어라"
        )

    monkeypatch.setattr(camera, "_new_client", _refuse)


@pytest.fixture
def jetson(monkeypatch):
    """`camera._new_client`를 가짜 상류로 갈아 끼우는 공장."""

    def _install(transport: _FakeJetson) -> _FakeJetson:
        monkeypatch.setattr(
            camera,
            "_new_client",
            lambda timeout: httpx.AsyncClient(transport=transport, timeout=timeout),
        )
        return transport

    return _install


@pytest.fixture
def limit_one(monkeypatch):
    """동시 접속 상한만 1로 낮춘다.

    모듈 상수가 아니라 `_setting`을 감싼다 — 팀장이 `camera_max_viewers`를 `config.py`에
    넣으면 설정값이 상수를 이기므로, 상수만 갈아 끼우는 방식은 그날 조용히 뜻을 잃는다.
    """
    real = camera._setting
    monkeypatch.setattr(
        camera,
        "_setting",
        lambda name, default: 1 if name == "camera_max_viewers" else real(name, default),
    )


@pytest.fixture
def camera_app():
    """⭐ **운영 앱 그대로**다(`app.main.app`). 자물쇠는 진짜다(익명이면 401).

    2026-08-04 전체검토 L17로 갈아탄 자리다. 예전에는 `FastAPI()`를 새로 세워 라우터만
    붙였는데, 그러면 `app/main.py`의 `include_router(camera.router)` 한 줄을 **지워도 이
    파일이 통째로 초록이다.** 2026-08-02에 `GET /api/staff/by-tag`가 늘 404인데 초록이던
    것과 같은 계열이라, 창구 거동과 배선을 한 자리에서 같이 잰다.

    ⚠ 딸린 대가 하나 — 운영 앱은 미들웨어를 얹고 있다(`OriginGuardMiddleware`). 그 가드는
    `Origin` 헤더가 실렸을 때만 판정하는데 이 파일은 그 헤더를 안 보내서 그대로 지난다.
    """
    return main_app


@pytest.fixture
def logged_in(camera_app):
    """자물쇠만 열어 둔 앱. 요원 한 명이 들어와 있는 상태다.

    세션 쿠키를 실제로 푸는 갈래는 `tests/test_auth_gates.py`가 잰다(모듈 머리 참고).

    ⚠ 이제 덮어쓰는 대상이 **운영 앱 한 개**라 반드시 되돌려야 한다. 안 걷으면 이 파일이
    끝난 뒤에도 카메라 자물쇠가 열린 채 남아, 딴 파일의 익명 401 케이스가 조용히 200이 된다.
    """
    actor = auth.AuthActor(
        id=1, username="cam-agent", display_name="보는 사람", role=auth.ROLE_AGENT
    )
    camera_app.dependency_overrides[camera._gate] = lambda: actor
    try:
        yield camera_app
    finally:
        camera_app.dependency_overrides.pop(camera._gate, None)


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _drive_asgi(app: FastAPI, *, cut_after: int = 1) -> tuple[dict, list[bytes]]:
    """앱을 ASGI 규약대로 직접 태우고, 본문 조각을 `cut_after`개 받은 뒤 창을 닫는다.

    "창을 닫는다" = `receive`가 `http.disconnect`를 내는 것이다. 그 뒤 starlette이 하는 일이
    실물 순서다 — task group 취소 → (본문 태스크 죽음) → background 정리
    (`starlette/responses.py` `StreamingResponse.__call__`). 이 함수가 돌아왔을 때는 background
    까지 끝난 뒤라, 좀비가 남았는지를 그 시점에 그대로 잰다.
    """
    body: list[bytes] = []
    start: dict = {}
    cut = asyncio.Event()
    asked = False

    async def receive() -> dict:
        nonlocal asked
        if not asked:
            asked = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await cut.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            start.update(message)
        elif message["type"] == "http.response.body":
            chunk = message.get("body", b"")
            if chunk:
                body.append(chunk)
            if len(body) >= cut_after:
                cut.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/camera/stream",
        "raw_path": b"/api/camera/stream",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 51000),
        "server": ("test", 80),
    }
    await app(scope, receive, send)
    return start, body


# ── 1. 로그인 없이 부르면 막힌다 ────────────────────────────────────────────

async def test_anonymous_is_blocked_even_when_flag_off(camera_app, jetson):
    """⭐ `AUTH_REQUIRE_LOGIN`이 꺼져 있어도 401이다.

    이 창구는 로그인과 함께 새로 생긴 자리라 플래그를 안 따라간다(`require_agent_always`).
    따라가게 두면 플래그가 꺼진 지금 공개 도메인에 카메라가 그대로 열린다.
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    transport = jetson(_FakeJetson())

    async with _client(camera_app) as client:
        res = await client.get("/api/camera/stream")

    assert res.status_code == 401
    assert transport.streams == [], "막힌 요청이 젯슨까지 갔다"
    assert camera.active_viewers() == 0


async def test_anonymous_is_blocked_when_flag_on(camera_app, jetson, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    jetson(_FakeJetson())
    try:
        async with _client(camera_app) as client:
            res = await client.get("/api/camera/stream")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert res.status_code == 401


async def test_real_session_cookie_opens_stream(camera_app, jetson):
    """⭐ **자물쇠를 안 열고** 진짜 세션 쿠키로 통과하는 갈래 하나(모듈 머리 참고).

    나머지 케이스는 `dependency_overrides`로 자물쇠를 열고 뒤쪽만 본다. 그러면 이 창구에
    자물쇠를 아예 안 걸어도, 쿠키 이름을 틀리게 읽어도 전부 통과한다 — 실제 로그인으로
    열리는지는 아무도 안 잰다.

    계정·세션을 만드는 방식은 `tests/test_auth_gates.py` `_session_cookie`와 같다(같은 세
    줄이라 새 방식을 만들지 않는다). 토큰은 `secrets.token_urlsafe`라 늘 ASCII다.
    """
    transport = jetson(_FakeJetson())
    async with get_session() as session:
        user = AppUser(
            username="cam-real-agent",
            password_hash=auth.hash_password("cam-pw-1234"),
            display_name="진짜 요원",
            role=auth.ROLE_AGENT,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    async with get_session() as session:
        token = await auth.create_session(session, user)

    async with _client(camera_app) as client:
        res = await client.get(
            "/api/camera/stream",
            headers={"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"},
        )

    assert res.status_code == 200, "진짜 세션 쿠키가 자물쇠를 못 열었다"
    # ⚠ **전부가 아니라 마지막 장**이다. 실시간 솎기(`camera_realtime_drop`)가 켜져 있으면
    #    소비자보다 앞서 달린 펌프가 밀린 장을 버리고 최신 한 장만 남긴다. 이 케이스가 재는
    #    것은 "자물쇠가 진짜 세션으로 열리나"라서, 어느 장이 왔는지가 아니라 **프레임이
    #    흘렀는지**로 판정한다. 솎기 자체는 아래 "밀린 프레임 버리기" 묶음이 따로 잰다.
    assert _without_stamp(res.content).endswith(_FRAMES[-1]), "마지막 프레임이 안 왔다"
    assert transport.streams[0].closed is True
    assert camera.active_viewers() == 0


async def test_bogus_session_cookie_is_blocked(camera_app, jetson):
    """살아 있는 세션이 아닌 쿠키는 401이다(쿠키만 있으면 통과하는 자리가 아니다)."""
    transport = jetson(_FakeJetson())

    async with _client(camera_app) as client:
        res = await client.get(
            "/api/camera/stream",
            headers={"Cookie": f"{auth.SESSION_COOKIE_NAME}=not-a-real-token"},
        )

    assert res.status_code == 401
    assert transport.streams == [], "막힌 요청이 젯슨까지 갔다"


# ── 2. 로그인하면 프레임이 흘러나온다 ───────────────────────────────────────

async def test_agent_gets_frames(logged_in, jetson):
    transport = jetson(_FakeJetson())

    async with _client(logged_in) as client:
        res = await client.get("/api/camera/stream")

    assert res.status_code == 200
    # 상류가 준 Content-Type을 그대로 흘린다(경계 문자열을 두 자리에 안 적는다).
    assert res.headers["content-type"] == _MJPEG
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-accel-buffering"] == "no"
    # ⚠ 위 케이스와 같은 이유로 **마지막 장**만 확인한다 — 실시간 솎기가 밀린 장을 버린다.
    assert _without_stamp(res.content).endswith(_FRAMES[-1]), "프레임이 안 흘렀다"
    # 스트림이 끝나면 자리도 상류 커넥션도 되돌아간다.
    assert camera.active_viewers() == 0
    assert transport.streams[0].closed is True


async def test_upstream_path_is_stream(logged_in, jetson, monkeypatch):
    """상류로 나가는 주소가 설정값 그대로인가(젯슨 루트는 안내 HTML이라 `/stream`이다)."""
    seen: list[str] = []

    class _Recording(_FakeJetson):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return await super().handle_async_request(request)

    jetson(_Recording())

    async with _client(logged_in) as client:
        await client.get("/api/camera/stream")

    # 팀장이 `camera_stream_url`을 config.py에 넣으면 설정값이 기본값을 이긴다. 그때도
    # 이 케이스가 그대로 돌게 기대값을 같은 함수에서 뽑는다.
    expected = camera._setting("camera_stream_url", camera.DEFAULT_STREAM_URL)
    assert seen == [expected]
    assert expected.endswith("/stream"), "상류 경로가 /stream이 아니다(루트는 안내 HTML이다)"


# ── 3. 젯슨이 죽어 있으면 502 ───────────────────────────────────────────────

async def test_upstream_down_is_502(logged_in, jetson):
    jetson(_FakeJetson(error=httpx.ConnectError("연결 거부")))

    async with _client(logged_in) as client:
        res = await client.get("/api/camera/stream")

    assert res.status_code == 502
    # 빈 200을 오래 물고 있지 않는다 — 상태줄부터 502다.
    assert "카메라에 연결하지" in res.json()["detail"]
    assert camera.active_viewers() == 0, "실패한 요청이 자리를 물고 있다"


async def test_upstream_error_status_is_502(logged_in, jetson):
    """상류가 붙긴 했는데 404(=경로가 바뀜)를 주면 그것도 502다."""
    transport = jetson(_FakeJetson(status_code=404, frames=[]))

    async with _client(logged_in) as client:
        res = await client.get("/api/camera/stream")

    assert res.status_code == 502
    assert transport.streams[0].closed is True, "안 쓸 상류를 안 닫았다"
    assert camera.active_viewers() == 0


async def test_upstream_html_200_is_502(logged_in, jetson):
    """⭐ 상류 주소에서 `/stream`을 빼면 젯슨은 **안내 HTML을 200으로** 준다.

    상태 코드만 보던 시절에는 그 HTML이 200 + `text/html`로 그대로 흘러서, 화면엔 깨진
    이미지만 뜨고 502를 기다리는 사람은 원인을 못 찾았다(배선 실수가 조용해지는 자리다).
    """
    transport = jetson(
        _FakeJetson(content_type="text/html; charset=utf-8", frames=[b"<html>preview</html>"])
    )

    async with _client(logged_in) as client:
        res = await client.get("/api/camera/stream")

    assert res.status_code == 502
    assert "카메라에 연결하지" in res.json()["detail"]
    assert transport.streams[0].closed is True, "안 쓸 상류를 안 닫았다"
    assert camera.active_viewers() == 0, "끊은 요청이 자리를 물고 있다"


async def test_missing_content_type_still_streams(logged_in, jetson):
    """⚠ 헤더가 **아예 없는** 상류는 막지 않는다 — 응답이 쓰는 기본값 갈래가 살아 있어야 한다.

    이 못이 없으면 "MJPEG가 아니면 502" 가드가 `FALLBACK_CONTENT_TYPE`을 조용히 죽인다.
    """
    jetson(_FakeJetson(content_type=None))

    async with _client(logged_in) as client:
        res = await client.get("/api/camera/stream")

    assert res.status_code == 200
    assert res.headers["content-type"] == camera.FALLBACK_CONTENT_TYPE
    assert res.content == b"".join(_FRAMES)


# ── 4. 동시 접속 상한을 넘기면 503 ──────────────────────────────────────────

async def test_over_limit_is_503(logged_in, jetson, limit_one):
    """한 명이 보고 있는 동안 두 번째가 오면 503이고, 첫 번째는 안 끊긴다."""
    gate = asyncio.Event()
    transport = jetson(_FakeJetson(gate=gate))

    async with _client(logged_in) as client:
        first = asyncio.create_task(client.get("/api/camera/stream"))
        # 첫 스트림이 자리를 잡을 때까지 기다린다(고정 sleep은 안 쓴다).
        for _ in range(200):
            if camera.active_viewers() == 1:
                break
            await asyncio.sleep(0.01)
        assert camera.active_viewers() == 1, "첫 스트림이 자리를 안 잡았다"

        second = await client.get("/api/camera/stream")
        assert second.status_code == 503
        assert second.headers["retry-after"] == "5"
        assert len(transport.streams) == 1, "상한에 걸린 요청이 젯슨까지 갔다"

        gate.set()
        first_res = await first

    assert first_res.status_code == 200, "뒤에 온 요청이 앞 스트림을 끊었다"
    assert camera.active_viewers() == 0


# ── 5. 클라이언트가 끊으면 상류 연결도 닫힌다 ───────────────────────────────

async def test_client_disconnect_closes_upstream(logged_in, jetson):
    """⭐ 실물 ASGI로 태워서 "보다가 창을 닫는" 순간을 만든다(모듈 머리 참고).

    앱이 돌아왔을 때는 starlette이 background 정리까지 끝낸 뒤다 — 그 시점에 젯슨 쪽이
    닫혔는지, 자리가 되돌아왔는지를 잰다.
    """
    gate = asyncio.Event()
    transport = jetson(_FakeJetson(gate=gate))

    start, body = await _drive_asgi(logged_in, cut_after=1)

    assert start["status"] == 200
    assert body and _without_stamp(body[0]) == _FRAMES[0]
    # 가짜 상류는 프레임을 다 낸 뒤 gate에서 기다린다 — 즉 끊긴 시점에 스트림은 열려 있었다.
    assert transport.streams[0].closed is True, "젯슨 쪽 연결이 좀비로 남았다"
    assert camera.active_viewers() == 0, "끊긴 뒤에도 자리를 물고 있다"
    gate.set()


async def test_disconnect_closes_upstream_even_when_close_is_cancelled(logged_in, jetson):
    """⭐ 정리가 **취소 중**에 들어와 못 닫혔으면 background가 이어서 닫아야 한다.

    끊김이 나면 본문 태스크가 취소되는데, 그때 제너레이터가 상류 `await`에 멈춰 있으면
    `finally`가 취소 스코프 안에서 돈다 — 거기서 부른 `aclose`는 첫 `await`에서 다시 취소로
    튄다. 예전 코드는 그걸 삼키고 "닫았다" 표시를 세워서, 뒤따르는 background 정리가 빈
    호출이 되고 젯슨에 좀비 커넥션이 남았다.

    `close_needs_await`가 그 자리를 만든다(가짜 정리가 await를 안 타면 취소가 전달될 자리가
    없어서 이 갈래가 안 재진다 — 그게 예전 시험이 못 잡은 이유다).
    """
    gate = asyncio.Event()
    transport = jetson(_FakeJetson(gate=gate, close_needs_await=True))

    await _drive_asgi(logged_in, cut_after=1)

    assert transport.streams[0].closed is True, "취소가 정리를 삼켜 좀비 커넥션이 남았다"
    assert camera.active_viewers() == 0, "끊긴 뒤에도 자리를 물고 있다"
    gate.set()


async def test_cancel_while_connecting_closes_upstream_client(jetson):
    """⭐ **연결 수립 중**에 취소가 오면 잡다 만 커넥션도 같이 닫힌다.

    `await client.send(...)`에 멈춰 있을 때 오는 `CancelledError`는 `BaseException`이라 창구의
    `except Exception`(=502 갈래)에 안 걸린다. 예전 코드는 바깥에서 **자리만** 반납하고 client를
    안 닫아서, 젯슨이 느린 날(연결 타임아웃 3초) 사용자가 탭을 닫으면 커넥션이 그대로 남았다.

    ⚠ 판정을 `AsyncClient.is_closed`로 하면 안 된다 — 그 플래그는 `_transport.aclose()`를 부르기
    **전에** 서서, 안 닫아도 True로 보인다(`_FakeJetson` 주석). 그래서 가짜 transport가 await를
    한 번 탄 뒤에 세우는 `pool_closed`로 잰다. 취소를 막아 내지 못하면 그 await에서 튀어 안 선다.

    반대편도 같이 잰다 — 취소를 삼키면 안 되니 `CancelledError`가 그대로 밖으로 나와야 한다.
    """
    hang = asyncio.Event()
    transport = jetson(_FakeJetson(connect_gate=hang))
    actor = auth.AuthActor(
        id=1, username="cam-agent", display_name="보는 사람", role=auth.ROLE_AGENT
    )

    task = asyncio.create_task(camera.stream_camera(actor=actor))
    await asyncio.wait_for(transport.connecting.wait(), 2)
    assert camera.active_viewers() == 1, "자리를 잡기 전에 취소돼서 재는 게 없다"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert camera.active_viewers() == 0, "취소된 요청이 자리를 물고 있다"
    assert transport.pool_closed is True, "취소 중에 상류 커넥션이 좀비로 남았다"
    hang.set()


async def test_aclose_quietly_reraises_cancel_but_still_closes_rest():
    """취소는 다시 던지되, 남은 것도 한 번씩은 닫아 본다(C2).

    취소를 먹으면 서버 종료 중 정리 태스크가 취소돼도 그걸 안 존중한다. 다시 던지는 게
    "덜 닫혔다"를 부르는 쪽에 알리는 신호이기도 하다.
    """
    closed: list[str] = []

    class _Cancelling:
        async def aclose(self) -> None:
            raise asyncio.CancelledError()

    class _Normal:
        async def aclose(self) -> None:
            closed.append("normal")

    class _Broken:
        async def aclose(self) -> None:
            raise RuntimeError("닫기 실패")

    with pytest.raises(asyncio.CancelledError):
        await camera._aclose_quietly(_Cancelling(), _Broken(), _Normal())

    assert closed == ["normal"], "취소 뒤에 오는 것을 안 닫았다"


# ── 6. 워커 1개 전제를 코드가 스스로 본다 ───────────────────────────────────

async def test_multi_worker_env_warns_on_first_request(logged_in, jetson, monkeypatch, caplog):
    """⭐ 워커를 늘리면 상한이 워커 수만큼 곱해진다 — 그 전제가 깨지면 로그가 말한다.

    가드가 있는 것만으로는 안 되고 **창구가 실제로 부르는지**까지 재야 한다. 그래서 함수를
    직접 안 부르고 요청 하나를 태운다.
    """
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    jetson(_FakeJetson())

    with caplog.at_level("WARNING", logger="c207.camera"):
        async with _client(logged_in) as client:
            res = await client.get("/api/camera/stream")

    assert res.status_code == 200, "경고는 남기되 요청은 막지 않는다"
    warned = [r for r in caplog.records if "워커" in r.getMessage()]
    assert warned, "워커 4개인데 아무 말이 없다"
    # ⚠ 곱한 값을 **여기서 다시 계산한다.** 예전에는 "12"를 글자로 박아 뒀는데, 2026-08-05에
    #    상한을 3에서 6으로 올리자 코드는 멀쩡한데 이 케이스만 깨졌다. 재려는 것은 "상한 × 워커를
    #    알려주나"이지 특정 숫자가 아니다.
    want = str(int(camera._setting("camera_max_viewers", camera.DEFAULT_MAX_VIEWERS)) * 4)
    assert want in warned[0].getMessage(), (
        f"상한이 곱해진 값({want})을 안 알려준다 — {warned[0].getMessage()}"
    )


async def test_single_worker_env_does_not_warn(logged_in, jetson, monkeypatch, caplog):
    """워커 1개(=지금 배포)면 경고가 없다 — 경고가 늘 뜨면 아무도 안 본다."""
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    jetson(_FakeJetson())

    with caplog.at_level("WARNING", logger="c207.camera"):
        async with _client(logged_in) as client:
            await client.get("/api/camera/stream")

    assert [r for r in caplog.records if "워커" in r.getMessage()] == []


async def test_worker_count_hint_reads_known_env_names():
    """워커 수가 드러나는 이름 셋. 숫자가 아니면 못 알아낸 것으로 친다."""
    assert camera.worker_count_hint({}) is None
    assert camera.worker_count_hint({"WEB_CONCURRENCY": "4"}) == 4
    assert camera.worker_count_hint({"GUNICORN_WORKERS": "2"}) == 2
    assert camera.worker_count_hint({"WEB_CONCURRENCY": "auto"}) is None


# ── 7. 상류 주소가 배선된 값인지 스스로 본다 ────────────────────────────────

async def test_first_request_announces_stream_target(logged_in, jetson, caplog):
    """⭐ 첫 요청에서 지금 어느 주소로 나가는지를 남긴다.

    가드가 있는 것만으로는 안 되고 **창구가 실제로 부르는지**까지 재야 한다(워커 전제 케이스와
    같은 이유). 배선 전이든 후든 한 줄은 남아야 하므로 이름만 보고 판정한다 — 팀장이
    `camera_stream_url`을 넣는 날 이 케이스가 조용히 뜻을 잃지 않게.
    """
    jetson(_FakeJetson())

    with caplog.at_level("INFO", logger="c207.camera"):
        async with _client(logged_in) as client:
            res = await client.get("/api/camera/stream")

    assert res.status_code == 200, "주소를 알리되 요청은 막지 않는다"
    said = [r for r in caplog.records if "camera_stream_url" in r.getMessage()]
    assert said, "첫 요청에서 어느 주소로 나가는지 아무 말이 없다"


async def test_unwired_stream_url_warns_with_default_address(monkeypatch, caplog):
    """설정에 이름이 없으면 WARNING으로 기본값(코드에 박힌 사설 IP)을 그대로 보여 준다.

    발표장 망이 바뀌면 여기부터 틀어지는데, 조용하면 화면이 502로 뜬 뒤에야 원인을 찾는다.
    두 번 불러도 한 줄인지도 같이 잰다 — 매 요청마다 뜨면 아무도 안 읽는다.
    """
    monkeypatch.setattr(camera, "_setting", lambda name, default: default)

    with caplog.at_level("INFO", logger="c207.camera"):
        camera._note_stream_target()
        camera._note_stream_target()

    said = [r for r in caplog.records if "camera_stream_url" in r.getMessage()]
    assert len(said) == 1, "첫 요청에서 한 번만 말해야 한다"
    assert said[0].levelname == "WARNING"
    assert camera.DEFAULT_STREAM_URL in said[0].getMessage(), "어느 주소로 나가는지 안 알려준다"


async def test_wired_stream_url_is_info_not_warning(monkeypatch, caplog):
    """설정이 들어오면 경고가 아니라 INFO 한 줄이고, 그 값이 그대로 찍힌다."""
    wired = "http://10.9.9.9:8090/stream"
    monkeypatch.setattr(
        camera,
        "_setting",
        lambda name, default: wired if name == "camera_stream_url" else default,
    )

    with caplog.at_level("INFO", logger="c207.camera"):
        camera._note_stream_target()

    said = [r for r in caplog.records if "camera_stream_url" in r.getMessage()]
    assert len(said) == 1
    assert said[0].levelname == "INFO", "배선이 끝났는데도 경고가 뜬다"
    assert wired in said[0].getMessage()


async def test_disconnect_cleanup_runs_once(jetson):
    """정리를 부르는 자리가 둘이라 **두 번 불려도 한 번만 먹어야** 한다.

    끊김이 나면 starlette은 본문 태스크를 취소하고 background 태스크를 부른다. 취소된
    제너레이터는 파이썬이 거둬 갈 때 자기 `finally`를 또 돈다 — 그때 자리를 두 번 반납하면
    **남이 보고 있는 자리**까지 비워져서, 상한이 뚫린다.

    옆에 손으로 자리 하나를 더 잡아 두고, 두 갈래를 다 태운 뒤에도 그 자리가 남아 있는지로
    잰다(카운터가 0에서 안 내려가는 성질에 가려지지 않게).
    """
    gate = asyncio.Event()
    transport = jetson(_FakeJetson(gate=gate))
    actor = auth.AuthActor(
        id=1, username="cam-watcher", display_name="보는 사람", role=auth.ROLE_AGENT
    )
    assert camera._try_acquire_slot(9) is True, "옆자리 하나를 못 잡았다"

    response = await camera.stream_camera(actor=actor)
    body = response.body_iterator
    assert _without_stamp(await body.__anext__()) == _FRAMES[0]
    assert camera.active_viewers() == 2

    # ① 끊김 직후 background 태스크가 먼저 돈다.
    await response.background()
    assert transport.streams[0].closed is True
    assert camera.active_viewers() == 1

    # ② 뒤늦게 거둬진 제너레이터가 자기 finally를 또 돈다.
    await body.aclose()
    assert camera.active_viewers() == 1, "자리를 두 번 반납해 남의 자리를 비웠다"
    gate.set()


# ── 밀린 프레임 버리기 (실시간 우선) ────────────────────────────────────────
#
# 2026-08-05 프론트 실측 — 상류가 초당 20.2장인데 브라우저는 7~8장만 그린다. 매초 12장씩
# 밀려 5초면 60장 넘게 뒤처졌다. 서버가 최신 한 장만 흘리면 그 밀림이 사라진다.
#
# ⚠ 이 묶음은 **DB도 앱도 안 탄다.** `_UpstreamLink`를 직접 세워 프레임 자르기만 잰다.
# 창구 배선은 위쪽 묶음이 이미 재고 있다.

_BOUND = b"--frameboundary"
_MJPEG_CT = "multipart/x-mixed-replace; boundary=frameboundary"


def _fake_frame(n: int) -> bytes:
    """번호를 본문에 심은 가짜 프레임 한 장. 어느 장이 나갔는지 세려고 심는다."""
    return _BOUND + b"\r\nContent-Type: image/jpeg\r\n\r\n" + (f"IMG{n:04d}".encode() * 4) + b"\r\n"


class _FakeUpstream:
    """`aiter_raw()`와 `headers`만 흉내 낸 상류."""

    def __init__(self, chunks: list[bytes], content_type: str) -> None:
        self._chunks = chunks
        self.headers = {"content-type": content_type}

    async def aiter_raw(self):
        for chunk in self._chunks:
            await asyncio.sleep(0)  # 펌프와 소비자가 번갈아 돌게 한다
            yield chunk


class _FakeClient:
    async def aclose(self) -> None:
        pass


def _link(chunks: list[bytes], content_type: str = _MJPEG_CT) -> camera._UpstreamLink:
    # 자리 반납이 전역 카운터를 깎으므로 잡았다 치고 하나 올려 둔다.
    camera._try_acquire_slot(99)
    return camera._UpstreamLink(_FakeClient(), _FakeUpstream(chunks, content_type), "tester")


async def _drain(link: camera._UpstreamLink, pause: float = 0.0) -> list[bytes]:
    out: list[bytes] = []
    async for frame in link.relay():
        out.append(frame)
        if pause:
            await asyncio.sleep(pause)
    return out


def _frame_numbers(frames: list[bytes]) -> list[int]:
    return [int(f.split(b"IMG")[1][:4]) for f in frames if b"IMG" in f]


# ── 영상 지연을 화면이 재게 하는 시각 헤더 (2026-08-06 사용자 요청) ────────────


def _stamp_of(frame: bytes) -> str | None:
    """나간 프레임에서 `X-Server-Frame-Time` 값을 뽑는다. 없으면 None."""
    for line in frame.split(b"\r\n"):
        if line.startswith(camera.FRAME_TIME_HEADER + b":"):
            return line.split(b":", 1)[1].decode().strip()
    return None


async def test_나가는_프레임마다_서버_시각이_붙는다():
    """⭐ 화면이 이 값을 자기 시계에서 빼 "서버 → 화면" 지연을 낸다.

    ⚠ 장 수를 못박지 않는다 — 솎기가 밀린 장을 버리는 것이 정상이라, 몇 장이 나가는지는
    이 케이스가 잴 것이 아니다(그건 위 솎기 묶음 몫이다). 여기서는 **나간 장에는 반드시**
    시각이 붙고 그 값이 화면이 뺄 수 있는 모양인지만 본다.
    """
    link = _link([_fake_frame(1), _fake_frame(2), _fake_frame(3)])

    frames = await _drain(link)

    assert frames, "프레임이 한 장도 안 나갔다"
    stamps = [_stamp_of(f) for f in frames]
    assert all(s is not None for s in stamps), stamps
    # ISO 문자열이고 tz가 붙어 있어야 화면이 자기 시계와 뺄 수 있다.
    for s in stamps:
        assert dt.datetime.fromisoformat(s).tzinfo is not None, s


async def test_꺼낸_뒤에는_슬롯의_시각도_비워진다():
    """⛔ 안 비우면 **다음 장에 옛 시각이 붙는다** — 지연이 실제보다 크게 나온다.

    프레임은 꺼내면서 비우는데 시각만 남기면 그 어긋남이 조용히 생긴다. 둘이 같은 자리에서
    같이 움직이는지를 여기서 못박는다.
    """
    link = _link([_fake_frame(1)])
    assert link._latest_at is None, "시작부터 값이 있으면 아래 판정이 무의미하다"

    await link._pump_frames(_BOUND)
    assert link._latest_frame is not None and link._latest_at is not None

    drained = [f async for f in link._newest_frames()]

    assert len(drained) == 1
    assert link._latest_frame is None
    assert link._latest_at is None, "프레임만 비우고 시각이 남았다"


async def test_시각_헤더가_그림을_안_건드린다():
    """지연 표시 하나 때문에 영상을 잃으면 손해다. 본문(JPEG)이 그대로여야 한다."""
    link = _link([_fake_frame(7)])

    frames = await _drain(link)

    assert len(frames) == 1
    body = frames[0].split(b"\r\n\r\n", 1)[1]
    assert body == b"IMG0007" * 4 + b"\r\n", body
    # 헤더 블록에만 늘었다 — 본문 앞 빈 줄이 여전히 하나다.
    assert frames[0].count(b"\r\n\r\n") == 1


def test_모양이_다른_프레임은_그대로_돌려준다():
    """상류가 헤더를 안 붙이는 판에서 억지로 끼우면 그림이 통째로 깨진다."""
    naked = b"--frameboundary\xff\xd8no-header\xff\xd9"
    at = dt.datetime.now(dt.timezone.utc)

    assert camera._stamp_frame(naked, at) == naked


async def test_같은_시각이_WS로도_나간다(monkeypatch):
    """⭐ 프론트 28차 요청. 화면이 `<img>` 로 그려서 **파트 헤더를 못 읽는다.**

    ⚠ 봉투 종류와 칸 이름을 **글자로 박는다.** 화면이 이 이름으로 읽으므로 바뀌면 시험이
    깨져야 맞다 — 상수를 import 해 비교하면 이름을 바꿔도 초록이라 계약을 못 지킨다.
    """
    sent: list[dict] = []

    async def _capture(message):
        sent.append(message)

    monkeypatch.setattr(camera.manager, "broadcast", _capture)
    link = _link([_fake_frame(1)])

    frames = await _drain(link)

    assert frames, "프레임이 안 나갔다"
    envelopes = [m for m in sent if m["type"] == "camera_frame_time"]
    assert envelopes, [m["type"] for m in sent]
    stamp = envelopes[0]["data"]["server_frame_time"]
    assert dt.datetime.fromisoformat(stamp).tzinfo is not None, stamp
    # ⭐ 헤더에 붙은 값과 **같은 시각**이어야 한다. 다르면 화면이 둘 중 무엇을 봐도
    #    지연이 어긋난다.
    assert _stamp_of(frames[0]) == stamp


async def test_WS_시각은_주기_안에_한_번만_나간다(monkeypatch):
    """⛔ 스로틀이 없으면 초당 스무 번 나간다 — 상한 셋이 붙으면 예순 번이다.

    ⚠ 스로틀을 **시청자마다** 들면 같은 시각이 붙은 수만큼 겹쳐 나간다. 모듈 전역인지를
    여기서 못박는다 — 링크 둘을 잇달아 돌려도 합쳐서 한 번이어야 한다.
    """
    sent: list[dict] = []

    async def _capture(message):
        sent.append(message)

    monkeypatch.setattr(camera.manager, "broadcast", _capture)

    await _drain(_link([_fake_frame(1), _fake_frame(2), _fake_frame(3)]))
    await _drain(_link([_fake_frame(4), _fake_frame(5)]))

    envelopes = [m for m in sent if m["type"] == "camera_frame_time"]
    assert len(envelopes) == 1, f"주기 안에 {len(envelopes)}번 나갔다"


async def test_중계가_터져도_영상은_그대로_간다(monkeypatch):
    """표시용 곁가지다. 여기서 터지면 영상 중계가 같이 죽는다."""

    async def _boom(message):
        raise RuntimeError("대시보드가 통째로 죽었다")

    monkeypatch.setattr(camera.manager, "broadcast", _boom)

    frames = await _drain(_link([_fake_frame(1)]))

    assert len(frames) == 1, "시각 중계가 터지면서 영상까지 끊겼다"


def test_줄바꿈이_LF뿐인_프레임에도_붙는다():
    """상류 구현에 따라 `\\n\\n`으로 헤더를 닫는다. 그 판에서 조용히 안 붙으면 안 된다."""
    lf_frame = b"--frameboundary\nContent-Type: image/jpeg\n\n\xff\xd8body\xff\xd9"
    at = dt.datetime.now(dt.timezone.utc)

    out = camera._stamp_frame(lf_frame, at)

    assert camera.FRAME_TIME_HEADER + b": " in out
    assert out.endswith(b"\xff\xd8body\xff\xd9"), out


@pytest.mark.parametrize(
    ("content_type", "want"),
    [
        (_MJPEG_CT, b"--frameboundary"),
        ('multipart/x-mixed-replace; boundary="quoted"', b"--quoted"),
        ("multipart/x-mixed-replace", None),  # boundary 파라미터가 없다
        ("text/html", None),
        (None, None),
    ],
)
async def test_boundary_marker_parses_content_type(content_type, want):
    assert camera._boundary_marker(content_type) == want


async def test_slow_client_gets_newest_frame_not_the_oldest():
    """⭐ 느린 화면은 **최신 장**을 받는다. 이게 이 기능의 존재 이유다."""
    chunks = [b"".join(_fake_frame(i) for i in range(10)) + _BOUND]
    link = _link(chunks)

    got = _frame_numbers(await asyncio.wait_for(_drain(link, pause=0.05), timeout=10))

    assert got, "한 장도 못 받았다"
    assert got[-1] == 9, f"옛 장을 받았다 — 받은 번호 {got}"
    assert link._dropped >= 8, f"밀린 장을 안 버렸다 — 버린 수 {link._dropped}"


async def test_frames_are_not_glued_into_one_blob():
    """⚠ 한 조각에 여러 장이 실려 와도 **한 장씩** 잘라야 한다.

    맨 앞 경계를 쓰면 그 사이 장이 통째로 묶여 나가서, 화면이 옛 장면부터 차례로 본다
    (=실시간이 아니다). 2026-08-05 프로브가 이 자리를 잡았다.
    """
    chunks = [b"".join(_fake_frame(i) for i in range(5)) + _BOUND]
    link = _link(chunks)

    got = await asyncio.wait_for(_drain(link, pause=0.05), timeout=10)

    assert got, "한 장도 못 받았다"
    for frame in got:
        assert frame.count(_BOUND) == 1, "여러 장이 한 덩어리로 묶여 나갔다"


async def test_unknown_boundary_falls_back_to_raw_passthrough():
    """경계를 못 뽑으면 조각을 그대로 흘린다 — 반쪽 JPEG를 내보내느니 밀리는 쪽이 낫다."""
    link = _link([b"raw-1", b"raw-2"], content_type="text/html")

    assert await asyncio.wait_for(_drain(link), timeout=10) == [b"raw-1", b"raw-2"]


async def test_switch_off_restores_old_passthrough(monkeypatch):
    """`camera_realtime_drop`을 끄면 예전 거동 그대로다(되돌릴 자리)."""
    real = camera._setting
    monkeypatch.setattr(
        camera,
        "_setting",
        lambda name, default: False if name == "camera_realtime_drop" else real(name, default),
    )
    link = _link([_fake_frame(0), _fake_frame(1) + _BOUND])

    got = await asyncio.wait_for(_drain(link), timeout=10)

    assert len(got) == 2, "조각을 그대로 안 흘렸다 — 스위치가 안 먹는다"


async def test_stream_ends_when_upstream_ends():
    """⚠ 상류가 조용히 끝나도 스트림이 닫혀야 한다. 안 닫히면 자리가 영영 물린다."""
    link = _link([_fake_frame(0) + _BOUND])

    got = await asyncio.wait_for(_drain(link), timeout=5)

    assert len(got) == 1
