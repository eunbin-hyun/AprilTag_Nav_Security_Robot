"""기기 스피커 폴링 창구 — `GET /api/sound-signals`.

라파이가 1초마다 "울릴 거 있어?"를 묻는 자리다. 몸통과 설계 근거(왜 폴링인가, 왜 확인
응답이 아니라 꺼내면 사라짐인가, 기기가 여럿일 때)는 전부 `app/device_sound.py` 모듈 머리에
있다. 여기는 창구 껍데기다.

## 게이트를 왜 `require_api_key`로 두나

라파이는 **사람이 아니라 세션을 못 쥔다.** 그래서 인입 3종과 같은 게이트를 쓴다 —
`app/routers/ingest.py`가 `dependencies=[Depends(require_api_key)]`를 라우터에 그대로 다는
것과 글자까지 같은 모양이다.

⭐ **`AUTH_REQUIRE_LOGIN`을 켜도 이 창구는 안 막힌다.** 근거는 갈림길이 `key_gate_or` 한
함수에만 있다는 것이다(`app/security.py` "로그인 이관 게이트" 절). `require_api_key`는 그
갈림을 안 지나고 `device_key_ok` → `keys_match` 한 줄로 끝나서 플래그를 아예 안 읽는다.
인입 3종이 켠 판에서도 기기 키로 도는 근거가 그것이고(`tests/test_auth_gates.py`
`test_flag_on_keeps_ingest_on_device_key`), 이 창구도 같은 이유로 같이 산다. 못은
`tests/test_device_sound.py`가 켠 판에서 직접 박는다.

⚠ **`app/routers/dispatch.py`의 `device_router`에 붙이지 마라.** 그쪽은 `key_gate_or(...,
device_key_fallback=True)`라 켠 뒤 기기 키 잣대가 더 엄하고(빈 값·저장소 기본키 거절),
붙는 창구 목록을 `tests/test_dispatch_key_gate.py`가 닫힌 집합으로 못박아 뒀다. 이 창구는
인입 계열이라 인입과 같은 게이트를 쓰는 게 맞다.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.device_sound import take_sounds
from app.schemas import SoundSignalOut, SoundSignalsOut
from app.security import require_api_key

router = APIRouter(
    prefix="/api", tags=["device-sound"], dependencies=[Depends(require_api_key)]
)


@router.get("/sound-signals", response_model=SoundSignalsOut)
async def poll_sound_signals(
    device_id: str = Query(min_length=1, max_length=64),
) -> SoundSignalsOut:
    """이 기기가 아직 안 받은 소리 신호를 준다. 같은 신호를 두 번 주지 않는다.

    ⚠ 조회처럼 보이지만 **부르면 상태가 바뀐다**(커서가 밀린다). GET인 이유는 기기 쪽이
    1초마다 부르는 가장 싼 모양이라서고, 그래서 이 응답은 캐시하면 안 된다 — 지금 경로에
    캐시를 얹는 자리가 없어 헤더를 따로 안 달았지만, nginx에 조각을 더할 일이 생기면 이
    창구를 캐시에서 빼라.

    `device_id`가 없으면 422다. 기본값을 두면 여러 기기가 한 커서를 나눠 써서, 한 대가
    가져간 신호를 다른 대는 영영 못 받는다 — 조용히 안 울리는 갈래라 제일 찾기 어렵다.
    """
    signals = take_sounds(device_id)
    return SoundSignalsOut(
        device_id=device_id,
        signals=[SoundSignalOut(**s.as_payload()) for s in signals],
    )
