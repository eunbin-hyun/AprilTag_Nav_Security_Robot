"""계정 창구 — 관리자 전용 게이트 · 마지막 관리자 보호 · 해시 비노출.

계약은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §3·§4·§8 확정 A다.
감사 기록 갈래는 `test_staff_audit.py`가 본다.

## 여기서 재는 것 일곱

1. **관리자 전용** — 요원·팀장이 403이고 세션이 없으면 401이다.
2. **해시 비노출** — 어떤 응답에도 `password_hash`가 안 실린다(§3).
3. **아이디 규칙** — 계정을 **만드는** 창구가 422로 막는다(로그인은 같은 걸 401로 흘린다).
4. **마지막 관리자 보호** — 강등도 해제도 403이다. 관리자가 둘이면 통과한다.
5. **해제·비밀번호 변경이 세션을 끊는다** — 무효화 시각이 남고 옛 비밀번호가 죽는다.
6. **계정 감사 조회** — `GET /api/users/{id}/audit`이 그 계정 기록만, 최근 것부터 준다.
7. **생성·비밀번호 재설정이 기록에 남는다** — 권한 이력에서 제일 무거운 두 사건이다.

## 시험 위생

계정·세션·명부·감사 테이블 비우기와 인메모리 잠금 초기화는 conftest `_clean` 하나가 한다.
이 파일이 자기 앞에서 또 돌리면 그 훅이 빠져도 이 파일만은 통과해 결함이 안 보인다
(2026-08-02 교차 검토로 걷어냈다).
"""
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

