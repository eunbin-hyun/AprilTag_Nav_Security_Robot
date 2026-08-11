# 주행 시험 명령 모음

2026-08-03 실차 시운전 기준. 값이 바뀌면 이 문서도 같이 고친다.

---

## 빠른 시작 — 주행 한 판 (가장 자주 씀)

```bash
cd ~/ros_ws && source install/setup.bash
unset ROS_DISCOVERY_SERVER          # 설정돼 있으면 토픽·서비스가 안 보인다

# ① 인식 (상시 실행. 이미 떠 있으면 건너뛴다)
ros2 launch robot_bringup tag_perception.launch.py \
    cam_device:=/dev/v4l/by-id/usb-046d_Brio_100_2515ZBE1XJY8-video-index0

# ② 주행 — 실장비. use_fake 를 빼면 가짜 STM32 라 차가 안 움직인다
ros2 launch robot_bringup tag_drive.launch.py use_fake:=false stm32_port:=/dev/ttyUSB0

# ③ -1. 웹을 띄운다
export WEB_BRIDGE_URL='wss://robot.example.com/ws/robot'
export WEB_BRIDGE_TOKEN='any'
ros2 run web_bridge web_bridge_node

# ③ -2. 시작명령 — 상태 확인 후 한 번만
ros2 topic echo /route/state --once
ros2 service call /route/start std_srvs/srv/Trigger
```

### 시작 전 확인 (30초)

```bash
# 인식이 살아 있나 — static TF 하나만 남고 카메라·태그 노드가 죽어 있는 일이 잦다
ros2 node list
ros2 topic hz /tag/target        # 안 나오면 ① 을 다시 띄운다

# 주행 노드가 정말 떠 있나
ros2 service list | grep /route/
```

`/tag/target` 이 없으면 모든 drive 단계가 "태그를 한 번도 못 봤다" 로
FAULT_OVERRUN 이 된다.

### start 가 거절될 때

거절 사유는 `route_logic.py` 의 `_preflight` 가 그대로 돌려준다.

| 응답 | 뜻 | 조치 |
|---|---|---|
| `이미 주행 중이다` | state 가 RUNNING | `ros2 service call /route/abort std_srvs/srv/Trigger` |
| `FAULT 상태다 (…)` | FAULT latch | `ros2 service call /route/reset_fault std_srvs/srv/Trigger` |
| `STM32 가 READY 가 아니다` | 링크·재무장 | §1 통신 진단 |
| `오도메트리가 유효하지 않다` | pose_valid=false | STM32 재기동 |
| `정지 요청이 활성이다` | `/safety/stop_active` | 정지 해제 |

⚠ `ros2 service call` 은 **서비스가 뜰 때까지 기다린다.** launch 전에 걸어둔
호출이 노드가 뜨는 순간 먹으므로, 두 번째 엔터가 "이미 주행 중이다" 로
거절되는 것은 정상이다 — 첫 호출이 이미 미션을 시작했다는 뜻이다.
(2026-08-06 실제로 이 상황을 회귀로 오인했다. 먼저 `/route/state` 를 본다.)

### 끝나고 확인할 로그

```bash
ls -t ~/.ros/log/*.log | head -3
grep -E "미션 시작|-> |FAULT|이미 주행" $(ls -t ~/.ros/log/python3_*.log | head -1)
```

---

## 실측 확정값 (이 문서의 숫자들의 근거)

| 항목 | 값 | 출처 |
|---|---|---|
| 최대속도 (무부하) | 1565 mm/s | STM32 실측 |
| 바닥 확인 속도 | 200~400 mm/s 주행 확인 | 2026-08-03 바닥 |
| 정지거리 | **약 5 cm** (250 mm/s 접근 시) | approach 실측 |
| 조향 한계 | 좌 +1955 / 우 −2869 cdeg (**비대칭**) | vehicle_config.h |
| 서보 중립 | ⚠ **cdeg 0 은 직진이 아니다. +500 이 직진** (`steer_trim_cdeg`) | 실측 2026-08-04 |
| 좌조향 사점 | ⚠ +977 이 +1955 보다 **더 왼쪽** — over-center. 정점 미측정 | 실측 2026-08-04 |
| 뒷차축 중심 → 차 앞머리 | **0.27 m** | 실측 |
| 카메라 높이 | **0.215 m** (`cam_z`, launch 기본값) | 실측 2026-08-04 |
| 태그 중심 높이 | **0.215 m** — 카메라와 동일 | 실측 2026-08-04 |
| 태그 근접 검출 | 0.64 m 까지 유지 확인 | 태그를 카메라 높이에 부착 후 |
| 태그 실측 크기 | **0.1335 m** (13.5×13.2 평균) | launch 기본값 반영 (2026-08-04) |
| 정지 판정 공식 | `trigger = 0.27 + 원하는 앞머리간격 + 0.05` | — |

