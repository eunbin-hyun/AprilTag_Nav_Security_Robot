"""로그인 기반 층 — 세션 수명 · 역할 게이트 · 마지막 관리자 보호.

DB를 타는 갈래만 여기 있다. 순수 판정(해시·잠금·`assert_can_write`)은
`test_auth_rules.py`, HTTP 창구는 `test_auth_endpoints.py`가 본다.
계약은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §1·§4다.

## 여기서 재는 것 넷

1. **세션 수명** — 절대 12시간은 활동해도 안 늘어나고, 유휴 2시간은 활동하면 밀린다.
   갱신 쓰기는 60초 안에 두 번 안 나간다.
2. **강등·해제가 다음 요청부터** — 계정을 해제하면 살아 있던 세션이 그 자리에서 죽는다.
   이게 JWT 대신 세션을 고른 이유라(§1.1), 안 재면 그 근거가 코드에 없는 셈이다.
3. **제1 불변** — `AUTH_REQUIRE_LOGIN=false`면 역할 게이트 셋이 무조건 통과하고, 쿠키가
   없으면 DB를 아예 안 건드린다.
4. **마지막 관리자 보호** — 동시 강등 두 건이 행 잠금으로 직렬화된다.

## 시험 위생

`app_user`·`auth_session`·`staff_audit` 비우기와 인메모리 잠금 초기화는 **conftest `_clean`
하나가 한다.** 예전에는 이 파일이 자기 앞에서 같은 걸 또 돌렸는데, 그러면 공용 훅이 실제로
일하는지를 이 파일이 가려 버린다 — 훅이 빠져도 이 파일만은 통과해서 결함이 안 보인다.
그래서 걷어냈고, 이 파일이 통과하는 게 곧 그 훅이 도는 증거다(2026-08-02 교차 검토).
"""
import asyncio
import datetime as dt

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update
from starlette.requests import Request

from app import auth
from app.config import get_settings
from app.db import get_session
from app.models import AppUser, AuthSession

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_T = dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """만료 축을 시험이 직접 감는다. 12시간을 실제로 기다릴 수는 없다.

    `clock["t"] += dt.timedelta(hours=13)`처럼 눈금을 밀면 "시간이 지났다"가 결정적으로 된다
    (conftest `wall_clock`과 같은 수법).
    """
    holder = {"t": _BASE_T}
    monkeypatch.setattr(auth, "_now", lambda: holder["t"])
    return holder


@pytest.fixture
def flag_on(monkeypatch):
    """`AUTH_REQUIRE_LOGIN=true` 구간. 설정은 lru_cache라 캐시를 앞뒤로 비운다."""
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _make_user(
    username: str, role: str = auth.ROLE_AGENT, password: str = "pw-1234", **values
) -> AppUser:
    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password(password),
            display_name=f"이름-{username}",
            role=role,
            **values,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _request(token: str | None = None) -> Request:
    """쿠키만 실은 최소 ASGI 요청. 역할 게이트를 직접 부를 때 쓴다."""
    headers = []
    if token is not None:
        headers.append((b"cookie", f"{auth.SESSION_COOKIE_NAME}={token}".encode()))
    return Request(
        {"type": "http", "method": "GET", "path": "/", "headers": headers,
         "query_string": b""}
    )


# ── 1. 세션 수명 (§1.4) ────────────────────────────────────────────────────

async def test_session_resolves_and_carries_identity(clock):
    user = await _make_user("agent1", role=auth.ROLE_AGENT)
    async with get_session() as session:
        token = await auth.create_session(session, user, user_agent="pytest-UA")
        actor = await auth.resolve_session(session, token)

    assert actor is not None
    assert (actor.id, actor.username, actor.role) == (user.id, "agent1", "agent")
    assert actor.display_name == "이름-agent1"

    # 저장된 건 원문이 아니라 sha256이다(§1.3 — 덤프 한 번에 세션이 통째로 털리는 걸 막는다).
    async with get_session() as session:
        row = (await session.execute(select(AuthSession))).scalars().one()
    assert row.token_hash == auth.token_digest(token)
    assert token not in row.token_hash
    assert row.user_agent == "pytest-UA"


