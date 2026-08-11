"""alembic 마이그레이션 왕복 안전망 — 0002·0003(room_id·boot_id 초안), 0004(경고 신원 칸),
0007(통과 행의 카드 UID 칸).

conftest.py의 세션 픽스처는 SQLAlchemy Base.metadata.create_all로 스키마를 바로 만들어서
alembic 마이그레이션 자체는 pytest 그물 밖이다(어제 검수가 잡은 잔여 위험, S15P11C207-186
코멘트). 이 파일은 conftest의 create_all 픽스처와 완전히 분리된 전용 DB(MIG_DB_NAME)에서
alembic upgrade/downgrade를 실제로 왕복시켜, 0002가 붙이는 room_id·0003이 붙이는 boot_id·
0004가 alert에 붙이는 신원 칸 4개가 진짜로 붙고 떨어지는지 inspector로 확인한다.

conftest의 시험 DB와 안 겹치게 임시 DB를 시험 시작에 새로 만들고 끝나면 지운다(같은 서버
안). 이름은 conftest.temp_migration_db_name이 (접두어 "mig", 시험 DB 이름, xdist 워커)에서
파생시킨다 — 그 함수 docstring에 왜 시험 DB 이름까지 넣어야 하는지(안 넣었을 때의 실측
재현)가 있다. 이 파일은 async 테스트가 아니다 — alembic.command가 부르는 env.py가
내부에서 asyncio.run()을 직접 호출하므로, 세션 이벤트 루프(pytest-asyncio loop_scope=session) 위에서
부르면 "asyncio.run() cannot be called from a running event loop"로 죽는다. 그래서
pytestmark 없이 평범한 sync 테스트로 두고, 우리가 필요한 비동기 작업(DB 생성·삭제·inspector
조회)도 각각 asyncio.run()으로 따로 돈다(순차 호출이라 겹치지 않는다). test_config_guard.py가
같은 방식으로 이미 이 스위트 안에서 sync 테스트를 쓰고 있다.
"""
import asyncio
import contextlib
import os
import pathlib

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
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
_TABLES = ("tagging_event", "gate_pass_event", "shuttle_arrival")

# 0004가 alert에 붙이는 사후 신원 확인 칸. 전부 nullable이라 이미 쌓인 경고 행이 안 다친다.
_ALERT_IDENTITY_COLUMNS = (
    "identified_person",
    "identified_by",
    "identified_at",
    "identify_note",
)
# 0004 바로 앞 리비전. 여기까지 내렸다가 다시 올려서 스키마가 같은지 본다.
_PRE_IDENTITY_REVISION = "b007id000003"

# 0007이 gate_pass_event에 붙이는 카드 UID 칸(S15P11C207-241)과 그 바로 앞 리비전.
_GATE_PASS_TAG_COLUMN = "tag_id"
_PRE_GATE_PASS_TAG_REVISION = "d15p0006dest"


def _base_test_url() -> str:
    """conftest.py와 같은 기준으로 서버 접속 정보를 얻는다(DB 이름만 갈아 낀다).

    ⚠ 2026-08-04 전체검토 L33 — 예전에는 여기서 `TEST_DATABASE_URL`만 읽었는데 conftest는
    `DATABASE_URL`을 먼저 본다. "같은 기준"이라 적어 놓고 잣대가 갈려 있던 자리라 이제 그
    함수를 그대로 부른다.
    """
    return resolved_test_database_url()


def _admin_db_name() -> str:
    """관리 커넥션(CREATE/DROP DATABASE)이 붙을 DB 이름.

    ⚠ 조립·분해를 conftest 헬퍼에 맡긴다. 예전엔 여기서 `rpartition("/")`로 잘랐는데,
      DSN에 `?ssl=require`가 붙으면 DB 이름이 `c207_test?ssl=require`가 돼서 그대로
      `CREATE DATABASE`에 실렸다(2026-08-02 백지 검토 C11).
    """
    return database_name(_base_test_url())


def _sqlalchemy_url(db_name: str) -> str:
    return swap_database_name(_base_test_url(), db_name)


def _asyncpg_dsn(db_name: str) -> str:
    return _sqlalchemy_url(db_name).replace("postgresql+asyncpg://", "postgresql://")


# 임시 DB 이름. 관리 커넥션이 붙는 시험 DB 이름에서 파생되므로, 시험 DB를 다르게 잡은
# 프로세스끼리 절대 같은 이름을 안 밟는다. 접두어 "mig"가 test_migrations_0005.py("mig5")와
# 갈라 준다.
MIG_DB_NAME = temp_migration_db_name(
    "mig", _admin_db_name(), os.environ.get("PYTEST_XDIST_WORKER", "main")
)


