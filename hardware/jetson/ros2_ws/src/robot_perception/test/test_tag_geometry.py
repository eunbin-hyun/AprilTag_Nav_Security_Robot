"""tag_geometry 단위시험. ROS 없이 돌아간다."""
import math

from robot_perception.tag_geometry import (
    GateConfig, SteeringConfig, TagGeometryConfig, TagObservation, TagTracker,
    clamp_steering, gate, path_errors, quat_rotate, steering_for_heading_hold,
    steering_for_path, tag_normal, turn_steering, wrap_pi,
)

# 태그를 수직으로 세워 로봇 쪽(-x)을 마주보게 하는 자세.
# 태그 로컬 +z(면 법선)를 base_link -x 로 보내는 회전 = Ry(-90°).
S = math.sqrt(0.5)
FACING = dict(qx=0.0, qy=-S, qz=0.0, qw=S)

# 실측 한계 (vehicle_config.h). 좌우가 비대칭이다.
STEER = SteeringConfig(lpf_alpha=1.0)          # 기하 시험은 필터 끈다
GOOD = dict(decision_margin=60.0, hamming=0, pixel_width=106.0)


def obs(tx, ty, stamp=0.0, tag_id=2, **kw):
    d = dict(FACING)
    d.update(GOOD)
    d.update(kw)
    return TagObservation(tag_id=tag_id, stamp=stamp, tx=tx, ty=ty, tz=0.0, **d)


# ── 기본 ───────────────────────────────────────────────────

def test_wrap_pi():
    assert abs(wrap_pi(0.0)) < 1e-9
    assert abs(wrap_pi(2 * math.pi)) < 1e-9
    assert abs(wrap_pi(math.pi + 0.1) - (-math.pi + 0.1)) < 1e-9
    # 정확히 ±pi 는 부동소수점 부호에 따라 어느 쪽이든 나온다. 같은 각도다.
    assert abs(abs(wrap_pi(-3 * math.pi)) - math.pi) < 1e-9


def test_wrap_pi_is_idempotent():
    for a in (0.3, -0.3, 3.0, -3.0, 10.0, -10.0):
        assert abs(wrap_pi(wrap_pi(a)) - wrap_pi(a)) < 1e-12


def test_quat_rotate_90_about_z():
    # +90° about z: x -> y
    q = (0.0, 0.0, S, S)
    x, y, z = quat_rotate(*q, 1.0, 0.0, 0.0)
    assert abs(x) < 1e-9 and abs(y - 1.0) < 1e-9 and abs(z) < 1e-9


def test_quat_rotate_identity():
    x, y, z = quat_rotate(0.0, 0.0, 0.0, 1.0, 1.0, 2.0, 3.0)
    assert (abs(x - 1.0) < 1e-9 and abs(y - 2.0) < 1e-9
            and abs(z - 3.0) < 1e-9)


def test_tag_normal_faces_robot():
    """로봇을 마주보는 태그의 법선 yaw 는 pi (=-x 방향)."""
    yaw, h = tag_normal(obs(2.0, 0.0))
    assert abs(abs(yaw) - math.pi) < 1e-6
    assert abs(h - 1.0) < 1e-6           # 수직 태그 -> 수평 성분 1.0


def test_tag_normal_lying_flat_has_no_horizontal():
    """바닥에 눕힌 태그는 법선이 위를 향해 수평 성분이 0 이다."""
    # 자세 없음(단위 쿼터니언) -> 법선 +z 그대로 = 위쪽
    yaw, h = tag_normal(obs(2.0, 0.0, qx=0.0, qy=0.0, qz=0.0, qw=1.0))
    assert h < 1e-6
    del yaw


# ── 경로 오차 ──────────────────────────────────────────────

def test_path_errors_aligned():
    """정면 2 m, 중심선 위 -> 오차 0, along 2."""
    ct, he, along = path_errors(2.0, 0.0, math.pi)
    assert abs(ct) < 1e-9
    assert abs(he) < 1e-9
    assert abs(along - 2.0) < 1e-9


def test_path_errors_robot_left_of_path():
    """태그가 로봇 오른쪽(-y)에 있으면 로봇이 경로 왼쪽 -> cross_track +."""
    ct, he, along = path_errors(2.0, -0.3, math.pi)
    assert ct > 0
    assert abs(ct - 0.3) < 1e-9
    assert abs(he) < 1e-9
    assert abs(along - 2.0) < 1e-9


def test_path_errors_robot_right_of_path():
    ct, _, _ = path_errors(2.0, 0.3, math.pi)
    assert ct < 0
    assert abs(ct + 0.3) < 1e-9


