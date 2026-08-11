"""초음파 장애물 HOLD 복구 회귀시험 (ROS·UART 없이 돌아간다).

`jetson_ultrasonic_hold_recovery_prompt.md` §필수 테스트 1~13 을 고정한다.

고치기 전의 결함: `tick()` 이 `not stm32_ready` 를 장애물 HOLD 처리보다 먼저
검사했고, `stm32_ready` 는 SAFE_STOP 을 **bit16=1 일 때만** 예외 허용했다.
그래서 `bit16=0, state=SAFE_STOP` 한 프레임이 들어오면 clear debounce 에
닿기도 전에 영구 FAULT 로 굳었고, FAULT 전이 훅이 `CMD_STOP` 을 보내
STM32 의 `rearm_required` 를 다시 세워 복구를 스스로 막았다.

여기서 재현하는 telemetry 순서 두 가지 (문서 명시):

    A: SAFE_STOP,bit16=1 -> READY,bit16=0
    B: SAFE_STOP,bit16=1 -> SAFE_STOP,bit16=0 -> READY,bit16=0

B 가 이번 수정의 핵심 대상이다.
"""
import math

from robot_navigation.route_logic import (
    FAULT, HOLD, HOLD_SUB_DEBOUNCE, HOLD_SUB_PRESENT, HOLD_SUB_REARM_WAIT,
    Inputs, RUNNING, RouteConfig, RouteMachine, Step, outbound_mission,
    relative_turn_mission,
)
from robot_perception.tag_geometry import SteeringConfig, TagTarget

STEER = SteeringConfig()
CFG = RouteConfig()

# 현장 로그의 bit. 0x8100 = bit8 RANGE_LOST(REPORT_ONLY) + bit15 SENSOR_STALE.
BIT8 = 0x100        # RANGE_LOST     — REPORT_ONLY, 차단 아님
BIT15 = 0x8000      # SENSOR_STALE   — SAFE_STOP, 차단
BIT9 = 0x200        # STEERING_INVALID — SAFE_STOP, 차단


# ── 입력 빌더 ──────────────────────────────────────────────
# telemetry 한 프레임을 그대로 흉내낸다. state 와 bit16 을 따로 줄 수 있어야
# 시나리오 B (bit16=0 인데 아직 SAFE_STOP) 를 표현할 수 있다.

def frame(now, *, state='READY', obstacle=False, tag=None,
          distance_m=0.0, yaw_rad=0.0, speed_mm_s=0.0,
          arm_blocked=False, telemetry_ok=True, odom_valid=True,
          imu_fused=True, blocking_bits=0, report_only_bits=0):
    """0x80 한 프레임에 해당하는 Inputs.

    state: 'READY' | 'DRIVING' | 'SAFE_STOP' | 'FAULT'
    blocking_bits: bit16 을 뺀 **차단** fault (runner 의 `other_blocking`)
    report_only_bits: 진단에만 실리는 bit (bit8 RANGE_LOST 등). 차단이 아니다.
    """
    return Inputs(
        now=now,
        odom_valid=odom_valid,
        yaw_rad=yaw_rad,
        distance_m=distance_m,
        tag=tag if tag is not None else TagTarget(),
        # stm32_ready 는 runner 와 같은 규칙으로 계산한다:
        #   READY/DRIVING 이거나, (장애물 활성 && SAFE_STOP)
        stm32_ready=(state in ('READY', 'DRIVING')
                     or (obstacle and state == 'SAFE_STOP')),
        stm32_state_ready=(state == 'READY'),
        stm32_state_name=state,
        telemetry_ok=telemetry_ok,
        telemetry_age_s=0.0 if telemetry_ok else 0.5,
        imu_fused=imu_fused,
        speed_mm_s=speed_mm_s,
        arm_blocked=arm_blocked,
        blocking_fault_bits=blocking_bits,
        # runner 와 **같은 규칙**으로 계산한다: 차단 bit 가 bit15 하나뿐인가.
        # bit8 RANGE_LOST 는 REPORT_ONLY 라 blocking_bits 에 애초에 안 들어온다.
        sensor_stale_only=(blocking_bits == BIT15),
        fault_bits=((0x10000 if obstacle else 0)
                    | report_only_bits | blocking_bits),
        obstacle=obstacle,
        stop_requested=False)


