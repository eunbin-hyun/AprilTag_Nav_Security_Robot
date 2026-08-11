"""명부·계정 감사 기록 — 같은 트랜잭션 · append-only · 행위자 문자열 사본.

계약은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §2.4·§4다.

## 여기서 재는 것 넷

1. **같은 트랜잭션** — 변경 행과 감사 행의 `xmin`이 같다. 두 INSERT 사이에 커밋이 끼면
   그 값이 갈라지므로, 이건 "커밋을 안 끼웠다"의 직접 증거다(코드를 안 읽고 재는 잣대).
2. **append-only** — 감사를 고치거나 지우는 창구가 라우팅 표에 아예 없다.
3. **행위자 문자열 사본** — 계정을 지워도 누가 했는지가 남는다(FK만 NULL로 끊긴다).
4. **계정 역할 변경도 남는다** — `staff_id`는 0(명부 행 아님) 표시고 진짜 대상은 본문에 있다.

## 시험 위생

`staff`·`staff_audit`·`app_user`·`auth_session` 비우기와 인메모리 잠금 초기화는 conftest
`_clean` 하나가 한다. 이 파일이 자기 앞에서 또 돌리면 그 훅이 빠져도 이 파일만은 통과해
결함이 안 보인다 — 2026-08-02 교차 검토로 걷어냈다.
"""
import pytest
from sqlalchemy import select, text

from app import auth
from app.db import engine, get_session
from app.main import app
from app.models import AppUser, Staff, StaffAudit
from app.routers.staff import ACCOUNT_AUDIT_STAFF_ID, add_audit, audit_snapshot

pytestmark = pytest.mark.asyncio(loop_scope="session")


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


async def _audit_rows(staff_id: int) -> list[StaffAudit]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(StaffAudit)
                    .where(StaffAudit.staff_id == staff_id)
                    .order_by(StaffAudit.id)
                )
            ).scalars().all()
        )


async def _xmin(table: str, row_id: int) -> str:
    """그 행을 마지막으로 쓴 트랜잭션 번호. 같은 커밋에 들어간 행끼리는 값이 같다."""
    async with get_session() as session:
        return (
            await session.execute(
                text(f"SELECT xmin::text FROM {table} WHERE id = :id"), {"id": row_id}
            )
        ).scalar_one()


# ── 1. 명부 변경 셋이 남는다 ───────────────────────────────────────────────


async def test_create_update_deactivate_each_leave_one_row(client):
    """등록·수정·해제가 각각 낱말 하나씩 남긴다. 행위자 넷도 같이 복사된다."""
    admin = await _make_user("admin1", auth.ROLE_ADMIN)
    headers = await _cookie(admin)

    created = await client.post(
        "/api/staff",
        json={"name": "홍길동", "rank": "agent", "student_no": "0451"},
        headers=headers,
    )
    assert created.status_code == 201
    staff_id = created.json()["id"]

    assert (
        await client.patch(
            f"/api/staff/{staff_id}", json={"name": "홍길순"}, headers=headers
        )
    ).status_code == 200
    assert (
        await client.post(f"/api/staff/{staff_id}/deactivate", headers=headers)
    ).status_code == 200

    rows = await _audit_rows(staff_id)
    assert [r.action for r in rows] == ["create", "update", "deactivate"]

    for row in rows:
        assert row.actor_user_id == admin.id
        assert row.actor_username == "admin1"
        assert row.actor_display_name == "이름-admin1"
        assert row.actor_role == "admin"

    assert rows[0].before is None, "등록에는 이전 값이 없다"
    assert rows[0].after["name"] == "홍길동"
    assert rows[1].before["name"] == "홍길동"
    assert rows[1].after["name"] == "홍길순"
    assert rows[2].before["is_active"] is True
    assert rows[2].after["is_active"] is False


async def test_idempotent_deactivate_does_not_add_noise(client):
    """이미 해제된 행을 다시 눌러도 기록이 안 는다 — 안 바뀐 조작까지 적으면 기록이 눌러 본
    횟수로 채워진다."""
    headers = await _cookie(await _make_user("admin2", auth.ROLE_ADMIN))
    staff_id = (
        await client.post(
            "/api/staff", json={"name": "해제대상", "rank": "agent"}, headers=headers
        )
    ).json()["id"]

    await client.post(f"/api/staff/{staff_id}/deactivate", headers=headers)
    await client.post(f"/api/staff/{staff_id}/deactivate", headers=headers)

    actions = [r.action for r in await _audit_rows(staff_id)]
    assert actions == ["create", "deactivate"]


