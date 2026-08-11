"""대시보드 초기 로드 스냅샷 GET /api/dashboard/snapshot 시험.

화면이 붙는 순간 한 번 읽어서 로봇 카드·미태깅 경고·이벤트 피드를 다 채울 수 있는지를 본다.
목록 API 3종과 같은 fetch 함수를 쓰므로 "스냅샷과 목록이 같은 값을 준다"까지 같이 건다.
"""
import datetime as dt

import pytest

from app.schemas import CommStatus

pytestmark = pytest.mark.asyncio(loop_scope="session")

SNAPSHOT = "/api/dashboard/snapshot"
BASE = dt.datetime(2026, 7, 27, 3, 0, 0, tzinfo=dt.timezone.utc)


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


def _tagging(event_id: str, tag_id: str, offset: float, gate_no: int = 1) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": gate_no,
        "tag_id": tag_id,
        "observed_at": _iso(offset),
    }


def _pass(event_id: str, direction: str | None, offset: float,
          status: str = "complete", gate_no: int = 1) -> dict:
    return {
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": gate_no,
        "direction": direction,
        "status": status,
        "beam_a_ts": _iso(offset),
        "beam_b_ts": _iso(offset + 0.2) if status == "complete" else None,
        "observed_at": _iso(offset),
    }


async def test_빈_상태_스냅샷_형태(client):
    """아무것도 안 들어온 상태에서도 화면이 바인딩할 칸이 전부 있어야 한다."""
    r = await client.get(SNAPSHOT)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "server_time", "ws_protocol_version", "robots",
        "active_alerts", "recent_events", "counts",
        # 이 스냅샷이 담은 마지막 사건의 시각. 화면이 WS 메시지를 "스냅샷보다 나중 소식"으로
        # 가리는 기준선이라, 담긴 게 없는 이 케이스에서는 null이다
        # (자세한 계약·불변식은 tests/test_dashboard_snapshot_cursor.py).
        "last_event_at",
        # 신원 보고 채널이 켜졌나. 꺼진 배포에서 화면이 "카드가 나갑니다"라고 얼버무리지
        # 않으려면 이 값이 필요하다.
        "identify_report_enabled",
        # 상단 기상 칩. `weather_update` WS 메시지는 값이 바뀔 때만 나가서, 새로 연 탭은
        # 다음 변화까지 칩이 비어 있었다(2026-08-03 교차 검증 W2). 아직 한 번도 못 받았으면
        # null이라 이 빈 상태 케이스에서는 null이다.
        "weather",
        # 게이트 센서가 지금 재고 있나(2026-08-07). 라파이가 터치로 켜고 끈 뒤 알려 준다.
        # ⛔ 한 번도 못 받았으면 null이고 그건 "괜찮다"가 아니라 "모른다"다.
        "gate_sensing",
        # 그 값을 마지막으로 받은 시각(2026-08-08). 값이 인메모리 "마지막 값"이라 화면이
        # "언제 기준"인지 적을 재료다. gate_sensing이 null이면 이 칸도 null이다.
        "gate_sensing_at",
    }
    assert body["robots"] == []
    assert body["active_alerts"] == []
    assert body["recent_events"] == []
    assert body["weather"] is None
    assert body["gate_sensing"] is None
    assert body["gate_sensing_at"] is None
    assert body["last_event_at"] is None
    assert body["counts"] == {"robots": 0, "active_alerts": 0, "recent_events": 0}
    # 화면이 시계 차이를 보정할 수 있게 tz를 실어 보낸다.
    assert dt.datetime.fromisoformat(body["server_time"]).tzinfo is not None


async def test_ws_버전이_메시지_version과_같다(client):
    """스냅샷을 읽은 화면이 그대로 WS를 열 수 있게 같은 버전을 준다."""
    from app.config import get_settings
    from app.ws import make_envelope

    r = await client.get(SNAPSHOT)
    assert r.json()["ws_protocol_version"] == get_settings().ws_protocol_version
    assert make_envelope("hello", {})["version"] == r.json()["ws_protocol_version"]


async def test_로봇_카드_필드(client, seed_robot):
    # ⚠ 낱말은 계약 값(`CommStatus`)으로 심는다. 예전엔 `"online"` 을 심었는데 그건 계약에
    #    없는 값이라, 카드가 그 글자를 그대로 돌려준다는 것만 재고 **실제 낱말이 흐르는지는
    #    못 쟀다.** 계약 밖 재료를 심으면 못이 헛돈다(2026-08-07 · AI 갈래가 짚음).
    robot_id = await seed_robot(name="patrol-1", mode="AUTO", battery=64,
                                network_status=CommStatus.WS_OK.value)
    body = (await client.get(SNAPSHOT)).json()
    assert body["counts"]["robots"] == 1
    card = body["robots"][0]
    assert card["id"] == robot_id
    assert card["name"] == "patrol-1"
    assert card["mode"] == "AUTO"
    assert card["battery"] == 64
    assert card["network_status"] == CommStatus.WS_OK.value
    # 값이 얼마나 오래됐나를 화면이 판단해야 해서 갱신 시각을 같이 준다.
    assert card["updated_at"] is not None
    # 목록 API와 같은 값이어야 화면이 두 그림을 안 그린다.
    assert (await client.get("/api/robots")).json() == body["robots"]


