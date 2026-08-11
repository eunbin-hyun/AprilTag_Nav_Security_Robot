"""날씨 조회 한 겹 (셔틀출동 설계 2026-07-31 §5 · 기상청 전환 2026-08-03).

## 정본은 기상청 단기예보다

2026-08-03에 공공데이터포털 **단기예보 조회서비스** 키를 받아 승인·활성까지 확인했다.
그래서 지금 1차 조회는 기상청이고, 오퍼레이션 둘을 한 번씩 불러 값 하나로 접는다.

| 오퍼레이션 | 무엇을 받나 | base_time |
| --- | --- | --- |
| `getUltraSrtNcst`(초단기실황) | PTY(강수형태)·T1H(기온)·REH(습도)·RN1(1시간 강수) | 정시 `HH00` |
| `getUltraSrtFcst`(초단기예보) | **SKY(하늘상태)** — 실황에는 이 값이 없다 | 매시 `HH30` |

두 번 부르는 이유가 SKY 하나다. 실황에 하늘상태가 안 와서 "지금 비는 안 오는데 맑나
흐리나"를 실황만으로는 못 가른다.

⚠ **인증키는 디코딩 판(88자, `==`로 끝남)을 쓴다.** 포털이 주는 인코딩 판(92자,
`%3D%3D`로 끝남)을 httpx·requests의 `params`로 넘기면 라이브러리가 `%`를 한 번 더
인코딩해서 `%253D`가 나가고 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`가 떨어진다(2026-08-03
실측 재현). 사람이 어느 판을 붙여 넣을지 못 믿어서 `_normalize_service_key`가 인코딩
판을 받아도 한 번 풀어 준다 — 기상청 키는 base64라 `%`가 원래 안 들어간다.

## 실패하면 wttr 사슬 → 마지막 성공값 순서로 물러선다

기상청이 실패해도 조회 한 겹이 통째로 죽지 않게, 예전 경로였던 `wttr.in`→`wttr.is`
사슬을 **뒤로 물려서** 남겨 뒀다(`weather_wttr_fallback_enabled`로 끌 수 있다). 마지막
고리는 그대로다.

1. 기상청 두 오퍼레이션 (`KMA_SERVICE_KEY`가 있을 때만 탄다)
2. `wttr.in` → `wttr.is` (같은 j1 본문이라 파서 한 벌)
3. 그래도 실패하면 **마지막으로 성공한 값**을 그대로 쓰고 `stale=True`를 단다 — 화면은
   그 표시를 보고 "날씨를 받지 못했습니다"를 띄운다. 한 번도 성공한 적이 없으면 `UNKNOWN`.

⚠ 마지막 성공값은 인메모리라 서버가 다시 뜨면 사라진다. 그래도 기본 목적지 자체는 DB에
남아서(daily_default_destination) 재기동이 그날의 결정을 되돌리지는 않는다.

## 주기 조회는 여기서 돈다

`start_weather_poller`가 기본 5분마다 한 바퀴를 돈다. 5분이면 오퍼레이션 둘을 합쳐 하루
576건이라 오퍼레이션당 한도(1만 건)의 6%다. 한 바퀴마다 받은 값을 `on_reading` 콜백에
넘기고, 기본 목적지 전환·WS 방송은 그 콜백을 받은 `app/dispatch.py`가 한다 — 이 모듈은
목적지도 소켓도 모른다(의존은 dispatch → weather 한 방향뿐이다).

⚠ **브라우저가 기상청을 직접 부르는 구조는 금지다.** 인증키가 화면으로 내려가고 호출이
화면 수만큼 는다. 서버가 한 번 받아 WS로 뿌린다.

## 상태 낱말은 명령 계약과 같은 여섯이다

`schemas/command.schema.json`의 `weather_status` enum(CLEAR·CLOUDY·LIGHT_RAIN·
HEAVY_RAIN·SNOW·UNKNOWN)을 그대로 쓴다. 은빈 노션 §5도 같은 여섯 값으로 실내·실외를
가른다 — 낱말이 갈라지면 젯슨과 서버가 다른 그림을 본다.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import Awaitable, Callable
from urllib.parse import quote, unquote

import httpx

logger = logging.getLogger("c207.weather")

# 기상청은 KST로 base_date·base_time을 받는다. UTC로 보내면 9시간 전 자료를 달라는 뜻이 된다.
# ⚠ 오프셋을 **더해서** 만든다(`astimezone`). ISO 문자열의 Z를 +09:00으로 바꿔치는 방식은
# 같은 순간을 9시간 다른 시각으로 읽는다(app/dispatch.py `kst_today`와 같은 규칙).
KST = dt.timezone(dt.timedelta(hours=9))


class WeatherStatus(str, Enum):
    """명령 계약(command.schema.json)의 weather_status 여섯 값."""

    CLEAR = "CLEAR"
    CLOUDY = "CLOUDY"
    LIGHT_RAIN = "LIGHT_RAIN"
    HEAVY_RAIN = "HEAVY_RAIN"
    SNOW = "SNOW"
    UNKNOWN = "UNKNOWN"


# wttr.in은 World Weather Online 날씨 코드를 그대로 준다(current_condition[0].weatherCode).
# 문구(weatherDesc)로 가르면 표기가 조금만 바뀌어도 판정이 흔들려서 숫자 코드로 가른다.
_CLEAR_CODES = frozenset({113})
_CLOUDY_CODES = frozenset({116, 119, 122, 143, 248, 260})
_LIGHT_RAIN_CODES = frozenset({176, 263, 266, 281, 284, 293, 296, 311, 353, 386})
_HEAVY_RAIN_CODES = frozenset({200, 299, 302, 305, 308, 314, 356, 359, 389})
# 진눈깨비(sleet)·우박도 여기 넣는다. 젖은 노면이라 실내 판정이 맞고, 계약에 따로 낱말이 없다.
_SNOW_CODES = frozenset(
    {179, 182, 227, 230, 317, 320, 323, 326, 329, 332, 335, 338, 350,
     362, 365, 368, 371, 374, 377, 392, 395}
)

# 이 상태면 "비·눈"이다 → 기본 목적지가 실내가 된다(설계 §2.1-2).
PRECIPITATION_STATUSES = frozenset(
    {WeatherStatus.LIGHT_RAIN, WeatherStatus.HEAVY_RAIN, WeatherStatus.SNOW}
)


def blocks_outdoor(status: WeatherStatus, precip_mm: float | None) -> bool:
    """실외 주행을 막는 날씨인가 — **실내·실외를 가르는 잣대 한 자리**.

    지금 날씨(`WeatherReading.precipitating`)와 시간대별 예보(`HourlyForecast.rain_or_snow`)가
    같은 함수를 지난다. 두 벌로 적으면 화면이 "지금은 실외인데 이따 21시는 실내"를 서로 다른
    규칙으로 그린다.

    코드가 강수 계열이거나 강수량이 0보다 크면 막는다. 코드가 흐림인데 비가 오는 경우
    (소나기 직후)를 강수량이 잡아 준다. UNKNOWN이고 강수량도 없으면 False라 기본 목적지는
    실외가 된다 — 시연 경로가 실외 하나뿐이라 그쪽이 안전하다.
    """
    return status in PRECIPITATION_STATUSES or (precip_mm or 0) > 0


# ── 값의 출처 (2026-08-06 · 프론트 31차 §4-1) ──────────────────────────────
#
# ⚠ **닫힌 집합이다.** 새 조회 경로를 붙이면 여기에 이름을 더하고 프론트에도 알려야 한다.
# 화면은 `wttr`일 때만 표식을 달고 나머지는 조용히 지나간다.
SOURCE_KMA = "kma"          # 기상청 초단기실황. 정상 경로다.
SOURCE_WTTR = "wttr"        # wttr.in·wttr.is 사슬. 기상청이 죽었을 때만 탄다.
SOURCE_UNKNOWN = "unknown"  # 아무 곳에서도 못 받았다.


@dataclass(frozen=True)
class WeatherReading:
    """한 번의 조회 결과. 화면·DB에 그대로 실리는 값이라 원문 문구까지 들고 있는다."""

    status: WeatherStatus
    # 원문 문구("Sunny"). 화면이 사람에게 보여줄 값이고 판정에는 안 쓴다.
    description: str
    precip_mm: float | None
    temp_c: int | None
    # ⭐ **관측 시각**(UTC)이다. 값을 받아온 시각이 아니다(2026-08-03 재검증 F6).
    #
    # 기상청 초단기실황은 정시 슬롯 자료라, 부른 시각과 관측 시각이 최대 1시간 45분 어긋난다.
    # 예전에는 여기에 `_now()`(조회 시각)를 실어서 KST 13:00 관측을 22:07 관측이라고
    # 말했다 — 화면이 "얼마나 낡았나"를 이 값으로 읽으니 그대로 속는다.
    # stale일 때는 마지막 성공값의 관측 시각이 그대로 따라온다.
    observed_at: dt.datetime
    # 이번 호출에서 새로 받았나. False면 조회가 실패했다는 뜻이다.
    ok: bool
    # 마지막 성공값을 대신 쓴 건가. 화면 "날씨를 받지 못했습니다" 표시의 근거다.
    stale: bool = False
    # ⭐ 이 값이 어디서 왔나(2026-08-06 · 프론트 31차 §4-1). `kma`·`wttr`·`unknown` 셋이다.
    #
    # ⚠ **화면은 `wttr`일 때만 표식을 단다.** 평소에도 "기상청"이라 적으면 글자만 늘고 아무
    # 말도 안 하는 칸이 된다(제어 모드 칩을 접은 것과 같은 잣대 — 프론트 31차 §4-1).
    #
    # ⚠ `stale`과 다른 축이다. `stale`은 "이번 조회가 실패해 마지막 성공값을 쓴다"이고
    # 이 칸은 "그 값을 처음 받아온 곳"이다. 물러설 때 `replace`로 `ok`·`stale`만 바꾸므로
    # 캐시가 실려도 원래 출처가 그대로 따라온다 — 그게 맞다. 기상청에서 받아 둔 값을
    # 재사용하는 것과 대체 서비스에서 받은 것은 화면이 다르게 읽어야 한다.
    source: str = SOURCE_UNKNOWN

    @property
    def precipitating(self) -> bool:
        """비·눈이 오는 상태인가. 판정은 `blocks_outdoor` 한 자리에 있다."""
        return blocks_outdoor(self.status, self.precip_mm)


UNKNOWN_DESCRIPTION = "날씨 정보 없음"

# 마지막으로 성공한 조회. 폴백의 정본이다.
_last_success: WeatherReading | None = None


def reset_weather_cache() -> None:
    """이 모듈의 인메모리 캐시를 전부 비운다(시험 위생 · 재기동과 같은 자리).

    마지막 성공값과 시간대별 예보 캐시 둘이다. 새 캐시가 생기면 여기 같이 건다 — conftest
    `_clean`이 부르는 자리가 이 함수 하나뿐이라, 여기 안 걸면 앞 케이스가 심은 값이 다음
    케이스로 샌다.
    """
    global _last_success
    _last_success = None
    reset_forecast_cache()


def last_success() -> WeatherReading | None:
    return _last_success


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


# ── 로그 위생 — 인증키는 한 줄도 안 싣는다 (2026-08-03 교차 검증 W1) ─────────
#
# ⚠ `res.raise_for_status()`가 던지는 `httpx.HTTPStatusError`는 메시지에 요청 URL을 통째로
# 담고, 그 URL에는 `serviceKey`가 평문으로 들어 있다. 그 문구를 그대로 `reasons`에 담아
# `logger.warning` 두 줄로 흘리고 있었다 — 5분 폴러라 포털이 죽어 있는 동안 인증키가 계속
# 쌓인다(실측 재현: `LEAK LINE: ... serviceKey=...`).
#
# 그래서 자리마다 손으로 지우지 않고 **로그·예외 문구로 나가기 전에 여기 한 자리를 지난다.**
_SERVICE_KEY_PARAM_RE = re.compile(r"(?i)(servicekey=)[^&\s'\"]+")
# 매터모스트 들어오는 웹훅 주소의 토큰. `https://<호스트>/hooks/<토큰>` 꼴이고 그 토큰 하나가
# 채널에 글을 쓰는 권한 전부다.
#
# ⚠ 이 자리가 필요한 이유 — 아래 httpx 로그 필터는 **httpx 로거 전체**에 걸린다. 그런데
# httpx를 쓰는 건 날씨만이 아니다(notify.py의 매터모스트 POST·ai_bridge·젯슨 어댑터).
# serviceKey만 가리면 같은 로거가 웹훅 토큰을 평문으로 찍는다 — 터지는 조건이 인증키와
# 똑같다(`--log-level debug` 한 번이나 `basicConfig` 한 줄). 2026-08-03 재검증이 실측 재현했다.
_WEBHOOK_PATH_RE = re.compile(r"(?i)(/hooks/)[A-Za-z0-9_\-]{8,}")
_SECRET_MASK = "***"

# 글자 그대로 지울 값의 최소 길이. `replace`가 문장 아무 데나 걸리는 방식이라, 키를 `1`처럼
# 짧게 둔 배포에서는 가리기가 로그 문장을 통째로 망가뜨린다 — 안 가리는 것보다 나쁘다.
# 진짜 키는 전부 이보다 길고(기상청 인증키·웹훅 주소·기기 키), 짧은 값은 애초에 자물쇠가
# 아니라 `security.secret_is_usable`이 따로 거절한다.
_MIN_LITERAL_SECRET_LEN = 8


def _secret_values() -> tuple[str, ...]:
    """가려야 할 비밀 문자열. 인코딩 판·디코딩 판을 같이 든다.

    설정을 못 읽어도 예외를 안 던진다 — 여기는 이미 실패를 적고 있는 경로라, 로그 위생이
    설정 오류로 같이 죽으면 원래 사유까지 잃는다. 키가 비면 빈 튜플이다(빈 문자열로
    `replace`를 돌리면 문구가 통째로 망가진다).
    """
    values: set[str] = set()
    try:
        key = (_setting_str("kma_service_key", "") or "").strip()
    except Exception:  # noqa: BLE001 - 로그 위생이 설정 오류로 죽으면 안 된다
        key = ""
    if key:
        values.update({key, unquote(key), quote(key, safe="")})
    # 매터모스트 웹훅 주소도 통째로 든다. 같은 httpx 로거를 지나므로 여기 없으면 토큰이
    # 평문으로 남는다(2026-08-03 재검증). 주소 전체를 넣는 이유는 `/hooks/` 정규식이
    # 못 잡는 모양으로 찍히는 자리가 있어도 글자 그대로 지우기 위해서다.
    #
    # 기기 키 셋도 같이 든다(2026-08-04 L4). 이 함수 docstring이 "가려야 할 비밀"이라고
    # 말하는데 실제로는 기상청 키와 웹훅 둘만 들고 있었다. 살아 있는 유출 경로는 WS
    # `?api_key=` 쿼리다 — 아래 httpx 로그 필터가 httpx 로거 전체에 걸리므로, 그 쿼리가
    # 실린 URL이 로거를 지나면 여기 없는 값은 평문으로 남는다.
    for name in (
        "mattermost_webhook_url",
        "mattermost_identify_webhook_url",
        "api_key",
        "dashboard_api_key",
        "gms_api_key",
    ):
        try:
            value = (_setting_str(name, "") or "").strip()
        except Exception:  # noqa: BLE001
            continue
        if len(value) >= _MIN_LITERAL_SECRET_LEN:
            values.add(value)
    return tuple(values)


def scrub_secrets(text: object) -> str:
    """로그·예외 문구로 나갈 문자열에서 인증키를 가린다.

    세 겹이다. `serviceKey=...` 꼴과 매터모스트 웹훅 경로(`/hooks/<토큰>`)를 정규식으로
    지우고(값이 뭐든 잡힌다), 설정에 든 비밀이 딴 모양으로 섞여 있어도 글자 그대로 지운다.

    ⚠ 이름은 날씨에서 왔지만 **이 자리는 날씨 전용이 아니다.** 아래 httpx 로그 필터가
    httpx 로거 전체에 걸리므로, 그 로거를 지나는 다른 비밀(매터모스트 토큰 등)도 여기서
    같이 막는다. 새 비밀이 생기면 `_secret_values`에 이름을 더한다.
    """
    masked = _SERVICE_KEY_PARAM_RE.sub(r"\1" + _SECRET_MASK, str(text))
    masked = _WEBHOOK_PATH_RE.sub(r"\1" + _SECRET_MASK, masked)
    for secret in _secret_values():
        masked = masked.replace(secret, _SECRET_MASK)
    return masked


# ── 남의 로거도 막는다 — httpx가 요청 URL을 통째로 찍는다 (2026-08-03 재검증 F2) ──
#
# 위 `scrub_secrets`는 **우리가 적는 문구**만 지난다. 그런데 httpx는 요청 하나마다 자기
# 로거로 URL을 통째로 찍는다(`httpx/_client.py`의 `logger.info('HTTP Request: %s %s ...')`).
# 거기 `serviceKey`가 평문이다 — 실측 재현:
#
#     INFO: HTTP Request: GET .../getUltraSrtNcst?serviceKey=...%2B%2Faa%3D%3D&... "401"
#
# 지금 배포 설정(root level 30)에서는 안 보이지만 `--log-level debug` 한 번이나
# `logging.basicConfig()` 한 줄이면 5분마다 평문 키가 쌓인다. 성공 호출에도 매번 찍는다.
#
# ⭐ **로거를 내리지 않고 필터를 단다.** 내리면(`setLevel(WARNING)`) 이 한 줄이 통째로
# 사라지는데, 그 줄은 우리만 쓰는 게 아니다 — 매터모스트 발사(`app/notify.py`)·LLM 다리
# (`app/ai_bridge.py`)·젯슨 어댑터가 전부 httpx를 쓰고, 바깥이 안 될 때 "요청이 나갔나
# 상태가 뭐였나"를 그 줄로 짚는다. 필터는 **레코드를 버리지 않고 값만 가려서** 관측은
# 그대로 두고 비밀만 뺀다.
#
# ⚠ 필터를 **로거에 단다**(핸들러가 아니라). 핸들러에 달면 배포마다 핸들러 구성이 달라
# 새는 자리가 생기고, `basicConfig` 한 줄이 새 핸들러를 붙이는 순간 그물 밖으로 나간다.
# 로거에 달면 그 로거를 지나는 레코드는 전부 지나간다(핸들러가 몇 개든, 나중에 붙든).


class HttpxSecretFilter(logging.Filter):
    """httpx 로그 레코드에서 인증키를 가린다. **레코드를 버리지는 않는다.**

    레코드는 아직 서식이 안 입혀진 상태(`msg` + `args`)라 args를 하나씩 본다. `%d`로 찍히는
    상태 코드까지 문자열로 바꾸면 서식이 터지므로, 실제로 가려진 것만 갈아 끼운다.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple):
                record.args = tuple(_scrub_log_arg(arg) for arg in record.args)
            if isinstance(record.msg, str):
                record.msg = scrub_secrets(record.msg)
        except Exception:  # noqa: BLE001 - 로그 위생이 로깅을 죽이면 안 된다
            pass
        return True


