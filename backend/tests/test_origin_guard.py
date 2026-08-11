"""교차 오리진 상태변경 차단 — 로그인 설계 확정본 §1.2의 보강 한 겹.

계약은 짧다. **상태변경 메서드(POST·PUT·PATCH·DELETE)에 `Origin`이 실려 있고 그 값이 자기
오리진이 아니면 403**이다. 그 밖은 전부 통과다.

## 여기서 재는 것 여덟

1. **교차 오리진 POST는 403** — 라우팅보다 앞이라 없는 경로도 404가 아니라 403이다.
2. **같은 오리진 POST는 그대로** — 화면이 자기 서버를 부르는 갈래가 안 막힌다.
3. **⭐ `Origin`이 없으면 통과** — 젯슨 인입·curl·젠킨스가 이 갈래다. 제1 불변의 증거이기도
   하다. 기존 시험 전부가 헤더 없이 부르므로, 이게 깨지면 저장소가 통째로 빨개진다.
4. **GET·OPTIONS·HEAD는 오리진이 달라도 통과** — 상태를 안 바꾸는 메서드는 판정 밖이다.
5. **프록시 뒤 스킴** — nginx가 443에서 받아 http로 넘기므로 `X-Forwarded-Proto`를 먼저
   본다. 안 보면 배포된 화면의 모든 상태변경이 403이 된다.
6. **⭐ 로컬 오리진은 설정 없이 통과** — 로컬 정적 서버로 화면을 띄우는 갈래다. 안 열면
   로컬 개발의 모든 상태변경이 403이다.
7. **`Origin: null`은 403** — 샌드박스 iframe·`data:` 문서가 싣는 값이라 신뢰할 근거가 없다.
8. **Host를 못 읽으면 닫는다** — 자기 오리진을 모르는 상태에서 여는 쪽으로 가면 방어가 없다.

## 같은 경계에 붙은 둘 (2026-08-02 백지검토 F18·F2)

9. **WebSocket 핸드셰이크도 오리진을 본다** — 미들웨어는 안 타지만(ASGI scope 타입으로 갈라
   흘려보낸다) 대시보드 채널이 `ws_origin_allowed`로 **같은 판정 함수**를 부른다. WS에는
   CORS가 없어서 이걸 안 걸면 남의 팀 페이지가 관제 스트림을 그대로 구독한다.
   ⚠ Origin 없는 갈래(젯슨 로봇 채널)는 지금처럼 통과해야 한다.
10. **`/docs`·`/redoc`·`/openapi.json`은 404** — 앱이 라우트를 아예 안 단다(§8 결정 2).
"""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import get_settings
from app.main import WS_UNAUTHORIZED_CODE, app
from app.security import OriginGuardMiddleware

pytestmark = pytest.mark.asyncio(loop_scope="session")

# httpx 시험 클라이언트의 base_url이 http://test다 — Host 헤더가 `test`라 자기 오리진이 이 값이다.
SELF_ORIGIN = "http://test"
OTHER_ORIGIN = "http://evil.example"


@pytest.fixture
def allow_origin(monkeypatch):
    """추가 허용 오리진 하나를 설정에 넣는다. 설정이 lru_cache라 캐시를 앞뒤로 비운다."""

    def _apply(value: str) -> None:
        monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", value)
        get_settings.cache_clear()

    try:
        yield _apply
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ── 1. 교차 오리진 상태변경은 403 ──────────────────────────────────────────