def tag(tag_id, along, cross=0.0, head=0.0):
    return TagTarget(tag_id=tag_id, valid=True, along=along, range_m=along,
                     d_x=along, cross_track=cross, heading_error=head)


def running_machine(cfg=CFG):
    """LEG1 주행 중인 상태머신."""
    m = RouteMachine(cfg, STEER)
    assert m.start(outbound_mission(cfg), 'outbound',
                   frame(0.0, state='READY')) == ''
    m.tick(frame(0.05, state='DRIVING', tag=tag(2, along=1.5)))
    assert m.state == RUNNING
    return m


def enter_obstacle_hold(m, t=0.1):
    """장애물 감지 -> HOLD 진입."""
    cmd = m.tick(frame(t, state='SAFE_STOP', obstacle=True,
                       tag=tag(2, along=1.5)))
    assert m.state == HOLD, m.state
    assert m.hold_reason == 'obstacle'
    return cmd


# ── 1. bit16=1 이면 HOLD + neutral ─────────────────────────

def test_1_obstacle_enters_hold_and_returns_neutral():
    m = running_machine()
    cmd = enter_obstacle_hold(m)
    assert cmd.is_neutral, cmd
    assert m.hold_substate == HOLD_SUB_PRESENT
    assert m.state != FAULT


# ── 2. bit16=0 직후 SAFE_STOP 은 FAULT 가 아니다 (핵심) ────

def test_2_clear_with_safe_stop_is_not_fault():
    """시나리오 B: bit16=0 인데 state 는 아직 SAFE_STOP.

    고치기 전에는 이 한 프레임이 영구 FAULT 를 만들었다.
    """
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    cmd = m.tick(frame(0.15, state='SAFE_STOP', obstacle=False,
                       tag=tag(2, along=1.5)))
    assert m.state == HOLD, f'FAULT 로 갔다: {m.fault_reason}'
    assert m.state != FAULT
    assert cmd.is_neutral
    assert m.hold_substate == HOLD_SUB_DEBOUNCE


def test_2b_clear_with_safe_stop_survives_many_frames():
    """복구 구간이 여러 프레임 이어져도 FAULT 로 넘어가지 않는다."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    t = 0.15
    for _ in range(8):                     # 0.4 s 분량
        cmd = m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                           tag=tag(2, along=1.5)))
        assert m.state == HOLD, f't={t} 에서 {m.state}: {m.fault_reason}'
        assert cmd.is_neutral
        t += 0.05


# ── 3. debounce 미달이면 READY 여도 출발하지 않는다 ────────

def test_3_ready_before_debounce_does_not_enable():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    # bit16=0 + 이미 READY. 하지만 clear 0.5 s 가 안 지났다.
    t = 0.15
    while t < 0.1 + CFG.obstacle_clear_hold_s:
        cmd = m.tick(frame(t, state='READY', obstacle=False,
                           tag=tag(2, along=1.5)))
        assert m.state == HOLD, f't={t}'
        assert cmd.is_neutral, f't={t} 에서 enable 이 나갔다: {cmd}'
        assert not cmd.enable
        t += 0.05


# ── 4. debounce 지났지만 READY 아니면 출발하지 않는다 ──────

def test_4_debounce_done_but_not_ready_does_not_enable():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    t = 0.15
    for _ in range(20):                    # 1.0 s — debounce 는 통과
        cmd = m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                           tag=tag(2, along=1.5)))
        assert not cmd.enable, f't={t} 에서 enable 이 나갔다'
        t += 0.05
    assert m.state == HOLD
    assert m.hold_substate == HOLD_SUB_REARM_WAIT


# ── 5. 두 조건 충족 시에만 같은 단계로 재개 ────────────────

def test_5_resumes_same_step_when_clear_and_ready():
    m = running_machine()
    # 단계 진행을 좀 만들어 둔다
    m.tick(frame(0.06, state='DRIVING', distance_m=0.4, tag=tag(2, along=1.4)))
    step_before = m.index
    enter_obstacle_hold(m, t=0.1)
    # bit16=0 이 0.5 s 유지 + READY
    t = 0.15
    while t <= 0.1 + CFG.obstacle_clear_hold_s + 1e-9:
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     distance_m=0.4, tag=tag(2, along=1.4)))
        t += 0.05
    cmd = m.tick(frame(t, state='READY', obstacle=False,
                       distance_m=0.4, tag=tag(2, along=1.4)))
    assert m.state == RUNNING, f'{m.state} {m.fault_reason}'
    assert m.index == step_before, '단계가 바뀌었다'
    assert cmd.enable, '재개했는데 enable=0'
    assert cmd.speed_mm_s > 0
    assert m.hold_substate is None
    # 진행거리는 유지된다 (단계를 다시 시작하지 않는다)
    assert m.traveled_m == 0.4


def test_5b_scenario_a_direct_safe_stop_to_ready():
    """시나리오 A: bit16=1/SAFE_STOP -> 곧바로 bit16=0/READY."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    t = 0.15
    while t <= 0.1 + CFG.obstacle_clear_hold_s + 1e-9:
        m.tick(frame(t, state='READY', obstacle=False, tag=tag(2, along=1.5)))
        t += 0.05
    cmd = m.tick(frame(t, state='READY', obstacle=False, tag=tag(2, along=1.5)))
    assert m.state == RUNNING, f'{m.state} {m.fault_reason}'
    assert cmd.enable


