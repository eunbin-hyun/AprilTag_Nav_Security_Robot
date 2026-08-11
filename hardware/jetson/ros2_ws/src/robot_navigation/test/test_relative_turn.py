"""상대 yaw 회전 (JETSON_YAW90) 단위시험.

체크리스트 §1 의 wrap 단위시험과 §7 의 가드 시나리오를 ROS 없이 고정한다.
시뮬레이션은 20 Hz tick 으로 yaw·거리·속도를 전진시킨다.
"""
import math

from robot_navigation.route_logic import (
    ARRIVED, FAULT, RUNNING, Inputs, RouteConfig, RouteMachine,
    relative_turn_mission,
)
from robot_perception.tag_geometry import SteeringConfig, TagTarget, wrap_pi

STEER = SteeringConfig()
CFG = RouteConfig()          # stop lead 기본 0 → turn_eps_deg(3.0) 사용


def ok_inputs(**kw):
    d = dict(now=0.0, odom_valid=True, yaw_rad=0.0, distance_m=0.0,
             tag=TagTarget(), stm32_ready=True, arm_blocked=False,
             telemetry_ok=True, stm32_state_ready=True,
             obstacle=False, stop_requested=False,
             imu_fused=True, speed_mm_s=150.0)
    d.update(kw)
    return Inputs(**d)


def start_turn(direction, target_deg=90.0, cfg=CFG):
    m = RouteMachine(cfg, STEER)
    why = m.start(relative_turn_mission(direction, target_deg, cfg),
                  'turn_left' if direction > 0 else 'turn_right',
                  ok_inputs())
    assert why == '', why
    return m


def run_arc(m, direction, deg_per_tick=2.0, ticks=200, speed=150.0,
            fused=True, arc=True):
    """원호 주행 시뮬레이션. (마지막 Command, 명령 이력) 반환."""
    yaw = 0.0
    dist = 0.0
    hist = []
    for i in range(ticks):
        now = (i + 1) * 0.05
        if m.state == RUNNING:
            yaw += direction * math.radians(deg_per_tick)
            if arc:
                dist += speed / 1000.0 * 0.05
        cmd = m.tick(ok_inputs(now=now, yaw_rad=yaw, distance_m=dist,
                               speed_mm_s=speed, imu_fused=fused))
        hist.append(cmd)
        if m.state != RUNNING:
            break
    return cmd, hist


# ── 체크리스트 §1: yaw wrap 단위시험 ───────────────────────

def test_wrap_basic_and_boundary():
    d = math.degrees
    assert round(d(wrap_pi(math.radians(11 - 10)))) == 1
    assert round(d(wrap_pi(math.radians(-179 - 179)))) == 2      # 179→-179 = +2
    assert round(d(wrap_pi(math.radians(179 - (-179))))) == -2   # -179→179 = -2


# ── 완료 경로 ──────────────────────────────────────────────

def test_left_90_completes_with_stop_lead():
    m = start_turn(+1)
    cmd, _ = run_arc(m, +1)
    assert m.state == ARRIVED
    # lead 3° → 87° 이상에서 완료. 2°/tick 이라 87~89° 사이.
    assert 86.5 <= m.turned_deg <= 90.0
    assert cmd.is_neutral


def test_right_90_completes_and_uses_negative_steering():
    m = start_turn(-1)
    cmd, hist = run_arc(m, -1)
    assert m.state == ARRIVED
    driving = [c for c in hist if c.enable]
    assert driving and all(c.steering_cdeg == -1800 for c in driving)


def test_left_uses_positive_fixed_steering():
    m = start_turn(+1)
    _, hist = run_arc(m, +1)
    driving = [c for c in hist if c.enable]
    assert driving and all(c.steering_cdeg == +1800 for c in driving)


def test_two_phase_slowdown():
    m = start_turn(+1)
    _, hist = run_arc(m, +1)
    speeds = [c.speed_mm_s for c in hist if c.enable]
    assert CFG.v_turn in speeds            # 초반 빠른 구간
    assert CFG.v_turn_slow in speeds       # 남은 각 15° 이하 감속 구간
    # 감속은 한 번 시작되면 완료까지 유지된다 (다시 빨라지지 않는다)
    last_fast = max(i for i, v in enumerate(speeds) if v == CFG.v_turn)
    first_slow = min(i for i, v in enumerate(speeds) if v == CFG.v_turn_slow)
    assert first_slow > last_fast


def test_30deg_target_for_first_field_test():
    m = start_turn(+1, target_deg=30.0)
    _, _ = run_arc(m, +1)
    assert m.state == ARRIVED
    assert 26.5 <= m.turned_deg <= 30.0


def test_per_direction_stop_lead():
    cfg = RouteConfig(stop_lead_left_deg=8.0, stop_lead_right_deg=2.0)
    ml = start_turn(+1, cfg=cfg)
    run_arc(ml, +1, deg_per_tick=1.0)
    mr = start_turn(-1, cfg=cfg)
    run_arc(mr, -1, deg_per_tick=1.0)
    assert 81.5 <= ml.turned_deg <= 83.0      # 90-8 직후
    assert -89.0 <= mr.turned_deg <= -87.5    # 90-2 직후