def _scrub_log_arg(value: object) -> object:
    """로그 인자 하나. 가릴 게 없으면 **원래 값 그대로** 돌려준다(자료형을 안 바꾼다)."""
    if not isinstance(value, (str, httpx.URL)):
        return value
    text = str(value)
    masked = scrub_secrets(text)
    return masked if masked != text else value


_httpx_secret_filter = HttpxSecretFilter()


def install_httpx_log_filter(logger_name: str = "httpx") -> logging.Logger:
    """httpx 로거에 가리개를 단다. 여러 번 불러도 한 번만 붙는다.

    ⚠ **이 모듈을 들이는 순간 자동으로 걸린다**(아래 한 줄). 배선을 `app/main.py`에 맡기면
    거기를 안 지나는 진입점(tools 스크립트·alembic·시험)이 그물 밖에 남고, 키를 쓰는 코드는
    어차피 이 모듈을 지나기 때문이다. 부작용이 "레코드 값 가리기" 하나뿐이라 import 자리에
    두는 값이 위험보다 크다고 봤다.
    """
    target = logging.getLogger(logger_name)
    if not any(isinstance(f, HttpxSecretFilter) for f in target.filters):
        target.addFilter(_httpx_secret_filter)
    return target


install_httpx_log_filter()


def classify(code: int | None) -> WeatherStatus:
    """날씨 코드를 계약 여섯 값 중 하나로 옮긴다. 모르는 코드는 UNKNOWN이다."""
    if code is None:
        return WeatherStatus.UNKNOWN
    if code in _CLEAR_CODES:
        return WeatherStatus.CLEAR
    if code in _CLOUDY_CODES:
        return WeatherStatus.CLOUDY
    if code in _LIGHT_RAIN_CODES:
        return WeatherStatus.LIGHT_RAIN
    if code in _HEAVY_RAIN_CODES:
        return WeatherStatus.HEAVY_RAIN
    if code in _SNOW_CODES:
        return WeatherStatus.SNOW
    return WeatherStatus.UNKNOWN


