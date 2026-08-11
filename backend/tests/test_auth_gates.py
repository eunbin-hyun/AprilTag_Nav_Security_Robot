"""역할 게이트 이관 — 기존 창구 33개가 플래그 양쪽에서 어떻게 도나.

계약은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §3 매트릭스와 §5.1이다.
표 한 줄이 코드 한 줄로 옮겨졌는지를 실제 HTTP 요청으로 잰다.

## ⭐ 여기서 재는 것 둘

1. **플래그 off = 지금 거동 그대로**(제1 불변). 조회는 익명 200이고, 쓰기는 예전 기기 키
   그대로다. 이 갈래가 깨지면 롤아웃 1~4단계에서 화면이 통째로 죽는다.
2. **플래그 on = 매트릭스대로**. 익명은 401, 급이 모자라면 403, 맞으면 200이다.

401과 403을 갈라 재는 게 중요하다(§7.2). 403을 401로 내면 요원이 관리자 창구를 한 번
건드릴 때마다 로그아웃되고, 401을 403으로 내면 화면이 로그인 창을 못 띄운다.

## 왜 창구를 다 안 재고 대표만 재나

같은 게이트 함수(`require_agent`·`key_gate_or`) 하나가 서른 개를 찍어내는 구조라, 계열마다
한 창구를 재면 그 계열 전체가 같이 판정된다. 대신 **계열은 하나도 안 빼고** 고른다 —
조회·통계·쓰기(기기 키였던 자리)·assistant·dispatch·devpage·WS·인입(안 바뀜)·healthz(안 바뀜).
명부(`/api/staff/*`)는 다른 갈래가 만드는 신설 창구라 여기서 안 본다.

⚠ 계정·세션 테이블 비우기와 인메모리 잠금 초기화는 conftest `_clean` 하나가 한다. 예전에는
이 파일이 자기 앞에서 같은 걸 또 돌렸는데, 그러면 공용 훅이 빠져도 이 파일만은 통과해서
훅의 결함이 안 보인다. 이 파일이 통과하는 게 그 훅이 실제로 돈다는 증거다(2026-08-02 교차 검토).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocket, WebSocketDisconnect

from app import ai_bridge, auth
from app.config import get_settings
from app.db import get_session
from app.main import WS_UNAUTHORIZED_CODE, app
from app.models import Alert, AppUser
from app.routers import assistant as assistant_router
from app.security import ws_dashboard_allowed

pytestmark = pytest.mark.asyncio(loop_scope="session")

API_KEY = get_settings().api_key
KEY_HEADERS = {"X-API-Key": API_KEY}
_PASSWORD = "gate-pw-1234"


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


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """assistant 창구를 부를 때 GMS를 절대 안 탄다(크레딧 보호 + 결정성).

    키가 없으면 폴백 문장으로 200이 나가므로, 게이트 판정만 보는 이 파일엔 그걸로 충분하다.
    """
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key=""),
    )


# ── 시험 자료 ──────────────────────────────────────────────────────────────

async def _make_user(username: str, role: str) -> AppUser:
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
        return user


async def _session_cookie(username: str, role: str) -> dict[str, str]:
    """그 급 계정 하나를 만들고 살아 있는 세션 쿠키 헤더를 돌려준다.

    ⚠ 토큰은 `secrets.token_urlsafe`라 늘 ASCII다. 헤더 값은 ascii로 인코드되므로 여기에
    한글이 섞이면 시험이 서버가 아니라 클라이언트에서 터진다(A갈래 실측).
    """
    user = await _make_user(username, role)
    async with get_session() as session:
        token = await auth.create_session(session, user)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


async def _seed_alert(**values) -> int:
    """경고 한 줄. 쓰기 창구 게이트를 재려면 404 말고 진짜 행이 있어야 한다."""
    async with get_session() as session:
        alert = Alert(
            type=values.pop("type", "untagged"),
            severity=values.pop("severity", "high"),
            **values,
        )
        session.add(alert)
        await session.commit()
        await session.refresh(alert)
        return alert.id


async def _read_alert(alert_id: int) -> Alert:
    async with get_session() as session:
        return (
            await session.execute(select(Alert).where(Alert.id == alert_id))
        ).scalars().one()


def _ws_first(path: str, **kwargs) -> dict:
    """붙어서 첫 메시지 한 장을 받는다(test_ws_auth.py `_connect`와 같은 모양).

    ⚠ **반드시 한 장을 읽어야** 거절이 잡힌다. 서버는 accept한 뒤에 1008로 닫으므로
    `websocket_connect.__enter__`는 거절당한 접속에서도 그냥 성공한다 — 안 읽으면 막힌
    핸드셰이크가 통과한 것처럼 보인다(이 파일 1차 실행에서 실제로 그렇게 샜다).
    """
    with TestClient(app) as client, client.websocket_connect(path, **kwargs) as ws:
        return ws.receive_json()


def _assert_ws_closed(path: str, **kwargs) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        _ws_first(path, **kwargs)
    assert caught.value.code == WS_UNAUTHORIZED_CODE


def _fake_ws(*, cookie: str | None = None, query: str = "") -> WebSocket:
    """핸드셰이크 판정에 필요한 것만 실은 최소 ASGI WebSocket.

    `ws_dashboard_allowed`가 보는 건 쿠키·헤더·쿼리 셋뿐이라 전송 짝은 안 쓴다. 판정 함수를
    직접 부르는 이유는 **DB를 시험 이벤트 루프 안에서 타야 하기 때문**이다 — TestClient는
    자기 루프를 따로 돌려서, 거기서 세션을 조회하면 커넥션 풀이 두 루프에 걸쳐 갈라진다
    (실측 — `got Future attached to a different loop`).
    """
    headers = []
    if cookie is not None:
        headers.append((b"cookie", cookie.encode("ascii")))

    async def _noop():  # pragma: no cover - 판정 경로가 안 쓴다
        return {"type": "websocket.receive"}

    async def _send(_message):  # pragma: no cover - 판정 경로가 안 쓴다
        return None

    return WebSocket(
        {
            "type": "websocket",
            "path": "/ws/dashboard",
            "headers": headers,
            "query_string": query.encode("ascii"),
        },
        receive=_noop,
        send=_send,
    )


# ── 1. 플래그 off = 지금 거동 그대로 (제1 불변) ────────────────────────────
#
# 기존 시험 775건이 이 갈래의 진짜 증거고, 여기 있는 건 "이 파일 안에서도 양쪽을 나란히
# 본다"는 대조군이다. 켠 케이스만 두면 off 구간이 조용히 바뀌어도 이 파일은 초록이다.


async def test_flag_off_keeps_read_endpoints_anonymous(client):
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    for path in (
        "/healthz",
        "/api/robots",
        "/api/events",
        "/api/alerts",
        "/api/eta",
        "/api/dashboard/snapshot",
        "/api/stats/today",
        "/api/dispatch/state",
        # ⚠ `/dev/telemetry`는 2026-08-06에 이 목록에서 빠졌다 — 아래
        #    `test_flag_off_still_blocks_dev_page`가 그 자리를 맡는다.
    ):
        r = await client.get(path)
        assert r.status_code == 200, f"{path}가 익명에서 막혔다 — {r.status_code} {r.text[:200]}"


async def test_flag_off_still_blocks_dev_page(client):
    """⭐ 개발자 페이지는 **플래그가 꺼져 있어도** 401이다 (2026-08-06 사용자 결정).

    ⚠ 판정이 뒤집힌 자리다. 8/6까지 이 경로는 위 `..._keeps_read_endpoints_anonymous`
    목록에 있었다 — 플래그가 꺼진 동안은 nginx `auth_basic`(계정 `c207dev`)이 실질
    자물쇠였고, 앱 층은 제1 불변대로 판정을 안 했다.

    **사용자가 그 별도 계정을 걷고 명부 계정으로만 들어가게 정하면서** 겹이 하나로 줄었다.
    그러면 앱 게이트가 플래그를 타는 순간 구멍이 된다 — 누가 `AUTH_REQUIRE_LOGIN`을
    되돌리면 이 페이지가 익명에게 통째로 열리고, 여기엔 **신원 입력 상자**가 있어서 연
    사람이 상위 보고선 채널로 가짜 신원 카드를 밀어 넣는다.

    그래서 게이트를 `require_admin_always`로 올렸다. 예보 창구(`/api/weather/forecast`)가
    같은 까닭으로 이미 이 꼴이다.
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"

    r = await client.get("/dev/telemetry")

    assert r.status_code == 401, f"플래그가 꺼졌다고 개발자 페이지가 열렸다 — {r.status_code}"
    assert r.json() == {"detail": "로그인이 필요합니다."}