from app import auth
from app.config import get_settings
from app.db import get_session
from app.models import AppUser, AuthSession, StaffAudit

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PASSWORD = "pw-12345678"


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def _make_user(username: str, role: str, **values) -> AppUser:
    async with get_session() as session:
        user = AppUser(
            username=username,
            password_hash=auth.hash_password(_PASSWORD),
            display_name=f"이름-{username}",
            role=role,
            is_active=values.pop("is_active", True),
            **values,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


async def _cookie(user: AppUser) -> dict[str, str]:
    async with get_session() as session:
        token = await auth.create_session(session, user)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


# ── 1. 관리자 전용 게이트 (§3) ─────────────────────────────────────────────


async def test_every_account_endpoint_is_admin_only(client, flag_on):
    """요원·팀장은 403이고 세션이 없으면 401이다. 401/403 갈림이 서야 로그아웃 루프가 안 난다."""
    target = await _make_user("target1", auth.ROLE_AGENT)
    admin = await _cookie(await _make_user("admin1", auth.ROLE_ADMIN))
    lower = [
        await _cookie(await _make_user("agent1", auth.ROLE_AGENT)),
        await _cookie(await _make_user("leader1", auth.ROLE_LEADER)),
    ]

    calls = [
        ("get", "/api/users", None),
        ("post", "/api/users",
         {"username": "newone", "password": _PASSWORD, "display_name": "새사람",
          "role": "agent"}),
        ("patch", f"/api/users/{target.id}", {"role": "leader"}),
        ("post", f"/api/users/{target.id}/password", {"password": "new-12345678"}),
    ]
    for method, path, body in calls:
        call = getattr(client, method)
        kwargs = {"json": body} if body is not None else {}
        assert (await call(path, **kwargs)).status_code == 401, path
        for headers in lower:
            assert (
                await call(path, headers=headers, **kwargs)
            ).status_code == 403, f"{path} / {headers}"
        assert (await call(path, headers=admin, **kwargs)).status_code in (200, 201, 204), path


# ── 2. 생성 (§8 확정 A) ────────────────────────────────────────────────────


async def test_create_user_hashes_the_password_and_never_echoes_it(client, flag_on):
    """만든 계정으로 실제 로그인이 된다. 응답에는 비밀번호도 해시도 안 실린다(§3)."""
    admin = await _cookie(await _make_user("admin2", auth.ROLE_ADMIN))
    r = await client.post(
        "/api/users",
        json={"username": "sonseuk", "password": _PASSWORD, "display_name": "손세욱",
              "role": "leader"},
        headers=admin,
    )
    assert r.status_code == 201
    assert r.json()["username"] == "sonseuk"
    assert r.json()["role"] == "leader"
    assert r.json()["is_active"] is True
    assert "password" not in r.text and "scrypt" not in r.text

    async with get_session() as session:
        stored = (
            await session.execute(
                select(AppUser.password_hash).where(AppUser.username == "sonseuk")
            )
        ).scalar_one()
    assert stored.startswith("scrypt$"), "비밀번호가 평문으로 저장됐다"

    login = await client.post(
        "/api/auth/login", json={"username": "sonseuk", "password": _PASSWORD}
    )
    assert login.status_code == 200


async def test_create_user_enforces_the_username_rule(client, flag_on):
    """규칙 밖 아이디는 422다. **만드는** 창구라 형식을 알려 줘도 되는 자리다.

    로그인 창구가 같은 입력을 401로 흘리는 건 계정 열거를 막으려는 반대 이유고, 둘이 다른
    게 정상이다(auth.USERNAME_RE 주석).
    """
    admin = await _cookie(await _make_user("admin3", auth.ROLE_ADMIN))
    for bad in ("so", "a" * 21, "son_seuk", "손세욱", "son seuk"):
        r = await client.post(
            "/api/users",
            json={"username": bad, "password": _PASSWORD, "display_name": "이름",
                  "role": "agent"},
            headers=admin,
        )
        assert r.status_code == 422, bad


async def test_create_user_folds_case_and_blocks_duplicates(client, flag_on):
    """아이디는 접어서 저장한다. 안 접으면 `Admin`과 `admin`이 다른 계정으로 선다."""
    admin = await _cookie(await _make_user("admin4", auth.ROLE_ADMIN))
    first = await client.post(
        "/api/users",
        json={"username": " SonSeuk ", "password": _PASSWORD, "display_name": "손세욱",
              "role": "agent"},
        headers=admin,
    )
    assert first.status_code == 201
    assert first.json()["username"] == "sonseuk"

    dup = await client.post(
        "/api/users",
        json={"username": "sonseuk", "password": _PASSWORD, "display_name": "다른사람",
              "role": "agent"},
        headers=admin,
    )
    assert dup.status_code == 409


async def test_short_password_is_rejected(client, flag_on):
    admin = await _cookie(await _make_user("admin5", auth.ROLE_ADMIN))
    r = await client.post(
        "/api/users",
        json={"username": "shortpw", "password": "1234", "display_name": "이름",
              "role": "agent"},
        headers=admin,
    )
    assert r.status_code == 422


async def test_list_users_never_leaks_hashes(client, flag_on):
    admin = await _cookie(await _make_user("admin6", auth.ROLE_ADMIN))
    await _make_user("agent2", auth.ROLE_AGENT)
    r = await client.get("/api/users", headers=admin)
    assert r.status_code == 200
    assert len(r.json()) == 2
    assert "scrypt" not in r.text and "password" not in r.text


# ── 3. 역할 수정 · 마지막 관리자 보호 (§4 · §8 결정 4) ─────────────────────


async def test_role_change_takes_effect_on_the_next_request(client, flag_on):
    """강등이 **다음 요청부터** 먹힌다. 이게 JWT 대신 세션을 고른 이유다(§1.1)."""
    admin = await _make_user("admin7", auth.ROLE_ADMIN)
    await _make_user("admin8", auth.ROLE_ADMIN)
    admin_headers = await _cookie(admin)
    victim = await _make_user("leader2", auth.ROLE_LEADER)
    victim_headers = await _cookie(victim)

    assert (await client.get("/api/staff", headers=victim_headers)).status_code == 200
    assert (
        await client.patch(
            f"/api/users/{victim.id}", json={"role": "agent"}, headers=admin_headers
        )
    ).status_code == 200
    assert (await client.get("/api/staff", headers=victim_headers)).status_code == 403


async def test_last_admin_cannot_be_demoted_or_deactivated(client, flag_on):
    """마지막 관리자는 강등도 해제도 403이다. 안 막으면 권한 고칠 사람이 사라진다."""
    admin = await _make_user("solo", auth.ROLE_ADMIN)
    await _make_user("agent3", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    demote = await client.patch(
        f"/api/users/{admin.id}", json={"role": "agent"}, headers=headers
    )
    assert demote.status_code == 403
    assert "마지막 관리자" in demote.json()["detail"]

    off = await client.patch(
        f"/api/users/{admin.id}", json={"is_active": False}, headers=headers
    )
    assert off.status_code == 403

    async with get_session() as session:
        row = await session.get(AppUser, admin.id)
        await session.refresh(row)
    assert (row.role, row.is_active) == (auth.ROLE_ADMIN, True), "막힌 요청이 계정을 바꿨다"


async def test_second_to_last_admin_can_step_down(client, flag_on):
    """관리자가 둘이면 자기 강등도 통과한다 — 정본은 마지막 하나만 막는다(§8 결정 4)."""
    admin = await _make_user("admin9", auth.ROLE_ADMIN)
    await _make_user("admin10", auth.ROLE_ADMIN)
    r = await client.patch(
        f"/api/users/{admin.id}", json={"role": "agent"}, headers=await _cookie(admin)
    )
    assert r.status_code == 200
    assert r.json()["role"] == "agent"


# ── 3-1. 급 사다리 (2026-08-08 백지검토 — 자기 승격·총괄 탈취 차단) ────────────


async def test_관리자는_자기를_owner로_못_올린다(client, flag_on):
    """자기 승격 차단이 `assert_can_write_account`가 생긴 이유다.

    지금 급만 봐도(본인은 동급이라 통과) 새 급만 봐도(강등 허용이 같이 막힘) 한쪽이 샌다 —
    본인 갈래는 "내려가기만 통과"로 갈라 재야 한다(docstring ③).
    """
    admin = await _make_user("esc1", auth.ROLE_ADMIN)
    await _make_user("esc2", auth.ROLE_ADMIN)  # 마지막 관리자 잠금과 갈라 재려고 둘을 둔다
    r = await client.patch(
        f"/api/users/{admin.id}", json={"role": "owner"}, headers=await _cookie(admin)
    )
    assert r.status_code == 403
    async with get_session() as session:
        row = await session.get(AppUser, admin.id)
        await session.refresh(row)
    assert row.role == auth.ROLE_ADMIN, "막힌 요청이 역할을 바꿨다"


async def test_관리자는_동급과_총괄_계정을_못_만진다(client, flag_on):
    """역할 수정·해제·비밀번호 셋 다 403이다 — 명부 사다리(§8 결정 4 표)와 같은 그림이다.

    비밀번호가 특히 무겁다: 남의 관리자·총괄 비밀번호를 갈면 그 계정을 통째로 넘겨받는다.
    """
    admin_headers = await _cookie(await _make_user("lad1", auth.ROLE_ADMIN))
    peer = await _make_user("lad2", auth.ROLE_ADMIN)
    boss = await _make_user("lboss", auth.ROLE_OWNER)
    for target in (peer, boss):
        for method, path, body in (
            ("patch", f"/api/users/{target.id}", {"role": "agent"}),
            ("patch", f"/api/users/{target.id}", {"is_active": False}),
            ("post", f"/api/users/{target.id}/password", {"password": "hijack-12345"}),
        ):
            r = await getattr(client, method)(path, json=body, headers=admin_headers)
            assert r.status_code == 403, f"{target.role} {path} {body}"
    # 거짓 옆의 참 — 아랫급은 같은 손이 그대로 된다(사다리가 과하게 닫히지 않았다).
    lower = await _make_user("lag1", auth.ROLE_AGENT)
    assert (
        await client.patch(
            f"/api/users/{lower.id}", json={"role": "leader"}, headers=admin_headers
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/users/{lower.id}/password", json={"password": "fresh-12345"},
            headers=admin_headers,
        )
    ).status_code == 204


async def test_관리자는_관리자급_계정을_못_만들지만_총괄은_만든다(client, flag_on):
    """생성도 새 급이 사다리를 탄다 — 만들어 두고 그 계정으로 들어가면 승격과 같은 길이다."""
    admin_headers = await _cookie(await _make_user("mk1", auth.ROLE_ADMIN))
    for role in ("admin", "owner"):
        r = await client.post(
            "/api/users",
            json={"username": f"mk{role}", "password": _PASSWORD,
                  "display_name": "이름", "role": role},
            headers=admin_headers,
        )
        assert r.status_code == 403, role
    owner_headers = await _cookie(await _make_user("mkowner", auth.ROLE_OWNER))
    r = await client.post(
        "/api/users",
        json={"username": "mkadmin2", "password": _PASSWORD,
              "display_name": "이름", "role": "admin"},
        headers=owner_headers,
    )
    assert r.status_code == 201


async def test_관리자_본인_비밀번호는_전용_창구로만_바꾼다(client, flag_on):
    """재설정 창구는 본인을 안 받는다(403) — 지금 비밀번호를 안 묻는 "잊었을 때" 창구라,
    본인까지 열면 세션을 훔친 쪽이 지금 비밀번호 없이 계정을 가져간다(08-08 교차 검증).
    본인은 지금 비밀번호를 확인하는 `POST /api/auth/password`로 간다."""
    admin = await _make_user("selfpw1", auth.ROLE_ADMIN)
    headers = await _cookie(admin)
    r = await client.post(
        f"/api/users/{admin.id}/password", json={"password": "nae-sae-bimil-99"},
        headers=headers,
    )
    assert r.status_code == 403
    assert "비밀번호 변경" in r.json()["detail"]
    # 참 쪽 — 전용 창구로는 지금 비밀번호를 대고 바꿀 수 있다.
    ok = await client.post(
        "/api/auth/password",
        json={"current_password": _PASSWORD, "new_password": "nae-sae-bimil-99"},
        headers=headers,
    )
    assert ok.status_code == 204, ok.text
    assert (
        await client.post(
            "/api/auth/login",
            json={"username": "selfpw1", "password": "nae-sae-bimil-99"},
        )
    ).status_code == 200


async def test_총괄은_자기를_강등하거나_해제할_수_없다(client, flag_on):
    """총괄이 스스로 내려가면 owner 행을 만질 사람이 없어진다(docstring ②의 못 — 명부 판
    state_change와 같은 그림). 거짓 옆의 참 — 남의 관리자 강등은 총괄이라 그대로 된다."""
    owner = await _make_user("ownself", auth.ROLE_OWNER)
    peer = await _make_user("ownadm1", auth.ROLE_ADMIN)
    await _make_user("ownadm2", auth.ROLE_ADMIN)  # 마지막 관리자 잠금을 안 밟게 하나 더
    headers = await _cookie(owner)
    for body in ({"role": "admin"}, {"is_active": False}):
        r = await client.patch(f"/api/users/{owner.id}", json=body, headers=headers)
        assert r.status_code == 403, body
        assert "총괄" in r.json()["detail"]
    ok = await client.patch(
        f"/api/users/{peer.id}", json={"role": "leader"}, headers=headers
    )
    assert ok.status_code == 200
    async with get_session() as session:
        row = await session.get(AppUser, owner.id)
        await session.refresh(row)
    assert (row.role, row.is_active) == (auth.ROLE_OWNER, True), "막힌 요청이 계정을 바꿨다"


async def test_모르는_역할은_닫는_쪽이다(flag_on):
    """RANK 밖 역할이 든 행은 403이다 — 급이 안 적힌 행을 아무나 건드리면 상한이 뜻을
    잃는다(단위 판정 · 창구 시험은 스키마 422가 먼저 막아 여기까지 못 온다)."""
    ghost_actor = auth.AuthActor(id=1, username="gh", display_name="가", role=auth.ROLE_ADMIN)
    with pytest.raises(HTTPException) as e:
        auth.assert_can_write_account(ghost_actor, target_role="ghost", target_user_id=2)
    assert e.value.status_code == 403


async def test_관리자가_교육생_계정을_만든다(client, flag_on):
    """교육생(trainee)은 사다리 맨 아래라 관리자 손으로 만들어진다(2026-08-08)."""
    admin = await _cookie(await _make_user("mktr1", auth.ROLE_ADMIN))
    r = await client.post(
        "/api/users",
        json={"username": "trainee1", "password": _PASSWORD,
              "display_name": "교육생 한 명", "role": "trainee"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "trainee"


async def test_deactivating_an_account_revokes_its_sessions(client, flag_on):
    """해제하면 그 사람의 살아 있는 세션에 무효화 시각이 찍힌다(§1.4 — 언제 끊겼나가 감사다)."""
    admin = await _make_user("admin11", auth.ROLE_ADMIN)
    victim = await _make_user("agent4", auth.ROLE_AGENT)
    victim_headers = await _cookie(victim)

    assert (await client.get("/api/auth/me", headers=victim_headers)).status_code == 200
    assert (
        await client.patch(
            f"/api/users/{victim.id}", json={"is_active": False},
            headers=await _cookie(admin),
        )
    ).status_code == 200

    assert (await client.get("/api/auth/me", headers=victim_headers)).status_code == 401
    async with get_session() as session:
        revoked = (
            await session.execute(
                select(AuthSession.revoked_at).where(AuthSession.user_id == victim.id)
            )
        ).scalars().all()
    assert all(v is not None for v in revoked), "세션이 안 끊겼다"

    # 로그인도 막힌다(응답은 계정 열거를 막는 같은 401이다).
    assert (
        await client.post(
            "/api/auth/login", json={"username": "agent4", "password": _PASSWORD}
        )
    ).status_code == 401


async def test_patch_needs_at_least_one_field_and_a_real_user(client, flag_on):
    admin = await _cookie(await _make_user("admin12", auth.ROLE_ADMIN))
    target = await _make_user("agent5", auth.ROLE_AGENT)
    assert (
        await client.patch(f"/api/users/{target.id}", json={}, headers=admin)
    ).status_code == 422
    assert (
        await client.patch("/api/users/424242", json={"role": "agent"}, headers=admin)
    ).status_code == 404


# ── 4. 비밀번호 다시 정하기 ────────────────────────────────────────────────


async def test_password_reset_kills_the_old_password_and_live_sessions(client, flag_on):
    """옛 비밀번호가 죽고 살아 있던 세션도 끊긴다. 안 끊으면 바꾼 뜻이 없다."""
    admin = await _make_user("admin13", auth.ROLE_ADMIN)
    victim = await _make_user("agent6", auth.ROLE_AGENT)
    victim_headers = await _cookie(victim)
    new_password = "sae-bimil-9876"

    r = await client.post(
        f"/api/users/{victim.id}/password",
        json={"password": new_password},
        headers=await _cookie(admin),
    )
    assert r.status_code == 204
    assert not r.content

    assert (await client.get("/api/auth/me", headers=victim_headers)).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"username": "agent6", "password": _PASSWORD}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/api/auth/login", json={"username": "agent6", "password": new_password}
        )
    ).status_code == 200


# ── 5. 계정 감사 조회 — `GET /api/users/{id}/audit` ────────────────────────


async def test_account_audit_read_is_admin_only(client, flag_on):
    """권한 매트릭스 — 익명 401 · 요원 403 · 팀장 403 · 관리자 200.

    401과 403 갈림이 여기서도 서야 한다(§7.2). 403을 401로 내면 급이 모자란 사람이 창구를
    한 번 건드릴 때마다 로그아웃된다.
    """
    target = await _make_user("audittarget", auth.ROLE_AGENT)
    admin = await _cookie(await _make_user("auditadmin", auth.ROLE_ADMIN))
    path = f"/api/users/{target.id}/audit"

    assert (await client.get(path)).status_code == 401
    for role, username in ((auth.ROLE_AGENT, "auditagent"), (auth.ROLE_LEADER, "auditleader")):
        headers = await _cookie(await _make_user(username, role))
        assert (await client.get(path, headers=headers)).status_code == 403, role
    assert (await client.get(path, headers=admin)).status_code == 200


async def test_account_audit_returns_that_accounts_changes_newest_first(client, flag_on):
    """그 계정에 벌어진 변경이 최근 것부터 나온다. 본문은 명부 감사와 같은 모양이다."""
    admin = await _make_user("audit1", auth.ROLE_ADMIN)
    await _make_user("audit2", auth.ROLE_ADMIN)  # 마지막 관리자 잠금을 안 밟게 둘을 둔다
    target = await _make_user("audittgt2", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    assert (
        await client.patch(f"/api/users/{target.id}", json={"role": "leader"}, headers=headers)
    ).status_code == 200
    assert (
        await client.patch(
            f"/api/users/{target.id}", json={"is_active": False}, headers=headers
        )
    ).status_code == 200

    r = await client.get(f"/api/users/{target.id}/audit", headers=headers)
    assert r.status_code == 200
    rows = r.json()
    assert [row["action"] for row in rows] == ["deactivate", "update"]

    # 행위자 넷이 문자열로도 복사돼 있다(§2.4).
    assert rows[0]["actor_user_id"] == admin.id
    assert rows[0]["actor_username"] == "audit1"
    assert rows[0]["actor_display_name"] == "이름-audit1"
    assert rows[0]["actor_role"] == "admin"
    # 대상과 갈래가 본문에 있다. `staff_id`는 "명부 행이 아니다" 표시(0)다.
    assert rows[0]["staff_id"] == 0
    assert rows[0]["after"]["target"] == "app_user"
    assert rows[0]["after"]["user_id"] == target.id
    assert rows[1]["before"]["role"] == "agent" and rows[1]["after"]["role"] == "leader"
    # ⚠ 해시가 기록으로 새면 응답에서 막은 걸 이 창구가 흘리는 셈이다.
    assert "scrypt" not in r.text and "password" not in r.text


async def test_계정_생성이_감사에_create로_남는다(client, flag_on):
    """⭐ 계정 생성은 권한 이력에서 제일 무거운 사건이다 — 안 남으면 "누가 이 계정을
    만들었나"가 어디에도 없다(§2.4 action enum의 `create`).

    창구를 타고 만든 계정만 잰다. 시험 헬퍼(`_make_user`)는 DB에 직접 넣는 자리라 기록이
    안 남는 게 맞다.
    """
    admin = await _make_user("auditc1", auth.ROLE_ADMIN)
    headers = await _cookie(admin)

    created = await client.post(
        "/api/users",
        json={"username": "manden", "password": _PASSWORD, "display_name": "만든사람",
              "role": "agent"},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    new_id = created.json()["id"]

    r = await client.get(f"/api/users/{new_id}/audit", headers=headers)
    rows = r.json()
    assert [row["action"] for row in rows] == ["create"]
    assert rows[0]["before"] is None, "만들기 전 상태는 없다"
    assert rows[0]["staff_id"] == 0
    assert rows[0]["after"]["target"] == "app_user"
    assert rows[0]["after"]["user_id"] == new_id
    assert rows[0]["after"]["username"] == "manden"
    assert rows[0]["after"]["role"] == "agent"
    # 행위자 사본(§2.4)이 생성에도 실린다.
    assert rows[0]["actor_username"] == "auditc1"
    # ⚠ 만들 때 받은 비밀번호가 기록으로 새면 안 된다.
    assert "scrypt" not in r.text and _PASSWORD not in r.text


async def test_비밀번호_재설정이_감사에_update로_남는다(client, flag_on):
    """⭐ 남의 비밀번호를 갈아 끼우는 것도 같은 급의 사건이다.

    낱말은 `update`다 — §2.4 enum이 셋뿐이라 새 낱말을 만들지 않고, 무엇을 바꿨나는 본문의
    `password_reset` 한 칸이 말한다. 비밀번호는 원문도 해시도 기록에 없다.
    """
    admin = await _make_user("auditp1", auth.ROLE_ADMIN)
    target = await _make_user("auditptgt", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    done = await client.post(
        f"/api/users/{target.id}/password",
        json={"password": "sae-bimil-1234"},
        headers=headers,
    )
    assert done.status_code == 204, done.text

    r = await client.get(f"/api/users/{target.id}/audit", headers=headers)
    rows = r.json()
    assert [row["action"] for row in rows] == ["update"]
    assert rows[0]["after"]["target"] == "app_user"
    assert rows[0]["after"]["user_id"] == target.id
    assert rows[0]["after"]["password_reset"] is True
    assert rows[0]["actor_username"] == "auditp1"
    # ⚠ 새 비밀번호도 해시도 기록에 없다.
    assert "sae-bimil-1234" not in r.text
    assert "scrypt" not in r.text and "password_hash" not in r.text


async def test_감사_사본은_잠금을_잡은_뒤_실물을_뜬다(client, flag_on, monkeypatch):
    """⭐ `before`는 **잠금 뒤에 다시 읽은 값**이라야 한다(검토 F10 수리).

    대상 행을 처음 읽는 자리는 잠금이 없다(교착을 피하려고 `assert_not_last_admin` 하나에만
    잠금을 맡긴 설계다). 그래서 "읽고 → 잠그고 → 쓰는" 사이에 딴 요청이 같은 계정을
    해제하고 커밋할 수 있고, 그때 메모리에 남은 옛 값으로 사본을 뜨면 감사 행이 DB 실물과
    다른 거짓을 남긴다. 권한 이력을 되짚는 유일한 자료라 그 한 줄이 거짓이면 표 전체가
    못 믿을 값이 된다.

    경합을 결정적으로 만든다 — `assert_not_last_admin`을 가로채, 그 창구가 대상 행을 읽은
    **뒤** 잠금을 잡기 **직전**에 딴 트랜잭션이 같은 계정을 해제하고 커밋하게 한다. 실제
    동시 요청 둘을 띄우면 어느 쪽이 먼저 도는지가 시험마다 갈려서 아무것도 못 재게 된다.
    """
    # 08-08 급 사다리(assert_can_write_account)로 관리자가 동급 계정을 못 만지게 되면서
    # 행위자를 총괄로 올렸다 — 이 시험이 재는 건 권한이 아니라 감사 사본의 신선도라
    # 목적은 그대로다. 필러 관리자는 마지막 관리자 잠금을 안 밟으려고 둔다.
    owner = await _make_user("lockowner", auth.ROLE_OWNER)
    target = await _make_user("locktgt", auth.ROLE_ADMIN)
    await _make_user("lockfiller", auth.ROLE_ADMIN)
    headers = await _cookie(owner)

    real_guard = auth.assert_not_last_admin

    async def _deactivate_then_guard(db, user_id):
        async with get_session() as other:
            await other.execute(
                update(AppUser).where(AppUser.id == target.id).values(is_active=False)
            )
            await other.commit()
        return await real_guard(db, user_id)

    monkeypatch.setattr(auth, "assert_not_last_admin", _deactivate_then_guard)

    r = await client.patch(
        f"/api/users/{target.id}", json={"role": "agent"}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["is_active"] is False, "응답이 DB 실물과 다르다"

    rows = (await client.get(f"/api/users/{target.id}/audit", headers=headers)).json()
    assert len(rows) == 1, rows
    before, after = rows[0]["before"], rows[0]["after"]
    assert before["is_active"] is False, (
        "감사 사본이 잠금 앞 옛 메모리 값을 떴다 — 기록이 DB 실물과 어긋난다"
    )
    assert before["role"] == auth.ROLE_ADMIN
    assert (after["role"], after["is_active"]) == (auth.ROLE_AGENT, False)


async def test_비밀번호_교체와_세션_끊기가_한_트랜잭션이다(client, flag_on, monkeypatch):
    """⭐ 커밋이 하나라야 한다(검토 F19 수리).

    예전에는 해시를 먼저 커밋하고 그 뒤에 세션 무효화를 따로 불렀다. 커밋 둘 사이에서
    죽으면 비밀번호만 바뀌고 옛 세션은 살아 있다 — "샜을지 모른다"라서 바꾸는 자리인데
    정작 새어 나간 세션이 남는, 이 창구가 막으려던 바로 그 상태다.

    무효화 단계에서 터뜨려 그 갈라짐을 잰다. 한 트랜잭션이면 해시도 감사 행도 같이
    되돌아가서 옛 비밀번호가 그대로 살아 있다. 갈라져 있으면 해시만 바뀐 채 남는다.
    """
    admin = await _make_user("txadmin", auth.ROLE_ADMIN)
    target = await _make_user("txtgt", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    # ⚠ 스텁이 `commit` 키워드를 받아야 한다. 창구가 `commit=False`로 불러야 두 쓰기가 한
    #   트랜잭션에 실리는데(F19 수리), 스텁 서명에 그 칸이 없으면 갈라짐을 재기도 전에
    #   TypeError로 죽는다 — 실제로 2026-08-02에 이 자리가 그 모양으로 빨갰다. 그리고 받은
    #   값을 그대로 들고 있다가 아래에서 `False`인지 본다. 안 보면 창구가 `commit=True`로
    #   되돌아가도(=커밋이 둘로 갈려도) 이 시험이 그대로 초록이다.
    called: dict[str, object] = {}

    async def _boom(db, user_id, *, commit: bool = True):
        called["commit"] = commit
        raise RuntimeError("세션 무효화 단계에서 죽었다")

    monkeypatch.setattr(auth, "revoke_user_sessions", _boom)

    with pytest.raises(RuntimeError):
        await client.post(
            f"/api/users/{target.id}/password",
            json={"password": "sae-bimil-1234"},
            headers=headers,
        )

    async with get_session() as session:
        row = await session.get(AppUser, target.id)
        audits = (
            await session.execute(
                select(StaffAudit).where(StaffAudit.after["user_id"].astext == str(target.id))
            )
        ).scalars().all()
    assert auth.verify_password(_PASSWORD, row.password_hash), (
        "세션을 못 끊었는데 해시만 바뀌어 남았다 — 커밋이 둘로 갈려 있다"
    )
    assert not auth.verify_password("sae-bimil-1234", row.password_hash)
    assert audits == [], "되돌아갔어야 할 감사 행이 남았다"
    assert called.get("commit") is False, (
        "창구가 세션 무효화를 자기 커밋으로 불렀다 — 해시 교체와 커밋이 둘로 갈려서, 그 사이에서 "
        "죽으면 옛 비밀번호로 연 세션이 절대 만료까지 산다(F19)"
    )


async def test_account_audit_does_not_mix_other_targets(client, flag_on):
    """남의 계정 기록도, 0번 자리에 같이 쌓이는 신원 삭제 기록도 안 섞인다.

    `staff_id=0`은 "명부 행이 아니다" 표시일 뿐이라 그 자리에 `identity_clear`가 같이 쌓인다.
    `staff_id`만 보고 거르면 두 갈래가 한 목록으로 나온다.
    """
    admin = await _make_user("audit3", auth.ROLE_ADMIN)
    await _make_user("audit4", auth.ROLE_ADMIN)
    mine = await _make_user("mine1", auth.ROLE_AGENT)
    other = await _make_user("other1", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    await client.patch(f"/api/users/{mine.id}", json={"role": "leader"}, headers=headers)
    await client.patch(f"/api/users/{other.id}", json={"role": "leader"}, headers=headers)

    # 명부 행이 아닌 감사 행 하나를 더 만든다(신원 삭제 갈래와 같은 모양).
    async with get_session() as session:
        session.add(
            StaffAudit(
                staff_id=0,
                action="identity_clear",
                actor_user_id=admin.id,
                actor_username="audit3",
                actor_display_name="이름-audit3",
                actor_role="admin",
                before={"identified_person": "가명-1"},
                after={"target": "alert_identity", "alert_id": 1},
            )
        )
        await session.commit()

    rows = (await client.get(f"/api/users/{mine.id}/audit", headers=headers)).json()
    assert len(rows) == 1
    assert rows[0]["after"]["user_id"] == mine.id
    assert all(row["after"]["target"] == "app_user" for row in rows)


async def test_account_audit_404_for_a_missing_account(client, flag_on):
    """없는 계정 번호면 404다 — "그 계정의 이력"을 읽는 자리라 대상이 실재해야 한다."""
    admin = await _cookie(await _make_user("audit5", auth.ROLE_ADMIN))
    assert (await client.get("/api/users/424242/audit", headers=admin)).status_code == 404


async def test_account_audit_is_read_only(client, flag_on):
    """쓰기·삭제 메서드가 아예 없다. append-only는 창구 부재로 지킨다(§4)."""
    admin = await _cookie(await _make_user("audit6", auth.ROLE_ADMIN))
    target = await _make_user("audittgt3", auth.ROLE_AGENT)
    path = f"/api/users/{target.id}/audit"
    for method in ("post", "patch", "delete", "put"):
        r = await getattr(client, method)(path, headers=admin)
        assert r.status_code == 405, f"{method}가 열려 있다"


async def test_플래그가_꺼져도_계정_감사_조회는_익명을_막는다(client):
    """계정 창구와 같은 급이라 플래그와 무관하게 늘 판정한다(검증 F1 수리와 같은 자리)."""
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    target = await _make_user("audittgt4", auth.ROLE_AGENT)
    assert (await client.get(f"/api/users/{target.id}/audit")).status_code == 401


# ── 6. 플래그 off 구간 — 지금 거동을 눈에 보이게 못박는다 ───────────────────


async def test_플래그가_꺼져도_계정_창구는_익명을_막는다(client):
    """⭐ 계정 창구는 **플래그와 무관하게** 늘 관리자만 통과한다(검증 F1 수리).

    폭이 제일 넓은 자리였다 — 열어 두면 롤아웃 1~4단계에 아무나 관리자 계정을 만들고,
    플래그를 켠 뒤 그 계정으로 들어오는 뒷문이 됐다(검증이 끝까지 뚫어 보였다).
    계정 심기는 창구가 아니라 `tools/seed_users.py`로 한다(§6.2 2단계).
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"
    created = await client.post(
        "/api/users",
        json={"username": "anonadmin", "password": _PASSWORD, "display_name": "익명",
              "role": "admin"},
    )
    assert created.status_code == 401, created.text
    assert (await client.get("/api/users")).status_code == 401


async def test_강등_갈래를_잠금_앞에서_안_가른다(client, flag_on, monkeypatch):
    """⭐ "대상이 지금 관리자인가"를 **잠금 없는 첫 SELECT로 판정하지 않는다**(3차 X20 수리).

    예전에는 `user.role == ADMIN and user.is_active`를 앞세워 갈래를 갈랐다. 그 값은
    `_get_user`가 잠금 없이 읽은 것이라, 그 사이에 관리자로 승격된 계정은 검사를 통째로
    건너뛴다 — 그 갈래로 강등이 겹치면 관리자가 0이 되고, 그때는 권한을 고칠 사람이 없다.

    `assert_not_last_admin`은 활성 관리자 행을 **잠근 뒤 다시 읽어** 판정하므로, 갈래를
    없애고 그 함수 하나에 맡기면 창이 사라진다. 대상 행을 `FOR UPDATE`로 미리 잠그는 쪽은
    안 쓴다 — 잠금 순서가 둘로 갈려 교착이 열린다(라우터 모듈 머리).

    경합을 결정적으로 만든다 — 창구가 대상 행을 읽은 **뒤** 잠금을 잡기 **직전**에, 딴
    트랜잭션이 대상을 관리자로 올리고 행위자를 내린다(감사 사본 시험과 같은 수법).
    옛 판에서는 갈래 조건이 거짓이라 이 훅이 아예 안 돌고 강등이 그대로 통과했다.
    """
    admin = await _make_user("x20adm", auth.ROLE_ADMIN)
    target = await _make_user("x20tgt", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    real_guard = auth.assert_not_last_admin

    async def _promote_then_guard(db, user_id):
        async with get_session() as other:
            await other.execute(
                update(AppUser).where(AppUser.id == target.id).values(role=auth.ROLE_ADMIN)
            )
            await other.execute(
                update(AppUser).where(AppUser.id == admin.id).values(role=auth.ROLE_AGENT)
            )
            await other.commit()
        return await real_guard(db, user_id)

    monkeypatch.setattr(auth, "assert_not_last_admin", _promote_then_guard)

    r = await client.patch(
        f"/api/users/{target.id}", json={"role": "agent"}, headers=headers
    )
    assert r.status_code == 403, (
        "승격이 겹친 대상을 그대로 강등했다 — 관리자가 0이 되는 자리다"
    )

    async with get_session() as session:
        row = await session.get(AppUser, target.id)
        await session.refresh(row)
    assert row.role == auth.ROLE_ADMIN, "막힌 요청이 계정을 바꿨다"
