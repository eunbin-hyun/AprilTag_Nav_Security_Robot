"""시연 도구 창구 (`POST /api/demo/gate-pass`).

프론트 22차 요구다. 시연 버튼이 만든 판정이 **화면에만 남고 서버에는 안 갔다** — 새로고침하면
사라져서 리허설에서 장면을 쌓아 둘 수가 없었다. 사용자가 "기록이 남아야 한다"고 두 번 짚었다.

⭐ **이 파일의 본체는 "판정을 우회하지 않는다"**이다. 창구가 `verdict`를 받아 그대로 저장하면
판정 규칙이 두 벌이 되고, 리허설에서 본 것과 발표 당일에 볼 것이 갈라진다. 그래서 그 판정이
나오도록 **입력을 만들어** 실제 인입 경로에 넣는다 — 아래 케이스가 그 결과를 잰다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import auth
from app.config import get_settings
from app.db import get_session
from app.models import Alert, AppUser, GatePassEvent, TaggingEvent

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest.fixture
def flag_on(monkeypatch):
    """로그인 게이트를 켠다. 이 창구는 `_always` 라 꺼도 판정하지만, 켠 판에서도 같아야 한다."""
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _make_user(username: str, role: str) -> AppUser:
    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password("pw-12345678"),
            display_name=f"이름-{username}",
            role=role,
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _cookie(user: AppUser) -> dict[str, str]:
    async with get_session() as session:
        token = await auth.create_session(session, user)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


@pytest.fixture
async def admin_cookie():
    return await _cookie(await _make_user("demoadmin", auth.ROLE_ADMIN))


async def _rows(model):
    async with get_session() as s:
        return (await s.execute(select(model).order_by(model.id))).scalars().all()


async def test_익명은_막힌다(client):
    """관리자 전용이다. 시연 자료를 만드는 것은 실제 통과 기록을 만드는 것과 같다."""
    res = await client.post("/api/demo/gate-pass", json={"verdict": "untagged"})
    assert res.status_code == 401, res.text


async def test_요원과_팀장도_막힌다(client, flag_on):
    """명부 수정과 같은 급이다 — 팀장도 안 된다."""
    for role in (auth.ROLE_AGENT, auth.ROLE_LEADER):
        user = await _make_user(f"demo{role}", role)
        res = await client.post(
            "/api/demo/gate-pass", json={"verdict": "untagged"},
            headers=await _cookie(user),
        )
        assert res.status_code == 403, f"{role}: {res.text}"


async def test_무단_통과가_실제로_저장되고_경고까지_생긴다(client, flag_on, admin_cookie):
    """⭐ 본체 — 화면에만 남던 것이 서버 표에 들어간다.

    무단 통과면 **경고도 같이** 만들어져야 한다(프론트가 "화면은 지금 배선 그대로 받는다"고
    한 자리다). 그게 실제 인입 경로를 그대로 타는 근거다.
    """
    res = await client.post(
        "/api/demo/gate-pass", json={"verdict": "untagged", "gate_no": 2},
        headers=admin_cookie,
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["requested"] == "untagged"
    assert body["verdict"] == "untagged", "판정기가 미태깅으로 안 읽었다"
    assert body["stored"] is True
    assert body["is_demo"] is True

    passes = await _rows(GatePassEvent)
    assert len(passes) == 1, "통과 행이 안 생겼다 — 새로고침하면 또 사라진다"
    assert passes[0].is_demo is True, "시연 표시가 안 붙었다 — 골라 지울 수가 없다"
    assert passes[0].gate_no == 2

    alerts = await _rows(Alert)
    assert len(alerts) == 1, "무단 통과인데 경고가 안 생겼다"
    assert alerts[0].is_demo is True, "경고에 시연 표시가 안 붙었다 — 통과만 지우면 남는다"


async def test_정상_통과는_태깅을_먼저_심어_크레딧을_만든다(client, flag_on, admin_cookie):
    """⭐ `normal`은 태깅이 먼저 있어야 나오는 판정이다.

    크레딧 없이 통과만 만들면 판정기가 미태깅으로 읽는다. 그래서 창구가 태깅 한 건을
    **통과보다 앞선 시각으로** 심는다.
    """
    res = await client.post(
        "/api/demo/gate-pass", json={"verdict": "normal"}, headers=admin_cookie
    )
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["verdict"] == "normal", (
        f"크레딧이 안 만들어져 미태깅으로 떨어졌다 — {body}"
    )
    assert body["tagging_event_id"], "태깅 이벤트 번호를 안 돌려줬다"

    taggings = await _rows(TaggingEvent)
    assert len(taggings) == 1
    assert taggings[0].is_demo is True, "태깅 행에도 시연 표시가 붙어야 한다"
    assert (await _rows(Alert)) == [], "정상 통과인데 경고가 생겼다"


async def test_한쪽만_감지된_통과(client, flag_on, admin_cookie):
    """빔 하나만 찍힌 갈래. 판정기가 미완성으로 읽는다."""
    res = await client.post(
        "/api/demo/gate-pass", json={"verdict": "beam_incomplete"}, headers=admin_cookie
    )
    assert res.status_code == 200, res.text
    assert res.json()["verdict"] == "beam_incomplete", res.text

    passes = await _rows(GatePassEvent)
    assert len(passes) == 1
    assert passes[0].beam_b_ts is None, "한쪽 감지인데 빔 둘 다 찍혔다"


async def test_퇴장은_방향이_반대다(client, flag_on, admin_cookie):
    """`exit`은 판정 낱말이 아니라 **방향**이다(B→A)."""
    res = await client.post(
        "/api/demo/gate-pass", json={"verdict": "exit"}, headers=admin_cookie
    )
    assert res.status_code == 200, res.text

    passes = await _rows(GatePassEvent)
    assert len(passes) == 1
    assert passes[0].direction == "B_TO_A", "퇴장인데 입장 방향으로 들어갔다"


async def test_같은_버튼을_여러_번_눌러도_쌓인다(client, flag_on, admin_cookie):
    """시연은 같은 장면을 반복해 만드는 것이 정상이다 — 멱등 열쇠가 매번 새로 떠야 한다."""
    for _ in range(3):
        res = await client.post(
            "/api/demo/gate-pass", json={"verdict": "untagged"}, headers=admin_cookie
        )
        assert res.status_code == 200, res.text
        assert res.json()["stored"] is True, "두 번째부터 중복으로 막혔다"

    assert len(await _rows(GatePassEvent)) == 3


async def test_verdict_를_직접_못_준다(client, flag_on, admin_cookie):
    """⛔ 모르는 낱말은 422다. 판정을 밖에서 넣는 길을 안 연다."""
    res = await client.post(
        "/api/demo/gate-pass", json={"verdict": "whatever"}, headers=admin_cookie
    )
    assert res.status_code == 422, res.text
