"""/cmd_vel(Twist) -> (speed_mm_s, steering_cdeg, enable) 변환 (순수 함수).

명세 §9.5, §11.5:
- v = linear.x [m/s], omega = angular.z [rad/s] (REP-103)
- steering = atan(wheelbase * omega / v), 좌(+)/우(-)
- |v| < v_eps 이고 |omega| > w_eps : Ackermann 제자리 회전 불가 -> 거부
- TBD 보호: wheelbase/max 값 미설정이면 비영 주행 금지 (명세 §20.5)
"""
import math
from dataclasses import dataclass


@dataclass
class DriveCmd:
    speed_mm_s: int
    steering_cdeg: int
    enable: bool


NEUTRAL = DriveCmd(0, 0, False)


def twist_to_drive(v: float, omega: float, *,
                   wheelbase_m: float,
                   max_speed_mm_s: int,
                   max_steering_cdeg: int,
                   v_eps: float = 0.02,
                   w_eps: float = 0.02):
    """변환 결과 DriveCmd 또는 None(거부: 제자리 회전 요청).

    반환 None이면 호출자는 neutral을 유지하고 경고를 남긴다.
    """
    # TBD 보호: 한계값이 없으면 비영 주행 금지
    if not wheelbase_m or not max_speed_mm_s or not max_steering_cdeg:
        return NEUTRAL

    if abs(v) < v_eps:
        if abs(omega) > w_eps:
            return None                      # 제자리 회전 -> 명령 거부
        return NEUTRAL                       # 정지

    steering_rad = math.atan(wheelbase_m * omega / v)
    speed = round(v * 1000.0)
    steering = round(math.degrees(steering_rad) * 100.0)

    # clamp (명세 §9.5)
    speed = max(-max_speed_mm_s, min(max_speed_mm_s, speed))
    steering = max(-max_steering_cdeg, min(max_steering_cdeg, steering))

    return DriveCmd(speed, steering, True)
