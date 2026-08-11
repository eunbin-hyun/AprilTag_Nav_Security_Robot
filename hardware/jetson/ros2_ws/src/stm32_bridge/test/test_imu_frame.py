"""0x83 TELEMETRY_IMU 프레임 시험. ROS 없이 돈다.

38 byte 고정 길이와 필드 순서를 고정한다. 필드 하나가 밀리면 gyro 가
가속도 자리로 들어가는데 예외는 안 난다 — 시험으로만 잡힌다.
"""
import struct

import pytest

from stm32_bridge import protocol as P
from stm32_bridge.parser import FrameParser


def sample(**kw):
    d = dict(mcu_time_ms=5250, quat_i=0, quat_j=0, quat_k=11585,
             quat_real=11585, gyro_x_mdeg_s=0, gyro_y_mdeg_s=0,
             gyro_z_mdeg_s=90_000, accel_x_mm_s2=100, accel_y_mm_s2=-200,
             accel_z_mm_s2=50, yaw_mdeg=45_000, gyro_accuracy=3,
             accel_accuracy=2, quat_accuracy=3,
             status_flags=(P.IMU_CONNECTED | P.IMU_GYRO_VALID
                           | P.IMU_LINEAR_ACCEL_VALID
                           | P.IMU_QUATERNION_VALID))
    d.update(kw)
    return d


# ── 길이·구조 ─────────────────────────────────────────────

def test_payload_length_is_38():
    """인수인계 문서가 못박은 값이다. 다르면 파서가 프레임을 폐기한다."""
    assert P.FIXED_LENGTHS[P.TELEMETRY_IMU] == 38
    assert struct.calcsize(P.IMU_STRUCT) == 38


def test_struct_format_matches_handover():
    assert P.IMU_STRUCT == '<IhhhhiiihhhiBBBB'


def test_wrong_length_is_rejected():
    with pytest.raises(ValueError):
        P.unpack_telemetry_imu(b'\x00' * 37)


# ── 왕복 ──────────────────────────────────────────────────

def test_roundtrip_preserves_every_field():
    d = sample()
    frames = FrameParser().feed(P.pack_telemetry_imu(7, **d))
    assert len(frames) == 1
    mid, seq, pl = frames[0]
    assert mid == P.TELEMETRY_IMU and seq == 7
    got = P.unpack_telemetry_imu(pl)
    assert got['mcu_time_ms'] == 5250
    assert got['quaternion_k_q14'] == 11585
    assert got['quaternion_real_q14'] == 11585
    assert got['gyro_z_mdeg_s'] == 90_000
    assert got['linear_accel_y_mm_s2'] == -200
    assert got['yaw_mdeg'] == 45_000
    assert got['gyro_accuracy'] == 3
    assert got['linear_accel_accuracy'] == 2
    assert got['quaternion_accuracy'] == 3


def test_field_order_not_shifted():
    """각 필드에 서로 다른 값을 넣어 자리가 밀리지 않았는지 본다."""
    d = sample(gyro_x_mdeg_s=11, gyro_y_mdeg_s=22, gyro_z_mdeg_s=33,
               accel_x_mm_s2=44, accel_y_mm_s2=55, accel_z_mm_s2=66,
               quat_i=1, quat_j=2, quat_k=3, quat_real=4)
    got = P.unpack_telemetry_imu(
        FrameParser().feed(P.pack_telemetry_imu(0, **d))[0][2])
    assert (got['gyro_x_mdeg_s'], got['gyro_y_mdeg_s'],
            got['gyro_z_mdeg_s']) == (11, 22, 33)
    assert (got['linear_accel_x_mm_s2'], got['linear_accel_y_mm_s2'],
            got['linear_accel_z_mm_s2']) == (44, 55, 66)
    assert (got['quaternion_i_q14'], got['quaternion_j_q14'],
            got['quaternion_k_q14'], got['quaternion_real_q14']) == (1, 2, 3, 4)


def test_negative_values_survive():
    got = P.unpack_telemetry_imu(
        FrameParser().feed(P.pack_telemetry_imu(
            0, **sample(gyro_z_mdeg_s=-90_000, yaw_mdeg=-179_000,
                        quat_k=-11585)))[0][2])
    assert got['gyro_z_mdeg_s'] == -90_000
    assert got['yaw_mdeg'] == -179_000
    assert got['quaternion_k_q14'] == -11585


# ── status bit ────────────────────────────────────────────

def test_status_bit_positions():
    """bit 순서가 인수인계 문서와 같아야 한다."""
    assert P.IMU_CONNECTED == 1 << 0
    assert P.IMU_GYRO_VALID == 1 << 1
    assert P.IMU_LINEAR_ACCEL_VALID == 1 << 2
    assert P.IMU_QUATERNION_VALID == 1 << 3
    assert P.IMU_STALE == 1 << 4
    assert P.IMU_SPI_ERROR == 1 << 5
    assert P.IMU_PROTOCOL_ERROR == 1 << 6


def test_healthy_requires_connected_and_gyro():
    assert P.imu_healthy(P.IMU_CONNECTED | P.IMU_GYRO_VALID)
    assert not P.imu_healthy(P.IMU_CONNECTED)
    assert not P.imu_healthy(P.IMU_GYRO_VALID)
    assert not P.imu_healthy(0)


def test_stale_makes_it_unhealthy():
    """데이터가 오래됐으면 valid bit 가 서 있어도 믿으면 안 된다."""
    ok = P.IMU_CONNECTED | P.IMU_GYRO_VALID
    assert P.imu_healthy(ok)
    assert not P.imu_healthy(ok | P.IMU_STALE)


def test_describe_lists_names():
    txt = P.describe_imu_status(P.IMU_CONNECTED | P.IMU_SPI_ERROR)
    assert 'CONNECTED' in txt and 'SPI_ERROR' in txt
    assert P.describe_imu_status(0) == 'none'


# ── IMU_LOST 는 주행을 막지 않는다 ─────────────────────────

def test_imu_lost_does_not_block_arming():
    """report-only 다. 이걸로 주행을 멈추면 IMU 없이는 아예 못 달린다
    (인수인계 2026-08-03 §5)."""
    assert not P.arm_blocking_faults(P.FAULT_IMU_LOST)


def test_imu_fused_is_not_required_for_pose():
    """IMU 가 없어도 STM32 는 모델로 복귀한다. pose 는 계속 유효하다."""
    base = (P.ODOM_VALID | P.ODOM_ENCODER_CALIBRATED
            | P.ODOM_GEOMETRY_CALIBRATED | P.ODOM_STEERING_ESTIMATED)
    src = P.ODOM_STEERING_COMMAND_ESTIMATE
    assert P.odom_pose_valid(base, src)                      # IMU_FUSED=0
    assert P.odom_pose_valid(base | P.ODOM_IMU_FUSED, src)   # IMU_FUSED=1