async def test_session_absolute_expiry_is_not_extended_by_activity(clock):
    """절대 만료는 활동으로 **안 늘어난다.**

    한 시간마다 꼬박꼬박 들러서 유휴 만료(2시간)를 계속 밀어내도, 12시간이 되면 죽는다.
    유휴만 있고 절대가 없으면 한 번 새어 나간 토큰이 영원히 산다.
    """
    user = await _make_user("agent2")
    async with get_session() as session:
        token = await auth.create_session(session, user)

    for hours in range(1, 12):
        clock["t"] = _BASE_T + dt.timedelta(hours=hours)
        async with get_session() as session:
            assert await auth.resolve_session(session, token) is not None, (
                f"{hours}시간째 활동인데 끊겼다"
            )

    clock["t"] = _BASE_T + dt.timedelta(hours=12, seconds=1)
    async with get_session() as session:
        assert await auth.resolve_session(session, token) is None, (
            "11시간째 활동이 절대 만료를 밀어냈다"
        )


async def test_session_idle_expiry_is_pushed_by_activity(clock):
    """유휴 2시간은 활동하면 밀린다. 1시간마다 들르는 사람은 안 끊긴다."""
    user = await _make_user("agent3")
    async with get_session() as session:
        token = await auth.create_session(session, user)

    for hours in (1, 2, 3):
        clock["t"] = _BASE_T + dt.timedelta(hours=hours)
        async with get_session() as session:
            assert await auth.resolve_session(session, token) is not None, (
                f"{hours}시간째 활동인데 유휴로 끊겼다"
            )

    # 마지막 활동(3시간째)에서 2시간 넘게 쉬면 죽는다. 절대 만료(12시간)는 아직 멀다.
    clock["t"] = _BASE_T + dt.timedelta(hours=5, seconds=1)
    async with get_session() as session:
        assert await auth.resolve_session(session, token) is None


async def test_session_touch_is_throttled_to_one_write_per_interval(clock):
    """조회가 잦아도 `last_seen_at` 쓰기는 60초에 한 번이다(쓰기 폭주 방어).

    화면이 주기로 조회를 때리는 자리라, 매 요청 UPDATE면 세션 테이블이 쓰기로 뒤덮인다.
    """

    async def _last_seen() -> dt.datetime:
        async with get_session() as session:
            return (await session.execute(select(AuthSession.last_seen_at))).scalar_one()

    user = await _make_user("agent4")
    async with get_session() as session:
        token = await auth.create_session(session, user)
    assert await _last_seen() == _BASE_T

    # 59초 뒤 조회 — 아직 안 쓴다.
    clock["t"] = _BASE_T + dt.timedelta(seconds=59)
    async with get_session() as session:
        assert await auth.resolve_session(session, token) is not None
    assert await _last_seen() == _BASE_T, "60초 안인데 갱신 쓰기가 나갔다"

    # 60초를 넘기면 그때 한 번 쓴다.
    clock["t"] = _BASE_T + dt.timedelta(seconds=61)
    async with get_session() as session:
        assert await auth.resolve_session(session, token) is not None
    assert await _last_seen() == _BASE_T + dt.timedelta(seconds=61)


async def test_revoked_session_dies_immediately(clock):
    """무효화는 다음 요청부터 먹히고, 행은 안 지운다 — 언제 끊겼나가 감사 자료다."""
    user = await _make_user("agent5")
    async with get_session() as session:
        token = await auth.create_session(session, user)
        await auth.revoke_session(session, token)
        assert await auth.resolve_session(session, token) is None
        row = (await session.execute(select(AuthSession))).scalars().one()
    assert row.revoked_at == _BASE_T, "행이 지워졌거나 무효화 시각이 안 남았다"


async def test_revoke_user_sessions_kills_every_live_one(clock):
    """그 사람의 세션을 통째로 끊는다(비밀번호 변경·계정 해제가 부르는 자리).

    남의 세션은 안 건드린다 — 한 사람을 내리는 조작이 전원을 로그아웃시키면 안 된다.
    """
    victim = await _make_user("agent6")
    other = await _make_user("agent7")
    async with get_session() as session:
        t1 = await auth.create_session(session, victim)
        t2 = await auth.create_session(session, victim)
        t3 = await auth.create_session(session, other)
        await auth.revoke_user_sessions(session, victim.id)

        assert await auth.resolve_session(session, t1) is None
        assert await auth.resolve_session(session, t2) is None
        assert await auth.resolve_session(session, t3) is not None


