"""odom_convert 단위시험. ROS 없이 돈다.

단위 변환과 쿼터니언은 틀려도 예외가 안 나고 조용히 틀린다 — 지도에 점이
1000 배 멀리 찍히거나 각도가 뒤집혀도 프로그램은 멀쩡히 돈다. 그래서 값을
시험으로 고정한다.
"""
import math

from robot_navigation.odom_convert import (
    BASE_FRAME, DeadReckon, ODOM_FRAME, odom_si, wrap_angle,
    yaw_to_quaternion,
)


def frame(**kw):
    """TELEMETRY_ODOMETRY 언팩 결과 형태."""
    d = dict(mcu_time_ms=0, x_mm=0, y_mm=0, yaw_mdeg=0, distance_mm=0,
             linear_speed_mm_s=0, yaw_rate_mdeg_s=0, steering_cdeg=0,
             curvature_micro_per_m=0, status_flags=0, steering_source=0,
             last_drive_seq=0)
    d.update(kw)
    return d


# ── 쿼터니언 ──────────────────────────────────────────────

def test_zero_yaw_is_identity():
    x, y, z, w = yaw_to_quaternion(0.0)
    assert (x, y, z) == (0.0, 0.0, 0.0)
    assert w == 1.0


def test_uses_half_angle():
    """절반각을 안 쓰면 각도가 두 배가 된다 — 가장 흔한 실수."""
    _, _, z, w = yaw_to_quaternion(math.pi / 2)      # 90°
    assert abs(z - math.sin(math.pi / 4)) < 1e-12
    assert abs(w - math.cos(math.pi / 4)) < 1e-12
    # 90° 를 그대로 넣었다면 z=sin(90°)=1.0 이 됐을 것이다
    assert abs(z - 1.0) > 0.29


def test_quaternion_is_unit():
    for deg in (-180, -90, -37, 0, 37, 90, 179):
        x, y, z, w = yaw_to_quaternion(math.radians(deg))
        assert abs(math.sqrt(x * x + y * y + z * z + w * w) - 1.0) < 1e-12


def test_sign_follows_yaw():
    """좌회전(+yaw)이면 z 가 양수. 뒤집히면 지도에서 로봇이 반대로 돈다."""
    assert yaw_to_quaternion(math.radians(30))[2] > 0
    assert yaw_to_quaternion(math.radians(-30))[2] < 0


def test_roll_pitch_always_zero():
    """2D 주행이다. x·y 성분이 생기면 좌표계 규약이 깨진 것이다."""
    for deg in (-90, 0, 45, 180):
        x, y, _, _ = yaw_to_quaternion(math.radians(deg))
        assert (x, y) == (0.0, 0.0)


# ── 단위 변환 ─────────────────────────────────────────────

def test_position_mm_to_m():
    si = odom_si(frame(x_mm=1234, y_mm=-567))
    assert abs(si['x'] - 1.234) < 1e-12
    assert abs(si['y'] + 0.567) < 1e-12


def test_yaw_mdeg_to_rad():
    """m-deg 는 1/1000 도다. 1000 으로 안 나누면 1000 배 틀린다."""
    si = odom_si(frame(yaw_mdeg=90_000))              # 90.000°
    assert abs(si['yaw'] - math.pi / 2) < 1e-9


def test_speed_and_yaw_rate():
    si = odom_si(frame(linear_speed_mm_s=200, yaw_rate_mdeg_s=45_000))
    assert abs(si['vx'] - 0.200) < 1e-12              # 200 mm/s = 0.2 m/s
    assert abs(si['wz'] - math.radians(45.0)) < 1e-9  # 45°/s


def test_distance_mm_to_m():
    assert abs(odom_si(frame(distance_mm=2500))['distance'] - 2.5) < 1e-12


def test_negative_values_survive():
    """후진·우회전은 음수다. 부호를 잃으면 지도에서 반대로 간다."""
    si = odom_si(frame(x_mm=-100, linear_speed_mm_s=-150,
                       yaw_rate_mdeg_s=-30_000))
    assert si['x'] < 0 and si['vx'] < 0 and si['wz'] < 0


def test_quat_matches_yaw_field():
    si = odom_si(frame(yaw_mdeg=-45_000))
    assert si['quat'] == yaw_to_quaternion(si['yaw'])


# ── 좌표계 이름 ───────────────────────────────────────────

def test_frame_names_follow_rep105():
    """이름이 틀리면 TF 트리가 안 붙고 rviz 에서 아무것도 안 보인다."""
    assert ODOM_FRAME == 'odom'
    assert BASE_FRAME == 'base_link'


# ── 각도 wrap ─────────────────────────────────────────────

