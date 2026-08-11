"""사건(Alert) 수명주기 — `POST /api/alerts/{id}/status`와 축 이어붙이기 (C10).

경고 행에 있던 대응 흔적은 `ack` 불리언과 신원 칸 넷뿐이었다. 서로 독립인 불리언 둘이라
"어디까지 진행됐나"를 가리키는 값이 아예 없다. 실서버 경고 18건이 전부 ack=false이고 그중
id 18은 신원까지 적힌 채 활성인데, 그게 잘못된 상태인지 정상인지 판정할 축조차 없었다.
요원이 각본 ⑤단계를 끝까지 밟아도 사건이 안 닫힌다.

## 무엇을 못박나

- 새 경고는 OPEN이고, 확인은 ACKED로, 신원 입력은 IDENTIFIED로, 종결은 RESOLVED로 올린다.
- **`ack`와 status는 안 묶는다.** 신원 입력은 여전히 ack를 안 건드린다(관제실 사람과 게이트
  안쪽 요원은 다른 사람이다). 이 계약은 test_alert_identify.py가 이미 못박아 놨고, 여기선
  status 축만 새로 잇는다.
- 전진만 되고 후퇴는 409. RESOLVED는 사유가 필수. IDENTIFIED는 상태 창구로 못 올린다.
- 같은 단계 재요청은 200 멱등이고 메시지를 더 안 낸다.
- 요원 이름(acked_by·resolved_by)이 인증 없는 응답·WS 메시지로 안 샌다.

## 수리 전 반증 (2026-07-30 실측, origin/dev eee14a0)

이름에 `반증`을 단 케이스는 수리 전 코드에서 실제로 실패하는 걸 확인했다.
- `test_반증_사건을_종결할_창구가_있다` — `POST /status`가 없어 404였다. 사건을 닫는 축이
  코드에 아예 없다는 걸 행동으로 보이는 자리다.
- `test_반증_상태로_추리면_그_단계만_온다` — `?status=` 파라미터를 서버가 조용히 무시해서
  세 건이 다 돌아왔다. 필터가 없는 게 아니라 **있는 척하고 안 듣는** 쪽이 더 위험하다.

순수 규칙(순위·정규화·전이 함수) 시험은 test_alert_lifecycle_rules.py에 갈라 뒀다.

⚠ 미태깅 경고는 같은 게이트에 10초 쿨다운이 걸려 있어서, 한 케이스에서 경고 여러 개가
필요하면 **게이트 번호를 달리 준다**(conftest가 케이스마다 쿨다운을 비우지만 케이스 안에서는
살아 있다).
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session
from app.models import Alert
from app.ws import manager

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 30, 5, 0, 0, tzinfo=dt.timezone.utc)
HEADERS = {"X-API-Key": get_settings().api_key}
FAKE_PERSON = "가명-1"


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


async def _make_untagged_alert(client, *, gate_no: int, tag: str) -> int:
    """태깅 없이 통과를 하나 넣어 미태깅 경고를 만들고 그 경고 id를 돌려준다.

    게이트마다 쿨다운이 따로라 gate_no를 달리 주면 한 케이스에서 여러 개를 만들 수 있다.
    """
    res = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": f"p-life-{tag}",
            "device_id": "raspberry01",
            "gate_no": gate_no,
            "direction": "A_TO_B",
            "status": "complete",
            "beam_a_ts": _iso(0),
            "beam_b_ts": _iso(0.2),
            "observed_at": _iso(0),
        },
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    assert res.json()["verdict"] == "untagged", res.text
    async with get_session() as session:
        row = (
            await session.execute(select(Alert).order_by(Alert.id.desc()).limit(1))
        ).scalar_one()
        return row.id


async def _row(client, alert_id: int) -> dict:
    rows = (await client.get("/api/alerts")).json()
    return [a for a in rows if a["id"] == alert_id][0]


@pytest.fixture
def sent(monkeypatch):
    """manager.broadcast를 가로채 메시지를 모은다(test_dashboard_ws.py와 같은 뼈대)."""
    box: list[dict] = []

    async def _capture(message: dict) -> None:
        box.append(message)

    monkeypatch.setattr(manager, "broadcast", _capture)
    return box


# ── 반증 (수리 전 코드에서 실패한 케이스) ────────────────────────────────────

async def test_반증_사건을_종결할_창구가_있다(client):
    """수리 전엔 이 창구가 없어 404였다 — 사건을 닫는 축이 코드에 아예 없었다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="proof-a")
    res = await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "RESOLVED", "actor": "요원A", "resolution": "false_positive"},
        headers=HEADERS,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "RESOLVED"
    assert body["resolution"] == "false_positive"
    assert body["resolved_at"] is not None


