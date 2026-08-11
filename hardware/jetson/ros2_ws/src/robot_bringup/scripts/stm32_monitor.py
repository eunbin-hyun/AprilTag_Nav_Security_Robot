#!/usr/bin/env python3
"""STM32 텔레메트리 실시간 요약 모니터 (읽기 전용).

`stm32_link_check.py --listen` 은 프레임을 전부 한 줄씩 찍어서 초당 50줄이
나온다. 상태를 눈으로 쫓기에는 안 맞아서, 이 도구는 **집계해서** 보여준다.
주기·상태·fault 이름·오도메트리 유효성·초음파를 한 화면에 모은다.

    ./stm32_monitor.py                        # /dev/ttyUSB0, 무한
    ./stm32_monitor.py --port /dev/ttyTHS1    # 온보드 UART
    ./stm32_monitor.py --seconds 10           # 10초만
    ./stm32_monitor.py --watch-obstacle       # OBSTACLE_NEAR 변화만 크게 알림

⚠ **읽기 전용이다.** CMD_DRIVE·CMD_STOP 을 절대 보내지 않으므로 차량이 움직일
  위험이 없다. 다만 heartbeat 를 보내지 않으므로 STM32 는 SAFE_STOP 에 머문다 —
  그게 정상이다 (명세 §12.4).

⚠ 포트는 한 프로세스만 열 수 있다. stm32_bridge 가 돌고 있으면
  "Device or resource busy" 가 난다. 먼저 끄고 실행하라.
"""
import argparse
import collections
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                '..', '..', 'stm32_bridge'))

try:
    import serial
except ImportError:
    raise SystemExit('pyserial 이 필요하다: python3 -m pip install pyserial')

from stm32_bridge import protocol as P                          # noqa: E402
from stm32_bridge.parser import FrameParser                     # noqa: E402