@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/shuttle-calls", {"gate_no": 1}),      # 원래 익명 200인 창구
        ("/api/tagging-events", {}),                  # 원래 키가 없으면 401인 창구
        ("/api/eopneun-gyeongro", {}),                # 원래 404인 경로
    ],
)
async def test_cross_origin_post_is_blocked(client, path, body):
    """교차 오리진 POST는 창구가 무엇이든 403이다.

    없는 경로까지 403인 게 중요하다 — 판정이 **라우팅보다 앞**이라는 뜻이라, 창구가 새로
    생겨도 이 방어를 자동으로 받는다. 창구마다 손으로 걸면 언젠가 한 자리를 빠뜨린다.
    """
    r = await client.post(path, json=body, headers={"Origin": OTHER_ORIGIN})
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "허용되지 않은 요청 출처입니다."


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
async def test_every_state_changing_method_is_covered(client, method):
    """POST 말고 PUT·PATCH·DELETE도 같이 막힌다. 한 메서드만 열려 있으면 방어가 아니다."""
    call = getattr(client, method)
    r = await call("/api/eopneun-gyeongro", headers={"Origin": OTHER_ORIGIN})
    assert r.status_code == 403, r.text


# ── 2. 같은 오리진은 그대로 ────────────────────────────────────────────────


async def test_same_origin_post_passes(client):
    """화면이 자기 서버를 부르는 갈래는 안 막힌다. 창구가 제 일을 하고 200이 나온다."""
    r = await client.post(
        "/api/shuttle-calls", json={"gate_no": 2}, headers={"Origin": SELF_ORIGIN}
    )
    assert r.status_code == 200, r.text


async def test_origin_case_and_trailing_slash_do_not_matter(client):
    """스킴·호스트 대소문자와 끝 슬래시는 같은 오리진으로 본다(대조 전에 한 모양으로 접는다)."""
    r = await client.post(
        "/api/shuttle-calls", json={"gate_no": 3}, headers={"Origin": "HTTP://TEST/"}
    )
    assert r.status_code == 200, r.text


# ── 3. ⭐ Origin이 없으면 통과 (제1 불변의 증거) ────────────────────────────


async def test_post_without_origin_passes_untouched(client):
    """헤더가 없는 호출은 판정을 아예 안 탄다 — 기기·서버 갈래(젯슨·curl·젠킨스)다.

    "막히지 않았다"를 창구의 **원래 응답**으로 잰다. 키 없는 인입은 401이고 403이 아니다.
    """
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 4})).status_code == 200
    assert (await client.post("/api/tagging-events", json={})).status_code == 401


async def test_device_ingest_with_key_still_works_without_origin(client, auth_headers):
    """기기 키를 든 인입이 그대로 산다. 이 갈래가 막히면 젯슨·라파이가 통째로 선다."""
    r = await client.post(
        "/api/tagging-events",
        json={
            "event_id": "raspberry01-1700000009000-0",
            "device_id": "raspberry01",
            "gate_no": 1,
            "tag_id": "20250001",
            "observed_at": "2026-08-02T09:00:00+09:00",
        },
        headers=auth_headers,
    )
    assert r.status_code in (200, 201), r.text


# ── 4. 상태를 안 바꾸는 메서드는 판정 밖 ───────────────────────────────────


@pytest.mark.parametrize("path", ["/healthz", "/api/alerts", "/api/robots"])
async def test_get_passes_from_any_origin(client, path):
    """GET은 오리진이 달라도 통과한다. 상태를 안 바꾸는 자리까지 막으면 화면 조회가 죽는다."""
    r = await client.get(path, headers={"Origin": OTHER_ORIGIN})
    assert r.status_code == 200, r.text


async def test_options_and_head_pass_from_any_origin(client):
    """OPTIONS·HEAD도 교차 오리진에서 통과한다 — 판정 밖 메서드라는 걸 못박는다.

    OPTIONS 면제의 근거가 "프리플라이트를 자르면 화면이 막힌다"가 아니라는 게 중요하다. 이
    앱에는 CORSMiddleware가 없어서 교차 오리진 프리플라이트는 허용 헤더를 못 받고 브라우저가
    그 자리에서 자른다 — 여기서 통과시켜도 본 요청은 못 나간다. 그래서 여는 게 구멍이 아니다.

    "안 막혔다"를 **라우팅이 답했다**로 잰다. 이 앱은 OPTIONS·HEAD 핸들러를 아무 데도 안 달아
    둬서(FastAPI는 starlette과 달리 GET 라우트에 HEAD를 자동으로 안 붙인다) 둘 다 405가
    정상이다. 405는 라우팅까지 갔다는 뜻이고, 미들웨어가 잘랐으면 라우팅 앞이라 403이었다 —
    없는 경로 POST가 404가 아니라 403인 위 시험이 그 대조군이다.
    """
    for method, path in (("options", "/api/shuttle-calls"), ("head", "/healthz")):
        r = await getattr(client, method)(path, headers={"Origin": OTHER_ORIGIN})
        assert r.status_code != 403, f"{method} {path}가 오리진으로 막혔다: {r.text}"
        assert r.status_code == 405, f"{method} {path}: {r.status_code}"


