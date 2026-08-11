"""마이그레이션 0006(그날 기본 목적지 표) 왕복 실측.

conftest.py의 세션 픽스처는 SQLAlchemy `create_all`로 스키마를 바로 만들어서 alembic
마이그레이션 자체는 pytest 그물 밖이다. 이 파일은 전용 임시 DB에서 alembic을 실제로
왕복시켜 셋을 본다.

① 0005까지 올린 DB에는 표가 없고, head로 올리면 칸 아홉이 생긴다.
② `service_date` UNIQUE가 진짜로 걸려 있다 — "자동 조회는 행이 없을 때만 만든다"는 규칙이
   그 인덱스 위에서 도는지라(ON CONFLICT DO NOTHING), 인덱스가 없으면 같은 날 행이 둘
   생기고 기본 목적지가 두 개가 된다.
③ downgrade가 표를 실제로 떼고, 다시 head로 올리면 스키마가 글자 하나까지 같다. 다른 표는
   안 다친다.

⚠ 이 파일은 async 시험이 아니다 — alembic.command가 내부에서 `asyncio.run()`을 부르므로
세션 이벤트 루프 위에서 부르면 죽는다(test_migrations_roundtrip.py 머리 주석과 같은 이유).
DB 이름 접두어도 "mig6"로 따로 뒀다 — 같은 이름을 쓰면 두 파일이 서로의 DB를 밟는다.
"""
import asyncio
import contextlib
import os
import pathlib

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from tests.conftest import (
    database_name,
    resolved_test_database_url,
    swap_database_name,
    temp_migration_db_name,
)

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

_TABLE = "daily_default_destination"
_COLUMNS = (
    "id",
    "service_date",
    "destination",
    "source",
    "weather_status",
    "weather_desc",
    "weather_ok",
    "decided_at",
    "updated_at",
)
_DATE_INDEX = "ix_daily_default_destination_service_date"
# 0006 바로 앞 리비전. 여기까지 내렸다가 다시 올려서 스키마가 같은지 본다.
_PRE_DEFAULT_REVISION = "c10life0005"


def _base_test_url() -> str:
    """본 시험과 **같은 잣대**로 서버 접속 정보를 얻는다 (2026-08-04 전체검토 L33).

    예전에는 여기서 `TEST_DATABASE_URL`만 읽었다. conftest는 `DATABASE_URL`을 먼저 보므로,
    그쪽만 잡고 돌리면 본 시험과 임시 DB가 서로 다른 서버로 갈렸다.
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


MIG_DB_NAME = temp_migration_db_name(
    "mig6", _admin_db_name(), os.environ.get("PYTEST_XDIST_WORKER", "main")
)


async def _create_mig_db() -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(_admin_db_name()))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{MIG_DB_NAME}"')
        await conn.execute(f'CREATE DATABASE "{MIG_DB_NAME}"')
    finally:
        await conn.close()


async def _drop_mig_db() -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(_admin_db_name()))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{MIG_DB_NAME}"')
    finally:
        await conn.close()


async def _run_sql(statements: list[str]) -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(MIG_DB_NAME))
    try:
        for sql in statements:
            await conn.execute(sql)
    finally:
        await conn.close()


async def _insert_twice_same_day() -> str:
    """같은 날짜로 두 번 넣어 본다. UNIQUE가 있으면 두 번째가 거절된다."""
    conn = await asyncpg.connect(_asyncpg_dsn(MIG_DB_NAME))
    try:
        await conn.execute(
            f"INSERT INTO {_TABLE} (service_date, destination, source)"
            " VALUES ('2026-07-31', 'OUTDOOR_TAGGING', 'auto')"
        )
        try:
            await conn.execute(
                f"INSERT INTO {_TABLE} (service_date, destination, source)"
                " VALUES ('2026-07-31', 'INDOOR_TAGGING', 'auto')"
            )
        except asyncpg.UniqueViolationError:
            return "rejected"
        return "accepted"
    finally:
        await conn.close()


async def _inspect_tables() -> set[str]:
    engine = create_async_engine(_sqlalchemy_url(MIG_DB_NAME))
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda sc: set(inspect(sc).get_table_names()))
    finally:
        await engine.dispose()


async def _inspect_default_table() -> dict[str, tuple]:
    """비교 가능한 꼴로 읽는다(타입 객체를 그대로 견주면 인스턴스가 달라 안 맞는다)."""
    engine = create_async_engine(_sqlalchemy_url(MIG_DB_NAME))
    try:
        async with engine.connect() as conn:

            def _read(sync_conn):
                insp = inspect(sync_conn)
                return {
                    c["name"]: (str(c["type"]), bool(c["nullable"]), str(c.get("default")))
                    for c in insp.get_columns(_TABLE)
                }

            return await conn.run_sync(_read)
    finally:
        await engine.dispose()


async def _inspect_indexes() -> set[str]:
    engine = create_async_engine(_sqlalchemy_url(MIG_DB_NAME))
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(
                lambda sc: {ix["name"] for ix in inspect(sc).get_indexes(_TABLE)}
            )
    finally:
        await engine.dispose()


@contextlib.contextmanager
def _database_url_pointed_at_mig_db():
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", _sqlalchemy_url(MIG_DB_NAME))
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


@contextlib.contextmanager
def _preserve_event_loop_pointer():
    """asyncio.run()이 지우는 '현재 이벤트 루프' 포인터를 되돌린다.

    안 되돌리면 뒤따르는 async 시험이 전부 "There is no current event loop"로 죽는다
    (test_migrations_roundtrip.py가 실측으로 잡은 함정이라 같은 방식을 쓴다).
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


