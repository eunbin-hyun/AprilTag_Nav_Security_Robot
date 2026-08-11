"""`gate_pass_event`와 `untagged_alert`를 잇는 열쇠(`pass_id`) 계약.

## 왜 이 파일이 따로 있나

프론트는 미태깅 경고를 **바로 앞에 온 통과**에 묶어 왔다. 두 메시지를 잇는 칸이 없어서다.
그 가정은 지금 우연히 맞다 — `ingest_gate_pass_event` 한 요청 안에서 통과 봉투를 먼저 보내고
미태깅이면 경고를 잇달아 보낸다. **그런데 게이트가 늘거나 통과 두 건이 동시에 들어오면
엉뚱한 통과에 붙는다.** 도착 순서는 계약이 아니라 지금 구현의 부산물이다.

그래서 못을 여기 따로 박는다. `test_dashboard_ws.py`는 "어떤 칸이 실리나"(칸 집합)를 재고,
이 파일은 **"두 봉투의 값이 같은가"**를 잰다. 칸 집합만 재면 서버가 `pass_id`에 엉뚱한 수를
실어도 초록이다.

## 무엇을 재나

1. 통과 봉투의 `pass_id`가 경고 봉투의 `source_id`와 **같은 값**이다(핵심 계약).
2. 그 값이 진짜 통과 행 PK다 — `GET /api/alerts?source_type=gate_pass&source_id=...`가
   그 경고를 실제로 집어 온다. 두 봉투에 같은 상수를 실어도 1번은 통과하니까, 값이 DB
   행을 가리킨다는 것까지 따로 잰다.
3. 정상 통과에도 칸이 실린다(경고가 안 나가는 갈래에서도 화면이 칸 유무를 검사할 일이 없다).
"""
from __future__ import annotations

import datetime as dt

import pytest

from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 8, 4, 3, 0, 0, tzinfo=dt.timezone.utc)


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


