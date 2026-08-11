"""BNO085 TELEMETRY_IMU(0x83) -> ROS 표준 단위 변환. 순수 함수만 둔다.

ROS 를 임포트하지 않는다. Q14 고정소수점과 m-deg 변환은 틀려도 예외가 안 나고
조용히 틀린다 — 각속도가 1000배로 찍히거나 쿼터니언이 단위가 아니어도 프로그램은
멀쩡히 돈다. 그래서 시험으로 고정한다.

⚠ **여기서 만든 yaw 를 위치·방향 판단에 쓰지 않는다.**
  위치와 yaw 는 `0x85 TELEMETRY_ODOMETRY` 만 쓴다. STM32 가 이미 gyro Z 를
  엔코더+조향 모델과 융합해서 `0x85` 에 실어 보내므로, 젯슨이 `0x83` 의 raw
  yaw 를 다시 더하면 이중 융합이 된다 (인수인계 2026-08-03 §1·§3).
  `0x83` 은 센서 건강 상태 확인과 모니터링·디버깅 용도다.

단위 규약:
    STM32          Q14, m-deg/s(1/1000 도/s), mm/s^2, m-deg
    ROS 표준        무차원 쿼터니언, rad/s, m/s^2, rad
"""
import math

# BNO085 quaternion 은 Q14 고정소수점이다. 1.0 == 16384.
Q14_SCALE = 16384.0

# 기본 좌표계 이름. BNO085 축은 STM32 가 차체 기준(X 전방·Y 좌·Z 상)으로
# 맞춰 보내므로 base_link 와 축 방향이 같다. 병진 오프셋은 차량 미조립으로
# 실측 전이다 — 별도 프레임을 쓰면 TF 가 없어 tf2 소비자가 못 쓴다.
DEFAULT_IMU_FRAME = 'base_link'


def q14_to_unit(i_q14: int, j_q14: int, k_q14: int, real_q14: int) -> tuple:
    """Q14 정수 4개 -> 단위 쿼터니언 (x, y, z, w).

    양자화 때문에 크기가 정확히 1 이 아니므로 정규화한다. 전부 0 이면
    (센서가 아직 값을 안 준 경우) 항등 쿼터니언을 준다 — 0 벡터를
    orientation 에 실으면 소비자 쪽에서 0 나눗셈이 난다.
    """
    x = i_q14 / Q14_SCALE
    y = j_q14 / Q14_SCALE
    z = k_q14 / Q14_SCALE
    w = real_q14 / Q14_SCALE
    n = math.sqrt(x * x + y * y + z * z + w * w)
    if n < 1e-9:
        return (0.0, 0.0, 0.0, 1.0)
    return (x / n, y / n, z / n, w / n)


def imu_si(d: dict) -> dict:
    """unpack_telemetry_imu() 결과 -> SI 단위 dict."""
    return {
        'quat': q14_to_unit(d['quaternion_i_q14'], d['quaternion_j_q14'],
                            d['quaternion_k_q14'], d['quaternion_real_q14']),
        'gyro': (math.radians(d['gyro_x_mdeg_s'] / 1000.0),
                 math.radians(d['gyro_y_mdeg_s'] / 1000.0),
                 math.radians(d['gyro_z_mdeg_s'] / 1000.0)),
        'accel': (d['linear_accel_x_mm_s2'] / 1000.0,
                  d['linear_accel_y_mm_s2'] / 1000.0,
                  d['linear_accel_z_mm_s2'] / 1000.0),
        # 참고용으로만 노출한다. 주행 판단에 쓰지 않는다 (모듈 docstring 참고).
        'yaw': math.radians(d['yaw_mdeg'] / 1000.0),
    }
