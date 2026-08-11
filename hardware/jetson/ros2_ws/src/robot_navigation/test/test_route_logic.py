"""route_logic 단위시험. ROS·UART 없이 돌아간다."""
import math

from robot_navigation.route_logic import (
    ARRIVED, DOCKED, FAULT, HOLD, IDLE, Inputs, NEUTRAL, RouteConfig,
    RouteMachine, RUNNING, Step, outbound_mission, return_mission,
    tag_target_from_dict, tag_target_to_dict,
)
from robot_perception.tag_geometry import SteeringConfig, TagTarget

STEER = SteeringConfig()
CFG = RouteConfig()


def ok_inputs(**kw):
    """모든 전제조건을 만족하는 입력."""
    d = dict(now=0.0, odom_valid=True, yaw_rad=0.0, distance_m=0.0,
             tag=TagTarget(), stm32_ready=True, arm_blocked=False,
             telemetry_ok=True, stm32_state_ready=True,
             obstacle=False, stop_requested=False)
    d.update(kw)
    return Inputs(**d)


def tag(tag_id, along, cross=0.0, head=0.0, valid=True):
    return TagTarget(tag_id=tag_id, valid=valid, along=along, range_m=along,
                     d_x=along, cross_track=cross, heading_error=head)


def machine():
    return RouteMachine(CFG, STEER)


# ── 전제조건 ───────────────────────────────────────────────

def test_starts_when_all_preconditions_met():
    m = machine()
    assert m.start(outbound_mission(), 'outbound', ok_inputs()) == ''
    assert m.state == RUNNING
    assert m.index == 0


def test_refuses_when_stm32_not_ready():
    m = machine()
    why = m.start(outbound_mission(), 'outbound',
                  ok_inputs(stm32_ready=False))
    assert 'READY' in why
    assert m.state == IDLE


def test_refuses_when_arm_blocked():
    m = machine()
    why = m.start(outbound_mission(), 'outbound', ok_inputs(arm_blocked=True))
    assert 'fault' in why
    assert m.state == IDLE


def test_refuses_when_odom_invalid():
    m = machine()
    why = m.start(outbound_mission(), 'outbound', ok_inputs(odom_valid=False))
    assert '오도메트리' in why


def test_refuses_when_stop_requested():
    m = machine()
    why = m.start(outbound_mission(), 'outbound',
                  ok_inputs(stop_requested=True))
    assert '정지' in why


def test_refuses_second_start_while_running():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    assert '주행 중' in m.start(outbound_mission(), 'outbound', ok_inputs())


def test_idle_commands_neutral():
    m = machine()
    assert m.tick(ok_inputs()) == NEUTRAL


# ── drive 단계 ─────────────────────────────────────────────

def test_drive_uses_visual_servo_when_tag_visible():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    # 로봇이 경로 왼쪽으로 벗어남 -> 우조향
    c = m.tick(ok_inputs(now=0.1, tag=tag(2, along=1.8, cross=0.2)))
    assert c.enable and c.speed_mm_s > 0
    assert c.steering_cdeg < 0


def test_drive_holds_heading_when_tag_lost():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    # 진입 yaw 0. 현재 yaw 가 -0.1 이면 왼쪽으로 되돌려야 한다 -> 좌조향
    c = m.tick(ok_inputs(now=0.1, yaw_rad=-0.1))
    assert c.enable
    assert c.steering_cdeg > 0
    assert c.speed_mm_s == CFG.v_approach


def test_drive_mode_reports_servo_vs_hold():
    # 목표 태그가 아니면 횡보정(cross_track)이 아예 꺼지고 heading 유지로
    # 떨어진다. 그 전환이 조용해서 "보정을 안 한다" 로 보였다 —
    # drive_mode 로 밖에서 구분할 수 있어야 한다.
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=1.8, cross=0.2)))
    assert m.drive_mode == 'servo'
    assert m.status()['drive_mode'] == 'servo'
    # 다른 태그 -> 횡보정 꺼짐
    m.tick(ok_inputs(now=0.2, tag=tag(4, along=1.8, cross=0.2)))
    assert m.drive_mode == 'hold'
    # 검출 무효도 마찬가지
    m.tick(ok_inputs(now=0.3, tag=tag(2, along=1.8, cross=0.2, valid=False)))
    assert m.drive_mode == 'hold'


