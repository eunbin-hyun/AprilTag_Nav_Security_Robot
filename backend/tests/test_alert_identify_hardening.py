"""사후 신원 확인 — 2026-07-30 검토가 잡은 결함들이 실제로 막혔나.

기본 계약은 test_alert_identify.py가 본다. 여기는 "고쳤다고 주장한 자리"만 각각 한 케이스씩
못박는다. 수리 전 코드에서 전부 실패하는 걸 확인하고 넣었다.

⚠ 실제 팀 채널로는 절대 안 보낸다. 보고 카드는 127.0.0.1 목 수신 서버로만 간다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import get_session
from app.models import Alert
from app.schemas import IDENTITY_MAX_LEN

pytestmark = pytest.mark.asyncio(loop_scope="session")

def _card_flat(payload: dict) -> str:
    """색 띠 카드(attachments)를 한 문자열로 펴서 `in` 대조에 쓴다.

    양식이 2026-07-30에 마크다운 글머리표에서 attachments로 바뀌었다. 값이 여러 칸에 흩어져서
    "이 값이 카드에 실렸나"를 보려면 제목·필드·푸터·fallback을 다 이어붙여야 한다.
    구조 자체(필드 집합·색·short 배치)는 이 헬퍼를 안 쓰고 직접 본다.
    """
    att = payload["attachments"][0]
    parts = [att.get("title") or "", att.get("text") or "", att.get("footer") or "", att.get("fallback") or ""]
    for f in att.get("fields") or []:
        parts += [f.get("title") or "", str(f.get("value") or "")]
    return "\n".join(parts)


BASE = dt.datetime(2026, 7, 30, 2, 0, 0, tzinfo=dt.timezone.utc)
FAKE_PERSON = "가명-1"
HEADERS = {"X-API-Key": get_settings().api_key}


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


# ── 목 매터모스트 ───────────────────────────────────────────────────────────

class _Recorder(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 계약
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            self.server.received.append(json.loads(raw))
        except ValueError:
            self.server.received.append({"_raw": raw.decode("utf-8", "replace")})
        try:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")
        except OSError:
            pass

    def log_message(self, *args) -> None:
        return None


class _MockMattermost(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Recorder)
        self.received: list[dict] = []

    @property
    def url(self) -> str:
        return f"http://{self.server_address[0]}:{self.server_address[1]}/hooks/mock"

    def handle_error(self, request, client_address) -> None:
        return None


@pytest.fixture
def mm_server():
    server = _MockMattermost()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def mm_config():
    """설정을 케이스 안에서만 바꾸고 되돌린다(검사 위생)."""
    settings = get_settings()
    before = (
        settings.mattermost_enabled,
        settings.mattermost_webhook_url,
        settings.mattermost_identify_webhook_url,
    )
    settings.mattermost_webhook_url = ""
    settings.mattermost_identify_webhook_url = ""
    try:
        yield settings
    finally:
        (
            settings.mattermost_enabled,
            settings.mattermost_webhook_url,
            settings.mattermost_identify_webhook_url,
        ) = before


@pytest.fixture
def make_untagged_alert(client, auth_headers):
    """미태깅 통과를 실제로 인입시켜 경고 행을 만든다."""

    async def _make(*, gate_no: int = 7, event_id: str = "p-hard-1") -> int:
        r = await client.post(
            "/api/gate-pass-events",
            json={"event_id": event_id, "device_id": "raspberry01", "gate_no": gate_no,
                  "direction": "A_TO_B", "status": "complete",
                  "beam_a_ts": _iso(0), "beam_b_ts": _iso(0.2), "observed_at": _iso(0)},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["verdict"] == "untagged"
        async with get_session() as s:
            return (
                await s.execute(select(Alert).where(Alert.type == "untagged"))
            ).scalars().one().id

    return _make


async def _load(alert_id: int) -> Alert:
    async with get_session() as s:
        return await s.get(Alert, alert_id)


# ── 인증 ────────────────────────────────────────────────────────────────────

async def test_키_없이_쓰기_창구를_부르면_401이다(client):
    """404보다 401이 먼저 나와야 한다.

    404가 떴다는 건 인증을 안 거치고 핸들러까지 갔다는 뜻이다 — 있는 번호였다면 그대로
    써진다. 수리 전엔 identify도 ack도 키 없이 404가 떴다(인터넷에서 실측).
    """
    assert (
        await client.post("/api/alerts/999999/identify", json={"person": FAKE_PERSON})
    ).status_code == 401
    assert (await client.post("/api/alerts/999999/ack")).status_code == 401
    assert (await client.get("/api/alerts/999999/identify")).status_code == 401
    assert (await client.delete("/api/alerts/999999/identify")).status_code == 401


async def test_틀린_키도_401이다(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()
    resp = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401, resp.text
    assert (await _load(alert_id)).identified_at is None


async def test_관제보조_3종도_키를_요구한다(client):
    """GMS 크레딧을 태우는 갈래라 인터넷에 열어 두면 팀 크레딧이 빨린다."""
    assert (await client.get("/api/assistant/daily-summary")).status_code == 401
    assert (await client.get("/api/assistant/alert-brief/1")).status_code == 401
    assert (
        await client.post("/api/assistant/chat", json={"question": "상황"})
    ).status_code == 401


# ── 동시 입력 ───────────────────────────────────────────────────────────────

async def test_동시_입력이_409_게이트를_못_뚫는다(client, make_untagged_alert, mm_config):
    """읽고→검사하고→쓰는 사이에 잠금이 없으면 409가 통째로 새어 나간다.

    수리 전 실측 — 같은 경고에 동시 6건을 쏘니 200이 다섯, DB엔 임의의 한 명만 남고 상위
    보고선엔 서로 다른 신원 카드가 "(정정)" 표시 없이 다섯 장 붙었다.

    ⚠ 커넥션 풀을 먼저 데워야 이 검사가 헛돌지 않는다. 풀이 차갑던 첫 실험에서는 뒤따르는
    다섯 요청이 새 커넥션을 여느라 줄을 서서, 수리 전 코드인데도 [200, 409×5]가 나왔다 —
    잠금이 있어서가 아니라 경합 자체가 안 일어난 것이다. 풀을 데우고 나서야 수리 전 코드가
    [200×5, 409]로 실제 결함을 드러냈다.
    """
    alert_id = await make_untagged_alert()
    await asyncio.gather(*[client.get("/healthz") for _ in range(8)])
    results = await asyncio.gather(*[
        client.post(
            f"/api/alerts/{alert_id}/identify",
            json={"person": f"가명-{i}"},
            headers=HEADERS,
        )
        for i in range(6)
    ])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409, 409, 409, 409], codes

    row = await _load(alert_id)
    winner = [r for r in results if r.status_code == 200][0]
    assert winner.json()["id"] == alert_id
    assert row.identified_person is not None


# ── 경고 종류 ───────────────────────────────────────────────────────────────

async def test_미태깅이_아닌_경고엔_못_적는다(client, mm_config):
    """카드 제목이 "미태깅 통과자 신원 확인" 고정이라, 딴 경고에 붙으면 보고선이 엉뚱한
    사건을 받는다. 계약이 화면 쪽에만 있으면 계약이 아니다."""
    async with get_session() as s:
        alert = Alert(type="robot_arrival", severity="info")
        s.add(alert)
        await s.commit()
        alert_id = alert.id

    r = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS
    )
    assert r.status_code == 400, r.text
    assert (await _load(alert_id)).identified_at is None


# ── 입력 무해화 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "person",
    [
        "정상값\n- 게이트: 99번\n- 신원: 조작된사람\n@channel",   # 카드 줄 위조
        "가명\r\n두번째줄",
        "가명\x00널",                                              # asyncpg가 500으로 튀던 값
        "가명\x07벨",
        "가명‮뒤집기",                                        # 양방향 재정렬
        "가명​제로폭",
    ],
)
async def test_구조_문자는_422로_막는다(client, make_untagged_alert, mm_config, person):
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": person}, headers=HEADERS
    )
    assert r.status_code == 422, r.text
    assert (await _load(alert_id)).identified_at is None


async def test_메모와_요원_칸도_같은_검사를_탄다(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()
    for field in ("identified_by", "note"):
        r = await client.post(
            f"/api/alerts/{alert_id}/identify",
            json={"person": FAKE_PERSON, field: "값\n- 위조줄"},
            headers=HEADERS,
        )
        assert r.status_code == 422, (field, r.text)
    assert (await _load(alert_id)).identified_at is None


async def test_붙여넣기로_딸려온_공백은_길이에_안_센다(client, make_untagged_alert, mm_config):
    """길이 검사가 털기 앞에 걸리면 실사용 값 5자가 공백 때문에 422로 막힌다."""
    alert_id = await make_untagged_alert()
    padded = " " * (IDENTITY_MAX_LEN + 20) + "가명-99"
    r = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": padded}, headers=HEADERS
    )
    assert r.status_code == 200, r.text
    assert (await _load(alert_id)).identified_person == "가명-99"


async def test_카드에_위조된_줄이_안_생긴다(client, make_untagged_alert, mm_config, mm_server):
    """스키마가 개행을 막지만, 카드를 만드는 자리도 두 번째 문을 둔다.

    ⚠ 이 케이스는 _build_identify_card를 직접 부른다 — API로는 개행이 422라 여기까지 안 온다.
    카드 조립이 나중에 다른 경로(배치 재발송)에서 불릴 때를 대비한 문이다.
    """
    from app.notify import _build_identify_card

    card = _build_identify_card(
        gate_no=7,
        alert_id=1,
        identified_person="정상값\n- 게이트: 99번\n- 신원: 조작된사람",
        identified_by="요원\n@channel",
        identified_at=BASE,
        note="[링크](http://evil.example)",
        corrected=False,
    )
    att = card["attachments"][0]
    fields = att["fields"]
    # 필드 수가 고정이면 값이 새 칸을 못 만든 것이다(신원·게이트·시각·요원 + 메모).
    assert len(fields) == 5, fields
    by_title = {f["title"]: f["value"] for f in fields}
    assert set(by_title) == {"신원", "게이트", "확인 시각", "확인한 요원", "메모"}, by_title
    # 값이 게이트 칸을 위조하지 못했다.
    assert by_title["게이트"] == "7번", by_title["게이트"]
    # 손으로 적은 값은 인라인 코드 안이라 마크다운도 @멘션도 안 살아난다.
    #
    # ⛔ **"확인 시각"도 여기 있어야 한다.** 예전에는 이 목록에서 빠져 있었고, 그래서 그 칸만
    # 맨 텍스트로 나가는 것을 아무도 못 잡았다 — 매터모스트가 `00:11:27`의 `:11:`을 이모지로
    # 바꿔 분이 사라졌다(2026-08-06). 값을 손으로 안 적는 칸이라 안전해 보이지만, 사람이
    # 안 적어도 **콜론이 들어가는 순간 마크다운 사정권**이다.
    for key in ("신원", "확인 시각", "확인한 요원", "메모"):
        assert by_title[key].startswith("`") and by_title[key].endswith("`"), (key, by_title[key])
    # 개행이 값에 남아도 구조를 못 깬다 — 필드가 별 칸이지만 무해화가 개행 자체를 걷는다.
    for key in ("신원", "확인한 요원", "메모"):
        assert "\n" not in by_title[key], (key, by_title[key])
    # fallback은 코드 스팬을 안 붙이는 자리라(푸시 미리보기) 제어문자 무해화만 걸린다.
    assert "\n" not in att["fallback"], att["fallback"]
    assert "@channel" not in att["fallback"]


# ── 고쳐 적기 ───────────────────────────────────────────────────────────────

async def test_고쳐_적을_때_안_보낸_칸은_안_지워진다(client, make_untagged_alert, mm_config):
    """네 칸을 무조건 대입하면 person만 담아 정정했을 때 요원 이름·메모가 같이 사라진다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-1", "identified_by": "요원-기존", "note": "메모-기존"},
        headers=HEADERS,
    )
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-2", "overwrite": True},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    row = await _load(alert_id)
    assert row.identified_person == "가명-2"
    assert row.identified_by == "요원-기존", "안 보낸 칸이 지워졌다"
    assert row.identify_note == "메모-기존", "안 보낸 칸이 지워졌다"


