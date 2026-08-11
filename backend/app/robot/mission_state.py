"""[초안·팀 확정 대기] 안건③ mission_status에 EN_ROUTE 넣기.

지금 enum은 ARRIVED·WEATHER_BLOCKED·RETURN_COMPLETE 셋뿐이라 로봇이 "이동 중"을
말할 방법이 없다. 그래서 로봇이 명령 없이 스스로 다시 출동해 또 도착하면 서버가
보는 값은 ARRIVED 다음에 또 ARRIVED다. 도착을 에지(값이 바뀐 순간)로 잡으니
두 번째 도착이 통째로 사라진다 — 연속 재출동 감지 불가의 뿌리다.

dev 트리는 ARRIVAL_EDGE_ON_RECOMMAND 플래그로 우회했는데, 그건 "같은 값이 또 와도
도착으로 친다"라서 하트비트 재전송·재접속 리플레이까지 도착으로 세는 부작용이 있다.
정공법은 사이에 EN_ROUTE를 넣어 에지를 되살리는 거다.

    ARRIVED → EN_ROUTE → ARRIVED   (에지 두 번, 재출동 도착이 살아난다)

설계 결정 두 가지(회의에서 뒤집을 수 있음).
- 낯선 상태값은 무시하고 기존 상태를 유지한다. 로봇 펌웨어가 먼저 올라가도 서버가
  안 죽는다. 플래그가 꺼져 있으면 EN_ROUTE도 이 길로 빠진다.
- 계약에 없는 전이(예: RETURN_COMPLETE → ARRIVED)는 unexpected로 표시만 하고
  상태는 그대로 받아 적는다. 상태를 버리면 로봇이랑 서버 그림이 어긋나서 더 위험하다.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


class MissionStatus:
    EN_ROUTE = "EN_ROUTE"              # [초안·팀 확정 대기] 안건③으로 새로 넣는 값
    ARRIVED = "ARRIVED"
    WEATHER_BLOCKED = "WEATHER_BLOCKED"
    RETURN_COMPLETE = "RETURN_COMPLETE"


LEGACY_STATUSES = frozenset(
    {MissionStatus.ARRIVED, MissionStatus.WEATHER_BLOCKED, MissionStatus.RETURN_COMPLETE}
)
DRAFT_STATUSES = LEGACY_STATUSES | {MissionStatus.EN_ROUTE}

# 전이 계약 초안. 키가 이전 상태, 값이 그 다음에 와도 자연스러운 상태다.
# 같은 값 재전송(하트비트·재접속 리플레이)은 어느 상태든 허용한다.
ALLOWED_TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: DRAFT_STATUSES,  # 첫 보고는 아무 값이나 올 수 있다
    MissionStatus.EN_ROUTE: frozenset(
        {MissionStatus.EN_ROUTE, MissionStatus.ARRIVED, MissionStatus.WEATHER_BLOCKED}
    ),
    MissionStatus.ARRIVED: frozenset(
        {MissionStatus.ARRIVED, MissionStatus.EN_ROUTE, MissionStatus.RETURN_COMPLETE,
         MissionStatus.WEATHER_BLOCKED}
    ),
    MissionStatus.WEATHER_BLOCKED: frozenset(
        {MissionStatus.WEATHER_BLOCKED, MissionStatus.EN_ROUTE, MissionStatus.RETURN_COMPLETE}
    ),
    MissionStatus.RETURN_COMPLETE: frozenset(
        {MissionStatus.RETURN_COMPLETE, MissionStatus.EN_ROUTE}
    ),
}


def known_statuses() -> frozenset[str]:
    """지금 받아들이는 상태값. 플래그가 꺼져 있으면 예전 셋 그대로다."""
    if get_settings().mission_status_en_route_enabled:
        return DRAFT_STATUSES
    return LEGACY_STATUSES


@dataclass(frozen=True)
class MissionObservation:
    accepted: bool               # 상태를 받아 적었나
    reason: str | None           # 안 받았으면 왜(no_status / unknown_status)
    previous: str | None
    current: str | None
    arrival_edge: bool           # 이번 보고가 새 도착인가(대시보드·알림 트리거 자리)
    redispatch: bool             # 도착 뒤 스스로 다시 나섰나(ARRIVED → EN_ROUTE)
    unexpected: bool             # 전이 계약에 없는 조합인가(로그만, 상태는 받아 적음)


class MissionTracker:
    """로봇별 마지막 mission_status를 든다.

    워커 1개 전제라 인메모리로 충분하다(ws.ConnectionManager랑 같은 결). 재기동하면
    비고, 로봇이 다음 상태를 올리는 순간 다시 채워진다 — 복원할 게 없다.
    """

    def __init__(self) -> None:
        self._last: dict[str, str] = {}

    def last(self, robot_id: str) -> str | None:
        return self._last.get(robot_id)

    def observe(self, robot_id: str, status: str | None) -> MissionObservation:
        previous = self._last.get(robot_id)

        if status is None:
            return MissionObservation(
                accepted=False, reason="no_status", previous=previous,
                current=previous, arrival_edge=False, redispatch=False, unexpected=False,
            )
        if status not in known_statuses():
            # 낯선 값 — 상태를 안 바꾸고 흘린다. 플래그 꺼진 EN_ROUTE도 여기로 온다.
            return MissionObservation(
                accepted=False, reason="unknown_status", previous=previous,
                current=previous, arrival_edge=False, redispatch=False, unexpected=False,
            )

        unexpected = status not in ALLOWED_TRANSITIONS.get(previous, DRAFT_STATUSES)
        arrival_edge = status == MissionStatus.ARRIVED and previous != MissionStatus.ARRIVED
        redispatch = previous == MissionStatus.ARRIVED and status == MissionStatus.EN_ROUTE

        self._last[robot_id] = status
        return MissionObservation(
            accepted=True, reason=None, previous=previous, current=status,
            arrival_edge=arrival_edge, redispatch=redispatch, unexpected=unexpected,
        )

    def reset(self) -> None:
        self._last.clear()


tracker = MissionTracker()


def reset_mission_states() -> None:
    """시험 격리용. 인메모리 상태가 다음 케이스로 새지 않게 비운다."""
    tracker.reset()