async def test_revoke_user_sessions_can_leave_the_commit_to_the_caller(clock):
    """`commit=False`면 커밋을 **부르는 쪽 트랜잭션**에 맡긴다(F19).

    비밀번호 재설정이 해시 교체와 세션 끊기를 한 커밋에 묶을 수 있어야 한다. 지금은 둘이
    다른 트랜잭션이라 사이에서 죽으면 옛 비밀번호로 연 세션이 12시간을 산다.

    커밋을 안 냈다는 걸 롤백으로 잰다 — 냈다면 되돌려도 무효화가 남는다.
    """
    user = await _make_user("agent_defer")
    async with get_session() as session:
        token = await auth.create_session(session, user)
        await auth.revoke_user_sessions(session, user.id, commit=False)
        assert await auth.resolve_session(session, token) is None
        await session.rollback()

    async with get_session() as session:
        assert await auth.resolve_session(session, token) is not None, (
            "commit=False인데 커밋이 나갔다 — 부르는 쪽이 두 쓰기를 한 커밋에 못 묶는다"
        )

    # 기본값은 지금 거동 그대로(자기 커밋)다.
    async with get_session() as session:
        await auth.revoke_user_sessions(session, user.id)
        await session.rollback()
    async with get_session() as session:
        assert await auth.resolve_session(session, token) is None


async def test_login_keeps_only_the_newest_sessions_per_user(clock):
    """세션은 상한(5)까지만 산다. 넘치면 **가장 오래된 것부터** 끊긴다(F21).

    로그인이 옛 세션을 안 끊고 쌓기만 하는데 낱장으로 끊을 창구도 없어서, 상한이 없으면
    기기를 옮겨 가며 로그인한 만큼 옛 토큰이 12시간 내내 산다.
    """
    limit = get_settings().session_max_per_user
    user = await _make_user("agent_many")
    tokens = []
    for i in range(limit + 2):
        # 눈금을 1초씩 밀어 "무엇이 오래된 것인가"를 시각으로도 못박는다.
        clock["t"] = _BASE_T + dt.timedelta(seconds=i)
        async with get_session() as session:
            tokens.append(await auth.create_session(session, user))

    async with get_session() as session:
        alive = [t for t in tokens if await auth.resolve_session(session, t) is not None]
    assert alive == tokens[-limit:], f"살아남은 세션이 {len(alive)}장이다(상한 {limit})"

    # 남의 세션은 안 건드린다 — 한 사람이 여러 번 로그인해서 전원을 끊으면 안 된다.
    other = await _make_user("agent_other")
    async with get_session() as session:
        other_token = await auth.create_session(session, other)
        assert await auth.resolve_session(session, other_token) is not None


async def test_password_hashing_does_not_block_the_event_loop(clock):
    """16MiB scrypt가 이벤트 루프를 안 막는다(F4).

    워커가 1개라 동기로 돌면 그 수백 밀리초가 곧 서버 전체의 정지다 — 로봇 WS 하트비트도
    출동 대기 창 타이머도 같이 선다. 그래서 심장박동 하나를 돌려 두고, 해시가 도는 동안
    루프가 실제로 돌았는지를 잰다. 막는 판에서는 이 수가 0이다.
    """
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(0.02)
        ticks = 0
        stored = await auth.hash_password_async("bi-mil-1234")
        assert await auth.verify_password_async("bi-mil-1234", stored) is True
        assert await auth.verify_password_async("틀린비번", stored) is False
        assert await auth.dummy_password_hash_async() == auth.dummy_password_hash()
    finally:
        beat.cancel()

    assert ticks >= 2, f"해시가 도는 동안 루프가 {ticks}번 돌았다 — 동기로 돌고 있다"


async def test_deactivated_account_kills_live_session(clock):
    """계정을 해제하면 **살아 있던 세션이 그 자리에서 죽는다.**

    강등·해제가 다음 요청부터 먹혀야 한다는 게 JWT 대신 세션을 고른 이유다(§1.1).
    세션 행만 보고 계정을 안 보면 해제한 사람이 만료까지 계속 돌아다닌다.
    """
    user = await _make_user("agent8")
    async with get_session() as session:
        token = await auth.create_session(session, user)
        assert await auth.resolve_session(session, token) is not None
        await session.execute(
            update(AppUser).where(AppUser.id == user.id).values(is_active=False)
        )
        await session.commit()
        assert await auth.resolve_session(session, token) is None


async def test_role_change_is_visible_on_the_next_request(clock):
    """강등이 다음 요청부터 먹힌다. 세션은 그대로인데 급만 내려간다."""
    user = await _make_user("agent9", role=auth.ROLE_ADMIN)
    async with get_session() as session:
        token = await auth.create_session(session, user)
        assert (await auth.resolve_session(session, token)).role == auth.ROLE_ADMIN
        await session.execute(
            update(AppUser).where(AppUser.id == user.id).values(role=auth.ROLE_AGENT)
        )
        await session.commit()
        assert (await auth.resolve_session(session, token)).role == auth.ROLE_AGENT


