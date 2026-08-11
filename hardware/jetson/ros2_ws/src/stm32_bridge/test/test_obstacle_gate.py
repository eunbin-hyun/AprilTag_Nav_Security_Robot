"""ObstacleGate 단위시험 — 장애물 HOLD·복구 정책.

`robot_navigation/test/test_obstacle_recovery.py` 와 **같은 규칙**을 검사한다.
그쪽은 ROS 주행 상태머신, 이쪽은 UART 를 직접 소유하는 도구(`drive_probe`)용
구현이다. 규칙을 고칠 때는 두 파일을 같이 돌려야 한다.
"""
from stm32_bridge import protocol as P
from stm32_bridge.obstacle_gate import (
    HOLD_DEBOUNCE, HOLD_PRESENT, HOLD_REARM_WAIT, ObstacleGate, RUN,
)


def tel(state, *, obstacle=False, extra_faults=0, last_seq=0):
    """0x80 한 프레임 (필요한 필드만)."""
    faults = (P.FAULT_OBSTACLE_NEAR if obstacle else 0) | extra_faults
    return {
        'drive_state': state,
        'active_fault_bits': faults,
        'measured_speed_mm_s': 0,
        'motor_duty_permille': 0,
        'encoder_count': 0,
        'yaw_cdeg': 0,
        'last_drive_seq': last_seq,
    }


def test_runs_when_clear_and_ready():
    g = ObstacleGate()
    assert g.update(0.0, tel(P.STATE_DRIVING), 0.02) is True
    assert g.substate == RUN
    assert not g.holding


def test_obstacle_holds_and_blocks_drive():
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    assert g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02) is False
    assert g.substate == HOLD_PRESENT
    assert g.hold_count == 1


def test_safe_stop_with_bit16_clear_is_not_abort():
    """시나리오 B — bit16=0 인데 아직 SAFE_STOP. 중단 사유가 아니다."""
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    for _ in range(8):
        assert g.update(t, tel(P.STATE_SAFE_STOP), 0.02) is False
        assert not g.aborted, g.abort_reason
        t += 0.05
    assert g.substate == HOLD_DEBOUNCE


def test_ready_before_debounce_does_not_resume():
    g = ObstacleGate(clear_hold_s=0.5)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    while t < 0.6:
        assert g.update(t, tel(P.STATE_READY), 0.02) is False, f't={t}'
        t += 0.05


def test_debounce_done_but_not_ready_does_not_resume():
    g = ObstacleGate(clear_hold_s=0.5, ready_timeout_s=2.0)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    for _ in range(20):
        assert g.update(t, tel(P.STATE_SAFE_STOP), 0.02) is False
        t += 0.05
    assert g.substate == HOLD_REARM_WAIT
    assert not g.aborted


def test_resumes_when_clear_and_ready():
    g = ObstacleGate(clear_hold_s=0.5)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    while t <= 0.6 + 1e-9:
        g.update(t, tel(P.STATE_SAFE_STOP), 0.02)
        t += 0.05
    assert g.update(t, tel(P.STATE_READY), 0.02) is True
    assert g.substate == RUN
    assert g.hold_count == 1
    assert g.hold_total_s > 0.4


