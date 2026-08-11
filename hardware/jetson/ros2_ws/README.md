# Jetson 자율주행 (ROS 2 Humble)

Jetson Orin Nano 에서 **AprilTag 로 경로를 따라가며** STM32F429I-DISC1 에 UART 로
주행 명령을 보내는 Ackermann 로봇의 Jetson 측 코드.

**담당** 은빈 · **하드웨어** Jetson Orin Nano + Logitech Brio 100 + STM32F429I-DISC1

> **Nav2 는 쓰지 않는다.** 경로가 고정(도크→코너→도착지)이고 실외라 SLAM·전역
> 계획의 이득이 없다. 코드는 지우지 않고 남겨뒀지만 기동 경로에서 빠져 있다.
> 라이다도 같은 이유로 보류 상태다.

---

## 역할 분담

Jetson 이 "어디로 갈지", STM32 가 "어떻게 그 속도를 유지할지" 를 담당한다.
모터 PID 와 조향 서보 제어는 전부 STM32 에 있고 Jetson 은 목표값만 보낸다.
리눅스 스케줄러가 튀거나 Jetson 이 죽어도 제어 주기가 흔들리지 않게 하기 위한 분리다.

```
카메라 → tag_localizer_cv → /tag/target
                                 │
                                 ▼  route_runner : 상태머신 + 조향 생성 + TX watchdog
                    UART 115200 8N1 바이너리 V3 (CP2102)
                                 │
                                 ▼  STM32 : 속도 PID / 조향 서보 / 엔코더 / 오도메트리
```

**`route_runner` 가 UART 를 직접 소유한다.** 포트는 한 프로세스만 열 수 있어서
`/cmd_vel` 같은 중간 토픽을 두면 브리지를 따로 띄워야 하고 watchdog 이 하나 더
생긴다. `CMD_DRIVE` 가 조향각을 받으므로 각도를 직접 만드는 편이 Twist 왕복
변환보다 정확하다.

---

## 패키지

| 패키지 | 상태 | 내용 |
|---|---|---|
| `stm32_bridge` | **구현됨** | UART V3 프레임·CRC·파서, 프로토콜 상수, `fake_stm32` |
| `robot_perception` | **구현됨** | `tag_geometry`(순수 계산), `tag_localizer_cv`(검출 노드) |
| `robot_navigation` | **구현됨** | `route_logic`(상태머신), `route_runner`(노드), `odom_convert` |
| `robot_bringup` | **구현됨** | launch 2개, 진단 도구 9개, 검증 도구 3개 |
| `web_bridge` | 부분 구현 | 웹소켓 송신만. **수신 루프 미구현** (담당: 별도) |
| `robot_description` | 비어 있음 | URDF/TF. 차량 조립 후 작성 |

---

## 빌드

```bash
source /opt/ros/humble/setup.bash
colcon build --symlink-install
source install/setup.bash
```

`--symlink-install` 로 빌드하면 **파이썬 파일 수정은 재빌드 없이 노드 재시작만으로**
반영된다. `setup.py`·`package.xml`·새 파일 추가 때만 다시 빌드한다.

`build/` `install/` `log/` 는 형상관리하지 않는다 (절대경로가 박혀 다른 환경에서
쓸 수 없다). `src/` 만 받아서 각자 빌드한다.

---

## 실행

### 전체 기동 (권장)

```bash
ros2 launch robot_bringup tag_drive.launch.py
```

띄우는 것: `socat` + `fake_stm32`(가짜일 때) + 카메라 static TF +
`tag_localizer_cv` + `route_runner`.

| 인자 | 기본값 | 설명 |
|---|---|---|
| `use_fake` | `true` | 가짜 STM32 사용. 실장비면 `false` |
| `stm32_port` | `/tmp/ttyJETSON` | `use_fake:=false` 일 때 `/dev/ttyUSB0` |
| `cam_x` `cam_y` `cam_z` | `0.12` `0.0` `0.15` | ⚠ **임시값.** 조립 후 실측 |
| `tag_size_m` | `0.1335` | 실측 2026-08-04 (13.5×13.2 cm 평균). 재인쇄 시 재측정 |
| `leg_max_m` | `3.0` | 태그 못 본 채 이만큼 가면 `FAULT_OVERRUN` |
| `preview_port` | `8090` | 카메라 화면 HTTP. `0` 이면 끔 |
| `normal_flip` | `false` | `cross_track` 부호가 반대면 `true` |
| `approach_tag_id` | `2` | `/route/approach` 가 향할 태그 |

카메라 화면은 `http://<젯슨IP>:8090`. **노트북에서 `localhost` 로 열면 안 된다** —
노트북 자신을 가리킨다. 노드가 두 주소를 다 로그에 찍는다.

### 미션 명령

