"""떠 있던 몫 — 통지 기록 구멍 · 그 기록 행 가리기 · 통계 시험자료 거르기 · 시각 컷 시간대.

파일 이름에 `orphan`을 넣은 건 앞 사이클에서 어느 조도 안 받아 떠 있던 몫만 모았다는 뜻이고,
다른 조 시험과 이름이 안 겹게 하는 자리다.

## 1. `notified_robot_id` 미기록 구멍

`notify_shuttle_arrival`은 명령을 실제로 보낸 뒤 `ShuttleArrival.notified_robot_id`에 그 로봇을
적는다. 예전에는 **같은 이름의 `Robot` 행이 이미 있을 때만** 적어서, 행이 없으면 "보냈는데 안
적힌" 상태로 남았다. 그 상태를 `routers/ingest.py`의 재전송 판정이 "아직 안 나갔다"로 읽어
같은 신호를 다시 내보내고, 로봇은 목적지 명령을 두 번 받는다.

행이 없는 상황은 억지 조건이 아니다. `robot_manager.register()`는 `robot_state` 프레임을 받은
직후에 불리고 `Robot` 행 커밋은 그 뒤라, 그 틈에 셔틀 신호가 들어오면 명령은 나가는데 행은
아직 없다. 정리 도구(`tools/purge_test_data.py`)나 손으로 `robot` 행을 지운 뒤 붙어 있던
커넥션도 같은 자리다. 아래 두 케이스는 그 틈을 `register()` 직접 호출로 그대로 재현한다.

## 2. 통지 기록용 행이 관제 화면에 새지 않기

1번 수리가 만드는 `Robot` 행은 이름만 있고 모드·배터리·통신이 전부 NULL이다. 거르지 않으면
셔틀 통지 한 번마다 관제 목록·스냅샷에 빈 로봇 카드가 서고, 그게 곧 발표 화면에 뜨는 가짜다
(운영 DB `robot` 테이블은 0행이라 전부 새로 생기는 카드다). 그래서 `fetch_robots`가 그릴 값이
하나도 없는 행을 뺀다. **기록은 DB에 남고 화면에서만 가린다** — 셋을 같이 재야 수리가 맞다.
① 통지 기록이 남나 ② 재전송이 두 번 안 보내나 ③ 그 행이 목록·스냅샷에 안 뜨나.

## 3. 통계 시험자료 거르기

`STATS_EXCLUDE_TEST_DATA`가 꺼져 있으면 예전 그대로 전부 세고, 켜면 기기 접두·시각 컷으로
시험 자료를 뺀다. 두 방향을 다 재서 스위치가 정말 갈래를 가르는지 본다.

## 4. 시각 컷의 시간대

`STATS_EXCLUDE_BEFORE`에 오프셋을 안 적어도 안 터진다. 그대로 두면 tz 없는 값이 되어 DB가
UTC로 견주고, 한국에서 적은 사람이 아홉 시간 어긋난 컷을 조용히 얻는다. 그래서 서버가
KST로 못박는다(`stats._assume_kst`). 값이 조용히 틀리는 걸 막는 자리라 거동으로 잰다.
"""
import datetime as dt
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db import get_session
from app.models import Alert, GatePassEvent, Robot, ShuttleArrival, TaggingEvent
from app.robot_channel import (
    handle_robot_state,
    notify_shuttle_arrival,
    record_presentation,
)
from app.routers.ingest import SHUTTLE_NOTIFY_HEADER
from app.schemas import RobotPosition, RobotStateIn, RobotStatusSummary
from app.stats import STATS_TZ_OFFSET, get_stats_filter, today_counts, untagged_stats
from app.ws import manager, robot_manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

ORPHAN_ROBOT = "jetson-orphan"


