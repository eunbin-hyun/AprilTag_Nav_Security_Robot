# 라즈베리파이(raspberry01) → 관제 서버 HTTP POST API 명세

> 최신 버전: `hardware/rpi/gate_server.py` 기준

라즈베리파이는 RFID 태깅 이벤트와 ToF 빔 통과 이벤트를 서버로 보고합니다. 최종 미태깅 판정은 서버의 크레딧 상태머신이 수행하고, 라즈베리파이는 현장 확인용 1차 판단을 `result` 필드로 함께 보냅니다.

센싱은 IDLE 상태로 시작합니다. 첫 번째 3초 터치에서 RFID/ToF 센싱을 시작하고, 두 번째 3초 터치에서 센싱을 중지한 뒤 중앙 서버에 복귀 요청을 보냅니다.

---

## 1. 기본 스펙

| 항목 | 결정 내용 |
| :--- | :--- |
| 이벤트 엔드포인트 | `POST /api/tagging-events`, `POST /api/gate-pass-events` |
| 복귀 엔드포인트 | `POST /api/dispatch/commands/return` |
| 인증 | `.env`의 `API_KEY`를 `X-API-Key` 헤더로 전송 |
| 타임아웃 | 3초 (`HTTP_TIMEOUT_S`) |
| 재시도 정책 | 2xx 성공 시 완료, 4xx는 버림, 그 외 실패/예외는 1초부터 30초까지 exponential backoff |
| 장치 식별 | `DEVICE_ID`, `GATE_NO`는 `.env`로 덮어쓰기 가능 |
| 시각 필드 | `observed_at`, `beam_a_ts`, `beam_b_ts`는 KST(+09:00) 포함 ISO timestamp |

---

## 2. 응답 코드 처리

| 응답 코드 | 라즈베리파이 처리 |
| :---: | :--- |
| 2xx | 성공, 다음 이벤트 진행 |
| 4xx | 요청을 버리고 다음 이벤트 진행. 401/403이면 `X-API-Key` 확인 힌트 출력 |
| 5xx / 예외 | 같은 이벤트를 재시도. 지연 시간은 1초부터 시작해 30초까지 증가 |

`event_id`는 `{device_id}-{unix_ms}-{seq}` 형식의 멱등 키입니다. 서버는 같은 `event_id`가 다시 들어와도 중복 저장하지 않아야 합니다.

---

## 3. RFID 태깅 이벤트

### `POST /api/tagging-events`

```json
{
  "event_id": "raspberry01-1721635200123-0001",
  "device_id": "raspberry01",
  "gate_no": 5,
  "tag_id": "A1B2C3",
  "observed_at": "2026-07-22T14:30:10.000+09:00"
}
```

| 필드 | 설명 |
| :--- | :--- |
| `event_id` | 라즈베리파이가 생성한 멱등 키 |
| `device_id` | 장치 ID |
| `gate_no` | 물리 게이트 번호 |
| `tag_id` | RFID 태그 UID |
| `observed_at` | RFID 태깅이 발생한 시각 |

---

## 4. ToF 통과 이벤트

### `POST /api/gate-pass-events`

ToF 빔 A/B 통과 이벤트를 하나로 묶어 보고합니다. 현재 `MATCH_WINDOW_S = 2`이므로 한쪽 빔 감지 후 2초 안에 반대쪽 빔이 감지되면 `complete`, 아니면 `incomplete`로 보냅니다.

```json
{
  "event_id": "raspberry01-1721635212540-0002",
  "device_id": "raspberry01",
  "gate_no": 5,
  "direction": "A_TO_B",
  "beam_a_ts": "2026-07-22T14:30:12.310+09:00",
  "beam_b_ts": "2026-07-22T14:30:12.540+09:00",
  "observed_at": "2026-07-22T14:30:12.540+09:00",
  "status": "complete",
  "result": "untagged"
}
```

| 필드 | 설명 |
| :--- | :--- |
| `event_id` | 라즈베리파이가 생성한 멱등 키 |
| `device_id` | 장치 ID |
| `gate_no` | 물리 게이트 번호 |
| `direction` | `A_TO_B`(입장), `B_TO_A`(퇴장), `null`(불완전) |
| `beam_a_ts` / `beam_b_ts` | 각 빔 감지 시각. 미감지 시 `null` |
| `observed_at` | 통과 이벤트 관측 시각 |
| `status` | `complete` 또는 `incomplete` |
| `result` | 라즈베리파이 1차 판단. `normal`, `untagged`, `exit`, `null` |

---

## 5. 로컬 1차 판단

태깅 위치가 빔 A와 겹쳐 있어, RFID 태깅 직후에는 잠시 빔 A를 무시하고 빔 B 확인을 기다립니다.

```text
1. RFID 태깅 발생 -> 태깅 대기 상태 진입 (TAG_ARM_TIMEOUT_S = 3초)
2. 태깅 대기 중 빔 B 감지 -> result = "normal"
3. 태깅 대기 3초 초과 -> 태깅 대기 해제
4. 태깅 없이 A -> B 통과 완성 -> result = "untagged"
5. B -> A 통과 완성 -> result = "exit"
6. 한쪽 빔만 감지 -> status = "incomplete", result = null
```

이 값은 서버 최종 판정을 대체하지 않습니다. 서버 판정과 대시보드 표시에서는 서버의 `verdict`와 라즈베리파이의 `device_result`를 구분해서 봅니다.

---

## 6. 복귀 요청

### `POST /api/dispatch/commands/return`

두 번째 3초 터치에서 센싱을 중지한 뒤 호출합니다. 요청 본문은 없습니다. `.env`의 `API_KEY`가 있으면 이벤트 전송과 동일하게 `X-API-Key` 헤더를 붙입니다.

성공 응답 예:

```json
{
  "command_id": "command-123",
  "notified_robot_count": 1
}
```

라즈베리파이는 성공 시 `command_id`와 `notified_robot_count`를 로그로 출력합니다. `notified_robot_count`가 1 이상이면 서버가 Jetson 쪽에 복귀 명령을 전달한 것입니다.

---

## 7. 현재 ToF 튜닝값

```text
TOF_TIMING_BUDGET_MS = 20
BEAM_THRESHOLD_CM = 20
BLOCK_CONFIRM_SAMPLES = 2
OPEN_CONFIRM_SAMPLES = 5
STALE_BLOCK_S = 1.0
STALE_RESET_DELTA_CM = 3.0
MATCH_WINDOW_S = 2
DEBOUNCE_S = 1
TOF_POLL_S = 0.05
```
