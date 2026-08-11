"""저신뢰 플래그(`unconsumed > low_confidence_credit_threshold`) 경계값 시험.

정찰 시험(test_low_confidence_when_unconsumed_piles_up)은 4장 남는 경우(초과)만
찍었다. 여기선 정확히 임계값(3) 자체는 초과가 아니라는 경계를 못박는다.
"""
import datetime as dt

import pytest

from app.config import get_settings
from app.credit.state_machine import Verdict, evaluate_pass
from app.db import get_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 24, 3, 0, 0, tzinfo=dt.timezone.utc)


async def _tag(session, event_id: str, tag_id: str) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models import TaggingEvent

    stmt = pg_insert(TaggingEvent).values(
        event_id=event_id,
        device_id="raspberry01",
        gate_no=1,
        tag_id=tag_id,
        observed_at=BASE,
        credit_state="issued",
        expires_at=BASE + dt.timedelta(seconds=get_settings().credit_ttl_sec),
    )
    await session.execute(stmt)
    await session.commit()


async def test_unconsumed_exactly_at_threshold_is_not_low_confidence():
    threshold = get_settings().low_confidence_credit_threshold
    # 통과 1건이 1장 먹고, 남은 유효 크레딧이 정확히 threshold(3)장이 되도록 threshold+1장 발급.
    async with get_session() as s:
        for i in range(threshold + 1):
            await _tag(s, f"BT{i}", f"tag-{i}")

    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P1", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            # 빔 두 칸을 실기기 모양(a < b)으로 채운다 — 빔 A만 있는 complete는 라파가 못
            # 만드는 자료라 정합 검사(S15P11C207-243)가 어긋난 주장으로 본다. 판정 창은
            # 그대로 beam_a_ts라 이 시험이 재는 임계 경계는 안 바뀐다.
            beam_a_ts=BASE + dt.timedelta(seconds=1),
            beam_b_ts=BASE + dt.timedelta(seconds=1.2),
            observed_at=BASE + dt.timedelta(seconds=1),
        )
    assert ev.verdict == Verdict.NORMAL
    assert ev.low_confidence is False, "unconsumed == threshold인데 저신뢰로 잡았다(경계가 >= 로 바뀐 회귀)"


async def test_unconsumed_one_over_threshold_is_low_confidence():
    threshold = get_settings().low_confidence_credit_threshold
    async with get_session() as s:
        for i in range(threshold + 2):
            await _tag(s, f"BT{i}", f"tag-{i}")

    async with get_session() as s:
        ev = await evaluate_pass(
            s, event_id="P1", device_id="raspberry01", gate_no=1,
            direction="A_TO_B", status="complete",
            # 빔 두 칸을 실기기 모양(a < b)으로 채운다 — 빔 A만 있는 complete는 라파가 못
            # 만드는 자료라 정합 검사(S15P11C207-243)가 어긋난 주장으로 본다. 판정 창은
            # 그대로 beam_a_ts라 이 시험이 재는 임계 경계는 안 바뀐다.
            beam_a_ts=BASE + dt.timedelta(seconds=1),
            beam_b_ts=BASE + dt.timedelta(seconds=1.2),
            observed_at=BASE + dt.timedelta(seconds=1),
        )
    assert ev.verdict == Verdict.NORMAL
    assert ev.low_confidence is True
