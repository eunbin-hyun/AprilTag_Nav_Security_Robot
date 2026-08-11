#!/usr/bin/env bash
# Logitech Brio 100 을 AprilTag 검출에 맞게 설정한다.
#
# ⚠ v4l2 컨트롤은 장치 인스턴스에 붙어 있어서 USB 재연결·재부팅 시 기본값으로
#   돌아간다. 즉 "한 번 설정해두면 된다" 가 아니다. 카메라 노드를 띄우기 전에
#   매번 실행해야 한다 (launch 에서 ExecuteProcess 로 호출할 예정).
#
# 사용:
#   ./camera_setup.sh                    # 기본 노출 150
#   ./camera_setup.sh 250                # 노출 지정
#   ./camera_setup.sh 150 /dev/robotcam  # 장치 지정
#
# 야외에서는 노출을 다시 잡아야 한다. 실내 측정 기준값(2026-07-31, 형광등):
#   exposure=40 -> 평균밝기 80/255,  150 -> 152/255,  400 -> 212/255
# 태그 검출은 흰 부분이 포화되지 않는 선에서 가장 짧은 노출이 유리하다
# (노출이 길면 주행 중 모션블러로 코너가 뭉개진다).

set -euo pipefail

EXPOSURE="${1:-150}"
DEV="${2:-/dev/video0}"
WIDTH=1280
HEIGHT=720
FPS=30

if [ ! -e "$DEV" ]; then
  echo "오류: $DEV 가 없다. 카메라 연결과 udev 규칙을 확인하라." >&2
  exit 1
fi

# YUYV 는 1280x720 에서 5 fps 밖에 안 나온다 (USB 2.0 대역폭). MJPG 필수.
v4l2-ctl -d "$DEV" \
  --set-fmt-video=width=$WIDTH,height=$HEIGHT,pixelformat=MJPG \
  --set-parm=$FPS >/dev/null

# auto_exposure: 1=Manual, 3=Aperture Priority(기본). Manual 로 바꿔야
# exposure_time_absolute 의 inactive 플래그가 풀린다 -- 순서가 중요하다.
v4l2-ctl -d "$DEV" -c auto_exposure=1
v4l2-ctl -d "$DEV" -c exposure_time_absolute="$EXPOSURE"

# 자동 보정들을 끈다. 켜두면 프레임마다 밝기·색이 흔들려서 태그 검출률과
# pose 가 같은 장면에서도 달라진다 -- 재현 불가능한 시험이 된다.
v4l2-ctl -d "$DEV" -c exposure_dynamic_framerate=0   # 어두우면 fps 를 스스로 낮춘다
v4l2-ctl -d "$DEV" -c backlight_compensation=0       # 역광 보정이 태그를 흐린다
v4l2-ctl -d "$DEV" -c white_balance_automatic=0      # 그레이스케일 변환 일관성

echo "=== $DEV 설정 완료 ==="
v4l2-ctl -d "$DEV" --get-fmt-video | grep -E "Width|Pixel"
v4l2-ctl -d "$DEV" -l | grep -E "auto_exposure|exposure_time_absolute|exposure_dynamic|backlight|white_balance_automatic"
