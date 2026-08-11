"""로그인 규칙 — 비밀번호 해시 · 실패 잠금 · 명부 쓰기 방어.

DB도 이벤트 루프도 안 타는 순수 판정만 모았다. 세션 수명·역할 게이트처럼 DB가 필요한
갈래는 `test_auth_core.py`, HTTP 창구는 `test_auth_endpoints.py`가 본다.

파일을 가른 이유는 루프 스코프다. DB 시험은 conftest의 세션 스코프 픽스처를 타야 해서
`pytest.mark.asyncio(loop_scope="session")`을 파일 전체에 걸어야 하는데, 그 표시를 동기
시험에 걸면 pytest가 매번 경고를 낸다. 계약은 `docs/로그인_설계초안_2026-08-01.md`
(사용자 확정본) §2.6·§4·§8이다.

⚠ 인메모리 잠금 초기화는 conftest `_clean` 하나가 한다. 이 파일은 동기 시험만 있는데도 그
훅을 탄다(`--setup-show`로 실측 — autouse async 픽스처는 동기 시험에도 붙는다). 자기 사본을
또 두면 그 훅이 빠져도 이 파일만은 통과해서 결함이 안 보인다(2026-08-02 교차 검토).
"""
import datetime as dt

import pytest
from fastapi import HTTPException

from app import auth
from app.models import Staff

