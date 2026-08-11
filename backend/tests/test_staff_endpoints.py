"""N14 명부 창구 — 역할 게이트 · 쓰기 방어 · by-tag 열거 방어.

계약은 `docs/로그인_설계초안_2026-08-01.md`(사용자 확정본) §3·§4·§8이다. 감사 기록 갈래는
`test_staff_audit.py`가 따로 본다.

## 여기서 재는 것 다섯

1. **역할별 401 / 403 / 200** — 목록·단건·쓰기는 팀장급, by-tag만 요원급이다.
2. **등록 직급 상한** — 팀장이 자기 급 이상으로 새 행을 만들 수 없다(우회 승격 차단).
3. **아랫급만 수정·해제** — 동급·윗급·본인 항목이 막힌다. 조회는 안 막힌다.
4. **by-tag는 활성 경고에 붙은 태그만** — 그 밖은 전부 같은 404다(§8 결정 6).
5. **플래그 off 구간의 열린 자리** — 지금 거동을 눈에 보이게 못박는다(라우터 머리 ⚠ 참고).

## 시험 위생

`staff`·`staff_audit`·`app_user`·`auth_session` 비우기와 인메모리 잠금 초기화는 conftest
`_clean` 하나가 한다. 이 파일이 자기 앞에서 또 돌리면 그 훅이 빠져도 이 파일만은 통과해
결함이 안 보인다 — 2026-08-02 교차 검토로 걷어냈다.
"""
import datetime as dt

import pytest
from sqlalchemy import select

from app import auth
from app.config import get_settings
from app.db import get_session
from app.models import Alert, AppUser, GatePassEvent, Staff, StaffAudit, TaggingEvent
from app.schemas import RoleName

pytestmark = pytest.mark.asyncio(loop_scope="session")

