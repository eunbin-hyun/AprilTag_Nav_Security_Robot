"""WS envelope 계약 — **두 전선 모두 내용물 칸이 `data`다** (2026-08-04 사용자 확정).

젯슨 공통 envelope가 `{type, robot_id, timestamp, seq, data}`라 로봇 전선(`/ws/robot`)이
먼저 `data`로 갔고(`hardware/jetson/ros2_ws/docs/젯슨_서버_송신_인터페이스_2026-08-01.md`
§3, §8-4), 2026-08-04에 대시보드 전선(`/ws/dashboard`)도 같은 이름으로 맞췄다.

예전에는 대시보드만 `payload`라 **한 로봇 보고가 두 이름으로 갈라져 나갔다**. 그래서 이
파일이 지키는 건 이제 둘이다.

1. 두 전선의 봉투가 칸까지 **같다** — 갈라지면 읽는 쪽이 전선마다 다른 파서를 들어야 한다.
2. 예전 이름 `payload`가 **어느 전선에서도 안 나간다** — 이름을 바꾸는 변경이라
   "새 이름이 있다"만 보면 두 이름을 같이 싣는 회귀가 그대로 통과한다.
"""
import asyncio
import datetime as dt
import functools
import importlib.util
import logging
import pathlib
from typing import Any

import pytest
import pytest_asyncio

from app.robot_channel import CommandLink, handle_robot_frame, register_command_link
from app.ws import (
    make_envelope,
    make_robot_envelope,
    manager,
    robot_heartbeat,
    robot_manager,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 봉투 다섯 칸. 두 전선이 같은 값을 쓰지만 이름을 둘로 둔다 — 시험 문장에서 어느 전선을
# 보고 있는지가 읽히고, 나중에 한쪽만 갈라질 때 고칠 자리가 이미 나뉘어 있다.
ROBOT_ENVELOPE_KEYS = {"type", "version", "robot_id", "timestamp", "data"}
DASHBOARD_ENVELOPE_KEYS = {"type", "version", "robot_id", "timestamp", "data"}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# 젯슨 브리지가 프레임을 만드는 파일. **실물을 그대로 불러 쓴다** — 값을 베껴 적으면 젯슨이
# 모양을 바꾼 날 이 시험만 옛 모양을 지킨다(전체검토 L42).
_JETSON_MESSAGES_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "hardware" / "jetson" / "ros2_ws" / "src" / "web_bridge" / "web_bridge" / "messages.py"
)


