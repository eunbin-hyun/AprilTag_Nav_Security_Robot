"""미태깅 통과자 사후 신원 확인 — `POST /api/alerts/{id}/identify`.

미태깅 통과는 태그를 안 댔으니 시스템에 신원 근거가 없다. 게이트에서 안 막고 흘려보낸 뒤
안쪽 요원이 붙잡아 확인한 신원을 여기로 적어 넣는다.

⚠ **실제 팀 채널로는 절대 안 보낸다.** 보고 카드는 전부 127.0.0.1에 띄운 목 수신 서버로만
간다(test_notify_mattermost.py와 같은 규칙). 신원 값도 전부 가짜다.

무엇을 못박나.
- 정상 입력이 DB에 남고, 조회 응답에 확인 상태가 뜬다.
- 없는 경고 번호는 404, 길이 초과는 422, 이미 적힌 경고는 409(overwrite=true면 통과).
- 두 번째 웹훅 URL이 비면 조용히 넘어가고 입력은 그대로 남는다(기본값이 이 상태다).
- 웹훅이 5xx를 줘도 입력 자체는 성공으로 남는다 — 커밋 뒤에 부르니까.
- 인증 없는 조회 API에 신원 원문이 안 샌다(개인정보 최소).
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from sqlalchemy import select

from app import notify
from app.config import get_settings
from app.db import get_session
from app.models import Alert
from app.schemas import IDENTITY_MAX_LEN, IDENTITY_NOTE_MAX_LEN

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


pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 30, 2, 0, 0, tzinfo=dt.timezone.utc)
# 신원 갈래는 전부 인증이 걸렸다. 키 없이 부르면 401이라 시험도 키를 실어야 한다.
HEADERS = {"X-API-Key": get_settings().api_key}
# 시연에서 권하는 표기다. 실명·학번을 넣을지는 팀 결정이라 시험도 가명만 쓴다.
FAKE_PERSON = "가명-1"


def _iso(offset_sec: float) -> str:
    return (BASE + dt.timedelta(seconds=offset_sec)).isoformat()


# ── 목 매터모스트 (test_notify_mattermost.py와 같은 뼈대) ────────────────────

class _Recorder(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 계약
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            self.server.received.append(json.loads(raw))
        except ValueError:
            self.server.received.append({"_raw": raw.decode("utf-8", "replace")})
        try:
            self.send_response(self.server.status)
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
        self.status = 200

    @property
    def url(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}/hooks/identify-mock"

    def handle_error(self, request, client_address) -> None:
        return None


@pytest.fixture
def mm_server():
    """로컬 목 매터모스트. 반드시 여기서 닫는다 — 스레드가 새면 다음 케이스가 흔들린다."""
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
    """설정을 케이스 안에서만 바꾸고 되돌린다. get_settings는 lru_cache라 인스턴스가 하나다.

    미태깅 URL도 같이 비운다 — 이 파일은 신원 채널만 보는데, 다른 케이스가 남긴 미태깅 URL이
    살아 있으면 인입 관통 픽스처가 엉뚱한 자리로 카드를 쏜다(검사 위생).
    """
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


# ── 경고 하나 만들기 ────────────────────────────────────────────────────────

@pytest.fixture
def make_untagged_alert(client, auth_headers):
    """미태깅 통과를 실제로 인입시켜 경고 행을 만든다. 경고 id를 돌려준다.

    Alert를 직접 INSERT하지 않는 이유 — 신원 카드에 실리는 게이트 번호는 경고가 아니라
    발화 이벤트에서 되짚는다. 이벤트 없이 심으면 그 되짚기 경로가 시험 그물 밖으로 샌다.
    """

    async def _make(*, gate_no: int = 7, event_id: str = "p-ident-1") -> int:
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
            alert = (
                await s.execute(select(Alert).where(Alert.type == "untagged"))
            ).scalars().one()
            return alert.id

    return _make


async def _load(alert_id: int) -> Alert:
    async with get_session() as s:
        return await s.get(Alert, alert_id)


# ── 정상 입력 ───────────────────────────────────────────────────────────────

async def test_신원_입력이_경고에_남는다(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()

    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "게이트 안쪽에서 확인"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    row = await _load(alert_id)
    assert row.identified_person == FAKE_PERSON
    assert row.identified_by == "요원-김"
    assert row.identify_note == "게이트 안쪽에서 확인"
    assert row.identified_at is not None


# ── 학번 칸 (2026-08-06 사용자 확정 · 프론트 27차) ──────────────────────────


async def test_학번을_이름과_따로_적는다(client, make_untagged_alert, mm_config):
    """⭐ 학번이 진짜 신원 근거다 — 이름은 동명이인이 있고 명부 대조도 학번으로 한다.

    그전까지 화면이 `person` 한 칸에 "이름 · 학번"을 이어 보내고 숫자를 되뽑아 썼다.
    """
    alert_id = await make_untagged_alert()

    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    row = await _load(alert_id)
    assert row.identified_person == FAKE_PERSON, "이름 칸에 학번이 섞였다"
    assert row.identified_student_no == "0912345"


async def test_학번은_안_적어도_된다(client, make_untagged_alert, mm_config):
    """⚠ 필수로 만들면 현장에서 이름만 알아낸 갈래가 통째로 막힌다."""
    alert_id = await make_untagged_alert()

    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    assert (await _load(alert_id)).identified_student_no is None


async def test_이름만_고쳐_적으면_학번은_그대로다(client, make_untagged_alert, mm_config):
    """⛔ 안 보낸 칸을 덮으면 정정 한 번에 앞서 적힌 학번이 사라진다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )

    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-99", "overwrite": True},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    row = await _load(alert_id)
    assert row.identified_person == "가명-99"
    assert row.identified_student_no == "0912345", "안 보낸 학번이 지워졌다"