@pytest.fixture(autouse=True)
def _no_dispatch_hold(monkeypatch):
    """출동 대기 창(2026-07-31 설계)을 꺼서 셔틀 신호가 예전처럼 즉시 발사되게 둔다.

    이 파일이 보는 건 "명령이 나갔는데 `notified_robot_id`가 안 적히는" 구멍이라, 명령이
    언제 나가느냐(즉시냐 5초 뒤냐)는 무관하다. 대기 창 시나리오는
    `tests/test_shuttle_dispatch.py`가 따로 본다.

    설정 인스턴스에 속성을 직접 대입하지 않고 env+cache_clear로 간다 — 뒤에 캐시를 비우는
    픽스처가 붙으면 속성 대입은 조용히 사라진다.
    """
    from app.config import get_settings

    monkeypatch.setenv("SHUTTLE_DISPATCH_HOLD_SEC", "0")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class FakeSocket:
    """매니저에 직접 꽂는 가짜 WebSocket(test_robot_channel.py와 같은 수법)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def count(self, type_: str) -> int:
        return sum(1 for m in self.sent if m.get("type") == type_)


@pytest_asyncio.fixture(loop_scope="session")
async def dash():
    sock = FakeSocket()
    await manager.connect(sock)
    try:
        yield sock
    finally:
        manager.disconnect(sock)


@pytest_asyncio.fixture(loop_scope="session")
async def orphan_robot():
    """`Robot` 행 없이 이름만 등록된 로봇 커넥션.

    ⚠ `robot_state` 프레임을 일부러 안 보낸다. 그게 이 케이스의 조건이다 — 프레임을 보내면
    `_upsert_robot`이 행을 만들어 버려서 재현하려는 구멍이 사라진다.
    """
    sock = FakeSocket()
    await robot_manager.connect(sock)
    robot_manager.register(sock, ORPHAN_ROBOT)
    try:
        yield sock
    finally:
        robot_manager.disconnect(sock)


async def _insert_arrival(session, event_id: str, gate_no: int = 2) -> int:
    arrival = ShuttleArrival(event_id=event_id, gate_no=gate_no, signal_ts=_now())
    session.add(arrival)
    await session.flush()
    await session.commit()
    return arrival.id


# ── 1. notified_robot_id 미기록 구멍 ──────────────────────────────────────

async def test_orphan_notify_records_arrival_without_robot_row(orphan_robot):
    """`Robot` 행이 없어도 "보냈다"는 사실이 DB에 남는다.

    남기는 방법은 그 이름으로 행을 만드는 것이다. 음수·0 같은 표식을 넣으면 값은 채워지지만
    짝짓기(`oldest_arrival_notified_to`)가 그 pk를 영영 못 찾아 그 신호가 미짝으로 굳는다.
    """
    async with get_session() as session:
        arrival_id = await _insert_arrival(session, "orphan-notify-1")
        delivered, command_id = await notify_shuttle_arrival(
            session, arrival_id=arrival_id, gate_no=2
        )

    assert delivered == 1, "등록된 커넥션이 하나니 명령은 나가야 한다"
    assert command_id == f"shuttle-{arrival_id}"
    assert orphan_robot.count("command") == 1

    async with get_session() as session:
        robot_row = (
            await session.execute(select(Robot).where(Robot.name == ORPHAN_ROBOT))
        ).scalars().one()
        arrival = (
            await session.execute(
                select(ShuttleArrival).where(ShuttleArrival.id == arrival_id)
            )
        ).scalars().one()
    assert arrival.notified_robot_id == robot_row.id


async def test_orphan_resend_does_not_command_twice(dash, orphan_robot, client, auth_headers):
    """행이 없던 통지도 재전송 판정이 "이미 나갔다"로 읽는다(두 번 출동 금지).

    수리 전에는 여기서 `command`가 2건이었다 — 1차 통지가 기록을 못 남겨서 중복 요청이
    재전송으로 빠졌다.
    """
    body = {
        "event_id": "orphan-resend-1",
        "gate_no": 2,
        "shuttle_no": "SHUTTLE-ORPHAN",
        "signal_ts": _now().isoformat(),
    }
    r1 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)
    r2 = await client.post("/api/shuttle-arrivals", json=body, headers=auth_headers)

    assert r1.json()["stored"] is True
    assert r2.json()["duplicate"] is True
    assert r1.headers[SHUTTLE_NOTIFY_HEADER] == "1"
    # "이미 한 대가 받았다"는 뜻의 1. 재전송을 했다는 뜻이 아니다.
    assert r2.headers[SHUTTLE_NOTIFY_HEADER] == "1"
    assert orphan_robot.count("command") == 1, "재전송이 같은 명령을 또 냈다"
    # 이 수리의 화면 대가가 0이라는 것도 같이 잰다. 기록을 남기려고 만든 행이 로봇 카드로
    # 새면 발표 화면에 빈 카드가 뜬다.
    assert (await client.get("/api/robots")).json() == [], "통지 기록용 행이 관제 목록에 떴다"


# ── 2. 통지 기록용 행이 관제 화면에 새지 않기 ──────────────────────────────

async def test_notify_row_is_recorded_but_hidden_until_status_arrives(orphan_robot, client):
    """통지 기록은 DB에 남고, 그 행은 상태 보고가 올 때까지 화면에 안 뜬다.

    수리 전에는 목록과 스냅샷에 `mode·battery·network_status`가 전부 null인 카드가 한 장 늘었다.
    """
    async with get_session() as session:
        arrival_id = await _insert_arrival(session, "orphan-hidden-1")
        delivered, _ = await notify_shuttle_arrival(
            session, arrival_id=arrival_id, gate_no=2
        )
    assert delivered == 1

    # ① 기록은 남는다 — 행이 실제로 만들어지고 통지가 그 행을 가리킨다.
    async with get_session() as session:
        robot_row = (
            await session.execute(select(Robot).where(Robot.name == ORPHAN_ROBOT))
        ).scalars().one()
        arrival = (
            await session.execute(
                select(ShuttleArrival).where(ShuttleArrival.id == arrival_id)
            )
        ).scalars().one()
    assert arrival.notified_robot_id == robot_row.id

    # ③ 그런데 화면 창구 셋 다 그 행을 안 준다.
    assert (await client.get("/api/robots")).json() == []
    snapshot = (await client.get("/api/dashboard/snapshot")).json()
    assert snapshot["robots"] == []
    assert snapshot["counts"]["robots"] == 0, "counts도 같은 모집단을 세야 한다"

    # 상태를 한 번 올리면 같은 행에 값이 차서 카드가 그때 나타난다(가리기가 영구가 아니다).
    await handle_robot_state(
        RobotStateIn(
            robot_id=ORPHAN_ROBOT, status_summary=RobotStatusSummary(battery=55)
        )
    )
    listed = (await client.get("/api/robots")).json()
    assert [(r["id"], r["name"], r["battery"]) for r in listed] == [
        (robot_row.id, ORPHAN_ROBOT, 55)
    ], "통지 때 만든 그 행에 붙어야 한다(새 행이 생기면 짝짓기가 끊긴다)"


async def test_position_only_report_keeps_card(client):
    """위치·LED만 올린 로봇은 DB 세 칸이 비어도 목록에 남는다 — 가리기 잣대가 과하지 않다.

    위치·LED는 `Robot` 컬럼이 아니라 서버 메모리 값이라, DB만 보고 거르면 이 로봇이 같이 사라진다.
    """
    async with get_session() as session:
        session.add(Robot(name="jetson-pos-only"))
        await session.commit()
    record_presentation(
        "jetson-pos-only",
        RobotStatusSummary(position=RobotPosition(x=1.5, y=2.5), led_status="GREEN"),
    )

    listed = (await client.get("/api/robots")).json()
    assert [r["name"] for r in listed] == ["jetson-pos-only"]
    assert listed[0]["led_status"] == "GREEN"
    assert listed[0]["mode"] is None, "DB 칸은 그대로 비어 있다"


# ── 3. 통계 시험자료 거르기 ────────────────────────────────────────────────

async def _seed_stats_rows(session) -> None:
    """실기기 1건 + 시험기기 1건 + 옛 실기기 1건을 같은 날(오늘 KST)에 심는다."""
    now = _now()
    old = now - dt.timedelta(days=2)
    session.add_all(
        [
            GatePassEvent(
                event_id="stats-real-1",
                device_id="raspberry01",
                gate_no=2,
                verdict="untagged",
                observed_at=now,
            ),
            GatePassEvent(
                event_id="stats-test-1",
                device_id="test-sim-a",
                gate_no=2,
                verdict="untagged",
                observed_at=now,
            ),
            GatePassEvent(
                event_id="stats-old-1",
                device_id="raspberry01",
                gate_no=2,
                verdict="untagged",
                observed_at=old,
            ),
            TaggingEvent(
                event_id="stats-tag-test-1",
                device_id="test-sim-a",
                gate_no=2,
                tag_id="TAG-T",
                observed_at=now,
            ),
            TaggingEvent(
                event_id="stats-tag-real-1",
                device_id="raspberry01",
                gate_no=2,
                tag_id="TAG-R",
                observed_at=now,
            ),
            ShuttleArrival(event_id="webcall-2-babc-1-1", gate_no=2, signal_ts=now),
            ShuttleArrival(event_id="gate2-1-0", gate_no=2, signal_ts=now),
            Alert(type="untagged", severity="warning", source_type="gate_pass", source_id=1),
        ]
    )
    await session.commit()


@pytest.fixture
def stats_filter_env(monkeypatch):
    """`STATS_*` 환경변수를 이 케이스에서만 갈아끼운다.

    ⚠ 설정은 `lru_cache`라 환경변수만 바꿔도 앞서 만들어진 값이 그대로 산다. 케이스 앞뒤로
    캐시를 비워야 스위치가 실제로 갈리고, 다음 케이스로 값이 안 샌다(검사 위생).
    """

    def apply(**env: str) -> None:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_stats_filter.cache_clear()

    get_stats_filter.cache_clear()
    try:
        yield apply
    finally:
        get_stats_filter.cache_clear()


async def test_stats_counts_everything_when_filter_off(stats_filter_env):
    """기본값(꺼짐)에서는 예전과 똑같이 전부 센다 — 계약이 조용히 안 바뀐다."""
    stats_filter_env(STATS_EXCLUDE_TEST_DATA="false")
    async with get_session() as session:
        await _seed_stats_rows(session)
        today = await today_counts(session)
        trend = await untagged_stats(session, days=7, window=3, gate_no=None)

    assert today.gate_pass.untagged == 2
    assert today.tagging == 2
    assert today.shuttle_arrival == 2
    assert today.active_alerts == 1
    assert trend.total == 3, "이틀 전 것까지 7일 구간 안이다"


async def test_stats_excludes_test_data_when_filter_on(stats_filter_env):
    """켜면 기기 접두·시각 컷으로 시험 자료가 빠진다."""
    stats_filter_env(
        STATS_EXCLUDE_TEST_DATA="true",
        STATS_TEST_DEVICE_PREFIXES="test-,webcall-",
        STATS_EXCLUDE_BEFORE=(_now() - dt.timedelta(days=1)).isoformat(),
    )
    async with get_session() as session:
        await _seed_stats_rows(session)
        today = await today_counts(session)
        trend = await untagged_stats(session, days=7, window=3, gate_no=None)

    assert today.gate_pass.untagged == 1, "test- 기기 1건이 빠져야 한다"
    assert today.gate_pass.total == 1
    assert today.tagging == 1
    assert today.shuttle_arrival == 1, "webcall- event_id 1건이 빠져야 한다"
    assert trend.total == 1, "시험기기 1건과 컷 앞 1건이 둘 다 빠져야 한다"
    # 경고는 기기 축이 없다 — 컷보다 뒤에 생긴 행이라 남는다.
    assert today.active_alerts == 1


async def test_stats_keeps_rows_without_device_id_when_filter_on(stats_filter_env):
    """`device_id`가 NULL인 예전 행은 거르기를 켜도 남는다.

    SQL에서 `device_id NOT LIKE 'test-%'`는 NULL에 NULL(=거짓)을 주므로, 가드가 없으면 기기
    이름을 안 보내던 예전 인입이 통째로 통계에서 사라진다.
    """
    stats_filter_env(
        STATS_EXCLUDE_TEST_DATA="true", STATS_TEST_DEVICE_PREFIXES="test-"
    )
    async with get_session() as session:
        session.add(
            GatePassEvent(
                event_id="stats-nodevice-1",
                device_id=None,
                gate_no=2,
                verdict="untagged",
                observed_at=_now(),
            )
        )
        await session.commit()
        today = await today_counts(session)

    assert today.gate_pass.untagged == 1


# ── 4. 시각 컷의 시간대 ────────────────────────────────────────────────────

def _naive_kst_cut(hours_ago: float) -> str:
    """지금보다 `hours_ago`시간 앞을 **KST 벽시계로 적은, 오프셋 없는** 문자열로 돌려준다.

    사람이 `.env`에 `2026-07-30T00:00:00`처럼 적는 그 모양이다. 날짜를 박지 않고 지금에서
    거꾸로 세는 이유는 하루가 지나면 깨지는 시험을 안 만들려고다.
    """
    local = (_now().astimezone(STATS_TZ_OFFSET) - dt.timedelta(hours=hours_ago))
    return local.replace(tzinfo=None).isoformat()


async def test_exclude_before_without_offset_is_read_as_kst(stats_filter_env):
    """오프셋 없는 컷은 KST로 못박힌다. 그리고 컷 경계가 정확히 그 KST 순간에 놓인다.

    ⚠ 보정이 없을 때의 예전 거동은 "UTC"가 아니라 **그 프로세스의 로컬 시간대**다 — naive
    datetime을 asyncpg가 `astimezone()`으로 로컬 tz를 붙여 보내기 때문이다. 2026-07-30 실측:
    같은 값이 한국 로케일 기계에서는 KST로, `TZ=UTC` 프로세스에서는 UTC로 읽혀 아홉 시간
    어긋났다. 그래서 이 케이스의 잣대는 `utcoffset()`이다 — 그건 어느 기계서 돌려도 같은
    답을 준다(수리 전에는 여기서 `None`이 나와 깨졌다). 로컬이 KST인 개발 기계에서는 아래
    건수 검사만으로는 예전 거동과 안 갈린다.

    건수 검사는 "컷이 KST 그 순간에 정확히 놓였나"를 잰다. 컷 10분 앞 자료와 10분 뒤 자료를
    같이 심어 뒤엣것 하나만 남는지 본다.
    """
    stats_filter_env(
        STATS_EXCLUDE_TEST_DATA="true",
        STATS_TEST_DEVICE_PREFIXES="test-",
        STATS_EXCLUDE_BEFORE=_naive_kst_cut(5),
    )
    cut = get_stats_filter().stats_exclude_before
    assert cut is not None
    assert cut.utcoffset() == dt.timedelta(hours=9), "tz가 안 붙으면 기계 로컬 tz로 읽힌다"

    async with get_session() as session:
        session.add_all(
            [
                GatePassEvent(
                    event_id="stats-kstcut-before",
                    device_id="raspberry01",
                    gate_no=2,
                    verdict="untagged",
                    observed_at=cut - dt.timedelta(minutes=10),
                ),
                GatePassEvent(
                    event_id="stats-kstcut-after",
                    device_id="raspberry01",
                    gate_no=2,
                    verdict="untagged",
                    observed_at=cut + dt.timedelta(minutes=10),
                ),
            ]
        )
        await session.commit()
        trend = await untagged_stats(session, days=7, window=3, gate_no=None)

    assert trend.total == 1, "컷 경계가 적은 KST 순간에 안 놓였다"


async def test_exclude_before_keeps_explicit_offset(stats_filter_env):
    """오프셋을 적은 값은 손대지 않는다. 보정은 tz가 없을 때만 끼어든다."""
    stats_filter_env(
        STATS_EXCLUDE_TEST_DATA="true",
        STATS_EXCLUDE_BEFORE="2026-07-30T00:00:00+00:00",
    )
    cut = get_stats_filter().stats_exclude_before
    assert cut == dt.datetime(2026, 7, 30, tzinfo=dt.timezone.utc)
