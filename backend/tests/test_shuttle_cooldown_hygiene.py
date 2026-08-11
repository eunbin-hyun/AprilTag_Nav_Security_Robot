"""접점③ — 셔틀 호출 쿨다운이 케이스·파일 경계를 새는지 못박는다.

## 무엇을 잡는 시험인가

`/api/shuttle-calls`의 쿨다운은 `app/shuttle_call.py`의 인메모리 dict라 conftest의
TRUNCATE로 안 지워진다. 예전에는 `test_shuttle_call.py`가 **자기 파일 안** autouse
픽스처로 비웠는데, 그 방식은 구조적으로 자기 파일만 지킨다 — 셔틀 호출을 부르는 **다음
파일**은 앞 파일이 남긴 눈금을 그대로 물려받아 원인 모를 429로 깨진다. 화면 창구를 쓰는
시험이 앞으로 늘어날 자리라(각본 ①이 화면에서 시작한다) 함정이 다시 밟힐 자리다.

그래서 초기화를 conftest `_clean`으로 올렸고, 이 파일이 그 수리의 반증 자리다.

## 가드가 병렬에서 조용히 무력해지던 자리 (2026-07-30 2차)

1차 판은 **파일 경계 오염**에만 기댔다. 짝인 `test_shuttle_call.py`의 1차 호출이 같은
프로세스에서 먼저 돌아야 눈금이 남는데, conftest가 pytest-xdist를 지원해서
(`PYTEST_XDIST_WORKER`·`_ensure_worker_database`) `pytest -n auto`면 두 케이스가 서로 다른
워커 프로세스로 흩어진다. 그러면 물려받을 눈금이 아예 없어서 **가드가 아무것도 안 지키는데
초록**이다. 교차 검증이 실측했다 — reset을 no-op으로 만들고 직렬로 돌리면 1 failed인데
`-n 2`만 붙이면 2 passed였다.

그래서 이 파일은 오염원을 **바깥 파일이 아니라 이 모듈의 훅**으로 옮겼다. `_leak_before_clean`
(모듈 범위)과 `_replant_leak_for_next_case`(케이스 범위 teardown)가 매 케이스 `_clean`
**앞에** 눈금을 심는다. 심는 자리와 검사하는 자리가 한 프로세스라 워커가 몇 개든, 케이스가
어느 워커로 흩어지든 재현이 성립한다. 픽스처 순서는 pytest 규칙 그대로다 — 높은 범위가
먼저 서고, 앞 케이스 teardown이 다음 케이스 setup보다 먼저다.

파일 경계 재현(`test_앞_파일_호출이_이_파일_호출을_삼키지_않는다`)은 그대로 남긴다. 진짜
파일 사이 누출을 보는 유일한 케이스라서다. 다만 짝이 딴 워커로 갔으면 조용히 통과하지 않고
**이유를 적고 skip**한다 — 초록으로 위장하는 게 이 결함의 본체였다.

## 실측 (2026-07-30, c207_seam_b_test)

- conftest `_clean`에 `reset_shuttle_call_state()`가 **없을 때** — 직렬이든 `-n 2`든
  `test_초기화_훅이_앞_케이스_눈금을_지운다`가 429로 빨개진다.
- 올린 **뒤** — 직렬·`-n 2` 둘 다 초록. 수리 전후를 두 방식으로 다 재서 남겼다.
"""
from __future__ import annotations

import pytest

from app import shuttle_call
from tests import test_shuttle_call as call_tests
from tests.test_shuttle_call import FROZEN_MONO, POLLUTION_GATE

pytestmark = pytest.mark.asyncio(loop_scope="session")

# 모듈 훅이 심는 눈금 자리. 파일 경계 재현이 쓰는 POLLUTION_GATE와 갈라 둔다 — 그쪽은 시각을
# 얼린 눈금이라 실시간 눈금을 같은 게이트에 겹치면 두 재현이 서로를 흔든다.
LEAK_GATE = 12