async def test_unknown_token_resolves_to_none(clock):
    async with get_session() as session:
        assert await auth.resolve_session(session, "") is None
        assert await auth.resolve_session(session, "no-such-token") is None


async def test_purge_only_removes_long_expired_rows(clock):
    """청소는 만료 뒤 보관 기간(7일)이 지난 행만 지운다. 어제 끝난 세션은 남는다."""
    user = await _make_user("agent10")
    async with get_session() as session:
        old = await auth.create_session(session, user)
    clock["t"] = _BASE_T + dt.timedelta(days=1)
    async with get_session() as session:
        newer = await auth.create_session(session, user)

    # 둘 다 만료됐고, 첫 번째만 보관 기간을 넘긴 시점으로 민다.
    clock["t"] = _BASE_T + dt.timedelta(days=7, hours=13)
    async with get_session() as session:
        removed = await auth.purge_expired_sessions(session)
        left = (await session.execute(select(AuthSession.token_hash))).scalars().all()
    assert removed == 1
    assert left == [auth.token_digest(newer)]
    assert auth.token_digest(old) not in left


# ── 2. 제1 불변 — 플래그 off면 역할 게이트가 no-op (§6.2 1단계) ────────────

@pytest.mark.parametrize(
    "gate", [auth.require_agent, auth.require_leader, auth.require_admin]
)
async def test_role_gates_pass_everything_while_flag_off(gate, clock):
    """세션이 없어도 셋 다 통과하고 None을 준다. 기존 창구 거동이 그대로다."""
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    async with get_session() as session:
        assert await gate(_request(), session) is None


async def test_role_gate_names_the_actor_while_flag_off(clock):
    """플래그가 꺼져 있어도 세션이 있으면 누군지는 알려준다(§6.2 3단계 실측용)."""
    user = await _make_user("leader1", role=auth.ROLE_LEADER)
    async with get_session() as session:
        token = await auth.create_session(session, user)
        actor = await auth.require_admin(_request(token), session)
    assert actor is not None and actor.role == auth.ROLE_LEADER, (
        "꺼진 구간에서 관리자 게이트가 팀장을 막았다 — 판정을 하면 안 된다"
    )


async def test_flag_off_gate_does_not_touch_db_without_cookie(clock, monkeypatch):
    """쿠키가 없으면 DB를 아예 안 건드린다.

    마이그레이션이 아직 안 돈 배포에서도 기존 창구가 안 터지는 근거다. 세션 조회 함수에
    폭탄을 놓고 안 밟히는지 본다.
    """

    async def _boom(*_args, **_kwargs):
        raise AssertionError("쿠키가 없는데 세션을 조회했다")

    monkeypatch.setattr(auth, "resolve_session", _boom)
    async with get_session() as session:
        assert await auth.require_agent(_request(), session) is None


# ── 3. 플래그 on — 401 / 403 갈림 (§7.2) ───────────────────────────────────

async def test_flag_on_without_session_is_401(flag_on, clock):
    """세션이 없으면 401이다 — 화면이 로그인으로 보낸다."""
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await auth.require_agent(_request(), session)
    assert exc.value.status_code == 401
    assert exc.value.detail == "로그인이 필요합니다."


