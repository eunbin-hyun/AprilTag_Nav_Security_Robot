# 싸큐리티 Jetson-STM32 UART Protocol V1

> **폐기된 과거 규격:** 활성 펌웨어는 V3.0 / wire protocol `0x02`를
> 사용한다. 현재 기준은
> [`jetson_stm32_uart_interface_v3.md`](jetson_stm32_uart_interface_v3.md)다.
> 이 문서는 과거 벤치 시험 기록을 위해서만 보존한다.

기준일: 2026-07-26  
상태: STM32 파트 기준안  
적용 대상: Jetson Orin - STM32F429 주행 제어기

## 1. 설계 원칙

- Jetson은 목표 속도와 목표 조향을 생성한다.
- STM32는 명령의 무결성, 범위, 최신성을 검사한 뒤 저수준 제어에 전달한다.
- 손상되거나 알 수 없는 프레임은 실행하지 않는다.
- 마지막 유효 `CMD_DRIVE` 후 300ms가 지나면 통신 Timeout으로 처리한다.
- 부팅, Timeout, Stop 이후에는 중립 명령을 먼저 받아야 다시 주행 명령을 허용한다.
- 링크가 복구되어도 과거 명령을 재실행하거나 자동 출발하지 않는다.
- 물리 E-stop은 UART 메시지로 대체하지 않는다.
- BNO085, VL53L1X와 조향 피드백은 소유권이 확정되지 않아 V1에서 제외한다.

## 2. 물리 링크

| 항목 | PC 검증 단계 | 최종 Jetson 링크 |
|---|---|---|
| STM32 주변장치 | USART1 | UART5 |
| STM32 핀 | PA9 TX / PA10 RX | PC12 TX / PD2 RX |
| 변환 경로 | 보드 내장 ST-LINK VCP | CP2102 USB-TTL |
| 전기 레벨 | 보드 내부 연결 | 3.3V TTL |
| 설정 | 115200, 8 data, no parity, 1 stop, no flow control | 동일 |
| RX | DMA Circular + UART IDLE | 동일 |
| TX | DMA Normal | 동일 |

PC 시험에서 사용하는 통신 모듈은 최종 모듈과 같다. UART5를 CubeMX에
추가한 뒤 `main.c`의 아래 한 줄만 변경한다.

```c
CommService_Init(&huart1);  /* PC/ST-LINK */
CommService_Init(&huart5);  /* Jetson/CP2102 */
```

## 3. 프레임 구조

```text
[SOF1][SOF2][VERSION][MESSAGE_ID][SEQUENCE][LENGTH][PAYLOAD][CRC_LOW][CRC_HIGH]
```

| Offset | 크기 | 필드 | V1 값/의미 |
|---:|---:|---|---|
| 0 | 1 | SOF1 | `0xAA` |
| 1 | 1 | SOF2 | `0x55` |
| 2 | 1 | VERSION | `0x01` |
| 3 | 1 | MESSAGE_ID | 메시지 종류 |
| 4 | 1 | SEQUENCE | 송신 방향별 0~255 순환 번호 |
| 5 | 1 | LENGTH | Payload 바이트 수, 최대 64 |
| 6 | N | PAYLOAD | 메시지별 정의 |
| 6+N | 2 | CRC16 | Low byte 먼저 전송 |

모든 2바이트·4바이트 정수는 Little Endian이다. C 구조체의 메모리를 그대로
전송하지 않고 필드별로 직렬화한다.

### CRC

- 알고리즘: CRC-16/CCITT-FALSE
- Polynomial: `0x1021`
- Initial value: `0xFFFF`
- RefIn / RefOut: false / false
- XorOut: `0x0000`
- 계산 범위: `VERSION`부터 Payload 마지막 바이트
- 전송 순서: CRC Low byte, CRC High byte
- 표준 확인값: ASCII `"123456789"` -> `0x29B1`

### Sequence

- Jetson 송신과 STM32 송신은 각자 독립 Sequence를 사용한다.
- 정상 송신 때마다 1 증가하고 `255 -> 0`으로 순환한다.
- 동일 Sequence 또는 명백히 과거인 프레임은 다시 실행하지 않는다.
- 통신 Timeout 이후에는 수신 Sequence 동기화를 새로 시작한다.
- 진단 Response는 대응하는 Request의 Sequence를 그대로 돌려준다.

## 4. 메시지 목록

