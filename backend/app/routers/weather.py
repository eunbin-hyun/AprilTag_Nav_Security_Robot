"""시간대별 예보 창구 (2026-08-03 사용자 확정 · 프론트에 계약을 미리 알린 자리).

관제 화면 상단의 기상 칩을 누르면 오늘·내일 시간대별 예보가 뜬다. 그 한 장을 주는 창구
하나뿐이다.

    GET /api/weather/forecast   (로그인 뒤)
    {
      "base_at": "2026-08-03T20:00:00+09:00",
      "hours": [
        { "at": "2026-08-03T21:00:00+09:00",
          "status": "CLEAR", "description": "맑음",
          "temp_c": 30, "pop": 0, "precip_mm": 0, "rain_or_snow": false,
          "humidity": 82, "wind_ms": 1.5 },
        ...
      ]
    }

## 이 파일이 판정을 안 한다

상태 낱말·문구·강수량·실외 차단은 전부 `app/weather.py`가 정한다. 여기는 그 값을 계약
모양으로 옮기기만 한다 — 같은 판정을 두 벌로 적으면 지금 날씨 칩과 예보 칸이 서로 다른
규칙으로 그려진다.

## ⚠ 이 창구는 `AUTH_REQUIRE_LOGIN`과 무관하게 늘 로그인을 요구한다

`require_agent_always`다. 같은 사이클에 붙은 카메라 창구와 같은 자물쇠고(`routers/camera.py`
`_gate`), 이유도 같다 — 제1 불변("지금 있는 창구의 거동을 안 바꾼다")은 **기존** 창구를
지키는 약속이라 새 창구에는 안 걸린다(`app/auth.py` `_role_dependency` always 주석).

2026-08-04 전체검토 D10이 뒤집은 자리다. 처음에는 `GET /api/dashboard/snapshot`과 맞춰
`require_agent`를 썼는데, 플래그가 꺼진 지금 배포에서 그게 **익명 창구**다. 그 뒤가 하루
1만 건짜리 기상청 오퍼레이션이라, 공개 도메인에 대고 누르는 만큼 남의 한도를 태울 수 있다.
아래 캐시·잠금이 증폭은 접어 주지만 "누가 부를 수 있나"는 못 좁힌다.

되돌릴 자리는 아래 `dependencies=[Depends(...)]` 한 줄이다.

## 새 스키마를 왜 여기 두나

`app/schemas.py`는 이번 사이클에 여러 조가 같이 만지는 파일이라 응답 모델을 이 파일 안에
뒀다(`app/routers/dispatch.py`와 같은 판단). 낱말 계약의 정본은 여전히
`schemas/command.schema.json`의 `weather_status` 여섯이고, 그 값을 만드는 자리는
`app/weather.WeatherStatus` 하나다.
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.auth import require_agent_always
from app.weather import KmaError, forecast_hours, scrub_secrets

logger = logging.getLogger("c207.weather")

router = APIRouter(prefix="/api/weather", tags=["weather"])

# 바깥이 죽었을 때 화면한테 "이만큼 뒤에 다시 오라"고 말하는 값(초). 기상청 오퍼레이션당
# 하루 한도가 1만 건이라, 실패를 즉시 재시도하는 화면 하나가 그 한도를 갉는다.
FORECAST_RETRY_AFTER_SEC = 30

# ⛔ 예보 한 번을 **끝까지** 도는 데 줄 총 시간(초). httpx `timeout=4.0`은 읽기 **사이**
# 간격이라 본문을 조금씩 나눠 받으면 그 값에 한 번도 안 걸린 채 훨씬 오래 끈다. 여기서
# 통째로 덮는다(2026-08-05 프론트 17차).
#
# 8초인 이유 — 한 번의 조회가 기상청을 최대 세 번 부른다(실황 · 실황 물러선 슬롯 · 예보).
# 하나에 4초를 다 쓰는 판을 겹쳐도 8초면 대개 끝나고, 그보다 오래 걸리면 사람이 화면 앞에서
# 기다릴 값이 아니다. ⚠ 늘릴 거면 화면 쪽 기다림 표시와 같이 봐야 한다.
FORECAST_TOTAL_BUDGET_SEC = 8.0


class ForecastHourOut(BaseModel):
    """한 시각 한 칸. 화면이 시간 띠 한 칸을 그리는 데 필요한 전부다.

    ⚠ **`extra="forbid"`가 두 목록이 갈린 걸 재는 못이다**(2026-08-04 L16). 이 일곱 칸의
    정본은 `app/weather.HourlyForecast.as_payload()`인데 두 파일에 갈려 있다. pydantic 기본
    (`extra="ignore"`)이면 `as_payload`에 칸을 더해도 여기서 조용히 버려져서 화면엔 안
    나가고 아무 데서도 안 빨개진다 — 새 칸을 넣은 사람이 프론트 탓을 하며 반나절을 쓴다.
    막아 두면 그 순간 예보 창구를 지나는 시험이 통째로 빨개진다.
    """

    model_config = ConfigDict(extra="forbid")

    # KST ISO 문자열(`+09:00`이 붙어 나간다).
    at: str
    # 계약 여섯 낱말(CLEAR·CLOUDY·LIGHT_RAIN·HEAVY_RAIN·SNOW·UNKNOWN).
    status: str
    description: str
    temp_c: int | None = None
    # 강수 확률(%).
    pop: int | None = None
    # 그 1시간 강수량(mm).
    precip_mm: float | None = None
    # 그 시각에 실외 주행을 막나. 지금 날씨의 실내·실외 잣대와 **같은 규칙**이다.
    rain_or_snow: bool
    # ⭐ 습도(%)·풍속(m/s) — 2026-08-06 사용자 요청. 단기예보가 이미 주던 값이다.
    # ⚠ 기상청이 그 칸을 안 주는 슬롯이 있어 둘 다 `None` 이 올 수 있다. 화면은 "—"로 그려라.
    humidity: int | None = None
    wind_ms: float | None = None


class ForecastOut(BaseModel):
    base_at: str
    hours: list[ForecastHourOut]


@router.get(
    "/forecast", response_model=ForecastOut, dependencies=[Depends(require_agent_always)]
)
async def get_forecast() -> ForecastOut:
    """오늘·내일 시간대별 예보.

    창구 안에서 짧게 캐시한다(기본 10분 · `WEATHER_FORECAST_CACHE_SEC`). 예보는 1시간
    단위라 화면이 누를 때마다 바깥을 탈 이유가 없고, 새 발표가 나오면 캐시 열쇠가 달라져서
    저절로 새로 받는다.

    ⚠ 바깥이 죽었으면 **503**이다. 지금 날씨(`weather_update`)처럼 마지막 성공값으로 물러서지
    않는다 — 그쪽은 값이 없으면 로봇 목적지를 못 정해서 낡은 값이라도 필요하지만, 여기는
    사람이 눌러서 보는 화면이라 낡은 예보를 조용히 보여 주는 쪽이 더 나쁘다.

    ⚠ 실패를 캐시에 안 담는 대신 **503에 `Retry-After`를 싣는다.** 안 실으면 화면이 곧장
    다시 부르는 흔한 모양에서 요청 하나가 그대로 기상청 호출 하나가 된다(2026-08-04 W2).
    동시에 몰린 요청은 `forecast_hours`의 잠금이 한 번으로 접어 준다.
    """
    try:
        # ⛔ **총 예산을 여기서 건다**(2026-08-05 프론트 17차). 아래 `forecast_hours`가 쓰는
        #    httpx `timeout=4.0`은 **읽기 사이 간격**이지 전체 시간이 아니다. 기상청이 본문을
        #    조금씩 나눠 보내면 그 4초에 한 번도 안 걸린 채 훨씬 오래 끈다 — 화면은 그동안
        #    "느리다"만 겪는다. `numOfRows`를 1500으로 올려 본문이 커진 만큼 더 커진 위험이다.
        #    ⚠ 이 자리는 **슬롯 물러서기까지 포함한 한 번의 조회 전체**를 덮는다. NO_DATA로
        #    슬롯 둘을 도는 갈래도 이 예산 안에서 끝난다.
        base_at, hours = await asyncio.wait_for(
            forecast_hours(), FORECAST_TOTAL_BUDGET_SEC
        )
    except (KmaError, httpx.HTTPError, asyncio.TimeoutError) as exc:
        # ⚠ 예외 문구에 요청 URL이 실릴 수 있고 거기 인증키가 들어 있다. 가린 뒤에만 싣는다.
        # ⚠ `asyncio.TimeoutError`도 같은 503 갈래다 — 화면 쪽에서 "기상청에서 못 받았다"로
        #   읽는 것이 맞고, 갈래를 늘리면 문구만 둘이 되고 사람이 할 일은 같다.
        logger.warning("시간대별 예보 조회가 실패했다 — %s", scrub_secrets(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="시간대별 예보를 받지 못했습니다. 잠시 뒤에 다시 시도해 주세요.",
            headers={"Retry-After": str(FORECAST_RETRY_AFTER_SEC)},
        )
    return ForecastOut(
        base_at=base_at.isoformat(),
        hours=[ForecastHourOut(**hour.as_payload()) for hour in hours],
    )