async def test_반증_상태로_추리면_그_단계만_온다(client):
    """수리 전엔 `?status=`를 서버가 조용히 무시해서 세 건이 다 돌아왔다.

    필터가 아예 없는 것보다 "있는 척하고 안 듣는" 쪽이 위험하다 — 시연 전에 "아직 안 닫힌
    사건"을 뽑았다고 믿고 다 닫은 것으로 볼 수 있다.
    """
    open_id = await _make_untagged_alert(client, gate_no=1, tag="proof-open")
    acked_id = await _make_untagged_alert(client, gate_no=2, tag="proof-acked")
    done_id = await _make_untagged_alert(client, gate_no=3, tag="proof-done")
    await client.post(f"/api/alerts/{acked_id}/ack", headers=HEADERS)
    await client.post(
        f"/api/alerts/{done_id}/status",
        json={"to": "RESOLVED", "resolution": "false_positive"},
        headers=HEADERS,
    )

    def _ids(rows):
        return {r["id"] for r in rows}

    assert _ids((await client.get("/api/alerts?status=OPEN")).json()) == {open_id}
    assert _ids((await client.get("/api/alerts?status=RESOLVED")).json()) == {done_id}
    not_closed = (await client.get("/api/alerts?status=OPEN&status=ACKED")).json()
    assert _ids(not_closed) == {open_id, acked_id}


# ── 기본 상태와 창구별 전이 ─────────────────────────────────────────────────

async def test_새_경고는_OPEN이고_미확인이다(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="fresh")
    row = await _row(client, alert_id)
    assert (row["status"], row["ack"]) == ("OPEN", False)
    assert row["acked_at"] is None and row["resolved_at"] is None
    assert row["resolution"] is None


async def test_확인_창구가_ACKED로_올린다(client):
    """예전 ack 창구가 새 축을 같이 올려야 한다 — 안 그러면 축이 둘로 갈라진다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="ack")
    body = (await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)).json()
    assert (body["status"], body["ack"]) == ("ACKED", True)
    assert body["acked_at"] is not None


async def test_신원_입력이_IDENTIFIED로_올리되_ack는_안_건드린다(client):
    """관제실에서 확인 누르는 사람과 안쪽에서 신원 적는 요원은 다른 사람이다.

    "어디까지 진행됐나"는 status가 답하고 "관제가 봤나"는 ack가 답한다. 예전엔 둘 다
    불리언이라 이 상태를 가리킬 값이 없었다(실서버 id 18).
    """
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="identify")
    body = (
        await client.post(
            f"/api/alerts/{alert_id}/identify",
            json={"person": FAKE_PERSON, "identified_by": "요원A"},
            headers=HEADERS,
        )
    ).json()
    assert body["status"] == "IDENTIFIED"
    assert body["ack"] is False, "신원 입력이 관제 확인까지 대신 찍었다"
    assert body["identified"] is True


async def test_뒤_단계_경고를_확인하면_status는_그대로고_ack만_뒤집힌다(client):
    """전진 전용이라 status는 안 밀리는데, 관제가 실제로 누른 사실은 남아야 한다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="ack-late")
    await client.post(f"/api/alerts/{alert_id}/identify",
                      json={"person": FAKE_PERSON}, headers=HEADERS)
    body = (await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)).json()
    assert body["status"] == "IDENTIFIED", "status가 뒤로 밀렸다"
    assert body["ack"] is True and body["acked_at"] is not None


async def test_확인한_경고를_다시_확인해도_상태가_안_흔들린다(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="ack2")
    await client.post(f"/api/alerts/{alert_id}/status",
                      json={"to": "RESOLVED", "resolution": "confirmed"}, headers=HEADERS)
    # 프론트가 목록 아무 줄에서 확인 버튼을 눌러도 닫힌 사건이 되열리면 안 된다.
    body = (await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)).json()
    assert body["status"] == "RESOLVED", "확인 버튼이 닫힌 사건을 되열었다"


