# Jetson–STM32 주행 제어 UART 연동 명세 V1

> **폐기된 과거 규격:** 활성 펌웨어는 V3.0 / wire protocol `0x02`를
> 사용한다. 현재 기준은
> [`jetson_stm32_uart_interface_v3.md`](jetson_stm32_uart_interface_v3.md)다.
> 이 문서는 과거 설계와 변경 이력을 확인하기 위해서만 보존한다.

| 항목 | 내용 |
|---|---|
| 문서 버전 | 1.0 |
| 작성일 | 2026-07-27 |
| 적용 대상 | Jetson Orin ↔ STM32F429 주행 제어기 |
| 기준 프로토콜 | UART Protocol V1 |
| 기준 구현 | STM32 `comm_protocol`, `comm_service`, `uart_transport` |

## 1. 목적과 적용 범위

이 문서는 Jetson과 STM32 주행 제어기 사이의 UART 연동에 필요한 구현 계약과
시험 기준을 정의한다.

Jetson은 목표 속도와 목표 조향을 생성하고, STM32는 수신 프레임의 무결성,
범위, 순서, 최신성을 검사한 뒤 저수준 주행 제어 계층에 전달한다.

이 문서의 범위는 다음과 같다.

- UART 물리·전기 설정
- Binary Frame과 CRC 생성·검증
- Message ID와 Payload 직렬화
- Sequence 관리
- 명령 주기와 통신 Timeout
- 부팅·정지·재연결 시 재무장 절차
- Telemetry와 Fault 해석
- 양 팀의 연동 시험 기준

물리 E-stop은 본 UART 프로토콜로 대체하지 않는다.

## 2. 현재 구현 상태

### 2.1 구현 완료

- USART1 115200 8N1 기반 PC–STM32 Echo 시험
- RX DMA Circular + UART IDLE 수신
- TX DMA Normal + 송신 Ring Buffer
- UART Protocol V1 Frame 생성 및 Stream Parser
- CRC-16/CCITT-FALSE
- `CMD_DRIVE`, `CMD_STOP`, `CMD_RESET_FAULT`
- `DIAG_ECHO_REQUEST`, `DIAG_ECHO_RESPONSE`
- `TELEMETRY_DRIVE`, `FAULT_EVENT` 직렬화 함수
- UART5 PC12 TX / PD2 RX
- UART5 RX DMA1 Stream0 Circular + TX DMA1 Stream7 Normal
- Jetson UART5 전용 Echo 시험 도구
- 300ms Drive 명령 Timeout
- 부팅·Timeout·Stop 이후 중립 명령 재무장
- 중복·과거 Sequence 거부
- PC Golden Frame 및 Echo 시험 도구

### 2.2 STM32팀 통합 필요 항목

- 실측 속도·조향 한계를 `CommService_SetDriveLimits()`로 설정
- 수신 명령을 Motor/Steering/Safety Manager가 소비하도록 연결
- `TELEMETRY_DRIVE` 10Hz 송신 연결
- Fault 변화 시 `FAULT_EVENT` 송신 연결
- 통신 Timeout 및 CRC 오류를 실제 Fault Manager에 연결
- TX DMA 오류 후 송신 복구 로직 보강

현재 펌웨어에서는 주행 한계가 설정되지 않았고 명령 소비 코드도 연결되지
않았다. 따라서 Echo 시험은 가능하지만 비영점 주행 명령은 실제 구동에
반영되지 않는다.

## 3. 역할 분담

| 구분 | 책임 |
|---|---|
| Jetson | 목표 속도·조향 생성 |
| Jetson | V1 Frame 생성, CRC 계산, Sequence 증가 |
| Jetson | `CMD_DRIVE`를 300ms보다 짧은 주기로 반복 송신 |
| Jetson | 부팅·재연결 시 중립 재무장 절차 수행 |
| Jetson | STM32 Telemetry·Fault 수신 및 상태 감시 |
| STM32 | Frame, CRC, Version, Length 검증 |
| STM32 | Sequence, Payload, 주행 한계 검증 |
| STM32 | 통신 Timeout 시 중립 명령과 안전 정지 게시 |
| STM32 | 검증된 명령을 저수준 제어기에 전달 |
| STM32 | 현재 주행 상태와 Fault를 Jetson에 송신 |

## 4. 물리 인터페이스

### 4.1 최종 연결

