"""계정 생성 경합 — 같은 아이디가 동시에 들어오면 409여야 한다 (백지 검토 F41).

## 왜 파일을 새로 팠나

`tests/test_account_endpoints.py`는 이번 사이클에 다른 조가 같이 만지는 자리라, 갈래 하나를
붙이려고 그 파일을 열면 텍스트 충돌이 난다. 경합 갈래는 성격이 뚜렷해서 따로 둬도 읽힌다.
합칠 거면 이 파일을 통째로 옮기면 된다.

## 무엇을 못박나

`create_user`는 중복 아이디를 두 겹으로 막는다.
1. 미리 보는 SELECT — 흔한 실수에 친절한 문구를 준다.
2. DB 유니크 제약 — **진짜 정본이다.**

같은 아이디로 두 요청이 동시에 오면 둘 다 1번을 지나고 뒤엣것이 2번에서 걸린다. 그걸 안
잡으면 **500**이 나간다 — 같은 사건에 응답이 409와 500 둘로 갈리면 화면이 재시도할지
문구를 띄울지 못 정한다.

## 경합을 어떻게 결정론으로 만드나

선검사와 INSERT 사이에 `await auth.hash_password_async(...)` 하나가 있다. 그 자리가 곧
경합 창이라, 그 함수를 갈아 끼워 "기다리는 동안 남이 먼저 넣는" 상황을 그대로 만든다.
`asyncio.gather`로 진짜 두 요청을 던지면 어느 쪽이 이길지가 매번 달라서, 시험이 간헐로
깨지거나 아예 경합 창을 못 밟는다.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app import auth
from app.db import get_session
from app.models import AppUser

pytestmark = pytest.mark.asyncio(loop_scope="session")

_USERNAME = "raceuser"
_PASSWORD = "pw-12345678"


async def _admin_cookie() -> dict[str, str]:
    """계정 창구는 플래그와 무관하게 늘 관리자 판정이라 세션이 필요하다."""
    async with get_session() as session:
        admin = AppUser(
            username="raceadmin",
            password_hash=auth.hash_password(_PASSWORD),
            display_name="경합 시험 관리자",
            role=auth.ROLE_ADMIN,
            is_active=True,
        )
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
        token = await auth.create_session(session, admin)
    return {"Cookie": f"{auth.SESSION_COOKIE_NAME}={token}"}


async def test_같은_아이디_동시_생성은_409다(client, monkeypatch):
    """반증 자리 — 수리 전에는 여기서 IntegrityError가 그대로 올라와 500이었다."""
    headers = await _admin_cookie()
    real_hash = auth.hash_password_async

    async def _insert_rival_then_hash(password: str) -> str:
        # 선검사는 이미 지났다. 그 사이에 딴 요청이 같은 아이디를 먼저 커밋한다.
        async with get_session() as session:
            session.add(
                AppUser(
                    username=_USERNAME,
                    password_hash=auth.hash_password(_PASSWORD),
                    display_name="먼저 들어온 요청",
                    role=auth.ROLE_AGENT,
                    is_active=True,
                )
            )
            await session.commit()
        # 한 번만 끼어든다 — 두 번째 호출부터는 그냥 해시만 만든다.
        monkeypatch.setattr(auth, "hash_password_async", real_hash)
        return await real_hash(password)

    monkeypatch.setattr(auth, "hash_password_async", _insert_rival_then_hash)

    res = await client.post(
        "/api/users",
        json={
            "username": _USERNAME,
            "password": _PASSWORD,
            "display_name": "나중에 들어온 요청",
            "role": auth.ROLE_AGENT,
        },
        headers=headers,
    )

    assert res.status_code == 409, f"경합이 500으로 샜다 — {res.status_code} {res.text}"
    assert res.json()["detail"] == "이미 있는 아이디입니다."

    # 행은 먼저 들어온 쪽 하나뿐이다(뒤엣것이 덮지도, 두 줄이 서지도 않는다).
    async with get_session() as session:
        rows = (
            await session.execute(
                select(AppUser).where(AppUser.username == _USERNAME)
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].display_name == "먼저 들어온 요청"