```bash
ros2 service call /route/approach std_srvs/srv/Trigger   # 태그 하나 접근 (체인 검증용)
ros2 service call /route/start    std_srvs/srv/Trigger   # 출발 1→2→3
ros2 service call /route/return   std_srvs/srv/Trigger   # 복귀 3→2→1
ros2 service call /route/abort    std_srvs/srv/Trigger   # 즉시 정지
ros2 service call /route/reset_fault std_srvs/srv/Trigger
```

응답에 `success` 와 거절 사유가 한글로 온다 (`"이미 주행 중이다"` 등).

> ⚠ 코스를 안 깔았으면 `/route/start` 가 아니라 `/route/approach` 를 쓴다.
> `start` 는 회전 후 태그 3 을 찾으므로 없으면 `FAULT_OVERRUN` 이 난다.

---

## 주행 로직

### 태그 배치 (4장, 코너에 2장)

| ID | 위치 | 마주보는 방향 |
|---:|---|---|
| 1 | 충전스테이션 | 복귀 진입 |
| 2 | 코너 · 면A | 출발 진입 |
| 3 | 도착지 | 출발 진입 |
| 4 | 코너 · 면B | 복귀 진입 |

코너는 2면 기둥이다. 갈 때와 올 때 진입 방향이 90° 달라 면이 두 개 필요하고,
ID 를 다르게 주면 상태머신이 "어느 방향에서 접근 중인지" 를 검증할 수 있다.

### 미션

| 출발 `/route/start` | 종료 조건 |
|---|---|
| `LEG1` 태그2 보며 직진 | `along ≤ 0.60 m` |
| `TURN 우 90°` 조향 −1268 cdeg 고정, R=0.6 m | yaw 누적 87° |
| `LEG2` 태그3 보며 직진 | `along ≤ 0.50 m` |
| `ARRIVED` | |

| 복귀 `/route/return` | 종료 조건 |
|---|---|
| `UTURN 우 180°` 조향 −2294 cdeg, R=0.319 m | yaw 누적 177° |
| `RET_LEG2` 태그4 보며 직진 | `along ≤ 0.60 m` |
| `TURN 좌 90°` 조향 +1268 cdeg | yaw 누적 87° |
| `RET_LEG1` 태그1 보며 직진 | `along ≤ 0.50 m` |
| `DOCKED` | |

복귀가 U턴으로 시작하는 이유: 왔던 길을 되돌아가려면 후진해야 하는데 후방
센서가 없고 카메라가 앞만 본다. U턴은 태그를 볼 필요가 없어 반경을 0.319 m 까지
줄여 공간을 아꼈다.

**회전 중에는 카메라를 전혀 쓰지 않는다.** STM32 yaw 만 보고 각도를 센다.

### 속도

`along ≥ 1.2 m` 순항 200 mm/s → `0.6 m` 접근 150 mm/s 로 선형 감속.
태그를 놓치면 진입 방향(yaw)을 유지하며 150 mm/s 로 전진하고 거리 상한 감시는
계속 돈다.

### 안전 감시

| 조건 | 결과 |
|---|---|
| `leg_max_m` 초과 | `FAULT_OVERRUN` (사유를 세 갈래로 구분해 알림) |
| 회전이 이론시간 × 2.5 초과 | 회전 타임아웃 FAULT |
| STM32 가 READY/DRIVING 아님 · 오도메트리 무효 | FAULT |
| `/safety/stop_active` · 장애물 | `HOLD` — 해제되면 자동 재개 |
| 상태머신이 200 ms 명령 갱신 실패 | **TX 스케줄러가 neutral 강제** |

마지막 항목이 핵심 안전장치다. 스케줄러는 최신 명령을 무한 반복하므로, 상태머신
타이머가 예외로 죽으면 로봇이 마지막 속도로 계속 간다 — STM32 watchdog 은
프레임이 계속 오니까 안 걸린다.

FAULT 는 래치된다(`reset_fault` 필요). HOLD 는 래치하지 않는다 — 사람이 지나갈
때마다 수동 재무장을 요구하면 시연이 불가능하다.

---

## 토픽 · 서비스

| 이름 | 타입 | 방향 |
|---|---|---|
| `/tag/target` | `String`(JSON) | `tag_localizer_cv` → `route_runner` |
| `/tag/diagnostics` | `String` | 검출 진단 |
| `/route/state` | `String`(JSON) | 상태머신 상태·단계·거리·fault 사유 |
| `/stm32/telemetry` | `String`(JSON) | STM32 텔레메트리 |
| `/stm32/connected` | `Bool` | 링크 생존 |
| `/stm32/odom` | `nav_msgs/Odometry` | **웹 관제 지도용.** 유효할 때만 발행 |
| `/stm32/odom_json` | `String`(JSON) | 진단·시험용 원본 |
| `/stm32/imu` | `sensor_msgs/Imu` | BNO085. **위치·방향엔 안 씀** |
| `/stm32/imu_json` | `String`(JSON) | 진단용 원본 |
| `/safety/stop_active` | `Bool` | → `route_runner` |
| `/route/{start,return,approach,abort,reset_fault}` | `std_srvs/Trigger` | 서비스 |