async def test_flag_off_still_blocks_weather_forecast(client, monkeypatch):
    """⭐ 예보 창구는 **플래그가 꺼져 있어도** 401이다 (2026-08-04 전체검토 D10·L21).

    ⚠ 판정이 뒤집힌 자리다. 8/4까지 이 케이스 이름은 `..._keeps_weather_forecast_anonymous`
    였고 200을 못박았다. 그 docstring이 "`require_agent_always`로 바뀌면 여기가 401로
    빨개진다"고 예고해 뒀는데, D10이 실제로 바꿨다 — 뒤가 하루 1만 건짜리 기상청
    오퍼레이션이라 공개 도메인에 대고 누르는 만큼 남의 한도를 태울 수 있어서다.

    이 창구는 로그인과 함께 새로 생긴 자리라 제1 불변("지금 있는 창구의 거동을 안 바꾼다")이
    안 걸린다. 카메라 창구와 같은 자물쇠·같은 근거다(`app/routers/weather.py` 모듈 머리).

    ⭐ **이 케이스가 L21이 말한 그물이다.** 플래그가 꺼진 구간의 `require_agent`는 판정을
    아예 안 하고 통과시켜서(`app/auth.py` `_role_dependency`), 켬 표만 있으면
    `dependencies=[Depends(...)]` 줄이 `require_agent`로 되돌아가도 아무도 안 빨개진다.
    켬·끔 두 표에 다 있어야 그 되돌림이 잡힌다.

    창구가 부르는 함수를 갈아 끼우는 건 그대로 둔다 — 기상청 키가 비어 있어 진짜로 부르면
    503이 나오고, 그러면 "게이트가 막았나 바깥이 죽었나"가 안 갈린다.
    """
    import datetime as _dt

    from app.routers import weather as weather_router

    called: list[bool] = []
    base_at = _dt.datetime(2026, 8, 3, 20, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=9)))

    async def _fake_forecast():
        called.append(True)
        return base_at, []

    monkeypatch.setattr(weather_router, "forecast_hours", _fake_forecast)

    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    r = await client.get("/api/weather/forecast")
    assert r.status_code == 401, (
        f"꺼진 구간에서 예보가 익명에게 열렸다 — {r.status_code} {r.text[:200]}"
    )
    assert r.json() == {"detail": "로그인이 필요합니다."}
    # ⭐ 상태 코드만 보면 "401을 내면서도 바깥은 이미 불렀다"를 못 잡는다. 막는 목적이
    # 기상청 한도를 지키는 것이라, 조회까지 안 갔는지를 같이 센다.
    assert called == [], "막힌 요청이 기상청 조회까지 갔다"


