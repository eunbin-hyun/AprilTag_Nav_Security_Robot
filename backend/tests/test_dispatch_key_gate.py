"""출동 라우터의 키 게이트(`DISPATCH_REQUIRE_API_KEY`)가 인입 창구와 같은 계약인가.

## 왜 이 파일이 따로 있나

`routers/dispatch.require_dispatch_key`가 `x_api_key != settings.api_key` 한 줄이었다.
`security.require_api_key`는 같은 키(`API_KEY`)를 보면서 그 모습을 이미 버린 자리다 —
빈 키 통과·비상수시간 대조·창구마다 갈리는 둘레 공백 셋을 `keys_match`로 닫았는데,
출동 라우터만 옛 모습이라 그 결함이 이 창구에 되살아나 있었다.

같은 키를 보는 게이트가 둘로 갈리면 한쪽만 고쳐진다. 그래서 여기서 재는 건 "출동 창구의
판정이 인입 창구와 **같은가**"다. 새 규칙을 따로 적어 두면 다음에 또 갈린다.

⚠ 스위치는 기본이 꺼짐이라 지금 배포 거동은 이 파일과 무관하다(제1 불변). 마지막 케이스가
그 사실을 같이 못박는다.

## 시험 위생

설정이 `lru_cache`라 픽스처가 앞뒤로 캐시를 비운다. 안 비우면 앞 케이스의 키가 다음
케이스로 새어 판정이 엉뚱한 값으로 돈다.

옛 스위치 갈래는 읽기 창구(`GET /api/dispatch/state`) 하나로만 잰다 — 그 게이트는
`router` 전체에 걸린 의존성이라 그 라우터의 어느 창구로 재도 같은 자리이고, 이 창구는
바깥 날씨 서버를 안 탄다(peek).

⚠ 복귀(`POST /commands/return`)는 **다른 라우터**(`device_router`)다. 그래서 "어느 창구로
재도 같다"가 그 창구에는 안 걸린다 — 아래 절이 복귀를 직접 친다.

## 아래 절 — 로그인을 켰을 때 (2026-08-04 사용자 확정)

"라파이는 키를 계속 받게 열어둔다"가 사용자 결정이다. 라파이 게이트 단말이 복귀를 직접
부르는데(`hardware/rpi/gate_server.py call_return_service`) 그 기기는 세션을 못 쥐어서,
`AUTH_REQUIRE_LOGIN`을 켜면 그 버튼이 401로 죽는다. 그래서 **복귀 하나만** 켠 뒤에도
기기 키를 받는다. 여기서 재는 건 셋이다.

  ① 켠 판에서 기기 키로 복귀가 통과하나.
  ② **나머지 여섯은 여전히 막히나** — 키만 들고 `/commands/move`를 치면 401이다.
     이게 "한 창구만 열었다"의 증거고, 없으면 다음 사람이 폴백을 라우터 전체로 넓혀도
     아무도 못 잡는다.
  ③ 켠 판의 폴백이 `DISPATCH_REQUIRE_API_KEY`가 아니라 **실제 키 대조**인가. 그 스위치는
     기본이 꺼짐이라 폴백으로 쓰면 "키를 계속 받는다"가 "익명도 통과"가 된다.
  ④ 그 대조가 **저장소 기본키(`dev-local-key`)를 거절**하나. 공개된 값이라 자물쇠가
     아니어서, 그걸로 열리면 로그인을 켠 뜻이 이 창구에서 통째로 사라진다.
"""
from __future__ import annotations

import logging
import re

import pytest

from app.config import INSECURE_DEFAULT_API_KEY, get_settings

pytestmark = pytest.mark.asyncio(loop_scope="session")

STATE = "/api/dispatch/state"
RETURN = "/api/dispatch/commands/return"
MOVE = "/api/dispatch/commands/move"


