#!/usr/bin/env python3
"""합성 `LaserScan` 발행기 — 라이다 없이 판정 로직을 검증한다.

실장비 없이 `lidar_probe.py` 와 앞으로 만들 `lidar_logic` 을 시험하기 위한
도구다. YDLidar X4 Pro 제원을 흉내내되 장애물을 **정확한 좌표로** 넣을 수
있어서, 실제 라이다로는 재현하기 어려운 경계 조건을 만들 수 있다.

    ./fake_scan.py                              # 빈 공간
    ./fake_scan.py --wall 0.8                   # 정면 0.8 m 벽
    ./fake_scan.py --box 0.6,0.0,0.3            # (x,y,너비) 물체
    ./fake_scan.py --box 0.6,0.45,0.3           # 옆으로 비킨 물체 (오판 시험)
    ./fake_scan.py --selfhit -175,175,0.08      # self-hit 흉내
    ./fake_scan.py --nan                        # 전부 NaN (INVALID 시험)
    ./fake_scan.py --dropout 0.5                # 유효 ray 50% 만
    ./fake_scan.py --spike 0.4,10               # 한 점 노이즈 (디바운스 시험)
    ./fake_scan.py --sweep 1.5:0.3:0.1          # 1.5 m → 0.3 m 로 접근

각도는 **센서 프레임 기준 도(°)**, 0° 가 정면, 좌측이 +다.
"""
import argparse
import math
import sys
import time

try:
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan
except ImportError as e:                                   # noqa: BLE001
    raise SystemExit(f'ROS 2 환경을 source 해야 한다: {e}')

# X4 Pro 실측 제원에 맞춘 기본값. 실장비 측정 후 조정한다.
N_RAYS = 440
HZ = 11.4
RANGE_MIN, RANGE_MAX = 0.10, 12.0


def parse_triplet(s: str, n: int = 3) -> tuple:
    """쉼표 또는 콜론으로 구분한 숫자 n 개."""
    parts = [float(x) for x in s.replace(':', ',').split(',')]
    if len(parts) != n:
        raise argparse.ArgumentTypeError(f'{n} 개 값이 필요하다: {s}')
    return tuple(parts)


# 값이 '-' 로 시작하면 argparse 가 옵션으로 오해한다. 실측 현장에서 음수 각도를
# 계속 쓰게 되므로(`--selfhit -178,-172,0.08`) 미리 `--opt=값` 으로 붙여준다.
TUPLE_OPTS = ('--box', '--selfhit', '--spike', '--sweep')


def glue_negative_args(argv: list) -> list:
    out, i = [], 0
    while i < len(argv):
        if argv[i] in TUPLE_OPTS and i + 1 < len(argv):
            out.append(f'{argv[i]}={argv[i + 1]}')
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


