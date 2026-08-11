"""기상청 날씨 층 — 조회·매핑·기본 목적지 전환·WS 방송 (2026-08-03).

## 이 파일이 지키는 것 다섯

1. **인증키 판 함정** — 인코딩 판(92자)을 `params`로 그대로 넘기면 라이브러리가 `%`를 한 번
   더 인코딩해서 기상청이 거절한다. 모의 서버가 그 이중 인코딩을 실제로 잡아내는지까지 본다.
2. PTY·SKY 조합이 계약 여섯 낱말로 옳게 떨어지는지.
3. 조회가 실패하면 마지막 성공값을 그대로 쓰고, 성공 이력이 없으면 UNKNOWN인지.
4. 비가 오기 시작하면 그날 기본 목적지가 실내로 넘어가는지(2026-08-03 새 규칙).
5. 요원이 손으로 고른 값을 날씨가 못 덮는지.

⚠ 바깥 망을 한 줄도 안 탄다. `conftest.stub_outbound_weather`가 `apis.data.go.kr`를 아예
터뜨리므로, 기상청 갈래를 보는 케이스는 여기 `_kma_client`로 자기 모의를 덮어써야 돈다.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import logging
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio

from app import dispatch as dispatch_mod
from app import weather as weather_mod
from app.config import get_settings
from app.db import get_session
from app.dispatch import (
    apply_weather_default,
    handle_weather_reading,
    insert_auto_default,
    kst_today,
    peek_today_default,
    set_manual_default,
    weather_snapshot,
)
from app.schemas import Destination
from app.weather import (
    KmaError,
    WeatherReading,
    WeatherStatus,
    classify_kma,
    fetch_kma_weather,
    kma_base_time_fcst,
    kma_ncst_slots,
    parse_rn1,
)
from tests.conftest import reset_in_memory_state

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 디코딩 판을 흉내 낸 값. 진짜 키처럼 base64라 `%`가 없고 `==`로 끝난다.
_DECODED_KEY = "Ab3+dE/fGh12jKlMnOpQrStUvWxYz0123456789aBcDeFgHiJkLmNoPqRsTuVwXyZ+/aBcDeFgHiJkLmNo=="
# 포털이 같이 주는 인코딩 판. `+` `/` `=`가 퍼센트 문자열로 바뀌어 있다.
_ENCODED_KEY = (
    "Ab3%2BdE%2FfGh12jKlMnOpQrStUvWxYz0123456789aBcDeFgHiJkLmNoPqRsTuVwXyZ%2B%2F"
    "aBcDeFgHiJkLmNo%3D%3D"
)

# 인증키가 틀렸을 때 기상청이 주는 본문. ⚠ HTTP는 200이고 본문만 XML이다.
_AUTH_ERROR_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><OpenAPI_ServiceResponse><cmmMsgHeader>'
    "<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg>"
    "<returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
)


def _ncst_body(*, pty: str = "0", rn1: str = "0", t1h: str = "29.4") -> dict:
    """초단기실황 본문. SKY가 없는 게 이 오퍼레이션의 특징이다."""
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {
                "dataType": "JSON",
                "items": {
                    "item": [
                        {"category": "PTY", "obsrValue": pty, "nx": 58, "ny": 74},
                        {"category": "RN1", "obsrValue": rn1},
                        {"category": "T1H", "obsrValue": t1h},
                        {"category": "REH", "obsrValue": "55"},
                    ]
                },
                "totalCount": 4,
            },
        }
    }


def _fcst_body(*, sky: str = "1", decoy_sky: str = "4") -> dict:
    """초단기예보 본문. 지금에 가장 가까운 슬롯이 `sky`이고 뒤 슬롯에 미끼를 하나 둔다."""
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {
                "dataType": "JSON",
                "items": {
                    "item": [
                        # 일부러 뒤 슬롯을 먼저 넣는다 — 정렬이 없으면 미끼가 뽑힌다.
                        {"category": "SKY", "fcstDate": "20260803", "fcstTime": "1600",
                         "fcstValue": decoy_sky},
                        {"category": "PTY", "fcstDate": "20260803", "fcstTime": "1500",
                         "fcstValue": "0"},
                        {"category": "SKY", "fcstDate": "20260803", "fcstTime": "1500",
                         "fcstValue": sky},
                    ]
                },
                "totalCount": 3,
            },
        }
    }


def _no_data_body() -> dict:
    """아직 안 만들어진 슬롯을 부르면 오는 본문. ⚠ HTTP는 200이다."""
    return {
        "response": {
            "header": {"resultCode": "03", "resultMsg": "NO_DATA"},
            "body": {},
        }
    }


def _kma_client(
    *,
    ncst: dict | None = None,
    fcst: dict | None = None,
    fail_ncst: bool = False,
    fail_fcst: bool = False,
    ncst_status: int = 200,
    no_data_slots: frozenset[str] = frozenset(),
):
    """기상청 모의 서버. 이중 인코딩된 인증키를 **실제로 거절한다.**

    거절 잣대가 이 파일의 핵심이다 — 나가는 URL을 httpx가 조립한 그대로(`httpx.URL`) 만들어
    보고 `%25`가 보이면 이중 인코딩이다. 디코딩 판을 넘겼을 때는 `%3D%3D`까지만 나가고,
    인코딩 판을 그대로 넘기면 `%253D%253D`가 된다(2026-08-03 실측과 같은 모양).

    `ncst_status`는 4xx·5xx 갈래를 재는 자리다(W1 회귀 못). 응답에 매다는 `request`가 인증키가
    실린 그 URL이라, 예전 코드의 `raise_for_status()`는 여기서 키가 든 문구를 던진다.

    `no_data_slots`는 base_time 문자열 묶음이다. 거기 든 슬롯은 NO_DATA를 준다 — 기상청이
    아직 안 낸 최신 슬롯을 흉내 내는 자리다(W3).
    """
    calls: list[tuple[str, dict]] = []

    class _Client(httpx.AsyncClient):
        async def get(self, url, *args, params=None, **kwargs):
            target = str(url)
            wire = str(httpx.URL(target, params=params or {}))
            request = httpx.Request("GET", wire)
            if "%25" in wire:
                return httpx.Response(200, text=_AUTH_ERROR_XML, request=request)
            calls.append((target, dict(params or {})))
            if weather_mod.KMA_NCST_OP in target:
                if fail_ncst:
                    raise httpx.ConnectError("실황이 죽었다", request=request)
                if ncst_status != 200:
                    return httpx.Response(
                        ncst_status, text="기상청이 아프다", request=request
                    )
                if str((params or {}).get("base_time")) in no_data_slots:
                    return httpx.Response(200, json=_no_data_body(), request=request)
                return httpx.Response(200, json=ncst or _ncst_body(), request=request)
            if weather_mod.KMA_FCST_OP in target:
                if fail_fcst:
                    raise httpx.ConnectError("예보가 죽었다", request=request)
                return httpx.Response(200, json=fcst or _fcst_body(), request=request)
            return await super().get(url, *args, params=params, **kwargs)

    return _Client, calls


@pytest.fixture
def wttr_off(monkeypatch):
    """wttr 폴백을 끈다.

    ⚠ `monkeypatch.setenv`만으로는 안 꺼진다. `weather._setting_*`가 `Settings`에 같은 이름
    필드가 있으면 그 값을 **먼저** 보는데 `get_settings()`가 lru_cache라, 캐시를 안 비우면
    코드 기본값(켜짐)이 그대로 이긴다. 팀장이 `weather_wttr_fallback_enabled`를 config.py에
    배선한 뒤로 이 자리가 조용히 무력해져서 실패 사슬 케이스 둘이 빨개졌다. conftest
    `no_hold`와 같은 수다.
    """
    monkeypatch.setenv("WEATHER_WTTR_FALLBACK_ENABLED", "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _use_kma(monkeypatch, client_cls, *, key: str = _DECODED_KEY) -> None:
    """기상청 경로를 켜고 모의 클라이언트를 물린다."""
    monkeypatch.setenv("KMA_SERVICE_KEY", key)
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", client_cls)
    weather_mod.reset_weather_cache()


def _reading(
    status: WeatherStatus,
    *,
    precip: float | None = 0.0,
    ok: bool = True,
    description: str | None = None,
    temp_c: int | None = 25,
) -> WeatherReading:
    return WeatherReading(
        status=status,
        description=description or status.value,
        precip_mm=precip,
        temp_c=temp_c,
        observed_at=dt.datetime.now(dt.timezone.utc),
        ok=ok,
    )


@pytest_asyncio.fixture
async def today_row(monkeypatch):
    """그날 기본 목적지 행을 미리 만들어 둔다(맑음 → 실외).

    ⭐ F3 회귀 자리. 이걸 안 만들면 `apply_weather_default`가 아침 게이트
    (`WEATHER_MORNING_HOUR`)에 막혀 행을 안 만들고 `payload["default"]`가 None이라,
    **KST 00:00~07:00에 CI를 돌리면** 그 칸을 읽는 케이스가 TypeError로 깨졌다. 방송·스냅샷을
    보는 케이스는 "행이 이미 있는 낮"이 전제라 그 전제를 시각이 아니라 자료로 세운다.

    아침 게이트 자체를 보는 케이스는 `now=`를 직접 넣어 시각을 못 박는다(그 둘은 그대로).

    ⭐ **날짜를 못박는다** (2026-08-04 전체검토 L27). F3 수리는 아침 게이트 의존만 걷었다.
    행을 `kst_today()`로 심은 뒤 본문이 자정을 넘기면 `apply_weather_default`가 **새 날짜**로
    행을 찾아 (None, False)를 돌려주고 `payload["default"]`가 다시 None이 된다. 창이 1초
    안쪽이라 드물지만 8/3~8/4 자정에 이미 비슷하게 밟았다 — 순서대로 돌린 판은 초록이고
    자정 뒤 판만 빨개져서 순서 의존으로 오해하기 쉬운 자리다.

    ⚠ 시계를 통째로 얼리지 않고 **"오늘이 며칠인가"만** 고정한다. `_utc_now`를 얼리면 출동
    대기 창 countdown(`app/dispatch.py`의 `deadline - _utc_now()`)까지 멈춰서, 이 픽스처를
    무는 케이스가 재는 것과 상관없는 자리가 같이 굳는다.

    ⚠ `now=`를 **명시로 넣은 호출은 그대로 둔다.** 아침 게이트를 시각으로 재는 케이스가
    그 길로 가는데, 거기까지 덮으면 그 케이스가 자기가 넣은 시각을 못 쓴다.
    """
    day = kst_today()
    real_kst_today = dispatch_mod.kst_today
    monkeypatch.setattr(
        dispatch_mod,
        "kst_today",
        lambda now=None: real_kst_today(now) if now is not None else day,
    )
    async with get_session() as session:
        await insert_auto_default(session, day, _reading(WeatherStatus.CLEAR))


# ── 1. 인증키 판 함정 ───────────────────────────────────────────────────────

async def test_디코딩판_인증키는_한_번만_인코딩돼_나간다(monkeypatch):
    client_cls, calls = _kma_client()
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather()

    assert reading.ok is True
    assert reading.status is WeatherStatus.CLEAR
    # 두 오퍼레이션을 한 번씩 부른다.
    assert [call[0].rsplit("/", 1)[-1] for call in calls] == [
        weather_mod.KMA_NCST_OP,
        weather_mod.KMA_FCST_OP,
    ]
    # params로 넘긴 값은 디코딩 판 그대로다(손으로 인코딩해서 넘기지 않는다).
    assert calls[0][1]["serviceKey"] == _DECODED_KEY


async def test_인코딩판을_그대로_넘기면_이중_인코딩으로_거절된다(monkeypatch):
    """⭐ 회귀 못. 정규화를 우회하고 인코딩 판을 그대로 `params`에 넘기면 실패해야 한다.

    이게 실패하면 모의 서버가 이중 인코딩을 **안 잡고 있다는** 뜻이라, 위·아래 케이스가
    통째로 뜻을 잃는다. 그래서 우회 경로를 일부러 만들어 그물이 살아 있음을 잰다.
    """
    client_cls, _ = _kma_client()
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", client_cls)

    async with client_cls() as client:
        with pytest.raises(KmaError) as caught:
            await weather_mod._kma_get(
                client,
                f"{weather_mod.KMA_BASE_URL}/{weather_mod.KMA_NCST_OP}",
                {"serviceKey": _ENCODED_KEY, "nx": "58", "ny": "74"},
            )

    # 본문이 JSON이 아니라 인증 오류 XML이다 — 실측에서 본 그 모양.
    assert "SERVICE_KEY_IS_NOT_REGISTERED_ERROR" in str(caught.value)


async def test_인코딩판을_넣어도_정규화가_한_번_풀어_준다(monkeypatch):
    """사람이 어느 판을 붙여 넣을지 못 믿는다. 받아도 디코딩 판으로 되돌려 쓴다."""
    client_cls, calls = _kma_client()
    _use_kma(monkeypatch, client_cls, key=_ENCODED_KEY)

    reading = await fetch_kma_weather()

    assert reading.ok is True
    assert calls[0][1]["serviceKey"] == _DECODED_KEY, "인코딩 판이 안 풀렸다"


async def test_키가_비면_기상청을_아예_안_부른다(monkeypatch):
    client_cls, calls = _kma_client()
    monkeypatch.setenv("KMA_SERVICE_KEY", "")
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", client_cls)

    with pytest.raises(KmaError):
        await fetch_kma_weather()
    assert calls == [], "키가 없는데 바깥을 불렀다"


# ── 2. DB 없이 도는 순수 함수 ──────────────────────────────────────────────

class Test순수함수는_DB_없이_돈다:
    """슬롯 계산·PTY 매핑처럼 DB를 한 줄도 안 쓰는 케이스 묶음 (2026-08-03 교차 검증 W8).

    conftest의 autouse 픽스처 둘이 세션 시작에 스키마를 만들고(`_schema`) 케이스마다
    TRUNCATE를 돌아서(`_clean`), **DB 없는 기기에서는 이 순수 함수들까지 한 케이스도 못
    돌았다.** 클래스 안에서만 그 둘을 no-op으로 덮는다.

    ⚠ conftest는 안 고쳤다 — 거기 손대면 이 파일을 쓰는 다른 시험 전부에 영향이 간다.
    덮기를 모듈이 아니라 클래스에 건 것도 같은 이유다. 이 파일의 나머지(기본 목적지 전환·
    WS 방송)는 여전히 conftest 그대로 DB를 쓴다.

    ⭐ 2026-08-04 전체검토 L24 — 덮개가 **DB 부분만** 걷는다. 예전에는 `_clean`을 통째로
    no-op으로 덮어서 TRUNCATE만이 아니라 출동 대기창 취소·WS 레지스트리·쿨다운·로그인 잠금
    초기화까지 이 묶음의 케이스에서 전부 안 돌았다. 다음 케이스가 시작하며 다시 비워 손해가
    작아 보였지만, **무작위 순서에서는 이 케이스들이 어디든 낀다.**
    """

    @pytest.fixture(scope="class", autouse=True)
    def _schema(self):
        yield

    @pytest_asyncio.fixture(loop_scope="session", autouse=True)
    async def _clean(self):
        """DB를 안 타는 절반만 그대로 돈다(위 docstring L24 절 참고)."""
        await reset_in_memory_state()
        yield

    def test_정규화는_한_번만_푼다(self):
        """되풀이해서 풀면 진짜 `%`가 든 값을 망가뜨린다."""
        assert weather_mod._normalize_service_key(_ENCODED_KEY) == _DECODED_KEY
        assert weather_mod._normalize_service_key(_DECODED_KEY) == _DECODED_KEY
        assert weather_mod._normalize_service_key("  " + _DECODED_KEY + " ") == _DECODED_KEY

    def test_실황은_제공된_최신_정시부터_한_칸씩_물러선다(self):
        """⭐ F5 회귀 못. 기상청은 HH00 실황을 **HH:40부터** 준다.

        그 전에 HH00을 부르면 무조건 NO_DATA라, 최신 정시를 HH:00부터 부르던 판은 매시
        HH:00~HH:40 구간에서 헛 호출을 한 번씩 더 썼다(하루 288 → 480건). 얻은 건 없었다 —
        그 구간에서 결국 쓰는 슬롯이 예전과 똑같은 (HH-1)00이기 때문이다.
        """
        # KST 14:20 — 아직 1400 자료가 없는 시각이라 1300부터 부른다(헛 호출 없음).
        before = dt.datetime(2026, 8, 3, 5, 20, tzinfo=dt.timezone.utc)
        assert kma_ncst_slots(before) == [("20260803", "1300"), ("20260803", "1200")]
        # KST 14:45 — 이제 1400이 나왔다.
        after = dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc)
        assert kma_ncst_slots(after) == [("20260803", "1400"), ("20260803", "1300")]
        # 경계는 정확히 HH:40이다.
        edge = dt.datetime(2026, 8, 3, 5, 40, tzinfo=dt.timezone.utc)  # = KST 14:40
        assert kma_ncst_slots(edge)[0] == ("20260803", "1400")
        just_before = dt.datetime(2026, 8, 3, 5, 39, tzinfo=dt.timezone.utc)
        assert kma_ncst_slots(just_before)[0] == ("20260803", "1300")

    def test_강수_감지_지연은_다섯_분만_줄었다(self):
        """⭐ F5 정직성 못. docstring이 "고쳤다"고 적고 있던 자리다.

        예전 규칙(`지금 - 45분`)과 지금 규칙(`지금 - 40분`)이 **하루 중 거의 모든 시각에 같은
        슬롯**을 고른다. 갈리는 건 매시 HH:40~HH:44 다섯 분뿐이다. 이 못이 빨개지면 누군가
        "지연을 줄였다"를 다시 적을 수 있게 된 것이라, 문서도 같이 봐야 한다.
        """
        base = dt.datetime(2026, 8, 3, 0, 0, tzinfo=dt.timezone.utc)  # = KST 09:00

        def _old_rule(moment):
            kst = moment.astimezone(dt.timezone(dt.timedelta(hours=9)))
            top = (kst - dt.timedelta(minutes=45)).replace(minute=0, second=0, microsecond=0)
            return top.strftime("%Y%m%d"), top.strftime("%H%M")

        differ = [
            minute
            for minute in range(60)
            if kma_ncst_slots(base + dt.timedelta(minutes=minute))[0]
            != _old_rule(base + dt.timedelta(minutes=minute))
        ]
        assert differ == [40, 41, 42, 43, 44], "예전 규칙과 갈리는 구간이 다섯 분이 아니다"

    def test_예보_슬롯은_HH30이고_30분_전이면_한시간_물러선다(self):
        # KST 14:20 → 45분 빼면 13:35(분 ≥ 30) → 1330.
        assert kma_base_time_fcst(
            dt.datetime(2026, 8, 3, 5, 20, tzinfo=dt.timezone.utc)
        ) == ("20260803", "1330")
        # KST 14:00 → 45분 빼면 13:15(분 < 30) → 한 시간 물러서 1230.
        assert kma_base_time_fcst(
            dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.timezone.utc)
        ) == ("20260803", "1230")

    def test_자정_언저리에는_날짜도_같이_물러선다(self):
        now = dt.datetime(2026, 8, 2, 15, 10, tzinfo=dt.timezone.utc)  # = KST 8/3 00:10
        # 00:10에는 0000 실황이 아직 없다(HH:40부터). 어제 2300으로 넘어간다.
        assert kma_ncst_slots(now) == [("20260802", "2300"), ("20260802", "2200")]
        assert kma_base_time_fcst(now) == ("20260802", "2230")

    @pytest.mark.parametrize(
        ("pty", "sky", "rn1", "expected"),
        [
            # PTY가 0이면 SKY가 판정한다.
            (0, 1, 0.0, WeatherStatus.CLEAR),
            (0, 3, 0.0, WeatherStatus.CLOUDY),
            (0, 4, 0.0, WeatherStatus.CLOUDY),
            (0, None, 0.0, WeatherStatus.UNKNOWN),
            (0, 9, 0.0, WeatherStatus.UNKNOWN),
            # ⭐ PTY가 0이 아니면 SKY보다 이긴다 — 하늘이 맑아도 비가 오면 비다.
            (1, 1, 0.5, WeatherStatus.LIGHT_RAIN),
            (1, 1, 3.0, WeatherStatus.HEAVY_RAIN),
            (1, 1, 12.0, WeatherStatus.HEAVY_RAIN),
            (5, 1, 0.0, WeatherStatus.LIGHT_RAIN),
            # ⛔ 4는 **단기예보에만 있는 소나기**다. 초단기 코드표에 없어서 목록에서 빠져
            #    있었고, 실서버 20시 슬롯이 강수 18mm 인데 상태만 UNKNOWN("날씨 정보 없음")
            #    으로 떨어지는 것을 실측했다(2026-08-05). 소나기 오는 시간이 화면에서
            #    "정보 없음"이 되는 자리다.
            (4, 1, 0.0, WeatherStatus.LIGHT_RAIN),
            (4, 1, 18.0, WeatherStatus.HEAVY_RAIN),
            (4, 3, 0.5, WeatherStatus.LIGHT_RAIN),
            (2, 1, 1.0, WeatherStatus.SNOW),
            (3, 3, 0.0, WeatherStatus.SNOW),
            (6, 4, 0.0, WeatherStatus.SNOW),
            (7, 1, 0.0, WeatherStatus.SNOW),
            # 계약 밖 코드는 조용히 맑음으로 읽지 않는다.
            (99, 1, 0.0, WeatherStatus.UNKNOWN),
            (None, 1, 0.0, WeatherStatus.CLEAR),
        ],
    )
    def test_PTY_SKY_조합이_계약_여섯_낱말로_떨어진다(self, pty, sky, rn1, expected):
        assert classify_kma(pty, sky, rn1) is expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("0", 0.0), ("1.5", 1.5), ("강수없음", 0.0), ("", 0.0), (None, None),
            ("1.0mm 미만", 1.0), ("30.0~50.0mm", 30.0),
        ],
    )
    def test_강수량_문구를_수로_읽는다(self, raw, expected):
        assert parse_rn1(raw) == expected


# ── 3. 조회 갈래 (모의 서버) ───────────────────────────────────────────────

async def test_예보의_가장_이른_슬롯을_고른다(monkeypatch):
    """뒤 슬롯이 목록 앞에 와 있어도 지금에 가까운 값이 이겨야 한다."""
    client_cls, _ = _kma_client(fcst=_fcst_body(sky="4", decoy_sky="1"))
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather()

    assert reading.status is WeatherStatus.CLOUDY  # 이른 슬롯 SKY=4(흐림)


async def test_비가_오면_예보가_죽어도_강수로_판정한다(monkeypatch):
    """비 신호는 실내 전환을 부르는 안전 신호라 두 번째 호출에 안 매달린다."""
    client_cls, _ = _kma_client(ncst=_ncst_body(pty="1", rn1="0.4"), fail_fcst=True)
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather()

    assert reading.status is WeatherStatus.LIGHT_RAIN
    assert reading.precipitating is True


async def test_비가_안_오는데_하늘상태를_못_받으면_실패다(monkeypatch):
    """맑음·흐림을 못 가르는 값을 성공으로 내보내면 화면 칩이 거짓말을 한다."""
    client_cls, _ = _kma_client(ncst=_ncst_body(pty="0"), fail_fcst=True)
    _use_kma(monkeypatch, client_cls)

    with pytest.raises(KmaError):
        await fetch_kma_weather()


async def test_resultCode가_정상이_아니면_실패다(monkeypatch):
    """⚠ 한도 초과·NO_DATA도 HTTP 200이라 상태 코드만 보면 빈 목록을 맑음으로 읽는다."""
    limited = {
        "response": {
            "header": {
                "resultCode": "22",
                "resultMsg": "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR",
            },
            "body": {},
        }
    }
    client_cls, _ = _kma_client(ncst=limited)
    _use_kma(monkeypatch, client_cls)

    with pytest.raises(KmaError) as caught:
        await fetch_kma_weather()
    assert "22" in str(caught.value)


async def test_최신_슬롯이_아직_없으면_한_칸_물러선다(monkeypatch):
    """⭐ W3 회귀 못. 기상청이 아직 안 낸 슬롯은 HTTP 200에 NO_DATA로 온다.

    최신부터 부르는 게 핵심이라, 최신이 없을 때 조용히 실패하면 예전보다 나빠진다.
    """
    now = dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc)  # = KST 14:45
    client_cls, calls = _kma_client(no_data_slots=frozenset({"1400"}))
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather(now=now)

    assert reading.ok is True
    ncst_slots = [
        call[1]["base_time"] for call in calls if weather_mod.KMA_NCST_OP in call[0]
    ]
    assert ncst_slots == ["1400", "1300"], "최신부터 부르고 없을 때만 물러서야 한다"


async def test_최신_슬롯이_있으면_한_번만_부른다(monkeypatch):
    """물러서기가 늘 도는 갈래가 되면 오퍼레이션 호출이 두 배가 된다."""
    now = dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc)  # = KST 14:45
    client_cls, calls = _kma_client()
    _use_kma(monkeypatch, client_cls)

    await fetch_kma_weather(now=now)

    ncst_slots = [
        call[1]["base_time"] for call in calls if weather_mod.KMA_NCST_OP in call[0]
    ]
    assert ncst_slots == ["1400"]


async def test_아직_안_나온_정시는_아예_안_부른다(monkeypatch):
    """⭐ F5 회귀 못. HH:40 전에 HH00을 부르면 무조건 NO_DATA라 헛 호출이다.

    예전 판은 매시 HH:00~HH:40 구간에서 실황을 두 번 불렀다(하루 288 → 480건).
    """
    now = dt.datetime(2026, 8, 3, 5, 20, tzinfo=dt.timezone.utc)  # = KST 14:20
    client_cls, calls = _kma_client(no_data_slots=frozenset({"1400"}))
    _use_kma(monkeypatch, client_cls)

    await fetch_kma_weather(now=now)

    ncst_slots = [
        call[1]["base_time"] for call in calls if weather_mod.KMA_NCST_OP in call[0]
    ]
    assert ncst_slots == ["1300"], "아직 안 나온 1400을 부르느라 호출을 한 번 더 썼다"


async def test_슬롯이_전부_없으면_실패다(monkeypatch):
    client_cls, _ = _kma_client(no_data_slots=frozenset({"1400", "1300"}))
    _use_kma(monkeypatch, client_cls)

    with pytest.raises(KmaError):
        await fetch_kma_weather(now=dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc))


# ── 3-1. 인증키가 로그로 새지 않는다 (W1 회귀 못) ──────────────────────────

@pytest.mark.parametrize("status_code", [401, 500])
async def test_HTTP_오류_문구에_인증키가_안_실린다(
    monkeypatch, caplog, wttr_off, status_code
):
    """⭐ W1 회귀 못. `raise_for_status()`가 던지는 `httpx.HTTPStatusError`는 메시지에 요청
    URL을 통째로 담고, 거기 `serviceKey`가 평문이다.

    그 문구가 `reasons`를 타고 `logger.warning` 두 줄로 흘렀다. 5분 폴러라 포털이 죽어 있는
    동안 인증키가 로그에 계속 쌓인다 — 로그를 누가 보든 키를 그대로 가져간다.

    ⚠ 판정은 **퍼센트 인코딩을 푼 뒤**에도 한다. URL에 실릴 때는 `+`·`/`·`=`가 `%2B`처럼
    바뀌어서, 글자 그대로만 찾으면 새고 있어도 초록이 뜬다.
    """
    client_cls, _ = _kma_client(ncst_status=status_code)
    _use_kma(monkeypatch, client_cls)

    with caplog.at_level(logging.INFO):
        reading = await weather_mod.fetch_current_weather()

    assert reading.status is WeatherStatus.UNKNOWN, "조회는 실패로 접혀야 한다"
    text = caplog.text
    assert str(status_code) in text, "상태 코드는 남아야 사람이 원인을 짚는다"
    assert weather_mod.KMA_NCST_OP in text, "어느 오퍼레이션인지도 남아야 한다"
    assert _DECODED_KEY not in text
    assert _DECODED_KEY not in unquote(text), "인코딩된 판으로 새고 있다"


async def test_가리기_헬퍼가_serviceKey를_지운다(monkeypatch):
    """뿌리에서 한 번 가리는 자리다. 새 사유 경로가 붙어도 여기만 지나면 안 샌다."""
    monkeypatch.setenv("KMA_SERVICE_KEY", _DECODED_KEY)
    leaky = (
        "Client error '401 Unauthorized' for url "
        f"'https://apis.data.go.kr/x?serviceKey={_ENCODED_KEY}&nx=58'"
    )

    masked = weather_mod.scrub_secrets(leaky)

    assert _ENCODED_KEY not in masked
    assert _DECODED_KEY not in unquote(masked)
    assert "401" in masked, "가리기가 사유까지 지우면 디버깅이 막힌다"


def test_키가_비면_가리기가_문구를_안_망가뜨린다():
    """빈 문자열로 replace를 돌리면 글자마다 마스크가 끼어든다."""
    assert weather_mod.scrub_secrets("그냥 사유") == "그냥 사유"


# ── 3-2. 남의 로거로도 안 샌다 (F2 회귀 못) ────────────────────────────────
#
# ⚠ 이 묶음은 **진짜 전송 경로**를 타야 뜻이 있다. 위 케이스들이 쓰는 `_kma_client`는
# `AsyncClient.get`을 통째로 갈아서 httpx의 `send()`를 한 번도 안 지나고, 그래서 httpx가
# 자기 로거로 URL을 찍는 자리를 못 봤다 — F2가 시험 그물을 그대로 통과한 이유다.
# `MockTransport`는 바깥으로 안 나가면서 그 안쪽 경로를 전부 태운다.

def _real_async_client() -> type[httpx.AsyncClient]:
    """conftest 가드가 안 씌워진 원본 `AsyncClient`.

    conftest는 `httpx.AsyncClient`(패키지 이름칸)를 갈아 끼우는데, 정의 자리인
    `httpx._client.AsyncClient`는 그대로 남는다. 이 묶음은 httpx 안쪽 전송·로깅 경로를
    타야 해서 원본이 필요하다 — 가드 판은 `apis.data.go.kr`를 보면 `get`에서 바로 터진다.
    """
    from httpx._client import AsyncClient

    return AsyncClient


async def test_httpx_자체_로거에도_인증키가_안_실린다(monkeypatch, caplog):
    """⭐ F2 회귀 못. httpx는 요청 하나마다 URL을 통째로 자기 로거에 찍는다.

    `logger.info('HTTP Request: %s %s "%s %d %s"', ...)`의 두 번째 인자가 요청 URL이고 거기
    `serviceKey`가 평문이다. 우리 `scrub_secrets`가 못 닿는 남의 로거라, 배포에서
    `--log-level debug` 한 번이나 `basicConfig` 한 줄이면 5분마다 평문 키가 쌓인다.
    성공 호출에도 매번 찍힌다 — 실패했을 때만 새는 W1과 다른 자리다.
    """
    monkeypatch.setenv("KMA_SERVICE_KEY", _DECODED_KEY)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ncst_body())

    url = f"{weather_mod.KMA_BASE_URL}/{weather_mod.KMA_NCST_OP}"
    with caplog.at_level(logging.INFO, logger="httpx"):
        async with _real_async_client()(
            transport=httpx.MockTransport(_handler)
        ) as client:
            await weather_mod._kma_get(
                client, url, {"serviceKey": _DECODED_KEY, "nx": "58", "ny": "74"}
            )

    text = caplog.text
    # 그물이 살아 있음부터 잰다 — 로그 한 줄이 아예 안 찍혔으면 이 못은 아무것도 안 본다.
    assert "HTTP Request" in text, "httpx 로그를 안 잡았다 — 못이 헛돈다"
    assert _DECODED_KEY not in text
    assert _DECODED_KEY not in unquote(text), "인코딩된 판으로 새고 있다"
    # 가리기가 관측까지 지우면 바깥이 안 될 때 원인을 못 짚는다.
    assert weather_mod.KMA_NCST_OP in text
    assert "nx=58" in text


async def test_httpx_로그의_상태코드는_그대로_남는다(monkeypatch, caplog):
    """가리개가 레코드를 버리거나 서식을 터뜨리면 안 된다.

    ⚠ 상태 코드는 `%d`로 찍힌다. 인자를 통째로 문자열로 바꾸면 서식이 터져서 로그가
    "--- Logging error ---"로 바뀐다(그러면 사고를 로그로 못 쫓는다).
    """
    monkeypatch.setenv("KMA_SERVICE_KEY", _DECODED_KEY)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="기상청이 아프다")

    with caplog.at_level(logging.INFO, logger="httpx"):
        async with _real_async_client()(
            transport=httpx.MockTransport(_handler)
        ) as client:
            with pytest.raises(KmaError):
                await weather_mod._kma_get(
                    client,
                    f"{weather_mod.KMA_BASE_URL}/{weather_mod.KMA_NCST_OP}",
                    {"serviceKey": _DECODED_KEY},
                )

    assert "503" in caplog.text
    assert "Logging error" not in caplog.text
    assert _DECODED_KEY not in unquote(caplog.text)


def test_가리개는_두_번_안_붙는다():
    """import마다·부를 때마다 붙으면 같은 레코드를 필터가 여러 번 지난다."""
    logger_obj = weather_mod.install_httpx_log_filter()
    weather_mod.install_httpx_log_filter()

    installed = [
        f for f in logger_obj.filters if isinstance(f, weather_mod.HttpxSecretFilter)
    ]
    assert len(installed) == 1


# ── 3-3. observed_at은 관측 시각이다 (F6 회귀 못) ───────────────────────────

async def test_observed_at이_쓴_슬롯의_관측_시각이다(monkeypatch):
    """⭐ F6 회귀 못. 예전에는 조회 시각(`_now()`)을 실어서 최대 1시간 45분 어긋났다."""
    now = dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc)  # = KST 14:45
    client_cls, _ = _kma_client()
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather(now=now)

    # 쓴 슬롯이 KST 14:00이다 = UTC 05:00.
    assert reading.observed_at == dt.datetime(2026, 8, 3, 5, 0, tzinfo=dt.timezone.utc)
    assert reading.observed_at != now, "조회 시각을 관측 시각이라고 말하고 있다"


async def test_물러선_슬롯이면_그_슬롯의_시각이_실린다(monkeypatch):
    """실측으로 잡힌 모습 그대로 — 쓴 값은 한 시간 전 관측인데 지금이라고 말했다."""
    now = dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc)  # = KST 14:45
    client_cls, _ = _kma_client(no_data_slots=frozenset({"1400"}))
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather(now=now)

    # 1400이 없어 1300으로 물러섰으니 관측 시각도 KST 13:00이어야 한다.
    assert reading.observed_at == dt.datetime(2026, 8, 3, 4, 0, tzinfo=dt.timezone.utc)


async def test_봉투의_observed_at이_관측_시각을_그대로_싣는다(monkeypatch, today_row, sent):
    """화면이 "얼마나 낡았나"를 읽는 칸이라 봉투까지 그대로 가야 뜻이 있다."""
    now = dt.datetime(2026, 8, 3, 5, 45, tzinfo=dt.timezone.utc)  # = KST 14:45
    client_cls, _ = _kma_client()
    _use_kma(monkeypatch, client_cls)

    reading = await fetch_kma_weather(now=now)
    await handle_weather_reading(reading)

    assert sent[0]["data"]["observed_at"] == "2026-08-03T05:00:00+00:00"


# ── 4. 실패 사슬 — 마지막 성공값 → UNKNOWN ─────────────────────────────────

async def test_조회가_실패하면_마지막_성공값을_그대로_쓴다(monkeypatch, wttr_off):
    good_cls, _ = _kma_client(ncst=_ncst_body(pty="1", rn1="5.0"))
    _use_kma(monkeypatch, good_cls)

    first = await weather_mod.fetch_current_weather()
    assert (first.status, first.ok, first.stale) == (WeatherStatus.HEAVY_RAIN, True, False)

    # 이제 기상청이 죽는다. 값은 그대로 살아 있고 `stale`만 붙는다.
    dead_cls, _ = _kma_client(fail_ncst=True)
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", dead_cls)

    second = await weather_mod.fetch_current_weather()

    assert second.status is WeatherStatus.HEAVY_RAIN
    assert second.ok is False, "실패했는데 성공으로 보이면 화면이 경고를 못 띄운다"
    assert second.stale is True
    assert second.observed_at == first.observed_at, "관측 시각이 갱신되면 얼마나 낡았는지 못 본다"
    # ⭐ `stale`과 `source`는 다른 축이다(프론트 31차 §4-1). 기상청에서 받아 둔 값을
    # 재사용하는 것이지 대체 서비스를 탄 것이 아니라, 화면이 대체 출처 표식을 달면 안 된다.
    assert second.source == weather_mod.SOURCE_KMA, "캐시를 썼다고 출처가 바뀌면 안 된다"


async def test_기상청에서_받으면_출처가_kma다(monkeypatch, wttr_off):
    good_cls, _ = _kma_client(ncst=_ncst_body(pty="0", rn1="0"))
    _use_kma(monkeypatch, good_cls)

    reading = await weather_mod.fetch_current_weather()

    assert reading.source == weather_mod.SOURCE_KMA


async def test_처음부터_실패면_UNKNOWN이다(monkeypatch, wttr_off):
    dead_cls, _ = _kma_client(fail_ncst=True)
    _use_kma(monkeypatch, dead_cls)  # 성공 이력을 여기서 비운다

    reading = await weather_mod.fetch_current_weather()

    assert reading.status is WeatherStatus.UNKNOWN
    assert reading.ok is False
    assert reading.stale is False
    assert reading.source == weather_mod.SOURCE_UNKNOWN


async def test_폴백을_끄면_wttr을_아예_안_부른다(monkeypatch, wttr_off):
    """⭐ 스위치가 실제로 먹는지를 재는 자리.

    끄기가 안 먹으면 conftest 스텁(맑음)이 늘 받아서, 위 실패 사슬 케이스 둘이 "폴백이
    성공한 갈래"를 재고 있게 된다 — 실제로 그렇게 뒤집혀 있었다.
    """
    dead_cls, calls = _kma_client(fail_ncst=True)
    _use_kma(monkeypatch, dead_cls)

    reading = await weather_mod.fetch_current_weather()

    assert reading.status is WeatherStatus.UNKNOWN
    assert not [call for call in calls if "wttr." in call[0]], "폴백을 껐는데 wttr을 불렀다"


async def test_기상청이_죽으면_wttr_사슬로_물러선다(monkeypatch):
    """폴백을 켜 두면(기본값) 예전 경로가 받는다. conftest 스텁이 맑음을 준다."""
    dead_cls, _ = _kma_client(fail_ncst=True)
    monkeypatch.setenv("KMA_SERVICE_KEY", _DECODED_KEY)
    weather_mod.reset_weather_cache()

    class _Mixed(dead_cls):
        async def get(self, url, *args, params=None, **kwargs):
            if "wttr." in str(url):
                return httpx.Response(
                    200,
                    json={"current_condition": [{
                        "weatherCode": "113", "weatherDesc": [{"value": "Sunny"}],
                        "precipMM": "0.0", "temp_C": "30",
                    }]},
                    request=httpx.Request("GET", str(url)),
                )
            return await super().get(url, *args, params=params, **kwargs)

    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _Mixed)

    reading = await weather_mod.fetch_current_weather()

    assert (reading.status, reading.ok, reading.stale) == (WeatherStatus.CLEAR, True, False)
    # ⭐ 화면이 대체 출처 표식을 다는 유일한 갈래다(프론트 31차 §4-1). 여기가 `kma`로
    # 남으면 요원은 기상청 값을 보고 있다고 믿는다.
    assert reading.source == weather_mod.SOURCE_WTTR


# ── 5. 기본 목적지 전환 ────────────────────────────────────────────────────
#
# ⚠ 아래 케이스들은 **자동 전환이 켜진 전제**다(conftest가 시험에서만 켠다).
#   운영 기본값은 꺼짐이고, 꺼진 갈래는 바로 다음 절 셋이 잰다.


@pytest.fixture
def auto_default_off(monkeypatch):
    """자동 전환을 끈 운영 기본값 상태.

    ⚠ `wttr_off`와 같은 수다 — `get_settings()`가 lru_cache라 캐시를 안 비우면 conftest가
    켜 둔 값이 그대로 이긴다. **끝날 때도 반드시 되돌린다.** 안 되돌리면 이 파일 뒤쪽의
    전환 케이스 일곱 건이 꺼진 설정을 물고 통째로 빨개진다(실측으로 밟았다).
    """
    monkeypatch.setenv("WEATHER_AUTO_DEFAULT_ENABLED", "false")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_스위치가_꺼져_있으면_비가_와도_안_넘어간다(auto_default_off):
    """⛔ 2026-08-03 사용자 확정 — 시연 기간에는 날씨가 목적지를 안 건드린다.

    실내 태깅 도착 태그가 시연 배치에 없어서, 넘기면 화면은 "실내"인데 젯슨은 그 명령을
    외부 경로로 태운다. 여기가 그 안전선이고 운영 기본값이 이쪽이다.
    """
    day = kst_today()
    async with get_session() as session:
        morning, _ = await insert_auto_default(session, day, _reading(WeatherStatus.CLEAR))
        assert morning.destination == Destination.OUTDOOR_TAGGING.value

        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.HEAVY_RAIN, precip=9.0)
        )

    assert changed is False
    assert stored.destination == Destination.OUTDOOR_TAGGING.value
    # ⭐ 목적지는 안 건드려도 **날씨 근거는 따라간다** — 화면 칩이 지금 날씨를 보여야 한다.
    assert stored.weather_status == WeatherStatus.HEAVY_RAIN.value


async def test_스위치가_꺼져_있으면_아침_첫_결정도_실외다(auto_default_off):
    """비 오는 아침에 그날을 처음 정할 때도 실내로 안 간다."""
    day = kst_today()
    async with get_session() as session:
        stored, inserted = await insert_auto_default(
            session, day, _reading(WeatherStatus.LIGHT_RAIN, precip=0.4)
        )

    assert inserted is True
    assert stored.destination == Destination.OUTDOOR_TAGGING.value
    assert stored.weather_status == WeatherStatus.LIGHT_RAIN.value


def test_스위치_기본값은_꺼짐이다(monkeypatch):
    """⚠ 운영 기본값이 꺼짐인지 못박는다. 이 값이 켜진 채로 배포되면 시연이 위험하다.

    conftest가 시험 환경에서만 켜므로, 환경변수를 걷어 낸 맨 설정으로 본다.
    """
    monkeypatch.delenv("WEATHER_AUTO_DEFAULT_ENABLED", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().weather_auto_default_enabled is False
        assert dispatch_mod.weather_auto_default_on() is False
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_비가_오면_기본_목적지가_실내로_넘어간다():
    """⭐ 2026-08-03 새 규칙. 아침에 실외로 정했어도 비가 오기 시작하면 실내다."""
    day = kst_today()
    async with get_session() as session:
        morning, inserted = await insert_auto_default(
            session, day, _reading(WeatherStatus.CLEAR)
        )
        assert inserted is True
        assert morning.destination == Destination.OUTDOOR_TAGGING.value

        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.LIGHT_RAIN, precip=0.4)
        )

    assert changed is True
    assert stored.destination == Destination.INDOOR_TAGGING.value
    assert stored.source == "auto"
    assert stored.weather_status == WeatherStatus.LIGHT_RAIN.value
    assert stored.updated_at is not None


async def test_눈이_와도_실내로_넘어간다():
    day = kst_today()
    async with get_session() as session:
        await insert_auto_default(session, day, _reading(WeatherStatus.CLOUDY))
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.SNOW)
        )

    assert (changed, stored.destination) == (True, Destination.INDOOR_TAGGING.value)


async def test_요원_수동_선택은_날씨가_못_덮는다():
    """⭐ 규칙 3. 손으로 고른 값이 그날 안에는 이긴다."""
    async with get_session() as session:
        picked = await set_manual_default(session, Destination.OUTDOOR_TAGGING)
        assert picked.source == "manual"

        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.HEAVY_RAIN, precip=20.0)
        )

    assert changed is False
    assert stored.destination == Destination.OUTDOOR_TAGGING.value, "날씨가 요원 선택을 덮었다"
    assert stored.source == "manual"


async def test_비가_그쳐도_실외로_안_되돌린다():
    """한 방향 걸쇠다. 되돌리는 규칙은 사용자가 안 정했다."""
    day = kst_today()
    async with get_session() as session:
        await insert_auto_default(session, day, _reading(WeatherStatus.CLEAR))
        await apply_weather_default(session, _reading(WeatherStatus.LIGHT_RAIN, precip=1.0))

        stored, changed = await apply_weather_default(session, _reading(WeatherStatus.CLEAR))

    assert changed is False
    assert stored.destination == Destination.INDOOR_TAGGING.value


async def test_이미_실내면_다시_안_쓴다():
    """같은 값을 5분마다 덮으면 updated_at만 흔들리고 화면이 헛 갱신된다."""
    day = kst_today()
    async with get_session() as session:
        await insert_auto_default(session, day, _reading(WeatherStatus.SNOW))
        stored, changed = await apply_weather_default(session, _reading(WeatherStatus.SNOW))

    assert changed is False
    assert stored.destination == Destination.INDOOR_TAGGING.value


async def test_아침_시각_전에는_행을_안_만든다():
    """새벽 날씨로 하루를 정해 버리지 않는다."""
    before = dt.datetime(2026, 8, 2, 20, 0, tzinfo=dt.timezone.utc)  # = KST 8/3 05:00
    async with get_session() as session:
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.CLEAR), now=before
        )
        assert (stored, changed) == (None, False)
        assert await peek_today_default(session, today=dt.date(2026, 8, 3)) is None


async def test_아침_시각을_지나면_주기_조회가_행을_만든다():
    after = dt.datetime(2026, 8, 2, 23, 0, tzinfo=dt.timezone.utc)  # = KST 8/3 08:00
    async with get_session() as session:
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.HEAVY_RAIN, precip=15.0), now=after
        )

    assert changed is True
    assert stored.service_date == dt.date(2026, 8, 3)
    assert stored.destination == Destination.INDOOR_TAGGING.value
    assert stored.source == "auto"


# ── 5-1. 날씨 근거는 목적지와 무관하게 따라간다 (W5) ────────────────────────

async def test_목적지가_안_바뀌어도_날씨_근거는_갱신된다():
    """⭐ W5 회귀 못. 조기 반환 때문에 DB 날씨 칸이 아침 값에 영영 굳었다.

    그래서 WS 한 장 안에서 `payload.status`(지금)와 `payload.default.weather.status`(DB)가
    서로 다른 값을 말하고, 화면이 어느 쪽을 칩에 쓸지 계약이 없었다.
    """
    day = kst_today()
    async with get_session() as session:
        await insert_auto_default(session, day, _reading(WeatherStatus.CLEAR))
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.CLOUDY)
        )

    assert changed is False, "맑음 → 흐림은 목적지를 안 바꾼다"
    assert stored.destination == Destination.OUTDOOR_TAGGING.value
    assert stored.weather_status == WeatherStatus.CLOUDY.value, "날씨 칸이 아침 값에 굳었다"


async def test_요원이_고른_행도_날씨_근거는_따라간다():
    """목적지는 요원 값이 이기고, "그때 날씨가 뭐였나"는 그대로 사실을 적는다."""
    async with get_session() as session:
        await set_manual_default(session, Destination.OUTDOOR_TAGGING)
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.HEAVY_RAIN, precip=20.0)
        )

    assert changed is False
    assert stored.source == "manual"
    assert stored.destination == Destination.OUTDOOR_TAGGING.value, "날씨가 요원 선택을 덮었다"
    assert stored.weather_status == WeatherStatus.HEAVY_RAIN.value


async def test_이미_실내인_행도_날씨_근거는_따라간다():
    day = kst_today()
    async with get_session() as session:
        await insert_auto_default(
            session, day, _reading(WeatherStatus.LIGHT_RAIN, precip=0.4)
        )
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.HEAVY_RAIN, precip=20.0)
        )

    assert changed is False, "이미 실내면 목적지는 안 다시 쓴다"
    assert stored.weather_status == WeatherStatus.HEAVY_RAIN.value


async def test_같은_날씨를_다시_받으면_행을_안_건드린다():
    """5분마다 같은 값을 쓰면 헛 write가 하루 288번이다."""
    day = kst_today()
    async with get_session() as session:
        await insert_auto_default(session, day, _reading(WeatherStatus.CLEAR))
        before = await peek_today_default(session, today=day)
        assert await dispatch_mod.refresh_weather_columns(
            session, day, _reading(WeatherStatus.CLEAR)
        ) is False
        after = await peek_today_default(session, today=day)

    assert (after.weather_status, after.updated_at) == (
        before.weather_status, before.updated_at,
    )


async def test_경합에서_진_자동_행은_바뀌었다고_안_말한다(monkeypatch):
    """⭐ W4 회귀 못. `on_conflict_do_nothing`이라 진 쪽은 한 행도 안 넣는데, 예전에는
    결과와 무관하게 `changed=True`를 돌려줘서 헛 방송이 한 장 나갔다.

    검사(peek)와 INSERT 사이에 요원 수동 행이 먼저 들어간 순간을 흉내 낸다.
    """
    day = dt.date(2026, 8, 3)
    after = dt.datetime(2026, 8, 2, 23, 0, tzinfo=dt.timezone.utc)  # = KST 8/3 08:00
    async with get_session() as session:
        await set_manual_default(session, Destination.OUTDOOR_TAGGING, today=day)

        real_peek = dispatch_mod.peek_today_default
        first = {"pending": True}

        async def _peek_empty_once(sess, *, today=None):
            if first["pending"]:
                first["pending"] = False
                return None
            return await real_peek(sess, today=today)

        monkeypatch.setattr(dispatch_mod, "peek_today_default", _peek_empty_once)
        stored, changed = await apply_weather_default(
            session, _reading(WeatherStatus.HEAVY_RAIN, precip=20.0), now=after
        )

    assert changed is False, "안 넣었는데 바뀌었다고 방송하면 화면이 헛 갱신된다"
    assert stored.source == "manual", "요원 행이 정본이다"


# ── 6. WS 방송 ─────────────────────────────────────────────────────────────

@pytest.fixture
def sent(monkeypatch):
    """대시보드로 나간 메시지를 모은다."""
    box: list[dict] = []

    async def _capture(message):
        box.append(message)

    monkeypatch.setattr(dispatch_mod.manager, "broadcast", _capture)
    return box


async def test_날씨가_바뀌면_대시보드로_한_장_나간다(today_row, sent):
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))

    assert len(sent) == 1
    envelope = sent[0]
    assert envelope["type"] == "weather_update"
    # 봉투 칸이 다른 대시보드 메시지와 나란해야 프론트가 한 벌로 읽는다.
    assert set(envelope) == {"type", "version", "robot_id", "timestamp", "data"}
    payload = envelope["data"]
    # 화면이 읽는 칸 집합. 늘어나면 여기와 프론트 계약 문서를 같이 고친다.
    assert set(payload) == {
        "status", "description", "temp_c", "precip_mm",
        "ok", "stale", "source", "observed_at", "default",
    }
    assert payload["status"] == "CLEAR"
    assert payload["ok"] is True
    assert payload["stale"] is False
    assert payload["default"]["destination"] == Destination.OUTDOOR_TAGGING.value


async def test_안_바뀌었으면_다시_안_쏜다(sent):
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))

    assert len(sent) == 1, "같은 값을 5분마다 쏘면 화면 로그가 도배된다"


async def test_비로_바뀌면_바뀐_목적지가_같은_장에_실린다(today_row, sent):
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))
    sent.clear()

    await handle_weather_reading(_reading(WeatherStatus.HEAVY_RAIN, precip=20.0))

    assert len(sent) == 1
    payload = sent[0]["data"]
    assert payload["status"] == "HEAVY_RAIN"
    # 칩과 목적지 표시가 어긋나지 않게 한 장에 같이 싣는다.
    assert payload["default"]["destination"] == Destination.INDOOR_TAGGING.value
    assert payload["default"]["source"] == "auto"


async def test_아침_게이트가_어디_있든_방송_케이스가_안_깨진다(monkeypatch, today_row, sent):
    """⭐ F3 회귀 못. KST 00:00~07:00에 CI를 돌리면 케이스 넷이 TypeError로 깨졌다.

    아침 게이트(`WEATHER_MORNING_HOUR`) 때문에 그날 행이 안 생겨 `payload["default"]`가
    None인데, 그 칸을 바로 파고들었다. 전제를 **시각이 아니라 자료로** 세우면(그날 행을
    미리 만드는 `today_row`) 게이트가 어디 있든 안 흔들린다. 게이트를 24시로 밀어 놓고
    돌려서 그 성질을 못 박는다 — 몇 시에 돌려도 "아침 전"이 된다.
    """
    monkeypatch.setattr(dispatch_mod, "WEATHER_MORNING_HOUR", 24)

    await handle_weather_reading(_reading(WeatherStatus.CLEAR))

    assert sent[0]["data"]["default"]["destination"] == (
        Destination.OUTDOOR_TAGGING.value
    )


async def test_문구만_바뀌어도_방송한다(today_row, sent):
    """⭐ F7 회귀 못. 억제 잣대가 `(상태, 조회성공)` 둘뿐이라 문구가 바뀌어도 안 나갔다.

    실측으로 방송은 한 장인데 DB 문구는 '흐림', 열려 있던 화면은 '구름많음'이었다 —
    SKY 3과 4가 둘 다 CLOUDY라 상태 낱말이 안 바뀐 자리다.
    """
    await handle_weather_reading(
        _reading(WeatherStatus.CLOUDY, description="구름많음")
    )
    sent.clear()

    await handle_weather_reading(_reading(WeatherStatus.CLOUDY, description="흐림"))

    assert len(sent) == 1, "낱말이 같다고 삼키면 화면 문구가 굳는다"
    assert sent[0]["data"]["description"] == "흐림"


async def test_기온만_바뀌어도_방송한다(today_row, sent):
    """칩이 그리는 값이라 이게 굳으면 화면이 몇 시간 전 온도를 보여 준다."""
    await handle_weather_reading(_reading(WeatherStatus.CLEAR, temp_c=29))
    sent.clear()

    await handle_weather_reading(_reading(WeatherStatus.CLEAR, temp_c=30))

    assert len(sent) == 1
    assert sent[0]["data"]["temp_c"] == 30


async def test_강수량만_바뀌어도_방송한다(today_row, sent):
    await handle_weather_reading(_reading(WeatherStatus.LIGHT_RAIN, precip=0.4))
    sent.clear()

    await handle_weather_reading(_reading(WeatherStatus.LIGHT_RAIN, precip=2.0))

    assert len(sent) == 1
    assert sent[0]["data"]["precip_mm"] == 2.0


async def test_관측_시각만_흘러도_다시_안_쏜다(today_row, sent):
    """⚠ 억제의 유일한 예외. `observed_at`은 조회마다 반드시 바뀌므로 잣대에 넣으면
    억제가 통째로 무력해져서 5분마다 같은 그림이 나간다."""
    first = _reading(WeatherStatus.CLEAR)
    later = dataclasses.replace(
        first, observed_at=first.observed_at + dt.timedelta(minutes=5)
    )

    await handle_weather_reading(first)
    await handle_weather_reading(later)

    assert len(sent) == 1


async def test_조회_실패도_한_번은_알린다(today_row, sent):
    """`ok=false`가 화면 "날씨를 받지 못했습니다"의 근거다."""
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))
    sent.clear()

    await handle_weather_reading(_reading(WeatherStatus.CLEAR, ok=False))

    assert len(sent) == 1
    assert sent[0]["data"]["ok"] is False


async def test_한_봉투_안에서_날씨_두_자리가_같은_값을_말한다(today_row, sent):
    """⭐ W5 회귀 못 — 화면이 칩에 무엇을 쓸지 계약이 서려면 두 자리가 같아야 한다."""
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))
    sent.clear()

    await handle_weather_reading(_reading(WeatherStatus.CLOUDY))

    payload = sent[0]["data"]
    assert payload["status"] == "CLOUDY"
    assert payload["default"]["weather"]["status"] == "CLOUDY"
    assert payload["default"]["weather"]["description"] == payload["description"]


# ── 6-1. 초기 로드 스냅샷 (W2) ─────────────────────────────────────────────

async def test_새로_연_탭이_스냅샷으로_날씨를_받는다(today_row):
    """⭐ W2 회귀 못. `weather_update`는 값이 바뀔 때만 나가서, 시연 중 탭을 새로 열면
    다음 변화가 올 때까지 상단 기상 칩이 비어 있었다."""
    await handle_weather_reading(_reading(WeatherStatus.HEAVY_RAIN, precip=20.0))

    async with get_session() as session:
        snap = await weather_snapshot(session)

    assert snap is not None
    assert snap["status"] == "HEAVY_RAIN"
    assert snap["default"]["destination"] == Destination.INDOOR_TAGGING.value


async def test_스냅샷과_WS_봉투가_같은_모양이다(sent):
    """모양이 갈리면 화면이 두 벌을 따로 파싱해야 한다 — 만드는 함수를 하나로 묶은 근거다."""
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))

    async with get_session() as session:
        snap = await weather_snapshot(session)

    assert snap == sent[0]["data"]


async def test_날씨를_한_번도_못_받았으면_스냅샷_칸이_비어_있다():
    """null이 "아직 못 받았습니다"의 근거다. 억지로 UNKNOWN을 만들면 화면이 거짓을 그린다."""
    async with get_session() as session:
        assert await weather_snapshot(session) is None


async def test_방송이_억제돼도_스냅샷은_지금_값을_준다(sent):
    """새로 붙는 탭이 봐야 하는 건 마지막 방송이 아니라 서버가 아는 지금 값이다."""
    await handle_weather_reading(_reading(WeatherStatus.CLEAR))
    await handle_weather_reading(_reading(WeatherStatus.CLEAR, precip=0.0))
    assert len(sent) == 1, "같은 값이라 방송은 한 번뿐이다"

    async with get_session() as session:
        snap = await weather_snapshot(session)

    assert snap["status"] == "CLEAR"


# ── 7. 주기 조회 시작·정지 ─────────────────────────────────────────────────

async def test_주기_조회가_돌고_콜백이_값을_받는다(monkeypatch):
    client_cls, _ = _kma_client(ncst=_ncst_body(pty="3"))
    _use_kma(monkeypatch, client_cls)

    got: list[WeatherReading] = []

    async def _on_reading(reading):
        got.append(reading)

    task = weather_mod.start_weather_poller(_on_reading, interval_sec=0.01)
    try:
        assert task is not None
        for _ in range(200):  # 최대 1초까지만 본다
            if len(got) >= 2:
                break
            await asyncio.sleep(0.005)
    finally:
        await weather_mod.stop_weather_poller()

    assert len(got) >= 2, "주기가 한 바퀴로 멈췄다"
    assert got[0].status is WeatherStatus.SNOW
    assert task.done(), "정지가 태스크를 안 접었다"


async def test_정지_뒤에는_한_바퀴도_더_안_돈다(monkeypatch):
    client_cls, calls = _kma_client()
    _use_kma(monkeypatch, client_cls)

    weather_mod.start_weather_poller(interval_sec=0.01)
    for _ in range(100):
        if calls:
            break
        await asyncio.sleep(0.005)
    await weather_mod.stop_weather_poller()

    seen = len(calls)
    await asyncio.sleep(0.05)
    assert len(calls) == seen, "정지 뒤에도 조회가 돌았다"


async def test_주기가_0이하면_안_띄운다(monkeypatch):
    monkeypatch.setenv("KMA_SERVICE_KEY", _DECODED_KEY)
    assert weather_mod.start_weather_poller(interval_sec=0) is None


async def test_한_바퀴가_터져도_루프가_안_죽는다(monkeypatch):
    """콜백이 터지는 건 흔한 갈래다(DB 한 번 못 붙음). 다음 주기에 다시 봐야 한다."""
    client_cls, _ = _kma_client()
    _use_kma(monkeypatch, client_cls)

    hits: list[int] = []

    async def _boom(reading):
        hits.append(1)
        raise RuntimeError("콜백이 터졌다")

    weather_mod.start_weather_poller(_boom, interval_sec=0.01)
    try:
        for _ in range(200):
            if len(hits) >= 3:
                break
            await asyncio.sleep(0.005)
    finally:
        await weather_mod.stop_weather_poller()

    assert len(hits) >= 3, "한 번 터지고 루프가 죽었다"


# ── 8. 시간대별 예보 창구 (2026-08-03 사용자 확정 · 프론트 계약 고정) ────────
#
# 관제 화면의 기상 칩을 누르면 오늘·내일 시간대별 예보가 뜬다. 지금 날씨와 오퍼레이션이
# 다르고(`getVilageFcst`), base_time이 3시간 간격 여덟 칸이라 슬롯 계산도 따로다.
#
# ⭐ 이 묶음이 지키는 핵심은 **판정을 두 벌로 안 적었나**다. 상태 낱말·문구·강수량·실외
# 차단이 지금 날씨와 같은 함수를 지나야 화면이 한 규칙으로 그린다.

def _vilage_item(date: str, clock: str, category: str, value: str) -> dict:
    return {
        "baseDate": "20260803", "baseTime": "2000",
        "category": category, "fcstDate": date, "fcstTime": clock,
        "fcstValue": value, "nx": 58, "ny": 74,
    }


# (fcstDate, fcstTime, {카테고리: 값})
_VILAGE_HOURS = [
    # 오늘 밤 — 맑음. 실외 그대로.
    ("20260803", "2100", {"SKY": "1", "PTY": "0", "TMP": "30", "POP": "0",
                          "PCP": "강수없음"}),
    # 오늘 밤 — 비가 세게 온다(PCP가 문턱 3.0mm 위).
    ("20260803", "2200", {"SKY": "4", "PTY": "1", "TMP": "28", "POP": "80",
                          "PCP": "5.0mm"}),
    # 내일 아침 — 구름많음.
    ("20260804", "0900", {"SKY": "3", "PTY": "0", "TMP": "26", "POP": "20",
                          "PCP": "강수없음"}),
    # 모레 — 화면에 실리면 안 되는 칸(오늘·내일만 준다).
    ("20260805", "0900", {"SKY": "1", "PTY": "0", "TMP": "31", "POP": "0",
                          "PCP": "강수없음"}),
]


def _vilage_body(hours=None) -> dict:
    """단기예보 본문. ⚠ 같은 시각 항목이 **흩어져** 온다(카테고리마다 한 줄)."""
    items = [
        _vilage_item(date, clock, category, value)
        for date, clock, values in (hours if hours is not None else _VILAGE_HOURS)
        for category, value in values.items()
    ]
    return {
        "response": {
            "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
            "body": {"dataType": "JSON", "items": {"item": items},
                     "totalCount": len(items)},
        }
    }


def _vilage_client(*, body: dict | None = None, fail: bool = False,
                   no_data_slots: frozenset[str] = frozenset()):
    """단기예보 모의 서버. 부른 params를 그대로 모은다."""
    calls: list[dict] = []

    class _Client(httpx.AsyncClient):
        async def get(self, url, *args, params=None, **kwargs):
            target = str(url)
            request = httpx.Request("GET", str(httpx.URL(target, params=params or {})))
            if weather_mod.KMA_VILAGE_OP in target:
                calls.append(dict(params or {}))
                if fail:
                    raise httpx.ConnectError("단기예보가 죽었다", request=request)
                if str((params or {}).get("base_time")) in no_data_slots:
                    return httpx.Response(200, json=_no_data_body(), request=request)
                return httpx.Response(200, json=body or _vilage_body(), request=request)
            return await super().get(url, *args, params=params, **kwargs)

    return _Client, calls


# KST 2026-08-03 20:30 = UTC 11:30. 발표 2000이 이미 나온 시각이다.
_FCST_NOW = dt.datetime(2026, 8, 3, 11, 30, tzinfo=dt.timezone.utc)


class Test예보_슬롯은_DB_없이_돈다:
    """3시간 간격 여덟 칸을 고르는 계산. DB를 한 줄도 안 쓴다(위 순수함수 묶음과 같은 수).

    ⭐ 덮개가 **DB 부분만** 걷는다 — 인메모리 위생은 그대로 돈다(전체검토 L24). 근거는
    `Test순수함수는_DB_없이_돈다` docstring에 있다.
    """

    @pytest.fixture(scope="class", autouse=True)
    def _schema(self):
        yield

    @pytest_asyncio.fixture(loop_scope="session", autouse=True)
    async def _clean(self):
        """DB를 안 타는 절반만 그대로 돈다."""
        await reset_in_memory_state()
        yield

    def test_이미_발표된_최신_칸부터_고른다(self):
        # KST 20:30 → 10분 물러서 20:20 → 여덟 칸 중 2000이 최신이다.
        assert weather_mod.kma_vilage_slots(_FCST_NOW) == [
            ("20260803", "2000"), ("20260803", "1700"),
        ]

    def test_발표_직후에는_아직_한_칸_앞을_본다(self):
        # KST 20:05 — 2000이 아직 안 나왔다고 보고 1700부터 부른다.
        just_after = dt.datetime(2026, 8, 3, 11, 5, tzinfo=dt.timezone.utc)
        assert weather_mod.kma_vilage_slots(just_after)[0] == ("20260803", "1700")

    def test_첫_칸_앞_새벽에는_어제_2300으로_넘어간다(self):
        """⚠ 정시 계산으로는 못 잡는 자리다. 00:00~02:10에는 오늘 칸이 하나도 없다."""
        dawn = dt.datetime(2026, 8, 2, 16, 30, tzinfo=dt.timezone.utc)  # = KST 8/3 01:30
        assert weather_mod.kma_vilage_slots(dawn) == [
            ("20260802", "2300"), ("20260802", "2000"),
        ]

    def test_흩어져_온_항목을_시각으로_묶는다(self):
        """본문은 카테고리마다 한 줄이라, 시각으로 안 묶으면 SKY·PTY·TMP가 따로 논다."""
        items = _vilage_body()["response"]["body"]["items"]["item"]

        hours = weather_mod.parse_vilage_items(items)

        assert [h.at.strftime("%m-%d %H") for h in hours] == [
            "08-03 21", "08-03 22", "08-04 09", "08-05 09",
        ], "시각 오름차순으로 묶여야 한다"
        assert hours[0].temp_c == 30
        assert hours[0].pop == 0
        assert hours[1].pop == 80

    def test_습도와_풍속도_같이_읽는다(self):
        """⭐ 2026-08-06 사용자 요청. 단기예보가 이미 주던 값을 안 읽고 있었다.

        ⚠ **풍속은 소수를 지켜야 한다.** `_to_int` 로 읽으면 1.5가 1로 깎여서 산들바람과
        무풍이 같은 값이 된다.
        """
        items = [
            _vilage_item("20260803", "2100", "SKY", "1"),
            _vilage_item("20260803", "2100", "REH", "82"),
            _vilage_item("20260803", "2100", "WSD", "1.5"),
        ]

        hours = weather_mod.parse_vilage_items(items)

        assert hours[0].humidity == 82
        assert hours[0].wind_ms == 1.5, "풍속이 정수로 깎였다"
        # 화면 계약에도 그대로 실린다.
        payload = hours[0].as_payload()
        assert payload["humidity"] == 82 and payload["wind_ms"] == 1.5

    def test_습도와_풍속이_없어도_그_칸을_안_버린다(self):
        """⚠ 기상청이 그 칸을 안 주는 슬롯이 있다. 없다고 예보 한 칸을 통째로 버리면 안 된다."""
        items = [_vilage_item("20260803", "2100", "SKY", "1")]

        hours = weather_mod.parse_vilage_items(items)

        assert len(hours) == 1
        assert hours[0].humidity is None and hours[0].wind_ms is None
        assert hours[0].status is WeatherStatus.CLEAR, "다른 값은 그대로 읽혀야 한다"

    def test_모양이_깨진_칸만_버린다(self):
        """한 칸이 깨졌다고 하루치를 통째로 못 쓰게 만들 이유가 없다."""
        items = [
            _vilage_item("20260803", "2100", "SKY", "1"),
            {"category": "SKY", "fcstDate": "8/3", "fcstTime": "21", "fcstValue": "1"},
            {"category": "", "fcstDate": "20260803", "fcstTime": "2200", "fcstValue": "1"},
        ]

        hours = weather_mod.parse_vilage_items(items)

        assert len(hours) == 1
        assert hours[0].at == dt.datetime(2026, 8, 3, 21, 0, tzinfo=weather_mod.KST)

    def test_예보도_지금_날씨와_같은_판정을_쓴다(self):
        """⭐ 계약의 핵심. 낱말·문구·실외 차단이 갈리면 칩과 예보 칸이 다른 그림을 그린다."""
        hours = weather_mod.parse_vilage_items(
            _vilage_body()["response"]["body"]["items"]["item"]
        )
        by_clock = {h.at.strftime("%m-%d %H"): h for h in hours}

        clear = by_clock["08-03 21"]
        assert clear.status is WeatherStatus.CLEAR
        assert clear.description == "맑음"
        assert clear.precip_mm == 0.0
        assert clear.rain_or_snow is False

        rain = by_clock["08-03 22"]
        # PTY가 0이 아니면 SKY(4=흐림)보다 이긴다 — 지금 날씨와 같은 규칙이다.
        assert rain.status is WeatherStatus.HEAVY_RAIN
        assert rain.description == "비"
        assert rain.precip_mm == 5.0
        assert rain.rain_or_snow is True

        # 낱말 매김이 정말 같은 함수를 지나는지 값으로 대조한다.
        assert rain.status is classify_kma(1, 4, 5.0)
        assert rain.rain_or_snow is weather_mod.blocks_outdoor(rain.status, 5.0)

    def test_오늘_내일만_남긴다(self):
        hours = weather_mod.parse_vilage_items(
            _vilage_body()["response"]["body"]["items"]["item"]
        )

        cut = weather_mod.slice_days(hours, now=_FCST_NOW)

        assert [h.at.strftime("%m-%d %H") for h in cut] == [
            "08-03 21", "08-03 22", "08-04 09",
        ], "모레 칸이 남았다"


async def test_예보를_받아_오늘_내일만_준다(monkeypatch):
    client_cls, calls = _vilage_client()
    _use_kma(monkeypatch, client_cls)

    base_at, hours = await weather_mod.forecast_hours(now=_FCST_NOW)

    assert base_at.isoformat() == "2026-08-03T20:00:00+09:00"
    assert calls[0]["base_date"] == "20260803"
    assert calls[0]["base_time"] == "2000"
    assert [h.as_payload()["at"] for h in hours] == [
        "2026-08-03T21:00:00+09:00",
        "2026-08-03T22:00:00+09:00",
        "2026-08-04T09:00:00+09:00",
    ]


async def test_아직_안_낸_발표면_한_칸_물러선다(monkeypatch):
    """⚠ 단기예보도 NO_DATA가 HTTP 200으로 온다. 물러서기가 없으면 발표 직후가 통째로 빈다."""
    client_cls, calls = _vilage_client(no_data_slots=frozenset({"2000"}))
    _use_kma(monkeypatch, client_cls)

    base_at, hours = await weather_mod.forecast_hours(now=_FCST_NOW)

    assert [call["base_time"] for call in calls] == ["2000", "1700"]
    assert base_at.strftime("%H%M") == "1700"
    assert hours, "물러선 발표로 값을 받았어야 한다"


async def test_예보_캐시가_같은_발표를_다시_안_부른다(monkeypatch):
    """예보는 1시간 단위라 화면이 누를 때마다 바깥을 탈 이유가 없다."""
    client_cls, calls = _vilage_client()
    _use_kma(monkeypatch, client_cls)

    await weather_mod.forecast_hours(now=_FCST_NOW)
    await weather_mod.forecast_hours(now=_FCST_NOW + dt.timedelta(minutes=3))

    assert len(calls) == 1, "캐시가 있는데 바깥을 다시 불렀다"


async def test_새_발표가_나오면_캐시를_버린다(monkeypatch):
    """⭐ 캐시 열쇠에 base_date·base_time을 넣는 이유. 시간이 남아도 새 발표가 이긴다."""
    client_cls, calls = _vilage_client()
    _use_kma(monkeypatch, client_cls)

    await weather_mod.forecast_hours(now=_FCST_NOW)
    # KST 23:30 — 발표 2300이 나온 시각이다(캐시 10분은 안 지났어도 열쇠가 다르다).
    later = _FCST_NOW + dt.timedelta(hours=3)
    await weather_mod.forecast_hours(now=later)

    assert [call["base_time"] for call in calls] == ["2000", "2300"]


async def test_물러선_뒤에도_만료까지는_다시_안_두드린다(monkeypatch):
    """⚠ 캐시 열쇠를 "받아 온 슬롯"으로 잡으면 여기가 무너진다.

    기상청 생성이 늦어 한 칸 물러선 동안, 원하던 슬롯과 열쇠가 영영 안 맞아서 요청마다
    바깥을 두드리게 된다. 화면이 누를 때마다 도는 창구라 그게 그대로 호출 수다.
    """
    client_cls, calls = _vilage_client(no_data_slots=frozenset({"2000"}))
    _use_kma(monkeypatch, client_cls)

    await weather_mod.forecast_hours(now=_FCST_NOW)
    seen = len(calls)
    await weather_mod.forecast_hours(now=_FCST_NOW + dt.timedelta(minutes=2))

    assert len(calls) == seen, "물러선 판을 캐시가 안 잡고 있다"


async def test_조회_실패는_캐시하지_않는다(monkeypatch):
    """실패를 담으면 한 번 죽은 바깥이 캐시 시간만큼 화면을 붙잡는다."""
    dead_cls, _dead_calls = _vilage_client(fail=True)
    _use_kma(monkeypatch, dead_cls)

    with pytest.raises((KmaError, httpx.HTTPError)):
        await weather_mod.forecast_hours(now=_FCST_NOW)

    good_cls, good_calls = _vilage_client()
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", good_cls)
    _base, hours = await weather_mod.forecast_hours(now=_FCST_NOW)

    assert hours, "실패가 캐시로 굳었다"
    assert good_calls, "두 번째 호출이 바깥을 안 탔다"


async def test_캐시가_만료되면_다시_받는다(monkeypatch):
    """만료를 끄고(0초) 재조회가 실제로 도는지 본다.

    ⚠ `setenv`만으로는 못 미덥다 — 팀장이 이 이름을 `Settings`에 배선하는 순간
    `get_settings()`의 lru_cache가 예전 값을 물어온다(`wttr_off`가 같은 수를 쓴다).
    """
    client_cls, calls = _vilage_client()
    _use_kma(monkeypatch, client_cls)
    monkeypatch.setenv("WEATHER_FORECAST_CACHE_SEC", "0")
    get_settings.cache_clear()
    try:
        await weather_mod.forecast_hours(now=_FCST_NOW)
        await weather_mod.forecast_hours(now=_FCST_NOW)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert len(calls) == 2


def _slow_vilage_client(gate: asyncio.Event):
    """바깥 호출이 **정말 매달리는** 모의 서버. 동시 요청 갈래를 재려면 이게 필요하다.

    ⚠ `_vilage_client`는 await를 한 번도 안 타서 코루틴 하나가 통째로 끝난 뒤에야 다음이
    돈다 — 그걸로 "동시에 몰렸다"를 재면 잠금이 없어도 초록이라 아무것도 안 재는 못이 된다.
    """
    calls: list[dict] = []

    class _Client(httpx.AsyncClient):
        async def get(self, url, *args, params=None, **kwargs):
            target = str(url)
            request = httpx.Request("GET", str(httpx.URL(target, params=params or {})))
            if weather_mod.KMA_VILAGE_OP in target:
                calls.append(dict(params or {}))
                await gate.wait()
                return httpx.Response(200, json=_vilage_body(), request=request)
            return await super().get(url, *args, params=params, **kwargs)

    return _Client, calls


def _slow_dead_vilage_client(gate: asyncio.Event):
    """위와 같은데 매달렸다가 **실패**한다(죽은 상류에 몰리는 갈래)."""
    calls: list[dict] = []

    class _Client(httpx.AsyncClient):
        async def get(self, url, *args, params=None, **kwargs):
            target = str(url)
            request = httpx.Request("GET", str(httpx.URL(target, params=params or {})))
            if weather_mod.KMA_VILAGE_OP in target:
                calls.append(dict(params or {}))
                await gate.wait()
                raise httpx.ConnectError("단기예보가 죽었다", request=request)
            return await super().get(url, *args, params=params, **kwargs)

    return _Client, calls


async def test_동시에_몰려도_바깥은_한_번만_탄다(monkeypatch):
    """⭐ 캐시 스탬피드. 만료 순간에 몰린 요청이 각자 기상청을 두드리던 자리다(W1).

    화면 탭이 셋이면 호출도 셋이었고, 타이머로 폴링하면 그 수만큼이다. 오퍼레이션당
    하루 한도가 1만 건이라 조용히 갉히는 쪽이 더 나쁘다.
    """
    gate = asyncio.Event()
    client_cls, calls = _slow_vilage_client(gate)
    _use_kma(monkeypatch, client_cls)

    flights = [
        asyncio.create_task(weather_mod.forecast_hours(now=_FCST_NOW)) for _ in range(5)
    ]
    # 다섯이 전부 잠금 앞에 서거나 바깥에 매달릴 때까지 돌려 준다(고정 sleep은 안 쓴다).
    for _ in range(50):
        await asyncio.sleep(0)
        if calls:
            break
    gate.set()
    results = await asyncio.gather(*flights)

    assert len(calls) == 1, f"바깥을 {len(calls)}번 탔다 — 잠금이 안 먹는다"
    assert all(hours for _base, hours in results), "기다린 요청이 값을 못 받았다"


async def test_몰린_요청은_앞사람_실패를_같이_받는다(monkeypatch):
    """⚠ 죽은 상류에 몰리면 줄을 서서 하나씩 다시 두드리던 자리다.

    실패는 캐시에 안 담는다는 결정은 그대로다(바로 아래 못) — 몰려 있는 **그 한 번**만
    나눠 갖는다.
    """
    gate = asyncio.Event()
    client_cls, calls = _slow_dead_vilage_client(gate)
    _use_kma(monkeypatch, client_cls)

    flights = [
        asyncio.create_task(weather_mod.forecast_hours(now=_FCST_NOW)) for _ in range(3)
    ]
    for _ in range(50):
        await asyncio.sleep(0)
        if calls:
            break
    gate.set()
    results = await asyncio.gather(*flights, return_exceptions=True)

    assert len(calls) == 1, f"죽은 상류를 {len(calls)}번 두드렸다"
    assert all(isinstance(r, (KmaError, httpx.HTTPError)) for r in results), results


async def test_잘린_목록은_경고를_남긴다(monkeypatch, caplog):
    """⚠ 한 쪽에 안 들어가면 기상청은 **에러 없이 짧은 목록**을 준다(W5).

    그러면 뒷날이 통째로 비는데 로그는 조용해서 화면에서만 드러난다.
    """
    body = _vilage_body()
    body["response"]["body"]["totalCount"] = 9999
    client_cls, _calls = _vilage_client(body=body)
    _use_kma(monkeypatch, client_cls)

    with caplog.at_level(logging.WARNING):
        _base, hours = await weather_mod.forecast_hours(now=_FCST_NOW)

    assert hours, "잘렸다고 받은 칸까지 버리면 안 된다"
    assert "잘렸다" in caplog.text and "9999" in caplog.text


async def test_WEATHER_FORECAST_DAYS를_실제로_읽는다(monkeypatch):
    """설정 이름이 있는데 읽는 자리가 없어서 코드 상수 2로 고정돼 있던 자리다(W8)."""
    client_cls, _calls = _vilage_client()
    _use_kma(monkeypatch, client_cls)
    monkeypatch.setenv("WEATHER_FORECAST_DAYS", "1")
    get_settings.cache_clear()
    try:
        _base, hours = await weather_mod.forecast_hours(now=_FCST_NOW)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    assert [h.at.strftime("%m-%d") for h in hours] == ["08-03", "08-03"], "내일이 남았다"


async def test_키가_비면_예보도_바깥을_안_부른다(monkeypatch):
    client_cls, calls = _vilage_client()
    monkeypatch.setenv("KMA_SERVICE_KEY", "")
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", client_cls)

    with pytest.raises(KmaError):
        await weather_mod.forecast_hours(now=_FCST_NOW)
    assert calls == []


# ── 8-1. 창구 (GET /api/weather/forecast) ──────────────────────────────────

@pytest_asyncio.fixture
async def forecast_client():
    """⭐ **운영 앱 그대로**(`app.main.app`)로 예보 창구를 부른다. 응답 모양(계약)을 잰다.

    ⚠ 2026-08-04 전체검토 L17로 뒤집힌 자리다. 예전에는 `FastAPI()`를 새로 세워
    `weather.router` 하나만 붙였는데, 그러면 `app/main.py`의 `include_router(weather.router)`
    한 줄을 **지워도 이 묶음이 통째로 초록이다.** 창구가 아무 데도 안 달린 채 계약만 초록인
    자리라, 2026-08-02에 `GET /api/staff/by-tag`가 늘 404인데 초록이던 것과 같은 계열이다.

    ⚠ 자물쇠를 `dependency_overrides`로 연다. 이 창구는 2026-08-04 전체검토 D10으로
    `require_agent_always`가 됐다 — 플래그와 무관하게 **늘** 401이라, 안 열면 아래 세 케이스가
    모양을 재기도 전에 401에서 끊긴다. 자물쇠 자체(익명 401·요원 200)는 여기서 재지 않는다.
    그 몫은 `tests/test_auth_gates.py`가 진다(플래그 켬·끔 양쪽).

    ⚠ 덮어쓰는 대상이 **운영 앱 한 개**라 반드시 되돌린다. 안 걷으면 이 파일이 끝난 뒤에도
    예보 자물쇠가 열린 채 남아, 딴 파일의 익명 401 케이스가 조용히 200이 된다.
    """
    from app import auth as auth_mod
    from app.main import app as main_app

    # ⚠ 열쇠는 `require_agent_always` **객체 자신**이다. 라우터가
    # `dependencies=[Depends(require_agent_always)]`로 그 객체를 그대로 물고 있어서다.
    # 그래서 이 덮개는 같은 자물쇠를 쓰는 창구(카메라·명부·계정)까지 같이 연다 — 자물쇠를
    # 재는 케이스와 같은 시각에 돌 수 없다는 뜻이라, finally에서 반드시 걷는다.
    gate = auth_mod.require_agent_always
    actor = auth_mod.AuthActor(
        id=1, username="fc-agent", display_name="보는 사람", role=auth_mod.ROLE_AGENT
    )
    main_app.dependency_overrides[gate] = lambda: actor
    transport = httpx.ASGITransport(app=main_app)
    try:
        async with _real_async_client()(transport=transport, base_url="http://test") as c:
            yield c
    finally:
        main_app.dependency_overrides.pop(gate, None)


async def test_창구가_프론트에_알린_모양_그대로_준다(monkeypatch, forecast_client):
    """⭐ 계약 회귀 못. 칸 이름이 하나만 달라져도 화면이 빈 칩을 그린다.

    ⚠ 벽시계를 `_FCST_NOW`(2026-08-03 KST 20:30)로 못박는다. 모의 자료가 08-03·04·05 고정
    날짜라, 시각을 안 고정하면 **자정을 넘기는 순간** 오늘·내일 창이 밀려 08-03 항목이
    통째로 잘리고 `hours[1]`이 딴 시각이 된다. 2026-08-04 00시대에 실제로 밟았다 —
    순서대로 돌린 판은 초록이고 자정 뒤 판만 빨개져서, 순서 의존으로 오해하기 쉬운 자리다.
    """
    monkeypatch.setattr(weather_mod, "_now", lambda: _FCST_NOW)
    client_cls, _ = _vilage_client()
    _use_kma(monkeypatch, client_cls)

    res = await forecast_client.get("/api/weather/forecast")

    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"base_at", "hours"}
    assert set(body["hours"][0]) == {
        "at", "status", "description", "temp_c", "pop", "precip_mm", "rain_or_snow",
        # ⭐ 2026-08-06에 늘었다(사용자 요청). 칸을 늘릴 때 이 집합을 같이 안 고치면
        # 여기가 빨개져서 알려 준다 — 그게 이 못의 일이다.
        "humidity", "wind_ms",
    }
    rain = body["hours"][1]
    assert rain["status"] == "HEAVY_RAIN"
    assert rain["description"] == "비"
    assert rain["pop"] == 80
    assert rain["precip_mm"] == 5.0
    assert rain["rain_or_snow"] is True
    # KST가 붙은 채로 나간다 — 화면이 시간대를 다시 맞출 필요가 없다.
    assert body["base_at"].endswith("+09:00")
    assert body["hours"][0]["at"].endswith("+09:00")


async def test_바깥이_죽으면_창구가_503이다(monkeypatch, forecast_client):
    """낡은 예보를 조용히 보여 주느니 못 받았다고 말한다(지금 날씨와 갈리는 자리)."""
    dead_cls, _ = _vilage_client(fail=True)
    _use_kma(monkeypatch, dead_cls)

    res = await forecast_client.get("/api/weather/forecast")

    assert res.status_code == 503
    assert "예보" in res.json()["detail"]
    # ⚠ 실패를 캐시에 안 담는 대신 화면한테 "이만큼 뒤에 오라"고 말한다. 이게 없으면
    # 곧장 다시 부르는 화면에서 요청 하나가 그대로 기상청 호출 하나가 된다(W2).
    assert res.headers["retry-after"] == "30"


async def test_창구_오류_문구에_인증키가_안_실린다(monkeypatch, forecast_client, caplog):
    """W1과 같은 계열. 실패 사유를 로그에 실을 때 가리기를 지나야 한다."""
    dead_cls, _ = _vilage_client(fail=True)
    _use_kma(monkeypatch, dead_cls)

    with caplog.at_level(logging.INFO):
        res = await forecast_client.get("/api/weather/forecast")

    assert res.status_code == 503
    assert _DECODED_KEY not in res.text
    assert _DECODED_KEY not in unquote(caplog.text)


async def test_총_예산을_넘기면_503으로_끊는다(monkeypatch, forecast_client):
    """⛔ **httpx timeout 4초는 총 시간이 아니다** (2026-08-05 프론트 17차).

    그 값은 읽기 **사이** 간격이라, 기상청이 본문을 조금씩 나눠 보내면 4초에 한 번도 안
    걸린 채 훨씬 오래 끈다. 화면은 그동안 "느리다"만 겪는다. 창구가 총 예산을 따로 걸어
    끊어야 한다.

    ⚠ 예산 상수를 낮춰서 잰다 — 8초를 실제로 기다리면 이 케이스 하나가 시험 전체를 끈다.
    """
    from app.routers import weather as weather_router

    started = asyncio.Event()

    async def _slow():
        started.set()
        await asyncio.sleep(5.0)          # 예산보다 한참 길다
        return dt.datetime.now(dt.timezone.utc), []

    monkeypatch.setattr(weather_router, "forecast_hours", _slow)
    monkeypatch.setattr(weather_router, "FORECAST_TOTAL_BUDGET_SEC", 0.2)

    began = asyncio.get_running_loop().time()
    res = await forecast_client.get("/api/weather/forecast")
    elapsed = asyncio.get_running_loop().time() - began

    assert started.is_set(), "조회를 아예 안 띄웠다"
    assert res.status_code == 503, res.text
    # ⭐ 시간 초과도 같은 503 갈래다 — 화면은 "기상청에서 못 받았다"로 읽으면 된다.
    assert "예보" in res.json()["detail"]
    assert res.headers["retry-after"] == "30"
    # 예산 0.2초인데 5초를 기다렸으면 못이 안 선 것이다. 여유를 넉넉히 준다.
    assert elapsed < 2.0, f"총 예산을 안 걸었다({elapsed:.2f}초 기다렸다)"
