"""개발자 텔레메트리 페이지 (S15P11C207-157).

FastAPI가 직접 서빙하는 단일 HTML이 뜨고, 대시보드 WS를 구독하는 코드가 실려 있는지 본다.

⚠ **자물쇠는 여기서 안 잰다.** 2026-08-06에 게이트가 `require_admin_always`로 올라가면서
익명 호출이 401이 됐는데, 이 파일이 재는 것은 **페이지 내용**이다. 게이트 판정은
`tests/test_auth_gates.py` 몫이다(플래그 꺼짐·켜짐, 요원 403, 관리자 200을 거기서 전부 센다).
그래서 아래 픽스처로 게이트만 열고 내용을 본다 — `tests/test_weather.py`가 같은 방식이다.
"""
import shutil

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture(autouse=True)
def _open_dev_gate():
    """이 파일 동안만 개발자 페이지 게이트를 연다. 끝나면 원상복구한다.

    ⚠ `yield` 뒤에서 반드시 되돌린다. 안 되돌리면 같은 세션의 다른 파일이 "게이트가 열린
    앱"을 물려받아, 자물쇠가 통째로 사라져도 시험이 초록으로 남는다.
    """
    from app.auth import require_admin_always
    from app.main import app

    app.dependency_overrides[require_admin_always] = lambda: None
    try:
        yield
    finally:
        app.dependency_overrides.pop(require_admin_always, None)


async def test_telemetry_page_served(client):
    r = await client.get("/dev/telemetry")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/html")
    assert "C207 개발자 텔레메트리" in r.text


async def test_telemetry_page_subscribes_dashboard_ws(client):
    """프론트 프레임워크 없이 바닐라 JS로 /ws/dashboard를 구독한다."""
    body = (await client.get("/dev/telemetry")).text
    assert "/ws/dashboard" in body
    assert "new WebSocket" in body
    # 이번에 붙은 메시지 종류가 화면에 반영돼야 한다.
    assert "robot_state" in body
    assert "robot_arrival" in body
    # 프레임워크·CDN 반입 금지 — 외부 스크립트 태그가 없어야 한다.
    assert "<script src=" not in body


async def test_telemetry_page_renders_departure_envelope(client):
    """출발 메시지도 화면에 잡혀야 한다 — 색 규칙이랑 from/to 전이 표시까지.

    출발은 `mission_status`가 null로 오니까(전이는 from/to로 온다) 종류별 처리가 없으면
    화면이 도착만 그리고 출발은 아무 표시 없이 흘려버린다.
    """
    body = (await client.get("/dev/telemetry")).text
    assert "robot_departure" in body
    # 색 규칙(CSS)과 종류별 문구가 둘 다 있어야 행이 도착과 구분된다.
    assert ".t-robot_departure" in body
    assert "게이트 출발" in body
    # 전이 표시 — 출발 행은 from_status·to_status로만 상태를 알 수 있다.
    assert "function transitionText(" in body
    assert "from_status" in body and "to_status" in body


async def test_telemetry_page_reads_today_stats_api(client):
    """오늘 집계는 서버가 센 값을 읽는다. 목록에서 세면 조회 상한(500)에 걸려 적게 나온다."""
    body = (await client.get("/dev/telemetry")).text
    assert "/api/stats/today" in body
    assert "function countFromServer(" in body
    # 날짜도 서버가 준 값을 쓴다 — 브라우저 시계가 틀어져도 화면과 집계가 안 갈린다.
    assert "got.today.date" in body


async def test_telemetry_page_falls_back_when_today_stats_missing(client):
    """집계 API가 없거나 실패해도 화면이 죽지 않는다 — 목록에서 세는 예전 방식으로 내려앉는다.

    그때는 상한에 걸릴 수 있다는 사실을 안내 줄에 그대로 적어야 한다. 안 적으면
    "서버가 센 수"와 "잘린 목록에서 센 수"가 같은 자리에 구별 없이 뜬다.
    """
    body = (await client.get("/dev/telemetry")).text
    assert "function countFromEvents(" in body
    assert "server || countFromEvents(evs)" in body
    assert "조회 상한 " in body


