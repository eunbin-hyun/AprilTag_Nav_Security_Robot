"""서버가 낸 명령과 기기 응답이 DB에 남나 (마이그레이션 0018 · §26-4).

⭐ **왜 있나** — 2026-08-06 로봇이 처음 붙은 날 확인해 보니 서버가 낸 명령이 **DB에 한 줄도
안 남고 있었다.** 로그 파일은 컨테이너를 갈면 사라지고 질의도 안 된다.

⛔ **그 옆에서 더 큰 게 나왔다.** 단독 명령(복귀·즉시이동·긴급정지)이 ACK 대조 목록에 아예
안 올라가서 젯슨 ACK가 전부 "모르는 번호"였고, `_log_command_ack`가 거기서 **조기 반환**하고
있어서 **거절이 화면까지 안 갔다.** 요원이 복귀를 눌렀는데 아무 반응이 없는 자리였다.
"""
import pytest
from sqlalchemy import select

from app.command_log import ACK_PLAYED, CHANNEL_GATE, CHANNEL_ROBOT, record_ack
from app.db import get_session
from app.models import DeviceCommandLog
from app.robot_channel import (
    command_link_known,
    notify_shuttle_arrival,
    register_ack_only_command,
    reset_robot_channel_state,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _rows(command_id: str) -> list[DeviceCommandLog]:
    async with get_session() as s:
        result = await s.execute(
            select(DeviceCommandLog)
            .where(DeviceCommandLog.command_id == command_id)
            .order_by(DeviceCommandLog.id)
        )
        return list(result.scalars())


# ── ACK 대조 (§26-4) ────────────────────────────────────────────────────


async def test_standalone_command_is_known_for_ack():
    """⭐ 셔틀 신호가 없는 단독 명령도 ACK 대조에서 아는 번호가 된다."""
    reset_robot_channel_state()
    register_ack_only_command("return-42")

    assert command_link_known("return-42") is True, (
        "복귀·즉시이동·긴급정지 ACK가 다시 '모르는 번호'로 떨어진다"
    )


async def test_unregistered_command_stays_unknown():
    """⚠ 안 낸 번호는 여전히 모르는 번호다 — 대조가 늘 참이 되면 검사가 죽는다."""
    reset_robot_channel_state()
    register_ack_only_command("return-43")

    assert command_link_known("return-99") is False


async def test_reset_clears_ack_only_list():
    """⚠ 시험 위생. 안 비우면 앞 케이스가 낸 이름이 다음 케이스 검사를 통과시킨다."""
    register_ack_only_command("return-44")
    reset_robot_channel_state()

    assert command_link_known("return-44") is False


# ── 기록 (0018) ─────────────────────────────────────────────────────────


async def test_robot_ack_is_recorded():
    """젯슨이 돌려준 응답이 행으로 남는다."""
    await record_ack(
        channel=CHANNEL_ROBOT,
        command_id="cmd-log-1",
        result="rejected",
        detail="배터리가 모자라 못 간다",
        target="jetson01",
    )

    rows = await _rows("cmd-log-1")
    assert len(rows) == 1
    assert rows[0].ack_result == "rejected"
    assert rows[0].ack_detail == "배터리가 모자라 못 간다"
    assert rows[0].target == "jetson01"
    assert rows[0].ack_at is not None


async def test_gate_sound_ack_is_recorded():
    """⭐ 게이트 소리는 발행 행이 없고 ack 행만 난다 — 큐가 인메모리 동기 함수라서다."""
    await record_ack(channel=CHANNEL_GATE, command_id="sound-77", result=ACK_PLAYED)

    rows = await _rows("sound-77")
    assert len(rows) == 1, "소리가 실제로 울린 기록이 안 남았다"
    assert rows[0].channel == CHANNEL_GATE
    assert rows[0].ack_result == ACK_PLAYED
    assert rows[0].issued_at is None, "발행 행이 없는 것이 이 채널의 정상이다"


async def test_late_ack_still_recorded():
    """⚠ 발행 행을 못 찾아도 ack 사실은 안 버린다.

    TTL이 지난 늦은 ACK가 실제로 온다. "모르는 번호였다"는 것 자체가 되짚을 값이다.
    """
    await record_ack(
        channel=CHANNEL_ROBOT, command_id="cmd-never-issued", result="accepted"
    )

    assert len(await _rows("cmd-never-issued")) == 1


async def test_long_detail_is_clipped():
    """⚠ 로봇 채널 인증이 기본 꺼짐이라 아무나 긴 글자를 올릴 수 있다."""
    await record_ack(
        channel=CHANNEL_ROBOT, command_id="cmd-long", result="rejected", detail="가" * 5000
    )

    assert len(((await _rows("cmd-long"))[0]).ack_detail) == 500


# ── 셔틀 출동도 발행을 남긴다 (2026-08-07 실기기 주행이 잡았다) ──────────────


async def test_셔틀_출동도_발행_시각을_남긴다():
    """⛔ **통로마다 갈려 있었다.** 단독 명령은 발행을 남기는데 셔틀 출동은 안 남겼다.

    2026-08-07 실기기 주행에서 드러났다 — `shuttle-52` 행에 발행 시각이 비고 ACK만 찍혀
    있었다. 응답이 와야 행이 생기는 구조라, **로봇이 답을 안 하면 기록이 통째로 없다.**
    정작 알고 싶은 "명령은 냈는데 답이 없다"가 그 경우다.

    ⭐ **로봇 0대에서 잰다.** 그 판이 바로 기록이 사라지던 자리다.
    """
    reset_robot_channel_state()
    async with get_session() as s:
        delivered, command_id = await notify_shuttle_arrival(s, arrival_id=9001, gate_no=5)
        await s.commit()

    assert delivered == 0
    rows = await _rows(command_id)
    assert len(rows) == 1, "명령을 냈는데 기록이 없다 — ACK가 와야 행이 생기는 구조로 돌아갔다"

    row = rows[0]
    assert row.issued_at is not None, "발행 시각이 비면 '언제 냈나'를 되짚을 수 없다"
    assert row.ack_at is None
    assert row.channel == CHANNEL_ROBOT
    assert row.payload["delivered"] == 0, "0대에 나간 사실 자체가 기록으로 남아야 한다"
    assert row.payload["arrival_id"] == 9001
    assert row.payload["gate_no"] == 5
    assert row.command_type, "명령 종류가 비면 무슨 명령이었는지 못 가린다"


async def test_셔틀_출동_ACK가_같은_행을_갱신한다():
    """⚠ 발행을 남기기 시작했으니 **ACK가 새 행을 만들면 안 된다** — 한 명령이 두 줄이 된다."""
    reset_robot_channel_state()
    async with get_session() as s:
        _, command_id = await notify_shuttle_arrival(s, arrival_id=9002, gate_no=5)
        await s.commit()

    await record_ack(
        channel=CHANNEL_ROBOT, command_id=command_id, result="accepted", detail="outbound 미션 시작"
    )

    rows = await _rows(command_id)
    assert len(rows) == 1, f"한 명령이 {len(rows)}줄이 됐다 — 발행 행을 못 찾고 새로 만들었다"
    assert rows[0].issued_at is not None
    assert rows[0].ack_at is not None
    assert rows[0].ack_result == "accepted"