# ── 체크리스트 §7: 가드 ────────────────────────────────────

def test_wrong_way_faults():
    m = start_turn(+1)                        # 좌회전 명령인데
    run_arc(m, -1, deg_per_tick=1.0)          # yaw 는 우로 돈다
    assert m.state == FAULT
    assert '반대 방향' in m.fault_reason


def test_implausible_yaw_jump_faults():
    m = start_turn(+1)
    m.tick(ok_inputs(now=0.05, yaw_rad=math.radians(2)))
    cmd = m.tick(ok_inputs(now=0.10, yaw_rad=math.radians(45)))  # 43°/tick
    assert m.state == FAULT
    assert 'yaw 점프' in m.fault_reason
    assert cmd.is_neutral


def test_distance_guard_when_yaw_frozen():
    m = start_turn(+1)
    dist = 0.0
    for i in range(300):
        dist += 0.01                          # yaw 불변, 거리만 증가
        m.tick(ok_inputs(now=(i + 1) * 0.05, yaw_rad=0.0, distance_m=dist))
        if m.state == FAULT:
            break
    assert m.state == FAULT
    assert '거리 가드' in m.fault_reason
    assert dist <= CFG.turn_max_m + 0.05


def test_fusion_required_after_motion_grace():
    m = start_turn(+1)
    _, _ = run_arc(m, +1, deg_per_tick=0.5, fused=False)
    assert m.state == FAULT
    assert 'IMU_FUSED=0' in m.fault_reason


def test_fusion_zero_ok_at_rest():
    m = start_turn(+1)
    for i in range(30):                       # 1.5초 정지 (유예 0.7s 초과)
        m.tick(ok_inputs(now=(i + 1) * 0.05, speed_mm_s=0.0,
                         imu_fused=False))
    assert m.state == RUNNING                 # 정지 중 미융합은 정상


def test_fusion_guard_can_be_disabled():
    cfg = RouteConfig(require_imu_fused=False)
    m = start_turn(+1, cfg=cfg)
    run_arc(m, +1, fused=False)
    assert m.state == ARRIVED


# ── 융합 dropout 디바운스 (2026-08-05 실장비 대응) ─────────
# 실장비 BNO085 는 IMU_FUSED 를 깜빡인다. 실측 토글:
#   켜짐 196 ms -> 꺼짐 -> 켜짐 542 ms -> 꺼짐 -> (34 ms 뒤 FAULT)
# 디바운스가 없으면 회전은 시작 0.7 s 후 반드시 실패한다.

def run_arc_flapping(m, direction, on_ticks, off_ticks, ticks=200,
                     deg_per_tick=1.0, speed=150.0):
    """융합이 on/off 를 반복하는 원호 주행. (마지막 Command, fused 이력)."""
    yaw, dist, fused_hist = 0.0, 0.0, []
    period = on_ticks + off_ticks
    cmd = None
    for i in range(ticks):
        now = (i + 1) * 0.05
        fused = (i % period) < on_ticks
        fused_hist.append(fused)
        if m.state == RUNNING:
            yaw += direction * math.radians(deg_per_tick)
            dist += speed / 1000.0 * 0.05
        cmd = m.tick(ok_inputs(now=now, yaw_rad=yaw, distance_m=dist,
                               speed_mm_s=speed, imu_fused=fused))
        if m.state != RUNNING:
            break
    return cmd, fused_hist


def test_short_fusion_dropout_does_not_fault():
    """실측 최장 dropout(542 ms) 은 견뎌야 한다 (허용 0.8 s)."""
    m = start_turn(+1, target_deg=30.0)
    # 4틱 켜짐(0.2s) / 10틱 꺼짐(0.5s) 반복 — 0.8 s 를 넘지 않는다
    _, hist = run_arc_flapping(m, +1, on_ticks=4, off_ticks=10,
                               deg_per_tick=1.0)
    assert False in hist, 'dropout 이 재현되지 않았다'
    assert m.state != FAULT, f'깜빡임에 FAULT: {m.fault_reason}'
    assert m.state == ARRIVED, m.state


def test_single_tick_dropout_does_not_fault():
    """한 표본 dropout 으로 죽지 않는다 — 예전 코드의 실패 모드."""
    m = start_turn(+1, target_deg=30.0)
    _, hist = run_arc_flapping(m, +1, on_ticks=20, off_ticks=1,
                               deg_per_tick=1.0)
    assert False in hist
    assert m.state == ARRIVED, f'{m.state} {m.fault_reason}'


def test_sustained_fusion_loss_still_faults():
    """진짜로 끊기면 여전히 FAULT 한다 (디바운스가 은폐하지 않는다)."""
    m = start_turn(+1)
    run_arc(m, +1, deg_per_tick=0.5, fused=False)
    assert m.state == FAULT
    assert 'IMU_FUSED=0' in m.fault_reason
    assert '연속' in m.fault_reason