async def test_telemetry_page_uses_robot_status_for_card(client):
    """로봇 카드의 정본은 `robot_status` 메시지다 (S15P11C207-81).

    `robot_state`(원본 중계)에서 카드 값을 뽑으면 "안 실린 필드는 안 덮는다"는 절대 상태
    규칙이 서버·화면 두 벌로 갈라진다. 카드에 없는 칸(odom·imu·sensor_health)만 원본에서 읽는다.

    ⚠ 미션 상태는 2026-08-01에 카드로 들어왔는데(프론트 요구 N10), 이 개발자 화면은 아직
    원본에서만 미션 타일을 채운다. 서버 계약과 화면이 어긋나는 게 아니라 화면이 아직 새 칸을
    안 쓰는 것뿐이다.
    """
    body = (await client.get("/dev/telemetry")).text
    assert "robot_status" in body
    assert "function applyRobotStatus(" in body
    # 스냅샷 robots[] 한 줄과 메시지 `data`가 같은 칸이라 그리는 함수도 하나다.
    assert "function applyRobotCard(" in body
    assert 'if (type === "robot_status") { applyRobotStatus(msg); }' in body
    # 원본 프레임에서 카드 값을 뽑던 자리가 남아 있으면 안 된다.
    assert "s.comm_status)" not in body.split("function summarize(")[0]


async def test_telemetry_page_escapes_server_values(client):
    """메시지 값은 기기가 올린 문자열이라 표 본문·class 속성 양쪽을 escape해야 한다."""
    body = (await client.get("/dev/telemetry")).text
    assert "function esc(" in body
    assert "function cssToken(" in body
    # 표 조립에 raw 삽입이 남아 있으면 안 된다.
    assert "esc(e.text)" in body
    assert "esc(e.type)" in body
    assert "cssToken(e.type)" in body
    assert 'e.text.replace(/</g, "&lt;")' not in body
    # 카운터 칩은 innerHTML 대신 텍스트 노드로 만든다.
    assert "chip.innerHTML" not in body


async def test_telemetry_page_has_identify_slot(client):
    """미태깅 통과자 사후 신원 확인 입력 자리 (임시).

    ⚠ 여긴 개발자 화면이다. 요원용 관제 대시보드가 생기면 그리로 옮긴다는 사실이 주석에
    남아 있어야, 나중에 읽는 사람이 "왜 개발자 화면에 요원 입력이 있나"를 안 헤맨다.
    """
    body = (await client.get("/dev/telemetry")).text
    assert "/identify" in body
    assert "function openIdent(" in body and "function sendIdent(" in body
    assert 'id="ident-person"' in body
    # 이전할 자리라는 표시 — 문구가 사라지면 임시라는 사실도 같이 사라진다.
    assert "관제 대시보드가 생기면" in body
    # 프레임워크·CDN 반입 금지는 여기서도 그대로다.
    assert "<script src=" not in body


async def test_telemetry_identify_slot_lives_outside_table(client):
    """입력 상자는 표 밖에 둔다.

    render()가 목록을 innerHTML로 통째로 갈아 끼우는데 이벤트가 하나 들어올 때마다 그게
    돈다. 표 안에 두면 요원이 치던 글자가 그때 날아간다.
    """
    body = (await client.get("/dev/telemetry")).text
    head, _, tail = body.partition('<tbody id="rows"')
    assert 'id="ident-person"' in head, "입력 상자가 표 안으로 들어갔다"
    assert 'id="ident-person"' not in tail


async def test_telemetry_identify_handles_409_and_length(client):
    """서버 계약을 화면이 그대로 안내한다 — 409는 고쳐 적기, 422는 길이다."""
    body = (await client.get("/dev/telemetry")).text
    assert "function identFailText(" in body
    assert "status === 409" in body and "status === 404" in body and "status === 422" in body
    assert "maxlength=\"64\"" in body and "maxlength=\"200\"" in body


async def test_telemetry_identify_recommends_pseudonym(client):
    """⚠ 개인정보 — 신원 칸에 뭘 넣을지는 팀 결정이고 시연에서는 가명을 권한다."""
    body = (await client.get("/dev/telemetry")).text
    assert "가명" in body
    assert "팀 결정" in body


