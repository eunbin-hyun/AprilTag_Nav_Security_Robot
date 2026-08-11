# Jetson 담당자 인수인계 — STM32 주행 제어 V3

작성 기준일: 2026-07-31  
대상 프로젝트: `ssacurity-stm32-drive`  
MCU/보드: STM32F429ZI / STM32F429I-DISC1  
Wire protocol version: `0x02`

이 문서는 Jetson 담당자가 STM32 주행 제어기와 처음 연결할 때 필요한
배선, 프레임, 명령 송신 규칙, 텔레메트리 해석, 시험 순서와 현재 제한사항을
한 문서에 모은 인수인계 자료다.

## 1. 먼저 전달할 파일

Jetson 담당자에게 다음 파일을 함께 전달한다.

| 파일 | 용도 |
| --- | --- |
| `docs/jetson_handover_2026-07-31.md` | 처음 읽는 인수인계 문서 |
| `docs/jetson_ai_implementation_prompt.md` | Jetson 파트 AI에 그대로 전달할 구현 요청서 |
| `docs/jetson_stm32_uart_interface_v3.md` | 상세 프로토콜 기준 |
| `tools/jetson_uart_echo_test.py` | Linux/Jetson UART Echo와 Golden Frame 시험 |
| `tools/uart_protocol_test.py` | 모든 메시지의 Python 인코더·디코더 참조 구현 |
| `Core/Inc/comm_protocol.h` | STM32 측 ID, 상태, fault 원본 정의 |

`uart_protocol_test.py`는 PC 시험 도구이면서 현재 wire format을 그대로
구현한 Python 레퍼런스다. Jetson 프로그램에서 파서를 새로 작성할 때 이
파일의 `crc16_ccitt_false`, `encode_frame`, `pop_frame` 및 telemetry
decoder를 기준으로 삼는다.

Jetson 담당자가 AI를 이용해 구현한다면
`docs/jetson_ai_implementation_prompt.md`의 `요청 시작`부터 `요청 끝`까지를
위 파일들과 함께 그대로 전달한다.

## 2. 현재 완료 상태

- 전진·후진 속도 명령 처리 완료
- MG996R 조향 명령과 7점 LUT 보간 완료
- 엔코더 속도·거리 계산 완료
- 명령 조향각 기반 아커만 오도메트리 완료
- `TELEMETRY_DRIVE`와 `TELEMETRY_ODOMETRY` 20 Hz 송신 완료
- 300 ms 통신 watchdog과 neutral 재무장 완료
- PC-as-Jetson COM11 통합 시험 완료
- 오도메트리 전진·좌회전·우회전·후진 부호 시험 완료

2026-07-30 실물 시험 결과:

```text
Command-estimated odometry scenario: PASS
Telemetry monitor: PASS
Frames received in 10 s:
DRIVE=208, RANGE=104, ODOMETRY=207
```

`ODOMETRY=207/10s`는 설계 주기인 약 20 Hz와 일치한다.

## 3. UART5 물리 연결

| Jetson 또는 3.3 V USB-UART | 방향 | STM32F429I-DISC1 |
| --- | :---: | --- |
| TXD | → | PD2 / UART5_RX / P1-40 |
| RXD | ← | PC12 / UART5_TX / P1-43 |
| GND | ↔ | GND / P1-63 또는 P1-64 |
| VCC | — | 연결하지 않음 |

통신 설정:

```text
115200 bps
8 data bits
no parity
1 stop bit
flow control 없음
3.3 V TTL
```

주의:

- TX와 RX는 교차 연결한다.
- 반드시 GND를 공통으로 연결한다.
- USB-UART의 VCC는 STM32에 연결하지 않는다.
- Jetson 직결 UART와 USB-UART TX를 동시에 STM32 RX에 연결하지 않는다.
- Jetson의 실제 `/dev/tty...` 장치명은 보드 모델과 연결 방식에 따라
  달라지므로 현장에서 확인한다.

## 4. STM32 통신 포트 전환

현재 PC COM11 시험용 설정은 다음과 같다.

