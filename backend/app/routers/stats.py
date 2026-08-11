"""통계 조회 API. 대시보드가 읽는 계열이라 조회 API 3종과 같이 인증 없이 연다(학내 시연 전제).

집계 규칙은 app.stats에 있다 — 여기선 파라미터를 받아 넘기고 검증만 한다.

두 창구 다 `STATS_EXCLUDE_TEST_DATA`(기본 꺼짐)를 켜면 시험 자료를 뺀 숫자를 낸다. 스위치가
요청 파라미터가 아니라 서버 설정인 건 일부러다 — 부르는 쪽이 고를 수 있으면 같은 화면의 두
카드가 서로 다른 모집단을 세고, 시연에서 어느 숫자가 맞나는 배포가 정해야 한다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_agent
from app.db import get_db
from app.schemas import (
    GATE_NO_MAX,
    GATE_NO_MIN,
    AlertBucketsOut,
    AlertResponseStatsOut,
    GatePassBucketsOut,
    GatePassHeatmapOut,
    TodayStatsOut,
    UntaggedStatsOut,
)
from app.stats import (
    DEFAULT_BUCKET_HOURS,
    DEFAULT_DAYS,
    DEFAULT_WINDOW,
    MAX_BUCKET_HOURS,
    MAX_DAYS,
    MAX_WINDOW,
    alert_response_stats,
    alert_severity_buckets,
    gate_pass_buckets,
    gate_pass_heatmap,
    today_counts,
    untagged_stats,
)

# 다섯 창구가 전부 요원이다(로그인 설계 확정본 §3). 라우터 한 자리에 걸어서 창구가 늘 때
# 새 줄이 게이트 없이 붙는 길을 없앤다.
#
# ⭐ 제1 불변 — `AUTH_REQUIRE_LOGIN=false`면 `require_agent`가 판정을 아예 안 한다. 그래서
# 켜기 전 구간에는 위 모듈 머리가 적은 "인증 없이 연다"가 글자 그대로 남는다.
router = APIRouter(tags=["stats"], dependencies=[Depends(require_agent)])


@router.get("/api/stats/untagged", response_model=UntaggedStatsOut)
async def get_untagged_stats(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="오늘을 포함한 최근 며칠"),
    window: int = Query(DEFAULT_WINDOW, ge=1, le=MAX_WINDOW, description="이동평균 창 크기(일)"),
    # ⚠ 상한이 빠져 있으면 이 값이 `app/stats.py`의 `GatePassEvent.gate_no == gate_no`로
    # 그대로 들어가고, 컬럼이 Integer라 int32를 넘는 값은 asyncpg DataError로 **500**이 된다.
    # 범위는 기기 인입 3종·화면 셔틀 호출과 같은 값을 쓴다(app.schemas).
    gate_no: int | None = Query(
        None,
        ge=GATE_NO_MIN,
        le=GATE_NO_MAX,
        description=f"지정하면 그 게이트만({GATE_NO_MIN}~{GATE_NO_MAX}), 비우면 전체 합",
    ),
    session: AsyncSession = Depends(get_db),
) -> UntaggedStatsOut:
    """미태깅 추이·이동평균·요일×시간대 히트맵을 한 번에 돌려준다.

    범위를 벗어난 파라미터는 조용히 자르지 않고 422다(/api/eta의 samples와 같은 정책).
    창이 구간보다 길면 이동평균이 전부 null이라 화면에 선이 아예 안 그려진다 — 값을 몰래
    줄이면 응답의 window와 실제 계산이 갈라지므로, 여기서도 자르지 않고 422로 돌려준다.
    """
    if window > days:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="이동평균 창은 조회 기간보다 길 수 없습니다.",
        )
    return await untagged_stats(session, days=days, window=window, gate_no=gate_no)


@router.get("/api/stats/today", response_model=TodayStatsOut)
async def get_today_stats(session: AsyncSession = Depends(get_db)) -> TodayStatsOut:
    """오늘(KST) 종류별 건수. 파라미터가 없다 — 하루 경계는 서버가 정한다.

    개발자 텔레메트리 화면이 목록을 받아 스스로 세던 값이다. 목록 조회는 500건에서 잘려서
    하루 건수가 그 위로 올라가면 화면 숫자가 실제보다 적었다. 세는 자리를 서버로 옮겨
    상한을 없앴고, 하루를 자르는 규칙은 /api/stats/untagged와 같은 함수를 쓴다.
    """
    return await today_counts(session)


@router.get("/api/stats/alert-response", response_model=AlertResponseStatsOut)
async def get_alert_response_stats(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="오늘을 포함한 최근 며칠"),
    types: str | None = Query(
        default=None,
        max_length=200,
        description="셀 경고 종류를 쉼표로. 생략하면 전 종류(예: untagged)",
    ),
    session: AsyncSession = Depends(get_db),
) -> AlertResponseStatsOut:
    """경고 확인·종결 소요시간을 구간 전체와 종류별로 돌려준다(프론트 요구 N2).

    소요시간은 경고가 생긴 시각(created_at)에서 acked_at·resolved_at까지의 초다. 확인·종결이
    아직 없는 경고는 표본에서 빠지고 total에만 남는다 — 0초로 채우면 "빨리 봤다"로 읽힌다.

    범위 밖 days는 조용히 자르지 않고 422다(/api/stats/untagged와 같은 정책).

    ⛔ **`types` 를 안 주면 안 가린다 — 그것이 기존 거동이다**(2026-08-07 · 화면 58차).
    이 창구를 **두 화면이 같이 쓰고 서로 다른 값을 기대한다.**

      - 교대 결산 — `?days=1&types=untagged` 로 무단 통과만
      - AI 탭 대응 통계 — 인자 없이, 이레 · 전 종류

    ⚠ **모르는 종류를 줘도 422가 아니라 0건이다.** 알림 종류가 코드 여러 자리에서 늘어나는
    값이라 여기서 닫힌 집합으로 막으면, 종류를 새로 만든 날 이 창구가 먼저 깨진다.
    """
    wanted = tuple(t.strip() for t in types.split(",") if t.strip()) if types else None
    return await alert_response_stats(session, days=days, types=wanted)


@router.get("/api/stats/gate-pass-buckets", response_model=GatePassBucketsOut)
async def get_gate_pass_buckets(
    hours: int = Query(
        DEFAULT_BUCKET_HOURS,
        ge=1,
        le=MAX_BUCKET_HOURS,
        description="지금이 든 칸까지 거슬러 몇 시간(15분 칸으로 나뉜다)",
    ),
    session: AsyncSession = Depends(get_db),
) -> GatePassBucketsOut:
    """게이트 통과 15분 버킷 × 판정 4갈래(프론트 요구 N12).

    칸 크기는 파라미터가 아니라 15분 고정이다 — 화면 두 칩이 서로 다른 격자를 그리면 같은
    차트에서 눈금이 갈린다. 건수가 0인 칸도 빠짐없이 실려서 조용하던 시간대가 안 사라진다.

    범위 밖 hours는 422다(/api/stats/untagged와 같은 정책).
    """
    return await gate_pass_buckets(session, hours=hours)


@router.get("/api/stats/gate-pass-heatmap", response_model=GatePassHeatmapOut)
async def get_gate_pass_heatmap(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="오늘을 포함한 최근 며칠"),
    gate_no: int | None = Query(None, ge=GATE_NO_MIN, le=GATE_NO_MAX),
    session: AsyncSession = Depends(get_db),
) -> GatePassHeatmapOut:
    """요일 × 시간대 통과 히트맵 (프론트 2차 요구 · 관제 개요 카드).

    ⭐ **판정을 안 가린다** — `/api/stats/untagged`의 히트맵은 미태깅만 세지만 이쪽은 통과
    전체다. "언제 사람이 몰리나"를 보는 카드라 정상 통과까지 들어가야 뜻이 선다.

    격자는 늘 7행 24열로 꽉 찬다. `max_count`는 색 농도 기준이고, 다 0이면 `peak`가 null이다.

    범위 밖 days는 422다(/api/stats/untagged와 같은 정책).
    """
    return await gate_pass_heatmap(session, days=days, gate_no=gate_no)


@router.get("/api/stats/alert-buckets", response_model=AlertBucketsOut)
async def get_alert_buckets(
    days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS, description="오늘을 포함한 최근 며칠"),
    session: AsyncSession = Depends(get_db),
) -> AlertBucketsOut:
    """경고 24시간 버킷 × 심각도 3갈래(프론트 요구 N12).

    칸 경계는 KST 로컬 날짜(00:00 KST)라 /api/stats/untagged의 daily와 같은 하루를 쓴다.
    지금부터 24시간씩 거꾸로 세지 않는 이유는 그러면 칸이 자정에 안 맞아 "어제"가 두 칸에
    걸치기 때문이다.

    범위 밖 days는 422다(/api/stats/untagged와 같은 정책).
    """
    return await alert_severity_buckets(session, days=days)
