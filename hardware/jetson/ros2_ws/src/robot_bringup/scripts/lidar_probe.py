#!/usr/bin/env python3
"""라이다 실측 도구 — 시현 전에 확정해야 하는 값을 `/scan` 에서 직접 잰다.

`lidar_logic` 을 짜기 전에 **숫자를 먼저 확보**하기 위한 스크립트다. self-hit
마스크와 전방 0° 오프셋 두 개는 코드로 추정할 수 없고, 틀리면 판정이 통째로
무의미해진다. 그래서 판정 로직보다 이 도구가 먼저다.

    ./lidar_probe.py                     # 제원 요약 (기본)
    ./lidar_probe.py selfhit             # self-hit / 차폐 구간 탐지
    ./lidar_probe.py front               # 전방 0° 오프셋 측정
    ./lidar_probe.py range               # 소품 감지 최대거리
    ./lidar_probe.py watch               # 섹터별 최소거리 실시간
    ./lidar_probe.py yaml                # 위 결과를 파라미터 초안으로 출력

공통 옵션:
    --topic /scan       구독 토픽
    --seconds 10        수집 시간 (selfhit/info)
    --min-points 3      물체로 인정할 최소 점 개수

⚠ 이 스크립트는 **읽기만** 한다. UART 를 열지 않고 주행 명령도 보내지 않는다.
  차량이 움직일 위험이 없으므로 반복 실행해도 안전하다.

⚠ 먼저 라이다 드라이버가 떠 있어야 한다. `/scan` 이 안 오면 원인을 드라이버와
  discovery 로 나눠서 안내한다 (아래 `_no_scan_help`).
"""
import argparse
import math
import statistics
import sys
import time

try:
    import rclpy
    from rclpy.executors import ExternalShutdownException
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import LaserScan
except ImportError as e:                                   # noqa: BLE001
    raise SystemExit(f'ROS 2 환경을 source 해야 한다: {e}\n'
                     '  source /opt/ros/humble/setup.bash\n'
                     '  source ~/ydlidar_x4pro/ros2_ws/install/setup.bash')


# Ctrl+C 는 rclpy 버전에 따라 KeyboardInterrupt 또는
# ExternalShutdownException 으로 온다. 둘 다 잡아야 측정 리포트가 출력된다.
STOP_EXC = (KeyboardInterrupt, ExternalShutdownException)


# ── scan 유틸 ──────────────────────────────────────────────

def ray_angles_deg(scan) -> list:
    """각 ray 의 센서 프레임 각도 [deg]. 0° 가 센서 정면이다 (차량 정면 아님)."""
    return [math.degrees(scan.angle_min + i * scan.angle_increment)
            for i in range(len(scan.ranges))]


def valid_mask(scan) -> list:
    """유효 ray 판정.

    NaN/Inf 와 range_min/max 밖을 모두 무효로 본다. 0.0 을 "측정 실패" 로 쓰는
    드라이버가 있어서 하한도 함께 본다 — 이걸 놓치면 차체 바로 앞에 물체가
    있는 것처럼 보인다.
    """
    lo, hi = scan.range_min, scan.range_max
    out = []
    for r in scan.ranges:
        out.append(math.isfinite(r) and r > 0.0 and lo <= r <= hi)
    return out


def clusters(scan, mask, max_gap: int = 2, range_jump: float = 0.12) -> list:
    """인접 ray 를 물체 단위로 묶는다.

    한 점 노이즈를 물체로 오인하지 않기 위해 필요하다. 인덱스가 이어지고
    거리 점프가 작은 구간을 한 덩어리로 본다.
    """
    n = len(scan.ranges)
    angs = ray_angles_deg(scan)
    out, cur = [], []

    def flush():
        if not cur:
            return
        rs = [scan.ranges[i] for i in cur]
        out.append({
            'n': len(cur),
            'r_min': min(rs),
            'r_mean': sum(rs) / len(rs),
            'ang_min': angs[cur[0]],
            'ang_max': angs[cur[-1]],
            'ang_mid': (angs[cur[0]] + angs[cur[-1]]) / 2.0,
        })

    last_i, last_r = None, None
    for i in range(n):
        if not mask[i]:
            continue
        r = scan.ranges[i]
        if (last_i is not None and i - last_i <= max_gap
                and abs(r - last_r) <= range_jump):
            cur.append(i)
        else:
            flush()
            cur = [i]
        last_i, last_r = i, r
    flush()
    return out


