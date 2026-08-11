"""모델 ↔ 마이그레이션 드리프트 그물 (2026-08-02 백지 검토 F42).

## 무엇을 잡는 시험인가

`conftest._schema`가 시험 스키마를 **`Base.metadata.create_all`로** 만든다. 그래서 시험은
"모델이 말하는 스키마"만 보고 돌고, **alembic 사슬이 만드는 진짜 배포 스키마는 아무도 안
본다.** 그 사이가 벌어져도 전량이 초록이다 — `app/models.py`에 칸 하나를 붙이고
마이그레이션을 안 쓰면, 로컬은 끝까지 통과하고 EC2에서만 `UndefinedColumnError`로 500이 난다.
젠킨스가 `alembic upgrade head`를 돌리고 컨테이너를 띄우니 배포는 성공으로 찍히고,
그 창구를 처음 누른 사람이 500을 맞는다.

`test_migrations_roundtrip.py`가 있지만 그건 **적어 둔 칸 몇 개**를 손으로 짚어서 왕복시키는
시험이다(0002·0003·0004·0007). 새로 붙는 칸은 거기 목록에 자동으로 안 들어간다 — 사람이
같이 안 적으면 그물이 안 늘어난다. 여기는 반대로 **전수**다. 짚는 목록이 없다.

## 어떻게 재나

빈 임시 DB에 `alembic upgrade head`를 돌린 뒤, 그 실물 스키마와 `Base.metadata`를 alembic의
autogenerate 비교기(`compare_metadata`)로 견준다. autogenerate가 "쓸 게 있다"고 말하면 그게
곧 드리프트다 — 마이그레이션을 새로 뜨면 나올 diff가 바로 그 목록이라서다.

## 시간 (⚠ 전량 배치가 길어지지 않게)

케이스가 **하나**다. DB를 한 번 만들고 `upgrade head` 한 번, 비교 한 번, DROP 한 번으로 끝난다
(실측 5초 안쪽). 임시 DB 이름은 `conftest.temp_migration_db_name`이 (접두어, 시험 DB 이름,
xdist 워커)에서 파생시켜서, 시험 DB를 다르게 잡은 프로세스끼리도 xdist 워커끼리도 안 밟는다.
접두어 `migdrift`가 `mig`(왕복)·`mig5`(0005)와 갈라 준다.

## 이 파일이 async가 아닌 이유

`alembic.command`가 부르는 `alembic/env.py`가 안에서 `asyncio.run()`을 직접 부른다. 세션
이벤트 루프 위에서 그걸 부르면 "asyncio.run() cannot be called from a running event loop"로
죽는다. 그래서 `test_migrations_roundtrip.py`와 같은 모양으로 평범한 sync 시험이고, 루프
포인터를 되돌리는 장치(`_preserve_event_loop_pointer`)도 그대로 쓴다.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import pathlib

import asyncpg
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db import Base
from tests.conftest import (
    database_name,
    resolved_test_database_url,
    swap_database_name,
    temp_migration_db_name,
)

import app.models  # noqa: F401  모델을 metadata에 등록한다(env.py와 같은 이유)

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

# alembic이 자기 자리 표시로 만드는 표. 모델에는 없는 게 정상이라 비교에서 뺀다.
_ALEMBIC_BOOKKEEPING = "alembic_version"


def _base_test_url() -> str:
    """conftest와 같은 기준으로 서버 접속 정보를 얻는다(DB 이름만 갈아 낀다).

    ⚠ 2026-08-04 전체검토 L33 — 예전에는 "같은 기준"이라 적어 놓고 `TEST_DATABASE_URL`만
    읽었다. conftest는 `DATABASE_URL`을 먼저 본다. 주석이 사실보다 앞서 있던 자리라
    이제 그 함수를 그대로 부른다.
    """
    return resolved_test_database_url()


def _admin_db_name() -> str:
    """⚠ `rpartition("/")`로 자르지 마라 (2026-08-02 백지 검토 C11 — L32에서 이 파일까지 닿았다).

    `...:5433/c207_test?ssl=require`처럼 파라미터가 붙으면 이름이 `c207_test?ssl=require`가
    되고, 그 문자열이 그대로 `CREATE DATABASE`와 관리 커넥션에 실린다.
    """
    return database_name(_base_test_url())


def _sqlalchemy_url(db_name: str) -> str:
    return swap_database_name(_base_test_url(), db_name)


def _asyncpg_dsn(db_name: str) -> str:
    return _sqlalchemy_url(db_name).replace("postgresql+asyncpg://", "postgresql://")


DRIFT_DB_NAME = temp_migration_db_name(
    "migdrift", _admin_db_name(), os.environ.get("PYTEST_XDIST_WORKER", "main")
)


async def _create_drift_db() -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(_admin_db_name()))
    try:
        # ⚠ `WITH (FORCE)`를 안 쓴다 — 이름 파생이 훗날 깨져도 남의 세션을 조용히 죽이는
        #   대신 "being accessed by other users"로 시끄럽게 실패하게 둔다
        #   (test_migrations_roundtrip.py에 그 실측 근거가 있다).
        await conn.execute(f'DROP DATABASE IF EXISTS "{DRIFT_DB_NAME}"')
        await conn.execute(f'CREATE DATABASE "{DRIFT_DB_NAME}"')
    finally:
        await conn.close()


async def _drop_drift_db() -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(_admin_db_name()))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{DRIFT_DB_NAME}"')
    finally:
        await conn.close()


@contextlib.contextmanager
def _database_url_pointed_at_drift_db():
    """DATABASE_URL을 임시 DB로 잠깐 돌린다.

    `app.db.engine`은 conftest 시점에 이미 시험 DB로 고정돼 있어 여기서 안 건드린다 —
    alembic env.py가 매 호출마다 `get_settings().database_url`을 새로 읽어 자기 엔진을
    만드는 구조라, env만 바꿔치면 된다.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", _sqlalchemy_url(DRIFT_DB_NAME))
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


