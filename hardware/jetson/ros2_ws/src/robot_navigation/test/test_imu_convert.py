"""imu_convert 단위시험. ROS 없이 돈다.

Q14 고정소수점과 m-deg 변환은 틀려도 예외가 안 나고 조용히 틀린다 — 각속도가
1000 배로 찍히거나 쿼터니언이 단위가 아니어도 프로그램은 멀쩡히 돈다.
"""
import math

from robot_navigation.imu_convert import (
    DEFAULT_IMU_FRAME, Q14_SCALE, imu_si, q14_to_unit,
)


def frame(**kw):
    """unpack_telemetry_imu() 반환 형태."""
    d = dict(mcu_time_ms=0, quaternion_i_q14=0, quaternion_j_q14=0,
             quaternion_k_q14=0, quaternion_real_q14=int(Q14_SCALE),
             gyro_x_mdeg_s=0, gyro_y_mdeg_s=0, gyro_z_mdeg_s=0,
             linear_accel_x_mm_s2=0, linear_accel_y_mm_s2=0,
             linear_accel_z_mm_s2=0, yaw_mdeg=0, gyro_accuracy=3,
             linear_accel_accuracy=3, quaternion_accuracy=3, status_flags=0)
    d.update(kw)
    return d


# ── Q14 쿼터니언 ──────────────────────────────────────────

def test_q14_scale_is_16384():
    """Q14 는 2^14 다. 다른 값을 쓰면 회전이 통째로 틀어진다."""
    assert Q14_SCALE == 16384.0


def test_identity():
    assert q14_to_unit(0, 0, 0, int(Q14_SCALE)) == (0.0, 0.0, 0.0, 1.0)


def test_result_is_unit_after_quantization():
    """Q14 양자화로 크기가 1 을 벗어나도 정규화돼 나와야 한다."""
    half = math.radians(37.0) / 2.0
    k = int(round(math.sin(half) * Q14_SCALE))
    r = int(round(math.cos(half) * Q14_SCALE))
    x, y, z, w = q14_to_unit(0, 0, k, r)
    assert abs(math.sqrt(x * x + y * y + z * z + w * w) - 1.0) < 1e-12


def test_recovers_yaw_angle():
    """쿼터니언에서 원래 각도가 나와야 한다 (절반각 규약 확인)."""
    for deg in (-150.0, -37.0, 0.0, 37.0, 150.0):
        half = math.radians(deg) / 2.0
        k = int(round(math.sin(half) * Q14_SCALE))
        r = int(round(math.cos(half) * Q14_SCALE))
        _, _, z, w = q14_to_unit(0, 0, k, r)
        got = math.degrees(2.0 * math.atan2(z, w))
        assert abs(got - deg) < 0.02, f'{deg} -> {got}'


def test_all_zero_gives_identity_not_nan():
    """센서가 아직 값을 안 주면 0 벡터가 온다. 정규화하면 0 나눗셈이다."""
    assert q14_to_unit(0, 0, 0, 0) == (0.0, 0.0, 0.0, 1.0)


# ── 단위 변환 ─────────────────────────────────────────────

def test_gyro_mdeg_to_rad():
    """m-deg/s 는 1/1000 도/s 다. 1000 으로 안 나누면 1000 배 틀린다."""
    si = imu_si(frame(gyro_z_mdeg_s=90_000))          # 90.000 deg/s
    assert abs(si['gyro'][2] - math.radians(90.0)) < 1e-9


def test_gyro_all_three_axes_independent():
    si = imu_si(frame(gyro_x_mdeg_s=1000, gyro_y_mdeg_s=-2000,
                      gyro_z_mdeg_s=3000))
    gx, gy, gz = si['gyro']
    assert abs(gx - math.radians(1.0)) < 1e-9
    assert abs(gy + math.radians(2.0)) < 1e-9
    assert abs(gz - math.radians(3.0)) < 1e-9


def test_accel_mm_to_m():
    si = imu_si(frame(linear_accel_x_mm_s2=1500,
                      linear_accel_z_mm_s2=-250))
    assert abs(si['accel'][0] - 1.5) < 1e-12
    assert abs(si['accel'][2] + 0.25) < 1e-12


def test_yaw_mdeg_to_rad():
    si = imu_si(frame(yaw_mdeg=-45_000))
    assert abs(si['yaw'] + math.pi / 4) < 1e-9


def test_negative_gyro_survives():
    """우회전은 음수다. 부호를 잃으면 회전 방향 진단이 반대가 된다."""
    assert imu_si(frame(gyro_z_mdeg_s=-30_000))['gyro'][2] < 0


def test_frame_default_is_base_link():
    """별도 프레임을 쓰면 TF 가 없어 tf2 소비자가 못 쓴다."""
    assert DEFAULT_IMU_FRAME == 'base_link'
