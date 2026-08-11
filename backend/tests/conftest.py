"""pytest 공용 픽스처.

테스트 DB는 관통 실측용 c207과 분리한다(TEST_DATABASE_URL 없으면 기본으로 5433/c207_test).
app 임포트 전에 환경변수를 세팅해야 엔진이 테스트 DB로 붙는다.

pytest-xdist로 병렬 실행하면 워커마다 별도 프로세스가 같은 DB에 붙어 스키마를
drop/create하고 매 케이스 앞에 TRUNCATE까지 해서 서로의 행을 지운다. 그래서 워커
안에서는(환경변수 PYTEST_XDIST_WORKER가 있을 때) DB 이름에 워커 접미사를 붙이고
없으면 만든다. 워커 밖(직렬 실행)에서는 접미사도 DB 생성도 없다 — 예전 거동 그대로다.

⚠ 두 사람(또는 두 셸)이 **같은 순간에** pytest를 돌릴 거면 `TEST_DATABASE_URL`이나
  `PYTEST_XDIST_WORKER`를 서로 다르게 잡아라. 같은 값으로 나란히 돌리면 같은 DB를 붙잡고
  케이스마다 서로의 행을 TRUNCATE로 지운다 — 코드가 멀쩡한데 양쪽이 간헐로 빨개진다.
  자세한 규약은 아래 `temp_migration_db_name` docstring에 있다.
"""
import asyncio
import hashlib
import os
import re
import threading
import time

# ── 시험 DB 가드 ────────────────────────────────────────────────────────────
# ⚠ 예전 코드는 os.environ.setdefault("DATABASE_URL", ...)였다. setdefault는 "이미 값이
#   있으면 그대로 둔다"라서, 운영 DATABASE_URL을 들고 있는 셸이나 컨테이너에서 pytest를
#   부르면 운영 값이 이기고 아래 _schema 픽스처의 drop_all이 운영 스키마를 통째로 지웠다.
#   실측으로 재현됐다 — 표시행 하나를 심어 둔 c207 DB에 시험 한 개를 돌리니 tagging_event가
#   drop되고 행이 1개에서 0개가 됐다.
#   그래서 지금은 "값이 없으면 채운다"가 아니라 "시험용이 아니면 시작을 거부한다"다.
_DEFAULT_TEST_DATABASE_URL = "postgresql+asyncpg://postgres@127.0.0.1:5433/c207_test"

# xdist 워커가 DB 이름 뒤에 붙이는 접미사(_gw0). 이름 판정에서 떼고 본다.
_XDIST_DB_SUFFIX_RE = re.compile(r"_gw\d+$")

# 시험 DB 이름 규칙. `_test`로 끝나거나 뒤에 숫자만 더 붙는다(c207_test·c207_test2).
# `_test`를 그냥 "포함"으로 느슨하게 잡으면 c207_latest 같은 이름이 통과해 버린다.
_TEST_DB_NAME_RE = re.compile(r"_test\d*$")


class DatabaseGuardError(RuntimeError):
    """시험 DB 가드가 실행을 막았을 때 던진다."""


