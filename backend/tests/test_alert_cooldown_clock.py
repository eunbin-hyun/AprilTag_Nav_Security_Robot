"""미태깅 경고 쿨다운의 시계 기준 시험 (E2E 관통검사 F2).

쿨다운 창을 서버 벽시계로 재면, 같은 시나리오를 배속으로 돌릴 때 이벤트가 벽시계로 더
촘촘히 도착해 경고가 조용히 줄어든다. 판정은 이벤트 시각끼리 비교해서 멀쩡한데 경고만
두 시계가 섞이던 자리다. 여기서는 세 가지를 못박는다.

1. 이벤트 시각이 쿨다운보다 멀면, 벽시계로 붙어서 들어와도 경고가 두 건 다 난다.
2. 같은 사건을 벽시계 간격만 바꿔 돌려도 경고 수가 같다(배속 불변).
3. offset 없는 시각이 들어와도 500이 나지 않는다.
4. 기기 시계가 멈추거나 뒤로 튀어도 벽시계 창이 지나면 경고가 다시 난다.
5. 시계 어긋난 기기 둘이 번갈아 들어와도 쿨다운이 유지된다.

4·5는 이벤트 축만 쓰던 판이 만든 결함이라, 두 축을 AND로 묶은 뒤의 회귀 방지다.

⚠ 2026-07-30부터 쿨다운이 억제하는 건 **외부 알림(팝업·매터모스트)뿐이고 Alert 행은 미태깅마다
무조건 남는다.** 예전엔 Alert insert 자체가 쿨다운 안에 있어서 창에 삼켜진 사건이 통째로
사라졌다(실서버 미태깅 297건 중 279건). 그래서 아래 케이스들은 `notify`로 억제를 재고,
Alert 행 수는 "사건이 다 남았나"를 재는 딴 잣대로 쓴다.
"""
import datetime as dt

import pytest
from sqlalchemy import func, select

from app.config import get_settings
from app.credit.state_machine import Verdict, evaluate_pass
from app.db import get_session
from app.models import Alert

pytestmark = pytest.mark.asyncio(loop_scope="session")

BASE = dt.datetime(2026, 7, 24, 3, 0, 0, tzinfo=dt.timezone.utc)


async def _untagged_pass(event_id: str, offset_sec: float, gate_no: int = 1, tz: bool = True):
    """크레딧 없는 A→B 통과 한 건. 판정은 항상 untagged다."""
    ts = BASE + dt.timedelta(seconds=offset_sec)
    if not tz:
        ts = ts.replace(tzinfo=None)
    async with get_session() as s:
        return await evaluate_pass(
            s, event_id=event_id, device_id="raspberry01", gate_no=gate_no,
            direction="A_TO_B", status="complete",
            beam_a_ts=ts, beam_b_ts=None, observed_at=ts,
        )


async def _count_alerts() -> int:
    async with get_session() as s:
        return (await s.execute(select(func.count()).select_from(Alert))).scalar_one()


# 1. 이벤트 시각이 쿨다운 밖이면 벽시계로 붙어 와도 경고가 두 건 난다
async def test_cooldown_window_uses_event_clock_not_wall_clock():
    cooldown = get_settings().alert_cooldown_sec
    ev1 = await _untagged_pass("P1", 1.0)
    # 이벤트 시각으로는 쿨다운을 넘겼지만 벽시계로는 같은 순간에 들어온 두 번째 건.
    ev2 = await _untagged_pass("P2", 1.0 + cooldown + 2.0)

    assert ev1.verdict == Verdict.UNTAGGED and ev2.verdict == Verdict.UNTAGGED
    assert ev1.notify is True and ev1.alert_id is not None
    assert ev2.notify is True and ev2.alert_id is not None, "벽시계 쿨다운이 두 번째 경고를 삼켰다"
    assert await _count_alerts() == 2