def test_drive_ignores_wrong_tag_id():
    """다른 태그가 보여도 목표 태그가 아니면 시각 서보를 쓰지 않는다."""
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    c = m.tick(ok_inputs(now=0.1, tag=tag(3, along=1.0, cross=0.5)))
    # 목표는 ID 2 다. ID 3 의 cross_track 이 반영되면 안 된다.
    assert c.steering_cdeg == 0          # heading 유지, yaw 오차 0


def test_drive_decelerates_toward_trigger():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    # 거리는 단계 설정에서 뽑는다. 예전에는 0.7 을 하드코딩했는데,
    # 코너 진입 거리를 1.0 으로 올리자 0.7 이 trigger 안쪽이 되어
    # 감속이 아니라 회전 단계로 넘어갔다 (2026-08-05).
    s = m.step
    mid = (s.trigger_m + s.decel_m) / 2.0
    far = m.tick(ok_inputs(now=0.1, tag=tag(2, along=s.decel_m + 0.3)))
    near = m.tick(ok_inputs(now=0.2, tag=tag(2, along=mid)))
    assert far.speed_mm_s == CFG.v_cruise
    assert CFG.v_approach <= near.speed_mm_s < CFG.v_cruise


def test_drive_advances_to_turn_at_trigger():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    trig = m.step.trigger_m
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=trig + 0.5)))
    assert m.step.kind == 'drive'
    m.tick(ok_inputs(now=0.2, tag=tag(2, along=trig - 0.05)))
    assert m.step.kind == 'turn'
    assert m.index == 1


# 상한 시험은 기본값에 의존하지 않게 직접 지정한다. 기본값이 바뀌어도
# "상한을 넘으면 멈춘다" 는 계약은 그대로 검증되어야 한다.
TIGHT = RouteConfig(leg_max_m=2.0)


def test_drive_overrun_faults():
    """태그를 못 봤는데 상한을 넘으면 정지한다. 가장 위험한 실패 모드."""
    m = RouteMachine(TIGHT, STEER)
    m.start(outbound_mission(TIGHT), 'outbound', ok_inputs())
    assert m.step.max_m == 2.0
    c = m.tick(ok_inputs(now=1.0, distance_m=2.5))
    assert m.state == FAULT
    assert 'OVERRUN' in m.fault_reason
    assert c == NEUTRAL


def test_drive_overrun_uses_absolute_distance():
    """후진으로 거리가 줄어도 절댓값으로 감시한다."""
    m = RouteMachine(TIGHT, STEER)
    m.start(outbound_mission(TIGHT), 'outbound', ok_inputs(distance_m=10.0))
    m.tick(ok_inputs(now=1.0, distance_m=7.5))
    assert m.state == FAULT


def test_leg_max_m_is_configurable():
    """구간 상한이 설정에서 전파된다. 시운전 중 넉넉하게 잡을 수 있어야 한다."""
    loose = RouteConfig(leg_max_m=5.0)
    for step in outbound_mission(loose) + return_mission(loose):
        if step.kind == 'drive':
            assert step.max_m == 5.0


# ── turn 단계 ──────────────────────────────────────────────

def _to_turn(m):
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=0.5)))
    assert m.step.kind == 'turn'


def test_turn_commands_right_steering():
    m = machine()
    _to_turn(m)
    c = m.tick(ok_inputs(now=0.2))
    assert c.enable
    # 예전에는 radius_m=0.6 을 turn_steering() 으로 역산해 -1268 을 냈다.
    # 그 값은 전선에서 조향 트림(+500)에 깎여 -7.68° 가 되고 실제 반경이
    # 1.67 m 로 벌어져 거리 가드에서 69° 만 돌고 FAULT 했다 (2026-08-05 실측).
    # 이제 코스 회전도 실장비 검증된 고정 조향을 쓴다.
    assert c.steering_cdeg == -CFG.turn_fixed_steering_cdeg
    assert c.speed_mm_s == CFG.v_turn