```text
Jetson USB ── CP2102 USB-TTL ── 3.3V UART ── STM32F429 UART5
```

| 항목 | 값 |
|---|---|
| Baud rate | 115200 |
| Data bits | 8 |
| Parity | None |
| Stop bits | 1 |
| Flow control | None |
| 전기 레벨 | 3.3V TTL |
| STM32 UART5 TX | PC12 |
| STM32 UART5 RX | PD2 |
| Jetson 장치 예시 | `/dev/ttyUSB0` |

배선은 다음과 같이 교차 연결한다.

```text
CP2102 TX  → STM32 PD2  (UART5 RX)
CP2102 RX  ← STM32 PC12 (UART5 TX)
CP2102 GND ↔ STM32 GND
```

STM32F429I-DISC1 확장 헤더 위치:

```text
P1-40 = PD2  (UART5 RX)
P1-43 = PC12 (UART5 TX)
P1-63 또는 P1-64 = GND
```

5V UART 신호를 연결하지 않는다. Jetson 운영 환경에서는 USB 장치 번호가
변경될 수 있으므로 CP2102 VID/PID 또는 Serial Number를 이용한 udev 고정
심볼릭 링크 사용을 권장한다.

### 4.2 현재 PC 시험 연결

```text
PC USB ── ST-LINK Virtual COM Port ── STM32 USART1
```

| STM32 신호 | 핀 |
|---|---|
| USART1 TX | PA9 |
| USART1 RX | PA10 |

PC 시험과 최종 Jetson 시험은 UART 인스턴스만 다르고, Transport, Frame,
Parser 및 서비스 정책은 동일하다.

Jetson Echo 시험:

```bash
python3 -m pip install pyserial
python3 tools/jetson_uart_echo_test.py --self-test
python3 tools/jetson_uart_echo_test.py --port /dev/ttyUSB0 --text JETSON
```

## 5. Frame 규격

```text
[SOF1][SOF2][VERSION][MESSAGE_ID][SEQUENCE][LENGTH][PAYLOAD][CRC_LOW][CRC_HIGH]
```

| Offset | 크기 | 필드 | 값 또는 의미 |
|---:|---:|---|---|
| 0 | 1 | SOF1 | `0xAA` |
| 1 | 1 | SOF2 | `0x55` |
| 2 | 1 | VERSION | `0x01` |
| 3 | 1 | MESSAGE_ID | 메시지 종류 |
| 4 | 1 | SEQUENCE | 송신 방향별 순환 번호 |
| 5 | 1 | LENGTH | Payload 바이트 수 |
| 6 | N | PAYLOAD | 메시지별 정의 |
| 6+N | 1 | CRC_LOW | CRC 하위 바이트 |
| 7+N | 1 | CRC_HIGH | CRC 상위 바이트 |

- 최대 Payload는 64바이트이다.
- 전체 Frame 크기는 `8 + LENGTH` 바이트이다.
- 모든 2바이트·4바이트 정수는 Little Endian이다.
- 문자열, JSON 또는 개행 기반 프로토콜이 아닌 Raw Binary 프로토콜이다.
- C/C++ 구조체 메모리를 그대로 송신하지 않고 필드별로 직렬화한다.
- Signed 정수는 2의 보수 표현을 사용한다.

## 6. CRC 규격

| 항목 | 값 |
|---|---|
| 알고리즘 | CRC-16/CCITT-FALSE |
| Polynomial | `0x1021` |
| Initial value | `0xFFFF` |
| RefIn / RefOut | false / false |
| XorOut | `0x0000` |
| 계산 범위 | VERSION부터 Payload 마지막 바이트 |
| 전송 순서 | CRC Low, CRC High |

표준 확인값:

```text
ASCII "123456789" → 0x29B1
```

CRC 계산 대상에는 `SOF1`, `SOF2`, `CRC_LOW`, `CRC_HIGH`를 포함하지 않는다.

## 7. Sequence 규칙

- Jetson 송신과 STM32 송신은 각자 독립된 Sequence를 사용한다.
- Sequence는 Message ID별이 아니라 송신 방향 전체에서 하나를 사용한다.
- 정상 송신 때마다 1 증가하며 `255 → 0`으로 순환한다.
- 동일 Sequence 또는 명백히 과거인 Frame은 다시 실행하지 않는다.
- STM32 구현은 이전 Sequence와의 차이가 `1~127`일 때 새 Frame으로 인정한다.
- 통신 Timeout 발생 후 STM32의 Jetson 수신 Sequence 동기화는 초기화된다.
- `DIAG_ECHO_RESPONSE`만 요청의 Sequence를 그대로 반환한다.
- STM32의 Telemetry/Fault Sequence는 Jetson 송신 Sequence와 별개이다.