async def test_flag_off_keeps_device_key_on_alert_writes(client):
    """쓰기 계열은 예전 기기 키 그대로다. 키 없으면 401, 있으면 200."""
    alert_id = await _seed_alert()

    without = await client.post(f"/api/alerts/{alert_id}/ack")
    assert without.status_code == 401
    assert without.json() == {"detail": "유효한 X-API-Key 헤더가 필요합니다."}

    with_key = await client.post(f"/api/alerts/{alert_id}/ack", headers=KEY_HEADERS)
    assert with_key.status_code == 200


async def test_flag_off_keeps_api_key_on_assistant(client):
    """AI 탭도 예전 키 게이트 그대로다 — off 구간을 무인증으로 안 연다(설계 P1)."""
    assert (await client.get("/api/assistant/daily-summary")).status_code == 401
    ok = await client.get("/api/assistant/daily-summary", headers=KEY_HEADERS)
    assert ok.status_code == 200, ok.text


async def test_flag_off_keeps_ack_actor_from_body(client):
    """세션이 없으면 `acked_by`도 예전 그대로 None이다(적을 자리가 원래 없었다)."""
    alert_id = await _seed_alert()
    assert (
        await client.post(f"/api/alerts/{alert_id}/ack", headers=KEY_HEADERS)
    ).status_code == 200
    assert (await _read_alert(alert_id)).acked_by is None