def nearest_cluster(scan, mask, min_points: int, max_gap: int = 2):
    """가장 가까운 물체. 없으면 None.

    `max_gap` 은 인접으로 인정할 ray 건너뜀 수다. 반사가 나쁜 표면에서는
    유효 ray 가 듬성듬성 들어와서 기본값 2 로는 **한 덩어리인 벽조차 1점
    조각으로 쪼개져** 아무것도 검출되지 않는다. 합성 scan `--dropout 0.7`
    에서 3 m 벽이 통째로 사라지는 것으로 확인했다. 검출이 안 되면 이 값을
    먼저 올려 보라.
    """
    cs = [c for c in clusters(scan, mask, max_gap=max_gap)
          if c['n'] >= min_points]
    return min(cs, key=lambda c: c['r_min']) if cs else None


def parse_span(t: str) -> tuple:
    """"시작°,끝°" 를 구간으로. 콜론도 허용한다."""
    v = [float(x) for x in t.replace(':', ',').split(',')]
    if len(v) != 2:
        raise argparse.ArgumentTypeError(f'"시작,끝" 형식이어야 한다: {t}')
    return (min(v), max(v))


# 값이 '-' 로 시작하면 argparse 가 옵션으로 오해한다. 마스크 각도는 대부분
# 음수라(`--mask -178,-172`) 미리 `--opt=값` 으로 붙여준다.
def glue_negative_args(argv: list) -> list:
    out, i = [], 0
    while i < len(argv):
        if argv[i] == '--mask' and i + 1 < len(argv):
            out.append(f'{argv[i]}={argv[i + 1]}')
            i += 2
            continue
        out.append(argv[i])
        i += 1
    return out


def probe_mask(scan, a) -> list:
    """유효 ray 에서 self-hit 마스크와 최소거리를 제외한다.

    self-hit 은 **언제나 가장 가까운 물체**라, 마스크 없이 최근접점을 찾으면
    front/range 모드가 전부 차체를 가리킨다. 그래서 selfhit 모드를 먼저 돌려
    구간을 얻고, 그 결과를 `--mask` 로 넘겨서 나머지 측정을 한다.
    """
    m = valid_mask(scan)
    if not a.mask and a.min_m <= 0.0:
        return m
    angs = ray_angles_deg(scan)
    for i in range(len(m)):
        if not m[i]:
            continue
        if a.min_m > 0.0 and scan.ranges[i] < a.min_m:
            m[i] = False
            continue
        for lo, hi in a.mask:
            if lo <= angs[i] <= hi:
                m[i] = False
                break
    return m


def merge_spans(idxs: list, angs: list) -> list:
    """인덱스 집합을 연속 각도 구간 리스트로 합친다."""
    if not idxs:
        return []
    spans, start, prev = [], idxs[0], idxs[0]
    for i in idxs[1:]:
        if i == prev + 1:
            prev = i
            continue
        spans.append((angs[start], angs[prev]))
        start = prev = i
    spans.append((angs[start], angs[prev]))
    return spans


# ── 수집 노드 ──────────────────────────────────────────────

def spin_tick(probe, timeout_sec: float = 0.2) -> bool:
    """한 번 spin 한다. 종료 신호면 False.

    rclpy 는 Ctrl+C 를 KeyboardInterrupt / ExternalShutdownException /
    RCLError(컨텍스트 무효) 중 무엇으로든 던진다 — 어느 것이 오는지는 신호가
    spin_once 안쪽 어디에 도착했느냐에 달렸다. 셋 다 "그만" 이라는 같은
    뜻이므로 여기서 흡수하고, 호출자는 그때까지 모은 값으로 리포트를 낸다.
    측정 도중 Ctrl+C 를 눌렀다고 결과를 잃으면 도구로 못 쓴다.
    """
    if not rclpy.ok():
        return False
    try:
        rclpy.spin_once(probe, timeout_sec=timeout_sec)
    except STOP_EXC:
        return False
    except Exception:                                      # noqa: BLE001
        if not rclpy.ok():        # 종료 중이면 정상 흐름이다
            return False
        raise
    return True