# ── 6. clear 중 재등장 → 타이머 리셋 ──────────────────────

def test_6_reappearing_obstacle_resets_timers():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    # 0.3 s 비었다가
    for t in (0.15, 0.2, 0.25, 0.3, 0.35):
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     tag=tag(2, along=1.5)))
    # 다시 감지
    m.tick(frame(0.4, state='SAFE_STOP', obstacle=True, tag=tag(2, along=1.5)))
    assert m.hold_substate == HOLD_SUB_PRESENT
    assert m._obstacle_clear_since is None
    assert m._ready_wait_since is None
    # 리셋됐으므로 0.4+0.5=0.9 이전에는 READY 여도 재개 못 한다
    cmd = m.tick(frame(0.8, state='READY', obstacle=False,
                       tag=tag(2, along=1.5)))
    assert m.state == HOLD, '타이머가 리셋되지 않았다'
    assert not cmd.enable


# ── 7. READY 대기 2.0 s 초과 → 명확한 이유로 FAULT ────────

def test_7_ready_wait_timeout_faults_with_reason():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    t = 0.15
    for _ in range(80):                    # 4 s — 계속 SAFE_STOP
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     tag=tag(2, along=1.5)))
        if m.state == FAULT:
            break
        t += 0.05
    assert m.state == FAULT, 'READY 대기가 무한정이다'
    assert 'READY' in m.fault_reason
    assert 'state=SAFE_STOP' in m.fault_reason


def test_7b_ready_wait_timeout_excludes_debounce():
    """타임아웃 2.0 s 는 debounce 통과 **이후**부터 센다."""
    m = running_machine(RouteConfig(obstacle_ready_timeout_s=2.0))
    enter_obstacle_hold(m, t=0.1)
    # debounce 0.5 s + 2.0 s = 2.6 s 근처까지는 FAULT 가 아니어야 한다
    t = 0.15
    while t < 0.1 + 0.5 + 1.9:
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     tag=tag(2, along=1.5)))
        t += 0.05
    assert m.state == HOLD, f'너무 일찍 FAULT: {m.fault_reason}'


# ── 8. 복구 중 다른 blocking fault → 즉시 FAULT ───────────

def test_8_other_blocking_fault_during_recovery_faults():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    m.tick(frame(0.15, state='SAFE_STOP', obstacle=False,
                 tag=tag(2, along=1.5)))
    assert m.state == HOLD
    # bit9 STEERING_INVALID 같은 차단 fault 가 뜨면 숨기지 않는다
    m.tick(frame(0.2, state='SAFE_STOP', obstacle=False, arm_blocked=True,
                 blocking_bits=0x200, tag=tag(2, along=1.5)))
    assert m.state == FAULT
    assert '차단 fault' in m.fault_reason
    assert '00000200' in m.fault_reason