**`/stm32/odom` 은 pose 가 유효할 때만 나간다.** `Odometry` 에는 유효 표시 칸이
없는데 서버는 받은 위치를 최신값으로 들고 있어서, 한 번 잘못 박히면 다음 유효
보고까지 지도에 남는다. 틀린 위치보다 없는 위치가 낫다. 유효/무효 전이는 로그로
알린다.

### 웹 연동 상태

`web_bridge` 가 `/stm32/telemetry` `/stm32/connected` `/stm32/odom` 을 구독해
EC2 로 올린다. `/stm32/imu` 도 발행하지만 **BNO085 실물은 아직 미장착**이다.

**명령 방향(웹 → 로봇)은 아직 안 이어져 있다.** `web_bridge` 가 소켓을 읽지
않는다. 읽은 뒤 위 `/route/*` 서비스를 부르면 된다 —
`cmd_destination`→`start`, `return_to_charge`→`return`, `emergency_stop`→`abort`.

---

## 실장비 없이 실행하기

`fake_stm32` 가 **UART 레벨에서** 프로토콜을 실제로 구현한다. ROS 토픽 레벨
가짜가 아니므로 코드가 fake 일 때와 실장비일 때 완전히 동일하다. 포트 경로만
바뀐다. 프레임·CRC·SEQ·파서가 검증에서 빠지지 않는다. 자전거 모델로 위치까지
적분하므로 회전·거리 판정도 돈다.

```bash
ros2 launch robot_bringup tag_drive.launch.py        # use_fake 가 기본 true
```

실장비 fault 상태를 재현해 차단 경로를 시험할 수도 있다.

```bash
ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32 --boot-faults 0xA300
```

---

## 시험

`protocol.py` `parser.py` `tag_geometry.py` `route_logic.py` `odom_convert.py` 는
**ROS import 를 금지**해서 ROS 없이 직접 시험한다.

```bash
python3 -m pytest src/stm32_bridge/test src/robot_perception/test \
                  src/robot_navigation/test -q
```

| 패키지 | 개수 | 범위 |
|---|---:|---|
| `stm32_bridge` | 43 | CRC·golden frame·SEQ wrap·파서 복구·fault 표 + IMU 프레임 12 |
| `robot_perception` | 36 | 좌표 변환, 경로 오차, 비대칭 조향 clamp, 태그 추적 |
| `robot_navigation` | 56 | 상태머신 33 + 오도메트리 12 + IMU 변환 11 |

`test_pep257` 은 기존 docstring 서식(`D213`) 문제로 실패하며 코드 결함이 아니다.

### 전 구간 검증 (실차·카메라 없이)

`course_sim.py` 가 카메라 역할을 대신한다. `fake_stm32` 가 적분한 로봇 위치를
받아 그 자리에서 카메라가 볼 태그 상을 계산해 되돌린다 — **닫힌 고리**다.

```
route_runner → CMD_DRIVE → fake_stm32 → pose → course_sim → /tag/target
      ↑                                                          │
      └──────────────────────────────────────────────────────────┘
```

```bash
bash src/robot_bringup/test/e2e.sh
```

성공 조건은 "FAULT 가 안 났다" 가 아니라 **모든 단계를 순서대로 밟는 것**이다.
현재 **5/5 통과** (출발 4단계 / 복귀 5단계 / abort / FAULT 래치 / reset_fault).
이동거리와 회전 호길이가 이론값과 일치하고, U턴 횡방향 이월 0.642 m 를 시각
서보가 수렴시킨다.

추측항법 yaw 오차 내성도 잰다.

```bash
YAW_BIAS_DEG=10 bash src/robot_bringup/test/e2e.sh
```

**±8° 통과, −12° 통과, +10° 에서 복귀 실패** — U턴 직후 태그 4 가 화각을 벗어나
한 번도 못 본다. **여유가 10° 뿐이라 IMU 없이는 위태롭다.**

> 이 검증은 코스 배치와 상태머신 연결만 확인한다. 카메라 검출 성능(조명·모션
> 블러·화각 끝 왜곡)과 실제 미끄러짐·조향 지연은 실장비에서만 확인된다.

---

## 진단 도구 (`src/robot_bringup/scripts/`)

