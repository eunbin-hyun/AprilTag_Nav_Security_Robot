"""conftest._worker_db_urls 순수 변환 검사 (S15P11C207-185).

워커 DB 이름 재조립이 urlunsplit에 쿼리스트링·프래그먼트를 안 넘겨서 버리던 결함의
회귀 시험. 검사 본문은 문자열 변환만 보지만, conftest 세션 픽스처 때문에 실행엔 시험 DB가 필요하다.
"""
from tests.conftest import _worker_db_urls, database_name, swap_database_name


def test_worker_db_url_appends_worker_suffix():
    admin_dsn, worker_url = _worker_db_urls(
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test", "gw0"
    )

    assert admin_dsn == "postgresql://postgres@127.0.0.1:5433/c207_test"
    assert worker_url == "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test_gw0"


def test_worker_db_url_preserves_query_string():
    admin_dsn, worker_url = _worker_db_urls(
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test?ssl=require", "gw1"
    )

    assert admin_dsn == "postgresql://postgres@127.0.0.1:5433/c207_test?ssl=require"
    assert worker_url == (
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test_gw1?ssl=require"
    )


# ── 마이그레이션 시험 둘이 쓰는 이름 분해·조립 (2026-08-02 백지 검토 C11) ────
#
# test_migrations_roundtrip.py·test_migrations_0005.py가 `rpartition("/")`로 직접 자르다가
# 파라미터가 붙은 DSN에서 DB 이름을 `c207_test?ssl=require`로 만들던 자리다. 두 파일이 이제
# 아래 두 함수를 쓰므로 계약을 여기서 굳힌다.


def test_database_name_ignores_query_string():
    assert (
        database_name("postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test?ssl=require")
        == "c207_test"
    )


def test_swap_database_name_keeps_query_string():
    swapped = swap_database_name(
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test?ssl=require", "mig_c207_test"
    )

    assert swapped == (
        "postgresql+asyncpg://postgres@127.0.0.1:5433/mig_c207_test?ssl=require"
    )