def _database_name(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path.lstrip("/")


def _is_test_database_url(url: str) -> bool:
    """DB 이름이 `_test`(뒤에 숫자 허용)로 끝나는가. 시험용 판별의 유일한 잣대다.

    ⚠ 호스트는 잣대로 쓸 수 없다. EC2 운영 DB 컨테이너(c207-db)가 그 호스트의
      127.0.0.1:5433에 그대로 매핑돼 있어서 "loopback이면 시험용"이라는 규칙은 정작
      운영 DB를 통과시킨다. 반대로 팀원 시험 컨테이너는 LAN IP에 뜨는 일이 흔해서
      loopback 강제는 정상 시험만 막는다. 그래서 운영(c207)과 시험(c207_test)을 실제로
      갈라 주는 DB 이름만 본다.
    """
    if not url:
        return False
    name = _database_name(url)
    if not name:
        return False
    return _TEST_DB_NAME_RE.search(_XDIST_DB_SUFFIX_RE.sub("", name)) is not None


def _resolve_test_database_url(env) -> str:
    """시험이 쓸 DSN을 정한다. 운영 DSN이 끼면 DatabaseGuardError를 던진다.

    우선순위는 예전과 같다 — 환경의 DATABASE_URL이 먼저고, 없으면 TEST_DATABASE_URL,
    그것도 없으면 기본값이다. 달라진 건 어느 자리에서 온 값이든 시험용인지 검사한다는 것뿐이다.
    """
    inherited = env.get("DATABASE_URL")
    if inherited:
        if not _is_test_database_url(inherited):
            raise DatabaseGuardError(
                "DATABASE_URL이 시험용이 아니다 — DB 이름 "
                f"{_database_name(inherited)!r}가 '_test'로 끝나지 않는다. "
                "시험은 스키마를 drop/create하므로 이 DSN으로는 시작하지 않는다. "
                "운영 환경변수를 가진 셸이나 컨테이너라면 DATABASE_URL을 unset하고, "
                "시험 DB를 따로 쓰려면 TEST_DATABASE_URL에 '..._test' DB를 넣어라."
            )
        return inherited

    configured = env.get("TEST_DATABASE_URL") or _DEFAULT_TEST_DATABASE_URL
    if not _is_test_database_url(configured):
        raise DatabaseGuardError(
            "TEST_DATABASE_URL이 시험용이 아니다 — DB 이름 "
            f"{_database_name(configured)!r}가 '_test'로 끝나지 않는다."
        )
    return configured


# ── app 임포트보다 먼저 환경을 잡는다 ──────────────────────────────────────
os.environ["DATABASE_URL"] = _resolve_test_database_url(os.environ)
os.environ.setdefault("API_KEY", "test-key")

# ── 시험이 기대는 설정 축을 명시로 고정 (2026-08-02 백지 검토 F59) ──────────
#
# `app/config.py`의 Settings가 `env_file=".env"`를 읽는다. 그래서 이 저장소에서 pytest를
# 돌리는 사람의 `.env`에 `AUTH_REQUIRE_LOGIN=true`나 `WS_REQUIRE_API_KEY_DASHBOARD=true`가
# 한 줄 있으면 **아무 시험도 안 고쳤는데 결과가 조용히 달라진다.** 제1 불변을 지키는 대조군
# (`test_auth_gates.test_flag_off_*`)이 켜진 채로 돌아 통째로 뜻을 잃는 자리다.
#
# 그래서 시험 결과를 가르는 축은 여기서 **코드 기본값과 같은 값으로 못박는다.** 값을 바꾸는
# 게 아니라 "사람마다 다른 .env가 못 이기게" 하는 게 목적이라, 전부 config.py 기본값 그대로다.
# 환경변수가 `.env`보다 세다(pydantic-settings 우선순위)는 게 이게 먹는 근거고, 케이스가
# `monkeypatch.setenv`로 축을 뒤집는 길은 그대로 열려 있다(`flag_on`·`no_hold`).
#
# ⚠ 여기 없는 축은 케이스가 `get_settings()`에서 **동적으로 읽어** 쓰는 값들이다(예:
#   `alert_cooldown_sec` — 시험이 그 값을 읽어 오프셋을 만든다). 그런 축은 .env가 달라도
#   시험이 같이 따라가므로 고정할 이유가 없다. 그래도 창 길이가 바뀌면 타이밍 여유가
#   달라지는 셋(쿨다운·크레딧 TTL·출동 대기 창)은 안전하게 같이 못박았다.
_PINNED_TEST_ENV = {
    # 로그인 롤아웃 스위치. 이 한 줄이 F59의 본체다.
    "AUTH_REQUIRE_LOGIN": "false",
    "SESSION_COOKIE_SECURE": "true",
    "AUTH_ALLOWED_ORIGINS": "",
    # WS 키 게이트 셋. 켜진 채로 돌면 익명 핸드셰이크 케이스가 통째로 뒤집힌다.
    "WS_REQUIRE_API_KEY": "false",
    "WS_REQUIRE_API_KEY_ROBOT": "false",
    "WS_REQUIRE_API_KEY_DASHBOARD": "false",
    "DASHBOARD_API_KEY": "",
    "DISPATCH_REQUIRE_API_KEY": "false",
    # 바깥으로 나가는 창구. URL이 비어 있어야 발사 자체가 안 일어난다(F58과 두 겹).
    "MATTERMOST_ENABLED": "true",
    "MATTERMOST_WEBHOOK_URL": "",
    "MATTERMOST_IDENTIFY_WEBHOOK_URL": "",
    "GMS_API_KEY": "",
    # 기상청 인증키. 비어 있어야 `weather.fetch_current_weather`가 기상청 경로를 안 타고
    # 스텁이 걸린 wttr 사슬로 간다. 이 줄이 없으면 개발자 `.env`에 실제 키가 한 줄 있는
    # 사람만 시험이 **바깥 기상청을 진짜로 부르고** 결과가 조용히 달라진다.
    # 기상청 갈래를 보는 시험은 `fetch_kma_weather`를 직접 부르거나 키를 setenv로 켠다.
    "KMA_SERVICE_KEY": "",
    # 날씨 주기 조회. 시험에서는 **꺼 둔다.**
    #
    # ⚠ 2026-08-03 실측으로 잡은 자리다. lifespan이 폴러를 띄우면 첫 바퀴를 안 기다리고
    #   곧장 돌아 `weather_update`를 대시보드로 **먼저** 민다. 그러면 "붙은 뒤 첫 메시지가
    #   내 것"이라고 보는 라이브 WS 케이스들이 남의 첫 장을 받아 통째로 뒤집힌다
    #   (test_ws_channels 2건 · test_ws_envelopes_live 3건). 라이브 거동 자체는 맞으므로
    #   코드를 비틀지 않고 시험에서만 끈다 — 폴러를 재는 케이스는 주기를 직접 켜서 본다.
    "WEATHER_POLL_INTERVAL_SEC": "0",
    # 날씨 자동 전환. **시험에서는 켠다.** 운영 기본값은 꺼짐이다(시연 안전 — config.py 참고).
    #
    # 갈라 둔 이유 — 그 거동(비 오면 실내로, 요원 선택이 이김, 한 방향 걸쇠)은 스위치를
    # 켰을 때의 계약이라 시험이 계속 지켜야 한다. 여기서 안 켜면 그 시험 열 건이 통째로
    # 죽고, 나중에 시연이 끝나 스위치를 켜는 날 아무도 그 거동을 안 지키고 있다.
    # 꺼진 갈래는 `test_스위치가_꺼져_있으면_*`이 따로 재고, 그 케이스들은 이 값을
    # 자기 안에서 꺼서 본다.
    "WEATHER_AUTO_DEFAULT_ENABLED": "true",
    # [초안·팀 확정 대기] 안건 셋. 켜면 판정 범위·허용 값이 달라진다.
    "CREDIT_SCOPE_ROOM": "false",
    "EVENT_ID_REQUIRE_BOOT_ID": "false",
    "MISSION_STATUS_EN_ROUTE_ENABLED": "false",
    "ARRIVAL_EDGE_ON_RECOMMAND": "false",
    # 창 길이 셋.
    "CREDIT_TTL_SEC": "3.0",
    "ALERT_COOLDOWN_SEC": "10.0",
    "SHUTTLE_DISPATCH_HOLD_SEC": "5.0",
    # 로그인 잠금·세션 수명. 잠금 케이스가 "5회"를 글자로 적어 두는 자리라 같이 못박는다.
    "LOGIN_FAIL_MAX_ATTEMPTS": "5",
    "LOGIN_FAIL_LOCK_SEC": "60.0",
    "SESSION_ABSOLUTE_TTL_SEC": "43200.0",
    "SESSION_IDLE_TTL_SEC": "7200.0",
    "SESSION_TOUCH_MIN_INTERVAL_SEC": "60.0",
    "SESSION_MAX_PER_USER": "5",
}
os.environ.update(_PINNED_TEST_ENV)


def _run_in_new_loop(coro_factory) -> None:
    """별 스레드에서 asyncio.run으로 코루틴을 돌린다.

    MainThread에서 asyncio.run을 부르면 뒤에 pytest-asyncio가 잡는 세션 루프 상태를
    흔들어 시험이 깨진다. 별 스레드에 가두면 MainThread 루프를 안 건드린다.
    """
    import asyncio

    box: dict[str, BaseException] = {}

    def _target() -> None:
        try:
            asyncio.run(coro_factory())
        except BaseException as exc:  # noqa: BLE001 - 그대로 되던진다
            box["err"] = exc

    t = threading.Thread(target=_target)
    t.start()
    t.join()
    if "err" in box:
        raise box["err"]


def _worker_db_urls(url: str, worker: str) -> tuple[str, str]:
    """워커 DB 이름을 만들고 (관리용 DSN, 워커 DB URL)을 돌려주는 순수 변환.

    쿼리스트링·프래그먼트도 원래 URL에서 그대로 들고 온다 — urlunsplit에 빈 문자열을
    넘기면 ?ssl=require 같은 파라미터가 재조립 과정에서 조용히 없어진다.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    base_db = parts.path.lstrip("/")
    worker_db = f"{base_db}_{worker}"

    # 관리 커넥션은 이미 있는 원래 DB로 붙는다. asyncpg는 +asyncpg 접미사를 모른다.
    admin_dsn = urlunsplit(
        (parts.scheme.split("+", 1)[0], parts.netloc, f"/{base_db}", parts.query, parts.fragment)
    )
    worker_url = urlunsplit(
        (parts.scheme, parts.netloc, f"/{worker_db}", parts.query, parts.fragment)
    )
    return admin_dsn, worker_url


def database_name(url: str) -> str:
    """DSN에서 DB 이름만 뽑는다.

    ⚠ `url.rpartition("/")[2]`로 뽑으면 안 된다. `...:5433/c207_test?ssl=require`처럼 파라미터가
      붙은 DSN에서 이름이 `c207_test?ssl=require`가 되고, 그 문자열이 그대로 `CREATE DATABASE`와
      관리 커넥션에 실린다. 마이그레이션 시험 둘이 이 방식으로 조립하고 있었다
      (2026-08-02 백지 검토 C11).
    """
    return _database_name(url)


def resolved_test_database_url() -> str:
    """마이그레이션·드리프트 시험이 임시 DB를 만들 때 기준으로 삼는 DSN. **한 자리다.**

    ⚠ 예전에는 그 시험 넷이 각자 `os.environ.get("TEST_DATABASE_URL", 기본값)`을 읽었다
    (2026-08-04 전체검토 L33). conftest는 `DATABASE_URL`을 **먼저** 보는데(위
    `_resolve_test_database_url`) 그쪽만 다르게 잡고 돌리면 본 시험은 그 DB로 가고 임시 DB는
    엉뚱한 서버에 만들어진다 — `temp_migration_db_name`이 이름 파생으로 막으려던 겹침이
    base_db가 갈리면서 이 갈래로 다시 열린다.

    이 함수가 돌려주는 값은 conftest 머리에서 이미 확정된 `DATABASE_URL`이다. 그래서
      - 우선순위(DATABASE_URL → TEST_DATABASE_URL → 기본값)가 본 시험과 글자 그대로 같고,
      - 시험용 DB 가드(`_is_test_database_url`)를 이미 지난 값이며,
      - xdist 워커 접미사(`_gw0`)까지 붙은 뒤라 워커마다 base_db가 저절로 갈린다.
    """
    return os.environ["DATABASE_URL"]


def swap_database_name(url: str, db_name: str) -> str:
    """DSN의 DB 이름만 갈아 끼운다. 쿼리스트링·프래그먼트는 원래 값 그대로 남는다.

    `_worker_db_urls`가 쓰는 재조립과 같은 규칙이다 — urlunsplit에 빈 문자열을 넘기면
    `?ssl=require` 같은 파라미터가 조용히 없어진다.
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db_name}", parts.query, parts.fragment))