def test_8b_comm_timeout_bit0_during_recovery_faults():
    """bit0 COMM_TIMEOUT 은 arm_blocked 로 오지 않는다(CLR_REARM).

    그래서 통신 자체가 끊긴 것은 telemetry_ok 로 잡아야 한다 — 13번과 함께
    이 경로가 복구 대기에 숨지 않는 것을 보장한다.
    """
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    m.tick(frame(0.2, state='SAFE_STOP', obstacle=False, telemetry_ok=False,
                 tag=tag(2, along=1.5)))
    assert m.state == FAULT
    assert 'telemetry' in m.fault_reason


# ── 9. 복구 구간에서 CMD_STOP 이 나가지 않는다 ────────────

def test_9_no_cmd_stop_during_obstacle_recovery():
    """CMD_STOP 은 runner 가 **FAULT/완료 전이에서만** 보낸다.

    따라서 "복구 중 CMD_STOP 무송신" 은 "복구 중 FAULT·완료 전이가 없다" 와
    같은 명제다. 전체 복구 구간의 상태 이력을 모아 그것을 직접 확인한다.
    """
    m = running_machine()
    seen = [m.state]
    enter_obstacle_hold(m, t=0.1)
    seen.append(m.state)
    t = 0.15
    # bit16=0 이지만 SAFE_STOP 인 구간을 길게 지나고 (시나리오 B)
    for _ in range(10):
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     tag=tag(2, along=1.5)))
        seen.append(m.state)
        t += 0.05
    # 그 뒤 READY 로 재개
    m.tick(frame(t, state='READY', obstacle=False, tag=tag(2, along=1.5)))
    seen.append(m.state)

    assert FAULT not in seen, f'복구 중 FAULT 전이 발생 → CMD_STOP 나감: {seen}'
    assert 'ARRIVED' not in seen and 'DOCKED' not in seen
    assert set(seen) <= {RUNNING, HOLD}, seen
    assert seen[-1] == RUNNING


# ── 9b~9h. bit15 순간 전환 (0x8100 현장 로그) ─────────────
# 고치기 전 결함: bit16 이 내려가고 bit15 가 대신 뜬 한 프레임에 arm_blocked 가
# 참이 되어 즉시 FAULT 로 굳었고, FAULT 훅의 CMD_STOP 이 STM32 의
# rearm_required 를 다시 세워 자동 복구를 스스로 막았다.

def stale_frame(t, **kw):
    """현장 로그의 0x8100 프레임: bit16 내려가고 bit8+bit15 가 떴다."""
    kw.setdefault('state', 'SAFE_STOP')
    return frame(t, obstacle=False, arm_blocked=True,
                 blocking_bits=BIT15, report_only_bits=BIT8,
                 tag=tag(2, along=1.5), **kw)