class ScanProbe(Node):
    """`/scan` 을 모으기만 하는 노드. 판정은 바깥에서 한다."""

    def __init__(self, topic: str):
        super().__init__('lidar_probe')
        self.frames = []
        self.stamps = []
        self.first = None
        # 센서 QoS(BEST_EFFORT)로 구독한다. 기본 RELIABLE 로 구독하면 드라이버가
        # BEST_EFFORT 로 발행할 때 **한 프레임도 안 들어온다** — 증상이 "드라이버
        # 죽음" 과 똑같아서 진단이 오래 걸리는 대표적인 함정이다.
        self.create_subscription(LaserScan, topic, self._on_scan,
                                 qos_profile_sensor_data)

    def _on_scan(self, msg: LaserScan):
        if self.first is None:
            self.first = msg
        self.frames.append(msg)
        self.stamps.append(time.monotonic())

    def hz(self) -> float:
        if len(self.stamps) < 2:
            return 0.0
        span = self.stamps[-1] - self.stamps[0]
        return (len(self.stamps) - 1) / span if span > 0 else 0.0


def collect(probe: ScanProbe, seconds: float, note: str = '') -> bool:
    """seconds 동안 수집. 한 프레임도 못 받으면 False."""
    if note:
        print(note)
    t0 = time.monotonic()
    last_report = t0
    while time.monotonic() - t0 < seconds:
        if not spin_tick(probe, 0.1):
            break
        now = time.monotonic()
        if now - last_report >= 1.0:
            left = seconds - (now - t0)
            print(f'\r  수집 중… {len(probe.frames)} 프레임, '
                  f'{left:.0f}초 남음   ', end='', flush=True)
            last_report = now
    print('\r' + ' ' * 50 + '\r', end='')
    if not probe.frames:
        _no_scan_help(probe)
        return False
    return True


def _no_scan_help(probe: ScanProbe):
    """scan 미수신 원인을 드라이버와 discovery 로 나눠서 안내한다.

    Discovery 문제면 토픽이 **하나도** 안 보이고, 드라이버 문제면 다른 토픽은
    보이는데 `/scan` 만 없다. 증상이 비슷해서 반드시 구분해서 물어야 한다.
    """
    print('\n✗ /scan 을 한 프레임도 못 받았다.\n')
    print('  1) 드라이버가 떠 있나')
    print('     ros2 node list | grep -i ydlidar')
    print('     → 노드가 없으면 드라이버 문제다. 포트·baud(128000)·전원 확인.')
    print('       라이다와 STM32 가 둘 다 CP2102 라 포트가 바뀌었을 수 있다:')
    print('       ros_ws/src/robot_bringup/scripts/serial_probe.py')
    print()
    print('  2) 토픽이 하나도 안 보이나')
    print('     ros2 topic list')
    print('     → 아무것도 없으면 discovery 문제다. 이 시스템은 현재 기본')
    print('       멀티캐스트 discovery 를 쓴다. ROS_DISCOVERY_SERVER 가')
    print('       설정돼 있으면 오히려 안 보인다:')
    print('       unset ROS_DISCOVERY_SERVER && ros2 daemon stop')
    print()
    print('  3) /scan 은 보이는데 여기로 안 오나')
    print('     ros2 topic hz /scan')
    print('     → 나온다면 QoS 문제다 (이 스크립트는 BEST_EFFORT 로 구독한다).')


# ── 모드: info ─────────────────────────────────────────────

