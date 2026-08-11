"""기기 스피커 폴링 창구 — `GET /api/sound-signals` + 셔틀 도착이 큐에 넣는 자리.

라파이에 블루투스 스피커를 달아 셔틀 도착 안내 소리를 낸다. 라파이는 서버로 보내기만 해서
서버가 말을 걸 길이 없으니, 1초마다 이 창구를 물어 가져간다. 설계 근거는
`app/device_sound.py` 모듈 머리에 있다.

## 여기서 재는 것 넷

1. ⭐ **같은 신호를 두 번 안 준다.** 1초에 한 번 부르는 기기가 같은 신호를 계속 받으면
   스피커가 무한히 울린다 — 이 창구가 막으려는 결함의 본체다.
2. **셔틀 도착이 실제로 큐에 넣는다.** 창구만 재면 "울릴 게 영영 안 들어오는" 판에서도
   초록이다. 그래서 도착 처리를 진짜로 부르고 창구를 읽어 견준다.
3. ⭐ **로그인을 켠 판에서도 기기 키로 통과한다.** 라파이는 세션을 못 쥔다 —
   `AUTH_REQUIRE_LOGIN`을 켜는 순간 401이 되면 스피커가 통째로 죽는다.
4. **재전송·중복은 신호를 안 늘린다.** 라파 sender는 2xx까지 무한 재전송이라
   (`app/routers/ingest.py` 모듈 머리) 이게 운영에서 늘 밟히는 갈래다.

## 회귀 못이 진짜인지 어떻게 쟀나 (2026-08-04 실측)

수리를 되돌려 실제로 빨개지는지 봤다. 결과는 worklog에 있고, 요약은 이렇다.
- `take_sounds`의 커서 밀기를 지우면 → `test_같은_신호를_두_번_주지_않는다` 빨개짐.
- `shuttle_call.record_shuttle_arrival`의 `push_sound` 한 줄을 지우면 →
  `test_셔틀_도착이_큐에_신호를_넣는다`·`test_화면_호출도_같은_신호를_넣는다` 빨개짐.
- `push_sound`를 중복 갈래까지 타게 옮기면 → `test_같은_event_id_재전송은_신호를_안_늘린다`
  빨개짐.
- 라우터 게이트를 `key_gate_or(...)`로 바꾸면 → `test_로그인을_켜도_기기_키로_통과한다`
  빨개짐.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app import device_sound
from app.config import get_settings
from app.device_sound import KIND_BUS_ARRIVAL, push_sound, take_sounds

pytestmark = pytest.mark.asyncio(loop_scope="session")

API_KEY = get_settings().api_key
KEY_HEADERS = {"X-API-Key": API_KEY}
POLL = "/api/sound-signals"
DEVICE = "raspberry01"


@pytest.fixture(autouse=True)
def _clean_sound_queue():
    """이 파일 케이스마다 큐를 비운다.

    conftest `_clean`에 안 올린 이유가 있다. 이 큐는 **읽는 자리가 이 파일뿐**이라, 남의
    시험이 남긴 신호가 있어도 아무 케이스도 안 흔든다(셔틀 쿨다운은 반대였다 — 창구가
    429를 뱉어서 남의 파일을 깼다). 읽는 자리가 늘면 그때 conftest로 올린다.

    앞뒤로 비운다. 뒤를 안 비우면 이 파일이 다른 파일한테 오염원이 된다.
    """
    device_sound.reset_sound_queue()
    yield
    device_sound.reset_sound_queue()


@pytest.fixture
def flag_on(monkeypatch):
    """`AUTH_REQUIRE_LOGIN=true` 구간. 설정이 lru_cache라 캐시를 앞뒤로 비운다.

    `tests/test_auth_gates.py`의 같은 이름 픽스처와 글자까지 같은 모양이다 — 그 파일들이
    전부 자기 사본을 든다(계정·auth_core·dispatch_key_gate·staff·ws_session_revalidate).
    """
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _iso(offset_sec: float = 0.0) -> str:
    return (
        dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset_sec)
    ).isoformat()


async def _poll(client, device_id: str = DEVICE, **kwargs) -> dict:
    res = await client.get(
        POLL, params={"device_id": device_id}, headers=KEY_HEADERS, **kwargs
    )
    assert res.status_code == 200, res.text
    return res.json()


# ── 1. ⭐ 같은 신호를 두 번 안 준다 ────────────────────────────────────────


async def test_같은_신호를_두_번_주지_않는다(client):
    """이 파일의 본체. 1초에 한 번 부르는 기기가 같은 신호를 계속 받으면 무한히 울린다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="sound-once")

    first = await _poll(client)
    assert [s["kind"] for s in first["signals"]] == [KIND_BUS_ARRIVAL]
    assert first["signals"][0]["gate_no"] == 1
    assert first["signals"][0]["event_id"] == "sound-once"

    second = await _poll(client)
    assert second["signals"] == [], "같은 신호가 두 번 나왔다 — 스피커가 계속 울린다"

    # 세 번째까지 본다. 두 번만 재면 "한 번 더 주고 마는" 결함이 초록으로 지난다.
    assert (await _poll(client))["signals"] == []