def test_course_turn_arcs_fit_distance_guard():
    # 2026-08-05 회귀: 코스 회전이 거리 가드 안에서 끝나야 한다.
    # 예전 outbound 는 호 2.63 m 를 그리는데 상한이 2.0 m 여서 69° 에서
    # FAULT 했다. 트림은 route_runner 가 전선에서 얹으므로 여기서 같은
    # 값을 실측 상수로 재현한다 (COMMANDS.md steer_trim_cdeg=500).
    TRIM, LO, HI = 500, -2869, 1955
    for mission in (outbound_mission(CFG), return_mission(CFG)):
        for s in [x for x in mission if x.kind == 'turn']:
            assert s.steering_cdeg, f'{s.name}: 조향 고정값이 없다'
            assert s.max_m == CFG.turn_max_m, f'{s.name}: 거리 가드 기본값'
            wire = max(LO, min(HI, s.steering_cdeg + TRIM))
            radius = (CFG.understeer_factor * CFG.wheelbase_m
                      / math.tan(math.radians(abs(wire) / 100.0)))
            arc = radius * math.radians(abs(s.turn_deg))
            assert arc <= s.max_m, (
                f'{s.name}: 전선 {wire:+d}cd → 실제 R {radius:.2f} m, '
                f'호 {arc:.2f} m 가 상한 {s.max_m:.2f} m 초과')
            # 타임아웃도 실제 필요시간보다 넉넉해야 한다.
            theory = math.radians(abs(s.turn_deg)) * s.radius_m
            need = arc / (CFG.v_turn / 1000.0)
            budget = theory / (CFG.v_turn / 1000.0) * CFG.turn_timeout_factor
            assert budget > need * 1.5, (
                f'{s.name}: 타임아웃 {budget:.1f}s 가 필요 {need:.1f}s 대비 '
                f'여유 부족')


def test_corner_legs_end_at_turn_radius():
    # 2026-08-05 실측: 회전반경이 좌 0.63 / 우 0.98 m 다. 90° 회전은 코너에서
    # R 만큼 앞에서 시작해야 접선이 맞으므로, 회전 직전 레그는 코너 진입
    # 거리로 끝나야 한다. 예전 0.60 은 모델 반경 0.42 가정이라 코너를 지난
    # 뒤에 돌기 시작했다.
    for mission in (outbound_mission(CFG), return_mission(CFG)):
        for prev, nxt in zip(mission, mission[1:]):
            if prev.kind != 'drive' or nxt.kind != 'turn':
                continue
            want = (CFG.corner_trigger_left_m if nxt.turn_deg > 0
                    else CFG.corner_trigger_right_m)
            assert prev.trigger_m == want, (
                f'{prev.name}: 회전 직전 레그가 {prev.trigger_m} m 에서 끝난다 '
                f'(코너 진입 {want} m 이어야 한다)')
            # trigger 를 올리면 감속 구간이 사라진다 — decel 도 같이 당겨야
            # 순항속도로 코너에 뛰어들지 않는다.
            assert nxt.max_m >= prev.trigger_m
            assert prev.decel_m - prev.trigger_m >= 0.3, (
                f'{prev.name}: 감속 구간이 '
                f'{prev.decel_m - prev.trigger_m:.2f} m 뿐이다')


def test_distance_params_are_all_rear_axle():
    # 거리 파라미터의 기준은 **하나**여야 한다 — 전부 뒷차축이다. along 이
    # 뒷차축 기준이고(tag_localizer_cv 가 tf2 로 base_link 변환) 로그도
    # 그 값을 찍으므로, 설정만 앞머리 기준이면 매번 환산해야 한다.
    cfg = RouteConfig(arrive_trigger_m=1.4, dock_trigger_m=1.15,
                      corner_trigger_right_m=0.8, corner_trigger_left_m=0.55)
    for mission in (outbound_mission(cfg), return_mission(cfg)):
        for prev, nxt in zip(mission, mission[1:]):
            if prev.kind != 'drive':
                continue
            if nxt.kind == 'turn':
                # 회전 방향에 맞는 진입 거리를 써야 한다. 좌우 반경이
                # 1.5 배 다르므로 한 값을 공유하면 한쪽이 반드시 틀린다.
                want = (cfg.corner_trigger_left_m if nxt.turn_deg > 0
                        else cfg.corner_trigger_right_m)
            else:
                # 도착(태그3)과 도크(태그1)는 요구가 달라 값이 따로다
                want = (cfg.arrive_trigger_m if prev.tag_id == 3
                        else cfg.dock_trigger_m)
            assert prev.trigger_m == want, (
                f'{prev.name}: {prev.trigger_m} 이 아니라 {want} 여야 한다')
            # trigger 를 올리면 감속 구간도 따라와야 한다 (순항 진입 방지)
            assert prev.decel_m - prev.trigger_m >= 0.3, (
                f'{prev.name}: 감속 구간 '
                f'{prev.decel_m - prev.trigger_m:.2f} m 뿐')