# 2. 배속 불변 — 벽시계 간격을 벌려도 경고 수가 같다
async def test_alert_count_is_invariant_to_wall_clock_spacing(wall_clock):
    """같은 사건을 벽시계 간격만 바꿔 두 번 돌려도 경고 수가 같다.

    ⚠ 예전 판은 **배속을 실제로 안 벌렸다** (2026-08-02 백지 검토 F69). 느린 판이 케이스마다
      `asyncio.sleep(0.05)`을 넣었는데 쿨다운이 10초라, 0초 간격과 0.05초 간격은 벽시계 축에서
      똑같이 "창 안"이다 — 두 판이 사실상 같은 조건을 두 번 돈 셈이라 이 시험이 아무것도
      안 갈랐다. 게다가 실시간에 기대서 배치 부하에 흔들렸다.

      그래서 `wall_clock`으로 눈금을 직접 밀어 **창을 진짜로 넘긴다.** 빠른 판은 눈금이 한 칸도
      안 움직이고(세 건이 같은 순간에 도착한 모습), 느린 판은 건마다 쿨다운보다 더 민다.

    두 판이 정말 다른 조건이었나까지 여기서 못박는다(아래 대조군 둘). 안 그러면 벽시계 축이
    통째로 죽어도 "불변"이 초록으로 남는다.
    """
    cooldown = get_settings().alert_cooldown_sec
    offsets = [1.0, 1.0 + cooldown + 2.0, 1.0 + (cooldown + 2.0) * 2]

    # 빠른 판 — 벽시계 눈금을 한 칸도 안 민다.
    started_at = wall_clock["t"]
    for i, off in enumerate(offsets):
        await _untagged_pass(f"FAST{i}", off)
    fast = await _count_alerts()
    assert wall_clock["t"] == started_at, "빠른 판에서 벽시계가 움직였다 — 두 판 조건이 안 갈렸다"

    # 느린 판 — 같은 사건인데 벽시계로는 건마다 쿨다운을 넘겨 흘려보낸다(게이트를 갈라
    # 앞 라운드와 안 섞이게 한다).
    for i, off in enumerate(offsets):
        wall_clock["t"] += cooldown + 2.0
        await _untagged_pass(f"SLOW{i}", off, gate_no=2)
    slow = await _count_alerts() - fast

    assert fast == 3
    assert slow == fast, f"벽시계 간격에 따라 경고 수가 달라졌다(빠름 {fast} · 느림 {slow})"

    # ── 대조군 — 두 판이 벽시계 축에서 정말 반대편에 있었나 ───────────────
    # ① 눈금을 안 밀면(빠른 판과 같은 조건) 이벤트 축까지 창 안인 짝은 삼켜져야 한다.
    ctrl_first = await _untagged_pass("CTRL0", 1.0, gate_no=9)
    ctrl_same_tick = await _untagged_pass("CTRL1", 1.5, gate_no=9)
    assert ctrl_first.notify is True
    assert ctrl_same_tick.notify is False, (
        "벽시계를 안 밀었는데 쿨다운이 안 물었다 — 벽시계 축이 죽어서 위 두 판이 "
        "같은 조건을 두 번 돈 것이다"
    )

    # ② 느린 판이 쓴 간격만큼 밀면 같은 짝이 다시 울려야 한다(창을 실제로 넘겼다는 증거).
    wall_clock["t"] += cooldown + 2.0
    ctrl_next_tick = await _untagged_pass("CTRL2", 1.6, gate_no=9)
    assert ctrl_next_tick.notify is True, (
        "느린 판이 쓴 벽시계 간격이 쿨다운 창을 안 넘겼다 — 배속을 벌린 게 아니다"
    )


# 3. 쿨다운 안이면 여전히 삼킨다 (기존 계약 유지)
async def test_within_event_window_still_suppressed():
    ev1 = await _untagged_pass("P1", 1.0)
    ev2 = await _untagged_pass("P2", 1.5)  # 이벤트 시각으로 0.5초 뒤 = 쿨다운 안
    assert ev1.notify is True
    # 알림만 삼켜진다 — 사건 기록은 남는다.
    assert ev2.notify is False and ev2.alert_id is not None
    assert await _count_alerts() == 2


