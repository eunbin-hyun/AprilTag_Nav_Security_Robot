# 라파이 → 젯슨: C → A 복귀 트리거 흐름 확인 요청

기준일: 2026-08-06

C → A 복귀는 라파이 터치 센서가 트리거하는 구조로 맞추고 싶습니다.

## 요청 흐름

```text
1. 로봇이 C(목적지/태깅 위치)에 도착
2. 사용자가 라파이 터치 센서를 누름
3. 라파이가 서버로 복귀 요청 이벤트 전송
4. 서버가 젯슨에게 return_to_charge 명령 전달
5. 서버가 라파이 소리 큐에 robot_departure 추가
6. 젯슨이 C → A 복귀 로직 실행
7. 젯슨이 A(충전스테이션) 도착/DOCKED 판단
8. 젯슨이 서버로 RETURN_COMPLETE 또는 DOCKED 상태 전송
9. 서버가 라파이 소리 큐에 robot_return_complete 추가
```

## 확인 부탁드릴 점

- 서버에서 `return_to_charge` 명령을 받았을 때 C → A 복귀 로직을 실행하면 되는지 확인 부탁드립니다.
- 복귀 완료 시 `/route/state = DOCKED` 또는 `mission_status = RETURN_COMPLETE`를 서버로 보내는지 확인 부탁드립니다.
- `robot_return_complete`는 젯슨이 직접 라파이에 보내는 게 아니라, 젯슨 → 서버 상태 보고 후 서버가 라파이 소리 큐에 넣는 구조로 보면 되는지 확인 부탁드립니다.

## 라파이 소리 타입

라파이 쪽 소리 타입은 다음 4개로 정리 예정입니다.

| command type | 의미 |
| --- | --- |
| `bus_arrival` | 버스/셔틀 도착 |
| `robot_departure` | 로봇 출발(A → C, C → A 모두 포함) |
| `robot_arrival` | C 도착 |
| `robot_return_complete` | A 복귀 완료 |