async def test_telemetry_keeps_raw_toggle_delegation(client):
    """신원 버튼을 위임에 끼우면서 원문 펼치기 버튼을 밀어내면 안 된다.

    두 버튼이 같은 위임 핸들러를 타므로 BUTTON까지 올라간 뒤 속성으로 갈라야 한다.
    """
    body = (await client.get("/dev/telemetry")).text
    assert 'data-ident' in body and 'data-seq' in body
    assert 'if (node.tagName === "BUTTON") { break; }' in body


# ── 화면 JS를 실제로 돌려 본다 ──────────────────────────────────────────────
# 위 검사들은 전부 `문자열 in body`라 "코드가 적혀 있다"까지만 증명한다. 위임 핸들러가 실제로
# 도는지, 409 갈래가 갈리는지, 다른 화면이 적은 신원이 이쪽 버튼에 반영되는지는 그 방식으로는
# 못 잡는다(`node --check`도 문법만 본다). 그래서 스텁 DOM에 얹어 진짜로 실행한다.

_HARNESS_CACHE: dict = {}

# node가 있나를 **수집 시점에** 정한다 (2026-08-02 백지 검토 F63).
#
# ⚠ 예전에는 `_run_telemetry_harness` 안에서 `pytest.skip`을 불렀다. 그러면 아래 여섯
#   케이스가 왜 안 돌았는지가 요약 줄에 안 뜨고 그냥 통과처럼 흘러가서, node 없는 기계
#   (젠킨스 이미지·새 팀원 노트북)에서 **화면 JS 그물이 통째로 사라진 걸 아무도 못 본다.**
#   마커로 올리면 수집 단계에서 skip이 잡혀 `-rs` 요약에 이유가 그대로 남는다.
_NODE = shutil.which("node")
requires_node = pytest.mark.skipif(
    _NODE is None,
    reason="node가 없어 telemetry.html JS 실행 검사 6건을 건너뛴다 — 이 기계에서는 "
           "화면 JS 계약(위임 핸들러·409 갈래·신원 반영)이 검증되지 않는다",
)


