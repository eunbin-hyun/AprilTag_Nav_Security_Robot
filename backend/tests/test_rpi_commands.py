"""라파이 명령 폴링 창구 — `GET /api/rpi/commands/poll` · `POST /api/rpi/commands/{id}/ack`.

실기기 폴러(`hardware/rpi/rpi_commands.py`)가 부르는 이름이다. 그 폴러는 모의 서버로
개발돼 서버에 라우트가 0건인 채로 올라왔고(커밋 `0283d6e`), 붙이면 404만 받아 시연에서
소리가 안 나는 자리였다. 서버를 폴러에 맞추기로 사용자가 정했다(2026-08-05).

## 여기서 재는 것 다섯

1. ⭐ **응답 칸 이름이 폴러가 읽는 그대로다.** 이 파일의 본체다 — `commands`·`command_id`·
   `type` 셋 중 하나만 어긋나도 폴러가 **빈 목록으로 읽고 조용히 아무 소리도 안 낸다.**
   오류도 안 나서 화면만 보면 못 가른다.
2. **같은 신호를 두 번 안 준다.** 1초에 한 번 부르는 기기가 같은 신호를 계속 받으면
   스피커가 무한히 울린다. 커서 밀기가 그 자리다.
3. ⭐ **ack 는 큐를 안 건드린다.** 확인 응답이 유실되면 신호가 되살아나 그 무한 반복이
   일어난다 — 근거는 `app/device_sound` 모듈 머리다. 그래서 ack 는 받아 두기만 한다.
4. **로그인을 켠 판에서도 기기 키로 통과한다.** 라파이는 세션을 못 쥔다.
5. **`gate_no`를 받되 안 거른다.** 폴러가 보내니 422로 튕기면 안 되고, 서버는 어느 기기가
   어느 게이트에 있는지 몰라서 거르면 조용히 안 울리는 기기가 생긴다.

## ⚠ 계약 문자열을 상수로 안 빼는 이유

칸 이름을 이 파일 안에 **리터럴로 박는다.** 서버 스키마에서 import 하면 이름을 바꿀 때
시험도 같이 따라가서 못이 헛돈다 — 값 자체가 계약인 자리라 양쪽이 따로 적혀 있어야
어긋남이 빨개진다(memory `feedback_boundary_test_imports_constant`).
"""
from __future__ import annotations

import pytest

from app import device_sound
from app.config import get_settings
from app.device_sound import KIND_BUS_ARRIVAL, push_sound

pytestmark = pytest.mark.asyncio(loop_scope="session")

API_KEY = get_settings().api_key
KEY_HEADERS = {"X-API-Key": API_KEY}
POLL = "/api/rpi/commands/poll"
DEVICE = "raspberry01"

# 실기기 폴러가 이 값이면 셔틀 도착 음원을 재생한다(`hardware/rpi/rpi_commands.py`
# `ARRIVAL_COMMAND_TYPES`). 서버 큐의 종류 이름과 같아야 변환 없이 흐른다.
#
# ⚠ **2026-08-06에 `shuttle_arrival`에서 바뀌었다**(라파이 요청). 라파이 음원 파일이
# `bus_arrival.wav`이고 그쪽 수용 집합이 이미 이 이름을 들고 있었다. **글자를 여기 박아 두는
# 이유** — 서버 상수(`KIND_BUS_ARRIVAL`)를 그대로 import하면 상수 이름만 바꿔도 시험이 따라가
# 계약이 조용히 깨진다. 기기가 읽는 값은 사람이 눈으로 대조해야 한다.
ARRIVAL_TYPE = "bus_arrival"


@pytest.fixture(autouse=True)
def _clean_sound_queue():
    """케이스마다 큐를 비운다.

    `tests/test_device_sound.py`의 같은 이름 픽스처와 짝이다. 두 파일이 **같은 인메모리
    큐를 읽어서**, 앞뒤로 비우지 않으면 서로 오염원이 된다. conftest 로 올리지 않고 각자
    자기 사본을 드는 이유는 그 파일의 주석과 같다 — 읽는 자리가 아직 둘뿐이라 전역으로
    올릴 값어치보다 "어느 파일이 무엇을 비우나"가 그 파일 안에 보이는 쪽이 낫다.
    """
    device_sound.reset_sound_queue()
    yield
    device_sound.reset_sound_queue()


