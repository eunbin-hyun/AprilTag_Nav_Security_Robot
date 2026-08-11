#!/usr/bin/env python3
"""터치센서 단독 확인: OUT -> 물리 29번(BCM GPIO5)."""

import signal
import time

import RPi.GPIO as GPIO  # Raspberry Pi 5에서는 rpi-lgpio가 이 이름을 제공한다


TOUCH_PIN = 29          # GPIO.BOARD 기준 물리 29번 = BCM GPIO5
HOLD_SECONDS = 3.0
POLL_SECONDS = 0.02


def main():
    running = True
    pressed_at = None
    hold_reported = False

    def stop(_signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(TOUCH_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    print("터치센서 확인 시작")
    print("OUT=물리 29번(GPIO5), VCC=3.3V, GND=GND")
    print("센서를 누르고 떼어 보세요. 종료: Ctrl+C\n")

    try:
        while running:
            now = time.monotonic()
            touched = GPIO.input(TOUCH_PIN) == GPIO.HIGH

            if touched:
                if pressed_at is None:
                    pressed_at = now
                    hold_reported = False
                    print("[HIGH] 터치 시작")
                elif not hold_reported and now - pressed_at >= HOLD_SECONDS:
                    hold_reported = True
                    print(f"[HOLD] {HOLD_SECONDS:.1f}초 길게 누름 확인")
            elif pressed_at is not None:
                duration = now - pressed_at
                print(f"[LOW]  터치 해제 (누른 시간 {duration:.2f}초)")
                pressed_at = None
                hold_reported = False

            time.sleep(POLL_SECONDS)
    finally:
        GPIO.cleanup(TOUCH_PIN)
        print("\nGPIO 정리 후 종료했습니다.")


if __name__ == "__main__":
    main()
