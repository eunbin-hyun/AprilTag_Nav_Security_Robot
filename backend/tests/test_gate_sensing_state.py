"""게이트 센싱 상태 창구 — `POST /api/gate-sensing-state`.

## 왜 이 창구가 생겼나

2026-08-07에 라파이가 **자는 채로 시작**하는 구조로 바뀌었다(황시은 · `a4e851b`). 사람이
터치 센서를 3초 눌러야 빔·카드 리더가 깨어난다.

⛔ **터치 전에는 태깅도 통과도 서버에 한 건도 안 온다.** 그런데 그 상태가 관제 화면에서
**"이상 없음"**으로 보였다 — 게이트 칩이 초록이고 오늘 통과 수가 0이다.

## ⚠ 이 파일이 지키는 것 — 셋을 나란히 본다

`true`·`false`·`null` 셋이 **서로 다른 뜻**이다. 한 가지만 재면 나머지 둘이 뒤섞여도 초록이다.
2026-08-06에 화면 갈래가 배운 것이 그것이라(참·거짓을 나란히 심으니 세 번째의 거짓 경고가
드러났다) 여기서도 셋을 같은 파일에 둔다.

## ⚠ 왜 `test_ingest_contract_required.py`에 안 넣었나

그 파일은 **탐지 이벤트**(태깅·통과·셔틀) 정본과 대조하는 자리다. 이 창구는 사건이 아니라
**기기 상태**라 `event_id`도 `observed_at`도 없다 — 그 정본에 끼우면 계약 뜻이 흐려진다.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")

ENDPOINT = "/api/gate-sensing-state"
SNAPSHOT = "/api/dashboard/snapshot"


async def _sensing(client) -> bool | None:
    return (await client.get(SNAPSHOT)).json()["gate_sensing"]


async def test_한_번도_안_받았으면_모른다(client):
    """⛔ `null`은 "괜찮다"가 아니라 "아직 못 받았다"이다.

    여기서 `False`를 주면 화면이 배포 직후마다 "센서가 꺼져 있습니다"를 띄운다 — 진짜
    꺼진 것과 구분이 안 되는 거짓 경고다.
    """
    assert await _sensing(client) is None


async def test_켜짐과_꺼짐이_나란히_서면_서로_다르다(client, auth_headers):
    """참·거짓·모름 셋이 한 케이스 안에서 갈리는지 본다."""
    assert await _sensing(client) is None, "사전 조건: 아직 못 받은 상태여야 한다"

    r = await client.post(ENDPOINT, json={"device_id": "raspberry01", "sensing": True},
                          headers=auth_headers)
    assert r.status_code == 200
    assert r.json() == {"device_id": "raspberry01", "sensing": True}
    assert await _sensing(client) is True

    await client.post(ENDPOINT, json={"device_id": "raspberry01", "sensing": False},
                      headers=auth_headers)
    assert await _sensing(client) is False, "끈 것이 스냅샷에 안 반영됐다"

    await client.post(ENDPOINT, json={"device_id": "raspberry01", "sensing": True},
                      headers=auth_headers)
    assert await _sensing(client) is True, "다시 켠 것이 반영 안 됐다 — 한 방향으로만 도는가"


async def test_게이트가_여럿이면_하나만_꺼져도_거짓이다(client, auth_headers):
    """⭐ 안전한 쪽으로 모은다.

    둘 중 하나가 자고 있는데 초록으로 그리면 **그 게이트 통과가 통째로 안 잡히는 것**을
    아무도 못 본다.
    """
    for device, on in (("raspberry01", True), ("raspberry02", True)):
        await client.post(ENDPOINT, json={"device_id": device, "sensing": on},
                          headers=auth_headers)
    assert await _sensing(client) is True

    await client.post(ENDPOINT, json={"device_id": "raspberry02", "sensing": False},
                      headers=auth_headers)
    assert await _sensing(client) is False, "하나가 꺼졌는데 전체가 초록이다"


async def test_sensing을_안_보내면_422다(client, auth_headers):
    """⚠ 선택 칸으로 두면 **안 실려 온 요청이 "꺼짐"으로 기록된다.**

    "모른다"와 "안 잰다"는 화면에서 다른 색이라 섞이면 거짓 경고가 된다.
    """
    r = await client.post(ENDPOINT, json={"device_id": "raspberry01"}, headers=auth_headers)
    assert r.status_code == 422
    assert await _sensing(client) is None, "422인데 상태가 바뀌었다"


async def test_키가_없으면_401이고_상태도_안_바뀐다(client):
    """기기 창구라 세션이 아니라 API 키다. 라우터 전체에 걸린 게이트를 여기서도 확인한다."""
    r = await client.post(ENDPOINT, json={"device_id": "raspberry01", "sensing": True})
    assert r.status_code == 401
    assert await _sensing(client) is None, "인증에 막혔는데 상태가 기록됐다"


async def test_기기_수_상한을_넘으면_오래된_것부터_밀린다(client, auth_headers):
    """`device_id`는 요청이 고르는 값이라 상한이 없으면 값만 바꿔 부르는 것으로 계속 자란다.

    ⚠ 상한 자체보다 **밀려도 판정이 안 뒤집히는지**가 요점이다 — 꺼진 기기가 밀려나면
    전체가 조용히 초록이 된다. 여기서는 꺼진 것을 마지막에 넣어 살아남는지 본다.
    """
    from app import gate_sensing

    for i in range(gate_sensing._MAX_DEVICES + 4):
        await client.post(ENDPOINT, json={"device_id": f"rpi-{i:02d}", "sensing": True},
                          headers=auth_headers)
    await client.post(ENDPOINT, json={"device_id": "rpi-last", "sensing": False},
                      headers=auth_headers)

    assert len(gate_sensing._state) <= gate_sensing._MAX_DEVICES, "상한을 넘겨 자랐다"
    assert await _sensing(client) is False, "마지막에 넣은 꺼짐이 밀려났다"
