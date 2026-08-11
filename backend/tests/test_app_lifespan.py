"""앱 기동 준비(lifespan)가 **실제로 도는지** 잰다.

## 왜 이 파일이 생겼나

`app/main.py`가 계정 열거 방지용 더미 해시를 기동 때 미리 채운다. 그런데 그 자리가
8/2에 조용히 죽어 있었다 — 모듈 머리가 이렇게 적혀 있었다.

    from app import auth                      # 인증 코어
    from app.routers import (..., auth, ...)  # ← 같은 이름을 덮어썼다

뒤에 오는 import가 이겨서 `auth`가 **라우터 모듈**을 가리켰고, lifespan이 부르는
`auth.dummy_password_hash_async`가 없는 속성이 됐다. 실 컨테이너 로그에 매 기동마다
`AttributeError`가 찍혔는데 **아무 시험도 안 깨졌다.** 이유가 둘이다.

  1. lifespan이 예외를 삼킨다(기동을 막을 이유가 없어서 일부러 그렇게 짰다).
  2. 시험은 `httpx.ASGITransport`로 앱을 직접 부르거나 `TestClient`를 쓰는데,
     전자는 lifespan을 **아예 안 태운다.**

그래서 "워밍업이 실패해도 조용하다"는 설계가 "워밍업이 영영 안 돈다"를 가려 줬다.
이 파일은 그 가림막을 걷는다 — lifespan을 직접 태우고 **결과를 실물로 확인**한다.

⚠ 여기서 상태 코드나 예외 없음으로 판정하지 마라. 그게 통과하던 판정이다.
"""
from __future__ import annotations

import asyncio

import pytest

from app import auth
from app import weather as weather_mod
from app.config import get_settings
from app.main import app, lifespan

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_lifespan_warms_the_dummy_password_hash():
    """기동을 태우면 더미 해시 캐시가 실제로 차야 한다.

    캐시가 비어 있으면 첫 로그인 요청이 scrypt를 한 번 더 태운다 — 그 한 번이
    "이 아이디는 없다"를 응답 시간으로 알려주는 오라클이다(백지검토 코덱스 X15).
    """
    auth.dummy_password_hash.cache_clear()
    assert auth.dummy_password_hash.cache_info().currsize == 0, "사전 조건: 캐시가 비어야 한다"

    async with lifespan(app):
        pass

    assert auth.dummy_password_hash.cache_info().currsize == 1, (
        "lifespan이 돌았는데 더미 해시 캐시가 안 찼다 — 워밍업이 죽어 있다"
    )


async def test_lifespan_actually_starts_the_weather_poller(monkeypatch):
    """⭐ 기동이 날씨 주기 조회를 **정말 띄우고**, 종료가 그걸 세우는가 (2026-08-04 전체검토 L22).

    ## 왜 이 케이스가 필요한가 — 워밍업 사문과 글자 그대로 같은 모양이다

    `app/main.py`의 `start_weather_poller(handle_weather_reading)` 한 줄이 **아무 시험도 안
    지나는 자리**였다. 두 겹으로 가려져 있었다.

      1. `conftest._PINNED_TEST_ENV`가 `WEATHER_POLL_INTERVAL_SEC`을 전역으로 `"0"`에 못박아
         `start_weather_poller`가 곧장 `None`을 돌려준다.
      2. lifespan이 그 호출을 `try/except`로 감싸 예외까지 삼킨다.

    그래서 그 줄을 통째로 지워도 전량이 초록이고, 시연장에서는 화면 기상 칩이 **첫 값에
    굳는다.** 8/2에 더미 해시 워밍업이 죽어 있던 것과 같은 계열이라 같은 파일에 둔다.

    ## 무엇으로 판정하나

    "예외가 안 났다"나 "함수가 있다"로 판정하면 안 된다 — 그게 통과하던 판정이다. 여기서는
    **콜백이 실제로 한 번 불렸는지**를 본다. 그러려면 루프가 돌고, 조회가 되고, lifespan이
    넘긴 콜백이 그 자리에 배선돼 있어야 한다. 셋 중 하나만 끊겨도 안 불린다.

    ⚠ 콜백을 `main` 이름칸에서 갈아 끼운다. `app/main.py`가 `from app.dispatch import
    handle_weather_reading`으로 **이름을 들여왔기** 때문에, `app.dispatch` 쪽을 갈아 끼우면
    lifespan은 여전히 진짜 함수를 넘긴다(그 함수는 DB를 탄다). 갈아 끼우는 김에 "lifespan이
    무엇을 넘기나"까지 같이 잰다 — 엉뚱한 콜백을 배선해도 루프는 돌기 때문이다.

    ⚠ 고정 sleep으로 기다리지 않는다. 첫 바퀴는 주기를 안 기다리고 곧장 도니까
    `asyncio.Event`로 그 순간을 잡고 상한만 `wait_for`로 건다.
    """
    import app.main as main_module

    from app.dispatch import handle_weather_reading

    assert main_module.handle_weather_reading is handle_weather_reading, (
        "lifespan이 넘길 콜백이 dispatch의 그 함수가 아니다 — 배선이 딴 이름으로 덮였다"
    )

    got: list[object] = []
    fired = asyncio.Event()

    async def _record(reading):
        got.append(reading)
        fired.set()

    monkeypatch.setattr(main_module, "handle_weather_reading", _record)
    # 이 케이스에서만 주기를 되살린다. conftest가 전역으로 0을 못박아 둔 그 축이다.
    monkeypatch.setenv("WEATHER_POLL_INTERVAL_SEC", "0.05")
    get_settings.cache_clear()
    try:
        assert weather_mod._poller_task is None, "앞 케이스가 폴러를 남겼다"

        async with lifespan(app):
            task = weather_mod._poller_task
            assert task is not None, (
                "기동이 날씨 폴러를 안 띄웠다 — main.py의 start_weather_poller 줄이 죽었다"
            )
            assert not task.done(), "폴러가 뜨자마자 죽었다"
            await asyncio.wait_for(fired.wait(), 5)

        assert got, "폴러가 돌았는데 콜백이 한 번도 안 불렸다 — 배선이 끊겼다"
        assert got[0].ok is True, f"조회가 스텁을 못 탔다: {got[0]!r}"
        # 종료가 폴러를 **세우고 접힐 때까지 기다리는지**까지 본다. 안 세우면 다음 케이스로
        # 새서, 붙은 뒤 첫 메시지를 세는 라이브 WS 케이스가 남의 첫 장을 받는다.
        assert weather_mod._poller_task is None, "종료가 폴러 손잡이를 안 놨다"
        assert task.done(), "종료 뒤에도 폴러가 살아 있다"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await weather_mod.stop_weather_poller()


