# Day 4 조향각 및 아커만 오도메트리

## 확정 배선

| 기능 | STM32F429I-DISC1 | Peripheral |
|---|---|---|
| MG996R PWM | PB4 / P1-25 | TIM3_CH1, 50Hz |
| PP-A818 VCC | P2-1 3V | 보드 3.3V 출력 |
| PP-A818 GND, DIR | P2-11 GND | 공통 GND |
| PP-A818 OUT | PC3 / P2-15 | ADC1_IN13 |

MG996R 전원은 STM32 보드가 아니라 외부 6V BEC에서 공급한다. BEC
GND와 STM32 GND는 공통으로 연결하되 서보 전류가 STM32 보드를 통해
흐르지 않게 한다.

## 구현된 실행 구조

| 주기 | 소유 태스크 | 기능 |
|---:|---|---|
| 10ms | ControlTask | TIM4 엔코더, ADC1 조향각, TIM3 서보, 속도 PID |
| 20ms | OdometryTask | ControlState snapshot을 사용한 아커만 pose 적분 |
| 10ms | SafetyTask | 초음파 안전 상태와 모터 차단 |
| 1ms | CommTask | UART 명령, 주행 및 오도메트리 텔레메트리 |

ADC1은 software-triggered polling 방식으로만 사용한다. DMA와 ADC
인터럽트는 사용하지 않으므로 기존 USART1 DMA2 Stream 2/7 및 FreeRTOS
인터럽트 우선순위와 충돌하지 않는다.

## 실측 전 안전 잠금

`App/Inc/vehicle_config.h`의 다음 설정은 의도적으로 0이다.

- `VEHICLE_STEERING_SENSOR_CALIBRATED`
- `VEHICLE_STEERING_SERVO_CALIBRATED`
- `VEHICLE_ODOMETRY_GEOMETRY_CALIBRATED`
- `VEHICLE_MAX_ABS_SPEED_MM_S`
- `VEHICLE_MAX_ABS_STEERING_CDEG`

이 상태에서는 MG996R PWM을 시작하지 않는다. 확인되지 않은 1500us를 실제
차량의 중앙으로 가정하지 않기 위해서다. 비중립 조향 명령과 production
`CMD_DRIVE`, 속도 PID, pose 적분도 허용되지 않는다. 엔코더 확인용 0% 읽기와
기존 제한시간 open-loop motor 진단 명령만 사용할 수 있다.

## 실차 보정 순서

1. 타이로드를 분리한 상태에서 외부 서보 테스터로 MG996R의 안전한 중앙
   pulse를 찾는다. STM32 펌웨어는 아직 PWM을 출력하지 않는다.
2. 바퀴를 직진으로 맞추고 서보를 측정된 중앙 pulse에 둔 다음, 서보혼을
   가장 가까운 스플라인 위치에 고정하고 타이로드 길이로 미세 조정한다.
3. 기구 간섭이 없는 안전한 좌/중앙/우 pulse와 실제 바퀴각을 측정한다.
4. AS5600 자석을 실제 조향축에 동축으로 설치한다.
5. 좌/중앙/우에서 `steering-monitor`로 ADC raw 값을 기록한다.
6. 휠베이스는 뒷축 중심부터 앞 킹핀축까지 측정한다.
7. 전륜 조향 윤거는 좌우 킹핀축 중심 사이를 측정한다.
8. 센서를 장착한 바퀴가 차량 기준 왼쪽인지 오른쪽인지 기록한다.
9. 검토된 값을 `vehicle_config.h`에 입력하고 calibration flag를 1로
   변경한다.
10. 바퀴를 띄운 상태에서 좌/중앙/우를 재검증한 뒤 저속 바닥 시험을 한다.

ADC raw 확인:

```powershell
py tools\uart_protocol_test.py steering-monitor --port COM14 --seconds 15
```

### 초기 서보 펄스 진단

업데이트된 UART V2 펌웨어를 먼저 플래시한다. 차량 구동모터가 정지하고
바퀴가 들린 상태에서만 실행한다.

```powershell
py tools\uart_protocol_test.py servo-off --port COM14
py tools\uart_protocol_test.py servo-calibrate --port COM14 --pulse-us 1500
py tools\uart_protocol_test.py servo-calibrate --port COM14 --pulse-us 1520
py tools\uart_protocol_test.py servo-off --port COM14
```

MG996R 진단 허용 범위는 실차 측정을 반영한 `750..1680us`이다. 측정된 기준점은
좌 750us, 직진 1300us, 우 1680us이며 실제 바퀴 각도 측정 전까지 생산 보정은
활성화하지 않는다. PC 도구는
명령을 100ms마다 갱신하고, STM32는 300ms 동안 갱신이 없으면 TIM3 PWM을
자동으로 정지한다. `servo-calibrate` 종료 시에도 `0us` 정지 명령을 보낸다.
응답에는 현재 AS5600 ADC raw가 포함된다.

750us 또는 1680us가 기계적 스토퍼에 부하를 거는 값이라면 최종 운용 한계에는
여유를 둔 별도 펄스를 사용한다.

## 아커만 변환

좌회전을 양의 조향 및 양의 yaw로 정의한다. 측정한 앞바퀴각을 `alpha`,
휠베이스를 `L`, 전륜 킹핀 윤거를 `T`, 센서 바퀴의 횡방향 위치를
왼쪽 `+T/2`, 오른쪽 `-T/2`의 `y_sensor`라고 하면:

```text
curvature = tan(alpha) / (L + y_sensor * tan(alpha))
center_steering = atan(L * curvature)
yaw_rate = vehicle_speed * curvature
```

OdometryTask는 encoder 누적거리의 20ms 차분과 위 curvature를 사용해
midpoint heading으로 `x`, `y`, `yaw`를 적분한다.

## 미확정값

- AS5600 좌/중앙/우 ADC raw
- MG996R 좌/중앙/우 pulse와 실제 바퀴각
- 휠베이스
- 전륜 킹핀 윤거
- 센서 장착 바퀴
- 실측 엔코더 counts/wheel revolution
- 하중 상태의 실제 타이어 구름 둘레

이 값들은 차량 실측 전에는 사실값으로 채우지 않는다.