@pytest.fixture
def sent(monkeypatch):
    """manager.broadcast를 가로채 메시지를 모은다(test_dashboard_ws.py와 같은 방식)."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


def _pass_body(event_id: str, gate_no: int, offset: float) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": gate_no,
        "direction": "A_TO_B",
        "status": "complete",
        "beam_a_ts": _iso(offset),
        "beam_b_ts": _iso(offset + 0.2),
        "observed_at": _iso(offset),
    }


def _one(box: list[dict], type_: str) -> dict:
    hits = [m["data"] for m in box if m["type"] == type_]
    assert len(hits) == 1, f"{type_} 메시지가 {len(hits)}장이다: {[m['type'] for m in box]}"
    return hits[0]


async def _skew_sequences(client, auth_headers) -> None:
    """`gate_pass_event.id`와 `alert.id` 시퀀스를 일부러 어긋내 놓는다.

    ⚠ **이걸 안 하면 못이 헐겁다(실측).** 케이스마다 표를 TRUNCATE로 비우니 두 시퀀스가
    같은 눈금에서 출발해서, 통과 한 건만 넣으면 `pass_id`도 `alert_id`도 똑같이 1이다.
    그 상태에서는 서버가 `pass_id` 자리에 **`alert_id`를 잘못 실어도 시험이 초록이다**
    (수리 뒤 일부러 바꿔 넣어 재현했다).

    그래서 경고를 안 내는 통과를 먼저 몇 건 넣는다. `status=incomplete`는 방향 판별 불가라
    판정 없이 행만 남고 Alert를 안 만든다 — 통과 시퀀스만 앞서간다.
    """
    for i in range(2):
        resp = await client.post(
            "/api/gate-pass-events",
            json={
                "event_id": f"p-skew-{i}",
                "device_id": "raspberry01",
                "gate_no": 5,
                "status": "incomplete",
                "observed_at": _iso(i),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text


async def test_미태깅이면_통과의_pass_id와_경고의_source_id가_같다(
    client, auth_headers, sent
):
    """⭐ 이 수리의 핵심 계약. 값이 어긋나면 화면이 남의 통과에 경고를 붙인다."""
    await _skew_sequences(client, auth_headers)
    sent.clear()

    resp = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-link-1", 1, 3), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text

    passed = _one(sent, "gate_pass_event")
    alert = _one(sent, "untagged_alert")

    assert passed["verdict"] == "untagged"
    assert isinstance(passed["pass_id"], int)
    assert alert["source_type"] == "gate_pass"
    # 도착 순서가 아니라 값으로 묶인다.
    assert passed["pass_id"] == alert["source_id"]
    # 시퀀스를 어긋내 놨으니 두 수가 다르다 — `alert_id`를 잘못 실으면 위 줄이 깨진다.
    assert passed["pass_id"] != alert["alert_id"], "시퀀스가 안 어긋났다 — 못이 헐겁다"


async def test_pass_id가_진짜_통과_행_PK다(client, auth_headers, sent):
    """두 봉투에 같은 상수를 실어도 위 케이스는 초록이다 — 값이 DB 행을 가리키는지 따로 잰다.

    경고 되짚기 축(`?source_type=&source_id=`)이 그 경고를 집어 오면 `pass_id`가 실제
    `gate_pass_event` 행 PK라는 뜻이다(역추적 규칙 S15P11C207-192).
    """
    await _skew_sequences(client, auth_headers)
    sent.clear()

    resp = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-link-2", 2, 3), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    passed = _one(sent, "gate_pass_event")
    alert = _one(sent, "untagged_alert")

    traced = await client.get(
        "/api/alerts", params={"source_type": "gate_pass", "source_id": passed["pass_id"]}
    )
    assert traced.status_code == 200, traced.text
    ids = [row["id"] for row in traced.json()]
    assert ids == [alert["alert_id"]], f"pass_id가 그 경고를 못 집었다: {traced.json()}"

    # 이벤트 피드 쪽 (kind, id)와도 같은 값이다 — 화면이 표 한 줄에 경고를 붙이는 길.
    events = await client.get("/api/events", params={"kind": "gate_pass", "limit": 50})
    assert events.status_code == 200, events.text
    row = next(e for e in events.json() if e["event_id"] == "p-link-2")
    assert row["id"] == passed["pass_id"]


async def test_정상_통과에도_pass_id가_실린다(client, auth_headers, sent):
    """경고가 안 나가는 갈래에서도 칸은 있다 — 화면이 칸 유무를 먼저 검사할 일이 없다."""
    tag = await client.post(
        "/api/tagging-events",
        json={"event_id": "t-link-3", "device_id": "raspberry01", "gate_no": 3,
              "tag_id": "UID-LINK", "observed_at": _iso(0)},
        headers=auth_headers,
    )
    assert tag.status_code == 200, tag.text
    sent.clear()

    resp = await client.post(
        "/api/gate-pass-events", json=_pass_body("p-link-3", 3, 1), headers=auth_headers
    )
    assert resp.status_code == 200, resp.text

    passed = _one(sent, "gate_pass_event")
    assert passed["verdict"] == "normal"
    assert isinstance(passed["pass_id"], int)
    # 정상 통과는 경고가 없다.
    assert [m["type"] for m in sent] == ["gate_pass_event"]


async def test_통과_두_건이_섞여_와도_각자_짝이_맞는다(client, auth_headers, sent):
    """도착 순서 추측이 깨지는 바로 그 판. 게이트가 둘이면 봉투 넷이 뒤섞여 나간다.

    지금 구현은 요청 하나에 두 장을 붙여 보내지만, 그건 계약이 아니다. 값으로 묶으면
    묶는 쪽이 순서를 몰라도 된다 — 그걸 여기서 잰다.
    """
    await _skew_sequences(client, auth_headers)
    sent.clear()

    for i, gate in enumerate((7, 8)):
        resp = await client.post(
            "/api/gate-pass-events",
            json=_pass_body(f"p-link-mix-{gate}", gate, 3 + i),
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text

    passes = {m["data"]["gate_no"]: m["data"] for m in sent if m["type"] == "gate_pass_event"}
    alerts = {m["data"]["gate_no"]: m["data"] for m in sent if m["type"] == "untagged_alert"}
    assert set(passes) == set(alerts) == {7, 8}
    assert passes[7]["pass_id"] == alerts[7]["source_id"]
    assert passes[8]["pass_id"] == alerts[8]["source_id"]
    # 서로 다른 행이다 — 한 값을 두 경고가 같이 가리키면 위 두 줄이 우연히 통과한다.
    assert passes[7]["pass_id"] != passes[8]["pass_id"]
