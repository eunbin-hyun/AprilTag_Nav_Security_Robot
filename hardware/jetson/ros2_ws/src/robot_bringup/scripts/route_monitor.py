#!/usr/bin/env python3
"""주행 상태 + STM32 로 나가는 명령을 터미널에 실시간 표시한다.

    ./route_monitor.py                 # 상태 + 명령
    ./route_monitor.py --tags          # 태그 검출까지 함께
    ./route_monitor.py --servo         # ★ 횡보정이 제대로 되는지 검증
    ./route_monitor.py --rate 5        # 출력 주기 [Hz]

⚠ `/route/state` 의 `command` 는 **제어층 출력**이다 (조향 트림 적용 전).
  전선에 실제로 나간 값은 `/stm32/command` 의 `steering_cdeg` 다. 트림이
  걸려 있으면 둘이 다르고, 좌회전은 한계에 포화하기까지 한다. 그래서
  이 모니터는 두 값을 나란히 보여준다.

--servo 는 횡보정을 판정하는 데 필요한 것만 모아 한 줄로 낸다:
  cross      태그 중심선에서 벗어난 양 [m]. 이게 0 으로 줄어야 정상
  str        제어층이 낸 조향. cross 와 **반대 부호**여야 한다
  wire       트림 얹은 실제 조향. `SAT` 이면 요청한 곡률이 안 나갔다
  기대        제어법으로 다시 계산한 값. str 과 어긋나면 게인·입력이 다르다
  모드        servo(횡보정 O) / hold(heading 유지 — cross 를 아예 무시)

읽기 전용이다. 명령을 보내지 않는다.
"""
import argparse
import json
import math
import sys
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

# 제어법을 다시 계산해 보기 위한 게인 기본값. tag_geometry.SteeringConfig 와
# 같아야 한다. 런치에서 kp_* 를 바꿨다면 --kp-cross/--kp-heading 로 맞춰라 —
# 안 맞추면 "기대" 열이 틀린 값을 가리킨다.
KP_CROSS = 4000.0        # cross_track[m] -> cdeg
KP_HEADING = 1200.0      # heading_error[rad] -> cdeg
# 이 이하의 cross 는 진동 판정에서 무시한다 (수렴 상태의 떨림).
FLIP_DEADBAND_M = 0.01


def bar(value, lo, hi, width=21):
    """중앙 0 기준 좌우 게이지. 조향을 눈으로 보기 쉽게."""
    mid = width // 2
    cells = [' '] * width
    cells[mid] = '|'
    if value == 0:
        return ''.join(cells)
    span = hi if value > 0 else -lo
    n = int(round(abs(value) / max(1, span) * mid))
    n = max(1, min(mid, n))
    for k in range(1, n + 1):
        idx = mid + k if value > 0 else mid - k
        if 0 <= idx < width:
            cells[idx] = '#'
    return ''.join(cells)


