"""공용 훅이 계정·세션·로그인 잠금을 정말 비우는지 못박는다 (2026-08-02 백지 검토 F60).

## 왜 이 파일이 필요한가

`conftest._clean`이 `app_user`·`auth_session`을 TRUNCATE하고 `reset_auth_state()`로 인메모리
로그인 잠금을 비운다. 로그인 시험 파일들(`test_auth_endpoints`·`test_auth_gates`·
`test_account_endpoints`)이 각자 들고 있던 자기 정리 코드는 그 훅으로 올리면서 걷어냈고,
근거는 **"그 파일들이 통과하는 게 훅이 도는 증거"**였다.

그 근거가 pytest-xdist에서 무너진다. 그 논증은 "앞 케이스가 남긴 오염을 뒷 케이스가 물려받아
깨진다"에 기대는데, conftest가 워커별 DB를 따로 만들기 때문에(`_ensure_worker_database`)
`-n auto`면 오염을 만드는 케이스와 그걸 밟는 케이스가 **서로 다른 프로세스·다른 DB**로
흩어진다. 그러면 물려받을 오염이 아예 없어서 **훅이 아무것도 안 지우는데도 전부 초록**이다.
셔틀 쿨다운은 `test_shuttle_cooldown_hygiene.py`가 이 구멍을 이미 지키고 있는데(그 파일 머리에
실측이 있다), 계정·세션·잠금에는 같은 자리가 없었다.

## 어떻게 무는가

오염원을 바깥 파일이 아니라 **이 모듈의 훅**으로 옮긴다. `_plant_before_clean`(모듈 범위)과
`_replant_for_next_case`(케이스 범위 teardown)가 매 케이스 `_clean` **앞에** 계정·세션·잠금을
심는다. 심는 자리와 검사하는 자리가 한 프로세스라 워커가 몇 개든, 케이스가 어느 워커로
흩어지든 재현이 성립한다. 픽스처 순서는 pytest 규칙 그대로다 — 높은 범위가 먼저 서고, 앞
케이스 teardown이 다음 케이스 setup보다 먼저다.

심고 나서 심긴 것까지 확인한다(`_planted`). 안 심겼는데 케이스가 "깨끗하다"를 보고 초록이면
가드가 무력해진 걸 못 본다 — 그게 이 결함의 본체다.

## 반증 방법

`conftest._clean`에서 `app_user`·`auth_session`을 TRUNCATE 목록에서 빼거나
`reset_auth_state()` 호출을 지우면, 아래 세 케이스가 직렬에서도 `-n 2`에서도 빨개진다.
"""
from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from app import auth
from app.db import get_session
from app.models import AppUser, AuthSession

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 오염원이 심는 아이디. 케이스가 같은 아이디를 다시 만들어 보므로 유니크 제약이 그대로
# 반증 도구가 된다 — 안 지워졌으면 두 번째 INSERT가 터진다.
WITNESS_USERNAME = "witnessuser"
# 잠금만 따로 보는 아이디. 계정 행이 없어도 잠금은 인메모리라 따로 산다.
WITNESS_LOCKED_USERNAME = "lockedwitness"

# 눈금이 실제로 심겼다는 증거. 비어 있으면 오염원이 안 돈 것이라, 케이스가 "깨끗하다"를 봐도
# 아무것도 증명하지 못한다(거짓 초록).
_planted: list[str] = []


async def _wipe() -> None:
    """이 파일이 만든 계정·세션 행을 걷는다(세션이 계정을 물고 있어 순서가 있다)."""
    async with get_session() as session:
        await session.execute(AuthSession.__table__.delete())
        await session.execute(AppUser.__table__.delete())
        await session.commit()


