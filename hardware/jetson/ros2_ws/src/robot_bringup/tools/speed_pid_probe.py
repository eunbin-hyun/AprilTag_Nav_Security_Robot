#!/usr/bin/env python3
r"""STM32 속도 PID 실차 응답 기록 도구 (STM32 팀 요청 2026-08-06).

조향을 **실제 주행이 쓰는 직진값으로 고정**한 채 속도만 계단으로 바꾸며,
`TELEMETRY_DRIVE`(0x80) 를 받을 때마다 CSV 에 한 행씩 적는다. 그래프는 그리지
않는다 — CSV 를 받아 STM32 쪽에서 그린다.

  # 기본 프로파일 (0→100→200→300→200→100→0, 각 3 s, 조향 500 cdeg)
  python3 speed_pid_probe.py --port /dev/ttyUSB0

  # 속도·유지시간·조향·출력경로를 바꾼다
  python3 speed_pid_probe.py --port /dev/ttyUSB0 \
      --speeds 0,150,300,150,0 --step-s 5 --steer 500 \
      --out ~/drive_test_logs/pid_$(date +%H%M).csv

실행 전 워크스페이스 source 필요 (stm32_bridge 를 import 한다):

  source ~/ros_ws/install/setup.bash

⚠ **바닥 주행이다.** 프로파일 전체 거리는 대략 Σ(속도×시간) 이다 — 기본값이면
  0.3+0.6+0.9+0.6+0.3 = 약 2.7 m 다. 앞을 비우고 한 명이 따라붙을 것.
  바퀴를 띄우고 돌리면 부하가 없어 PID 응답이 실차와 다르게 나온다.

⚠ **UART 를 직접 소유한다.** `route_runner` 와 동시에 띄우면 포트 충돌이다.

⚠ **조향 기본값 500 cdeg 의 출처** — `route_runner` 는 조향 명령에
  `steer_trim_cdeg` 를 더해서 내보내고(`route_runner.py` 의 트림 합산),
  그 값의 실운영 기본값이 `tag_drive.launch.py` 의 `steer_trim_cdeg=500` 이다.
  즉 주행 중 "직진" 은 전선에 500 cdeg 로 실린다 (2026-08-04 실측으로 정한 값).
  ※ `route_runner` 의 노드 기본값은 0 이지만 그건 launch 없이 띄웠을 때 얘기다.
  이 도구는 `drive_probe.py` 와 마찬가지로 트림을 자동 적용하지 않는다 —
  `--steer` 로 받은 값을 **그대로** 전선에 싣는다.

Ctrl-C 는 어느 시점에든 안전하다. 속도 0 (neutral) 을 쏜 뒤 CSV 를 닫는다.
"""
import argparse
import csv
import os
import sys
import time

try:
    import serial
except ImportError:
    sys.exit('pyserial 이 없습니다: pip3 install pyserial')

try:
    from stm32_bridge import protocol as P
    from stm32_bridge.obstacle_gate import ObstacleGate
except ImportError:
    sys.exit('stm32_bridge 를 찾을 수 없습니다. 워크스페이스를 source 하십시오:\n'
             '  source ~/ros_ws/install/setup.bash')

# UART 연결·ARM·송신·수신 구조는 drive_probe 를 그대로 쓴다. 같은 디렉터리에
# 있으므로 스크립트를 어디서 실행하든 import 된다 (스크립트 위치가 sys.path).
try:
    from drive_probe import (HARD_SPEED_LIMIT, Probe, STEER_MAX, STEER_MIN,
                             TICK)
except ImportError:
    sys.exit('drive_probe.py 를 찾을 수 없습니다. 같은 tools/ 디렉터리에서 '
             '실행하십시오.')

DEFAULT_SPEEDS = '0,100,200,300,200,100,0'
DEFAULT_STEER_CDEG = 500        # 실운영 직진값 (launch steer_trim_cdeg)

# STM32 팀이 요청한 칸. 순서를 바꾸지 말 것 — 저쪽 그래프 스크립트가 이 순서를
# 전제한다.
CSV_FIELDS = [
    'elapsed_s',
    'commanded_speed_mm_s',
    'measured_speed_mm_s',
    'motor_duty_permille',
    'encoder_count',
    'commanded_steering_cdeg',
    'drive_state',
    'active_fault_bits',
]


