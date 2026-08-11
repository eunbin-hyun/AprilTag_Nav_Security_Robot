"""명부 조회 이력 — `staff_access_log` 표와 `GET /api/staff/access-log`.

"누가 명부를 봤나"가 아무 데도 안 남던 자리다. 태그 UID 가 요청 줄에 평문으로 쌓이는 것을
막으려고 nginx·uvicorn 액세스 로그를 양쪽 다 껐는데(2026-08-02), 그러면서 조회 기록도 같이
사라졌다. 사용자가 남기는 쪽으로 정했다(2026-08-05, 결정 대기 7번).

## 여기서 재는 것 다섯

1. ⭐ **태그 UID 원문이 기록에 안 들어간다.** 이 파일의 본체다 — 원문을 담으면 액세스
   로그를 끈 것이 헛일이 된다. 앞 네 글자만 남는다.
2. **조회 세 갈래가 각각 남는다** — 목록·단건·태그.
3. ⭐ **실패한 조회는 안 남는다.** 404 갈래를 남기면 존재하지 않는 태그를 넣은 요청이
   그대로 표에 쌓여 열거 시도의 저장소가 된다.
4. **조회 이력 창구 자체는 이력을 안 남긴다.** 남기면 이력을 볼 때마다 이력이 늘어서
   감사 대상이 자기 조회에 파묻힌다.
5. **급이 팀장 이상이다.** 요원한테 열린 명부 창구는 `by-tag` 하나뿐이라는 §3과 같은 잣대다.

헬퍼 모양은 `tests/test_staff_endpoints.py`와 같게 뒀다 — 같은 표를 만지는 시험이 서로
다른 방식으로 계정·명부를 심으면 둘을 견줄 때마다 그 차이부터 걷어내야 한다.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from app import auth
from app.db import get_session
from app.models import Alert, AppUser, GatePassEvent, Staff, StaffAccessLog

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACCESS_LOG = "/api/staff/access-log"
_BASE_T = dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


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


async def _leader() -> dict[str, str]:
    return await _cookie(await _make_user("acclog-leader", "leader"))


async def _agent() -> dict[str, str]:
    return await _cookie(await _make_user("acclog-agent", "agent"))


async def _make_staff(name: str = "홍길동", **values) -> Staff:
    async with get_session() as session:
        row = Staff(name=name, is_active=values.pop("is_active", True), **values)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _seed_alert_for_tag(tag_id: str) -> None:
    """그 태그를 물고 있는 활성 경고 하나. `by-tag`는 이게 없으면 404다(§8 결정 6)."""
    async with get_session() as session:
        event = GatePassEvent(
            event_id=f"acclog-{tag_id}",
            observed_at=_BASE_T,
            received_at=_BASE_T,
            tag_id=tag_id,
        )
        session.add(event)
        await session.commit()
        await session.refresh(event)
        session.add(
            Alert(
                type="untagged_pass",
                severity="warning",
                source_type="gate_pass",
                source_id=event.id,
                ack=False,
            )
        )
        await session.commit()


async def _rows() -> list[StaffAccessLog]:
    async with get_session() as session:
        return list(
            (
                await session.execute(
                    select(StaffAccessLog).order_by(StaffAccessLog.id)
                )
            ).scalars().all()
        )


# ── 1. ⭐ UID 원문이 안 들어간다 ──────────────────────────────────────────


async def test_태그_조회는_UID_앞_네_글자만_남긴다(client):
    """이 파일의 본체. 원문이 남으면 액세스 로그를 끈 것이 헛일이 된다."""
    tag = "AABBCCDDEEFF"
    staff = await _make_staff("태그사람", tag_id=tag, student_no="1500001")
    await _seed_alert_for_tag(tag)
    headers = await _leader()

    res = await client.get(f"/api/staff/by-tag/{tag}", headers=headers)
    assert res.status_code == 200, res.text

    rows = await _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "by_tag"
    assert row.target_staff_id == staff.id
    # ⭐ 원문이 아니라 앞 네 글자 + 말줄임이다.
    assert row.tag_prefix == "AABB…"
    assert tag not in (row.tag_prefix or ""), "UID 원문이 기록에 들어갔다"


# ── 2. 조회 세 갈래가 각각 남는다 ─────────────────────────────────────────


async def test_목록_조회가_남는다(client):
    await _make_staff("목록사람")
    headers = await _leader()

    res = await client.get("/api/staff", headers=headers)
    assert res.status_code == 200

    rows = await _rows()
    assert [r.action for r in rows] == ["list"]
    # 몇 줄을 가져갔나가 남는다 — "명부 전체를 긁었나"를 되짚는 축이다.
    assert rows[0].result_count == len(res.json())
    assert rows[0].target_staff_id is None
    assert rows[0].tag_prefix is None


async def test_배경_적재는_안_남는다(client):
    """⭐ `bg=1`이면 조회 이력을 안 남긴다 (2026-08-05 프론트 17차).

    화면이 로그인 직후 명부를 배경으로 한 번 받아 와서, 팀장·관리자가 **로그인할 때마다**
    `list` 한 줄이 쌓였다. 사용자가 "로그인할 때마다 명부 이력 조회 뜨는 거 고쳤나"라고
    짚은 자리다.

    ⚠ 사람이 명부 탭을 직접 여는 갈래에는 이 표식이 안 붙는다 — 그건 남아야 한다.
    """
    await _make_staff("배경사람")
    headers = await _leader()

    res = await client.get("/api/staff?bg=1", headers=headers)
    assert res.status_code == 200, res.text
    assert len(res.json()) >= 1, "이력만 안 남을 뿐 목록은 그대로 온다"
    assert await _rows() == [], "배경 적재인데 조회 이력이 남았다"

    # 같은 창구를 표식 없이 부르면 그대로 남는다 — 표식이 이력을 통째로 끈 게 아니다.
    res2 = await client.get("/api/staff", headers=headers)
    assert res2.status_code == 200
    assert [r.action for r in await _rows()] == ["list"]


async def test_단건_조회가_남는다(client):
    staff = await _make_staff("단건사람")
    headers = await _leader()

    assert (await client.get(f"/api/staff/{staff.id}", headers=headers)).status_code == 200

    rows = await _rows()
    assert [r.action for r in rows] == ["detail"]
    assert rows[0].target_staff_id == staff.id


async def test_행위자가_기록에_남는다(client):
    """계정이 바뀌거나 지워져도 기록이 그때 그대로 남아야 감사다 — 문자열로도 복사한다."""
    staff = await _make_staff("행위자시험")
    headers = await _leader()

    await client.get(f"/api/staff/{staff.id}", headers=headers)

    row = (await _rows())[0]
    assert row.actor_user_id is not None
    assert row.actor_username == "acclog-leader"
    assert row.actor_role == "leader"


# ── 3. ⭐ 실패한 조회는 안 남는다 ─────────────────────────────────────────


async def test_없는_태그_조회는_안_남긴다(client):
    """404 는 정보가 한 줄도 안 나갔으니 "봤다"가 아니다.

    남기면 존재하지 않는 태그를 넣은 요청이 그대로 표에 쌓여 열거 시도의 저장소가 된다.
    """
    headers = await _leader()
    assert (
        await client.get("/api/staff/by-tag/NOSUCHTAG9999", headers=headers)
    ).status_code == 404
    assert await _rows() == []


async def test_없는_명부_번호_조회는_안_남긴다(client):
    headers = await _leader()
    assert (await client.get("/api/staff/999999", headers=headers)).status_code == 404
    assert await _rows() == []


# ── 4. 조회 이력 창구 ─────────────────────────────────────────────────────


async def test_이력_창구가_최근_것부터_준다(client):
    staff = await _make_staff("이력사람")
    headers = await _leader()

    await client.get("/api/staff", headers=headers)
    await client.get(f"/api/staff/{staff.id}", headers=headers)

    res = await client.get(ACCESS_LOG, headers=headers)
    assert res.status_code == 200, res.text
    assert [r["action"] for r in res.json()] == ["detail", "list"], "최근 것이 먼저다"


async def test_이력_창구는_자기_조회를_안_남긴다(client):
    """남기면 이력을 볼 때마다 이력이 늘어서 감사 대상이 자기 조회에 파묻힌다."""
    await _make_staff("자기조회")
    headers = await _leader()

    await client.get("/api/staff", headers=headers)
    before = len(await _rows())

    await client.get(ACCESS_LOG, headers=headers)
    await client.get(ACCESS_LOG, headers=headers)

    assert len(await _rows()) == before


# ── 5. 급 ────────────────────────────────────────────────────────────────


async def test_요원은_이력을_못_본다(client):
    """요원한테 열린 명부 창구는 `by-tag` 하나뿐이다(§3)."""
    headers = await _agent()
    assert (await client.get(ACCESS_LOG, headers=headers)).status_code == 403


async def test_익명은_이력을_못_본다(client):
    """`require_leader_always` 라 플래그와 무관하게 늘 막힌다."""
    assert (await client.get(ACCESS_LOG)).status_code == 401