async def _plant() -> None:
    """다음 케이스로 새야 하는 계정·세션·잠금을 심고, 심긴 것까지 여기서 확인한다.

    ⚠ 심기 전에 먼저 걷는다. 케이스가 자기 안에서 같은 아이디를 다시 만들어 보기 때문에
    (`test_초기화_훅이_앞_케이스_계정을_지운다`), 안 걷으면 teardown의 다시 심기가 유니크
    제약에 걸려 터진다. 걷는 게 판정을 무르게 만들지 않는다 — 이 함수는 "오염을 만든다"가
    일이고, "훅이 지웠나"는 케이스가 `_clean` 뒤에 본다.
    """
    await _wipe()
    async with get_session() as session:
        user = AppUser(
            username=WITNESS_USERNAME,
            # 해시 계산을 안 한다 — 이 갈래는 로그인 창구를 안 타고 행 존재만 본다.
            # scrypt 한 번이 케이스마다 붙으면 배치가 그만큼 길어진다.
            password_hash="not-verified-in-this-file",
            display_name="증인",
            role=auth.ROLE_ADMIN,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        now = dt.datetime.now(dt.timezone.utc)
        session.add(
            AuthSession(
                token_hash=auth.token_digest(f"witness-token-{user.id}"),
                user_id=user.id,
                created_at=now,
                expires_at=now + dt.timedelta(hours=12),
                last_seen_at=now,
            )
        )
        await session.commit()

    # 인메모리 잠금 — 상한만큼 실패시켜 실제로 잠긴 상태를 만든다.
    for _ in range(auth.get_settings().login_fail_max_attempts):
        auth.record_login_failure(WITNESS_LOCKED_USERNAME)

    assert await _count(AppUser) == 1, "오염원이 계정을 못 심었다"
    assert await _count(AuthSession) == 1, "오염원이 세션을 못 심었다"
    assert auth.login_lock_remaining(WITNESS_LOCKED_USERNAME) > 0, (
        "오염원이 로그인 잠금을 못 심었다 — 이 시험은 아무것도 못 지킨다"
    )
    _planted.append(WITNESS_USERNAME)


async def _count(model) -> int:
    async with get_session() as session:
        return (
            await session.execute(select(func.count()).select_from(model))
        ).scalar_one()


@pytest_asyncio.fixture(scope="module", loop_scope="session", autouse=True)
async def _plant_before_clean():
    """첫 케이스 몫 오염을 심고, 파일이 끝나면 되돌린다.

    모듈 범위라 케이스 범위 autouse인 conftest `_clean`보다 **먼저** 선다. 그래서 "앞 케이스가
    남긴 상태"와 같은 자리에 놓인다.

    끝에 되돌리는 건 검사 위생이다 — 심은 오염을 파일 밖으로 흘리면 다음 파일이 우리가 만든
    행을 물려받는다(그게 애초에 이 파일이 잡는 함정이다).
    """
    await _plant()
    yield
    await _wipe()
    auth.reset_auth_state()


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _replant_for_next_case():
    """케이스가 끝날 때마다 오염을 다시 심는다.

    모듈 훅은 한 번만 돌아서 첫 케이스만 덮는다. 케이스 순서가 바뀌거나 케이스가 늘면 가드가
    조용히 무력해지니, 매 케이스 앞에 오염이 있게 만든다. teardown에서 심으면 다음 케이스
    setup의 `_clean`보다 앞이다.
    """
    yield
    await _plant()


async def test_초기화_훅이_앞_케이스_계정을_지운다():
    """`_clean`의 TRUNCATE 목록에서 `app_user`가 빠지면 여기가 빨개진다.

    행 수만 보지 않고 **같은 아이디를 다시 만들어 본다** — 유니크 제약이 걸린 칸이라, 안
    지워졌으면 계정을 만드는 다음 파일이 원인 모를 409·IntegrityError로 깨지는 그 갈래를
    그대로 재현한다.
    """
    assert _planted, "오염원 훅이 안 돌았다 — 오염 없이 초록이면 가드가 무력해진 것이다"
    assert await _count(AppUser) == 0, (
        "앞 케이스가 남긴 계정 행이 그대로다 — conftest _clean이 app_user를 안 비운다"
    )

    async with get_session() as session:
        session.add(
            AppUser(
                username=WITNESS_USERNAME,
                password_hash="not-verified-in-this-file",
                display_name="같은 아이디",
                role=auth.ROLE_AGENT,
            )
        )
        await session.commit()


async def test_초기화_훅이_앞_케이스_세션을_지운다():
    """`auth_session`이 목록에서 빠지면 여기가 빨개진다.

    세션이 남으면 "살아 있는 세션 수"를 세는 케이스(상한 강제·로그아웃 감사)가 앞 파일 몫까지
    같이 세서 원인 모를 어긋남으로 깨진다.
    """
    assert _planted, "오염원 훅이 안 돌았다"
    assert await _count(AuthSession) == 0, (
        "앞 케이스가 남긴 세션 행이 그대로다 — conftest _clean이 auth_session을 안 비운다"
    )


async def test_초기화_훅이_로그인_잠금을_푼다():
    """`reset_auth_state()` 호출이 빠지면 여기가 빨개진다.

    인메모리라 TRUNCATE로 안 지워지는 상태다. 5회 실패로 잠긴 아이디가 다음 케이스로 새면
    정상 로그인이 429가 된다 — 셔틀 쿨다운 누출과 똑같은 계열이다.
    """
    assert _planted, "오염원 훅이 안 돌았다"
    assert auth.login_lock_remaining(WITNESS_LOCKED_USERNAME) == 0.0, (
        "앞 케이스가 남긴 로그인 잠금이 그대로다 — conftest _clean이 "
        "reset_auth_state()를 안 부른다"
    )


async def test_초기화_함수가_실제로_잠금_표를_비운다():
    """훅이 부르는 함수 자체의 계약을 따로 못박는다.

    위 케이스들은 "이 케이스 앞에 비워졌다"까지만 본다. 훅이 지나간 뒤 케이스 안에서 새로
    찍힌 잠금은 그대로 남아야 정상이라, 초기화 함수의 계약은 따로 본다
    (`test_shuttle_cooldown_hygiene.py`의 마지막 케이스와 같은 짝).
    """
    for _ in range(auth.get_settings().login_fail_max_attempts):
        auth.record_login_failure("inside-this-case")
    assert auth.login_lock_remaining("inside-this-case") > 0

    auth.reset_auth_state()
    assert auth.login_lock_remaining("inside-this-case") == 0.0