async def test_lifespan_actually_warms_the_assistant(monkeypatch):
    """⭐ 기동이 관제 보조 데우기를 **정말 부르는가** (2026-08-07 AI 갈래 15차).

    ## 왜 이 케이스가 필요한가 — 위 둘과 글자 그대로 같은 모양이다

    `assistant_warm_up_on_start` 설정이 8/7까지 **읽는 자리가 저장소에 0건**이었다. 필드는
    있는데 아무도 안 봐서, 캐시가 빈 첫 질문이 7.5초를 물었다(두 번째부터는 0.7초다).
    "설정이 있다"가 "그게 돈다"를 가려 준 자리라 더미 해시·날씨 폴러와 같은 파일에 둔다.

    ## 무엇으로 판정하나

    ⚠ **배경 작업이라 `lifespan` 진입만으로는 아직 안 불렸다.** 태스크가 붙잡히기 전에
    단언하면 안 불린 채로 통과한다 — `asyncio.Event`로 그 순간을 잡는다.

    ⚠ 종료가 손잡이를 **놓는지**까지 본다. 안 놓으면 데우기가 다음 케이스로 새서, 뒤에
    오는 관제 보조 시험이 남의 호출을 자기 것으로 센다.
    """
    from app import ai_bridge

    fired = asyncio.Event()
    got: list[object] = []

    async def _record(cfg):
        got.append(cfg)
        fired.set()
        return {"warmed": 2, "failed": 0}

    monkeypatch.setattr(ai_bridge, "warm_up", _record)
    monkeypatch.setenv("ASSISTANT_WARM_UP_ON_START", "true")
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            await asyncio.wait_for(fired.wait(), 5)

        assert got, "기동이 데우기를 안 불렀다 — main.py의 배선이 죽었다"
        assert got[0].model, f"설정이 안 실려 왔다: {got[0]!r}"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


async def test_lifespan_skips_the_warm_up_when_the_switch_is_off(monkeypatch):
    """⛔ 스위치를 끄면 **안 불러야** 한다.

    끄는 갈래가 안 지켜지면 스위치가 장식이 된다. 위 케이스만 있으면 `if` 조건을 통째로
    지워도 전량이 초록이다.

    ⚠ 데우기가 터져도 **기동이 안 막히는지**까지 같이 본다 — 여기서 예외가 새면
    `lifespan` 진입 자체가 죽어서 이 케이스가 통과할 수 없다.
    """
    from app import ai_bridge

    called: list[object] = []

    async def _boom(cfg):
        called.append(cfg)
        raise RuntimeError("모델 서버가 죽어 있다")

    monkeypatch.setattr(ai_bridge, "warm_up", _boom)
    monkeypatch.setenv("ASSISTANT_WARM_UP_ON_START", "false")
    get_settings.cache_clear()
    try:
        async with lifespan(app):
            await asyncio.sleep(0)  # 배경 작업이 있었다면 여기서 한 번 깨어난다

        assert not called, "스위치를 껐는데 데우기가 돌았다 — if 조건이 죽었다"
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_main_holds_the_auth_core_not_the_router():
    """`app.main`이 쥔 인증 모듈이 **코어**여야 한다(라우터가 아니라).

    이 단언이 이 파일의 뿌리다. 이름이 덮이면 `test_lifespan_warms_the_dummy_password_hash`가
    캐시로 잡지만, 여기서 한 번 더 못박아 두면 원인이 한눈에 보인다.

    ⚠ **범위는 `auth` 한 이름뿐이다.** `app/`과 `app/routers/`에 같은 이름으로 있는 모듈이
    `auth`·`dispatch`·`stats` 셋인데 여기서 재는 건 `auth`다. 나머지 둘을 라우터 import가
    덮어도 이 시험은 초록이니, "이름 덮어쓰기 일반"을 막는 그물로 읽지 마라(2026-08-02
    2차 검토 G51). 지금 `auth` 갈래는 main.py가 `from app import auth as auth_core`로
    별칭을 써서 구조로도 한 겹 막혀 있다.
    """
    import app.main as main_module

    assert main_module.auth_core is auth, "app.main의 인증 코어가 딴 모듈로 덮였다"
    assert hasattr(main_module.auth_core, "dummy_password_hash_async")