def test_reappearing_obstacle_resets_timers():
    g = ObstacleGate(clear_hold_s=0.5)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    for t in (0.15, 0.2, 0.25, 0.3):
        g.update(t, tel(P.STATE_SAFE_STOP), 0.02)
    g.update(0.35, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    assert g.substate == HOLD_PRESENT
    # 리셋됐으므로 0.35+0.5=0.85 이전엔 READY 여도 재개 못 한다
    assert g.update(0.7, tel(P.STATE_READY), 0.02) is False
    assert g.hold_count == 1        # 같은 HOLD 안에서의 재등장


def test_ready_timeout_aborts_with_reason():
    g = ObstacleGate(clear_hold_s=0.5, ready_timeout_s=2.0)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    while t < 5.0 and not g.aborted:
        g.update(t, tel(P.STATE_SAFE_STOP), 0.02)
        t += 0.05
    assert g.aborted
    assert 'READY' in g.abort_reason


def test_other_blocking_fault_aborts_immediately():
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    # bit9 STEERING_INVALID 가 뜨면 복구 대기에 숨기지 않는다
    g.update(0.15, tel(P.STATE_SAFE_STOP,
                       extra_faults=P.FAULT_STEERING_INVALID), 0.02)
    assert g.aborted
    assert 'STEERING_INVALID' in g.abort_reason


STALE_0X8100 = P.FAULT_RANGE_LOST | P.FAULT_SENSOR_STALE


def test_bit15_alone_while_holding_is_graced_not_aborted():
    """현장 로그 0x8100 — HOLD 중 bit15 단독은 짧게 유예한다 (§7)."""
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    assert g.update(0.15, tel(P.STATE_SAFE_STOP,
                              extra_faults=STALE_0X8100), 0.02) is False
    assert not g.aborted, g.abort_reason
    assert g.substate == HOLD_PRESENT
    # 측정이 살아나 bit16 으로 돌아와도 그대로 HOLD
    g.update(0.20, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    assert not g.aborted, g.abort_reason


def test_bit15_longer_than_grace_aborts():
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    while not g.aborted and t < 1.0:
        g.update(t, tel(P.STATE_SAFE_STOP, extra_faults=STALE_0X8100), 0.02)
        t += 0.05
    assert g.aborted, '상한을 넘겼는데 계속 유예했다'
    assert 'SENSOR_STALE' in g.abort_reason
    assert t > 0.40, f'너무 이르게 중단: t={t}'


def test_bit15_while_running_aborts_immediately():
    """HOLD 가 아닌 정상 주행 중 bit15 는 유예 없이 즉시 중단."""
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.05, tel(P.STATE_DRIVING, extra_faults=STALE_0X8100), 0.02)
    assert g.aborted
    assert 'SENSOR_STALE' in g.abort_reason


def test_bit15_with_other_blocking_fault_aborts_immediately():
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    g.update(0.15, tel(P.STATE_SAFE_STOP,
                       extra_faults=STALE_0X8100 | P.FAULT_STEERING_INVALID),
             0.02)
    assert g.aborted
    assert 'STEERING_INVALID' in g.abort_reason


def test_telemetry_loss_during_grace_aborts():
    g = ObstacleGate(telemetry_timeout_s=0.5)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    g.update(0.15, tel(P.STATE_SAFE_STOP, extra_faults=STALE_0X8100), 0.02)
    assert not g.aborted
    g.update(0.20, tel(P.STATE_SAFE_STOP, extra_faults=STALE_0X8100), 0.9)
    assert g.aborted
    assert 'telemetry' in g.abort_reason


def test_grace_restarts_for_each_flicker():
    """유예는 flicker 마다 새로 센다 — 타이머를 안 버리면 두 번째에 중단된다."""
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    t = 0.15
    for _ in range(4):
        g.update(t, tel(P.STATE_SAFE_STOP, extra_faults=STALE_0X8100), 0.02)
        assert not g.aborted, f't={t}: {g.abort_reason}'
        t += 0.05
        g.update(t, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
        assert not g.aborted, f't={t}: {g.abort_reason}'
        t += 0.05


def test_bit15_clear_restarts_debounce():
    """bit15 가 떠 있던 시간을 clear debounce 에 세지 않는다."""
    g = ObstacleGate()
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    for t in (0.15, 0.20, 0.25, 0.30):
        g.update(t, tel(P.STATE_SAFE_STOP, extra_faults=STALE_0X8100), 0.02)
    t = 0.35                               # 여기서부터 0.5 s
    for _ in range(9):                     # 0.35 → 0.75 (0.40 s, 미달)
        assert g.update(t, tel(P.STATE_READY), 0.02) is False, (
            f't={t} 에서 재개했다 — bit15 구간을 debounce 에 세었다')
        t += 0.05
    assert g.update(0.90, tel(P.STATE_READY), 0.02) is True
    assert g.substate == RUN


def test_report_only_fault_does_not_abort():
    """REPORT_ONLY(CRC_ERROR 등) 는 주행을 막지 않는다."""
    g = ObstacleGate()
    assert g.update(0.0, tel(P.STATE_DRIVING,
                             extra_faults=P.FAULT_CRC_ERROR), 0.02) is True
    assert not g.aborted


def test_telemetry_loss_aborts_even_while_holding():
    g = ObstacleGate(telemetry_timeout_s=0.5)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    g.update(0.1, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
    g.update(0.2, tel(P.STATE_SAFE_STOP, obstacle=True), 0.9)
    assert g.aborted
    assert '단절' in g.abort_reason


def test_no_telemetry_at_all_aborts():
    g = ObstacleGate()
    assert g.update(0.0, None, 999.0) is False
    assert g.aborted
    assert '미수신' in g.abort_reason


def test_permanent_obstacle_aborts_at_max_hold():
    g = ObstacleGate(max_hold_s=1.0)
    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    t = 0.1
    while t < 5.0 and not g.aborted:
        g.update(t, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
        t += 0.05
    assert g.aborted
    assert '치워지지' in g.abort_reason


def test_hold_elapsed_accumulates_across_two_holds():
    g = ObstacleGate(clear_hold_s=0.2)

    def recover(start):
        t = start
        g.update(t, tel(P.STATE_SAFE_STOP, obstacle=True), 0.02)
        t += 0.05
        while t <= start + 0.05 + 0.2 + 1e-9:
            g.update(t, tel(P.STATE_SAFE_STOP), 0.02)
            t += 0.05
        assert g.update(t, tel(P.STATE_READY), 0.02) is True
        return t

    g.update(0.0, tel(P.STATE_DRIVING), 0.02)
    t = recover(0.1)
    first = g.hold_total_s
    assert first > 0.2
    t = recover(t + 0.5)
    assert g.hold_count == 2
    assert g.hold_total_s > first