def mode_info(probe: ScanProbe, a) -> dict:
    if not collect(probe, a.seconds, f'제원 측정 — {a.seconds:.0f}초 수집한다.'):
        return {}
    s = probe.first
    n = len(s.ranges)
    inc = math.degrees(s.angle_increment)

    ratios = []
    for f in probe.frames:
        m = valid_mask(f)
        ratios.append(sum(m) / len(m) if m else 0.0)

    print('── /scan 제원 ' + '─' * 45)
    print(f'  frame_id          {s.header.frame_id}')
    print(f'  ray 개수          {n}')
    print(f'  각도 범위         {math.degrees(s.angle_min):+.1f}° ~ '
          f'{math.degrees(s.angle_max):+.1f}°')
    print(f'  각도 분해능       {inc:.3f}°/ray')
    print(f'  거리 범위         {s.range_min:.3f} ~ {s.range_max:.1f} m')
    print(f'  실측 주기         {probe.hz():.2f} Hz '
          f'({len(probe.frames)} 프레임)')
    print(f'  유효 ray 비율     평균 {statistics.mean(ratios)*100:.1f}% '
          f'(최저 {min(ratios)*100:.1f}%)')

    # 점 간격은 거리에 비례한다. cluster 조건을 "연속 N개" 로 잡으면 안 되는
    # 이유가 여기서 눈에 보인다 — 같은 크기 물체가 멀면 점이 확 줄어든다.
    print('\n  거리별 ray 간격과 100 mm 물체에 걸리는 점 개수')
    for d in (0.3, 0.5, 1.0, 2.0, 3.0):
        gap_mm = d * math.radians(inc) * 1000.0
        pts = max(1, int(0.100 / (d * math.radians(inc))))
        print(f'    {d:>4.1f} m   간격 {gap_mm:>5.1f} mm   약 {pts:>3d} 점')

    lo = statistics.mean(ratios)
    if lo < 0.5:
        print('\n  ⚠ 유효 ray 비율이 낮다. 주변이 트여 있거나(반사체 없음)')
        print('    흡수체가 많다. 시현장에서 다시 재라.')
    return {'frame_id': s.header.frame_id, 'hz': probe.hz(),
            'inc_deg': inc, 'range_min': s.range_min,
            'range_max': s.range_max, 'n_rays': n}


# ── 모드: selfhit ──────────────────────────────────────────

def mode_selfhit(probe: ScanProbe, a) -> dict:
    print('── self-hit / 차폐 구간 탐지 ' + '─' * 32)
    print('  차량 주변 1 m 안을 **완전히 비우고** 실행하라.')
    print('  이 상태에서도 계속 잡히는 것은 차체 자신뿐이다.\n')
    if not collect(probe, a.seconds, f'  {a.seconds:.0f}초 수집한다.'):
        return {}

    s = probe.first
    n = len(s.ranges)
    angs = ray_angles_deg(s)
    frames = probe.frames

    hit, blocked = [], []
    for i in range(n):
        rs, nvalid = [], 0
        for f in frames:
            if i >= len(f.ranges):
                continue
            r = f.ranges[i]
            if math.isfinite(r) and r > 0.0 and s.range_min <= r <= s.range_max:
                rs.append(r)
                nvalid += 1
        ratio = nvalid / len(frames)
        if ratio < 0.05:
            # 항상 무효 = 아무것도 안 돌아온다. 트인 방향일 수도 있지만,
            # 좁은 구간이면 뭔가가 빔을 막고 있다는 뜻이다.
            blocked.append(i)
        elif ratio > 0.90 and len(rs) >= 3:
            # 항상 잡히고 거의 안 흔들리면 = 센서에 붙어 도는 물체 = 차체
            sd = statistics.pstdev(rs)
            if sd < 0.010 and statistics.median(rs) < a.selfhit_max_m:
                hit.append((i, statistics.median(rs), sd))

    hit_idx = [h[0] for h in hit]
    spans = merge_spans(hit_idx, angs)

    # ── A. 근거리 고정 반사 = range_min 바깥에 있는 차체
    print(f'  A. 근거리 고정 반사   {len(hit_idx)} ray / {n}')
    for lo, hi in spans:
        sub = [h for h in hit if lo <= angs[h[0]] <= hi]
        d = statistics.median([x[1] for x in sub]) if sub else 0.0
        print(f'     {lo:+7.2f}° ~ {hi:+7.2f}°   거리 {d*1000:.0f} mm')
    if not spans:
        print('     없음')

    # ── B. 항상 무효 = 차체가 range_min 보다 가깝거나 빔이 막혔다
    #
    # 이 구간을 "물체 없음" 으로 넘기면 안 된다. 차체가 range_min 안쪽에
    # 있으면 반사가 돌아오는데도 **무효로 보고**되므로, 짧은 거리로 잡히는
    # A 가 아니라 여기로 떨어진다. A 만 보고 "self-hit 없음" 이라고 결론내면
    # 실제로는 상시 STOP 인 장착을 통과시키게 된다.
    #
    # 트인 방향과 구분해야 한다. 넓게 뚫린 방향은 정상이고, **양옆은 멀쩡한데
    # 좁게 끊긴 구간**만 차체·브래킷 혐의가 있다.
    bspans = [(lo, hi) for lo, hi in merge_spans(blocked, angs)
              if 1.0 <= hi - lo <= 30.0]
    print(f'\n  B. 항상 무효 (폭 1~30° 만)   {len(bspans)} 개')
    for lo, hi in bspans:
        near = [f.ranges[i] for f in frames[:5] for i in range(n)
                if (lo - 6.0 <= angs[i] < lo or hi < angs[i] <= hi + 6.0)
                and math.isfinite(f.ranges[i]) and f.ranges[i] > 0]
        side = f'{statistics.median(near):.2f} m' if near else '이웃도 무효'
        print(f'     {lo:+7.2f}° ~ {hi:+7.2f}°   폭 {hi - lo:>4.1f}°  '
              f'양옆 {side}')
    if not bspans:
        print('     없음')

    all_spans = sorted(spans + bspans)
    print()
    if all_spans:
        print('  → self_hit_mask_deg 후보 (A + B, 여유 ±1°)')
        print('    ' + str([[round(lo - 1.0, 1), round(hi + 1.0, 1)]
                            for lo, hi in all_spans]))
        print(f'\n    B 구간은 눈으로 확인하라. range_min={s.range_min:.2f} m '
              '안쪽 물체는 무효로')
        print('    보고되므로 차체가 여기로 나타난다. 브래킷이 보이면 마스크하고,')
        print('    그냥 트인 방향이면 빼라.')
    else:
        print('  → 마스크할 구간 없음. 차체가 스캔 평면 밖이다 — 좋은 장착이다.')
    return {'self_hit_spans': all_spans}