def test_9b_field_log_bit16_to_0x8100_to_bit16_stays_hold():
    """현장 로그 재현: bit16 → 0x8100(50 ms) → bit16. FAULT 없이 HOLD 유지."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    cmds = [m.tick(stale_frame(0.15)), m.tick(stale_frame(0.20))]
    assert m.state == HOLD, f'FAULT 로 갔다: {m.fault_reason}'
    assert m.hold_substate == HOLD_SUB_PRESENT
    # bit16 이 다시 뜬다 (측정이 살아나 가까운 물체를 유효하게 봤다)
    cmds.append(m.tick(frame(0.25, state='SAFE_STOP', obstacle=True,
                             tag=tag(2, along=1.5))))
    assert m.state == HOLD
    assert all(c.is_neutral for c in cmds), cmds


def test_9c_transient_0x8100_then_clear_resumes_same_step():
    """0x8100 이 스쳐 간 뒤 해제되면 debounce 를 새로 세고 같은 단계로 재개한다."""
    m = running_machine()
    step_before = m.index
    enter_obstacle_hold(m, t=0.1)
    m.tick(stale_frame(0.15))
    assert m.state == HOLD
    # bit15 해제. **이 시점부터** clear debounce 0.5 s 를 새로 센다.
    t = 0.20
    for _ in range(9):                     # 0.45 s — 아직 미달
        cmd = m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                           tag=tag(2, along=1.5)))
        assert cmd.is_neutral, f't={t} 에서 비영 명령'
        assert m.state == HOLD
        t += 0.05
    resumed = m.tick(frame(0.75, state='READY', obstacle=False,
                           tag=tag(2, along=1.5)))
    assert m.state == RUNNING, m.fault_reason
    assert resumed.enable and resumed.speed_mm_s > 0
    assert m.index == step_before, '다른 단계로 재개했다'


def test_9d_debounce_restarts_from_bit15_clear_not_from_bit16_clear():
    """bit15 가 떠 있던 시간을 debounce 에 세면 안 된다.

    bit16 이 내려간 직후 bit15 가 0.2 s 떠 있었다면, 그 0.2 s 는 "장애물이 없다"
    는 근거가 못 된다. 그것까지 세면 흔들리는 센서로 조기 출발한다.
    """
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    for t in (0.15, 0.20, 0.25, 0.30):     # bit15 가 0.15 s 유지 (상한 안)
        m.tick(stale_frame(t))
    assert m.state == HOLD
    # bit15 해제. debounce 는 **여기서부터** 0.5 s 다. bit16 이 내려간 0.15 부터
    # 셌다면 0.65 에 재개해 버리므로, 아래 루프가 그것을 잡는다.
    t = 0.35
    for _ in range(9):                     # 0.35 → 0.75 (0.40 s, 아직 미달)
        m.tick(frame(t, state='READY', obstacle=False, tag=tag(2, along=1.5)))
        assert m.state == HOLD, (f't={t} 에서 재개했다 — bit15 구간을 '
                                 f'debounce 에 세었다')
        assert m.hold_substate == HOLD_SUB_DEBOUNCE
        t += 0.05
    m.tick(frame(0.90, state='READY', obstacle=False, tag=tag(2, along=1.5)))
    assert m.state == RUNNING, m.fault_reason


def test_9e_bit15_longer_than_grace_faults():
    """상한(0.25 s)을 넘기면 진짜 센서 고장으로 다뤄 기존대로 FAULT."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    t = 0.15
    while m.state == HOLD and t < 1.0:
        m.tick(stale_frame(t))
        t += 0.05
    assert m.state == FAULT, '상한을 넘겼는데 계속 유예했다'
    assert '차단 fault' in m.fault_reason
    assert '00008000' in m.fault_reason
    # 유예는 0.25 s 를 넘겨서 끝난다 (첫 stale 0.15 + 0.25 = 0.40 이후)
    assert t > 0.40, f'너무 이르게 FAULT: t={t}'


def test_9f_bit15_while_driving_faults_immediately():
    """장애물 HOLD 가 아닌 정상 주행 중 bit15 는 유예 없이 즉시 FAULT."""
    m = running_machine()
    assert m.state == RUNNING
    m.tick(stale_frame(0.15, state='DRIVING'))
    assert m.state == FAULT, '주행 중 bit15 를 유예했다'
    assert '00008000' in m.fault_reason


def test_9g_bit15_with_other_blocking_fault_faults_immediately():
    """bit15 + STEERING_INVALID 처럼 다른 차단 fault 가 섞이면 유예 없음."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    m.tick(frame(0.15, state='SAFE_STOP', obstacle=False, arm_blocked=True,
                 blocking_bits=BIT15 | BIT9, report_only_bits=BIT8,
                 tag=tag(2, along=1.5)))
    assert m.state == FAULT, 'bit15+bit9 를 유예했다'
    assert '00008200' in m.fault_reason


def test_9h_telemetry_timeout_during_grace_faults_immediately():
    """유예 중에도 통신단절은 숨기지 않는다."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    m.tick(stale_frame(0.15))
    assert m.state == HOLD
    m.tick(stale_frame(0.20, telemetry_ok=False))
    assert m.state == FAULT
    assert 'telemetry' in m.fault_reason