@pytest.fixture
def flag_on(monkeypatch):
    """`AUTH_REQUIRE_LOGIN=true` 구간. 설정이 lru_cache 라 캐시를 앞뒤로 비운다."""
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _poll(client, device_id: str = DEVICE, **params) -> dict:
    res = await client.get(
        POLL, params={"device_id": device_id, **params}, headers=KEY_HEADERS
    )
    assert res.status_code == 200, res.text
    return res.json()


# ── 1. ⭐ 응답 칸 이름이 폴러 계약 그대로다 ────────────────────────────────


async def test_응답_칸_이름이_실기기_폴러_계약과_같다(client):
    """이 파일의 본체. 이름이 어긋나면 폴러가 빈 목록으로 읽고 조용히 안 울린다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=3, event_id="rpi-contract")

    body = await _poll(client)

    # 겉봉 이름. 폴러 `normalize_commands`가 이 이름을 먼저 본다.
    assert "commands" in body, "겉봉이 commands 가 아니면 폴러가 빈 목록으로 읽는다"
    assert len(body["commands"]) == 1

    cmd = body["commands"][0]
    # 폴러 `command_type`이 보는 이름. 이 값이 ARRIVAL 목록에 들어야 음원이 나간다.
    assert cmd["type"] == ARRIVAL_TYPE
    # 폴러 `command_id`가 보는 이름. ack 경로에 문자열로 끼운다.
    assert isinstance(cmd["command_id"], str) and cmd["command_id"]
    # 곁들이 칸 둘. 화면·로그에서 도착 행과 이어 보는 열쇠다.
    assert cmd["gate_no"] == 3
    assert cmd["event_id"] == "rpi-contract"


async def test_울릴_게_없으면_빈_목록이다(client):
    """평소 모습이다. 1초에 한 번 이 답을 받는다."""
    assert await _poll(client) == {"commands": []}


async def test_한_번에_쌓인_명령을_한꺼번에_준다(client):
    """폴링 주기 사이에 둘이 들어오면 둘 다 나온다. 순서는 들어온 순서다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="a")
    push_sound(KIND_BUS_ARRIVAL, gate_no=2, event_id="b")

    body = await _poll(client)
    assert [c["event_id"] for c in body["commands"]] == ["a", "b"]
    # command_id 가 서로 달라야 ack 가 엉키지 않는다.
    assert len({c["command_id"] for c in body["commands"]}) == 2


# ── 2. 같은 신호를 두 번 안 준다 ──────────────────────────────────────────