def _ensure_worker_database(url: str, worker: str) -> str:
    """워커별 DB 이름을 만들고, 그 DB가 없으면 관리 커넥션으로 CREATE DATABASE 한다."""
    from urllib.parse import urlsplit

    admin_dsn, worker_url = _worker_db_urls(url, worker)
    worker_db = urlsplit(worker_url).path.lstrip("/")

    async def _create() -> None:
        import asyncpg

        conn = await asyncpg.connect(admin_dsn)
        try:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", worker_db
            )
            if not exists:
                await conn.execute(f'CREATE DATABASE "{worker_db}"')
        finally:
            await conn.close()

    # 이름이 워커마다 달라 경합은 없지만, 여러 워커가 같은 순간에 CREATE DATABASE를
    # 때리면 템플릿 잠금이 겹쳐 "being accessed by other users"로 튄다. 몇 번 재시도한다.
    last: BaseException | None = None
    for _ in range(5):
        try:
            _run_in_new_loop(_create)
            break
        except Exception as exc:  # noqa: BLE001 - 재시도가 다 실패하면 되던진다
            last = exc
            time.sleep(0.5)
    else:
        raise RuntimeError(f"워커 DB {worker_db} 생성 실패") from last

    return worker_url


# ── 마이그레이션 왕복 시험이 쓰는 임시 DB 이름 ──────────────────────────────
# 이 규칙은 test_migrations_roundtrip.py와 test_migrations_0005.py 둘이 같이 지켜야 하는
# 계약이라 한 함수에 모았다. 두 파일이 각자 이름을 조립하면 한쪽만 고쳐지는 순간 다시
# 서로를(그리고 남의 프로세스를) 밟는다.

_PG_IDENT_MAX_BYTES = 63
# CREATE DATABASE "..." 안에 그대로 실어도 되는 글자만 남긴다. DB 이름은 DSN에서 오는 값이라
# 따옴표 같은 글자가 섞이면 식별자 인용을 깨고 SQL로 새어 나간다.
_DB_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9]")


