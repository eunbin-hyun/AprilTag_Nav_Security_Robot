# 라파이 → 백엔드: 라파이 소리 큐 타입 정리 요청

기준일: 2026-08-06

라파이 스피커용 command type을 아래 4개로 정리하고 싶습니다.

| command type | 의미 | 라파이 기본 음원 파일 |
| --- | --- | --- |
| `bus_arrival` | 버스/셔틀이 게이트에 도착했을 때 | `bus_arrival.wav` |
| `robot_departure` | 로봇이 현재 위치에서 다음 지점으로 출발했을 때(A → C, C → A 모두 포함) | `robot_departure.wav` |
| `robot_arrival` | 로봇이 C(목적지/태깅 위치)에 도착했을 때 | `robot_arrival.wav` |
| `robot_return_complete` | 로봇이 A(충전스테이션)로 복귀 완료했을 때 | `robot_return_complete.wav` |

## 정리 요청

- 기존 `shuttle_arrival`, `play_shuttle_arrival_sound`는 `bus_arrival`로 통합 부탁드립니다.
- 기존 `departing_to_destination`은 `robot_departure`로 통합 부탁드립니다.
- 기존 `arrived_at_destination`은 `robot_arrival`로 통합 부탁드립니다.
- `robot_return_complete`는 복귀 시작이 아니라, 젯슨이 A 도착/`DOCKED`를 판단한 뒤의 복귀 완료 안내입니다.
- 같은 이벤트에 대해 `arrived_at_destination`과 `robot_arrival`을 동시에 큐에 넣으면 중복 안내가 날 수 있으니 하나만 넣어 주세요.

## C → A 복귀 흐름

```text
라파이 터치 센서 감지
→ 라파이가 서버로 복귀 요청 전송
→ 서버가 젯슨에게 복귀 명령 전달
→ 서버가 라파이 소리 큐에 robot_departure 추가
→ 젯슨이 C → A 복귀 로직 실행
→ 젯슨이 A 도착/DOCKED 판단
→ 서버가 라파이 소리 큐에 robot_return_complete 추가
```

`robot_departure`는 C → A 복귀를 시작할 때도 사용합니다. 음성 문구는 방향을 특정하지 않고 "로봇이 출발합니다"처럼 잡는 것이 안전합니다.

`robot_return_complete`는 위 흐름의 마지막 단계에서만 보내는 완료 안내입니다.
