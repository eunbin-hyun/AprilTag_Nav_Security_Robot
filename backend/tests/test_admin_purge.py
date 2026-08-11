"""시연 자료 비우기 창구 — `POST /api/admin/purge-demo-data`.

발표 직전에 화면(시연 도구 안 "이벤트·알림 초기화")이 부르는 창구다. 되돌릴 수 없어서
안전선을 권한과 확인 문구 둘로 세웠다.

## 여기서 재는 것 다섯

1. ⭐ **확인 문구가 없거나 다르면 400.** 이 파일의 본체다 — 버튼 오조작을 막는 마지막
   관문이라, 이게 뚫리면 실수 한 번에 실 자료가 사라진다.
2. ⭐ **관리자만.** 팀장·요원·익명 전부 막힌다. 되돌릴 수 없는 자리라 명부 수정보다 위다.
3. **지우는 범위가 스크립트와 같다.** 두 자리가 갈라지면 "스크립트로 지운 것과 버튼으로
   지운 것이 다른" 상태가 되고 발표 당일에야 드러난다.
4. ⛔ **시연 자산은 안 지운다** — 계정·세션·명부·감사 기록 둘. 계정이 날아가면 발표 때
   로그인할 길이 없다.
5. **시퀀스가 1부터 다시.** 지우기만 하면 번호가 안 내려가서 발표 첫 통과가 8102번으로 뜬다.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select

from app import auth
from app.db import get_session
from app.models import (
    Alert,
    AppUser,
    GatePassEvent,
    Staff,
    StaffAccessLog,
    StaffAudit,
    TaggingEvent,
)
from tools.purge_test_data import NEVER_PURGED, PURGE_ORDER

pytestmark = pytest.mark.asyncio(loop_scope="session")

PURGE = "/api/admin/purge-demo-data"
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


async def _admin() -> dict[str, str]:
    return await _cookie(await _make_user("purge-admin", "admin"))


async def _seed_demo_rows() -> None:
    """지울 대상과 안 지울 대상을 같이 심는다. 둘을 갈라 재야 뜻이 선다."""
    async with get_session() as session:
        session.add_all(
            [
                GatePassEvent(
                    event_id="purge-gp-1", observed_at=_BASE_T, received_at=_BASE_T
                ),
                TaggingEvent(
                    event_id="purge-tg-1",
                    observed_at=_BASE_T,
                    received_at=_BASE_T,
                    tag_id="PURGETAG",
                ),
                Alert(type="untagged_pass", severity="warning", ack=False),
                # 안 지울 대상 둘 — 명부와 조회 이력
                Staff(name="남을사람", is_active=True),
                StaffAccessLog(action="list", result_count=1),
            ]
        )
        await session.commit()


async def _count(model) -> int:
    async with get_session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar()


# ── 1. ⭐ 확인 문구 ───────────────────────────────────────────────────────


async def test_확인_문구가_없으면_400이고_한_행도_안_지운다(client):
    """이 파일의 본체. 이게 뚫리면 실수 한 번에 실 자료가 사라진다."""
    await _seed_demo_rows()
    headers = await _admin()

    res = await client.post(PURGE, json={"confirm": ""}, headers=headers)
    assert res.status_code == 400, res.text
    assert await _count(GatePassEvent) == 1, "400인데 지워졌다"


async def test_확인_문구가_다르면_400이다(client):
    """소문자·비슷한 낱말도 안 받는다 — 실수로 통과하면 관문이 아니다."""
    headers = await _admin()
    for wrong in ("purge", "DELETE", "PURGE ", "지우기"):
        res = await client.post(PURGE, json={"confirm": wrong}, headers=headers)
        assert res.status_code == 400, f"{wrong!r} 가 통과했다"


async def test_본문이_아예_없으면_422다(client):
    headers = await _admin()
    assert (await client.post(PURGE, headers=headers)).status_code == 422


# ── 2. ⭐ 권한 ────────────────────────────────────────────────────────────


async def test_익명은_못_지운다(client):
    """`require_admin_always` 라 플래그와 무관하게 늘 막힌다."""
    assert (await client.post(PURGE, json={"confirm": "PURGE"})).status_code == 401


async def test_팀장도_못_지운다(client):
    """되돌릴 수 없는 자리라 명부 수정보다 위다."""
    headers = await _cookie(await _make_user("purge-leader", "leader"))
    assert (
        await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)
    ).status_code == 403


async def test_요원도_못_지운다(client):
    headers = await _cookie(await _make_user("purge-agent", "agent"))
    assert (
        await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)
    ).status_code == 403


# ── 3·4. 지우는 범위 ─────────────────────────────────────────────────────


async def test_관리자가_지우면_대상만_비고_시연_자산은_남는다(client):
    """⛔ 계정이 날아가면 발표 때 로그인할 길이 없다. 갈라서 잰다."""
    await _seed_demo_rows()
    headers = await _admin()

    res = await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)
    assert res.status_code == 200, res.text
    body = res.json()

    # 지울 대상은 비었다.
    assert await _count(GatePassEvent) == 0
    assert await _count(TaggingEvent) == 0
    assert await _count(Alert) == 0

    # ⛔ 시연 자산은 그대로다.
    assert await _count(Staff) == 1, "명부가 지워졌다"
    assert await _count(StaffAccessLog) == 1, "감사 기록이 지워졌다 — append-only 위반"
    assert await _count(AppUser) >= 1, "계정이 지워졌다 — 발표 때 로그인 못 한다"

    # 응답이 무엇을 얼마나 지웠는지 알려준다.
    assert body["total"] == sum(body["deleted"].values())
    assert body["sequences_reset"] is True


async def test_지운_사실이_감사에_남는다(client):
    """⭐ **표가 비어도 "누가 언제 몇 건을 지웠다"는 남아야 한다** (2026-08-06).

    ⛔ 예전에는 이 창구가 로그만 찍고 감사에 안 남겼다. 그래서 실제로 잃은 것이 있다 —
    사용자가 개발자 화면에서 보고 남겨 둔 **카드 UID 태깅 셋이 사라졌는데, 누가 언제
    지웠는지 되짚을 자리가 한 곳도 없었다.** 시퀀스가 1로 리셋된 것만 흔적이었다.

    ⚠ `staff_audit` 는 `PURGE_ORDER` 밖이라 **다음 비우기에도 이 기록은 안 지워진다.**
    """
    await _seed_demo_rows()
    headers = await _admin()

    before = await _count(StaffAudit)
    res = await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)
    assert res.status_code == 200, res.text

    async with get_session() as session:
        row = (
            await session.execute(
                select(StaffAudit).order_by(StaffAudit.id.desc()).limit(1)
            )
        ).scalar_one()

    assert await _count(StaffAudit) == before + 1, "지웠는데 감사에 한 줄도 안 남았다"
    assert row.action == "purge_demo_data"
    assert row.actor_username, "누가 지웠는지가 안 남았다"
    # 몇 건을 지웠나까지 남는다 — 되짚을 때 그 수가 근거다.
    assert row.after["total"] == res.json()["total"]
    assert row.after["sequences_reset"] is True


async def test_감사_기록은_다음_비우기에도_안_지워진다(client):
    """append-only 계약이 두 번째 비우기에서도 지켜지나."""
    await _seed_demo_rows()
    headers = await _admin()
    await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)
    first = await _count(StaffAudit)

    await _seed_demo_rows()
    await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)

    assert await _count(StaffAudit) == first + 1, (
        "두 번째 비우기가 앞 감사 기록을 지웠다 — append-only 위반"
    )


async def test_지우는_표_목록이_스크립트와_같다(client):
    """두 자리가 갈라지면 "스크립트로 지운 것과 버튼으로 지운 것이 다른" 상태가 된다.

    ⚠ 창구가 PURGE_ORDER 를 그대로 import 하므로 지금은 구조상 같다. 이 시험은 나중에
    창구가 자기 목록을 따로 들게 바뀌면 그 순간 빨개지라고 두는 못이다.
    """
    await _seed_demo_rows()
    headers = await _admin()

    body = (
        await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)
    ).json()

    assert set(body["deleted"]) == set(PURGE_ORDER)
    assert not set(body["deleted"]) & set(NEVER_PURGED), "안 지울 표가 목록에 들어갔다"


# ── 5. 시퀀스 ────────────────────────────────────────────────────────────


async def test_지운_뒤_번호가_1부터_다시_시작한다(client):
    """지우기만 하면 번호가 안 내려가서 발표 첫 통과가 8102번으로 뜬다."""
    await _seed_demo_rows()
    headers = await _admin()
    await client.post(PURGE, json={"confirm": "PURGE"}, headers=headers)

    async with get_session() as session:
        row = GatePassEvent(
            event_id="after-purge", observed_at=_BASE_T, received_at=_BASE_T
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.id == 1, f"번호가 1부터가 아니다 — {row.id}"


# ── 명부 조회 이력 비우기 ───────────────────────────────────────────────────
#
# 2026-08-05 사용자가 시연 도구에 버튼을 두라고 정했다. 리허설이 남긴 조회 자국 22건이
# 발표에서 "누가 명부를 봤나" 표를 못 쓰게 만들기 때문이다.
#
# ⭐ **위험을 감사로 막는다.** 프론트가 짚은 위험이 "관리자가 자기 조회 흔적을 지울 수 있어
# 그 표가 감사 구실을 못 한다"였는데, 지운 사실 자체를 `staff_audit`(append-only)에 남겨
# **표는 비어도 "누가 언제 몇 건을 지웠다"가 남게** 했다.


async def _make_access_rows(n: int) -> None:
    async with get_session() as session:
        for i in range(n):
            session.add(
                StaffAccessLog(
                    action="list",
                    actor_username=f"tester{i}",
                    result_count=0,
                )
            )
        await session.commit()


async def test_조회_이력을_비우고_지운_수를_돌려준다(client):
    await _make_access_rows(3)

    res = await client.post(
        "/api/admin/purge-access-log", json={"confirm": "PURGE"}, headers=await _admin()
    )

    assert res.status_code == 200
    assert res.json()["deleted"] == 3
    assert await _count(StaffAccessLog) == 0


async def test_지운_사실이_감사에_남는다(client):
    """⭐ 이 케이스가 이 창구를 감사 가능하게 만드는 자리다. 빠지면 흔적이 통째로 사라진다."""
    from app.models import StaffAudit

    await _make_access_rows(2)

    res = await client.post(
        "/api/admin/purge-access-log", json={"confirm": "PURGE"}, headers=await _admin()
    )

    audit_id = res.json()["audit_id"]
    async with get_session() as session:
        row = (
            await session.execute(select(StaffAudit).where(StaffAudit.id == audit_id))
        ).scalars().one()
    assert row.action == "purge_access_log"
    assert row.after == {"deleted": 2}
    assert row.actor_username, "누가 지웠는지가 안 남았다"


async def test_확인_문구가_없으면_이력을_안_지운다(client):
    await _make_access_rows(2)

    res = await client.post(
        "/api/admin/purge-access-log", json={"confirm": "nope"}, headers=await _admin()
    )

    assert res.status_code == 400
    assert await _count(StaffAccessLog) == 2, "확인 문구가 틀렸는데 지웠다"


@pytest.mark.parametrize("role", [auth.ROLE_AGENT, auth.ROLE_LEADER])
async def test_관리자가_아니면_이력을_못_지운다(client, role):
    await _make_access_rows(1)
    user = await _make_user(f"who_{role}", role)

    res = await client.post(
        "/api/admin/purge-access-log",
        json={"confirm": "PURGE"},
        headers=await _cookie(user),
    )

    assert res.status_code == 403
    assert await _count(StaffAccessLog) == 1


async def test_익명은_이력을_못_지운다(client):
    await _make_access_rows(1)

    res = await client.post("/api/admin/purge-access-log", json={"confirm": "PURGE"})

    assert res.status_code == 401
    assert await _count(StaffAccessLog) == 1
