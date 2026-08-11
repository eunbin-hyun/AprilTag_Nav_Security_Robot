# Jetson–STM32 UART 연동 구현 기준 V3.0

이 문서는 Jetson 팀이 전달한 `Jetson–STM32 UART 연동 명세 V3.0`과
`Jetson_STM32_통신명세_V2.3_변경사항.md`를 STM32 프로젝트에 적용한
결과를 기록한다. Wire protocol version은 `0x02`다.

## 1. 물리 연결

| Jetson/USB-UART | 방향 | STM32F429I-DISC1 |
|---|:---:|---|
| TXD | → | PD2 / UART5_RX / P1-40 |
| RXD | ← | PC12 / UART5_TX / P1-43 |
| GND | ↔ | GND / P1-63 또는 P1-64 |

- 115200 bps, 8-N-1, flow control 없음, 3.3 V TTL
- TX/RX는 교차하고 GND는 공통으로 연결한다.
- USB-UART의 VCC는 연결하지 않는다.
- Jetson J12 직결과 USB-UART를 동시에 연결하지 않는다.

## 2. Frame

```text
[AA][55][02][MSG_ID][SEQ][LEN][PAYLOAD 0..64][CRC_L][CRC_H]
```

- CRC-16/CCITT-FALSE: poly `0x1021`, init `0xFFFF`, xorout `0`
- CRC 범위: `VERSION`부터 `PAYLOAD` 끝까지
- 다중 바이트 정수와 CRC는 little-endian이다.
- 송신자는 메시지 종류와 무관한 단일 `uint8_t` TX SEQ를 사용한다.
- 수신자는 duplicate와 old frame을 재실행하지 않는다.
- 유효 frame을 350 ms 이상 받지 못한 뒤의 첫 frame은 새 세션으로
  수락한다.

Parser는 256-byte 지속 버퍼에서 SOF를 재탐색한다. version, length, CRC
오류 시 후보 SOF의 첫 바이트만 버리며, 불완전 frame은 100 ms 후 같은
방식으로 복구한다.

## 3. 메시지

| ID | 이름 | 방향 | Payload | 주기 |
|---:|---|---|---:|---|
| `0x10` | `CMD_DRIVE` | Jetson → STM32 | 5 | 20 Hz |
| `0x11` | `CMD_STOP` | Jetson → STM32 | 1 | 이벤트 |
| `0x12` | `CMD_RESET_FAULT` | Jetson → STM32 | 4 | 이벤트 |
| `0x80` | `TELEMETRY_DRIVE` | STM32 → Jetson | 26 | 20 Hz |
| `0x81` | `FAULT_EVENT` | STM32 → Jetson | 14 | 상태 변화 |
| `0x82` | `COMMAND_RESULT` | STM32 → Jetson | 4 | 단발 명령 |
| `0x83` | `TELEMETRY_IMU` | STM32 → Jetson | 예약 | — |
| `0x84` | `TELEMETRY_RANGE` | STM32 → Jetson | 13 | 10 Hz |
| `0x85` | `TELEMETRY_ODOMETRY` | STM32 → Jetson | 36 | 20 Hz |
| `0xF0` | `DIAG_ECHO_REQ` | Jetson → STM32 | 0..31 | 개발 |
| `0xF1` | `DIAG_ECHO_RESP` | STM32 → Jetson | 1..32 | 개발 |
| `0xF2` | `DIAG_MOTOR_TEST_REQ` | PC → STM32 | 4 | 벤치 |
| `0xF3` | `DIAG_MOTOR_TEST_RESP` | STM32 → PC | 6 | 벤치 |

기존 V1의 `0x82 TELEMETRY_ODOMETRY`와 `0xF4..0xF5` PID 진단은 V3 활성 UART
프로토콜에서 사용하지 않는다. `0xF2..0xF3` 모터 진단은 실차 배선 확인을 위해
V2 frame 형식으로 복구했으며 ±20~95%, 100~10000ms 범위에서만 동작한다.

### Echo

Echo response header의 SEQ는 STM32 자체 TX 카운터다. Payload는
`[요청 SEQ][요청 data...]`다. 요청 payload 내부의 `AA 55`도 그대로
보존한다.

### TELEMETRY_DRIVE

26-byte payload 순서는 다음과 같다.