async def test_빈_값으로_보낸_칸은_지워진다(client, make_untagged_alert, mm_config):
    """"안 보냈다"와 "비워서 보냈다"는 다른 뜻이다 — 지우는 길도 있어야 한다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-1", "identified_by": "요원-기존", "note": "메모-기존"},
        headers=HEADERS,
    )
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-2", "note": "", "overwrite": True},
        headers=HEADERS,
    )
    row = await _load(alert_id)
    assert row.identify_note is None
    assert row.identified_by == "요원-기존"


async def test_정정_카드는_병합된_값을_싣는다(
    client, make_untagged_alert, mm_config, mm_server
):
    """요청 본문만 보고 카드를 만들면 정정 때 안 보낸 칸이 "미기재"로 보고된다."""
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-1", "identified_by": "요원-기존"},
        headers=HEADERS,
    )
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-2", "overwrite": True},
        headers=HEADERS,
    )
    assert len(mm_server.received) == 2
    assert "요원-기존" in _card_flat(mm_server.received[1])
    assert "미기재" not in _card_flat(mm_server.received[1])


# ── 신원 읽기·지우기 ────────────────────────────────────────────────────────

async def test_인증된_창구로_원문을_되읽는다(client, make_untagged_alert, mm_config):
    """이 창구가 없으면 신원이 write-only다 — 웹훅이 꺼진 배포에선 아무도 못 읽는다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "메모칸"},
        headers=HEADERS,
    )
    body = (await client.get(f"/api/alerts/{alert_id}/identify", headers=HEADERS)).json()
    assert body["identified"] is True
    assert body["identified_person"] == FAKE_PERSON
    assert body["identified_by"] == "요원-김"
    assert body["identify_note"] == "메모칸"