```c
#define VEHICLE_COMM_USE_STLINK_VCP 1U
```

실제 Jetson UART5 연결 전 다음과 같이 바꾸고 STM32를 다시 빌드·플래시한다.

```c
#define VEHICLE_COMM_USE_STLINK_VCP 0U
```

위치는 `App/Inc/vehicle_config.h`다. 이 변경은 통신 물리 포트만
USART1에서 UART5로 바꾸며 wire protocol과 제어 로직은 바꾸지 않는다.

## 5. 공통 프레임

```text
[AA][55][02][MSG_ID][SEQ][LEN][PAYLOAD...][CRC_L][CRC_H]
```

| 필드 | 크기 | 설명 |
| --- | ---: | --- |
| SOF | 2 | 고정 `AA 55` |
| VERSION | 1 | 고정 `02` |
| MSG_ID | 1 | 메시지 종류 |
| SEQ | 1 | 송신자별 공통 `uint8`, 0~255 순환 |
| LEN | 1 | payload 길이, 최대 64 |
| PAYLOAD | 0~64 | 메시지별 데이터 |
| CRC | 2 | little-endian |

CRC:

```text
CRC-16/CCITT-FALSE
poly   = 0x1021
init   = 0xFFFF
xorout = 0
입력 범위 = VERSION부터 PAYLOAD 마지막 바이트까지
```

모든 다중 바이트 정수와 CRC는 little-endian이다.

## 6. SEQ 규칙

Jetson은 메시지 종류와 관계없이 송신 프레임마다 하나의 SEQ를 증가시킨다.

```text
... FE → FF → 00 → 01 ...
```

주의:

- 같은 SEQ를 다시 보내면 duplicate로 실행되지 않는다.
- 너무 오래된 SEQ도 실행되지 않는다.
- `CMD_DRIVE` 다음 `CMD_STOP`에 같은 SEQ를 사용하면 STOP이 실행되지 않을
  수 있으므로 모든 송신 프레임에서 반드시 증가시킨다.
- Jetson→STM32 유효 프레임이 350 ms 이상 없으면 다음 첫 프레임을 새
  세션으로 수락한다.

## 7. 메시지 ID

| ID | 이름 | 방향 | Payload | 주기 |
| ---: | --- | --- | ---: | --- |
| `0x10` | `CMD_DRIVE` | Jetson → STM32 | 5 | 20 Hz |
| `0x11` | `CMD_STOP` | Jetson → STM32 | 1 | 이벤트 |
| `0x12` | `CMD_RESET_FAULT` | Jetson → STM32 | 4 | 이벤트 |
| `0x80` | `TELEMETRY_DRIVE` | STM32 → Jetson | 26 | 20 Hz |
| `0x81` | `FAULT_EVENT` | STM32 → Jetson | 14 | 상태 변경 |
| `0x82` | `COMMAND_RESULT` | STM32 → Jetson | 4 | 단발 명령 응답 |
| `0x83` | `TELEMETRY_IMU` | STM32 → Jetson | 예약 | 미구현 |
| `0x84` | `TELEMETRY_RANGE` | STM32 → Jetson | 13 | 10 Hz |
| `0x85` | `TELEMETRY_ODOMETRY` | STM32 → Jetson | 36 | 20 Hz |
| `0xF0` | `DIAG_ECHO_REQ` | Jetson → STM32 | 0~31 | 연결 시험 |
| `0xF1` | `DIAG_ECHO_RESP` | STM32 → Jetson | 1~32 | 연결 시험 |

`0x82`는 V3에서 `COMMAND_RESULT`다. 구형 V1의 `0x82
TELEMETRY_ODOMETRY`로 해석하면 안 된다.

## 8. Jetson이 보내야 하는 명령

### 8.1 CMD_DRIVE — `0x10`

Python struct:

```python
struct.pack("<hhB", speed_mm_s, steering_cdeg, drive_enable)
```

| Offset | 형식 | 필드 | 단위/범위 |
| ---: | --- | --- | --- |
| 0 | `int16` | target speed | -1565~+1565 mm/s |
| 2 | `int16` | target steering | -2869~+1955 cdeg |
| 4 | `uint8` | drive enable | 0 또는 1 |