async def test_울릴_게_없으면_빈_목록이다(client):
    """평소 모습이다. 1초에 한 번 이 답을 받는다."""
    body = await _poll(client)
    assert body == {"device_id": DEVICE, "signals": []}


async def test_가져간_뒤에_들어온_신호는_받는다(client):
    """커서가 밀린 뒤에도 새 신호는 온다 — 한 번 비면 영영 안 오는 결함을 막는다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="first")
    assert len((await _poll(client))["signals"]) == 1
    assert (await _poll(client))["signals"] == []

    push_sound(KIND_BUS_ARRIVAL, gate_no=2, event_id="second")
    again = await _poll(client)
    assert [s["event_id"] for s in again["signals"]] == ["second"]


async def test_한_번에_쌓인_신호를_한꺼번에_준다(client):
    """폴링 주기 사이에 둘이 들어오면 둘 다 나온다. 순서는 들어온 순서다."""
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="a")
    push_sound(KIND_BUS_ARRIVAL, gate_no=2, event_id="b")

    body = await _poll(client)
    assert [s["event_id"] for s in body["signals"]] == ["a", "b"]
    assert [s["seq"] for s in body["signals"]] == sorted(
        s["seq"] for s in body["signals"]
    )
    assert (await _poll(client))["signals"] == []


# ── 2. 셔틀 도착이 실제로 큐에 넣는다 ──────────────────────────────────────


async def test_셔틀_도착이_큐에_신호를_넣는다(client, no_hold):
    """기기 인입으로 도착을 넣고 창구를 읽어 견준다.

    `no_hold`로 5초 대기 창을 끈다. 이 시험이 보는 건 "도착이 소리를 넣나"라 출동 창이
    도는 걸 기다릴 이유가 없다(conftest `no_hold` docstring).
    """
    res = await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "snd-arr-1", "gate_no": 3, "signal_ts": _iso()},
        headers=KEY_HEADERS,
    )
    assert res.status_code == 200, res.text
    assert res.json()["stored"] is True

    body = await _poll(client)
    assert len(body["signals"]) == 1, "셔틀이 도착했는데 울릴 신호가 안 들어왔다"
    signal = body["signals"][0]
    assert signal["kind"] == KIND_BUS_ARRIVAL
    assert signal["gate_no"] == 3
    assert signal["event_id"] == "snd-arr-1"


async def test_화면_호출도_같은_신호를_넣는다(client, no_hold):
    """창구가 둘인데 몸통이 하나라 화면 버튼도 스피커를 울린다.

    ⚠ 이게 `record_shuttle_arrival`에 끼운 근거다. 라우터에 붙였으면 창구마다 한 줄씩
    적어야 하고, 한쪽을 빠뜨리면 "화면에서 부르면 안 울린다"가 조용히 생긴다.
    """
    res = await client.post("/api/shuttle-calls", json={"gate_no": 5})
    assert res.status_code == 200, res.text

    body = await _poll(client)
    assert len(body["signals"]) == 1
    assert body["signals"][0]["gate_no"] == 5
    # 화면 호출은 서버가 event_id를 만든다(`webcall-` 접두).
    assert body["signals"][0]["event_id"].startswith("webcall-")


async def test_같은_event_id_재전송은_신호를_안_늘린다(client, no_hold):
    """⭐ 라파 sender는 2xx까지 무한 재전송이라 운영에서 늘 밟히는 갈래다.

    중복 갈래에서도 소리를 넣으면 기기가 재전송할 때마다 한 번씩 더 울린다 — 그게 딱
    이 창구가 막으려던 그 모양이다.
    """
    body = {"event_id": "snd-dup-1", "gate_no": 2, "signal_ts": _iso()}
    first = await client.post("/api/shuttle-arrivals", json=body, headers=KEY_HEADERS)
    assert first.json()["stored"] is True

    second = await client.post("/api/shuttle-arrivals", json=body, headers=KEY_HEADERS)
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True

    polled = await _poll(client)
    assert len(polled["signals"]) == 1, (
        f"재전송이 소리를 늘렸다 — {len(polled['signals'])}번 울린다"
    )


# ── 3. ⭐ 로그인을 켜도 기기 키로 통과한다 ─────────────────────────────────


async def test_로그인을_켜도_기기_키로_통과한다(client, flag_on):
    """라파이는 세션을 못 쥔다 — 켠 뒤 401이 되면 스피커가 통째로 죽는다.

    인입 3종이 켠 판에서도 기기 키로 도는 것과 같은 성질이다
    (`test_auth_gates.test_flag_on_keeps_ingest_on_device_key`). 근거는 `require_api_key`가
    `key_gate_or` 갈림을 안 지나 플래그를 아예 안 읽는다는 것이다.
    """
    assert get_settings().auth_require_login is True, "flag_on이 안 먹었다"
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="snd-flagon")

    res = await client.get(POLL, params={"device_id": DEVICE}, headers=KEY_HEADERS)
    assert res.status_code == 200, f"켠 판에서 기기 키가 막혔다 — {res.status_code} {res.text[:200]}"
    assert len(res.json()["signals"]) == 1


async def test_켠_판에서도_키_없으면_401(client, flag_on):
    """열린 폭이 기기 키 하나뿐이다. 익명까지 열리면 키 게이트가 뜻을 잃는다."""
    res = await client.get(POLL, params={"device_id": DEVICE})
    assert res.status_code == 401, res.text


async def test_끈_판에서도_키_없으면_401(client):
    """제1 불변 — 꺼진 구간 거동도 같다(기기 키 그대로)."""
    assert get_settings().auth_require_login is False
    res = await client.get(POLL, params={"device_id": DEVICE})
    assert res.status_code == 401, res.text
    assert res.json() == {"detail": "유효한 X-API-Key 헤더가 필요합니다."}


async def test_device_id가_없으면_422(client):
    """기본값을 두면 여러 기기가 한 커서를 나눠 써서 한쪽이 조용히 안 울린다."""
    res = await client.get(POLL, headers=KEY_HEADERS)
    assert res.status_code == 422, res.text


# ── 4. 기기가 여럿일 때 ────────────────────────────────────────────────────


async def test_기기가_둘이면_각자_한_번씩_받는다(client):
    """지금은 라파이 하나지만 늘어도 안 깨지는 모양인지 본다.

    한 기기가 가져갔다고 다른 기기 몫이 사라지면, 두 번째 라파이를 붙이는 날 한 대만
    울린다 — 붙여 보기 전엔 아무도 모르는 갈래다.
    """
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="snd-multi")

    a = await _poll(client, "raspberry01")
    b = await _poll(client, "raspberry02")
    assert len(a["signals"]) == 1
    assert len(b["signals"]) == 1, "먼저 부른 기기가 남의 신호까지 가져갔다"
    assert a["device_id"] == "raspberry01" and b["device_id"] == "raspberry02"

    # 각자 두 번째 폴링은 비어 있다 — 기기별로 따로 세는 게 맞는지까지 본다.
    assert (await _poll(client, "raspberry01"))["signals"] == []
    assert (await _poll(client, "raspberry02"))["signals"] == []


# ── 5. 신호 나이 상한 ──────────────────────────────────────────────────────


async def test_오래된_신호는_안_준다(monkeypatch):
    """폴링 루프가 한참 멈췄다 붙었을 때 밀린 안내를 연달아 울리지 않는다.

    단조 시계를 감아서 잰다(실제로 30초를 기다리면 시험이 느려지고 시간에 흔들린다 —
    conftest `wall_clock`과 같은 방식).

    ⚠ 창구가 아니라 몸통을 직접 부른다. 창구를 태우면 나이 상한이 아니라 HTTP 왕복을 재게
    되고, 시계를 감는 자리도 두 겹이 된다.

    ⚠ 아무것도 안 기다리는데 `async def`인 이유는 모듈 머리의 `pytestmark`다. sync 함수로
    두면 그 마커가 안 맞아 경고가 뜨고, `-W error`가 들어오는 순간 이 못이 **결함이 아니라
    마커 때문에** 조용히 죽는다(`test_dispatch_key_gate.py`가 같은 자리를 이미 밟았다).
    """
    holder = {"t": 1_000.0}
    monkeypatch.setattr(device_sound, "_mono", lambda: holder["t"])

    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="snd-stale")
    holder["t"] += device_sound._SIGNAL_TTL_SEC + 1
    assert take_sounds("raspberry09") == [], "나이가 지난 안내가 그대로 나갔다"

    # 상한 안쪽은 그대로 나간다 — 상한이 모든 걸 삼키면 창구가 아무 일도 안 하는 것과 같다.
    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="snd-fresh")
    holder["t"] += 1
    assert [s.event_id for s in take_sounds("raspberry10")] == ["snd-fresh"]


async def test_나이가_지나_건너뛴_신호는_커서_뒤에_안_남는다(monkeypatch):
    """건너뛴 신호가 커서 뒤에 남으면 폴링마다 매번 다시 걸러진다(값은 같고 일만 는다).

    `async def`인 이유는 바로 위 케이스와 같다(모듈 머리 `pytestmark`).
    """
    holder = {"t": 2_000.0}
    monkeypatch.setattr(device_sound, "_mono", lambda: holder["t"])

    push_sound(KIND_BUS_ARRIVAL, gate_no=1, event_id="snd-old")
    holder["t"] += device_sound._SIGNAL_TTL_SEC + 1
    assert take_sounds("raspberry11") == []

    # 커서가 큐 끝까지 밀렸다면, 시계를 되감아도 그 신호를 다시 집지 않는다.
    holder["t"] -= device_sound._SIGNAL_TTL_SEC
    assert take_sounds("raspberry11") == [], "건너뛴 신호가 커서 뒤에 남아 되살아났다"