def test_9i_no_fault_transition_across_whole_flicker_recovery():
    """8번 요구: 복구 구간 전체에서 CMD_STOP 이 안 나간다.

    CMD_STOP 은 runner 가 FAULT·완료 전이에서만 보내므로(9번과 같은 논리),
    flicker 를 포함한 전 구간에 FAULT 전이가 없음을 직접 확인한다.
    """
    m = running_machine()
    seen = [m.state]
    enter_obstacle_hold(m, t=0.1)
    seen.append(m.state)
    for t in (0.15, 0.20):                 # 0x8100 스침
        m.tick(stale_frame(t))
        seen.append(m.state)
    t = 0.25
    for _ in range(11):                    # bit15 해제 후 debounce
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     tag=tag(2, along=1.5)))
        seen.append(m.state)
        t += 0.05
    m.tick(frame(t, state='READY', obstacle=False, tag=tag(2, along=1.5)))
    seen.append(m.state)

    assert FAULT not in seen, f'FAULT 전이 발생 → CMD_STOP 나감: {seen}'
    assert set(seen) <= {RUNNING, HOLD}, seen
    assert seen[-1] == RUNNING


def test_9j_repeated_flicker_does_not_accumulate_grace():
    """유예는 flicker 마다 새로 센다.

    타이머를 안 버리면 두 번째 flicker 가 지난 번 시작 시각과 비교돼 첫
    프레임에 상한을 넘기고 FAULT 로 간다.
    """
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    t = 0.15
    for _ in range(4):                     # bit15 스침 → bit16 복귀 를 4회
        m.tick(stale_frame(t))
        assert m.state == HOLD, f't={t}: {m.fault_reason}'
        t += 0.05
        m.tick(frame(t, state='SAFE_STOP', obstacle=True,
                     tag=tag(2, along=1.5)))
        assert m.state == HOLD, f't={t}: {m.fault_reason}'
        t += 0.05


# ── 10. neutral·재개 명령의 SEQ 증가는 runner 담당 ────────

def test_10_recovery_emits_only_neutral_then_enable():
    """복구 구간 명령열: neutral 만 -> 재개 시 enable=1.

    SEQ 증가는 runner 의 TX 루프가 매 송신마다 `_next_seq()` 로 보장한다.
    상태머신 쪽에서 고정할 것은 "복구 중에는 비영 명령이 없다" 다.
    """
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    cmds = []
    t = 0.15
    for _ in range(12):
        cmds.append(m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                                 tag=tag(2, along=1.5))))
        t += 0.05
    assert all(c.is_neutral for c in cmds), cmds
    resumed = m.tick(frame(t, state='READY', obstacle=False,
                           tag=tag(2, along=1.5)))
    assert resumed.enable and resumed.speed_mm_s > 0


# ── 11. HOLD 시간은 회전 timeout 에서 제외된다 ────────────