async def test_OPEN에서_바로_종결할_수_있다(client):
    """센서 오검출 18건을 하나하나 ack부터 밟게 하면 정리를 아예 못 한다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="skip")
    body = (
        await client.post(
            f"/api/alerts/{alert_id}/status",
            json={"to": "RESOLVED", "resolution": "false_positive"},
            headers=HEADERS,
        )
    ).json()
    assert body["status"] == "RESOLVED"
    # 관제가 안 본 채로 일괄 종결한 건과 확인까지 한 건은 다른 사실이다.
    assert body["ack"] is False and body["acked_at"] is None


async def test_신원_삭제는_확인_증거를_따라_내려온다(client):
    """신원이 없는데 IDENTIFIED로 남으면 거짓말이다. 수명주기 축의 유일한 후퇴다."""
    never_acked = await _make_untagged_alert(client, gate_no=1, tag="clear-open")
    await client.post(f"/api/alerts/{never_acked}/identify",
                      json={"person": FAKE_PERSON}, headers=HEADERS)
    body = (
        await client.request("DELETE", f"/api/alerts/{never_acked}/identify", headers=HEADERS)
    ).json()
    assert body["status"] == "OPEN", "확인 안 한 경고가 ACKED로 올라갔다"
    assert body["identified"] is False

    acked = await _make_untagged_alert(client, gate_no=2, tag="clear-acked")
    await client.post(f"/api/alerts/{acked}/ack", headers=HEADERS)
    await client.post(f"/api/alerts/{acked}/identify",
                      json={"person": FAKE_PERSON}, headers=HEADERS)
    body = (
        await client.request("DELETE", f"/api/alerts/{acked}/identify", headers=HEADERS)
    ).json()
    assert body["status"] == "ACKED"


async def test_종결된_사건은_신원을_지워도_안_되열린다(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="clear-resolved")
    await client.post(f"/api/alerts/{alert_id}/identify",
                      json={"person": FAKE_PERSON}, headers=HEADERS)
    await client.post(f"/api/alerts/{alert_id}/status",
                      json={"to": "RESOLVED", "resolution": "confirmed"}, headers=HEADERS)
    body = (
        await client.request("DELETE", f"/api/alerts/{alert_id}/identify", headers=HEADERS)
    ).json()
    assert body["status"] == "RESOLVED"


# ── 창구 계약 (거절 조건) ───────────────────────────────────────────────────

async def test_사유_없이_종결하면_422(client):
    """왜 닫았는지 없이 닫으면 센서 오검출과 진짜 사건이 같은 모양으로 사라진다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="no-reason")
    res = await client.post(
        f"/api/alerts/{alert_id}/status", json={"to": "RESOLVED"}, headers=HEADERS
    )
    assert res.status_code == 422, res.text


async def test_종결이_아닌데_사유를_보내면_422(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="stray-reason")
    res = await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "ACKED", "resolution": "confirmed"},
        headers=HEADERS,
    )
    assert res.status_code == 422, res.text


async def test_모르는_종결_사유는_422(client):
    """사유가 자유 문자열이면 요원이 적은 문장이 인증 없는 목록으로 나가고 집계가 흔들린다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="bad-reason")
    res = await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "RESOLVED", "resolution": "그냥"},
        headers=HEADERS,
    )
    assert res.status_code == 422, res.text


async def test_IDENTIFIED는_상태_창구로_못_올린다(client):
    """신원 원문 없이 그 상태로 올리면 감사 기록이 빈 채로 종결 후보가 된다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="no-identified")
    res = await client.post(
        f"/api/alerts/{alert_id}/status", json={"to": "IDENTIFIED"}, headers=HEADERS
    )
    assert res.status_code == 422, res.text


async def test_모르는_상태값은_422(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="unknown")
    res = await client.post(
        f"/api/alerts/{alert_id}/status", json={"to": "CLOSED"}, headers=HEADERS
    )
    assert res.status_code == 422, res.text


async def test_후퇴_요청은_409(client):
    """감사 기록이라 되돌리기를 기본값으로 열면 닫힌 사건이 조용히 되열린다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="back")
    await client.post(f"/api/alerts/{alert_id}/status",
                      json={"to": "RESOLVED", "resolution": "duplicate"}, headers=HEADERS)
    res = await client.post(
        f"/api/alerts/{alert_id}/status", json={"to": "ACKED"}, headers=HEADERS
    )
    assert res.status_code == 409, res.text
    assert (await _row(client, alert_id))["status"] == "RESOLVED"


async def test_없는_경고는_404(client):
    res = await client.post(
        "/api/alerts/999999/status", json={"to": "ACKED"}, headers=HEADERS
    )
    assert res.status_code == 404


async def test_키_없이_전이하면_401(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="noauth")
    res = await client.post(f"/api/alerts/{alert_id}/status", json={"to": "ACKED"})
    assert res.status_code == 401
    # 실제로 안 바뀌었나까지 본다 — 401을 주고 상태만 올려두면 최악이다.
    assert (await _row(client, alert_id))["status"] == "OPEN"


# ── 멱등과 메시지 ───────────────────────────────────────────────────────────

async def test_같은_단계_재요청은_200이고_메시지를_안_낸다(client, sent):
    """화면 둘이 서로의 재전이를 받아 같은 줄을 계속 다시 그리는 걸 막는다(ack 규칙과 같다)."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="idem")
    sent.clear()

    first = await client.post(
        f"/api/alerts/{alert_id}/status", json={"to": "ACKED"}, headers=HEADERS
    )
    second = await client.post(
        f"/api/alerts/{alert_id}/status", json={"to": "ACKED"}, headers=HEADERS
    )
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["status"] == "ACKED"
    assert [m["type"] for m in sent] == ["alert_status"], "재전이가 메시지를 한 장 더 냈다"


