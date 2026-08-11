"""로그인 창구 셋 — `POST /api/auth/login` · `logout` · `GET /api/auth/me`.

계약은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §7.1이다. 응답 코드·본문 문장·
쿠키 속성까지 그 문서에 적힌 그대로를 잰다 — 프론트(-350·-351)가 이 모양에 붙는다.

## 여기서 재는 것 넷

1. **계정 열거 방지** — 아이디가 없을 때와 비밀번호가 틀렸을 때가 **같은 401·같은 문장**이다.
2. **쿠키 속성 넷** — HttpOnly·Secure·SameSite=Lax·Path=/. 하나라도 빠지면 토큰이 새거나
   WS 핸드셰이크에 안 실린다.
3. **잠금** — 계정당 5회 실패면 429 + Retry-After.
4. **멱등 로그아웃** — 세션이 없어도 204다. 화면이 상태를 몰라도 그냥 부르면 된다.

⚠ 이 창구 셋은 `AUTH_REQUIRE_LOGIN`과 **무관하게** 산다(§6.2 3단계). 그래서 이 파일은
플래그를 안 건드리고 기본값(false) 그대로 돈다 — 켜기 전 구간에서도 돈다는 게 계약이다.

⚠ 계정·세션 테이블 비우기와 실패 잠금 초기화는 conftest `_clean` 하나가 한다. 특히 이
파일은 5회 실패 잠금을 일부러 만드는 케이스가 있어서, 그 훅이 안 돌면 뒤 케이스가 429로
깨진다 — 그래서 이 파일이 통과하는 게 훅이 도는 직접 증거다(2026-08-02 교차 검토).
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app import auth
from app.config import get_settings
from app.db import get_session
from app.models import AppUser, AuthSession, StaffAudit

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_T = dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
_PASSWORD = "bi-mil-1234"
_BAD_CREDENTIALS = "아이디 또는 비밀번호가 올바르지 않습니다."


@pytest.fixture
def clock(monkeypatch):
    holder = {"t": _BASE_T}
    monkeypatch.setattr(auth, "_now", lambda: holder["t"])
    return holder


@pytest.fixture
def http_cookie(monkeypatch):
    """`SESSION_COOKIE_SECURE=false` 구간.

    시험 클라이언트는 `http://test`로 붙는다. Secure 쿠키는 http 응답에서 브라우저·쿠키
    보관함이 **저장 자체를 안 해서**, 보관함을 타는 케이스는 이 스위치를 꺼야 잰다.
    배포 기본값(true)에서 헤더가 제대로 나가는지는 `test_login_cookie_attributes`가 본다.
    """
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _make_user(
    username: str, role: str = auth.ROLE_ADMIN, password: str = _PASSWORD, **values
) -> AppUser:
    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password(password),
            display_name="손세욱",
            role=role,
            **values,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _cookie_header(token: str, name: str | None = None) -> dict[str, str]:
    """토큰을 쿠키 헤더로 직접 싣는다(보관함 규칙에 안 기대는 결정적 경로).

    이름을 안 주면 **지금 설정이 심는 이름**을 쓴다 — 배포 기본값(Secure)에서는
    `__Host-c207_session`이다(F20).
    """
    return {"Cookie": f"{name or auth.session_cookie_name()}={token}"}


def _token_from(response) -> str:
    """Set-Cookie 헤더에서 토큰 원문만 뽑는다."""
    raw = response.headers["set-cookie"]
    assert raw.startswith(f"{auth.session_cookie_name()}=")
    return raw.split(";", 1)[0].split("=", 1)[1]


def _parse_set_cookie(raw: str) -> tuple[str, str, dict[str, str]]:
    """`Set-Cookie` 한 줄을 (이름, 값, 속성표)로 가른다.

    ⚠ 왜 파서가 필요한가 (2026-08-02 백지 검토 F71) — 예전 판은 헤더 전체를 소문자로 눌러
    `"secure" in lowered` 식으로 봤다. 그 방식은 **"있어야 할 것"만 보고 "없어야 할 것"은
    못 본다.** `Domain=.p.ssafy.io`가 덧붙어도 `"domain=" not in lowered` 하나로만 걸리고,
    `SameSite=None`이 들어와도 `"samesite=lax"`가 어딘가에 있으면 그냥 통과한다. 값 안에
    우연히 같은 글자가 섞이는 길도 열려 있다(토큰은 base64url이라 실제로 그럴 수 있다).

    속성 이름은 대소문자를 안 가리므로 소문자로 접고, 값은 그대로 둔다(비교는 부르는 쪽에서).
    플래그 속성(HttpOnly·Secure)은 값이 빈 문자열로 들어간다.
    """
    first, *rest = (part.strip() for part in raw.split(";"))
    name, _, value = first.partition("=")
    attrs: dict[str, str] = {}
    for part in rest:
        if not part:
            continue
        key, sep, val = part.partition("=")
        attrs[key.strip().lower()] = val.strip() if sep else ""
    return name, value, attrs


# ── 1. 로그인 성공 ─────────────────────────────────────────────────────────

async def test_login_success_returns_identity_and_sets_cookie(client, clock):
    await _make_user("sonseuk", role=auth.ROLE_ADMIN)
    r = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )

    assert r.status_code == 200
    body = r.json()
    # ⚠ `id`는 값을 못 박는다 — 시퀀스라 시험 순서에 따라 바뀐다. **있고 int인지**만 재고
    #    나머지 칸은 통째로 비교한다(칸이 하나 늘거나 줄면 여기서 잡힌다).
    #    `id`는 명부 `app_user_id`와 대조하려고 2026-08-05에 넣었다(프론트 1차 A-2).
    assert isinstance(body.pop("id", None), int), "계정 id가 안 실렸다"
    assert body == {
        "username": "sonseuk",
        "display_name": "손세욱",
        "role": "admin",
    }
    # 해시가 어떤 응답에도 안 실린다(§3).
    assert "password" not in r.text and "scrypt" not in r.text

    token = _token_from(r)
    async with get_session() as session:
        row = (await session.execute(select(AuthSession))).scalars().one()
    assert row.token_hash == auth.token_digest(token), "DB엔 sha256만 남아야 한다"
    assert row.expires_at == _BASE_T + dt.timedelta(hours=12)


async def test_login_cookie_attributes(client, clock):
    """쿠키 속성 넷. 배포 기본값(Secure 켜짐) 그대로 잰다.

    - HttpOnly가 빠지면 JS가 토큰을 읽는다.
    - Secure가 빠지면 http로 토큰이 나간다.
    - SameSite=Lax가 빠지면 교차 사이트 위조 방어가 사라진다.
    - Path=/가 아니면 WS 핸드셰이크(`/ws/dashboard`)에 안 실린다.

    이름에 `__Host-` 접두가 붙는 것도 여기서 같이 잰다 — 그 접두가 성립하는 조건이 위 셋과
    같아서(Secure·Path=/·Domain 없음) 한 자리에서 봐야 한다(F20).

    ⭐ 속성을 **파싱해서** 있어야 할 것과 없어야 할 것을 둘 다 잰다(F71). 헤더 전체 부분문자열
    대조는 덧붙은 속성을 못 본다 — 그 자리가 `__Host-` 접두를 깨는 정확한 갈래다.
    """
    assert get_settings().session_cookie_secure is True, "배포 기본값이 바뀌었다"
    await _make_user("sonseuk")
    r = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    name, value, attrs = _parse_set_cookie(r.headers["set-cookie"])

    assert name == auth.SESSION_COOKIE_NAME_SECURE, (
        "https 배포인데 __Host- 접두가 없다 — 형제 팀 호스트가 상위 도메인 쿠키로 세션을 고정할 수 있다"
    )
    assert value, "토큰이 안 실렸다"

    # ── 있어야 할 것 ──────────────────────────────────────────────────────
    assert "httponly" in attrs, "HttpOnly가 없다 — JS가 토큰을 읽는다"
    assert "secure" in attrs, "Secure가 없다 — http로 토큰이 나간다"
    assert attrs.get("samesite", "").lower() == "lax", (
        f"SameSite가 Lax가 아니다: {attrs.get('samesite')!r}"
    )
    assert attrs.get("path") == "/", (
        f"Path가 /가 아니다: {attrs.get('path')!r} — WS 핸드셰이크에 쿠키가 안 실린다"
    )
    assert attrs.get("max-age") == "43200", (
        f"브라우저 수명이 서버 절대 만료와 달라졌다: {attrs.get('max-age')!r}"
    )

    # ── 없어야 할 것 (`__Host-` 접두의 필수 조건) ─────────────────────────
    assert "domain" not in attrs, (
        "__Host- 접두 쿠키에 Domain이 붙었다 — 브라우저가 저장을 통째로 거절한다"
    )
    assert attrs.get("samesite", "").lower() != "none", (
        "SameSite=None이면 교차 사이트 위조 방어가 사라진다"
    )


async def test_cookie_name_falls_back_to_plain_on_http(client, http_cookie, clock):
    """http 로컬 개발에서는 옛 이름 그대로다. `__Host-`는 Secure가 필수라 못 쓴다(F20)."""
    assert get_settings().session_cookie_secure is False
    await _make_user("sonseuk")
    r = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    raw = r.headers["set-cookie"]
    assert raw.startswith("c207_session="), raw
    assert "__host-" not in raw.lower(), (
        "http인데 __Host- 이름을 심었다 — 브라우저가 저장을 거부해 로그인이 매번 튕긴다"
    )


async def test_legacy_cookie_name_is_still_accepted(client, clock):
    """읽을 때는 **두 이름을 다** 받는다.

    이름을 바꾸는 배포가 나가는 순간, 이미 붙어 있던 사람들의 브라우저에는 옛 이름 쿠키만
    있다. 새 이름만 읽으면 그 자리에서 전원이 로그아웃된다(F20 롤아웃 조건).
    """
    await _make_user("sonseuk")
    login = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    token = _token_from(login)

    old = await client.get(
        "/api/auth/me", headers=_cookie_header(token, name=auth.SESSION_COOKIE_NAME)
    )
    assert old.status_code == 200, "옛 이름 쿠키가 무시됐다 — 롤아웃에서 전원 로그아웃이다"
    assert old.json()["username"] == "sonseuk"


async def test_logout_clears_both_cookie_names(client, clock):
    """로그아웃은 두 이름을 다 지운다. 한쪽이 남으면 화면이 로그인 상태로 착각한다(F20)."""
    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    names = {raw.split("=", 1)[0] for raw in r.headers.get_list("set-cookie")}
    assert names == {auth.SESSION_COOKIE_NAME, auth.SESSION_COOKIE_NAME_SECURE}, names


async def test_login_accepts_uppercase_username(client, clock):
    """대소문자를 접어 받는다 — 아이디 규칙이 소문자라 접어도 남의 계정에 안 닿는다."""
    await _make_user("sonseuk")
    r = await client.post(
        "/api/auth/login", json={"username": " SonSeuk ", "password": _PASSWORD}
    )
    assert r.status_code == 200


# ── 2. 로그인 실패 — 계정 열거 방지 (§2.6·§7.1) ────────────────────────────

async def test_wrong_password_and_unknown_user_look_identical(client, clock):
    """둘을 구분하지 않는다. 구분하면 그게 곧 계정 열거다."""
    await _make_user("sonseuk")

    wrong = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": "틀린비번"}
    )
    missing = await client.post(
        "/api/auth/login", json={"username": "nobody", "password": _PASSWORD}
    )

    assert wrong.status_code == missing.status_code == 401
    assert wrong.json() == missing.json() == {"detail": _BAD_CREDENTIALS}
    assert "set-cookie" not in wrong.headers
    assert "set-cookie" not in missing.headers


async def test_unknown_user_branch_still_verifies_a_dummy_hash(client, clock, monkeypatch):
    """없는 아이디 갈래도 **해시 검증을 한 번 하고 간다**(계정 열거 방지, 2026-08-02 F64).

    안 하면 응답 시간이 "그 아이디는 있다/없다"를 그대로 알려 준다 — 있는 아이디는 scrypt
    16MiB 한 번(수백 밀리초)이고 없는 아이디는 곧장 401이라, 시계 하나면 명부가 통째로 샌다.

    ⚠ **시간을 안 잰다.** 벽시계 비교는 배치 부하에서 흔들려 가짜 실패를 만든다(팀원 여럿이
      동시에 pytest를 돌리는 이 저장소에서 이미 겪은 계열이다). 대신 "없는 아이디 갈래에서도
      해시 검증이 불렸나"를 직접 센다 — 방어를 지우면 호출이 0이 되므로 결정적으로 빨개진다.

    같이 재는 것 하나 더. 넘긴 저장값이 **더미 해시**여야 한다. 아무 문자열이나 넘기면
    `verify_password`가 형식 파싱에서 곧장 떨어져 scrypt를 안 돌고, 그러면 호출은 있는데
    걸리는 시간이 없어서 오라클이 그대로 살아 있다.
    """
    seen: list[str] = []
    real_verify = auth.verify_password_async

    async def _spy(password: str, stored: str) -> bool:
        seen.append(stored)
        return await real_verify(password, stored)

    monkeypatch.setattr(auth, "verify_password_async", _spy)

    r = await client.post(
        "/api/auth/login", json={"username": "nobody", "password": _PASSWORD}
    )
    assert r.status_code == 401
    assert len(seen) == 1, (
        f"없는 아이디 갈래가 해시 검증을 {len(seen)}번 했다 — 0이면 계정 열거 방어가 지워진 것이다"
    )
    assert seen[0] == auth.dummy_password_hash(), (
        "없는 아이디 갈래가 더미 해시가 아닌 값으로 검증했다 — 그 값이 scrypt 형식이 아니면 "
        "파싱에서 곧장 떨어져 응답 시간이 다시 갈린다"
    )
    assert seen[0].startswith("scrypt$"), "더미 해시가 진짜 scrypt 저장 형식이 아니다"

    # 있는 아이디 갈래는 그 계정의 저장 해시로 같은 일을 한다 — 두 갈래가 같은 양의 일을
    # 한다는 게 이 방어의 뜻이다.
    user = await _make_user("sonseuk")
    seen.clear()
    wrong = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": "틀린비번"}
    )
    assert wrong.status_code == 401
    assert seen == [user.password_hash]


async def test_deactivated_account_cannot_log_in(client, clock):
    """해제된 계정은 비밀번호가 맞아도 못 들어온다. 응답은 역시 같은 401이다."""
    await _make_user("gone", is_active=False)
    r = await client.post(
        "/api/auth/login", json={"username": "gone", "password": _PASSWORD}
    )
    assert r.status_code == 401
    assert r.json() == {"detail": _BAD_CREDENTIALS}
    # 한 번으로는 안 잠긴다(상한이 5회다). 이 갈래도 실패로 센다는 건 아래 오라클 시험이 잰다.
    assert auth.login_lock_remaining("gone") == 0.0


async def test_deactivated_account_counts_failures_too(client, clock):
    """해제된 계정 갈래도 **실패로 센다**(F14 — 타이밍 오라클 차단).

    안 세면 이 창구가 상태 코드로 답을 흘린다. 비밀번호가 틀리면 세고 맞으면 안 세니까,
    공격자는 후보 하나를 넣고 **다음 요청이 429로 갈리는지**만 보면 그 후보의 정오를
    그대로 읽는다. 여기서는 맞는 비밀번호만 5번 넣어 잠기는지를 본다 — 안 세던 판에서는
    영원히 401이라 이 시험이 빨개진다.
    """
    await _make_user("gone", is_active=False)
    for _ in range(5):
        r = await client.post(
            "/api/auth/login", json={"username": "gone", "password": _PASSWORD}
        )
        assert r.status_code == 401, "해제 갈래의 응답 코드가 바뀌었다"

    assert auth.login_lock_remaining("gone") > 0, (
        "맞는 비밀번호를 5번 넣어도 안 잠긴다 — 429/401 갈림이 비밀번호 정오를 알려 준다"
    )
    locked = await client.post(
        "/api/auth/login", json={"username": "gone", "password": "틀린비번"}
    )
    assert locked.status_code == 429


async def test_login_locks_account_after_five_failures(client, clock):
    """계정당 5회 실패면 429 + Retry-After. 여섯 번째는 비밀번호가 맞아도 안 들어간다."""
    await _make_user("sonseuk")
    for _ in range(5):
        r = await client.post(
            "/api/auth/login", json={"username": "sonseuk", "password": "틀린비번"}
        )
        assert r.status_code == 401

    locked = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    assert locked.status_code == 429
    assert locked.json() == {"detail": "잠시 후 다시 시도해 주세요."}
    assert int(locked.headers["retry-after"]) >= 1

    # 60초가 지나면 풀린다.
    clock["t"] = _BASE_T + dt.timedelta(seconds=61)
    ok = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    assert ok.status_code == 200


async def test_login_rejects_empty_and_oversized_fields(client, clock):
    """길이만 막는다. 아이디 **모양**은 안 막는다 — 422로 갈리면 그게 형식 열거다.

    ⚠ 이름이 oversized를 약속하는데 본문에 그 단언이 없었다(2026-08-02 X116). 상한은
    아이디 32자·비밀번호 200자(`schemas.LoginIn`)이고, 상한이 조용히 풀리면 임의로 긴
    입력이 scrypt 16MiB를 태우는 자리가 열린다.
    """
    empty = await client.post("/api/auth/login", json={"username": "", "password": "x"})
    assert empty.status_code == 422

    weird = await client.post(
        "/api/auth/login", json={"username": "손세욱!!", "password": _PASSWORD}
    )
    assert weird.status_code == 401, "규칙 밖 아이디도 422가 아니라 같은 401이어야 한다"

    # 아이디 33자 — DB 칸 폭(32)을 한 글자 넘겼다.
    long_username = await client.post(
        "/api/auth/login", json={"username": "s" * 33, "password": _PASSWORD}
    )
    assert long_username.status_code == 422, (
        "33자 아이디가 검증을 지났다 — DB 칸 폭을 넘겨 조회가 어긋난다"
    )
    assert "set-cookie" not in long_username.headers

    # 비밀번호 201자 — 상한(200)을 한 글자 넘겼다.
    oversized = "p" * 201
    long_password = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": oversized}
    )
    assert long_password.status_code == 422, (
        "201자 비밀번호가 검증을 지났다 — 임의로 긴 입력이 scrypt를 태운다"
    )


async def test_oversized_password_error_does_not_echo_the_secret(client, clock):
    """⭐ 422 본문에 **비밀번호 원문이 안 실린다**(`main._scrub_validation_error`).

    pydantic 오류 dict는 실패한 값을 `input`에 담고 FastAPI 기본 처리기가 그대로 응답에
    싣는다. 그래서 상한을 넘긴 비밀번호로 로그인을 때리면 원문이 통째로 돌아왔다. 응답은
    nginx 액세스 로그·브라우저 개발자 도구·에러 리포터에 남는 자리라 한 번 새면 여러 곳에
    굳는다. 그 마스킹이 회귀로 사라지지 않게 여기서 잰다.

    같이 재는 것 하나 더 — `loc`·`msg`·`type`은 **남아 있어야 한다**. 방어를 얹느라 화면이
    "어느 칸이 왜 틀렸나"를 못 읽으면 그건 고친 게 아니다.
    """
    oversized = "p" * 201
    r = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": oversized}
    )
    assert r.status_code == 422

    assert oversized not in r.text, "422 응답에 비밀번호 원문이 그대로 돌아왔다"
    detail = r.json()["detail"]
    password_errors = [
        err for err in detail if "password" in [str(part) for part in err.get("loc", ())]
    ]
    assert password_errors, f"password 칸 오류가 안 실렸다: {detail}"
    for err in password_errors:
        assert err.get("input") == "***", f"비밀 칸 input이 안 지워졌다: {err.get('input')!r}"
        assert "ctx" not in err, f"ctx가 남았다 — 경계값과 함께 원문이 섞일 수 있다: {err}"
        # 화면이 읽어야 하는 정보는 그대로 남는다.
        assert err.get("msg") and err.get("type")


# ── 3. /api/auth/me ────────────────────────────────────────────────────────

async def test_me_without_session_is_401(client, clock):
    """플래그가 꺼져 있어도 세션이 없으면 401이다 — 이 창구의 뜻이 "지금 로그인돼 있나"다."""
    assert get_settings().auth_require_login is False
    r = await client.get("/api/auth/me")
    assert r.status_code == 401
    assert r.json() == {"detail": "로그인이 필요합니다."}


async def test_me_returns_identity_with_session(client, clock):
    await _make_user("leader1", role=auth.ROLE_LEADER)
    login = await client.post(
        "/api/auth/login", json={"username": "leader1", "password": _PASSWORD}
    )
    token = _token_from(login)

    r = await client.get("/api/auth/me", headers=_cookie_header(token))
    assert r.status_code == 200
    body = r.json()
    # ⚠ 위 로그인 케이스와 같은 이유로 `id`는 값이 아니라 **모양**만 잰다.
    #    ⭐ 다만 **로그인 응답과 같은 id여야 한다** — 두 창구가 다른 사람을 가리키면
    #    화면이 "이 명부 줄이 내 계정인가"를 틀리게 판정한다.
    assert body.pop("id", None) == login.json().get("id"), "로그인과 me 의 계정 id가 갈렸다"
    assert body == {
        "username": "leader1",
        "display_name": "손세욱",
        "role": "leader",
    }


async def test_me_rejects_expired_and_garbage_tokens(client, clock):
    await _make_user("sonseuk")
    login = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    token = _token_from(login)

    # ⚠ 토큰은 ASCII로 적는다. HTTP 헤더 값은 ascii로 인코드되므로 한글을 넣으면 시험이
    # 서버가 아니라 클라이언트에서 터진다(실측 — UnicodeEncodeError).
    bad = await client.get("/api/auth/me", headers=_cookie_header("no-such-token"))
    assert bad.status_code == 401

    clock["t"] = _BASE_T + dt.timedelta(hours=12, seconds=1)
    expired = await client.get("/api/auth/me", headers=_cookie_header(token))
    assert expired.status_code == 401, "절대 만료를 넘긴 토큰이 통과했다"


# ── 4. 로그아웃 ────────────────────────────────────────────────────────────

async def test_logout_revokes_session_and_clears_cookie(client, clock):
    await _make_user("sonseuk")
    login = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    token = _token_from(login)

    out = await client.post("/api/auth/logout", headers=_cookie_header(token))
    assert out.status_code == 204
    assert not out.content, "204에 본문이 실렸다"
    cleared = out.headers["set-cookie"].lower()
    assert cleared.startswith(f"{auth.SESSION_COOKIE_NAME}=")
    assert "max-age=0" in cleared or "expires=" in cleared

    # 세션은 지워지지 않고 무효화된다 — 언제 끊겼나가 감사 자료다(§1.4).
    async with get_session() as session:
        row = (await session.execute(select(AuthSession))).scalars().one()
    assert row.revoked_at == _BASE_T

    again = await client.get("/api/auth/me", headers=_cookie_header(token))
    assert again.status_code == 401, "로그아웃한 토큰이 다음 요청에서 살아 있다"


async def test_logout_is_idempotent_without_session(client, clock):
    """세션이 없어도 204다. 화면이 상태를 몰라도 그냥 부르면 된다."""
    r = await client.post("/api/auth/logout")
    assert r.status_code == 204
    assert "set-cookie" in r.headers, "쿠키 만료는 세션이 없어도 나가야 한다"


async def test_로그아웃은_무효화가_터져도_쿠키를_만료시킨다(client, clock, monkeypatch):
    """⭐ 쿠키 만료는 세션 무효화가 실패해도 **반드시** 나간다(3차 X18 수리).

    창구 docstring이 그걸 계약으로 적어 놓고도 본문은 직선이었다 — `revoke_session`은 자기
    커밋을 내므로 DB가 흔들리면 그 자리에서 500이 나가고 만료 쿠키는 안 실렸다. 브라우저에
    죽은 토큰이 남으면 화면이 계속 로그인 상태로 착각한다.

    무효화 단계를 터뜨려서 잰다. 둘 중 반드시 나가야 하는 쪽은 쿠키 만료다 — 세션 행은
    어차피 절대 만료로 죽는다.
    """
    await _make_user("sonseuk")
    login = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    token = _token_from(login)

    async def _boom(db, tok):
        raise RuntimeError("무효화 단계에서 죽었다")

    monkeypatch.setattr(auth, "revoke_session", _boom)

    out = await client.post("/api/auth/logout", headers=_cookie_header(token))
    assert out.status_code == 204, out.text
    cleared = out.headers["set-cookie"].lower()
    assert "max-age=0" in cleared or "expires=" in cleared, (
        "무효화가 터지자 만료 쿠키가 안 나갔다 — 화면이 계속 로그인 상태로 착각한다"
    )


# ── 5. 보관함을 타는 실제 흐름 ─────────────────────────────────────────────

async def test_browser_style_cookie_roundtrip(client, http_cookie, clock):
    """쿠키를 손으로 안 싣고 보관함에 맡긴 채 로그인 → me → 로그아웃 → me를 돈다.

    브라우저가 실제로 하는 일이 이것이다. 헤더를 직접 넣는 케이스만 두면 "우리가 심은
    쿠키를 우리가 읽는" 것밖에 못 재고, 서버가 내려보낸 쿠키가 다시 올라오는 왕복은 안 잰다.
    """
    await _make_user("sonseuk", role=auth.ROLE_ADMIN)

    assert (
        await client.post(
            "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
        )
    ).status_code == 200

    me = await client.get("/api/auth/me")
    assert me.status_code == 200, "보관함에 담긴 쿠키가 다음 요청에 안 실렸다"
    assert me.json()["role"] == "admin"

    assert (await client.post("/api/auth/logout")).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 401


# ── 본인 비밀번호 변경 — POST /api/auth/password (2026-08-08 설정 창) ────────
# 관리자 재설정 창구와 반대 갈래다: 지금 비밀번호를 반드시 확인하고, 급은 안 본다(본인
# 전용), 이 요청을 실어 온 세션은 남긴다. 세 계약이 각각 시험 하나씩이다.


async def test_password_change_needs_login(client, clock):
    """세션 없이 부르면 401 — 플래그와 무관하게 늘 잠긴 창구다."""
    r = await client.post(
        "/api/auth/password",
        json={"current_password": "mu-eot-이든", "new_password": "sae-bimil-12"},
    )
    assert r.status_code == 401


async def test_password_change_keeps_current_session_kills_others(client, clock):
    """204 — 옛 비밀번호가 죽고, 다른 세션은 끊기고, 이 요청의 세션은 산다."""
    user = await _make_user("pwself1", auth.ROLE_AGENT)
    async with get_session() as s:
        mine = await auth.create_session(s, user)
        other = await auth.create_session(s, user)

    r = await client.post(
        "/api/auth/password",
        json={"current_password": _PASSWORD, "new_password": "sae-bimil-77"},
        headers=_cookie_header(mine),
    )
    assert r.status_code == 204, r.text
    assert not r.content

    assert (
        await client.get("/api/auth/me", headers=_cookie_header(mine))
    ).status_code == 200, "바꾼 본인이 그 자리에서 쫓겨났다"
    assert (
        await client.get("/api/auth/me", headers=_cookie_header(other))
    ).status_code == 401, "다른 기기 세션이 살아남았다 — 바꾼 뜻이 없다"

    assert (
        await client.post(
            "/api/auth/login", json={"username": "pwself1", "password": _PASSWORD}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"username": "pwself1", "password": "sae-bimil-77"}
        )
    ).status_code == 200

    # 감사 행이 남고, 본인 변경 표시가 붙고, 비밀번호는 원문도 해시도 안 실린다.
    async with get_session() as s:
        rows = (
            await s.execute(
                select(StaffAudit).where(
                    StaffAudit.after["user_id"].astext == str(user.id)
                )
            )
        ).scalars().all()
    assert len(rows) == 1, rows
    assert rows[0].after.get("password_change_own") is True
    assert "sae-bimil" not in str(rows[0].after) and "scrypt" not in str(rows[0].after)


async def test_password_change_trainee_can_use_it(client, clock):
    """교육생도 자기 것은 자기가 바꾼다 — 이 창구는 급이 아니라 신원만 본다."""
    user = await _make_user("pwtrainee", auth.ROLE_TRAINEE)
    async with get_session() as s:
        tok = await auth.create_session(s, user)
    r = await client.post(
        "/api/auth/password",
        json={"current_password": _PASSWORD, "new_password": "sae-bimil-88"},
        headers=_cookie_header(tok),
    )
    assert r.status_code == 204, r.text


async def test_password_change_wrong_current_counts_toward_lockout(client, clock):
    """틀린 지금 비밀번호는 400이고 로그인 잠금과 같은 계좌로 센다.

    안 세면 세션을 훔친 쪽이 이 창구로 지금 비밀번호를 무한정 추측한다 — 로그인 창구를
    5회 잠금으로 막아 놓고 옆문을 열어 두는 셈이다.
    """
    user = await _make_user("pwwrong", auth.ROLE_AGENT)
    async with get_session() as s:
        tok = await auth.create_session(s, user)
    h = _cookie_header(tok)
    bad = {"current_password": "teul-rin-1234", "new_password": "sae-bimil-99"}
    for i in range(5):
        r = await client.post("/api/auth/password", json=bad, headers=h)
        assert r.status_code == 400, (i, r.text)
        assert "teul-rin" not in r.text and "sae-bimil" not in r.text

    assert (
        await client.post("/api/auth/password", json=bad, headers=h)
    ).status_code == 429, "다섯 번 틀렸는데 계속 추측하게 둔다"
    assert (
        await client.post(
            "/api/auth/login", json={"username": "pwwrong", "password": _PASSWORD}
        )
    ).status_code == 429, "로그인 잠금과 계좌가 갈라져 있다"

    async with get_session() as s:
        row = await s.get(AppUser, user.id)
    assert auth.verify_password(_PASSWORD, row.password_hash), "막힌 요청이 해시를 바꿨다"


async def test_password_change_rejects_short_new_password(client, clock):
    """새 비밀번호 길이 규칙(8자)은 관리자 재설정 창구와 같은 값이다 — 422."""
    user = await _make_user("pwshort", auth.ROLE_AGENT)
    async with get_session() as s:
        tok = await auth.create_session(s, user)
    r = await client.post(
        "/api/auth/password",
        json={"current_password": _PASSWORD, "new_password": "1234"},
        headers=_cookie_header(tok),
    )
    assert r.status_code == 422


async def test_password_change_opens_login_attempt_for_atomic_lockout(
    client, clock, monkeypatch
):
    """⭐ 잠금 검사 직후 `login_attempt`로 자리를 잡아야 한다(2차 교차 검증).

    안 잡으면 검사와 record_login_failure 사이 scrypt 창에 병렬로 밀어 넣은 요청이 전부
    옛 검사를 통과해, 5회 상한에 N회 추측이 나가는 X77 병렬 우회가 이 창구에 열린다.
    창구가 그 아이디로 login_attempt를 실제로 여는지 스파이로 못박는다 — 안 열면 이
    시험이 빨개진다(예전 판이 그랬다).
    """
    user = await _make_user("pwatomic", auth.ROLE_AGENT)
    async with get_session() as s:
        tok = await auth.create_session(s, user)

    opened = []
    real = auth.login_attempt

    def _spy(username):
        opened.append(username)
        return real(username)

    monkeypatch.setattr(auth, "login_attempt", _spy)

    r = await client.post(
        "/api/auth/password",
        json={"current_password": _PASSWORD, "new_password": "sae-bimil-66"},
        headers=_cookie_header(tok),
    )
    assert r.status_code == 204, r.text
    assert opened == ["pwatomic"], "비밀번호 변경이 login_attempt로 자리를 안 잡았다 — 병렬 우회가 열린다"