def test_turn_completes_at_target_angle():
    m = machine()
    _to_turn(m)
    # -90° 까지 조금씩 돌린다
    for k in range(1, 20):
        yaw = math.radians(-5.0 * k)
        c = m.tick(ok_inputs(now=0.2 + 0.1 * k, yaw_rad=yaw))
        if m.step is None or m.step.kind != 'turn':
            break
    assert m.index == 2
    assert m.step.kind == 'drive'
    assert m.step.tag_id == 3
    del c


def test_turn_accumulates_across_wrap():
    """180° U턴은 wrap 경계를 넘는다. 누적이 unwrap 돼야 한다.

    yaw 는 매 tick ±pi 로 wrap 되지만 누적량은 계속 커져야 한다. wrap 된
    값을 그대로 목표와 비교하면 180° 판정이 경계에서 깨진다.
    """
    # 시작 yaw 를 -100° 로 둔다. 우회전 180° 중 80° 지점에서 -180° 경계를
    # 넘으므로 wrap 이 확실히 회전 도중에 일어난다. 시작 0° 로 하면 경계와
    # 목표가 겹쳐서 검증력이 없다.
    m = machine()
    yaw = math.radians(-100.0)
    assert m.start(return_mission(), 'return', ok_inputs(yaw_rad=yaw)) == ''
    assert m.step.kind == 'turn'
    assert m.step.turn_deg == -180.0
    last_turned = 0.0
    wrap_seen = False
    for k in range(1, 60):
        prev_raw = yaw
        yaw = wrapped(yaw - math.radians(5.0))
        # 원시 yaw 가 ±pi 경계를 넘으면 큰 점프로 나타난다
        if abs(yaw - prev_raw) > math.pi:
            wrap_seen = True
        m.tick(ok_inputs(now=0.1 * k, yaw_rad=yaw))
        assert -math.pi <= yaw <= math.pi          # 입력은 항상 wrap 상태
        if m.step is None or m.step.kind != 'turn':
            break
        # 단계가 넘어가면 _turned 가 다음 단계용으로 리셋되므로,
        # 아직 turn 인 동안의 마지막 값을 붙잡아 둔다.
        last_turned = m.turned_deg

    assert wrap_seen, '회전 도중 wrap 이 일어나지 않았다 — 시험이 무의미하다'
    # eps 3° 라 177° 에서 종료된다. 직전 관측값은 172~178° 사이다.
    assert 170.0 < abs(last_turned) < 180.0
    assert m.index == 1
    assert m.step.tag_id == 4


def wrapped(a):
    return math.atan2(math.sin(a), math.cos(a))


def test_turn_timeout_faults():
    m = machine()
    _to_turn(m)
    # yaw 가 전혀 변하지 않는다 (조향 고장·바퀴 공전 등)
    m.tick(ok_inputs(now=100.0))
    assert m.state == FAULT
    assert '타임아웃' in m.fault_reason


# ── 미션 완주 ──────────────────────────────────────────────

def test_outbound_mission_reaches_arrived():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=0.5)))     # LEG1 -> turn
    yaw = 0.0
    for k in range(1, 25):
        yaw = wrapped(yaw - math.radians(5.0))
        m.tick(ok_inputs(now=1.0 + 0.1 * k, yaw_rad=yaw))
        if m.step and m.step.kind == 'drive':
            break
    assert m.step.tag_id == 3
    c = m.tick(ok_inputs(now=5.0, yaw_rad=yaw, tag=tag(3, along=0.4)))
    assert m.state == ARRIVED
    assert c == NEUTRAL