def test_flag_off_keeps_dashboard_ws_anonymous():
    """WS 대시보드도 쿠키 없이 붙는다(WS 키 플래그가 기본 꺼짐이라 예전 그대로)."""
    assert _ws_first("/ws/dashboard")["type"] == "hello"


# ── 2. 플래그 on × 익명 = 401 (§7.2 — 화면이 로그인 창을 띄우는 코드) ───────


async def test_flag_on_rejects_anonymous_everywhere(client, flag_on):
    for path in (
        "/api/robots",
        "/api/events",
        "/api/alerts",
        "/api/eta",
        "/api/dashboard/snapshot",
        "/api/stats/today",
        "/api/dispatch/state",
        "/dev/telemetry",
        "/api/assistant/daily-summary",
        # 예보 창구는 8/3에 생겼는데 이 반복문에 안 실려 있었다(2026-08-04 조사 축①-5).
        # 화면 기상 칩이 부르는 자리라, 게이트 줄이 지워져도 잡을 그물이 없었다.
        "/api/weather/forecast",
    ):
        r = await client.get(path)
        assert r.status_code == 401, f"{path}가 익명에게 열렸다 — {r.status_code}"
        assert r.json() == {"detail": "로그인이 필요합니다."}


async def test_flag_on_rejects_anonymous_writes(client, flag_on):
    alert_id = await _seed_alert()
    for path in (
        f"/api/alerts/{alert_id}/ack",
        f"/api/alerts/{alert_id}/identify",
        "/api/shuttle-calls",
        "/api/dispatch/commands/return",
    ):
        r = await client.post(path, json={})
        assert r.status_code == 401, f"{path}가 익명에게 열렸다 — {r.status_code}"


async def test_flag_on_does_not_accept_device_key_as_person(client, flag_on):
    """⭐ 켠 뒤엔 기기 키가 사람 자리를 못 메운다.

    둘 다 통과시키면 브라우저가 여전히 키를 들어야 해서 -238이 그대로 남는다(§3).
    """
    alert_id = await _seed_alert()
    assert (
        await client.post(f"/api/alerts/{alert_id}/ack", headers=KEY_HEADERS)
    ).status_code == 401
    assert (
        await client.get("/api/assistant/daily-summary", headers=KEY_HEADERS)
    ).status_code == 401


async def test_flag_on_keeps_healthz_anonymous(client, flag_on):
    """healthz만 켠 뒤에도 익명이다 — 젠킨스·모니터링이 부르는 자리다(§3 표 첫 줄)."""
    r = await client.get("/healthz")
    assert r.status_code == 200


async def test_flag_on_keeps_ingest_on_device_key(client, flag_on):
    """인입 3창구는 사람이 부르는 자리가 아니라 기기 키 그대로다(§3).

    젯슨·라파이는 세션을 못 쥔다 — 여기까지 세션으로 바꾸면 현장 배선이 통째로 끊긴다.
    """
    body = {
        "event_id": "gate-auth-gate-1",
        "gate_no": 1,
        "tag_id": "AABBCCDD11223344",
        "observed_at": "2026-08-01T12:00:00+09:00",
    }
    anon = await client.post("/api/tagging-events", json=body)
    assert anon.status_code == 401
    with_key = await client.post("/api/tagging-events", json=body, headers=KEY_HEADERS)
    assert with_key.status_code in (200, 201), with_key.text


# ── 3. 플래그 on × 요원 세션 = 매트릭스대로 ────────────────────────────────


