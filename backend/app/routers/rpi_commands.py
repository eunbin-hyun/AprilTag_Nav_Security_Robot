"""라파이 명령 폴링 창구 — `GET /api/rpi/commands/poll` · `POST /api/rpi/commands/{id}/ack`.

## 왜 창구가 둘이 됐나

소리 신호를 나르는 창구는 원래 `GET /api/sound-signals` 하나였다(`app/routers/device_sound.py`).
그런데 라파이 담당(황시은)이 실기기 폴러를 **모의 서버로 개발해** 올렸고(`hardware/rpi/
rpi_commands.py`·`mock_rpi_command_server.py`, 커밋 `0283d6e`), 그 폴러가 부르는 주소가
여기 둘이다. 서버에는 그 라우트가 0건이라 **실기기를 붙이면 404만 받고 시연에서 소리가
안 난다.**

맞추는 방향을 서버 쪽으로 정한 건 사용자다(2026-08-05). 실기기 코드를 다시 만지는 것보다
서버에 창구를 붙이는 쪽이 싸고, 그쪽은 이미 모의 서버와 시험까지 짜 뒀다.

⚠ **기존 `/api/sound-signals`는 그대로 둔다.** 계약을 이미 전달했고 시험도 붙어 있다. 두
창구가 같은 큐(`app/device_sound`)를 보되 커서는 `device_id`별로 따로 든다 — 한 기기가 두
창구를 같이 쓰면 같은 신호를 두 번 받으니, **실기기는 둘 중 하나만 써라.** 지금 실기기가
쓰는 것은 이쪽이다.

## ⭐ ack 는 받아 두기만 한다 — 신호는 이미 꺼낼 때 사라진다

폴러는 소리를 낸 뒤 `POST .../ack`를 보낸다. 그런데 이 서버는 **꺼내는 순간 그 기기 몫으로
끝내는**(커서를 미는) 방식이다. 그 판단의 근거는 `app/device_sound` 모듈 머리에 있다 —
확인 응답이 유실되면 신호가 되살아나 스피커가 무한히 울린다.

그래서 ack 는 큐를 건드리지 않는다. 그래도 창구를 만드는 이유는 둘이다.

1. **없으면 폴러가 매번 에러를 찍는다.** `rpi_commands.py:111`이 400 이상이면 로그를 남긴다.
2. **"기기가 실제로 소리를 냈다"는 유일한 신호다.** 폴링만으로는 받아 갔다는 것까지고,
   ack 가 와야 재생까지 갔다는 뜻이다. 시연 전에 소리 경로를 확인할 때 이 로그가 근거다.

⚠ ack 응답이 유실돼도 안전하다 — 폴러가 실패를 로그만 찍고 넘어가고(`rpi_commands.py:111-114`),
신호는 이미 커서로 사라져서 되살아날 길이 없다.

## 게이트는 인입과 같다

라파이는 사람이 아니라 세션을 못 쥔다. 그래서 `require_api_key` 하나다 —
`app/routers/device_sound.py` 머리에 적은 근거가 그대로 적용된다. `AUTH_REQUIRE_LOGIN`을
켜도 이 창구는 안 막힌다(그 갈림은 `key_gate_or`에만 있고 `require_api_key`는 안 지난다).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path, Query

from app.command_log import ACK_PLAYED, CHANNEL_GATE, record_ack
from app.device_sound import take_sounds
from app.schemas import RpiCommandAckOut, RpiCommandOut, RpiCommandsOut
from app.security import require_api_key

logger = logging.getLogger("c207.rpi")

router = APIRouter(
    prefix="/api/rpi/commands", tags=["rpi-commands"],
    dependencies=[Depends(require_api_key)],
)

# 명령 id 접두사. 폴러는 이 값을 문자열로만 다루고(ack 경로에 그대로 끼운다) 뜻을 안 본다.
# 접두사를 붙이는 이유는 로그에서 "이 번호가 어느 계열인가"가 바로 읽히게 하려는 것뿐이다.
_ID_PREFIX = "sound-"


@router.get("/poll", response_model=RpiCommandsOut)
async def poll_commands(
    device_id: str = Query(min_length=1, max_length=64),
    gate_no: int | None = Query(default=None, ge=0, le=9999),
) -> RpiCommandsOut:
    """이 기기가 아직 안 받은 명령을 준다. **부르면 상태가 바뀐다**(커서가 밀린다).

    응답 모양은 실기기 폴러가 읽는 그대로다 — `{"commands": [{"command_id", "type", ...}]}`.
    `type`은 큐에 담긴 소리 종류를 **변환 없이 그대로** 내보낸다(`type=s.kind`).

    ⭐ **낱말은 넷이다**(2026-08-06 라파이 요청 반영) — `bus_arrival`·`robot_departure`·
    `robot_arrival`·`robot_return_complete`. 정본은 `app/device_sound.py`의 `KIND_*`이고
    라파이 음원 파일 이름과 1:1이다.

    ⚠ **한쪽만 바꾸면 조용히 안 울린다.** 라파이 `hardware/rpi/rpi_commands.py`의 수용 집합에
    없는 종류는 "알 수 없는 명령 무시"로 버려진다 — 에러도 로그도 안 남는다. 늘릴 때는 그쪽
    집합과 이 개수 표시를 같이 고쳐야 한다.

    ⚠ **`gate_no`는 받기만 하고 안 거른다.** 서버는 어느 기기가 어느 게이트에 있는지 모른다
    (그 짝은 기기 쪽 `GATE_NO`에만 있다 — `app/device_sound` 모듈 머리 "기기가 여럿일 때").
    폴러가 보내니 422로 튕기지 않으려고 받아 두고, 서버가 그 짝을 알게 되는 날 거르는
    자리다. 지금 거르면 게이트 번호가 어긋난 기기가 조용히 안 울린다.
    """
    signals = take_sounds(device_id)
    return RpiCommandsOut(
        commands=[
            RpiCommandOut(
                command_id=f"{_ID_PREFIX}{s.seq}",
                type=s.kind,
                gate_no=s.gate_no,
                event_id=s.event_id,
            )
            for s in signals
        ]
    )


@router.post("/{command_id}/ack", response_model=RpiCommandAckOut)
async def ack_command(
    command_id: str = Path(min_length=1, max_length=128),
) -> RpiCommandAckOut:
    """기기가 명령을 처리했다고 알린다. **큐는 안 건드린다** — 위 모듈 머리 참고.

    모르는 `command_id`가 와도 200이다. 큐에 없는 번호인지를 서버가 알 수 없기 때문이다 —
    꺼낼 때 이미 지웠으니 "그런 명령이 없다"와 "이미 처리했다"가 서버에서 같은 모양이다.
    404를 주면 폴러가 정상 흐름에서 에러를 찍는다.

    ⚠ 로그를 `warning`으로 남기는 이유는 `c207.rpi`의 유효 레벨이 운영에서 WARNING이라
    `info`로 적으면 한 줄도 안 남기 때문이다(2026-08-04에 `c207.security`에서 같은 자리를
    밟았다). 시연 전 소리 경로 확인이 이 로그에 걸려 있다.

    ⭐ **2026-08-06부터 DB에도 남긴다**(0018). 소리 큐가 인메모리 동기 함수라 **발행 자리에서는
    DB를 못 만진다** — 그래서 이 ack가 "그 소리가 실제로 울렸다"를 남기는 유일한 자리다.
    큐에 넣은 것보다 실제로 울린 것이 더 값진 기록이다.
    """
    logger.warning("rpi_command_ack command_id=%s", command_id)
    await record_ack(
        channel=CHANNEL_GATE, command_id=command_id, result=ACK_PLAYED
    )
    return RpiCommandAckOut(acked=True, command_id=command_id)