| ID | 방향 | 이름 | Payload 길이 |
|---:|---|---|---:|
| `0x10` | Jetson -> STM32 | `CMD_DRIVE` | 5 |
| `0x11` | Jetson -> STM32 | `CMD_STOP` | 1 |
| `0x12` | Jetson -> STM32 | `CMD_RESET_FAULT` | 4 |
| `0x80` | STM32 -> Jetson | `TELEMETRY_DRIVE` | 21 |
| `0x81` | STM32 -> Jetson | `FAULT_EVENT` | 12 |
| `0x82` | STM32 -> Jetson | `TELEMETRY_SENSOR` | 예약, 사용 금지 |
| `0xF0` | PC/Jetson -> STM32 | `DIAG_ECHO_REQUEST` | 0~32 |
| `0xF1` | STM32 -> PC/Jetson | `DIAG_ECHO_RESPONSE` | 요청과 동일 |
| `0xF2` | PC -> STM32 | `DIAG_MOTOR_TEST_REQUEST` | 4 |
| `0xF3` | STM32 -> PC | `DIAG_MOTOR_TEST_RESPONSE` | 5 |
| `0xF4` | PC -> STM32 | `DIAG_PID_TEST_REQUEST` | 4 |
| `0xF5` | STM32 -> PC | `DIAG_PID_TEST_RESPONSE` | 5 |

## 5. Payload 정의

### 5.1 CMD_DRIVE - 0x10

| Offset | 크기 | 형식 | 필드 | 단위/규칙 |
|---:|---:|---|---|---|
| 0 | 2 | `int16` | target_speed_mm_s | mm/s, 음수는 역방향 |
| 2 | 2 | `int16` | target_steering_cdeg | 0.01도, 부호는 양측 합의 좌표계 |
| 4 | 1 | `uint8` | drive_enable | 0 또는 1 |

`command_timeout`은 패킷 필드로 보내지 않고 STM32의 300ms 안전 상수로 둔다.
Manual/Auto 구분은 상위 임무 계층의 책임이므로 V1 저수준 Payload에서 제외한다.

실측 속도·조향 한계가 `CommService_SetDriveLimits()`로 설정되기 전에는 중립
명령 외의 `CMD_DRIVE`를 거부한다.

중립·재무장 명령:

```text
target_speed_mm_s = 0
target_steering_cdeg = 0
drive_enable = 0
```

부팅, Timeout, `CMD_STOP` 이후에는 이 중립 명령을 먼저 받아야 한다.

### 5.2 CMD_STOP - 0x11

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 1 | `uint8` | stop_reason |

| 값 | 의미 |
|---:|---|
| 0 | Operator |
| 1 | Mission complete |
| 2 | Obstacle |
| 3 | Remote request |
| 4 | Internal |

`CMD_STOP`은 운영 정지이며 물리 E-stop이 아니다. 수신 즉시 상위 제어에 중립
명령을 전달하고 재무장 상태로 들어간다.

### 5.3 CMD_RESET_FAULT - 0x12

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 4 | `uint32` | acknowledged_fault_bits |

이 메시지는 Fault 해제 요청일 뿐이다. 원인이 실제로 해소되고 해당 Fault가
원격 해제 가능한 경우에만 Safety Manager가 해제한다. 통신 모듈은 모터를
직접 재활성화하지 않는다.

### 5.4 TELEMETRY_DRIVE - 0x80

| Offset | 크기 | 형식 | 필드 | 단위 |
|---:|---:|---|---|---|
| 0 | 2 | `int16` | target_speed_mm_s | mm/s |
| 2 | 2 | `int16` | measured_speed_mm_s | mm/s |
| 4 | 4 | `int32` | encoder_count | 누적 Count |
| 8 | 2 | `int16` | motor_duty_permille | -1000~1000 |
| 10 | 2 | `int16` | steering_cdeg | 0.01도 |
| 12 | 1 | `uint8` | system_state | 아래 상태 표 |
| 13 | 4 | `uint32` | active_fault_bits | Fault bit mask |
| 17 | 4 | `uint32` | uptime_ms | STM32 부팅 후 ms |

상태 값:

| 값 | 상태 |
|---:|---|
| 0 | BOOT |
| 1 | SELF_TEST |
| 2 | READY |
| 3 | DRIVING |
| 4 | SAFE_STOP |
| 5 | FAULT |

권장 송신 주기는 10Hz이다.

