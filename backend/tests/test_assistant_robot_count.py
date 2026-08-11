"""브리핑의 온라인 로봇 수가 진짜인가 (2026-08-06 · 팀원 교차 검증).

⛔ **죽은 비교였다.** `Robot.network_status == "online"` 으로 세고 있었는데 통신 상태 낱말은
`CommStatus` 셋(`WS_OK`·`POLLING_GRACE`·`SERVER_DOWN`)뿐이다. **어떤 로봇이 붙어 있어도 늘
0**이었고, 브리핑이 "온라인 로봇 0대"를 계속 말했다.

⚠ 이 계열은 조용해서 무섭다 — 값이 0이라도 SQL은 성공하고 화면도 정상으로 보인다.
"숫자가 이상하다"를 누가 눈치채기 전까지 아무도 모른다.
"""
import datetime as dt

import pytest
from sqlalchemy import delete

from app.db import get_session
from app.models import Robot
from app.assistant_data import collect_daily_facts
from app.schemas import CommStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_online_robot_count_uses_real_contract_word():
    """붙어 있는 로봇을 실제로 센다."""
    async with get_session() as s:
        await s.execute(delete(Robot))
        s.add_all(
            [
                Robot(name="on-1", network_status=CommStatus.WS_OK.value),
                Robot(name="on-2", network_status=CommStatus.WS_OK.value),
                Robot(name="off-1", network_status=CommStatus.SERVER_DOWN.value),
            ]
        )
        await s.commit()

    async with get_session() as s:
        facts = await collect_daily_facts(s, dt.date.today())

    assert facts["robots"]["online"] == 2, (
        f"온라인 로봇을 {facts['robot_online']}대로 셌다 — 계약에 없는 낱말로 비교하면 늘 0이 된다"
    )
    assert facts["robots"]["total"] == 3


async def test_zero_when_nothing_is_connected():
    """⚠ 늘 0이던 결함을 고치면서 **진짜 0인 경우까지 잃으면 안 된다.**"""
    async with get_session() as s:
        await s.execute(delete(Robot))
        s.add(Robot(name="off-only", network_status=CommStatus.SERVER_DOWN.value))
        await s.commit()

    async with get_session() as s:
        facts = await collect_daily_facts(s, dt.date.today())

    assert facts["robots"]["online"] == 0
    assert facts["robots"]["total"] == 1


async def test_낱말이_WS_OK여도_오래된_보고면_온라인이_아니다():
    """⛔ `network_status`는 마지막 프레임에 찍히고 **스스로 안 내려간다.**

    로봇이 꺼져도 `WS_OK`가 그대로 남는다. 실제로 끊긴 지 **8시간 51분** 뒤에도 브리핑이
    "1대가 온라인"이라 말했고, 같은 순간 화면은 "신호 지연"이라 적어 **두 곳이 다른 말을
    했다**(프론트 48차, 2026-08-07).

    ⭐ 참·거짓을 나란히 심는다 — 방금 온 로봇과 오래된 로봇을 같은 낱말로 두고, 세는 쪽이
    시각을 보는지만 가른다. 한쪽만 심으면 "늘 0"이나 "늘 전부"가 정상으로 읽힌다.
    """
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as s:
        s.add_all(
            [
                Robot(
                    name="fresh-1",
                    network_status=CommStatus.WS_OK.value,
                    updated_at=now,
                ),
                Robot(
                    name="stale-1",
                    network_status=CommStatus.WS_OK.value,
                    updated_at=now - dt.timedelta(hours=8, minutes=51),
                ),
            ]
        )
        await s.commit()

    async with get_session() as s:
        facts = await collect_daily_facts(s, dt.date.today())

    assert facts["robots"]["total"] == 2
    assert facts["robots"]["online"] == 1, (
        "낱말만 보고 세면 꺼진 로봇도 온라인이 된다 — 마지막 보고 시각을 같이 봐야 한다"
    )