def test_return_mission_reaches_docked():
    m = machine()
    m.start(return_mission(), 'return', ok_inputs())
    yaw = 0.0
    for k in range(1, 80):
        yaw = wrapped(yaw - math.radians(5.0))
        m.tick(ok_inputs(now=0.1 * k, yaw_rad=yaw))
        if m.step and m.step.kind == 'drive':
            break
    assert m.step.tag_id == 4                              # U턴 완료
    m.tick(ok_inputs(now=20.0, yaw_rad=yaw, tag=tag(4, along=0.5)))
    assert m.step.kind == 'turn' and m.step.turn_deg == 90.0
    for k in range(1, 25):
        yaw = wrapped(yaw + math.radians(5.0))
        m.tick(ok_inputs(now=20.0 + 0.1 * k, yaw_rad=yaw))
        if m.step and m.step.kind == 'drive':
            break
    assert m.step.tag_id == 1
    m.tick(ok_inputs(now=40.0, yaw_rad=yaw, tag=tag(1, along=0.4)))
    assert m.state == DOCKED


# ── HOLD / FAULT ───────────────────────────────────────────

def test_obstacle_holds_and_auto_resumes():
    """장애물은 latch 하지 않는다. clear 가 debounce 시간 유지되면 복귀한다."""
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=1.5)))
    c = m.tick(ok_inputs(now=0.2, obstacle=True, tag=tag(2, along=1.5)))
    assert m.state == HOLD
    assert m.hold_reason == 'obstacle'
    assert c == NEUTRAL
    # clear 직후 첫 tick 은 아직 debounce 중 — 재출발하면 안 된다
    c = m.tick(ok_inputs(now=0.3, tag=tag(2, along=1.5)))
    assert m.state == HOLD
    assert c == NEUTRAL
    # debounce(0.5s) 경과 후 복귀
    c = m.tick(ok_inputs(now=0.85, tag=tag(2, along=1.5)))
    assert m.state == RUNNING
    assert c.enable
    assert m.index == 0                       # 단계를 다시 시작하지 않는다


def test_stop_request_holds():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, stop_requested=True))
    assert m.state == HOLD
    m.tick(ok_inputs(now=0.2))
    assert m.state == RUNNING


def test_arm_blocked_faults_and_latches():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, arm_blocked=True))
    assert m.state == FAULT
    # 원인이 사라져도 자동 복귀하지 않는다
    m.tick(ok_inputs(now=0.2))
    assert m.state == FAULT


def test_odom_invalid_faults():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, odom_valid=False))
    assert m.state == FAULT
    assert '오도메트리' in m.fault_reason


def test_stm32_not_ready_faults():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, stm32_ready=False))
    assert m.state == FAULT


def test_reset_fault_returns_to_idle():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.abort('시험')
    assert m.state == FAULT
    assert m.reset_fault() is True
    assert m.state == IDLE
    assert m.reset_fault() is False
    assert m.start(outbound_mission(), 'outbound', ok_inputs()) == ''


def test_fault_refuses_start():
    m = machine()
    m.abort('시험')
    assert 'FAULT' in m.start(outbound_mission(), 'outbound', ok_inputs())


# ── 조향 한계 ──────────────────────────────────────────────

def test_correction_limit_is_symmetric():
    # 트림 +500 이 좌측 실효 한계를 +1455 로 깎으므로, 제한이 없으면 우측만
    # -2869 까지 써서 보정 권한이 2배 비대칭이 된다. 게인이 아니라 한계만
    # 맞춰서 선형 구간의 감쇠비는 좌우 동일하게 유지한다.
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    lim = CFG.correction_limit_cdeg
    assert lim > 0, '기본값은 제한이 걸려 있어야 한다'
    right = m.tick(ok_inputs(now=0.1, tag=tag(2, along=1.5, cross=5.0)))
    left = m.tick(ok_inputs(now=0.2, tag=tag(2, along=1.5, cross=-5.0)))
    assert right.steering_cdeg == -lim
    assert left.steering_cdeg == lim
    assert abs(right.steering_cdeg) == abs(left.steering_cdeg)


