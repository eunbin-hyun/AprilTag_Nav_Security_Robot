#!/bin/bash
# 가상 코스 닫힌 고리 검증. 카메라·실차 없이 전체 주행을 끝까지 돌린다.
set +u
SP="$(cd "$(dirname "$0")" && pwd)"
# 로그는 저장소 밖에 쓴다. 시험 산출물이 형상관리에 섞이면 안 된다.
OUT="${TMPDIR:-/tmp}/route_e2e"
mkdir -p "$OUT"
source "$SP/../../../install/setup.bash"
unset ROS_DISCOVERY_SERVER

socat pty,raw,echo=0,link=/tmp/ttyJETSON pty,raw,echo=0,link=/tmp/ttySTM32 &
SOCAT=$!
for i in $(seq 1 40); do [ -e /tmp/ttySTM32 ] && break; sleep 0.1; done
ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32 > "$OUT/fake.log" 2>&1 &
FAKE=$!
sleep 1
ros2 run robot_navigation route_runner --ros-args \
  -p serial_port:=/tmp/ttyJETSON -p leg_max_m:=3.5 > "$OUT/runner.log" 2>&1 &
RUN=$!
YAW_BIAS_DEG=${YAW_BIAS_DEG:-0} python3 "$SP/course_sim.py" > "$OUT/sim.log" 2>&1 &
SIM=$!
sleep 3

echo "로그: $OUT"
python3 "$SP/verify.py"
RC=$?

kill $SIM $RUN $FAKE $SOCAT 2>/dev/null
sleep 1
# ros2 run 래퍼만 죽으면 자식이 남는다. 이름으로 확실히 정리한다.
pkill -f 'lib/robot_navigation/rout''e_runner' 2>/dev/null
pkill -f 'lib/stm32_bridge/fak''e_stm32' 2>/dev/null
sleep 0.5
exit $RC