async def test_조회_창구가_학번을_돌려준다(client, make_untagged_alert, mm_config):
    """⚠ 출력 칸 이름은 `identified_student_no` 다 — 입력(`student_no`)과 다르다.

    다른 신원 칸도 같은 규칙이다(`person` → `identified_person`).
    """
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )

    r = await client.get(f"/api/alerts/{alert_id}/identify", headers=HEADERS)

    assert r.status_code == 200, r.text
    assert r.json()["identified_student_no"] == "0912345"


async def test_신원을_지우면_학번도_같이_사라진다(client, make_untagged_alert, mm_config):
    """⛔ 이 창구가 신원 파기의 유일한 수단이다. 한 칸이라도 남으면 "지웠다"가 거짓이 된다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )

    r = await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)

    assert r.status_code == 200, r.text
    row = await _load(alert_id)
    assert row.identified_person is None
    assert row.identified_student_no is None, "파기했는데 학번이 남았다"


async def test_감사에는_학번_원문이_안_남는다(client, make_untagged_alert, mm_config):
    """⛔ **학번은 첫 글자도 안 남긴다.** 앞자리가 입학 연도·학과라 몇 글자로 사람이 좁혀진다.

    `staff_audit` 에는 삭제 창구가 없어서, 파기 기록에 원문을 담으면 그 값이 영구히 남는다.
    """
    from app.models import StaffAudit

    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )
    await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)

    async with get_session() as s:
        row = (
            await s.execute(
                select(StaffAudit).where(StaffAudit.action == "identity_clear")
            )
        ).scalars().one()

    masked = row.before["identified_student_no"]
    assert masked == {"length": 7}, masked
    assert "0912345" not in str(row.before), "학번 원문이 감사에 남았다"


# ── 명부 자동 잇기 · 학번으로 경고 찾기 (프론트 27차 §3-2) ──────────────────


async def _add_staff(name: str, student_no: str | None, *, active: bool = True) -> int:
    from app.models import Staff

    async with get_session() as s:
        row = Staff(name=name, student_no=student_no, is_active=active)
        s.add(row)
        await s.commit()
        return row.id


async def test_학번이_명부에_있으면_경고를_그_사람에_잇는다(
    client, make_untagged_alert, mm_config
):
    """⭐ 지금까지는 요원이 화면에서 눈으로만 대조하고 서버에 아무 기록도 안 남았다."""
    staff_id = await _add_staff("김요원", "0912345")
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )

    assert (await _load(alert_id)).staff_id == staff_id


async def test_해제된_명부_행에도_잇는다(client, make_untagged_alert, mm_config):
    """해제는 삭제가 아니다 — 그 사람 학번으로 경고가 났을 때야말로 이름을 알아야 한다."""
    staff_id = await _add_staff("퇴사자", "0900001", active=False)
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0900001"},
        headers=HEADERS,
    )

    assert (await _load(alert_id)).staff_id == staff_id


async def test_같은_학번이_여럿이면_안_잇는다(client, make_untagged_alert, mm_config):
    """⛔ 명부 `student_no` 에는 유니크 제약이 없다. 아무나 고르면 **엉뚱한 사람이
    무단 통과자로 기록된다** — 못 고르는 것이 잘못 고르는 것보다 낫다."""
    await _add_staff("동명이번-A", "0977777")
    await _add_staff("동명이번-B", "0977777")
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0977777"},
        headers=HEADERS,
    )

    assert (await _load(alert_id)).staff_id is None, "둘 중 아무나 골랐다"


async def test_조회_응답이_명부에_이었는지를_알려준다(
    client, make_untagged_alert, mm_config
):
    """⭐ 프론트 31차 §4-2 — 화면이 자기 명부 사본으로 이름을 말하다 서버와 어긋나는 것을 막는다."""
    await _add_staff("김요원", "0912345")
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )

    body = (await client.get(f"/api/alerts/{alert_id}/identify", headers=HEADERS)).json()
    assert body["staff_linked"] is True
    # 명부 내용은 안 실린다 — 이 창구가 학번에서 이름을 뽑는 두 번째 길이 되면 안 된다.
    assert "김요원" not in json.dumps(body, ensure_ascii=False)


async def test_같은_학번이_여럿이면_조회_응답도_안_이었다고_말한다(
    client, make_untagged_alert, mm_config
):
    """⛔ 화면이 제일 헷갈리는 갈래다 — **학번은 멀쩡히 적혀 있는데 서버는 안 이었다.**

    이 칸이 없으면 화면은 자기 명부에서 둘 중 하나를 골라 "명부에 있습니다 — ○○○"이라
    적고, 서버 기록에는 아무도 안 이어져 있다. 두 쪽이 다른 말을 한다.
    """
    await _add_staff("동명이번-A", "0977777")
    await _add_staff("동명이번-B", "0977777")
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0977777"},
        headers=HEADERS,
    )

    body = (await client.get(f"/api/alerts/{alert_id}/identify", headers=HEADERS)).json()
    assert body["identified_student_no"] == "0977777", "학번은 적혔어야 한다"
    assert body["staff_linked"] is False, "안 이었는데 이었다고 말한다"


async def test_신원을_파기하면_명부_연결도_안_이었다고_말한다(
    client, make_untagged_alert, mm_config
):
    """파기는 학번과 연결을 같이 지운다(`_link_staff_by_student_no`). 조회도 따라가야 한다."""
    await _add_staff("김요원", "0912345")
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )
    assert (
        await client.get(f"/api/alerts/{alert_id}/identify", headers=HEADERS)
    ).json()["staff_linked"] is True

    await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)

    body = (await client.get(f"/api/alerts/{alert_id}/identify", headers=HEADERS)).json()
    assert body["staff_linked"] is False
    assert body["identified_student_no"] is None


async def test_학번을_고치면_연결도_따라간다(client, make_untagged_alert, mm_config):
    """⛔ 안 따라가면 그 경고가 계속 딴 사람을 가리킨다."""
    first = await _add_staff("먼저", "0911111")
    second = await _add_staff("나중", "0922222")
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0911111"},
        headers=HEADERS,
    )
    assert (await _load(alert_id)).staff_id == first

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0922222", "overwrite": True},
        headers=HEADERS,
    )

    assert (await _load(alert_id)).staff_id == second


async def test_신원을_지우면_명부_연결도_끊긴다(client, make_untagged_alert, mm_config):
    """⛔ 안 끊으면 **파기했다는 신원이 명부 행을 통해 그대로 되짚힌다.**"""
    await _add_staff("지울사람", "0933333")
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0933333"},
        headers=HEADERS,
    )
    assert (await _load(alert_id)).staff_id is not None

    await client.delete(f"/api/alerts/{alert_id}/identify", headers=HEADERS)

    assert (await _load(alert_id)).staff_id is None, "파기했는데 명부 연결이 남았다"


async def test_학번으로_그_사람_경고를_찾는다(client, make_untagged_alert, mm_config):
    """⭐ "이 사람이 전에도 무단 통과했나"를 요원이 그 자리에서 본다.

    ⚠ 경고를 하나만 만든다 — `make_untagged_alert` 가 `.one()` 이라 이 파일에서는 경고가
    둘이 되면 픽스처 자체가 터진다. 여기서 재는 것은 **학번으로 걸러지나**이고, 그건 한
    건으로도 잡힌다(딴 학번으로 물으면 안 나와야 한다).
    """
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0955555"},
        headers=HEADERS,
    )

    r = await client.get("/api/alerts/by-student-no/0955555", headers=HEADERS)

    assert r.status_code == 200, r.text
    assert [row["id"] for row in r.json()] == [alert_id]
    # ⚠ 신원 원문은 안 실린다 — 실으면 이 창구 하나로 "학번 → 이름"이 뽑힌다.
    assert "identified_person" not in r.json()[0]

    # 딴 학번으로 물으면 안 나온다. 이게 없으면 "아무 학번이나 전부 준다"도 초록이다.
    other = await client.get("/api/alerts/by-student-no/0966666", headers=HEADERS)
    assert other.json() == []


async def test_없는_학번은_404가_아니라_빈_목록이다(client):
    """404 로 가르면 "학번은 있는데 경고가 없다"와 "학번 자체가 없다"가 밖에서 갈린다."""
    r = await client.get("/api/alerts/by-student-no/0000000", headers=HEADERS)

    assert r.status_code == 200, r.text
    assert r.json() == []


async def test_학번_경고_조회는_익명에_안_열린다(client):
    """⛔ 열면 **"이 학번이 무단 통과했나"를 아무나 물어볼 수 있다.**"""
    r = await client.get("/api/alerts/by-student-no/0912345")

    assert r.status_code == 401, r.text


async def test_입력_응답에_확인_상태가_실린다(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김"},
        headers=HEADERS,
    )
    body = r.json()
    assert body["identified"] is True
    assert body["identified_at"] is not None
    # 확인한 요원 이름은 이 응답에 안 싣는다 — 같은 스키마를 인증 없는 목록이 쓴다.
    assert "identified_by" not in body
    # ack는 다른 사건이라 안 건드린다.
    assert body["ack"] is False


async def test_확인_시각은_서버가_찍는다(client, make_untagged_alert, mm_config):
    """요청이 시각을 보내도 안 먹는다 — 감사 기록의 시각을 부르는 쪽이 정하면 못 믿는다."""
    alert_id = await make_untagged_alert()
    before = dt.datetime.now(dt.timezone.utc)
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_at": "2001-01-01T00:00:00+00:00"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    row = await _load(alert_id)
    assert row.identified_at >= before - dt.timedelta(seconds=5)


async def test_옵션_칸은_빈_문자열을_안_적은_것으로_본다(client, make_untagged_alert, mm_config):
    """화면 입력 상자를 비워 두면 ""가 올라온다. 422로 막으면 '옵션'이 거짓말이 된다."""
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "", "note": "   "},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    row = await _load(alert_id)
    assert row.identified_by is None
    assert row.identify_note is None


# ── 거절 갈래 ───────────────────────────────────────────────────────────────

async def test_없는_경고_번호는_404(client, mm_config):
    r = await client.post("/api/alerts/999999/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    assert r.status_code == 404, r.text


async def test_길이_초과는_422(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가" * (IDENTITY_MAX_LEN + 1)},
        headers=HEADERS,
    )
    assert r.status_code == 422, r.text
    row = await _load(alert_id)
    assert row.identified_at is None, "거절된 입력이 경고에 남았다"


async def test_상한_길이는_통과한다(client, make_untagged_alert, mm_config):
    """경계값 — 초과만 막고 상한 자체는 받는다."""
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가" * IDENTITY_MAX_LEN, "note": "나" * IDENTITY_NOTE_MAX_LEN},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text


async def test_메모_길이_초과도_422(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "note": "나" * (IDENTITY_NOTE_MAX_LEN + 1)},
        headers=HEADERS,
    )
    assert r.status_code == 422, r.text


async def test_공백만_친_신원은_422(client, make_untagged_alert, mm_config):
    """공백이 저장되면 '신원이 적혔다'는 상태가 거짓이 되고 그대로 보고까지 나간다."""
    alert_id = await make_untagged_alert()
    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": "   "}, headers=HEADERS)
    assert r.status_code == 422, r.text
    row = await _load(alert_id)
    assert row.identified_at is None


async def test_이미_적힌_경고는_409(client, make_untagged_alert, mm_config):
    alert_id = await make_untagged_alert()
    first = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON},
        headers=HEADERS,
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": "가명-2"},
        headers=HEADERS,
    )
    assert second.status_code == 409, second.text

    row = await _load(alert_id)
    assert row.identified_person == FAKE_PERSON, "막았는데도 값이 덮였다"


async def test_overwrite면_고쳐_적힌다(client, make_untagged_alert, mm_config):
    """오타 정정 경로. 명시 플래그가 있을 때만 열린다."""
    alert_id = await make_untagged_alert()
    await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-2", "overwrite": True},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    row = await _load(alert_id)
    assert row.identified_person == "가명-2"


# ── 두 번째 웹훅 ────────────────────────────────────────────────────────────

async def test_두번째_웹훅이_비면_조용히_넘어간다(client, make_untagged_alert, mm_config):
    """지금 기본 상태다 — 채널을 아직 안 만들었으니 URL이 비어 있다."""
    assert mm_config.mattermost_identify_webhook_url == ""
    alert_id = await make_untagged_alert()

    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    assert r.status_code == 200, r.text
    row = await _load(alert_id)
    assert row.identified_person == FAKE_PERSON, "웹훅이 꺼져 있다고 입력까지 사라졌다"


async def test_기본값이_꺼짐인지_직접_본다():
    """설정 기본값 자체를 못박는다 — 시험 픽스처가 비운 값을 보고 착각하면 안 된다."""
    from app.config import Settings

    fresh = Settings(_env_file=None)
    assert fresh.mattermost_identify_webhook_url == ""
    assert fresh.mattermost_identify_active is False


async def test_URL을_넣으면_보고_카드가_날아간다(
    client, make_untagged_alert, mm_config, mm_server
):
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert(gate_no=9)

    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "메모칸"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text

    assert len(mm_server.received) == 1, "신원이 들어왔는데 보고가 안 나갔다"
    text = _card_flat(mm_server.received[0])
    assert "9번" in text, "게이트 번호를 발화 이벤트에서 못 되짚었다"
    assert "요원-김" in text
    assert FAKE_PERSON in text          # 보고선이 목적이라 신원은 안 가린다
    assert str(alert_id) in text
    assert "메모칸" in text
    assert "KST" in text, "확인 시각이 안 실렸다"
    assert "(정정)" not in text

    # 사용자가 2026-07-30에 고른 양식 — 색 띠 카드에 값을 두 칸으로 갈라 넣는다.
    att = mm_server.received[0]["attachments"][0]
    assert att["color"] == "#f65b4e", "색 띠가 붉은색이 아니다"
    assert FAKE_PERSON in att["fallback"], "카드를 못 그리는 클라이언트가 신원을 못 읽는다"
    titles = [f["title"] for f in att["fields"]]
    assert titles == ["신원", "게이트", "확인 시각", "확인한 요원", "메모"], titles
    # 앞 넷은 두 칸 정렬, 메모만 한 줄을 다 쓴다.
    assert [f["short"] for f in att["fields"]] == [True, True, True, True, False]
    assert f"경고 {alert_id}번" in att["footer"]


async def test_값이_없는_칸은_필드를_아예_안_만든다(
    client, make_untagged_alert, mm_config, mm_server
):
    """두 칸 정렬이라 빈 칸이 남으면 줄이 어긋나 보인다. 그래서 빈 필드를 넣지 않고 뺀다."""
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS
    )
    assert r.status_code == 200, r.text

    fields = mm_server.received[0]["attachments"][0]["fields"]
    titles = [f["title"] for f in fields]
    assert titles == ["신원", "게이트", "확인 시각", "확인한 요원"], titles
    assert "메모" not in titles, "메모를 안 적었는데 빈 필드가 생겼다"
    # 요원을 안 적으면 칸은 남기고 값만 '미기재'다 — 두 칸 정렬을 지키려고 이 칸은 안 뺀다.
    by_title = {f["title"]: f["value"] for f in fields}
    assert by_title["확인한 요원"] == "미기재"


async def test_미태깅_채널로는_신원이_안_간다(
    client, make_untagged_alert, mm_config, mm_server
):
    """채널 분리의 핵심 — 미태깅 URL만 켠 배포에 신원이 새면 안 된다."""
    mm_config.mattermost_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()
    mm_server.received.clear()  # 인입 때 나간 미태깅 카드는 여기 관심사가 아니다

    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert mm_server.received == [], "신원 보고가 미태깅 채널로 샜다"


async def test_정정_보고는_카드에_표시된다(client, make_untagged_alert, mm_config, mm_server):
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()
    await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": "가명-2", "overwrite": True},
        headers=HEADERS,
    )
    assert len(mm_server.received) == 2
    assert "(정정)" in _card_flat(mm_server.received[1])
    assert "가명-2" in _card_flat(mm_server.received[1])
    # 정정 표시는 제목에 붙는다 — 색은 처음 보고와 같게 둔다(같은 사건이라 색을 바꾸면 딴 일로 읽힌다).
    assert "(정정)" in mm_server.received[1]["attachments"][0]["title"]
    assert mm_server.received[1]["attachments"][0]["color"] == "#f65b4e"


async def test_카드에_학번이_따로_적힌다(client, make_untagged_alert, mm_config, mm_server):
    """⭐ 프론트 27차 §3-2가 요청한 자리. 이름 칸에 섞이면 상위 보고선이 대조를 못 한다.

    ⚠ 값이 **인라인 코드 안**이어야 한다. 학번은 숫자라 매터모스트가 `:11:` 같은 조각을
    이모지로 읽는 갈래에 그대로 노출된다(같은 날 확인 시각이 그렇게 먹혔다).
    """
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "student_no": "0912345"},
        headers=HEADERS,
    )

    fields = {f["title"]: f["value"] for f in mm_server.received[0]["attachments"][0]["fields"]}
    assert fields["학번"] == "`0912345`", fields
    assert fields["신원"] == f"`{FAKE_PERSON}`", "이름 칸에 학번이 섞였다"


async def test_학번이_없으면_카드에_그_칸이_안_생긴다(
    client, make_untagged_alert, mm_config, mm_server
):
    """두 칸 정렬이라 빈 칸이 남으면 줄이 어긋나 보인다 — 메모 칸과 같은 잣대다."""
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()

    await client.post(
        f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS
    )

    titles = [f["title"] for f in mm_server.received[0]["attachments"][0]["fields"]]
    assert "학번" not in titles, titles


async def test_409로_막힌_건_보고가_안_나간다(client, make_untagged_alert, mm_config, mm_server):
    mm_config.mattermost_identify_webhook_url = mm_server.url
    alert_id = await make_untagged_alert()
    await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": "가명-2"}, headers=HEADERS)
    assert r.status_code == 409
    assert len(mm_server.received) == 1, "저장도 안 된 값이 보고선으로 나갔다"


async def test_웹훅이_5xx여도_입력은_성공으로_남는다(
    client, make_untagged_alert, mm_config, mm_server
):
    """웹훅은 커밋 뒤에 부른다 — 보고 실패가 요원의 입력을 되돌리면 안 된다."""
    mm_config.mattermost_identify_webhook_url = mm_server.url
    mm_server.status = 500
    alert_id = await make_untagged_alert()

    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["identified"] is True

    row = await _load(alert_id)
    assert row.identified_person == FAKE_PERSON, "웹훅 5xx가 입력까지 되돌렸다"
    assert len(mm_server.received) == 1  # 몸통은 갔고 판정만 실패다


async def test_웹훅이_죽어도_입력은_남는다(client, make_untagged_alert, mm_config, monkeypatch):
    """연결 거부가 그대로 예외로 올라오는 경로. 아무도 안 듣는 자리로 쏜다."""
    monkeypatch.setattr(notify, "MATTERMOST_TIMEOUT_S", 0.2)
    mm_config.mattermost_identify_webhook_url = "http://127.0.0.1:9/hooks/dead"
    alert_id = await make_untagged_alert()

    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    assert r.status_code == 200, r.text
    row = await _load(alert_id)
    assert row.identified_person == FAKE_PERSON


async def test_킬스위치가_신원_보고도_멈춘다(client, make_untagged_alert, mm_config, mm_server):
    """MATTERMOST_ENABLED=false는 매터모스트 발사 전체를 멈추는 스위치다.

    신원 카드만 몰래 나가면 그 스위치가 거짓말이 된다.
    """
    mm_config.mattermost_identify_webhook_url = mm_server.url
    mm_config.mattermost_enabled = False
    alert_id = await make_untagged_alert()

    r = await client.post(f"/api/alerts/{alert_id}/identify", json={"person": FAKE_PERSON}, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert mm_server.received == []


# ── 개인정보 최소 ───────────────────────────────────────────────────────────

async def test_조회_API에_신원_원문이_안_샌다(client, make_untagged_alert, mm_config):
    """GET /api/alerts·스냅샷은 인증이 없다. 원문을 실으면 누구나 읽는다."""
    alert_id = await make_untagged_alert()
    await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "메모칸"},
        headers=HEADERS,
    )

    listed = (await client.get("/api/alerts")).json()
    row = [a for a in listed if a["id"] == alert_id][0]
    assert row["identified"] is True
    assert "identified_person" not in row
    assert "identify_note" not in row
    # 확인한 요원 이름도 뺀다 — identified_at과 짝을 이루면 근무 기록이 인터넷에 나간다.
    assert "identified_by" not in row

    snap = (await client.get("/api/dashboard/snapshot")).text
    assert FAKE_PERSON not in snap
    assert "메모칸" not in snap
    assert "요원-김" not in snap


async def test_입력_응답에도_신원_원문이_없다(client, make_untagged_alert, mm_config):
    """부른 쪽이 이미 아는 값이고, 응답 본문은 프록시 로그에 남을 자리가 더 많다."""
    alert_id = await make_untagged_alert()
    r = await client.post(
        f"/api/alerts/{alert_id}/identify",
        json={"person": FAKE_PERSON, "note": "메모칸"},
        headers=HEADERS,
    )
    assert FAKE_PERSON not in r.text
    assert "메모칸" not in r.text


async def test_로그에_신원_값이_안_찍힌다(client, make_untagged_alert, mm_config, caplog):
    """운영 중 로그가 신원 목록이 되면 안 된다. 남기는 건 경고 번호와 글자 수까지다.

    우리 로거(c207.*)만 본다. 루트를 통째로 DEBUG로 열면 SQLAlchemy 엔진이 SQL 바인딩
    파라미터를 찍어서 신원이 거기 딸려 나온다 — 그건 우리 로그 문장 문제가 아니라
    DB 드라이버 레벨 얘기라 여기서 판정할 대상이 아니다(운영에선 WARNING이다).
    """
    alert_id = await make_untagged_alert()
    with caplog.at_level("DEBUG", logger="c207.query"), \
            caplog.at_level("DEBUG", logger="c207.notify"):
        await client.post(
            f"/api/alerts/{alert_id}/identify",
            json={"person": FAKE_PERSON, "identified_by": "요원-김", "note": "메모칸"},
        headers=HEADERS,
    )
    ours = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("c207.")
    )
    assert "경고" in ours, "신원 입력이 로그를 아예 안 남겼다(검사가 헛돌고 있다)"
    assert FAKE_PERSON not in ours
    assert "메모칸" not in ours
    assert "요원-김" not in ours
