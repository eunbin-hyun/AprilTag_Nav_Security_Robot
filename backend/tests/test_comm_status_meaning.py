"""통신 상태는 서버가 직접 판정한다 (2026-08-06 · 프론트 40·42차 · 팀원 교차 검증).

⛔ **셋이 따로 같은 자리를 짚었다.** 젯슨 `comm_status`는 `/stm32/connected`를 실어
**STM32 시리얼 링크**를 말하는데, 서버 `CommStatus`는 **로봇↔서버 통신**이다("소켓 close가
아니라 하트비트 연속 미스로 판정한다"). 이름만 같고 뜻이 달랐다.

그대로 받던 동안 **UART 케이블이 빠지면 화면이 "통신 정지"라 말하고 명령 버튼까지 잠겼다** —
웹 연결은 멀쩡한데도. 프론트가 42차 §3-2에서 갈래 셋을 주며 정해 달라고 했고,
"서버가 두 칸을 갈라 받는다"로 답했다.
"""
import pytest

from app.jetson_adapter import adapt_jetson_frame, reset_jetson_adapter_state
from app.schemas import CommStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _status(comm: str):
    return {
        "type": "status_summary",
        "robot_id": "comm-1",
        "seq": 1,
        "data": {"battery": None, "operation_mode": "unknown", "comm_status": comm},
    }


def test_uart_down_does_not_say_comm_down():
    """⛔ **이 시험이 이 파일의 존재 이유다.**

    STM32 링크가 끊겼다고 로봇↔서버 통신이 끊긴 것은 아니다. 이 프레임이 도착했다는 사실
    자체가 통신이 살아 있다는 증거다.
    """
    reset_jetson_adapter_state()
    result = adapt_jetson_frame(_status("disconnected"))

    assert result.payload["status_summary"]["comm_status"] == CommStatus.WS_OK.value, (
        "UART가 끊겼다고 통신 정지로 적으면 화면이 명령 버튼을 통째로 잠근다"
    )


def test_jetson_value_is_kept_as_stm32_link():
    """⚠ 버리지는 않는다 — 개발자 텔레메트리로 흘려 되짚을 근거를 남긴다."""
    reset_jetson_adapter_state()
    result = adapt_jetson_frame(_status("disconnected"))

    assert result.payload["sensor_health"]["stm32_link"] == "disconnected"


def test_connected_also_lands_in_stm32_link():
    """정상일 때도 같은 자리에 남는다. 값이 한쪽에만 있으면 화면이 판정을 못 한다."""
    reset_jetson_adapter_state()
    result = adapt_jetson_frame(_status("connected"))

    assert result.payload["sensor_health"]["stm32_link"] == "connected"
    assert result.payload["status_summary"]["comm_status"] == CommStatus.WS_OK.value