_BASE_T = dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def flag_on(monkeypatch):
    """`AUTH_REQUIRE_LOGIN=true` 구간. 설정이 lru_cache라 캐시를 앞뒤로 비운다."""
    monkeypatch.setenv("AUTH_REQUIRE_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
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
    """그 계정의 세션 쿠키 헤더. 로그인 창구를 안 거치고 바로 세션을 판다.

    토큰은 `secrets.token_urlsafe`라 늘 ASCII다 — 헤더 값은 ascii로 인코드되므로 한글을
    넣으면 시험이 서버가 아니라 클라이언트에서 터진다(A가 실측으로 짚은 함정).
    """
    async with get_session() as session:
        token = await auth.create_session(session, user)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


async def _make_staff(name: str = "홍길동", **values) -> Staff:
    async with get_session() as session:
        row = Staff(name=name, is_active=values.pop("is_active", True), **values)
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


async def _seed_alert_for_tag(tag_id: str, *, ack: bool = False, kind: str = "gate_pass") -> int:
    """그 태그를 물고 있는 경고 하나를 심는다. `ack=True`면 활성이 아니다.

    태그와 경고를 잇는 길은 역추적 규칙 하나다 — 경고의 `(source_type, source_id)`가
    이벤트 한 줄의 `(kind, id)`고 그 이벤트가 `tag_id`를 들고 있다(S15P11C207-192).
    """
    async with get_session() as session:
        if kind == "gate_pass":
            event = GatePassEvent(
                event_id=f"gp-{tag_id}-{ack}",
                observed_at=_BASE_T,
                received_at=_BASE_T,
                tag_id=tag_id,
            )
        else:
            event = TaggingEvent(
                event_id=f"tg-{tag_id}-{ack}",
                observed_at=_BASE_T,
                received_at=_BASE_T,
                tag_id=tag_id,
            )
        session.add(event)
        await session.flush()
        alert = Alert(
            type="untagged", source_type=kind, source_id=event.id, ack=ack
        )
        session.add(alert)
        await session.commit()
        await session.refresh(alert)
        return alert.id


# ── 0. 낱말 대조 — 스키마 enum과 판정 사다리가 어긋나면 안 된다 ─────────────


async def test_role_words_match_the_auth_ladder():
    """전선 enum(`RoleName`)과 판정 사다리(`auth.RANK`)가 같은 낱말 셋이어야 한다.

    두 자리에 나눠 적힌 이유는 순환 import다(schemas ← config ← auth). 한쪽만 늘어나면
    422와 403 판정이 갈라지므로 여기서 붙들어 둔다.
    """
    assert {r.value for r in RoleName} == set(auth.RANK)


# ── 1. 역할 게이트 401 / 403 / 200 (§3 매트릭스) ───────────────────────────


async def test_staff_read_endpoints_need_leader(client, flag_on):
    """목록·단건·감사 읽기는 팀장급이다. 요원은 403이고 세션이 없으면 401이다."""
    row = await _make_staff(rank=auth.ROLE_AGENT)
    agent = await _cookie(await _make_user("agent1", auth.ROLE_AGENT))
    leader = await _cookie(await _make_user("leader1", auth.ROLE_LEADER))

    for path in ("/api/staff", f"/api/staff/{row.id}", f"/api/staff/{row.id}/audit"):
        assert (await client.get(path)).status_code == 401, path
        assert (await client.get(path, headers=agent)).status_code == 403, path
        assert (await client.get(path, headers=leader)).status_code == 200, path


async def test_staff_write_endpoints_need_leader(client, flag_on):
    """등록·수정·해제도 팀장급부터다. 요원은 자기 아래 급이 없어 사다리만으로 닫힌다."""
    row = await _make_staff(rank=auth.ROLE_AGENT)
    agent = await _cookie(await _make_user("agent2", auth.ROLE_AGENT))

    assert (
        await client.post("/api/staff", json={"name": "새사람", "rank": "agent"})
    ).status_code == 401
    assert (
        await client.post(
            "/api/staff", json={"name": "새사람", "rank": "agent"}, headers=agent
        )
    ).status_code == 403
    assert (
        await client.patch(f"/api/staff/{row.id}", json={"name": "고침"}, headers=agent)
    ).status_code == 403
    assert (
        await client.post(f"/api/staff/{row.id}/deactivate", headers=agent)
    ).status_code == 403


async def test_admin_passes_every_staff_endpoint(client, flag_on):
    """관리자는 아랫급 창구를 다 쓴다 — 요원 창구에서 막히면 안 된다.

    ⚠ 만드는 직급이 `leader`다. 총괄 등급이 생긴 뒤로 **관리자는 관리자 급을 못 만든다**
    (2026-08-05 owner 도입 — 등록 직급 상한도 같은 사다리를 탄다).
    """
    admin = await _cookie(await _make_user("admin1", auth.ROLE_ADMIN))
    created = await client.post(
        "/api/staff", json={"name": "관리자가 만든 사람", "rank": "leader"}, headers=admin
    )
    assert created.status_code == 201
    staff_id = created.json()["id"]
    assert (await client.get("/api/staff", headers=admin)).status_code == 200
    assert (
        await client.patch(f"/api/staff/{staff_id}", json={"name": "고침"}, headers=admin)
    ).status_code == 200
    assert (
        await client.post(f"/api/staff/{staff_id}/deactivate", headers=admin)
    ).status_code == 200


# ── 2. 등록 직급 상한 (§4 · 범위 정본 §5-1) ────────────────────────────────


async def test_create_rank_ceiling_blocks_self_and_upper(client, flag_on):
    """팀장은 **자기보다 아래** 직급으로만 등록한다.

    수정·해제만 막으면 "관리자 직급으로 신규 등록"이라는 우회 승격 길이 남는다.
    """
    leader = await _cookie(await _make_user("leader2", auth.ROLE_LEADER))

    ok = await client.post(
        "/api/staff", json={"name": "요원행", "rank": "agent"}, headers=leader
    )
    assert ok.status_code == 201
    assert ok.json()["rank"] == "agent"

    for rank in ("leader", "admin"):
        blocked = await client.post(
            "/api/staff", json={"name": "위로", "rank": rank}, headers=leader
        )
        assert blocked.status_code == 403, rank
        assert "아랫급" in blocked.json()["detail"]

    # 직급 없는 행도 팀장은 못 만든다 — 모르는 급은 닫는 쪽이라야 상한이 뜻을 갖는다.
    assert (
        await client.post("/api/staff", json={"name": "무급"}, headers=leader)
    ).status_code == 403

    async with get_session() as session:
        names = (await session.execute(select(Staff.name))).scalars().all()
    assert names == ["요원행"], f"막혔어야 할 등록이 남았다: {names}"


async def test_unknown_rank_word_is_422_not_403(client, flag_on):
    """사전 밖 직급은 스키마가 422로 막는다 — 판정 층까지 안 간다(§4 층 가름)."""
    leader = await _cookie(await _make_user("leader3", auth.ROLE_LEADER))
    r = await client.post(
        "/api/staff", json={"name": "이상한급", "rank": "superadmin"}, headers=leader
    )
    assert r.status_code == 422


# ── 3. 아랫급만 수정·해제 (범위 정본 §2) ───────────────────────────────────


async def test_patch_cannot_promote_a_row_above_the_actor(client, flag_on):
    """요원 행을 팀장으로 올리는 요청이 막힌다. 옛 급만 보면 통과하는 자리다."""
    leader = await _cookie(await _make_user("leader4", auth.ROLE_LEADER))
    row = await _make_staff("요원", rank=auth.ROLE_AGENT)

    blocked = await client.patch(
        f"/api/staff/{row.id}", json={"rank": "leader"}, headers=leader
    )
    assert blocked.status_code == 403

    async with get_session() as session:
        rank = (
            await session.execute(select(Staff.rank).where(Staff.id == row.id))
        ).scalar_one()
    assert rank == auth.ROLE_AGENT, "막힌 요청이 직급을 바꿨다"


async def test_patch_and_deactivate_block_same_and_upper_rank(client, flag_on):
    """동급·윗급 행은 못 고치고 못 내린다. 조회는 그래도 된다(§3)."""
    leader = await _cookie(await _make_user("leader5", auth.ROLE_LEADER))
    peer = await _make_staff("동급", rank=auth.ROLE_LEADER)
    upper = await _make_staff("윗급", rank=auth.ROLE_ADMIN)

    for row in (peer, upper):
        assert (
            await client.patch(
                f"/api/staff/{row.id}", json={"name": "고침"}, headers=leader
            )
        ).status_code == 403
        assert (
            await client.post(f"/api/staff/{row.id}/deactivate", headers=leader)
        ).status_code == 403
        assert (
            await client.get(f"/api/staff/{row.id}", headers=leader)
        ).status_code == 200, "조회까지 막으면 누가 있는지도 모른 채 등록하게 된다"


async def test_own_row_is_blocked_for_leader_and_open_for_admin(client, flag_on):
    """본인 항목은 급이 아래여도 막힌다. 그 처리는 관리자 몫이다(확정 D).

    판정 열쇠는 `app_user_id` 링크 하나다 — 이름이 같아도 링크가 없으면 남의 행이다.
    """
    leader_user = await _make_user("leader6", auth.ROLE_LEADER)
    admin_user = await _make_user("admin2", auth.ROLE_ADMIN)
    mine = await _make_staff("이름-leader6", rank=auth.ROLE_AGENT, app_user_id=leader_user.id)
    same_name = await _make_staff("이름-leader6", rank=auth.ROLE_AGENT)

    leader = await _cookie(leader_user)
    blocked = await client.patch(
        f"/api/staff/{mine.id}", json={"name": "내가 고침"}, headers=leader
    )
    assert blocked.status_code == 403
    assert "본인" in blocked.json()["detail"]

    # 동명이인은 남의 행이다(이름 문자열 비교를 안 쓰는 근거).
    assert (
        await client.patch(
            f"/api/staff/{same_name.id}", json={"name": "남의 행"}, headers=leader
        )
    ).status_code == 200

    # 관리자는 남의 본인 항목도 처리한다(§2.5 관리자 무조건 통과).
    assert (
        await client.patch(
            f"/api/staff/{mine.id}", json={"name": "관리자가 고침"},
            headers=await _cookie(admin_user),
        )
    ).status_code == 200



async def test_admin_cannot_touch_admin_rows(client, flag_on):
    """⛔ 관리자는 관리자 급 행을 못 고친다 — 남의 것도 자기 것도 (2026-08-05 owner 도입).

    총괄 등급이 생기기 전에는 관리자가 사다리를 통째로 비켜갔다. 지금은 "자기보다
    아랫급만"이라는 규칙 하나가 본인 항목까지 같이 막는다.
    """
    me = await _make_user("adminO1", auth.ROLE_ADMIN)
    other = await _make_user("adminO2", auth.ROLE_ADMIN)
    mine = await _make_staff("나-adminO1", rank=auth.ROLE_ADMIN, app_user_id=me.id)
    theirs = await _make_staff("남-adminO2", rank=auth.ROLE_ADMIN, app_user_id=other.id)
    cookie = await _cookie(me)

    own = await client.patch(f"/api/staff/{mine.id}", json={"name": "내가 고침"}, headers=cookie)
    assert own.status_code == 403, own.text
    assert "본인" in own.json()["detail"]

    peer = await client.patch(f"/api/staff/{theirs.id}", json={"name": "남 고침"}, headers=cookie)
    assert peer.status_code == 403, peer.text
    assert "아랫급" in peer.json()["detail"]

    for path in (f"/api/staff/{mine.id}/deactivate", f"/api/staff/{mine.id}/reactivate"):
        assert (await client.post(path, headers=cookie)).status_code == 403, path
    assert (await client.delete(f"/api/staff/{mine.id}", headers=cookie)).status_code == 403

    # ⭐ 아랫급은 그대로 통과한다 — 관리자가 아무것도 못 하게 된 것이 아니다.
    below = await _make_staff("팀장행", rank=auth.ROLE_LEADER)
    assert (
        await client.patch(f"/api/staff/{below.id}", json={"name": "아랫급 고침"}, headers=cookie)
    ).status_code == 200


async def test_owner_may_edit_own_row_but_not_deactivate_or_delete(client, flag_on):
    """⭐ 총괄은 전부 — 본인 행 고치기도. **자기 계정 정지·삭제만** 막힌다."""
    me = await _make_user("ownerA", auth.ROLE_OWNER)
    mine = await _make_staff("총괄-나", rank=auth.ROLE_OWNER, app_user_id=me.id)
    admin_row = await _make_staff("관리자행", rank=auth.ROLE_ADMIN)
    cookie = await _cookie(me)

    # 본인 행 고치기 — 열린다.
    r = await client.patch(f"/api/staff/{mine.id}", json={"name": "총괄이 고침"}, headers=cookie)
    assert r.status_code == 200, r.text

    # 관리자 급 남의 행도 고친다(이 예외가 없으면 admin 행이 영구 잠금이다).
    assert (
        await client.patch(f"/api/staff/{admin_row.id}", json={"name": "총괄이 고침2"}, headers=cookie)
    ).status_code == 200

    # ⛔ 자기 계정 정지·삭제만 막힌다.
    stop = await client.post(f"/api/staff/{mine.id}/deactivate", headers=cookie)
    assert stop.status_code == 403, stop.text
    assert "총괄" in stop.json()["detail"]
    assert (await client.delete(f"/api/staff/{mine.id}", headers=cookie)).status_code == 403

    # 남의 행은 내려도 된다.
    assert (
        await client.post(f"/api/staff/{admin_row.id}/deactivate", headers=cookie)
    ).status_code == 200


# ── 4. 수정·해제 거동 ──────────────────────────────────────────────────────


async def test_patch_only_touches_sent_fields(client, flag_on):
    """보낸 칸만 바뀐다. 이름만 고치려던 요청이 태그·학번을 지우면 안 된다."""
    leader = await _cookie(await _make_user("leader7", auth.ROLE_LEADER))
    row = await _make_staff(
        "원래이름", rank=auth.ROLE_AGENT, tag_id="TAG-A", student_no="0451"
    )

    r = await client.patch(f"/api/staff/{row.id}", json={"name": "새이름"}, headers=leader)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "새이름"
    assert body["tag_id"] == "TAG-A"
    assert body["student_no"] == "0451"

    # null로 **보낸** 칸은 지워진다.
    cleared = await client.patch(
        f"/api/staff/{row.id}", json={"student_no": None}, headers=leader
    )
    assert cleared.json()["student_no"] is None
    assert cleared.json()["tag_id"] == "TAG-A"


async def test_empty_patch_body_is_422(client, flag_on):
    leader = await _cookie(await _make_user("leader8", auth.ROLE_LEADER))
    row = await _make_staff(rank=auth.ROLE_AGENT)
    assert (
        await client.patch(f"/api/staff/{row.id}", json={}, headers=leader)
    ).status_code == 422


async def test_deactivate_lowers_the_flag_and_is_idempotent(client, flag_on):
    """해제는 행을 **안 지우고** `is_active`를 내린다. 다시 눌러도 200이다."""
    leader = await _cookie(await _make_user("leader9", auth.ROLE_LEADER))
    row = await _make_staff(rank=auth.ROLE_AGENT)

    first = await client.post(f"/api/staff/{row.id}/deactivate", headers=leader)
    assert first.status_code == 200
    assert first.json()["is_active"] is False

    again = await client.post(f"/api/staff/{row.id}/deactivate", headers=leader)
    assert again.status_code == 200

    async with get_session() as session:
        left = (await session.execute(select(Staff.id))).scalars().all()
    assert left == [row.id], "해제가 행을 지웠다 — alert.staff_id가 끊긴다"


async def test_duplicate_tag_is_409_on_create_and_patch(client, flag_on):
    leader = await _cookie(await _make_user("leader10", auth.ROLE_LEADER))
    await _make_staff("먼저", rank=auth.ROLE_AGENT, tag_id="TAG-DUP")
    other = await _make_staff("나중", rank=auth.ROLE_AGENT, tag_id="TAG-OTHER")

    dup = await client.post(
        "/api/staff",
        json={"name": "겹침", "rank": "agent", "tag_id": "TAG-DUP"},
        headers=leader,
    )
    assert dup.status_code == 409

    dup2 = await client.patch(
        f"/api/staff/{other.id}", json={"tag_id": "TAG-DUP"}, headers=leader
    )
    assert dup2.status_code == 409

    # 자기 태그를 자기한테 다시 넣는 건 겹침이 아니다.
    assert (
        await client.patch(
            f"/api/staff/{other.id}", json={"tag_id": "TAG-OTHER"}, headers=leader
        )
    ).status_code == 200


async def test_unknown_account_link_is_422(client, flag_on):
    """없는 계정 번호를 링크로 넣으면 422다. FK에만 맡기면 500으로 나간다."""
    leader = await _cookie(await _make_user("leader11", auth.ROLE_LEADER))
    r = await client.post(
        "/api/staff",
        json={"name": "링크불량", "rank": "agent", "app_user_id": 99999},
        headers=leader,
    )
    assert r.status_code == 422


async def test_missing_staff_id_is_404(client, flag_on):
    leader = await _cookie(await _make_user("leader12", auth.ROLE_LEADER))
    assert (await client.get("/api/staff/424242", headers=leader)).status_code == 404
    assert (
        await client.get("/api/staff/424242/audit", headers=leader)
    ).status_code == 404
    assert (
        await client.post("/api/staff/424242/deactivate", headers=leader)
    ).status_code == 404


# ── 5. by-tag — 요원급 + 활성 경고 제한 (§3 · §8 결정 6) ────────────────────


async def test_by_tag_is_open_to_agents_while_the_list_is_not(client, flag_on):
    """요원은 by-tag만 쓴다. 목록 열람이 아니라 이벤트 문맥 조회라서다(범위 정본 §3)."""
    agent = await _cookie(await _make_user("agent3", auth.ROLE_AGENT))
    await _make_staff("김요원", rank=auth.ROLE_AGENT, tag_id="TAG-1", student_no="0451")
    await _seed_alert_for_tag("TAG-1")

    ok = await client.get("/api/staff/by-tag/TAG-1", headers=agent)
    assert ok.status_code == 200
    assert ok.json() == {"tag_id": "TAG-1", "name": "김요원", "student_no": "0451"}
    # 직급·계정 링크·활성 여부는 안 나간다 — 그건 명부 권한자가 보는 값이다.
    assert "rank" not in ok.text and "app_user_id" not in ok.text

    assert (await client.get("/api/staff", headers=agent)).status_code == 403
    assert (await client.get("/api/staff/by-tag/TAG-1")).status_code == 401


async def test_by_tag_needs_an_active_alert(client, flag_on):
    """활성 경고가 없는 태그는 안 열린다. 없으면 요원이 명부를 통째로 긁을 수 있다."""
    agent = await _cookie(await _make_user("agent4", auth.ROLE_AGENT))
    await _make_staff("이요원", rank=auth.ROLE_AGENT, tag_id="TAG-2", student_no="0452")

    assert (await client.get("/api/staff/by-tag/TAG-2", headers=agent)).status_code == 404

    alert_id = await _seed_alert_for_tag("TAG-2")
    assert (await client.get("/api/staff/by-tag/TAG-2", headers=agent)).status_code == 200

    # 관제가 확인을 누르면(ack) 그 태그로는 더 못 캔다.
    async with get_session() as session:
        alert = await session.get(Alert, alert_id)
        alert.ack = True
        await session.commit()
    assert (await client.get("/api/staff/by-tag/TAG-2", headers=agent)).status_code == 404


async def test_by_tag_works_through_tagging_events_too(client, flag_on):
    """태깅 이벤트가 낸 경고도 같은 길로 붙는다(가지가 통과 하나뿐이면 안 된다)."""
    agent = await _cookie(await _make_user("agent5", auth.ROLE_AGENT))
    await _make_staff("박요원", rank=auth.ROLE_AGENT, tag_id="TAG-3")
    await _seed_alert_for_tag("TAG-3", kind="tagging")
    assert (await client.get("/api/staff/by-tag/TAG-3", headers=agent)).status_code == 200


async def test_by_tag_hides_every_failure_behind_the_same_404(client, flag_on):
    """실패 갈래 셋이 밖에서 똑같이 보인다 — 다르면 그 차이가 곧 열거 신호다."""
    agent = await _cookie(await _make_user("agent6", auth.ROLE_AGENT))
    # ① 명부에 있는데 활성 경고가 없는 태그
    await _make_staff("가려진사람", rank=auth.ROLE_AGENT, tag_id="TAG-HIDDEN")
    hidden = await client.get("/api/staff/by-tag/TAG-HIDDEN", headers=agent)
    # ② 활성 경고는 있는데 명부에 없는 태그
    await _seed_alert_for_tag("TAG-NOBODY")
    nobody = await client.get("/api/staff/by-tag/TAG-NOBODY", headers=agent)
    # ③ 아무 데도 없는 태그
    nothing = await client.get("/api/staff/by-tag/TAG-NOTHING", headers=agent)

    codes = {hidden.status_code, nobody.status_code, nothing.status_code}
    bodies = {hidden.json()["detail"], nobody.json()["detail"], nothing.json()["detail"]}
    assert codes == {404}, f"실패 갈래가 코드로 갈렸다: {codes}"
    assert len(bodies) == 1, f"실패 갈래가 문장으로 갈렸다: {bodies}"


async def _pipeline_untagged_alert(client, auth_headers, *, gate_no: int = 5) -> tuple[int, int]:
    """**실파이프라인**으로 미태깅 경고 하나를 만든다. (alert_id, gate_pass_event.id)를 준다.

    위 `_seed_alert_for_tag`는 통과 행에 `tag_id`를 손으로 적고 그 위에 미태깅 경고를
    얹는다. 그 조합은 실서버에서 **만들어질 수 없다** — 미태깅은 태운 크레딧이 없다는 뜻이라
    `credit/state_machine`이 `tag_id`를 NULL로 남긴다(그 파일의 -241 주석). 손으로 심은
    자료로만 초록이 나면 창구가 실제로 도는지는 아무도 안 잰 셈이라, 아래 두 케이스는 인입
    창구로 진짜 판정을 태워서 나온 행만 쓴다.

    크레딧이 하나도 없는 게이트로 A→B 통과를 보낸다. 빔 칸을 채우는 이유는 방향 정합 검사가
    빈 칸을 "어긋난 주장"으로 보고 판정을 보류로 접기 때문이다(그러면 미태깅 경고가 안 난다).
    """
    r = await client.post(
        "/api/gate-pass-events",
        json={
            "event_id": f"gp-pipeline-{gate_no}",
            "device_id": "raspberry01",
            "gate_no": gate_no,
            "direction": "A_TO_B",
            "status": "complete",
            "beam_a_ts": _BASE_T.isoformat(),
            "beam_b_ts": (_BASE_T + dt.timedelta(seconds=0.2)).isoformat(),
            "observed_at": _BASE_T.isoformat(),
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "untagged", r.text

    async with get_session() as session:
        alert = (
            await session.execute(select(Alert).where(Alert.source_type == "gate_pass"))
        ).scalars().one()
        return alert.id, alert.source_id


async def test_실파이프라인이_만든_경고에는_붙일_태그가_없다(client, auth_headers, flag_on):
    """⚠ **N14 요원 창구가 지금 구조적으로 죽어 있다**(검토 F22 — 원인은 파이프라인 쪽).

    by-tag가 답하려면 활성 경고가 걸린 이벤트 행에 `tag_id`가 있어야 하는데,
      ① 통과 계열에서 경고가 나는 판정은 미태깅뿐이고, 미태깅은 태운 크레딧이 없어
         `gate_pass_event.tag_id`가 NULL이다.
      ② 태깅 계열 경고를 만드는 생산자는 이 저장소에 **한 자리도 없다**
         (`source_type="tagging"`으로 Alert를 넣는 코드가 0건이다).
    그래서 실서버에서는 이 창구가 늘 404다. 이 케이스는 그 사실을 실물로 못박아, 손으로
    심은 자료가 내는 초록에 가려지지 않게 한다.

    ⭐ 파이프라인이 통과 행에 UID를 채우게 되면 이 케이스가 빨개진다. 그때는 이 시험을
    지우는 게 아니라 아래 `test_태그가_채워지면_by_tag가_실제로_맞물린다`와 함께 "무엇을
    채웠나"에 맞춰 갱신해라 — 창구 계약이 바뀐 자리지 시험이 틀린 게 아니다.
    """
    agent = await _cookie(await _make_user("agentpipe1", auth.ROLE_AGENT))
    await _make_staff("파이프사람", rank=auth.ROLE_AGENT, tag_id="TAG-PIPE", student_no="0460")

    _, pass_id = await _pipeline_untagged_alert(client, auth_headers)

    async with get_session() as session:
        row = await session.get(GatePassEvent, pass_id)
    assert row.tag_id is None, "미태깅 통과에 UID가 붙었다 — 파이프라인 계약이 바뀌었다"

    # 명부에 멀쩡히 있는 태그인데도 붙을 자리가 없어 404다.
    r = await client.get("/api/staff/by-tag/TAG-PIPE", headers=agent)
    assert r.status_code == 404, r.text

    # 태깅 계열 경고 생산자가 없다는 것도 같이 못박는다 — 그쪽 가지도 실물에서는 안 돈다.
    async with get_session() as session:
        kinds = set(
            (await session.execute(select(Alert.source_type))).scalars().all()
        )
    assert "tagging" not in kinds, "태깅 경고 생산자가 생겼다 — 위 ② 전제가 바뀌었다"


async def test_태그가_채워지면_by_tag가_실제로_맞물린다(client, auth_headers, flag_on):
    """조회 쪽 역추적(경고 → 이벤트 → 태그)이 **파이프라인이 만든 행 위에서** 붙나.

    경고 행도 통과 행도 실판정이 만든 것 그대로 쓰고, 파이프라인 팀이 채워 주기로 한 칸
    (`gate_pass_event.tag_id`) **하나만** 채운다. 그 한 칸이 차는 순간 창구가 답한다면
    staff.py 쪽은 고칠 게 없다는 뜻이고, 남은 건 파이프라인 몫이다.

    ack까지 같이 본다 — 관제가 확인을 누르면 다시 닫히는 게 열거 방어 계약이라(§8 결정 6),
    "채우니 열렸다"만 재고 끝내면 방어가 살아 있는지는 안 잰 셈이다.
    """
    agent = await _cookie(await _make_user("agentpipe2", auth.ROLE_AGENT))
    await _make_staff("파이프사람2", rank=auth.ROLE_AGENT, tag_id="TAG-PIPE2", student_no="0461")

    alert_id, pass_id = await _pipeline_untagged_alert(client, auth_headers, gate_no=6)

    async with get_session() as session:
        row = await session.get(GatePassEvent, pass_id)
        row.tag_id = "TAG-PIPE2"
        await session.commit()

    ok = await client.get("/api/staff/by-tag/TAG-PIPE2", headers=agent)
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"tag_id": "TAG-PIPE2", "name": "파이프사람2", "student_no": "0461"}

    async with get_session() as session:
        alert = await session.get(Alert, alert_id)
        alert.ack = True
        await session.commit()
    assert (
        await client.get("/api/staff/by-tag/TAG-PIPE2", headers=agent)
    ).status_code == 404


async def test_by_tag_still_answers_for_deactivated_rows(client, flag_on):
    """해제된 행도 돌려준다 — 그 사람 태그로 경고가 났을 때야말로 이름이 필요하다."""
    agent = await _cookie(await _make_user("agent7", auth.ROLE_AGENT))
    await _make_staff(
        "떠난사람", rank=auth.ROLE_AGENT, tag_id="TAG-GONE", is_active=False
    )
    await _seed_alert_for_tag("TAG-GONE")
    r = await client.get("/api/staff/by-tag/TAG-GONE", headers=agent)
    assert r.status_code == 200
    assert r.json()["name"] == "떠난사람"


# ── 6. 플래그 off 구간 — 지금 거동을 눈에 보이게 못박는다 ───────────────────


async def test_플래그가_꺼져도_명부_창구는_익명을_막는다(client):
    """⭐ 명부 창구는 **플래그와 무관하게** 늘 판정한다(검증 F1 수리).

    제1 불변은 "지금 있는 창구의 거동을 안 바꾼다"는 약속이라, 로그인과 함께 새로 생긴
    창구에는 걸리지 않는다. 그대로 열어 두면 롤아웃 1~4단계 내내 익명이 명부를 읽고
    관리자 직급 행까지 만들 수 있었다 — 그래서 `require_leader_always`를 쓴다.

    계정 심기는 창구가 아니라 `tools/seed_users.py`가 하는 자리다(설계 §6.2 2단계).
    """
    assert get_settings().auth_require_login is False, "기본값이 false가 아니다"

    listed = await client.get("/api/staff")
    assert listed.status_code == 401, listed.text

    created = await client.post("/api/staff", json={"name": "익명등록", "rank": "admin"})
    assert created.status_code == 401, created.text

    async with get_session() as session:
        rows = (
            (await session.execute(select(Staff.id).where(Staff.name == "익명등록")))
            .scalars()
            .all()
        )
    assert rows == [], "익명 요청이 명부 행을 남겼다"


# ── 7. 백지 검토 3차 수리 ──────────────────────────────────────────────────


async def test_자기_계정_링크는_등록도_수정도_막힌다(client, flag_on):
    """⭐ 새로 거는 `app_user_id`가 자기 계정이면 403이다(3차 X32 수리).

    `assert_can_write`의 본인 항목 차단은 **대상 행의 지금 값**만 본다. 그래서 등록은 그
    검사를 아예 안 지나고, 수정도 "바꾸기 전 링크"만 보고 통과한다 — 팀장이 자기 계정을
    링크한 행을 새로 만들거나 남의 행을 자기 링크로 갈아 끼우면, 그 뒤로 그 행은 본인
    항목이라 자기가 다시 못 고치는 게 아니라 **자기 마음대로 쥔 행**이 된다.
    `staff.app_user_id`에 유니크가 없어서 몇 장이든 만들 수 있다는 게 이 갈래의 폭이다.

    관리자는 그대로 통과한다 — 막으면 admin 급 행의 링크를 아무도 못 거는 잠금이 된다.
    """
    leader_user = await _make_user("x32leader", auth.ROLE_LEADER)
    admin_user = await _make_user("x32admin", auth.ROLE_ADMIN)
    leader = await _cookie(leader_user)

    created = await client.post(
        "/api/staff",
        json={"name": "내 카드", "rank": "agent", "app_user_id": leader_user.id},
        headers=leader,
    )
    assert created.status_code == 403, created.text
    assert "본인 계정" in created.json()["detail"]

    async with get_session() as session:
        rows = (
            (await session.execute(select(Staff.id).where(Staff.name == "내 카드")))
            .scalars()
            .all()
        )
    assert rows == [], "403인데 행이 남았다"

    # 남의 행을 자기 링크로 갈아 끼우는 길도 같은 자리에서 막힌다.
    someone = await _make_staff("남의 행", rank=auth.ROLE_AGENT)
    patched = await client.patch(
        f"/api/staff/{someone.id}",
        json={"app_user_id": leader_user.id},
        headers=leader,
    )
    assert patched.status_code == 403, patched.text

    # 딴 사람 계정을 거는 건 그대로 된다(막은 건 "자기 계정"뿐이다).
    other = await client.patch(
        f"/api/staff/{someone.id}",
        json={"app_user_id": admin_user.id},
        headers=leader,
    )
    assert other.status_code == 200, other.text

    # ⭐ **총괄만** 자기 계정을 건다. 관리자는 이제 막힌다(2026-08-05 owner 도입 —
    #    `assert_can_write`의 본인 항목 규칙과 한 방향으로 맞췄다).
    admin_blocked = await client.post(
        "/api/staff",
        json={"name": "관리자 카드", "rank": "agent", "app_user_id": admin_user.id},
        headers=await _cookie(admin_user),
    )
    assert admin_blocked.status_code == 403, admin_blocked.text
    assert "본인 계정" in admin_blocked.json()["detail"]

    owner_user = await _make_user("ownerLink", auth.ROLE_OWNER)
    owner_created = await client.post(
        "/api/staff",
        json={"name": "총괄 카드", "rank": "agent", "app_user_id": owner_user.id},
        headers=await _cookie(owner_user),
    )
    assert owner_created.status_code == 201, owner_created.text


async def test_태그_유니크_충돌은_500이_아니라_409다(client, flag_on, monkeypatch):
    """⭐ 선검사와 INSERT 사이에서 겹친 태그는 409로 나간다(3차 X79 수리).

    `_assert_tag_free`가 자기 docstring으로 인정한 자리다 — 그 검사와 쓰기 사이는 안
    잠겨서, 같은 순간 같은 UID를 넣는 두 요청 중 뒤엣것이 유니크 인덱스에 걸린다. 예전에는
    그게 500으로 나가서 **같은 사건에 응답이 409와 500 둘로 갈렸다**(화면이 재시도할지
    문구를 띄울지 못 정한다).

    경합을 결정적으로 만든다 — 선검사를 통째로 지나가게 해서 "검사는 통과했는데 DB가
    막는" 상태를 그대로 만든다. 실제 동시 요청 둘을 띄우면 어느 쪽이 먼저 도는지가
    시험마다 갈려서 아무것도 못 재게 된다(계정 창구 쪽 경합 시험과 같은 수법).
    """
    from app.routers import staff as staff_router

    leader = await _cookie(await _make_user("x79leader", auth.ROLE_LEADER))
    await _make_staff("먼저 온 사람", rank=auth.ROLE_AGENT, tag_id="TAG-DUP")

    async def _pass_through(db, tag_id, *, exclude_id=None):
        return None

    monkeypatch.setattr(staff_router, "_assert_tag_free", _pass_through)

    created = await client.post(
        "/api/staff",
        json={"name": "나중 사람", "rank": "agent", "tag_id": "TAG-DUP"},
        headers=leader,
    )
    assert created.status_code == 409, created.text
    assert created.json()["detail"] == "이미 다른 항목이 쓰고 있는 태그입니다."

    # 수정 갈래도 같은 답이다. 등록은 flush에서 터지지만 수정은 flush가 따로 없어서
    # 커밋에서 올라온다 — 두 자리를 다 감쌌다는 걸 여기서 못박는다.
    mine = await _make_staff("고칠 사람", rank=auth.ROLE_AGENT)
    patched = await client.patch(
        f"/api/staff/{mine.id}", json={"tag_id": "TAG-DUP"}, headers=leader
    )
    assert patched.status_code == 409, patched.text

    # 되돌렸으니 뒤이은 조회가 죽은 트랜잭션을 안 만난다.
    assert (await client.get("/api/staff", headers=leader)).status_code == 200


# ── 복구(다시 활성) ─────────────────────────────────────────────────────────
#
# 2026-08-05 프론트 요청. `deactivate`는 있는데 되돌리는 창구가 없어서, 화면 "복구" 버튼이
# 눌러도 아무 데도 안 가는 상태였다. `PATCH`에 `is_active`를 받지 않고 창구를 따로 둔 이유는
# 해제와 같다 — **이름 한 칸 고치려던 요청이 사람을 되살리는 갈래를 아예 안 만든다.**


async def test_reactivate_raises_the_flag_and_is_idempotent(client, flag_on):
    """복구는 깃발을 올린다. 다시 눌러도 200이다(해제와 같은 규칙)."""
    leader = await _cookie(await _make_user("leader20", auth.ROLE_LEADER))
    row = await _make_staff(rank=auth.ROLE_AGENT)

    await client.post(f"/api/staff/{row.id}/deactivate", headers=leader)

    first = await client.post(f"/api/staff/{row.id}/reactivate", headers=leader)
    assert first.status_code == 200
    assert first.json()["is_active"] is True

    again = await client.post(f"/api/staff/{row.id}/reactivate", headers=leader)
    assert again.status_code == 200, "이미 활성인 행에 다시 보내면 멱등이어야 한다"


async def test_reactivate_keeps_tag_and_student_no(client, flag_on):
    """⚠ 해제는 행을 안 지우므로 되살리면 **태그·학번이 그대로** 있어야 한다."""
    leader = await _cookie(await _make_user("leader21", auth.ROLE_LEADER))
    row = await _make_staff(rank=auth.ROLE_AGENT, tag_id="TAG-KEEP")

    await client.post(f"/api/staff/{row.id}/deactivate", headers=leader)
    back = await client.post(f"/api/staff/{row.id}/reactivate", headers=leader)

    assert back.json()["tag_id"] == "TAG-KEEP", "되살렸더니 태그가 사라졌다"


async def test_reactivate_uses_the_same_gate_as_deactivate(client, flag_on):
    """⛔ 되살리는 쪽만 느슨하면 "못 고치는 사람이 되살릴 수는 있다"는 구멍이 난다."""
    leader = await _cookie(await _make_user("leader22", auth.ROLE_LEADER))
    same_rank = await _make_staff("동급", rank=auth.ROLE_LEADER)

    blocked = await client.post(
        f"/api/staff/{same_rank.id}/reactivate", headers=leader
    )
    assert blocked.status_code == 403, "동급 행을 되살릴 수 있으면 해제 상한이 뜻을 잃는다"


async def test_reactivate_is_written_to_audit_with_its_own_action(client, flag_on):
    """감사 기록에서 "누가 되살렸나"가 `update` 더미에 안 묻힌다."""
    leader = await _cookie(await _make_user("leader23", auth.ROLE_LEADER))
    row = await _make_staff(rank=auth.ROLE_AGENT)

    await client.post(f"/api/staff/{row.id}/deactivate", headers=leader)
    await client.post(f"/api/staff/{row.id}/reactivate", headers=leader)

    async with get_session() as session:
        actions = (
            await session.execute(
                select(StaffAudit.action)
                .where(StaffAudit.staff_id == row.id)
                .order_by(StaffAudit.id)
            )
        ).scalars().all()
    assert actions[-2:] == ["deactivate", "reactivate"], f"감사 기록이 {actions}"


# ── 영구 삭제 ───────────────────────────────────────────────────────────────
#
# 2026-08-05 사용자가 관리 버튼을 수정·일시정지·해제(영구 삭제) 셋으로 나누라고 정했다.
# ⛔ **사건에 엮인 행은 못 지운다.** DB는 안 막으므로(`ondelete="SET NULL"`) 창구 판정
# 한 줄이 유일한 방어다 — 이 케이스들이 그 줄을 지킨다.


async def test_아무_사건에도_안_엮인_행은_지운다(client, flag_on):
    """오타로 만든 행을 지우는 길이다. 예전에는 일시정지밖에 없었다."""
    leader = await _cookie(await _make_user("leader30", auth.ROLE_LEADER))
    row = await _make_staff("오타행", rank=auth.ROLE_AGENT)

    res = await client.delete(f"/api/staff/{row.id}", headers=leader)

    assert res.status_code == 204
    async with get_session() as session:
        left = (await session.execute(select(Staff.id).where(Staff.id == row.id))).all()
    assert left == [], "행이 안 지워졌다"


async def test_경고에_엮인_행은_409로_막는다(client, flag_on):
    """⛔ 이 케이스가 빠지면 지운 순간 그 경고가 조용히 주인을 잃는다."""
    leader = await _cookie(await _make_user("leader31", auth.ROLE_LEADER))
    row = await _make_staff("엮인사람", rank=auth.ROLE_AGENT)
    async with get_session() as session:
        session.add(Alert(type="untagged", staff_id=row.id))
        await session.commit()

    res = await client.delete(f"/api/staff/{row.id}", headers=leader)

    assert res.status_code == 409
    assert res.json()["detail"]["reason"] == "staff_linked_to_alerts"
    assert res.json()["detail"]["linked_alerts"] == 1
    async with get_session() as session:
        still = (await session.execute(select(Staff.id).where(Staff.id == row.id))).all()
    assert still, "409를 냈는데 행이 지워졌다"


async def test_삭제도_해제와_같은_자물쇠를_쓴다(client, flag_on):
    """지우는 쪽만 느슨하면 "못 고치는 사람이 지울 수는 있다"는 구멍이 난다."""
    leader = await _cookie(await _make_user("leader32", auth.ROLE_LEADER))
    same_rank = await _make_staff("동급행", rank=auth.ROLE_LEADER)

    res = await client.delete(f"/api/staff/{same_rank.id}", headers=leader)

    assert res.status_code == 403


async def test_지운_행의_스냅샷이_감사에_남는다(client, flag_on):
    """⭐ 지운 뒤에는 대상 행이 없으므로 **감사 행이 유일한 흔적**이다."""
    leader = await _cookie(await _make_user("leader33", auth.ROLE_LEADER))
    row = await _make_staff("지울사람", rank=auth.ROLE_AGENT, tag_id="TAG-GONE")
    staff_id = row.id

    await client.delete(f"/api/staff/{staff_id}", headers=leader)

    async with get_session() as session:
        audit = (
            await session.execute(
                select(StaffAudit)
                .where(StaffAudit.staff_id == staff_id, StaffAudit.action == "delete")
            )
        ).scalars().one()
    assert audit.before["name"] == "지울사람"
    assert audit.before["tag_id"] == "TAG-GONE", "지운 행의 태그가 기록에 안 남았다"
    assert audit.actor_username, "누가 지웠는지가 안 남았다"
