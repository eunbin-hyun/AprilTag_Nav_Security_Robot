"""healthz 스모크."""
import pytest

from app.db import get_db
from app.main import app

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_healthz_ok(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "up"


async def test_healthz_returns_200_even_when_db_is_down(client):
    """DB가 끊겨도 `/healthz`는 200이고 `db`만 down이다 (2026-08-02 백지 검토 C9).

    ⚠ 이 계약을 시험이 못 박아 두는 이유는 **판정하는 쪽이 상태 코드만 보면 안 되기 때문**이다.
      `query.healthz`는 `except Exception`으로 db_state를 down으로 적고도 200을 낸다. 그래서
      compose healthcheck·배포 스모크가 200만 보면 DB가 끊긴 컨테이너가 healthy로 보인다.
      실제로 그 자리를 재는 케이스가 하나도 없었다 — 지금은 compose healthcheck와
      `deploy/jenkins-deploy.sh` 스모크가 둘 다 본문의 `db`까지 읽는다.

    상태 코드를 500으로 바꾸는 수리는 안 한다. 200 + `db:down`이 이미 계약이고, 바꾸면
    화면·프록시가 함께 달라진다.
    """

    class _BrokenSession:
        """`SELECT 1`이 터지는 세션. healthz는 execute 하나만 쓴다."""

        async def execute(self, *args, **kwargs):
            raise RuntimeError("DB 커넥션이 끊겼다(모의)")

    async def _broken_db():
        yield _BrokenSession()

    # try/finally로 반드시 되돌린다 — 안 되돌리면 뒤따르는 케이스가 통째로 이 세션을 문다.
    app.dependency_overrides[get_db] = _broken_db
    try:
        resp = await client.get("/healthz")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "down"