async def test_agent_session_passes_read_and_write(client, flag_on):
    cookie = await _session_cookie("agent1", auth.ROLE_AGENT)
    for path in (
        "/api/robots",
        "/api/events",
        "/api/alerts",
        "/api/dashboard/snapshot",
        "/api/stats/today",
        "/api/dispatch/state",
        "/api/assistant/daily-summary",
    ):
        r = await client.get(path, headers=cookie)
        assert r.status_code == 200, f"{path}가 요원을 막았다 — {r.status_code} {r.text[:200]}"

    alert_id = await _seed_alert()
    ack = await client.post(f"/api/alerts/{alert_id}/ack", headers=cookie)
    assert ack.status_code == 200, ack.text


async def test_agent_session_passes_eta_and_shuttle_call(client, flag_on):
    """`/api/eta`와 `POST /api/shuttle-calls`의 **양성 경로** (2026-08-02 백지 검토 F70).

    이 둘은 위 반복문에 안 실려서 "켠 뒤 익명이면 401"만 있고 "요원이면 200"이 없었다.
    다른 창구가 통과한다고 이 둘이 통과한다는 논증이 안 선다 — 계열 대표 논증은 같은 게이트
    함수를 **한 자리에서** 공유할 때만 성립하는데, 이 둘은 `dependencies=[Depends(require_agent)]`를
    각자 라우트 데코레이터에 적어 둔다(app/routers/query.py). 그 줄 하나를 빠뜨리거나 잘못
    적으면 다른 창구는 전부 초록인 채로 여기만 닫힌다(요원이 403·401을 맞아 화면이 멈춘다).

    `/api/eta`는 기록이 없어도 200에 빈 응답이 계약이라, 표본을 안 심어도 게이트만 정확히 잰다.
    """
    cookie = await _session_cookie("agent7", auth.ROLE_AGENT)

    eta = await client.get("/api/eta", headers=cookie)
    assert eta.status_code == 200, f"/api/eta가 요원을 막았다 — {eta.status_code} {eta.text[:200]}"
    assert eta.json()["samples"] == 0

    call = await client.post(
        "/api/shuttle-calls", json={"gate_no": 21}, headers=cookie
    )
    assert call.status_code == 200, (
        f"/api/shuttle-calls가 요원을 막았다 — {call.status_code} {call.text[:200]}"
    )
    # 게이트를 실제로 받았는지까지 본다. 200만 보면 게이트가 뒤바뀌어도 못 잡는다.
    assert call.json()["gate_no"] == 21


async def test_agent_session_passes_weather_forecast(client, flag_on, monkeypatch):
    """`GET /api/weather/forecast`의 **양성 경로** (2026-08-04 조사 축①-5).

    F70(`/api/eta`·`/api/shuttle-calls`)과 같은 계열 결함이다. 이 창구도 라우트 데코레이터에
    `dependencies=[Depends(require_agent_always)]`를 따로 적어서, 그 줄 하나가 지워지거나
    `require_admin`으로 바뀌어도 다른 창구는 전부 초록이다. 그런데 화면 기상 칩이 부르는
    자리라 요원이 403을 맞으면 눌러도 안 열린다.

    ⚠ 2026-08-04 정정 — 예전 주석은 "`tests/test_weather.py`의 `forecast_client`가
    `FastAPI()`를 새로 만들어 실앱 배선을 한 번도 안 본다"였는데 낡았다(전체검토 L17).
    그 픽스처도 이제 `app.main.app`을 그대로 쓴다. 대신 거기는 자물쇠를
    `dependency_overrides`로 열고 **응답 모양**만 재고, 자물쇠 판정은 이 파일 몫이다.

    ⚠ 기상청은 안 탄다. `conftest._PINNED_TEST_ENV`가 `KMA_SERVICE_KEY`를 비워 둬서 진짜로
    부르면 503이 나오는데, 그러면 "게이트를 지났나"가 아니라 "바깥이 살아 있나"를 재게 된다.
    창구가 부르는 함수만 갈아 끼워 게이트 판정만 남긴다.
    """
    import datetime as _dt

    from app.routers import weather as weather_router

    base_at = _dt.datetime(2026, 8, 3, 20, 0, tzinfo=_dt.timezone(_dt.timedelta(hours=9)))

    async def _fake_forecast():
        return base_at, []

    monkeypatch.setattr(weather_router, "forecast_hours", _fake_forecast)

    cookie = await _session_cookie("agent9", auth.ROLE_AGENT)
    r = await client.get("/api/weather/forecast", headers=cookie)
    assert r.status_code == 200, (
        f"/api/weather/forecast가 요원을 막았다 — {r.status_code} {r.text[:200]}"
    )
    # 게이트만 보고 200을 세면 응답이 통째로 바뀌어도 못 잡는다. 계약 칸까지 본다.
    assert r.json() == {"base_at": base_at.isoformat(), "hours": []}


