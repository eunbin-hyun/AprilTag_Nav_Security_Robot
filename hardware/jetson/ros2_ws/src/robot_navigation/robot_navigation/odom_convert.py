"""STM32 오도메트리 -> ROS 표준 단위 변환. 순수 함수만 둔다.

ROS 를 임포트하지 않는다. 단위 변환과 쿼터니언 계산은 틀려도 조용히 틀리는
종류의 결함이라 (지도에 점이 1000배 멀리 찍히거나 각도가 뒤집혀도 예외가
안 난다) ROS 없이 시험으로 고정한다. 메시지에 담는 일만 route_runner 가
한다.

단위 규약:
    STM32          mm, m-deg(1/1000 도), mm/s, m-deg/s
    ROS 표준        m, rad, m/s, rad/s
"""
import math

# nav_msgs/Odometry 의 좌표계 이름. REP-105 규약을 따른다.
ODOM_FRAME = 'odom'
BASE_FRAME = 'base_link'


def yaw_to_quaternion(yaw_rad: float) -> tuple:
    """z축 회전만 있는 쿼터니언 (x, y, z, w).

    2D 주행이라 roll·pitch 는 0 이다. 절반각을 쓰는 것이 쿼터니언 정의다 —
    yaw 를 그대로 넣으면 각도가 두 배가 된다.
    """
    half = yaw_rad / 2.0
    return (0.0, 0.0, math.sin(half), math.cos(half))


def odom_si(o: dict) -> dict:
    """TELEMETRY_ODOMETRY dict -> SI 단위 dict.

    입력은 protocol.unpack_telemetry_odometry() 의 반환값 그대로다.
    """
    yaw = math.radians(o['yaw_mdeg'] / 1000.0)
    return {
        'x': o['x_mm'] / 1000.0,
        'y': o['y_mm'] / 1000.0,
        'yaw': yaw,
        'quat': yaw_to_quaternion(yaw),
        'vx': o['linear_speed_mm_s'] / 1000.0,
        'wz': math.radians(o['yaw_rate_mdeg_s'] / 1000.0),
        'distance': o['distance_mm'] / 1000.0,
    }


def wrap_angle(rad: float) -> float:
    """각도를 -pi~pi 로 접는다. 0x83 원점을 빼도 값이 안 불어나게 한다."""
    return math.atan2(math.sin(rad), math.cos(rad))


class DeadReckon:
    """엔코더 거리 + **주행이 쓰는 yaw** 로 위치를 적분한다.

    ## 왜 이 클래스가 필요한가

    STM32 가 0x85 에 실어 보내는 x·y 는 **자기 융합 yaw 로 적분한 값**이다.
    그 yaw 는 조향 명령 기반 모델 성분을 25% 섞어서 실제보다 부풀려지고
    (2026-08-05 실측 1.275 배), 조향 트림이 걸려 있으면 직진을 곡선으로
    오해한다 (트림 +500·축간거리 0.135 m 에서 약 37 deg/m). 그래서 위치도
    같이 틀어진다 — "yaw 만 틀리고 x·y 는 맞다" 는 성립하지 않는다.

    주행은 회전을 0x83 IMU quaternion yaw 로 판정한다. 그 yaw 와 엔코더
    거리로 위치를 다시 세우면 관제 지도가 **차가 실제로 판단한 것과 같은
    것**을 보게 된다.

    ## 규약

    - 첫 유효 표본의 yaw 를 원점(0)으로 잡는다. 0x83 은 game rotation vector
      라 절대 방위가 아니고 부팅 시점 기준이라, 안 빼면 `odom` 프레임 정의가
      깨진다 (REP-105 는 "시작 자세가 원점" 이다).
    - 거리 증분은 **부호를 그대로** 쓴다. `distance_mm` 은 signed 누적이라
      후진하면 줄어든다. abs 를 씌우면 후진이 전진으로 기록된다.
    - `pause()` 는 적분을 멈추되 위치를 **버리지 않는다.** 거리 기준점만
      새로 잡아서, 무효 구간의 이동분이 복귀 순간 한꺼번에 튀지 않게 한다.
      틀린 위치보다 없는 위치가 낫다는 기존 정책과 같은 방향이다.
    """

    def __init__(self):
        """원점을 아직 못 잡은 상태로 시작한다 (첫 `update` 가 잡는다)."""
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0              # 원점을 뺀 상대 yaw [rad]
        self._yaw0 = None           # 첫 유효 표본의 yaw (원점)
        self._last_distance = None  # 직전 표본의 누적 거리 [m]

    @property
    def started(self) -> bool:
        """원점을 잡은 적이 있나."""
        return self._yaw0 is not None

    def pause(self):
        """적분을 멈춘다. 위치·원점은 유지하고 거리 기준점만 버린다.

        게이트가 닫힌 구간(오도메트리 무효·0x83 폴백)에서 부른다. 다음
        `update` 는 그 시점의 거리를 새 기준으로 삼으므로, 멈춘 동안 움직인
        거리는 위치에 반영되지 않는다.
        """
        self._last_distance = None

    def update(self, distance_m: float, yaw_rad: float) -> tuple:
        """한 걸음 적분하고 `(x, y, yaw)` 를 돌려준다.

        `distance_m` 은 누적값(증분이 아니다), `yaw_rad` 는 0x83 원본 yaw 다.
        """
        if self._yaw0 is None:
            self._yaw0 = yaw_rad
        self.yaw = wrap_angle(yaw_rad - self._yaw0)
        if self._last_distance is not None:
            ds = distance_m - self._last_distance
            self.x += ds * math.cos(self.yaw)
            self.y += ds * math.sin(self.yaw)
        self._last_distance = distance_m
        return (self.x, self.y, self.yaw)