async def test_blocked_write_leaves_no_audit_row(client, monkeypatch):
    """403으로 막힌 요청은 기록도 안 남긴다 — 방어가 판정 뒤 기록보다 앞에 선다."""
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        leader = await _cookie(await _make_user("leader1", auth.ROLE_LEADER))
        r = await client.post(
            "/api/staff", json={"name": "위로", "rank": "admin"}, headers=leader
        )
        assert r.status_code == 403
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()

    async with get_session() as session:
        count = (await session.execute(select(StaffAudit.id))).scalars().all()
    assert count == [], "막힌 요청이 감사 행을 남겼다"


# ── 2. 같은 트랜잭션 (§4) ──────────────────────────────────────────────────


async def test_audit_row_shares_the_transaction_with_the_change(client):
    """명부 행과 감사 행의 `xmin`이 같다 = 두 INSERT 사이에 커밋이 없었다.

    갈라지면 변경만 남고 기록이 빠지는(또는 그 반대인) 창이 실제로 열려 있다는 뜻이다.
    해제(UPDATE)도 같은 잣대로 잰다 — UPDATE된 행의 xmin은 그 트랜잭션 번호로 바뀐다.
    """
    headers = await _cookie(await _make_user("admin3", auth.ROLE_ADMIN))

    created = await client.post(
        "/api/staff", json={"name": "같은트랜잭션", "rank": "agent"}, headers=headers
    )
    staff_id = created.json()["id"]
    audit_id = (await _audit_rows(staff_id))[0].id
    assert await _xmin("staff", staff_id) == await _xmin("staff_audit", audit_id), (
        "등록과 감사 기록이 다른 트랜잭션에서 커밋됐다"
    )

    await client.post(f"/api/staff/{staff_id}/deactivate", headers=headers)
    audit_id = (await _audit_rows(staff_id))[-1].id
    assert await _xmin("staff", staff_id) == await _xmin("staff_audit", audit_id), (
        "해제와 감사 기록이 다른 트랜잭션에서 커밋됐다"
    )


# ── 3. append-only는 창구 부재로 지킨다 (§4) ───────────────────────────────


async def test_no_endpoint_can_modify_or_delete_audit_records():
    """감사 경로에 읽기 말고는 아무 메서드도 없다.

    "수정·삭제 라우터를 아예 안 만든다"가 계약이라, 코드를 읽는 대신 라우팅 표를 직접 센다.
    나중에 누가 편의로 DELETE를 얹으면 여기가 빨개진다.
    """
    audit_routes = [
        r for r in app.routes if "audit" in getattr(r, "path", "")
    ]
    assert audit_routes, "감사 읽기 창구가 아예 없다"
    for route in audit_routes:
        assert set(route.methods) <= {"GET", "HEAD"}, (
            f"{route.path}에 읽기 밖 메서드가 붙었다: {route.methods}"
        )