def test_path_errors_heading_sign():
    """경로가 오른쪽으로 기울면 로봇은 상대적으로 왼쪽을 향한다 -> he +."""
    ct, he, _ = path_errors(2.0, 0.0, math.pi - 0.1)
    assert he > 0
    assert abs(he - 0.1) < 1e-6
    del ct


# ── 조향 clamp: 비대칭 ─────────────────────────────────────

def test_clamp_is_asymmetric():
    """좌우 한계가 다르다. 대칭 clamp 는 결함이다."""
    assert clamp_steering(5000, STEER) == 1955      # 좌 한계
    assert clamp_steering(-5000, STEER) == -2869    # 우 한계
    assert clamp_steering(0, STEER) == 0


def test_clamp_keeps_right_turn_that_symmetric_clamp_would_cut():
    """회귀 시험: 우회전 -2295 는 잘리지 않아야 한다.

    2026-07-31 실증된 결함. max_steering_cdeg 하나로 대칭 clamp 하면
    -2295 요청이 -1955 로 잘려 회전반경이 0.319 -> 0.380 m (19% 초과)가
    되었다. 반대로 2869 를 대칭으로 쓰면 좌회전이 실제 한계 +1955 를 넘겨
    STM32 가 COMMAND_LIMIT 으로 거부하고, CMD_DRIVE 는 COMMAND_RESULT 가
    없어서 조용히 실패한다.
    """
    assert clamp_steering(-2295, STEER) == -2295
    symmetric_would_be = max(-1955, min(1955, -2295))
    assert symmetric_would_be == -1955
    assert clamp_steering(-2295, STEER) != symmetric_would_be


def test_steering_for_path_steers_right_when_robot_is_left():
    st = steering_for_path(0.3, 0.0, STEER)
    assert st < 0                                   # 우조향


def test_steering_for_path_steers_left_when_robot_is_right():
    assert steering_for_path(-0.3, 0.0, STEER) > 0


def test_steering_for_path_errors_reinforce():
    """두 오차가 같은 부호면 보정이 합쳐진다 (상쇄되지 않는다)."""
    only_ct = steering_for_path(0.2, 0.0, STEER)
    only_he = steering_for_path(0.0, 0.2, STEER)
    both = steering_for_path(0.2, 0.2, STEER)
    assert both < only_ct < 0
    assert both < only_he < 0


def test_steering_heading_hold_sign():
    """목표 yaw 가 현재보다 왼쪽이면 좌조향(+)."""
    assert steering_for_heading_hold(0.1, STEER) > 0
    assert steering_for_heading_hold(-0.1, STEER) < 0
    assert steering_for_heading_hold(0.0, STEER) == 0


def test_turn_steering_radius_0_6_is_symmetric():
    """R=0.6 m 는 좌우 한계 안쪽이라 양방향 같은 크기로 가능하다."""
    left = turn_steering(0.6, 0.135, left=True, cfg=STEER)
    right = turn_steering(0.6, 0.135, left=False, cfg=STEER)
    assert left == 1268                              # atan(0.135/0.6)=12.68°
    assert right == -1268
    assert left == -right


def test_turn_steering_minimum_radius_is_asymmetric():
    """최소 회전반경이 좌우 다르다. 우 0.2467 m / 좌 0.3806 m.

    R = L / tan(한계각) 이므로 우 = 0.135/tan(28.69°), 좌 = 0.135/tan(19.55°).
    """
    r_right = 0.135 / math.tan(math.radians(28.69))
    r_left = 0.135 / math.tan(math.radians(19.55))
    assert abs(r_right - 0.2467) < 1e-3
    assert abs(r_left - 0.3806) < 1e-3
    # 최소반경에서는 한계각이 그대로 나온다 (clamp 에 닿지 않음)
    assert turn_steering(r_right, 0.135, left=False, cfg=STEER) == -2869
    assert turn_steering(r_left, 0.135, left=True, cfg=STEER) == 1955


def test_turn_steering_below_minimum_radius_clamps():
    """최소반경보다 작게 요청하면 한계로 잘린다."""
    assert turn_steering(0.20, 0.135, left=False, cfg=STEER) == -2869
    assert turn_steering(0.20, 0.135, left=True, cfg=STEER) == 1955


def test_turn_steering_tight_right_is_not_reachable_on_left():
    """우측 최소반경 0.247 m 는 좌측으로 불가능하다.

    좌측은 최대 +19.55° 라 같은 반경을 못 만든다. 그래서 복귀 좌회전을 출발
    우회전과 같은 반경으로 하려면 둘 다 R=0.6 m 처럼 여유 있는 값을 써야 한다.
    """
    right = turn_steering(0.247, 0.135, left=False, cfg=STEER)
    left = turn_steering(0.247, 0.135, left=True, cfg=STEER)
    assert right == -2866                # 한계 안쪽이라 그대로
    assert left == 1955                  # 좌측은 한계에 걸림
    assert abs(right) > abs(left)


