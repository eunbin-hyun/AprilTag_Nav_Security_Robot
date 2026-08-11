"""WS 핸드셰이크 인증 (S15P11C207-238).

증명할 것 여섯.
1. 기본값(플래그 전부 꺼짐)에서 두 채널이 예전처럼 익명으로 열린다 — EC2 현 배포 거동 불변이
   이 카드의 계약이다. 이게 깨지면 배포가 즉시 죽는다.
2. 켜면 키를 세 창구(X-API-Key·Bearer·쿼리) 어느 쪽으로 실어도 통과하고, 틀린 키는 막힌다.
3. 채널 스위치가 서로 독립이다 — 로봇만 켜도 대시보드는 열려 있다.
4. 비ASCII 키가 와도 500이 아니라 조용한 거절이다(compare_digest str 함정).
5. **채널마다 기대하는 키가 다르다** — 대시보드는 기기 키(API_KEY)를 안 받고
   DASHBOARD_API_KEY만 받는다(카드 원문 "기기용 키와 사람용 인증을 갈라야 합니다").
6. **못 쓰는 자물쇠는 닫는다** — 빈 키·저장소 기본키(dev-local-key)로는 아무도 못 들어온다.

⚠ 이 파일은 starlette TestClient라 **전선 위 거동은 못 본다**. 거절 코드(1008)·헤더 인코딩
  같은 건 TestClient가 합성한 값이 통과해 버려서, 실 uvicorn 판은 test_ws_auth_live.py에 있다.
  둘은 짝이다 — 여기에 케이스를 더할 때 전선이 걸리는 항목이면 저기에도 넣는다.

플래그는 env+`get_settings.cache_clear()`로 바꾼다. lru_cache 인스턴스 속성을 직접 대입하면
xdist 병렬에서 값이 갈린다 — env가 늘 정본인 쪽이 안전하다(test_room_scope_draft와 같은 방식).

DB를 안 타는 경로만 고른다(hello·registered까지만 받고 끊는다). 그래서 동기 TestClient를
그대로 쓰고 커넥션 풀은 안 건드린다.
"""
import contextlib

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import INSECURE_DEFAULT_API_KEY, Settings, get_settings
from app.main import WS_UNAUTHORIZED_CODE, app
from app.security import _ws_auth_required, bearer_token, keys_match, ws_expected_key

API_KEY = "ws-auth-test-key"
DASH_KEY = "dashboard-only-key"
WRONG_KEY = "ws-auth-test-key-wrong"
# 한글 키. compare_digest에 str로 그냥 넣으면 TypeError → 500이 나던 자리다.
NON_ASCII_KEY = "열쇠키한글"
# 둘레에 공백이 붙은 키. 창구마다 공백을 떼는 규칙이 달라 갈라지던 자리다.
SPACED_KEY = " sp key "


