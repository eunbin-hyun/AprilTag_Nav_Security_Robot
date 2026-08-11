#!/usr/bin/env bash
# USB 허브 포트별 연결 장치를 보여준다. udev 규칙을 ID_PATH 로 잡으려면
# "어느 물리 구멍이 어느 포트 번호인가" 를 알아야 하는데, 이 프로젝트는
# 라이다와 STM32 어댑터가 둘 다 CP2102 10c4:ea60 이고 시리얼도 0001 로 같아서
# ID_PATH 말고는 구분 수단이 없다. 그래서 이 매핑이 필요하다.
#
#   ./usb_map.sh            현재 포트 점유 상태
#   ./usb_map.sh --watch    꽂고 뺄 때마다 변화를 출력 (물리 포트 번호 확인용)
#
# --watch 로 물리 매핑을 만드는 방법:
#   1) 장치를 모두 뺀다
#   2) --watch 를 켠다
#   3) 아무 장치를 왼쪽 첫 구멍에 꽂는다 -> 출력된 포트 번호를 적는다
#   4) 빼고, 다음 구멍으로 옮긴다. 4개 구멍을 다 돌면 매핑 완성

set -uo pipefail

nodes_for() {   # $1 = 포트 경로 조각 (예: 1-2.3)
  local out=""
  for n in /dev/ttyUSB* /dev/ttyACM* /dev/video*; do
    [ -e "$n" ] || continue
    if udevadm info -n "$n" 2>/dev/null | grep -q "$1"; then
      out+="$n "
    fi
  done
  printf '%s' "$out"
}

snapshot() {
  for hub in 1-2 2-1; do
    local hp="/sys/bus/usb/devices/$hub"
    [ -d "$hp" ] || continue
    local mx spd
    mx=$(cat "$hp/maxchild" 2>/dev/null || echo 4)
    spd=$(cat "$hp/speed" 2>/dev/null)
    echo "── 허브 $hub  ($(cat "$hp/product" 2>/dev/null), ${spd}M)"
    for p in $(seq 1 "$mx"); do
      local d="$hp/$hub.$p"
      if [ -d "$d" ]; then
        printf "   포트 %s : ★ %-34s %s:%s  %sM  %s\n" "$p" \
          "$(cat "$d/product" 2>/dev/null || echo '?')" \
          "$(cat "$d/idVendor" 2>/dev/null)" \
          "$(cat "$d/idProduct" 2>/dev/null)" \
          "$(cat "$d/speed" 2>/dev/null)" \
          "$(nodes_for "$hub.$p")"
      else
        printf "   포트 %s : -\n" "$p"
      fi
    done
  done
}

serials() {
  echo "── 시리얼 장치 식별 정보"
  local found=0
  for n in /dev/ttyUSB* /dev/ttyACM*; do
    [ -e "$n" ] || continue
    found=1
    local info vid pid ser path
    info=$(udevadm info -n "$n" 2>/dev/null)
    vid=$(grep -oP 'ID_VENDOR_ID=\K.*' <<<"$info")
    pid=$(grep -oP 'ID_MODEL_ID=\K.*' <<<"$info")
    ser=$(grep -oP 'ID_SERIAL_SHORT=\K.*' <<<"$info")
    path=$(grep -oP 'ID_PATH=\K.*' <<<"$info")
    printf "   %-14s %s:%s  serial=%-14s %s\n" "$n" "$vid" "$pid" "${ser:-?}" "$path"
    # 0001 은 CP2102 공장 기본값이라 고유하지 않다. 두 어댑터가 같으면
    # 시리얼로는 구분이 불가능하므로 ID_PATH 로 묶어야 한다.
    if [ "$ser" = "0001" ]; then
      echo "        ⚠ serial=0001 은 CP2102 공장 기본값 — 고유하지 않다"
    fi
  done
  if [ "$found" = 0 ]; then
    echo "   (없음)"
  fi
}

if [ "${1:-}" = "--watch" ]; then
  echo "포트 변화 감시 중. Ctrl-C 로 종료."
  echo
  prev=""
  while true; do
    cur=$(snapshot)
    if [ "$cur" != "$prev" ]; then
      echo "───── $(date '+%H:%M:%S') 변화 감지"
      echo "$cur"
      serials
      echo
      prev="$cur"
    fi
    sleep 0.5
  done
fi

snapshot
echo
serials