# ── 모드: front ────────────────────────────────────────────

def mode_front(probe: ScanProbe, a) -> dict:
    print('── 전방 0° 오프셋 측정 ' + '─' * 38)
    print('  차량 **정면 1 m 앞 정중앙**에 밝은 무광 판을 세워라.')
    print('  판 말고 다른 물체는 2 m 안에 두지 마라.')
    print('  각도가 안정되면 Ctrl+C.\n')

    hist, last_r = [], None
    while spin_tick(probe):
        if not probe.frames:
            continue
        f = probe.frames[-1]
        probe.frames = probe.frames[-5:]
        c = nearest_cluster(f, probe_mask(f, a), a.min_points, a.max_gap)
        if c is None:
            print('\r  물체 없음 — 판이 스캔 평면에 안 걸린다        ',
                  end='', flush=True)
            continue
        last_r = c['r_min']
        hist.append(c['ang_mid'])
        hist = hist[-40:]
        med = statistics.median(hist)
        print(f'\r  최근접 {c["r_min"]:.3f} m  각도 {c["ang_mid"]:+7.2f}°  '
              f'점 {c["n"]:>3d}  |  중앙값 {med:+7.2f}°   ',
              end='', flush=True)

    if not hist:
        print('\n\n  ✗ 물체를 한 번도 못 잡았다.')
        print('    판이 스캔 평면 높이에 없거나, 반사가 안 되는 재질이다.')
        print('    판 높이를 바꿔가며 다시 시도하라 — 이게 장착 높이 실측이다.')
        return {}

    med = statistics.median(hist)
    sd = statistics.pstdev(hist) if len(hist) > 1 else 0.0
    print(f'\n\n  측정값  {med:+.2f}°  (표준편차 {sd:.2f}°, {len(hist)} 샘플)')
    print(f'\n  → mount_yaw_offset_deg: {med:.2f}')
    print('    판정 코드에서 각 ray 각도에 이 값을 빼면 차량 정면이 0° 가 된다.')
    if sd > 1.0:
        print('\n  ⚠ 흔들림이 크다. 판이 비스듬하거나 다른 물체가 같이 잡힌다.')
    if last_r is not None and last_r < 0.30:
        print(f'\n  ⚠ 잡힌 물체가 {last_r:.2f} m 로 너무 가깝다 — 판이 아니라')
        print('    차체(self-hit)일 가능성이 높다. selfhit 모드를 먼저 돌리고')
        print('    그 결과를 --mask 로 넘겨라:')
        print('      ./lidar_probe.py selfhit')
        print('      ./lidar_probe.py front --mask -178,-172 --mask 99,107')
    return {'mount_yaw_offset_deg': round(med, 2)}