def test_correction_limit_off_reaches_hardware_limits():
    # 제한을 끄면 기구 한계(비대칭)까지 간다 — 그 계약은 그대로 남아야 한다.
    cfg = RouteConfig(correction_limit_cdeg=0)
    m = RouteMachine(cfg, STEER)
    m.start(outbound_mission(cfg), 'outbound', ok_inputs())
    c = m.tick(ok_inputs(now=0.1, tag=tag(2, along=1.5, cross=5.0)))
    assert c.steering_cdeg == STEER.min_cdeg == -2869
    c = m.tick(ok_inputs(now=0.2, tag=tag(2, along=1.5, cross=-5.0)))
    assert c.steering_cdeg == STEER.max_cdeg == 1955


def test_turn_uses_full_range_despite_correction_limit():
    # 보정 상한은 drive 단계 전용이다. 회전은 U턴이 -2600 을 써야 하므로
    # 여기에 걸리면 안 된다.
    m = machine()
    _to_turn(m)
    c = m.tick(ok_inputs(now=0.2))
    assert abs(c.steering_cdeg) == CFG.turn_fixed_steering_cdeg
    assert abs(c.steering_cdeg) > CFG.correction_limit_cdeg


def test_advances_by_encoder_when_tag_lost_near_trigger():
    # 코너 진입 1.0 m 에서 태그가 화각을 벗어나면(반화각 25.8°) 단계가
    # 끝나지 못해 코너를 지나쳐 직진한다 (2026-08-05 현장). 마지막으로 본
    # 거리에서 남은 만큼을 엔코더로 채우고 넘어가야 한다.
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    trig = m.step.trigger_m
    # 아직 0.30 m 남은 지점에서 마지막으로 본다
    m.tick(ok_inputs(now=0.1, distance_m=0.0,
                     tag=tag(2, along=trig + 0.30)))
    assert m.step.kind == 'drive'
    # 태그 소실 — 0.20 m 만 갔으면 아직 부족하다
    m.tick(ok_inputs(now=0.2, distance_m=0.20))
    assert m.step.kind == 'drive', '남은 거리를 다 못 갔는데 넘어갔다'
    # 남은 거리를 채우면 넘어간다 (경계에서 재지 않는다 — 부동소수
    # 오차를 시험하는 셈이고, 실제로는 20 Hz 에 7.5 mm 씩 전진한다)
    m.tick(ok_inputs(now=0.3, distance_m=0.32))
    assert m.step.kind == 'turn'
    assert m.advance_reason == 'coast'


def test_no_encoder_advance_when_tag_lost_far_away():
    # 멀리서 놓친 경우는 추측항법을 믿지 않는다 — heading 드리프트가 쌓여
    # 코너 위치가 어긋나면 회전 자체가 무의미해진다.
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    trig = m.step.trigger_m
    far = CFG.tag_lost_coast_max_m + 0.5
    m.tick(ok_inputs(now=0.1, distance_m=0.0, tag=tag(2, along=trig + far)))
    m.tick(ok_inputs(now=0.2, distance_m=far + 0.5))
    assert m.step.kind == 'drive', '멀리서 놓쳤는데 추측항법으로 넘어갔다'


# ── 직렬화 ─────────────────────────────────────────────────

def test_tag_target_roundtrip():
    t = TagTarget(tag_id=2, valid=True, d_x=1.5, d_y=-0.2, along=1.51,
                  cross_track=0.2, heading_error=-0.05, range_m=1.51,
                  decision_margin=55.0, pixel_width=140.0)
    back = tag_target_from_dict(tag_target_to_dict(t))
    assert back == t


def test_tag_target_from_partial_dict():
    back = tag_target_from_dict({'tag_id': 3, 'valid': True, 'unknown': 1})
    assert back.tag_id == 3 and back.valid is True
    assert back.along == 0.0


def test_status_is_json_friendly():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    st = m.status()
    assert st['state'] == RUNNING
    assert st['step_kind'] == 'drive'
    assert st['target_tag_id'] == 2
    assert st['step_total'] == 4
    import json
    json.dumps(st)                     # 예외 없이 직렬화되어야 한다


def test_step_dataclass_defaults_are_sane():
    s = Step('drive')
    assert s.max_m > s.decel_m > s.trigger_m


# ── 장애물 clear debounce (내일과제 §2, 2026-08-04) ────────