async def test_미태깅_한_건이_스냅샷에_다_뜬다(client, auth_headers):
    """시연 ④ 장면 — 태깅 없이 통과한 한 건이 경고·이벤트 양쪽에 잡혀야 한다."""
    r = await client.post("/api/gate-pass-events", json=_pass("p-untagged", "A_TO_B", 0),
                          headers=auth_headers)
    assert r.json()["verdict"] == "untagged"

    body = (await client.get(SNAPSHOT)).json()
    assert body["counts"]["active_alerts"] == 1
    alert = body["active_alerts"][0]
    assert alert["type"] == "untagged"
    assert alert["severity"] == "high"
    assert alert["source_type"] == "gate_pass"
    assert alert["ack"] is False
    assert alert["created_at"] is not None

    ev = body["recent_events"][0]
    assert ev["kind"] == "gate_pass"
    assert ev["event_id"] == "p-untagged"
    assert ev["direction"] == "A_TO_B"
    assert ev["verdict"] == "untagged"
    assert ev["gate_no"] == 1


async def test_정상_통과는_경고를_안_남긴다(client, auth_headers):
    """시연 ③ 장면 — 태깅하고 지나가면 이벤트 피드에만 찍힌다."""
    await client.post("/api/tagging-events", json=_tagging("t-ok", "UID-1", 0),
                      headers=auth_headers)
    await client.post("/api/gate-pass-events", json=_pass("p-ok", "A_TO_B", 1),
                      headers=auth_headers)

    body = (await client.get(SNAPSHOT)).json()
    assert body["counts"]["active_alerts"] == 0
    assert body["active_alerts"] == []
    kinds = {e["kind"]: e for e in body["recent_events"]}
    assert kinds["gate_pass"]["verdict"] == "normal"
    # 태깅 줄은 tag_id를 실어야 화면이 "누가 찍었나"를 보여줄 수 있다.
    assert kinds["tagging"]["tag_id"] == "UID-1"
    assert kinds["tagging"]["verdict"] is None


async def test_셔틀_도착도_이벤트_피드에_실린다(client, auth_headers):
    """시연 ① 장면 — 셔틀 도착이 대시보드에 뜬다."""
    await client.post(
        "/api/shuttle-arrivals",
        json={"event_id": "s-1", "gate_no": 2, "shuttle_no": "SH-07", "signal_ts": _iso(0)},
        headers=auth_headers,
    )
    body = (await client.get(SNAPSHOT)).json()
    ev = body["recent_events"][0]
    assert ev["kind"] == "shuttle_arrival"
    assert ev["shuttle_no"] == "SH-07"
    assert ev["gate_no"] == 2
    assert ev["direction"] is None and ev["tag_id"] is None


async def test_확인한_알림은_활성에서_빠진다(client, auth_headers):
    """시연 ⑤ 장면 — 보안 요원이 확인하면 활성 경고에서 내려간다."""
    await client.post("/api/gate-pass-events", json=_pass("p-ack", "A_TO_B", 0),
                      headers=auth_headers)
    alert_id = (await client.get(SNAPSHOT)).json()["active_alerts"][0]["id"]

    acked = await client.post(f"/api/alerts/{alert_id}/ack", headers=auth_headers)
    assert acked.status_code == 200 and acked.json()["ack"] is True

    body = (await client.get(SNAPSHOT)).json()
    assert body["active_alerts"] == []
    assert body["counts"]["active_alerts"] == 0
    # 이력은 남는다 — 목록 API는 기본이 전체다.
    assert len((await client.get("/api/alerts")).json()) == 1
    assert (await client.get("/api/alerts", params={"active": True})).json() == []


async def test_이벤트_피드는_최근순_상한이_걸린다(client, auth_headers):
    for i in range(3):
        await client.post("/api/tagging-events", json=_tagging(f"t-{i}", "UID-9", i),
                          headers=auth_headers)
    events = (await client.get("/api/events", params={"limit": 2})).json()
    assert [e["event_id"] for e in events] == ["t-2", "t-1"]
    # 상한 밖 값은 422로 막는다(스냅샷 쿼리가 통째로 끌려나오지 않게).
    assert (await client.get("/api/events", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/events", params={"limit": 9999})).status_code == 422


async def test_스냅샷_이벤트도_행_id를_싣는다(client, auth_headers):
    """스냅샷의 이벤트 줄에도 계열 테이블 행 id가 있어야 알림을 되짚는다(S15P11C207-192).

    스냅샷이 목록 API와 같은 fetch_events를 쓰니까 두 경로가 같은 id를 줘야 하고,
    미태깅 경고의 source_id가 그 id와 맞아야 화면이 "이 경고를 낸 통과"를 찾아간다.
    """
    await client.post("/api/gate-pass-events", json=_pass("p-trace", "A_TO_B", 0),
                      headers=auth_headers)

    body = (await client.get(SNAPSHOT)).json()
    ev = body["recent_events"][0]
    assert isinstance(ev["id"], int), "스냅샷 이벤트에 행 id가 안 실렸다"

    # 목록 API로 본 같은 줄과 id가 갈라지면 화면이 두 그림을 그린다.
    listed = [e for e in (await client.get("/api/events")).json()
              if e["event_id"] == "p-trace"][0]
    assert listed["id"] == ev["id"]

    alert = body["active_alerts"][0]
    assert alert["source_type"] == ev["kind"] == "gate_pass"
    assert alert["source_id"] == ev["id"]


async def test_스냅샷은_인증_없이_열린다(client, auth_headers):
    """조회 계열은 대시보드가 바로 읽는다(인입만 X-API-Key)."""
    assert (await client.get(SNAPSHOT)).status_code == 200
    # 반대로 인입은 키가 없으면 401이라는 기존 계약이 안 흔들렸는지 같이 본다.
    r = await client.post("/api/tagging-events", json=_tagging("t-noauth", "UID-2", 0))
    assert r.status_code == 401
