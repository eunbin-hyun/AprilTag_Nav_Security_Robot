"""셔틀 도착 → 로봇 출동 (정본: docs/셔틀출동_설계_2026-07-31.md, 지라 S15P11C207-339).

## 무엇을 못박나

- **대기 창** — 셔틀이 와도 명령이 바로 안 나간다. 5초 안에 고르면 그 목적지로, 안 고르면
  그날 기본값으로, "대기"면 아예 안 나간다. 즉시 이동은 창을 취소하고 이긴다.
- **날씨 폴백** — 조회가 죽어도 마지막 성공값으로 돈다. 한 번도 성공한 적이 없으면
  UNKNOWN이고 실외로 간다(시연 경로가 실외 하나뿐이다).
- **수동 우선** — 요원이 고른 기본 목적지를 그날 안에는 자동 조회가 못 덮는다.
- **긴급정지** — 세우고 **푸는 것까지**. 푸는 자리가 없으면 시연 중 한 번 누르고 끝난다.

## 시간을 어떻게 다루나

대기 창은 진짜 asyncio 타이머다. 시험에서는 `hold` 픽스처로 창을 0.2초까지 줄이고,
"됐나"를 폴링으로 기다린다(`_wait_until`). 고정 sleep으로 기다리면 시험 DB가 원격일 때
왕복 지연이 길어져 간헐 실패가 난다 — 자동 발사 한 번이 DB 왕복 네 번이다.

⚠ 이 파일은 **바깥 날씨 서버를 절대 안 탄다.** autouse `stub_weather`가 조회 함수를
갈아 끼운다. 안 그러면 시험이 wttr.in 응답 속도에 매달리고, 그쪽이 죽은 날 통째로 빨개진다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest
from sqlalchemy import select

from app import dispatch as dispatch_mod
from app import weather as weather_mod
from app.config import get_settings
from app.db import get_session
from app.models import Alert, DailyDefaultDestination, ShuttleArrival
from app.robot_channel import ARRIVAL_EXPIRED_ALERT_TYPE, handle_robot_frame
from app.schemas import Destination
from app.weather import WeatherReading, WeatherStatus
from app.ws import manager, robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _reading(status: WeatherStatus, *, precip: float = 0.0, ok: bool = True) -> WeatherReading:
    return WeatherReading(
        status=status,
        description=status.value.title(),
        precip_mm=precip,
        temp_c=30,
        observed_at=dt.datetime.now(dt.timezone.utc),
        ok=ok,
    )


async def _wait_until(check, timeout: float = 10.0, interval: float = 0.05) -> bool:
    """조건이 참이 될 때까지 기다린다(고정 sleep 대신 폴링).

    자동 발사 한 번이 DB 왕복 네 번이라, 시험 DB가 원격이면 고정 sleep은 시간이 아슬아슬해
    간헐 실패를 만든다. 조건이 서면 바로 빠져나오니 빠르기도 하다.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return True
        await asyncio.sleep(interval)
    return check()


async def _past_deadline(hold_sec: float, *, margin: float = 0.4) -> None:
    """대기 창 마감을 **확실히 지나 보낸다.** "아무것도 안 나갔다"를 재기 전에 쓴다 (F68).

    ## 왜 고정 sleep을 쓰면 안 되나

    이 파일의 부정 단언 여덟 자리가 `await asyncio.sleep(1.0)  # 원래 마감을 넉넉히 지나
    보낸다` 꼴이었다. 창 길이는 바로 위에서 `hold(0.6)`으로 정하는데 기다리는 값은 딴 자리에
    글자로 박혀 있어서, **둘이 어긋나는 날 시험이 조용히 뜻을 잃는다** — 누가 `hold(0.6)`을
    `hold(1.5)`로 올리면 `sleep(1.0)`이 마감 전에 깨고, "명령이 안 나갔다"는 취소가 먹혀서가
    아니라 **아직 안 나가서** 초록이 된다. 실패로도 안 드러나는 종류의 무력화다.

    그래서 기다리는 길이를 창 길이에서 **파생**시킨다. `hold(...)`가 돌려주는 값을 그대로
    받으므로 창을 바꾸면 기다림이 따라온다.

    ## 기다린 뒤에 한 번 더 본다

    시간만 지나면 "마감을 지났다"가 아니다. 발사에 들어간 태스크는 `_claim`이 창을
    `_windows`에서 빼 가서 목록에는 없지만 여전히 도는 중이다 — 그 상태로 명령 수를 세면
    아직 안 나간 것을 안 나갈 것으로 읽는다. 그래서 창 목록과 발사 태스크가 **둘 다 빈**
    것까지 확인하고 돌아온다. 이 단언이 서야 뒤따르는 부정 단언이 뜻을 갖는다.
    """
    await asyncio.sleep(hold_sec + margin)
    drained = await _wait_until(
        lambda: not dispatch_mod._windows and not dispatch_mod._firing_tasks,
        timeout=5.0,
    )
    assert drained, (
        "마감을 지났는데 대기 창이나 발사 태스크가 남아 있다 — "
        f"windows={sorted(dispatch_mod._windows)} firing={len(dispatch_mod._firing_tasks)}. "
        "이 상태로 '명령이 안 나갔다'를 세면 아직 안 나갔을 뿐인 걸 통과로 읽는다."
    )


# ── 픽스처 ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def stub_weather(monkeypatch):
    """날씨 조회를 갈아 끼운다. 기본은 맑음이라 기본 목적지가 실외로 정해진다.

    `stub_weather["reading"]`에 다른 값을 넣으면 그때부터 그 값이 나온다.
    """
    box: dict[str, Any] = {"reading": _reading(WeatherStatus.CLEAR), "calls": 0}

    async def _fake() -> WeatherReading:
        box["calls"] += 1
        return box["reading"]

    monkeypatch.setattr(dispatch_mod, "fetch_current_weather", _fake)
    return box


@pytest.fixture
def hold(monkeypatch):
    """대기 창 길이를 이 케이스에서만 바꾼다.

    설정 인스턴스에 속성을 직접 대입하지 않고 env+cache_clear로 간다 — 캐시를 비우는
    픽스처가 뒤에 붙으면 속성 대입은 조용히 사라진다(test_robot_channel.py 실측).
    """

    def _set(seconds: float) -> float:
        monkeypatch.setenv("SHUTTLE_DISPATCH_HOLD_SEC", str(seconds))
        get_settings.cache_clear()
        return seconds

    try:
        yield _set
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def env_setting(monkeypatch):
    """설정 하나를 이 케이스에서만 바꾼다(env+cache_clear, `hold`와 같은 방식)."""

    def _set(name: str, value: str) -> None:
        monkeypatch.setenv(name, value)
        get_settings.cache_clear()

    try:
        yield _set
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def commands(monkeypatch):
    """로봇으로 나간 명령을 모은다. 로봇 한 대가 붙어 있는 것처럼 군다."""
    box: list[dict] = []

    async def _send(command, robot_id=None):
        box.append(command)
        return ["jetson01"]

    monkeypatch.setattr(robot_manager, "send_command", _send)
    return box