# ── 게이트 ─────────────────────────────────────────────────

def test_gate_passes_good_observation():
    o = obs(2.0, 0.0)
    _, h = tag_normal(o)
    assert gate(o, h) == ''


def test_gate_rejects_unregistered_id():
    o = obs(2.0, 0.0, tag_id=7)
    assert '미등록' in gate(o, 1.0)


def test_gate_rejects_hamming():
    assert 'hamming' in gate(obs(2.0, 0.0, hamming=1), 1.0)


def test_gate_rejects_low_margin():
    assert 'decision_margin' in gate(obs(2.0, 0.0, decision_margin=5.0), 1.0)


def test_gate_rejects_small_tag():
    assert 'px' in gate(obs(2.0, 0.0, pixel_width=12.0), 1.0)


def test_gate_rejects_out_of_range():
    assert '너무 가까움' in gate(obs(0.1, 0.0), 1.0)
    assert '너무 멂' in gate(obs(9.0, 0.0), 1.0)


def test_gate_rejects_tilted_tag():
    assert '기울' in gate(obs(2.0, 0.0), 0.1)


# ── 추적기 ─────────────────────────────────────────────────

def _tracker(**kw):
    g = GateConfig(**kw) if kw else GateConfig()
    return TagTracker(2, gate_cfg=g, steer_cfg=STEER)


def test_tracker_first_valid_observation():
    t = _tracker().update(obs(2.0, -0.3), now=0.0)
    assert t.valid and t.reason == ''
    assert abs(t.d_x - 2.0) < 1e-9
    assert abs(t.cross_track - 0.3) < 1e-9
    assert abs(t.range_m - math.hypot(2.0, 0.3)) < 1e-9


def test_tracker_holds_through_short_dropout():
    """역광에 순간 놓쳐도 lost_frames 동안은 마지막 값을 유지한다."""
    tr = _tracker(lost_frames=3)
    first = tr.update(obs(2.0, 0.0, stamp=0.0))
    assert first.valid
    for i in range(3):
        t = tr.update(None)
        assert t.valid, f'{i + 1}번째 미검출에서 이미 invalid'
        assert '유지' in t.reason
    t = tr.update(None)
    assert not t.valid
    assert t.reason == '미검출'


def test_tracker_rejects_impossible_jump():
    tr = _tracker()
    tr.update(obs(2.0, 0.0, stamp=0.0))
    t = tr.update(obs(1.0, 0.0, stamp=0.05))       # 0.05s 에 1 m
    assert '점프' in t.reason
    assert abs(t.d_x - 2.0) < 1e-9                 # 이전 값 유지


def test_tracker_accepts_plausible_motion():
    tr = _tracker()
    tr.update(obs(2.0, 0.0, stamp=0.0))
    t = tr.update(obs(1.99, 0.0, stamp=0.05))      # 0.01 m -> 0.2 m/s
    assert t.valid and t.reason == ''


def test_tracker_rejection_counter():
    tr = _tracker()
    tr.update(obs(2.0, 0.0, tag_id=7))
    tr.update(obs(2.0, 0.0, tag_id=7))
    assert sum(tr.rejected.values()) == 2


def test_tracker_lowpass_filter_smooths():
    """필터를 켜면 새 값이 바로 반영되지 않는다."""
    tr = TagTracker(2, steer_cfg=SteeringConfig(lpf_alpha=0.5))
    tr.update(obs(2.0, 0.0, stamp=0.0))
    t = tr.update(obs(2.0, -0.2, stamp=0.1))
    # cross_track 이 0 -> 0.2 로 가는 중간값이어야 한다
    assert 0.0 < t.cross_track < 0.2
    assert abs(t.cross_track - 0.1) < 1e-9


def test_tracker_steering_uses_asymmetric_clamp():
    tr = _tracker()
    t = tr.update(obs(2.0, -2.0))                  # 큰 좌측 이탈
    assert tr.steering(t) == -2869                 # 우 한계까지


def test_tracker_returns_zero_steering_when_invalid():
    tr = _tracker()
    t = tr.update(None)
    assert not t.valid
    assert tr.steering(t) == 0


def test_geometry_config_flip_reverses_normal():
    cfg = TagGeometryConfig(normal_flip=True)
    yaw, h = tag_normal(obs(2.0, 0.0), cfg)
    assert abs(yaw) < 1e-6                         # pi -> 0
    assert abs(h - 1.0) < 1e-6
