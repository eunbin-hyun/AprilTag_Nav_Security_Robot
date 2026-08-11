"""계정 심기 도구 `tools/seed_users.py` (2026-08-02 백지 검토 F61).

## 왜 시험이 필요한가

이 스크립트가 **롤아웃 2단계 전부**다(설계 §6.2). 계정 창구(`POST /api/users`)는 관리자
세션을 요구하니까 첫 계정은 창구로 못 만들고, 그 닭·달걀을 여기서 푼다. 그런데 시험이
0건이었다 — 켜기 직전 한 번 도는 자리가 조용히 틀리면 **아무도 못 들어가는 잠금**이 나고,
그때는 되돌릴 창구도 없다(플래그를 다시 끄는 배포가 유일한 길이다).

## 여기서 재는 것 셋

1. **아이디 정규화** — 대문자·둘레 공백이 섞여 들어와도 저장되는 값은 소문자 한 모양이다.
   여기가 어긋나면 심은 아이디와 로그인이 조회하는 아이디가 갈려서(`auth.normalize_username`
   이 로그인 쪽을 접는다) 비밀번호가 맞는데도 계속 401이다.
2. **멱등** — 이미 있는 계정에 `--update` 없이 다시 돌리면 **한 칸도 안 건드린다.** 배포
   스크립트에 넣을 수 있어야 한다는 게 이 도구의 계약이고, 안 지켜지면 재배포 한 번이
   운영 중인 계정의 비밀번호를 갈아 버린다.
3. **`--update`의 세션 무효화** — 비밀번호를 다시 정하면 옛 비밀번호로 연 세션이 죽는다.
   안 죽으면 다시 정한 뜻이 절대 만료(12시간)까지 없다.

## 시험이 스크립트를 부르는 방식

`main()`이 아니라 `_seed()`를 직접 부른다. `main()`은 `asyncio.run`을 쓰는데, MainThread에서
그걸 부르면 pytest-asyncio 세션 루프 포인터가 지워져 뒤따르는 async 시험이 통째로 깨진다
(`test_migrations_roundtrip.py`가 같은 함정을 실측으로 적어 뒀다). `_seed`는 평범한
코루틴이라 세션 루프 위에서 그대로 await하면 된다 — 인자 조립(`argparse`)만 우리가 한다.

비밀번호는 `--password-stdin` 갈래로 넣는다. 물어보기(getpass) 갈래는 사람이 있어야 도는
자리라 시험이 태울 수 없고, 자동화가 실제로 쓰는 길도 표준입력 쪽이다.
"""
from __future__ import annotations

import argparse
import io
import sys

import pytest
from sqlalchemy import select

from app import auth
from app.db import get_session
from app.models import AppUser, AuthSession
from tools import seed_users

pytestmark = pytest.mark.asyncio(loop_scope="session")

_PASSWORD = "seed-pw-1234"
_NEW_PASSWORD = "seed-pw-5678"