def test_wrap_angle_folds_to_pi():
    assert abs(wrap_angle(math.radians(190)) - math.radians(-170)) < 1e-9
    assert abs(wrap_angle(math.radians(-190)) - math.radians(170)) < 1e-9
    assert abs(wrap_angle(math.radians(45)) - math.radians(45)) < 1e-9


# ── DeadReckon (엔코더 + 0x83 yaw 재적분) ─────────────────
#
# 이 계산이 틀리면 관제 지도의 로봇이 엉뚱한 데로 간다. 예외는 안 난다.

def test_first_sample_sets_origin():
    """첫 표본의 yaw 가 원점이다. 안 빼면 odom 프레임 정의가 깨진다."""
    dr = DeadReckon()
    assert not dr.started
    x, y, yaw = dr.update(0.0, math.radians(137.0))   # 아무 방향으로 서 있어도
    assert dr.started
    assert (x, y) == (0.0, 0.0)
    assert abs(yaw) < 1e-12                           # 시작은 항상 0


def test_first_sample_does_not_move():
    """첫 표본은 거리 기준점만 잡는다. 누적 거리를 증분으로 오해하면 안 된다."""
    dr = DeadReckon()
    x, y, _ = dr.update(12.5, 0.0)                    # 이미 12.5 m 달려온 상태
    assert (x, y) == (0.0, 0.0)


def test_straight_east():
    dr = DeadReckon()
    dr.update(0.0, 0.0)
    x, y, _ = dr.update(1.0, 0.0)
    assert abs(x - 1.0) < 1e-9
    assert abs(y) < 1e-9


def test_turn_then_straight():
    """90° 좌회전 뒤 1 m 는 +y 로 간다. 부호가 뒤집히면 지도가 거울이 된다."""
    dr = DeadReckon()
    dr.update(0.0, 0.0)
    dr.update(1.0, 0.0)                               # 동쪽 1 m
    x, y, _ = dr.update(2.0, math.radians(90))        # 북쪽 1 m
    assert abs(x - 1.0) < 1e-9
    assert abs(y - 1.0) < 1e-9


def test_reverse_keeps_sign():
    """distance_mm 은 signed 다. abs 를 씌우면 후진이 전진으로 기록된다."""
    dr = DeadReckon()
    dr.update(0.0, 0.0)
    dr.update(1.0, 0.0)
    x, _, _ = dr.update(0.7, 0.0)                     # 0.3 m 후진
    assert abs(x - 0.7) < 1e-9


def test_square_closes():
    """네 변을 돌면 출발점으로 돌아온다 — 이 시험의 본론."""
    dr = DeadReckon()
    dr.update(0.0, 0.0)
    dist = 0.0
    for i in range(4):
        dist += 1.0
        dr.update(dist, math.radians(90 * i))         # 변을 그 방향으로 1 m
        dr.update(dist, math.radians(90 * (i + 1)))   # 제자리에서 방향만 전환
    assert math.hypot(dr.x, dr.y) < 1e-9


def test_yaw_wrap_does_not_break_integration():
    """±180° 를 넘어가도 궤적이 안 튄다. wrap 을 빼먹으면 여기서 터진다."""
    dr = DeadReckon()
    dr.update(0.0, math.radians(170))
    x0, y0, _ = dr.update(1.0, math.radians(170))     # 원점 기준 0° 방향 1 m
    x1, y1, yaw = dr.update(2.0, math.radians(-170))  # 원점 기준 +20°
    assert abs(yaw - math.radians(20)) < 1e-9
    assert x1 > x0 and y1 > y0                        # 왼쪽 앞으로 갔다


def test_pause_drops_distance_but_keeps_position():
    """무효 구간의 이동분이 복귀 순간 한꺼번에 튀면 안 된다."""
    dr = DeadReckon()
    dr.update(0.0, 0.0)
    dr.update(1.0, 0.0)
    dr.pause()
    # 멈춘 동안 5 m 를 더 달렸다 (게이트가 닫혀 발행은 안 됐다)
    x, y, _ = dr.update(6.0, 0.0)
    assert abs(x - 1.0) < 1e-9                        # 1 m 자리 그대로
    x, _, _ = dr.update(7.0, 0.0)                     # 복귀 뒤 1 m 는 정상 반영
    assert abs(x - 2.0) < 1e-9


def test_pause_keeps_origin():
    """pause 가 원점까지 버리면 복귀 때 방향이 통째로 회전한다."""
    dr = DeadReckon()
    dr.update(0.0, math.radians(30))
    dr.pause()
    _, _, yaw = dr.update(1.0, math.radians(30))
    assert abs(yaw) < 1e-9                            # 여전히 30° 가 원점