@contextlib.contextmanager
def ws_flags(
    *, master=False, robot=False, dashboard=False, api_key=API_KEY, dashboard_key=DASH_KEY
):
    """WS 인증 플래그와 키 둘을 잠깐 바꾼다. 빠져나갈 때 되돌린다."""
    mp = pytest.MonkeyPatch()
    mp.setenv("API_KEY", api_key)
    mp.setenv("DASHBOARD_API_KEY", dashboard_key)
    mp.setenv("WS_REQUIRE_API_KEY", "true" if master else "false")
    mp.setenv("WS_REQUIRE_API_KEY_ROBOT", "true" if robot else "false")
    mp.setenv("WS_REQUIRE_API_KEY_DASHBOARD", "true" if dashboard else "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


def _connect(path: str, **kwargs) -> dict:
    """붙어서 첫 메시지 한 장을 받는다. 막히면 WebSocketDisconnect가 올라온다."""
    with TestClient(app) as client, client.websocket_connect(path, **kwargs) as ws:
        return ws.receive_json()


def _assert_open(path: str, expected_type: str, **kwargs) -> None:
    assert _connect(path, **kwargs)["type"] == expected_type


def _assert_closed(path: str, **kwargs) -> None:
    with pytest.raises(WebSocketDisconnect) as caught:
        _connect(path, **kwargs)
    assert caught.value.code == WS_UNAUTHORIZED_CODE


# ── 1. 기본값 = 현 배포 거동 불변 ────────────────────────────────────────────


def test_default_settings_leave_both_channels_anonymous():
    """플래그를 아무것도 안 건드린 상태. EC2가 지금 도는 모습 그대로다."""
    settings = get_settings()
    assert settings.ws_require_api_key is False
    assert settings.ws_require_api_key_robot is False
    assert settings.ws_require_api_key_dashboard is False
    assert settings.ws_auth_required_robot is False
    assert settings.ws_auth_required_dashboard is False


def test_dashboard_key_default_is_empty():
    """대시보드 키는 기본이 빈 값이다 — 기기 키를 물려받지 않는다.

    여기서 기본값이 API_KEY를 따라가게 바뀌면 두 키를 갈라놓은 게 도로 합쳐진다.
    """
    assert Settings.model_fields["dashboard_api_key"].default == ""


def test_flags_off_allows_anonymous_on_both_channels():
    with ws_flags():
        _assert_open("/ws/dashboard", "hello")
        _assert_open("/ws/robot", "registered")


# ── 2. 켜짐 × 창구 세 종류 × 틀린 키 ─────────────────────────────────────────


def test_robot_flag_on_rejects_anonymous():
    with ws_flags(robot=True):
        _assert_closed("/ws/robot")


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"headers": {"x-api-key": API_KEY}}, id="x-api-key"),
        pytest.param({"headers": {"authorization": f"Bearer {API_KEY}"}}, id="bearer"),
        # 젯슨 web_bridge는 소문자가 아니라 'Bearer'로 싣지만, 스킴 대소문자는 안 따진다.
        pytest.param({"headers": {"authorization": f"bearer {API_KEY}"}}, id="bearer-소문자"),
        pytest.param({"params": {"api_key": API_KEY}}, id="쿼리"),
    ],
)
def test_robot_flag_on_accepts_every_key_channel(kwargs):
    with ws_flags(robot=True):
        _assert_open("/ws/robot", "registered", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"headers": {"x-api-key": WRONG_KEY}}, id="x-api-key"),
        pytest.param({"headers": {"authorization": f"Bearer {WRONG_KEY}"}}, id="bearer"),
        pytest.param({"params": {"api_key": WRONG_KEY}}, id="쿼리"),
        # Bearer가 아닌 스킴은 키로 안 친다.
        pytest.param({"headers": {"authorization": f"Basic {API_KEY}"}}, id="스킴-틀림"),
        # 토큰 없는 Bearer.
        pytest.param({"headers": {"authorization": "Bearer"}}, id="토큰-없음"),
        # 빈 값은 통과가 아니라 거절이다.
        pytest.param({"headers": {"x-api-key": ""}}, id="빈-헤더"),
    ],
)
def test_robot_flag_on_rejects_bad_keys(kwargs):
    with ws_flags(robot=True):
        _assert_closed("/ws/robot", **kwargs)


def test_any_matching_channel_passes_even_if_another_is_wrong():
    """세 창구는 동급이라 하나라도 맞으면 통과한다(프록시가 헤더를 덧붙이는 경우)."""
    with ws_flags(robot=True):
        _assert_open(
            "/ws/robot",
            "registered",
            headers={"x-api-key": WRONG_KEY, "authorization": f"Bearer {API_KEY}"},
        )


# ── 3. 채널 독립 ─────────────────────────────────────────────────────────────


def test_robot_flag_does_not_close_dashboard():
    with ws_flags(robot=True):
        _assert_closed("/ws/robot")
        _assert_open("/ws/dashboard", "hello")


def test_dashboard_flag_does_not_close_robot():
    with ws_flags(dashboard=True):
        _assert_closed("/ws/dashboard")
        _assert_open("/ws/robot", "registered")


def test_dashboard_flag_on_accepts_query_key():
    """브라우저 WebSocket은 헤더를 못 실어서 쿼리가 유일한 길이다."""
    with ws_flags(dashboard=True):
        _assert_open("/ws/dashboard", "hello", params={"api_key": DASH_KEY})


def test_master_switch_closes_both_channels():
    """예전 WS_REQUIRE_API_KEY는 둘 다 켜는 상위 스위치로 남는다(하위 호환)."""
    with ws_flags(master=True):
        _assert_closed("/ws/robot")
        _assert_closed("/ws/dashboard")
        _assert_open("/ws/robot", "registered", headers={"x-api-key": API_KEY})
        _assert_open("/ws/dashboard", "hello", params={"api_key": DASH_KEY})


def test_master_switch_wins_over_channel_flags_left_off():
    with ws_flags(master=True, robot=False, dashboard=False):
        settings = get_settings()
        assert settings.ws_auth_required_robot is True
        assert settings.ws_auth_required_dashboard is True


# ── 5. 기기 키와 화면 키를 갈라 둔다 (카드 원문) ─────────────────────────────


def test_dashboard_rejects_device_key():
    """대시보드는 기기 키를 안 받는다.

    카드 원문이 막으려는 게 바로 이 조합이다 — 브라우저가 기기 키를 들면 개발자 도구에
    그대로 보이고, 그 키 한 장이 인입·명령 HTTP 창구까지 통째로 연다. 세 창구 전부 막는다.
    """
    with ws_flags(dashboard=True):
        _assert_closed("/ws/dashboard", params={"api_key": API_KEY})
        _assert_closed("/ws/dashboard", headers={"x-api-key": API_KEY})
        _assert_closed("/ws/dashboard", headers={"authorization": f"Bearer {API_KEY}"})