# 눈금이 실제로 심겼다는 증거. 비어 있으면 오염원이 안 돌았다는 뜻이라, 시험이 "지워졌다"를
# 봐도 아무것도 증명하지 못한다(거짓 초록). 그래서 케이스가 이 값을 같이 본다.
_planted: list[float] = []


def _plant_leak() -> None:
    """다음 케이스로 새야 하는 눈금을 심는다.

    심고 나서 심긴 것까지 여기서 확인한다 — 안 심겼는데 케이스가 "깨끗하다"를 보고 초록이면
    가드가 무력해진 걸 못 본다. 1차 판이 병렬에서 당한 게 정확히 그 모양이었다.
    """
    shuttle_call.mark_call(LEAK_GATE, None)
    remain = shuttle_call.in_call_cooldown(LEAK_GATE, None)
    assert remain > 0, "오염원이 눈금을 못 심었다 — 이 시험은 아무것도 못 지킨다"
    _planted.append(remain)


@pytest.fixture(scope="module", autouse=True)
def _leak_before_clean():
    """첫 케이스 몫 눈금을 심고, 파일이 끝나면 되돌린다.

    모듈 범위라 케이스 범위 autouse인 conftest `_clean`보다 **먼저** 선다(pytest는 높은
    범위를 먼저 세운다). 그래서 "앞 케이스가 남긴 눈금"과 같은 자리에 놓인다.

    끝에 되돌리는 건 검사 위생이다 — 심은 눈금을 파일 밖으로 흘리면 다음 파일이 우리가 만든
    오염을 물려받는다(그게 애초에 이 파일이 잡는 함정이다).
    """
    _plant_leak()
    yield
    shuttle_call.reset_shuttle_call_state()


@pytest.fixture(autouse=True)
def _replant_leak_for_next_case():
    """케이스가 끝날 때마다 눈금을 다시 심는다.

    모듈 훅은 한 번만 돌아서 첫 케이스만 덮는다. 케이스 순서가 바뀌거나 케이스가 늘면
    가드가 조용히 무력해지니(1차 판이 당한 것과 같은 결), 매 케이스 앞에 눈금이 있게 만든다.
    teardown에서 심으면 다음 케이스 setup의 `_clean`보다 앞이다.
    """
    yield
    _plant_leak()


@pytest.fixture
def frozen_mono(monkeypatch):
    """1차 호출 케이스와 **같은 눈금**을 보게 시각을 얼린다.

    실시간에 기대면 두 파일 사이에 5초가 그냥 지나가 재현이 시간에 흔들린다. 눈금을 맞추면
    "쿨다운 창이 안 비워졌다"만 남는다.
    """
    holder = {"t": FROZEN_MONO}
    monkeypatch.setattr(shuttle_call, "_mono", lambda: holder["t"])
    return holder


async def test_초기화_훅이_앞_케이스_눈금을_지운다(client):
    """병렬에서도 무는 반증 자리 — `_clean`이 reset을 안 부르면 여기가 429다.

    오염원이 이 모듈 훅이라 워커 배분과 무관하다. 시각을 안 얼리는 이유는 눈금이 방금
    (앞 케이스 teardown이나 모듈 setup에서) 심긴 실시간 값이라, 창 5초 안에 이 케이스가
    도는 게 확실해서다.
    """
    assert _planted, "오염원 훅이 안 돌았다 — 눈금 없이 초록이면 가드가 무력해진 것이다"
    assert shuttle_call.in_call_cooldown(LEAK_GATE, None) == 0.0, (
        "앞 케이스가 남긴 눈금이 그대로다 — conftest _clean이 "
        "reset_shuttle_call_state()를 안 부른다"
    )
    res = await client.post("/api/shuttle-calls", json={"gate_no": LEAK_GATE})
    assert res.status_code == 200, (
        f"눈금을 물려받아 호출이 삼켜졌다 (응답={res.text})"
    )


