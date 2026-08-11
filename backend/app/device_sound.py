"""기기 스피커에 낼 소리를 서버가 쌓아 두고, 기기가 폴링으로 가져가는 신호 큐.

## 왜 폴링인가

라파이(`hardware/rpi/gate_server.py`)는 서버로 **보내기만** 한다 — 인입 POST 셋과 복귀 POST가
전부고, 서버가 먼저 말을 거는 길이 아예 없다. 로봇처럼 WS를 열게 하면 기기 쪽에 재접속·
하트비트 층이 통째로 새로 생긴다. 라파이는 이미 `requests`로 서버를 부르고 있어서, 1초마다
GET 한 번이 그쪽 코드가 제일 적게 드는 길이다(팀 확정).

## ⭐ 한 번 준 신호는 다시 안 준다 — 확인 응답이 아니라 "꺼내면 사라짐"

라파이가 1초마다 부르는데 같은 신호를 계속 받으면 스피커가 무한히 울린다. 그래서 신호는
**꺼내는 순간 그 기기 몫으로는 끝난다**(커서를 큐 끝으로 밀어 버린다).

확인 응답(ack)을 안 고른 이유는 왕복이 하나 더 늘어서가 아니라, **그 응답이 유실되면 신호가
되살아나 바로 그 무한 반복이 일어나서**다. 잃는 쪽과 겹치는 쪽 중 어느 쪽이 나쁜가로 갈랐다 —
응답 한 장이 유실되면 안내 한 번을 놓치고 끝이지만, 겹치면 시연 내내 울린다. TTL 방식도
같은 이유로 뺐다. TTL만 두면 그 창 안에서는 같은 신호가 몇 번이고 다시 나간다.

## 신호 나이 상한이 따로 있는 이유

커서만으로는 **잠깐 끊겼다 돌아온 기기**를 못 막는다. 폴링 루프가 30초 멈췄다 붙으면 그 사이
쌓인 도착 신호를 한꺼번에 받아 연달아 울린다. 도착 안내는 지나가면 뜻이 없어서,
`_SIGNAL_TTL_SEC`보다 오래된 신호는 아예 안 준다. 커서가 "이미 준 것"을 막고 나이 상한이
"이제 와서 울릴 이유가 없는 것"을 막는다 — 겹치는 규칙이 아니라 서로 다른 갈래다.

## 기기가 여럿일 때

`ShuttleArrivalIn`에도 화면 호출(`ShuttleCallIn`)에도 **device_id 칸이 없다**(app/schemas.py).
신호를 만드는 자리는 어느 라파이한테 줄지 모른다는 뜻이다. 그래서 큐를 기기별로 가르지 않고
**한 줄에 쌓고 기기마다 커서만 따로 든다** — 한 번 넣으면 붙어 있는 기기가 각자 한 번씩
가져간다. 지금은 `raspberry01` 하나지만 두 대가 돼도 이 모양이 그대로 돈다.

게이트마다 다른 소리를 내야 하면 그때 거르면 된다 — 그 자리를 위해 `gate_no`를 신호에 실어
둔다. 지금 서버가 안 거르는 이유는 "어느 기기가 어느 게이트에 있나"를 서버가 모르기
때문이다(그 짝은 기기 쪽 `GATE_NO`에만 있다). 서버가 그 짝을 알게 되는 날이 거르는 자리다.

## 재기동하면 신호가 사라진다 — 그게 맞다

인메모리다. 도착 안내는 **지나간 순간**이라 재기동 뒤에 되살아나면 이미 떠난 셔틀을 알리게
된다. 사건 기록은 `shuttle_arrival` 행이 이미 남기고, 이 큐가 드는 건 "지금 울려라"라는
휘발성 지시다. 옆의 셔틀 상태(`shuttle_call._last_call`·`dispatch._windows`·긴급정지)가 전부
DB 밖인 것과 같은 잣대다.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

# 소리 종류. 응답에 종류를 싣는 이유는, 소리가 늘 때 기기가 음원을 고를 근거가 응답 안에
# 있어야 해서다. 종류 없이 시작하면 두 번째 소리가 붙는 날 창구 계약을 바꿔야 하고, 그때는
# 기기 코드가 이미 배포돼 있다.
#
# ⭐ **낱말은 넷이다**(2026-08-06 라파이 요청). 라파이 음원 파일 이름과 1:1이다 —
# `bus_arrival.wav`·`robot_departure.wav`·`robot_arrival.wav`·`robot_return_complete.wav`.
# 늘리면 `hardware/rpi/rpi_commands.py`의 수용 집합과 이 개수 표시를 같이 고쳐야 한다.

# ⚠ **이름이 `shuttle_arrival`에서 바뀌었다**(2026-08-06). 라파이가 음원을 `bus_arrival.wav`로
# 잡아 두었고 그쪽 코드가 이미 이 이름을 받고 있어서 맞췄다.
#
# ⛔ **서버 안의 다른 `shuttle_arrival`은 그대로다.** 그 낱말은 다섯 축에 쓰인다 — 소리 큐
# (여기), DB 표 이름(`models.ShuttleArrival.__tablename__`), 이벤트 종류(`/api/events`의
# `kind`), 경고 출처(`robot_channel.SHUTTLE_SOURCE_TYPE`), 챗봇 자료 키(`assistant_data`).
# **여기만 갈아야 한다** — 나머지를 같이 바꾸면 화면 이벤트 필터와 경고 역추적이 끊긴다.
KIND_BUS_ARRIVAL = "bus_arrival"

# ⭐ 로봇 사건 둘(2026-08-06 사용자 배선). 젯슨과 라파는 서로 직접 못 붙어서, 젯슨이 서버로
# 올린 `mission_status`를 서버가 이 큐로 넘긴다(`robot_channel.MISSION_SOUND_KINDS`).
#
# ⚠ **값이 알림 종류(`robot_channel.ROBOT_ARRIVAL_ALERT_TYPE` 등)와 같은 것은 우연이 아니다.**
# 폴링 창구가 `kind`를 그대로 `type`으로 내보내고(`routers/rpi_commands.py`), 라파이 폴러가
# 그 문자열로 음원을 고른다. 한쪽만 바꾸면 조용히 안 울린다.
#
# ✅ **라파이가 넷을 다 받게 됐다**(2026-08-07 11:35 · `a4e851b` · 황시은). `ARRIVAL_COMMAND_TYPES`가
# 이 파일의 낱말 넷과 1:1로 맞춰졌고 음원 파일도 넷 다 들어갔다. ⚠ 예전 "라파이는 아직 이 둘을
# 모른다"는 주석은 무효다.
#
# ✅ **2026-08-09 확인 — `a4e851b`는 `dev`에 이미 병합됐다.** 예전 줄("`feature/rpi-sounds`
# 브랜치에만 있고 dev에는 아직 없다")은 무효다. `git branch -a --contains a4e851b`에
# `remotes/origin/dev`가 뜨고, `hardware/rpi/rpi_commands.py`의 `ARRIVAL_COMMAND_TYPES`에 낱말
# 넷이 다 있고 wav 넷도 들어와 있다. 그 낡은 줄을 믿으면 시연 전에 "라파가 `robot_arrival`·
# `robot_return_complete`를 아직 모른다"로 잘못 판단한다.
#
# ⚠ 그래도 **실기기가 무엇을 돌리는지는 브랜치가 아니라 그 기기에 깔린 사본으로 판정한다.**
# dev에 있다는 것과 라파에 배포됐다는 것은 다른 말이다.
#
# ⚠ **라파이가 뺀 이름 둘** — `shuttle_arrival`·`play_shuttle_arrival_sound`는 그쪽 수용 집합에서
# 사라졌다. 이 파일은 이미 `bus_arrival`을 쓰고 있어서 안 걸리지만, **여기에 그 옛 이름을 다시
# 넣으면 조용히 안 울린다.**
KIND_ROBOT_ARRIVAL = "robot_arrival"
KIND_ROBOT_RETURN_COMPLETE = "robot_return_complete"

# ⭐ 로봇 출발(2026-08-06 라파이 요청). **A→C 출동과 C→A 복귀 둘 다 이 하나를 쓴다** —
# 음성 문구를 "로봇이 출발합니다"처럼 방향 없이 잡기로 라파이와 합의했다.
#
# ⚠ **위 셋과 넣는 자리가 다르다.** 도착·복귀완료는 젯슨이 올린 `mission_status`를 보고
# 넣는데(상태 전이), 출발은 **서버가 명령을 실제로 보낸 순간**에 넣는다
# (`dispatch.broadcast_outcome`). 상태 전이로 잡으면 "도착에서 벗어난 전이"가 복귀로 빠질
# 때도 실패로 멈출 때도 같이 나서, **길에 멈춘 로봇 옆에서 출발 안내가 울린다** — 그래서
# 8/6 새벽 배선에서는 아예 뺐던 자리다. 라파이 안대로 명령 시점으로 옮기니 그 갈래가 사라졌다.
KIND_ROBOT_DEPARTURE = "robot_departure"

# 큐에 담아 두는 신호 최대 개수. 넘치면 오래된 쪽부터 밀려난다. 나이 상한이 이미 30초라
# 정상 운영에서는 몇 개를 넘길 일이 없고, 이 값은 기기가 통째로 죽어 있을 때의 메모리 상한이다.
_MAX_QUEUED = 32

# 이 초를 넘긴 신호는 안 준다(모듈 머리 "신호 나이 상한").
_SIGNAL_TTL_SEC = 30.0

# 커서를 들고 있는 기기 수 상한. `device_id`는 요청이 고르는 값이라 상한이 없으면 값만 바꿔
# 부르는 것으로 dict가 계속 자란다(기기 키 뒤라 아무나는 아니지만, 오타 하나로도 는다).
_MAX_DEVICE_CURSORS = 64


@dataclass(frozen=True)
class SoundSignal:
    """울릴 소리 하나. `created_at`은 나이 판정에만 쓰는 단조 시계라 응답에 안 싣는다."""

    seq: int
    kind: str
    gate_no: int | None
    event_id: str | None
    created_at: float

    def as_payload(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "gate_no": self.gate_no,
            "event_id": self.event_id,
        }


_lock = threading.Lock()
_signals: deque[SoundSignal] = deque(maxlen=_MAX_QUEUED)
_cursors: dict[str, int] = {}
_seq = 0


def _mono() -> float:
    """단조 시계. 시험이 갈아끼우는 자리라 함수로 뺀다(`shuttle_call._mono`와 같은 방식)."""
    return time.monotonic()


def push_sound(
    kind: str, *, gate_no: int | None = None, event_id: str | None = None
) -> int:
    """소리 하나를 큐에 넣는다. 붙어 있는 기기가 각자 한 번씩 가져간다.

    락으로 감싸는 이유는 `shuttle_call.next_web_call_event_id`와 같다 — 워커가 하나여도
    asyncio가 아니라 스레드에서 부를 수 있다(BackgroundTasks·시험).
    """
    global _seq
    with _lock:
        _seq += 1
        signal = SoundSignal(
            seq=_seq,
            kind=kind,
            gate_no=gate_no,
            event_id=event_id,
            created_at=_mono(),
        )
        _signals.append(signal)
        return signal.seq


def take_sounds(device_id: str) -> list[SoundSignal]:
    """이 기기가 아직 안 가져간 신호를 꺼낸다. **꺼내면 그 기기 몫으로는 끝난다.**"""
    now = _mono()
    with _lock:
        seen = _cursors.get(device_id, 0)
        fresh = [
            s
            for s in _signals
            if s.seq > seen and now - s.created_at <= _SIGNAL_TTL_SEC
        ]
        if _signals:
            # ⚠ 커서를 "준 것"이 아니라 **큐 끝**으로 민다. 준 것 기준으로 밀면 나이가 지나
            # 건너뛴 신호가 커서 뒤에 영영 남아, 폴링마다 매번 다시 걸러진다(1초에 한 번씩
            # 같은 일을 되풀이하고 결과는 늘 같다).
            _cursors[device_id] = _signals[-1].seq
        if len(_cursors) > _MAX_DEVICE_CURSORS:
            # 커서를 통째로 버린다. 잃는 건 "어디까지 줬나"뿐이고 그 뒤 첫 폴링은 나이 상한
            # 안에 남은 신호만 받는다(최대 몇 초짜리다). 기기가 예순 대를 넘길 판이 아니라
            # 정교한 축출을 만들 이유가 없다 — 여긴 메모리 상한이지 거동이 아니다.
            _cursors.clear()
        return fresh


def reset_sound_queue() -> None:
    """시험 위생. 인메모리라 앞 케이스가 넣은 신호가 다음 케이스로 샌다.

    `_seq`는 일부러 안 되돌린다. 되돌리면 어디선가 살아남은 커서가 새 신호보다 큰 번호를
    들고 있게 돼서, 그 기기만 조용히 아무것도 못 받는다.
    """
    with _lock:
        _signals.clear()
        _cursors.clear()