def test_0006이_표를_붙이고_뗀다():
    """0005엔 표가 없고, head엔 칸 아홉이 있고, 내리면 표째로 떨어진다."""
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()

                command.upgrade(cfg, _PRE_DEFAULT_REVISION)
                assert _TABLE not in asyncio.run(_inspect_tables()), (
                    "0005 시점에 이미 표가 있다 — 리비전 사슬이 어긋났다"
                )

                command.upgrade(cfg, "head")
                at_head = asyncio.run(_inspect_default_table())
                for name in _COLUMNS:
                    assert name in at_head, f"{_TABLE}.{name}이 head 뒤에도 없음"
                # 날씨 칸 셋은 nullable이다 — 요원이 손으로 정한 행은 날씨 근거가 없다.
                for name in ("weather_status", "weather_desc", "weather_ok", "updated_at"):
                    assert at_head[name][1] is True, f"{name}이 nullable이 아니다"
                # 결정 자체는 비면 안 된다.
                for name in ("service_date", "destination", "source"):
                    assert at_head[name][1] is False, f"{name}이 nullable이다"

                command.downgrade(cfg, _PRE_DEFAULT_REVISION)
                tables = asyncio.run(_inspect_tables())
                assert _TABLE not in tables, "downgrade 뒤에도 표가 남았다"
                # 다른 표는 안 다친다.
                assert {"alert", "shuttle_arrival", "robot"} <= tables

                command.upgrade(cfg, "head")
                again = asyncio.run(_inspect_default_table())
                assert again == at_head, "왕복 뒤 스키마가 달라졌다"
        finally:
            asyncio.run(_drop_mig_db())


def test_0006이_날짜_UNIQUE를_건다():
    """하루에 행 하나. 이 제약이 "자동 조회는 행이 없을 때만 만든다"의 잠금 장치다.

    UNIQUE가 없으면 같은 날 행이 둘 생기고, 요원이 고른 값과 날씨가 정한 값이 나란히
    살아남아 어느 쪽이 기본인지 서버가 못 정한다.
    """
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                command.upgrade(_alembic_config(), "head")
                assert _DATE_INDEX in asyncio.run(_inspect_indexes())
                assert asyncio.run(_insert_twice_same_day()) == "rejected", (
                    "같은 날짜 행이 두 번 들어갔다 — UNIQUE가 안 걸렸다"
                )
        finally:
            asyncio.run(_drop_mig_db())