# ── 5. 설정으로 여는 자리 ──────────────────────────────────────────────────


async def test_configured_origin_is_allowed(client, allow_origin):
    """`AUTH_ALLOWED_ORIGINS`에 적은 오리진은 통과한다(오리진이 갈리는 개발 자리)."""
    allow_origin(f"{OTHER_ORIGIN},http://localhost:5173")
    assert (
        await client.post(
            "/api/shuttle-calls", json={"gate_no": 5}, headers={"Origin": OTHER_ORIGIN}
        )
    ).status_code == 200

    assert (
        await client.post(
            "/api/shuttle-calls",
            json={"gate_no": 6},
            headers={"Origin": "http://localhost:5173"},
        )
    ).status_code == 200


async def test_configured_origin_does_not_open_everything(client, allow_origin):
    """적은 값만 열린다 — 목록에 없는 오리진은 그대로 403이다."""
    allow_origin("http://localhost:5173")
    r = await client.post(
        "/api/shuttle-calls", json={"gate_no": 7}, headers={"Origin": OTHER_ORIGIN}
    )
    assert r.status_code == 403


# ── 6. 프록시 뒤 스킴 (배포에서 제일 위험한 자리) ──────────────────────────


async def test_forwarded_proto_decides_the_scheme(client):
    """nginx가 443에서 받아 컨테이너로 http로 넘긴다 — 그때 브라우저 Origin은 https다.

    `X-Forwarded-Proto`를 안 보면 자기 오리진이 늘 `http://...`로 계산돼서 **배포된 화면의
    모든 상태변경 요청이 403**이 된다. 그 갈래를 여기서 못박는다.
    """
    ok = await client.post(
        "/api/shuttle-calls",
        json={"gate_no": 8},
        headers={"Origin": "https://test", "X-Forwarded-Proto": "https"},
    )
    assert ok.status_code == 200, ok.text

    # 프록시가 여럿이면 `https, http`처럼 쌓인다. 맨 앞이 바깥에서 들어온 스킴이다.
    chained = await client.post(
        "/api/shuttle-calls",
        json={"gate_no": 9},
        headers={"Origin": "https://test", "X-Forwarded-Proto": "https, http"},
    )
    assert chained.status_code == 200, chained.text


async def test_scheme_mismatch_without_the_proxy_header_is_blocked(client):
    """그 헤더가 없으면 스킴이 다른 오리진은 남이다 — 판정이 헐거워지지 않았다는 반증."""
    r = await client.post(
        "/api/shuttle-calls", json={"gate_no": 1}, headers={"Origin": "https://test"}
    )
    assert r.status_code == 403


# ── 7. ⭐ 로컬 오리진은 설정 없이 통과 (로컬 개발이 통째로 서는 자리) ────────