async def test_agent_is_blocked_from_admin_and_leader_gates(client, flag_on):
    """급이 모자라면 **403**이다. 401로 내면 화면이 요원을 로그아웃시킨다(§7.2)."""
    cookie = await _session_cookie("agent2", auth.ROLE_AGENT)
    alert_id = await _seed_alert()

    dev = await client.get("/dev/telemetry", headers=cookie)
    assert dev.status_code == 403, "텔레메트리는 관리자 전용이다(범위 정본 §2)"
    assert dev.json() == {"detail": "권한이 없습니다."}

    cleared = await client.delete(f"/api/alerts/{alert_id}/identify", headers=cookie)
    assert cleared.status_code == 403, "신원 기록 삭제는 팀장급이다(§8 결정 5)"


async def test_leader_clears_identity_and_admin_opens_telemetry(client, flag_on):
    """윗급은 통과한다 — 403이 급 판정이지 창구 봉쇄가 아니라는 증거다."""
    alert_id = await _seed_alert()

    leader = await _session_cookie("leader1", auth.ROLE_LEADER)
    assert (
        await client.delete(f"/api/alerts/{alert_id}/identify", headers=leader)
    ).status_code == 200
    # 팀장도 텔레메트리는 못 본다. 관리자 전용 줄이 팀장까지 열리면 매트릭스가 한 칸 샌다.
    assert (await client.get("/dev/telemetry", headers=leader)).status_code == 403

    admin = await _session_cookie("admin1", auth.ROLE_ADMIN)
    assert (await client.get("/dev/telemetry", headers=admin)).status_code == 200


async def test_신원_삭제는_감사_기록을_남긴다(client, flag_on):
    """지운 행위 자체가 기록으로 남는다(§8 결정 5 후반, 검증 F2 수리).

    이 창구는 신원 기록을 되돌리는 유일한 갈래라, 흔적이 없으면 "누가 언제 무엇을 지웠나"가
    통째로 사라진다. 명부 행이 아니라서 `staff_id=0`으로 쌓인다.

    ⚠ 지울 값이 있었을 때만 남는다 — 멱등 재호출까지 세면 기록이 "지운 적 없는 삭제"로
    부풀어 감사 값이 떨어진다.
    """
    from app.models import StaffAudit

    alert_id = await _seed_alert()
    leader = await _session_cookie("leaderaudit", auth.ROLE_LEADER)
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "홍길동"},
        headers=leader,
    )

    assert (
        await client.delete(f"/api/alerts/{alert_id}/identify", headers=leader)
    ).status_code == 200

    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(StaffAudit)
                    .where(StaffAudit.action == "identity_clear")
                    .order_by(StaffAudit.id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1, "신원 삭제가 감사 기록을 안 남겼다"
    row = rows[0]
    assert row.staff_id == 0
    assert row.actor_role == auth.ROLE_LEADER
    assert row.actor_username == "leaderaudit"
    # ⚠ 감사에는 원문이 아니라 마스킹만 남는다(F8) — 그 테이블은 삭제 창구가 없어서,
    # 원문을 넣으면 "파기했다"고 믿은 신원이 영구히 남는다.
    assert row.before["identified_person"] == {"initial": "홍", "length": 3}
    assert row.after["alert_id"] == alert_id

    # 멱등 재호출은 기록을 안 늘린다.
    assert (
        await client.delete(f"/api/alerts/{alert_id}/identify", headers=leader)
    ).status_code == 200
    async with get_session() as session:
        again = (
            await session.execute(
                select(StaffAudit).where(StaffAudit.action == "identity_clear")
            )
        ).scalars().all()
    assert len(again) == 1, "빈 신원을 다시 지웠는데 기록이 늘었다"


async def test_revoked_session_is_rejected_next_request(client, flag_on):
    """로그아웃·강등이 **다음 요청부터** 먹히는 게 세션을 고른 이유다(§1.1)."""
    cookie = await _session_cookie("agent3", auth.ROLE_AGENT)
    assert (await client.get("/api/robots", headers=cookie)).status_code == 200

    assert (await client.post("/api/auth/logout", headers=cookie)).status_code == 204
    assert (await client.get("/api/robots", headers=cookie)).status_code == 401


# ── 4. ⭐ 자칭 문자열 제거 (§3) ────────────────────────────────────────────
#
# 공용 키 하나로는 서버가 "정말 그 사람인가"를 못 판정해서, 감사 기록에 남의 이름을 적어
# 넣는 길이 열려 있었다. 세션이 붙으면 그 칸을 서버가 안다.


async def test_ack_records_session_display_name(client, flag_on):
    cookie = await _session_cookie("agent4", auth.ROLE_AGENT)
    alert_id = await _seed_alert()

    assert (
        await client.post(f"/api/alerts/{alert_id}/ack", headers=cookie)
    ).status_code == 200
    assert (await _read_alert(alert_id)).acked_by == "이름-agent4"


async def test_status_transition_ignores_self_claimed_actor(client, flag_on):
    cookie = await _session_cookie("agent5", auth.ROLE_AGENT)
    alert_id = await _seed_alert()

    r = await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "ACKED", "actor": "남의이름"},
        headers=cookie,
    )
    assert r.status_code == 200, r.text
    assert (await _read_alert(alert_id)).acked_by == "이름-agent5", (
        "본문 자칭 문자열이 감사 기록을 이겼다"
    )