def temp_migration_db_name(prefix: str, base_db: str, worker: str) -> str:
    """마이그레이션 왕복 시험이 만들고 지우는 임시 DB 이름을 결정적으로 만든다.

    ⚠ 예전에는 `f"mig_test_{워커}"`처럼 **시험 DB 이름을 아예 안 보고** 워커 접미사만 붙였다.
      그래서 같은 서버에서 TEST_DATABASE_URL만 다르게(c207_a_test·c207_b_test) pytest 둘을
      띄우면 양쪽이 같은 `mig_test_main`을 붙잡고 서로 DROP/CREATE로 밟았다. 실측 재현 —
      A는 2실패, B는 4실패에 `DuplicateDatabaseError: database "mig_test_main" already
      exists`와 `UniqueViolationError: ... pg_database_datname_index`가 났다. 오늘 여러 조가
      본 "간헐 실패 3~4건"의 정체가 이것이다.

    그래서 이름을 (접두어, 시험 DB 이름, xdist 워커) 셋에서 파생시킨다.
      - 시험 DB를 다르게 잡은 프로세스는 base_db가 달라 이름이 갈린다.
      - 같은 시험 DB를 xdist로 나눠 쓰는 워커는 worker가 달라 갈린다.
      - 접두어가 갈라서 두 시험 파일이 서로를 안 밟는다(mig·mig5).

    읽을 수 있는 토막과 해시를 같이 넣는다. 해시만 두면 시험이 깨진 채 남긴 DB가 어느
    설정에서 왔는지 못 짚고, 읽을 수 있는 토막만 두면 PostgreSQL이 식별자를 63바이트에서
    **조용히 자르면서** 긴 이름끼리 다시 겹친다. 해시는 자르기 전 원본에서 뜨니 겹치지 않는다.
    길이는 접두어 4 + 읽는 토막 24 + 워커 8 + 해시 10 + 밑줄·접미사 9 = 최대 55바이트라
    63바이트 안이다.

    이름을 `_test`로 끝낸다 — 이 DB URL이 `DATABASE_URL`에 잠깐 들어가므로(각 시험 파일의
    `_database_url_pointed_at_mig_db`) 위 `_is_test_database_url` 가드를 통과해야 한다.

    ⚠ **이름에 PID·난수를 안 넣는다. 그래서 동시 실행 규약이 따로 있다** (2026-08-02 백지 검토
      C15). 같은 `TEST_DATABASE_URL`·같은 워커 이름으로 pytest 둘을 나란히 띄우면 이 이름이
      겹친다. 그런데 그때는 임시 DB보다 **본 시험 DB가 먼저 겹친다** — xdist 밖에서는
      `DATABASE_URL`을 그대로 쓰고 케이스마다 `_TRUNCATE_TABLES`를 비우므로 두 실행이 서로의
      행을 지운다. 이름에 PID를 넣어도 그 충돌은 안 막힌다.

      그래서 규약은 이름 쪽이 아니라 실행 쪽이다. **동시에 돌리는 사람마다 `TEST_DATABASE_URL`을
      다르게 잡는다**(`c207_a_test`·`c207_b_test`처럼). 그러면 본 시험 DB가 갈리고, base_db가
      달라지니 이 임시 DB 이름도 따라 갈린다. 한 사람이 여러 갈래로 나눠 돌릴 때는
      `PYTEST_XDIST_WORKER`를 달리 잡아도 같은 효과다.
    """
    tag = hashlib.sha256(f"{base_db}\x00{worker}".encode()).hexdigest()[:10]
    readable = _DB_NAME_UNSAFE_RE.sub("_", base_db)[:24]
    safe_worker = _DB_NAME_UNSAFE_RE.sub("_", worker)[:8]
    return f"{prefix}_{readable}_{safe_worker}_{tag}_test"


_XDIST_WORKER = os.environ.get("PYTEST_XDIST_WORKER")
if _XDIST_WORKER:
    os.environ["DATABASE_URL"] = _ensure_worker_database(
        os.environ["DATABASE_URL"], _XDIST_WORKER
    )

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402

API_KEY = get_settings().api_key


# ── 케이스마다 비우는 표 (2026-08-02 백지 검토 F56) ─────────────────────────
#
# ⚠ 손으로 적은 문자열 목록이라 **metadata와 대조하는 가드가 없으면 조용히 샌다.** 실제로
# 표 셋(`sticker_inspection`·`temporary_pass`·`kiosk`)과 `robot_status_log`가 목록 밖에
# 있었다. 앞 케이스가 심은 행이 다음 케이스로 새는 자리고, 새 표가 늘 때마다 같은 함정이
# 다시 열린다. 그래서 아래 `_assert_truncate_list_is_closed`가 이 목록을 `Base.metadata`와
# 대조해서 **닫힌 집합**으로 만든다 — 모델에 표를 하나 붙이고 여기 안 적으면 세션 시작에서
# 곧장 터진다.
#
# CASCADE가 참조 쪽을 같이 비우니 순서는 손으로 안 잡는다. 그래도 목록에는 **전부** 적는다 —
# "CASCADE가 끌어가니 안 적어도 된다"는 판단이 표 사이 관계가 바뀌는 날 조용히 틀려진다.
_TRUNCATE_TABLES = (
    "tagging_event",
    "gate_pass_event",
    "shuttle_arrival",
    "alert",
    "robot",
    "robot_status_log",
    # 서버가 낸 명령과 기기 응답(0018). 케이스마다 명령을 내니 안 비우면 앞 케이스 행이
    # 다음 케이스의 "몇 건 남았나" 검사로 샌다.
    "device_command_log",
    "daily_default_destination",
    "staff",
    "staff_audit",
    # 명부 조회 이력(0012). `app_user`를 FK 로 물어서 그보다 **먼저** 비워야 한다.
    "staff_access_log",
    # 관제 챗봇 대화(0019). ⚠ `chat_conversation`이 `app_user`를 FK 로 물어서 그보다 **먼저**
    # 비운다. 메시지는 대화를 물으므로 메시지가 앞이다(CASCADE 가 끌어가지만 목록에는 전부 적는다).
    "chat_message",
    "chat_conversation",
    # 일일 요약 캐시(0020). FK 가 없어 자리는 자유지만, 안 비우면 앞 케이스가 만든 요약을
    # 다음 케이스가 캐시에서 꺼내 써서 "LLM 을 안 불렀는데 문장이 있다"가 된다.
    "assistant_daily_brief",
    "auth_session",
    "app_user",
    "sticker_inspection",
    "temporary_pass",
    "kiosk",
)


def _assert_truncate_list_is_closed() -> None:
    """`_TRUNCATE_TABLES`가 `Base.metadata`의 표 전체와 정확히 같은가.

    세션에 한 번만 돈다. 어긋나면 어느 쪽으로 어긋났는지를 이름으로 알려준다 — "빠진 표"는
    케이스 사이 오염이고, "없는 표"는 TRUNCATE가 곧장 터지는 자리다.
    """
    listed = set(_TRUNCATE_TABLES)
    known = set(Base.metadata.tables)
    missing = sorted(known - listed)
    unknown = sorted(listed - known)
    assert not missing, (
        f"conftest _TRUNCATE_TABLES에 빠진 표가 있다: {missing}. "
        "앞 케이스가 심은 행이 다음 케이스로 샌다 — 목록에 넣어라."
    )
    assert not unknown, (
        f"conftest _TRUNCATE_TABLES에 모델에 없는 표가 있다: {unknown}. "
        "이름이 바뀌었거나 지워진 표다."
    )


