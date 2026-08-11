"""주행 시계열이 실제로 쌓이나 (마이그레이션 0017).

⭐ **왜 이 시험이 있나** — 2026-08-06 로봇이 처음 붙은 날 주행 실패가 5건 났는데 사유도
그때 위치도 안 남아 원인을 못 봤다. `robot_status_log`는 0001이 만든 뒤로 INSERT가 한 자리도
없는 빈 표였다. 그 자리를 살렸고, **다시 죽는 것을 막는 못**이 여기다.

⚠ **"칸이 있다"가 아니라 "행이 쌓인다"를 본다.** 모델에 칸을 더해 놓고 배선을 안 붙이면
`Base.metadata`는 멀쩡한데 표는 계속 빈다 — 그게 0001부터 0016까지 실제로 벌어진 일이다.
"""
import pytest
from sqlalchemy import select

from app.db import get_session
from app.models import Robot, RobotStatusLog
from app.robot_channel import handle_robot_state
from app.schemas import MissionStatus, RobotStateIn

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _rows(robot_name: str) -> list[RobotStatusLog]:
    """그 로봇이 남긴 시계열을 시각 순으로 읽는다."""
    async with get_session() as s:
        robot = (
            await s.execute(select(Robot).where(Robot.name == robot_name))
        ).scalar_one()
        result = await s.execute(
            select(RobotStatusLog)
            .where(RobotStatusLog.robot_id == robot.id)
            .order_by(RobotStatusLog.ts, RobotStatusLog.id)
        )
        return list(result.scalars())


async def test_odom_and_imu_are_stored():
    """중계만 되고 사라지던 odom·imu가 행으로 남는다."""
    await handle_robot_state(
        RobotStateIn(
            robot_id="log-odom",
            odom={"x": 1.5, "y": -2.5, "yaw": 0.3, "valid": True},
            imu={"ax": 0.01, "az": 9.8},
        )
    )

    rows = await _rows("log-odom")
    assert len(rows) == 1, "odom·imu를 실은 프레임이 한 행도 안 남았다"
    assert rows[0].odom["x"] == 1.5
    assert rows[0].imu["az"] == 9.8


async def test_failure_reason_is_stored():
    """⭐ 주행 실패 사유(`log`)가 그때 위치와 함께 남는다 — 이 시험이 이 표의 존재 이유다."""
    await handle_robot_state(
        RobotStateIn(
            robot_id="log-failed",
            mission_status=MissionStatus.FAILED,
            log="에이프릴태그를 12초 동안 못 봐서 멈췄다",
            status_summary={"position": {"x": 2.0, "y": -1.3, "theta": 0.5}},
        )
    )

    rows = await _rows("log-failed")
    assert len(rows) == 1
    assert rows[0].log == "에이프릴태그를 12초 동안 못 봐서 멈췄다", (
        "젯슨이 실은 실패 사유가 버려졌다 — 왜 멈췄는지 되짚을 길이 없어진다"
    )
    assert rows[0].mission_status == "FAILED"
    assert rows[0].position["x"] == 2.0, "실패한 그 자리를 알아야 원인을 좁힌다"


async def test_original_odom_and_map_position_are_both_kept():
    """⚠ 젯슨 원본(`odom`)과 서버 보정 지도 좌표(`position`)를 **둘 다** 남긴다.

    앞은 로봇을 켠 자리가 원점인 상대 좌표이고 뒤는 재부팅 오프셋을 보정한 값이라 서로 다르다.
    하나만 남기면 보정이 맞았는지 나중에 못 가린다.
    """
    await handle_robot_state(
        RobotStateIn(
            robot_id="log-both",
            odom={"x": 0.5, "y": 0.5},
            status_summary={"position": {"x": 3.5, "y": -4.5, "theta": 0.0}},
        )
    )

    row = (await _rows("log-both"))[0]
    assert row.odom["x"] == 0.5, "젯슨 원본이 사라졌다"
    assert row.position["x"] == 3.5, "서버 보정 좌표가 사라졌다"


async def test_frames_accumulate_instead_of_overwriting():
    """⚠ `robot` 표와 달리 덮어쓰지 않고 쌓인다 — "그때 어디 있었나"가 목적이라서다."""
    for x in (1.0, 2.0, 3.0):
        await handle_robot_state(
            RobotStateIn(robot_id="log-accum", odom={"x": x, "y": 0.0})
        )

    rows = await _rows("log-accum")
    assert len(rows) == 3, "덮어쓰고 있다 — 시계열이 아니라 현재 상태가 된다"
    assert [r.odom["x"] for r in rows] == [1.0, 2.0, 3.0]


async def test_jetson_reason_lands_in_log_column():
    """⭐ 젯슨이 `mission_status.reason`으로 보낸 사유가 `log` 칸까지 닿는다.

    ⛔ 2026-08-06까지는 어댑터가 그 값을 뽑고도 버렸다 — 담을 자리가 없었기 때문이다.
    이름이 둘인 까닭은 계약이 먼저 `log`로 서 있었고 젯슨 프레임이 `reason`으로 와서다.
    **가운데(어댑터)에서 한 번만 옮기고 서버 안쪽은 `log` 한 이름으로 흐른다.**
    """
    from app.jetson_adapter import adapt_jetson_frame, reset_jetson_adapter_state

    reset_jetson_adapter_state()
    adapted = adapt_jetson_frame(
        {
            "type": "mission_status",
            "robot_id": "log-reason",
            "seq": 1,
            "data": {"status": "FAILED", "reason": "FAULT_OVERRUN — 태그 2를 못 봤다"},
        }
    )
    assert adapted.error is None, adapted.error
    assert adapted.payload["log"] == "FAULT_OVERRUN — 태그 2를 못 봤다", (
        "젯슨 사유가 어댑터에서 버려졌다 — 왜 멈췄는지 되짚을 길이 다시 막힌다"
    )

    await handle_robot_state(RobotStateIn.model_validate(adapted.payload))

    rows = await _rows("log-reason")
    assert len(rows) == 1
    assert rows[0].log == "FAULT_OVERRUN — 태그 2를 못 봤다"
    assert rows[0].mission_status == "FAILED"


async def test_empty_frame_writes_nothing():
    """⚠ 남길 게 없는 프레임은 행을 안 만든다.

    젯슨은 배터리·모드만 바뀐 보고도 올리는데 그건 `robot` 행이 이미 들고 있다. 그것까지
    쌓으면 표가 두 벌이 되고, 되짚을 때 빈 행을 헤집게 된다.
    """
    await handle_robot_state(
        RobotStateIn(
            robot_id="log-empty",
            status_summary={"battery": 77, "mode": "MANUAL"},
        )
    )

    assert await _rows("log-empty") == [], "남길 값이 없는 프레임이 빈 행을 만들었다"