async def test_resolved_transition_ignores_self_claimed_actor(client, flag_on):
    """RESOLVED 전이도 세션 실명이 이긴다 (2026-08-02 백지 검토 F67).

    ACKED 갈래만 재고 있었는데, `apply_transition`은 `acked_by`와 `resolved_by`를 **다른 if
    안에서** 찍는다(app/alert_lifecycle.py). 한쪽만 재면 종결 기록이 자칭 문자열로 되돌아가도
    초록이다. 그리고 종결은 "이 사건을 누가 닫았나"라 감사 값이 제일 무거운 칸이다.

    ⚠ RESOLVED는 `resolution`이 필수다(없으면 422). 그 값이 빠지면 게이트가 아니라 스키마가
      막아서, 실명 덮어쓰기를 재기 전에 시험이 다른 이유로 떨어진다.
    """
    cookie = await _session_cookie("agent8", auth.ROLE_AGENT)
    alert_id = await _seed_alert()

    r = await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "RESOLVED", "resolution": "confirmed", "actor": "남의이름"},
        headers=cookie,
    )
    assert r.status_code == 200, r.text

    alert = await _read_alert(alert_id)
    assert alert.status == "RESOLVED"
    assert alert.resolution == "confirmed"
    assert alert.resolved_by == "이름-agent8", (
        "종결 기록에 본문 자칭 문자열이 남았다 — 누가 닫았는지가 위조 가능하다"
    )
    # 건너뛴 종결이라 관제 확인 기록은 안 찍힌다(수명주기 계약). 여기가 뒤집히면 "관제가
    # 안 본 채 일괄 종결한 건"과 "확인한 건"이 같은 모양이 된다.
    assert alert.acked_by is None and alert.ack is False


async def test_identify_overwrites_self_claimed_identified_by(client, flag_on):
    """신원 기록이 상위 보고선으로 나가는 감사 자료라, 여기가 제일 위험한 자리다."""
    cookie = await _session_cookie("agent6", auth.ROLE_AGENT)
    alert_id = await _seed_alert()

    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "홍길동 학생", "identified_by": "남의이름"},
        headers=cookie,
    )
    assert r.status_code == 200, r.text

    alert = await _read_alert(alert_id)
    assert alert.identified_by == "이름-agent6"
    # 수명주기 쪽 기록도 같은 값을 봐야 한다 — 두 칸이 갈리면 어느 쪽이 진짜인지가 모호해진다.
    assert alert.status == "IDENTIFIED"