정상 예:

```text
CMD_DRIVE sequence=10
CMD_DRIVE sequence=11
CMD_STOP  sequence=12
DIAG_ECHO_REQUEST sequence=13
```

Jetson 프로세스 내부에는 모든 STM32 송신 메시지가 공유하는 단일
`uint8` TX Sequence 카운터를 둔다.

## 8. 메시지 목록

| ID | 방향 | 이름 | Payload 길이 |
|---:|---|---|---:|
| `0x10` | Jetson → STM32 | `CMD_DRIVE` | 5 |
| `0x11` | Jetson → STM32 | `CMD_STOP` | 1 |
| `0x12` | Jetson → STM32 | `CMD_RESET_FAULT` | 4 |
| `0x80` | STM32 → Jetson | `TELEMETRY_DRIVE` | 21 |
| `0x81` | STM32 → Jetson | `FAULT_EVENT` | 12 |
| `0x82` | STM32 → Jetson | `TELEMETRY_SENSOR` | 예약, 사용 금지 |
| `0xF0` | Jetson/PC → STM32 | `DIAG_ECHO_REQUEST` | 0~32 |
| `0xF1` | STM32 → Jetson/PC | `DIAG_ECHO_RESPONSE` | 요청과 동일 |

## 9. Jetson → STM32 Payload

### 9.1 CMD_DRIVE (`0x10`)

| Offset | 크기 | 형식 | 필드 | 단위·제약 |
|---:|---:|---|---|---|
| 0 | 2 | `int16` | `target_speed_mm_s` | mm/s, 음수는 역방향 |
| 2 | 2 | `int16` | `target_steering_cdeg` | 0.01도 |
| 4 | 1 | `uint8` | `drive_enable` | 0 또는 1 |

Python 직렬화:

```python
payload = struct.pack(
    "<hhB",
    target_speed_mm_s,
    target_steering_cdeg,
    drive_enable,
)
```

중립·재무장 명령:

```text
target_speed_mm_s = 0
target_steering_cdeg = 0
drive_enable = 0
```

부팅, 통신 Timeout, `CMD_STOP` 이후에는 이 중립 명령을 먼저 보내야 한다.
주행 한계가 설정되기 전에는 중립 명령 이외의 `CMD_DRIVE`가 거부된다.

### 9.2 CMD_STOP (`0x11`)

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 1 | `uint8` | `stop_reason` |

| 값 | 의미 |
|---:|---|
| 0 | Operator |
| 1 | Mission complete |
| 2 | Obstacle |
| 3 | Remote request |
| 4 | Internal |

Python 직렬화:

```python
payload = struct.pack("<B", stop_reason)
```

`CMD_STOP`은 운영 정지이며 물리 E-stop이 아니다. STM32는 수신 즉시 중립
명령을 게시하고 재무장 상태로 진입한다.

### 9.3 CMD_RESET_FAULT (`0x12`)

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 4 | `uint32` | `acknowledged_fault_bits` |

Python 직렬화:

```python
payload = struct.pack("<I", acknowledged_fault_bits)
```

이 메시지는 Fault 해제 요청이다. 원인이 해소되고 해당 Fault가 원격 해제
가능한 경우에만 STM32 Safety Manager가 해제한다. 이 요청만으로 모터를
재활성화하지 않는다.

### 9.4 DIAG_ECHO_REQUEST (`0xF0`)

- Payload 길이: 0~32바이트
- STM32는 Payload를 변경하지 않고 `DIAG_ECHO_RESPONSE`로 반환한다.
- 응답 Sequence는 요청 Sequence와 동일하다.
- Raw UART 바이트 자체는 Echo하지 않는다.

## 10. STM32 → Jetson Payload

### 10.1 TELEMETRY_DRIVE (`0x80`)