@pytest_asyncio.fixture(scope="session", loop_scope="session", autouse=True)
async def _schema():
    """세션 시작에 스키마를 만들고 끝에 지운다."""
    _assert_truncate_list_is_closed()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


def _reset_ws_registries() -> None:
    """WS 커넥션 레지스트리 둘(`manager`·`robot_manager`)을 비운다 (2026-08-02 백지 검토 F65).

    다른 인메모리 상태와 같은 이유다 — DB 밖이라 TRUNCATE로 안 지워지고, 앞 케이스가 남긴
    소켓이 목록에 남으면 다음 케이스의 브로드캐스트가 죽은 소켓으로 나가거나
    `dashboard_count`·`robot_count`가 실제와 어긋난다. TestClient·실 uvicorn을 쓰는 케이스가
    비정상 경로로 끝나면 실제로 남는다.

    ⚠ **app/ws.py에는 `reset_*` 함수가 없다.** 그 파일은 이 갈래의 담당 밖이라 신설을 못 했고,
    그래서 여기서 커넥션을 하나씩 `disconnect()`로 걷는다 — 목록을 직접 `clear()`하지 않는
    이유는 `robot_manager`가 `_robots`와 `_misses` 두 자리를 같이 들고 있어서, 그 짝을 맞추는
    규칙이 매니저 안에 있기 때문이다. 순회에만 내부 자료를 읽는다.
    `app/ws.py`에 `reset_ws_state()`를 새로 파는 건 **이관 필요**로 보고했다.
    """
    from app.ws import manager, robot_manager

    for ws in list(manager._dashboard):
        manager.disconnect(ws)
    for ws in list(robot_manager._robots):
        robot_manager.disconnect(ws)


async def reset_in_memory_state() -> None:
    """`_clean`의 **DB 아닌 절반**. DB 밖 인메모리 상태를 전부 되돌린다.

    아래 `_clean`이 부르는 유일한 자리이고, **DB 없이 도는 케이스 묶음이 `_clean`을 덮을 때
    이걸 직접 부른다**(2026-08-04 전체검토 L24). 예전에는 그 묶음들이 `_clean`을 통째로
    no-op으로 덮어서 TRUNCATE만이 아니라 출동 대기창 취소·WS 레지스트리·쿨다운·로그인 잠금
    초기화까지 그 34개 케이스에서 전부 안 돌았다. 지금은 다음 케이스가 시작하며 다시 비워
    손해가 작지만, **무작위 순서에서는 그 34개가 어디든 낀다.**

    ⚠ 새 인메모리 상태를 얹을 때마다 여기에 한 줄 추가한다. 목록이 두 벌로 갈리면
    (파일 안 autouse 픽스처로 각자 비우는 방식) 그 파일 하나만 지킨다 — conftest가 셔틀
    쿨다운·로그인 잠금에서 세 번 적어 둔 그 자리다.

    ⚠ 출동 대기 창 취소를 맨 앞에 두고, 부르는 쪽이 이 함수를 TRUNCATE **앞에** 부른다.
    순서가 반대면 행이 지워진 뒤 타이머가 터져 "없는 신호"로 도는 갈래를 매 케이스마다
    태우게 된다. 나머지 초기화는 DB를 한 줄도 안 만져서 TRUNCATE와 순서가 무관하다.
    """
    from app.auth import reset_auth_state
    from app.credit.state_machine import reset_cooldowns
    from app.dispatch import reset_dispatch_state
    from app.robot.mission_state import reset_mission_states
    from app.robot_channel import reset_robot_channel_state
    from app import gate_sensing
    from app.routers.assistant import reset_call_quota
    from app.routers.camera import reset_camera_state
    from app.shuttle_call import reset_shuttle_call_state
    from app.weather import reset_weather_cache

    # ⚠ 취소한 태스크를 **기다려야** 정리가 끝난다. `reset_dispatch_state`는 `_claim`을 지나
    # 발사 중인 태스크까지 취소하고 그 목록을 돌려주는데, 안 기다리면 취소가 실제로 접히기
    # 전에 다음 케이스가 시작해 남의 시험에 명령이 샌다.
    await asyncio.gather(*reset_dispatch_state(), return_exceptions=True)
    reset_auth_state()
    reset_cooldowns()
    reset_robot_channel_state()
    reset_mission_states()
    reset_shuttle_call_state()
    reset_weather_cache()
    # ⭐ 카메라 자리 카운터·관제 보조 호출 눈금 (2026-08-04 전체검토 L19).
    #
    # `reset_camera_state`는 `tests/test_camera.py`가 자기 파일 안 autouse로만 불렀다 — 딴
    # 파일이 `/api/camera/stream`을 한 번 부르고 비정상으로 끝나면 그 카운터가 새어, 다음
    # 파일의 동시접속 상한 503 케이스가 원인 모를 실패로 뒤집힌다.
    #
    # `reset_call_quota`는 **저장소 전체에 부르는 자리가 하나도 없었다**(정의만 있는 죽은
    # 함수). `_calls` 열쇠는 ASGITransport에서 `request.client`가 None이라 전 시험이
    # `ip:unknown` 한 버킷을 나눠 쓴다 — 60초에 60건을 넘기면 뒤 케이스가 429로 빨개지는데
    # 지금 안 넘는 것뿐이다.
    reset_camera_state()
    reset_call_quota()
    _reset_ws_registries()
    # 게이트 센싱 상태도 DB 밖이라 TRUNCATE로 안 지워진다. 앞 케이스가 "꺼짐"을 남기면
    # 다음 케이스의 스냅샷이 `null`을 기대하는 자리에서 `false`를 받는다.
    gate_sensing.reset()


