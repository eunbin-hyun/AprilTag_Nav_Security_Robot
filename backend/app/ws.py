"""WebSocket 연결 관리·브로드캐스트.

- /ws/dashboard: 서버가 대시보드로 push. 메시지는 {type, version, robot_id, timestamp, data} 한 겹 메시지로 고정.
- /ws/robot: 로봇이 서버로 접속을 연다(outbound). 서버는 이 목록으로 명령을 밀어낸다.
- ⚠ **두 전선의 내용물 칸은 `data` 하나다**(2026-08-04 사용자 확정 · 프론트 합의). 예전에는
  대시보드만 `payload`라 봉투가 두 벌이었는데, 그 이름을 읽던 EC2 관제 화면이 서버 API를
  안 부르는 게 실측으로 확인돼(최근 30분 0건) 두 이름을 같이 싣는 기간 없이 한 번에 옮겼다.
- 브로드캐스트·명령 발신은 커넥션별 try/except를 걸어 죽은 클라이언트를 그 자리에서 잘라낸다.
- 워커 1개 전제라 커넥션 목록을 인메모리로 든다(워커 2개 이상이면 Redis pub/sub이 필요).
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

from fastapi import WebSocket

from app.config import get_settings


def make_envelope(type_: str, data: dict[str, Any], robot_id: str | None = None) -> dict[str, Any]:
    """WS 공용 메시지. version은 설정값, timestamp는 서버 UTC ISO8601.

    내용물 칸은 **두 전선 모두 `data`다**(2026-08-04 통일). 젯슨 공통 envelope가
    `{type, robot_id, timestamp, seq, data}`라(`hardware/jetson/ros2_ws/docs/
    젯슨_서버_송신_인터페이스_2026-08-01.md` §3) 로봇 전선이 먼저 `data`로 갔고, 대시보드도
    같은 이름으로 맞췄다 — 이름이 갈려 있으면 한 로봇 보고가 두 이름으로 나가서 읽는 쪽이
    전선마다 다른 파서를 든다.
    """
    return {
        "type": type_,
        "version": get_settings().ws_protocol_version,
        "robot_id": robot_id,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
        "data": data,
    }


def make_robot_envelope(
    type_: str, data: dict[str, Any], robot_id: str | None = None
) -> dict[str, Any]:
    """**로봇 전선**(`/ws/robot`)으로 나가는 메시지.

    ⚠ 2026-08-04 통일 뒤로 `make_envelope`와 결과가 **글자 그대로 같다.** 그래도 남겨 둔
    이유는 둘이다. 부르는 자리에서 "이건 로봇으로 나간다"가 이름으로 읽히고, 젯슨 쪽이
    나중에 `seq`를 요구하면(README "로봇 전선 봉투까지 같이 바꾸는 계약 변경") 갈라질 자리가
    여기 하나로 남는다. 새 코드가 굳이 이걸 고를 이유는 없다 — 로봇 전선이면 이걸 써라.
    """
    return make_envelope(type_, data, robot_id)


# WS 한 장을 밀어내는 데 봐주는 최대 시간(초). 넘기면 실패와 같게 끊는다.
#
# ⚠ 없으면 **요청 경로가 화면 하나에 매달린다.** 브로드캐스트는 게이트·경고 API가 커밋 뒤에
# 부르는 자리라(`routers/query.py`), Wi-Fi에서 얼어붙은 화면 하나가 send를 안 끝내면 그
# 요청 응답도 로봇 명령도 같이 선다. 워커가 하나라 더 아프다.
#
# 값을 config.Settings에 안 넣은 이유는 `SHUTTLE_CALL_COOLDOWN_SEC`과 같다 — 이번 사이클에
# 여러 조가 config.py를 같이 만져서 줄을 더하면 텍스트 충돌이 난다.
WS_SEND_TIMEOUT_SEC = 3.0


async def _send_guarded(sock: WebSocket, message: dict[str, Any]) -> None:
    """WS 한 장을 밀어내되 `WS_SEND_TIMEOUT_SEC`을 넘기면 실패와 같게 터뜨린다.

    ⭐ **나가는 자리를 여기 하나로 모은다.** 예전에는 브로드캐스트와 명령 발신만 시간을
    재고 하트비트 ping은 맨 `send_json`이었다 — 얼어붙은 로봇 소켓에 ping을 밀면 그
    커넥션의 하트비트 태스크가 그 자리에서 영영 멈춰서, 스테일을 잡으라고 둔 장치가 정작
    그 스테일에 걸려 아무도 안 끊는다. `robot_count`는 죽은 로봇을 계속 세고 명령은
    그 소켓으로 나간다.
    """
    await asyncio.wait_for(sock.send_json(message), timeout=WS_SEND_TIMEOUT_SEC)


async def _close_quietly(sock: WebSocket, code: int = 1001) -> None:
    """소켓을 닫되 닫기가 실패해도 삼킨다.

    ⭐ **보내기가 실패한 커넥션은 목록에서 빼는 것만으로 안 끝난다.** 목록에서만 빼면
    `/ws/robot`·`/ws/dashboard` 라우트 루프는 계속 `receive`를 돌고, 로봇이 상태를 다시
    올리면 `register`가 **이미 없는 키에** 쓰려다 아무 일도 안 한다(no-op). 그러면 그 로봇은
    붙어 있는데 명령 대상 목록엔 영영 안 들어오는 블랙홀이 된다. 하트비트도 못 잡는다 —
    프레임이 올라올 때마다 `note_activity`가 미스를 0으로 되돌리기 때문이다.

    소켓까지 닫아야 라우트의 `receive`가 터져 `finally`로 빠지고, 로봇이 재접속해서 정상
    등록된다.
    """
    try:
        await sock.close(code=code)
    except Exception:
        # 이미 닫혔거나 닫기를 모르는 객체다. 어느 쪽이든 더 할 게 없다.
        pass


class ConnectionManager:
    def __init__(self) -> None:
        self._dashboard: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._dashboard.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._dashboard.discard(ws)

    @property
    def dashboard_count(self) -> int:
        return len(self._dashboard)

    async def broadcast(self, message: dict[str, Any]) -> None:
        """대시보드 전체에 밀어낸다. 한 커넥션이 죽어도 나머지는 계속 간다."""
        if not self._dashboard:
            return
        targets = list(self._dashboard)

        async def _send(sock: WebSocket) -> None:
            try:
                # 시간 초과도 실패와 같게 다룬다 — 얼어붙은 화면 하나가 요청 경로를
                # 붙잡으면 게이트·경고 응답과 로봇 명령이 같이 밀린다.
                await _send_guarded(sock, message)
            except Exception:
                # 목록에서 빼고 **소켓까지 닫는다** — 안 닫으면 라우트 루프가 계속 돌면서
                # 죽은 화면을 붙잡고 있다(`_close_quietly` 설명).
                self.disconnect(sock)
                await _close_quietly(sock)

        await asyncio.gather(*(_send(s) for s in targets))


class RobotConnectionManager:
    """로봇 outbound WebSocket 커넥션 관리 (아키텍처 2026-07-23 §02).

    로봇이 서버로 먼저 접속을 열어서 교내 Wi-Fi NAT을 뚫을 일이 없다. 서버는 붙어 있는
    커넥션 목록으로 명령을 밀어낸다. 지금은 로봇 1대지만 목록으로 들면 대수가 늘어도 그대로 돈다.

    robot_id는 핸드셰이크가 아니라 첫 RobotState를 받은 뒤에야 알 수 있어서, connect 시점엔
    None으로 두고 register로 나중에 채운다. 아직 자기를 밝히지 않은 커넥션(robot_id=None)에는
    명령을 보내지 않는다 — 누가 받는지 모르는 채로 출동 명령을 내리면 안 되기 때문이다.
    정식 인증(M-6)이 붙기 전까지의 최소 가드다.

    하트비트 미스도 여기서 센다. ping을 보낼 때 미스를 올리고 pong을 받으면 0으로 되돌린다.
    허용치를 넘으면 스테일로 보고 커넥션을 닫아 robot_count에서 뺀다.
    """

    def __init__(self) -> None:
        self._robots: dict[WebSocket, str | None] = {}
        self._misses: dict[WebSocket, int] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._robots[ws] = None
        self._misses[ws] = 0

    def claim_conflict(self, ws: WebSocket, robot_id: str) -> str | None:
        """이 커넥션이 **이미 밝힌 이름과 다른 이름**을 들고 왔나. 그러면 원래 이름을 돌려준다.

        ⭐ 로봇 채널 인증은 공통 키 하나뿐이라(`security.ws_expected_key("robot")`) 키가
        "누구인지"를 증명하지 않는다. 소켓 하나가 프레임마다 robot_id를 갈아 가며 올리면
        ①남의 로봇 카드·에지 상태를 자기 값으로 덮고 ②`register`의 옛 커넥션 밀어내기가
        진짜 로봇 소켓을 끊고 ③그 로봇 앞으로 나가는 명령을 가져간다. 한 커넥션이 한 이름에
        묶이면 최소한 "붙은 채로 갈아타는" 길은 닫힌다.

        ⚠ 여기까지가 **지금 아는 것으로 할 수 있는 전부다.** 처음부터 남의 이름으로 붙는
        갈래는 이 함수로 못 막는다 — 로봇별 자격(토큰)이 있어야 하고 그건 젯슨 배포까지
        걸린 팀 결정이다(M-6).
        """
        current = self._robots.get(ws)
        return current if current is not None and current != robot_id else None

    def register(self, ws: WebSocket, robot_id: str) -> list[WebSocket]:
        """이 커넥션이 자기 robot_id를 밝혔다. **같은 ID를 든 옛 커넥션은 밀어낸다.**

        밀어낸 소켓 목록을 돌려준다 — 부르는 쪽이 `drop_stale`로 닫아 줘야 그 라우트 루프가
        `finally`로 빠진다(`robot_channel.handle_robot_frame`이 그 자리다).

        ⚠ 목록이 `dict[WebSocket, robot_id]`라 같은 ID가 두 줄로 남을 수 있다. 로봇이
        재접속하는 동안 옛 소켓이 아직 안 걷혔으면 `send_command`의 대상 고르기가 **둘 다**
        골라서, 로봇 한 대가 같은 명령을 두 장 받고 `delivered_ids` 길이도 2가 된다 —
        통지 수가 부풀고 CommandLink도 두 벌 쌓인다. 여기서 옛 줄을 바로 빼는 게 그 뿌리다.
        """
        if ws not in self._robots:
            return []
        self._robots[ws] = robot_id
        stale = [s for s, rid in self._robots.items() if rid == robot_id and s is not ws]
        for sock in stale:
            self.disconnect(sock)
        return stale

    def disconnect(self, ws: WebSocket) -> None:
        self._robots.pop(ws, None)
        self._misses.pop(ws, None)

    @property
    def robot_count(self) -> int:
        return len(self._robots)

    def robot_ids(self) -> list[str]:
        """아직 자기를 밝히지 않은 커넥션은 뺀다."""
        return [rid for rid in self._robots.values() if rid]

    def robot_id_of(self, ws: WebSocket) -> str | None:
        """이 커넥션이 밝힌 robot_id(아직 안 밝혔으면 None).

        연결이 끊길 때 어느 로봇의 인메모리 상태를 버릴지 고르려면 disconnect 앞에서
        이걸 물어봐야 한다.
        """
        return self._robots.get(ws)

    # ── 하트비트 미스 판정 ────────────────────────────────────────────────

    def note_activity(self, ws: WebSocket) -> None:
        """이 커넥션에서 뭐가 하나 올라왔다 — 미스 카운터를 되돌린다.

        ⚠ 살아 있다는 증거는 pong만이 아니다. 상태 프레임이 초당 몇 장씩 올라오는 커넥션은
        pong을 한 번도 안 보내도 분명히 살아 있다. 젯슨 브리지(`web_bridge_node.WebSocketClient`)가
        딱 그 모양이라 — 소켓을 **읽지 않아서** ping을 보고도 답을 못 한다 — pong만 세면
        15초 × 미스 2번마다 `drop_stale`이 1001로 끊고 재접속 루프가 돈다. 그러면 어댑터가
        프레임을 받아 준다는 이 작업의 목표가 45초 창에서만 성립한다(실측 결함 1).
        """
        if ws in self._misses:
            self._misses[ws] = 0

    def note_pong(self, ws: WebSocket) -> None:
        """로봇이 하트비트에 답을 보내왔다. `note_activity`와 같은 자리를 되돌린다."""
        self.note_activity(ws)

    def record_ping(self, ws: WebSocket) -> int:
        """ping 한 번을 미응답으로 세고 지금까지 쌓인 미스 수를 돌려준다."""
        self._misses[ws] = self._misses.get(ws, 0) + 1
        return self._misses[ws]

    async def drop_stale(self, ws: WebSocket) -> None:
        """응답이 끊긴 커넥션을 목록에서 빼고 닫는다(robot_count에서도 빠진다)."""
        self.disconnect(ws)
        await _close_quietly(ws)

    async def send_command(
        self, command: dict[str, Any], robot_id: str | None = None
    ) -> list[str]:
        """명령 메시지를 로봇으로 밀어내고 실제로 받은 robot_id 목록을 돌려준다.

        robot_id를 주면 그 로봇만, 없으면 자기를 밝힌 로봇 전부. 빈 목록이면 명령이 안
        갔다는 뜻이라 호출자가 알림·기록에 반영해야 한다. 미식별 커넥션은 대상에서 뺀다.

        ⚠ 보내기가 실패하면 목록에서 빼는 데서 그치지 않고 `drop_stale`로 **소켓까지 닫는다.**
        목록에서만 빼면 그 로봇이 상태를 계속 올려도 `register`가 no-op이라 명령을 영영 못
        받는 블랙홀이 된다(`_close_quietly` 설명).
        """
        targets = [
            (ws, rid)
            for ws, rid in self._robots.items()
            if rid and (robot_id is None or rid == robot_id)
        ]
        delivered: list[str] = []
        for ws, rid in targets:
            envelope = make_robot_envelope("command", command, robot_id=rid)
            try:
                # 얼어붙은 로봇 소켓 하나가 셔틀 인입 요청을 통째로 붙잡지 않게 한다
                # (`WS_SEND_TIMEOUT_SEC` 설명). 시간 초과는 배달 실패와 같게 친다.
                await _send_guarded(ws, envelope)
                delivered.append(rid)
            except Exception:
                await self.drop_stale(ws)
        return delivered


async def robot_heartbeat(ws: WebSocket, interval_sec: float, max_miss: int = 2) -> None:
    """서버→로봇 하트비트(계약 10~20초).

    pong이 max_miss번 연속 안 오면 그 커넥션을 스테일로 보고 닫는다 — 소켓이 끊긴 걸
    OS가 알려주지 않는 경우(전원 차단·Wi-Fi 이탈)에도 robot_count가 실제와 맞아야 한다.

    ⚠ 연결이 끝나면 호출한 쪽이 finally에서 이 태스크를 반드시 취소해야 한다. 안 하면
    죽은 소켓에 계속 send를 시도하는 태스크가 남는다(FastAPI WS 흔한 누수 1순위).
    """
    while True:
        await asyncio.sleep(interval_sec)
        if robot_manager.record_ping(ws) > max_miss:
            await robot_manager.drop_stale(ws)
            return
        try:
            # ⚠ 맨 `send_json`이면 안 된다. 얼어붙은 소켓에서는 이 await가 안 끝나 하트비트
            # 태스크가 그대로 서고, 그 커넥션은 미스가 더 안 쌓여 영영 스테일로 안 잡힌다.
            await _send_guarded(ws, make_robot_envelope("ping", {}))
        except Exception:
            # 이미 죽은 소켓이다. 목록에서 빼고 태스크를 끝낸다.
            await robot_manager.drop_stale(ws)
            return


manager = ConnectionManager()
robot_manager = RobotConnectionManager()
