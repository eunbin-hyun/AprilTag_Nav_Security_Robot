"""게이트 센싱 상태 — 라파이가 터치로 켜고 끄는 그 상태의 **마지막 값**만 든다.

## 왜 이 모듈이 생겼나

2026-08-07에 라파이가 **자는 채로 시작**하는 구조로 바뀌었다(황시은 · `a4e851b`). 사람이
터치 센서를 3초 눌러야 빔·카드 리더가 깨어난다.

⛔ **터치 전에는 태깅도 통과도 서버에 한 건도 안 온다.** 그런데 그 상태가 관제 화면에서
**"이상 없음"**으로 보였다 — 게이트 칩이 초록이고 오늘 통과 수가 0이다. 시연에서 터치를
깜빡하면 "잘 돌고 있다"로 읽히는데 실제로는 아무것도 안 재고 있다.

⭐ 화면 갈래가 그릴 자리를 만들어 두고 이 재료를 요청했다(프론트 61차).

## 왜 DB가 아니라 메모리인가

⭐ **재시작하면 "모른다"로 돌아가는 것이 맞다.** DB에 남겨 두면 서버가 다시 뜬 뒤에도
**옛 값을 지금 상태인 양** 화면에 실어 준다 — 그 사이에 누가 라파이를 껐어도 초록이다.
모르는 것을 모른다고 말하는 쪽이 정직하고, 화면도 `null`을 "아직 못 받았습니다"로 그린다.

⚠ **그래서 배포 뒤에는 라파이가 다시 보낼 때까지 `null`이다.** 라파이는 상태가 바뀔 때만
보내므로 몇 시간 갈 수 있다. 그 공백을 못 견디게 되면 주기 전송을 요청하는 쪽이지,
DB에 굳히는 쪽이 아니다.

## ⚠ 안 실려 온 것과 거짓은 다르다

`sensing`을 안 보낸 기기는 **여기 안 들어온다.** `False`로 기록하지 않는다 — "안 잰다"와
"모른다"가 화면에서 다른 색이라 섞으면 거짓 경고가 된다.
"""
from __future__ import annotations

import datetime as dt
import threading

# 상태를 들고 있을 기기 수 상한. `device_id`는 요청이 고르는 값이라 상한이 없으면 값만 바꿔
# 부르는 것으로 dict가 계속 자란다(오타 하나로도 는다). 게이트가 열여섯을 넘을 일이 없다.
_MAX_DEVICES = 16

_lock = threading.Lock()
_state: dict[str, bool] = {}
# ⭐ 마지막으로 받은 시각(2026-08-08 교차 검증). 값이 "마지막 값"이라 라파이 전원이 빠지면
#   옛 상태가 굳는데, 시각이 없으면 화면이 "언제 기준"인지 적을 길이 없다. 값을 삭히는
#   TTL은 일부러 안 둔다 — 오래된 값을 지우는 정책은 화면·운영이 정할 일이고, 여긴 사실
#   (마지막 보고와 그 시각)만 든다.
_last_at: dt.datetime | None = None


def record(device_id: str, sensing: bool) -> None:
    """기기 하나의 마지막 센싱 상태를 적는다."""
    global _last_at
    with _lock:
        if device_id not in _state and len(_state) >= _MAX_DEVICES:
            # 넘치면 가장 오래된 것부터 민다(dict는 삽입 순서를 지킨다).
            _state.pop(next(iter(_state)))
        _state[device_id] = sensing
        _last_at = dt.datetime.now(dt.timezone.utc)


def snapshot() -> bool | None:
    """화면에 실을 한 값. 못 받았으면 `None`이다.

    ⭐ **게이트가 여럿이면 하나라도 꺼져 있을 때 거짓이다.** 안전한 쪽으로 모은다 — 둘 중
    하나가 자고 있는데 초록으로 그리면 그 게이트 통과가 통째로 안 잡히는 것을 못 본다.
    """
    with _lock:
        if not _state:
            return None
        return all(_state.values())


def snapshot_at() -> dt.datetime | None:
    """마지막 보고 시각. 값을 한 번도 못 받았으면 `None`이다 — `snapshot()`과 같은 잣대."""
    with _lock:
        if not _state:
            return None
        return _last_at


def reset() -> None:
    """시험용. 케이스 사이에 상태가 새지 않게 비운다 — 시각도 같이 비워야 짝이 맞는다."""
    global _last_at
    with _lock:
        _state.clear()
        _last_at = None