def _pre_room_id_revision() -> str:
    """0002 이전(=0001) 리비전 id. alembic ScriptDirectory에서 base 리비전을 실물로 읽는다.

    0001이 down_revision=None인 유일한 base라 get_base()로 정확히 그 id가 나온다.
    """
    cfg = _alembic_config()
    return ScriptDirectory.from_config(cfg).get_base()


async def _create_mig_db() -> None:
    # 관리 커넥션은 TEST_DATABASE_URL이 가리키는 기존 DB로 붙어서 CREATE DATABASE만
    # 낸다. 이 커넥션 자체는 임시 DB를 안 쓴다.
    #
    # ⚠ 두 DROP에서 `WITH (FORCE)`를 뺐다. FORCE는 그 DB에 붙은 **남의 세션을 끊고** 지운다.
    #   이름이 프로세스마다 안 갈리던 때 그게 실제로 남의 시험을 죽였다 — 동시 실행 재현에서
    #   상대 프로세스가 `ConnectionDoesNotExistError: connection was closed in the middle of
    #   operation`으로 깨졌다. 이제 이름이 우리 설정에서만 나오니 끊을 남이 없고, FORCE를 뺐으니
    #   훗날 이름 파생이 깨져도 남을 조용히 죽이는 대신 "being accessed by other users"로
    #   시끄럽게 실패한다. 앞선 실행이 깨진 채 남긴 DB는 그 프로세스와 함께 커넥션도 죽어서
    #   FORCE 없이 지워진다(alembic env.py도 NullPool로 열고 dispose까지 한다).
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


