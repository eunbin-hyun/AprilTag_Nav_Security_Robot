#!/usr/bin/env python3
"""
VL53L1X ToF 센서 A/B 모니터링 스크립트.

게이트 통합 코드와 같은 배선/주소를 사용한다.
  - 빔 A XSHUT: GPIO17, I2C 주소 0x30
  - 빔 B XSHUT: GPIO27, I2C 주소 0x31
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

SENSORS = [
    {"name": "A", "xshut": board.D17, "address": 0x30},
    {"name": "B", "xshut": board.D27, "address": 0x31},
]


def parse_args():
    parser = argparse.ArgumentParser(description="VL53L1X 빔 A/B 센서값 실시간 모니터링")
    parser.add_argument("--threshold", type=float, default=30.0,
                        help="차단 판정 거리 [cm] (기본 30)")
    parser.add_argument("--mode", choices=MODES.keys(), default="short",
                        help="거리 모드 (기본 short)")
    parser.add_argument("--budget", type=int, default=50,
                        help="timing budget [ms] (기본 50)")
    parser.add_argument("--interval", type=float, default=0.05,
                        help="화면 갱신 주기 [s] (기본 0.05)")
    parser.add_argument("--avg", type=int, default=3,
                        help="이동 평균 샘플 수 (기본 3)")
    parser.add_argument("--csv", metavar="FILE",
                        help="측정값을 CSV 파일로 저장")
    return parser.parse_args()


def setup_xshut():
    pins = []
    for cfg in SENSORS:
        pin = digitalio.DigitalInOut(cfg["xshut"])
        pin.switch_to_output(value=False)
        pins.append(pin)
    time.sleep(0.05)
    return pins


def bring_up_sensors(i2c, args, xshut_pins):
    sensors = []
    for cfg, pin in zip(SENSORS, xshut_pins):
        pin.value = True
        time.sleep(0.05)

        try:
            sensor = adafruit_vl53l1x.VL53L1X(i2c, address=0x29)
        except ValueError:
            print(f"[에러] 빔 {cfg['name']} 센서를 기본 주소 0x29에서 찾지 못했습니다.")
            print("       XSHUT 배선과 `i2cdetect -y 1` 결과를 확인해 주세요.")
            sys.exit(1)

        sensor.set_address(cfg["address"])
        sensor.distance_mode = MODES[args.mode]
        sensor.timing_budget = args.budget
        sensor.start_ranging()

        model_id, module_type, mask_rev = sensor.model_info
        print(f"[빔 {cfg['name']}] addr=0x{cfg['address']:02X} "
              f"model=0x{model_id:02X} module=0x{module_type:02X} mask=0x{mask_rev:02X}")

        sensors.append({"name": cfg["name"], "sensor": sensor})
    return sensors


def read_distance(sensor):
    try:
        if sensor.data_ready:
            distance_cm = sensor.distance
            sensor.clear_interrupt()
            return distance_cm
    except OSError as exc:
        return exc
    return None


def format_distance(distance_cm, avg_cm, threshold_cm):
    if isinstance(distance_cm, OSError):
        return f"I2C 오류: {distance_cm}".ljust(30)
    if distance_cm is None:
        return "대기/범위밖".ljust(30)

    blocked = distance_cm < threshold_cm
    state = "BLOCK" if blocked else "open "
    return f"{distance_cm:6.1f} cm  avg={avg_cm:6.1f} cm  {state}"


def main():
    args = parse_args()

    i2c = busio.I2C(board.SCL, board.SDA)
    xshut_pins = setup_xshut()
    sensors = bring_up_sensors(i2c, args, xshut_pins)
    windows = {item["name"]: deque(maxlen=max(1, args.avg)) for item in sensors}
    latest = {item["name"]: (None, None) for item in sensors}

    csv_file = None
    writer = None
    if args.csv:
        csv_file = open(args.csv, "w", newline="")
        writer = csv.writer(csv_file)
        writer.writerow(["timestamp", "beam", "distance_cm", "avg_cm", "blocked"])

    print(f"\nthreshold={args.threshold:.1f} cm, mode={args.mode}, budget={args.budget} ms")
    print("Ctrl+C 로 종료\n")

    try:
        while True:
            now = time.time()
            parts = []

            for item in sensors:
                name = item["name"]
                distance_cm = read_distance(item["sensor"])

                if isinstance(distance_cm, OSError):
                    latest[name] = (distance_cm, None)
                elif distance_cm is not None:
                    windows[name].append(distance_cm)
                    avg_cm = sum(windows[name]) / len(windows[name])
                    latest[name] = (distance_cm, avg_cm)

                    if writer:
                        writer.writerow([
                            f"{now:.3f}",
                            name,
                            f"{distance_cm:.2f}",
                            f"{avg_cm:.2f}",
                            int(distance_cm < args.threshold),
                        ])
                        csv_file.flush()

                distance, avg = latest[name]
                parts.append(f"{name}: {format_distance(distance, avg or 0.0, args.threshold)}")

            print("\r" + "   ".join(parts), end="", flush=True)
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        for item in sensors:
            try:
                item["sensor"].stop_ranging()
            except Exception:
                pass
        if csv_file:
            csv_file.close()
        for pin in xshut_pins:
            try:
                pin.deinit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