def test_dropout_timer_resets_on_recovery():
    """융합이 돌아오면 dropout 타이머가 초기화된다."""
    cfg = RouteConfig(fusion_dropout_grace_s=0.8)
    m = start_turn(+1, target_deg=90.0, cfg=cfg)
    yaw, dist, t = 0.0, 0.0, 0.0
    # 0.7 s 꺼짐 → 한 틱 켜짐 → 다시 0.7 s 꺼짐. 각각 0.8 s 미만이므로
    # 타이머가 리셋되면 FAULT 가 나지 않아야 한다.
    for phase in range(2):
        for _ in range(14):                    # 0.7 s
            t += 0.05
            yaw += math.radians(0.5)
            dist += 0.15 * 0.05
            m.tick(ok_inputs(now=t, yaw_rad=yaw, distance_m=dist,
                             speed_mm_s=150.0, imu_fused=False))
            assert m.state == RUNNING, (
                f'phase {phase} 에서 조기 FAULT: {m.fault_reason}')
        t += 0.05
        yaw += math.radians(0.5)
        dist += 0.15 * 0.05
        m.tick(ok_inputs(now=t, yaw_rad=yaw, distance_m=dist,
                         speed_mm_s=150.0, imu_fused=True))
        assert m.state == RUNNING, m.fault_reason


def test_dropout_grace_is_configurable():
    """허용 시간을 줄이면 더 빨리 FAULT 한다."""
    cfg = RouteConfig(fusion_dropout_grace_s=0.1)
    m = start_turn(+1, cfg=cfg)
    run_arc_flapping(m, +1, on_ticks=1, off_ticks=10, deg_per_tick=0.5)
    assert m.state == FAULT
    assert 'IMU_FUSED=0' in m.fault_reason


# ── clearance 교체 지점 ────────────────────────────────────

class _Deny:
    def __init__(self, start=True, cont=True):
        self._s, self._c = start, cont

    def start_allowed(self, status):
        return self._s

    def continue_allowed(self, status):
        return self._c


def test_clearance_refuses_start():
    m = RouteMachine(CFG, STEER)
    m.clearance = _Deny(start=False)
    why = m.start(relative_turn_mission(+1, 90.0, CFG), 'turn_left',
                  ok_inputs())
    assert '공간' in why


def test_clearance_aborts_mid_turn():
    m = start_turn(+1)
    m.clearance = _Deny(cont=False)
    run_arc(m, +1, ticks=3)
    assert m.state == FAULT
    assert 'clearance' in m.fault_reason


def test_direction_must_be_unit():
    try:
        relative_turn_mission(2, 90.0, CFG)
        assert False, 'ValueError 가 나야 한다'
    except ValueError:
        pass


# ── yaw 출처 (2026-08-05 실측 대응) ────────────────────────
# 0x85 융합 yaw 는 모델 성분 25% 때문에 1.275 배 부풀려진다. 그걸로 90° 를
# 판정하면 물리적으로 71° 에서 멈춘다. 0x83 IMU quaternion 을 쓰면 오염이
# 없고, STM32 의 IMU_FUSED 여부도 무의미해진다.

def test_imu_yaw_skips_fusion_guard():
    """yaw_is_imu 면 IMU_FUSED=0 이어도 회전이 FAULT 되지 않는다."""
    m = start_turn(+1, target_deg=30.0)
    yaw, dist, t = 0.0, 0.0, 0.0
    for _ in range(60):                       # 3 s — 유예·디바운스 모두 초과
        t += 0.05
        yaw += math.radians(1.0)
        dist += 0.15 * 0.05
        inp = ok_inputs(now=t, yaw_rad=yaw, distance_m=dist,
                        speed_mm_s=150.0, imu_fused=False)
        inp.yaw_is_imu = True                 # 0x83 을 직접 읽는 상태
        m.tick(inp)
        if m.state != RUNNING:
            break
    assert m.state != FAULT, f'IMU yaw 인데 융합 가드가 걸렸다: {m.fault_reason}'
    assert m.state == ARRIVED, m.state


def test_odom_yaw_still_uses_fusion_guard():
    """yaw_is_imu 가 아니면 기존 가드가 그대로 동작한다 (회귀 방지)."""
    m = start_turn(+1)
    run_arc(m, +1, deg_per_tick=0.5, fused=False)
    assert m.state == FAULT
    assert 'IMU_FUSED=0' in m.fault_reason


def test_turn_max_m_covers_measured_radius():
    """실측 R 0.685 m 에서 90° 를 돌 거리(1.08 m)가 가드 안에 들어온다."""
    cfg = RouteConfig()
    need = math.radians(90.0) * 0.685
    assert need < cfg.turn_max_m, (
        f'90° 에 {need:.2f} m 필요한데 가드가 {cfg.turn_max_m} m 다')
    # 예전 기본값 0.90 은 걸렸다는 것도 같이 고정한다
    assert need > 0.90, '이 시험의 전제가 바뀌었다'
