"""마이그레이션 0008(로그인) 왕복 — 계정·세션·명부 칸·감사 테이블이 붙고 떨어지나.

conftest의 세션 픽스처는 `Base.metadata.create_all`로 스키마를 바로 만들어서 alembic
자체는 pytest 그물 밖이다. 그래서 여기도 `test_migrations_roundtrip.py`와 같은 수법으로
**전용 임시 DB**를 만들어 실제로 upgrade/downgrade를 돌린다.

## 왜 이 파일이 따로 있나

0008은 앞선 마이그레이션들과 결이 다르다. 0004·0007은 칸 하나를 붙였다 떼는 추가형이라
왕복이 저절로 닫히는데, 0008은 **테이블 셋 + 칸 일곱 + FK + 유니크 인덱스**를 한 번에
얹는다. 내리는 순서가 하나만 틀려도(예: `app_user`를 `auth_session`보다 먼저) FK가 막아
downgrade가 통째로 선다. 그 순서를 실물로 재는 자리다.

⚠ 이 왕복은 **자료가 안 남는다.** 내리면 계정·세션·감사 기록이 통째로 사라진다 —
0004·0007 왕복과 다른 점이고, 되돌리기 전에 덤프를 뜨라는 근거다.

이 파일은 async 테스트가 아니다. alembic.command가 부르는 env.py가 내부에서
`asyncio.run()`을 직접 호출하므로 세션 이벤트 루프 위에서 부르면 죽는다
(`test_migrations_roundtrip.py` 머리에 그 실측이 적혀 있다).
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
from tests.conftest import database_name, swap_database_name, temp_migration_db_name
# 루프 포인터 보존은 계약이라 사본을 안 만들고 그대로 가져다 쓴다 — 두 파일이 각자 들면
# 한쪽만 고쳐지는 순간 다시 "There is no current event loop"로 뒤 시험이 통째로 깨진다.
from tests.test_migrations_roundtrip import _preserve_event_loop_pointer

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]

# 0008 바로 앞 리비전(= 0007 gate_pass tag_id). 여기까지 내렸다가 다시 올린다.
_PRE_AUTH_REVISION = "e241tag00007"

_NEW_TABLES = ("app_user", "auth_session", "staff_audit")
# staff에 붙는 칸 일곱. 전부 nullable이거나 server_default가 있어서 기존 행이 안 다친다.
_STAFF_NEW_COLUMNS = (
    "tag_id", "student_no", "rank", "is_active", "app_user_id",
    "created_at", "updated_at",
)
# 0008이 staff에 거는 FK 이름(마이그레이션의 `_STAFF_USER_FK`와 같은 값).
_STAFF_USER_FK = "fk_staff_app_user_id"


def _base_test_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL", "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test"
    )


def _admin_db_name() -> str:
    """관리 커넥션(CREATE/DROP DATABASE)이 붙을 DB 이름.

    ⚠ 조립·분해를 conftest 헬퍼에 맡긴다. 예전엔 여기서 `rpartition("/")`로 잘랐는데,
      DSN에 `?ssl=require`가 붙으면 DB 이름이 `c207_test?ssl=require`가 돼서 그대로
      `CREATE DATABASE`에 실렸다(2026-08-02 백지 검토 C11·X115). `test_migrations_roundtrip.py`가
      먼저 고쳐졌고 이 파일만 예전 방식이 남아 있었다.
    """
    return database_name(_base_test_url())


def _sqlalchemy_url(db_name: str) -> str:
    """DB 이름만 갈아 낀다. 쿼리스트링·프래그먼트는 원래 값 그대로 남는다."""
    return swap_database_name(_base_test_url(), db_name)


def _asyncpg_dsn(db_name: str) -> str:
    return _sqlalchemy_url(db_name).replace("postgresql+asyncpg://", "postgresql://")


# 접두어 "mig8"이 이 파일을 test_migrations_roundtrip("mig")·0005("mig5")·0006과 갈라 준다.
# 왜 시험 DB 이름까지 이름에 넣어야 하는지는 conftest.temp_migration_db_name docstring에 있다.
MIG_DB_NAME = temp_migration_db_name(
    "mig8", _admin_db_name(), os.environ.get("PYTEST_XDIST_WORKER", "main")
)

# 이 파일이 만드는 임시 DB로 DATABASE_URL을 돌리려면 위 헬퍼가 이 이름을 봐야 한다.
# test_migrations_roundtrip의 컨텍스트 매니저는 자기 모듈 상수를 보므로 그대로 못 쓴다.
_MIG_URL = _sqlalchemy_url(MIG_DB_NAME)


async def _create_mig_db() -> None:
    conn = await asyncpg.connect(_asyncpg_dsn(_admin_db_name()))
    try:
        # FORCE를 안 쓴다 — 이름이 우리 설정에서만 나오니 끊을 남이 없고, 훗날 이름 파생이
        # 깨져도 남을 조용히 죽이는 대신 시끄럽게 실패한다(roundtrip 파일과 같은 판단).
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


async def _seed_staff_row() -> int:
    """0008을 올리기 **전에** 명부 행을 하나 심는다. 0001이 만든 칸 둘만 쓴다."""
    conn = await asyncpg.connect(_asyncpg_dsn(MIG_DB_NAME))
    try:
        return await conn.fetchval(
            "INSERT INTO staff (name, role) VALUES ($1, $2) RETURNING id",
            "기존명부",
            "guard",
        )
    finally:
        await conn.close()


async def _read_staff_row(staff_id: int) -> dict | None:
    conn = await asyncpg.connect(_asyncpg_dsn(MIG_DB_NAME))
    try:
        row = await conn.fetchrow("SELECT * FROM staff WHERE id = $1", staff_id)
        return dict(row) if row is not None else None
    finally:
        await conn.close()


def _alembic_config() -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@contextlib.contextmanager
def _database_url_pointed_at_mig_db_named(url: str):
    """DATABASE_URL을 잠깐 이 파일의 임시 DB로 돌린다. env+lru_cache 재구성이라 되돌리기 확실하다.

    roundtrip 파일의 같은 이름 헬퍼는 그 모듈 상수(MIG_DB_NAME)를 보므로 그대로 못 쓴다.

    app.db.engine은 conftest 시점에 이미 고정돼 있어 안 건드린다 — alembic env.py가 매
    호출마다 `get_settings().database_url`을 새로 읽어 자기 엔진을 만드는 구조라 env만
    바꿔치면 된다.
    """
    mp = pytest.MonkeyPatch()
    mp.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    try:
        yield
    finally:
        mp.undo()
        get_settings.cache_clear()


def _columns(insp, table: str) -> dict:
    """칸 하나를 (타입 표기, nullable, server_default)로 눌러 읽는다.

    ⚠ **server_default를 같이 읽는 게 요점이다**(X103). 0008 머리가 "staff에 붙는 칸은
      nullable이거나 server_default가 있어서 이미 쌓인 행이 안 깨진다"를 계약으로 걸어
      뒀는데, 예전 판은 타입과 nullable만 봐서 `is_active`의 `true` 기본값이 통째로
      사라져도 초록이었다. 기본값이 없는 NOT NULL 칸을 붙이면 행이 하나라도 있는 DB에서
      upgrade가 그 자리에서 선다.

    inspector가 주는 `default`는 이미 문자열(`'true'`·`now()`)이거나 None이라 그대로 든다.
    """
    return {
        c["name"]: (str(c["type"]), bool(c["nullable"]), c.get("default"))
        for c in insp.get_columns(table)
    }


def _foreign_keys(insp, table: str) -> list:
    """FK를 비교 가능한 꼴로 읽는다.

    ⚠ 예전 판은 `get_foreign_keys`를 아예 안 불렀다(X103). FK는 0008 downgrade 순서를
      정하는 당사자고(`staff.app_user_id`·`auth_session.user_id`가 `app_user`를 문다),
      `ondelete`가 바뀌면 계정 하나를 지울 때 명부·세션·감사가 어떻게 되는지가 통째로
      바뀐다. 그런데 그 자리를 재는 눈이 없었다.

    `staff_audit.staff_id`에 FK가 **없어야 한다**는 계약(모듈 머리)도 이 목록으로 잰다 —
    붙어 있으면 목록에 한 줄이 더 생긴다.
    """
    return sorted(
        (
            fk.get("name"),
            tuple(fk["constrained_columns"]),
            fk["referred_table"],
            tuple(fk["referred_columns"]),
            (fk.get("options") or {}).get("ondelete"),
        )
        for fk in insp.get_foreign_keys(table)
    )


async def _inspect() -> dict:
    """임시 DB의 테이블·칸·인덱스·FK를 비교 가능한 꼴로 읽는다."""
    engine = create_async_engine(_MIG_URL)
    try:
        async with engine.connect() as conn:

            def _read(sync_conn):
                insp = inspect(sync_conn)
                tables = set(insp.get_table_names())
                out = {"tables": tables, "staff": {}, "indexes": {}, "fks": {}}
                out["staff"] = _columns(insp, "staff")
                out["indexes"]["staff"] = {
                    ix["name"]: bool(ix.get("unique")) for ix in insp.get_indexes("staff")
                }
                out["fks"]["staff"] = _foreign_keys(insp, "staff")
                for table in _NEW_TABLES:
                    if table in tables:
                        out[table] = _columns(insp, table)
                        out["indexes"][table] = {
                            ix["name"]: bool(ix.get("unique"))
                            for ix in insp.get_indexes(table)
                        }
                        out["fks"][table] = _foreign_keys(insp, table)
                return out

            return await conn.run_sync(_read)
    finally:
        await engine.dispose()


def test_alembic_0008_roundtrip_auth_login():
    """head → 0007 → head를 돌고 스키마가 글자 하나까지 같은지 본다.

      ① head에 테이블 셋이 서고 `staff`에 칸 일곱이 붙는다. 새 칸은 전부 nullable이거나
         server_default가 있어서 이미 쌓인 명부 행이 안 다친다.
      ② `staff.tag_id`는 **유니크 인덱스**다 — 태그 하나가 두 사람에 붙는 걸 DB가 막는다.
      ③ 0007로 내리면 테이블 셋과 칸 일곱이 실제로 떨어지고, 기존 칸(`name`·`role`)은 남는다.
         `staff.role`을 안 건드린다는 계약이 여기서 받쳐진다(§2.3).
      ④ 다시 head로 올라간다 — downgrade가 재-upgrade를 막지 않는다.
    """
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db_named(_MIG_URL):
                cfg = _alembic_config()

                # ① 빈 DB → head
                command.upgrade(cfg, "head")
                at_head = asyncio.run(_inspect())
                for table in _NEW_TABLES:
                    assert table in at_head["tables"], f"{table}이 upgrade head 뒤에도 없음"
                for name in _STAFF_NEW_COLUMNS:
                    assert name in at_head["staff"], f"staff.{name}이 안 붙음"
                # is_active·created_at은 NOT NULL인데 server_default가 있어서 기존 행이 산다.
                # ⭐ nullable만 보면 안 된다 — NOT NULL인데 기본값이 없으면 행이 하나라도
                #   있는 DB에서 upgrade가 그 자리에서 선다(X103).
                assert at_head["staff"]["is_active"][1] is False
                assert at_head["staff"]["is_active"][2] is not None, (
                    "is_active가 NOT NULL인데 server_default가 없다 — 기존 명부 행이 있으면 upgrade가 선다"
                )
                assert "true" in at_head["staff"]["is_active"][2].lower()
                assert at_head["staff"]["created_at"][1] is False
                assert at_head["staff"]["created_at"][2] is not None, (
                    "created_at이 NOT NULL인데 server_default가 없다"
                )
                assert "now()" in at_head["staff"]["created_at"][2].lower()
                # 나머지 칸은 nullable이라 기본값이 없어도 된다(=NULL로 들어간다).
                for name in ("tag_id", "student_no", "rank", "app_user_id", "updated_at"):
                    assert at_head["staff"][name][1] is True, f"staff.{name}이 nullable이 아니다"
                # 토큰 해시 칸은 sha256 hex 길이(64)여야 한다. 좁으면 조회가 통째로 어긋난다.
                assert at_head["auth_session"]["token_hash"][0] == "VARCHAR(64)"

                # ② 유니크 인덱스 셋
                assert at_head["indexes"]["staff"]["ix_staff_tag_id"] is True
                assert at_head["indexes"]["app_user"]["ix_app_user_username"] is True
                assert (
                    at_head["indexes"]["auth_session"]["ix_auth_session_token_hash"] is True
                )

                # ②-2 FK 넷. `ondelete`까지 잰다 — 계정 하나를 지웠을 때 명부·세션·감사가
                #     어떻게 되는지가 이 값 하나로 갈린다(X103).
                assert (
                    _STAFF_USER_FK,
                    ("app_user_id",),
                    "app_user",
                    ("id",),
                    "SET NULL",
                ) in at_head["fks"]["staff"], (
                    f"staff → app_user FK가 계약과 다르다: {at_head['fks']['staff']}"
                )
                assert [fk[1:] for fk in at_head["fks"]["auth_session"]] == [
                    (("user_id",), "app_user", ("id",), "CASCADE")
                ], "auth_session → app_user FK가 CASCADE가 아니다 — 계정을 지워도 세션이 남는다"
                # staff_audit은 actor_user_id **하나만** FK다. staff_id에 FK가 붙으면
                # 명부 행이 사라질 때 감사가 같이 끌려가거나 INSERT가 막힌다(모듈 머리).
                assert [fk[1:] for fk in at_head["fks"]["staff_audit"]] == [
                    (("actor_user_id",), "app_user", ("id",), "SET NULL")
                ], f"staff_audit FK가 계약과 다르다: {at_head['fks']['staff_audit']}"

                # ③ 0007로 내린다. FK 순서가 틀리면 여기서 선다.
                command.downgrade(cfg, _PRE_AUTH_REVISION)
                at_0007 = asyncio.run(_inspect())
                for table in _NEW_TABLES:
                    assert table not in at_0007["tables"], f"{table}이 downgrade 뒤에도 남음"
                for name in _STAFF_NEW_COLUMNS:
                    assert name not in at_0007["staff"], f"staff.{name}이 downgrade 뒤에도 남음"
                # 기존 칸은 그대로다 — `staff.role`은 0008이 안 건드린다는 계약(§2.3).
                assert "name" in at_0007["staff"] and "role" in at_0007["staff"]
                # FK도 같이 떨어진다. 남으면 재-upgrade가 중복 이름으로 선다.
                assert at_0007["fks"]["staff"] == [], (
                    f"staff FK가 downgrade 뒤에도 남음: {at_0007['fks']['staff']}"
                )

                # ④ 다시 head로. 스키마가 내리기 전과 같아야 한다.
                command.upgrade(cfg, "head")
                assert asyncio.run(_inspect()) == at_head, "왕복 뒤 스키마가 달라졌다"
        finally:
            asyncio.run(_drop_mig_db())


def test_alembic_0008_keeps_existing_staff_rows():
    """⭐ **이미 쌓인 명부 행이 있는 상태**에서 0008을 올린다(X103).

    위 왕복 시험은 빈 DB만 잰다. 그런데 0008 머리가 계약으로 건 문장은 빈 DB 얘기가
    아니다 — "staff에 붙는 칸은 nullable이거나 server_default가 있어서 이미 쌓인 행이
    0건이어도 있어도 안 깨진다"이다. 행이 0건일 때만 재면 그 계약의 절반이 무검증이고,
    기본값 없는 NOT NULL 칸을 하나 붙이는 순간 배포 DB에서만 upgrade가 선다.
    (지금 실서버 `staff`가 0행이라 배포가 조용히 지나갈 수 있는 만큼 더 필요한 그물이다.)

    재는 것 셋.
      ① 0007까지 올려 행을 하나 심고 head로 올려도 **upgrade가 안 선다**.
      ② 그 행이 그대로 살아 있고 기존 칸(`name`·`role`) 값이 안 바뀐다.
      ③ 새 칸이 기대한 값으로 채워진다 — `is_active`는 server_default로 True,
         `created_at`은 now()로 채워지고, nullable 칸 다섯은 NULL이다.

    끝에 downgrade도 한 번 태운다. 칸을 떼는 게 **행을 지우는 게 아니라는** 것까지 봐야
    "되돌려도 명부는 남는다"가 받쳐진다.
    """
    with _preserve_event_loop_pointer():
        asyncio.run(_create_mig_db())
        try:
            with _database_url_pointed_at_mig_db_named(_MIG_URL):
                cfg = _alembic_config()

                # ① 0008 직전까지만 올리고 기존 행을 심는다.
                command.upgrade(cfg, _PRE_AUTH_REVISION)
                staff_id = asyncio.run(_seed_staff_row())

                command.upgrade(cfg, "head")

                # ② 행이 그대로다.
                row = asyncio.run(_read_staff_row(staff_id))
                assert row is not None, "0008을 올리자 기존 명부 행이 사라졌다"
                assert row["name"] == "기존명부"
                assert row["role"] == "guard", "0008이 안 건드린다던 staff.role이 바뀌었다"

                # ③ 새 칸의 기본값.
                assert row["is_active"] is True, (
                    "server_default가 없어서 기존 행의 is_active가 안 채워졌다"
                )
                assert row["created_at"] is not None, "created_at이 now()로 안 채워졌다"
                for name in ("tag_id", "student_no", "rank", "app_user_id", "updated_at"):
                    assert row[name] is None, f"nullable 칸 staff.{name}에 값이 들어갔다: {row[name]!r}"

                # 칸을 떼는 건 행을 지우는 게 아니다.
                command.downgrade(cfg, _PRE_AUTH_REVISION)
                back = asyncio.run(_read_staff_row(staff_id))
                assert back is not None, "downgrade가 명부 행을 통째로 지웠다"
                assert back["name"] == "기존명부" and back["role"] == "guard"
        finally:
            asyncio.run(_drop_mig_db())
