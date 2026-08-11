"""쿨다운 창의 정확한 경계값(elapsed==0, elapsed==cooldown_sec) 시험.

`_in_cooldown`은 이벤트 시각 창과 벽시계 창을 AND로 묶어 `elapsed < cooldown_sec`을 본다.
두 끝을 다 찍어서 "이 창에 딱 걸치면 어느 쪽인가"를 코드가 아니라 시험으로 못박는다.
"""
import datetime as dt

import pytest

from app.config import get_settings
from app.credit.state_machine import Verdict, evaluate_pass
from app.db import get_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 24, 3, 0, 0, tzinfo=dt.timezone.utc)


async def _untagged(event_id: str, offset_sec: float, gate_no: int = 1):
    ts = BASE + dt.timedelta(seconds=offset_sec)
    async with get_session() as s:
        return await evaluate_pass(
            s, event_id=event_id, device_id="raspberry01", gate_no=gate_no,
            direction="A_TO_B", status="complete",
            beam_a_ts=ts, beam_b_ts=None, observed_at=ts,
        )


async def test_elapsed_zero_is_suppressed_only_inside_wall_window(wall_clock):
    """elapsed==0은 "언제나 삼킴"이 아니라 "벽시계 창 안일 때만 삼킴"이다.

    두 축을 AND로 묶기 전에는 시각이 안 움직이면 경고가 영구 봉쇄됐다.
    """
    cooldown = get_settings().alert_cooldown_sec
    ev1 = await _untagged("Z1", 0.0)
    ev2 = await _untagged("Z2", 0.0)  # 같은 이벤트 시각(elapsed=0) — 두 축 다 창 안쪽
    assert ev1.notify is True
    assert ev2.notify is False

    # 이벤트 시각은 그대로인데 실제 시간만 창을 넘겼다 — 다시 울려야 한다.
    wall_clock["t"] += cooldown + 1.0
    ev3 = await _untagged("Z3", 0.0)
    assert ev3.notify is True, "elapsed==0이라는 이유로 벽시계 창 밖에서도 삼켰다"


async def test_elapsed_exactly_cooldown_is_not_suppressed():
    cooldown = get_settings().alert_cooldown_sec
    ev1 = await _untagged("W1", 0.0)
    ev2 = await _untagged("W2", cooldown)  # elapsed == cooldown_sec 정확히 — 경계 밖(< 라서 통과)
    assert ev1.notify is True
    assert ev2.notify is True, "elapsed == cooldown_sec인데 삼켰다 — 경계가 <= 로 바뀐 회귀"


async def test_elapsed_just_under_cooldown_is_suppressed():
    cooldown = get_settings().alert_cooldown_sec
    ev1 = await _untagged("V1", 0.0)
    ev2 = await _untagged("V2", cooldown - 0.001)
    assert ev1.notify is True
    assert ev2.notify is False, "elapsed가 cooldown 바로 아래인데 삼키지 않았다"
