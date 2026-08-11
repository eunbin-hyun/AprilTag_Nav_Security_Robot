"""Robot.updated_at 갱신 계약 (S15P11C207-190).

로봇 상태 UPDATE가 붙으면 updated_at이 INSERT 때 값에 머물지 않고 다시 찍혀야 한다.
기존 시험은 is not None만 봐서 얼어붙은 시각을 통과시켰다 — 여기서 "커진다"까지 본다.

시각은 모델 다른 컬럼들과 같은 timestamptz + func.now() 방식이라, 서로 다른 트랜잭션이면
now()(트랜잭션 시작 시각)가 달라진다.
"""
import datetime as dt

import pytest

from app.db import get_session
from app.models import Robot

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _read_updated_at(robot_id: int) -> dt.datetime:
    """세션을 새로 열어 DB에 적힌 값을 그대로 읽는다(expire_on_commit=False 회피)."""
    async with get_session() as s:
        robot = await s.get(Robot, robot_id)
        return robot.updated_at


async def test_robot_updated_at_moves_on_update():
    """상태 UPDATE 뒤 updated_at이 INSERT 시각보다 커진다."""
    async with get_session() as s:
        robot = Robot(name="jetson-updated-at", mode="MANUAL", battery=50)
        s.add(robot)
        await s.commit()
        robot_id = robot.id

    inserted_at = await _read_updated_at(robot_id)
    assert inserted_at is not None
    assert inserted_at.tzinfo is not None, "다른 시각 컬럼과 같은 timestamptz여야 한다"

    async with get_session() as s:
        robot = await s.get(Robot, robot_id)
        robot.battery = 41
        await s.commit()

    after_update = await _read_updated_at(robot_id)
    assert after_update > inserted_at, (
        "상태 UPDATE에도 updated_at이 INSERT 시각에 머물렀다 — 대시보드가 낡은 값을 '방금'으로 본다"
    )


async def test_robot_updated_at_moves_on_every_update():
    """연속 UPDATE마다 시각이 다시 찍힌다(한 번만 갱신되고 마는 것도 결함)."""
    async with get_session() as s:
        robot = Robot(name="jetson-updated-at-2", mode="MANUAL", battery=90)
        s.add(robot)
        await s.commit()
        robot_id = robot.id

    stamps = []
    for battery in (80, 70, 60):
        async with get_session() as s:
            robot = await s.get(Robot, robot_id)
            robot.battery = battery
            await s.commit()
        stamps.append(await _read_updated_at(robot_id))

    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps), (
        f"UPDATE마다 updated_at이 올라가야 한다 — 실제 {stamps}"
    )