def parse_wttr_j1(payload: dict) -> WeatherReading:
    """wttr.in `?format=j1` 본문에서 현재 날씨를 뽑는다.

    본문 모양이 바뀌어도 예외를 밖으로 안 던진다 — 파싱 실패는 조회 실패와 같은 자리라
    부르는 쪽이 폴백으로 처리하게 UNKNOWN을 돌려준다.

    ⚠ `observed_at`에 조회 시각을 싣는다. wttr은 "지금 값"을 주고 관측 시각을 정확한
    자료형으로 안 줘서(`localObsDateTime`이 지역 시각 문자열이다) 기상청 갈래처럼 슬롯
    시각을 실을 수 없다. 여기는 죽은 1차를 대신하는 폴백이라 그 근사를 그대로 둔다 —
    억지로 문자열을 파싱하면 시간대까지 얽혀 더 나쁜 거짓말이 된다.
    """
    try:
        current = (payload.get("current_condition") or [])[0]
    except (IndexError, AttributeError, TypeError):
        return unknown_reading(ok=False)

    desc_list = current.get("weatherDesc") or [{}]
    description = (desc_list[0] or {}).get("value") or UNKNOWN_DESCRIPTION
    return WeatherReading(
        status=classify(_to_int(current.get("weatherCode"))),
        description=str(description).strip() or UNKNOWN_DESCRIPTION,
        precip_mm=_to_float(current.get("precipMM")),
        temp_c=_to_int(current.get("temp_C")),
        observed_at=_now(),
        ok=True,
        source=SOURCE_WTTR,
    )


def unknown_reading(*, ok: bool = False) -> WeatherReading:
    return WeatherReading(
        status=WeatherStatus.UNKNOWN,
        description=UNKNOWN_DESCRIPTION,
        precip_mm=None,
        temp_c=None,
        observed_at=_now(),
        ok=ok,
        stale=False,
    )


def _fallback(reason: str) -> WeatherReading:
    """조회가 실패했을 때 무엇을 돌려줄까 — 마지막 성공값, 없으면 UNKNOWN.

    ⚠ 사유 문구가 로그로 나가는 **마지막 관문**이라 여기서 한 번 더 가린다(W1). 위쪽에서
    이미 가려 왔어도 한 번 더 도는 건 공짜고, 새 사유 경로가 붙을 때 이 자리가 그물이 된다.
    """
    safe = scrub_secrets(reason)
    if _last_success is not None:
        logger.warning(
            "날씨 조회 실패(%s) — 마지막 성공값(%s, %s)을 그대로 쓴다",
            safe, _last_success.status.value, _last_success.observed_at.isoformat(),
        )
        return replace(_last_success, ok=False, stale=True)
    logger.warning("날씨 조회 실패(%s) — 마지막 성공값도 없어 UNKNOWN으로 간다", safe)
    return unknown_reading(ok=False)


# ── 설정값 한 자리 (config.py 배선 전에도 도는 기본값) ──────────────────────
#
# ⚠ 이 사이클에는 여러 조가 `app/config.py`를 같이 만져서 필드를 직접 더하면 텍스트 충돌이
# 난다(`ws.WS_SEND_TIMEOUT_SEC`가 같은 이유로 코드 상수다). 그래서 읽는 순서를 셋으로 뒀다.
#
#   1. `Settings`에 같은 이름 필드가 **있으면** 그 값 — 팀장이 config.py에 넣는 순간 자동으로 이긴다
#   2. 없으면 같은 이름 환경변수(대문자)
#   3. 그것도 없으면 코드 기본값
#
# ⚠ 2번이 `.env` 파일을 **안 읽는다.** pydantic-settings는 `.env`를 자기 안으로만 읽고
# `os.environ`에 안 흘리기 때문이다. 그래서 `.env`에 `KMA_SERVICE_KEY`를 적어도 config.py에
# 필드가 생기기 전까지는 안 먹는다 — 배선 요청 1번이 그 자리다.

def _setting_str(name: str, default: str = "") -> str:
    from app.config import get_settings

    value = getattr(get_settings(), name, None)
    if value in (None, ""):
        value = os.environ.get(name.upper(), "")
    return str(value) if value not in (None, "") else default


# ⚠ 값이 **아예 없는 것**과 **못 읽는 값이 들어온 것**을 가른다. 예전에는 둘 다 경고를 냈다 —
# config.py에 아직 안 배선된 설정은 늘 빈 문자열이라, 부를 때마다 "수로 못 읽는다" 경고가
# 찍혔다(사람이 잘못 적은 값과 구분이 안 돼서 진짜 경고까지 같이 묻힌다).