### 5.5 FAULT_EVENT - 0x81

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 4 | `uint32` | active_fault_bits |
| 4 | 4 | `uint32` | latched_fault_bits |
| 8 | 4 | `uint32` | occurred_at_ms |

Fault 발생·해제 변화 시 송신한다. `TELEMETRY_DRIVE`에도 현재 Active Fault를
반복 포함해 이벤트 프레임 하나가 손실되어도 현재 상태를 복구할 수 있게 한다.

Fault bit:

| Bit | 이름 |
|---:|---|
| 0 | COMM_TIMEOUT |
| 1 | CRC_ERROR |
| 2 | BAD_COMMAND |
| 3 | ENCODER_INVALID |
| 4 | MOTOR_STALL |
| 5 | DIRECTION_FAULT |
| 6 | CONTROL_OVERRUN |
| 7 | IMU_LOST |
| 8 | RANGE_LOST |
| 9 | STEERING_INVALID |
| 10 | ESTOP_ACTIVE |
| 11 | INTERNAL_ERROR |

### 5.6 DIAG_ECHO - 0xF0 / 0xF1

`DIAG_ECHO_REQUEST`의 Payload를 변경하지 않고 `DIAG_ECHO_RESPONSE`로 돌려준다.
이 메시지는 배선뿐 아니라 DMA, Stream Parser, Length, Sequence, CRC, TX Queue를
함께 검증한다. 임의의 Raw byte는 안전을 위해 Echo하지 않는다.

Golden request:

```text
Payload: ASCII "STM32"
AA 55 01 F0 00 05 53 54 4D 33 32 DC A0
```

Golden response:

```text
AA 55 01 F1 00 05 53 54 4D 33 32 0F E7
```

### 5.7 DIAG_MOTOR_TEST - 0xF2 / 0xF3

PC 벤치 시험 전용 명령이다. 실제 Jetson 주행 제어에서는 사용하지 않고
`CMD_DRIVE`를 사용한다.

Request Payload:

| Offset | 크기 | 형식 | 필드 | 단위/규칙 |
|---:|---:|---|---|---|
| 0 | 2 | `int16` | duty_permille | 0 또는 -600~-200 / 200~600, 0.1% |
| 2 | 2 | `uint16` | duration_ms | 100~10000ms |

`duty_permille=0`은 즉시 정지 요청이며 `duration_ms`는 0으로 정규화한다.
실차의 모터 dead zone 때문에 비영 진단 출력은 절댓값 20% 이상만 허용한다.
그 외 값은 지정된 시간이 지나면 펌웨어가 자동으로 출력을 비활성화한다.

Response Payload:

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 1 | `uint8` | status |
| 1 | 2 | `int16` | accepted_duty_permille |
| 3 | 2 | `uint16` | accepted_duration_ms |

Status는 `0=ACCEPTED`, `1=INVALID_PAYLOAD`, `2=OUT_OF_RANGE`이다. 응답은
명령 수락 여부이며 물리적인 모터 회전 성공을 의미하지 않는다.

모터 진단 요청이 들어오면 STM32는 표준 `TELEMETRY_DRIVE(0x80)`도 활성화한다.
PC 도구는 이 운영 텔레메트리의 `encoder_count`와 `motor_duty_permille`을
사용해 엔코더 방향, 카운트 변화 및 자동 정지를 검증한다. 별도의 엔코더
진단 Message ID를 추가하지 않는다.

### 5.8 DIAG_PID_TEST - 0xF4 / 0xF5

PC 벤치 시험 전용 폐루프 속도 진단이다. 생산용 `CMD_DRIVE` 제한을 해제하지
않고 `ControlTask`의 엔코더 속도 계산, PID, 모터 출력 경로를 시간 제한으로
검증한다.

Request Payload:

| Offset | 크기 | 형식 | 필드 | 단위/규칙 |
|---:|---:|---|---|---|
| 0 | 2 | `int16` | target_speed_mm_s | -300~300, 0 제외 |
| 2 | 2 | `uint16` | duration_ms | 500~5000ms |

Response Payload:

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 1 | `uint8` | status |
| 1 | 2 | `int16` | accepted_target_speed_mm_s |
| 3 | 2 | `uint16` | accepted_duration_ms |