@pytest_asyncio.fixture(loop_scope="session", autouse=True)
async def _clean():
    """매 테스트 앞에 인입·알림 테이블을 비우고, 인메모리 쿨다운 상태도 초기화한다.

    쿨다운은 DB 밖 상태라 TRUNCATE로 안 지워진다 — 안 지우면 앞 케이스의 미태깅이
    다음 케이스의 알림을 조용히 삼킨다(검사 위생).

    로봇 채널 인메모리 상태(mission_status 에지 추적·명령 상관관계)도 같은 이유로 비운다.
    안 비우면 앞 케이스의 ARRIVED가 남아 다음 케이스의 도착이 "값 안 바뀜"으로 삼켜진다.

    셔틀 화면 호출 쿨다운(app/shuttle_call.py)도 같은 인메모리 상태다. 예전에는
    test_shuttle_call.py가 자기 파일 안 autouse 픽스처로 비웠는데, 그건 구조적으로 그 파일
    하나만 지킨다 — `/api/shuttle-calls`를 부르는 **다음 파일**은 앞 파일이 남긴 눈금을
    물려받아 원인 모를 429로 깨진다(같은 게이트를 두 파일에서 연달아 부르는 시험 짝으로
    실측 재현했다: test_shuttle_call.py 1차 + test_shuttle_cooldown_hygiene.py 2차).
    그래서 여기로 올렸다.

    [초안·팀 확정 대기] 안건③ 추적기(app/robot/mission_state.py)도 같은 인메모리 상태라
    여기서 같이 비운다. 새 인메모리 상태를 얹을 때마다 이 훅에 추가한다.

    ⭐ 0008이 만든 표 셋(`app_user`·`auth_session`·`staff_audit`)과 칸이 늘어난 `staff`도
    목록에 있다. 예전에는 로그인 시험 파일들이 **자기 파일 안에서** 같은 TRUNCATE를 돌려
    막았는데, 그 방어는 구조적으로 그 파일 하나만 지킨다 — 계정을 만드는 **다음 파일**은 앞
    파일이 남긴 아이디를 물려받아 409(중복 아이디)나 "마지막 관리자" 잠금으로 깨진다.
    셔틀 쿨다운을 이 훅으로 올린 것과 같은 판단이다.

    ⚠ 지우는 순서를 손으로 안 잡는다. `CASCADE`가 참조 쪽을 같이 비우고(`app_user` →
    `auth_session`·`staff`·`staff_audit.actor_user_id`, `staff` → `alert.staff_id`) 그 표들이
    전부 이 목록 안이라, 이 한 문장이 닫힌 집합이다. 목록 밖 표를 CASCADE가 끌고 가면
    Postgres가 그 이름을 말해 준다.

    ⭐ 로그인 실패 잠금(`app/auth.py`의 인메모리 `_login_failures`)도 여기서 비운다. 표를
    올려 놓고 이건 안 비우면, 5회 실패로 잠긴 아이디가 다음 케이스로 새어 정상 로그인이
    429가 된다 — 위 셔틀 쿨다운과 똑같은 계열이다. 로그인 시험 파일들이 각자 비우던 자리를
    이 훅으로 올렸고(2026-08-02 교차 검토), 그 파일들의 자기 정리 코드는 걷어냈다. 그
    파일들이 지금도 통과한다는 게 이 훅이 실제로 일한다는 증거다.

    ⚠ 출동 대기 창(app/dispatch.py)은 **꼭 여기서 취소해야 한다.** 그냥 두면 앞 케이스가
    연 5초 asyncio 타이머가 다음 케이스가 DB를 기다리는 동안 터져서, 남의 시험에 목적지
    명령과 WS 메시지가 새어 든다(케이스 하나가 5초를 넘지 않아도 케이스 몇 개를 건너뛰어
    터진다). 날씨 폴백 캐시도 같은 이유로 비운다 — 앞 케이스가 심은 가짜 날씨가 다음
    케이스의 기본 목적지를 정하면 안 된다.

    ⭐ 비우는 표 목록은 이제 `_TRUNCATE_TABLES` 한 자리고, `Base.metadata`와 대조하는 가드가
    세션 시작에 돈다(F56). 손으로 적은 목록이라 표 넷이 조용히 빠져 있었다.

    ⭐ WS 커넥션 레지스트리(`app/ws.py`의 `manager`·`robot_manager`)도 같은 인메모리 상태라
    `_reset_ws_registries()`가 걷는다(F65). 그 함수 docstring에 왜 여기서 하는지가 있다.

    ⭐ 인메모리 초기화는 전부 `reset_in_memory_state()` 한 함수로 갈라 뒀다(L24). DB 없이
    도는 케이스 묶음이 이 픽스처를 덮을 때 그 함수만 부르면, TRUNCATE만 걷히고 나머지
    초기화는 그대로 돈다 — 덮개가 인메모리 위생까지 통째로 끄던 자리가 닫혔다.
    """
    # ⚠ 인메모리 정리를 TRUNCATE보다 먼저 한다. 순서가 반대면 출동 대기 창이 살아 있는 채로
    # 행이 지워져, 뒤늦게 터진 타이머가 "없는 신호"로 도는 갈래를 매 케이스마다 태운다.
    await reset_in_memory_state()
    async with engine.begin() as conn:
        await conn.exec_driver_sql(
            f"TRUNCATE {', '.join(_TRUNCATE_TABLES)} RESTART IDENTITY CASCADE"
        )
    yield


# ── 바깥 날씨 서버 차단 ─────────────────────────────────────────────────────
# 2026-07-31 검증 지적: 날씨 스텁이 test_shuttle_dispatch.py 안에만 있어서, 다른 파일이
# 연 5초 대기 창이 지연으로 터지면 그 자리에서 wttr.in을 **진짜로** 불렀다. 바깥이 느린
# 날엔 시험이 느려지고, 죽은 날엔 통째로 빨개진다. 그래서 여기로 올렸다.
_STUB_WTTR_J1 = {
    "current_condition": [
        {
            "weatherCode": "113",              # 맑음 → 기본 목적지가 실외
            "weatherDesc": [{"value": "Sunny"}],
            "precipMM": "0.0",
            "temp_C": "30",
        }
    ]
}


@pytest.fixture
def no_hold(monkeypatch):
    """출동 대기 창을 꺼서 셔틀 신호가 예전처럼 즉시 발사되게 둔다.

    2026-07-31 설계가 셔틀 도착과 목적지 명령 사이에 5초 창을 끼웠다(app/dispatch.py).
    "명령이 실제로 나갔나"·"재전송이 유실을 복구하나"를 세는 케이스는 그 5초를 기다릴
    이유가 없어서 창을 끈다. 창이 있고 없고를 가르는 시나리오는
    `tests/test_shuttle_dispatch.py`가 본다.

    ⚠ 재전송 복구 시험은 창을 **반드시** 꺼야 한다. 창이 도는 중이면 재전송이 아예 안
    나가는 게 계약이라(창이 살아 있다는 건 "아직 안 나갔다"이지 "유실됐다"가 아니다),
    창을 켠 채로는 복구 갈래를 못 본다.

    설정 인스턴스에 속성을 직접 대입하지 않는다 — 뒤에 `get_settings.cache_clear()`를
    부르는 픽스처가 있으면 그 대입이 조용히 사라진다.

    시험 파일마다 자기 사본을 들고 있으면 계약이 바뀔 때 한쪽만 고쳐진다. 그래서 여기
    한 자리에 뒀다(2026-07-31에 파일 셋이 같은 픽스처를 필요로 했다).
    """
    monkeypatch.setenv("SHUTTLE_DISPATCH_HOLD_SEC", "0")
    get_settings.cache_clear()
    try:
        yield
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