def test_11_hold_time_excluded_from_turn_timeout():
    """장애물 앞에 오래 서 있었다는 이유로 회전 타임아웃이 터지면 안 된다."""
    cfg = RouteConfig()
    m = RouteMachine(cfg, STEER)
    steps = [Step('turn', 'TURN 우 90°', turn_deg=-90.0, radius_m=0.6),
             Step('stop', 'DONE')]
    assert m.start(steps, 'turn_right', frame(0.0, state='READY')) == ''

    theory = (math.radians(90.0) * 0.6) / (cfg.v_turn / 1000.0)
    budget = theory * cfg.turn_timeout_factor

    # 회전을 조금 진행한다 (0.4 s). 거리는 실제로 굴러간 만큼만 올린다 —
    # HOLD 중에는 정지 상태이므로 그대로 멈춰 있어야 한다.
    t, yaw, dist = 0.05, 0.0, 0.0
    while t < 0.45:
        yaw -= math.radians(2.0)
        dist += 0.2 * 0.05                 # 200 mm/s × 50 ms
        m.tick(frame(t, state='DRIVING', yaw_rad=yaw, speed_mm_s=200.0,
                     distance_m=dist))
        t += 0.05
    assert m.state == RUNNING, m.fault_reason

    # 장애물로 budget 보다 훨씬 긴 시간 HOLD (거리·yaw 동결)
    m.tick(frame(t, state='SAFE_STOP', obstacle=True, yaw_rad=yaw,
                 distance_m=dist))
    assert m.state == HOLD
    hold_end = t + budget * 3.0
    while t < hold_end:
        m.tick(frame(t, state='SAFE_STOP', obstacle=True, yaw_rad=yaw,
                     distance_m=dist))
        t += 0.05
    assert m.state == HOLD, f'HOLD 중 FAULT: {m.fault_reason}'
    assert t > budget, 'HOLD 가 예산보다 길지 않아 시험 의미가 없다'

    # 해제 -> 재개. HOLD 시간이 빠졌으므로 아직 타임아웃이 아니어야 한다.
    t += 0.05
    clear_end = t + CFG.obstacle_clear_hold_s + 0.1
    while t < clear_end:
        m.tick(frame(t, state='SAFE_STOP', obstacle=False, yaw_rad=yaw,
                     distance_m=dist))
        t += 0.05
    m.tick(frame(t, state='READY', obstacle=False, yaw_rad=yaw,
                 distance_m=dist))
    assert m.state == RUNNING, f'재개 실패: {m.state} {m.fault_reason}'
    t += 0.05
    dist += 0.2 * 0.05
    m.tick(frame(t, state='DRIVING', yaw_rad=yaw, speed_mm_s=200.0,
                 distance_m=dist))
    assert m.state == RUNNING, f'재개 직후 타임아웃: {m.fault_reason}'
    assert m.active_elapsed_s(t) < budget, (
        f'HOLD 가 제외되지 않았다: active={m.active_elapsed_s(t):.2f}s')


def test_11b_active_elapsed_excludes_hold():
    m = running_machine()
    enter_obstacle_hold(m, t=1.0)
    for t in (1.05, 1.5, 2.0, 2.5):
        m.tick(frame(t, state='SAFE_STOP', obstacle=True,
                     tag=tag(2, along=1.5)))
    # 벽시계로는 2.5 s 지났지만 HOLD 1.5 s 는 빠져야 한다
    assert m.active_elapsed_s(2.5) < 1.1, m.active_elapsed_s(2.5)


# ── 12. 재개 첫 tick 이 yaw 점프 가드를 오발하지 않는다 ────

def test_12_resume_does_not_trip_yaw_jump_guard():
    """HOLD 중 yaw 가 흘렀어도 재개 첫 tick 이 단일 점프로 보이면 안 된다.

    STM32 의 COMMAND_ESTIMATE yaw 는 정지 중에도 드리프트할 수 있다. HOLD
    구간 전체 변화를 한 tick 변화로 계산하면 yaw_jump_limit_deg(30°) 가
    즉시 터진다.
    """
    cfg = RouteConfig()
    m = RouteMachine(cfg, STEER)
    steps = [Step('turn', 'TURN 우 90°', turn_deg=-90.0, radius_m=0.6),
             Step('stop', 'DONE')]
    assert m.start(steps, 'turn_right', frame(0.0, state='READY')) == ''
    yaw = 0.0
    t = 0.05
    for _ in range(4):
        yaw -= math.radians(2.0)
        m.tick(frame(t, state='DRIVING', yaw_rad=yaw, speed_mm_s=200.0))
        t += 0.05
    assert m.state == RUNNING

    m.tick(frame(t, state='SAFE_STOP', obstacle=True, yaw_rad=yaw))
    assert m.state == HOLD
    # HOLD 동안 yaw 가 45° 흘렀다 (가드 한계 30° 를 훨씬 넘는 양)
    yaw_drifted = yaw + math.radians(45.0)
    t += 0.05
    clear_end = t + cfg.obstacle_clear_hold_s + 0.1
    while t < clear_end:
        m.tick(frame(t, state='SAFE_STOP', obstacle=False,
                     yaw_rad=yaw_drifted))
        t += 0.05
    turned_before = m.turned_deg
    m.tick(frame(t, state='READY', obstacle=False, yaw_rad=yaw_drifted))
    assert m.state == RUNNING, f'{m.state} {m.fault_reason}'
    # 재개 후 첫 주행 tick
    t += 0.05
    m.tick(frame(t, state='DRIVING', yaw_rad=yaw_drifted, speed_mm_s=200.0))
    assert m.state != FAULT, f'yaw 점프 가드 오발: {m.fault_reason}'
    # 정지 중 드리프트는 회전 진행량으로 세지 않는다
    assert abs(m.turned_deg - turned_before) < 5.0, (
        f'HOLD 드리프트가 진행량에 섞였다: '
        f'{turned_before:.1f} -> {m.turned_deg:.1f}')