async def test_신원을_지우면_NULL로_돌아간다(client, make_untagged_alert, mm_config):
    """수리 전엔 되돌릴 길이 alembic downgrade(컬럼 통째 drop)뿐이었다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "메모칸"},
        headers=HEADERS,
    )
    r = await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["identified"] is False

    row = await _load(alert_id)
    assert (row.identified_person, row.identified_by, row.identify_note, row.identified_at) == (
        None, None, None, None
    )
    # 지운 뒤엔 409 없이 다시 적힌다.
    again = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": "가명-9"}, headers=HEADERS
    )
    assert again.status_code == 200, again.text


async def test_지우기는_보고선으로_신원을_안_흘린다(
    client, make_untagged_alert, mm_config, mm_server
):
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS
    )
    mm_server.received.clear()
    r = await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert mm_server.received == []


# ── 대시보드 알림 ───────────────────────────────────────────────────────────

async def test_신원을_적으면_대시보드에_메시지가_나간다(
    client, auth_headers, make_untagged_alert, mm_config
):
    """ack가 S15P11C207-81에서 고친 것과 같은 문제다 — 두 화면이 갈린다.

    ⚠ 봉투 `data`에 신원 원문·요원 이름이 실리면 안 된다. /ws/dashboard는 인증이 없다.
    """
    from app.ws import manager

    sent: list[dict] = []

    class _Sock:
        async def send_json(self, message):
            sent.append(message)

    sock = _Sock()
    manager._dashboard.add(sock)
    try:
        alert_id = await make_untagged_alert()
        sent.clear()
        r = await client.post(
            f"/api/alerts/{alert_id}/identify",
            json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "메모칸"},
            headers=HEADERS,
        )
        assert r.status_code == 200, r.text
    finally:
        manager._dashboard.discard(sock)

    types = [m["type"] for m in sent]
    assert types == ["alert_identified"], types
    payload = sent[0]["data"]
    assert set(payload) == {
        "alert_id", "type", "severity", "identified", "identified_at", "corrected",
        "staff_linked",
    }
    assert payload["alert_id"] == alert_id
    assert payload["identified"] is True
    assert payload["corrected"] is False
    # 학번을 안 보냈으니 이을 대상이 없다.
    assert payload["staff_linked"] is False
    blob = json.dumps(sent, ensure_ascii=False)
    assert FAKE_PERSON not in blob
    assert "요원-김" not in blob
    assert "메모칸" not in blob


# ── 보고 채널 켜짐 노출 ─────────────────────────────────────────────────────

async def test_보고_채널이_켜졌나를_화면이_알_수_있다(client, mm_config, mm_server):
    """꺼진 배포에서 요원이 "보냈다"고 믿는 순간이 실제로는 무발사였다(운영 실측)."""
    assert (await client.get("/healthz")).json()["identify_report_enabled"] is False
    snap = (await client.get("/api/dashboard/snapshot")).json()
    assert snap["identify_report_enabled"] is False

    mm_config.mattermost_identify_webhook_url = mm_server.url
    assert (await client.get("/healthz")).json()["identify_report_enabled"] is True
    snap = (await client.get("/api/dashboard/snapshot")).json()
    assert snap["identify_report_enabled"] is True


# ── 미태깅만 추리기 ─────────────────────────────────────────────────────────

async def test_경고_목록을_종류로_추릴_수_있다(client, auth_headers, make_untagged_alert, mm_config):
    """미태깅만 모은 목록이 없어서 요원이 실시간 피드에서 눈으로 찾아야 했다.

    피드는 오래된 행부터 버리므로 조금만 지나면 손댈 방법이 아예 사라진다.
    """
    await make_untagged_alert()
    async with get_session() as s:
        s.add(Alert(type="robot_arrival", severity="info"))
        await s.commit()

    everything = (await client.get("/api/alerts")).json()
    assert {a["type"] for a in everything} == {"untagged", "robot_arrival"}

    only = (await client.get("/api/alerts", params={"type": "untagged"})).json()
    assert [a["type"] for a in only] == ["untagged"]


# ── 신원 삭제 감사 — 행위자 표기와 원문 비보존 ──────────────────────────────


async def _identity_clear_rows() -> list:
    from app.models import StaffAudit

    async with get_session() as s:
        return (
            (
                await s.execute(
                    select(StaffAudit)
                    .where(StaffAudit.action == "identity_clear")
                    .order_by(StaffAudit.id)
                )
            )
            .scalars()
            .all()
        )


async def _leader_cookie(username: str = "leaderghost") -> dict[str, str]:
    """팀장 계정 하나를 만들고 그 세션 쿠키를 돌려준다.

    토큰은 `secrets.token_urlsafe`라 늘 ASCII다 — 헤더 값은 ascii로 인코드되므로 한글을
    넣으면 시험이 서버가 아니라 클라이언트에서 터진다.
    """
    from app import auth
    from app.models import AppUser

    async with get_session() as s:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password("pw-12345678"),
            display_name=f"이름-{username}",
            role=auth.ROLE_LEADER,
            is_active=True,
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        token = await auth.create_session(s, user)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


async def test_키로_통과한_삭제는_감사에_사람_이름을_안_남긴다(
    client, make_untagged_alert, mm_config
):
    """⭐ 게이트와 감사 행위자가 **같은 의존성**에서 나와야 한다(검토 F17 수리).

    예전에는 게이트가 `_leader_or_key`, 행위자가 `require_leader`로 갈려 있었다. 플래그가
    꺼진 구간에서 `require_leader`는 판정을 아예 안 하고 쿠키 주인만 알려 주므로, 기기 키로
    통과한 요청에 요원·팀장 쿠키가 얹혀 있기만 하면 **급 판정을 한 번도 안 거친 사람**이
    감사에 행위자로 실렸다. 감사에서 제일 나쁜 거짓말이다 — 안 한 사람을 한 사람으로 적는다.

    지금은 키 갈래의 행위자가 `None`이라 네 칸이 NULL로 남는다. 그 NULL 자체가 "로그인
    전에 기기 키로 벌어진 일"이라는 기록이다.
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"

    alert_id = await make_untagged_alert()
    ghost = await _leader_cookie()
    await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS
    )

    # 키로 통과시키면서 쿠키도 같이 싣는다 — 옛 코드가 이름을 주워 담던 바로 그 모양이다.
    r = await client.delete(
        f"/api/alerts/{alert_id}/identify", headers={**HEADERS, **ghost}
    )
    assert r.status_code == 200, r.text

    rows = await _identity_clear_rows()
    assert len(rows) == 1
    row = rows[0]
    assert (
        row.actor_user_id,
        row.actor_username,
        row.actor_display_name,
        row.actor_role,
    ) == (None, None, None, None), "판정을 안 거친 쿠키 주인이 행위자로 실렸다"


