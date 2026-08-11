#!/usr/bin/env python3
"""
VL53L1X (TOF400C) ToF 거리 센서 - 라즈베리파이 테스트 스크립트

사용 라이브러리: adafruit-circuitpython-vl53l1x (+ adafruit-blinka)

예시:
    python3 vl53l1x_test.py                     # 기본(long 모드, 100ms)
    python3 vl53l1x_test.py --mode short        # 근거리 정밀 모드(~1.3m)
    python3 vl53l1x_test.py --budget 200 --avg 5
    python3 vl53l1x_test.py --csv log.csv       # CSV로 기록
"""

import argparse
import csv
import sys
import time
from collections import deque

import board
import busio
import adafruit_vl53l1x

MODES = {"short": 1, "long": 2}


def parse_args():
    p = argparse.ArgumentParser(description="VL53L1X ToF 거리 센서 테스트")
    p.add_argument("--mode", choices=MODES.keys(), default="long",
                   help="short: ~1.3m, 외광에 강함 / long: ~4m (기본값: long)")
    p.add_argument("--budget", type=int, default=100,
                   help="timing budget [ms]. 20/33/50/100/200/500 중 하나. 클수록 정확·느림")
    p.add_argument("--interval", type=float, default=0.05,
                   help="측정값 폴링 주기 [s] (기본 0.05)")
    p.add_argument("--avg", type=int, default=1,
                   help="이동 평균 샘플 수 (기본 1 = 평균 없음)")
    p.add_argument("--address", type=lambda x: int(x, 0), default=0x29,
                   help="I2C 주소 (기본 0x29)")
    p.add_argument("--csv", metavar="FILE", default=None,
                   help="측정값을 CSV 파일로 저장")
    return p.parse_args()


def main():
    args = parse_args()

    i2c = busio.I2C(board.SCL, board.SDA)
    try:
        sensor = adafruit_vl53l1x.VL53L1X(i2c, address=args.address)
    except ValueError:
        print(f"[에러] I2C 주소 {hex(args.address)} 에서 센서를 찾지 못했습니다.")
        print("       배선과 `i2cdetect -y 1` 결과를 확인해 주세요.")
        sys.exit(1)

    sensor.distance_mode = MODES[args.mode]
    sensor.timing_budget = args.budget

    model_id, module_type, mask_rev = sensor.model_info
    print(f"Model ID: 0x{model_id:02X}  Module Type: 0x{module_type:02X}  Mask Rev: 0x{mask_rev:02X}")
    print(f"Distance mode : {args.mode} ({sensor.distance_mode})")
    print(f"Timing budget : {sensor.timing_budget} ms")
    print("측정 시작 (Ctrl+C 로 종료)\n")

    sensor.start_ranging()

    window = deque(maxlen=max(1, args.avg))
    csv_file = None
    writer = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["timestamp", "distance_mm", "avg_mm"])

    count = 0
    t0 = time.monotonic()

    try:
        while True:
            if sensor.data_ready:
                distance_cm = sensor.distance  # cm 단위, 범위 밖이면 None
                sensor.clear_interrupt()

                if distance_cm is None:
                    print("측정 실패 (범위 밖 / 반사광 부족)")
                else:
                    mm = distance_cm * 10.0
                    window.append(mm)
                    avg = sum(window) / len(window)
                    count += 1
                    rate = count / (time.monotonic() - t0)
                    print(f"{mm:8.1f} mm   (평균 {avg:7.1f} mm, {rate:4.1f} Hz)")

                    if writer:
                        writer.writerow([f"{time.time():.3f}", f"{mm:.1f}", f"{avg:.1f}"])
                        csv_file.flush()

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        try:
            sensor.stop_ranging()
        except Exception:
            pass
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()