# ── 13. telemetry 0.3 s 단절 → 통신 FAULT (복구에 숨지 않음) ──

def test_13_telemetry_timeout_is_not_hidden_by_recovery():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    # 복구 대기 중 통신이 끊긴다
    m.tick(frame(0.15, state='SAFE_STOP', obstacle=False,
                 tag=tag(2, along=1.5)))
    assert m.state == HOLD
    m.tick(frame(0.2, state='SAFE_STOP', obstacle=False, telemetry_ok=False,
                 tag=tag(2, along=1.5)))
    assert m.state == FAULT
    assert '단절' in m.fault_reason


def test_13b_telemetry_timeout_while_obstacle_present_faults():
    """bit16=1 로 HOLD 중이어도 통신단절은 즉시 FAULT 다."""
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    m.tick(frame(0.2, state='SAFE_STOP', obstacle=True, telemetry_ok=False,
                 tag=tag(2, along=1.5)))
    assert m.state == FAULT
    assert 'telemetry' in m.fault_reason


# ── 운영자 정지는 debounce 없이 즉시 재개 (기존 동작 보존) ──

def test_operator_stop_resumes_without_debounce():
    m = running_machine()
    stop = frame(0.1, state='READY', tag=tag(2, along=1.5))
    stop.stop_requested = True
    m.tick(stop)
    assert m.state == HOLD
    assert m.hold_reason == 'operator'
    cmd = m.tick(frame(0.15, state='READY', tag=tag(2, along=1.5)))
    assert m.state == RUNNING, '운영자 정지 해제가 즉시 재개되지 않았다'
    assert cmd.enable


# ── 상태 요약에 복구 하위상태가 노출된다 ──────────────────

def test_status_exposes_recovery_substate():
    m = running_machine()
    enter_obstacle_hold(m, t=0.1)
    assert m.status()['hold_substate'] == HOLD_SUB_PRESENT
    m.tick(frame(0.15, state='SAFE_STOP', obstacle=False,
                 tag=tag(2, along=1.5)))
    st = m.status()
    assert st['hold_substate'] == HOLD_SUB_DEBOUNCE
    assert st['hold_reason'] == 'obstacle'
    assert 'hold_elapsed_s' in st


# ── 상대 회전 미션에서도 동일하게 동작한다 ────────────────

def test_relative_turn_survives_obstacle_hold():
    cfg = RouteConfig()
    m = RouteMachine(cfg, STEER)
    assert m.start(relative_turn_mission(-1, 90.0, cfg), 'turn_right',
                   frame(0.0, state='READY')) == ''
    yaw, t = 0.0, 0.05
    for _ in range(3):
        yaw -= math.radians(2.0)
        m.tick(frame(t, state='DRIVING', yaw_rad=yaw, speed_mm_s=200.0))
        t += 0.05
    m.tick(frame(t, state='SAFE_STOP', obstacle=True, yaw_rad=yaw))
    assert m.state == HOLD
    t += 0.05
    clear_end = t + cfg.obstacle_clear_hold_s + 0.1
    while t < clear_end:
        m.tick(frame(t, state='SAFE_STOP', obstacle=False, yaw_rad=yaw))
        t += 0.05
    assert m.state == HOLD, f'{m.fault_reason}'
    m.tick(frame(t, state='READY', obstacle=False, yaw_rad=yaw))
    assert m.state == RUNNING, f'{m.state} {m.fault_reason}'
