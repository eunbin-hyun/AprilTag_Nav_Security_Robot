"""화면 셔틀 가상 호출 — `POST /api/shuttle-calls` (C6).

셔틀은 실기기를 안 만들고 화면 버튼으로 가상 호출한다(사용자 확정). 그런데 기기용 인입
`POST /api/shuttle-arrivals`가 X-API-Key를 요구해서, 화면이 각본 ①을 시작할 방법이 없었다.
화면이 그 키를 들면 브라우저에 키가 노출되고 그 키 하나로 태깅·통과 인입과 신원 입력까지
전부 열린다.

## 무엇을 못박나

- 키 없이 200이고, 신호 행이 실제로 남고, 화면이 배달 결과(로봇 수·command_id)를 받는다.
- **열린 폭이 셔틀 하나뿐이다.** 태깅·통과·신원 갈래는 키 없이 여전히 401이다 — 이 시험이
  그 경계를 지킨다.
- event_id·signal_ts는 서버가 정한다(화면이 못 정한다).
- 같은 게이트 짧은 시간 중복 호출은 429이고 Retry-After가 실린다.
- 기기 인입과 화면 호출이 **같은 모양의 행·같은 WS 메시지**를 만든다(갈래가 갈라지는 걸 막는다).

호출 쿨다운은 DB 밖 인메모리 상태라 TRUNCATE로 안 지워진다. 예전에는 이 파일이 자기
autouse 픽스처로 직접 비웠는데, 그 픽스처가 **이 파일 안에서만** 듣는 게 함정이었다 —
`/api/shuttle-calls`를 부르는 다음 시험 파일은 앞 케이스가 남긴 쿨다운을 그대로 물려받아
원인 모를 429로 깨진다. 그래서 초기화를 conftest `_clean` 훅으로 올리고(모든 파일이 같이
받는다) 이 파일의 픽스처는 걷어냈다. 그 함정을 못박는 짝 시험은
`test_shuttle_cooldown_hygiene.py`에 있다.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app import shuttle_call
from app.config import get_settings
from app.db import get_session
from app.eventid import parse_event_id
from app.main import app
from app.models import ShuttleArrival
from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 30, 6, 0, 0, tzinfo=dt.timezone.utc)
HEADERS = {"X-API-Key": get_settings().api_key}

# 접점③ 오염 재현에 쓰는 게이트. 이 파일이 1차 호출을, test_shuttle_cooldown_hygiene.py가
# 2차 호출을 맡는다. 다른 케이스가 안 쓰는 번호로 둬야 오염 출처가 하나로 좁혀진다.
POLLUTION_GATE = 11

# 두 파일이 같은 눈금을 보게 맞춘 얼린 단조 시각. 값이 갈라지면 재현이 무의미해진다.
FROZEN_MONO = 1_000.0

# 1차 호출이 **이 프로세스에서** 돌았다는 증거. 짝 파일이 읽는다.
# xdist는 두 케이스를 다른 워커 프로세스로 흩어서, 짝 파일이 물려받을 눈금이 아예 없는데도
# 초록이 됐다(교차 검증 실측). 그 상태를 짝이 알아채고 skip으로 드러내게 하는 witness다.
POLLUTION_CALLED: list[str] = []


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


@pytest.fixture
def sent(monkeypatch):
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


# ⚠ `no_hold` 픽스처는 conftest.py로 올렸다(2026-07-31). 재전송 복구를 보는 파일이 셋으로
# 늘어서, 파일마다 사본을 들고 있으면 계약이 바뀔 때 한쪽만 고쳐진다.


@pytest.fixture
def frozen_mono(monkeypatch):
    """쿨다운의 단조 시계를 시험이 직접 감는다(conftest의 wall_clock과 같은 방식).

    실제로 5초를 기다리면 시험이 느려지고 시간에 흔들린다.
    """
    holder = {"t": FROZEN_MONO}
    monkeypatch.setattr(shuttle_call, "_mono", lambda: holder["t"])
    return holder


# ── 기본 동작 ──────────────────────────────────────────────────────────────

async def test_키_없이_호출해도_200이고_행이_남는다(client):
    res = await client.post("/api/shuttle-calls", json={"gate_no": 1, "shuttle_no": "3호차"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["stored"] is True
    assert (body["gate_no"], body["shuttle_no"]) == (1, "3호차")
    # 로봇이 안 붙어 있으니 0이다. 이 값이 없으면 화면은 200만 보고 "출동했다"고 그린다.
    assert body["notified_robot_count"] == 0

    async with get_session() as session:
        row = (
            await session.execute(
                select(ShuttleArrival).where(ShuttleArrival.event_id == body["event_id"])
            )
        ).scalar_one()
        assert (row.gate_no, row.shuttle_no) == (1, "3호차")


async def test_서버가_event_id와_시각을_정한다(client):
    """화면이 event_id를 정하면 남의 신호를 덮거나 멱등 키를 골라 적재를 조용히 막을 수 있다."""
    res = await client.post(
        "/api/shuttle-calls",
        # 보내도 무시돼야 한다(extra 필드).
        json={"gate_no": 7, "event_id": "내가-정한-키", "signal_ts": _iso(0)},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["event_id"] != "내가-정한-키"
    assert body["event_id"].startswith("webcall-7-")
    # signal_ts는 서버 현재 시각이라 BASE(2026-07-30 06:00)와 다르다.
    assert body["signal_ts"] != _iso(0)


async def test_생성한_event_id가_안건2_형식으로_파싱된다(client):
    """event_id_require_boot_id를 나중에 켜도 화면 호출이 형식 때문에 막히면 안 된다."""
    res = await client.post("/api/shuttle-calls", json={"gate_no": 9})
    parts = parse_event_id(res.json()["event_id"])
    assert parts is not None
    assert parts.legacy is False, "boot_id 없는 예전 3토막으로 만들어졌다"
    assert parts.device_id == "webcall-9"


async def test_연타해도_event_id가_안_겹친다(client):
    """겹치면 두 번째 호출이 재시도 중복으로 조용히 사라진다(안건②가 고치려는 그 함정)."""
    ids = {
        shuttle_call.next_web_call_event_id(1) for _ in range(50)
    }
    assert len(ids) == 50


async def test_호출이_대시보드_메시지로_나간다(client, sent):
    """도착 알림 한 장에 대기 창 정보까지 실린다(설계 §6-7).

    `pending`은 2026-07-31에 붙은 칸이다. 화면이 카운트다운을 그리려면 마감 시각·남은 초·
    버튼 셋이 이 한 장에 다 있어야 한다 — 따로 물어보게 만들면 5초짜리 창에서 왕복이 한 번
    더 늘어난다.
    """
    await client.post("/api/shuttle-calls", json={"gate_no": 2, "shuttle_no": "1호차"})
    assert [m["type"] for m in sent] == ["shuttle_arrival"]
    payload = sent[0]["data"]
    assert set(payload) == {
        "event_id", "gate_no", "shuttle_no", "notified_robot_count", "command_id",
        "pending",
        # 도착 행 PK. 화면 호출 창구도 기기 인입과 같은 칸을 내야 계약이 안 갈라진다.
        "arrival_id",
    }
    assert payload["gate_no"] == 2
    # 대기 창이 열렸으니 아직 아무 명령도 안 나갔다.
    assert payload["notified_robot_count"] == 0
    pending = payload["pending"]
    assert pending["hold_sec"] == get_settings().shuttle_dispatch_hold_sec
    assert 0 < pending["remain_sec"] <= pending["hold_sec"]
    assert pending["choices"] == ["INDOOR_TAGGING", "OUTDOOR_TAGGING", "HOLD"]
    assert pending["deadline"] > pending["opened_at"]


# ── 열린 폭이 셔틀 하나뿐인가 ───────────────────────────────────────────────

@pytest.mark.parametrize(
    "path, body",
    [
        ("/api/tagging-events",
         {"event_id": "t-open", "gate_no": 1, "tag_id": "u1", "observed_at": _iso(0)}),
        ("/api/gate-pass-events",
         {"event_id": "p-open", "gate_no": 1, "direction": "A_TO_B", "status": "complete",
          "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)}),
        ("/api/shuttle-arrivals",
         {"event_id": "s-open", "gate_no": 1, "signal_ts": _iso(0)}),
    ],
)
async def test_다른_인입은_여전히_키를_요구한다(client, path, body):
    """셔틀 창구를 여는 대신 다른 인입까지 열리면 이 설계의 뜻이 사라진다."""
    res = await client.post(path, json=body)
    assert res.status_code == 401, f"{path}가 키 없이 통과했다"


async def test_신원_갈래도_여전히_키를_요구한다(client):
    res = await client.post("/api/alerts/1/identify", json={"person": "가명-1"})
    assert res.status_code == 401


# ── 입력 검증 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("gate_no", [0, -1, 10000])
async def test_범위_밖_게이트는_422(client, gate_no):
    res = await client.post("/api/shuttle-calls", json={"gate_no": gate_no})
    assert res.status_code == 422


async def test_게이트를_안_보내면_422(client):
    """화면 버튼이 어느 게이트인지 안 정하면 로봇 목적지 명령 대상이 흐려진다."""
    res = await client.post("/api/shuttle-calls", json={})
    assert res.status_code == 422


async def test_긴_셔틀번호는_500이_아니라_422(client):
    """상한이 없으면 asyncpg StringDataRightTruncation이 500으로 올라온다(수리 전 실측)."""
    res = await client.post(
        "/api/shuttle-calls", json={"gate_no": 1, "shuttle_no": "x" * 33}
    )
    assert res.status_code == 422, res.text


async def test_기기_인입의_긴_셔틀번호도_422(client):
    """같은 상한을 기기 인입에도 걸었다. 수리 전엔 여기가 500이었다."""
    res = await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "s-long", "gate_no": 1, "shuttle_no": "x" * 33,
              "signal_ts": _iso(0)},
        headers=HEADERS,
    )
    assert res.status_code == 422, res.text


async def test_셔틀번호_상한_경계는_32자까지_받는다(client):
    res = await client.post(
        "/api/shuttle-calls", json={"gate_no": 1, "shuttle_no": "x" * 32}
    )
    assert res.status_code == 200, res.text


# ── 남용 방어 ──────────────────────────────────────────────────────────────

async def test_같은_게이트_연타는_429다(client, frozen_mono):
    """화면 버튼은 연타된다. 그때마다 로봇에 목적지 명령이 새로 나가면 도착 판정이 흔들린다."""
    first = await client.post("/api/shuttle-calls", json={"gate_no": 3})
    assert first.status_code == 200

    second = await client.post("/api/shuttle-calls", json={"gate_no": 3})
    assert second.status_code == 429, second.text
    assert int(second.headers["Retry-After"]) >= 1


async def test_쿨다운이_지나면_다시_호출된다(client, frozen_mono):
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 3})).status_code == 200
    frozen_mono["t"] += shuttle_call.SHUTTLE_CALL_COOLDOWN_SEC + 0.1
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 3})).status_code == 200


async def test_연타가_쿨다운_창을_뒤로_안_끈다(client, frozen_mono):
    """창 안 요청이 눈금을 밀면 누르는 걸 멈출 때까지 영원히 안 열린다."""
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 3})).status_code == 200
    for _ in range(4):
        frozen_mono["t"] += 1.0
        assert (
            await client.post("/api/shuttle-calls", json={"gate_no": 3})
        ).status_code == 429
    # 첫 호출로부터 5.1초. 창을 안 밀었으면 여기서 열려야 한다.
    frozen_mono["t"] += 1.2
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 3})).status_code == 200


async def test_쿨다운은_게이트별로_따로다(client, frozen_mono):
    """한 게이트 연타가 다른 게이트 호출을 막으면 게이트 둘로 하는 시연이 안 된다."""
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 4})).status_code == 200
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 5})).status_code == 200
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 4})).status_code == 429


async def test_쿨다운_오염_재현_1차_호출(client, frozen_mono):
    """접점③ 오염 재현 ①. 짝은 `test_shuttle_cooldown_hygiene.py`의 2차 호출이다.

    같은 게이트를 **서로 다른 파일**에서 연달아 부른다. 케이스 사이에 쿨다운이 안 비워지면
    뒤 파일 케이스가 원인 모를 429로 깨진다 — 이 파일 안 autouse 픽스처로 비우던 예전 방식이
    구조적으로 못 잡던 갈래이고, conftest `_clean`으로 올려야 닫힌다.

    시각을 얼려서 두 케이스가 같은 눈금(1000.0)을 보게 만든다. 실시간에 기대면 두 케이스
    사이에 5초가 그냥 지나가 버려 재현이 시간에 흔들린다.

    ⚠ 이 짝은 **같은 프로세스**에서만 성립한다. `-n 2`면 짝이 딴 워커로 가서 오염이 아예 안
    일어난다 — 그때 짝 파일이 조용히 초록이 되지 않게 POLLUTION_CALLED로 알려 준다. 병렬에서도
    무는 가드는 짝 파일의 모듈 훅 쪽이다.
    """
    res = await client.post("/api/shuttle-calls", json={"gate_no": POLLUTION_GATE})
    assert res.status_code == 200, res.text
    POLLUTION_CALLED.append(res.json()["event_id"])


async def test_거절된_호출은_쿨다운_눈금을_안_찍는다(client, frozen_mono):
    """422로 막힌 호출이 다음 5초를 삼키면 화면이 다시 눌러도 429만 돌아온다."""
    bad = await client.post("/api/shuttle-calls", json={"gate_no": 6, "shuttle_no": "x" * 33})
    assert bad.status_code == 422
    assert (await client.post("/api/shuttle-calls", json={"gate_no": 6})).status_code == 200


# ── 기기 인입과 갈래가 갈라지지 않았나 ──────────────────────────────────────

async def test_기기_인입과_화면_호출이_같은_모양을_만든다(client, sent):
    """두 창구가 같은 모양의 행·같은 WS 메시지를 만드나.

    ⚠ 이 시험은 **칸 집합까지만** 본다. 예외 갈래(중복 재전송 복구·본문 충돌 409·응답 헤더)가
    한쪽에서 사라져도 여기는 초록이다 — 그래서 갈래별 시험을 아래 절에 따로 뒀다.
    """
    device = await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "s-cmp-device", "gate_no": 8, "shuttle_no": "2호차",
              "signal_ts": _iso(0)},
        headers=HEADERS,
    )
    assert device.status_code == 200
    device_msg = [m for m in sent if m["type"] == "shuttle_arrival"][-1]
    sent.clear()

    web = await client.post("/api/shuttle-calls", json={"gate_no": 8, "shuttle_no": "2호차"})
    assert web.status_code == 200
    web_msg = [m for m in sent if m["type"] == "shuttle_arrival"][-1]

    # 메시지 칸 집합이 같아야 한다(event_id·값은 다르다).
    assert set(device_msg["data"]) == set(web_msg["data"])
    assert device_msg["data"]["gate_no"] == web_msg["data"]["gate_no"]

    # DB 행도 같은 칸이 채워져야 한다. 셔틀 두 행을 나란히 놓고 event_id만 빼고 비교한다.
    async with get_session() as session:
        rows = (
            await session.execute(
                select(ShuttleArrival).where(ShuttleArrival.gate_no == 8)
                .order_by(ShuttleArrival.id)
            )
        ).scalars().all()
    assert len(rows) == 2
    d, w = rows
    assert (d.shuttle_no, d.gate_no, d.room_id) == (w.shuttle_no, w.gate_no, w.room_id)
    # boot_id는 둘 다 event_id에서 뽑은 값이다(같은 규칙). 기기 쪽은 예전 3토막이라 NULL이고,
    # 화면 호출은 서버 프로세스 표식이 들어간다.
    assert d.boot_id is None
    assert w.boot_id == shuttle_call.web_call_boot_id()


# ── 예외 갈래가 공통 몸통에 살아있나 ────────────────────────────────────────
# 접점②. 예전엔 중복 재전송 복구·본문 충돌 409·응답 헤더 셋이 `routers/ingest.py` 몸통에만
# 있었다. 기기 몸통을 `record_shuttle_arrival` 호출 한 줄로 갈아 끼울 때 갈래를 같이 안
# 올리면 "통지 유실 복구"(P0-4-c)가 조용히 사라진다 — 위 대조 시험은 칸 집합만 보니 그
# 소실을 못 잡는다.
#
# 그래서 여기서는 라우터를 안 거치고 **공통 몸통을 직접** 부른다. 어느 창구가 부르든 갈래가
# 몸통에 있다는 게 증명되어야 하고, 화면 창구는 event_id를 서버가 만들어서 중복 갈래를
# 아예 못 만들기 때문이다.


@pytest.fixture
def dead_robot(monkeypatch):
    """붙은 로봇이 없어 명령이 못 나가는 상태를 만든다."""
    from app.ws import robot_manager

    async def _dead(command, robot_id=None):
        return []

    monkeypatch.setattr(robot_manager, "send_command", _dead)


def _alive_robot(monkeypatch, name: str, box: list[dict]):
    """이름이 `name`인 로봇에 명령이 실제로 나가게 만들고, 나간 명령을 box에 모은다."""
    from app.ws import robot_manager

    async def _alive(command, robot_id=None):
        box.append(command)
        return [name]

    monkeypatch.setattr(robot_manager, "send_command", _alive)


async def _record(session, event_id: str, **over):
    values = {
        "event_id": event_id,
        "gate_no": 21,
        "signal_ts": BASE,
        "shuttle_no": "SH-21",
    }
    values.update(over)
    return await shuttle_call.record_shuttle_arrival(session, **values)


async def test_몸통이_통지_유실을_재전송으로_복구한다(
    monkeypatch, seed_robot, dead_robot, no_hold
):
    """반증 자리 — 이 갈래가 몸통에 없으면 명령이 사라진 채 duplicate로 튕긴다(P0-4-c).

    ⚠ `no_hold`가 붙은 이유(2026-07-31 검증 P1 수리). 재전송 복구는 "명령이 이미 나갔어야
    하는데 유실됐다"는 갈래다. 대기 창이 **아직 도는 중**이면 명령은 유실된 게 아니라 아직
    안 나간 것이라, 지금 몸통은 그때 재전송을 안 태우고 창 마감을 기다린다
    (`test_shuttle_dispatch.py`의 "재시도가_열린_대기창을_안_깬다"가 그 갈래를 못박는다).
    여기서는 창 없이 즉시 발사한 뒤 유실된 진짜 복구 갈래를 본다.
    """
    await seed_robot(name="jetson01")

    async with get_session() as session:
        first = await _record(session, "SEAM-RESEND")
    assert first.stored is True
    assert first.notified_robot_count == 0, "로봇이 죽었는데 통지된 것으로 셌다"

    issued: list[dict] = []
    _alive_robot(monkeypatch, "jetson01", issued)
    async with get_session() as session:
        second = await _record(session, "SEAM-RESEND")

    assert second.stored is False, "중복인데 새 행이 생겼다"
    assert second.resent is True, "재전송 복구 갈래를 안 탔다"
    assert len(issued) == 1, "같은 요청 재전송이 명령을 다시 안 냈다"
    assert second.notified_robot_count == 1
    # command_id가 결정적이라 로봇 쪽에서 1차 명령과 같은 명령으로 접힌다.
    assert issued[0]["command_id"] == f"shuttle-{second.arrival_id}"


async def test_이미_통지된_신호는_몸통이_두_번_안_낸다(monkeypatch, seed_robot, no_hold):
    """대조군 — 복구 갈래가 로봇을 두 번 출동시키면 도착 판정이 흔들린다."""
    await seed_robot(name="jetson01")
    issued: list[dict] = []
    _alive_robot(monkeypatch, "jetson01", issued)

    async with get_session() as session:
        first = await _record(session, "SEAM-ONCE")
    assert (first.stored, first.notified_robot_count) == (True, 1)

    async with get_session() as session:
        second = await _record(session, "SEAM-ONCE")

    assert len(issued) == 1, "이미 통지된 신호에 명령이 또 나갔다"
    assert (second.stored, second.resent) == (False, False)
    assert second.notified_robot_count == 1, "적어도 한 대가 받았다는 사실이 안 드러난다"


async def test_몸통이_본문_충돌을_409로_거절한다(dead_robot):
    """조용히 버리면 서로 다른 사건이 한 행으로 접히고 기기 쪽이 알 방법이 없다."""
    from fastapi import HTTPException

    async with get_session() as session:
        await _record(session, "SEAM-CONFLICT")

    with pytest.raises(HTTPException) as caught:
        async with get_session() as session:
            await _record(session, "SEAM-CONFLICT", shuttle_no="SH-99")

    assert caught.value.status_code == 409
    detail = caught.value.detail
    assert detail["reason"] == "event_id_payload_mismatch"
    assert detail["conflicting_fields"] == ["shuttle_no"]


async def test_같은_본문_중복은_409가_아니라_조용히_지나간다(dead_robot, no_hold):
    """재시도는 정상 흐름이다. 여기가 409면 기기 재시도 큐가 영구히 막힌다.

    `no_hold`는 위 재전송 시험과 같은 이유다 — 창이 도는 중이면 재전송을 안 태운다.
    """
    async with get_session() as session:
        await _record(session, "SEAM-SAME")
    async with get_session() as session:
        again = await _record(session, "SEAM-SAME")
    assert (again.stored, again.resent) == (False, True)


async def test_몸통이_통지_결과를_응답_헤더에_싣는다(
    monkeypatch, seed_robot, dead_robot, no_hold
):
    """헤더 계약이 몸통에 있어야 창구가 늘어도 값이 안 어긋난다.

    `no_hold`로 창을 꺼서 0 → 1 복구를 그 자리에서 본다(창이 도는 중이면 재전송을 안 탄다).
    """
    from fastapi import Response

    await seed_robot(name="jetson01")

    dead = Response()
    async with get_session() as session:
        await _record(session, "SEAM-HEADER", response=dead)
    assert dead.headers[shuttle_call.SHUTTLE_NOTIFY_HEADER] == "0"

    issued: list[dict] = []
    _alive_robot(monkeypatch, "jetson01", issued)
    recovered = Response()
    async with get_session() as session:
        await _record(session, "SEAM-HEADER", response=recovered)
    # 재전송으로 복구된 값이 그대로 헤더에 실린다(0 → 1).
    assert recovered.headers[shuttle_call.SHUTTLE_NOTIFY_HEADER] == "1"


# ── 갈아 끼운 기기 인입이 갈래를 그대로 들고 있나 ──────────────────────────
# 위 절은 몸통을 직접 불러서 증명했다. 여기서는 **라우터를 거쳐** 같은 갈래가 살아있는지
# 본다 — 몸통을 한 줄 호출로 갈아 끼울 때 갈래가 새는 게 이 작업의 최대 위험이라, 두 층에서
# 각각 못박는다.


async def test_기기_인입도_통지_수를_본문에_싣는다(client, seed_robot, monkeypatch, no_hold):
    """접점①. 예전엔 기기 창구만 헤더로 주고 화면 창구만 본문으로 줬다(계약 둘)."""
    await seed_robot(name="jetson01")
    _alive_robot(monkeypatch, "jetson01", [])

    device = await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "SEAM-BODY-1", "gate_no": 22, "signal_ts": _iso(0)},
        headers=HEADERS,
    )
    assert device.status_code == 200, device.text
    assert device.json()["notified_robot_count"] == 1

    web = await client.post("/api/shuttle-calls", json={"gate_no": 23})
    assert web.status_code == 200, web.text
    # 두 창구가 같은 이름의 본문 칸으로 같은 사실을 준다.
    assert web.json()["notified_robot_count"] == 1


async def test_기기_인입_응답_헤더는_그대로_남았다(client, dead_robot):
    """본문 칸이 생겼어도 헤더를 지우는 건 계약 축소다(라파가 나중에 읽을 자리)."""
    res = await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "SEAM-HDR-KEEP", "gate_no": 24, "signal_ts": _iso(0)},
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    assert res.headers[shuttle_call.SHUTTLE_NOTIFY_HEADER] == "0"
    # 헤더와 본문이 같은 값을 가리켜야 한다. 갈라지면 읽는 쪽이 어느 쪽을 믿을지 못 정한다.
    assert res.json()["notified_robot_count"] == 0


async def test_라우터를_거친_중복_재전송도_복구된다(
    client, seed_robot, monkeypatch, dead_robot, no_hold
):
    """갈아 끼운 뒤 P0-4-c 복구가 살아있나 — 칸 집합 대조 시험이 못 보는 갈래다.

    `no_hold`는 몸통 시험과 같은 이유다(창이 도는 중이면 재전송을 안 태운다).
    """
    await seed_robot(name="jetson01")
    body = {"event_id": "SEAM-E2E-RESEND", "gate_no": 25, "signal_ts": _iso(0)}

    first = await client.post("/api/shuttle-arrivals", json=body, headers=HEADERS)
    assert first.headers[shuttle_call.SHUTTLE_NOTIFY_HEADER] == "0"
    assert first.json()["notified_robot_count"] == 0
    assert first.json()["resent"] is False, "1차 인입이 복구분으로 표시됐다"

    issued: list[dict] = []
    _alive_robot(monkeypatch, "jetson01", issued)
    second = await client.post("/api/shuttle-arrivals", json=body, headers=HEADERS)

    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert len(issued) == 1, "같은 요청 재전송이 명령을 다시 안 냈다"
    assert second.headers[shuttle_call.SHUTTLE_NOTIFY_HEADER] == "1"
    assert second.json()["notified_robot_count"] == 1
    # 복구 사실이 본문에도 실린다. 예전엔 서버 로그와 헤더에만 있어서 기기 쪽은 duplicate만
    # 보고 "명령이 다시 나갔다"를 알 방법이 없었다(P0-4-c의 남은 반쪽).
    assert second.json()["resent"] is True, "복구분인데 본문이 그 사실을 안 싣는다"


async def test_화면_창구가_복구_표시를_몸통에서_옮긴다(client, monkeypatch):
    """`resent`를 기본값 False에 기대지 않고 몸통(ShuttleRecordResult) 값을 실제로 옮기나.

    이 창구는 event_id를 서버가 매 호출 새로 만들어서 복구 갈래가 구조적으로 안 생긴다 —
    그래서 몸통 값을 안 옮겨도 지금은 응답이 **우연히** 맞는다(그래서 위 칸 모양 대조 시험도
    이 결함을 못 봤다). 그 우연에 기대면 event_id를 부르는 쪽이 정하게 바뀌는 순간 이 창구만
    조용히 거짓을 내보낸다. 몸통을 복구분으로 갈아 끼워서 그 한 줄이 실제로 있는지 본다.
    """
    from app.routers import query as query_router

    async def _resent_body(session, **kw):
        return shuttle_call.ShuttleRecordResult(
            stored=False,
            arrival_id=77,
            notified_robot_count=1,
            command_id="shuttle-77",
            resent=True,
        )

    monkeypatch.setattr(query_router, "record_shuttle_arrival", _resent_body)
    res = await client.post("/api/shuttle-calls", json={"gate_no": 27})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resent"] is True, "몸통이 복구분이라 했는데 화면 응답이 기본값 False로 나갔다"
    # 같은 몸통에서 오는 이웃 칸도 같이 본다 — 한 칸만 옮기는 실수를 잡는다.
    assert (body["notified_robot_count"], body["command_id"]) == (1, "shuttle-77")


async def test_두_창구_응답_계약이_칸_모양까지_같다(client):
    """접점① 마무리 — 같은 사실의 칸이 창구마다 nullable 여부까지 같아야 한다.

    1차에는 기기 쪽 `notified_robot_count`만 `int | None = None`이라, OpenAPI를 보고 짜는
    프론트가 기기 창구에만 None 갈래를 들어야 했다(교차 검증 지적). 라우터는 두 창구 다 몸통
    값을 늘 채우니 없을 수 있는 값이 아니다. 문서(OpenAPI)를 잣대로 본다 — 읽는 쪽이 실제로
    보는 게 그 문서라서다.
    """
    # ⚠ HTTP로 `/openapi.json`을 부르지 않는다 — 그 창구는 바깥 노출을 닫으려고 꺼져 있어서
    # 404다(설계 §8 결정 2). 스키마 자체는 그대로라 앱에서 직접 뽑는다.
    spec = app.openapi()
    device = spec["components"]["schemas"]["ShuttleIngestAck"]
    web = spec["components"]["schemas"]["ShuttleCallOut"]

    for name, schema in (("기기", device), ("화면", web)):
        prop = schema["properties"]["notified_robot_count"]
        assert prop.get("type") == "integer", f"{name} 창구 통지 칸이 정수가 아니다: {prop}"
        assert "anyOf" not in prop, f"{name} 창구 통지 칸이 nullable이다: {prop}"
        assert "notified_robot_count" in schema["required"], (
            f"{name} 창구가 통지 칸을 필수로 안 본다 — 읽는 쪽이 None 갈래를 들어야 한다"
        )
        assert "resent" in schema["properties"], (
            f"{name} 창구가 복구 사실(resent)을 못 싣는다"
        )


async def test_라우터를_거친_본문_충돌도_409다(client, dead_robot):
    body = {"event_id": "SEAM-E2E-CONFLICT", "gate_no": 26, "shuttle_no": "SH-1",
            "signal_ts": _iso(0)}
    assert (await client.post(
        "/api/shuttle-arrivals", json=body, headers=HEADERS)).status_code == 200

    clash = await client.post(
        "/api/shuttle-arrivals", json={**body, "shuttle_no": "SH-9"}, headers=HEADERS
    )
    assert clash.status_code == 409, clash.text
    assert clash.json()["detail"]["conflicting_fields"] == ["shuttle_no"]
    # 409엔 헤더를 안 붙인다(예전 거동과 같다 — 예외가 헤더 찍기 전에 올라간다).
    assert shuttle_call.SHUTTLE_NOTIFY_HEADER not in clash.headers