NAMES = {
    P.TELEMETRY_DRIVE: 'DRIVE',
    P.FAULT_EVENT: 'FAULT_EVENT',
    P.COMMAND_RESULT: 'CMD_RESULT',
    P.TELEMETRY_IMU: 'IMU',
    P.TELEMETRY_RANGE: 'RANGE',
    P.TELEMETRY_ODOMETRY: 'ODOMETRY',
}
# 설계 주기 (명세 §7). 실측이 이것과 다르면 링크나 펌웨어 문제다.
EXPECTED_HZ = {P.TELEMETRY_DRIVE: 20.0, P.TELEMETRY_RANGE: 10.0,
               P.TELEMETRY_ODOMETRY: 20.0}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/dev/ttyUSB0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--seconds', type=float, default=None,
                    help='총 실행 시간. 생략하면 Ctrl-C 까지')
    ap.add_argument('--interval', type=float, default=1.0,
                    help='요약 출력 주기 (기본 1.0초)')
    ap.add_argument('--watch-obstacle', action='store_true',
                    help='OBSTACLE_NEAR(bit 16) 변화를 크게 알린다')
    args = ap.parse_args(argv)

    try:
        ser = serial.Serial(args.port, args.baud, timeout=0)
    except Exception as e:                                      # noqa: BLE001
        raise SystemExit(f'{args.port} 를 열 수 없다: {e}\n'
                         '  - stm32_bridge 나 다른 프로세스가 점유 중인지 확인\n'
                         '  - usb_map.sh 로 포트 존재 확인')

    # 열자마자 쌓여 있던 바이트를 버린다. 그렇지 않으면 첫 read 가 수 KB 를
    # 한 번에 가져오고, 파서의 버퍼 상한(1024 B)에 걸려 대부분이 noise 로
    # 집계된다. 실제로는 손상이 아니라 낡은 프레임인데 손상처럼 보인다.
    ser.reset_input_buffer()

    par = FrameParser()
    cnt = collections.Counter()
    last = {}
    prev_faults = None
    prev_obstacle = None
    t0 = time.monotonic()
    next_print = t0 + args.interval
    window_start = t0
    window = collections.Counter()

    print(f'{args.port} @ {args.baud} 8-N-1 · 읽기 전용 '
          '(CMD_DRIVE 미송신 — SAFE_STOP 유지가 정상)\n')
    try:
        while args.seconds is None or time.monotonic() - t0 < args.seconds:
            d = ser.read(4096)
            if d:
                for mid, seq, pl in par.feed(d):
                    cnt[mid] += 1
                    window[mid] += 1
                    last[mid] = pl
            par.check_timeout()

            # fault 변화는 주기와 무관하게 즉시 알린다 — 놓치면 원인 추적이 어렵다
            if P.TELEMETRY_DRIVE in last:
                try:
                    t = P.unpack_telemetry(last[P.TELEMETRY_DRIVE])
                except (ValueError, Exception):                 # noqa: BLE001
                    t = None
                if t:
                    f = t['active_fault_bits']
                    if f != prev_faults:
                        if prev_faults is not None:
                            gained, lost = f & ~prev_faults, prev_faults & ~f
                            parts = []
                            if gained:
                                parts.append(f'+[{P.describe_faults(gained)}]')
                            if lost:
                                parts.append(f'-[{P.describe_faults(lost)}]')
                            print(f'  ⚡ fault 0x{f:08X} ' + ' '.join(parts),
                                  flush=True)
                        prev_faults = f
                    if args.watch_obstacle:
                        ob = bool(f & P.FAULT_OBSTACLE_NEAR)
                        if ob != prev_obstacle:
                            prev_obstacle = ob
                            mark = ('████ 장애물 감지 (OBSTACLE_NEAR) ████'
                                    if ob else '──── 장애물 해제 ────')
                            print(f'\n  {mark}\n', flush=True)

            now = time.monotonic()
            if now >= next_print:
                elapsed = now - window_start
                print(f'──── t={now - t0:6.1f}s ' + '─' * 46)
                for mid in sorted(window):
                    hz = window[mid] / elapsed
                    exp = EXPECTED_HZ.get(mid)
                    flag = ''
                    if exp:
                        flag = ' ✓' if abs(hz - exp) / exp < 0.2 else ' ← 설계와 다름'
                    print(f'   0x{mid:02X} {NAMES.get(mid, "?"):<11}'
                          f'{window[mid]:>4}개 {hz:>5.1f} Hz'
                          f'{f" (설계 {exp:.0f})" if exp else ""}{flag}')
                if not window:
                    print('   수신 없음 — 배선·baud·펌웨어 송신 여부 확인')

                if P.TELEMETRY_DRIVE in last:
                    t = P.unpack_telemetry(last[P.TELEMETRY_DRIVE])
                    blk = P.arm_blocking_faults(t['active_fault_bits'])
                    print(f"   상태 {P.STATE_NAMES.get(t['drive_state'], '?'):<10}"
                          f" speed {t['target_speed_mm_s']:>5}/"
                          f"{t['measured_speed_mm_s']:<5} mm/s"
                          f"  steer {t['steering_cmd_cdeg']:>6} cdeg"
                          f"  enc {t['encoder_count']}")
                    print(f"   fault 0x{t['active_fault_bits']:08X}"
                          f"  ARM차단 {'없음' if not blk else P.describe_faults(blk)}")

                if P.TELEMETRY_ODOMETRY in last:
                    o = P.unpack_telemetry_odometry(last[P.TELEMETRY_ODOMETRY])
                    pv = P.odom_pose_valid(o['status_flags'],
                                           o['steering_source'])
                    print(f"   odom x={o['x_mm']:>6} y={o['y_mm']:>6} mm"
                          f"  yaw={o['yaw_mdeg'] / 1000:>8.3f}°"
                          f"  dist={o['distance_mm']:>6} mm"
                          f"  pose_valid={pv}")

                if P.TELEMETRY_RANGE in last:
                    r = P.unpack_telemetry_range(last[P.TELEMETRY_RANGE])
                    vals = ['—' if v is None else f'{v}mm'
                            for v in r['ranges_mm']]
                    print(f"   range mask=0x{r['valid_mask']:02X}  "
                          f"FL={vals[0]} FR={vals[1]} "
                          f"RL={vals[2]} RR={vals[3]}"
                          f"{'   (전부 무효 = V3 채널 미매핑, 정상)' if not r['valid_mask'] else ''}")
                print(flush=True)
                window.clear()
                window_start = now
                next_print = now + args.interval
            time.sleep(0.003)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()

    dur = time.monotonic() - t0
    print(f'\n═══ 총 {dur:.1f}초 ═══')
    for mid in sorted(cnt):
        print(f'   0x{mid:02X} {NAMES.get(mid, "?"):<11}{cnt[mid]:>5}개'
              f' {cnt[mid] / dur:>5.1f} Hz')
    print(f'   파서 valid={par.stats.valid_frames} crc_err={par.stats.crc_errors}'
          f' noise={par.stats.noise_bytes} schema_err={par.stats.schema_errors}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
