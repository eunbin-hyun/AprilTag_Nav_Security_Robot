#!/usr/bin/env python3
"""
VL53L1X (TOF400C) ToF 거리 센서 2개 동시 테스트 - 라즈베리파이 스크립트

두 센서는 SDA/SCL/전원/GND를 공유하고, XSHUT만 따로 뺍니다 (docs/wiring.md 참고).
    센서 #1 XSHUT -> GPIO17 (물리 11번)
    센서 #2 XSHUT -> GPIO27 (물리 13번)

VL53L1X 는 부팅 시 모두 같은 기본 주소(0x29)를 씁니다.
따라서 XSHUT 로 한 개씩 깨워서 주소를 겹치지 않게 재할당합니다.
    1) 두 센서 모두 XSHUT LOW (전원 차단)
    2) 센서 #1 만 XSHUT HIGH -> 주소를 0x30 으로 변경
    3) 센서 #2 만 XSHUT HIGH -> 주소를 0x31 으로 변경

사용 라이브러리: adafruit-circuitpython-vl53l1x (+ adafruit-blinka)

예시:
    python3 ToF2_test.py                     # 기본(long 모드, 100ms)
    python3 ToF2_test.py --mode short        # 근거리 정밀 모드(~1.3m)
    python3 ToF2_test.py --budget 200 --avg 5
    python3 ToF2_test.py --csv log.csv       # CSV로 기록
"""

import argparse
import csv
import sys
import time
from collections import deque

import board
import busio
import digitalio
import adafruit_vl53l1x

MODES = {"short": 1, "long": 2}

# 센서별 XSHUT 핀과 재할당할 I2C 주소
SENSORS = [
    {"name": "#1", "xshut": board.D17, "address": 0x30},
    {"name": "#2", "xshut": board.D27, "address": 0x31},
]


def parse_args():
    p = argparse.ArgumentParser(description="VL53L1X ToF 거리 센서 2개 테스트")
    p.add_argument("--mode", choices=MODES.keys(), default="long",
                   help="short: ~1.3m, 외광에 강함 / long: ~4m (기본값: long)")
    p.add_argument("--budget", type=int, default=100,
                   help="timing budget [ms]. 20/33/50/100/200/500 중 하나. 클수록 정확·느림")
    p.add_argument("--interval", type=float, default=0.5,
                   help="측정값 폴링 주기 [s] (기본 0.05)")
    p.add_argument("--avg", type=int, default=1,
                   help="이동 평균 샘플 수 (기본 1 = 평균 없음)")
    p.add_argument("--csv", metavar="FILE", default=None,
                   help="측정값을 CSV 파일로 저장")
    return p.parse_args()


def setup_xshut():
    """모든 XSHUT 핀을 출력으로 잡고 LOW(전원 차단)로 초기화."""
    pins = []
    for s in SENSORS:
        pin = digitalio.DigitalInOut(s["xshut"])
        pin.direction = digitalio.Direction.OUTPUT
        pin.value = False  # 센서 끄기
        pins.append(pin)
    time.sleep(0.05)
    return pins


def bring_up_sensors(i2c, args, xshut_pins):
    """XSHUT 로 한 개씩 깨워 주소를 재할당하고 초기화한 센서 리스트를 반환."""
    sensors = []
    for cfg, pin in zip(SENSORS, xshut_pins):
        # 이 센서만 전원 인가
        pin.value = True
        time.sleep(0.05)  # 부팅 대기

        try:
            sensor = adafruit_vl53l1x.VL53L1X(i2c, address=0x29)
        except ValueError:
            print(f"[에러] 센서 {cfg['name']} 를 기본 주소(0x29)에서 찾지 못했습니다.")
            print("       배선과 XSHUT 연결, `i2cdetect -y 1` 결과를 확인해 주세요.")
            sys.exit(1)

        # 다음 센서와 겹치지 않게 주소 재할당
        sensor.set_address(cfg["address"])

        sensor.distance_mode = MODES[args.mode]
        sensor.timing_budget = args.budget

        model_id, module_type, mask_rev = sensor.model_info
        print(f"[센서 {cfg['name']}] addr=0x{cfg['address']:02X}  "
              f"Model ID: 0x{model_id:02X}  Module Type: 0x{module_type:02X}  "
              f"Mask Rev: 0x{mask_rev:02X}")

        sensors.append({"name": cfg["name"], "sensor": sensor})

    return sensors


def main():
    args = parse_args()

    i2c = busio.I2C(board.SCL, board.SDA)
    xshut_pins = setup_xshut()

    sensors = bring_up_sensors(i2c, args, xshut_pins)

    print(f"\nDistance mode : {args.mode}")
    print(f"Timing budget : {args.budget} ms")
    print("측정 시작 (Ctrl+C 로 종료)\n")

    for s in sensors:
        s["sensor"].start_ranging()

    # 센서별 이동 평균 윈도우
    windows = {s["name"]: deque(maxlen=max(1, args.avg)) for s in sensors}

    csv_file = None
    writer = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        writer = csv.writer(csv_file)
        header = ["timestamp"]
        for s in sensors:
            header += [f"{s['name']}_distance_mm", f"{s['name']}_avg_mm"]
        writer.writerow(header)

    count = 0
    t0 = time.monotonic()

    try:
        while True:
            row = [f"{time.time():.3f}"]
            parts = []
            for s in sensors:
                sensor = s["sensor"]
                name = s["name"]

                try:
                    ready = sensor.data_ready
                    distance_cm = None
                    if ready:
                        distance_cm = sensor.distance  # cm 단위, 범위 밖이면 None
                        sensor.clear_interrupt()
                except OSError as e:
                    parts.append(f"{name}: I2C 오류: {e}")
                    row += ["", ""]
                    continue

                if ready:
                    if distance_cm is None:
                        parts.append(f"{name}:   ----  mm")
                        row += ["", ""]
                    else:
                        mm = distance_cm * 10.0
                        windows[name].append(mm)
                        avg = sum(windows[name]) / len(windows[name])
                        parts.append(f"{name}: {mm:7.1f} mm (평균 {avg:7.1f})")
                        row += [f"{mm:.1f}", f"{avg:.1f}"]
                else:
                    parts.append(f"{name}:   (대기)  ")
                    row += ["", ""]

            count += 1
            rate = count / (time.monotonic() - t0)
            print("   ".join(parts) + f"   [{rate:4.1f} Hz]")

            if writer:
                writer.writerow(row)
                csv_file.flush()

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        for s in sensors:
            try:
                s["sensor"].stop_ranging()
            except Exception:
                pass
        if csv_file:
            csv_file.close()


if __name__ == "__main__":
    main()
