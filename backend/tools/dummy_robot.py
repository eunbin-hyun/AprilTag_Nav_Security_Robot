"""더미 로봇 클라이언트 (지라 S15P11C207-156).

실로봇 없이 `/ws/robot` 경로를 검증하는 CLI다. 로봇처럼 서버로 접속을 열고 RobotState를
올리며, 서버가 내려보내는 명령과 하트비트를 받아 ACK·pong으로 답한다.

프레임은 {type, version, robot_id, timestamp, <본문>} 다섯 칸이다. **로봇 전선의 본문 칸은
`data`다**(젯슨 공통 envelope에 맞춘 2026-08-01 확정 — 대시보드도 2026-08-04부터 같은 이름이다).
- 올리는 것: robot_state · command_ack · pong
- 받는 것: registered · state_ack · command · ping · error

`robot_state`까지 포함해 이 전선의 모든 프레임이 `data` 한 이름을 쓴다(2026-08-01 저녁 확정).

사용 예:
  # 상태를 1초마다 3번 올리고 5초 더 붙어 명령을 기다린다
  python -m tools.dummy_robot --states 3 --interval 1 --linger 5

  # 게이트 도착을 보고한다(대시보드에 도착 알림이 뜨고 ETA 구간 기록이 생긴다)
  python -m tools.dummy_robot --states 1 --mission ARRIVED

  # 잘못된 상태를 올려 서버 검증 응답(error 메시지)을 본다
  python -m tools.dummy_robot --invalid
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
from typing import Any

import websockets

PROTOCOL_VERSION = "1"
# 로봇 전선의 본문 칸 이름. 서버 `app/ws.make_robot_envelope`와 같은 값이어야 한다.
WIRE_BODY_KEY = "data"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def envelope(
    type_: str, body: dict[str, Any], robot_id: str, body_key: str = WIRE_BODY_KEY
) -> str:
    return json.dumps(
        {
            "type": type_,
            "version": PROTOCOL_VERSION,
            "robot_id": robot_id,
            "timestamp": now_iso(),
            body_key: body,
        }
    )


def build_state(args: argparse.Namespace, seq: int, last: bool) -> dict[str, Any]:
    """RobotState 한 건. mission_status는 기본이 마지막 한 번이고, --mission-repeat면 매번 싣는다.

    실로봇은 절대 상태를 주기로 올리니 도착 뒤 보고마다 ARRIVED가 계속 실린다. 그 모습을
    재현해 서버가 도착을 한 번만 치는지 보려면 --mission-repeat를 쓴다.
    """
    payload: dict[str, Any] = {
        "robot_id": args.robot_id,
        "status_summary": {
            # 배터리가 조금씩 줄어드는 모습을 보여 화면 갱신을 눈으로 확인할 수 있게 한다.
            "battery": max(0, args.battery - seq),
            "mode": args.mode,
            "comm_status": "WS_OK",
            "position": {"x": 1.0 + seq * 0.5, "y": 2.0, "theta": 0.0},
            "led_status": args.led,
        },
    }
    if args.telemetry:
        payload["odom"] = {"x": 1.0 + seq * 0.5, "y": 2.0, "linear_x": 0.3}
        payload["imu"] = {"yaw": 0.01 * seq, "pitch": 0.0, "roll": 0.0}
        payload["sensor_health"] = {"tof_a": "ok", "tof_b": "ok", "rfid": "ok"}
        payload["log"] = f"더미 로봇 상태 보고 {seq + 1}회"
    if args.mission and (last or args.mission_repeat):
        payload["mission_status"] = args.mission
    if args.invalid:
        # 서버 검증(battery 0~100)을 일부러 벗어나게 만든다.
        payload["status_summary"]["battery"] = 500
    return payload


class Counters:
    def __init__(self) -> None:
        self.state_ack = 0
        self.command = 0
        self.error = 0
        self.ping = 0


async def _reader(ws, robot_id: str, counters: Counters) -> None:
    """서버 프레임을 받아 출력하고, 명령·하트비트에 답한다."""
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[recv 비JSON] {raw!r}")
            continue
        print(f"[recv] {json.dumps(msg, ensure_ascii=False)}")
        mtype = msg.get("type")
        # 서버가 내려보내는 봉투의 본문 칸도 `data`다(두 전선이 같은 이름이다).
        body = msg.get(WIRE_BODY_KEY) or {}
        if mtype == "state_ack":
            counters.state_ack += 1
        elif mtype == "command":
            counters.command += 1
            await ws.send(
                envelope(
                    "command_ack",
                    {"command_id": body.get("command_id"), "accepted": True},
                    robot_id,
                )
            )
            print(f"  [ack 발신] command_id={body.get('command_id')}")
        elif mtype == "ping":
            counters.ping += 1
            await ws.send(envelope("pong", {}, robot_id))
        elif mtype == "error":
            counters.error += 1


async def run(args: argparse.Namespace) -> int:
    counters = Counters()
    sent = 0
    async with websockets.connect(args.url) as ws:
        print(f"[connected] {args.url} robot_id={args.robot_id}")
        reader = asyncio.create_task(_reader(ws, args.robot_id, counters))
        try:
            for seq in range(args.states):
                payload = build_state(args, seq, last=(seq == args.states - 1))
                await ws.send(
                    envelope("robot_state", payload, args.robot_id)
                )
                sent += 1
                print(f"[send] robot_state {seq + 1}/{args.states}")
                if seq < args.states - 1:
                    await asyncio.sleep(args.interval)
            # 마지막 응답과 서버 명령을 받을 시간을 준다.
            await asyncio.sleep(args.linger)
        finally:
            # ⚠ 태스크를 안 취소하면 소켓이 닫힌 뒤에도 남는다.
            reader.cancel()

    print(
        f"\n결과: 상태 {sent}건 발신 · state_ack {counters.state_ack}건 · "
        f"명령 {counters.command}건 · error {counters.error}건 · ping {counters.ping}건"
    )
    if args.invalid:
        # 잘못된 상태는 error 메시지가 와야 성공이다.
        return 0 if counters.error >= 1 else 1
    return 0 if counters.state_ack >= sent else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C207 더미 로봇 클라이언트 (outbound WS)")
    p.add_argument("--url", default="ws://127.0.0.1:8000/ws/robot")
    p.add_argument("--robot-id", default="jetson01")
    p.add_argument("--states", type=int, default=1, help="올릴 RobotState 건수")
    p.add_argument("--interval", type=float, default=1.0, help="상태 보고 간격(초)")
    p.add_argument("--linger", type=float, default=2.0, help="마지막 상태 뒤 더 붙어 있는 시간(초)")
    p.add_argument("--battery", type=int, default=88)
    p.add_argument(
        "--mode",
        default="INDOOR_TAGGING",
        choices=["INDOOR_TAGGING", "OUTDOOR_TAGGING", "CHARGING_STATION", "MANUAL"],
    )
    p.add_argument("--led", default="GREEN")
    p.add_argument(
        "--mission",
        default=None,
        choices=["ARRIVED", "WEATHER_BLOCKED", "RETURN_COMPLETE"],
        help="마지막 상태에 실을 mission_status",
    )
    p.add_argument(
        "--mission-repeat",
        action="store_true",
        help="mission_status를 매 보고에 싣는다(주기 보고 재현 — 서버는 전이 한 번만 알림해야 한다)",
    )
    p.add_argument("--telemetry", action="store_true", help="odom·imu·sensor_health·log까지 싣는다")
    p.add_argument("--invalid", action="store_true", help="검증에 걸리는 상태를 한 번 올린다")
    args = p.parse_args(argv)
    if args.invalid:
        args.states = 1
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
