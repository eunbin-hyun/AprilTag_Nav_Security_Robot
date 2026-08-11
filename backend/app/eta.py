"""예상 도착 시간 (지라 S15P11C207-158 · 역할상세 §5).

구간은 지금 하나다 — 셔틀 도착 신호(`ShuttleArrival.signal_ts`)에서 그 신호에 묶인 로봇
도착 알림(`Alert.type='robot_arrival'`)까지 걸린 시간. 짝짓기를 만드는 쪽이 robot_channel(-156)
이라 알림 종류 이름과 짝짓기 질의도 거기서 가져다 쓴다. 여기선 그 기록을 읽기만 한다.

최근 N건을 오래된 것부터 EWMA로 눌러 쓴다. 단순 평균이 아닌 이유는 최근 주행(정체·속도
변화)을 반영하면서도 이상치에 덜 흔들리기 때문이다. 구간이 하나뿐이라 세그먼트 합산은
아직 없다 — 노선이 여러 구간으로 쪼개지면 구간별 EWMA를 더하는 방식으로 늘린다.

기상청 보정(+N분)과 LLM 안내 문장은 여기 없다. 역할상세 §5의 P1이고 API 키·보정 계수가
팀 확정 대기라, 지금은 실측 기록만으로 낸다.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import Alert, ShuttleArrival
from app.robot_channel import (
    ROBOT_ARRIVAL_ALERT_TYPE,
    SHUTTLE_SOURCE_TYPE,
    latest_unmatched_arrival,
)
from app.schemas import EtaOut

SAMPLE_LIMIT_MAX = 200


async def recent_segment_seconds(
    session: AsyncSession, gate_no: int | None, limit: int
) -> list[float]:
    """최근 구간 소요시간을 오래된 것부터 돌려준다(EWMA를 시간 순서로 먹여야 하니까).

    도착 시각은 Alert.created_at(서버 수신 시각)이다. 로봇이 올리는 RobotState에는 관측
    시각 필드가 없어서(스키마 3종 유지) 서버가 받은 시각을 도착 시각으로 삼는다.
    """
    stmt = (
        select(ShuttleArrival.signal_ts, Alert.created_at)
        .join(
            Alert,
            and_(
                Alert.source_type == SHUTTLE_SOURCE_TYPE,
                Alert.source_id == ShuttleArrival.id,
                Alert.type == ROBOT_ARRIVAL_ALERT_TYPE,
            ),
        )
    )
    if gate_no is not None:
        stmt = stmt.where(ShuttleArrival.gate_no == gate_no)
    stmt = stmt.order_by(ShuttleArrival.signal_ts.desc()).limit(limit)

    max_sec = get_settings().eta_max_sample_sec
    seconds: list[float] = []
    for signal_ts, arrived_at in (await session.execute(stmt)).all():
        gap = (arrived_at - signal_ts).total_seconds()
        # 음수(기기 시계 어긋남)와 지나치게 큰 값(신호만 오고 한참 뒤 도착)은 버린다.
        if 0 < gap <= max_sec:
            seconds.append(gap)
    seconds.reverse()
    return seconds


def ewma(values: Sequence[float], alpha: float) -> float | None:
    """지수 이동평균. 첫 값에서 출발해 뒤로 갈수록 alpha 가중을 준다. 빈 목록이면 None."""
    if not values:
        return None
    smoothed = values[0]
    for value in values[1:]:
        smoothed = alpha * value + (1 - alpha) * smoothed
    return smoothed


async def estimate_eta(
    session: AsyncSession, *, gate_no: int | None = None, limit: int | None = None
) -> EtaOut:
    """구간 기록으로 예상 도착 시간을 낸다. 기록이 없으면 빈 응답(method="none")."""
    settings = get_settings()
    sample_limit = (
        settings.eta_sample_limit if limit is None else max(1, min(limit, SAMPLE_LIMIT_MAX))
    )
    seconds = await recent_segment_seconds(session, gate_no, sample_limit)
    eta_sec = ewma(seconds, settings.eta_ewma_alpha)

    if eta_sec is None:
        return EtaOut(
            gate_no=gate_no,
            method="none",
            samples=0,
            message="아직 주행 기록이 없어 예상 도착 시간을 계산하지 못합니다.",
        )

    pending = await latest_unmatched_arrival(session, gate_no)
    eta_at = (
        pending.signal_ts + dt.timedelta(seconds=eta_sec) if pending is not None else None
    )
    return EtaOut(
        gate_no=gate_no,
        method="ewma",
        samples=len(seconds),
        eta_sec=round(eta_sec, 1),
        eta_at=eta_at,
        alpha=settings.eta_ewma_alpha,
        measured_sec=[round(s, 1) for s in seconds],
        message=(
            f"예상 도착까지 약 {round(eta_sec)}초 걸립니다. "
            f"최근 주행 {len(seconds)}건을 반영했습니다."
        ),
    )
