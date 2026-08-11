"""관제 챗봇 대화 저장 — ⛔ **계정 격리가 이 파일의 본체다** (0019 · 2026-08-07).

사용자 확정으로 대화를 서버에 남기게 됐다. 그러면서 **"내 대화만 보인다"가 기능이 아니라
보안 계약**이 됐다 — 관제 PC 는 요원이 교대로 쓰는 자리라, 조회에 `user_id` 조건이 빠지면
앞 사람이 무엇을 물었는지가 다음 사람에게 그대로 보인다.

못박는 것.
- 남의 대화를 **404** 로 막나(403 이 아니다 — 그러면 번호를 훑어 존재를 알아낼 수 있다).
- 목록에 **내 것만** 나오나.
- 이어 쓰기가 같은 대화에 붙나, 비우면 새로 열리나.
- 남의 대화 번호로 이어 쓰려 하면 막나.
- 로그인 없이(API 키만) 부르면 **주인 없는 행을 안 만드나.**

⚠ LLM 은 안 부른다. `test_assistant_endpoints.py` 와 같은 잣대로 키 없는 설정을 덮어써서
폴백 갈래로만 돈다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app import ai_bridge, auth
from app.config import get_settings
from app.db import get_session
from app.models import AppUser, ChatConversation, ChatMessage
from app.routers import assistant as assistant_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PASSWORD = "pw-12345678"
KEY_HEADERS = {"X-API-Key": get_settings().api_key}


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    """실호출 차단. 폴백 갈래로만 돈다."""
    monkeypatch.setattr(
        assistant_router.ai_bridge,
        "build_config",
        lambda: ai_bridge.AssistantConfig(api_key=""),
    )


@pytest.fixture
def flag_on(monkeypatch):
    """⛔ 로그인 요구를 켠다. **안 켜면 쿠키가 아예 안 먹는다.**

    `key_gate_or` 는 플래그가 꺼져 있으면 `X-API-Key` 게이트를 태우고 **행위자로 None 을
    돌려준다.** 그러면 대화 저장 자체를 건너뛰므로(주인 없는 행을 안 만든다) 계정 격리를
    잴 수가 없다 — 실제로 이 파일을 처음 돌렸을 때 쿠키를 보냈는데 401 이 왔다.

    ⚠ 실서버는 로그인이 켜져 있다. 꺼진 쪽이 시험 기본값일 뿐이다.
    """
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _make_user(username: str, role: str = "agent") -> AppUser:
    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password(_PASSWORD),
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


async def _ask(client, headers: dict, question: str, conversation_id: int | None = None):
    body: dict = {"question": question}
    if conversation_id is not None:
        body["conversation_id"] = conversation_id
    return await client.post("/api/assistant/chat", json=body, headers=headers)


# ── 1. ⛔ 계정 격리 ─────────────────────────────────────────────────────────

async def test_남의_대화는_404다_403이_아니다(client, flag_on):
    """403으로 가르면 번호를 훑어 '그 번호에 대화가 있다'를 알아낼 수 있다."""
    mine = await _make_user("convmine1")
    other = await _make_user("convother1")

    made = await _ask(client, await _cookie(other), "지금 로봇 상태 알려줘")
    assert made.status_code == 200
    other_id = made.json()["conversation_id"]
    assert other_id is not None

    resp = await client.get(
        f"/api/assistant/conversations/{other_id}", headers=await _cookie(mine)
    )
    assert resp.status_code == 404, "남의 대화가 열렸다 — 계정 격리가 깨졌다"

    # 아예 없는 번호도 같은 404여야 한다. 둘이 갈리면 존재가 샌다.
    missing = await client.get(
        "/api/assistant/conversations/99999999", headers=await _cookie(mine)
    )
    assert missing.status_code == 404
    assert resp.json() == missing.json(), "남의 것과 없는 것의 응답이 다르면 존재가 샌다"


async def test_목록에_내_것만_나온다(client, flag_on):
    mine = await _make_user("convmine2")
    other = await _make_user("convother2")

    await _ask(client, await _cookie(other), "남의 질문이다")
    made = await _ask(client, await _cookie(mine), "내 질문이다")
    my_id = made.json()["conversation_id"]

    resp = await client.get("/api/assistant/conversations", headers=await _cookie(mine))
    assert resp.status_code == 200
    ids = [row["id"] for row in resp.json()]
    assert my_id in ids
    assert len(ids) == 1, f"내 대화 하나만 보여야 하는데 {len(ids)}건이다"


async def test_남의_대화_번호로_이어쓰기를_못_한다(client, flag_on):
    """조회만 막고 쓰기를 안 막으면 남의 대화에 내 문답이 섞인다."""
    mine = await _make_user("convmine3")
    other = await _make_user("convother3")

    made = await _ask(client, await _cookie(other), "남의 대화다")
    other_id = made.json()["conversation_id"]

    resp = await _ask(client, await _cookie(mine), "끼어들기", conversation_id=other_id)
    assert resp.status_code == 404

    async with get_session() as session:
        n = (
            await session.execute(
                select(func.count())
                .select_from(ChatMessage)
                .where(ChatMessage.conversation_id == other_id)
            )
        ).scalar_one()
    assert n == 2, "남의 대화에 줄이 늘었다 — 질문·답변 둘 그대로여야 한다"


# ── 2. 이어 쓰기 ───────────────────────────────────────────────────────────

async def test_대화를_비우면_새로_열리고_주면_이어_붙는다(client, flag_on):
    user = await _make_user("convflow1")
    headers = await _cookie(user)

    first = await _ask(client, headers, "첫 질문이다")
    cid = first.json()["conversation_id"]
    assert cid is not None

    second = await _ask(client, headers, "이어서 묻는다", conversation_id=cid)
    assert second.json()["conversation_id"] == cid

    third = await _ask(client, headers, "새로 시작한다")
    assert third.json()["conversation_id"] != cid, "비웠는데 앞 대화에 붙었다"

    detail = await client.get(f"/api/assistant/conversations/{cid}", headers=headers)
    roles = [m["role"] for m in detail.json()["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]


async def test_제목은_첫_질문에서_뽑고_답변_갈래를_같이_남긴다(client, flag_on):
    user = await _make_user("convflow2")
    headers = await _cookie(user)

    resp = await _ask(client, headers, "게이트 상황 알려줘")
    cid = resp.json()["conversation_id"]

    detail = (await client.get(f"/api/assistant/conversations/{cid}", headers=headers)).json()
    assert detail["title"] == "게이트 상황 알려줘"

    answer = detail["messages"][1]
    assert answer["role"] == "assistant"
    # 키가 없으니 폴백이어야 한다. 이 칸이 비면 "그때 왜 이 답이 나왔나"를 되짚을 수 없다.
    assert answer["generated_by"] == "fallback"


# ── 3. 로그인 없는 경로 ────────────────────────────────────────────────────

async def test_API키만으로_부르면_주인_없는_대화를_안_만든다(client):
    """시연 도구처럼 키로 여는 경로가 있다. 거기서 행을 만들면 아무에게도 안 보이는 행이 쌓인다."""
    async with get_session() as session:
        before = (
            await session.execute(select(func.count()).select_from(ChatConversation))
        ).scalar_one()

    resp = await _ask(client, KEY_HEADERS, "키로만 묻는다")
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] is None
    assert resp.json()["answer"], "저장을 건너뛰어도 답변은 나가야 한다"

    async with get_session() as session:
        after = (
            await session.execute(select(func.count()).select_from(ChatConversation))
        ).scalar_one()
    assert after == before


# ── 4. 지우기 (2026-08-07 프론트 51차 요청) ────────────────────────────────


async def test_지우면_메시지까지_같이_사라진다(client, flag_on):
    """⭐ 외래키 CASCADE 가 실제로 도는지 잰다 — 계약만 적어 두고 안 재면 고아 행이 쌓인다.

    ⚠ 여기는 **행 수를 직접 센다.** 창구가 204 를 주는 것과 DB 에서 사라진 것은 다른 말이다.
    """
    mine = await _make_user("convdel1")
    headers = await _cookie(mine)

    made = await _ask(client, headers, "지금 로봇 상태 알려줘")
    conv_id = made.json()["conversation_id"]
    assert conv_id is not None

    async with get_session() as s:
        before = (
            await s.execute(
                select(func.count()).select_from(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
            )
        ).scalar_one()
    assert before > 0, "지우기를 재기 전에 메시지가 있어야 한다"

    resp = await client.delete(f"/api/assistant/conversations/{conv_id}", headers=headers)
    assert resp.status_code == 204

    async with get_session() as s:
        conv_left = await s.get(ChatConversation, conv_id)
        msg_left = (
            await s.execute(
                select(func.count()).select_from(ChatMessage)
                .where(ChatMessage.conversation_id == conv_id)
            )
        ).scalar_one()
    assert conv_left is None, "대화가 안 지워졌다"
    assert msg_left == 0, f"메시지 {msg_left}줄이 고아로 남았다 — CASCADE 가 안 돈다"


async def test_지운_대화는_목록에서도_사라진다(client, flag_on):
    """⛔ 화면에서만 감추던 것을 고치는 게 이 창구의 목적이라, 목록까지 봐야 끝이다."""
    mine = await _make_user("convdel2")
    headers = await _cookie(mine)

    keep = (await _ask(client, headers, "남길 대화")).json()["conversation_id"]
    drop = (await _ask(client, headers, "지울 대화")).json()["conversation_id"]

    assert (await client.delete(
        f"/api/assistant/conversations/{drop}", headers=headers
    )).status_code == 204

    listed = await client.get("/api/assistant/conversations", headers=headers)
    ids = [row["id"] for row in listed.json()]
    assert drop not in ids, "지웠는데 목록에 되살아났다"
    assert keep in ids, "엉뚱한 대화까지 지웠다"


async def test_남의_대화는_못_지운다_404다(client, flag_on):
    """⚠ 조회와 **같은 404** 여야 한다. 여기만 403 이면 그 갈래로 존재가 샌다."""
    mine = await _make_user("convdel3")
    other = await _make_user("convdel4")

    other_id = (await _ask(
        client, await _cookie(other), "남의 질문이다"
    )).json()["conversation_id"]

    resp = await client.delete(
        f"/api/assistant/conversations/{other_id}", headers=await _cookie(mine)
    )
    assert resp.status_code == 404, "남의 대화가 지워졌다 — 계정 격리가 깨졌다"

    missing = await client.delete(
        "/api/assistant/conversations/99999999", headers=await _cookie(mine)
    )
    assert missing.status_code == 404
    assert resp.json() == missing.json(), "남의 것과 없는 것의 응답이 다르면 존재가 샌다"

    # ⛔ 실제로 안 지워졌는지까지 본다 — 404 를 주면서 지워 버리면 최악이다.
    async with get_session() as s:
        assert await s.get(ChatConversation, other_id) is not None


# ── 5. 이어 묻기용 이력 (2026-08-07 관제 보조 갈래 요청) ───────────────────


async def test_이어_물으면_지난_대화가_실려_간다(client, flag_on, monkeypatch):
    """⭐ 이력이 실제로 넘어가는지 **호출을 가로채서** 본다.

    ⚠ 넘긴다는 코드만 보고 판정하면 안 된다 — 인자 이름이 어긋나거나 기본값에 걸려도
    조용히 빈 이력으로 돌고, 답은 그럴듯하게 나와서 아무도 안 걸린다.
    """
    seen: dict = {}

    real = assistant_router.ai_bridge.chat

    async def _spy(cfg, question, facts, history=None):
        seen["history"] = history
        # ⚠ 예외를 던지면 안 된다 — 폴백 처리가 이 함수 **안**에 있어서 밖으로 새 나간다.
        return await real(cfg, question, facts, history=history)

    monkeypatch.setattr(assistant_router.ai_bridge, "chat", _spy)

    mine = await _make_user("convhist1")
    headers = await _cookie(mine)

    first = await _ask(client, headers, "오늘 무단 통과가 몇 건이야")
    conv_id = first.json()["conversation_id"]
    assert seen["history"] == [], "첫 질문에는 이력이 없어야 한다"

    await _ask(client, headers, "그럼 어제는", conversation_id=conv_id)

    hist = seen["history"]
    assert hist, "이어 물었는데 이력이 안 실렸다"
    assert hist[0]["role"] == "user"
    assert hist[0]["content"] == "오늘 무단 통과가 몇 건이야"
    assert hist[-1]["role"] == "assistant", "시간 순이어야 한다 — 뒤집히면 모델이 역할을 헷갈린다"
    # ⛔ 지금 질문이 이력에도 들어가면 같은 말이 두 번 실린다.
    assert all(m["content"] != "그럼 어제는" for m in hist)


async def test_이력은_두_턴에서_잘린다(client, flag_on, monkeypatch):
    """⚠ 안 자르면 프롬프트 앞부분이 계속 자라 지연이 그만큼 붙는다."""
    seen: dict = {}

    real = assistant_router.ai_bridge.chat

    async def _spy(cfg, question, facts, history=None):
        seen["history"] = history
        # ⚠ 예외를 던지면 안 된다 — 폴백 처리가 이 함수 **안**에 있어서 밖으로 새 나간다.
        return await real(cfg, question, facts, history=history)

    monkeypatch.setattr(assistant_router.ai_bridge, "chat", _spy)

    mine = await _make_user("convhist2")
    headers = await _cookie(mine)

    conv_id = (await _ask(client, headers, "질문 1")).json()["conversation_id"]
    for i in range(2, 6):
        await _ask(client, headers, f"질문 {i}", conversation_id=conv_id)

    hist = seen["history"]
    assert len(hist) == 4, f"두 턴(넷)에서 잘려야 하는데 {len(hist)}개가 실렸다"
    # ⚠ 마지막 호출은 "질문 5"라 이력은 그 앞 두 쌍이다 — 질문 3·답3·질문 4·답4.
    #    오래된 쪽이 남으면 이어지는 질문에 못 답하니 **앞머리가 질문 3인지**를 본다.
    assert hist[0]["content"] == "질문 3"
    assert hist[2]["content"] == "질문 4"
    assert all(m["content"] != "질문 1" for m in hist), "오래된 턴이 안 잘렸다"


async def test_남의_대화_이력은_안_샌다(client, flag_on, monkeypatch):
    """⛔ 남의 대화 번호로 이어 물으면 404다 — 그 전에 이력을 읽어도 안 된다."""
    seen: dict = {"called": False}

    real = assistant_router.ai_bridge.chat

    async def _spy(cfg, question, facts, history=None):
        seen["called"] = True
        return await real(cfg, question, facts, history=history)

    monkeypatch.setattr(assistant_router.ai_bridge, "chat", _spy)

    other = await _make_user("convhist3")
    mine = await _make_user("convhist4")
    other_id = (await _ask(
        client, await _cookie(other), "남의 질문이다"
    )).json()["conversation_id"]

    seen["called"] = False
    resp = await _ask(client, await _cookie(mine), "이어 묻는 척", conversation_id=other_id)

    assert resp.status_code == 404
    assert seen["called"] is False, "404인데 LLM까지 갔다 — 남의 이력이 실릴 뻔했다"