@pytest.fixture
def dispatch_key(monkeypatch):
    """`DISPATCH_REQUIRE_API_KEY`를 켜고 `API_KEY`를 케이스가 정한 값으로 바꾼다."""

    def _set(api_key: str) -> None:
        monkeypatch.setenv("DISPATCH_REQUIRE_API_KEY", "true")
        monkeypatch.setenv("API_KEY", api_key)
        get_settings.cache_clear()

    try:
        yield _set
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_빈_키를_둔_배포에서_빈_헤더가_통과하면_안_된다(client, dispatch_key):
    """`API_KEY=""`로 뜬 배포. 옛 한 줄 대조(`x_api_key != settings.api_key`)는 여기서
    빈 헤더를 **통과**시켰다 — 자물쇠를 켰는데 아무나 로봇을 세우고 보낼 수 있었다.

    기대값이 비면 아무도 못 들어오는 쪽으로 닫는 게 `keys_match` 계약이다. 헤더를 아예 안
    싣는 갈래도 같은 401이라야 한다.
    """
    dispatch_key("")

    empty = await client.get(STATE, headers={"X-API-Key": ""})
    assert empty.status_code == 401, empty.text

    none = await client.get(STATE)
    assert none.status_code == 401, none.text

    # 아무 값이나 넣어도 열리지 않는다(빈 기대값은 무엇과도 안 맞는다).
    guess = await client.get(STATE, headers={"X-API-Key": "guess-anything"})
    assert guess.status_code == 401, guess.text


async def test_둘레_공백은_양쪽에서_떼고_본다(client, dispatch_key):
    """HTTP 헤더 파서가 앞뒤 공백을 떼고 올린다. 기대값 쪽도 같이 떼야 창구가 안 갈린다.

    옛 대조는 `" spaced-key "` != `"spaced-key"`라 정상 화면이 통째로 401이었다.
    """
    dispatch_key("  spaced-key  ")

    ok = await client.get(STATE, headers={"X-API-Key": "spaced-key"})
    assert ok.status_code == 200, ok.text


async def test_맞는_키는_통과하고_틀린_키는_401이다(client, dispatch_key):
    """자물쇠가 여전히 자물쇠다 — 닫는 쪽만 고치고 여는 쪽이 막히면 시연이 선다."""
    dispatch_key("dispatch-key-1")

    ok = await client.get(STATE, headers={"X-API-Key": "dispatch-key-1"})
    assert ok.status_code == 200, ok.text

    bad = await client.get(STATE, headers={"X-API-Key": "dispatch-key-2"})
    assert bad.status_code == 401, bad.text
    assert bad.json()["detail"] == "유효한 X-API-Key 헤더가 필요합니다."


async def test_비ASCII_키가_와도_500이_아니라_401이다(client, dispatch_key):
    """`hmac.compare_digest`는 str을 받으면 두 값이 다 ASCII일 때만 돈다.

    옛 `!=` 대조는 예외가 안 났지만, 대조를 공용 함수로 옮기면서 이 갈래가 500으로 터지면
    수리가 새 결함을 심는 셈이다. `keys_match`가 바이트로 바꿔 견주는 걸 여기서 못박는다.

    ⚠ 헤더 값을 **bytes로 싣는다.** httpx는 str 헤더를 ascii로 인코드해서, 한글을 str로
    주면 서버가 아니라 클라이언트에서 터진다 — 전선을 안 타므로 재려던 걸 못 잰다.
    """
    dispatch_key("dispatch-key-1")

    r = await client.get(STATE, headers={"X-API-Key": "열쇠키한글".encode()})
    assert r.status_code == 401, r.text


async def test_스위치가_꺼져_있으면_키_없이_그대로_열린다(client):
    """⭐ 제1 불변 — 기본값(꺼짐)에서는 지금 거동이 한 글자도 안 바뀐다."""
    assert get_settings().dispatch_require_api_key is False, "기본값이 false가 아니다"

    r = await client.get(STATE)
    assert r.status_code == 200, r.text


