# Jetson 파트 AI에 그대로 전달할 구현 요청서

아래 `요청 시작`부터 `요청 끝`까지를 Jetson 프로젝트를 작업하는 AI에게
그대로 전달한다. 함께 전달할 파일은 다음과 같다.

- `docs/jetson_handover_2026-07-31.md`
- `docs/jetson_stm32_uart_interface_v3.md`
- `tools/jetson_uart_echo_test.py`
- `tools/uart_protocol_test.py`
- `Core/Inc/comm_protocol.h`

---

## 요청 시작

너는 Jetson 측 자율주행 소프트웨어와 STM32 주행 제어기의 UART 연동을
구현하는 담당자다.

코드를 바로 수정하지 말고 먼저 현재 Jetson 프로젝트 전체를 분석해라.

확인할 것:

- ROS/ROS2 사용 여부와 버전
- 현재 주행 명령을 만드는 노드와 메시지
- 현재 odometry, TF, IMU, 장애물 데이터 흐름
- 사용 중인 UART/serial 라이브러리
- 실행 방식, launch 파일, systemd 또는 Docker 사용 여부
- 단위, 좌표계, frame 이름
- 동일 UART를 이미 사용하는 프로세스가 있는지
- 테스트와 CI 구조

분석 후 기존 구조에 가장 적게 영향을 주는 방식으로 UART transport,
protocol parser, command scheduler, telemetry publisher를 구현해라.

### 1. 절대 조건

- STM32 wire protocol version은 `0x02`다.
- UART는 115200 bps, 8-N-1, no flow control, 3.3 V TTL이다.
- 하나의 프로세스만 UART device를 열어야 한다.
- 송신과 수신을 별도 터미널/프로세스로 나누지 마라.
- 한 UART owner 안에서 RX parser와 TX scheduler를 함께 운용해라.
- 주행 중 `CMD_DRIVE`는 목표값이 변하지 않아도 20 Hz로 계속 보낸다.
- 송신하는 모든 메시지는 종류와 무관하게 하나의 TX SEQ를 공유한다.
- SEQ는 프레임마다 증가하고 `255 → 0`으로 wrap한다.
- `CMD_DRIVE`와 `CMD_STOP`에 같은 SEQ를 재사용하지 마라.
- STM32의 300 ms watchdog을 상위 제어 안전장치로 대체하려 하지 마라.
- 물리 조향센서, IMU, 유효한 방향별 초음파 텔레메트리가 있다고 가정하지
  마라.
- STM32는 전방 초음파가 0.20 m 미만이면 로컬 STOP하고
  `OBSTACLE_NEAR` fault를 보낸다. 0.30 m 이상 정상 샘플 3회가 STOP 해제
  조건이다.
- `TELEMETRY_DRIVE.yaw_cdeg=0`과
  `steering_feedback_cdeg=0`을 유효 센서값으로 사용하지 마라.
- 오도메트리는 `0x85 TELEMETRY_ODOMETRY`만 사용해라.
- 구형 V1의 `0x82 TELEMETRY_ODOMETRY`를 사용하지 마라.

### 2. 공통 프레임

```text
[AA][55][02][MSG_ID][SEQ][LEN][PAYLOAD...][CRC_L][CRC_H]
```

```text
SOF1 = 0xAA
SOF2 = 0x55
VERSION = 0x02
MAX_PAYLOAD = 64
모든 다중 바이트 값 = little-endian
CRC = CRC-16/CCITT-FALSE
poly = 0x1021
init = 0xFFFF
xorout = 0
CRC 입력 = VERSION부터 PAYLOAD 마지막 바이트까지
```

RX parser 요구사항:

- serial read 경계와 frame 경계가 일치한다고 가정하지 마라.
- 여러 frame이 한 번에 들어오는 경우를 처리해라.
- frame이 여러 read로 나뉘는 경우를 처리해라.
- noise, 잘못된 version/length/CRC 뒤에서 다음 `AA 55`를 재탐색해라.
- payload 내부에 `AA 55`가 들어갈 수 있다.
- 파싱 실패 하나로 serial thread를 종료하지 마라.

### 3. Jetson → STM32: 보내야 하는 값

#### CMD_DRIVE `0x10`, 5 bytes, 20 Hz

```python
payload = struct.pack(
    "<hhB",
    target_speed_mm_s,
    target_steering_cdeg,
    drive_enable,
)
```