# 4. offset 없는 시각이 와도 500이 아니다
async def test_naive_timestamp_does_not_crash_cooldown():
    ev1 = await _untagged_pass("P1", 1.0, tz=False)
    ev2 = await _untagged_pass("P2", 1.5, tz=False)
    assert ev1.notify is True and ev2.notify is False

    # aware 표시가 남은 뒤 naive 가 들어와도 TypeError 로 죽지 않는다
    ev3 = await _untagged_pass("P3", 2.0, gate_no=3, tz=True)
    ev4 = await _untagged_pass("P4", 2.5, gate_no=3, tz=False)
    assert ev3.notify is True and ev4.notify is False


# 5. 시계가 뒤로 튀어도 잠기지 않는다 — 다만 푸는 축은 벽시계다
async def test_backward_clock_jump_recovers_on_wall_clock(wall_clock):
    cooldown = get_settings().alert_cooldown_sec
    ev_future = await _untagged_pass("P1", 3600.0)   # 시계가 1시간 앞으로 튄 건
    ev_now = await _untagged_pass("P2", 5.0)          # 제 시각으로 돌아온 건
    assert ev_future.notify is True
    # 경과가 음수여도 삼킨다. 음수를 우회로 뚫으면 시계 어긋난 기기가 쿨다운을 무력화한다.
    assert ev_now.notify is False

    # 봉쇄를 푸는 건 벽시계 축이다 — 실제 시간이 창을 넘기면 다시 울린다.
    wall_clock["t"] += cooldown + 1.0
    ev_later = await _untagged_pass("P3", 5.5)
    assert ev_later.notify is True, "미래 시각 한 건이 이후 경고를 잠갔다"
    # 울린 건 둘(ev_future·ev_later)인데 사건은 셋 다 남는다.
    assert await _count_alerts() == 3


# 6. 기기 시계가 멈춰도 벽시계 창이 지나면 경고가 다시 난다 (영구 봉쇄 회귀)
async def test_frozen_device_clock_recovers_after_wall_window(wall_clock):
    cooldown = get_settings().alert_cooldown_sec

    # 같은 beam_a_ts로 6건 — 기기 시계가 멈춘 모습. 실제 시간도 안 흘렀으니 첫 건만 운다.
    first = await _untagged_pass("FZ0", 7.0, gate_no=4)
    assert first.notify is True
    for i in range(1, 6):
        ev = await _untagged_pass(f"FZ{i}", 7.0, gate_no=4)
        assert ev.notify is False

    # 시각은 여전히 안 움직이는데 실제 시간이 쿨다운을 넘겼다.
    wall_clock["t"] += cooldown + 1.0
    revived = await _untagged_pass("FZ9", 7.0, gate_no=4)
    assert revived.notify is True, "기기 시계가 멈추자 미태깅 경고가 영구 봉쇄됐다"
    # 울린 건 둘(first·revived)인데 사건은 7건 다 남는다.
    assert await _count_alerts() == 7


# 7. 한 게이트에 시계 어긋난 기기 둘이 번갈아 들어와도 쿨다운이 유지된다
async def test_skewed_device_clocks_do_not_defeat_cooldown(wall_clock):
    # 기기 A는 시계가 1시간 빠르고 기기 B는 제 시각이다. 둘이 번갈아 미태깅을 만든다.
    ev_a1 = await _untagged_pass("SK0", 3600.0, gate_no=5)
    ev_b1 = await _untagged_pass("SK1", 0.0, gate_no=5)
    ev_a2 = await _untagged_pass("SK2", 3601.0, gate_no=5)
    ev_b2 = await _untagged_pass("SK3", 1.0, gate_no=5)

    assert ev_a1.notify is True
    for ev in (ev_b1, ev_a2, ev_b2):
        assert ev.notify is False, "음수 경과가 우회로가 돼서 쿨다운이 무력화됐다"
    # 울린 건 하나인데 사건은 4건 다 남는다.
    assert await _count_alerts() == 4