# ── 모드: range ────────────────────────────────────────────

def mode_range(probe: ScanProbe, a) -> dict:
    print('── 소품 감지 최대거리 ' + '─' * 39)
    print('  시현에 쓸 판을 정면에 두고 **천천히 멀어져라**.')
    print('  감지가 끊기는 지점이 이 소품의 한계 거리다. Ctrl+C 로 종료.\n')

    best, lost_at = 0.0, None
    while spin_tick(probe):
        if not probe.frames:
            continue
        f = probe.frames[-1]
        probe.frames = probe.frames[-5:]
        c = nearest_cluster(f, probe_mask(f, a), a.min_points, a.max_gap)
        if c is None:
            print(f'\r  감지 끊김        (최대 {best:.2f} m)          ',
                  end='', flush=True)
            if best > 0 and lost_at is None:
                lost_at = best
            continue
        if c['r_min'] > best:
            best = c['r_min']
            lost_at = None
        bar = '█' * min(40, int(c['r_min'] * 10))
        print(f'\r  {c["r_min"]:>5.2f} m  점 {c["n"]:>3d}  {bar:<40} '
              f'(최대 {best:.2f} m)', end='', flush=True)

    print(f'\n\n  감지 최대거리  {best:.2f} m')
    if best == 0.0:
        print('  ✗ 한 번도 검출 못 했다. 유효 ray 가 있는데도 0 이면 반사가')
        print('    듬성듬성해서 cluster 가 쪼개진 것이다. 다음을 올려 보라:')
        print(f'      --max-gap {a.max_gap * 3}   (현재 {a.max_gap})')
        print(f'      --min-points 2  (현재 {a.min_points})')
        print('    ./lidar_probe.py info 로 유효 ray 비율을 먼저 확인하라.')
        return {'detect_max_m': 0.0}
    if best < 1.2:
        print('  ⚠ 감속 시작(1.0 m)을 걸기엔 여유가 없다. 판을 키우거나')
        print('    더 밝은 재질로 바꿔라. 감지거리는 소품이 좌우한다.')
    else:
        print(f'  → 감속 시작을 {min(1.0, best * 0.7):.2f} m 이하로 잡으면')
        print('    여유 있게 잡힌다.')
    return {'detect_max_m': round(best, 2)}


# ── 모드: watch ────────────────────────────────────────────

SECTORS = [('좌후', -180, -135), ('좌', -135, -45), ('좌전', -45, -15),
           ('전방', -15, 15), ('우전', 15, 45), ('우', 45, 135),
           ('우후', 135, 180)]


def mode_watch(probe: ScanProbe, a) -> dict:
    print('── 섹터별 최소거리 (센서 프레임) ' + '─' * 28)
    print('  RViz 없이 방향이 맞는지 눈으로 확인한다. Ctrl+C 로 종료.')
    print('  ⚠ 아직 mount_yaw_offset 을 안 뺀 센서 기준 각도다.\n')
    off = a.offset_deg
    while spin_tick(probe):
        if not probe.frames:
            continue
        f = probe.frames[-1]
        probe.frames = probe.frames[-5:]
        m = probe_mask(f, a)
        angs = ray_angles_deg(f)
        line = []
        for name, lo, hi in SECTORS:
            rs = [f.ranges[i] for i in range(len(f.ranges))
                  if m[i] and lo <= angs[i] - off < hi]
            v = f'{min(rs):.2f}' if rs else '  --'
            line.append(f'{name} {v}')
        print('\r  ' + ' │ '.join(line) + '   ', end='', flush=True)
    print()
    return {}


# ── 모드: yaml ─────────────────────────────────────────────

