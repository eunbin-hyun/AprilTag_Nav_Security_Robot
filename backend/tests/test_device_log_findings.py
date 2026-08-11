"""실기기 로그에서 캔 결함 둘 (2026-08-06 밤 · 팀원 교차 검증).

⭐ **자료 출처가 다르다.** 여태는 서버 DB만 보고 판단했는데, 사용자가 라파이·젯슨을 통째로
받아 와서(`기기수집_2026-08-06/`) **기기 쪽 원본**을 처음 봤다. 둘 다 거기서만 보이던 것이다.

① 연속 실패는 첫 건만 알림이 났다 — 그날 17분 안에 같은 사유로 네 번 멈췄는데 화면엔 한 번
② 재부팅 신호를 좌표 없는 프레임이 먹어서 지도의 로봇이 원점으로 순간이동했다
"""
import datetime as dt

import pytest

from app.jetson_adapter import adapt_jetson_frame, reset_jetson_adapter_state
from app.robot_channel import (
    handle_robot_state,
    register_ack_only_command,
    reset_robot_channel_state,
)
from app.schemas import MissionStatus, RobotStateIn

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ── ① 연속 실패 ────────────────────────────────────────────────────────


async def _fail(robot_id: str):
    return await handle_robot_state(
        RobotStateIn(robot_id=robot_id, mission_status=MissionStatus.FAILED)
    )


async def test_repeated_failure_without_new_command_is_one_event():
    """⚠ 명령이 안 나갔으면 같은 실패 재보고는 사건이 아니다(주기 보고 폭주 방어)."""
    reset_robot_channel_state()
    first = await _fail("fail-quiet")
    again = await _fail("fail-quiet")

    assert len(first.notices) == 1
    assert again.notices == [], "명령도 안 나갔는데 실패가 두 번 사건이 되면 알림이 폭주한다"


async def test_failure_after_new_command_is_a_new_event():
    """⭐ 사이에 새 명령이 나갔으면 **다시 멈춘 것**이라 알림이 또 나야 한다.

    ⛔ 2026-08-06 실기기 로그 — 15:12·15:24·15:25·15:29에 같은 사유(`ARM 차단 fault`)로
    네 번 멈췄고 그 사이마다 목적지 명령이 나갔는데, 화면에는 **첫 건만** 떴다.
    사람이 가서 봐야 하는 급(warning)인데 두 번째부터 조용했다.

    ⚠ 사유로는 못 가른다 — 그날 네 건이 글자까지 같았다.
    """
    reset_robot_channel_state()
    await _fail("fail-recommand")

    register_ack_only_command("return-after-fail")
    again = await _fail("fail-recommand")

    assert len(again.notices) == 1, (
        "새 명령을 내고 또 멈췄는데 알림이 안 났다 — 요원이 두 번째 멈춤을 모른다"
    )


async def test_standalone_command_counts_too():
    """⚠ 셔틀 출동만이 아니라 **복귀·즉시이동도** 새 사건의 근거다.

    명령 목록이 둘로 갈려 있어서(짝짓기용·ACK 대조용) 한쪽만 보면 복귀 도중 두 번 멈춘
    경우가 조용해진다.
    """
    reset_robot_channel_state()
    await _fail("fail-standalone")
    register_ack_only_command("move-1")

    assert len((await _fail("fail-standalone")).notices) == 1


# ── ② 재부팅 재정박 ────────────────────────────────────────────────────


def _odom(seq: int, **data):
    return {
        "type": "odom",
        "robot_id": "reanchor-1",
        "seq": seq,
        "data": {"frame": "odom", "position_type": "relative_estimate", **data},
    }


def test_reboot_survives_a_coordinateless_frame():
    """⛔ 재부팅 직후 첫 프레임이 좌표를 못 실어도 재정박 기회를 안 잃는다.

    예전에는 seq를 조기 반환보다 **먼저** 소비해서, 그 프레임이 `unavailable`이면 재정박은
    건너뛰는데 신호만 사라졌다. 다음 프레임부터는 seq가 늘어 재부팅으로 안 보였고,
    **지도의 로봇이 원점 근처로 순간이동한 채 그대로 남았다.**

    ⚠ 젯슨이 실제로 그 갈래를 탄다 — IMU를 못 쓰면 좌표 없는 프레임만 보낸다(회신 §4).
    """
    reset_jetson_adapter_state()
    # 재부팅 전 — 지도 좌표가 (10, 20) 근처까지 갔다.
    adapt_jetson_frame(_odom(5, x=10.0, y=20.0, yaw=0.0, valid=True))

    # 재부팅. seq가 되돌아갔는데 **좌표를 못 실었다.**
    adapt_jetson_frame(_odom(0, x=None, y=None, valid=False))

    # 그다음 유효 프레임 — 젯슨 원점이 0으로 되돌아간 상태다.
    result = adapt_jetson_frame(_odom(1, x=0.1, y=0.0, yaw=0.0, valid=True))

    position = result.payload["status_summary"]["position"]
    assert position["x"] > 9.0, (
        f"재정박을 못 걸어 지도의 로봇이 원점으로 순간이동했다 (x={position['x']})"
    )


def test_normal_sequence_does_not_reanchor():
    """⚠ 재부팅이 아니면 보정을 걸지 않는다 — 늘 걸면 로봇이 제자리에 붙박인다."""
    reset_jetson_adapter_state()
    adapt_jetson_frame(_odom(1, x=1.0, y=0.0, yaw=0.0, valid=True))
    result = adapt_jetson_frame(_odom(2, x=2.0, y=0.0, yaw=0.0, valid=True))

    assert result.payload["status_summary"]["position"]["x"] == pytest.approx(2.0)