async def test_복귀도_스위치가_꺼져_있으면_키_없이_그대로_열린다(client):
    """⭐ 제1 불변 — 라우터를 가른 뒤에도 꺼진 구간의 복귀는 예전 그대로다.

    복귀만 `device_router`로 옮겼으니 그 창구의 off 거동을 따로 못박는다. 위 케이스는
    `router` 쪽만 재서 이 갈래를 안 본다.
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    assert get_settings().dispatch_require_api_key is False, "기본값이 false가 아니다"

    r = await client.post(RETURN)
    assert r.status_code == 200, r.text


async def test_옛_스위치는_복귀에도_그대로_걸린다(client, dispatch_key):
    """`DISPATCH_REQUIRE_API_KEY`는 롤백 구간(플래그를 되돌린 판)의 유일한 자물쇠다.

    복귀를 딴 라우터로 옮기면서 이 스위치가 그 창구에서 빠지면, 되돌린 배포에서 복귀만
    무인증으로 열린다. 두 라우터가 **같은 `legacy_key_gate`**를 쓰는지를 여기서 잰다.

    ⚠ 401을 **먼저** 친다. 200이 앞서면 유량 제한 눈금이 찍혀 뒤 요청이 429가 된다
    (401은 의존성에서 잘려 창구 함수에 안 들어가므로 눈금을 안 남긴다).
    """
    dispatch_key("dispatch-key-1")

    bad = await client.post(RETURN, headers={"X-API-Key": "dispatch-key-2"})
    assert bad.status_code == 401, bad.text

    ok = await client.post(RETURN, headers={"X-API-Key": "dispatch-key-1"})
    assert ok.status_code == 200, ok.text


# ── 로그인을 켰을 때 — 복귀만 키를 계속 받는다 (2026-08-04 사용자 확정) ──────


@pytest.fixture
def flag_on(monkeypatch):
    """`AUTH_REQUIRE_LOGIN=true` 구간. 설정이 lru_cache라 캐시를 앞뒤로 비운다.

    ⚠ 셸 환경변수로는 못 켠다 — conftest `_PINNED_TEST_ENV`가 import 시점에 `false`로
    통째로 덮어서, 밖에서 켠 값이 시험에 안 닿는다. 케이스 안에서 뒤집는 길만 열려 있다
    (test_auth_gates.py의 같은 이름 픽스처와 같은 모양이다).
    """
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _agent_cookie(username: str) -> dict[str, str]:
    """요원 계정 하나를 만들고 살아 있는 세션 쿠키 헤더를 돌려준다."""
    from app import auth
    from app.db import get_session
    from app.models import AppUser

    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password("dispatch-pw-1234"),
            display_name=f"이름-{username}",
            role=auth.ROLE_AGENT,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    async with get_session() as session:
        token = await auth.create_session(session, user)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


async def test_로그인을_켜도_기기_키면_복귀가_통과한다(client, flag_on):
    """⭐ 사용자 확정 — 라파이 게이트 단말의 복귀 버튼이 켠 뒤에도 산다.

    이 케이스가 빨개지면 `AUTH_REQUIRE_LOGIN=true`로 켜는 순간 현장 단말이 401로 죽는다.

    ⚠ **전제를 먼저 못박는다.** 폴백은 저장소 기본키를 거절하므로(아래 케이스), 이 시험이
    쓰는 키가 그 기본키면 여기가 "여는 쪽"을 재는 게 아니라 그냥 빨개진다. 시험 판의
    `API_KEY`는 conftest가 `test-key`로 잡는데, 그 한 줄이 바뀌면 이 케이스가 무엇을 재는지
    조용히 달라지므로 값을 여기서 확인한다.
    """
    assert get_settings().api_key != INSECURE_DEFAULT_API_KEY, (
        "시험 판의 API_KEY가 저장소 기본키다 — conftest를 확인해라"
    )

    r = await client.post(RETURN, headers={"X-API-Key": get_settings().api_key})
    assert r.status_code == 200, r.text
    # 게이트만 보고 끝내지 않는다 — 통과한 요청이 실제로 복귀 명령이어야 한다.
    assert r.json()["command"] == "return_to_charge"


async def test_켠_뒤_키로는_이동_창구가_안_열린다(client, flag_on):
    """⭐ "한 창구만 열었다"의 증거. 폴백이 라우터 전체로 번지면 여기가 빨개진다."""
    r = await client.post(
        MOVE,
        json={"destination": "INDOOR_TAGGING"},
        headers={"X-API-Key": get_settings().api_key},
    )
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "로그인이 필요합니다."}


_PATH_PARAM_RE = re.compile(r"\{[^}]+\}")


def session_only_endpoints() -> list[tuple[str, str]]:
    """세션만 받는 라우터(`router`)의 창구 전부를 (메서드, 경로)로 뽑는다.

    ⚠ **목록을 손으로 안 적는 게 요점이다.** 베껴 두면 창구를 새로 붙인 사람이 여기 한 줄을
    안 더해도 아무도 안 잡는다 — 그 창구가 기기 키로 열려도 시험은 초록이다. 라우트 집합에서
    뽑으면 붙이는 순간 자동으로 사정권에 든다.

    경로 인자에는 아무 값이나 넣는다. 게이트가 라우터 레벨 의존성이라 창구 함수보다 앞이고,
    그래서 그 id의 행이 없어도 판정은 401에서 끝난다.
    """
    from app.routers.dispatch import router

    return sorted(
        (method, _PATH_PARAM_RE.sub("1", route.path))
        for route in router.routes
        for method in route.methods
    )


async def test_켠_뒤_키로는_나머지_창구도_전부_안_열린다(client, flag_on):
    """이동 하나만 재면 딴 창구가 조용히 열려도 못 잡는다 — `router` 쪽을 전부 친다."""
    key = {"X-API-Key": get_settings().api_key}
    blocked = session_only_endpoints()
    assert blocked, "세션 전용 라우터에서 창구를 하나도 못 뽑았다"
    assert ("POST", RETURN) not in blocked, "복귀가 세션 전용 라우터에 붙어 있다"

    for method, path in blocked:
        # 본문은 안 싣는다. 게이트가 본문 검증보다 **앞**이라 401이 422보다 먼저 나오고,
        # 본문을 손으로 적어 두면 그것도 사본이라 창구가 늘 때 같이 낡는다.
        r = await client.request(method, path, headers=key)
        assert r.status_code == 401, f"{method} {path}가 기기 키로 열렸다 — {r.status_code}"


async def test_켠_뒤_틀린_키는_복귀도_못_연다(client, flag_on):
    r = await client.post(RETURN, headers={"X-API-Key": "not-the-device-key"})
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "로그인이 필요합니다."}


async def test_켠_뒤_키를_안_실으면_복귀가_막힌다(client, flag_on):
    """⭐ 폴백이 `DISPATCH_REQUIRE_API_KEY`가 아니라 **실제 키 대조**라는 증거.

    그 스위치는 기본이 꺼짐이고, 꺼진 스위치를 폴백으로 쓰면 아무 검사 없이 통과시킨다 —
    "키를 계속 받는다"가 그 자리에서 "익명도 통과"로 뒤집힌다. 여기가 401이어야 로그인을
    켠 뜻이 이 창구에서도 산다.
    """
    assert get_settings().dispatch_require_api_key is False, "이 케이스의 전제가 깨졌다"

    r = await client.post(RETURN)
    assert r.status_code == 401, r.text


async def test_기기_키가_빈_배포에서는_아무도_복귀를_못_연다(client, flag_on, monkeypatch):
    """`API_KEY=""`로 뜬 배포. 빈 기대값은 무엇과도 안 맞는다(`keys_match` 계약).

    이 갈래가 열리면 키를 안 넣은 배포에서 익명이 로봇을 부른다.
    """
    monkeypatch.setenv("API_KEY", "")
    get_settings.cache_clear()

    assert (await client.post(RETURN, headers={"X-API-Key": ""})).status_code == 401
    assert (await client.post(RETURN, headers={"X-API-Key": "guess"})).status_code == 401
    assert (await client.post(RETURN)).status_code == 401


async def test_기기_키_통과는_운영에_남는_레벨로_적힌다(client, flag_on, caplog):
    """⭐ 감사 줄이 운영에서 실제로 보이나.

    이 갈래는 "사람 행위자가 없는 명령"을 남기는 유일한 자리라 로그가 유일한 흔적이다.
    그런데 `logger.info`로 적으면 운영에서 **한 줄도 안 남는다** — 앱이 `basicConfig`·
    `dictConfig`를 안 부르고 컨테이너도 `--log-level` 없이 떠서(`backend/Dockerfile` CMD)
    `c207.security`의 유효 레벨이 root 기본값 WARNING이다. 핸들러가 0개라 WARNING부터만
    `logging.lastResort`가 stderr로 흘린다(2026-08-04 uvicorn `LOGGING_CONFIG` 실측).

    ⚠ 그래서 "줄이 남았나"가 아니라 **레벨**을 잰다. caplog는 자기 핸들러를 붙여 INFO도
    잡으므로, 존재만 재면 운영에서 안 보이는 판도 그대로 초록이다.
    """
    with caplog.at_level(logging.DEBUG, logger="c207.security"):
        r = await client.post(RETURN, headers={"X-API-Key": get_settings().api_key})
    assert r.status_code == 200, r.text

    audit = [
        rec for rec in caplog.records
        if rec.name == "c207.security" and "기기 키로" in rec.getMessage()
    ]
    assert audit, "기기 키 폴백이 감사 줄을 아예 안 남겼다"
    assert all(rec.levelno >= logging.WARNING for rec in audit), (
        f"감사 줄 레벨이 낮다 — 운영(root WARNING)에서 안 보인다: "
        f"{[logging.getLevelName(rec.levelno) for rec in audit]}"
    )


async def test_켠_뒤_저장소_기본키로는_복귀가_안_열린다(client, flag_on, monkeypatch):
    """⭐ 폴백이 `secret_is_usable`을 앞세운다는 증거.

    `dev-local-key`는 `.env.example`에 그대로 적혀 있는 값이라 자물쇠가 아니다(리포 자신이
    `security.secret_is_usable`에서 그렇게 판정한다). `API_KEY`를 안 덮은 배포에서 이 갈래가
    열리면 공개된 문자열 하나로 아무나 로봇을 불러들이고, 로그인을 켠 뜻이 이 창구에서만
    통째로 사라진다. WS 로봇 채널은 이미 같은 겹을 쓴다(`ws_expected_key`).

    ⚠ 인입 게이트(`require_api_key`)는 **반대로** 기본키를 받는다. 로컬 개발이 그 값으로
    도는 자리라 일부러 열어 둔 결정이고, 그 잣대는 여기서 안 건드린다
    (`test_ingest_*`가 그쪽을 잰다).
    """
    monkeypatch.setenv("API_KEY", INSECURE_DEFAULT_API_KEY)
    get_settings.cache_clear()

    r = await client.post(RETURN, headers={"X-API-Key": INSECURE_DEFAULT_API_KEY})
    assert r.status_code == 401, r.text
    assert r.json() == {"detail": "로그인이 필요합니다."}

    # 둘레 공백으로 기본키를 비껴가는 길도 없다 — 판정 전에 양쪽을 strip한다.
    monkeypatch.setenv("API_KEY", f"  {INSECURE_DEFAULT_API_KEY}  ")
    get_settings.cache_clear()
    padded = await client.post(RETURN, headers={"X-API-Key": INSECURE_DEFAULT_API_KEY})
    assert padded.status_code == 401, padded.text


async def test_켠_뒤_요원_세션도_복귀를_그대로_누른다(client, flag_on):
    """키 갈래를 연 뒤에도 관제 화면 갈래가 살아 있나.

    복귀는 화면 버튼과 라파이가 같이 쓰는 유일한 창구다. 폴백을 넣다가 세션 갈래가
    끊기면 켠 배포에서 요원이 복귀를 못 누른다.
    """
    cookie = await _agent_cookie("dispatchagent")
    r = await client.post(RETURN, headers=cookie)
    assert r.status_code == 200, r.text


async def test_기기_키_라우터에는_복귀_하나만_붙어_있다():
    """⭐ 열린 집합을 닫는다 — `device_router`에 창구를 더 붙이면 여기가 빨개진다.

    라우터 레벨 의존성을 그대로 둔 대가로 "이 라우터에 붙이면 기기 키로 열린다"는 함정이
    생긴다. 그 함정이 조용히 터지지 않게 목록을 여기서 못박는다. 창구를 정말 늘려야 하면
    이 시험을 같이 고치면서 "그 창구도 공용 기기 키로 연다"를 눈으로 확인하게 된다.

    ⚠ 아무것도 안 기다리는데 `async def`인 이유는 모듈 머리의
    `pytestmark = pytest.mark.asyncio(...)`다. sync 함수로 두면 그 마커가 안 맞아 경고가
    뜨고, `-W error`가 들어오는 순간 이 못이 **결함이 아니라 마커 때문에** 조용히 죽는다.
    """
    from app.routers.dispatch import device_router

    routes = {
        (route.path, tuple(sorted(route.methods))) for route in device_router.routes
    }
    assert routes == {(RETURN, ("POST",))}, (
        f"기기 키 라우터의 창구 목록이 바뀌었다: {sorted(routes)}"
    )