| 값 | 형식 | 단위와 범위 |
| --- | --- | --- |
| target_speed_mm_s | int16 | -1565~+1565 mm/s |
| target_steering_cdeg | int16 | -2869~+1955, 0.01 deg |
| drive_enable | uint8 | 0 또는 1 |

부호:

```text
speed + = 전진
speed - = 후진
steering + = 왼쪽
steering - = 오른쪽
```

정확한 neutral/rearm:

```text
speed=0
steering=0
drive_enable=0
```

`drive_enable=0`이면서 속도나 조향이 0이 아닌 값은 보내지 마라.

`CMD_DRIVE`는 `COMMAND_RESULT`가 오지 않는다. 수락 확인은 다음 필드로
한다.

```text
TELEMETRY_DRIVE.last_drive_seq
TELEMETRY_ODOMETRY.last_drive_seq
```

#### CMD_STOP `0x11`, 1 byte

```python
payload = struct.pack("<B", stop_reason)
```

```text
0 operator
1 mission complete
2 obstacle
3 remote request
4 internal
5 ROS command timeout
6 link shutdown
```

`COMMAND_RESULT`의 `request_message_id=0x11`과 request SEQ를 대조해
수락 여부를 확인해라.

#### CMD_RESET_FAULT `0x12`, 4 bytes

```python
payload = struct.pack("<I", acknowledged_fault_bits)
```

STM32가 `SAFE_STOP`, `FAULT`, `ESTOP`일 때만 사용한다. 현재 fault mask를
확인한 뒤 실제로 확인하고 해제하려는 bit만 1로 보내라.

#### DIAG_ECHO_REQ `0xF0`, 0~31 bytes

연결 확인 전용이다. Echo response payload는 다음 순서다.

```text
[request_sequence][original_request_payload...]
```

response frame header의 SEQ는 STM32 자체 TX SEQ이므로 request frame
header SEQ와 같다고 가정하지 마라.

### 4. STM32 → Jetson: 받아야 하는 값

#### TELEMETRY_DRIVE `0x80`, 26 bytes, 20 Hz

```python
(
    mcu_time_ms,
    target_speed_mm_s,
    measured_speed_mm_s,
    motor_duty_permille,
    steering_cmd_cdeg,
    steering_feedback_cdeg,
    encoder_count,
    yaw_cdeg,
    drive_state,
    last_drive_seq,
    active_fault_bits,
) = struct.unpack("<IhhhhhihBBI", payload)
```

주의:

```text
motor_duty_permille 단위 = 0.1%
steering_cmd_cdeg 단위 = 0.01 deg
steering_feedback_cdeg = 현재 유효하지 않음
yaw_cdeg = IMU 미장착으로 현재 0 placeholder
```

#### TELEMETRY_ODOMETRY `0x85`, 36 bytes, 20 Hz

```python
(
    mcu_time_ms,
    x_mm,
    y_mm,
    yaw_mdeg,
    distance_mm,
    linear_speed_mm_s,
    yaw_rate_mdeg_s,
    steering_cdeg,
    curvature_micro_per_m,
    status_flags,
    steering_source,
    last_drive_seq,
) = struct.unpack("<IiiiihihiHBB", payload)
```

좌표계와 단위:

```text
+x = 차량 전방
+y = 차량 왼쪽
+yaw = 좌회전
x/y/distance = mm
yaw = 0.001 deg
speed = mm/s
yaw_rate = 0.001 deg/s
steering = 0.01 deg
curvature = 1e-6 / m
```

status bit:

```text
bit 0 VALID
bit 1 ENCODER_CALIBRATED
bit 2 GEOMETRY_CALIBRATED
bit 3 STEERING_ESTIMATED
bit 4 IMU_FUSED
bit 5 INPUT_INVALID
```

steering source:

```text
0 NONE
1 SENSOR
2 COMMAND_ESTIMATE
```

현재 pose 사용 조건:

```python
required = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3)
pose_valid = (
    (status_flags & required) == required
    and (status_flags & (1 << 5)) == 0
    and steering_source == 2
)
```

현재 조향센서는 없다. PWM↔등가 중심 조향각 7점 LUT를 사용한 추정
오도메트리다. ROS odometry covariance를 실제 조향센서+IMU 융합 시스템처럼
낮게 설정하지 마라.

추가 처리:

- `yaw_mdeg`의 ±180 deg wrap을 처리해라.
- STM32 reset에 따른 `mcu_time_ms`, encoder, pose의 갑작스러운 초기화를
  감지해라.
- UART pose reset 명령은 아직 없다.
- Jetson local origin이 필요하면 첫 valid pose를 offset으로 저장해라.