def mode_yaml(probe: ScanProbe, a) -> dict:
    info = mode_info(probe, a)
    if not info:
        return {}
    mask_yaml = str([[round(lo, 1), round(hi, 1)] for lo, hi in a.mask])
    print('\n── 파라미터 초안 ' + '─' * 43)
    print('  config/lidar_monitor.yaml 로 저장한다. --mask 와 --offset-deg 로')
    print('  넘긴 실측값이 반영된다. TODO 는 줄자로 재서 채운다.')
    if not a.mask:
        print('\n  ⚠ --mask 가 비었다. selfhit 모드를 먼저 돌려라.')
    if a.offset_deg == 0.0:
        print('  ⚠ --offset-deg 가 0 이다. front 모드를 먼저 돌려라.')
    print()
    print(f"""lidar_monitor:
  ros__parameters:
    scan_topic: "{a.topic}"
    # ── 장착 (front 모드 실측) ──
    mount_yaw_offset_deg: {a.offset_deg}
    mount_x_m: 0.0           # TODO: 줄자 — 뒷바퀴축 기준 라이다 x
    mount_y_m: 0.0           # TODO: 줄자 — 중심선에서 좌우 어긋남
    # ── self-hit 마스크 (selfhit 모드 실측) ──
    self_hit_mask_deg: {mask_yaml}
    # ── 차체 (TODO: 줄자로 실측) ──
    half_width_m: 0.09
    side_margin_m: 0.05
    # ── 판정 영역 ──
    slow_x_m: 1.00
    stop_x_m: 0.55
    clear_x_m: 0.70          # stop 보다 크게 — 떨림 방지
    # ── 디바운스 (거리 무관하게 프레임으로 잡는다) ──
    min_points: {a.min_points}
    stop_frames: 2           # 연속 N 프레임이면 STOP
    clear_frames: 5          # 연속 N 프레임이면 해제
    # ── 유효성 ──
    scan_stale_s: {max(0.25, 3.0 / max(1.0, info['hz'])):.2f}
    min_valid_ray_ratio: 0.20
    # ── 측정된 센서 제원 (참고) ──
    # frame_id={info['frame_id']} hz={info['hz']:.1f}
    # inc={info['inc_deg']:.3f}deg rays={info['n_rays']}
    # range={info['range_min']:.3f}~{info['range_max']:.1f}m""")
    return info


# ── 진입점 ─────────────────────────────────────────────────

MODES = {'info': mode_info, 'selfhit': mode_selfhit, 'front': mode_front,
         'range': mode_range, 'watch': mode_watch, 'yaml': mode_yaml}


def main():
    p = argparse.ArgumentParser(
        description='라이다 실측 도구 (읽기 전용)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='모드: ' + ' / '.join(MODES))
    p.add_argument('mode', nargs='?', default='info', choices=list(MODES))
    p.add_argument('--topic', default='/scan')
    p.add_argument('--seconds', type=float, default=10.0,
                   help='수집 시간 (info/selfhit/yaml)')
    p.add_argument('--min-points', type=int, default=3,
                   help='물체로 인정할 최소 점 개수')
    p.add_argument('--selfhit-max-m', type=float, default=0.30,
                   help='이 거리 안에서만 self-hit 으로 본다')
    p.add_argument('--offset-deg', type=float, default=0.0,
                   help='watch 모드에서 뺄 mount_yaw_offset_deg')
    p.add_argument('--mask', type=parse_span, action='append', default=[],
                   metavar='시작°,끝°',
                   help='self-hit 마스크. selfhit 모드 결과를 넣는다. 반복 가능')
    p.add_argument('--min-m', type=float, default=0.0,
                   help='이 거리보다 가까운 반사를 무시한다')
    p.add_argument('--max-gap', type=int, default=2,
                   help='한 물체로 이을 ray 건너뜀 수. 반사가 나쁘면 올려라')
    a = p.parse_args(glue_negative_args(sys.argv[1:]))

    rclpy.init(args=None)
    probe = ScanProbe(a.topic)
    print(f'\n{a.topic} 구독 중 (BEST_EFFORT)…\n')
    try:
        MODES[a.mode](probe, a)
    finally:
        probe.destroy_node()
        rclpy.try_shutdown()
    print()


if __name__ == '__main__':
    sys.exit(main() or 0)