| Offset | 크기 | 형식 | 필드 | 단위 |
|---:|---:|---|---|---|
| 0 | 2 | `int16` | `target_speed_mm_s` | mm/s |
| 2 | 2 | `int16` | `measured_speed_mm_s` | mm/s |
| 4 | 4 | `int32` | `encoder_count` | 누적 Count |
| 8 | 2 | `int16` | `motor_duty_permille` | -1000~1000 |
| 10 | 2 | `int16` | `steering_cdeg` | 0.01도 |
| 12 | 1 | `uint8` | `system_state` | 상태 값 |
| 13 | 4 | `uint32` | `active_fault_bits` | Fault bit mask |
| 17 | 4 | `uint32` | `uptime_ms` | 부팅 후 ms |

Python 역직렬화:

```python
(
    target_speed_mm_s,
    measured_speed_mm_s,
    encoder_count,
    motor_duty_permille,
    steering_cdeg,
    system_state,
    active_fault_bits,
    uptime_ms,
) = struct.unpack("<hhihhBII", payload)
```

System state:

| 값 | 상태 |
|---:|---|
| 0 | BOOT |
| 1 | SELF_TEST |
| 2 | READY |
| 3 | DRIVING |
| 4 | SAFE_STOP |
| 5 | FAULT |

권장 송신 주기는 10Hz이다.

### 10.2 FAULT_EVENT (`0x81`)

| Offset | 크기 | 형식 | 필드 |
|---:|---:|---|---|
| 0 | 4 | `uint32` | `active_fault_bits` |
| 4 | 4 | `uint32` | `latched_fault_bits` |
| 8 | 4 | `uint32` | `occurred_at_ms` |

Python 역직렬화:

```python
active, latched, occurred_at_ms = struct.unpack("<III", payload)
```

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

Fault 발생·해제 변화 시 이벤트를 송신한다. 현재 Active Fault는
`TELEMETRY_DRIVE`에도 반복 포함하므로 `FAULT_EVENT` 하나가 손실되어도 현재
상태를 복구할 수 있어야 한다.

## 11. Timeout과 안전 동작

- 마지막 유효 `CMD_DRIVE` 이후 300ms가 지나면 통신 Timeout으로 처리한다.
- CRC 오류, 잘못된 Payload, 알 수 없는 ID, 중복 Sequence는 Timeout 시각을
  갱신하지 않는다.
- Timeout 시 STM32는 중립 Drive 명령을 게시한다.
- Timeout 시 재무장이 필요하며 과거 명령을 자동 재실행하지 않는다.
- Jetson은 `CMD_DRIVE`를 300ms보다 짧은 간격으로 계속 송신해야 한다.
- 기본 제안 송신 주기는 20Hz이며, 최종 주기는 양 팀이 합의한다.
- 정지 상태를 유지하면서 링크를 활성 상태로 유지하려면 중립
  `CMD_DRIVE`를 동일 주기로 송신할 수 있다.

## 12. 부팅 및 재연결 절차

### 12.1 정상 부팅

```text
1. Serial 포트 열기
2. 선택적으로 DIAG_ECHO_REQUEST로 링크 검증
3. 중립 CMD_DRIVE 전송
4. 중립 명령 수락 후 정상 CMD_DRIVE 주기 송신 시작
```

### 12.2 Jetson 프로세스 또는 USB 재연결

기존 통신 세션이 남아 있을 수 있으므로 다음 절차를 사용한다.

```text
1. Serial 포트 재연결
2. 최소 350ms 대기하여 STM32 통신 Timeout과 Sequence 재동기화 유도
3. Jetson TX Sequence를 초기화
4. 중립 CMD_DRIVE 전송
5. 정상 CMD_DRIVE 주기 송신 시작
```

재연결 직후 과거 주행 명령을 재전송하거나 자동으로 주행을 재개하지 않는다.

## 13. 오류 처리와 ACK 정책

| 조건 | STM32 처리 |
|---|---|
| SOF 이전 쓰레기 바이트 | 폐기 후 다음 SOF 탐색 |
| Length > 64 | Frame 폐기 |
| Version != 1 | Frame 폐기 |
| CRC 오류 | Frame 폐기, Timeout 갱신 안 함 |
| 알 수 없는 Message ID | 실행하지 않음 |
| 잘못된 Payload 길이·값 | 실행하지 않음 |
| 중복·과거 Sequence | 재실행하지 않음 |
| 마지막 유효 Drive > 300ms | 중립 명령, Timeout, 재무장 |

V1에는 일반 명령에 대한 ACK/NACK Frame이 없다.