async def test_감사에_지운_신원_원문이_안_남는다(client, make_untagged_alert, mm_config):
    """⭐ `staff_audit`에는 삭제 창구가 없다 — 원문을 복사하면 영구 보존이다(검토 F8 수리).

    이 창구가 신원의 유일한 파기 수단인데(보관 기간·일괄 파기 규칙이 아직 없다), 지우면서
    같은 값을 감사 표에 옮겨 적으면 파기했다고 믿은 신원이 지워지지 않은 채로 남는다.

    그래도 "무엇이 지워졌나"는 남는다 — 이름은 첫 글자와 길이, 메모는 길이다.
    """
    alert_id = await make_untagged_alert()
    note = "메모칸-내용-길다"
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": note},
        headers=HEADERS,
    )
    assert (
        await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)
    ).status_code == 200

    rows = await _identity_clear_rows()
    assert len(rows) == 1
    before = rows[0].before
    dumped = json.dumps(before, ensure_ascii=False)
    assert FAKE_PERSON not in dumped, f"신원 원문이 감사에 남았다: {dumped}"
    assert note not in dumped, f"메모 원문이 감사에 남았다: {dumped}"

    # 무엇이 지워졌나는 남는다.
    assert before["identified_person"] == {
        "initial": FAKE_PERSON[0],
        "length": len(FAKE_PERSON),
    }
    assert before["identify_note"] == {"length": len(note)}
    # 요원 실명은 "누가 적었나"라 그대로 둔다(행위자 칸과 같은 계열이다).
    assert before["identified_by"] == "요원-김"