#### TELEMETRY_RANGE `0x84`, 13 bytes, 10 Hz

```python
(
    mcu_time_ms,
    front_left_mm,
    front_right_mm,
    rear_left_mm,
    rear_right_mm,
    valid_mask,
) = struct.unpack("<IHHHHB", payload)
```

valid mask:

```text
bit 0 front_left
bit 1 front_right
bit 2 rear_left
bit 3 rear_right
```

현재는 네 값이 모두 `0xFFFF`, `valid_mask=0`이다. `valid_mask`가 0인
거리값을 장애물 거리로 publish하거나 판단에 사용하지 마라.

#### FAULT_EVENT `0x81`, 14 bytes

```python
(
    occurred_at_ms,
    active_fault_bits,
    latched_fault_bits,
    action,
    state,
) = struct.unpack("<IIIBB", payload)
```

action:

```text
0 REPORT_ONLY
1 HOLD_NEUTRAL
2 SAFE_STOP
3 OUTPUT_DISABLE
4 LATCHED_STOP
```

#### COMMAND_RESULT `0x82`, 4 bytes

```python
(
    request_message_id,
    request_sequence,
    result_code,
    state,
) = struct.unpack("<BBBB", payload)
```

result:

```text
0 ACCEPTED
1 INVALID_STATE
2 OUT_OF_RANGE
3 FAULT_ACTIVE
4 UNSUPPORTED
5 INVALID_VALUE
6 DUPLICATE
7 NOT_ARMED
```

request message ID와 request sequence를 둘 다 대조해서 pending request를
완료해라.

### 5. state와 fault

state:

```text
0 BOOT
1 SELF_TEST
2 READY
3 DRIVING
4 SAFE_STOP
5 FAULT
6 ESTOP
```

fault:

```text
bit  0 COMM_TIMEOUT
bit  1 CRC_ERROR
bit  2 BAD_COMMAND
bit  3 ENCODER_INVALID
bit  4 MOTOR_STALL
bit  5 DIRECTION
bit  6 CONTROL_OVERRUN
bit  7 IMU_LOST
bit  8 RANGE_LOST
bit  9 STEERING_INVALID
bit 10 ESTOP_ACTIVE
bit 11 INTERNAL
bit 12 COMMAND_LIMIT
bit 13 UART_RX_OVERFLOW
bit 14 UART_TX_ERROR
bit 15 SENSOR_STALE
bit 16 OBSTACLE_NEAR
```

### 6. 반드시 구현할 상태 흐름

연결/재연결:

```text
UART open
→ RX parser 시작
→ neutral CMD_DRIVE(0,0,0)
→ last_drive_seq 일치 확인
→ READY 확인
```

주행:

```text
최신 planner 명령 저장
→ 독립 20 Hz scheduler가 CMD_DRIVE 송신
→ last_drive_seq와 state/fault 감시
```

정상 종료:

```text
CMD_STOP
→ COMMAND_RESULT ACCEPTED 확인
→ UART close
```

통신 timeout/SAFE_STOP 복구:

```text
주행 명령 중지
→ fault와 상태 확인
→ neutral CMD_DRIVE(0,0,0)
→ READY 확인
→ 필요 시 새 주행 명령 시작
```

planner/ROS 명령이 자체 timeout을 넘기면 이전 목표를 계속 보내지 말고
`CMD_STOP` reason 5를 보내라.

### 7. ROS 또는 상위 모듈 매핑

현재 프로젝트 구조를 분석한 후 이름은 기존 규칙에 맞추되 최소한 다음
인터페이스를 제공해라.

- 상위 planner의 속도/조향 명령 → `CMD_DRIVE`
- STM drive telemetry → 상태/진단 topic
- STM odometry → odometry topic
- fault event → 즉시 fault/diagnostic publish
- command result → stop/reset service 또는 action 결과
- 연결 상태, RX rate, CRC error, last sequence lag 진단

단위 변환:

```python
x_m = x_mm / 1000.0
y_m = y_mm / 1000.0
yaw_rad = math.radians(yaw_mdeg / 1000.0)
linear_x_mps = linear_speed_mm_s / 1000.0
angular_z_rad_s = math.radians(yaw_rate_mdeg_s / 1000.0)
```

ROS `frame_id`, `child_frame_id`, TF 발행 주체와 covariance는 기존 Jetson
프로젝트를 분석한 뒤 중복 발행이 없도록 결정해라.

### 8. 구현 품질 요구사항