@pytest.mark.parametrize(
    "origin",
    [
        # ⚠ 포트 번호에 뜻이 없다. "로컬이면 포트·스킴과 무관하게 통과"를 재는 표본이라
        #   아무 포트나 골라도 되고, 프론트가 실제로 쓰는 포트가 바뀌어도 안 고친다.
        "http://localhost:5173",     # 로컬 개발 서버
        "http://127.0.0.1:4173",     # 다른 포트·다른 표기
        "http://localhost",          # 포트 없는 로컬
        "https://localhost:5173",    # https로 띄운 로컬
        "http://127.0.0.1:8000",     # 백엔드를 직접 연 탭
    ],
)
async def test_local_origins_pass_without_configuration(client, origin):
    """⭐ localhost·127.0.0.1은 포트·스킴과 무관하게 **설정 없이** 통과한다.

    안 열면 로컬 개발이 통째로 선다 — 화면을 로컬 서버로 띄우면 Origin 이 그 서버 주소
    그대로 실리는데 요청은 백엔드(다른 포트)로 가서,
    자기 오리진 대조가 모든 상태변경을 403으로 떨어뜨린다(`AUTH_ALLOWED_ORIGINS`를 안 적은
    새 팀원의 기본 상태가 정확히 이것이다).

    열어도 방어는 안 헐거워진다. 이 가드가 막는 건 "사용자가 연 남의 웹페이지가 브라우저를
    시켜 우리 서버에 쓰기를 보내는 것"인데, 그 공격 페이지의 오리진은 공격자 도메인이지
    localhost일 수 없다. 사용자 기기 안의 악성 프로세스는 브라우저 없이 직접 부르면 되고
    (Origin을 안 실으면 그만이다) 그건 애초에 이 가드가 막을 수 있는 자리가 아니다.
    """
    r = await client.post("/api/shuttle-calls", json={"gate_no": 1}, headers={"Origin": origin})
    assert r.status_code == 200, r.text


async def test_lookalike_hosts_are_not_local(client):
    """이름에 localhost가 들어간 남의 도메인은 로컬이 아니다 — 호스트를 통째로 대조한다."""
    for origin in ("http://localhost.evil.example", "http://notlocalhost", "http://127.0.0.2"):
        r = await client.post(
            "/api/shuttle-calls", json={"gate_no": 1}, headers={"Origin": origin}
        )
        assert r.status_code == 403, f"{origin}이 로컬로 통했다: {r.text}"


# ── 8. `Origin: null` (샌드박스 iframe·data: 문서) ──────────────────────────