부호:

- 속도 `+`: 전진
- 속도 `-`: 후진
- 조향 `+`: 왼쪽
- 조향 `-`: 오른쪽

유효한 중립/재무장 명령은 정확히 다음 값이다.

```text
speed=0, steering=0, drive_enable=0
```

`drive_enable=0`이면서 속도나 조향이 0이 아니면 잘못된 명령이다.

주행 중에는 같은 목표를 유지하더라도 새 SEQ로 50 ms마다, 즉 20 Hz로
계속 전송한다. 300 ms 동안 수락 가능한 새 `CMD_DRIVE`가 없으면 STM32가
모터를 정지하고 `SAFE_STOP`으로 전환한다.

`CMD_DRIVE`에는 별도 `COMMAND_RESULT`가 없다. 다음 두 값으로 수락 여부를
확인한다.

```text
TELEMETRY_DRIVE.last_drive_seq
TELEMETRY_ODOMETRY.last_drive_seq
```

### 8.2 CMD_STOP — `0x11`

Python struct:

```python
struct.pack("<B", stop_reason)
```

| 값 | 의미 |
| ---: | --- |
| 0 | operator |
| 1 | mission complete |
| 2 | obstacle |
| 3 | remote request |
| 4 | internal |
| 5 | ROS command timeout |
| 6 | link shutdown |

STM32는 `COMMAND_RESULT`를 돌려준다. STOP 이후 다시 주행하려면 neutral
재무장을 먼저 보내야 한다.

### 8.3 CMD_RESET_FAULT — `0x12`

Python struct:

```python
struct.pack("<I", acknowledged_fault_bits)
```

`SAFE_STOP`, `FAULT`, `ESTOP` 상태에서만 수락한다. 모든 fault를 무조건
지우기보다 Jetson이 확인한 bit만 1로 보내는 방식을 권장한다.

## 9. STM32가 보내는 텔레메트리

### 9.1 TELEMETRY_DRIVE — `0x80`

Python struct:

```python
struct.unpack("<IhhhhhihBBI", payload)
```

| 필드 | 단위 |
| --- | --- |
| mcu_time_ms | ms, `uint32` |
| target_speed_mm_s | mm/s |
| measured_speed_mm_s | mm/s |
| motor_duty_permille | 0.1% |
| steering_cmd_cdeg | 0.01 deg |
| steering_feedback_cdeg | 0.01 deg |
| encoder_count | count |
| yaw_cdeg | 0.01 deg |
| drive_state | 상태 enum |
| last_drive_seq | 마지막 수락 CMD_DRIVE SEQ |
| active_fault_bits | fault bit mask |

현재 주의점:

- 조향센서가 없으므로 `steering_feedback_cdeg=0`은 유효 센서값이 아니다.
- IMU가 없으므로 `yaw_cdeg=0`은 자리표시자다.
- yaw는 `TELEMETRY_ODOMETRY.yaw_mdeg`를 사용한다.

### 9.2 TELEMETRY_ODOMETRY — `0x85`

Python struct:

```python
struct.unpack("<IiiiihihiHBB", payload)
```

| 필드 | 형식 | 단위/설명 |
| --- | --- | --- |
| mcu_time_ms | `uint32` | ms |
| x_mm | `int32` | 출발점 기준 전방 좌표 |
| y_mm | `int32` | 출발점 기준 왼쪽 좌표 |
| yaw_mdeg | `int32` | 왼쪽 회전 양수, 0.001 deg |
| distance_mm | `int32` | signed 누적 엔코더 거리 |
| linear_speed_mm_s | `int16` | signed 속도 |
| yaw_rate_mdeg_s | `int32` | 0.001 deg/s |
| steering_cdeg | `int16` | LUT 등가 중심 조향각 |
| curvature_micro_per_m | `int32` | `1e-6 / m` |
| status_flags | `uint16` | 오도메트리 상태 |
| steering_source | `uint8` | 조향 출처 |
| last_drive_seq | `uint8` | 마지막 수락 CMD_DRIVE |

