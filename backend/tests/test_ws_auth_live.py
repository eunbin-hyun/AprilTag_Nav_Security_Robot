"""WS 인증을 **실 uvicorn 전선**에서 확인한다 (S15P11C207-238).

왜 파일이 따로 있나.
  starlette TestClient는 ASGI 앱을 그대로 부르는 통이라, 전선에서만 드러나는 것들을 못 본다.
  실제로 44개가 전부 초록인 채로 아래 셋이 다 틀려 있었다(2026-07-31 실측).
    1. 거절 코드 — accept 전 `close(1008)`은 ASGI 규약상 핸드셰이크 거절이라 전선에는
       `HTTP 403`만 나갔다. TestClient는 그 자리에서 1008을 합성해 돌려줘 시험만 통과했다.
    2. 헤더 인코딩 — 전선 바이트를 starlette이 latin-1로 올리는 탓에 UTF-8로 실린 한글 키가
       헤더 창구에서만 잘렸다. TestClient로 str 헤더를 주면 이 경로를 안 지난다.
    3. 헤더 공백 — HTTP 파서가 값 둘레 공백을 떼는 건 진짜 서버에서만 일어난다.

그래서 여기서는 진짜 uvicorn을 띄우고 `websockets` 클라이언트로 붙는다.
  - 서버는 서브프로세스다. env 조합마다 하나씩 띄우고 세션 끝에 전부 내린다(모듈 finalizer).
  - 포트는 0번 바인딩으로 빈 자리를 받는다. ⚠ 그 소켓을 닫고 uvicorn이 다시 묶는 사이는
    예약이 아니라 **빈 창**이라, 같은 순간 남이 그 번호를 집어 갈 수 있다(TOCTOU). 그래서
    "안 겹친다"가 아니라 **겹치면 새 포트로 몇 번 다시 띄운다**로 받친다(_server의 재시도).
  - DB를 안 탄다. 핸드셰이크에서 끝나는 경로만 찌른다.
  - asyncio.run은 안 부른다(`websockets.sync`가 자기 스레드에서 돈다) — MainThread 루프를
    지우면 뒤따르는 async 시험이 통째로 깨진다.
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
import socket
import subprocess
import sys
import time

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import connect

from app import auth
from app.config import INSECURE_DEFAULT_API_KEY
from app.main import WS_UNAUTHORIZED_CODE
from tests.conftest import _run_in_new_loop

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API_KEY = "live-device-key"
DASH_KEY = "live-dashboard-key"
NON_ASCII_KEY = "열쇠키한글"
SPACED_KEY = " sp key "

# 띄워 둔 서버를 env 조합으로 재사용한다. 조합 하나에 기동 1~3초라 매번 띄우면 배치가 는다.
_SERVERS: dict[frozenset, int] = {}
_PROCS: list[subprocess.Popen] = []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_PORT_ATTEMPTS = 3


def _spawn(port: int, env_extra: dict[str, str]) -> subprocess.Popen:
    env = dict(os.environ)
    # 시험 프로세스가 잡아 둔 시험 DB를 그대로 물려준다(핸드셰이크는 DB를 안 타지만,
    # 혹시 뒤에 케이스가 늘어도 운영 DSN이 새지 않게).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.update(env_extra)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _PROCS.append(proc)
    return proc


def _server(**env_extra: str) -> int:
    """이 env로 뜬 uvicorn의 포트. 없으면 띄운다.

    ⚠ 0번 바인딩으로 받은 포트는 **예약이 아니다.** 소켓을 닫고 uvicorn이 다시 묶는 사이에
      남(다른 xdist 워커·같은 기계의 딴 프로세스)이 그 번호를 집어 가면 uvicorn이 곧장
      죽는다. 그 자리를 시험 실패로 두면 코드가 멀쩡한데 배치가 빨개진다. 그래서 기동 실패는
      새 포트로 몇 번 다시 띄우고, 그래도 안 되면 그때 마지막 출력을 그대로 올린다.
    """
    key = frozenset(env_extra.items())
    if key in _SERVERS:
        return _SERVERS[key]

    last = ""
    for attempt in range(_PORT_ATTEMPTS):
        port = _free_port()
        proc = _spawn(port, dict(env_extra))

        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                last = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                    _SERVERS[key] = port
                    return port
            except OSError:
                time.sleep(0.2)
        else:
            # 안 죽었는데 시간이 다 됐다. 포트 충돌이 아니라 기동 자체가 느리거나 막힌 것이라
            # 다시 띄워도 같은 자리에서 걸린다.
            raise RuntimeError(f"uvicorn이 시간 안에 안 떴다(포트 {port})")

        if attempt + 1 < _PORT_ATTEMPTS:
            time.sleep(0.3)

    raise RuntimeError(
        f"uvicorn이 포트 {_PORT_ATTEMPTS}번을 바꿔 가며 다 뜨다 죽었다: {last[-2000:]}"
    )


def teardown_module(module) -> None:  # noqa: ARG001 - pytest 훅 서명
    """띄운 서버를 전부 내린다. 남기면 다음 배치가 포트·CPU를 물고 시작한다."""
    for proc in _PROCS:
        proc.terminate()
    for proc in _PROCS:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    _PROCS.clear()
    _SERVERS.clear()


def _probe(port: int, path: str, headers: dict | None = None, query: str = "") -> tuple[str, object]:
    """붙어서 첫 메시지까지. ("OPEN", type) / ("CLOSED", 닫기코드) / ("REJECT", HTTP 상태)."""
    url = f"ws://127.0.0.1:{port}{path}{query}"
    try:
        with connect(url, additional_headers=headers or {}, open_timeout=10) as ws:
            frame = ws.recv(timeout=10)
    except InvalidStatus as exc:
        # 핸드셰이크 자체가 잘린 경우. 여기 오면 close 코드는 전선에 안 실린 것이다.
        return "REJECT", exc.response.status_code
    except ConnectionClosed as exc:
        return "CLOSED", exc.rcvd.code if exc.rcvd else None
    import json

    return "OPEN", json.loads(frame)["type"]


def _assert_open(port: int, path: str, expected_type: str, **kw) -> None:
    assert _probe(port, path, **kw) == ("OPEN", expected_type)


def _assert_closed(port: int, path: str, **kw) -> None:
    """거절은 전선 위 1008이어야 한다. HTTP 403이면 코드가 안 실린 것이라 실패다."""
    assert _probe(port, path, **kw) == ("CLOSED", WS_UNAUTHORIZED_CODE)


# ── 1. 거절 코드가 전선에 실제로 나간다 ──────────────────────────────────────


def test_anonymous_close_code_is_on_the_wire():
    port = _server(API_KEY=API_KEY, WS_REQUIRE_API_KEY_ROBOT="true")
    _assert_closed(port, "/ws/robot")


def test_wrong_key_close_code_is_on_the_wire():
    port = _server(API_KEY=API_KEY, WS_REQUIRE_API_KEY_ROBOT="true")
    _assert_closed(port, "/ws/robot", headers={"x-api-key": "nope"})


# ── 2. 창구 셋이 전선에서도 동급이다 ─────────────────────────────────────────


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"headers": {"x-api-key": API_KEY}}, id="x-api-key"),
        pytest.param({"headers": {"authorization": f"Bearer {API_KEY}"}}, id="bearer"),
        pytest.param({"query": f"?api_key={API_KEY}"}, id="쿼리"),
    ],
)
def test_every_key_channel_opens_on_the_wire(kw):
    port = _server(API_KEY=API_KEY, WS_REQUIRE_API_KEY_ROBOT="true")
    _assert_open(port, "/ws/robot", "registered", **kw)


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"headers": {"x-api-key": NON_ASCII_KEY}}, id="x-api-key"),
        pytest.param({"headers": {"authorization": f"Bearer {NON_ASCII_KEY}"}}, id="bearer"),
        # 퍼센트 인코딩된 UTF-8. 브라우저·requests가 쿼리에 싣는 모습 그대로다.
        pytest.param(
            {"query": "?api_key=%EC%97%B4%EC%87%A0%ED%82%A4%ED%95%9C%EA%B8%80"}, id="쿼리"
        ),
    ],
)
def test_non_ascii_key_opens_on_every_channel(kw):
    """한글 키. 수리 전에는 쿼리만 통과하고 헤더 둘이 403이었다 — 젯슨이 통째로 잠겼다."""
    port = _server(API_KEY=NON_ASCII_KEY, WS_REQUIRE_API_KEY_ROBOT="true")
    _assert_open(port, "/ws/robot", "registered", **kw)


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"headers": {"x-api-key": SPACED_KEY}}, id="x-api-key"),
        pytest.param({"headers": {"authorization": f"Bearer {SPACED_KEY}"}}, id="bearer"),
        pytest.param({"query": "?api_key=%20sp%20key%20"}, id="쿼리"),
    ],
)
def test_spaced_key_opens_on_every_channel(kw):
    """둘레 공백이 낀 키. 수리 전에는 쿼리만 통과했다(헤더 파서가 공백을 떼기 때문)."""
    port = _server(API_KEY=SPACED_KEY, WS_REQUIRE_API_KEY_ROBOT="true")
    _assert_open(port, "/ws/robot", "registered", **kw)


# ── 3. 못 쓰는 자물쇠 · 키 분리 ──────────────────────────────────────────────


def test_repo_default_key_does_not_open_the_wire():
    """API_KEY를 안 넣고 스위치만 켠 배포. 수리 전에는 저장소 기본키로 OPEN이었다."""
    port = _server(WS_REQUIRE_API_KEY_ROBOT="true")
    _assert_closed(port, "/ws/robot", headers={"x-api-key": INSECURE_DEFAULT_API_KEY})


def test_dashboard_rejects_device_key_on_the_wire():
    """대시보드는 기기 키를 안 받는다. 수리 전에는 기기 키 쿼리로 화면이 열렸다."""
    port = _server(
        API_KEY=API_KEY, DASHBOARD_API_KEY=DASH_KEY, WS_REQUIRE_API_KEY_DASHBOARD="true"
    )
    _assert_closed(port, "/ws/dashboard", query=f"?api_key={API_KEY}")
    _assert_open(port, "/ws/dashboard", "hello", query=f"?api_key={DASH_KEY}")
    # 같은 서버에서 로봇 채널은 스위치가 꺼져 있어 예전처럼 익명으로 열린다(채널 독립).
    _assert_open(port, "/ws/robot", "registered")


def test_dashboard_key_missing_closes_on_the_wire():
    port = _server(API_KEY=API_KEY, WS_REQUIRE_API_KEY_DASHBOARD="true")
    _assert_closed(port, "/ws/dashboard", query=f"?api_key={API_KEY}")
    _assert_closed(port, "/ws/dashboard")


# ── 4. 기본값 배포는 예전 그대로 ─────────────────────────────────────────────


def test_default_deployment_stays_anonymous_on_the_wire():
    """플래그를 안 켠 서버. EC2가 지금 도는 모습이고, 이게 깨지면 배포가 즉시 죽는다."""
    port = _server(API_KEY=API_KEY)
    _assert_open(port, "/ws/robot", "registered")
    _assert_open(port, "/ws/dashboard", "hello")


# ── 5. 로그인을 켠 서버 — 세션 쿠키 양성 경로 (2026-08-02 백지 검토 F66) ─────
#
# ⚠ 여기 오기 전에는 "세션 쿠키로 WS가 열린다"를 **판정 함수 직접 호출**로만 재고 있었다
#   (`test_auth_gates.test_ws_dashboard_allows_session_cookie` → `ws_dashboard_allowed`).
#   그 방식은 함수의 계약은 재지만 **라우트를 한 번도 안 탄다** — main.py가 그 함수를 안 부르게
#   바뀌거나, 판정 뒤 `manager.connect`·hello 발신 사이가 깨지거나, 쿠키가 핸드셰이크 헤더에서
#   scope로 안 올라오면 전부 초록인 채로 화면이 통째로 못 붙는다. 이 파일 머리에 적힌
#   "44개가 전부 초록인 채로 셋이 다 틀려 있었다"와 같은 계열이다.
#
# 그래서 실 uvicorn에 진짜 쿠키를 실어 붙는다. DB는 시험 프로세스가 잡은 시험 DB 그대로를
# 서브프로세스가 물려받으므로(_server가 os.environ을 통째로 넘긴다), 여기서 심은 세션 행을
# 서버가 그대로 읽는다.


def _asyncpg_dsn() -> str:
    """지금 시험 DB의 asyncpg DSN. asyncpg는 `+asyncpg` 접미사를 모른다."""
    return os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


def _plant_live_session(username: str, role: str = auth.ROLE_AGENT) -> str:
    """계정 한 장과 살아 있는 세션 한 장을 심고 **원문 토큰**을 돌려준다.

    ⚠ SQLAlchemy 엔진(`app.db.engine`)을 안 쓴다. 그 엔진은 세션 이벤트 루프에 묶여 있어서
      여기(동기 시험)에서 쓰면 커넥션 풀이 두 루프에 걸쳐 갈라진다. 그리고 MainThread에서
      `asyncio.run`을 부르면 뒤따르는 async 시험이 통째로 깨진다(파일 머리 참고). 그래서
      conftest의 `_run_in_new_loop`(별 스레드 안 asyncio.run)에 asyncpg 한 줄만 태운다.

    비밀번호 해시는 아무 값이나 둔다 — 이 갈래는 로그인 창구를 안 타고 세션 토큰만 쓴다.
    """
    token = secrets.token_urlsafe(32)

    async def _insert() -> None:
        import asyncpg

        conn = await asyncpg.connect(_asyncpg_dsn())
        try:
            user_id = await conn.fetchval(
                "INSERT INTO app_user (username, password_hash, display_name, role, is_active)"
                " VALUES ($1, $2, $3, $4, true) RETURNING id",
                username, "not-used-in-this-branch", f"이름-{username}", role,
            )
            now = dt.datetime.now(dt.timezone.utc)
            await conn.execute(
                "INSERT INTO auth_session"
                " (token_hash, user_id, created_at, expires_at, last_seen_at)"
                " VALUES ($1, $2, $3, $4, $5)",
                auth.token_digest(token), user_id, now,
                now + dt.timedelta(hours=12), now,
            )
        finally:
            await conn.close()

    _run_in_new_loop(_insert)
    return token


def _cookie_headers(token: str) -> dict[str, str]:
    """브라우저가 같은 오리진 핸드셰이크에 자동으로 싣는 모습 그대로."""
    return {"cookie": f"{auth.session_cookie_name()}={token}"}


def test_session_cookie_opens_dashboard_on_the_wire():
    """살아 있는 요원 세션 쿠키면 실 전선에서 `/ws/dashboard`가 열린다."""
    port = _server(API_KEY=API_KEY, AUTH_REQUIRE_LOGIN="true")
    token = _plant_live_session("wslive1")
    _assert_open(port, "/ws/dashboard", "hello", headers=_cookie_headers(token))


def test_flag_on_closes_dashboard_without_session_on_the_wire():
    """켠 뒤 익명·가짜 토큰은 전선 위 1008이다.

    양성만 두면 "판정이 아예 안 도는 판"도 초록이라, 같은 서버에서 음성 둘을 나란히 본다.
    """
    port = _server(API_KEY=API_KEY, AUTH_REQUIRE_LOGIN="true")
    _assert_closed(port, "/ws/dashboard")
    _assert_closed(port, "/ws/dashboard", headers=_cookie_headers("no-such-token"))
    # 켠 뒤에도 기기 키는 사람 자리를 못 메운다(§5.1 — 폴백을 안 남긴다).
    _assert_closed(port, "/ws/dashboard", query=f"?api_key={API_KEY}")


def test_flag_on_keeps_robot_channel_on_device_key_on_the_wire():
    """로봇 채널은 로그인이 안 건드린다 — 젯슨은 세션을 못 쥔다(§5.2)."""
    port = _server(API_KEY=API_KEY, AUTH_REQUIRE_LOGIN="true")
    _assert_open(port, "/ws/robot", "registered")