# ── 5. WS 대시보드 세션 전환 (§5.1) ────────────────────────────────────────
#
# ⚠ 갈래를 두 층으로 나눠 잰다.
#   - **핸드셰이크 전체**(TestClient) — DB를 안 타는 갈래만. 익명 거절·로봇 채널이 그것이다.
#   - **판정 함수**(`ws_dashboard_allowed`) — 세션 조회처럼 DB를 타는 갈래. TestClient는 자기
#     이벤트 루프를 따로 돌려서, 거기서 DB를 타면 커넥션 풀이 두 루프에 걸쳐 갈라진다
#     (실측 — `got Future attached to a different loop`). 판정이 함수 하나에 모여 있어서
#     그 함수를 직접 부르면 계약을 그대로 잰다.


def test_ws_dashboard_rejects_anonymous_when_flag_on(flag_on):
    """켠 뒤엔 쿠키 없는 핸드셰이크가 1008로 닫힌다.

    403으로 자르지 않는 이유는 브라우저가 그때 1006만 받아 무한 재접속을 돌기 때문이다
    (main.py `reject_unauthorized` 실측 주석).
    """
    _assert_ws_closed("/ws/dashboard")


def test_ws_robot_stays_on_device_key(flag_on):
    """로봇 채널은 기기 쪽이라 로그인이 안 건드린다(§5.2).

    젯슨·라파이는 사람이 아니라 세션을 못 쥔다. 여기까지 세션으로 바꾸면 로봇이 통째로
    끊긴다 — 그 채널을 잠그는 건 별건 스위치(`WS_REQUIRE_API_KEY_ROBOT`)다.
    """
    assert _ws_first("/ws/robot")["type"] == "registered"


async def test_ws_dashboard_allows_session_cookie(flag_on):
    """살아 있는 요원 세션 쿠키면 통과한다 — 쿠키는 같은 오리진 핸드셰이크에 자동으로 실린다."""
    user = await _make_user("wsagent", auth.ROLE_AGENT)
    async with get_session() as session:
        token = await auth.create_session(session, user)

    ok = await ws_dashboard_allowed(
        _fake_ws(cookie=f"{auth.SESSION_COOKIE_NAME}={token}")
    )
    assert ok is True
    assert await ws_dashboard_allowed(_fake_ws(cookie="c207_session=no-such")) is False
    assert await ws_dashboard_allowed(_fake_ws()) is False


async def test_ws_dashboard_does_not_fall_back_to_dashboard_key(flag_on, monkeypatch):
    """⭐ 켠 뒤 `DASHBOARD_API_KEY`는 폴백으로 안 남는다 — 남기면 세션 우회 뒷문이다(§5.1)."""
    monkeypatch.setenv("DASHBOARD_API_KEY", "dash-key-for-gate-test")
    monkeypatch.setenv("WS_REQUIRE_API_KEY_DASHBOARD", "true")
    get_settings.cache_clear()
    try:
        allowed = await ws_dashboard_allowed(
            _fake_ws(query="api_key=dash-key-for-gate-test")
        )
        assert allowed is False, "세션을 켠 뒤에도 대시보드 키가 통했다"
    finally:
        get_settings.cache_clear()


async def test_ws_dashboard_keeps_key_branch_when_flag_off(monkeypatch):
    """제1 불변 — 꺼진 구간에선 옛 키 판정 그대로다(기본 꺼짐이라 익명도 통과)."""
    assert get_settings().auth_require_login is False
    assert await ws_dashboard_allowed(_fake_ws()) is True

    monkeypatch.setenv("DASHBOARD_API_KEY", "dash-key-for-gate-test")
    monkeypatch.setenv("WS_REQUIRE_API_KEY_DASHBOARD", "true")
    get_settings.cache_clear()
    try:
        assert await ws_dashboard_allowed(_fake_ws()) is False
        assert (
            await ws_dashboard_allowed(_fake_ws(query="api_key=dash-key-for-gate-test"))
        ) is True
    finally:
        get_settings.cache_clear()