@contextlib.contextmanager
def _database_url_pointed_at_mig_db():
    """DATABASE_URL을 임시 DB로 잠깐 돌린다. env+lru_cache 재구성이라 되돌리기 확실하다.

    app.db.engine은 conftest 시점에 이미 관리자 DB로 고정돼 있어 여기서 안 건드린다 —
    alembic env.py가 매 호출마다 get_settings().database_url을 새로 읽어서 자기 엔진을
    새로 만드는 구조라, 우리는 env만 바꿔치면 된다.
    """
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
    """asyncio.run()은 끝나면 스레드의 '현재 이벤트 루프' 포인터를 None으로 지운다.

    alembic.command.upgrade/downgrade가 부르는 env.py가 내부에서 asyncio.run()을 쓰고,
    우리도 CREATE/DROP DATABASE·inspector 조회에 asyncio.run()을 쓴다. 그 부작용으로
    pytest-asyncio가 세션 스코프(loop_scope=session)로 잡아둔 루프의 '현재' 지정이
    날아가서, 뒤따르는 비동기 시험이 전부 "There is no current event loop"로 죽는 걸
    실측으로 재현했다(1라운드 56실패). 세션 루프 객체 자체는 안 닫히니 포인터만
    원래대로 돌려주면 된다.
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


async def _inspect_mig_db() -> dict[str, dict]:
    """임시 DB의 컬럼·인덱스 실물을 inspector로 읽는다."""
    engine = create_async_engine(_sqlalchemy_url(MIG_DB_NAME))
    try:
        async with engine.connect() as conn:

            def _read(sync_conn):
                insp = inspect(sync_conn)
                out = {}
                for table in _TABLES:
                    cols = {c["name"]: c for c in insp.get_columns(table)}
                    idx_names = {ix["name"] for ix in insp.get_indexes(table)}
                    out[table] = {"columns": cols, "indexes": idx_names}
                return out

            return await conn.run_sync(_read)
    finally:
        await engine.dispose()


async def _inspect_alert_table() -> dict[str, tuple]:
    """alert 테이블 스키마를 비교 가능한 꼴로 읽는다.

    inspector가 주는 dict를 그대로 비교하면 안 된다 — 'type' 값이 SQLAlchemy 타입 객체라
    같은 VARCHAR(64)라도 인스턴스가 달라 dict끼리 안 맞는다. 이름·타입 표기·nullable·기본값만
    문자열로 눌러서 비교한다.
    """
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


def test_alembic_head_roundtrip_room_and_boot_id():
    """빈 DB에서 head까지 올리고, 0001까지 내려서 되돌아가는지 보고, 다시 head로 올린다."""
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()

                # 1) 빈 DB → head. room_id(0002)·boot_id(0003)가 세 테이블 다 붙어야 한다.
                command.upgrade(cfg, "head")
                state = asyncio.run(_inspect_mig_db())
                for table in _TABLES:
                    cols = state[table]["columns"]
                    assert "room_id" in cols, f"{table}.room_id가 upgrade head 뒤에도 없음"
                    assert cols["room_id"]["nullable"] is True
                    assert "boot_id" in cols, f"{table}.boot_id가 upgrade head 뒤에도 없음"
                    assert cols["boot_id"]["nullable"] is True
                    assert f"ix_{table}_room_id" in state[table]["indexes"]

                # 2) 0002 이전으로 downgrade. 두 컬럼 다 실제로 떨어져야 한다.
                command.downgrade(cfg, _pre_room_id_revision())
                state = asyncio.run(_inspect_mig_db())
                for table in _TABLES:
                    cols = state[table]["columns"]
                    assert "room_id" not in cols, f"{table}.room_id가 downgrade 뒤에도 남음"
                    assert "boot_id" not in cols, f"{table}.boot_id가 downgrade 뒤에도 남음"

                # 3) 다시 head로. downgrade가 재-upgrade를 막지 않는지 확인한다.
                command.upgrade(cfg, "head")
                state = asyncio.run(_inspect_mig_db())
                for table in _TABLES:
                    cols = state[table]["columns"]
                    assert "room_id" in cols
                    assert "boot_id" in cols
        finally:
            asyncio.run(_drop_mig_db())


def test_alembic_0004_roundtrip_alert_identity():
    """0004(경고 신원 칸) 왕복. head → 0003 → head를 돌고 스키마가 같은지 본다.

    0002·0003 왕복과 DB를 나눠 쓰지 않고 같은 임시 DB를 쓴다 — 만들고 지우는 걸
    케이스 안에서 닫으니 서로 안 밟는다. 여기서 확인하는 건 세 가지다.
      ① head에 칸 4개가 붙고 전부 nullable이다(기존 행이 안 다친다).
      ② 0003으로 내리면 4개가 실제로 떨어진다.
      ③ 다시 head로 올리면 내리기 전 alert 스키마와 글자 하나까지 같다.
    """
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()

                command.upgrade(cfg, "head")
                at_head = asyncio.run(_inspect_alert_table())
                for name in _ALERT_IDENTITY_COLUMNS:
                    assert name in at_head, f"alert.{name}이 upgrade head 뒤에도 없음"
                    assert at_head[name][1] is True, f"alert.{name}이 nullable이 아니다"

                command.downgrade(cfg, _PRE_IDENTITY_REVISION)
                at_0003 = asyncio.run(_inspect_alert_table())
                for name in _ALERT_IDENTITY_COLUMNS:
                    assert name not in at_0003, f"alert.{name}이 downgrade 뒤에도 남음"
                # 신원 칸만 떨어지고 원래 칸은 그대로여야 한다.
                assert "ack" in at_0003 and "created_at" in at_0003

                command.upgrade(cfg, "head")
                again = asyncio.run(_inspect_alert_table())
                assert again == at_head, "왕복 뒤 alert 스키마가 달라졌다"
        finally:
            asyncio.run(_drop_mig_db())


def test_alembic_0007_roundtrip_gate_pass_tag_id():
    """0007(통과 행의 카드 UID 칸) 왕복. head → 0006 → head를 돌고 칸이 붙고 떨어지는지 본다.

    추가형만 쓰기로 한 자리라(배포 창 위험) 보는 것도 셋으로 좁다.
      ① head에 `gate_pass_event.tag_id`가 붙고 **nullable**이다 — 이미 쌓인 통과 행이
         안 다치고, 크레딧을 안 태운 판정이 NULL로 남는 계약이 스키마에서 받쳐진다.
      ② 0006으로 내리면 그 칸만 떨어진다(옆 칸은 그대로).
      ③ 다시 head로 올라간다 — downgrade가 재-upgrade를 막지 않는다.
    """
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db():
                cfg = _alembic_config()

                command.upgrade(cfg, "head")
                cols = asyncio.run(_inspect_mig_db())["gate_pass_event"]["columns"]
                assert _GATE_PASS_TAG_COLUMN in cols, "0007 뒤에도 gate_pass_event.tag_id가 없음"
                assert cols[_GATE_PASS_TAG_COLUMN]["nullable"] is True

                command.downgrade(cfg, _PRE_GATE_PASS_TAG_REVISION)
                cols = asyncio.run(_inspect_mig_db())["gate_pass_event"]["columns"]
                assert _GATE_PASS_TAG_COLUMN not in cols, "downgrade 뒤에도 tag_id가 남음"
                # 옆 칸까지 같이 떨어지면 추가형 왕복이 아니다.
                assert "matched_tagging_event_id" in cols and "verdict" in cols

                command.upgrade(cfg, "head")
                cols = asyncio.run(_inspect_mig_db())["gate_pass_event"]["columns"]
                assert _GATE_PASS_TAG_COLUMN in cols
        finally:
            asyncio.run(_drop_mig_db())