좌표계:

```text
+x = 차량 전방
+y = 차량 왼쪽
+yaw = 좌회전
```

오도메트리 status:

| Bit | 이름 | 의미 |
| ---: | --- | --- |
| 0 | VALID | 현재 update 유효 |
| 1 | ENCODER_CALIBRATED | 엔코더 보정 적용 |
| 2 | GEOMETRY_CALIBRATED | 차량 형상값 적용 |
| 3 | STEERING_ESTIMATED | 조향 명령 추정값 사용 |
| 4 | IMU_FUSED | IMU 융합됨 |
| 5 | INPUT_INVALID | 입력 또는 형상 오류 |

steering source:

| 값 | 의미 |
| ---: | --- |
| 0 | NONE |
| 1 | SENSOR |
| 2 | COMMAND_ESTIMATE |

현재 정상 조건:

```text
VALID=1
ENCODER_CALIBRATED=1
GEOMETRY_CALIBRATED=1
STEERING_ESTIMATED=1
IMU_FUSED=0
INPUT_INVALID=0
steering_source=2
```

Jetson은 bit 0~3을 확인하고 bit 5가 0일 때만 pose를 유효하게 사용한다.
조향센서가 없으므로 이 pose는 실제 feedback이 아니라 명령 기반 추정이다.

추가 주의:

- `yaw_mdeg`는 -180~+180 deg 부근에서 wrap된다.
- STM32가 리셋되면 pose가 0으로 초기화된다.
- UART로 pose를 리셋하는 명령은 아직 없다.
- Jetson에서 새 local origin이 필요하면 최초 수신 pose를 빼서 사용한다.
- 공중 바퀴 시험 pose는 가상 이동 결과이며 바닥 정확도를 보장하지 않는다.

### 9.3 TELEMETRY_RANGE — `0x84`

Python struct:

```python
struct.unpack("<IHHHHB", payload)
```

현재 네 채널 모두:

```text
distance=0xFFFF
valid_mask=0
```

HC-SR04가 로컬 안전 제어에는 사용되지만 V3의 front-left/front-right 물리
채널이 아직 확정되지 않아 Jetson용 거리 프레임에는 매핑하지 않았다.
Jetson은 `valid_mask` bit가 0인 값을 장애물 거리로 사용하면 안 된다.

### 9.4 FAULT_EVENT — `0x81`

Python struct:

```python
struct.unpack("<IIIBB", payload)
```

순서:

```text
occurred_at_ms
active_fault_bits
latched_fault_bits
action
state
```

action 값:

| 값 | 이름 |
| ---: | --- |
| 0 | REPORT_ONLY |
| 1 | HOLD_NEUTRAL |
| 2 | SAFE_STOP |
| 3 | OUTPUT_DISABLE |
| 4 | LATCHED_STOP |

### 9.5 COMMAND_RESULT — `0x82`

Python struct:

```python
struct.unpack("<BBBB", payload)
```

순서:

```text
request_message_id
request_sequence
result_code
state
```

result code:

| 값 | 이름 |
| ---: | --- |
| 0 | ACCEPTED |
| 1 | INVALID_STATE |
| 2 | OUT_OF_RANGE |
| 3 | FAULT_ACTIVE |
| 4 | UNSUPPORTED |
| 5 | INVALID_VALUE |
| 6 | DUPLICATE |
| 7 | NOT_ARMED |

## 10. 상태와 fault

drive state:

| 값 | 이름 |
| ---: | --- |
| 0 | BOOT |
| 1 | SELF_TEST |
| 2 | READY |
| 3 | DRIVING |
| 4 | SAFE_STOP |
| 5 | FAULT |
| 6 | ESTOP |

fault bits:

| Bit | 이름 |
| ---: | --- |
| 0 | COMM_TIMEOUT |
| 1 | CRC_ERROR |
| 2 | BAD_COMMAND |
| 3 | ENCODER_INVALID |
| 4 | MOTOR_STALL |
| 5 | DIRECTION |
| 6 | CONTROL_OVERRUN |
| 7 | IMU_LOST |
| 8 | RANGE_LOST |
| 9 | STEERING_INVALID |
| 10 | ESTOP_ACTIVE |
| 11 | INTERNAL |
| 12 | COMMAND_LIMIT |
| 13 | UART_RX_OVERFLOW |
| 14 | UART_TX_ERROR |
| 15 | SENSOR_STALE |
| 16 | OBSTACLE_NEAR |

`telemetry-monitor`처럼 heartbeat를 보내지 않고 수신만 하면 약 300 ms 뒤
`SAFE_STOP + COMM_TIMEOUT`이 나타나는 것이 정상이다.

## 11. HC-SR04 로컬 안전 동작

STM32 로컬 안전 계층은 Jetson 명령보다 우선한다.

```text
거리 < 0.20 m  → STOP
거리 ≥ 0.30 m  → 3회 연속 clear 후 STOP 해제
거리 < 0.60 m  → CAUTION
거리 ≥ 0.65 m  → CAUTION 해제
```

센서 stale 기준은 200 ms다. `NO_ECHO`는 현재 최대거리 방향으로 처리하지만
진짜 timeout/out-of-range/stale은 안전정지를 요구할 수 있다.
후방 센서는 없으며 현재 설정에서는 후진 명령을 허용한다.
거리 STOP이 발생하면 `TELEMETRY_DRIVE.active_fault_bits`와
`FAULT_EVENT`에 `OBSTACLE_NEAR`가 설정된다. 0.20 m는 최후 강제정지
임계값이며 실제 바닥 정지거리 보장값은 아니다.

## 12. 첫 연결 시험 순서

### 12.1 Jetson 준비

```bash
python3 -m pip install pyserial
```

USB-UART 장치 확인 예시:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

장치 접근 권한이 없다면 사용 중인 Linux 배포판의 serial 그룹과 udev
설정을 확인한다.

### 12.2 소프트웨어 자체 시험

```bash
python3 tools/jetson_uart_echo_test.py --self-test
```

합격:

```text
Protocol self-test: PASS
Golden frames GF-01..GF-07: PASS
```

### 12.3 UART5 Echo

STM32가 `VEHICLE_COMM_USE_STLINK_VCP=0U`로 플래시됐는지 확인한 뒤:

```bash
python3 tools/jetson_uart_echo_test.py \
  --port /dev/ttyUSB0 \
  --text JETSON
```

합격:

```text
Jetson <-> STM32 UART5 echo: PASS
```

### 12.4 운영 경로 시험

처음에는 차량을 뒤집어 구동 바퀴가 모두 공중에 있는 상태에서 시행한다.

```bash
python3 tools/uart_protocol_test.py telemetry-monitor \
  --port /dev/ttyUSB0 \
  --seconds 10
```

수신 전용 monitor이므로 `SAFE_STOP + COMM_TIMEOUT`은 정상이다. 다음이
나와야 한다.

```text
Telemetry monitor: PASS
ODOMETRY 약 200 frames / 10 s
Odometry status: PASS
```

운영 주행 시나리오는 Jetson에서 같은 도구를 실행할 수 있는 환경이라면:

```bash
python3 tools/uart_protocol_test.py odometry-test \
  --port /dev/ttyUSB0 \
  --wheels-off-ground
```

## 13. Jetson 제어 루프 권장 순서

```text
1. UART open
2. RX parser 계속 실행
3. neutral CMD_DRIVE(0,0,0) 송신
4. TELEMETRY_DRIVE state=READY 및 last_drive_seq 확인
5. 주행 중 CMD_DRIVE를 20 Hz로 반복
6. DRIVE/ODOMETRY telemetry의 last_drive_seq 감시
7. fault/state가 비정상이면 상위 주행 계획 중지
8. 임무 종료 시 CMD_STOP 송신
9. COMMAND_RESULT ACCEPTED 확인
10. UART close
```