def _run_telemetry_harness():
    """하네스를 한 번만 돌리고 결과를 나눠 쓴다(node 기동이 케이스마다 반복되면 느려진다).

    ⚠ 예전 판은 캐시를 **읽기만 하고 쓰는 자리가 없어서** 사문이었다(F62). 케이스 여섯이
    각자 node를 새로 띄워 배치가 그만큼 길어졌다. 결과는 입력이 없는 순수 실행이라 한 장을
    나눠 써도 케이스끼리 안 섞인다 — 하네스가 자기 안에서 시나리오를 다 돌고 판정 값만
    dict로 뱉는 구조다.
    """
    if "result" in _HARNESS_CACHE:
        return _HARNESS_CACHE["result"]
    import json
    import subprocess
    from pathlib import Path

    if _NODE is None:  # pragma: no cover - 마커가 먼저 걸러 준다
        pytest.skip("node가 없어 화면 JS 실행 검사를 건너뛴다")
    here = Path(__file__).resolve().parent
    page = here.parent / "app" / "static" / "telemetry.html"
    proc = subprocess.run(
        [_NODE, str(here / "telemetry_harness.js"), str(page)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"하네스가 죽었다\n{proc.stdout}\n{proc.stderr}"
    _HARNESS_CACHE["result"] = json.loads(proc.stdout.strip().splitlines()[-1])
    return _HARNESS_CACHE["result"]


@requires_node
async def test_화면_JS가_실제로_돈다():
    """부팅 → 미태깅 경보 수신 → 버튼 클릭 → 전송까지 한 번에 태운다."""
    r = _run_telemetry_harness()
    assert r["ws_url"].endswith("/ws/dashboard")
    assert r["panel_has_row"], "미태깅 경보가 신원 입력 대상 표에 안 붙었다"
    assert r["form_opened"], "위임 핸들러가 안 돌아 입력 폼이 안 열렸다"
    assert "게이트 7번" in r["form_target"], "어느 경고에 적는지 표시가 약하다"


@requires_node
async def test_화면이_API키를_헤더로만_보낸다():
    """키는 헤더로만 싣는다. 쿼리 파라미터로 실으면 nginx 액세스 로그에 통째로 남는다."""
    r = _run_telemetry_harness()
    assert r["sent_method"] == "POST"
    assert r["sent_key"] == "test-key"
    assert r["key_not_in_url"], "API 키가 URL에 실렸다"
    assert r["sent_body_has_person"]


@requires_node
async def test_신원_창구는_키가_없어도_서버를_부른다():
    """⭐ 로그인을 켠 뒤 관리자가 신원을 못 적던 자리 (2026-08-04 수리).

    켜면 신원 창구는 **세션 쿠키**로 돌고 서버가 `X-API-Key`를 아예 안 본다
    (`app/security.py` `key_gate_or`의 플래그 갈림). 그런데 화면이 fetch를 부르기도 전에
    "맨 위 서버 인입 API 키를 먼저 넣어 주세요"로 막아서, 관리자로 로그인해 페이지를 열어도
    신원을 못 읽고 못 적었다. 안내 문구도 진짜 원인과 무관해서 사람이 없는 키를 찾아 헤맨다.

    ⚠ 인입 갈래(`sendShuttleArrival`·`needIngestKey`)의 키 가드는 **그대로 둔다** — 그쪽은
    켠 뒤에도 진짜로 기기 키가 필요하다(설계 §3, 인입 3종은 안 바뀐다).
    """
    r = _run_telemetry_harness()
    assert r["no_key_called_server"], "키가 없다고 화면이 서버를 안 불렀다 — 켠 판에서 관리자가 막힌다"
    assert r["no_key_sent_url"].startswith("/api/alerts/"), r["no_key_sent_url"]
    # 401 안내가 두 원인을 다 짚는다. 한쪽만 적으면 켠 뒤에 엉뚱한 곳을 뒤진다.
    assert "로그인" in r["unauthorized_note"]
    assert "API 키" in r["unauthorized_note"]


async def test_인입_갈래는_키_가드를_그대로_둔다(client):
    """켠 뒤에도 기기 키가 진짜로 필요한 자리는 부르기 전에 막는 게 맞다.

    신원 가드를 걷으면서 같이 걷어 버리면, 키 없이 인입을 눌러 401을 받고 나서야 알게 된다.
    """
    body = (await client.get("/dev/telemetry")).text
    assert "function needIngestKey(" in body
    assert "function sendShuttleArrival(" in body
    assert "인입 창구라 API 키가 필요합니다" in body
    assert "셔틀 도착은 인입 창구라 API 키가 필요합니다" in body


@requires_node
async def test_다른_화면이_적은_신원이_이쪽에_반영된다():
    """대시보드 두 대 갈림 — ack가 S15P11C207-81에서 고친 것과 같은 결함이다."""
    r = _run_telemetry_harness()
    assert r["panel_label_after"], "alert_identified를 받고도 버튼이 안 바뀌었다"
    assert r["panel_class_done"]


@requires_node
async def test_이미_적힌_경고는_고쳐_적기가_미리_켜진다():
    """안 켜 두면 요원이 그대로 보내고 409를 받고서야 알게 된다."""
    r = _run_telemetry_harness()
    assert r["overwrite_prechecked_new"] is False
    assert r["overwrite_prechecked_done"] is True
    assert "덮어씁니다" in r["done_note"]
    assert "고쳐 적기" in r["conflict_note"]


@requires_node
async def test_목록_지우기가_신원_입력_대상을_안_지운다():
    """수리 전엔 "목록 지우기" 한 번에 신원 버튼이 통째로 사라졌다."""
    r = _run_telemetry_harness()
    assert r["panel_survives_clear"]
    assert r["panel_has_history_row"], "이력으로 받은 미태깅 경고가 표에 안 붙었다"
    assert r["panel_still_has_live_row"]


@requires_node
async def test_보고_채널이_꺼지면_화면이_못박는다():
    """"설정돼 있으면 나갑니다"는 요원이 보냈다고 믿게 만든다(운영 실측 — 무발사였다)."""
    r = _run_telemetry_harness()
    assert "보고 채널이 꺼져 있어" in r["panel_note"]