def _args(**overrides) -> argparse.Namespace:
    """`main()`이 조립하는 것과 같은 모양의 인자 묶음."""
    values = {
        "username": "seeded1",
        "display_name": "손세욱",
        "role": auth.ROLE_ADMIN,
        "password_stdin": True,
        "update": False,
        "list": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.fixture
def stdin_password(monkeypatch):
    """표준입력에 비밀번호 한 줄을 깔고, **읽혔는지까지** 알려주는 스트림을 돌려준다.

    멱등 케이스가 "비밀번호를 아예 안 물어봤다"를 재려면 읽힘 여부가 필요하다 — 스크립트가
    행을 안 건드렸어도 입력을 먼저 삼켰다면, 자동화에서 그 다음 줄이 밀려 다음 계정이
    엉뚱한 비밀번호를 받는다.
    """

    def _plant(password: str) -> io.StringIO:
        stream = io.StringIO(f"{password}\n")
        monkeypatch.setattr(sys, "stdin", stream)
        return stream

    return _plant


async def _read_user(username: str) -> AppUser | None:
    async with get_session() as session:
        return (
            await session.execute(select(AppUser).where(AppUser.username == username))
        ).scalar_one_or_none()


# ── 1. 아이디 정규화 ───────────────────────────────────────────────────────


async def test_아이디를_접어서_심는다(stdin_password):
    """대문자·둘레 공백이 섞여도 저장값은 소문자 한 모양이다.

    로그인 창구가 `normalize_username`으로 접어서 조회하니(app/routers/auth.py), 심는 쪽이
    안 접으면 그 계정에는 영원히 못 들어간다.
    """
    stdin_password(_PASSWORD)
    await seed_users._seed(_args(username="  SonSeuk1  "))

    user = await _read_user("sonseuk1")
    assert user is not None, "접은 아이디로 안 심겼다"
    assert user.role == auth.ROLE_ADMIN
    assert user.is_active is True
    assert auth.verify_password(_PASSWORD, user.password_hash)
    # 원문 그대로 심긴 행이 따로 생기면 안 된다.
    assert await _read_user("  SonSeuk1  ") is None


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("ab", id="너무 짧음"),
        pytest.param("a" * 21, id="너무 김"),
        pytest.param("손세욱", id="한글"),
        pytest.param("son.seuk", id="점"),
    ],
)
async def test_규칙_밖_아이디는_안_심는다(stdin_password, bad):
    """아이디 규칙(§8 확정 A)은 **만드는 자리**가 막는다.

    창구(`POST /api/users`)와 이 도구가 같은 `USERNAME_RE`를 봐야 두 길로 들어온 계정이
    같은 모양이다. 여기가 뚫리면 규칙 밖 아이디가 DB에만 생기고 화면·창구는 그 존재를 모른다.
    """
    stdin_password(_PASSWORD)
    with pytest.raises(SystemExit):
        await seed_users._seed(_args(username=bad))

    async with get_session() as session:
        rows = (await session.execute(select(AppUser))).scalars().all()
    assert rows == [], "규칙 밖 아이디가 심겼다"


async def test_짧은_비밀번호는_안_심는다(stdin_password):
    """8자 미만은 거절한다. 계정 창구와 같은 하한이라 두 길이 안 갈린다."""
    stdin_password("1234")
    with pytest.raises(SystemExit):
        await seed_users._seed(_args(username="shortpw"))
    assert await _read_user("shortpw") is None


# ── 2. 멱등 ────────────────────────────────────────────────────────────────


async def test_두_번_돌려도_기존_계정을_안_건드린다(stdin_password):
    """`--update` 없이 다시 돌리면 비밀번호·이름·직급이 그대로다.

    배포 스크립트에 넣을 수 있어야 한다는 게 이 도구의 계약이다. 안 지켜지면 재배포 한 번이
    운영 중인 계정의 비밀번호를 조용히 갈아서, 그 사람이 다음 로그인에 못 들어간다.
    """
    stdin_password(_PASSWORD)
    await seed_users._seed(_args(username="idem1", display_name="처음이름"))
    before = await _read_user("idem1")
    assert before is not None

    # 두 번째 판 — 이름·직급·비밀번호를 다 바꿔서 넣는데도 아무 일도 안 일어나야 한다.
    stream = stdin_password(_NEW_PASSWORD)
    await seed_users._seed(
        _args(username="idem1", display_name="바뀐이름", role=auth.ROLE_AGENT)
    )

    after = await _read_user("idem1")
    assert after.password_hash == before.password_hash, "멱등 판이 비밀번호를 갈았다"
    assert after.display_name == "처음이름", "멱등 판이 실명을 갈았다"
    assert after.role == auth.ROLE_ADMIN, "멱등 판이 직급을 갈았다"
    assert stream.tell() == 0, (
        "멱등 판이 표준입력을 읽었다 — 자동화에서 다음 계정의 비밀번호 줄이 밀린다"
    )


