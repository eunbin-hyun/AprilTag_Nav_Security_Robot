"""임시 마이그레이션 DB 이름 규칙 회귀 시험.

예전 이름은 `f"mig_test_{워커}"`로 **시험 DB 이름을 아예 안 봤다.** 그래서 같은 서버에서
TEST_DATABASE_URL만 다르게 pytest 둘을 띄우면 양쪽이 같은 임시 DB를 DROP/CREATE로 밟았다
(실측 재현 — 한쪽 2실패·다른 쪽 4실패, `DuplicateDatabaseError: database "mig_test_main"
already exists`). 여기 검사가 그 규칙을 코드로 굳힌다.

⚠ 마지막 두 검사는 실제 시험 파일 둘이 만든 이름을 직접 본다. 순수 함수만 검사하면 한
파일이 함수를 안 쓰게 바뀌어도 초록이 나온다 — 계약을 지켜야 하는 자리는 부르는 쪽이다.
"""
from tests.conftest import _is_test_database_url, temp_migration_db_name

_PG_IDENT_MAX_BYTES = 63


def test_시험_DB가_다르면_임시_DB_이름도_갈린다():
    """이 파일의 존재 이유. 같은 서버·같은 워커라도 시험 DB가 다르면 이름이 달라야 한다."""
    a = temp_migration_db_name("mig", "c207_seam_test", "main")
    b = temp_migration_db_name("mig", "c207_seam_a_test", "main")

    assert a != b


def test_워커가_다르면_이름이_갈린다():
    gw0 = temp_migration_db_name("mig", "c207_test", "gw0")
    gw1 = temp_migration_db_name("mig", "c207_test", "gw1")

    assert gw0 != gw1


def test_접두어가_두_시험_파일을_갈라_준다():
    assert temp_migration_db_name("mig", "c207_test", "main") != temp_migration_db_name(
        "mig5", "c207_test", "main"
    )


def test_같은_입력이면_같은_이름이다():
    """재발행·재시도가 같은 DB를 집어야 한다(결정적이어야 한다)."""
    assert temp_migration_db_name("mig", "c207_test", "main") == temp_migration_db_name(
        "mig", "c207_test", "main"
    )


def test_이름이_conftest_시험DB_가드를_통과한다():
    """이 DB URL이 DATABASE_URL에 잠깐 들어가므로 가드를 통과해야 한다."""
    name = temp_migration_db_name("mig", "c207_test", "main")

    assert name.endswith("_test")
    assert _is_test_database_url(f"postgresql+asyncpg://u@127.0.0.1:5435/{name}")


def test_긴_시험DB_이름도_63바이트를_안_넘는다():
    """PostgreSQL은 식별자를 63바이트에서 조용히 자른다 — 자르기가 곧 이름 충돌이다."""
    long_a = "c207_" + "x" * 50 + "_aaa_test"
    long_b = "c207_" + "x" * 50 + "_bbb_test"

    name_a = temp_migration_db_name("mig5", long_a, "gw15")
    name_b = temp_migration_db_name("mig5", long_b, "gw15")

    assert len(name_a.encode()) <= _PG_IDENT_MAX_BYTES
    assert len(name_b.encode()) <= _PG_IDENT_MAX_BYTES
    # 앞 24글자가 같아도 해시가 자르기 전 원본에서 떠서 갈린다.
    assert name_a != name_b


def test_식별자로_못_쓰는_글자를_눌러_담는다():
    """DB 이름은 DSN에서 오는 값이라 따옴표가 섞이면 CREATE DATABASE "..." 인용을 깬다."""
    name = temp_migration_db_name('mig', 'c207"; DROP DATABASE x; --_test', "main")

    assert '"' not in name and " " not in name and ";" not in name


def test_실제_시험_파일_둘이_서로_다른_이름을_쓴다():
    """계약을 지켜야 하는 자리는 부르는 쪽이다 — 두 파일의 실제 값을 본다."""
    from tests import test_migrations_0005, test_migrations_roundtrip

    assert test_migrations_roundtrip.MIG_DB_NAME != test_migrations_0005.MIG_DB_NAME


def test_실제_시험_파일이_이름_규칙을_그대로_쓴다():
    """워커 접미사만 붙던 예전 규칙으로 되돌아가면 여기서 걸린다.

    직렬이든 xdist든 같은 식으로 판정한다 — 지금 프로세스의 워커 이름을 그대로 넣고 견준다.
    """
    import os

    from tests import test_migrations_0005, test_migrations_roundtrip

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")

    assert test_migrations_roundtrip.MIG_DB_NAME == temp_migration_db_name(
        "mig", test_migrations_roundtrip._admin_db_name(), worker
    )
    assert test_migrations_0005.MIG_DB_NAME == temp_migration_db_name(
        "mig5", test_migrations_0005._admin_db_name(), worker
    )