- serial port owner는 하나만 둔다.
- RX thread/task가 blocking TX 때문에 멈추지 않게 한다.
- planner callback에서 serial write를 직접 반복하지 말고 최신 명령을
  thread-safe하게 저장한 뒤 20 Hz scheduler가 보낸다.
- outbound event 명령과 heartbeat가 하나의 SEQ generator를 공유하게 한다.
- reconnect 시 parser buffer와 pending request를 안전하게 초기화한다.
- raw serial logging은 옵션으로 제공하되 제어 loop를 방해하지 않게 한다.
- 잘못된 payload length의 frame을 unpack하지 않는다.
- signed/unsigned와 little-endian을 정확히 지킨다.
- `mcu_time_ms` uint32 wrap과 STM32 reset을 구분할 수 있게 한다.
- 상위 프로세스 종료 시 가능하면 `CMD_STOP`을 보내는 종료 handler를 둔다.

### 9. 테스트 요구사항

먼저 제공된 레퍼런스 시험을 실행해라.

```bash
python3 -m pip install pyserial
python3 tools/jetson_uart_echo_test.py --self-test
```

반드시 통과:

```text
Protocol self-test: PASS
Golden frames GF-01..GF-07: PASS
```

하드웨어 연결 후:

```bash
python3 tools/jetson_uart_echo_test.py \
  --port /dev/ttyUSB0 \
  --text JETSON
```

합격:

```text
Jetson <-> STM32 UART5 echo: PASS
```

새로 구현한 Jetson 코드에는 최소 다음 unit test를 추가해라.

1. CRC golden vector `123456789 → 0x29B1`
2. Golden Frame GF-01~GF-07
3. 한 frame이 여러 read로 분할되는 경우
4. 여러 frame이 한 read에 들어오는 경우
5. garbage 뒤 SOF 재탐색
6. CRC 오류 뒤 다음 정상 frame 복구
7. payload 내부 `AA 55`
8. signed speed/steering/odometry decode
9. SEQ 255→0 wrap
10. command result request ID/SEQ 매칭
11. odometry status/source 유효성 판정
12. STM32 reset과 timestamp wrap 처리

차량을 뒤집어 바퀴를 띄운 상태의 통합 합격 기준:

```text
neutral → READY
forward → DRIVING
left command → positive odometry yaw
right command → negative odometry yaw
reverse → negative distance
heartbeat 중단 → 약 300 ms 뒤 SAFE_STOP/COMM_TIMEOUT
neutral → READY 재무장
CMD_STOP → COMMAND_RESULT ACCEPTED
ODOMETRY 수신율 약 20 Hz
```

### 10. 현재 확정 하드웨어 값

```text
wheelbase = 0.135 m
front steering pivot track = 0.085 m
tire nominal diameter = 64 mm
tire nominal circumference = 201.06 mm
encoder = 823 counts/rev
speed range = -1565~+1565 mm/s
motor output ceiling = 95% PWM
ultrasonic local stop enter = 0.20 m
ultrasonic local stop clear = 0.30 m, 3 consecutive valid samples
steering limit = -28.69~+19.55 deg
servo center = 1215 us
```

7점 LUT:

```text
+19.55 deg → 750 us
+14.18 deg → 905 us
+10.27 deg → 1060 us
  0.00 deg → 1215 us
 -7.09 deg → 1370 us
-17.56 deg → 1525 us
-28.69 deg → 1680 us
```

### 11. 아직 가정하면 안 되는 것

- 조향센서가 있다는 가정
- IMU yaw가 들어온다는 가정
- `TELEMETRY_DRIVE.yaw_cdeg`가 유효하다는 가정
- 초음파 방향 채널이 유효하다는 가정
- 공중 바퀴 오도메트리가 바닥 실제 위치와 동일하다는 가정
- 물리 E-stop이 구현됐다는 가정
- UART pose reset 명령이 있다는 가정
- Jetson serial device가 반드시 `/dev/ttyUSB0`이라는 가정

### 12. 구현 후 보고 형식

작업 후 다음 순서로 보고해라.

1. 기존 Jetson 프로젝트 분석 결과
2. 선택한 UART/ROS 구조와 이유
3. 수정한 파일
4. 송신 명령과 수신 telemetry 매핑
5. state/watchdog/reconnect 처리
6. unit test 결과
7. Golden Frame 결과
8. UART5 Echo 결과
9. 바퀴 공중 통합 시험 결과
10. 남아 있는 실제 하드웨어 미검증 사항

## 요청 끝