_BASE_T = dt.datetime(2026, 8, 1, 12, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def clock(monkeypatch):
    """잠금 시계를 시험이 직접 감는다(60초를 실제로 기다릴 이유가 없다)."""
    holder = {"t": _BASE_T}
    monkeypatch.setattr(auth, "_now", lambda: holder["t"])
    return holder


# ── 1. 비밀번호 해시 왕복 (§2.6) ───────────────────────────────────────────

def test_password_hash_roundtrip():
    stored = auth.hash_password("bi-mil-1234")
    assert auth.verify_password("bi-mil-1234", stored) is True
    assert auth.verify_password("bi-mil-1235", stored) is False
    # 저장 형식에 파라미터가 실린다 — 나중에 세기를 올려도 기존 행이 안 깨진다.
    assert stored.startswith("scrypt$16384$8$1$32$")
    # DB 칸이 255자다. 형식이 길어져 잘리면 검증이 통째로 무너진다.
    assert len(stored) <= 255


def test_password_hash_is_salted():
    """같은 비밀번호라도 저장값이 매번 다르다. 같으면 무지개표 한 장에 전부 털린다."""
    a = auth.hash_password("same-password")
    b = auth.hash_password("same-password")
    assert a != b
    assert auth.verify_password("same-password", a)
    assert auth.verify_password("same-password", b)


def test_password_verify_handles_non_ascii_and_garbage():
    """비ASCII 비밀번호가 500을 안 낸다. 깨진 저장값도 예외 없이 False다.

    `hmac.compare_digest`는 str을 받으면 두 값이 다 ASCII일 때만 돌고 아니면 TypeError를
    던진다(`app/security.py` 머리의 실측 함정). 파생 바이트끼리 견주므로 그 조건이 안 선다.
    """
    stored = auth.hash_password("한글비밀번호😀")
    assert auth.verify_password("한글비밀번호😀", stored) is True
    assert auth.verify_password("한글비밀번호", stored) is False

    assert auth.verify_password("x", "") is False
    assert auth.verify_password("x", "plaintext") is False
    assert auth.verify_password("x", "scrypt$16384$8$1$32$!!!!$????") is False
    assert auth.verify_password("x", "argon2$1$2$3$4$5$6") is False
    assert auth.verify_password("", stored) is False


def test_username_rule_is_lowercase_alnum_3_to_20():
    """아이디 규칙(§8 확정 A). **계정을 만드는** 창구가 이걸로 막고 로그인은 안 막는다."""
    for good in ("son", "sonseuk", "user01", "a" * 20):
        assert auth.USERNAME_RE.match(good), good
    for bad in ("so", "a" * 21, "SonSeuk", "son_seuk", "손세욱", "son seuk", ""):
        assert not auth.USERNAME_RE.match(bad), bad


# ── 2. 로그인 실패 잠금 (§8 결정 3 — 계정당 5회·60초) ──────────────────────

def test_login_lock_after_five_failures_and_release(clock):
    for _ in range(4):
        auth.record_login_failure("sonseuk")
    assert auth.login_lock_remaining("sonseuk") == 0.0, "4회는 아직 안 잠근다"

    auth.record_login_failure("sonseuk")
    assert auth.login_lock_remaining("sonseuk") == pytest.approx(60.0)
    # 잠금은 계정별이다 — 남의 아이디까지 같이 잠기면 한 사람이 전원을 잠글 수 있다.
    assert auth.login_lock_remaining("nam") == 0.0

    clock["t"] = _BASE_T + dt.timedelta(seconds=61)
    assert auth.login_lock_remaining("sonseuk") == 0.0
    # 풀린 뒤 실패 한 번에 바로 다시 잠기지 않는다(오타 한 번이 영구 잠금이 되면 안 된다).
    auth.record_login_failure("sonseuk")
    assert auth.login_lock_remaining("sonseuk") == 0.0


def test_login_lock_key_is_case_folded(clock):
    """대소문자를 갈아 가며 잠금을 피해 갈 수 없다. 아이디 규칙이 소문자라 접는다."""
    for _ in range(5):
        auth.record_login_failure("SonSeuk")
    assert auth.login_lock_remaining("sonseuk") > 0
    assert auth.login_lock_remaining("SONSEUK") > 0

    auth.clear_login_failures("SONSEUK")
    assert auth.login_lock_remaining("sonseuk") == 0.0


def test_login_failure_map_drops_stale_entries(clock):
    """오래된 항목은 걷힌다 — 열쇠를 공격자가 정하는 표라 안 걷으면 메모리가 계속 자란다(F6).

    아직 유효한 잠금은 **안 걷는다.** 걷으면 그게 곧 잠금 해제다.
    """
    auth.record_login_failure("old-one")
    for _ in range(5):
        auth.record_login_failure("locked-one")
    assert auth.login_lock_remaining("locked-one") > 0

    # TTL(1시간)을 넘긴 시점에 새 실패가 들어오면 그때 청소가 돈다.
    clock["t"] = _BASE_T + dt.timedelta(hours=2)
    auth.record_login_failure("fresh-one")

    keys = set(auth._login_failures)
    assert "old-one" not in keys, "TTL 지난 항목이 안 걷혔다"
    assert "fresh-one" in keys
    # 잠금은 2시간 뒤라 이미 풀렸으니 같이 걷힌다 — 그게 이 표가 무한히 안 자라는 근거다.
    assert "locked-one" not in keys


def test_login_failure_map_is_capped_and_still_counts_new_failures(clock, monkeypatch):
    """상한을 넘겨도 **새 실패는 반드시 세진다**(F6).

    상한에 닿았을 때 새 항목을 안 만들면, 쓰레기 아이디로 표를 채워 놓고 진짜 아이디를
    두드리는 것만으로 잠금이 통째로 무력화된다. 여기서는 상한을 8로 줄여 그 조건을 만든다.
    """
    monkeypatch.setattr(auth, "_FAIL_ENTRY_MAX", 8)
    for i in range(50):
        auth.record_login_failure(f"junk{i}")

    assert len(auth._login_failures) <= 8, f"상한을 넘겼다: {len(auth._login_failures)}"

    for _ in range(5):
        auth.record_login_failure("victim")
    assert auth.login_lock_remaining("victim") > 0, (
        "표가 상한이라 새 실패를 못 셌다 — 그게 곧 잠금 무력화다"
    )


def test_capped_map_evicts_unlocked_entries_before_live_locks(clock, monkeypatch):
    """버릴 후보는 **잠기지 않은 항목부터**. 살아 있는 잠금을 밀어내면 그게 잠금 해제다(F6)."""
    monkeypatch.setattr(auth, "_FAIL_ENTRY_MAX", 8)
    for _ in range(5):
        auth.record_login_failure("victim")
    assert auth.login_lock_remaining("victim") > 0

    # 잠금이 아직 살아 있는 30초 시점에 쓰레기로 표를 가득 채운다.
    clock["t"] = _BASE_T + dt.timedelta(seconds=30)
    for i in range(50):
        auth.record_login_failure(f"junk{i}")

    assert auth.login_lock_remaining("victim") > 0, (
        "표를 채우는 것만으로 남의 잠금이 풀렸다"
    )


# ── 3. assert_can_write 갈래 전부 (§4) ─────────────────────────────────────

def _actor(role: str, user_id: int = 10) -> auth.AuthActor:
    return auth.AuthActor(id=user_id, username=f"u{user_id}", display_name="이름", role=role)


def _row(app_user_id: int | None = None, rank: str | None = None) -> Staff:
    """명부 행 하나. DB에 안 넣는다 — 판정 함수는 칸 두 개만 본다."""
    return Staff(name="대상", rank=rank, app_user_id=app_user_id)


def _forbidden(*args) -> HTTPException:
    with pytest.raises(HTTPException) as exc:
        auth.assert_can_write(*args)
    assert exc.value.status_code == 403, "쓰기 방어 실패는 403이다(401 아님 — §7.2)"
    return exc.value


def test_owner_passes_everything_but_own_state_change():
    """⭐ 총괄(`owner`)은 전부 통과다 — **본인 행도**. (2026-08-05 사용자 확정 · 프론트 20차)

    ⚠ 딱 하나 막는 것은 **자기 계정을 정지·삭제**하는 것이다. 그러면 총괄이 사라지고
    되살릴 사람도 같이 없어진다. 고치기(이름·학번·태그)는 열어 둔다.
    """
    owner = _actor(auth.ROLE_OWNER, user_id=1)

    # 남의 행 — 급을 안 가리고 전부.
    auth.assert_can_write(owner, auth.ROLE_ADMIN, _row(app_user_id=99, rank="admin"))
    auth.assert_can_write(owner, auth.ROLE_OWNER, _row(app_user_id=99, rank="owner"))
    auth.assert_can_write(owner, None, _row(app_user_id=99))
    auth.assert_can_write(owner, auth.ROLE_ADMIN, None)  # 신규 등록

    # 본인 행 — 고치기는 열린다.
    auth.assert_can_write(owner, auth.ROLE_OWNER, _row(app_user_id=1, rank="owner"))

    # ⛔ 본인 행 상태 바꾸기만 막힌다.
    with pytest.raises(HTTPException) as exc:
        auth.assert_can_write(
            owner, auth.ROLE_OWNER, _row(app_user_id=1, rank="owner"), state_change=True
        )
    assert exc.value.status_code == 403
    assert "총괄" in exc.value.detail


def test_admin_cannot_touch_admin_rows_anymore():
    """⛔ **관리자는 이제 관리자 급 행을 못 고친다** — 남의 것도 자기 것도.

    총괄 등급이 생기기 전에는 관리자가 사다리를 통째로 비켜갔다. 그 예외 때문에 본인
    항목 특례를 따로 둬야 했고 A안·B안으로 갈렸다. 위에 한 급을 얹으니 **"자기보다
    아랫급만"이라는 규칙 하나**로 정리된다.
    """
    admin = _actor(auth.ROLE_ADMIN, user_id=7)

    # 남의 admin 행 — 동급이라 막힌다.
    assert "아랫급" in _forbidden(
        admin, auth.ROLE_ADMIN, _row(app_user_id=99, rank="admin")
    ).detail
    # 윗급(owner) 행도 막힌다.
    assert "아랫급" in _forbidden(
        admin, auth.ROLE_OWNER, _row(app_user_id=99, rank="owner")
    ).detail
    # 본인 행은 본인 검사에서 먼저 막힌다.
    assert "본인 항목" in _forbidden(
        admin, auth.ROLE_ADMIN, _row(app_user_id=7, rank="admin")
    ).detail

    # ⭐ 아랫급은 그대로 통과한다 — 관리자가 아무것도 못 하게 된 것이 아니다.
    auth.assert_can_write(admin, auth.ROLE_LEADER, _row(app_user_id=99, rank="leader"))
    auth.assert_can_write(admin, auth.ROLE_AGENT, _row(app_user_id=99, rank="agent"))


def test_can_write_leader_only_below():
    """팀장은 아랫급만. 동급·윗급은 막힌다."""
    leader = _actor(auth.ROLE_LEADER, user_id=3)
    auth.assert_can_write(leader, auth.ROLE_AGENT, _row(app_user_id=None, rank="agent"))

    assert "아랫급" in _forbidden(leader, auth.ROLE_LEADER, _row(rank="leader")).detail
    assert "아랫급" in _forbidden(leader, auth.ROLE_ADMIN, _row(rank="admin")).detail


def test_can_write_agent_cannot_write_anything():
    """요원은 명부 쓰기가 아예 없다 — 자기 아래 급이 없어서 사다리만으로 닫힌다."""
    agent = _actor(auth.ROLE_AGENT, user_id=5)
    for rank in (auth.ROLE_AGENT, auth.ROLE_LEADER, auth.ROLE_ADMIN):
        assert _forbidden(agent, rank, _row(rank=rank)).status_code == 403


def test_can_write_registration_rank_ceiling():
    """등록 직급 상한. 같은 함수 하나가 막는다 — 수정·해제만 막으면 우회 승격 길이 남는다.

    `target_row`가 없는 호출(=신규 등록)이라 판정은 지정한 직급 하나로만 갈린다.
    """
    leader = _actor(auth.ROLE_LEADER, user_id=3)
    auth.assert_can_write(leader, auth.ROLE_AGENT)
    assert _forbidden(leader, auth.ROLE_LEADER).status_code == 403
    assert _forbidden(leader, auth.ROLE_ADMIN).status_code == 403


def test_can_write_blocks_own_row_even_when_rank_is_lower():
    """본인 항목은 급이 아래여도 막힌다. 자기 카드를 자기가 지우는 잠금 사고 방지다.

    판정 열쇠는 `app_user_id` 링크 하나다(확정 D) — 이름이 같아도 링크가 없으면 남의 행이다.
    """
    leader = _actor(auth.ROLE_LEADER, user_id=3)
    blocked = _forbidden(leader, auth.ROLE_AGENT, _row(app_user_id=3, rank="agent"))
    assert "본인" in blocked.detail
    # 이름이 같아도 링크가 없으면 본인이 아니다(동명이인 한 명이면 남의 행이 잠긴다).
    auth.assert_can_write(leader, auth.ROLE_AGENT, _row(app_user_id=None, rank="agent"))


def test_can_write_promotion_needs_both_ranks_checked():
    """직급을 올리는 수정은 **옛 급과 새 급 둘 다** 넣어 불러야 막힌다.

    요원 행을 팀장으로 올리는 요청은 옛 급(agent)만 보면 통과한다 — 그게 우회 승격 길이다.
    창구가 두 번 부르는 게 계약이라(함수 docstring), 여기서 그 두 번을 실제로 재현한다.
    """
    leader = _actor(auth.ROLE_LEADER, user_id=3)
    row = _row(app_user_id=None, rank=auth.ROLE_AGENT)

    auth.assert_can_write(leader, row.rank, row)                    # 옛 급 — 통과
    assert _forbidden(leader, auth.ROLE_LEADER, row).status_code == 403  # 새 급 — 막힘


def test_can_write_unknown_rank_is_closed():
    """직급을 모르는 행은 **닫는 쪽**이다. 열어 두면 상한이 뜻을 잃는다."""
    leader = _actor(auth.ROLE_LEADER, user_id=3)
    assert _forbidden(leader, None, _row(rank=None)).status_code == 403
    assert _forbidden(leader, "superadmin").status_code == 403


def test_can_write_is_noop_without_actor():
    """행위자가 없으면(=플래그 off 구간) 통과한다. 제1 불변이 여기서도 선다."""
    auth.assert_can_write(None, auth.ROLE_ADMIN, _row(app_user_id=1, rank="admin"))