프로세스가 비정상 종료될 경우 STM32의 300 ms watchdog이 모터를 정지하지만,
Jetson 프로그램도 종료 처리에서 가능하면 `CMD_STOP`을 보내야 한다.

## 14. ROS/상위 소프트웨어 매핑 시 주의

권장 단위 변환:

```text
x_m = x_mm / 1000.0
y_m = y_mm / 1000.0
yaw_rad = radians(yaw_mdeg / 1000.0)
linear_x_mps = linear_speed_mm_s / 1000.0
angular_z_rad_s = radians(yaw_rate_mdeg_s / 1000.0)
```

`nav_msgs/Odometry`로 변환할 경우 frame 이름과 covariance는 Jetson/ROS
팀에서 확정한다. 현재 yaw는 IMU 융합값이 아니고 조향도 명령 기반이므로
센서 융합 오도메트리와 동일한 낮은 covariance를 부여하면 안 된다.

## 15. 현재 하드웨어 보정값

| 항목 | 값 |
| --- | ---: |
| 휠베이스 | 0.135 m |
| 앞바퀴 조향축 중심 간 거리 | 0.085 m |
| 타이어 명목 지름 | 64 mm |
| 타이어 명목 둘레 | 201.06 mm |
| 엔코더 | 823 count/rev |
| 속도 명령 범위 | -1565~+1565 mm/s |
| 모터 출력 상한 | 절댓값 95% PWM |
| 왼쪽 최대 중심 등가각 | +19.55 deg |
| 오른쪽 최대 중심 등가각 | -28.69 deg |
| 서보 중앙 | 1215 us |

조향 LUT:

| 중심 등가각 | PWM |
| ---: | ---: |
| +19.55 deg | 750 us |
| +14.18 deg | 905 us |
| +10.27 deg | 1060 us |
| 0.00 deg | 1215 us |
| -7.09 deg | 1370 us |
| -17.56 deg | 1525 us |
| -28.69 deg | 1680 us |

측정점 사이의 각도는 구간별 선형 보간한다.

## 16. 아직 완료되지 않은 항목

- 실제 Jetson UART5 물리 Echo
- 실제 Jetson 장치명과 직접 UART/USB-UART 방식 확정
- 바닥에서 직선거리·회전반경·오도메트리 오차 측정
- 타이어 하중 상태 실구름 둘레 보정
- HC-SR04의 V3 방향 채널 매핑
- 최종 장애물 정지거리 확정
- IMU 장착, 포맷 확정 및 yaw 융합
- 물리 E-stop 입력과 독립 모터 전원 차단
- UART 기반 STM32 pose reset 명령

## 17. 문제 발생 시 빠른 확인

| 증상 | 먼저 확인할 것 |
| --- | --- |
| 아무 응답 없음 | UART5 빌드인지, TX/RX 교차, 공통 GND, 장치명 |
| Echo만 안 됨 | `--self-test`, baud 115200, 3.3 V TTL |
| `SAFE_STOP/COMM_TIMEOUT` | 새 SEQ의 CMD_DRIVE가 20 Hz로 오는지 |
| 주행 명령 무시 | 먼저 neutral 재무장했는지 |
| STOP 무시 | CMD_DRIVE와 같은 SEQ를 재사용하지 않았는지 |
| 명령 범위 fault | 속도 ±1565, 조향 -28.69~+19.55 확인 |
| odometry 없음 | `0x85`, payload 36 byte로 파싱하는지 |
| odometry invalid | status bit 0~3과 bit 5 확인 |
| DRIVE yaw가 0 | 정상; IMU 없음. ODOM yaw 사용 |
| RANGE가 전부 invalid | 정상; V3 방향 채널 아직 미매핑 |

## 18. 인수인계 완료 기준

내일 현장에서 다음 네 줄을 확인하면 UART/프로토콜 인수인계는 완료로 본다.

```text
Golden frames GF-01..GF-07: PASS
Jetson <-> STM32 UART5 echo: PASS
TELEMETRY_ODOMETRY 약 20 Hz 수신
neutral → READY → DRIVING → CMD_STOP → SAFE_STOP 상태 전이 확인
```