| 도구 | 용도 |
|---|---|
| `usb_map.sh` | USB 포트별 연결 장치 확인 |
| `serial_probe.py` | 시리얼 포트 탐색·식별 |
| `stm32_monitor.py` | UART 프레임 실시간 관찰 |
| `drive_test_suite.py` | STM32 팀 제공 11개 시나리오 |
| `route_monitor.py` | `/route/state` 요약 |
| `camera_setup.sh` · `camera_preview.py` | 카메라 설정·미리보기 |
| `tag_detect_test.py` | 태그 검출 단독 시험 |
| `tag_drive.py` | ROS 이전 standalone 판. **현재 미사용** |

⚠ **`route_runner` 와 `stm32_monitor.py`·`drive_test_suite.py` 를 동시에 띄우면
안 된다.** 시리얼 포트는 한 프로세스만 열 수 있다.

---

## 확정된 실측값

| 값 | 확정치 |
|---|---|
| `wheelbase_m` | **0.135 m** |
| `max_speed_mm_s` | **1565** |
| 조향 한계 | **비대칭 −2869 / +1955 cdeg** (우 −28.69° / 좌 +19.55°) |
| tick/m | **4094** (823 tick / 0.20106 m) |
| 최소 회전반경 | 우 0.2467 m / 좌 0.3806 m |

**조향이 비대칭인 것이 중요하다.** 대칭으로 clamp 하면 한쪽이 STM32 에서
`COMMAND_LIMIT` 으로 거부되는데 `CMD_DRIVE` 는 응답이 없어서 조용히 실패한다.
좌회전은 +19.55° 한계 때문에 **반경 0.38 m 밑으로 못 줄인다.**

### 아직 미확정

| 값 | 담당 |
|---|---|
| 카메라 장착 위치 `cam_x/y/z` | 차량 조립 후 실측 (현재 임시값) |
| ~~태그 인쇄물 실측 크기~~ | 완료 — 0.1335 (2026-08-04) |
| 윤거·차체 크기 | 조립 후 |
| IMU (BNO085) | **실물 미장착.** 수신 경로는 구현됨. U턴 정확도가 여기 달려 있다 |

---

## 알려진 함정

**노드 재시작** — `Ctrl-C` 로 `ros2 run` 래퍼만 죽고 자식이 살아남아 시리얼·카메라를
붙잡는 경우가 있다. 재실행이 안 되면 먼저 확인한다.

```bash
ps -ef | grep -E 'route_runner|tag_localizer' | grep -v grep
```

**udev** — 라이다와 STM32 용 CP2102 가 **VID:PID 가 둘 다 `10c4:ea60` 이고
시리얼도 `0001` 로 같다.** 구분되는 속성이 `ID_PATH` 밖에 없다. 규칙 정리 전에는
한 번에 하나만 꽂는다.

**Discovery Server** — **현재 사용하지 않는다.** 라이다를 보류하면서 필요가
없어졌고, `ROS_DISCOVERY_SERVER` 가 켜져 있으면 `ros2 topic list` 와 서비스가
보이지 않는다. `.bashrc` 에서 주석 처리해뒀다.

**ROS 2 서비스는 같은 기기 안에서만 보인다.** 노트북에서
`ros2 service call /route/start` 를 부르면 `waiting for service` 에서 멈춘다.
젯슨에서 실행해야 한다.

**카메라 화소 형식** — YUYV 로 열리면 5 fps 밖에 안 나온다. MJPG 를 강제해야 한다
(`tag_localizer_cv` 가 이미 설정한다).

---

## AprilTag 인쇄물

`src/robot_bringup/apriltags/` 에 ID 1~4 의 A4 인쇄용 PDF 가 있다.
tag36h11, **v2 = 검은 사각형 140 mm**, 254 dpi(1 px = 정확히 0.1 mm).
생성은 `tools/make_apriltag_sheets.py` 다.

**v1(160 mm)은 쓰지 않는다.** 내용이 A4 를 꽉 채워(200 mm) 프린터의 인쇄 불가
영역과 겹쳤고, 배율 100 % 로 뽑아도 드라이버가 축마다 다른 비율로 줄였다 —
실측 155.0 × 152.0 mm, 종횡비 +2.0 % 왜곡. `solvePnP` 는 태그를 정사각형으로
가정하므로 **종횡비 왜곡은 없는 기울기를 만들어내고 보정할 방법이 없다.**

v2 는 내용을 175 mm 로 줄여 사방 여백 17.5 mm 를 확보하고, **100 mm 검증자를
가로·세로에 넣었다.**

**인쇄 후 검증자 막대 두 개를 재라. 둘 다 100.0 mm 여야 한다.** 두 값이 다르면
축마다 다른 배율이니 다시 뽑아야 한다. 한 변만 재면 이 왜곡을 놓친다 — v1 에서
실제로 놓쳤다.