class DriveTap:
    """FrameParser 를 감싸 0x80 을 **한 장도 빠뜨리지 않고** 넘겨준다.

    `Probe.pump_rx` 는 최신 프레임만 `self.t` 에 남긴다. 한 번의 read 에 0x80
    이 두 장 들어오면 앞의 것이 덮여 사라지므로, 그것만 보고 CSV 를 적으면
    표본이 조용히 빠진다. PID 응답은 과도구간의 표본 밀도가 전부라 그 손실이
    그대로 분석 오차가 된다. 그래서 파서 출구에서 가로챈다.

    `Probe` 를 고치지 않고 `parser` 속성만 바꿔 끼우는 이유: `drive_probe` 는
    실차 시험에서 이미 검증된 코드라 손대지 않는 편이 안전하다.
    """

    def __init__(self, inner, on_drive):
        self._inner = inner
        self._on_drive = on_drive

    def feed(self, data, now=None):
        """원본 feed 결과를 그대로 돌려주되 0x80 은 콜백으로도 흘린다."""
        frames = self._inner.feed(data, now)
        for msg_id, _seq, pl in frames:
            if msg_id == P.TELEMETRY_DRIVE:
                self._on_drive(P.unpack_telemetry(pl))
        return frames

    def check_timeout(self, now=None):
        """불완전 프레임 폐기 타이머 — 원본에 그대로 위임한다."""
        return self._inner.check_timeout(now)

    @property
    def stats(self):
        """파서 진단 카운터 (원본 것)."""
        return self._inner.stats


class CsvSink:
    """0x80 한 장 = CSV 한 행. 매 행 flush 한다.

    flush 를 미루지 않는 이유: 이 도구의 산출물은 CSV 하나뿐이고, 실차에서는
    Ctrl-C 보다 거친 방법(전원 차단·프로세스 kill)으로 끝나는 일이 흔하다.
    20~50 Hz 에서 flush 비용은 무시할 수 있다.
    """

    def __init__(self, path):
        self.path = path
        self.rows = 0
        self._f = open(path, 'w', newline='')
        self._w = csv.writer(self._f)
        self._w.writerow(CSV_FIELDS)
        self._f.flush()

    def write(self, elapsed_s, cmd_speed, cmd_steer, tel):
        """텔레메트리 한 장을 요청받은 칸만 골라 적는다."""
        self._w.writerow([
            f'{elapsed_s:.3f}',
            cmd_speed,
            tel['measured_speed_mm_s'],
            tel['motor_duty_permille'],
            tel['encoder_count'],
            cmd_steer,
            tel['drive_state'],
            tel['active_fault_bits'],
        ])
        self._f.flush()
        self.rows += 1

    def close(self):
        """두 번 불러도 안전하다 (finally 경로가 겹친다)."""
        if self._f is not None:
            self._f.close()
            self._f = None