async def test_전이_메시지에_요원_이름이_안_실린다(client, sent):
    """/ws/dashboard는 인증이 없다. acked_by·resolved_by는 우리 쪽 요원 이름이다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="ws")
    sent.clear()

    await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "RESOLVED", "actor": "요원홍길동", "resolution": "false_positive"},
        headers=HEADERS,
    )
    assert len(sent) == 1
    payload = sent[0]["data"]
    assert set(payload) == {
        "alert_id", "type", "severity", "status", "ack", "resolution", "resolved_at"
    }
    assert payload["status"] == "RESOLVED"
    assert "요원홍길동" not in str(sent), "요원 이름이 WS 메시지로 샜다"


async def test_확인_창구는_예전_메시지_한_장만_낸다(client, sent):
    """프론트 계약(test_dashboard_ws.py)이 ack 창구 메시지를 alert_ack 한 장으로 못박았다.

    셋을 alert_status 하나로 합치는 건 프론트 전환과 같이 가야 하는 팀 결정이라, 축만
    이어 두고 메시지는 안 건드렸다 — 이 시험이 그 결정을 지키는 자리다.
    """
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="ack-msg")
    sent.clear()
    await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)
    assert [m["type"] for m in sent] == ["alert_ack"]


# ── 조회 필터와 개인정보 ────────────────────────────────────────────────────

async def test_신원_단계도_추려_볼_수_있다(client):
    """요원이 신원을 적었지만 아직 안 닫힌 사건 목록 — 종결 대기 큐다."""
    identified = await _make_untagged_alert(client, gate_no=1, tag="q-identified")
    await _make_untagged_alert(client, gate_no=2, tag="q-open")
    await client.post(f"/api/alerts/{identified}/identify",
                      json={"person": FAKE_PERSON}, headers=HEADERS)
    rows = (await client.get("/api/alerts?status=IDENTIFIED")).json()
    assert {r["id"] for r in rows} == {identified}


async def test_모르는_상태로_추리면_422(client):
    res = await client.get("/api/alerts?status=낯선값")
    assert res.status_code == 422


async def test_상태_필터는_종류_필터와_같이_먹는다(client):
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="combo")
    rows = (await client.get("/api/alerts?status=OPEN&type=untagged")).json()
    assert {r["id"] for r in rows} == {alert_id}
    assert (await client.get("/api/alerts?status=OPEN&type=arrival")).json() == []


async def test_요원_이름은_인증_없는_응답에_안_실린다(client):
    """acked_by·resolved_by는 identified_by와 같은 이유로 목록·스냅샷에 안 싣는다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="privacy")
    await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)
    await client.post(
        f"/api/alerts/{alert_id}/status",
        json={"to": "RESOLVED", "actor": "요원김철수", "resolution": "confirmed"},
        headers=HEADERS,
    )
    listed = (await client.get("/api/alerts")).text
    snapshot = (await client.get("/api/dashboard/snapshot")).text
    for body in (listed, snapshot):
        assert "요원김철수" not in body
        assert "acked_by" not in body and "resolved_by" not in body

    # DB엔 남아 있어야 한다 — 안 싣는 것과 안 적는 건 다르다.
    async with get_session() as session:
        row = await session.get(Alert, alert_id)
        assert row.resolved_by == "요원김철수"


async def test_스냅샷도_수명주기_칸을_싣는다(client):
    """목록과 스냅샷이 같은 fetch 함수를 타는지 확인한다 — 갈라지면 화면이 두 그림을 그린다."""
    alert_id = await _make_untagged_alert(client, gate_no=1, tag="snap")
    await client.post(f"/api/alerts/{alert_id}/ack", headers=HEADERS)
    snap = (await client.get("/api/dashboard/snapshot")).json()
    # ack=true라 활성 목록에서는 빠진다(그 축은 예전 그대로다).
    assert snap["active_alerts"] == []

    listed = await _row(client, alert_id)
    assert listed["status"] == "ACKED"
    assert set(listed) >= {"status", "acked_at", "resolved_at", "resolution"}
