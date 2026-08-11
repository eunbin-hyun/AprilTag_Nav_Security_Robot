"""시험 DB 가드 (운영 DB 파괴 방지).

예전 conftest는 `os.environ.setdefault("DATABASE_URL", ...)`이라 운영 DATABASE_URL을 들고
있는 셸·컨테이너에서 pytest를 부르면 운영 값이 이겼고, 세션 픽스처의 `drop_all`이 운영
스키마를 통째로 지웠다. 실측 반증 — 운영 모양 DB에 행 하나를 심고 시험 한 개를 돌리니
수리 전에는 종료 코드 0에 그 행이 사라졌다(운영 이미지 안에서 `pytest`로도 같이 재현했다).

여기서 검사하는 것은 둘이다.
1. 판별·해석 순수 함수 — 무엇을 시험용으로 볼지
2. 진짜 거동 — 운영 DSN을 실은 환경에서 pytest를 부르면 수집 전에 0이 아닌 코드로 죽나

⚠ conftest 심볼은 함수 안에서 늦게 임포트한다. 모듈 맨 위에서 가져오면 수리 전 코드에서
  파일 자체가 안 열려 2번 검사가 수집조차 안 된다 — 임포트 실패는 거동 반증이 아니다.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

# 운영 모양 DSN. 포트 1은 어디에도 없어서, 가드가 뚫려도 실제 DB에 닿지 않는다.
PROD_SHAPED_DSN = "postgresql+asyncpg://postgres@127.0.0.1:1/c207"
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _guard():
    import tests.conftest as conftest

    return conftest


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test",
        "postgresql+asyncpg://postgres@192.168.50.100:5479/c207_test",
        # 두 번째 시험 DB를 파는 흔한 이름. 뒤 숫자는 허용한다.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test2",
        # xdist 워커 접미사가 붙어도 시험용이다.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test_gw0",
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test2_gw11",
        # 쿼리스트링이 붙어도 이름만 본다.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test?ssl=require",
    ],
)
def test_test_shaped_urls_pass(url):
    assert _guard()._is_test_database_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        # 실제 EC2 운영 DSN 모양. 이것이 막아야 하는 값이다.
        "postgresql+asyncpg://c207:pw@c207-db:5432/c207",
        # ⚠ 운영 DB가 그 호스트의 loopback 5433에 매핑돼 있다. 호스트로는 못 가른다.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207",
        # 이름 앞에 test가 있어도 끝이 아니면 안 된다.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/test_c207",
        # "test를 포함하면 통과" 식으로 느슨하게 잡으면 이런 이름이 새어 들어온다.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_latest",
        "postgresql+asyncpg://postgres@127.0.0.1:5433/testing",
        # DB 이름이 없는 DSN.
        "postgresql+asyncpg://postgres@127.0.0.1:5433/",
        "",
    ],
)
def test_non_test_shaped_urls_are_rejected(url):
    assert _guard()._is_test_database_url(url) is False


def test_inherited_prod_database_url_refuses():
    guard = _guard()
    with pytest.raises(guard.DatabaseGuardError) as exc:
        guard._resolve_test_database_url({"DATABASE_URL": PROD_SHAPED_DSN})
    assert "시험용이 아니다" in str(exc.value)


def test_inherited_test_database_url_is_honored():
    """시험용 DSN을 직접 넣어 쓰는 예전 흐름은 그대로 산다."""
    url = "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test2"
    assert _guard()._resolve_test_database_url({"DATABASE_URL": url}) == url


def test_prod_test_database_url_refuses():
    guard = _guard()
    with pytest.raises(guard.DatabaseGuardError):
        guard._resolve_test_database_url({"TEST_DATABASE_URL": PROD_SHAPED_DSN})


def test_falls_back_to_configured_then_default():
    guard = _guard()
    url = "postgresql+asyncpg://postgres@127.0.0.1:5479/c207_test"
    assert guard._resolve_test_database_url({"TEST_DATABASE_URL": url}) == url
    assert guard._resolve_test_database_url({}).endswith("/c207_test")


def test_pytest_run_with_prod_dsn_dies_before_collecting():
    """진짜 거동 반증 — 운영 DSN을 실은 채 pytest를 부르면 수집 전에 죽어야 한다.

    수리 전 코드에서는 이 실행이 종료 코드 0으로 수집을 끝냈다(실행까지 갔으면 drop_all이
    돌았다). 순수 함수 검사만으로는 conftest가 그 함수를 실제로 부르는지 못 잡아서, 하위
    프로세스로 pytest를 한 번 더 띄워 확인한다.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env["DATABASE_URL"] = PROD_SHAPED_DSN

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/test_healthz.py"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )

    assert proc.returncode != 0, f"운영 DSN인데 수집이 통과했다\n{proc.stdout}\n{proc.stderr}"
    assert "DatabaseGuardError" in (proc.stdout + proc.stderr)