class Monitor(Node):
    def __init__(self, args):
        super().__init__('route_monitor')
        self.a = args
        self.state = None
        self.tags = {}
        self.diag = None
        self.wire = None
        self.create_subscription(String, '/route/state', self.on_state, 5)
        self.create_subscription(String, '/tag/target', self.on_tag, 10)
        self.create_subscription(String, '/tag/diagnostics', self.on_diag, 5)
        self.create_subscription(String, '/stm32/command', self.on_wire, 5)
        self.create_timer(1.0 / max(0.5, args.rate), self.show)
        self.n = 0
        # 횡보정 추세 판정용. cross 부호가 계속 뒤집히면 진동이다.
        self._cross_hist = []

    def on_state(self, m):
        try:
            self.state = json.loads(m.data)
        except json.JSONDecodeError:
            pass

    def on_wire(self, m):
        try:
            self.wire = json.loads(m.data)
        except json.JSONDecodeError:
            pass

    def on_tag(self, m):
        try:
            t = json.loads(m.data)
            self.tags[t['tag_id']] = (t, time.monotonic())
        except (json.JSONDecodeError, KeyError):
            pass

    def on_diag(self, m):
        try:
            self.diag = json.loads(m.data)
        except json.JSONDecodeError:
            pass

    def show_servo(self, st):
        """횡보정 판정용 한 줄. 무엇을 보고 무엇을 의심할지까지 찍는다."""
        mode = st.get('drive_mode') or '-'
        if st.get('step_kind') != 'drive':
            print(f'{"":>10} 횡보정: drive 단계가 아니다 '
                  f'({st.get("step_kind") or "-"})', flush=True)
            return
        if mode != 'servo':
            vis = st.get('tags_visible') or []
            print(f'{"":>10} ★ 횡보정 OFF (heading 유지) — 목표태그 '
                  f'{st["target_tag_id"]} 미확보. 보이는 태그 {vis}. '
                  f'cross 를 아예 쓰지 않는 구간이다', flush=True)
            return
        # ★ /stm32/command 에서 **한 메시지로** 꺼낸다. /route/state 의 태그
        #   값과 command 를 짝지으면 최대 한 tick 어긋나서 전이 구간마다
        #   거짓 "게인 이상" 경고가 난다 (실측 ±1600 cdeg).
        w = self.wire or {}
        cross = w.get('servo_cross_m')
        head = w.get('servo_head_deg')
        sd = w.get('servo_steering_cdeg')
        if cross is None or head is None or sd is None:
            print(f'{"":>10} 횡보정: /stm32/command 의 servo 값 대기 중',
                  flush=True)
            return
        # 제어법을 그대로 다시 계산한다. 어긋나면 게인이 다르다는 뜻이다
        # (런치에서 kp_* 를 바꿨으면 --kp-cross/--kp-heading 로 맞춰라).
        kc, kh = self.a.kp_cross, self.a.kp_heading
        want = -(kc * cross + kh * math.radians(head))
        ct, ht = -kc * cross, -kh * math.radians(head)
        # 진동 판정에 데드밴드를 둔다. cross 가 ±0.000 근처에서 떠는 것은
        # 수렴한 상태이지 진동이 아니다 — 그걸 세면 정상 주행에도 경고가 뜬다.
        self._cross_hist.append(cross)
        del self._cross_hist[:-6]
        big = [c for c in self._cross_hist if abs(c) >= FLIP_DEADBAND_M]
        flips = sum(1 for a, b in zip(big, big[1:]) if a * b < 0)
        note = []
        if w.get('saturated'):
            note.append('SAT(요청 곡률 미출력)')
        # 데드밴드(1 cm)를 넘는 값끼리 2회 이상 부호가 뒤집혔으면 중심선을
        # 두 번 가로지른 것이다 — 수렴이 아니라 진동이다.
        if flips >= 2:
            note.append(f'cross 부호 {flips}회 반전 — 진동 (heading 항 부족)')
        # clamp 에 걸린 값은 제어법과 다를 수밖에 없다 — 게인 탓이 아니다.
        clamped = sd in (-2869, 1955)
        if not clamped and abs(want - sd) > 5:
            note.append(f'기대와 {want - sd:+.0f}cd 차이 — 게인 확인')
        if clamped:
            note.append(f'제어 출력이 한계({sd})에 붙었다 — 보정 여력 없음')
        print(f'{"":>10} 횡보정 ON  cross={cross:>+6.3f} m  '
              f'head={head:>+6.1f}°  str={sd:>+6} cdeg  '
              f'기대={want:>+7.0f} (cross항 {ct:>+6.0f} / head항 {ht:>+6.0f})',
              flush=True)
        if note:
            print(f'{"":>10}   ⚠ ' + ' · '.join(note), flush=True)

    def show(self):
        st = self.state
        self.n += 1
        if st is None:
            if self.n % 10 == 1:
                print('  /route/state 대기 중 — route_runner 가 떠 있는지 확인',
                      flush=True)
            return
        c = st['command']
        sp, sd = c['speed_mm_s'], c['steering_cdeg']
        head = (f"[{st['state']:<8}] {st['step_index'] + 1}/{st['step_total']} "
                f"{st['step_name']:<18}")
        cmd = (f"CMD_DRIVE  speed={sp:>+5} mm/s  steering={sd:>+6} cdeg "
               f"({sd / 100.0:>+6.2f}°)  enable={int(c['enable'])}")
        # 전선 실제값을 명령 옆에 붙인다 — 트림이 걸려 있으면 위의 steering
        # 과 다르고, 그 차이가 곧 실제 곡률의 차이다.
        w = self.wire
        if w and w.get('trim_cdeg'):
            cmd += (f"  ->전선 {w['steering_cdeg']:>+6} cdeg "
                    f"({w['steering_deg']:>+6.2f}°)"
                    f"{'  SAT' if w.get('saturated') else ''}")
        gauge = bar(sd, -2869, 1955)
        extra = (f"주행 {st['traveled_m']:>5.2f} m  회전 {st['turned_deg']:>+7.1f}°  "
                 f"목표태그 {st['target_tag_id']}  "
                 f"stm32={st.get('stm32_state', '?')}")
        print(f'{head} {cmd}', flush=True)
        print(f'{"":>10} 조향 [{gauge}]  {extra}', flush=True)
        if st.get('fault_reason'):
            print(f'{"":>10} ⚠ {st["fault_reason"]}', flush=True)
        if st.get('stm32_faults'):
            print(f'{"":>10} fault: {st["stm32_faults"]}', flush=True)

        if self.a.servo:
            self.show_servo(st)

        if self.a.tags:
            now = time.monotonic()
            fresh = {i: t for i, (t, ts) in self.tags.items()
                     if now - ts < 1.0}
            if fresh:
                for i in sorted(fresh):
                    t = fresh[i]
                    print(f'{"":>10} 태그 {i}: along={t["along"]:>5.2f} m  '
                          f'횡오차={t["cross_track"]:>+6.3f} m  '
                          f'접근각={t["heading_error"] * 57.3:>+6.1f}°  '
                          f'{t["pixel_width"]:>5.0f} px', flush=True)
            else:
                print(f'{"":>10} 태그 검출 없음', flush=True)
            if self.diag and self.diag.get('rejected'):
                print(f'{"":>10} 거부: {self.diag["rejected"]}', flush=True)
        print(flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--tags', action='store_true', help='태그 검출도 표시')
    ap.add_argument('--servo', action='store_true',
                    help='횡보정 검증 (cross·조향·기대값·진동/포화 경고)')
    ap.add_argument('--kp-cross', type=float, default=KP_CROSS,
                    help='기대값 재계산용. 런치에서 바꿨으면 맞춰라')
    ap.add_argument('--kp-heading', type=float, default=KP_HEADING)
    ap.add_argument('--rate', type=float, default=2.0, help='출력 주기 [Hz]')
    a = ap.parse_args(argv)
    rclpy.init()
    n = Monitor(a)
    print('읽기 전용 모니터. Ctrl-C 로 종료.\n')
    try:
        rclpy.spin(n)
    except KeyboardInterrupt:
        pass
    finally:
        n.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(main())