async def test_같은_명령을_두_번_주지_않는다(client):
    """1초에 한 번 부르는 기기가 같은 명령을 계속 받으면 스피커가 무한히 울린다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="once")

    assert len((await _poll(client))["commands"]) == 1
    assert (await _poll(client))["commands"] == [], "같은 명령이 두 번 나왔다"
    # 세 번째까지 본다. 두 번만 재면 "한 번 더 주고 마는" 결함이 초록으로 지난다.
    assert (await _poll(client))["commands"] == []


async def test_두_창구가_커서를_기기별로_따로_든다(client):
    """`/api/sound-signals`와 이 창구가 같은 큐를 본다. 커서는 `device_id`별이다.

    ⚠ **한 기기가 두 창구를 같이 쓰면 뒤에 부른 쪽이 빈손이다.** 실기기가 둘 중 하나만
    써야 한다는 계약의 근거가 여기다 — 두 창구를 번갈아 부르면 소리가 절반만 난다.
    """
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="shared")

    # 같은 기기 이름으로 옛 창구를 먼저 부르면 커서가 밀려 이 창구는 빈손이다.
    old = await client.get(
        "/api/sound-signals", params={"device_id": DEVICE}, headers=KEY_HEADERS
    )
    assert old.status_code == 200
    assert len(old.json()["signals"]) == 1
    assert (await _poll(client))["commands"] == []

    # ⭐ 딴 기기는 커서가 따로라 **큐에 남은 것을 전부** 받는다. 한 대가 가져갔다고 다른
    # 대 몫이 사라지면 안 되기 때문이다(`device_sound` 모듈 머리 "기기가 여럿일 때").
    push_sound(KIND_BUS_ARRIVAL, gate_no=2, event_id="per-device")
    fresh = (await _poll(client, device_id="raspberry02"))["commands"]
    assert [c["event_id"] for c in fresh] == ["shared", "per-device"]


# ── 3. ⭐ ack ─────────────────────────────────────────────────────────────


async def test_ack_는_200_이고_큐를_안_건드린다(client):
    """ack 가 큐를 건드리면 응답 유실 때 신호가 되살아나 무한 반복이 난다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="ack-target")
    cmd = (await _poll(client))["commands"][0]

    res = await client.post(
        f"/api/rpi/commands/{cmd['command_id']}/ack", headers=KEY_HEADERS
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"acked": True, "command_id": cmd["command_id"]}

    # ack 뒤에도 같은 명령이 되살아나면 안 된다.
    assert (await _poll(client))["commands"] == []


async def test_모르는_command_id_로_ack_해도_200_이다(client):
    """꺼낼 때 이미 지워서 "없는 명령"과 "이미 처리한 명령"이 서버에서 같은 모양이다.

    404를 주면 폴러가 정상 흐름에서 에러를 찍는다(`rpi_commands.py:111-113`).
    """
    res = await client.post("/api/rpi/commands/sound-999999/ack", headers=KEY_HEADERS)
    assert res.status_code == 200
    assert res.json()["acked"] is True


# ── 4. 게이트 ─────────────────────────────────────────────────────────────


async def test_키가_없으면_401_이다(client):
    """기기 키 게이트. 인입 3종과 같은 잣대다."""
    assert (await client.get(POLL, params={"device_id": DEVICE})).status_code == 401
    assert (await client.post("/api/rpi/commands/x/ack")).status_code == 401


async def test_로그인을_켜도_기기_키로_통과한다(client, flag_on):
    """라파이는 세션을 못 쥔다 — 켜는 순간 401이 되면 스피커가 통째로 죽는다.

    `require_api_key`는 `key_gate_or` 갈림을 안 지나서 플래그를 아예 안 읽는다.
    """
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="flag-on")

    res = await client.get(POLL, params={"device_id": DEVICE}, headers=KEY_HEADERS)
    assert res.status_code == 200, "로그인을 켜니 라파이가 막혔다 — 스피커가 죽는다"
    assert len(res.json()["commands"]) == 1

    ack = await client.post("/api/rpi/commands/sound-1/ack", headers=KEY_HEADERS)
    assert ack.status_code == 200


# ── 5. gate_no ───────────────────────────────────────────────────────────


async def test_gate_no_를_받되_안_거른다(client):
    """폴러가 늘 보낸다. 422로 튕기면 폴링이 통째로 죽고, 거르면 조용히 안 울린다.

    서버는 어느 기기가 어느 게이트에 있는지 모른다 — 그 짝은 기기 쪽 `GATE_NO`에만 있다.
    """
    push_sound(KIND_BUS_ARRIVAL, gate_no=7, event_id="gate-mismatch")

    # 기기가 보낸 gate_no(1)와 신호의 gate_no(7)가 달라도 준다.
    body = await _poll(client, gate_no=1)
    assert [c["gate_no"] for c in body["commands"]] == [7]


async def test_device_id_가_없으면_422_다(client):
    """기본값을 두면 여러 기기가 한 커서를 나눠 써서, 한 대가 가져간 신호를 다른 대는
    영영 못 받는다 — 조용히 안 울리는 갈래라 제일 찾기 어렵다."""
    assert (await client.get(POLL, headers=KEY_HEADERS)).status_code == 422
