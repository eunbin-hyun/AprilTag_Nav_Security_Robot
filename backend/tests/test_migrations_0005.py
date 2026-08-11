"""마이그레이션 0005(사건 수명주기) 왕복 + 예전 행 backfill 실측.

conftest.py의 세션 픽스처는 SQLAlchemy `create_all`로 스키마를 바로 만들어서 alembic
마이그레이션 자체는 pytest 그물 밖이다. 이 파일은 전용 임시 DB(`MIG_DB_NAME`)에서 alembic을
실제로 왕복시켜 세 가지를 본다.

① 0004까지 올린 DB에 실서버와 같은 모양의 경고 세 종류를 심고, 0005로 올렸을 때 각각 어느
   상태로 읽히는지 — 특히 "신원까지 적혔는데 ack=false"인 행(실서버 id 18)이 IDENTIFIED로
   읽히는지.
② upgrade가 예전 칸(ack)을 한 행도 안 바꾸는지 — 예전 조작 기록을 사후에 고쳐 쓰면
   감사 기록이 거짓이 된다.
③ downgrade가 칸을 실제로 떼고, 다시 head로 올렸을 때 alert 스키마가 글자 하나까지 같은지.

⚠ 이 파일은 async 시험이 아니다 — alembic.command가 내부에서 `asyncio.run()`을 부르므로
세션 이벤트 루프 위에서 부르면 죽는다(test_migrations_roundtrip.py 머리 주석과 같은 이유).
DB 이름도 그 파일과 안 겹치게 접두어를 따로 뒀다("mig5" 대 "mig") — 같은 이름을 쓰면 두
파일이 서로의 DB를 DROP/CREATE로 밟는다. 이름을 만드는 규칙과 `WITH (FORCE)`를 뺀 이유는
conftest.temp_migration_db_name과 test_migrations_roundtrip.py의 `_create_mig_db` 주석에 있다.
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

# 0005가 alert에 붙이는 수명주기 칸.
_LIFECYCLE_COLUMNS = (
    "status",
    "acked_at",
    "acked_by",
    "resolved_at",
    "resolved_by",
    "resolution",
)
_PRE_LIFECYCLE_REVISION = "1de4711d0004"


def _base_test_url() -> str:
    # 잣대는 conftest 한 자리다 — `DATABASE_URL`이 먼저고 없으면 `TEST_DATABASE_URL`이다.
    # 여기서 `TEST_DATABASE_URL`만 읽으면 본 시험과 임시 DB가 다른 서버로 갈린다
    # (2026-08-04 전체검토 L33).
    return resolved_test_database_url()


def _admin_db_name() -> str:
    # 조립·분해는 conftest 헬퍼가 한다(쿼리스트링 보존, 2026-08-02 백지 검토 C11).
    return database_name(_base_test_url())


def _sqlalchemy_url(db_name: str) -> str:
    return swap_database_name(_base_test_url(), db_name)


def _asyncpg_dsn(db_name: str) -> str:
    return _sqlalchemy_url(db_name).replace("postgresql+asyncpg://", "postgresql://")


# 임시 DB 이름. 관리 커넥션이 붙는 시험 DB 이름에서 파생되므로, 시험 DB를 다르게 잡은
# 프로세스끼리 절대 같은 이름을 안 밟는다. 접두어 "mig5"가 test_migrations_roundtrip.py("mig")와
# 갈라 준다.
MIG_DB_NAME = temp_migration_db_name(
    "mig5", _admin_db_name(), os.environ.get("PYTEST_XDIST_WORKER", "main")
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


async def _fetch_alerts() -> list[dict]:
    conn = await asyncpg.connect(_asyncpg_dsn(MIG_DB_NAME))
    try:
        rows = await conn.fetch(
            "SELECT id, ack, status, acked_at, identified_at FROM alert ORDER BY id"
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def _fetch_ack_only() -> list[dict]:
    """수명주기 칸이 없는 상태(0004)에서 읽는다."""
    conn = await asyncpg.connect(_asyncpg_dsn(MIG_DB_NAME))
    try:
        rows = await conn.fetch(
            "SELECT id, ack, identified_at FROM alert ORDER BY id"
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def _inspect_alert_table() -> dict[str, tuple]:
    engine = create_async_engine(_sqlalchemy_url(MIG_DB_NAME))
    try:
        async with engine.connect() as conn:

            def _read(sync_conn):
                insp = inspect(sync_conn)
                return {
                    c["name"]: (str(c["type"]), bool(c["nullable"]), str(c.get("default")))
                    for c in insp.get_columns("alert")
                }

            return await conn.run_sync(_read)
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


# 실서버 경고 18건과 같은 모양 세 가지를 심는다.
#  id 1 — 손도 안 댄 미태깅 경고(실서버 17건이 이 모양)
#  id 2 — 확인만 누른 경고
#  id 3 — 신원까지 적혔는데 ack=false (실서버 id 18. C10이 지목한 그 모순 상태)
_SEED = [
    "INSERT INTO alert (id, type, severity, source_type, source_id, ack)"
    " VALUES (1, 'untagged', 'high', 'gate_pass', 11, false)",
    "INSERT INTO alert (id, type, severity, source_type, source_id, ack)"
    " VALUES (2, 'untagged', 'high', 'gate_pass', 12, true)",
    "INSERT INTO alert (id, type, severity, source_type, source_id, ack,"
    " identified_person, identified_by, identified_at)"
    " VALUES (3, 'untagged', 'high', 'gate_pass', 13, false,"
    " '가명-1', '요원A', '2026-07-29 10:00:00+00')",
]


def test_0005가_예전_경고를_상태로_읽어낸다():
    """실서버와 같은 모양 세 행이 각각 OPEN·ACKED·IDENTIFIED로 읽혀야 한다."""
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()
                command.upgrade(cfg, _PRE_LIFECYCLE_REVISION)
                asyncio.run(_run_sql(_SEED))

                # 심은 뒤 0005 전 상태 확인 — id 3이 "신원 있는데 미확인"이다.
                before = asyncio.run(_fetch_ack_only())
                assert [(r["id"], r["ack"]) for r in before] == [(1, False), (2, True), (3, False)]
                assert before[2]["identified_at"] is not None

                command.upgrade(cfg, "head")
                rows = asyncio.run(_fetch_alerts())
                by_id = {r["id"]: r for r in rows}

                assert by_id[1]["status"] == "OPEN"
                assert by_id[1]["ack"] is False
                assert by_id[2]["status"] == "ACKED"
                assert by_id[3]["status"] == "IDENTIFIED"
                # ack는 한 행도 안 바뀐다 — 예전 조작 기록을 사후에 고쳐 쓰지 않는다.
                assert [(r["id"], r["ack"]) for r in rows] == [
                    (1, False), (2, True), (3, False)
                ], "마이그레이션이 예전 ack 값을 고쳤다"

                # 예전 행의 확인 시각은 모르니 NULL로 둔다(없는 시각을 지어내지 않는다).
                assert all(r["acked_at"] is None for r in rows)
        finally:
            asyncio.run(_drop_mig_db())


def test_0005_왕복이_예전_칸을_안_다친다():
    """downgrade가 새 칸만 떼고 예전 칸(ack·신원)은 그대로 둬야 한다."""
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()
                command.upgrade(cfg, _PRE_LIFECYCLE_REVISION)
                asyncio.run(_run_sql(_SEED))
                command.upgrade(cfg, "head")
                at_head = asyncio.run(_inspect_alert_table())
                for name in _LIFECYCLE_COLUMNS:
                    assert name in at_head, f"alert.{name}이 head 뒤에도 없음"

                # 0005 뒤에 사람이 실제로 확인 처리한 행을 하나 만든다(acked_at이 채워진다).
                asyncio.run(
                    _run_sql([
                        "INSERT INTO alert (id, type, severity, ack, status, acked_at,"
                        " identified_person, identified_at)"
                        " VALUES (4, 'untagged', 'high', true, 'IDENTIFIED',"
                        " '2026-07-30 01:00:00+00', '가명-2', '2026-07-30 01:05:00+00')"
                    ])
                )

                command.downgrade(cfg, _PRE_LIFECYCLE_REVISION)
                at_0004 = asyncio.run(_inspect_alert_table())
                for name in _LIFECYCLE_COLUMNS:
                    assert name not in at_0004, f"alert.{name}이 downgrade 뒤에도 남음"
                assert "ack" in at_0004 and "identified_person" in at_0004

                rows = asyncio.run(_fetch_ack_only())
                by_id = {r["id"]: r for r in rows}
                # ack 값 넷이 전부 심은 그대로 남아야 한다(왕복이 조작 기록을 안 바꾼다).
                assert [(r["id"], r["ack"]) for r in rows] == [
                    (1, False), (2, True), (3, False), (4, True)
                ]
                # 신원 원문도 그대로 있다.
                assert by_id[3]["identified_at"] is not None

                command.upgrade(cfg, "head")
                again = asyncio.run(_inspect_alert_table())
                assert again == at_head, "왕복 뒤 alert 스키마가 달라졌다"
        finally:
            asyncio.run(_drop_mig_db())


def test_0005가_status_인덱스를_붙인다():
    """상태 필터가 붙는 자리라 인덱스가 있어야 한다. 이름은 SQLAlchemy 규칙과 같게 맞췄다."""
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()
                command.upgrade(cfg, "head")

                async def _idx() -> set[str]:
                    engine = create_async_engine(_sqlalchemy_url(MIG_DB_NAME))
                    try:
                        async with engine.connect() as conn:
                            return await conn.run_sync(
                                lambda sc: {
                                    ix["name"] for ix in inspect(sc).get_indexes("alert")
                                }
                            )
                    finally:
                        await engine.dispose()

                assert "ix_alert_status" in asyncio.run(_idx())
        finally:
            asyncio.run(_drop_mig_db())
