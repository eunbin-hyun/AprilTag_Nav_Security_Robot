# Day 3 초음파 안전 계층

## 적용 범위

- 대상 보드: STM32F429I-DISC1
- 거리 센서: HC-SR04 1개, 차량 전방 장착
- 계측: TIM2 CH2 입력 캡처, 1 MHz(1 count = 1 us)
- 안전 판단: FreeRTOS `SafetyTask`, 10 ms 주기, High 우선순위
- 거리 측정: FreeRTOS `UltrasonicTask`, 60 ms 주기, Normal 우선순위

## 배선

| HC-SR04 | STM32F429I-DISC1 | 설정 |
| --- | --- | --- |
| VCC | 외부 5 V | HC-SR04 전원 |
| GND | GND | 센서와 보드 공통 접지 |
| TRIG | P1-21 / PA5 | 3.3 V GPIO 출력, 10 us 펄스 |
| ECHO | P1-28 / PB3 | TIM2_CH2 / AF1 입력 캡처 |

PB3는 STM32F429ZI 데이터시트상 `FT`(5 V tolerant) 디지털 I/O이므로,
보드와 센서가 함께 전원 공급되고 공통 GND가 연결된 조건에서 HC-SR04
ECHO를 PB3에 직접 연결한다. 10 kΩ/20 kΩ 분압기는 선택 사항이며 현재
기본 배선에는 사용하지 않는다. PA5와 PB3를 사용하므로 디버거의 SWO
trace는 사용하지 않고 SWD만 사용한다.

## 타이머 및 태스크 설정

| 항목 | 값 | 근거 |
| --- | --- | --- |
| TIM2 입력 클럭 | 90 MHz | APB1 45 MHz, APB1 prescaler가 1이 아니므로 타이머 클럭 2배 |
| Prescaler | 89 | 90 MHz / (89 + 1) = 1 MHz |
| Counter period | 4,294,967,295 | 32-bit free-running counter |
| Capture channel | TIM2 CH2 / PB3 / AF1 | ECHO 상승·하강 에지 시간 측정 |
| TIM2 IRQ priority | 6 | FreeRTOS syscall 경계 priority 5보다 낮은 선점 우선순위 |
| ECHO timeout | 30 ms | HC-SR04 유효 거리의 왕복 시간을 포함 |
| 측정 주기 | 60 ms | 계획서 요구값 |

입력 캡처 ISR은 상승·하강 캡처값 저장과 세마포어 해제만 수행한다. 거리
계산, 유효성 판정, 3개 샘플 중앙값 필터는 `UltrasonicTask`에서 수행한다.

## 안전 상태와 임계값

| 조건 | 상태 | 제어 동작 |
| --- | --- | --- |
| 거리 0.65 m 이상 | CLEAR | 전진 허용 |
| 거리 0.60~0.65 m, 이전 상태 CAUTION | CAUTION 유지 | 현재 명령 유지 |
| 거리 0.20~0.60 m | CAUTION | 현재 명령 유지, 경고 상태 |
| 거리 0.20 m 미만 | STOP | PID 적분 초기화, PWM 0, BTS7960 EN Low |
| STOP 후 거리 0.30 m 이상 3회 | CAUTION 또는 CLEAR | 채터링 방지 후 정지 해제 |

95% PWM 무부하 실측속도 1.565 m/s에서는 0.20 m를 약 0.128초에
통과하므로 이 임계값은 최후 강제정지 기준이지 충돌 방지 정지거리 보장값이
아니다. 실제 바닥·하중 상태의 정지거리를 별도로 측정해야 한다.
| 센서 invalid | STOP | 즉시 PWM 0 및 EN Low |
| 센서 stale | STOP | 새 측정이 들어올 때까지 출력 정지 |
| ECHO 없음 | CLEAR | 전방이 비어 있다고 판단 |
| SafetyTask 갱신이 50 ms 초과 | STOP | ControlTask의 독립 fail-safe 감시 |
| 후진 명령/후진 검출 | STOP | 후방 센서가 없으므로 후진 금지 |

CAUTION 진입은 0.60 m, 해제는 0.65 m로 히스테리시스를 둔다. STOP 해제는
0.30 m 이상을 연속 3회 확인한다. 초음파 거리, invalid 또는 stale 상태는
E-stop을 래치하지 않으며 안전 조건이 회복되면 자동으로 재출발한다.
수동 E-stop과 모터 하드웨어 고장에 의한 래치는 별도로 유지한다.

## 디버거 확인 변수

- `shared_state.sequence` (`task_ultrasonic.c`): 초음파 측정 루프 횟수
- `shared_state.distance_m`: 필터 적용 거리
- `shared_state.pulse_us`: 마지막 정상 ECHO 폭
- `shared_state.status`: 센서 상태
- `shared_request.loop_count` (`task_safety.c`): 안전 판단 루프 횟수
- `shared_request.level`, `reason`, `latched`: 안전 상태와 원인

정상 주기라면 1초 동안 초음파 sequence는 약 16~17, Safety loop_count는
약 100 증가한다.

## 실차 전 벤치 시험

모터를 지면에서 분리하거나 모터 전원을 차단한 상태로 먼저 시험한다.

1. 센서와 보드의 공통 GND 및 ECHO-PB3 직접 연결을 확인한다.
2. TRIG가 약 60 ms 간격, High 폭 10 us인지 확인한다.
3. 1.0 m, 0.60 m, 0.30 m, 0.20 m, 0.15 m 위치에 평평한 표적을 두고
   `distance_m`, `level`, `reason`을 확인한다.
4. 0.20 m 안쪽에서 PWM이 0이 되고 PE2/PE3가 Low인지 확인한다.
5. 표적을 0.30 m 이상으로 이동해도 3개의 정상 샘플 전에는 STOP이
   해제되지 않는지 확인한다.
6. 0.15 m 안쪽에서도 STOP만 발생하고 표적을 0.30 m 이상 치우면 자동으로
   재출발하는지 확인한다.
7. ECHO 선을 분리했을 때 `NO_ECHO`로 판단하고 전진이 허용되는지 확인한다.
8. 음수 PWM/속도 명령을 보내 후진이 차단되는지 확인한다.
9. 기존 서보, 엔코더, UART 통신과 텔레메트리가 동시에 정상인지 회귀
    시험한다.

## 정상 판정과 실패 증상

- 정상: 거리 오차가 시험 환경의 허용 범위 안이고, 임계값 전이가 위 표와
  일치하며, STOP에서 TIM5 CH1·CH4 compare가 0이고 PE2·PE3가 Low다.
- 실패: 거리값 고정/튀는 값, 주기 카운터 정지, 0.20 m 안에서 PWM 발생,
  장애물을 치운 뒤에도 STOP 유지, 후진 PWM 발생, UART timeout 또는
  엔코더 누락.

실제 차량 제동거리에 맞는 임계값 보정, 온도에 따른 음속 보정, 비스듬하거나
흡음성인 장애물 검출률 평가는 실제 하드웨어 시험 후 수행한다.
