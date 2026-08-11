"""스냅샷 기준 시각(`last_event_at`) 계약 시험 — 순서 어긋남을 화면이 가릴 수 있게 하는 축.

## 무엇을 막는 시험인가

화면은 WS를 먼저 열고 스냅샷을 읽는다. 그 사이에 들어온 사건은 WS로 **먼저** 도착하는데,
뒤늦게 온 스냅샷이 그 자리를 낡은 값으로 덮어 버렸다. 응답에 "이 스냅샷이 어디까지
담았나"가 없어서 화면이 가릴 방법이 없었기 때문이다.

`last_event_at`이 그 기준선이고, 계약은 한 줄이다.

    스냅샷에 안 담긴 변화는 **반드시** WS 봉투 `timestamp` > `last_event_at` 이다.

여기 케이스들이 그 불변식을 양쪽에서 조인다.
  - 안 담긴 사건(스냅샷 뒤 인입)은 방송 시각이 기준선보다 **늦다** → 화면이 살린다.
  - 담긴 사건도 방송 시각이 기준선보다 늦거나 같다 → 화면이 한 번 더 그리지만 event_id로
    걸러진다. 안전한 쪽으로만 틀리게 만든 것이다.
  - 기준선은 **서버 현재 시각이 아니다**(`server_time`보다 이르다). 조회를 끝낸 시각을
    실으면 조회 사이에 커밋된 사건까지 "이미 담겼다"로 읽혀서 계약이 뒤집힌다.
  - 조회 순서가 뒤집히면 위 사슬이 끊긴다 → 순서도 못박는다.

⚠ WS 봉투 `timestamp`는 API 프로세스 시계고 `last_event_at`은 DB 시계(`now()`)다. 같은
호스트라야 견줄 수 있다는 전제가 이 시험에도 그대로 깔린다(DashboardSnapshot 주석).
"""
import datetime as dt

import pytest

from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

SNAPSHOT = "/api/dashboard/snapshot"
BASE = dt.datetime(2026, 7, 27, 3, 0, 0, tzinfo=dt.timezone.utc)


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


def _tagging(event_id: str, offset: float = 0) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 1,
        "tag_id": "UID-1",
        "observed_at": _iso(offset),
    }


def _ts(envelope: dict) -> dt.datetime:
    return dt.datetime.fromisoformat(envelope["timestamp"])


@pytest.fixture
def sent(monkeypatch):
    """방송을 가로채 봉투를 모은다(test_dashboard_ws.py와 같은 수법).

    실 소켓을 붙이면 시험이 자기 이벤트 루프를 새로 열어 asyncpg 풀이 딴 루프에 묶인다.
    """
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


async def test_기준시각은_담긴_마지막_사건의_수신시각(client, auth_headers):
    """`last_event_at`은 `recent_events[0].received_at`과 같은 값이다."""
    await client.post("/api/tagging-events", json=_tagging("cur-a", 0),
                      headers=auth_headers)
    await client.post("/api/tagging-events", json=_tagging("cur-b", 1),
                      headers=auth_headers)

    body = (await client.get(SNAPSHOT)).json()
    assert body["recent_events"][0]["event_id"] == "cur-b"
    assert body["last_event_at"] == body["recent_events"][0]["received_at"]
    assert dt.datetime.fromisoformat(body["last_event_at"]).tzinfo is not None


async def test_기준시각은_서버_현재시각이_아니다(client, auth_headers):
    """조회를 끝낸 시각을 실으면 안 된다 — 그러면 조회 사이 사건까지 '담겼다'로 읽힌다."""
    await client.post("/api/tagging-events", json=_tagging("cur-not-now"),
                      headers=auth_headers)

    body = (await client.get(SNAPSHOT)).json()
    cursor = dt.datetime.fromisoformat(body["last_event_at"])
    server_time = dt.datetime.fromisoformat(body["server_time"])
    assert cursor < server_time, "기준 시각이 서버 현재 시각으로 밀렸다"


async def test_스냅샷_뒤_사건은_기준시각보다_늦게_방송된다(client, auth_headers, sent):
    """⭐ 핵심 불변식 — 스냅샷이 안 담은 사건은 방송 시각이 기준선보다 늦다.

    이게 깨지면 화면이 새 값을 '이미 스냅샷에 있다'로 잘못 가리고, 경고는 다시 방송되지
    않으므로 그대로 사라진다.
    """
    await client.post("/api/tagging-events", json=_tagging("cur-before", 0),
                      headers=auth_headers)
    cursor = dt.datetime.fromisoformat(
        (await client.get(SNAPSHOT)).json()["last_event_at"]
    )

    sent.clear()
    await client.post("/api/tagging-events", json=_tagging("cur-after", 1),
                      headers=auth_headers)
    after = [m for m in sent if m["type"] == "tagging_event"]
    assert len(after) == 1
    assert _ts(after[0]) > cursor, "스냅샷에 없는 사건이 기준선보다 이르게 방송됐다"