- Jetson은 `CMD_DRIVE` 전송 성공만으로 실제 적용을 단정하지 않는다.
- 적용 상태는 `TELEMETRY_DRIVE`의 목표값, 시스템 상태 및 Fault로 확인한다.
- Echo 응답은 링크와 프로토콜 진단용이며 Drive 명령 ACK가 아니다.

## 14. Golden Frame

Echo Payload:

```text
ASCII "STM32" = 53 54 4D 33 32
```

요청:

```text
AA 55 01 F0 00 05 53 54 4D 33 32 DC A0
```

응답:

```text
AA 55 01 F1 00 05 53 54 4D 33 32 0F E7
```

Jetson 구현은 실제 장치 연결 전에 이 Golden Frame과 CRC 결과가 일치해야
한다.

## 15. Jetson 구현 권장 구조

```text
Serial Reader
  └─ Byte Buffer
      └─ Stream Frame Parser
          ├─ CRC/Version/Length 검증
          ├─ TELEMETRY_DRIVE 처리
          ├─ FAULT_EVENT 처리
          └─ DIAG_ECHO_RESPONSE 처리

Drive Command Publisher
  ├─ 단일 TX Sequence 관리
  ├─ CMD_DRIVE 주기 송신
  ├─ Timeout/재연결 시 중립 재무장
  └─ Serial write 실패 감시
```

Serial read 결과가 항상 Frame 단위로 도착한다고 가정하면 안 된다. 한 번의
read에 Frame 일부만 들어오거나 여러 Frame이 함께 들어올 수 있으므로 반드시
누적 Byte Buffer와 Stream Parser를 사용한다.

## 16. 양 팀 합의 필요 항목

연동 시험 전에 다음 값을 확정한다.

| 항목 | 결정값 |
|---|---|
| 조향 양수 방향 | 미정: 좌회전 또는 우회전 |
| 최대 절대속도 | 미정, 단위 mm/s |
| 최대 절대조향 | 미정, 단위 cdeg |
| CMD_DRIVE 송신 주기 | 기본 제안 20Hz |
| TELEMETRY_DRIVE 송신 주기 | 권장 10Hz |
| Jetson 고정 장치 경로 | 미정, udev rule 권장 |
| CP2102 식별 정보 | 미정 |
| 연결 끊김 UI·로그 정책 | 미정 |
| 원격 해제 가능한 Fault 목록 | 미정 |

## 17. 연동 시험 항목과 합격 기준

| 시험 | 합격 기준 |
|---|---|
| CRC 자체 시험 | `"123456789"` 결과 `0x29B1` |
| Golden Frame 생성 | 문서의 Echo 요청과 바이트 단위 일치 |
| Echo 시험 | Sequence와 Payload가 동일한 `0xF1` 수신 |
| 분할 수신 | Frame을 여러 read로 나눠 받아도 정상 파싱 |
| 연속 수신 | 여러 Frame을 한 read로 받아도 모두 정상 파싱 |
| CRC 오류 | 명령이 실행되지 않음 |
| 중복 Sequence | 명령이 다시 실행되지 않음 |
| 잘못된 Length | 명령이 실행되지 않음 |
| 부팅 직후 비영점 Drive | 거부 |
| 중립 후 정상 Drive | 허용 |
| Drive 송신 중단 | 300ms 후 STM32 중립·안전 정지 |
| 재연결 | 과거 명령 재실행 없이 중립 후 재출발 |
| Telemetry | 21바이트 Payload를 필드별로 정확히 복원 |
| Fault Event | 12바이트 Payload와 Fault bit 정확히 복원 |

## 18. 전달 파일

Jetson팀에는 다음 파일을 함께 전달한다.

- 본 문서: `docs/jetson_stm32_uart_interface_v1.md`
- 상세 원본 규격: `docs/uart_protocol_v1.md`
- PC 기준 구현: `tools/uart_protocol_test.py`

PC 기준 구현의 CRC, Frame 생성, Stream Parser를 Jetson 구현과 비교 검증한다.

## 19. 변경 관리

다음 항목이 변경되면 문서 버전과 Protocol Version 변경 여부를 함께 검토한다.

- Frame 구조 또는 CRC 범위
- Message ID
- Payload 필드, 형식, 길이, 단위
- Sequence 판정 방식
- Timeout 값과 재무장 정책
- 시스템 상태 또는 Fault bit 의미

예약 ID나 Payload에 임의 필드를 추가하지 않는다. 양 팀 합의와 문서 개정 후
구현한다.