async def test_flag_on_insufficient_role_is_403_not_401(flag_on, clock):
    """급이 모자라면 **403**이다. 401로 주면 요원이 관리자 창구를 건드릴 때마다 로그아웃된다."""
    user = await _make_user("agent11", role=auth.ROLE_AGENT)
    async with get_session() as session:
        token = await auth.create_session(session, user)
        assert await auth.require_agent(_request(token), session) is not None
        with pytest.raises(HTTPException) as exc:
            await auth.require_leader(_request(token), session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "권한이 없습니다."


async def test_flag_on_ladder_is_inclusive_upward(flag_on, clock):
    """윗급은 아랫급 창구를 다 쓴다. 관리자가 요원 창구에서 막히면 안 된다."""
    admin = await _make_user("admin12", role=auth.ROLE_ADMIN)
    async with get_session() as session:
        token = await auth.create_session(session, admin)
        for gate in (auth.require_agent, auth.require_leader, auth.require_admin):
            assert await gate(_request(token), session) is not None


async def test_flag_on_expired_session_is_401(flag_on, clock):
    """만료된 세션은 403이 아니라 401이다 — 화면이 로그인으로 보내야 한다(§7.2)."""
    user = await _make_user("admin13", role=auth.ROLE_ADMIN)
    async with get_session() as session:
        token = await auth.create_session(session, user)
    clock["t"] = _BASE_T + dt.timedelta(hours=12, seconds=1)
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await auth.require_agent(_request(token), session)
    assert exc.value.status_code == 401


# ── 4. 마지막 관리자 보호 — 행 잠금 직렬화 (§4) ────────────────────────────

async def test_last_admin_demotion_is_blocked(clock):
    admin = await _make_user("solo", role=auth.ROLE_ADMIN)
    await _make_user("agentx", role=auth.ROLE_AGENT)
    async with get_session() as session:
        with pytest.raises(HTTPException) as exc:
            await auth.assert_not_last_admin(session, admin.id)
    assert exc.value.status_code == 403
    assert "마지막 관리자" in exc.value.detail


async def test_second_to_last_admin_demotion_passes(clock):
    a = await _make_user("admin_a", role=auth.ROLE_ADMIN)
    await _make_user("admin_b", role=auth.ROLE_ADMIN)
    async with get_session() as session:
        await auth.assert_not_last_admin(session, a.id)  # 안 터지면 통과


async def test_non_admin_target_is_never_blocked(clock):
    """관리자가 아닌 계정을 내리는 건 마지막 관리자 규칙과 무관하다."""
    await _make_user("solo2", role=auth.ROLE_ADMIN)
    agent = await _make_user("agenty", role=auth.ROLE_AGENT)
    async with get_session() as session:
        await auth.assert_not_last_admin(session, agent.id)


async def test_inactive_admin_does_not_count_as_a_survivor(clock):
    """해제된 관리자는 남은 관리자로 안 센다 — 세면 로그인 못 하는 계정이 잠금을 푼다."""
    live = await _make_user("admin_live", role=auth.ROLE_ADMIN)
    await _make_user("admin_off", role=auth.ROLE_ADMIN, is_active=False)
    async with get_session() as session:
        with pytest.raises(HTTPException):
            await auth.assert_not_last_admin(session, live.id)


async def test_concurrent_demotion_of_two_admins_leaves_one(clock):
    """관리자 둘을 **동시에** 강등하면 하나만 통과한다. 행 잠금이 그걸 만든다.

    락이 없으면 두 트랜잭션이 각자 "관리자 2명"을 보고 둘 다 통과해 관리자가 0이 된다.
    그래서 두 트랜잭션을 배리어로 **같은 순간에** 잠금 앞에 세운다 — 스냅샷이 겹친 그
    상태가 사고 조건 그대로다. 잠근 쪽이 커밋하면 기다리던 쪽이 다시 읽어(READ COMMITTED
    잠금 재평가) 이미 내려간 사람을 빼고 세게 되고, 거기서 403이 난다.

    ⚠ 잠금과 강등 UPDATE가 **같은 트랜잭션**이어야 뜻이 있다. 잠그고 커밋한 뒤 UPDATE를
    따로 내면 그 사이가 다시 열린다.
    """
    a = await _make_user("admin_1", role=auth.ROLE_ADMIN)
    b = await _make_user("admin_2", role=auth.ROLE_ADMIN)
    barrier = asyncio.Barrier(2)

    async def demote(user_id: int) -> int:
        async with get_session() as session:
            # 트랜잭션을 먼저 연다. 둘 다 열린 뒤에 잠금을 잡으러 가야 사고 조건이 선다.
            await session.execute(select(AppUser.id).limit(1))
            await barrier.wait()
            try:
                await auth.assert_not_last_admin(session, user_id)
            except HTTPException as exc:
                return exc.status_code
            await session.execute(
                update(AppUser).where(AppUser.id == user_id).values(role=auth.ROLE_AGENT)
            )
            await session.commit()
            return 200

    # 잠금이 깨져 서로 물면 시험이 매달린다. 여기서 끊어 실패로 만든다.
    results = await asyncio.wait_for(
        asyncio.gather(demote(a.id), demote(b.id)), timeout=30
    )
    assert sorted(results) == [200, 403], f"동시 강등 결과가 {results}다"

    async with get_session() as session:
        left = (
            await session.execute(
                select(AppUser.username).where(
                    AppUser.role == auth.ROLE_ADMIN, AppUser.is_active.is_(True)
                )
            )
        ).scalars().all()
    assert len(left) == 1, f"동시 강등 뒤 남은 관리자가 {left}다 — 0이면 잠금 사고다"
