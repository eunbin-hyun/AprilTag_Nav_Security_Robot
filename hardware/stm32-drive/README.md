# ssacurity-stm32-drive

## Troubleshooting

- [프로젝트 전체 트러블슈팅 README](docs/troubleshooting/README.md)
- [조향센서 없이 MG996R 7점 LUT를 만든 상세 과정](docs/steering_without_sensor_troubleshooting.md)

싸큐리티 자율주행 로봇의 STM32F429 주행 제어 펌웨어 프로젝트입니다.

현재 Jetson 연동 기준은 **UART 명세 V3.0 / wire protocol `0x02`**입니다.

- STM32F429I-DISC1 / STM32F429ZIT6
- Jetson 통신 UART5: PC12 TX / PD2 RX, 115200 8-N-1
- RX DMA1 Stream0 Circular + 512-byte 수신 ring
- TX DMA1 Stream7 Normal + 송신 ring
- CRC-16/CCITT-FALSE
- 256-byte 지속 parser, SOF 재탐색, incomplete frame 100 ms timeout
- 송신자별 단일 SEQ와 350 ms 세션 리셋
- `CMD_DRIVE`, `CMD_STOP`, `CMD_RESET_FAULT`
- `TELEMETRY_DRIVE`·`TELEMETRY_ODOMETRY` 20 Hz,
  `TELEMETRY_RANGE` 10 Hz
- `FAULT_EVENT`, `COMMAND_RESULT`, `DIAG_ECHO`
- 300 ms drive watchdog와 neutral 재무장

상세 규격과 현재 구현 범위는
[`docs/jetson_stm32_uart_interface_v3.md`](docs/jetson_stm32_uart_interface_v3.md)를
참조합니다.

Jetson 담당자에게 전달할 통합 인수인계 자료는
[`docs/jetson_handover_2026-07-31.md`](docs/jetson_handover_2026-07-31.md)에
정리되어 있습니다.

Jetson 파트의 AI/Codex에 그대로 전달할 구현 요청서는
[`docs/jetson_ai_implementation_prompt.md`](docs/jetson_ai_implementation_prompt.md)입니다.

## 배선

```text
Jetson/CP2102 TXD  -> STM32 PD2  (UART5_RX, P1-40)
Jetson/CP2102 RXD  <- STM32 PC12 (UART5_TX, P1-43)
Jetson/CP2102 GND  -- STM32 GND  (P1-63 또는 P1-64)
CP2102 VCC         -- 연결하지 않음
```

3.3 V TTL을 사용하고 TX/RX를 교차 연결합니다. Jetson J12 직결 UART와
USB-UART TX를 동시에 STM32 RX에 연결하지 않습니다.

USART1(PA9/PA10)은 보드 내장 ST-LINK VCP 시험용이고 UART5는 최종 Jetson
배선용입니다. 활성 포트는 `App/Inc/vehicle_config.h`의
`VEHICLE_COMM_USE_STLINK_VCP`로 선택합니다. 현재 값은 PC 통합 시험을 위한
USART1/ST-LINK VCP입니다.

## Jetson Echo 시험

`VEHICLE_COMM_USE_STLINK_VCP=0U`로 빌드한 최신 ELF를 STM32에 플래시한 뒤
Jetson에서 실행합니다.

```bash
python3 -m pip install pyserial
python3 tools/jetson_uart_echo_test.py --self-test
python3 tools/jetson_uart_echo_test.py --port /dev/ttyUSB0 --text JETSON
```

자체 시험은 Jetson 팀이 전달한 Golden Frame GF-01~GF-07을 모두
검증합니다. 정상 실물 통신 결과는 다음과 같습니다.

```text
Jetson <-> STM32 UART5 echo: PASS
```

## 안전 잠금

`App/Inc/vehicle_config.h`는 실측 지속속도 `±1565 mm/s`와 실측 전체 조향 범위
`-28.69°(오른쪽, 1680us) ~ +19.55°(왼쪽, 750us)`를 허용합니다.
모터 출력은 무부하 포화 측정점인 최대 95% PWM을 사용합니다. 중간 각도는
7점 LUT에서 구간별 선형 보간합니다. 조향 범위는 최종 조립
차량에서 측정했지만 실제 바닥 주행 속도와 정지거리는 별도 검증이 필요합니다.

PC에서 실제 Jetson `CMD_DRIVE` 경로를 시험하려면:

```powershell
py tools\uart_protocol_test.py drive-scenario `
  --port COM11 `
  --wheels-off-ground
```

모터를 정지한 상태에서 조향 명령 전체 범위를 확인하려면:

```powershell
py tools\uart_protocol_test.py steering-sweep `
  --port COM11 `
  --wheels-off-ground
```

실측 최대 조향각을 좌우로 세 번 크게 왕복하려면:

```powershell
py tools\uart_protocol_test.py steering-full-sweep `
  --port COM11 `
  --cycles 3 `
  --hold-seconds 1.2 `
  --wheels-off-ground
```

저속·최대속도, 좌우 2.5°/5°/10°, 정방향·역방향 조향 및 Watchdog을
포함한 확장 시험은 다음과 같습니다.

```powershell
py tools\uart_protocol_test.py drive-scenario `
  --port COM11 `
  --profile extended `
  --wheels-off-ground
```

현재 STM32가 Jetson으로 보내는 주행·Fault·거리 텔레메트리를 수동 주행 없이
확인하려면:

```powershell
py tools\uart_protocol_test.py telemetry-monitor `
  --port COM11 `
  --seconds 5
```

명령 조향각 기반 아커만 오도메트리의 전진·좌회전·우회전·후진 부호와
Jetson 송신을 확인하려면:

```powershell
py tools\uart_protocol_test.py odometry-test `
  --port COM11 `
  --wheels-off-ground
```

세부 절차와 합격 기준은
[`docs/jetson_stm32_uart_interface_v3.md`](docs/jetson_stm32_uart_interface_v3.md)에
정리되어 있습니다.

물리 E-stop 입력과 독립 모터 에너지 차단 경로는 아직 핀이 확정되지
않았습니다. UART `CMD_STOP`은 물리 E-stop을 대신하지 않습니다.

`tools/uart_protocol_test.py`는 현재 V2 wire protocol의 모터·서보 진단과
운영 `CMD_DRIVE` 시험을 지원합니다. V1의 `0x82 TELEMETRY_ODOMETRY`와
`0xF4..0xF5` PID 진단은 현재 활성 펌웨어에서 사용하지 않습니다.

현재 `OdometryTask`는 엔코더 이동거리와 7점 LUT의 등가 중심 조향 명령각으로
아커만 pose를 적분하고, V2 `0x85 TELEMETRY_ODOMETRY`로 20 Hz 송신합니다.
조향센서 실측값이 아니므로 모든 정상 프레임에 `STEERING_ESTIMATED`와
`COMMAND_ESTIMATE` source가 포함됩니다. IMU가 장착되기 전에는
`IMU_FUSED`가 설정되지 않습니다.
