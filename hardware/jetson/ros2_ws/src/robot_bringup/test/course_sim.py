"""가상 코스 시뮬레이터 — 카메라 역할을 대신한다.

fake_stm32 가 적분한 로봇 위치(/stm32/odom_json)를 받아, 그 자리에서 카메라가
실제로 볼 태그 상을 계산해 /tag/target 으로 되돌린다. 즉 **닫힌 고리**다:

    route_runner -> CMD_DRIVE -> fake_stm32 -> pose -> (여기) -> /tag/target
                                                                     |
                                        route_runner 로 되돌아감 <----+

손으로 태그를 흔드는 시험은 로봇이 움직여도 태그 거리가 그에 맞게 줄지 않아
기하가 어긋난다. 이 시뮬레이터는 그 불일치가 없다.

⚠ 이것은 코스 배치와 상태머신 연결을 검증할 뿐, 카메라 검출 성능(조명·모션
  블러·화각 끝 왜곡)은 검증하지 못한다. 그건 실장비에서만 확인된다.
"""
import json
import math
import os

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

# 코스 배치 (m, rad). normal_yaw = 태그 면이 바라보는 방향.
#   1 도크        복귀 진입(+x 에서 -x 로 접근)을 마주봄
#   2 코너 면A    출발 진입(-x 에서 +x 로 접근)을 마주봄
#   3 도착지      출발 진입(+y 에서 -y 로 접근)을 마주봄
#   4 코너 면B    복귀 진입(-y 에서 +y 로 접근)을 마주봄
TAGS = {
    1: (0.00, -0.30, 0.0),
    2: (2.00, 0.00, math.pi),
    3: (2.00, -2.60, math.pi / 2),
    4: (2.00, -0.30, -math.pi / 2),
}

FOV_DEG = 65.0          # Brio 100 수평 화각
MAX_RANGE_M = 4.0
YAW_BIAS_DEG = float(os.environ.get('YAW_BIAS_DEG', '0'))


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


class CourseSim(Node):
    def __init__(self):
        super().__init__('course_sim')
        self.pub = self.create_publisher(String, '/tag/target', 5)
        self.create_subscription(String, '/stm32/odom_json', self.on_odom, 5)
        self.seen = set()

    def on_odom(self, msg):
        o = json.loads(msg.data)
        rx, ry = o['x_mm'] / 1000.0, o['y_mm'] / 1000.0
        # 추측항법 yaw 오차 주입. 상태머신은 odom yaw 로 회전을 끝내지만
        # 실제 차체는 그만큼 틀어져 있다 — 그 상황을 그대로 만든다.
        ryaw = math.radians(o['yaw_mdeg'] / 1000.0 + YAW_BIAS_DEG)

        # ★ 보이는 태그를 **전부** 발행한다 — tag_localizer_cv 와 같게.
        #   예전에는 가장 가까운 하나만 냈는데, 그게 실장비와 다른 지점이라
        #   "태그2·4 가 같이 보이면 4 가 2 를 덮는다" 는 버그를 시뮬레이터가
        #   가려버렸다 (2026-08-05 현장에서 발견). 코스 배치상 태그2 와 4 는
        #   0.30 m 떨어져 있어 실제로 자주 같이 보인다.
        visible = []
        for tid, (tx, ty, nyaw) in TAGS.items():
            # 진행 방향 = 태그 법선의 반대
            ph = wrap(nyaw + math.pi)
            dx, dy = math.cos(ph), math.sin(ph)
            vx, vy = tx - rx, ty - ry
            along = vx * dx + vy * dy
            # 경로 좌측 성분. 로봇이 경로 왼쪽이면 cross > 0 (우조향 유도)
            cross = -(vx * -dy + vy * dx)
            herr = wrap(ryaw - ph)
            rng = math.hypot(vx, vy)
            bearing = wrap(math.atan2(vy, vx) - ryaw)
            if along <= 0.05 or rng > MAX_RANGE_M:
                continue
            if abs(bearing) > math.radians(FOV_DEG / 2.0):
                continue
            visible.append((rng, tid, along, cross, herr, bearing, nyaw))

        if not visible:
            self.pub.publish(String(data=json.dumps(
                {'tag_id': 0, 'valid': False, 'reason': 'FOV 밖',
                 'd_x': 0.0, 'd_y': 0.0, 'range_m': 0.0, 'bearing': 0.0,
                 'cross_track': 0.0, 'heading_error': 0.0, 'along': 0.0,
                 'normal_yaw': 0.0, 'decision_margin': 0.0,
                 'pixel_width': 0.0, 'age_s': 0.0})))
            return
        for rng, tid, along, cross, herr, bearing, nyaw in visible:
            self.seen.add(tid)
            self.pub.publish(String(data=json.dumps({
                'tag_id': tid, 'valid': True, 'reason': '',
                'd_x': along, 'd_y': -cross, 'range_m': rng,
                'bearing': bearing,
                'cross_track': cross, 'heading_error': herr, 'along': along,
                'normal_yaw': nyaw, 'decision_margin': 60.0,
                'pixel_width': max(10.0, 160.0 / max(0.1, rng)),
                'age_s': 0.0})))


def main():
    rclpy.init()
    rclpy.spin(CourseSim())


if __name__ == '__main__':
    main()