def run_profile(pr, speeds, step_s, steer, sink):
    """속도 계단을 순서대로 밟으며 매 0x80 을 CSV 에 적는다.

    `drive_probe.run_cmd` 와 같은 뼈대다. 다른 점은 두 가지다.

    1. 계단 사이에 neutral 을 끼우지 않는다. 100 → 200 을 **끊김 없이** 밟아야
       계단 응답(상승시간·오버슈트·정착)이 나온다. 중간에 0 을 넣고 싶으면
       `--speeds` 에 0 을 적으면 된다.
    2. 속도 0 구간도 `enable=1` 로 보낸다. `enable=0`(neutral) 은 PID 를 끄는
       것이라 감속 응답이 안 남는다. 우리가 보려는 것은 "목표 0 을 향해 PID 가
       어떻게 줄이는가" 이므로 0 도 하나의 목표값으로 취급한다.

    초음파 장애물은 `ObstacleGate` 정책대로 neutral 로 바꿔 보내며 기다린다.
    게이트를 안 걸면 SAFE_STOP 에 빠진 뒤 남은 계단이 전부 **거부되어** 평평한
    CSV 가 나온다 — 그 편이 훨씬 나쁘다. HOLD 로 보낸 시간은 계단 시간에서
    제외하므로 각 계단은 실제 주행 시간을 온전히 채운다. 다만 CSV 의
    `elapsed_s` 는 **벽시계 그대로**다 (시간축을 조작하지 않는다). HOLD 구간은
    `commanded_speed_mm_s=0` 으로 남으니 나중에 골라낼 수 있다.
    """
    gate = ObstacleGate()
    t0 = time.monotonic()
    aborted = None
    hold_logged = None

    for idx, target in enumerate(speeds, 1):
        step_t0 = time.monotonic()
        next_tx = step_t0
        last_draw = 0.0
        rows0 = sink.rows
        while True:
            now = time.monotonic()
            active = (now - step_t0) - gate.hold_elapsed_s(now)
            if active >= step_s:
                break

            # pump_rx 안에서 DriveTap 이 CSV 행을 쓴다. 지금 무엇을 명령하고
            # 있는지를 그쪽에 알려야 하므로 먼저 갱신한다.
            pr.pump_rx()

            age = (now - pr.t_at) if pr.t_at is not None else 999.0
            may_drive = gate.update(now, pr.t, age)
            if gate.aborted:
                aborted = gate.abort_reason
                break

            if now >= next_tx:
                if may_drive:
                    pr.send(target, steer, True)
                else:
                    pr.send(0, 0, False)
                next_tx += TICK
                if next_tx <= now:
                    next_tx = now + TICK

            if gate.substate != hold_logged:
                hold_logged = gate.substate
                if gate.holding:
                    print()
                    print(f'  ⏸ 장애물 {gate.substate} — neutral 유지')
                elif gate.hold_count:
                    print(f'  ▶ 재개 (HOLD {gate.hold_elapsed_s(now):.1f}s '
                          '제외됨)')

            if pr.t and now - last_draw > 0.2:
                last_draw = now
                fb = pr.t['active_fault_bits']
                st = P.STATE_NAMES.get(pr.t['drive_state'], '?')
                print(f'\r  [{idx}/{len(speeds)}] target={target:>4} '
                      f"measured={pr.t['measured_speed_mm_s']:>5} "
                      f"duty={pr.t['motor_duty_permille']:>5}‰ "
                      f'state={st} rows={sink.rows}'
                      + ('' if may_drive else f' [{gate.substate}]')
                      + (f' fault=0x{fb:08X}' if fb else '')
                      + '   ', end='', flush=True)
            time.sleep(0.002)

        print(f'\r  [{idx}/{len(speeds)}] target={target:>4} 완료 — '
              f'표본 {sink.rows - rows0} 행'.ljust(78))
        if aborted:
            break

    total = time.monotonic() - t0
    return {'aborted': aborted, 'elapsed_s': total,
            'hold_count': gate.hold_count,
            'hold_s': round(gate.hold_elapsed_s(time.monotonic()), 2)}


def parse_speeds(text, cap):
    """'0,100,200' → [0, 100, 200]. 상한을 넘으면 그 자리에서 종료한다."""
    try:
        speeds = [int(s) for s in text.split(',') if s.strip() != '']
    except ValueError:
        sys.exit(f'--speeds 형식이 잘못됐습니다: {text!r} (예: 0,100,200,0)')
    if not speeds:
        sys.exit('--speeds 가 비었습니다')
    bad = [s for s in speeds if not 0 <= s <= cap]
    if bad:
        sys.exit(f'속도 {bad} 가 상한 {cap} mm/s 를 넘습니다 '
                 '(--max-speed 로 올릴 수 있습니다)')
    return speeds