class FakeScan(Node):
    def __init__(self, a):
        super().__init__('fake_scan')
        self.a = a
        self.pub = self.create_publisher(LaserScan, a.topic,
                                         qos_profile_sensor_data)
        self.angle_min = -math.pi
        self.inc = 2.0 * math.pi / N_RAYS
        self.t0 = time.monotonic()
        self.n = 0
        self.create_timer(1.0 / a.rate, self._tick)
        self.get_logger().info(
            f'{a.topic} 발행 시작 — {N_RAYS} ray, {a.rate:.1f} Hz')

    def _sweep_scale(self) -> float:
        """--sweep: 시작→끝 거리를 왕복시킨다. 접근/이탈 히스테리시스 시험용."""
        if not self.a.sweep:
            return None
        lo, hi, speed = self.a.sweep[1], self.a.sweep[0], self.a.sweep[2]
        span = abs(hi - lo)
        if span < 1e-6 or speed <= 0:
            return lo
        period = 2.0 * span / speed
        t = (time.monotonic() - self.t0) % period
        d = t * speed
        return hi - d if d <= span else lo + (d - span)

    def _ranges(self) -> list:
        a = self.a
        rays = [float('inf')] * N_RAYS
        angs = [self.angle_min + i * self.inc for i in range(N_RAYS)]

        # 배경 벽 (원형). 없으면 무한대 = 빈 공간
        if a.room > 0:
            rays = [a.room] * N_RAYS

        def put(x: float, y: float, width: float):
            """(x, y) 중심, 폭 width 인 정면 평면 물체를 광선으로 변환한다."""
            for i, th in enumerate(angs):
                ct, st = math.cos(th), math.sin(th)
                if ct <= 1e-6:                    # 뒤쪽 광선은 안 맞는다
                    continue
                r = x / ct                        # x = r·cosθ 평면 교점
                if r <= 0 or r > RANGE_MAX:
                    continue
                if abs(r * st - y) <= width / 2.0:
                    rays[i] = min(rays[i], r)

        scale = self._sweep_scale()
        if scale is not None:
            put(scale, 0.0, a.sweep_width)

        if a.wall is not None:
            put(a.wall, 0.0, 10.0)
        for bx, by, bw in a.box:
            put(bx, by, bw)

        # self-hit: 고정 각도 구간을 아주 짧은 거리로 채운다
        for lo, hi, d in a.selfhit:
            for i, th in enumerate(angs):
                if lo <= math.degrees(th) <= hi:
                    rays[i] = d

        if a.nan:
            rays = [float('nan')] * N_RAYS
        elif a.dropout > 0:
            # 결정적으로 솎아낸다 — 난수를 쓰면 시험이 재현되지 않는다
            keep = max(1, int(round(1.0 / max(1e-6, 1.0 - a.dropout))))
            rays = [r if (i % keep) == 0 else float('inf')
                    for i, r in enumerate(rays)]

        # 한 점 노이즈는 **맨 마지막**에 넣는다. dropout 앞에 두면 노이즈 ray
        # 자체가 솎여 나가서, 디바운스를 시험하려는 신호가 사라진다.
        for d, adeg in a.spike:
            i = int(round((math.radians(adeg) - self.angle_min) / self.inc))
            if 0 <= i < N_RAYS:
                rays[i] = d
        return rays

    def _tick(self):
        m = LaserScan()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = self.a.frame
        m.angle_min = self.angle_min
        m.angle_max = self.angle_min + (N_RAYS - 1) * self.inc
        m.angle_increment = self.inc
        m.scan_time = 1.0 / self.a.rate
        m.time_increment = m.scan_time / N_RAYS
        m.range_min = RANGE_MIN
        m.range_max = RANGE_MAX
        m.ranges = self._ranges()
        self.pub.publish(m)
        self.n += 1
        if self.n % int(self.a.rate * 5) == 0:
            self.get_logger().info(f'{self.n} 프레임 발행')


def main():
    p = argparse.ArgumentParser(description='합성 LaserScan 발행기')
    p.add_argument('--topic', default='/scan')
    p.add_argument('--frame', default='laser_frame')
    p.add_argument('--rate', type=float, default=HZ)
    p.add_argument('--room', type=float, default=0.0,
                   help='배경 원형 벽 거리 [m]. 0 이면 빈 공간')
    p.add_argument('--wall', type=float, default=None,
                   help='정면 벽 거리 [m]')
    p.add_argument('--box', type=parse_triplet, action='append', default=[],
                   metavar='x,y,폭', help='물체. 여러 번 지정 가능')
    p.add_argument('--selfhit', type=parse_triplet, action='append',
                   default=[], metavar='시작°,끝°,거리',
                   help='self-hit 구간')
    p.add_argument('--spike', type=lambda s: parse_triplet(s, 2),
                   action='append', default=[], metavar='거리,각도°',
                   help='한 점 노이즈')
    p.add_argument('--sweep', type=parse_triplet, default=None,
                   metavar='시작:끝:속도', help='정면 물체를 왕복시킨다')
    p.add_argument('--sweep-width', type=float, default=0.30)
    p.add_argument('--nan', action='store_true', help='전부 NaN')
    p.add_argument('--dropout', type=float, default=0.0,
                   help='무효로 만들 ray 비율 0~1')
    a = p.parse_args(glue_negative_args(sys.argv[1:]))

    rclpy.init(args=None)
    node = FakeScan(a)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == '__main__':
    sys.exit(main() or 0)