# ── 바깥으로 나가는 POST 차단 (2026-08-02 백지 검토 F58) ────────────────────
#
# 날씨 갈래(httpx `.get`)만 막혀 있었다. 매터모스트 발사는 `app/notify.py`의 `client.post`라
# 그물 밖이었고, `.env`에 `MATTERMOST_WEBHOOK_URL`이 한 줄 있으면 **미태깅을 만드는 케이스
# 수십 개가 팀 채널에 진짜 카드를 쏜다.** 시험을 한 번 돌릴 때마다 보안 채널이 도배된다.
#
# 잣대는 "매터모스트 URL인가"가 아니라 **loopback 밖인가**다. URL로 가르면 설정을 딴 이름으로
# 옮기거나 새 웹훅이 붙는 순간 다시 샌다. 시험이 실제로 부르는 바깥 주소는 자기가 띄운 목
# 서버(127.0.0.1)뿐이라, 그 밖으로 나가는 POST는 예외 없이 사고다.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "test", "testserver"})


class OutboundPostBlocked(RuntimeError):
    """시험이 loopback 밖으로 요청을 내려 했을 때 던진다.

    이름에 POST가 남아 있는 건 이 그물이 F58에서 매터모스트 발사를 막으려고 생겼기
    때문이다. 지금은 **메서드를 안 가린다**(아래 `block_outbound_post` 참고).
    """


def _post_target_allowed(url) -> bool:
    """이 POST를 내보내도 되나. host가 없으면(상대 경로) 클라이언트 base_url 몫이라 통과."""
    from urllib.parse import urlsplit

    host = urlsplit(str(url)).hostname
    return host is None or host in _LOOPBACK_HOSTS


@pytest.fixture(autouse=True)
def block_outbound_post(monkeypatch):
    """loopback 밖으로 나가는 httpx 요청을 **메서드를 안 가리고** 막는다.

    막는 자리를 `send_untagged_alert`가 아니라 그 아래 httpx로 잡은 이유는 날씨 가드와 같다 —
    발사 함수가 둘(`send_untagged_alert`·`send_identify_report`)이고 앞으로 더 늘 자리라,
    HTTP 경계 한 자리를 막으면 어느 경로로 들어와도 못 나간다.

    ⭐ **잣대를 `send`에 건다** (2026-08-04 전체검토 L20). 예전에는 `post` 하나만 덮어서
    `send`·`stream`·`request`·`get`이 통째로 그물 밖이었다. 그런데 카메라 프록시는
    `client.send(..., stream=True)`로 나간다(`app/routers/camera.py`) — 딴 파일이
    `/api/camera/stream`을 한 번 부르면 시험이 사설 IP `192.168.0.13:8090`으로 **진짜 TCP를
    열었다.** 지금 막는 건 `test_camera.py` 안 autouse 하나뿐인데, conftest가 셔틀 쿨다운·
    로그인 잠금에서 세 번 적어 둔 대로 파일 안 방어는 그 파일만 지킨다.

    httpx는 `get`·`post`·`request`·`stream`을 전부 `send`로 모아 보내므로(httpx 0.28
    `_client.py`) 이 한 자리가 다섯 갈래를 다 덮는다. 아래 `post` 오버라이드는 지운 게
    아니라 **문구를 위해 남겼다** — 매터모스트가 이 그물이 생긴 이유라, 그 갈래에서는
    ".env에 웹훅이 남았나"를 곧장 짚어 주는 게 낫다.

    던지는 쪽을 골랐다(가짜 200 대신). `notify._post_card`가 예외를 삼켜 `False`를 돌려주므로
    실제 거동은 "카드가 안 나갔다"로 정확히 떨어지고, 반대로 가짜 200을 주면 시험이
    "발사 성공"을 보고 **안 나간 카드를 나갔다고 세는** 더 나쁜 자리가 생긴다.

    ⚠ 시험 클라이언트(`client` 픽스처)는 이 가드를 안 탄다. conftest가 `from httpx import
    AsyncClient`로 원본 클래스를 이미 이름에 붙여 뒀기 때문이다(모듈 속성만 갈아 끼운다).
    자기 목 서버로 쏘는 `test_notify_mattermost.py`도 127.0.0.1이라 그대로 통과한다.
    """
    import httpx

    real_client = httpx.AsyncClient

    def _rides_real_network(client, url) -> bool:
        """이 클라이언트가 **진짜 망**으로 나가나. 가짜 transport를 물렸으면 False.

        ⭐ **이 갈림이 없으면 그물이 가짜까지 잡는다.** 카메라 프록시 시험은
        `camera._new_client`를 갈아 끼워 `httpx.AsyncClient(transport=_FakeJetson())`으로
        도는데, 상류 주소는 젯슨 사설 IP 그대로다. URL만 보고 막으면 **망을 한 번도 안 타는
        시험이 통째로 502**가 된다(2026-08-05에 `tests/test_camera.py` 열셋이 그렇게 깨졌다).

        그물이 막아야 하는 것은 "바깥으로 진짜 나가는 것"이지 "바깥 주소를 쓰는 것"이 아니다.

        ⚠ `_transport_for_url`은 httpx 내부 API다. 판이 바뀌어 사라지면 `_transport`로
        물러서고, 그마저 없으면 **막는 쪽**으로 판정한다 — 그물이 뚫리는 것보다 시끄러운
        쪽이 안전하다.
        """
        pick = getattr(client, "_transport_for_url", None)
        transport = getattr(client, "_transport", None)
        if pick is not None:
            try:
                transport = pick(httpx.URL(url))
            except Exception:  # noqa: BLE001 - 내부 API가 바뀌면 아래 기본값으로 간다
                pass
        if transport is None:
            return True
        return isinstance(transport, httpx.AsyncHTTPTransport)

    class _OutboundPostGuardedClient(real_client):
        async def send(self, request, *args, **kwargs):
            if _rides_real_network(self, request.url) and not _post_target_allowed(request.url):
                raise OutboundPostBlocked(
                    f"시험이 loopback 밖으로 {request.method}를 냈다: {str(request.url)!r}. "
                    "바깥을 타는 갈래는 가짜 transport나 스텁을 깔아라 — 시험은 실기기·"
                    "바깥 서버에 진짜로 붙지 않는다."
                )
            return await super().send(request, *args, **kwargs)

        async def post(self, url, *args, **kwargs):
            if _rides_real_network(self, url) and not _post_target_allowed(url):
                raise OutboundPostBlocked(
                    f"시험이 loopback 밖으로 POST를 냈다: {url!r}. "
                    "매터모스트 웹훅이 .env에 남아 있는지 확인해라 — 시험은 바깥 채널로 "
                    "카드를 쏘지 않는다."
                )
            return await super().post(url, *args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _OutboundPostGuardedClient)
    return _OutboundPostGuardedClient