@contextlib.contextmanager
def _preserve_event_loop_pointer():
    """`asyncio.run()`이 지우는 '현재 이벤트 루프' 포인터를 되돌린다.

    안 되돌리면 뒤따르는 async 시험이 전부 "There is no current event loop"로 죽는다
    (test_migrations_roundtrip.py가 1라운드 56실패로 실측했다).
    """
    try:
        saved = asyncio.get_event_loop()
    except RuntimeError:
        saved = None
    try:
        yield
    finally:
        asyncio.set_event_loop(saved)


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


async def _diff_against_models() -> list:
    """alembic이 만든 실물 스키마와 `Base.metadata`의 차이를 뽑는다.

    `compare_type`은 켠다 — 칸이 있고 없고만 보면 `String(16)`을 `String(64)`로 넓힌 변경이
    그대로 새서, 배포에서 `value too long for type character varying(16)`으로 터진다.
    `compare_server_default`는 안 켠다. Postgres가 돌려주는 기본값 표기가 SQLAlchemy 쪽 표기와
    글자로 안 맞는 일이 흔해서 가짜 diff가 쏟아지고, 그 축은 마이그레이션 왕복 시험이 이미 본다.
    """
    engine = create_async_engine(_sqlalchemy_url(DRIFT_DB_NAME))
    try:
        async with engine.connect() as conn:

            def _read(sync_conn):
                context = MigrationContext.configure(
                    sync_conn, opts={"compare_type": True}
                )
                return compare_metadata(context, Base.metadata)

            return await conn.run_sync(_read)
    finally:
        await engine.dispose()


def _flatten(diffs: list) -> list[tuple]:
    """`compare_metadata` 결과를 낱개 diff 튜플로 편다.

    칸 단위 변경은 표별로 묶인 리스트로 오고 표·인덱스 단위 변경은 튜플 하나로 온다.
    두 모양이 섞여 있어서, 거르고 사람이 읽게 적으려면 먼저 편다.
    """
    out: list[tuple] = []
    for entry in diffs:
        if isinstance(entry, list):
            out.extend(entry)
        else:
            out.append(entry)
    return out


def _mentions_bookkeeping(diff: tuple) -> bool:
    """alembic 자기 자리 표(`alembic_version`)에 대한 diff인가.

    그 표는 `Base.metadata`에 없는 게 정상이라 늘 "지워라"로 나온다 — 유일하게 미리 아는
    소음이라 여기서만 뺀다. 이름으로 거르는 이유는 diff 튜플 모양이 종류마다 달라서
    (표는 `Table` 객체, 칸은 `(op, schema, table_name, Column)`) 한 자리에서 꺼낼 칸이 없어서다.
    """
    return _ALEMBIC_BOOKKEEPING in repr(diff)


def _describe(diff: tuple) -> str:
    """diff 하나를 한 줄로. 터졌을 때 무엇을 마이그레이션에 적어야 하는지가 바로 보이게."""
    op = diff[0]
    rest = ", ".join(
        getattr(part, "name", None) or str(part) for part in diff[1:] if part is not None
    )
    return f"{op}: {rest}"


def test_alembic_스키마가_모델과_같다():
    """`alembic upgrade head` 결과와 `app/models.py`가 한 글자도 안 어긋난다.

    빨개지면 고칠 자리는 둘 중 하나다.
      ① 모델에 칸·표·인덱스를 붙이고 마이그레이션을 안 썼다 → `alembic revision`을 뜬다.
      ② 마이그레이션만 고치고 모델을 안 고쳤다 → 모델을 맞춘다.
    어느 쪽인지는 아래 실패 메시지의 `add_*`(모델에만 있다)·`remove_*`(DB에만 있다)로 갈린다.
    """
    with _preserve_event_loop_pointer():
        asyncio.run(_create_drift_db())
        try:
            with _database_url_pointed_at_drift_db():
                command.upgrade(_alembic_config(), "head")
                raw = asyncio.run(_diff_against_models())
        finally:
            asyncio.run(_drop_drift_db())

    drift = [d for d in _flatten(raw) if not _mentions_bookkeeping(d)]
    assert drift == [], (
        "alembic 사슬이 만든 스키마와 app/models.py가 어긋난다 — 이대로 배포하면 "
        "시험은 전량 초록인데 EC2에서 500이 난다.\n"
        + "\n".join(f"  - {_describe(d)}" for d in drift)
    )
