# 로봇 소리 명령과 복귀 전체 흐름

기준일: 2026-08-06

전체 흐름은 아래처럼 정리합니다.

```text
[A: 충전스테이션]
   ↓
로봇 출발
   ↓
[C: 목적지/태깅 위치]
   ↓
터치 센서로 복귀 요청
   ↓
[A: 충전스테이션 복귀]
```

## 1. A → C 출발

```text
서버가 젯슨에게 출발 명령 전달
→ 젯슨이 A → C 주행 로직 실행
→ 서버가 라파이 소리 큐에 robot_departure 추가
→ 라파이 스피커에서 robot_departure.wav 재생
```

- `robot_departure`: 로봇이 A에서 C로 출발했다는 안내
- 음원: `robot_departure.wav`

## 2. C 도착

```text
젯슨이 C 도착 판단
→ 젯슨이 서버로 ARRIVED 또는 robot_arrival 상태 보고
→ 서버가 라파이 소리 큐에 robot_arrival 추가
→ 라파이 스피커에서 robot_arrival.wav 재생
```

- `robot_arrival`: 로봇이 C, 즉 목적지/태깅 위치에 도착했다는 안내
- 음원: `robot_arrival.wav`

## 3. C → A 복귀 요청

```text
사용자가 C 지점에서 라파이 터치 센서 누름
→ 라파이가 서버로 복귀 요청 이벤트 전송
→ 서버가 젯슨에게 return_to_charge 명령 전달
→ 서버가 라파이 소리 큐에 robot_departure 추가
→ 라파이 스피커에서 robot_departure.wav 재생
→ 젯슨이 C → A 복귀 로직 실행
```

이 단계는 복귀 시작입니다. 이때 출발 안내는 `robot_departure`를 사용합니다. `robot_return_complete`를 보내는 단계가 아닙니다.

`robot_departure`는 A → C 출발과 C → A 출발에 모두 사용합니다. 음성 문구는 방향을 특정하지 않고 "로봇이 출발합니다"처럼 잡는 것이 안전합니다.

## 4. A 복귀 완료

```text
젯슨이 A 도착/DOCKED 판단
→ 젯슨이 서버로 RETURN_COMPLETE 또는 DOCKED 상태 보고
→ 서버가 라파이 소리 큐에 robot_return_complete 추가
→ 라파이 스피커에서 robot_return_complete.wav 재생
```

- `robot_return_complete`: 로봇이 A 충전스테이션으로 복귀 완료했다는 안내
- 음원: `robot_return_complete.wav`

## 5. 버스/셔틀 도착

```text
서버가 버스/셔틀 도착 판단
→ 서버가 라파이 소리 큐에 bus_arrival 추가
→ 라파이 스피커에서 bus_arrival.wav 재생
```

- `bus_arrival`: 버스/셔틀이 게이트에 도착했다는 안내
- 기존 `shuttle_arrival`, `play_shuttle_arrival_sound`는 이 타입으로 통합

## 최종 소리 타입

| command type | 의미 | 음원 |
| --- | --- | --- |
| `bus_arrival` | 버스/셔틀 도착 | `bus_arrival.wav` |
| `robot_departure` | 로봇 출발(A → C, C → A 모두 포함) | `robot_departure.wav` |
| `robot_arrival` | C 도착 | `robot_arrival.wav` |
| `robot_return_complete` | A 복귀 완료 | `robot_return_complete.wav` |

## 핵심 정리

```text
출발/도착/복귀 완료 안내음은 서버 → 라파이 소리 큐
C → A 복귀 시작 트리거는 라파이 터치 센서 → 서버 → 젯슨
복귀 완료 판단은 젯슨 → 서버 → 라파이
```