def test_robot_rejects_dashboard_key():
    """반대 방향도 막힌다 — 화면 키로 로봇 채널에 못 붙는다(가짜 로봇 방지)."""
    with ws_flags(robot=True):
        _assert_closed("/ws/robot", headers={"x-api-key": DASH_KEY})
        _assert_closed("/ws/robot", params={"api_key": DASH_KEY})


def test_dashboard_key_missing_closes_channel():
    """스위치만 켜고 DASHBOARD_API_KEY를 안 넣으면 아무도 못 들어온다(열어 두는 쪽이 나쁘다)."""
    with ws_flags(dashboard=True, dashboard_key=""):
        assert ws_expected_key("dashboard") is None
        _assert_closed("/ws/dashboard")
        _assert_closed("/ws/dashboard", params={"api_key": API_KEY})


def test_expected_key_is_per_channel():
    with ws_flags(master=True):
        assert ws_expected_key("robot") == API_KEY
        assert ws_expected_key("dashboard") == DASH_KEY
        # 갈래가 늘었는데 여기를 안 고치면 기대 키가 없어 닫힌다.
        assert ws_expected_key("오타친채널") is None


# ── 6. 못 쓰는 자물쇠는 닫는다 ───────────────────────────────────────────────


def test_repo_default_key_is_not_a_lock():
    """API_KEY를 안 넣고 스위치만 켜면 닫힌다.

    기본키는 저장소(.env.example·config.py)에 그대로 적혀 있어서, 통과시키면 켜 놓고 잠긴
    줄 아는 보안극장이 된다. 수리 전에는 실 uvicorn에서 이 조합이 OPEN이었다.
    """
    with ws_flags(robot=True, api_key=INSECURE_DEFAULT_API_KEY):
        assert ws_expected_key("robot") is None
        _assert_closed("/ws/robot", headers={"x-api-key": INSECURE_DEFAULT_API_KEY})
        _assert_closed("/ws/robot")


def test_insecure_default_constant_matches_settings_default():
    """상수와 기본값이 갈라지면 위 거절이 조용히 풀린다. 한 자리에서 붙들어 둔다."""
    assert Settings.model_fields["api_key"].default == INSECURE_DEFAULT_API_KEY


def test_blank_key_closes_channel():
    """공백만 든 키도 빈 키다."""
    with ws_flags(robot=True, api_key="   "):
        assert ws_expected_key("robot") is None
        _assert_closed("/ws/robot", headers={"x-api-key": "   "})


# ── 7. 창구 셋이 정말 동급인가 (인코딩·공백) ─────────────────────────────────


def test_utf8_non_ascii_key_opens_on_every_channel():
    """한글 키를 전선 UTF-8 바이트로 실으면 헤더 둘도 쿼리와 똑같이 통과한다.

    ⚠ 헤더는 **bytes로** 넣어야 이 자리를 찌른다. str로 주면 클라이언트가 먼저 인코딩을
      정해 버려 서버가 받는 바이트가 실제 젯슨과 달라진다.
    수리 전에는 헤더 둘만 잘렸다(쿼리는 UTF-8, 헤더는 latin-1로 디코드돼 바이트가 갈렸다).
    """
    raw = NON_ASCII_KEY.encode("utf-8")
    with ws_flags(robot=True, api_key=NON_ASCII_KEY):
        _assert_open("/ws/robot", "registered", headers={"x-api-key": raw})
        _assert_open("/ws/robot", "registered", headers={"authorization": b"Bearer " + raw})
        _assert_open("/ws/robot", "registered", params={"api_key": NON_ASCII_KEY})


def test_spaced_key_opens_on_every_channel():
    """둘레 공백이 붙은 키도 창구 셋이 같은 답을 낸다.

    HTTP 헤더 파서는 값 둘레 공백을 떼고 올리는데 쿼리는 안 뗀다. 수리 전에는 그래서
    `API_KEY=" sp key "`가 쿼리로만 통과했다(실 uvicorn 실측). 안쪽 공백은 그대로 대조한다.
    """
    with ws_flags(robot=True, api_key=SPACED_KEY):
        _assert_open("/ws/robot", "registered", headers={"x-api-key": SPACED_KEY})
        _assert_open("/ws/robot", "registered", headers={"x-api-key": SPACED_KEY.strip()})
        _assert_open(
            "/ws/robot", "registered", headers={"authorization": f"Bearer {SPACED_KEY}"}
        )
        _assert_open("/ws/robot", "registered", params={"api_key": SPACED_KEY})
        # 안쪽 공백은 여전히 값의 일부다.
        _assert_closed("/ws/robot", params={"api_key": "spkey"})