---

## 0. 공통 준비

```bash
cd ~/ros_ws && source install/setup.bash

# 장치 확인
ls /dev/ttyUSB0 /dev/video0

# 포트 충돌 확인 — 아무것도 안 나와야 함 (UART 는 한 프로세스만)
pgrep -af "drive_probe|drive_test|odom_square|stm32_monitor|link_check|route_runner"
```

⚠ 규칙: **UART(/dev/ttyUSB0)를 여는 도구는 동시에 하나만.** launch 가 떠 있으면
drive_probe 등을 실행하지 말 것.

---

## 1. 통신 진단 — `stm32_link_check` (구동 없음, 안전)

```bash
# 상태·fault·초음파 엿보기 (가장 자주 씀)
timeout 8 python3 ~/stm32_link_check.py --port /dev/ttyUSB0 --listen 4 --protocol 2 \
  | grep -E "DRIVE |RANGE"

# echo 왕복 (양방향 확인)
python3 ~/stm32_link_check.py --port /dev/ttyUSB0 --echo --protocol 2
```

fault 는 이름으로 찍힌다. `REPORT_ONLY` 는 주행을 안 막는다.

---

## 2. 수동 구동 — `drive_probe` (튜닝·측정용)

```bash
cd ~/ros_ws/src/robot_bringup/tools
```

```bash
# 속도 계단 (⚠ 바퀴 공중) — 데드밴드 측정
python3 drive_probe.py --port /dev/ttyUSB0 --staircase 60,100,150,200,250,313

# 조향 sweep (⚠ 바퀴 공중) — +가 좌회전인지 눈 확인
python3 drive_probe.py --port /dev/ttyUSB0 --sweep

# 고정 주행 (바닥) — 형식: 속도,조향cdeg,초
python3 drive_probe.py --port /dev/ttyUSB0 --hold 300,0,5       # 직진
python3 drive_probe.py --port /dev/ttyUSB0 --hold 200,-2869,10  # 우최대 원 (반경 검증)
```

| 옵션 | 의미 |
|---|---|
| `--max-speed 500` | 상한 해제 (기본 313 = 20%) |
| `--yes` | Enter 확인 생략 |

- 속도는 **목표값**이다. duty(출력%)는 STM32 PID 가 자동으로 올린다 —
  안 움직이면 duty 를 볼 것 (900‰+ 인데 정지 = 전원/기계 문제)
- **Ctrl-C 언제나 안전** (neutral 5발 후 종료)
- socat(가짜) 사용 시 연속 실행 사이 3초 간격 (실물 포트는 무관)

⚠ **`drive_probe` 는 조향 트림을 적용하지 않는다.** `route_runner` 의
`steer_trim_cdeg` 는 여기 없다. 바닥 직진은 실측 중립값을 직접 넣을 것:

```bash
python3 drive_probe.py --port /dev/ttyUSB0 --hold 400,500,5   # 직진 (500 = 실측 중립)
python3 drive_probe.py --port /dev/ttyUSB0 --hold 400,0,5     # 비교용: 우측으로 휜다
```

**초음파 장애물 복구도 이 도구에 들어 있다** (`stm32_bridge.obstacle_gate`).
장애물이 뜨면 neutral 로 바꿔 보내며 기다리고(STM32 는 SAFE_STOP 에서 비영
명령을 거부한다), 해제·rearm 후 같은 명령으로 재개한다. **HOLD 시간은 구간
시간에서 제외**되므로 `--hold 400,500,5` 는 언제나 주행 5초를 채운다. 다른 차단
fault·통신단절은 즉시 중단하고, 복구 경로에서 `CMD_STOP` 을 보내지 않는다.

```
⏸ 장애물 OBSTACLE_PRESENT — neutral 유지 (CMD_STOP 안 보냄)
⏸ 장애물 OBSTACLE_CLEAR_DEBOUNCE — neutral 유지 (CMD_STOP 안 보냄)
⏸ 장애물 STM32_REARM_WAIT — neutral 유지 (CMD_STOP 안 보냄)
▶ 재개 (HOLD 2.0s 제외됨)
```

---

## 3. 자동 통합시험 — `drive_test_suite` (11 시나리오, 합불+로그)

```bash
cd ~/ros_ws/src/robot_bringup/scripts
./drive_test_suite.py --only 1                          # 통신만 (아무때나)
./drive_test_suite.py --wheels-off-ground --only 1-9    # 벤치 일괄 (⚠ 바퀴 공중)
./drive_test_suite.py --on-ground --only 10             # 실바닥 오도메트리
```