def _setting_float(name: str, default: float) -> float:
    raw = _setting_str(name, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s 값 %r을 수로 못 읽는다 — 기본값 %s로 간다", name.upper(), raw, default)
        return default


def _setting_int(name: str, default: int) -> int:
    raw = _setting_str(name, "")
    if raw == "":
        return default
    try:
        return int(float(raw))
    except ValueError:
        logger.warning("%s 값 %r을 수로 못 읽는다 — 기본값 %s로 간다", name.upper(), raw, default)
        return default


def _setting_bool(name: str, default: bool) -> bool:
    raw = _setting_str(name, "")
    if raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── 기상청 단기예보 조회서비스 ──────────────────────────────────────────────

KMA_BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
KMA_NCST_OP = "getUltraSrtNcst"
KMA_FCST_OP = "getUltraSrtFcst"

# 5번 게이트(광주 광산구 하남산단6번로 133) 격자. 2026-08-03 실측으로 확인했고, 서울(60·127)과
# 값이 갈리는 것으로 좌표가 실제로 먹는 것까지 봤다 — 잘못 넣어도 200이 떨어져서 값 대조 없이는
# 안 드러난다.
# ⚠ 2026-08-04 정정 — 여기 있던 58·74는 광주 시청(서구 치평동) 칸이다. 격자가 5km라 같은 광주
# 안에서도 실황이 갈린다. 정본은 `app/config.py`의 `kma_nx`·`kma_ny`(57·75)이고, 그 필드가 있는
# 지금은 이 상수가 안 이겨서 거동은 안 바뀐다. 다만 필드가 빠지는 날 조용히 시청 좌표로
# 되돌아가는 자리라 정본에 맞춰 둔다.
KMA_NX_GWANGJU = 57
KMA_NY_GWANGJU = 75

# 초단기**예보**(HH30 슬롯)에만 쓰는 여유. 실측으로는 정시 32분에 그 정시 자료가 이미
# 있었지만, 생성이 늦는 날 `NO_DATA`를 맞지 않게 45분으로 잡았다.
KMA_LOOKBACK_MIN = 45

# 초단기**실황** HH00 슬롯이 실제로 나오는 시각(정시 + 이 분). 기상청 제공 시각이 HH:40이다
# (2026-08-03 재검증 F5 실측). 이 값보다 일찍 부르면 무조건 `NO_DATA`다.
KMA_NCST_AVAILABLE_MIN = 40

# 실황 슬롯을 최대 몇 칸까지 물러서 볼까. 1이면 `[제공된 최신 정시, 그 한 시간 전]`이다.
# 물러서기는 **생성이 늦는 날**을 위한 여유지, 평소에 늘 도는 갈래가 아니다.
KMA_NCST_SLOT_FALLBACKS = 1

# 한 슬롯에 오는 항목이 실황 8개·예보는 여섯 항목 × 여섯 슬롯이라 넉넉히 잡는다.
KMA_NUM_OF_ROWS = 100

# 이 이상이면 HEAVY_RAIN이다(mm/h). 기상청 강수 강도 구분에서 '보통'이 시작되는 값이 3mm/h라
# 거기를 문턱으로 뒀다(약함 1~3 / 보통 3~15 / 강함 15~30). 실내·실외 판정에는 영향이 없다 —
# 둘 다 강수라 어느 쪽이든 실내다. 화면 칩 문구만 갈라진다.
KMA_HEAVY_RAIN_MM = 3.0

# PTY(강수형태) → 계약 낱말. 0은 "없음"이라 여기 없고 SKY가 대신 판정한다.
# 진눈깨비·눈날림 계열(2·6·7)을 SNOW로 접는 건 예전 wttr 매핑과 같은 결이다 — 계약에 낱말이
# 여섯뿐이라 젖은 노면을 실내로 보내는 쪽이 안전하다.
#
# ⛔ **코드표가 오퍼레이션마다 다르다**(2026-08-05 실서버 실측으로 잡았다).
#   - 초단기(`getUltraSrtNcst`·`getUltraSrtFcst`) — 0 없음 / 1 비 / 2 비·눈 / 3 눈 /
#     5 빗방울 / 6 빗방울·눈날림 / 7 눈날림
#   - 단기예보(`getVilageFcst`) — 0 없음 / 1 비 / 2 비·눈 / 3 눈 / **4 소나기**
#
#   ⚠ **4는 단기예보에만 있다.** 예보 슬롯을 붙이기 전에는 실황만 봐서 안 만났고, 그래서
#   목록에 빠진 채로 남았다. 실서버에서 20시 슬롯이 강수 18mm·`rain_or_snow=True`인데
#   상태만 `UNKNOWN`("날씨 정보 없음")으로 떨어지는 것을 실측했다 — **소나기가 오는 시간이
#   화면에 "정보 없음"으로 뜬다.** 판정이 강수량 칸에만 기대고 낱말은 비는 자리다.
_PTY_RAIN = frozenset({1, 4, 5})        # 1 비 / 4 소나기(단기예보) / 5 빗방울
_PTY_SNOW = frozenset({2, 3, 6, 7})     # 2 비·눈 / 3 눈 / 6 빗방울·눈날림 / 7 눈날림
_PTY_NAMES = {
    0: "강수 없음", 1: "비", 2: "비·눈", 3: "눈", 4: "소나기",
    5: "빗방울", 6: "빗방울·눈날림", 7: "눈날림",
}
# SKY(하늘상태) → 계약 낱말. 계약에 '구름많음'과 '흐림'을 가르는 낱말이 없어서 둘 다 CLOUDY다.
_SKY_STATUS = {1: WeatherStatus.CLEAR, 3: WeatherStatus.CLOUDY, 4: WeatherStatus.CLOUDY}
_SKY_NAMES = {1: "맑음", 3: "구름많음", 4: "흐림"}

# 실황 RN1이 "강수없음"처럼 낱말로 오는 판이 있다. 예보 쪽은 "1.0mm 미만"·"1.0~29.9mm"다.
_RN1_NONE_WORDS = frozenset({"강수없음", "적설없음", "-", "null", "none"})
_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


class KmaError(RuntimeError):
    """기상청 조회가 실패했다. 부르는 쪽이 폴백 사슬로 넘긴다."""


class KmaNoData(KmaError):
    """그 슬롯 자료가 아직 없다(resultCode 03·빈 목록).

    다른 실패와 갈라 두는 이유가 하나다 — **한 칸 뒤 슬롯으로 물러서도 되는 실패**가
    이것뿐이다. 접속 실패·한도 초과·인증 오류로 물러서면 죽은 바깥을 슬롯 수만큼 더
    두드리게 된다.
    """


def kma_service_key() -> str:
    """쓸 인증키. 비어 있으면 기상청 경로를 아예 안 탄다."""
    return _normalize_service_key(_setting_str("kma_service_key", ""))


def _normalize_service_key(raw: str) -> str:
    """인코딩 판(92자)을 받아도 디코딩 판(88자)으로 되돌린다.

    ⚠ 이게 이 모듈에서 제일 잘 밟는 함정이다. 포털은 같은 키를 두 판으로 주는데,
    인코딩 판(`...%3D%3D`)을 `params`로 넘기면 httpx가 `%`를 다시 인코딩해서
    `...%253D%253D`가 나가고 기상청이 `SERVICE_KEY_IS_NOT_REGISTERED_ERROR`를 준다.
    HTTP는 200이라 로그만 보면 "키가 안 됐다"로 보인다(2026-08-03 실측 재현).

    기상청 키는 base64(A–Z a–z 0–9 `+` `/` `=`)라 `%`가 원래 없다. 그래서 `%`가 보이면
    사람이 인코딩 판을 붙여 넣었다는 뜻이고, 한 번 풀어 주는 게 안전하다. 한 번만 푼다 —
    되풀이하면 진짜 `%`가 든 값(다른 서비스 키)을 망가뜨린다.
    """
    key = (raw or "").strip()
    if "%" not in key:
        return key
    decoded = unquote(key)
    logger.warning(
        "KMA_SERVICE_KEY가 인코딩 판(%d자)이다 — 디코딩 판(%d자)으로 풀어 쓴다. "
        "params로 그대로 넘기면 %%가 이중 인코딩돼 SERVICE_KEY_IS_NOT_REGISTERED_ERROR가 난다",
        len(key), len(decoded),
    )
    return decoded


def _kst_now(now: dt.datetime | None = None) -> dt.datetime:
    moment = now or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(KST)


def kma_ncst_slots(
    now: dt.datetime | None = None, *, back: int = KMA_NCST_SLOT_FALLBACKS
) -> list[tuple[str, str]]:
    """부를 초단기실황 슬롯을 **최신 순**으로 — `[제공된 최신 정시, 그 한 시간 전, ...]`.

    ## ⚠ 강수 감지를 못 줄인다. 문서를 실물에 맞춰 적는다 (2026-08-03 재검증 F5)

    한때 이 자리에 "예전에는 최대 1시간 44분이 걸렸고 지금은 고쳤다"고 적혀 있었다.
    **사실이 아니다.** 기상청이 HH00 실황을 HH:40부터 주므로, 예전(`지금 - 45분`)이 보던
    슬롯과 지금 보는 슬롯이 하루 중 거의 모든 시각에 **같다.** 줄어든 건 HH:40~HH:44
    다섯 분뿐이다(최대 1시간 44분 → 1시간 39분).

    | 지금 시각 | 예전(-45분) | 지금(-40분) |
    | --- | --- | --- |
    | HH:20 | (HH-1)00 | (HH-1)00 — 같다 |
    | HH:42 | (HH-1)00 | HH00 — 여기 다섯 분이 전부다 |

    지연의 뿌리는 우리 계산이 아니라 **기상청 제공 시각**이라 이 층에서는 못 줄인다.
    실황보다 빠른 신호를 원하면 초단기예보의 PTY를 같이 보는 길이 있는데, 그건 관측이
    아니라 예보라 틀릴 수 있고 지금 규칙이 한 방향 걸쇠(비가 그쳐도 안 되돌림)라 잘못된
    예보 하나가 그날을 통째로 실내에 가둔다. **사용자·팀 결정 자리로 올렸다.**

    ## 그래서 헛 호출만 되돌린다

    최신 정시(HH00)를 HH:00부터 부르던 판은 HH:00~HH:40 구간에서 **반드시 NO_DATA를 맞고**
    한 칸 물러섰다 — 실황 호출이 하루 288건에서 480건으로 늘기만 하고 얻은 게 없었다.
    지금은 첫 슬롯부터 "이미 제공된" 정시라 평소 한 번이면 끝나고, 물러서기는 생성이 늦는
    날에만 돈다.
    """
    top = (
        _kst_now(now) - dt.timedelta(minutes=KMA_NCST_AVAILABLE_MIN)
    ).replace(minute=0, second=0, microsecond=0)
    return [
        (moment.strftime("%Y%m%d"), moment.strftime("%H%M"))
        for moment in (top - dt.timedelta(hours=step) for step in range(back + 1))
    ]


def kma_slot_kst(base_date: str, base_time: str) -> dt.datetime:
    """`("20260803", "1300")` 슬롯을 KST 시각으로. 기상청 슬롯은 전부 KST다."""
    return dt.datetime.strptime(f"{base_date}{base_time}", "%Y%m%d%H%M").replace(
        tzinfo=KST
    )


def kma_base_time_fcst(now: dt.datetime | None = None) -> tuple[str, str]:
    """초단기예보 슬롯(매시 `HH30`). 45분 뺀 시각이 30분 전이면 한 시간을 더 물러선다.

    ⚠ 물러서지 않으면 아직 생성 안 된 슬롯(예: 14:20에 14:30)을 달라고 해서 `NO_DATA`다.
    자정 언저리에서는 날짜도 같이 하루 물러선다 — `timedelta` 연산이라 저절로 맞는다.
    """
    moment = _kst_now(now) - dt.timedelta(minutes=KMA_LOOKBACK_MIN)
    if moment.minute < 30:
        moment -= dt.timedelta(hours=1)
    moment = moment.replace(minute=30, second=0, microsecond=0)
    return moment.strftime("%Y%m%d"), moment.strftime("%H%M")


def kma_items(payload: dict) -> list[dict]:
    """본문에서 항목 목록을 꺼낸다. 헤더가 정상이 아니면 KmaError다.

    ⚠ `resultCode`를 반드시 본다. 기상청은 NO_DATA(03)·한도 초과(22)도 **HTTP 200**으로
    주기 때문에, 상태 코드만 보면 빈 목록을 "맑음"으로 잘못 읽는다.
    """
    try:
        response = payload["response"]
        header = response["header"]
    except (KeyError, TypeError) as exc:
        raise KmaError(f"본문에 response.header가 없다 — {payload!r:.200}") from exc

    code = str(header.get("resultCode", "")).strip()
    # 03(NO_DATA)만 "슬롯이 아직 없다"로 갈라 둔다 — 이것만 한 칸 물러서서 다시 물어도 된다.
    if code == "03":
        raise KmaNoData(f"기상청 NO_DATA — {header.get('resultMsg')!r}")
    if code not in ("00", "0"):
        raise KmaError(f"기상청 오류 응답 — resultCode={code!r} {header.get('resultMsg')!r}")

    body = response.get("body") or {}
    items = ((body.get("items") or {}).get("item")) or []
    if not isinstance(items, list):
        raise KmaError(f"items.item이 목록이 아니다 — {type(items).__name__}")
    if not items:
        raise KmaNoData("항목이 하나도 없다(슬롯이 아직 안 만들어졌다)")
    # ⚠ 한 쪽(`numOfRows`)에 안 들어가면 기상청은 **에러 없이 짧은 목록**을 준다. 그러면
    # 단기예보에서 내일이 통째로 비는데 화면에서만 드러나고 로그는 조용하다. 잘렸다는
    # 사실만 남긴다 — 거절하면 반쪽이라도 그릴 수 있는 판을 통째로 버린다(2026-08-04 W5).
    total = body.get("totalCount")
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = None
    if total is not None and total > len(items):
        logger.warning(
            "기상청 항목이 잘렸다 — totalCount %d인데 %d개만 받았다(numOfRows를 올리거나"
            " 페이지를 더 불러야 한다). 뒷날 예보가 빈다.",
            total, len(items),
        )
    return items


def ncst_values(items: list[dict]) -> dict[str, str]:
    """실황 항목을 `{카테고리: 값}`으로 접는다."""
    return {
        str(item.get("category")): str(item.get("obsrValue"))
        for item in items
        if item.get("category")
    }


def fcst_first(items: list[dict], category: str) -> str | None:
    """예보 항목 중 그 카테고리의 **가장 이른** 슬롯 값. 지금에 제일 가까운 예보다."""
    picked = sorted(
        (item for item in items if item.get("category") == category),
        key=lambda item: (str(item.get("fcstDate", "")), str(item.get("fcstTime", ""))),
    )
    return str(picked[0].get("fcstValue")) if picked else None


def parse_rn1(raw) -> float | None:
    """1시간 강수량(mm). "강수없음"은 0으로, 수가 섞인 문구는 그 수로 읽는다."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.lower() in _RN1_NONE_WORDS:
        return 0.0
    found = _NUMBER_RE.search(text)
    return float(found.group()) if found else None


def classify_kma(
    pty: int | None, sky: int | None, precip_mm: float | None = None
) -> WeatherStatus:
    """PTY·SKY를 계약 여섯 값 중 하나로 옮긴다.

    ⭐ **PTY가 0이 아니면 SKY보다 이긴다**(설계 확정). 비가 오는데 하늘상태가 '구름많음'인
    슬롯이 실제로 있어서, SKY를 먼저 보면 비 오는 날을 실외로 보낸다.
    """
    if pty is not None and pty in _PTY_SNOW:
        return WeatherStatus.SNOW
    if pty is not None and pty in _PTY_RAIN:
        threshold = _setting_float("kma_heavy_rain_mm", KMA_HEAVY_RAIN_MM)
        if (precip_mm or 0.0) >= threshold:
            return WeatherStatus.HEAVY_RAIN
        return WeatherStatus.LIGHT_RAIN
    if pty in (0, None):
        return _SKY_STATUS.get(sky, WeatherStatus.UNKNOWN)
    # 계약 밖 PTY. 새 코드가 생겼거나 본문이 이상하다 — 조용히 맑음으로 읽지 않는다.
    logger.warning("모르는 PTY 값 %r — UNKNOWN으로 둔다", pty)
    return WeatherStatus.UNKNOWN


def describe_kma(pty: int | None, sky: int | None) -> str:
    """화면에 그대로 나가는 사람용 문구. 판정에는 안 쓴다."""
    if pty not in (0, None):
        return _PTY_NAMES.get(pty, UNKNOWN_DESCRIPTION)
    return _SKY_NAMES.get(sky, UNKNOWN_DESCRIPTION)


def reading_from_kma(
    ncst: dict[str, str],
    sky_raw: str | None,
    observed_at: dt.datetime | None = None,
) -> WeatherReading:
    """실황 값 묶음과 SKY 하나를 `WeatherReading` 한 덩어리로 접는다.

    ⚠ `observed_at`은 **쓴 슬롯의 관측 시각**이어야 한다(2026-08-03 재검증 F6). 예전에는
    여기서 `_now()`를 박아서, KST 13:00 관측을 22:07 관측이라고 내보냈다 — 한 칸 물러선
    갈래만이 아니라 정상 갈래도 최대 1시간 45분 어긋난다. 화면이 "얼마나 낡았나"를 이
    값으로 읽으니 그대로 속는다. 안 주면 조회 시각으로 물러서지만, 기상청 갈래는 늘 준다.
    """
    pty = _to_int(ncst.get("PTY"))
    sky = _to_int(sky_raw)
    precip = parse_rn1(ncst.get("RN1"))
    return WeatherReading(
        status=classify_kma(pty, sky, precip),
        description=describe_kma(pty, sky)[:64],
        precip_mm=precip,
        temp_c=_to_int(ncst.get("T1H")),
        observed_at=observed_at or _now(),
        ok=True,
        source=SOURCE_KMA,
    )


async def _kma_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    """한 오퍼레이션을 부르고 JSON 본문을 돌려준다.

    ⚠ 인증키 오류는 **JSON이 아니라 XML**로 온다(`<OpenAPI_ServiceResponse>`). `res.json()`이
    거기서 터지므로 본문 앞머리를 로그에 실어 준다 — 안 그러면 "JSON 파싱 실패"만 남아서
    키 문제인 걸 못 알아본다. 그 본문도 `scrub_secrets`를 지난다.

    ⚠ 4xx·5xx에 `raise_for_status()`를 **안 쓴다.** 그게 던지는 `httpx.HTTPStatusError`는
    메시지에 요청 URL을 통째로 담고 거기 `serviceKey`가 평문이다(W1). 상태 코드와
    오퍼레이션 이름만 남기고, 원인 예외를 매달지도 않는다(`from None`) — 매달면
    `logger.exception`이 체인을 따라가며 그 URL을 그대로 찍는다.
    """
    op = url.rstrip("/").rsplit("/", 1)[-1]
    res = await client.get(url, params=params)
    if res.is_error:
        raise KmaError(f"기상청 {op} 응답이 HTTP {res.status_code}다") from None
    try:
        return res.json()
    except Exception as exc:  # noqa: BLE001 - 본문 모양이 뭐든 여기서 접는다
        raise KmaError(
            f"기상청 {op} 본문이 JSON이 아니다(인증키 오류는 XML로 온다) — "
            f"{scrub_secrets(res.text[:200])!r}"
        ) from exc


async def _fetch_ncst(
    client: httpx.AsyncClient,
    base: str,
    common: dict,
    now: dt.datetime | None,
) -> tuple[dict[str, str], dt.datetime]:
    """실황을 **최신 슬롯부터** 부른다. 아직 안 만들어진 슬롯이면 한 칸 물러선다.

    물러서는 건 `KmaNoData`일 때뿐이다(그 갈래 설명은 `KmaNoData` docstring).

    ⚠ 값과 함께 **그 값의 관측 시각(UTC)**을 돌려준다. 어느 슬롯을 실제로 썼는지는 여기서만
    알 수 있어서, 안 돌려주면 부르는 쪽이 조회 시각으로 때울 수밖에 없다(F6).
    """
    last: KmaNoData | None = None
    for order, (base_date, base_time) in enumerate(kma_ncst_slots(now)):
        try:
            items = kma_items(
                await _kma_get(
                    client,
                    f"{base}/{KMA_NCST_OP}",
                    {**common, "base_date": base_date, "base_time": base_time},
                )
            )
        except KmaNoData as exc:
            last = exc
            logger.info(
                "실황 슬롯 %s %s가 아직 없다 — 한 칸 물러선다", base_date, base_time
            )
            continue
        if order:
            logger.info(
                "실황 최신 슬롯이 아직 없어 %s %s 슬롯을 썼다", base_date, base_time
            )
        observed_at = kma_slot_kst(base_date, base_time).astimezone(dt.timezone.utc)
        return ncst_values(items), observed_at
    raise last if last is not None else KmaError("부를 실황 슬롯이 하나도 없다")


@contextlib.asynccontextmanager
async def _http(shared: httpx.AsyncClient | None, timeout: float):
    """빌린 클라이언트가 있으면 그대로 쓰고, 없으면 이 한 번을 위해 하나 판다.

    호출마다 새로 파면 커넥션 풀이 매번 버려져서 TCP·TLS 악수를 다시 한다. 5분 폴러는
    한 바퀴에 서너 번 나가니 전면 장애 때 하루 1,100번쯤이다(2026-08-04 L38). 그래서
    주기 조회는 클라이언트 하나를 들고 바퀴마다 빌려준다.

    ⚠ **빌린 건 안 닫는다.** 닫는 자리는 판 자리 하나다 — 빌린 쪽이 닫으면 다음 바퀴가
    닫힌 클라이언트를 든다. 모듈 전역으로 안 둔 이유도 같다. `httpx.AsyncClient`는 자기를
    만든 이벤트 루프의 소켓을 들고 있어서, 수명을 쥔 주인이 없으면 루프가 바뀌는 판에서
    조용히 어긋난다(같은 이유로 `_forecast_flight_lock`도 루프에 매여 있다).
    """
    if shared is not None:
        yield shared
        return
    async with httpx.AsyncClient(timeout=timeout) as owned:
        yield owned


async def fetch_kma_weather(
    *,
    service_key: str | None = None,
    base_url: str | None = None,
    nx: int | None = None,
    ny: int | None = None,
    timeout: float | None = None,
    now: dt.datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> WeatherReading:
    """기상청 오퍼레이션 둘을 불러 지금 날씨 하나로 접는다. 실패하면 `KmaError`다.

    ⚠ `serviceKey`를 `params`로 넘긴다(URL에 손으로 이어 붙이지 않는다). 디코딩 판을
    넘겨야 httpx가 정확히 한 번 인코딩해서 나간다 — `_normalize_service_key` 설명 참고.

    실황이 먼저다. 예보(SKY)가 실패해도 **PTY가 강수를 가리키면 그대로 돌려준다** —
    비가 온다는 신호는 실내 전환을 부르는 안전 신호라 두 번째 호출에 매달면 안 된다.
    비가 안 올 때는 SKY가 있어야 맑음·흐림을 가르므로 그때만 실패로 접는다.
    """
    key = _normalize_service_key(service_key if service_key is not None else kma_service_key())
    if not key:
        raise KmaError("KMA_SERVICE_KEY가 비어 있다")

    base = (base_url or _setting_str("kma_base_url", KMA_BASE_URL)).rstrip("/")
    grid_x = nx if nx is not None else _setting_int("kma_nx", KMA_NX_GWANGJU)
    grid_y = ny if ny is not None else _setting_int("kma_ny", KMA_NY_GWANGJU)
    wait = timeout if timeout is not None else _setting_float("weather_timeout_sec", 4.0)

    common = {
        "serviceKey": key,
        "dataType": "JSON",
        "numOfRows": str(KMA_NUM_OF_ROWS),
        "pageNo": "1",
        "nx": str(grid_x),
        "ny": str(grid_y),
    }
    fcst_date, fcst_time = kma_base_time_fcst(now)

    async with _http(client, wait) as client:
        ncst, observed_at = await _fetch_ncst(client, base, common, now)
        sky_raw: str | None = None
        try:
            sky_raw = fcst_first(
                kma_items(
                    await _kma_get(
                        client,
                        f"{base}/{KMA_FCST_OP}",
                        {**common, "base_date": fcst_date, "base_time": fcst_time},
                    )
                ),
                "SKY",
            )
        except (KmaError, httpx.HTTPError) as exc:
            # ⚠ 예외 문구를 그대로 실으면 인증키가 샌다(W1). 가린 뒤에만 싣는다.
            why = scrub_secrets(exc)
            pty = _to_int(ncst.get("PTY"))
            if pty in (0, None):
                raise KmaError(f"하늘상태를 못 받아 맑음·흐림을 못 가른다 — {why}") from None
            logger.warning("초단기예보(SKY)가 실패했지만 실황 PTY=%s라 강수로 판정한다 — %s", pty, why)

    return reading_from_kma(ncst, sky_raw, observed_at)


# ── 시간대별 예보 (단기예보 getVilageFcst · 2026-08-03 사용자 확정) ──────────
#
# 관제 화면의 기상 칩을 누르면 오늘·내일 시간대별 예보가 뜬다. 지금 날씨(초단기실황)와는
# 오퍼레이션도 주기도 다르다.
#
# | | 지금 날씨 | 시간대별 예보 |
# | --- | --- | --- |
# | 오퍼레이션 | `getUltraSrtNcst` + `getUltraSrtFcst` | `getVilageFcst` 하나 |
# | base_time | 매시 | 3시간마다 여덟 칸 |
# | 언제 부르나 | 5분 주기(서버가 먼저) | 화면이 누를 때(짧은 캐시) |
#
# ⭐ **판정 함수를 새로 안 짠다.** 상태 낱말은 `classify_kma`, 문구는 `describe_kma`,
# 강수량 문구는 `parse_rn1`, 실내·실외 잣대는 `blocks_outdoor`를 그대로 지난다. 같은 판정을
# 두 벌로 적으면 화면이 "지금은 실외인데 21시는 실내"를 서로 다른 규칙으로 그린다.

KMA_VILAGE_OP = "getVilageFcst"

# 단기예보 발표 시각 여덟 칸(KST). 이 밖의 base_time을 부르면 NO_DATA다.
KMA_VILAGE_BASE_TIMES = (
    "0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300",
)

# 발표 시각에서 자료가 실제로 나올 때까지의 여유(분). 아직 안 낸 슬롯을 부르면 NO_DATA라
# 이 여유만큼 물러선 뒤 최신 칸을 고른다. 그래도 늦는 날은 `KmaNoData`를 받고 한 칸 더 간다.
KMA_VILAGE_AVAILABLE_MIN = 10

# 몇 칸까지 물러서 볼까. 1이면 `[제공된 최신 발표, 그 3시간 전]`이다.
KMA_VILAGE_SLOT_FALLBACKS = 1

# 한 발표에 사흘치 × 시간마다 × 항목 열두 개가 온다. 오늘·내일을 덮으려면 넉넉해야 한다 —
# 모자라면 목록이 잘려서 내일이 통째로 빈다(페이지를 더 부르면 호출이 배로 는다).
#
# ⛔ **1000은 실제로 모자랐다**(2026-08-05 실서버 실측). 17시 발표에서 `totalCount`가
#    **1052**로 와서 아래 `_items` 가 "항목이 잘렸다"를 찍었다. 기상청은 잘린 것을 에러로
#    안 알리고 **짧은 목록을 200으로** 주기 때문에, 화면에서 뒷시간이 비는 것으로만 드러난다.
#    ⚠ 잘리는 쪽이 **뒤**라서 내일 늦은 시간부터 사라진다 — 우리가 쓰는 범위(오늘·내일)의
#    끝자락이 바로 그 자리다.
#    여유를 두고 1500으로 올렸다. 응답만 커지고 호출 수는 그대로다. 그래도 모자라면 위
#    경고 로그가 다시 알려 준다.
KMA_VILAGE_NUM_OF_ROWS = 1500

# 예보는 1시간 단위라 5분마다 부를 이유가 없다. 창구 안에서만 들고 있는다.
# ⭐ 600 → 3600. 까닭은 `config.weather_forecast_cache_sec` 주석에 있다 — 발표가 3시간마다고
#   캐시 열쇠에 발표 슬롯이 들어 있어서 새 발표는 이 값과 무관하게 저절로 새로 받는다.
WEATHER_FORECAST_CACHE_SEC = 3600.0

# 화면에 실어 줄 날 수. 1이면 오늘만, 2면 오늘·내일이다.
FORECAST_DAYS = 2


@dataclass(frozen=True)
class HourlyForecast:
    """한 시각의 예보 한 칸. 화면 계약(`GET /api/weather/forecast`)의 `hours[]` 원소다."""

    # KST 정시. 화면이 그대로 쓰라고 `+09:00`이 붙은 채로 나간다.
    at: dt.datetime
    status: WeatherStatus
    description: str
    temp_c: int | None
    # 강수 확률(%). 단기예보에만 있는 값이라 지금 날씨 봉투에는 없다.
    pop: int | None
    # 그 1시간 강수량(mm).
    precip_mm: float | None
    # 그 시각에 비·눈이 오나. 지금 날씨와 **같은 잣대**를 지난다(`blocks_outdoor`).
    #
    # ⚠ 예전 이름은 `blocks_outdoor`였다. 2026-08-03에 사용자가 "날씨로 실외를 잠그는
    # 방향은 안 간다"로 정하면서 뜻이 "막는다"에서 "온다"로 바뀌어 이름도 같이 갈았다.
    # 화면은 이 칸으로 그 시간대를 눈에 띄게 표시만 하고 버튼을 잠그지 않는다.
    rain_or_snow: bool
    # ⭐ 습도(%)·풍속(m/s). 단기예보가 이미 주는데 안 읽고 있던 값이다(2026-08-06 사용자 요청).
    #
    # ⭐ **습도가 하늘상태를 보완한다.** SKY 는 구름량이라 안개를 못 말하는데, 새벽 복사안개는
    # 오히려 맑아야 낀다 — 화면에 "맑음"만 뜨면 밖이 뿌연 날 요원이 값을 의심한다. 습도가
    # 같이 보이면 그 갈래가 눈에 보인다.
    #
    # ⚠ 둘 다 **옵션이다.** 기상청이 그 칸을 안 주는 슬롯이 있고, 없다고 예보 한 칸을 통째로
    # 버릴 이유가 없다. 화면은 `None` 을 "—"로 그리면 된다.
    humidity: int | None = None
    wind_ms: float | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "status": self.status.value,
            "description": self.description,
            "temp_c": self.temp_c,
            "pop": self.pop,
            "precip_mm": self.precip_mm,
            "rain_or_snow": self.rain_or_snow,
            "humidity": self.humidity,
            "wind_ms": self.wind_ms,
        }


def kma_vilage_slots(
    now: dt.datetime | None = None, *, back: int = KMA_VILAGE_SLOT_FALLBACKS
) -> list[tuple[str, str]]:
    """부를 단기예보 발표 슬롯을 **최신 순**으로.

    발표가 3시간 간격이라 정시 계산으로는 못 구한다 — 여덟 칸 중에서 고른다. 첫 칸 앞
    (자정~02:10)에서는 **어제 2300**으로 넘어간다. `timedelta` 연산이라 날짜가 저절로 맞는다.
    """
    moment = _kst_now(now) - dt.timedelta(minutes=KMA_VILAGE_AVAILABLE_MIN)
    slots: list[tuple[str, str]] = []
    for _ in range(back + 1):
        slot = _vilage_slot_at(moment)
        slots.append(slot)
        # 다음 바퀴는 이 슬롯 **직전**부터 다시 고른다 — 같은 칸을 두 번 담지 않는다.
        moment = kma_slot_kst(*slot) - dt.timedelta(minutes=1)
    return slots


def _vilage_slot_at(moment: dt.datetime) -> tuple[str, str]:
    """그 KST 시각 기준으로 이미 발표된 가장 최신 칸."""
    hhmm = moment.strftime("%H%M")
    passed = [t for t in KMA_VILAGE_BASE_TIMES if t <= hhmm]
    if passed:
        return moment.strftime("%Y%m%d"), passed[-1]
    yesterday = moment - dt.timedelta(days=1)
    return yesterday.strftime("%Y%m%d"), KMA_VILAGE_BASE_TIMES[-1]


def forecast_hour(at: dt.datetime, values: dict[str, str]) -> HourlyForecast:
    """한 시각의 항목 묶음(`{카테고리: 값}`)을 예보 한 칸으로 접는다."""
    pty = _to_int(values.get("PTY"))
    sky = _to_int(values.get("SKY"))
    precip = parse_rn1(values.get("PCP"))
    status = classify_kma(pty, sky, precip)
    return HourlyForecast(
        at=at,
        status=status,
        description=describe_kma(pty, sky)[:64],
        temp_c=_to_int(values.get("TMP")),
        pop=_to_int(values.get("POP")),
        precip_mm=precip,
        rain_or_snow=blocks_outdoor(status, precip),
        # ⚠ 풍속은 소수로 온다("1.5"). `_to_int` 는 `int(float(...))` 라 터지진 않지만 **1로
        # 깎는다** — 산들바람과 무풍이 같은 값이 된다. 습도는 정수라 `_to_int` 가 맞다.
        humidity=_to_int(values.get("REH")),
        wind_ms=_to_float(values.get("WSD")),
    )


def parse_vilage_items(items: list[dict]) -> list[HourlyForecast]:
    """단기예보 항목을 시각별로 묶어 예보 목록으로. **시각 오름차순**이다.

    본문은 `{category, fcstDate, fcstTime, fcstValue}`가 항목 하나라, 같은 시각 항목 열둘이
    흩어져 온다. 시각으로 묶어야 SKY·PTY·TMP가 한 칸에 모인다.

    모양이 이상한 항목은 조용히 버린다 — 한 칸이 깨졌다고 하루치를 통째로 못 쓰게 만들 이유가
    없다. 반대로 시각을 못 읽은 항목을 억지로 지금으로 채우면 화면이 거짓 시각을 그린다.
    """
    buckets: dict[tuple[str, str], dict[str, str]] = {}
    for item in items:
        category = str(item.get("category") or "")
        date = str(item.get("fcstDate") or "")
        clock = str(item.get("fcstTime") or "")
        if not category or len(date) != 8 or len(clock) != 4:
            continue
        buckets.setdefault((date, clock), {})[category] = str(item.get("fcstValue"))

    hours: list[HourlyForecast] = []
    for (date, clock), values in sorted(buckets.items()):
        try:
            at = kma_slot_kst(date, clock)
        except ValueError:
            logger.warning("예보 시각 %r %r을 못 읽는다 — 그 칸을 버린다", date, clock)
            continue
        hours.append(forecast_hour(at, values))
    return hours


def slice_days(
    hours: list[HourlyForecast], *, days: int = FORECAST_DAYS, now: dt.datetime | None = None
) -> list[HourlyForecast]:
    """오늘부터 `days`일치만 남긴다(KST 날짜 기준).

    ⚠ 이미 지난 시각을 걷어내지 않는다. 기상청이 주는 첫 칸이 발표 시각 다음 정시라, 늦은
    발표(2000)를 받으면 오늘이 21·22·23시 세 칸뿐이고 이른 발표(0200)를 받으면 새벽부터
    다 온다. 화면이 하루 띠를 그리는 자리라 받은 대로 싣고, 지금이 어디인지는 화면이 자기
    시계로 표시한다.
    """
    today = _kst_now(now).date()
    last = today + dt.timedelta(days=days - 1)
    return [hour for hour in hours if today <= hour.at.date() <= last]


async def fetch_kma_forecast(
    *,
    service_key: str | None = None,
    base_url: str | None = None,
    nx: int | None = None,
    ny: int | None = None,
    timeout: float | None = None,
    now: dt.datetime | None = None,
) -> tuple[dt.datetime, list[HourlyForecast]]:
    """단기예보를 한 번 부른다. `(발표 시각 KST, 예보 전부)`이고 실패하면 `KmaError`다.

    자른 목록이 아니라 **받은 전부**를 돌려준다 — 오늘·내일로 자르는 건 부르는 쪽이 자기
    시계로 한다. 캐시에 담긴 뒤 자정을 넘겨도 그 자름이 늘 지금 날짜를 따르게 하려는 거다.
    """
    key = _normalize_service_key(service_key if service_key is not None else kma_service_key())
    if not key:
        raise KmaError("KMA_SERVICE_KEY가 비어 있다")

    base = (base_url or _setting_str("kma_base_url", KMA_BASE_URL)).rstrip("/")
    grid_x = nx if nx is not None else _setting_int("kma_nx", KMA_NX_GWANGJU)
    grid_y = ny if ny is not None else _setting_int("kma_ny", KMA_NY_GWANGJU)
    wait = timeout if timeout is not None else _setting_float("weather_timeout_sec", 4.0)

    common = {
        "serviceKey": key,
        "dataType": "JSON",
        "numOfRows": str(KMA_VILAGE_NUM_OF_ROWS),
        "pageNo": "1",
        "nx": str(grid_x),
        "ny": str(grid_y),
    }

    last: KmaNoData | None = None
    async with httpx.AsyncClient(timeout=wait) as client:
        for order, (base_date, base_time) in enumerate(kma_vilage_slots(now)):
            try:
                items = kma_items(
                    await _kma_get(
                        client,
                        f"{base}/{KMA_VILAGE_OP}",
                        {**common, "base_date": base_date, "base_time": base_time},
                    )
                )
            except KmaNoData as exc:
                last = exc
                logger.info(
                    "단기예보 발표 %s %s가 아직 없다 — 한 칸 물러선다", base_date, base_time
                )
                continue
            if order:
                logger.info(
                    "단기예보 최신 발표가 아직 없어 %s %s를 썼다", base_date, base_time
                )
            return kma_slot_kst(base_date, base_time), parse_vilage_items(items)
    raise last if last is not None else KmaError("부를 단기예보 발표 슬롯이 하나도 없다")


# 마지막으로 받아 둔 예보. `(발표 슬롯 열쇠, 발표 시각, 예보 전부, 만료 눈금)`이다.
#
# ⚠ **조회 실패는 안 담는다.** 담으면 한 번 죽은 바깥이 캐시 시간만큼 화면을 붙잡는다.
# ⚠ 만료는 단조 시계(`time.monotonic`)로 잰다 — 벽시계는 NTP 보정에 뒤로 뛴다.
_forecast_cache: tuple[tuple[str, str, int, int], dt.datetime, list[HourlyForecast], float] | None = None


# ── 한 번에 하나만 바깥을 탄다 (2026-08-04 수리 W1) ─────────────────────────
#
# 캐시 검사와 대입 사이에 `await`가 있어서, **만료 순간에 동시에 들어온 요청 N개가 전부
# 캐시 미스로 떨어져 각자 기상청을 두드렸다.** 탭 셋이면 호출 셋이고, 화면이 타이머로
# 폴링하면 그 수만큼이다(오퍼레이션당 하루 한도가 1만 건이다).
#
# 잠금을 기다린 뒤에는 **캐시를 다시 본다** — 앞사람이 채웠으면 바깥을 안 탄다.
# 앞사람이 **실패**했으면 그 실패를 그대로 받는다. 그렇게 안 하면 몰려온 요청이 줄을 서서
# 하나씩 바깥을 두드려, 죽은 상류에 대고 요청 수만큼 재시도하는 모양이 된다.
#
# ⚠ 그 실패 나눠 갖기는 **몰려 있는 동안만**이다. 뒤에 새로 오는 요청은 그냥 바깥을 탄다 —
# "조회 실패는 캐시에 안 담는다"는 결정(위 `_forecast_cache` 주석)을 안 뒤집는다.
_forecast_lock: asyncio.Lock | None = None
_forecast_lock_loop: asyncio.AbstractEventLoop | None = None
# 이번 비행에서 난 실패와, 비행이 끝날 때마다 오르는 눈금.
_forecast_flight_error: BaseException | None = None
_forecast_flight_seq = 0


def _forecast_flight_lock() -> asyncio.Lock:
    """지금 도는 루프에 매인 잠금 하나.

    ⚠ 모듈 자리에서 `asyncio.Lock()`을 한 번 만들어 두면 **루프가 바뀌는 순간 터진다** —
    `asyncio.Lock`은 처음 경합할 때 그 루프에 자기를 매어 두고, 다른 루프에서 경합하면
    `RuntimeError: is bound to a different event loop`다. 시험은 케이스·파일마다 루프가
    갈릴 수 있고, 경합이 없는 판에서는 조용히 지나가서 **터질 때만 터진다.** 그래서 루프가
    바뀌면 새로 만든다(서버는 루프가 하나라 늘 같은 잠금이다).
    """
    global _forecast_lock, _forecast_lock_loop
    loop = asyncio.get_running_loop()
    if _forecast_lock is None or _forecast_lock_loop is not loop:
        _forecast_lock = asyncio.Lock()
        _forecast_lock_loop = loop
    return _forecast_lock


def reset_forecast_cache() -> None:
    """예보 캐시를 비운다(시험 위생 · 재기동과 같은 자리)."""
    global _forecast_cache, _forecast_flight_error
    _forecast_cache = None
    _forecast_flight_error = None


async def forecast_hours(
    *, days: int | None = None, now: dt.datetime | None = None
) -> tuple[dt.datetime, list[HourlyForecast]]:
    """오늘·내일 시간대별 예보. 창구가 부르는 자리다.

    예보는 1시간 단위라 화면이 누를 때마다 바깥을 탈 이유가 없다. 캐시 열쇠는 **그 시각에
    부르려던 발표 슬롯(base_date·base_time)과 격자**다. 새 발표가 나오면 만료가 남아 있어도
    열쇠가 달라져서 저절로 새로 받는다.

    ⚠ 열쇠에 "부르려던" 슬롯을 넣는다(실제로 받아 온 슬롯이 아니라). 기상청 생성이 늦어
    한 칸 물러섰을 때, 받아 온 슬롯으로 열쇠를 잡으면 원하던 슬롯과 영영 안 맞아서 **요청마다
    바깥을 두드린다.** 이러면 늦는 동안에도 만료까지는 앞 발표를 그대로 쓴다.

    ⚠ 자름(오늘·내일)은 캐시 **밖**에서 한다. 자정을 넘긴 뒤에도 "오늘"이 지금 날짜를 따른다.

    ⚠ 동시에 여럿이 불러도 **바깥은 한 번만 탄다**(위 잠금 절). 부르는 쪽은 그대로 두면 된다.
    """
    global _forecast_cache, _forecast_flight_error, _forecast_flight_seq

    if days is None:
        # 설정이 뜻을 갖는 자리. 예전에는 코드 상수로 고정돼서 `WEATHER_FORECAST_DAYS`를
        # 넣어도 아무 일이 안 일어났다(2026-08-04 수리 W8).
        days = _setting_int("weather_forecast_days", FORECAST_DAYS)
    grid = (
        _setting_int("kma_nx", KMA_NX_GWANGJU),
        _setting_int("kma_ny", KMA_NY_GWANGJU),
    )
    key = (*kma_vilage_slots(now)[0], *grid)
    cached = _forecast_cache
    if cached is not None and cached[0] == key and time.monotonic() < cached[3]:
        return cached[1], slice_days(cached[2], days=days, now=now)

    # 잠금을 기다리기 **전에** 눈금을 적어 둔다. 기다리는 사이에 눈금이 오르면 "내가 기다린
    # 그 비행이 방금 끝났다"는 뜻이다.
    waited_seq = _forecast_flight_seq
    async with _forecast_flight_lock():
        cached = _forecast_cache
        if cached is not None and cached[0] == key and time.monotonic() < cached[3]:
            return cached[1], slice_days(cached[2], days=days, now=now)
        if _forecast_flight_seq != waited_seq and _forecast_flight_error is not None:
            # 내가 기다리는 사이에 앞사람이 실패했다. 줄줄이 다시 두드리지 않는다.
            logger.info("앞 예보 조회가 방금 실패했다 — 같은 실패를 그대로 돌려준다")
            raise _forecast_flight_error
        # ⚠ 비행을 **시작할 때** 비운다. 앞 비행의 실패가 남아 있으면, 이번 비행이 취소로
        # 끝났을 때(요청을 부른 화면이 창을 닫는 갈래) 기다리던 요청이 **낡은 실패**를
        # 받는다. 취소는 여기 안 담는다 — 남의 요청이 취소됐다고 내 요청까지 취소로
        # 끝나면 안 되니, 그때는 기다린 쪽이 자기 몫으로 바깥을 탄다.
        _forecast_flight_error = None
        try:
            base_at, hours = await fetch_kma_forecast(now=now)
        except Exception as exc:  # noqa: BLE001 - 사유는 부르는 쪽이 가려 적는다
            _forecast_flight_error = exc
            raise
        finally:
            _forecast_flight_seq += 1
        ttl = _setting_float("weather_forecast_cache_sec", WEATHER_FORECAST_CACHE_SEC)
        _forecast_cache = (key, base_at, hours, time.monotonic() + ttl)
    logger.info(
        "시간대별 예보를 새로 받았다 — 발표 %s, %d칸(%.0f초 동안 다시 안 부른다)",
        base_at.isoformat(), len(hours), ttl,
    )
    return base_at, slice_days(hours, days=days, now=now)


# ── 조회 한 자리 ────────────────────────────────────────────────────────────

async def _fetch_wttr_chain(
    client: httpx.AsyncClient | None = None,
) -> tuple[WeatherReading | None, str]:
    """예전 경로(wttr.in → wttr.is). 성공한 값과 마지막 실패 사유를 돌려준다."""
    from app.config import get_settings

    settings = get_settings()
    timeout = settings.weather_timeout_sec
    # 대체 도메인은 같은 본문을 주므로 파서 한 벌로 사슬을 돈다. 비워 두면 1차 하나만 탄다.
    urls = [settings.weather_api_url]
    if settings.weather_api_fallback_url:
        urls.append(settings.weather_api_fallback_url)

    last_reason = "조회할 주소가 없다"
    # ⚠ 클라이언트는 사슬 **밖**에서 한 번만 잡는다. 예전에는 고리마다 새로 파서, 1차가
    # 죽어 2차로 넘어가는 흔한 갈래가 악수를 두 번 했다.
    async with _http(client, timeout) as http:
        for order, url in enumerate(urls):
            try:
                res = await http.get(url)
                res.raise_for_status()
                reading = parse_wttr_j1(res.json())
            except Exception as exc:  # noqa: BLE001 - 어떤 실패든 다음 고리로 넘긴다
                last_reason = f"{url} — {type(exc).__name__}: {scrub_secrets(exc)}"
                continue
            if not reading.ok:
                last_reason = f"{url} — 본문 모양이 예상과 다르다"
                continue
            if order > 0:
                # 1차가 죽어 폴백이 받은 사실을 남긴다 — 조용히 넘기면 1차가 죽은 걸 아무도 모른다.
                logger.warning("날씨 1차 조회 실패, 대체 도메인이 받았다 — %s", last_reason)
            return reading, last_reason
    return None, last_reason


async def fetch_current_weather(
    client: httpx.AsyncClient | None = None,
) -> WeatherReading:
    """지금 날씨 하나. 기상청 → wttr 사슬 → 마지막 성공값 순서로 물러선다.

    성공하면 마지막 성공값을 갱신하고, 전부 실패하면 그 값을 stale로 돌려준다.
    예외는 절대 밖으로 안 나간다 — 날씨 한 번 못 받았다고 셔틀 출동이 멈추면 안 된다.

    `client`를 넘기면 그 커넥션 풀을 그대로 탄다(주기 조회가 넘긴다 — `_poll_loop`).
    안 넘기면 이 한 번을 위해 하나 파고 나갈 때 닫는다. 부르는 쪽 거동은 그대로다.
    """
    global _last_success

    reasons: list[str] = []
    kma_failed = False

    if kma_service_key():
        try:
            reading = await fetch_kma_weather(client=client)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 다음 고리로 넘긴다
            kma_failed = True
            # ⚠ 이 문구가 아래 logger.warning 두 줄을 타고 로그로 나간다 — 가린 뒤에 담는다(W1).
            reasons.append(f"기상청 — {type(exc).__name__}: {scrub_secrets(exc)}")
        else:
            _last_success = reading
            logger.info(
                "기상청 조회 성공 — %s(%s) 강수 %smm 기온 %s도",
                reading.status.value, reading.description, reading.precip_mm, reading.temp_c,
            )
            return reading
    else:
        reasons.append("기상청 — KMA_SERVICE_KEY가 비어 있어 안 탄다")

    if _setting_bool("weather_wttr_fallback_enabled", True):
        # 키가 아예 없는 판(예전 배포 그대로)은 정상이라 경고를 안 낸다 — 5분마다 도는
        # 루프라 여기서 warning을 내면 로그가 하루 288줄 도배된다. 키가 있는데 실패한
        # 경우만 시끄럽게 남긴다.
        if kma_failed:
            logger.warning("기상청 조회가 실패했다 — wttr 사슬로 물러선다: %s", reasons[-1])
        reading, reason = await _fetch_wttr_chain(client)
        if reading is not None:
            _last_success = reading
            logger.info(
                "날씨 조회 성공 — %s(%s) 강수 %smm 기온 %s도",
                reading.status.value, reading.description, reading.precip_mm, reading.temp_c,
            )
            return reading
        reasons.append(f"wttr — {reason}")

    return _fallback(" / ".join(reasons) or "조회 경로가 하나도 없다")


# ── 주기 조회 ──────────────────────────────────────────────────────────────
#
# 5분 × 오퍼레이션 둘이면 하루 576건이라 오퍼레이션당 한도 1만 건의 6%다. 주기를 1분으로
# 줄여도 2880건이라 여유는 있지만, 화면 칩이 1분마다 바뀔 이유가 없어서 5분이 기본이다.

WEATHER_POLL_INTERVAL_SEC = 300.0

# 조회가 이어서 실패하면 다음 바퀴를 뒤로 민다. 상류가 죽어 있는 동안 5분마다 두드려 봐야
# 받아 올 값은 없고 오퍼레이션 한도만 갉는다(2026-08-04 L38). 대기는 `주기 × 2^연속실패`이고
# 이 단계에서 멈춘다 — 3이면 기본 주기의 여덟 배(5분 → 40분)다. 한 번이라도 받아 오면
# 눈금이 0으로 돌아가서 바로 원래 주기다.
WEATHER_POLL_BACKOFF_STEPS = 3

# 한 바퀴가 끝날 때 받은 값을 넘길 자리. 목적지 전환·WS 방송이 여기 붙는다.
OnReading = Callable[["WeatherReading"], Awaitable[None]]

_poller_task: asyncio.Task | None = None


def start_weather_poller(
    on_reading: OnReading | None = None, *, interval_sec: float | None = None
) -> asyncio.Task | None:
    """주기 조회를 띄운다. 이미 돌고 있으면 그 태스크를 그대로 돌려준다.

    FastAPI `lifespan`에서 부르는 자리다(배선은 `app/main.py`, 팀장 몫). 첫 바퀴는 기다리지
    않고 **바로** 돈다 — 기동 직후 화면 칩이 비어 있는 시간을 줄인다.

    주기를 0 이하로 두면 아예 안 띄운다(되돌릴 자리). 그때는 예전처럼 그날 첫 물음이
    조회를 태우는 lazy 갱신만 남는다.
    """
    global _poller_task

    if _poller_task is not None and not _poller_task.done():
        return _poller_task

    interval = (
        interval_sec
        if interval_sec is not None
        else _setting_float("weather_poll_interval_sec", WEATHER_POLL_INTERVAL_SEC)
    )
    if interval <= 0:
        logger.info("날씨 주기 조회가 꺼져 있다(주기 %.1f초) — lazy 갱신만 돈다", interval)
        return None

    _poller_task = asyncio.create_task(_poll_loop(on_reading, interval), name="weather-poller")
    logger.info("날씨 주기 조회를 %.0f초 주기로 띄웠다", interval)
    return _poller_task


async def stop_weather_poller() -> None:
    """주기 조회를 세우고 **접힐 때까지 기다린다.**

    ⚠ 기다리지 않으면 종료 뒤에도 태스크가 다음 await에서 한 번 더 깨어난다. 시험에서는
    다음 케이스로 새고, 라이브에서는 종료 로그 뒤에 조회 로그가 한 줄 더 찍힌다.
    """
    global _poller_task

    task, _poller_task = _poller_task, None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _poll_loop(on_reading: OnReading | None, interval: float) -> None:
    """한 바퀴가 터져도 루프는 안 죽는다 — 다음 주기에 다시 본다.

    클라이언트 하나를 여기서 들고 바퀴마다 빌려준다(`_http` 설명). 수명을 쥔 자리가 여기라
    `stop_weather_poller`가 태스크를 접으면 `async with`가 같이 닫는다.
    """
    timeout = _setting_float("weather_timeout_sec", 4.0)
    failures = 0
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            try:
                reading = await fetch_current_weather(client)
                # ⚠ 백오프 잣대는 **조회가 됐나** 하나다. 아래 콜백(DB 쓰기·WS 방송)이
                # 터진 건 상류 한도와 무관해서 주기를 안 늘린다 — 늘리면 DB가 돌아온
                # 뒤에도 화면 칩이 최대 40분 낡은 채로 있는다.
                failures = 0 if reading.ok else min(failures + 1, WEATHER_POLL_BACKOFF_STEPS)
                if on_reading is not None:
                    await on_reading(reading)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 루프 밖으로 새면 주기 조회가 통째로 멈춘다
                logger.exception("날씨 주기 조회 한 바퀴가 실패했다 — 다음 주기에 다시 본다")
            wait = interval * (2 ** failures)
            if failures:
                logger.info(
                    "날씨 조회가 %d바퀴 이어서 실패했다 — 다음 바퀴를 %.0f초 뒤로 민다",
                    failures, wait,
                )
            await asyncio.sleep(wait)