async def test_앞_파일_호출이_이_파일_호출을_삼키지_않는다(client, frozen_mono):
    """수리 전 실측 — 여기가 429였다(앞 파일 1차 호출의 쿨다운을 물려받았다).

    ⚠ 이 케이스만 파일 경계를 본다. 짝(`test_shuttle_call.test_쿨다운_오염_재현_1차_호출`)이
    같은 프로세스에서 **먼저** 돌아야 성립한다. 안 돌았을 때 조용히 통과하면 "가드가 초록인데
    아무것도 안 지킨다"가 되므로, 이유를 적고 skip한다.

    ⚠ 2026-08-04 전체검토 L23 — 예전 skip 사유는 방아쇠를 "xdist 분배" 하나로 적어서 사람을
    잘못 이끌었다. 짝이 안 도는 길은 셋이고, **평소 도는 직렬 판에서 흔한 건 xdist가 아니다.**

      1. 파일 하나만 골라 돌렸다(`pytest tests/test_shuttle_cooldown_hygiene.py`) — 제일 잦다.
      2. 시험 순서가 바뀌어 이 파일이 짝보다 먼저 왔다(무작위 순서·`-k` 선택).
      3. `pytest -n`으로 두 케이스가 다른 워커 프로세스로 흩어졌다.

    셋 다 "가드가 헛돈다"는 같은 뜻인데 고칠 자리가 달라서, 어느 길인지를 사유에 적는다.

    ⚠ 검토 문서는 "이 저장소엔 pytest-xdist가 안 깔려 있다"고 적었는데 **실물은 반대다** —
    `requirements-dev.txt:12`에 `pytest-xdist==3.8.0`이 있고 venv에도 깔려 있다. 그래서 3번을
    지우지 않고 남겼다. 다만 `PYTEST_XDIST_WORKER`가 없으면 3번은 아니라고 말할 수 있다.
    """
    if not call_tests.POLLUTION_CALLED:
        import os

        worker = os.environ.get("PYTEST_XDIST_WORKER")
        cause = (
            f"xdist 워커({worker})가 짝을 딴 프로세스로 가져갔다"
            if worker
            else "직렬 실행인데 짝이 이 프로세스에서 안 돌았다 — 이 파일만 골라 돌렸거나 "
                 "시험 순서가 짝보다 앞섰다"
        )
        pytest.skip(
            f"짝(test_shuttle_call.test_쿨다운_오염_재현_1차_호출)이 안 돌아 파일 경계 "
            f"재현이 성립하지 않는다: {cause}. "
            "전량(`pytest tests/`)으로 돌리면 이 케이스가 산다. "
            "어느 판에서든 무는 가드는 test_초기화_훅이_앞_케이스_눈금을_지운다다."
        )
    res = await client.post("/api/shuttle-calls", json={"gate_no": POLLUTION_GATE})
    assert res.status_code == 200, (
        "앞 파일이 남긴 쿨다운을 물려받았다 — conftest _clean이 "
        f"reset_shuttle_call_state()를 안 부른다 (응답={res.text})"
    )


async def test_초기화_훅이_실제로_쿨다운_dict를_비운다():
    """훅이 부르는 함수가 정말 비우는지 직접 본다.

    위 시험들은 "이 케이스 앞에 비워졌다"까지만 본다. 훅이 지나간 뒤 케이스 안에서 새로 찍힌
    눈금은 그대로 남아야 정상이라, 초기화 함수 자체의 계약은 따로 못박는다.
    """
    shuttle_call.mark_call(POLLUTION_GATE, None)
    assert shuttle_call.in_call_cooldown(POLLUTION_GATE, None) > 0

    shuttle_call.reset_shuttle_call_state()
    assert shuttle_call.in_call_cooldown(POLLUTION_GATE, None) == 0.0