async def test_audit_read_is_newest_first_and_leader_gated(client, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        admin = await _cookie(await _make_user("admin4", auth.ROLE_ADMIN))
        agent = await _cookie(await _make_user("agent1", auth.ROLE_AGENT))
        staff_id = (
            await client.post(
                "/api/staff", json={"name": "이력", "rank": "agent"}, headers=admin
            )
        ).json()["id"]
        await client.patch(f"/api/staff/{staff_id}", json={"name": "이력2"}, headers=admin)

        listed = await client.get(f"/api/staff/{staff_id}/audit", headers=admin)
        assert listed.status_code == 200
        assert [r["action"] for r in listed.json()] == ["update", "create"]
        # 해시·비밀번호 같은 값이 기록으로 새지 않는다.
        assert "password" not in listed.text and "scrypt" not in listed.text

        assert (
            await client.get(f"/api/staff/{staff_id}/audit", headers=agent)
        ).status_code == 403
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ── 4. 행위자 문자열 사본 (§2.4) ───────────────────────────────────────────


async def test_actor_text_survives_the_account_being_deleted(client):
    """계정을 지워도 누가 했는지가 남는다. FK만 NULL로 끊기고 문자열 셋이 증거로 남는다."""
    admin = await _make_user("admin5", auth.ROLE_ADMIN)
    headers = await _cookie(admin)
    staff_id = (
        await client.post(
            "/api/staff", json={"name": "기록보존", "rank": "agent"}, headers=headers
        )
    ).json()["id"]

    async with engine.begin() as conn:
        await conn.exec_driver_sql(f"DELETE FROM app_user WHERE id = {admin.id}")

    row = (await _audit_rows(staff_id))[0]
    assert row.actor_user_id is None, "FK가 안 끊겼다(ON DELETE SET NULL)"
    assert row.actor_username == "admin5"
    assert row.actor_display_name == "이름-admin5"
    assert row.actor_role == "admin"


async def test_행위자가_없어도_기록은_남는다(client):
    """행위자가 없는 갈래에서도 기록은 남는다. 네 칸이 NULL인 것 자체가 기록이다.

    ⚠ 예전엔 "플래그 off면 익명이 명부를 만든다"로 이 갈래를 쟀는데, 그 구멍은 F1 수리로
    막혔다(명부·계정 창구는 플래그와 무관하게 판정). 지금 actor가 없을 수 있는 자리는
    `add_audit`을 부르는 쪽이 세션 없이 부르는 갈래라, 함수 단위로 그 계약을 못박는다.
    """
    async with get_session() as session:
        row = Staff(name="주인없는기록", rank=auth.ROLE_AGENT, is_active=True)
        session.add(row)
        await session.flush()
        add_audit(
            session,
            staff_id=row.id,
            action="create",
            actor=None,
            before=None,
            after=audit_snapshot(row),
        )
        await session.commit()
        staff_id = row.id

    saved = (await _audit_rows(staff_id))[0]
    assert (saved.actor_user_id, saved.actor_username, saved.actor_role) == (None, None, None)
    assert saved.actor_display_name is None
    assert saved.after["name"] == "주인없는기록"


# ── 5. 계정 역할 변경도 남는다 ─────────────────────────────────────────────


async def test_account_role_change_and_deactivate_leave_records(client):
    """계정 권한이 바뀌면 감사 행이 남는다.

    `staff_id`는 0 — "명부 행이 아니다" 표시다(`staff_audit.staff_id`가 NOT NULL이고 FK가
    없어서 쓸 수 있는 자리다). 진짜 대상은 본문의 `target`·`user_id`에 있다.
    """
    admin = await _make_user("admin6", auth.ROLE_ADMIN)
    await _make_user("admin7", auth.ROLE_ADMIN)  # 마지막 관리자 잠금을 안 밟게 둘을 둔다
    target = await _make_user("agent2", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    assert (
        await client.patch(
            f"/api/users/{target.id}", json={"role": "leader"}, headers=headers
        )
    ).status_code == 200
    assert (
        await client.patch(
            f"/api/users/{target.id}", json={"is_active": False}, headers=headers
        )
    ).status_code == 200

    rows = await _audit_rows(ACCOUNT_AUDIT_STAFF_ID)
    assert [r.action for r in rows] == ["update", "deactivate"]
    assert rows[0].before["role"] == "agent" and rows[0].after["role"] == "leader"
    assert rows[0].after["target"] == "app_user"
    assert rows[0].after["user_id"] == target.id
    assert rows[1].after["is_active"] is False
    for row in rows:
        assert row.actor_username == "admin6"
        assert "password" not in row.after and "password_hash" not in row.after


async def test_account_records_do_not_leak_into_staff_audit_lists(client):
    """계정 기록이 명부 이력 창구로 새지 않는다 — 0번 명부 행은 실재하지 않아 404다."""
    admin = await _make_user("admin8", auth.ROLE_ADMIN)
    await _make_user("admin9", auth.ROLE_ADMIN)
    target = await _make_user("agent3", auth.ROLE_AGENT)
    headers = await _cookie(admin)

    await client.patch(f"/api/users/{target.id}", json={"role": "leader"}, headers=headers)
    staff_id = (
        await client.post(
            "/api/staff", json={"name": "명부사람", "rank": "agent"}, headers=headers
        )
    ).json()["id"]

    listed = await client.get(f"/api/staff/{staff_id}/audit", headers=headers)
    assert [r["action"] for r in listed.json()] == ["create"]
    assert all(r["staff_id"] == staff_id for r in listed.json())

    assert (await client.get("/api/staff/0/audit", headers=headers)).status_code == 404

    async with get_session() as session:
        left = (await session.execute(select(Staff.id))).scalars().all()
    assert left == [staff_id]