def test_obstacle_retrigger_resets_clear_timer():
    """debounce 대기 중 장애물이 재등장하면 타이머가 0 부터 다시 시작한다."""
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, obstacle=True, tag=tag(2, along=1.5)))
    assert m.state == HOLD
    m.tick(ok_inputs(now=0.2, tag=tag(2, along=1.5)))          # clear 시작
    m.tick(ok_inputs(now=0.5, obstacle=True, tag=tag(2, along=1.5)))  # 재등장!
    # 재등장 없었다면 0.2+0.5=0.7 에 복귀했겠지만, 리셋됐으므로 아직 HOLD
    c = m.tick(ok_inputs(now=0.8, tag=tag(2, along=1.5)))
    assert m.state == HOLD
    assert c == NEUTRAL
    # 0.6(새 clear 시작) + 0.5 = 1.1 이후에야 복귀
    m.tick(ok_inputs(now=1.35, tag=tag(2, along=1.5)))
    assert m.state == RUNNING


def test_obstacle_resume_keeps_step_progress():
    """복귀 시 단계 index·진행 기준(거리)이 초기화되지 않는다."""
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, distance_m=0.4, tag=tag(2, along=1.5)))
    before = m.traveled_m
    m.tick(ok_inputs(now=0.2, distance_m=0.4, obstacle=True,
                     tag=tag(2, along=1.5)))
    m.tick(ok_inputs(now=0.9, distance_m=0.4, tag=tag(2, along=1.5)))   # clear 시작
    m.tick(ok_inputs(now=1.45, distance_m=0.5, tag=tag(2, along=1.4)))  # debounce 경과
    assert m.state == RUNNING
    assert m.index == 0
    assert m.traveled_m >= before + 0.1 - 1e-9   # 기준 유지 = 누적 이어짐


def test_obstacle_with_critical_fault_prefers_fault():
    """장애물 HOLD 가 치명 fault 판단을 가리면 안 된다 (§2.4 우선순위)."""
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=1.5)))
    m.tick(ok_inputs(now=0.2, obstacle=True, arm_blocked=True,
                     tag=tag(2, along=1.5)))
    assert m.state == FAULT


def test_operator_hold_resumes_immediately_without_debounce():
    """운영자 정지는 장애물과 달리 해제 즉시 재개한다."""
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, stop_requested=True, tag=tag(2, along=1.5)))
    assert m.state == HOLD
    assert m.hold_reason == 'operator'
    c = m.tick(ok_inputs(now=0.15, tag=tag(2, along=1.5)))
    assert m.state == RUNNING
    assert c.enable


def test_obstacle_hold_reason_in_status():
    m = machine()
    m.start(outbound_mission(), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, obstacle=True, tag=tag(2, along=1.5)))
    assert m.status()['hold_reason'] == 'obstacle'


# ── 종점 정렬 조건 ─────────────────────────────────────────

def _to_arrive(m, cfg=CFG):
    """LEG2(태그3 도착) 단계까지 진행시킨다."""
    m.start(outbound_mission(cfg), 'outbound', ok_inputs())
    m.tick(ok_inputs(now=0.1, tag=tag(2, along=0.5)))
    yaw = 0.0
    for k in range(1, 30):
        yaw = wrapped(yaw - math.radians(5.0))
        m.tick(ok_inputs(now=1.0 + 0.1 * k, yaw_rad=yaw))
        if m.step and m.step.kind == 'drive':
            break
    assert m.step.tag_id == 3
    return yaw


def test_corner_has_no_align_condition():
    # 코너 진입은 yaw 로 회전하므로 정렬을 요구하지 않는다. 요구하면 태그가
    # 화각을 벗어나는 구간에서 영원히 끝나지 않는다.
    for mission in (outbound_mission(CFG), return_mission(CFG)):
        for prev, nxt in zip(mission, mission[1:]):
            if prev.kind == 'drive' and nxt.kind == 'turn':
                assert prev.align_cross_m == 0.0
                assert prev.align_head_deg == 0.0


def test_arrival_waits_for_alignment():
    # 거리는 맞지만 자세가 틀리면 완료하지 않는다 — 예전에는 비뚤어진 채로
    # 그냥 ARRIVED 였다.
    m = machine()
    yaw = _to_arrive(m)
    bad = math.radians(CFG.align_head_deg + 5.0)
    m.tick(ok_inputs(now=5.0, yaw_rad=yaw,
                     tag=tag(3, along=m.step.trigger_m - 0.01, head=bad)))
    assert m.state == RUNNING, '정렬이 안 됐는데 완료했다'
    assert m.step.tag_id == 3