def default_out_path():
    """~/drive_test_logs/speed_pid_<시각>.csv — 기존 로그 자리와 같다."""
    d = os.path.expanduser('~/drive_test_logs')
    stamp = time.strftime('%Y%m%d_%H%M%S')
    return os.path.join(d, f'speed_pid_{stamp}.csv')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', required=True)
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--speeds', default=DEFAULT_SPEEDS,
                    help=f'속도 계단 [mm/s], 쉼표 구분 (기본 {DEFAULT_SPEEDS})')
    ap.add_argument('--step-s', type=float, default=3.0,
                    help='각 계단 유지 시간 [s] (기본 3.0)')
    ap.add_argument('--steer', type=int, default=DEFAULT_STEER_CDEG,
                    help=f'고정 조향 [cdeg] (기본 {DEFAULT_STEER_CDEG} = '
                         '실운영 직진값). 전선에 그대로 실린다')
    ap.add_argument('--out', default=None,
                    help='CSV 경로 (기본 ~/drive_test_logs/speed_pid_<시각>.csv)')
    ap.add_argument('--max-speed', type=int, default=313,
                    help='속도 상한 [mm/s]. 기본 313 = 최대출력 1565 의 20%%')
    ap.add_argument('--yes', action='store_true', help='시작 확인 생략')
    a = ap.parse_args()

    cap = min(a.max_speed, HARD_SPEED_LIMIT)
    speeds = parse_speeds(a.speeds, cap)
    if not STEER_MIN <= a.steer <= STEER_MAX:
        sys.exit(f'--steer 는 {STEER_MIN}..{STEER_MAX} cdeg 범위여야 합니다 '
                 '(비대칭 실측 한계)')

    out = a.out or default_out_path()
    out = os.path.expanduser(out)
    parent = os.path.dirname(os.path.abspath(out))
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # 대략적인 주행 거리를 미리 알려 준다 — 공간 확보 판단에 쓴다.
    rough_m = sum(speeds) * a.step_s / 1000.0
    print(f'속도 계단 : {speeds} mm/s (각 {a.step_s}s)')
    print(f'고정 조향 : {a.steer} cdeg  (트림 미적용 — 전선에 그대로)')
    print(f'CSV       : {out}')
    print(f'⚠ 바닥 주행입니다. 예상 이동거리 약 {rough_m:.1f} m — 앞을 비우고 '
          '한 명이 따라붙으십시오.')
    if not a.yes:
        input('Enter 를 누르면 시작합니다 (Ctrl-C 취소): ')

    try:
        pr = Probe(a.port, a.baud, ticks_per_m=4093.8)
    except serial.SerialException as e:
        sys.exit(f'포트를 열 수 없습니다: {e}')

    sink = None
    rc = 0
    try:
        if not pr.arm():
            return 1
        sink = CsvSink(out)
        # ARM 이 끝난 뒤에 tap 을 끼운다 — arming 중 neutral 응답까지 CSV 에
        # 들어가면 첫 계단의 t=0 이 흐려진다.
        state = {'speed': 0, 'steer': a.steer, 't0': time.monotonic()}

        def on_drive(tel):
            sink.write(time.monotonic() - state['t0'],
                       state['speed'], state['steer'], tel)

        pr.parser = DriveTap(pr.parser, on_drive)

        # run_profile 이 pr.send 로 내보내는 값을 그대로 기록해야 하므로
        # send 를 감싸 마지막 명령을 state 에 남긴다.
        raw_send = pr.send

        def send(speed, steer, enable):
            state['speed'] = int(speed)
            state['steer'] = int(steer)
            raw_send(speed, steer, enable)

        pr.send = send

        print(f'\n=== 속도 PID 응답 기록 시작 ({len(speeds)} 계단) ===')
        r = run_profile(pr, speeds, a.step_s, a.steer, sink)

        print(f"\n  총 {r['elapsed_s']:.1f}s · {sink.rows} 행 기록")
        if r['hold_count']:
            print(f"  장애물 HOLD {r['hold_count']}회 {r['hold_s']}s "
                  '(계단 시간에서 제외 · CSV 에는 commanded_speed=0 으로 남음)')
        if r['aborted']:
            print(f"  ⚠ 중단: {r['aborted']}")
            print('     남은 계단은 실행하지 않았습니다 — 원인 해소 후 재실행')
            rc = 1
        if sink.rows == 0:
            print('  ⚠ 0x80 을 한 장도 못 받았습니다 — 포트·전원·배선 확인')
            rc = 1
    except KeyboardInterrupt:
        print('\n  중단 — 속도 0 송신 후 CSV 를 닫습니다')
    finally:
        try:
            pr.neutral_burst()
        except Exception:                                  # noqa: BLE001
            pass
        try:
            pr.ser.close()
        except Exception:                                  # noqa: BLE001
            pass
        if sink is not None:
            sink.close()
            print(f'  CSV 저장됨: {sink.path} ({sink.rows} 행)')
    return rc


if __name__ == '__main__':
    sys.exit(main())