Status는 `0=ACCEPTED`, `1=INVALID_PAYLOAD`, `2=OUT_OF_RANGE`이다. PID 출력은
최대 절댓값 95%로 제한되며 시간이 만료되면 자동 정지한다. PC 도구도 시험
종료 시 별도의 정지 요청을 보내 이중으로 출력을 차단한다.

현재 적용 보정값은 `1600 count/rev`, 바퀴 둘레 `201.06mm`이다. PC 시험
도구는 기존 텔레메트리 형식을 변경하지 않고 `encoder_count` 변화량과
`uptime_ms`로 휠 RPM을 계산한다. 최종 차량 장착 후 실제 회전수와
이동거리로 보정값을 다시 확인한다.

## 6. 오류 처리

| 조건 | 처리 |
|---|---|
| SOF 이전 쓰레기 바이트 | 폐기 후 다음 SOF 탐색 |
| Length > 64 | 프레임 폐기 |
| Version != 1 | 프레임 폐기 |
| CRC 오류 | 프레임 폐기, Timeout 시각 갱신 금지 |
| 알 수 없는 Message ID | 실행하지 않고 진단 카운터 증가 |
| 잘못된 Payload 길이/값 | 실행하지 않고 진단 카운터 증가 |
| 중복·과거 Sequence | 재실행 금지 |
| 마지막 유효 CMD_DRIVE > 300ms | 중립 명령 게시, Timeout, 재무장 요구 |
| UART/DMA 오류 | 오류 카운터 증가 후 RX DMA 재시작 시도 |

## 7. PC 시험

ST-LINK USB를 PC에 연결하고 COM 포트를 확인한다.

```powershell
py -m pip install pyserial
py tools\uart_protocol_test.py self-test
py tools\uart_protocol_test.py list
py tools\uart_protocol_test.py echo --port COM5 --text STM32
```

성공 기준:

```text
Protocol echo: PASS
```

일반 시리얼 터미널에서 문자를 입력하는 Raw Echo가 아니다. 반드시 V1 프레임을
생성하는 이 도구 또는 같은 규격을 구현한 Jetson 프로그램으로 시험한다.

모터를 비활성화한 상태에서 현재 엔코더 값을 읽는다.

```powershell
py tools\uart_protocol_test.py encoder-read --port COM14
```

바퀴를 손으로 정확히 한 바퀴 돌려 방향과 한 바퀴당 카운트를 측정한다.

```powershell
py tools\uart_protocol_test.py encoder-monitor --port COM14 --seconds 10
```

바퀴를 지면에서 띄운 뒤 제한시간 모터 출력과 엔코더 방향을 함께 검사한다.

```powershell
py tools\uart_protocol_test.py encoder-test --port COM14 --percent 30 --duration 1000
py tools\uart_protocol_test.py encoder-test --port COM14 --percent -30 --duration 1000
```

양수 PWM에서 Count가 증가하고 음수 PWM에서 Count가 감소해야 현재 소프트웨어
방향 정의와 일치한다. 반대로 나오면 배선을 임의로 바꾸기 전에
`Encoder_SetDirectionSign(-1)`로 좌표계 부호를 보정한다.

임시 보정값으로 시간 제한 PID 속도 시험을 실행한다.

```powershell
py tools\uart_protocol_test.py pid-test --port COM14 --target-mm-s 150 --duration 3000
```

펌웨어와 PC 도구는 PID 제어 주기와 같은 약 10ms 간격으로 목표속도,
측정속도, 목표 휠 RPM, 측정 휠 RPM, PWM 및 누적 엔코더 Count를 표시한다.
시험 후반부 평균속도, 평균 휠 RPM, 평균 오차와 최대 PWM을 요약한다.
첫 시험은 바퀴를 지면에서 띄우고 `+150mm/s`부터 시작한다.

## 8. 구현 파일

| 파일 | 책임 |
|---|---|
| `uart_transport.c/.h` | UART DMA, RX/TX Ring Buffer, 오류 복구 |
| `comm_protocol.c/.h` | Frame, CRC16, Stream Parser, 공용 자료형 |
| `comm_service.c/.h` | 명령 검증, Timeout, 재무장, Echo, Telemetry |
| `uart_protocol_test.py` | PC 기준 구현과 Golden Frame 검증 |

UART/DMA 콜백에서는 바이트 이동과 상태 표시만 수행한다. Parsing, 명령 처리,
로깅, Blocking HAL 호출은 메인 루프 또는 향후 `CommRxTask`에서 수행한다.