# ── 8. HTTP 창구가 빈 키로 열리지 않는다 ─────────────────────────────────────


def test_http_ingest_rejects_empty_key_deployment():
    """`API_KEY=""`로 뜬 배포에서 빈 X-API-Key 헤더가 통과하면 안 된다.

    수리 전 실 uvicorn 실측 — 헤더가 없으면 401인데 `X-API-Key: `를 빈 값으로 실으면
    인증을 지나 본문 검증(422)까지 들어갔다. WS만 닫고 HTTP를 열어 두면 같은 배포에서
    인입 창구가 그대로 열린 채다.
    """
    with ws_flags(api_key=""):
        with TestClient(app) as client:
            # 비ASCII 키는 bytes로 넣는다 — str로 주면 httpx가 ascii 인코딩에서 먼저 죽는다.
            for headers in (
                {"X-API-Key": ""},
                {},
                {"X-API-Key": WRONG_KEY},
                {"X-API-Key": NON_ASCII_KEY.encode("utf-8")},
            ):
                assert client.post("/api/tagging-events", json={}, headers=headers).status_code == 401


def test_http_ingest_still_accepts_the_real_key():
    """빈 키를 닫으면서 정상 키까지 막으면 안 된다(회귀 방지)."""
    with ws_flags(api_key=API_KEY):
        with TestClient(app) as client:
            # 키는 통과하고 본문 검증에서 걸린다 = 401이 아니다.
            assert client.post(
                "/api/tagging-events", json={}, headers={"X-API-Key": API_KEY}
            ).status_code == 422


# ── 4. 비ASCII 키가 500을 안 낸다 ────────────────────────────────────────────


def test_non_ascii_query_key_is_rejected_not_crashed():
    """한글 키를 실어도 조용한 1008 거절이다. compare_digest에 str을 넣던 시절엔 500이었다."""
    with ws_flags(robot=True):
        _assert_closed("/ws/robot", params={"api_key": NON_ASCII_KEY})


def test_non_ascii_header_key_is_rejected_not_crashed():
    """헤더 쪽 비ASCII도 같다.

    ⚠ 헤더 값은 bytes로 넣어야 이 자리를 실제로 찔러 본다 — httpx가 str 헤더를 ascii로
    인코딩하려다 클라이언트 쪽에서 먼저 죽어, 서버 코드까지 가지도 못한다. 전선을 타고 온
    바이트를 starlette이 latin-1로 디코드하니, 여기 넣는 값도 latin-1 바이트로 만든다.
    """
    latin1_key = "Bearer kéy".encode("latin-1")
    with ws_flags(robot=True):
        _assert_closed("/ws/robot", headers={"authorization": latin1_key})
        _assert_closed("/ws/robot", headers={"x-api-key": "kéy".encode("latin-1")})


def test_non_ascii_api_key_still_matches_itself():
    """기대값 쪽이 비ASCII여도 대조가 산다 — 바이트로 바꿔 비교하기 때문이다."""
    with ws_flags(robot=True, api_key=NON_ASCII_KEY):
        _assert_open("/ws/robot", "registered", params={"api_key": NON_ASCII_KEY})
        _assert_closed("/ws/robot", params={"api_key": "열쇠키한금"})


@pytest.mark.parametrize(
    "supplied, expected, want",
    [
        ("k", "k", True),
        ("k", "K", False),
        ("", "k", False),
        (None, "k", False),
        # 기대값이 비면 아무도 못 들어온다(열어 두는 쪽이 더 나쁘다).
        ("k", "", False),
        (None, "", False),
        # ↓ 이 넷이 예전에 TypeError를 내던 조합이다.
        (NON_ASCII_KEY, "k", False),
        ("k", NON_ASCII_KEY, False),
        (NON_ASCII_KEY, NON_ASCII_KEY, True),
        ("키", "값", False),
    ],
)
def test_keys_match_never_raises(supplied, expected, want):
    assert keys_match(supplied, expected) is want


@pytest.mark.parametrize(
    "raw, want",
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER abc", "abc"),
        ("Bearer  abc ", "abc"),
        ("Bearer", None),
        ("Bearer   ", None),
        ("Basic abc", None),
        ("abc", None),
        ("", None),
        (None, None),
        ("Bearer 키한글", "키한글"),
    ],
)
def test_bearer_token_parsing(raw, want):
    assert bearer_token(raw) == want


def test_unknown_channel_fails_closed():
    """갈래가 늘었는데 여기를 안 고치면, 조용히 열리지 말고 닫히게 한다."""
    with ws_flags():
        assert _ws_auth_required("robot") is False
        assert _ws_auth_required("dashboard") is False
        assert _ws_auth_required("오타친채널") is True