@pytest.fixture(autouse=True)
def stub_outbound_weather(monkeypatch, block_outbound_post):
    """시험은 바깥 날씨 서버를 **절대** 안 탄다. 맑음 한 장이 늘 돌아온다.

    막는 자리를 `fetch_current_weather`가 아니라 그 아래 httpx로 잡은 이유가 있다 —
    `app/dispatch.py`가 그 함수를 이름으로 가져가서(`from app.weather import ...`) 모듈
    한 곳만 갈아 끼우면 갈래가 샌다. HTTP 경계 한 자리를 막으면 어느 경로로 들어와도 못
    나간다.

    날씨 URL이 아닌 요청은 진짜 클라이언트로 그대로 넘긴다 — 매터모스트·LLM처럼 자기
    스텁을 쓰는 시험을 여기서 건드리면 안 된다. 폴백 갈래를 보는 시험은 이 픽스처 뒤에
    자기 가짜를 덮어써서 그대로 이긴다(test_shuttle_dispatch.py `_fake_client`).

    ⚠ `block_outbound_post`를 인자로 받는 게 **순서 계약**이다. 둘 다 같은
    `httpx.AsyncClient` 속성 한 자리를 갈아 끼우므로, 이 픽스처가 뒤에 서야 POST 가드를
    상속한 채로 두 겹이 겹친다. 그리고 밖에서 보이는 클래스 이름이 `_WeatherGuardedClient`로
    남아야 `test_weather_network_guard.py`가 그물이 살아 있음을 계속 잰다 — 순서가 뒤집히면
    그 이름 검사가 빨개져서 조용히 안 넘어간다.
    """
    import httpx

    from app import weather as weather_mod

    # ⚠ 원본이 아니라 **지금 붙어 있는** 클래스를 상속한다(= POST 가드가 씌워진 판).
    real_client = httpx.AsyncClient
    weather_url = get_settings().weather_api_url

    class _WeatherGuardedClient(real_client):
        async def get(self, url, *args, **kwargs):
            target = str(url)
            # 기상청은 스텁 본문을 안 준다 — **터뜨린다.** 위 `_PINNED_TEST_ENV`가
            # KMA_SERVICE_KEY를 비워 두므로 여기까지 오는 시험은 자기가 일부러 키를 켠
            # 시험뿐이고, 그런 시험은 자기 모의 응답을 들고 와야 한다. 가짜 200을 주면
            # "기상청이 답했다"를 시험이 통과로 세는 더 나쁜 자리가 생긴다.
            if "apis.data.go.kr" in target:
                raise RuntimeError(
                    f"시험이 기상청을 진짜로 부르려 했다: {target!r}. "
                    "KMA 갈래를 보는 시험은 httpx.AsyncClient를 직접 모의해라 "
                    "(tests/test_weather.py의 _kma_client 참고)."
                )
            # "wttr."는 wttr.in(1차)과 wttr.is(대체 도메인) 둘 다 잡는다 — 사슬이 늘어도
            # 시험이 바깥으로 못 나가는 계약은 그대로여야 한다.
            if target == weather_url or "wttr." in target:
                return httpx.Response(
                    200, json=_STUB_WTTR_J1, request=httpx.Request("GET", target)
                )
            return await super().get(url, *args, **kwargs)

    monkeypatch.setattr(weather_mod.httpx, "AsyncClient", _WeatherGuardedClient)
    return _STUB_WTTR_J1


@pytest_asyncio.fixture(loop_scope="session")
async def seed_robot():
    """로봇 행을 심는다. 대시보드 스냅샷의 로봇 카드 검증용.

    robot은 _clean의 TRUNCATE 대상이라 케이스가 끝나면 저절로 비워진다.

    ⛔ **기본값은 계약 낱말이어야 한다.** 예전에는 `mode="AUTO"`·`network_status="online"`을
    심었는데 둘 다 실물에 없는 값이다(`RobotMode` 넷·`CommStatus` 셋). 계약 밖 재료를 심으면
    **죽은 비교가 안 걸린다** — `== "online"`으로 세던 코드가 실제로는 늘 0을 내는데 시험은
    통과했다(`test_assistant_robot_count.py` 참고). 여기가 공유 기본값이라 새 시험이 무심코
    물려받는다.
    """
    from sqlalchemy import insert

    from app.models import Robot
    from app.schemas import CommStatus, RobotMode

    async def _seed(**values) -> int:
        payload = {"name": "patrol-1", "mode": RobotMode.OUTDOOR_TAGGING.value, "battery": 87,
                   "network_status": CommStatus.WS_OK.value, **values}
        async with engine.begin() as conn:
            result = await conn.execute(insert(Robot).values(**payload).returning(Robot.id))
            return result.scalar_one()

    return _seed


@pytest_asyncio.fixture(loop_scope="session")
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    return {"X-API-Key": API_KEY}


@pytest.fixture
def wall_clock(monkeypatch):
    """쿨다운의 벽시계 축을 시험이 직접 감는다.

    실제로 alert_cooldown_sec(10초)를 기다리면 시험이 느려지고 시간에 흔들린다.
    `wall_clock["t"] += 11`처럼 눈금을 밀어서 "실제 시간이 지났다"를 결정적으로 만든다.
    """
    from app.credit import state_machine

    holder = {"t": 1_000.0}
    monkeypatch.setattr(state_machine, "_wall_now", lambda: holder["t"])
    return holder