```text
<IhhhhhihBBI
mcu_time_ms
target_speed_mm_s
measured_speed_mm_s
motor_duty_permille
steering_cmd_cdeg
steering_feedback_cdeg
encoder_count
yaw_cdeg
drive_state
last_drive_seq
active_fault_bits
```

IMU가 없으므로 `yaw_cdeg=0`이다. `last_drive_seq`는 마지막으로 실제
수락한 `CMD_DRIVE`의 SEQ이며, 거부된 명령에서는 갱신하지 않는다.

현재 HC-SR04는 전방 장착만 확인되고 좌/우 채널 위치가 정해지지 않았다.
따라서 `TELEMETRY_RANGE`는 네 거리를 `0xFFFF`, `valid_mask=0`으로
전송한다. 좌/우 물리 위치를 확정한 뒤에만 해당 채널을 활성화한다.

STM32 `SafetyTask`의 전방 센서 임계값(정지 진입 0.20 m)은
하드웨어 보호를 임의로 제거하지 않기 위해 유지했다. 따라서 Range frame이
invalid여도 로컬 안전 계층은 센서 invalid/stale 또는 근접 장애물에서
`SAFE_STOP`을 요구할 수 있다. 이 값은 V3의 최종 `near_stop_mm`로 확정된
값이 아니며 실차 정지거리 시험 후 교체해야 한다.

### TELEMETRY_ODOMETRY

36-byte payload 순서는 다음과 같다.

```text
<IiiiihihiHBB
mcu_time_ms
x_mm
y_mm
yaw_mdeg
distance_mm
linear_speed_mm_s
yaw_rate_mdeg_s
steering_cdeg
curvature_micro_per_m
status_flags
steering_source
last_drive_seq
```

`steering_source=2`는 물리 조향센서가 아니라 7점 LUT의 등가 중심 조향
명령각을 사용했다는 뜻이다. 이 경우 `status_flags`의 bit 3
`STEERING_ESTIMATED`가 반드시 함께 설정된다.

| status bit | 의미 |
| ---: | --- |
| 0 | pose update valid |
| 1 | encoder calibrated |
| 2 | wheelbase/track geometry calibrated |
| 3 | steering command estimate used |
| 4 | IMU fused |
| 5 | input invalid |

현재는 IMU가 없으므로 bit 4는 0이다. 공중 바퀴 시험은 계산 부호와 UART
전송만 검증하며 실제 바닥 위치 정확도를 보증하지 않는다.

## 4. 안전 동작

- 신규·유효·수락 가능한 `CMD_DRIVE`만 300 ms watchdog을 갱신한다.
- 300 ms 만료 시 출력 중립, `COMM_TIMEOUT`, `SAFE_STOP`, 재무장 요구
- 재무장은 neutral `CMD_DRIVE(0,0,0)` 수락 후에만 해제
- `CMD_STOP`과 `CMD_RESET_FAULT`는 `COMMAND_RESULT`를 반환
- 현재 속도 명령 범위는 실측 지속속도 기준 `-1565~+1565 mm/s`이다.
- 현재 모터 출력 상한은 무부하 포화 측정점인 절댓값 95% PWM이다.
- 거리 0.20 m 미만에서 로컬 STOP하며 `OBSTACLE_NEAR` fault를 Jetson에
  보고한다. 0.30 m 이상 정상 샘플 3회 후 STOP을 해제한다.
- 조향 범위는 비대칭 `-28.69°~+19.55°`이며 7점 LUT 측정 범위와 같다.
- 위 한계보다 큰 `drive_enable=1` 명령은 거부
- UART `CMD_STOP`은 물리 E-stop을 대체하지 않는다.

물리 E-stop 입력은 현재 프로젝트에 할당된 핀이 없으므로
`COMM_STATE_ESTOP`과 fault bit 정의만 적용돼 있다. 실제 HW 경로와 GPIO가
확정되기 전에는 물리 E-stop 구현 완료로 간주하지 않는다.

## 5. PC-as-Jetson 운영 명령 시험

현재 `VEHICLE_COMM_USE_STLINK_VCP=1`이므로 PC 시험은 STM32F429I-DISC1의
ST-LINK USB VCP(COM 포트)에서 한다. 이 시험은 진단용 PWM 명령이 아니라
Jetson과 동일한 `CMD_DRIVE` 20 Hz heartbeat, 속도 PID, 서보 명령,
`TELEMETRY_DRIVE`, `CMD_STOP` 경로를 사용한다.

전제:

- 차량을 뒤집거나 구동 바퀴를 모두 지면에서 띄운다.
- HC-SR04 정면 30 cm 이내를 비운다.
- 12 V 모터 전원, 서보용 5 V 강압 전원, STM32 GND가 공통인지 확인한다.
- 업데이트된 펌웨어를 플래시한 뒤 보드를 한 번 리셋한다.

단일 명령 시험:

```powershell
py tools\uart_protocol_test.py drive-test `
  --port COM11 `
  --speed-mm-s 80 `
  --steering-deg 0 `
  --seconds 1.5 `
  --verify-watchdog `
  --wheels-off-ground
```

`steering-deg`는 중앙 등가 조향각이며 양수는 좌회전, 음수는 우회전이다.
도구는 neutral 재무장, 20 Hz 주행 명령, 텔레메트리 수락 확인, 엔코더
방향, 300 ms link-loss 정지, 재무장, `CMD_STOP`까지 검사한다.

전체 시나리오:

```powershell
py tools\uart_protocol_test.py drive-scenario `
  --port COM11 `
  --wheels-off-ground
```

순서는 전진 직진 → 전진 좌조향 → 전진 우조향 → 후진 직진 → heartbeat
중단이다. 각 단계에서 `PASS`가 출력되고 마지막에
`PC-as-Jetson full scenario: PASS`가 나와야 한다.

현재 조향센서는 미장착이다. 따라서 `steering_feedback_cdeg`는
0(유효하지 않음)이지만, 오도메트리는 실측한 7점 등가 중심각 LUT와
휠베이스 0.135 m를 이용해 명령 조향각 기반으로 적분한다.
`TELEMETRY_ODOMETRY`의 `steering_source=2`와
`STEERING_ESTIMATED`가 설정된 경우에만 이 추정 pose를 사용한다.
기구 유격·서보 미도달·타이어 미끄러짐은 감지할 수 없으므로 실제 바닥
정확도는 별도로 검증해야 한다.

## 6. Jetson/UART5 이관과 Echo 시험

COM VCP 시험이 끝나면 `App/Inc/vehicle_config.h`의
`VEHICLE_COMM_USE_STLINK_VCP`를 `0U`로 바꾸고 한 번 다시 빌드·플래시한다.
그 뒤 같은 wire protocol과 같은 명령 도구를 Jetson의 UART5 배선에서
사용한다. USART1 시험은 제어/프로토콜 전체를 검증하지만 PC12/PD2의 실제
배선까지 검증하지는 않으므로 UART5 Echo 1회는 생략하지 않는다.

```bash
python3 -m pip install pyserial
python3 tools/jetson_uart_echo_test.py --self-test
python3 tools/jetson_uart_echo_test.py \
  --port /dev/ttyUSB0 \
  --text JETSON
```

정상 결과:

```text
Protocol self-test: PASS
Golden frames GF-01..GF-07: PASS
Jetson <-> STM32 UART5 echo: PASS
```

## 7. 확정값과 미확정 항목

확정하여 코드에 반영한 값:

- 휠베이스: 0.135 m
- 앞바퀴 조향축 중심 간 거리: 0.085 m
- 엔코더: 823 count/바퀴 1회전
- 타이어 명목 지름: 64 mm, 명목 둘레: 201.06 mm
- 속도 명령 범위: -1565~+1565 mm/s
- 모터 출력 상한: 95% PWM
- 조향 명령 범위: -28.69~+19.55 deg
- 서보 7점 LUT: 750, 905, 1060, 1215, 1370, 1525, 1680 us
- 통신 watchdog: 300 ms
- 오도메트리 송신: `0x85`, 36 byte, 20 Hz

실제 차량에서 아직 검증하거나 확정해야 하는 항목:

- 하중 상태 타이어 실구름 둘레와 바닥 직선거리 오차
- 바닥 주행 회전반경과 명령 기반 yaw 오차
- 기구 유격, 서보 미도달 및 타이어 미끄러짐
- HC-SR04의 V3 front-left/front-right 채널 배치
- 0.20 m 강제정지값의 실제 바닥 정지거리 검증
- 물리 E-stop 입력과 독립 모터 에너지 차단 경로
- 실제 Jetson UART 장치 경로와 UART5 물리 Echo
- IMU 장착 후 yaw 보정과 `TELEMETRY_IMU` 포맷

엔코더 값은 정방향 3회전 +2473, 역방향 3회전 -2464를 평균해
823 count/바퀴 1회전으로 결정했다.