@pytest.fixture
def sent(monkeypatch):
    """대시보드로 나간 WS 메시지를 모은다."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


def _results(sent: list[dict]) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == "shuttle_dispatch_result"]


async def _call_shuttle(client, gate_no: int) -> dict:
    res = await client.post("/api/shuttle-calls", json={"gate_no": gate_no})
    assert res.status_code == 200, res.text
    return res.json()


async def _arrival_id(event_id: str) -> int:
    async with get_session() as session:
        return (
            await session.execute(
                select(ShuttleArrival.id).where(ShuttleArrival.event_id == event_id)
            )
        ).scalar_one()


async def _discarded(arrival_id: int) -> int:
    """그 셔틀 신호가 짝짓기 목록에서 빠졌나(만료 표식 수)."""
    async with get_session() as session:
        rows = (
            await session.execute(
                select(Alert).where(
                    Alert.type == ARRIVAL_EXPIRED_ALERT_TYPE,
                    Alert.source_id == arrival_id,
                )
            )
        ).scalars().all()
    return len(rows)


def _destination_commands(commands: list[dict]) -> list[dict]:
    return [c for c in commands if c["type"] == "cmd_destination"]


# ── 대기 창 ────────────────────────────────────────────────────────────────

async def test_셔틀이_와도_명령이_바로_안_나간다(client, commands, sent, hold):
    """반증 자리 — 수리 전에는 이 자리에서 목적지 명령이 그대로 나갔다."""
    hold(5.0)
    body = await _call_shuttle(client, 31)

    assert commands == [], "대기 창이 열렸는데 명령이 벌써 나갔다"
    assert body["notified_robot_count"] == 0
    pending = [m for m in sent if m["type"] == "shuttle_arrival"][-1]["data"]["pending"]
    assert pending["arrival_id"] == await _arrival_id(body["event_id"])
    assert pending["hold_sec"] == 5.0
    assert pending["choices"] == ["INDOOR_TAGGING", "OUTDOOR_TAGGING", "HOLD"]
    assert 0 < pending["remain_sec"] <= 5.0


async def test_아무도_안_고르면_기본목적지로_자동_발사한다(client, commands, sent, hold):
    """5초가 그냥 지나면 그날 기본값으로 나간다(설계 §2.2-5)."""
    hold(0.2)
    body = await _call_shuttle(client, 32)

    assert await _wait_until(lambda: bool(commands)), "대기 창이 지났는데 명령이 안 나갔다"
    assert commands[0]["type"] == "cmd_destination"
    # 맑음이라 실외다(stub_weather 기본값).
    assert commands[0]["cmd_destination"] == "OUTDOOR_TAGGING"
    assert commands[0]["command_id"] == body["command_id"]

    assert await _wait_until(lambda: bool(_results(sent)))
    result = _results(sent)[-1]
    assert result["source"] == "auto"
    assert result["destination"] == "OUTDOOR_TAGGING"
    assert result["notified_robot_count"] == 1
    assert result["event_id"] == body["event_id"]

    # 자동 발사도 통지 기록을 남긴다 — 안 남기면 재전송 판정이 "안 나갔다"로 읽어 두 번 낸다.
    async with get_session() as session:
        row = (
            await session.execute(
                select(ShuttleArrival).where(ShuttleArrival.event_id == body["event_id"])
            )
        ).scalars().one()
    assert row.notified_robot_id is not None


async def test_비가_오면_자동_발사가_실내로_간다(client, commands, hold, stub_weather):
    stub_weather["reading"] = _reading(WeatherStatus.LIGHT_RAIN, precip=1.2)
    hold(0.2)
    await _call_shuttle(client, 33)

    assert await _wait_until(lambda: bool(commands))
    assert commands[0]["cmd_destination"] == "INDOOR_TAGGING"


async def test_요원이_고르면_그_목적지로_바로_나간다(client, commands, sent, hold):
    """고른 순간 나간다. 남은 초를 채우고 나가면 시연에서 "왜 안 가지"로 보인다."""
    hold(5.0)
    body = await _call_shuttle(client, 34)
    arrival_id = await _arrival_id(body["event_id"])

    res = await client.post(
        f"/api/dispatch/pending/{arrival_id}/choose",
        json={"choice": "INDOOR_TAGGING"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "manual"
    assert res.json()["destination"] == "INDOOR_TAGGING"

    assert len(commands) == 1, "선택이 명령을 안 냈거나 두 번 냈다"
    assert commands[0]["cmd_destination"] == "INDOOR_TAGGING"
    assert _results(sent)[-1]["source"] == "manual"


async def test_선택한_뒤에는_타이머가_두_번_안_쏜다(client, commands, hold):
    """창을 닫았는데 타이머가 살아 있으면 로봇이 명령을 두 번 받는다."""
    window = hold(0.6)
    body = await _call_shuttle(client, 35)
    arrival_id = await _arrival_id(body["event_id"])

    res = await client.post(
        f"/api/dispatch/pending/{arrival_id}/choose",
        json={"choice": "OUTDOOR_TAGGING"},
    )
    assert res.status_code == 200
    await _past_deadline(window)
    assert len(commands) == 1, "취소된 타이머가 명령을 또 냈다"


async def test_대기를_고르면_명령이_안_나가고_신호를_버린다(client, commands, sent, hold):
    """"대기"는 이번 셔틀을 통째로 넘기는 선택이다. 그 신호는 나중에 되살리지 않는다."""
    window = hold(0.6)
    body = await _call_shuttle(client, 36)
    arrival_id = await _arrival_id(body["event_id"])

    res = await client.post(
        f"/api/dispatch/pending/{arrival_id}/choose", json={"choice": "HOLD"}
    )
    assert res.status_code == 200, res.text
    outcome = res.json()
    assert outcome["source"] == "hold_cancelled"
    assert outcome["command"] is None and outcome["destination"] is None
    assert outcome["notified_robot_count"] == 0

    await _past_deadline(window)
    assert commands == [], "대기를 골랐는데 명령이 나갔다"

    # 버린 신호는 짝짓기 목록에서 빠져야 한다. 안 빼면 나중에 온 도착이 여기 붙어 게이트가
    # 뒤바뀌고, TTL이 지날 때까지 미짝 목록 맨 앞을 차지한다.
    async with get_session() as session:
        closed = (
            await session.execute(
                select(Alert).where(
                    Alert.type == ARRIVAL_EXPIRED_ALERT_TYPE,
                    Alert.source_id == arrival_id,
                )
            )
        ).scalars().all()
    assert len(closed) == 1, "버린 셔틀 신호가 미짝 목록에 그대로 남았다"


async def test_즉시_이동이_대기창을_이긴다(client, commands, sent, hold):
    """5초 창이 떠 있어도 즉시 이동이 이긴다(사용자 확정, 설계 §2.4)."""
    window = hold(0.6)
    body = await _call_shuttle(client, 37)

    res = await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )
    assert res.status_code == 200, res.text
    outcome = res.json()
    assert outcome["source"] == "immediate"
    assert outcome["destination"] == "INDOOR_TAGGING"
    # 창에 매인 신호로 나가야 도착 짝짓기·ETA가 안 끊긴다.
    assert outcome["event_id"] == body["event_id"]

    await _past_deadline(window)
    assert len(commands) == 1, "즉시 이동으로 닫은 창의 타이머가 또 쐈다"
    assert commands[0]["cmd_destination"] == "INDOOR_TAGGING"


async def test_창이_없을_때의_즉시_이동은_단독_명령이다(client, commands, hold):
    hold(5.0)
    res = await client.post(
        "/api/dispatch/commands/move", json={"destination": "OUTDOOR_TAGGING"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["arrival_id"] is None
    assert len(commands) == 1
    assert commands[0]["command_id"].startswith("move-")


async def test_닫힌_창에_다시_고르면_404(client, commands, hold):
    """5초가 지났거나 딴 사람이 먼저 골랐다는 뜻이다. 화면은 "이미 결정됐습니다"로 그린다."""
    hold(0.2)
    body = await _call_shuttle(client, 38)
    arrival_id = await _arrival_id(body["event_id"])

    assert await _wait_until(lambda: bool(commands))
    late = await client.post(
        f"/api/dispatch/pending/{arrival_id}/choose",
        json={"choice": "INDOOR_TAGGING"},
    )
    assert late.status_code == 404, late.text
    assert len(commands) == 1, "닫힌 창에 대한 선택이 명령을 또 냈다"


async def test_대기창_상태를_창구로_되살린다(client, hold):
    """화면이 5초 사이에 새로고침되면 WS 알림을 이미 놓친 뒤다."""
    hold(5.0)
    body = await _call_shuttle(client, 39)

    state = await client.get("/api/dispatch/state")
    assert state.status_code == 200, state.text
    payload = state.json()
    assert len(payload["pending"]) == 1
    assert payload["pending"][0]["event_id"] == body["event_id"]
    assert payload["pending"][0]["remain_sec"] > 0
    assert payload["emergency_stop"] is False


async def test_자동_발사된_신호에도_로봇_도착이_붙는다(client, sent, hold):
    """대기 창을 거쳐 나간 명령도 짝짓기 근거를 남기나.

    `notify_shuttle_arrival`을 그대로 타는지 보는 자리다. 여기서 새로 쏘면 CommandLink가
    안 남아 ETA 표본이 통째로 끊긴다.
    """
    hold(0.2)

    class _Sock:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def accept(self) -> None:
            return None

        async def send_json(self, message: dict) -> None:
            self.sent.append(message)

    sock = _Sock()
    await robot_manager.connect(sock)
    try:
        await handle_robot_frame(
            sock,
            {
                "type": "robot_state",
                "version": "1",
                "data": {"robot_id": "jetson01", "status_summary": {"battery": 90}},
            },
        )
        body = await _call_shuttle(client, 40)
        arrival_id = await _arrival_id(body["event_id"])
        assert await _wait_until(
            lambda: any(m.get("type") == "command" for m in sock.sent)
        ), "대기 창이 지났는데 명령이 로봇까지 안 갔다"

        await handle_robot_frame(
            sock,
            {
                "type": "robot_state",
                "version": "1",
                "data": {"robot_id": "jetson01", "mission_status": "ARRIVED"},
            },
        )
    finally:
        robot_manager.disconnect(sock)

    notice = [m for m in sent if m["type"] == "robot_arrival"]
    assert notice, [m["type"] for m in sent]
    assert notice[-1]["data"]["shuttle_arrival_id"] == arrival_id


# ── 날씨 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "code, expected",
    [
        (113, WeatherStatus.CLEAR),
        (119, WeatherStatus.CLOUDY),
        (296, WeatherStatus.LIGHT_RAIN),
        (308, WeatherStatus.HEAVY_RAIN),
        (338, WeatherStatus.SNOW),
        (320, WeatherStatus.SNOW),      # 진눈깨비도 실내 판정이 맞다
        (999, WeatherStatus.UNKNOWN),   # 모르는 코드
        (None, WeatherStatus.UNKNOWN),
    ],
)
async def test_날씨코드가_계약_여섯값으로_옮겨진다(code, expected):
    assert weather_mod.classify(code) is expected


async def test_wttr_본문을_파싱한다():
    """2026-07-31 실측 본문과 같은 모양(설계 §5.1)."""
    reading = weather_mod.parse_wttr_j1(
        {
            "current_condition": [
                {
                    "weatherCode": "113",
                    "weatherDesc": [{"value": "Sunny"}],
                    "precipMM": "0.0",
                    "temp_C": "32",
                }
            ]
        }
    )
    assert (reading.status, reading.description) == (WeatherStatus.CLEAR, "Sunny")
    assert (reading.precip_mm, reading.temp_c) == (0.0, 32)
    assert reading.ok is True and reading.precipitating is False


async def test_본문_모양이_다르면_UNKNOWN이다():
    """파싱 실패는 조회 실패와 같은 자리다 — 예외로 올리면 셔틀 출동이 멈춘다."""
    broken = weather_mod.parse_wttr_j1({"nope": []})
    assert broken.status is WeatherStatus.UNKNOWN and broken.ok is False


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _fake_client(payload: dict | None = None, error: Exception | None = None):
    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> bool:
            return False

        async def get(self, url):
            if error is not None:
                raise error
            return _FakeResponse(payload or {})

    return _Client


async def test_조회가_죽으면_마지막_성공값을_쓴다(monkeypatch):
    """공식 서비스가 아니라 시연 중에 죽을 수 있다(설계 §5.2)."""
    weather_mod.reset_weather_cache()
    good = {
        "current_condition": [
            {"weatherCode": "296", "weatherDesc": [{"value": "Light rain"}],
             "precipMM": "1.4", "temp_C": "24"}
        ]
    }
    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _fake_client(good))
    first = await weather_mod.fetch_current_weather()
    assert (first.status, first.ok, first.stale) == (WeatherStatus.LIGHT_RAIN, True, False)

    monkeypatch.setattr(
        weather_mod.httpx, "AsyncClient", _fake_client(error=RuntimeError("망 끊김"))
    )
    fallback = await weather_mod.fetch_current_weather()
    assert fallback.status is WeatherStatus.LIGHT_RAIN, "마지막 성공값을 안 썼다"
    assert (fallback.ok, fallback.stale) == (False, True), "폴백이라는 사실이 안 드러난다"
    # 폴백 값도 판정에 그대로 쓰인다 — 비였으니 실내다.
    assert dispatch_mod.destination_for(fallback) is Destination.INDOOR_TAGGING


async def test_성공한_적_없으면_UNKNOWN이고_실외로_간다(monkeypatch):
    """시연 경로가 실외 하나뿐이라 모르면 실외가 안전하다(설계 §3)."""
    weather_mod.reset_weather_cache()
    monkeypatch.setattr(
        weather_mod.httpx, "AsyncClient", _fake_client(error=RuntimeError("망 끊김"))
    )
    reading = await weather_mod.fetch_current_weather()
    assert (reading.status, reading.ok, reading.stale) == (WeatherStatus.UNKNOWN, False, False)
    assert dispatch_mod.destination_for(reading) is Destination.OUTDOOR_TAGGING


# ── 그날 기본 목적지 ───────────────────────────────────────────────────────

async def test_그날_첫_조회가_날씨로_정하고_DB에_남는다(client, stub_weather):
    stub_weather["reading"] = _reading(WeatherStatus.SNOW)
    res = await client.get("/api/dispatch/default-destination")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["destination"] == "INDOOR_TAGGING"
    assert body["source"] == "auto"
    assert body["weather"]["status"] == "SNOW"
    assert body["weather"]["ok"] is True

    async with get_session() as session:
        rows = (
            await session.execute(select(DailyDefaultDestination))
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].service_date == dispatch_mod.kst_today()


async def test_두_번째_조회는_날씨를_다시_안_받는다(client, stub_weather):
    """lazy 갱신은 그날 한 번이다. 매 조회마다 바깥을 타면 화면이 느려진다."""
    await client.get("/api/dispatch/default-destination")
    await client.get("/api/dispatch/default-destination")
    assert stub_weather["calls"] == 1


async def test_요원_선택이_그날_자동조회를_이긴다(client, stub_weather):
    """설계 §2.1-3. 손으로 고른 값을 날씨가 덮으면 요원 조작이 무의미해진다."""
    manual = await client.post(
        "/api/dispatch/default-destination", json={"destination": "INDOOR_TAGGING"}
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["source"] == "manual"

    # 자동 조회가 다시 돌 자리(맑음이라 실외로 바꾸려 든다)를 태워도 안 바뀐다.
    stub_weather["reading"] = _reading(WeatherStatus.CLEAR)
    again = await client.get("/api/dispatch/default-destination")
    assert again.json()["destination"] == "INDOOR_TAGGING", "날씨가 요원 선택을 덮었다"
    assert again.json()["source"] == "manual"
    assert stub_weather["calls"] == 0, "행이 있는데도 날씨를 조회했다"


async def test_자동으로_정한_뒤에도_요원이_덮을_수_있다(client, stub_weather):
    stub_weather["reading"] = _reading(WeatherStatus.CLEAR)
    auto = await client.get("/api/dispatch/default-destination")
    assert (auto.json()["destination"], auto.json()["source"]) == ("OUTDOOR_TAGGING", "auto")

    manual = await client.post(
        "/api/dispatch/default-destination", json={"destination": "INDOOR_TAGGING"}
    )
    assert (manual.json()["destination"], manual.json()["source"]) == ("INDOOR_TAGGING", "manual")
    # 날씨 근거는 그대로 남는다 — 그날 무슨 날씨였는지는 여전히 사실이다.
    assert manual.json()["weather"]["status"] == "CLEAR"

    async with get_session() as session:
        rows = (await session.execute(select(DailyDefaultDestination))).scalars().all()
    assert len(rows) == 1, "덮어쓰기가 아니라 행을 새로 만들었다"


async def test_기본목적지는_재기동에도_살아남는다(client, stub_weather):
    """인메모리면 서버가 한 번만 다시 떠도 그날의 결정이 사라진다(설계 §2.1-4)."""
    await client.post(
        "/api/dispatch/default-destination", json={"destination": "INDOOR_TAGGING"}
    )
    # 재기동과 같은 자리 — 인메모리 상태를 전부 비운다.
    dispatch_mod.reset_dispatch_state()
    weather_mod.reset_weather_cache()

    async with get_session() as session:
        stored = await dispatch_mod.peek_today_default(session)
    assert stored is not None and stored.destination == "INDOOR_TAGGING"
    assert stored.source == "manual"


async def test_자동_발사가_기본목적지를_따른다(client, commands, hold, stub_weather):
    """요원이 아침에 실내로 고쳐 뒀으면 자동 발사도 실내로 간다."""
    await client.post(
        "/api/dispatch/default-destination", json={"destination": "INDOOR_TAGGING"}
    )
    stub_weather["reading"] = _reading(WeatherStatus.CLEAR)
    hold(0.2)
    await _call_shuttle(client, 41)

    assert await _wait_until(lambda: bool(commands))
    assert commands[0]["cmd_destination"] == "INDOOR_TAGGING"


async def test_KST_날짜는_오프셋을_더해서_만든다():
    """⚠ ISO 문자열의 Z를 +09:00으로 바꿔치면 같은 순간이 9시간 다른 시각이 된다.

    UTC 15시는 KST로 다음 날 0시다. 문자열 치환이면 같은 날 15시로 읽혀 날짜가 안 넘어간다.
    """
    utc_evening = dt.datetime(2026, 7, 31, 15, 30, tzinfo=dt.timezone.utc)
    assert dispatch_mod.kst_today(utc_evening) == dt.date(2026, 8, 1)

    utc_morning = dt.datetime(2026, 7, 31, 14, 59, tzinfo=dt.timezone.utc)
    assert dispatch_mod.kst_today(utc_morning) == dt.date(2026, 7, 31)

    # naive 시각은 UTC로 읽는다(달라지면 하루가 통째로 밀린다).
    assert dispatch_mod.kst_today(dt.datetime(2026, 7, 31, 15, 30)) == dt.date(2026, 8, 1)


# ── 복귀·긴급정지 ──────────────────────────────────────────────────────────

async def test_복귀_명령이_계약대로_나간다(client, commands, sent):
    res = await client.post("/api/dispatch/commands/return")
    assert res.status_code == 200, res.text
    assert commands[0]["type"] == "return_to_charge"
    assert commands[0]["return_to_charge"] is True
    assert "cmd_destination" not in commands[0]
    assert res.json()["destination"] == "CHARGING_STATION"
    assert _results(sent)[-1]["command"] == "return_to_charge"


# ── 게이트 스피커 출발 안내 (2026-08-06 라파이 요청) ───────────────────────


def _sound_kinds(device_id: str = "sound-depart") -> list[str]:
    from app.device_sound import take_sounds

    return [s.kind for s in take_sounds(device_id)]


async def test_복귀_명령이_나가면_출발_안내를_큐에_넣는다(client, commands):
    """C→A 복귀도 `robot_departure` 하나를 쓴다(방향을 안 가른다)."""
    from app.device_sound import KIND_ROBOT_DEPARTURE, reset_sound_queue

    reset_sound_queue()
    res = await client.post("/api/dispatch/commands/return")
    assert res.status_code == 200, res.text
    assert res.json()["notified_robot_count"] > 0, "로봇에 안 닿았으면 이 케이스가 무의미하다"

    assert _sound_kinds() == [KIND_ROBOT_DEPARTURE]


async def test_즉시_이동도_같은_출발_안내를_쓴다(client, commands):
    """A→C 출동과 C→A 복귀가 같은 낱말이다 — 음성 문구가 방향을 안 말한다."""
    from app.device_sound import KIND_ROBOT_DEPARTURE, reset_sound_queue

    reset_sound_queue()
    res = await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )
    assert res.status_code == 200, res.text

    assert _sound_kinds() == [KIND_ROBOT_DEPARTURE]


async def test_긴급정지는_출발_안내를_안_낸다(client, commands):
    """⛔ 세우는 명령이라 "출발합니다"가 나가면 정반대를 말한다."""
    from app.device_sound import reset_sound_queue

    reset_sound_queue()
    res = await client.post(
        "/api/dispatch/commands/emergency-stop", json={"engage": True}
    )
    assert res.status_code == 200, res.text

    assert _sound_kinds() == [], "긴급정지에 출발 안내가 붙었다"


async def test_로봇이_안_붙었으면_출발_안내를_안_낸다(client):
    """⚠ 아무것도 안 움직이는데 게이트에서 "출발합니다"가 울리면 거짓말이다.

    `commands` 픽스처를 일부러 안 받는다 — 붙은 로봇이 0인 상태를 만들려는 것이다.
    """
    from app.device_sound import reset_sound_queue

    reset_sound_queue()
    res = await client.post("/api/dispatch/commands/return")
    assert res.status_code == 200, res.text
    assert res.json()["notified_robot_count"] == 0, "이 케이스는 로봇이 0이어야 뜻이 있다"

    assert _sound_kinds() == []


async def test_긴급정지_결과는_즉시이동으로_안_적힌다(client, commands, sent):
    """⛔ 반증 자리 — 수리 전에는 `source`가 `immediate`라, 화면이 그것을 사람 말로 옮기며
    **"즉시 이동으로 대신 보냈습니다"**라고 적었다(2026-08-06 프론트 27차 · 사용자가 화면에서
    잡았다). 누른 적 없는 버튼을 눌렀다고 말하는 셈이었다.

    ⚠ 값을 글자로 박는다. 화면이 이 문자열로 문구를 고르므로 이름이 바뀌면 시험이 깨져야
    맞다 — 상수를 import해 비교하면 이름을 바꿔도 초록이라 계약을 못 지킨다.
    """
    for engage in (True, False):
        res = await client.post(
            "/api/dispatch/commands/emergency-stop", json={"engage": engage}
        )
        assert res.status_code == 200, res.text

    sources = [r["source"] for r in _results(sent)]
    assert sources == ["emergency_stop", "emergency_stop"], sources
    assert "immediate" not in sources, "비상 정지가 즉시 이동으로 기록됐다"
    # 셔틀을 아무 데도 안 보낸 것이다 — 목적지가 실리면 화면이 또 갈 곳을 말한다.
    assert all(r["destination"] is None for r in _results(sent))


async def test_긴급정지를_세우고_푼다(client, commands):
    """푸는 자리가 없으면 시연 중 한 번 누르고 끝난다(설계 §2.4)."""
    stop = await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})
    assert stop.status_code == 200, stop.text
    assert commands[-1]["type"] == "emergency_stop"
    assert commands[-1]["emergency_stop"] is True
    assert (await client.get("/api/dispatch/state")).json()["emergency_stop"] is True

    release = await client.post(
        "/api/dispatch/commands/emergency-stop", json={"engage": False}
    )
    assert release.status_code == 200, release.text
    assert commands[-1]["emergency_stop"] is False
    assert (await client.get("/api/dispatch/state")).json()["emergency_stop"] is False
    assert len(commands) == 2


async def test_긴급정지는_본문_없이도_세워진다(client, commands):
    """⭐ 칸이 `engage` 하나뿐이라 화면이 본문을 안 실을 이유가 충분하다.

    파라미터에 기본값이 없으면 FastAPI가 본문을 필수로 봐서 `fetch(url, {method:'POST'})`
    한 줄이 **422**였다(축④ ⑨). "막히면 안 되는 창구"라고 적어 놓고 가장 흔한 호출 모양이
    막히던 자리다.
    """
    res = await client.post("/api/dispatch/commands/emergency-stop")

    assert res.status_code == 200, res.text
    assert res.json()["emergency_stop"] is True
    assert commands[-1]["type"] == "emergency_stop"
    assert (await client.get("/api/dispatch/state")).json()["emergency_stop"] is True

    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": False})


async def test_창_정리가_터져도_정지_프레임은_나간다(client, commands, monkeypatch):
    """⭐ 서버는 정지 상태인데 로봇은 정지 프레임을 못 받는 반쪽 상태를 막는다(축⑤ D3).

    플래그를 먼저 세우는 순서는 그대로 옳다(그 await 구간에 들어온 이동 요청을 막는다).
    문제는 가운데 DB 정리가 터졌을 때 500이 나가면서 **정지가 로봇에 안 갔다**는 것이다.
    """
    async def _boom(session, why):
        raise RuntimeError("대기 창 정리가 터졌다")

    monkeypatch.setattr(dispatch_mod, "_cancel_windows_for_immediate", _boom)

    res = await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})

    assert res.status_code == 200, res.text
    assert commands[-1]["type"] == "emergency_stop"
    assert commands[-1]["emergency_stop"] is True, "로봇에 정지가 안 갔다"
    assert dispatch_mod.emergency_stopped() is True, "세운 플래그를 되돌리면 더 위험하다"

    monkeypatch.undo()
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": False})


async def test_긴급정지가_대기창을_취소한다(client, commands, hold):
    """세워 놓고 5초 뒤에 출동 명령이 나가면 안 된다."""
    window = hold(0.6)
    await _call_shuttle(client, 42)
    res = await client.post(
        "/api/dispatch/commands/emergency-stop", json={"engage": True}
    )
    assert res.status_code == 200

    await _past_deadline(window)
    assert len(commands) == 1, "긴급정지 뒤에 대기 창 타이머가 목적지 명령을 냈다"
    assert commands[0]["type"] == "emergency_stop"


# ── 긴급정지가 걸린 뒤에는 무엇도 안 움직인다 (2026-07-31 검증 P1) ─────────
# 예전에는 긴급정지 상태를 `GET /api/dispatch/state` 응답에서만 읽었다. 그래서 세워 놓은
# 뒤에 들어온 셔틀이 대기 창을 열고 5초 뒤에 그대로 목적지 명령을 쐈다.

async def test_긴급정지_중에_온_셔틀은_창도_안_열리고_명령도_안_나간다(
    client, commands, sent, hold
):
    """반증 자리 — 수리 전에는 여기서 ['emergency_stop', 'cmd_destination']이 나갔다."""
    window = hold(0.3)
    stop = await client.post(
        "/api/dispatch/commands/emergency-stop", json={"engage": True}
    )
    assert stop.status_code == 200, stop.text

    body = await _call_shuttle(client, 51)
    arrival_id = await _arrival_id(body["event_id"])

    # 대기 창이 아예 안 열린다(카운트다운도 없다).
    arrival_msg = [m for m in sent if m["type"] == "shuttle_arrival"][-1]["data"]
    assert arrival_msg["pending"] is None, "긴급정지 중인데 대기 창이 열렸다"
    assert (await client.get("/api/dispatch/state")).json()["pending"] == []

    await _past_deadline(window)
    assert _destination_commands(commands) == [], "세운 로봇에 목적지 명령이 나갔다"
    assert [c["type"] for c in commands] == ["emergency_stop"]

    blocked = _results(sent)[-1]
    assert blocked["source"] == "emergency_blocked"
    assert blocked["command"] is None and blocked["destination"] is None
    assert blocked["arrival_id"] == arrival_id
    # 안 가는 신호는 짝짓기 목록에서 빼야 뒤에 온 도착이 여기 안 붙는다.
    assert await _discarded(arrival_id) == 1


async def test_창이_도는_중에_긴급정지가_걸리면_자동발사가_스스로_접는다(
    client, commands, sent, hold, monkeypatch
):
    """발사 직전 재검사 자리. 창을 열 때 한 번 본 걸로 끝내면 5초 사이가 구멍이 된다.

    창구를 거치면 창이 그 자리에서 취소되니, 여기서는 상태만 바꿔 **두 번째 관문**을 본다.
    """
    hold(0.4)
    body = await _call_shuttle(client, 52)
    arrival_id = await _arrival_id(body["event_id"])

    monkeypatch.setattr(dispatch_mod, "_emergency_stopped", True)

    assert await _wait_until(lambda: bool(_results(sent)))
    assert _destination_commands(commands) == [], "긴급정지 중인데 자동 발사가 나갔다"
    assert _results(sent)[-1]["source"] == "emergency_blocked"
    assert await _discarded(arrival_id) == 1


async def test_목적지를_구하는_사이에_걸린_긴급정지도_자동발사를_접는다(
    client, commands, sent, hold, monkeypatch
):
    """세 번째 관문(F32·F33). 마감 직후 검사와 실제 발사 사이가 구멍이었다.

    그 사이에 미리 조회 join(최대 0.5초)과 DB 왕복이 낀다. 이 태스크는 이미 `_claim`으로
    `_windows`에서 빠져 있어서 긴급정지가 창을 취소해도 **이 태스크는 안 죽는다** — 그래서
    정지 프레임 뒤에 목적지 명령이 꽂혔다. 여기서는 목적지를 돌려주는 그 순간에 긴급정지를
    걸어, 발사 바로 앞 재검사가 정말 있는지 본다.
    """
    hold(0.3)
    real = dispatch_mod.destination_without_fetch

    async def _stop_while_deciding(session, **kwargs):
        destination = await real(session, **kwargs)
        # 목적지는 다 구했다. 요원이 지금 긴급정지를 눌렀다.
        dispatch_mod._emergency_stopped = True
        return destination

    monkeypatch.setattr(dispatch_mod, "destination_without_fetch", _stop_while_deciding)

    body = await _call_shuttle(client, 61)
    arrival_id = await _arrival_id(body["event_id"])

    assert await _wait_until(lambda: bool(_results(sent)))
    assert _destination_commands(commands) == [], "긴급정지가 걸렸는데 자동 발사가 나갔다"
    assert _results(sent)[-1]["source"] == "emergency_blocked"
    assert await _discarded(arrival_id) == 1


async def test_긴급정지_세우는_도중에_들어온_이동은_막힌다(client, sent, monkeypatch):
    """반증 자리(F32) — 플래그를 명령 전송 **뒤에** 세우던 시절의 구멍.

    세우기는 창 정리(DB 커밋)와 WS 송신을 `await`한다. 그 사이에 들어온 이동 요청이
    `_require_not_stopped`를 그냥 지나서, 로봇 입장에선 정지 프레임 **뒤에** 목적지 명령이
    도착했다. 지금은 세울 때 플래그를 먼저 세우고, 풀 때는 프레임이 나간 뒤에 내린다.
    """
    box: list[dict] = []
    seen: dict[str, Any] = {}

    async def _send(command, robot_id=None):
        box.append(command)
        if command["type"] == "emergency_stop":
            # 프레임이 나가는 이 순간의 상태를 본다.
            seen[str(command["emergency_stop"])] = dispatch_mod.emergency_stopped()
            if command["emergency_stop"]:
                async with get_session() as session:
                    try:
                        await dispatch_mod.dispatch_immediate_move(
                            session, Destination.INDOOR_TAGGING
                        )
                        seen["move_blocked"] = False
                    except dispatch_mod.EmergencyStopEngaged:
                        seen["move_blocked"] = True
        return ["jetson01"]

    monkeypatch.setattr(robot_manager, "send_command", _send)

    async with get_session() as session:
        await dispatch_mod.dispatch_emergency_stop(session, engage=True)
    assert seen["True"] is True, "정지 프레임이 나가는 동안 아직 안 세워져 있었다"
    assert seen["move_blocked"] is True, "세우는 도중에 들어온 이동이 통과했다"

    async with get_session() as session:
        await dispatch_mod.dispatch_emergency_stop(session, engage=False)
    # 해제는 반대다 — 프레임이 나간 뒤에 내려야 그 틈으로 이동이 먼저 안 나간다.
    assert seen["False"] is True, "해제 프레임보다 플래그가 먼저 내려갔다"
    assert dispatch_mod.emergency_stopped() is False

    assert [c["type"] for c in box] == ["emergency_stop", "emergency_stop"], (
        "막혔어야 할 이동 명령이 로봇으로 나갔다"
    )


async def test_대기창이_꺼져_있어도_확정_결과가_한_장_나간다(client, commands, sent, hold):
    """계약 §0 "어느 갈래로 닫히든 한 장"(F38). 이 갈래만 결과를 안 냈다.

    순서도 같이 본다 — `shuttle_arrival`이 먼저고 결과가 뒤다(§2.1). 결과가 앞서면 화면이
    아직 모르는 신호의 확정을 먼저 받는다.
    """
    hold(0)
    body = await _call_shuttle(client, 62)
    arrival_id = await _arrival_id(body["event_id"])

    assert _destination_commands(commands), "대기 창이 꺼졌는데 명령이 안 나갔다"
    results = _results(sent)
    assert len(results) == 1, "창을 안 여는 즉시 발사 갈래가 결과를 안 냈거나 두 번 냈다"
    assert results[0]["source"] == "hold_disabled"
    assert results[0]["destination"] == Destination.OUTDOOR_TAGGING.value
    assert results[0]["arrival_id"] == arrival_id
    assert results[0]["command_id"] == f"shuttle-{arrival_id}"
    assert results[0]["notified_robot_count"] == 1

    types = [m["type"] for m in sent]
    assert types.index("shuttle_arrival") < types.index("shuttle_dispatch_result")


async def test_긴급정지_차단_결과도_도착_알림_뒤에_온다(client, commands, sent, hold):
    """F37 — 예전에는 `shuttle_dispatch_result`가 `shuttle_arrival`을 앞질렀다."""
    hold(0.3)
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})
    sent.clear()

    await _call_shuttle(client, 63)

    types = [m["type"] for m in sent]
    assert "shuttle_arrival" in types and "shuttle_dispatch_result" in types
    assert types.index("shuttle_arrival") < types.index("shuttle_dispatch_result"), (
        "확정 결과가 도착 알림보다 먼저 나갔다"
    )
    assert _results(sent)[-1]["source"] == "emergency_blocked"


async def test_긴급정지_중_이동_창구는_전부_409다(client, commands, hold):
    """해제 전에는 사람이 눌러도 안 움직인다. 푸는 창구만 늘 열려 있다."""
    hold(5.0)
    body = await _call_shuttle(client, 53)
    arrival_id = await _arrival_id(body["event_id"])
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})

    move = await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )
    back = await client.post("/api/dispatch/commands/return")
    assert (move.status_code, back.status_code) == (409, 409), (move.text, back.text)

    # 창이 이미 취소됐으니 선택은 404인데, 창이 살아 있어도 실내·실외는 409여야 한다.
    async with get_session() as session:
        await dispatch_mod.begin_dispatch(session, arrival_id=arrival_id, gate_no=53)
    assert dispatch_mod.pending_window_open(arrival_id) is False, (
        "긴급정지 중인데 대기 창이 다시 열렸다"
    )

    with pytest.raises(dispatch_mod.EmergencyStopEngaged):
        async with get_session() as session:
            await dispatch_mod.choose_in_window(
                session, arrival_id=arrival_id, choice=dispatch_mod.WindowChoice.INDOOR_TAGGING
            )

    release = await client.post(
        "/api/dispatch/commands/emergency-stop", json={"engage": False}
    )
    assert release.status_code == 200, "해제까지 막히면 시연이 그 자리에서 끝난다"
    after = await client.post(
        "/api/dispatch/commands/move", json={"destination": "OUTDOOR_TAGGING"}
    )
    assert after.status_code == 200, after.text


async def test_긴급정지_중_대기_선택은_그대로_받는다(client, commands, hold, monkeypatch):
    """HOLD는 명령을 안 내는 선택이다. 세운 상태에서도 신호를 정리할 길은 열어 둔다."""
    hold(5.0)
    body = await _call_shuttle(client, 54)
    arrival_id = await _arrival_id(body["event_id"])
    monkeypatch.setattr(dispatch_mod, "_emergency_stopped", True)

    res = await client.post(
        f"/api/dispatch/pending/{arrival_id}/choose", json={"choice": "HOLD"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["source"] == "hold_cancelled"
    assert commands == []


async def test_긴급정지_중_재전송은_로봇을_안_깨운다(client, commands, auth_headers):
    """기기 재시도가 세운 로봇을 다시 움직이면 안 된다."""
    payload = {
        "event_id": "ESTOP-RESEND-1",
        "gate_no": 55,
        "signal_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    first = await client.post("/api/shuttle-arrivals", json=payload, headers=auth_headers)
    assert first.status_code == 200, first.text
    arrival_id = await _arrival_id(payload["event_id"])
    async with get_session() as session:
        await dispatch_mod.choose_in_window(
            session, arrival_id=arrival_id, choice=dispatch_mod.WindowChoice.HOLD
        )
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})
    commands.clear()

    again = await client.post("/api/shuttle-arrivals", json=payload, headers=auth_headers)
    assert again.status_code == 200, again.text
    assert _destination_commands(commands) == [], "긴급정지 중인데 재전송이 명령을 냈다"


# ── 창이 둘 이상일 때 (2026-07-31 검증 P1) ─────────────────────────────────
# 게이트가 다른 셔틀이 연달아 오면 창이 둘이다. 예전에는 즉시 명령이 창 하나만 닫아서
# 남은 창이 몇 초 뒤에 목적지 명령을 냈다 — 긴급정지에서는 세운 로봇이 다시 움직였다.

async def test_긴급정지가_열린_대기창_전부를_취소한다(client, commands, hold):
    """반증 자리 — 수리 전에는 창 둘 중 하나가 살아남아 2초 뒤에 목적지를 쐈다.

    ⚠ 창을 2초로 잡는다. 셔틀 호출 하나가 DB 왕복 여러 번이라, 창이 짧으면 두 번째 호출을
    보내기도 전에 첫 창이 스스로 터져서 "창이 둘"인 상황이 아예 안 만들어진다.
    """
    window = hold(2.0)
    first = await _call_shuttle(client, 62)
    second = await _call_shuttle(client, 63)
    assert len((await client.get("/api/dispatch/state")).json()["pending"]) == 2

    res = await client.post(
        "/api/dispatch/commands/emergency-stop", json={"engage": True}
    )
    assert res.status_code == 200, res.text

    await _past_deadline(window)
    assert _destination_commands(commands) == [], "남은 대기 창이 목적지 명령을 냈다"
    assert await _discarded(await _arrival_id(first["event_id"])) == 1
    assert await _discarded(await _arrival_id(second["event_id"])) == 1


async def test_즉시_이동이_대기창_둘을_다_닫는다(client, commands, sent, hold):
    """명령은 먼저 열린 창에 매이고 나머지 신호는 버린다."""
    window = hold(2.0)
    first = await _call_shuttle(client, 64)
    second = await _call_shuttle(client, 65)
    assert len((await client.get("/api/dispatch/state")).json()["pending"]) == 2

    res = await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["event_id"] == first["event_id"], "먼저 열린 창에 안 맸다"

    await _past_deadline(window)
    assert len(_destination_commands(commands)) == 1, "남은 대기 창이 명령을 또 냈다"
    assert await _discarded(await _arrival_id(second["event_id"])) == 1


async def test_복귀가_대기창_둘을_다_닫는다(client, commands, hold):
    window = hold(2.0)
    first = await _call_shuttle(client, 66)
    second = await _call_shuttle(client, 67)
    assert len((await client.get("/api/dispatch/state")).json()["pending"]) == 2

    res = await client.post("/api/dispatch/commands/return")
    assert res.status_code == 200, res.text

    await _past_deadline(window)
    assert [c["type"] for c in commands] == ["return_to_charge"]
    assert await _discarded(await _arrival_id(first["event_id"])) == 1
    assert await _discarded(await _arrival_id(second["event_id"])) == 1


# ── 기기 재시도와 대기 창 (2026-07-31 검증 P1) ──────────────────────────────

async def test_기기_재시도가_열린_대기창을_안_깬다(client, commands, hold, auth_headers):
    """반증 자리 — 수리 전에는 재시도가 창을 닫고 즉시 쐈다.

    그러면 화면은 카운트다운을 계속 그리는데 로봇은 이미 떠난 상태가 된다. 창이 살아 있다는
    건 "명령이 아직 나갈 예정"이지 "유실됐다"가 아니다.
    """
    hold(3.0)
    payload = {
        "event_id": "RETRY-IN-WINDOW-1",
        "gate_no": 56,
        "signal_ts": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    first = await client.post("/api/shuttle-arrivals", json=payload, headers=auth_headers)
    assert first.status_code == 200, first.text
    arrival_id = await _arrival_id(payload["event_id"])
    assert dispatch_mod.pending_window_open(arrival_id) is True, (
        "재시도를 보내기도 전에 창이 닫혔다 — 창을 더 길게 잡아라"
    )

    again = await client.post("/api/shuttle-arrivals", json=payload, headers=auth_headers)
    assert again.status_code == 200, again.text
    assert again.json()["duplicate"] is True
    assert again.json()["resent"] is False, "창이 도는 중인데 재전송으로 표시됐다"
    assert commands == [], "재시도가 대기 창을 깨고 즉시 쐈다"
    # 창이 그대로 살아 있어야 화면 카운트다운이 거짓말이 안 된다.
    assert dispatch_mod.pending_window_open(arrival_id) is True

    # 그리고 마감 때 딱 한 번 나간다(재시도가 마감을 뒤로 밀지도 않는다).
    assert await _wait_until(lambda: bool(commands))
    assert len(_destination_commands(commands)) == 1


# ── 창구 방어 (2026-07-31 검증 P2) ─────────────────────────────────────────

async def test_이동_연타는_429로_삼킨다(client, commands, env_setting):
    """인증이 없는 창구라 이게 유일한 유량 제한이다."""
    env_setting("DISPATCH_COMMAND_MIN_INTERVAL_SEC", "30")
    ok = await client.post(
        "/api/dispatch/commands/move", json={"destination": "OUTDOOR_TAGGING"}
    )
    assert ok.status_code == 200, ok.text
    blocked = await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )
    assert blocked.status_code == 429, blocked.text
    assert int(blocked.headers["Retry-After"]) >= 1
    assert len(commands) == 1, "연타가 로봇까지 그대로 갔다"

    # 복귀는 다른 통이라 같은 순간에도 받는다.
    assert (await client.post("/api/dispatch/commands/return")).status_code == 200


async def test_긴급정지는_연타_제한을_안_받는다(client, commands, env_setting):
    """세우기도 풀기도 막히면 안 되는 자리다."""
    env_setting("DISPATCH_COMMAND_MIN_INTERVAL_SEC", "30")
    for engage in (True, False, True):
        res = await client.post(
            "/api/dispatch/commands/emergency-stop", json={"engage": engage}
        )
        assert res.status_code == 200, res.text
    assert len(commands) == 3


async def test_키를_요구하게_켜면_401이다(client, env_setting, auth_headers):
    """공개 도메인에 그대로 두지 않을 수 있는 스위치가 있나(기본은 꺼짐)."""
    assert (await client.get("/api/dispatch/state")).status_code == 200

    env_setting("DISPATCH_REQUIRE_API_KEY", "true")
    assert (await client.get("/api/dispatch/state")).status_code == 401
    assert (
        await client.post(
            "/api/dispatch/commands/emergency-stop", json={"engage": True}
        )
    ).status_code == 401
    with_key = await client.get("/api/dispatch/state", headers=auth_headers)
    assert with_key.status_code == 200, with_key.text


# ── 발사 경로는 바깥 HTTP를 안 기다린다 (2026-07-31 검증 P2) ────────────────

async def test_자동_발사가_느린_날씨_서버를_안_기다린다(
    client, commands, hold, monkeypatch
):
    """반증 자리 — 수리 전에는 5초 창이 닫힌 뒤 날씨 타임아웃(4초)을 더 기다렸다."""
    started = asyncio.Event()

    async def _slow() -> WeatherReading:
        started.set()
        await asyncio.sleep(5.0)
        return _reading(WeatherStatus.LIGHT_RAIN, precip=2.0)

    monkeypatch.setattr(dispatch_mod, "fetch_current_weather", _slow)
    hold(0.2)

    began = time.monotonic()
    await _call_shuttle(client, 57)
    assert await _wait_until(lambda: bool(commands)), "명령이 아예 안 나갔다"
    elapsed = time.monotonic() - began

    assert started.is_set(), "미리 조회를 아예 안 띄웠다"
    # 창 + 봐주는 시간(PREFETCH_JOIN_SEC) + DB 왕복 여유. 수리 전이면 여기서 날씨 응답을
    # 통째로 기다려 5초를 넘긴다 — 여유를 넉넉히 줘도 갈래가 갈린다.
    budget = 0.2 + dispatch_mod.PREFETCH_JOIN_SEC + 1.5
    assert elapsed < budget, f"날씨 서버를 기다렸다({elapsed:.2f}초)"
    # 아직 아무 값도 못 받았으니 설정 기본값(실외)으로 나간다.
    assert commands[0]["cmd_destination"] == "OUTDOOR_TAGGING"


async def test_자정을_넘겨_만료되는_창은_열린_날의_기본값을_쓴다(
    client, commands, hold, monkeypatch
):
    """창이 23:59:58에 열려 자정을 넘겨 터지면, 그날(열린 날)의 결정이 그대로 나가야 한다.

    발사 시점에 날짜를 다시 계산하면 아직 아무것도 없는 "다음 날" 행을 찾아 기본값이
    통째로 뒤바뀐다.
    """
    await client.post(
        "/api/dispatch/default-destination", json={"destination": "INDOOR_TAGGING"}
    )
    hold(0.3)
    await _call_shuttle(client, 58)

    # 창이 도는 사이에 KST 날짜가 넘어간 상황.
    tomorrow = dispatch_mod.kst_today() + dt.timedelta(days=1)
    monkeypatch.setattr(dispatch_mod, "kst_today", lambda now=None: tomorrow)

    assert await _wait_until(lambda: bool(commands))
    assert commands[0]["cmd_destination"] == "INDOOR_TAGGING", (
        "자정을 넘기면서 그날의 결정이 날아갔다"
    )


# ── room_id 대칭 (2026-07-31 검증 P4) ──────────────────────────────────────

async def test_room_플래그를_켜면_확정_결과에도_room_id가_실린다(
    client, commands, sent, hold, env_setting
):
    """예전에는 `shuttle_arrival`에만 실리고 확정 결과엔 안 실려서 두 메시지를 못 묶었다."""
    env_setting("CREDIT_SCOPE_ROOM", "true")
    hold(5.0)
    body = await client.post(
        "/api/shuttle-calls", json={"gate_no": 59, "room_id": "A-101"}
    )
    assert body.status_code == 200, body.text
    arrival_id = await _arrival_id(body.json()["event_id"])

    arrival_msg = [m for m in sent if m["type"] == "shuttle_arrival"][-1]["data"]
    assert arrival_msg["room_id"] == "A-101"

    res = await client.post(
        f"/api/dispatch/pending/{arrival_id}/choose", json={"choice": "OUTDOOR_TAGGING"}
    )
    assert res.status_code == 200, res.text
    assert res.json()["room_id"] == "A-101"
    assert _results(sent)[-1]["room_id"] == "A-101", "확정 결과에 room_id가 없다"


# ── 명령 계약 ──────────────────────────────────────────────────────────────

async def test_나가는_명령이_command_schema를_통과한다(client, commands, hold):
    """계약 밖 칸·값이 로봇까지 내려가면 젯슨이 통째로 거절한다."""
    import json
    import pathlib

    import jsonschema

    schema = json.loads(
        (
            pathlib.Path(__file__).resolve().parents[1] / "schemas" / "command.schema.json"
        ).read_text(encoding="utf-8")
    )

    hold(0.2)
    await _call_shuttle(client, 43)
    assert await _wait_until(lambda: bool(commands))
    await client.post("/api/dispatch/commands/return")
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": False})
    await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )

    assert len(commands) == 5
    for command in commands:
        jsonschema.validate(command, schema)


async def test_명령_프레임에_안_쓴_칸이_안_실린다(client, commands, hold):
    """반증 자리 — `CommandOut`에 None 아닌 기본값이 붙거나 `exclude_none`이 빠지면 여기서 깨진다.

    정본이 `additionalProperties:false`라 빈 칸이 실리면 젯슨이 프레임을 통째로 거절한다.
    그런데 스키마 검증만으로는 못 잡는다 — `emergency_stop` 같은 칸은 `["boolean","null"]`이라
    복귀 명령에 `"emergency_stop": null`이 딸려 나가도 그대로 통과한다. 그래서 종류마다
    **칸 집합을 통째로** 못박는다. 모델이 `app/dispatch.py`와 `app/schemas.py` 두 자리로
    갈려 있던 걸 하나로 합치면서(X43) 붙였다.
    """
    hold(0.2)
    await _call_shuttle(client, 44)
    assert await _wait_until(lambda: bool(commands))
    await client.post("/api/dispatch/commands/return")
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": True})
    await client.post("/api/dispatch/commands/emergency-stop", json={"engage": False})
    await client.post(
        "/api/dispatch/commands/move", json={"destination": "INDOOR_TAGGING"}
    )

    base = {"command_id", "type", "ttl_ms"}
    expected = [
        base | {"cmd_destination"},   # 셔틀 자동 발사
        base | {"return_to_charge"},  # 복귀
        base | {"emergency_stop"},    # 긴급정지 세우기
        base | {"emergency_stop"},    # 긴급정지 풀기
        base | {"cmd_destination"},   # 즉시 이동
    ]
    assert [set(c) for c in commands] == expected


def _command_schema() -> dict:
    import json
    import pathlib

    return json.loads(
        (
            pathlib.Path(__file__).resolve().parents[1] / "schemas" / "command.schema.json"
        ).read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "command, why",
    [
        (
            {"command_id": "x", "type": "cmd_destination",
             "cmd_destination": "OUTDOOR_TAGGING", "speed": 1.5},
            "계약에 없는 칸",
        ),
        (
            {"command_id": "x", "type": "cmd_destination", "ttl_ms": 30000},
            "목적지 명령인데 목적지가 없다",
        ),
        (
            {"command_id": "x", "type": "cmd_destination", "cmd_destination": None},
            "목적지가 null이면 로봇이 어디로 갈지 모른다",
        ),
        (
            {"command_id": "x", "type": "emergency_stop"},
            "긴급정지인데 세우는지 푸는지가 없다",
        ),
        (
            {"command_id": "x", "type": "return_to_charge"},
            "복귀인데 return_to_charge 칸이 없다",
        ),
    ],
)
async def test_계약_밖_명령은_스키마가_막는다(command, why):
    """반증 자리 — 예전 스키마는 이 다섯을 전부 통과시켰다(2026-07-31 검증 P3).

    "계약 밖 칸·값을 막는다"고 적어 놓고 실제로 못 막으면, 젯슨이 통째로 거절하는 명령이
    시연 당일에야 드러난다.
    """
    import jsonschema

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(command, _command_schema())


@pytest.mark.parametrize(
    "command",
    [
        {"command_id": "shuttle-1", "type": "cmd_destination", "ttl_ms": 30000,
         "cmd_destination": "INDOOR_TAGGING"},
        {"command_id": "return-1", "type": "return_to_charge", "return_to_charge": True},
        {"command_id": "estop-1", "type": "emergency_stop", "emergency_stop": False},
        {"command_id": "w-1", "type": "weather_status", "weather_status": "SNOW"},
    ],
)
async def test_계약_안_명령은_그대로_통과한다(command):
    """조이면서 정상 명령까지 막으면 그게 더 큰 사고다(해제 명령의 false 포함)."""
    import jsonschema

    jsonschema.validate(command, _command_schema())