로그: `~/drive_test_logs/*.jsonl`

---

## 3-1. 오도메트리 x·y 검증 — `odom_square_test` (정사각형 주행)

**yaw 는 0x83 IMU 로 확정됐다** (2026-08-06). 이 시험은 두 yaw 중 어느 쪽이
맞는지 고르는 게 아니라, **0x83 을 기준으로 주행했을 때 궤적이 맞는지** 본다.
회전 종료 판정도 0x83 하나로만 하고, 0x83 을 못 쓰면 시험을 시작하지 않는다 —
조용히 0x85 로 폴백하면 확정 사항을 어긴 결과가 통과로 찍힌다.

기준선은 **엔코더 거리 + 0x83 yaw 로 재적분한 궤적**이다. 직선 한 번으로는
오차가 안 드러나므로 정사각형을 돌아 **누적**시킨다.

```bash
cd ~/ros_ws/src/robot_bringup/scripts

# ① 가짜 STM32 로 리허설 (차 없이 · 로직·판정·로그 경로 확인)
socat pty,raw,echo=0,link=/tmp/ttyJETSON pty,raw,echo=0,link=/tmp/ttySTM32 &
ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32 &
./odom_square_test.py --port /tmp/ttyJETSON --on-ground --no-prompt

# ② 실장비 — 트림 없이 (STM32 적분 자체가 깨끗한지)
./odom_square_test.py --port /dev/ttyUSB0 --on-ground --steer-trim 0

# ③ 실장비 — 트림 걸고 (실주행과 같은 조건)
./odom_square_test.py --port /dev/ttyUSB0 --on-ground --steer-trim 500

# ④ 줄자로 재서 다시 — 이걸 줘야 "진짜 맞는지" 가 판정된다
./odom_square_test.py --port /dev/ttyUSB0 --on-ground --steer-trim 500 \
    --measured-closure-m 0.42 --measured-side-m 1.02
```

판정 네 가지 + 참고 측정 하나:

| 항목 | 뜻 |
|---|---|
| 0x83 표본 건전성 | 기준선이 이 값 하나라 결손율(기본 90% 이상)을 먼저 본다 |
| 직진 중 0x83 드리프트 | 직진 명령 중 **실제로** 휜 양. 서보 중립·토우·슬립이 여기 나온다 |
| 회전 정확도 | 90° 명령에 0x83 기준 몇 도 돌았나. 평균이 +면 관성 과회전이다 |
| **폐합 오차 (기준선)** | 엔코더 + 0x83 재적분 궤적이 출발점에 돌아왔나 — **본 판정** |
| (참고) STM32 x·y 오염량 | 보고된 x·y 가 기준선과 몇 m 갈라졌나. 판정 아님 |

⚠ **궤적이 예쁜 정사각형으로 그려져도 통과가 아니다.** 센서가 자기 오차를 못
보면 화면에는 정사각형이 뜨고 차는 마름모를 그린다. 0x83 을 믿기로 했다고 해서
0x83 이 자동으로 진실이 되는 건 아니다 — BNO085 게인 오차나 바닥 슬립은 IMU 도
못 잡는다. 그래서 `--measured-*` 를 안 주면 그 항목은 PASS 가 아니라
**미검증**으로 남는다.

"참고: STM32 x·y 오염량" 은 `/stm32/odom` 을 0x83 기반으로 바꾸는 작업의
**before 값**이다. STM32 x·y 는 0x85 yaw 로 적분되므로 트림이 걸리면 크게
갈라진다 — 트림 +500·축간거리 0.135 m 의 예측 드리프트가 **약 37°/m** 다.
②(트림 0)와 ③(트림 500)의 차이가 곧 트림이 만든 오염분이다.

주요 인자 (전부 기본값이 있다):

```bash
--side-m 1.0            # 한 변. 회전반경 0.42 m 라 2.3 m × 2.3 m 공간이 필요
--laps 1                # 정사각형 바퀴 수
--speed 100             # 직진 mm/s. 차체 무게 때문에 100 이하로 (2026-08-06)
--speed-turn 100        # 회전 mm/s. 마지막 각도에서 바퀴가 서면 올린다
--direction right       # right | left
--turn-lead-deg 0       # 과회전하면 판정이 알려주는 값으로 올린다
--imu-dropout-grace 0.8 # 회전 중 0x83 끊김 허용. 넘기면 멈춘다(0x85 폴백 없음)
```

로그: `~/drive_test_logs/odom_square_*.jsonl` (표본 전량 + 궤적 좌표)