@functools.lru_cache(maxsize=1)
def _jetson_messages():
    """`hardware/.../web_bridge/messages.py`를 ROS 없이 그대로 들인다.

    그 파일은 stdlib(`datetime`·`json`)만 쓰므로 패키지로 안 세워도 파일 하나로 돈다.
    `sys.path`에 안 얹는 이유는 `web_bridge` 이름이 백엔드 import 공간에 새면 안 되기 때문이다.
    """
    if not _JETSON_MESSAGES_PATH.exists():
        pytest.skip(f"젯슨 생성기를 못 찾았다: {_JETSON_MESSAGES_PATH}")
    spec = importlib.util.spec_from_file_location(
        "c207_jetson_messages_under_test", _JETSON_MESSAGES_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeSocket:
    """매니저에 직접 꽂는 가짜 WebSocket. 받은 메시지만 모아 둔다."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    async def close(self, code: int = 1000) -> None:
        return None

    def types(self) -> list[str]:
        return [m.get("type") for m in self.sent]

    def last(self, type_: str) -> dict[str, Any] | None:
        found = [m for m in self.sent if m.get("type") == type_]
        return found[-1] if found else None


@pytest_asyncio.fixture(loop_scope="session")
async def dash():
    sock = FakeSocket()
    await manager.connect(sock)
    try:
        yield sock
    finally:
        manager.disconnect(sock)


@pytest_asyncio.fixture(loop_scope="session")
async def robot():
    sock = FakeSocket()
    await robot_manager.connect(sock)
    try:
        yield sock
    finally:
        robot_manager.disconnect(sock)


# ── 봉투 두 벌 ─────────────────────────────────────────────────────────────

async def test_봉투는_두_전선_모두_data다():
    """같은 값을 두 전선에 실으면 봉투가 칸까지 똑같이 나온다."""
    body = {"reason": "frame_not_object"}
    to_robot = make_robot_envelope("error", body, robot_id="RB1-01")
    to_dash = make_envelope("error", body, robot_id="RB1-01")

    assert set(to_robot) == ROBOT_ENVELOPE_KEYS
    assert set(to_dash) == DASHBOARD_ENVELOPE_KEYS
    assert to_robot["data"] == body
    assert to_dash["data"] == body
    # 예전 이름이 같이 실려 나가는 회귀를 여기서 막는다("새 이름이 있다"만 보면 안 잡힌다).
    assert "payload" not in to_robot
    assert "payload" not in to_dash
    # 나머지 넷도 같은 규칙이어야 한다 — 갈라지면 두 전선이 조용히 다른 계약이 된다.
    assert to_robot["version"] == to_dash["version"]
    assert to_robot["robot_id"] == to_dash["robot_id"] == "RB1-01"


# ── 서버 → 로봇 송신 ───────────────────────────────────────────────────────

async def test_명령은_data로_나간다(robot):
    robot_manager.register(robot, "RB1-01")
    command = {"command_id": "shuttle-7", "type": "cmd_destination",
               "cmd_destination": "OUTDOOR_TAGGING"}

    delivered = await robot_manager.send_command(command)

    assert delivered == ["RB1-01"]
    sent = robot.last("command")
    assert sent is not None, robot.types()
    assert set(sent) == ROBOT_ENVELOPE_KEYS, sent
    assert sent["data"] == command


async def test_하트비트_ping은_data로_나간다(robot):
    task = asyncio.create_task(robot_heartbeat(robot, 0.01, max_miss=50))
    try:
        for _ in range(30):
            await asyncio.sleep(0.01)
            if robot.last("ping") is not None:
                break
    finally:
        task.cancel()

    ping = robot.last("ping")
    assert ping is not None, robot.types()
    assert set(ping) == ROBOT_ENVELOPE_KEYS, ping
    assert ping["data"] == {}


@pytest.mark.parametrize(
    ("frame", "reason"),
    [
        (42, "frame_not_object"),
        ({"type": "no_such_type", "data": {}}, "unknown_frame_type"),
        ({"type": "robot_state", "data": {"status_summary": {}}}, "invalid_robot_state"),
    ],
)
async def test_error_응답은_data로_나간다(robot, frame, reason):
    """잘못된 프레임 세 갈래 모두 봉투가 `data`여야 한다(한 갈래만 고친 회귀 방지)."""
    await handle_robot_frame(robot, frame)

    err = robot.last("error")
    assert err is not None, robot.types()
    assert set(err) == ROBOT_ENVELOPE_KEYS, err
    assert err["data"]["reason"] == reason


async def test_state_ack과_대시보드_중계가_같은_이름으로_나간다(dash, robot):
    """한 프레임이 두 전선으로 갈라지는 자리 — 같은 케이스에서 둘을 같이 본다."""
    await handle_robot_frame(
        robot,
        {
            "type": "robot_state",
            "version": "1",
            "robot_id": "RB1-01",
            "timestamp": _now(),
            "data": {"robot_id": "RB1-01", "status_summary": {"battery": 77}},
        },
    )

    ack = robot.last("state_ack")
    assert ack is not None, robot.types()
    assert set(ack) == ROBOT_ENVELOPE_KEYS, ack
    assert ack["data"]["robot_pk"] > 0

    # 대시보드로 가는 중계 둘도 같은 이름이다 — 한 보고가 두 이름으로 갈라지지 않는다.
    for type_ in ("robot_state", "robot_status"):
        relayed = dash.last(type_)
        assert relayed is not None, dash.types()
        assert set(relayed) == DASHBOARD_ENVELOPE_KEYS, relayed
        assert "payload" not in relayed, relayed
    assert dash.last("robot_status")["data"]["battery"] == 77


# ── 로봇 → 서버 수신 ───────────────────────────────────────────────────────

async def test_command_ack은_data에서_읽는다(robot, caplog):
    """ACK 본문을 `data`에서 읽는지 로그로 본다.

    "답을 안 보낸다"만 보면 `payload`를 읽는 예전 코드도 그대로 통과한다 — 실제로 읽은
    값까지 봐야 계약이 못박힌다.
    """
    with caplog.at_level(logging.INFO, logger="c207.robot"):
        await handle_robot_frame(
            robot,
            {
                "type": "command_ack",
                "version": "1",
                "robot_id": "RB1-01",
                "timestamp": _now(),
                "data": {"command_id": "shuttle-9", "accepted": True},
            },
        )

    assert robot.sent == [], robot.types()   # ACK에는 답을 안 보낸다
    assert "shuttle-9" in caplog.text, caplog.text


async def test_젯슨_생성기가_만든_ACK를_그대로_먹인다(robot, caplog):
    """⭐ **실기기가 진짜로 만드는 프레임**을 손으로 안 짓고 그대로 먹인다 (전체검토 L42).

    바로 위 케이스는 `{"type","version","robot_id","timestamp","data"}` 다섯 칸을 손으로
    조립한다. 그런데 젯슨 브리지는 그 봉투를 **안 씌운다** — `web_bridge_node`가
    `messages.command_ack_message(...)`가 돌려준 `{"type","data"}` 두 칸을 그대로
    `json.dumps` 해서 올린다(`hardware/.../web_bridge_node.py`의 `_enqueue` 자리).

    즉 위 케이스가 재는 모양은 전선에 한 번도 안 흐른다. 그래서 서버가 `version`이나
    `robot_id`를 필수로 읽기 시작해도 위 케이스는 초록이고 실기기만 조용히 막힌다 —
    "시험 72건 전부 통과인데도 D2가 살아 있던" 자리와 같은 계열이다.

    ⚠ **가짜를 만들지 말고 그 파일을 불러 쓴다.** 값을 여기 베껴 적으면 젯슨이 모양을 바꾼
    날 이 시험만 옛 모양을 지킨다(`tests/test_jetson_adapter.py`가 "젯슨 messages.py와 같은
    모양"이라고 주석으로만 적어 둔 자리가 그렇다). `messages.py`는 stdlib만 쓰므로 ROS 없이
    그대로 import 된다.

    판정은 "예외가 안 났다"가 아니다. **서버가 그 `command_id`를 실제로 읽어서 자기가 낸
    명령과 대조했는지**까지 본다 — 모르는 이름이면 경고 갈래로 갈리므로 그 갈림이 곧 답이다.
    """
    from app.robot_channel import CommandLink, register_command_link

    # ⭐ **젯슨이 2026-08-06에 결과와 사유를 실기 시작했다**(회신 §1 · `accepted`·`reason`).
    #    그 전에는 인자가 `command_id` 하나였고 봉투도 `type`·`data` 둘뿐이었다.
    #    이 시험이 그 변화를 잡아 줬다 — 시그니처가 바뀌자 곧장 빨개졌다.
    ack = _jetson_messages().command_ack_message("shuttle-9", True, None)

    # 봉투 칸을 못박는다. 서버가 안 읽는 칸이 늘어도 그 사실은 여기서 드러나야 한다.
    assert set(ack) == {"type", "version", "data"}, f"젯슨 ACK 모양이 바뀌었다: {ack}"
    assert ack["data"] == {
        "command_id": "shuttle-9",
        # ⚠ 수락이면 `accepted`다. 거절(`rejected`)일 때 서버가 그 사실을 화면까지
        #    흘리는 것은 §27-3에서 고쳤고 딴 시험이 잰다 — 여기 판정은 `command_id` 대조다.
        "result": "accepted",
        "reason": None,
    }

    # 서버가 낸 명령으로 등록해 둔다. 이게 있어야 "읽었다"와 "못 읽었다"가 로그로 갈린다.
    robot_manager.register(robot, "RB1-01")
    register_command_link(
        "RB1-01",
        CommandLink(
            command_id="shuttle-9",
            arrival_id=1,
            gate_no=3,
            issued_at=dt.datetime.now(dt.timezone.utc),
        ),
    )

    with caplog.at_level(logging.INFO, logger="c207.robot"):
        await handle_robot_frame(robot, ack)

    assert robot.sent == [], f"실기기 ACK에 답을 보냈다 — {robot.types()}"
    assert "로봇 명령 ACK" in caplog.text, (
        f"서버가 실기기 ACK의 command_id를 자기 명령과 대조하지 못했다: {caplog.text}"
    )
    assert "모르는 command_id" not in caplog.text, caplog.text
    assert "command_id가 없다" not in caplog.text, (
        "`data`가 아닌 자리에서 읽고 있다 — 실기기 ACK가 통째로 버려진다"
    )


async def test_pong은_data_틀로_와도_미스를_되돌린다(robot):
    """하트비트 응답은 본문을 안 읽지만, 계약대로 `data`를 달고 와도 살아 있다는 증거다."""
    assert robot_manager.record_ping(robot) == 1

    await handle_robot_frame(
        robot,
        {
            "type": "pong",
            "version": "1",
            "robot_id": "RB1-01",
            "timestamp": _now(),
            "data": {},
        },
    )

    assert robot.sent == [], robot.types()          # pong에는 error도 답도 없다
    assert robot_manager.record_ping(robot) == 1    # 미스가 0으로 되돌아갔다


# ── 태그 검출값 중계 (2026-08-05) ───────────────────────────────────────────
#
# ⛔ **이 갈래가 없던 동안 서버는 초당 다섯 번 `unknown_frame_type`을 되돌려 보냈다.**
# 젯슨은 `aee8ce4`(2026-08-04)부터 `/tag/target`을 5Hz로 올리고 있었는데 서버
# `JETSON_FRAME_TYPES`에 그 이름이 없어서 통째로 튕겼다. 화면은 로봇을 평면도에 **절대
# 좌표로** 못 그렸다 — 로봇 좌표는 켠 자리가 원점이라 껐다 켜면 어긋나고, 태그는 자리가
# 고정이라 기준점이 된다.
#
# ⭐ 아래 케이스는 **젯슨 실물 생성기**(`_jetson_messages`)가 만든 봉투를 그대로 태운다.
# 서버가 자기 상상으로 만든 dict를 재면 젯슨이 이름을 바꾸는 날 조용히 갈라진다.


async def test_tag_target이_대시보드로_흘러간다(dash, robot):
    """⭐ 젯슨이 만든 봉투를 서버가 받아 화면으로 중계한다."""
    jetson = _jetson_messages()
    frame = jetson.tag_target_message(
        "ROBOT-1",
        7,
        {"tag_id": 3, "along": 0.82, "cross_track": -0.04, "heading_error": 0.11},
    )

    await handle_robot_frame(robot, frame)

    assert "error" not in robot.types(), f"서버가 에러를 돌려보냈다: {robot.sent}"
    relayed = dash.last("tag_target")
    assert relayed is not None, "화면으로 한 장도 안 흘렀다"
    assert relayed["data"]["tag_id"] == 3
    assert relayed["data"]["along"] == pytest.approx(0.82)
    assert relayed["data"]["valid"] is True


async def test_검출_실패도_그대로_흘린다(dash, robot):
    """⚠ `valid=False`는 **버리지 않는다.** 화면이 "태그를 놓쳤다"를 그리는 근거다."""
    jetson = _jetson_messages()
    frame = jetson.tag_target_message(
        "ROBOT-1", 8, {"tag_id": 3}, valid=False, reason="no_detection"
    )

    await handle_robot_frame(robot, frame)

    relayed = dash.last("tag_target")
    assert relayed is not None, "검출 실패 보고를 버렸다"
    assert relayed["data"]["valid"] is False
    assert relayed["data"]["reason"] == "no_detection"
    assert relayed["data"]["along"] is None


async def test_tag_target은_unknown_frame_type이_아니다(robot):
    """회귀 못 — 이 이름이 갈래에서 빠지면 다시 초당 다섯 번 에러가 나간다."""
    jetson = _jetson_messages()
    frame = jetson.tag_target_message("ROBOT-1", 9, {"tag_id": 1, "along": 1.0})

    await handle_robot_frame(robot, frame)

    unknown = [
        m for m in robot.sent
        if m.get("type") == "error" and (m.get("data") or {}).get("reason") == "unknown_frame_type"
    ]
    assert not unknown, "tag_target이 아직 모르는 봉투로 튕긴다"


async def test_칸_이름_오타는_거절한다(robot):
    """⚠ 젯슨이 칸 이름을 바꾸면 **조용히 흘리지 말고 에러로 말한다.**

    `extra="forbid"`가 없으면 오타가 그대로 통과해 화면이 늘 빈 값을 그리고, 그 원인을
    아무 데서도 못 찾는다(2026-08-04 전체검토 L12와 같은 결).
    """
    await handle_robot_frame(
        robot,
        {"type": "tag_target", "robot_id": "ROBOT-1", "seq": 10,
         "data": {"tag_id": 3, "alng": 0.82}},
    )

    err = robot.last("error")
    assert err is not None, "오타를 그대로 통과시켰다"
    assert err["data"]["reason"] == "invalid_tag_target"


def _link(command_id: str) -> CommandLink:
    """서버가 낸 명령 하나를 기억시키는 값. 짝짓기 근거라 arrival_id 는 아무 값이면 된다."""
    return CommandLink(
        command_id=command_id, arrival_id=1, gate_no=1,
        issued_at=dt.datetime.now(dt.timezone.utc),
    )


# ── command_ack 결과 칸 (2026-08-05 젯슨 §6-4) ─────────────────────────────
#
# 예전 계약은 `{"command_id": ...}` 하나뿐이라 **젯슨이 명령을 거절해도 알릴 방법이
# 없었다.** 서버는 출발한 줄 알고 계속 기다리고 화면은 5초 창을 띄운 채 굳는다.


async def test_거절_ACK가_화면까지_흘러간다(robot, dash):
    """⭐ 본체 — `result=rejected`면 대시보드로 새 봉투가 나간다."""
    robot_manager.register(robot, "RB1-01")
    command = {"command_id": "shuttle-9", "type": "cmd_destination",
               "cmd_destination": "OUTDOOR_TAGGING"}
    await robot_manager.send_command(command)
    # ⚠ 서버가 낸 명령으로 기억시킨다 — 모르는 command_id 는 앞 갈래에서 걸러진다.
    register_command_link("RB1-01", _link("shuttle-9"))

    await handle_robot_frame(robot, {
        "type": "command_ack", "version": "1",
        "data": {"command_id": "shuttle-9", "result": "rejected",
                 "reason": "이미 주행 중이다"},
    })

    sent = dash.last("robot_command_rejected")
    assert sent is not None, f"거절이 화면으로 안 갔다 — {dash.types()}"
    assert sent["data"]["command_id"] == "shuttle-9"
    assert sent["data"]["reason"] == "이미 주행 중이다"
    assert sent["robot_id"] == "RB1-01"


async def test_수락_ACK도_화면까지_흘러간다(robot, dash):
    """⭐ **2026-08-07에 뒤집혔다**(사용자 확정). 예전 이름은 `…화면을_안_건드린다`였다.

    ⛔ 그때는 "받아들인 명령까지 흘리면 소음만 는다"로 보고 거절만 보냈다. 뒤집은 근거는
    하나다 — **"거절이 안 왔으니 닿았겠지"는 안 닿은 것과 구분이 안 된다.** 명령이 로봇에
    실제로 닿았는지를 화면이 알 길이 없어서 명령 칩이 "접수"에서 멈춰 있었다.

    ⚠ 되돌리고 싶어지면 **소음이 실제로 얼마나 되는지부터 재라.** 그 판단으로 한 번
    닫았다가 화면이 못 그리는 단계가 생겼다.
    """
    robot_manager.register(robot, "RB1-01")
    await robot_manager.send_command({"command_id": "shuttle-10", "type": "cmd_destination"})
    register_command_link("RB1-01", _link("shuttle-10"))

    await handle_robot_frame(robot, {
        "type": "command_ack", "version": "1",
        "data": {"command_id": "shuttle-10", "result": "accepted"},
    })

    sent = dash.last("robot_command_accepted")
    assert sent is not None, f"수락이 화면으로 안 갔다 — {dash.types()}"
    assert sent["data"]["command_id"] == "shuttle-10"
    assert sent["robot_id"] == "RB1-01"
    # ⚠ 거절 봉투로 새면 화면이 정상 명령을 실패로 그린다. 둘은 끝까지 갈려 있어야 한다.
    assert dash.last("robot_command_rejected") is None, dash.types()


async def test_result가_없는_구판_ACK도_안_깨진다(robot, dash):
    """⚠ 젯슨 구판이 그대로 붙어도 예전처럼 로그만 남고 끝난다."""
    robot_manager.register(robot, "RB1-01")
    await robot_manager.send_command({"command_id": "shuttle-11", "type": "cmd_destination"})
    register_command_link("RB1-01", _link("shuttle-11"))

    await handle_robot_frame(robot, {
        "type": "command_ack", "version": "1",
        "data": {"command_id": "shuttle-11"},
    })

    assert dash.last("robot_command_rejected") is None, dash.types()
