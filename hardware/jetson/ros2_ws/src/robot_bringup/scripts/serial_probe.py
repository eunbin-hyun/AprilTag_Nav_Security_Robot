#!/usr/bin/env python3
"""시리얼 포트에 붙은 게 라이다인지 STM32 인지 통신으로 판별한다.

라이다 어댑터와 STM32 어댑터가 둘 다 CP2102 `10c4:ea60` 이고 시리얼도 `0001`
로 같아서, USB 식별 정보로는 구분이 불가능하다. `/dev/ydlidar` 심볼릭 링크도
SDK udev 규칙이 10c4:ea60 아무거나 잡아버리므로 근거가 못 된다. 그래서 실제로
포트를 열어서 트래픽 패턴으로 판별한다.

    ./serial_probe.py                    # 모든 후보 포트 검사
    ./serial_probe.py /dev/ttyUSB0       # 특정 포트만
    ./serial_probe.py --seconds 5        # 청취 시간 지정

판별 기준:
    라이다            128000 bps 에서 초당 약 11 KB 연속 스트리밍
                      (선로 속도의 약 90%. YDLidar 는 항상 쏜다)
    STM32 (UART5 활성) V3 프레임(AA 55 02)이 CRC 통과하며 20 Hz 로 들어옴
    침묵              STM32 어댑터인데 펌웨어가 UART5 로 안 보내는 상태
                      (VEHICLE_COMM_USE_STLINK_VCP=1U) 또는 배선 문제

⚠ 이 스크립트는 **읽기만** 한다. CMD_DRIVE 를 절대 보내지 않으므로 차량이
  움직일 위험이 없다. 진단용으로 안전하게 반복 실행할 수 있다.

⚠ 포트는 한 프로세스만 열 수 있다. stm32_bridge 나 라이다 드라이버가 돌고
  있으면 "Device or resource busy" 가 난다. 먼저 끄고 실행하라.
"""
import argparse
import glob
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                '..', '..', 'stm32_bridge'))

try:
    import serial
except ImportError:
    raise SystemExit('pyserial 이 필요하다: python3 -m pip install pyserial')

try:
    from stm32_bridge.parser import FrameParser
except ImportError:
    FrameParser = None            # 파서 없이도 트래픽량만으로 판별 가능

# 라이다는 선로 속도의 90% 가까이 쓴다. STM32 는 약 1.8 KB/s.
LIDAR_MIN_RATE = 5000.0           # B/s. 이 이상이면 연속 스트리밍 장치
BAUDS = (115200, 128000)


def probe(port: str, seconds: float) -> dict:
    result = {'port': port, 'error': None, 'runs': []}
    for baud in BAUDS:
        try:
            ser = serial.Serial(port, baud, timeout=0)
        except Exception as e:                            # noqa: BLE001
            result['error'] = str(e)
            return result
        par = FrameParser() if FrameParser else None
        total = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < seconds:
            d = ser.read(4096)
            if d:
                total += len(d)
                if par:
                    par.feed(d)
            if par:
                par.check_timeout()
            time.sleep(0.005)
        ser.close()
        result['runs'].append({
            'baud': baud,
            'bytes': total,
            'rate': total / seconds,
            'frames': par.stats.valid_frames if par else None,
            'crc_errors': par.stats.crc_errors if par else None,
        })
    return result


def verdict(result: dict) -> str:
    if result['error']:
        return f"열 수 없음 — {result['error']}"

    frames = max((r['frames'] or 0) for r in result['runs'])
    if frames > 0:
        return ('★ STM32 (UART5 활성) — V3 프레임 수신됨. '
                'stm32_bridge 를 붙일 수 있다')

    hi = max(result['runs'], key=lambda r: r['rate'])
    if hi['rate'] >= LIDAR_MIN_RATE:
        return (f"★ 라이다 — {hi['baud']} bps 에서 {hi['rate']:.0f} B/s 연속 "
                '스트리밍. YDLidar 패턴')

    if hi['rate'] > 0:
        return (f"판별 불가 — {hi['rate']:.0f} B/s 로 트래픽은 있으나 V3 프레임이 "
                '없다. baud 불일치나 TX/RX 교차 확인')

    # 침묵의 의미가 포트 종류에 따라 다르다. ttyTHS 는 Jetson 온보드 UART 라서
    # 배선하지 않은 상태가 기본이고, 조용한 것이 정상이다. USB 어댑터가
    # 조용한 것은 상대가 무송신이라는 뜻이다 — 섞으면 오진한다.
    if 'ttyTHS' in result['port']:
        return ('침묵 — Jetson 온보드 UART. 40핀 헤더에 아직 배선하지 않았거나 '
                '상대가 무송신. 배선 확인은 jetson_pin_check.py --mode uart')
    return ('침묵 — CP2102 어댑터가 붙어 있으나 상대가 무송신. STM32 펌웨어가 '
            'UART5 로 보내지 않는 상태(VEHICLE_COMM_USE_STLINK_VCP=1U)이거나 '
            'TX/RX·GND 배선 미완')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('ports', nargs='*',
                    help='검사할 포트. 생략하면 ttyUSB*·ttyTHS* 자동 탐색')
    ap.add_argument('--seconds', type=float, default=3.0,
                    help='baud 당 청취 시간 (기본 3.0)')
    args = ap.parse_args(argv)

    ports = args.ports or sorted(glob.glob('/dev/ttyUSB*')
                                 + glob.glob('/dev/ttyTHS*'))
    if not ports:
        print('검사할 시리얼 포트가 없다. usb_map.sh 로 연결 상태를 확인하라.')
        return 1

    print(f'읽기 전용 진단 — CMD_DRIVE 는 보내지 않는다 '
          f'(포트당 {args.seconds * len(BAUDS):.0f}초 소요)\n')
    for port in ports:
        r = probe(port, args.seconds)
        print(f'───────── {port}')
        for run in r['runs']:
            extra = ''
            if run['frames'] is not None:
                extra = (f"  V3프레임={run['frames']}"
                         f" CRC오류={run['crc_errors']}")
            print(f"   {run['baud']:>7} bps : {run['bytes']:>7} B"
                  f"  = {run['rate']:>8.0f} B/s{extra}")
        print(f'   → {verdict(r)}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