⚠ `UART_RX_OVERFLOW`(bit13) 같은 `REPORT_ONLY` fault 는 **주행을 막지 않는다.**
시작 전에 `CMD_RESET_FAULT` 로 한 번 지우고 들어간다(`--no-reset-fault` 로 끔).
중단 기준은 `route_runner` 와 같은 `arm_blocking_faults` — neutral 로 안 풀리는
`MOTOR_STALL`·`STEERING_INVALID`·`ESTOP_ACTIVE`·`OBSTACLE_NEAR` 등만 멈춘다.
**주행 중에 bit13 이 새로 뜨면 그건 이번에 생긴 것**이니 따로 봐야 한다.

---

## 4. ROS 태그 주행 — launch 2개로 분리됨

**카메라·태그 인식은 주행과 분리되어 상시 실행이다.** 주행 launch 를 몇 번이든
재시작해도 카메라 화면(:8090)이 끊기지 않는다.

```
tag_perception.launch.py (상시)      tag_drive.launch.py (주행할 때만)
├─ camera_static_tf                  ├─ socat + fake_stm32 (개발 모드만)
└─ tag_localizer_cv                  └─ route_runner
   ├─ /tag/target 발행  ───────────────► /tag/target 구독
   └─ HTTP MJPEG :8090
   ▲
   └── systemd(4-3)가 부팅 때 이걸 띄운다. 서비스가 켜져 있으면
       손으로 또 띄우지 마라 — 카메라 중복 점유로 죽는다.
```

**평소 주행 절차는 런치 1개 + 서비스 1개다.** 서비스가 켜져 있으니 터미널에서
띄우는 것은 `tag_drive.launch.py` 하나뿐이다.

### 4-1. 인식 기동