def test_arrival_completes_when_aligned_holds():
    m = machine()
    yaw = _to_arrive(m)
    good = dict(tag=tag(3, along=m.step.trigger_m - 0.01), yaw_rad=yaw)
    # 한 표본으로는 완료하지 않는다 (heading 이 떨리므로 유지시간을 본다)
    m.tick(ok_inputs(now=5.0, **good))
    assert m.state == RUNNING
    m.tick(ok_inputs(now=5.0 + CFG.align_hold_s + 0.01, **good))
    assert m.state == ARRIVED
    assert m.aligned is True
    assert m.advance_reason == 'tag'


def test_arrival_gives_up_at_distance_floor():
    # 정렬을 기다리다 앞머리가 태그에 닿으면 안 된다. 하한에 도달하면
    # 완료하되 **실패를 기록**한다 (매달리면 미션이 끝나지 않는다).
    m = machine()
    yaw = _to_arrive(m)
    bad = math.radians(CFG.align_head_deg + 5.0)
    floor = m.step.trigger_m - CFG.align_extra_m
    m.tick(ok_inputs(now=5.0, yaw_rad=yaw,
                     tag=tag(3, along=floor - 0.01, head=bad)))
    assert m.state == ARRIVED, '하한에서도 안 끝나면 미션이 끝나지 않는다'
    assert m.aligned is False
    assert m.advance_reason == 'align_floor'
    assert m.status()['align_miss_head_deg'] is not None


def test_align_condition_can_be_disabled():
    # 0 이면 예전 동작(거리만) 으로 돌아간다 — 롤백 경로를 남긴다.
    cfg = RouteConfig(align_cross_m=0.0, align_head_deg=0.0)
    m = RouteMachine(cfg, STEER)
    yaw = _to_arrive(m, cfg)
    m.tick(ok_inputs(now=5.0, yaw_rad=yaw,
                     tag=tag(3, along=m.step.trigger_m - 0.01,
                             head=math.radians(30.0))))
    assert m.state == ARRIVED


def test_start_clears_previous_mission_result():
    # 2026-08-05 회귀: aligned/align_miss 는 미션이 끝난 뒤 읽히므로
    # _advance 에서 지우지 않는다. 그래서 **다음 미션 시작 때** 지워야 하는데
    # 그 짝이 없어서 2차 미션이 1차 결과를 물려받았다 (실행으로 확인).
    m = machine()
    yaw = _to_arrive(m)
    bad = math.radians(CFG.align_head_deg + 5.0)
    floor = m.step.trigger_m - CFG.align_extra_m
    m.tick(ok_inputs(now=5.0, yaw_rad=yaw,
                     tag=tag(3, along=floor - 0.01, head=bad)))
    assert m.state == ARRIVED and m.aligned is False
    assert m.advance_reason == 'align_floor'
    # 2차 미션 시작 — 이전 결과가 남아 있으면 웹·로그가 오독한다
    assert m.start(return_mission(), 'return', ok_inputs(now=6.0)) == ''
    assert m.aligned is None, '이전 미션의 정렬 결과가 남았다'
    assert m.align_miss is None
    assert m.advance_reason == ''
    assert m.status()['align_miss_head_deg'] is None


def test_align_extra_m_leaves_debounce_window():
    # align_extra_m 은 앞머리 여유와 **디바운스 시간창을 동시에** 지배한다.
    # 0 이면 첫 tick 에 하한 분기가 걸려 align_hold_s 를 볼 기회가 없다.
    v = CFG.v_approach / 1000.0
    window_s = CFG.align_extra_m / v
    assert window_s > CFG.align_hold_s, (
        f'창 {window_s:.2f}s 가 유지시간 {CFG.align_hold_s}s 보다 짧다 — '
        f'단일 표본 판정이 된다')
    # 그리고 앞머리 여유를 너무 깎아서도 안 된다 (0.23 m 는 거부된 값)
    nose = CFG.arrive_trigger_m - CFG.align_extra_m - CFG.front_overhang_m
    assert nose >= 0.30, f'하한 도달 시 앞머리 {nose:.2f} m 는 너무 가깝다'