async def test_멱등_판은_세션을_안_끊는다(stdin_password):
    """이미 있는 계정을 다시 돌려도 붙어 있는 사람이 안 튕긴다.

    "아무것도 안 건드린다"에 세션도 들어간다. 여기가 새면 배포마다 요원이 전원 로그아웃된다.
    """
    stdin_password(_PASSWORD)
    await seed_users._seed(_args(username="idem2"))
    user = await _read_user("idem2")
    async with get_session() as session:
        await auth.create_session(session, user)

    stdin_password(_NEW_PASSWORD)
    await seed_users._seed(_args(username="idem2"))

    async with get_session() as session:
        row = (await session.execute(select(AuthSession))).scalars().one()
    assert row.revoked_at is None, "멱등 판이 살아 있는 세션을 끊었다"


# ── 3. `--update` ──────────────────────────────────────────────────────────


async def test_update가_비밀번호를_갈고_세션을_끊는다(stdin_password):
    """비밀번호를 다시 정하면 옛 비밀번호가 죽고 살아 있던 세션도 같이 끊긴다.

    끊는 자리가 없으면 옛 비밀번호로 연 창이 절대 만료(12시간)까지 그대로 산다 — 비밀번호를
    바꾼 뜻이 통째로 사라진다(auth.revoke_user_sessions docstring과 같은 자리).
    """
    stdin_password(_PASSWORD)
    await seed_users._seed(_args(username="upd1", display_name="처음이름"))
    user = await _read_user("upd1")
    async with get_session() as session:
        token = await auth.create_session(session, user)

    stdin_password(_NEW_PASSWORD)
    await seed_users._seed(
        _args(
            username="upd1",
            display_name="바뀐이름",
            role=auth.ROLE_LEADER,
            update=True,
        )
    )

    after = await _read_user("upd1")
    assert auth.verify_password(_NEW_PASSWORD, after.password_hash), "새 비밀번호가 안 먹는다"
    assert not auth.verify_password(_PASSWORD, after.password_hash), "옛 비밀번호가 아직 산다"
    assert after.display_name == "바뀐이름"
    assert after.role == auth.ROLE_LEADER

    async with get_session() as session:
        row = (await session.execute(select(AuthSession))).scalars().one()
        assert row.revoked_at is not None, "--update가 살아 있는 세션을 안 끊었다"
        # 무효화가 실제로 먹는지까지 본다 — `revoked_at`만 찍히고 판정이 안 보면 뜻이 없다.
        assert await auth.resolve_session(session, token) is None


async def test_update가_해제된_계정을_되살린다(stdin_password):
    """해제된 계정에 `--update`를 걸면 다시 활성이다.

    잠금 사고에서 되돌리는 유일한 길이 이 도구라(창구는 관리자 세션이 있어야 들어간다),
    해제된 계정을 못 되살리면 관리자가 전부 해제된 판에서 빠져나올 자리가 없다.
    """
    stdin_password(_PASSWORD)
    await seed_users._seed(_args(username="revive1"))
    async with get_session() as session:
        user = (
            await session.execute(select(AppUser).where(AppUser.username == "revive1"))
        ).scalar_one()
        user.is_active = False
        await session.commit()

    stdin_password(_NEW_PASSWORD)
    await seed_users._seed(_args(username="revive1", update=True))

    after = await _read_user("revive1")
    assert after.is_active is True, "--update가 해제된 계정을 안 되살렸다"


async def test_update는_없는_계정도_만든다(stdin_password):
    """`--update`를 붙였는데 계정이 없으면 그냥 만든다.

    배포 스크립트가 늘 `--update`를 달고 도는 게 자연스러운 모양이라, 첫 실행에서 터지면
    롤아웃 2단계가 통째로 멈춘다.
    """
    stdin_password(_PASSWORD)
    await seed_users._seed(_args(username="upsert1", update=True))
    assert await _read_user("upsert1") is not None