> ### ⚠ 먼저 이것부터 확인해라
> ```bash
> systemctl is-active tag-perception     # active 면 이미 떠 있다
> ```
> **`active` 면 4-1 을 건너뛰고 4-2 로 간다.** systemd 서비스(4-3)가 부팅 때
> `tag_perception.launch.py` 를 **이미 띄워놨다** — 그게 두 런치 중 하나다.
> 여기서 손으로 또 띄우면 카메라가 겹쳐서 이렇게 죽는다:
> ```
> RuntimeError: /dev/video0 를 열 수 없다. 카메라는 한 프로세스만 열 수 있다.
> ```
> 이건 인식이 고장난 게 아니라 **이미 정상 동작 중**이라는 신호다.
> 프리뷰(http://192.168.100.252:8090)와 `/tag/target` 은 그대로 살아 있다.

서비스가 `inactive` 일 때만 손으로 띄운다:

```bash
cd ~/ros_ws && source install/setup.bash
ros2 launch robot_bringup tag_perception.launch.py

# 카메라 장치를 고정 경로로 (재부팅 후 video 번호가 바뀔 수 있다)
ros2 launch robot_bringup tag_perception.launch.py \
  cam_device:=/dev/v4l/by-id/usb-046d_Brio_100_2515ZBE1XJY8-video-index0
```

서비스를 켜둔 채로 손으로 띄우고 싶으면 먼저 내려야 한다:

```bash
sudo systemctl stop tag-perception
```

| 인자 | 기본 | 설명 |
|---|---|---|
| `cam_z` | **0.215** | 지면→렌즈중심 실측. 태그 중심 높이와 같아야 한다 |
| `cam_x` | 0.12 | ⚠ 뒷차축→렌즈. **미실측** — 정지 위치에 1:1 로 실린다 |
| `tag_size_m` | **0.1335** | 인쇄물 실측 (13.5×13.2 평균) |
| `normal_flip` | false | 2026-08-04 확인: false 가 맞다 (along +1.90, head +1.1°) |
| `exposure` / `preview_port` | 400 / 8090 | |

⚠ **카메라는 한 프로세스만 열 수 있다.** 이게 떠 있는 동안
`scripts/tag_drive.py` 나 손으로 띄운 `tag_localizer_cv` 는 실패한다.
점유 확인: `fuser -v /dev/video0` · `ss -ltnp | grep 8090`

### 4-2. 주행 기동

```bash
cd ~/ros_ws && source install/setup.bash
ros2 launch robot_bringup tag_drive.launch.py use_fake:=false \
  stm32_port:=/dev/ttyUSB0 \
  v_cruise_mm_s:=150 v_approach_mm_s:=100 v_turn_mm_s:=150 \
  kp_cross:=2000 kp_heading:=800 \
  leg_max_m:=3.0
```

카메라 인자(`cam_*`, `tag_size_m`, `exposure`, `preview_port`, `normal_flip`)는
여기 없다 — 4-1 로 넘겨야 한다.

| 인자 | 현재값 | 설명 |
|---|---|---|
| `v_cruise/approach/turn_mm_s` | 250/200/250 | 저속 정밀 시험은 150/100/150 도 가능 — 단 100 은 바닥 미확인 |
| `kp_cross` | 2000 | 옆 10 cm → 2° 보정. 지그재그면 1500, 굼뜨면 2500 |
| `kp_heading` | 800 | 머리 10° → 1.4° 보정 |
| `steer_trim_cdeg` | **500** | 조향 중립 트림. cdeg 0 은 우측 약 5° |
| `approach_trigger_m` | 0.7 (기본) | 정지 판정. 최종 정지는 −5 cm (관성) → along 0.65 |
| `leg_max_m` | 3.0 | 태그 소실 채 이만큼 가면 FAULT_OVERRUN |
| `odom_pose_source` | **tick** | `/stm32/odom` 의 pose 출처. 아래 참고 |

#### `/stm32/odom` 이 웹 지도에 보내는 값 (2026-08-06 변경)

`tick`(기본)은 **주행이 그 tick 에 실제로 쓴 값**을 보낸다 — yaw 는 0x83 IMU
quaternion, x·y 는 엔코더 누적거리를 그 yaw 로 재적분한 값이다. 관제 지도가
차와 같은 것을 보게 된다.

⚠ **0x83 을 못 쓰면 발행하지 않는다.** 주행은 0x85 로 폴백해 계속 가지만 웹
지도에는 위치가 안 뜬다 — 두 yaw 는 원점이 달라서 섞이는 순간 지도의 로봇이
튀기 때문이다. 그래서 **BNO085 가 빠졌거나 quaternion accuracy 가 낮으면 관제
화면에 로봇이 안 나타난다.** 시연 중 그 상황이면 롤백한다:

```bash
ros2 launch robot_bringup tag_drive.launch.py ... odom_pose_source:=stm32
```

`stm32` 는 예전 동작(0x85 원본 x·y·yaw)이다. 조향 트림이 걸려 있으면 STM32 가
직진을 곡선으로 오해해 위치가 휘지만(트림 +500 에서 약 37 deg/m), 적어도
화면에는 뜬다. **주행에는 어느 쪽이든 영향이 없다.**

확인:

```bash
ros2 topic hz /stm32/odom          # tick 모드에서 20 Hz. 안 나오면 0x83 문제
ros2 topic echo /stm32/odom_json   # 0x85 원본은 모드와 무관하게 항상 나온다
```

로그로도 갈린다 — `/stm32/odom 발행 시작 — 주행이 쓰는 값` / `/stm32/odom 중단
(odom_valid=… yaw_source=…)`.

⚠ `odom_pose_source:=tick` 과 `yaw_source:=odom` 은 같이 못 쓴다(게이트가 영원히
닫힌다). 노드가 뜨는 자리에서 거부한다.

### 관찰

```bash
ros2 topic echo /route/state            # 별도 터미널
ros2 topic echo /tag/target             # 인식만 확인할 때
ros2 topic echo /tag/diagnostics        # 게이트 탈락 사유
```
```
http://192.168.100.252:8090             # 카메라 프리뷰 (노트북에서. localhost 금지)
```

프리뷰 주소는 인식 launch 시작 직후 로그에 찍힌다.
안 보이면: `grep -rh "미리보기" ~/.ros/log/*.log | tail -1`

### 4-3. 부팅 자동 실행 (systemd)

**이미 등록·enable 되어 있다 (2026-08-05).** 아래는 재설치·롤백용이다.
등록된 상태에서는 **4-1 을 실행하지 않는다** — 서비스가 그 launch 를 돌린다.

수동 검증이 끝난 뒤에만 등록한다.

```bash
sudo cp ~/ros_ws/src/robot_bringup/systemd/tag-perception.service \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tag-perception
systemctl status tag-perception
journalctl -u tag-perception -f
```

⚠ **이 유닛은 `ROS_DISCOVERY_SERVER` 를 쓰지 않는다.** 저장소가 기본 멀티캐스트
DDS 로 합의했고 `.bashrc` 의 해당 줄도 주석 처리되어 있다. 한쪽만 Discovery
Server 를 쓰면 **systemd 노드와 대화형 터미널이 서로를 못 봐서** `/tag/target`
이 route_runner 에 도달하지 않고, 모든 drive 단계가 "태그를 한 번도 못 봤다"
FAULT_OVERRUN 이 된다.

```bash
sudo systemctl stop tag-perception       # 손으로 카메라를 쓸 때
sudo systemctl disable --now tag-perception   # 롤백
```

### 미션 명령

⚠ **반드시 별도 터미널에서.** 4-2 의 `ros2 launch` 는 터미널을 붙잡고 끝나지
않으므로, 같은 터미널에 이어 쓰면 아래 명령은 **실행되지 않는다** (상태가
`IDLE` 에 계속 머물고 거절 WARN 도 안 찍힌다 — 호출 자체가 없어서다).
한 번에 하나씩, 완료(`ARRIVED`)나 `FAULT` 를 확인한 뒤 다음 것을 부른다.

```bash
cd ~/ros_ws && source install/setup.bash    # 새 터미널마다

ros2 service call /route/approach    std_srvs/srv/Trigger  # 태그2 접근·정지 (검증용)
ros2 service call /route/start       std_srvs/srv/Trigger  # 출발 코스 1→2→3
ros2 service call /route/return      std_srvs/srv/Trigger  # 복귀 3→2→1 (첫단계 U턴은 카메라 무관)
ros2 service call /route/turn_left   std_srvs/srv/Trigger  # 상대 yaw 좌회전 (기본 90°)
ros2 service call /route/turn_right  std_srvs/srv/Trigger  # 상대 yaw 우회전
ros2 service call /route/abort       std_srvs/srv/Trigger  # 비상 정지 (언제든)
ros2 service call /route/reset_fault std_srvs/srv/Trigger  # STM32 fault reset (통신계열+E-STOP) + Jetson FAULT 해제
```

거절되면 응답에 사유가 한글로 온다 ("STM32 가 READY 가 아니다" 등).

### 코스 배치

```
출발 ──직진 1.5~2.5m──▶ [태그2] 코너     ┐ 태그 중심높이 = 카메라 높이 21.5cm 필수!
                          │ 우회전 R0.6m  │ (높이 안 맞으면 근접에서 프레임 이탈
                          ▼              │  → 정지 판정 놓침)
                       [태그3] 도착       ┘
```

- LEG 정지 판정: 태그2 앞 0.60 / 태그3 앞 0.50 (뒷차축 기준, 하드코딩)
- 복귀는 태그 4(코너 반대면)·1(도크) 추가 배치 후

### 상대 yaw 회전 시험 (JETSON_YAW90)

카메라·태그 불필요 — 0x85 오도메트리 **융합 yaw** 만 쓴다. 완료 시 로그에
`회전 완료 — signed error` 가 찍히고, 그 값으로 stop lead 를 조정한다.

> 🚫 **2026-08-05 현재 실장비에서 성립하지 않는다.** "태그 불필요" 라서 우회로처럼
> 보이지만, 회전은 **융합 yaw 를 요구하는 쪽**이다. `imu=OK/g0` (BNO085 자이로
> accuracy 0) 이라 STM32 가 `IMU_FUSED` 를 세우지 않고, 코스 코너와 **똑같은**
> 가드에 걸린다 (주행 0.7 s 후 FAULT). fake 는 `g3` 을 보고하므로 통과한다 —
> fake 성공을 실장비 가능으로 오해하지 말 것.
>
> 추가로 `turn_steering_cdeg` 1800 + 트림 500 = 2300 → **1955 로 clamp** 되고,
> 그 값은 좌조향 사점을 넘은 영역이다 (`--steer-scan` 미측정). IMU 가 풀려도
> 좌회전은 언더스티어한다. 로그의 `wire=` 값으로 확인할 수 있다.
>
> 선행 조건 둘 중 하나: **기계 재트림(`steer_trim_cdeg:=0`)** 또는
> **자이로 accuracy ≥ 1**.

🖥 **터미널 1** — launch. 이건 터미널을 붙잡고 끝나지 않는다.

```bash
# 1단계: 30° 저속 (바닥 1 m² 확보)
cd ~/ros_ws && source install/setup.bash
ros2 launch robot_bringup tag_drive.launch.py use_fake:=false \
  stm32_port:=/dev/ttyUSB0 turn_target_deg:=30.0 \
  v_turn_mm_s:=150 v_turn_slow_mm_s:=200
```

🖥 **터미널 2** — 미션 명령. **launch 와 같은 터미널에 이어 쓰면 실행되지 않는다.**

```bash
cd ~/ros_ws && source install/setup.bash
ros2 service call /route/turn_left  std_srvs/srv/Trigger
# ↑ 완료(ARRIVED) 또는 FAULT 를 확인한 뒤에 다음 것을 호출한다.
#   연달아 넣으면 "이미 주행 중이다" 로 거절된다.
ros2 service call /route/turn_right std_srvs/srv/Trigger
```

```bash
# 2단계: 90° (1단계 방향·부호 정상일 때만) — 방향별 5회 반복
#   터미널 1 의 launch 를 turn_target_deg:=90.0 으로 재기동
```

⚠ `preview_port` 는 이제 `tag_drive.launch.py` 인자가 아니다 (4-1 로 이동).
넘겨도 **조용히 무시**되므로 오류는 안 나지만 아무 효과도 없다.

**서비스가 거절되면 상태가 IDLE 에 머문다.** 사유는 `ros2 service call` 응답의
`message` 와 launch 터미널의 **WARN 줄**에 한글로 찍힌다. 상태가 IDLE 인데
회전을 안 하면 그 두 곳을 먼저 볼 것 — 수용됐다면 즉시 `RUNNING/YAW ...` 로
바뀐다.

| 인자 | 기본 | 설명 |
|---|---|---|
| `turn_target_deg` | 90 | 목표 회전량. 첫 시험은 30 |
| `turn_steering_cdeg` | 1800 | 좌우 대칭 고정 조향 (±18°, R≈0.42 m) |
| `v_turn_slow_mm_s` | 90 | 잔여 15° 감속 속도. ⚠ 바닥 데드밴드(200 미만 출발불가) 아래 — 실차는 200 부터 |
| `stop_lead_left_deg` / `right` | 3.0 | 관성 보정 조기정지량. 방향별 별도 |
| `turn_max_m` | 0.90 | 이동거리 가드 (yaw 불변 시 무한 원호 방지) |

stop lead 조정 (방향별 5회 signed error 평균으로):

```
평균 +4° 오버슈트 → 해당 방향 stop_lead 를 3+4=7 로
평균 −3° 부족    → 해당 방향 stop_lead 를 줄임
```

자동 FAULT: 역방향 5°(조향·yaw 부호 오류) · yaw 점프 30°/tick ·
이동 0.9 m 초과 · 주행 0.7 s 후에도 IMU_FUSED=0 · 타임아웃.
정지 중 IMU_FUSED=0 은 정상이다.

fake 검증 결과 (2026-08-04): 30°·90° 좌우 전부 오차 ±2.6° 이내,
90° 이동거리 0.636 m ≈ 이론 호길이 0.652 m − stop lead.

### STM32 fault reset 시험 (CMD_RESET_FAULT)

`/route/reset_fault` 는 **통신·명령 계열 fault 만** STM32 에서 지운다
(CRC_ERROR·BAD_COMMAND·COMMAND_LIMIT·UART_RX/TX). 장애물·센서(자동해제)·
조향(보정필요)·latch 계열은 대상이 아니다. 주행 재시작 명령이 아니며,
해제 후에도 별도 출발 명령이 필요하다.

```bash
# launch 떠 있는 상태에서 (실장비: bit13 재부팅 없이 해제 검증)
ros2 service call /route/reset_fault std_srvs/srv/Trigger
```

응답·로그 읽는 법:

```
success=True "reset 요청 전송: [bit들] — telemetry 확인 중"
  → 몇 초 안에 로그: "STM32 fault 해제 확인"           = 성공
  → "reset 시간초과 — 잔여 [bit들]"                   = STM32 쪽 확인 필요
success=False "reset 대상 fault 없음 (활성: ...)"      = STM32엔 지울 게 없음
success=False "주행 중이다 — 먼저 /route/abort"
```

fake 재현: `fake_stm32 --boot-faults 0x2006` (bit1·2·13) 심고 호출.
혼합 검증: `0x2206` → bit9 는 자동 제외되고 요청분만 해제된다.

### 초음파 장애물 HOLD·자동 재개 시험

> **Jetson 구현·시험 완료 (2026-08-04).** 상태머신·fake·회귀시험 27건 통과.
>
> ⚠ **실장비 통합검증 확인 항목**: STM32 저장소와 플래시 펌웨어의 버전이
> 갈려 있다 — bit16·0x84 가 있는 빌드(8/3 오전 관측)와 없는 빌드가 섞여
> 플래시되고 있다. 실장비에서 bit16 을 안전 신호로 쓰기 전에
> **버전 정렬·재플래시·on-wire 검증**(link_check 10초 캡처로 0x84 13B·bit16
> 확인)을 통과해야 한다. Jetson 쪽 구현을 보류할 이유는 아니다.
> STM32 자체 정지(350mm PWM 0 / 200mm E-STOP 래치)는 별개로 동작한다.

**복구 하위상태** (`/route/state` 의 `hold_substate`):

```
OBSTACLE_PRESENT         bit16=1. clear 타이머 정지, neutral 20 Hz
OBSTACLE_CLEAR_DEBOUNCE  bit16=0 이 0.5 s 연속 유지되길 대기
STM32_REARM_WAIT         debounce 통과. STM32 READY(2) 를 최대 2.0 s 대기
```

**두 조건을 모두** 만족해야 재개한다 — bit16=0 이 0.5 s 유지 **AND** state=READY.
어느 쪽이 먼저 와도 다른 쪽을 기다린다. 복구 구간에는 `CMD_STOP` 을 보내지
않는다 (`CMD_STOP` 은 `rearm_required` 를 다시 세워 복구를 스스로 막는다).

```bash
# fake 로 재현 (실장비·손 불필요)
ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32 \
  --obstacle-at 5 --obstacle-hold-s 2 --obstacle-ready-delay-ms 600

# on-wire 확인 스크립트 (socat + fake, CMD_STOP 0건 확인)
python3 ~/ros_ws/src/robot_bringup/scripts/obstacle_recovery_wire_check.py

# 복구 구간 프레임 단위 추적 (기본 off)
ros2 launch robot_bringup tag_drive.launch.py ... debug_obstacle_recovery:=true
```

| 인자 | 기본 | 설명 |
|---|---|---|
| `obstacle_ready_timeout_s` | 2.0 | debounce 통과 후 READY 대기 상한. 넘으면 FAULT |
| `debug_obstacle_recovery` | false | 복구 구간 한 줄 추적 로그 |

주행 중 장애물 감지(bit16) → **HOLD + neutral** (FAULT 아님) → 치워진 뒤
**0.5 s 연속 clear** 시 같은 단계에서 자동 재개. 운영자 정지
(`/safety/stop_active`)는 debounce 없이 해제 즉시 재개.

```bash
# 주행 중 (approach 등) 초음파 앞에 손·판 대기 → 관찰:
ros2 topic echo /route/state
#   "state": "HOLD", "hold_reason": "obstacle"   ← 정지 확인
# 손 치우고 0.5초 → RUNNING 복귀, 같은 단계·진행거리 유지
```

| 인자 | 기본 | 설명 |
|---|---|---|
| `obstacle_clear_hold_s` | 0.5 | clear 안정 대기. 재개가 성급하면 0.8~1.0 |

재개가 안 되면: `hold_substate` 를 먼저 본다.

- `OBSTACLE_PRESENT` 에서 안 벗어남 → 초음파가 flicker 로 재감지 중. telemetry
  의 `OBSTACLE_NEAR` 확인
- `STM32_REARM_WAIT` 에서 2.0 s 후 FAULT → STM32 가 READY 로 오지 않는다.
  FAULT 로그의 `active_fault` 에 다른 bit 가 있는지 확인

치명 fault·통신단절은 HOLD 가 아니라 FAULT 가 우선한다 (의도된 동작) —
복구 대기가 그것을 숨기지 않는다.

---

## 5. 문제 해결

| 증상 | 원인 → 조치 |
|---|---|
| `could not open port` | 장치 없음 → `ls /dev/ttyUSB*`. USB 재삽입 후 2~3초 대기 |
| ARM 실패 + fault 이름 출력 | `SAFE_STOP` 등급 fault 활성. `OBSTACLE_NEAR` 면 초음파 앞 비우기 |
| echo 무응답 (수신은 정상) | **TX 선 단선** — CP2102 TXD→PD2 재확인 (실제 사례) |
| 태그 인식 안 됨 | **`tag_perception.launch.py` 가 떠 있는지** → 카메라 USB (`ls /dev/video*`) → 프리뷰로 검출 확인 → 태그 높이 |
| 카메라를 열 수 없다 | 중복 점유. `fuser -v /dev/video0` → systemd 서비스면 `sudo systemctl stop tag-perception` |
| 8090 이 이미 사용 중 | `ss -ltnp \| grep 8090` → 기존 프로세스 종료 또는 `preview_port` 변경 |
| 태그 근접 소실 후 전진 계속 | 태그 중심을 카메라 높이로 + trigger 상향 |
| 직진인데 휨 | drive_probe `--hold 300,±100,5` 로 상쇄값 찾기 → STM32 중립 트림 |
| 보정이 과함 (지그재그) | `kp_cross` ↓ (2000→1500), 속도 ↓ |
| 보정 방향 반대 (점점 이탈) | `normal_flip:=true` |
| `ros2 topic list` 빈약 | PC 에서 `ros2 daemon stop && sleep 2` 후 재시도 |
| 프리뷰 안 열림 | launch 살아있는지 + Jetson IP 로 접속했는지 |

## 6. 안전 수칙

- 계단·sweep·suite 3~9·11 은 **바퀴 공중**
- 바닥 주행은 한 명이 따라붙고, abort 터미널 상시 대기
- 물리 E-stop 이 아직 없다 — 이상하면 **들어올리는 것**이 E-stop
- 송신이 끊기면 STM32 가 0.3 초 내 자체 정지한다 (watchdog) — Ctrl-C 는 안전