async def test_담긴_사건도_방송이_기준선보다_늦다(client, auth_headers, sent):
    """담긴 사건은 중복으로 한 번 더 그려질 뿐이다 — 틀리는 방향이 안전한 쪽인지 본다.

    `received_at`은 DB 시계의 트랜잭션 시작 시각이고 방송은 커밋 뒤라, 같은 사건이면 늘
    방송이 나중이다. 그래서 화면은 '못 가리고 한 번 더 그림'(event_id로 걸러짐)으로만
    틀리고, '있는데 가려서 잃음'으로는 안 틀린다.
    """
    await client.post("/api/tagging-events", json=_tagging("cur-dup"),
                      headers=auth_headers)
    broadcast = [m for m in sent if m["type"] == "tagging_event"][0]

    body = (await client.get(SNAPSHOT)).json()
    cursor = dt.datetime.fromisoformat(body["last_event_at"])
    assert _ts(broadcast) >= cursor
    # 중복 판정 키는 그대로 살아 있어야 화면이 걸러낸다.
    assert body["recent_events"][0]["event_id"] == broadcast["data"]["event_id"]


async def test_이벤트_조회가_맨_앞이다(client, auth_headers, monkeypatch):
    """⭐ 조회 순서를 못박는다 — 기준선의 근거가 '이벤트를 먼저 읽는다'이기 때문이다.

    로봇·경고를 먼저 읽으면 그 조회와 이벤트 조회 사이에 커밋된 변화가 스냅샷엔 없는데
    기준선은 그 뒤로 밀려서, 화면이 그 변화를 '담겼다'로 잘못 가린다. 로봇 카드는 다음
    보고가 덮어 주지만 경고는 재방송이 없어 영영 사라진다.
    """
    from app.routers import query as q

    order: list[str] = []

    def _spy(name: str, fn):
        async def _wrapped(*args, **kwargs):
            order.append(name)
            return await fn(*args, **kwargs)
        return _wrapped

    for name in ("fetch_events", "fetch_robots", "fetch_alerts"):
        monkeypatch.setattr(q, name, _spy(name, getattr(q, name)))

    await client.post("/api/tagging-events", json=_tagging("cur-order"),
                      headers=auth_headers)
    assert (await client.get(SNAPSHOT)).status_code == 200
    assert order[0] == "fetch_events", f"이벤트가 맨 앞이 아니다: {order}"


async def test_이벤트_피드_정렬축에_인덱스가_있다():
    """`(received_at, id)` 인덱스 셋이 실물 스키마에 있나(마이그레이션 0010).

    이게 없으면 피드 UNION이 3표를 통째로 훑고 정렬한다 — 실측으로 스냅샷 이벤트 조회가
    0.055 ms에서 27 ms로 간다(0010 머리의 실측표). 느려질 뿐 답은 맞아서 다른 시험은
    아무도 안 깨지므로, 축이 사라지는 걸 여기서만 잡는다.

    ⚠ 계획(EXPLAIN)이 아니라 **인덱스 존재**를 본다. 행이 적은 시험 DB에서는 옵티마이저가
    Seq Scan을 고르는 게 정상이라 계획으로 재면 가짜 실패가 난다. 모델과 마이그레이션이
    같은지는 tests/test_schema_drift.py가 따로 본다.
    """
    from sqlalchemy import text

    from app.db import engine

    expected = {
        "ix_tagging_event_received_at_id": "tagging_event",
        "ix_gate_pass_event_received_at_id": "gate_pass_event",
        "ix_shuttle_arrival_received_at_id": "shuttle_arrival",
    }
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "SELECT indexname, tablename, indexdef FROM pg_indexes "
            "WHERE indexname = ANY(:names)"
        ), {"names": list(expected)})).all()

    found = {r.indexname: (r.tablename, r.indexdef) for r in rows}
    assert set(found) == set(expected), f"빠진 인덱스: {set(expected) - set(found)}"
    for name, table in expected.items():
        assert found[name][0] == table
        # 칸 순서가 뒤집히면 `ORDER BY received_at, id`가 인덱스 순서를 못 탄다.
        assert "(received_at, id)" in found[name][1], found[name][1]