async def test_null_origin_is_blocked(client):
    """문자열 `null`은 403이다. 지금 거동이 옳아서 못박는 것이지 바꾸려는 게 아니다.

    샌드박스 iframe·`data:` 문서·일부 리다이렉트가 이 값을 싣는다. 호스트가 없어 자기
    오리진과도 로컬과도 안 맞아 그대로 떨어진다 — "오리진을 잃은 문맥"이라 신뢰할 근거가
    없다. 정말 열어야 하면 `AUTH_ALLOWED_ORIGINS`에 적는 길이 남아 있다.
    """
    r = await client.post(
        "/api/shuttle-calls", json={"gate_no": 1}, headers={"Origin": "null"}
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"] == "허용되지 않은 요청 출처입니다."


# ── 9. Host를 못 읽으면 닫는다 (httpx로는 못 재는 갈래) ─────────────────────


async def test_missing_host_header_closes_the_gate():
    """Host가 없고 Origin이 남이면 403이다. 자기 오리진을 모르는 채 여는 길은 없다.

    이 갈래는 시험 클라이언트로 못 잰다 — httpx도 TestClient도 Host를 **늘** 싣기 때문에
    `request_self_origin`이 None을 돌려주는 분기가 무시험 사문으로 남는다. 그래서 ASGI
    scope를 직접 만들어 미들웨어를 부른다(HTTP/1.0 클라이언트·직접 조립한 ASGI 호출이
    실제로 이 모양이다).

    "안쪽 앱이 아예 안 불렸다"까지 같이 재는 게 중요하다 — 403 본문만 보면 창구가 돌고 나서
    응답만 갈아 끼운 경우와 구별이 안 된다.
    """
    scope = {
        "type": "http",
        "http_version": "1.0",
        "method": "POST",
        "path": "/api/shuttle-calls",
        "scheme": "http",
        "headers": [(b"origin", OTHER_ORIGIN.encode())],   # Host 없음
    }
    inner_called = []
    sent = []

    async def inner(scope, receive, send):
        inner_called.append(scope)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await OriginGuardMiddleware(inner)(scope, receive, send)

    assert inner_called == [], "Host 없는 교차 오리진 요청이 창구까지 갔다"
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 403


async def test_missing_host_still_honours_configured_origins(allow_origin):
    """Host를 못 읽어도 설정에 적힌 값은 통과한다 — 닫는 쪽이 "전부 막기"는 아니다."""
    allow_origin(OTHER_ORIGIN)
    scope = {
        "type": "http",
        "http_version": "1.0",
        "method": "POST",
        "path": "/api/shuttle-calls",
        "scheme": "http",
        "headers": [(b"origin", OTHER_ORIGIN.encode())],
    }
    inner_called = []

    async def inner(scope, receive, send):
        inner_called.append(scope)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    await OriginGuardMiddleware(inner)(scope, receive, send)
    assert len(inner_called) == 1


# ── 10. WebSocket 핸드셰이크 (F18 — 미들웨어 밖, 같은 판정 함수) ────────────
#
# 미들웨어는 여전히 WS를 안 탄다(ASGI scope 타입으로 갈라 흘려보낸다). 대신 대시보드 채널이
# `ws_origin_allowed`를 부르고, 그 함수가 HTTP 가드와 **같은 `origin_allowed`**를 본다 —
# 계약을 두 벌 적으면 한쪽만 고쳐진다.
#
# 핸드셰이크는 httpx 시험 클라이언트로 못 재서 TestClient를 쓴다(`test_auth_gates._ws_first`와
# 같은 모양이고, 그쪽도 async 시험 안에서 부른다).


def _connect(path: str, **kwargs) -> dict:
    """붙어서 첫 메시지 한 장을 받는다. 막히면 WebSocketDisconnect가 올라온다.

    막는 방식이 "accept 뒤 close(1008)"이라(main.py `reject_unauthorized`) 핸드셰이크 자체는
    성공한다. 그래서 "붙었나"가 아니라 **첫 메시지**로 판정한다.
    (`test_ws_auth._connect`와 같은 모양이다 — 같은 계약을 재는 자리라 잣대를 맞춘다.)
    """
    with TestClient(app) as tc, tc.websocket_connect(path, **kwargs) as ws:
        return ws.receive_json()


def _assert_ws_closed(path: str, **kwargs) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        _connect(path, **kwargs)
    assert caught.value.code == WS_UNAUTHORIZED_CODE


async def test_cross_origin_websocket_is_rejected():
    """⭐ 남의 오리진이 실린 대시보드 핸드셰이크는 1008로 닫힌다.

    WS에는 CORS가 없다. 운영 환경에서는 대시보드와 API를 동일한 site 아래에 배치하고,
    형제 팀 주소는 오리진만 다르고 같은 site라 Lax 쿠키가 핸드셰이크에 그대로 실린다 — 안
    막으면 남의 팀 페이지가 `new WebSocket(...)` 한 줄로 관제 이벤트 스트림을 통째로 구독한다.

    403으로 안 자르고 accept 뒤 1008로 닫는 건 기존 계약 그대로다(브라우저가 403이면 1006만
    받아 무한 재접속을 돈다).
    """
    _assert_ws_closed("/ws/dashboard", headers={"Origin": OTHER_ORIGIN})


async def test_same_origin_websocket_passes():
    """자기 오리진에서 연 화면은 그대로 붙는다. 이게 막히면 관제 화면이 통째로 죽는다.

    TestClient의 Host가 `testserver`고 WS 스킴이 `ws`라 자기 오리진은 `http://testserver`다.
    스킴을 `ws`인 채로 대조하면 자기 오리진이 `ws://testserver`가 돼 **모든 핸드셰이크**가
    남으로 보인다 — 그 자리를 여기서 잰다.
    """
    assert _connect("/ws/dashboard", headers={"Origin": "http://testserver"})["type"] == "hello"


async def test_local_origin_websocket_passes():
    """로컬 개발(로컬 서버로 띄운 화면)도 설정 없이 붙는다 — HTTP 가드와 같은 잣대다."""
    assert _connect("/ws/dashboard", headers={"Origin": "http://localhost:5173"})["type"] == "hello"


async def test_websocket_without_origin_still_passes():
    """⭐ Origin 없는 핸드셰이크는 지금처럼 통과한다 — 젯슨·`websockets`·wscat이 이 갈래다.

    브라우저는 WS 핸드셰이크에 Origin을 늘 싣기 때문에, 이 문이 브라우저 갈래를 여는 구멍이
    되지 않는다. 반대로 이게 막히면 로봇이 통째로 잠긴다.
    """
    assert _connect("/ws/dashboard")["type"] == "hello"


async def test_robot_channel_blocks_cross_origin_browsers():
    """⭐ 로봇 채널도 남의 오리진은 1008로 닫는다(2026-08-08 사용자 확정 · 백지검토 ②).

    ⛔ **예전에는 이 자리가 반대였다** — `test_robot_channel_is_not_origin_checked`가 "로봇
    채널은 오리진 판정을 아예 안 탄다"를 못으로 박아 뒀다. 그 사이 백지검토가 대가를 실측했다:
    이 채널이 익명인데 Origin 검사도 없어서, 남의 웹페이지가 붙어 로봇 ID를 주장하면 **진짜
    젯슨 소켓이 닫히고**(ws.py `claim_conflict`) 그 뒤 출동 명령이 그쪽으로 갔다.

    WS 인증 전체를 켜는 것은 발표 뒤로 정해진 별개 결정이고, 이 한 줄은 서버만 고쳐도 되며
    기기를 안 끊는다 — 아래 시험이 그 "안 끊긴다"를 같이 못박는다.
    """
    _assert_ws_closed("/ws/robot", headers={"Origin": OTHER_ORIGIN})


async def test_robot_channel_without_origin_still_passes():
    """⭐ 기기는 그대로 붙는다 — Origin 없는 갈래(젯슨 web_bridge·`websockets`·wscat)다.

    이 시험이 위 가드의 짝이다. 위만 있으면 "다 막았다"가 통과하므로, 막지 **말아야 할** 쪽을
    여기서 나란히 잰다. 이게 빨개지면 로봇이 통째로 잠긴 것이다.
    """
    assert _connect("/ws/robot")["type"] == "registered"


async def test_robot_channel_same_origin_passes():
    """우리 화면 오리진은 로봇 채널에도 통과한다 — 판정 함수가 대시보드와 같은 하나여서다."""
    assert _connect(
        "/ws/robot", headers={"Origin": "http://testserver"}
    )["type"] == "registered"


# ── 11. 문서 창구는 닫혔다 (F2 — 로그인 설계 §8 결정 2) ─────────────────────


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_api_docs_are_closed(client, path):
    """세 창구가 404다. 권한 판정이 아니라 **라우트 부재**라 익명·인증 구분이 없다.

    쓰기 창구 지도(신원 입력·명령 전송까지)가 통째로 담긴 자리라 익명에게 열어 둘 수 없다.
    nginx 조각에서도 같은 셋을 404로 끊었지만 그쪽은 손 배포라, 앱 층이 실제로 닫혀 있는지를
    여기서 잰다.
    """
    assert (await client.get(path)).status_code == 404


async def test_openapi_schema_is_still_generatable():
    """HTTP 창구만 없앤 것이지 명세가 사라진 게 아니다 — 도구는 `app.openapi()`로 읽는다.

    이 구분을 못박아 두지 않으면 "문서를 껐으니 스키마 검사도 못 한다"로 잘못 읽힌다.
    """
    spec = app.openapi()
    assert spec["info"]["title"] == "C207 싸큐리티 관제 서버"
    assert "/api/alerts" in spec["paths"]
