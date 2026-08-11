# C207 싸큐리티 관제 서버 (백엔드)

로봇 밖 전부 — 이벤트 인입·저장·실시간 중계를 맡는 FastAPI 서버다. 스택은 FastAPI + SQLAlchemy 2(async/asyncpg) + alembic + pydantic v2, Python 3.12, uvicorn 워커 1개 전제.

정본: `docs/아키텍처_2026-07-23.html`(최신 확정판) · `docs/0722_숙제_세욱_역할상세_2026-07-22.md`.

## 지금 구현된 범위

- 인입 API 3종 — `POST /api/tagging-events`, `POST /api/gate-pass-events`, `POST /api/shuttle-arrivals`. 전부 `X-API-Key` 인증 + `event_id` 멱등(UNIQUE + `ON CONFLICT DO NOTHING`). **필수 칸은 창구마다 다르다 — 아래 "인입 필수 칸" 절.** 세 계약 모두 **[초안·팀 확정 대기]** 옵션 칸 `room_id`(32자)·`boot_id`(16자)를 받는다 — 안 보내면 `null`이라 기존 퍼블리셔가 그대로 통과한다. `boot_id`는 안 보내도 `event_id`(`{device_id}-b{boot8}-{unix_ms}-{seq}`)에 들어 있으면 서버가 뽑아 채운다. 두 칸의 **판정 효과**는 플래그(`CREDIT_SCOPE_ROOM`·`EVENT_ID_REQUIRE_BOOT_ID`) 뒤에 있고 기본은 꺼짐이지만, **DB 컬럼은 플래그와 무관하게 이미 있다**(마이그레이션 0002·0003).
- 조회 API — `GET /api/dashboard/snapshot`(초기 로드, **아키텍처 02절 목록 밖 신규 — 팀 확정 대기**), `GET /api/robots`, `GET /api/events`(`?kind=`로 인입 계열을 추린다), `GET /api/alerts`(`?type=untagged`로 종류를, `?source_type=&source_id=`로 발화 이벤트를 추린다), `POST /api/alerts/{id}/ack`, `GET /api/eta`, `GET /healthz`.
- 신원 확인 API 3종 — `POST`·`GET`·`DELETE /api/alerts/{id}/identify`(미태깅 통과자 사후 신원 확인). 셋 다 **아키텍처 02절 목록 밖 신규 — 팀 확정 대기**다.
- 셔틀 가상 호출 — `POST /api/shuttle-calls`. 관제 화면이 버튼으로 셔틀 도착 각본을 시작하는 자리다(셔틀은 실기기를 안 만든다 — 사용자 확정). **인입 3종과 달리 여기만 인증이 없고**, 대신 게이트별 5초 쿨다운이 유일한 유량 제한이다. **아키텍처 02절 목록 밖 신규 — 팀 확정 대기.** 자세한 계약은 아래 "셔틀 가상 호출" 절.
- 출동 창구 7종 — `GET`·`POST /api/dispatch/default-destination`(그날 기본 목적지 조회·수동 지정), `GET /api/dispatch/state`(열린 대기 창·긴급정지·그날 기본값 복구), `POST /api/dispatch/pending/{arrival_id}/choose`(5초 대기 창 안에서 실내·실외·대기 고르기), `POST /api/dispatch/commands/move`·`/return`·`/emergency-stop`(즉시 이동·복귀·긴급정지 세우기와 풀기). **일곱 다 기본이 무인증이다** — `DISPATCH_REQUIRE_API_KEY`가 기본 꺼짐이라 켜야 `X-API-Key`를 요구한다(셔틀 가상 호출과 같은 이유로, 화면이 기기 키를 들면 인입까지 통째로 열린다). 대신 이동·복귀에 `DISPATCH_COMMAND_MIN_INTERVAL_SEC`(기본 1초) 연타 제한이 붙고 긴급정지 창구만 그 제한을 안 받는다. 자세한 계약은 `app/routers/dispatch.py` 모듈 머리와 `app/dispatch.py`.
- 관제 보조 API 3종 — `GET /api/assistant/daily-summary`, `GET /api/assistant/alert-brief/{id}`, `POST /api/assistant/chat`. 조회 데이터만 근거로 문장을 만들고 로봇 명령 갈래가 없다. **아키텍처 02절 목록 밖 신규 — 팀 확정 대기**이고, 읽기만 해도 GMS 크레딧을 태우므로 `X-API-Key`를 요구한다.
- **인증 경계** — 읽기(GET 조회 계열)는 열어 두고, 상태를 바꾸는 갈래(`/ack`·`/identify` POST·DELETE)와 신원 원문 읽기(`/identify` GET), 관제 보조 3종에 인입과 같은 `X-API-Key`를 건다. 자세한 건 아래 "인증은 어디까지 걸렸나".
- 통계 조회 API 5종 — `GET /api/stats/untagged`(미태깅 일자별 추이 + 이동평균 + 요일×시간대 히트맵), `GET /api/stats/today`(오늘 KST 종류별 건수), `GET /api/stats/alert-response`(경고 확인·종결 소요시간, 프론트 요구 N2), `GET /api/stats/gate-pass-buckets`·`GET /api/stats/alert-buckets`(15분·24시간 버킷, 프론트 요구 N12). 전부 **아키텍처 02절 목록 밖 신규 — 팀 확정 대기**다. 조회 계열이라 인증 없이 연다. 집계만 하고 예측 모델은 안 쓴다(AI 활용 조사 정본 2026-07-16). 자세한 계약은 아래 "미태깅 통계"·"오늘 집계"·"경고 대응 통계"·"시간 버킷 집계" 절.
- 크레딧 상태머신 — 게이트 단위 FIFO 소비·lazy expiry·퇴장 회수(**기본 꺼짐** — 아래 참고)·미태깅 알림(`app/credit/state_machine.py`). 시계가 둘이다 — **판정 창**(소비 가능 여부·저신뢰 집계)은 기기가 보낸 이벤트 시각(`beam_a_ts`), **만료 청소**(남은 issued를 expired로 닫기)는 이벤트 시각과 서버 수신 시각 중 이른 쪽이다(시계 둘의 합의 — 양쪽이 다 만료로 볼 때만 닫는다). 기기 시각 하나로 재면 라파이 시계가 미래로 한 번 튀는 순간 그 게이트의 유효 크레딧이 통째로 만료되고, 서버 시각 하나로 재면 백로그·배속으로 뒤늦게 밀려온 통과가 남의 유효 크레딧을 닫아 정상 통과가 미태깅으로 뒤집힌다. 청소는 입장(A→B)·퇴장(B→A) 양쪽에서 **판정 뒤에** 돈다(순서가 뒤집히면 정상 통과가 미태깅으로 뒤집힌다). **퇴장 회수는 지금 꺼져 있다** — `app/credit/state_machine.py`의 `EXIT_RECOVERY_ENABLED = False`이고 **팀 결정 대기**다(정본 아키텍처 468행은 회수한다고 적었는데, 이 회수가 구조적으로 남의 크레딧만 먹는다는 실측이 나왔다 — 태깅은 게이트 바깥에서 일어나니 B→A 퇴장을 만드는 사람은 크레딧 주인이 아니다). 상수를 True로 되돌리면 아래 거동이 그대로 산다. 그 켜진 경로도 시계 둘의 합의로 도는데 방향이 반대다 — 두 시각 중 **늦은 쪽**으로 견줘, 이벤트 시각으로도 서버 수신 시각으로도 TTL 안인 크레딧만 회수한다. 한쪽이라도 만료로 보면 회수하지 않고 청소에 맡긴다. 기기 시계가 과거로 튄 퇴장에서는 청소(이른 쪽 기준)가 아무것도 안 닫기 때문에, 이 가드가 없으면 벽시계로 이미 만료된 크레딧이 회수돼 회수 기록이 엉뚱한 통과를 가리킨다.
- WebSocket 2종 — `/ws/dashboard`, `/ws/robot`(로봇 outbound — 상태 수신·알림 중계·명령 발신·하트비트). **두 전선 모두 메시지가 `{type, version, robot_id, timestamp, data}` 고정이다**(2026-08-04 통일). 아래 "로봇 outbound WS 프레임" 참고.
- 명부·계정 창구 — `/api/staff/*`(요원용 `GET /by-tag/{tag_id}` 하나 + 팀장급 목록·단건·감사·등록·수정·해제), `/api/users/*`(관리자 전용 목록·생성·수정·비밀번호 재설정·`GET /{id}/audit`). **둘 다 `AUTH_REQUIRE_LOGIN`과 무관하게 늘 역할을 판정한다** — 아래 "로그인을 켠 뒤 역할 게이트" 절 끝의 ⚠ 참고.
- 로그인 창구 — `POST /api/auth/login`·`GET /api/auth/me`·`POST /api/auth/logout`. 세션 쿠키를 심고 지운다. 자세한 건 아래 "로그인을 켠 뒤 역할 게이트".
- 개발자 텔레메트리 페이지 — `GET /dev/telemetry`. FastAPI가 직접 서빙하는 단일 HTML+바닐라 JS. **배포에서는 nginx `auth_basic`이 앞에 한 겹 더 있다**(`deploy/nginx-c207-api.conf`의 `location /dev/`).
- ⛔ **문서 창구 셋(`/docs`·`/redoc`·`/openapi.json`)은 꺼져 있다**(2026-08-02, 로그인 설계 확정본 §8 결정 2 · 사용자 확정). `app/main.py`가 `docs_url`·`redoc_url`·`openapi_url`을 전부 `None`으로 줘서 FastAPI가 그 라우트를 **아예 안 단다** — 권한 거절이 아니라 라우트 부재라 **404**다. nginx 조각도 같은 셋을 404로 끊어 두 겹이다(nginx는 젠킨스 배포 밖이라 손 배포 전까지는 앱 층이 유일한 방어다). **스키마가 사라진 건 아니고 HTTP 창구만 없다** — 시험·도구가 명세를 읽어야 하면 `app.openapi()`를 직접 부른다(딕셔너리가 그대로 나온다). ⚠ 기동 확인에 `/openapi.json`을 쓰지 마라. 그 자리는 `/healthz`다.
- 미태깅 매터모스트 알림 — 경고가 나는 순간 Incoming Webhook으로 카드를 밀어낸다(S15P11C207-82).
- DB 모델 14개(`app/models.py`의 `Base` 상속 클래스 수) + alembic 마이그레이션 10개. `0001` 초기 스키마, `0002` 인입 3계열 `room_id`, `0003` 인입 3계열 `boot_id`, `0004` 경고에 사후 신원 확인 칸 4개(`identified_person`·`identified_by`·`identified_at`·`identify_note`, 전부 nullable·인덱스 없음), `0005` 경고 생애주기 칸(`0005_alert_lifecycle.py`). 로봇 채널·ETA를 붙일 때는 스키마 변경이 없었다.
  - `0006` 그날 기본 목적지 표 `daily_default_destination`(`d15p0006dest` — 셔틀 출동 설계 §2.1. 아침에 날씨로 정한 값이 인메모리면 재기동 한 번에 사라진다).
  - `0007` 빔 통과 행에 소비한 크레딧의 카드 UID 칸 `gate_pass_event.tag_id`(`e241tag00007` — S15P11C207-241).
  - `0008` 로그인 시스템(`f10g1n000008` — `app_user`·`auth_session` 표 둘 + `staff` 칸 확장 + `staff_audit` 감사 기록. S15P11C207-349·-347).
  - `0009` 자주 도는 질의 축에 인덱스 넷(`g1nd3x000009` — `ix_tagging_event_credit_window`·`ix_gate_pass_event_observed_at`·`ix_alert_source`·`ix_alert_created_at_id`. 백지검토 보류 F47~F50). **인덱스 CREATE뿐이라 칸·표·자료를 한 줄도 안 바꾼다** — downgrade가 `DROP INDEX`라 손실 없이 닫힌다. 축을 고른 실측 근거는 파일 머리에 있다.
  - `0010` 이벤트 피드 정렬 축에 인덱스 셋(`h1feed000010` — `ix_tagging_event_received_at_id`·`ix_gate_pass_event_received_at_id`·`ix_shuttle_arrival_received_at_id`, 셋 다 `(received_at, id)`). **`0009`가 이 조회를 하나도 안 덮었다** — `0009`의 `ix_gate_pass_event_observed_at`은 기기 관측 시각이고 피드는 서버 수신 시각으로 줄을 세운다. 그래서 화면이 붙을 때마다 인입 3표를 통째로 훑고 정렬했다. 짝지어 잰 값이 `GET /api/dashboard/snapshot` 응답 p50 33.75 ms → 9.93 ms, 피드 SQL 26.11 ms → 0.10 ms다(자료 12.4만 행. 실측표는 파일 머리). `0009`와 같이 인덱스 CREATE뿐이라 자료를 안 바꾸고 downgrade가 `DROP INDEX`다.
  - ⚠ `0002`·`0003`은 **플래그와 무관하게** 컬럼을 만든다. `credit_scope_room`·`event_id_require_boot_id`를 안 켜도 DB엔 `room_id`·`boot_id` 여섯 칸이 이미 있다(EC2 실측). 안건 ①②가 반려되면 되돌림 마이그레이션이 따로 필요하다.
  - 저장소 현 head는 `h1feed000010`(=`0010`)이다. 예전 표기 `g1nd3x000009`는 `0010` 이전 값, `f10g1n000008`은 `0009` 이전 값이고, 더 예전 `c10life0005`는 `0006`~`0008` 이전 값이라 전부 낡았다. ⚠ **EC2 운영 DB의 `alembic_version`이 실제로 어디까지 올라가 있는지는 이 문서에서 확인 못 했다** — 배포 전에 `docker exec c207-backend alembic current`로 직접 봐라(확인 필요).
- 공용 메시지 스키마 3종 — `schemas/detection_event.schema.json`, `robot_state.schema.json`, `command.schema.json`.
- CLI 5종 — `tools/dummy_publisher.py`(라파이 인입), `tools/dummy_robot.py`(로봇 outbound WS), `tools/ws_probe.py`(대시보드 수신), `tools/purge_test_data.py`(시험 자료 정리 — 아래 "시험 자료 정리 도구" 절), `tools/seed_users.py`(로그인 계정 심기 — 2026-08-02 들어왔다).

### 인입 필수 칸 (2026-08-04 확정 — 느슨한 쪽)

정본 `schemas/detection_event.schema.json`이 `required`로 `["event_id","kind","observed_at"]`을 적어 뒀는데 서버는 그 셋을 요구한 적이 없었다. 정본만 보고 만든 생산자가 `kind`를 실으면 조용히 버려지고, 셔틀은 `observed_at` 때문에 422를 맞는데 문서로는 원인을 못 짚었다(전체검토 2026-08-04 L11). **모델을 조이면 라파이가 지금 보내는 프레임이 막혀 시연이 깨지므로 정본 쪽을 서버 실측값에 맞췄다.**

| 창구 | 필수 칸 | 그 밖에 받는 칸(전부 옵션) |
|---|---|---|
| `POST /api/tagging-events` | `event_id` · `tag_id` · `observed_at` | `device_id` · `gate_no` · `room_id` · `boot_id` |
| `POST /api/gate-pass-events` | `event_id` · `observed_at` | `device_id` · `gate_no` · `room_id` · `boot_id` · `direction` · `status` · `beam_a_ts` · `beam_b_ts` · `result` · `tag_id` |
| `POST /api/shuttle-arrivals` | `event_id` · `signal_ts` | `gate_no` · `room_id` · `boot_id` · `shuttle_no` |

세 창구 공통 필수는 `event_id` 하나뿐이라 정본의 최상위 `required`도 그 하나다. 창구별 목록은 정본의 `x-required-by-endpoint`에 있고, **코드와 갈리면 `tests/test_ingest_contract_required.py`가 깨진다** — 인입 모델의 필수 칸을 고치면 정본도 같이 고쳐야 한다.

**안 받기로 한 칸 둘.** 지우기만 하면 다음 사람이 "왜 빠졌지" 하고 되돌리니 사유를 같이 남긴다.

- **`kind`** — 본문으로 안 받는다. 창구 URL이 셋으로 갈려 있어 서버가 계열을 스스로 안다. 조회가 내려주는 `kind`는 `app/routers/query.py`가 표마다 `literal(...)`로 붙이는 값이지 기기가 보낸 값이 아니다. 본문에서 받으면 URL과 본문이 어긋날 때 뭘 믿을지를 또 정해야 하고, 지금은 pydantic 기본(`extra=ignore`)이라 실어 보내도 조용히 버려진다. **다시 볼 자리** — 한 창구가 여러 계열을 한 프레임에 받는 **통합·배치 인입**을 만들면 그때는 본문 `kind`가 있어야 계열이 갈린다.
- **셔틀의 `observed_at`** — 없앤 게 아니라 이름이 `signal_ts`다. 셔틀은 "관측"이 아니라 "도착 신호"라 축 이름을 갈랐고, 조회·통계가 읽을 때 `signal_ts`를 `observed_at` 자리에 끼워 준다(`app/routers/query.py:274`, `app/stats.py:463-464`). 셔틀 인입에 `observed_at`을 실어 보내면 버려진다. **다시 볼 자리** — 인입 세 표를 한 이벤트 표로 합치거나 "인입 시각은 offset 필수"를 정하는 회의 안건(아래 "아직 아닌 것")을 처리할 때 축 이름을 하나로 합칠지 같이 본다.

⚠ 정본에 `additionalProperties`는 **일부러 안 걸었다.** 걸면 기기가 지금 보내는 여분 칸이 통째로 막힌다. 대신 모르는 칸이 조용히 버려지는 건 전체검토 L12로 따로 남아 있다(이번 판단 밖).

### 대시보드 WS 메시지 종류

| type | 언제 | 핵심 `data` 칸 |
|---|---|---|
| `hello` | 대시보드가 붙을 때 한 번 | channel · clients |
| `tagging_event` | 태깅 인입 | event_id · gate_no · tag_id |
| `gate_pass_event` | 빔 통과 인입(판정 포함) | pass_id · verdict · matched_tagging_event_id · tag_id · low_confidence · device_result |
| `untagged_alert` | 미태깅 판정 | alert_id · source_id(=통과의 `pass_id`) · low_confidence |
| `alert_ack` | 경고를 확인 처리했을 때 | alert_id · type · severity · ack |
| `alert_identified` | 경고에 신원을 적었거나 지웠을 때 | alert_id · type · severity · identified · identified_at · corrected |
| `alert_status` | 사건 수명주기가 실제로 전진했을 때(`POST /api/alerts/{id}/status`) | alert_id · type · severity · status · ack · resolution · resolved_at |
| `shuttle_arrival` | 셔틀 도착 인입 | arrival_id · event_id · gate_no · shuttle_no · notified_robot_count · command_id · pending |
| `shuttle_dispatch_result` | 출동 목적지가 확정됐을 때(대기 창 마감·요원 선택·즉시 명령·긴급정지 차단·대기 창 꺼짐·재전송) | arrival_id · event_id · gate_no · source · command · destination · emergency_stop · command_id · notified_robot_count · decided_at |
| `robot_state` | 로봇이 상태를 올릴 때 | status_summary · odom · imu · sensor_health · log |
| `robot_status` | 그 보고로 로봇 카드가 갱신됐을 때 | id · name · mode · battery · network_status · updated_at · position · led_status · mission_status |
| `robot_arrival` | `mission_status`가 ARRIVED로 **바뀔 때** | alert_id · message · shuttle_arrival_id · from_status · to_status |
| `robot_departure` | ARRIVED에서 벗어날 때(게이트 출발) | alert_id · message · shuttle_arrival_id · from_status · to_status |
| `robot_return_complete` | 로봇 복귀 완료 | alert_id · message · from_status · to_status |
| `robot_weather_blocked` | 우천 미출동 | alert_id · message(severity=warning) |

**`robot_state`와 `robot_status`는 다르다.** 상태 보고 한 건에 둘이 짝으로 나가는데 싣는 게 갈린다. `robot_state`는 로봇이 올린 프레임 원본 중계라 DB에 안 남는 값(odom·imu·sensor_health)까지 그대로 흐르고, `robot_status`는 그 보고로 **로봇 카드가 어떻게 됐나**다. 뒤엣것의 칸은 스냅샷 `robots[]` 한 줄과 똑같아서, 화면이 스냅샷 다시 읽지 않고 로봇 카드를 그 자리에서 갈아끼운다. 카드 아홉 칸 중 앞 여섯은 `Robot` 행 값이고, `position`·`led_status`·`mission_status`는 DB 컬럼이 아니라 서버 메모리에 든 최신값이다(`mission_status`는 프론트 요구 N10으로 2026-08-01에 붙었다 — 계약은 아래 스냅샷 절에 적었다). 원본 프레임에서 화면이 직접 카드 값을 뽑으면 "안 실린 필드는 안 덮는다"는 절대 상태 규칙이 서버·화면 두 벌로 갈라지기 때문에, 그 판정은 서버 한 곳에서만 한다(S15P11C207-81).

미션 알림 메시지는 전이를 `from_status`·`to_status`로 싣는다. `mission_status`는 "이 알림이 무슨 상태에 대한 알림인가"라서 출발 메시지에서는 `null`이다 — 출발은 `mission_status` enum에 없는 사건이고, 여기에 새 상태(RETURN_COMPLETE)를 실으면 화면이 출발을 복귀완료로 읽는다.

### mission_status는 에지 트리거다

RobotState는 절대 상태라 주기 보고마다 같은 `mission_status`가 계속 실려 온다. 그래서 알림은 **값이 바뀐 순간만** 낸다 — 도착 보고가 5번 와도 도착 알림 1건, Alert 행 1건, ETA 표본 1건이다. ARRIVED에서 다른 상태로 넘어가는 전이는 출발(`robot_departure`)로 도착과 대칭으로 남긴다. 직전 상태는 인메모리로 들기 때문에 서버를 재기동하면 다음 보고가 다시 전이로 잡힌다(크레딧 쿨다운과 같은 성질). 다만 **로봇 커넥션이 끊기는 것만으로는 안 버린다** — 로봇은 Wi-Fi가 한 번 끊겼다 붙어도 같은 자리에 서 있어서, 버리면 재접속 첫 보고가 가짜 도착·미짝 신호 오귀속·가짜 ETA 표본을 한꺼번에 만든다. 상태 수명은 `MISSION_STATE_TTL_SEC`(기본 3600초) 청소 하나가 정하고, 기준은 **마지막 보고 시각**이라 게이트에 오래 서 있는 로봇의 상태는 안 걷힌다. 그 "마지막 보고"는 `mission_status`가 실린 보고만이 아니라 **그 로봇의 모든 상태 보고**다 — 주행 중엔 `mission_status`를 비우고 올리는 로봇이 있어서, 실린 보고만 세면 게이트로 달려가는 동안 상태가 걷히고 도착해서 올린 ARRIVED가 가짜 도착이 된다. 반대로 TTL을 이미 넘긴 상태는 보고 한 건으로 되살리지 않는다(청소를 먼저 밟고 갱신한다) — 한참 사라졌다 돌아온 로봇의 진짜 도착이 묻히면 안 되니까.

**연속 도착(복귀 보고 없는 재출동)은 지금 안 잡는다.** 게이트1에 도착한 로봇이 복귀 보고 없이 게이트2로 재출동하면 `mission_status`는 ARRIVED에서 ARRIVED 그대로다 — enum에 "이동 중" 같은 중간 상태가 없어 로봇이 피할 방법이 없다. "직전 도착 뒤에 새 목적지 명령이 나갔으면 새 도착"으로 치는 길은 `ARRIVAL_EDGE_ON_RECOMMAND`로 열 수 있는데 **기본은 꺼짐**이다. 명령이 붙어 있는 로봇 전부에게 브로드캐스트라, 켜 두면 게이트에 서 있는 로봇도 새 신호마다 명령을 받아 다음 주기 보고가 가짜 도착이 된다(0.5초 보고 주기가 ETA 구간 표본으로 들어가 예측을 무너뜨린다). 제대로 잡는 정공법은 `robot_state.schema.json`의 `mission_status`에 중간 상태를 넣는 것이고 월요일 팀 안건이다.

도착을 어느 셔틀 신호에 묶느냐는 짐작으로 하지 않는다. 순서는 셋이다. ① 목적지 명령을 보낼 때 남긴 인메모리 상관(이 로봇에 이 신호로 명령했다), ② DB에 이 로봇 앞으로 통지됐다고 남은 신호(`notified_robot_id`) — 재기동으로 ①이 비어도 여기서 짝짓기를 이어받는다, ③ **어느 로봇에도 명령 안 된** 신호. 딴 로봇이 받아 출동 중인 신호를 늦게 접속한 로봇의 도착이 가로채면 게이트가 뒤바뀌니, ②는 robot_pk가 맞는 신호만 보고 ③은 명령된 신호를 통째로 뺀다. 명령했던 게이트를 알면 ②·③ 모두 그 게이트 안에서만 찾고, 그 안에서는 가장 **오래된** 유효 미짝 신호부터 채워 옛 신호가 영구 미짝으로 남지 않게 한다.

`ARRIVAL_MATCH_TTL_SEC`(기본 900초)을 넘긴 미짝 신호는 `shuttle_arrival_expired` Alert로 닫는다. 청소는 **도착 처리마다** 돈다(상관관계로 짝이 맞아도 돈다 — 폴백 경로에만 두면 명령을 잘 받는 로봇이 계속 도착하는 동안 옛 신호가 영구히 열린 채로 남는다). 명령 상관 큐도 같은 TTL로 걷어내는데, 그 청소는 도착이 아니라 명령 발신 때 돈다 — 도착을 한 번도 안 올리는 로봇(전원 차단·통신 이탈)의 큐가 무한히 쌓이는 걸 막는 자리다.

### 로봇 outbound WS 프레임

로봇이 올리는 것은 `status_summary`·`odom`·`imu`(젯슨) 또는 `robot_state`(더미·시험) 그리고 `command_ack` · `pong`, 서버가 내려보내는 것은 `registered` · `state_ack` · `command` · `ping` · `error`다. 잘못된 프레임은 연결을 끊지 않고 `error` 메시지로 돌려준다.

#### ⚠ 이 전선의 envelope 본문 칸은 `data`다 (대시보드와 다르다)

```json
{ "type": "command", "version": "1", "robot_id": "RB1-01",
  "timestamp": "2026-08-01T10:47:40.213Z", "data": { "command_id": "shuttle-7" } }
```

- **왜** — 젯슨 web_bridge의 공통 envelope가 `{type, robot_id, timestamp, seq, data}`다(`hardware/jetson/ros2_ws/docs/젯슨_서버_송신_인터페이스_2026-08-01.md` §3). 젯슨을 안 고치고 **서버가 젯슨에 맞춘다**가 확정 방향이라(2026-08-01 사용자 결정), 로봇으로 나가는 프레임도 같은 칸으로 통일했다. 같은 문서 §8-4가 `command_ack`도 `data`로 간다고 못박아 뒀다.
- **대시보드(`/ws/dashboard`)도 `data`다**(2026-08-04 사용자 확정 · 프론트 합의). 예전에는 대시보드만 `payload`라 로봇 보고를 중계하는 `robot_state`·`robot_status`가 **한 보고인데 두 이름으로 갈라져 나갔다**. 두 이름을 같이 싣는 기간 없이 한 번에 옮긴 근거는 셋이다 — EC2에 떠 있는 관제 화면은 곧 내려갈 React 이고 목 모드로 굳어 서버를 안 부르며(지라 `-256`이 브라우저 네트워크 기록으로 실측했다 — `/api/*` 0건·WS 0본, 목 시뮬레이터가 화면을 그린다), 새 관제 화면은 아직 서버에 안 붙었고, 개발자 페이지는 서버와 같은 커밋으로 배포된다.

  ⚠ **서버 로그의 "최근 N분 호출 0건"은 이 근거가 못 된다.** 아무도 화면을 안 열었어도, 잘못된 빌드가 떠 있어도 똑같이 0건이다(`frontend/.env.production` 머리말이 2026-07-30 실사고로 기록해 둔 그 모양이다). 근거로 쓸 수 있는 건 "그 빌드가 무엇을 부르게 돼 있나"를 번들·네트워크 기록으로 실측한 `-256` 쪽이다.

  ✅ **닫혔다(2026-08-04, 커밋 `e739346`).** 예전 React 저장소가 `obj.payload ?? {}`로 읽어 실시간 갱신이 조용히 0건이 되는 갈래였는데, 프론트가 그 저장소를 걷어내고 단일 HTML(`frontend/index.html`) 한 장으로 갈아탔다. 새 화면은 `data`를 읽는다.
- **봉투 이름은 경계 밖 서버 코드에 못 들어온다.** 그 이름을 아는 자리는 셋뿐이다 — 씌우는 `app/ws.make_envelope`, 접는 `app/robot_channel.handle_robot_frame`, 젯슨 프레임을 접는 `app/jetson_adapter`. 그 밖은 전부 서버 dict로 흐른다. `make_robot_envelope`은 이제 `make_envelope`을 그대로 부르는 껍데기인데, 부르는 자리에서 로봇 전선임이 읽히고 젯슨이 `seq`를 요구할 때 갈라질 자리를 하나로 남겨 두려고 유지한다. 계약은 `tests/test_robot_wire_envelope.py`가 두 전선을 같이 못박고, 예전 이름 `payload`가 안 나가는 것까지 본다.
- **`robot_state` 인입도 `data`다**(2026-08-01 저녁, 전선 전체 통일). 로봇이 올리는 프레임에 `payload`라는 이름은 더 이상 없다 — 더미 로봇·시험 헬퍼까지 전수로 맞췄다.
- **명령은 자기를 밝힌 커넥션에만 간다** — `robot_state`를 한 번도 안 보낸 커넥션은 명령 대상에서 뺀다(정식 WS 인증 M-6 전까지의 최소 가드).
- **하트비트 미스 판정** — `ping`에 `pong`이 `WS_HEARTBEAT_MAX_MISS`(기본 2)번 연속 안 오면 그 커넥션을 스테일로 보고 닫아 `robot_count`에서 뺀다.
- **명령 메시지는 `CommandOut`으로 조립한다** — `SHUTTLE_DESTINATION`이 `command.schema.json`의 `cmd_destination` enum 밖이면 기동 시점에 설정 검증으로 터진다.

### 미태깅 매터모스트 알림 (S15P11C207-82)

미태깅 경고가 나는 순간 Incoming Webhook으로 카드 한 장이 보안 채널에 붙는다. 대시보드를 안 보고 있어도 사람이 알게 하는 자리다.

- **스위치는 `MATTERMOST_WEBHOOK_URL` 하나다.** 비어 있으면 조용히 스킵이고(로그도 debug 한 줄), 넣으면 그때부터 실제로 나간다. URL을 지우지 않고 발사만 멈추려면 `MATTERMOST_ENABLED=false`를 명시한다.
- **경고 흐름을 절대 안 막는다.** DB 커밋 뒤에 부르고, 타임아웃 3초에, 실패(연결 거부·타임아웃·4xx·5xx)는 전부 삼켜서 warning 로그만 남긴다. 웹훅이 죽어 있어도 판정·경고 행·대시보드 메시지는 그대로 간다.
- **비-2xx 응답도 실패로 센다** — 폐기된 웹훅 URL은 예외를 안 내고 4xx를 준다. 성공으로 세면 아무도 못 받은 경고가 '보냄'으로 기록된다.
- 시험은 `tests/test_notify_mattermost.py`가 127.0.0.1에 띄운 목 수신 서버로만 한다. **실제 팀 채널로 쏘는 시험은 안 만든다.**

### 사건 수명주기 창구 — `POST /api/alerts/{id}/status`

경고가 열렸다가 닫히는 축이다. 예전에는 대응 흔적이 `ack` 불리언 하나와 신원 칸 넷뿐이라 "어디까지 진행됐나"를 가리키는 값이 아예 없었고, 요원이 각본 ⑤단계를 끝까지 밟아도 화면에서 사건이 안 닫혔다. 낱말·전이 규칙의 정본은 `app/alert_lifecycle.py` 모듈 머리이고, 창구는 `app/routers/query.py`의 `transition_alert`다. 상태를 바꾸는 갈래라 `X-API-Key`를 요구한다.

상태는 넷이고 순위가 하나다 — `OPEN`(0) < `ACKED`(1) < `IDENTIFIED`(2) < `RESOLVED`(3).

| 상태 | 뜻 | 누가 올리나 |
| --- | --- | --- |
| `OPEN` | 경고가 났고 아무 대응 흔적이 없다 | 상태머신이 경고를 만들 때(서버 기본값) |
| `ACKED` | 관제가 확인을 눌렀다 | `POST /ack`, 이 창구 |
| `IDENTIFIED` | 안쪽 요원이 붙잡아 신원까지 적었다 | `POST /identify`만 |
| `RESOLVED` | 사건이 닫혔다. 닫은 사유가 같이 남는다 | 이 창구(`resolution` 필수) |

요청 본문은 셋이다. `to`(필수 — `ACKED` 또는 `RESOLVED`), `actor`(옵션, 64자), `resolution`(RESOLVED일 때 필수). `resolution` 낱말은 `confirmed`(실제 미태깅) · `false_positive`(센서 오검출) · `duplicate`(같은 사건이 여러 경고로 났다) · `other` 넷이다 — 인증 없는 목록 API에 실리는 값이라 자유 문자열로 안 뒀고, "오검출 몇 %"를 세는 통계 축이기도 해서 낱말이 흔들리면 집계가 안 된다.

```bash
curl -X POST 'http://127.0.0.1:8000/api/alerts/18/status' \
  -H 'X-API-Key: dev-local-key' -H 'Content-Type: application/json' \
  -d '{"to": "RESOLVED", "resolution": "false_positive", "actor": "관제-1"}'
```

전이 규칙 일곱.

1. **전진만 된다.** 낮은 쪽으로 내리는 요청은 **409**다. 감사 기록이라 되돌리기를 기본값으로 열면 "닫힌 사건이 다시 열렸다"가 조용히 일어난다. 재개 창구가 필요한지는 팀 결정이다.
2. **건너뛰기는 된다.** `OPEN`에서 바로 `RESOLVED`로 갈 수 있다. 센서 오검출로 쌓인 경고를 하나하나 ack부터 밟게 하면 정리를 아예 못 한다.
3. **`ACKED`로 가는 전이만 `ack`·`acked_at`·`acked_by`를 찍는다.** `RESOLVED`로 건너뛴 전이는 `ack`를 안 건드린다 — 관제가 안 본 채로 일괄 종결한 건과 확인까지 한 건은 다른 사실이다.
4. **`IDENTIFIED`는 이 창구로 못 올린다(422).** 신원 원문 없이 그 상태로 올리면 감사 기록이 빈 채로 종결 후보가 된다. 그 상태를 올릴 자격은 신원 창구뿐이다.
5. **신원을 지우면 `IDENTIFIED`가 한 단계 내려온다**(`demote_after_identity_cleared`). 유일한 후퇴이고, 내려갈 자리는 `acked_at`이 있으면 `ACKED` 없으면 `OPEN`이다. 이미 `RESOLVED`인 사건은 안 건드린다.
6. **`RESOLVED`로 갈 때 `resolution`이 필수**(422)이고, **반대로 `ACKED`에 `resolution`을 실으면 그것도 422**다(종결일 때만 받는 칸이다).
7. **같은 단계로 다시 보내면 200 멱등이고 메시지는 안 나간다**(ack 재확인 규칙과 같다).

상태 코드 정리.

| 언제 | 코드 |
| --- | --- |
| 전이가 실제로 일어났거나 같은 단계 재요청 | `200` (응답은 `AlertOut`) |
| 없는 경고 번호 | `404` |
| 뒤 단계에서 앞 단계로 내리려 함 | `409` |
| `to`가 `IDENTIFIED`·`OPEN`이거나 enum 밖 | `422` |
| `RESOLVED`인데 `resolution`이 없음 / `RESOLVED`가 아닌데 `resolution`을 실음 | `422` |
| `actor`에 개행·제어문자·NUL·양방향 재정렬 문자 | `422`(신원 입력과 같은 전처리) |

- **`ack`와 `status`는 서로 다른 축이다. 파생 관계가 아니다.** `status`는 "어디까지 진행됐나", `ack`는 "관제 화면에서 확인을 눌렀나"다. **`ack=false`인데 `IDENTIFIED`인 행은 결함이 아니라 정의된 값**이고(관제는 아직 안 눌렀고 안쪽 요원이 신원만 적은 단계) 실서버 경고 id 18이 실제로 그 모양이다. 한쪽으로 다른 쪽을 유도하면 종 배지 숫자가 달라진다.
- **경고 행을 `SELECT ... FOR UPDATE`로 잠근다**(신원 갈래와 같은 `_lock_alert`). 잠금이 없으면 두 요청이 "읽고 → 판정하고 → 쓰는" 사이에 끼어들어 둘 다 새 전이로 메시지를 낸다.
- **커밋 뒤에 `alert_status`를 브로드캐스트한다.** 실제로 바뀐 경우에만 나가고, `data`는 `alert_id`·`type`·`severity`·`status`·`ack`·`resolution`·`resolved_at`이다. **`acked_by`·`resolved_by`는 안 싣는다** — `/ws/dashboard`는 인증이 없고 그 값은 우리 쪽 요원 이름이다(`AlertOut`이 같은 이유로 안 싣는다).
- **`POST /ack`는 여전히 `alert_ack` 한 장만 낸다.** 축은 이어져 있어서(같은 `apply_transition`을 탄다) 확인을 누르면 `status`도 `ACKED`로 오르지만, 메시지 종류는 안 바꿨다 — 프론트 계약 시험(`tests/test_dashboard_ws.py`)이 그 창구의 메시지 집합을 `["alert_ack"]`로 못박아 놨다. 세 창구(확인·신원·상태)를 `alert_status` 하나로 합치는 건 프론트 전환과 같이 가야 하는 팀 결정이다.
- **⚠ `actor`는 자칭 문자열이다.** 공용 `X-API-Key` 하나로는 서버가 "정말 그 사람인가"를 못 판정한다. `AUTH_REQUIRE_LOGIN`을 켠 구간에서는 본문 `actor`를 **무시하고 세션 실명으로 덮는다**(위 "자칭 문자열 제거"). 칸은 스키마에서 안 지운다 — 지우면 그 칸을 싣던 옛 클라이언트가 422를 맞는다.
- 예전 행이 어느 상태로 읽히는지는 마이그레이션 `0005`의 backfill이 정한다. `identified_at`이 있으면 `IDENTIFIED`, 아니고 `ack=true`면 `ACKED`, 나머지는 `OPEN`이다. **`ack`·`acked_at`·`resolved_at`은 한 행도 안 바꾼다** — 예전 조작 기록을 사후에 고쳐 쓰지 않는다. 그래서 예전 `ACKED` 행은 `ack=true`인데 `acked_at`이 NULL인 유일한 예외다.

### 미태깅 통과자 사후 신원 확인 — `/api/alerts/{id}/identify` (POST · GET · DELETE)

미태깅 통과는 태그를 안 댔으니 **시스템에 신원 근거가 없다.** 그래서 게이트에서 물리로 막지 않고 흘려보내고, 관제가 즉시 알리고, 게이트 안쪽 보안 요원이 붙잡아 신원을 확인한 뒤 관제 화면에 직접 적어 넣는다. 그 입력이 경고 행에 남고 별도 매터모스트 채널(상위 보고선)로 카드가 나간다.

세 갈래 다 **`X-API-Key`가 있어야 한다.** 감사 기록에 쓰는 창구를 인터넷에 열어 두면 아무나 위조 신원을 적고 상위 보고선으로 카드를 쏜다(2026-07-30 검토 실측 — 수리 전에는 키 없이 그대로 통했다).

`POST` 요청 본문은 `person`(필수, 64자), `identified_by`(옵션, 64자), `note`(옵션, 200자), `overwrite`(옵션, 기본 false)다. 확인 시각은 서버가 찍는다 — 감사 기록의 시각을 부르는 쪽이 정하면 못 믿는다.

- **없는 경고 번호는 404**, **미태깅(`type="untagged"`)이 아닌 경고는 400**, **이미 신원이 적힌 경고는 409**다. 고쳐 적으려면 `overwrite: true`를 명시한다. 뒤엣값이 조용히 이기면 두 요원이 다른 신원을 적었을 때 앞 기록이 흔적 없이 사라진다. 정정으로 들어온 건은 보고 카드에 `(정정)`으로 찍힌다.
- **`overwrite`여도 안 보낸 칸은 안 건드린다.** `person`만 담아 정정하면 요원 이름과 메모는 앞 값 그대로 남는다. 지우고 싶으면 그 칸을 빈 문자열로 **보낸다** — "안 보냈다"와 "비워서 보냈다"를 가른다.
- **경고 행을 `SELECT ... FOR UPDATE`로 잠그고 409를 판정한다.** 잠금이 없으면 "읽고 → 검사하고 → 쓰는" 사이에 다른 요청이 끼어들어 409 게이트를 통째로 지나간다(실측 — 동시 6건 중 다섯이 200을 받고 보고선에 서로 다른 신원 카드가 정정 표시 없이 다섯 장 붙었다).
- **입력에 개행·제어문자·NUL·양방향 재정렬 문자를 못 넣는다(422).** 카드가 줄을 `\n`으로 이어붙인 한 덩어리라, 신원 칸에 개행을 끼우면 위조된 줄이 진짜 줄 사이에 붙고 `@channel` 멘션까지 나간다. 길이 검사는 **앞뒤 공백을 턴 뒤에** 건다 — 붙여넣기로 딸려온 공백 때문에 실사용 값이 막히면 안 된다.
- **`ack`는 안 건드린다.** "확인했다(ack)"와 "누구였는지 적었다(identify)"는 다른 사건이다.
- **커밋 뒤에 `alert_identified` 메시지를 브로드캐스트한다.** 대시보드가 두 대 열려 있으면 한쪽에서 적은 신원이 다른 쪽에 안 보여서, 둘째 요원이 같은 건에 붙었다가 409를 맞는다(`alert_ack`가 S15P11C207-81에서 고친 것과 같은 결함).
- **⚠ 개인정보** — `person`은 자유 문자열이다. **실명·학번을 넣을지는 팀 결정이고, 시연에서는 가명("가명-1")을 권한다.** 이 값은 서버 로그에 안 찍고(남기는 건 경고 번호와 글자 수까지), 인증 없는 조회 API에도 안 싣는다. `GET /api/alerts`·스냅샷·`alert_identified` 메시지의 경고 한 줄에는 `identified`(bool)·`identified_at`까지만 나간다. **확인한 요원(`identified_by`)도 뺐다** — 우리 쪽 사람 이름이라 덜 민감해 보이지만 `identified_at`과 짝을 이루면 "누가 몇 시에 근무했나"가 인터넷에 그대로 나간다. `POST` 응답도 같은 스키마라 신원을 되돌려주지 않는다.
- **`GET /api/alerts/{id}/identify`가 원문을 되읽는 유일한 창구다**(같은 키). `identified_person`·`identified_by`·`identify_note`·`identified_at`을 준다. 이게 없으면 신원이 write-only가 된다 — 보고 웹훅이 꺼진 배포에서는 적어 넣은 값을 읽을 수 있는 사람이 아무도 없는 채로 개인정보만 쌓인다(운영 실측). 409를 맞은 요원이 덮어쓸지 판단하는 자리이기도 하다.
- **`DELETE /api/alerts/{id}/identify`가 칸 넷을 NULL로 되돌린다**(멱등, 같은 키). 잘못 적은 신원을 되돌릴 길이 이전엔 `alembic downgrade`(컬럼 통째 drop)뿐이었다. **지금은 이게 유일한 파기 수단이다** — 보관 기간·일괄 파기 잡은 아직 없다(아래 "아직 아닌 것").
- **두 번째 웹훅은 `MATTERMOST_IDENTIFY_WEBHOOK_URL`이다.** 미태깅 채널(`MATTERMOST_WEBHOOK_URL`)과 **별개 설정이고 기본은 빈 문자열 = 꺼짐**이라, 안 넣으면 아무 데도 안 쏘고 입력만 남는다. 한쪽만 켠 배포가 정상 상태다. `MATTERMOST_ENABLED=false`는 두 채널을 다 멈춘다. 카드에는 게이트·확인 시각·확인한 요원·신원·경고 번호가 들어가고, **신원은 안 가린다** — 이 채널의 존재 이유가 "누구였는지"를 알리는 거라 가리면 보고가 무의미해진다. 대신 노출 범위를 이 채널 하나로 묶고, 요원이 적은 값은 카드에서 인라인 코드로 감싸 마크다운·멘션이 안 살아나게 한다.
- **채널이 켜졌는지는 `GET /healthz`와 스냅샷의 `identify_report_enabled`로 본다.** 꺼진 배포에서 화면이 "카드가 나갑니다"라고 얼버무리면 요원은 보고를 보냈다고 믿는데 실제로는 무발사다(운영 실측 — EC2에 URL이 아예 없는데 DB엔 신원이 적혀 있었다).
- 웹훅은 커밋 뒤에, 그것도 **응답을 돌려준 뒤에** 부른다(`BackgroundTasks`). 요청 안에서 기다리면 느린 채널 하나가 요원의 화면을 그 시간만큼 붙잡는다(실측 3.12초). 웹훅이 5xx를 줘도 신원 입력은 남고 응답은 200이다.
- 화면 입력 자리는 지금 개발자 텔레메트리(`app/static/telemetry.html`)에 **임시로** 붙어 있다 — 무단 통과 경고만 모은 표와 그 위의 입력 폼이다. 요원용 관제 화면이 생기면 그리로 옮긴다.

### 인증은 어디까지 걸렸나

| 갈래 | 인증 | 왜 |
| --- | --- | --- |
| 인입 3종 | `X-API-Key` | 기존 계약 그대로. 기기가 헤더로 싣는다 |
| `POST /api/alerts/{id}/ack` | `X-API-Key` | 상태를 바꾼다 |
| `POST /api/alerts/{id}/status` | `X-API-Key` | 사건을 다음 단계로 전진시킨다(종결 포함). 아래 "사건 수명주기 창구" 절 |
| `/api/alerts/{id}/identify` POST·GET·DELETE | `X-API-Key` | 감사 기록 쓰기 + 신원 원문 읽기 |
| `/api/assistant/*` 3종 | `X-API-Key` + **앱 층 호출 상한** | 읽기만 해도 GMS 크레딧을 태운다(팀 공용 10만). 60초에 60회를 넘기면 **429 + `Retry-After`**다 — 눈금 단위는 로그인이 켜지면 사람, 꺼져 있으면 부른 자리(IP). ⚠ 이 상한은 `AUTH_REQUIRE_LOGIN`과 **무관하게 항상** 돈다(`app/routers/assistant.py`의 `_quota_gate`) |
| `POST /api/shuttle-calls` | 없음 | 화면 버튼이 직접 부른다. 화면이 인입 키를 들면 그 키로 태깅·통과 인입과 신원 입력까지 열리니, 여는 폭을 셔틀 하나로 좁혔다. 대신 게이트별 쿨다운이 붙는다 |
| `/api/dispatch/*` 7종 | `DISPATCH_REQUIRE_API_KEY`(기본 꺼짐) | 같은 이유로 기본이 무인증이다. 켜면 라우터 전체가 `X-API-Key`(기기 키 `API_KEY`)를 요구한다. 여는 폭이 로봇 조작이라 셔틀 호출보다 넓어서, 이동·복귀에 `DISPATCH_COMMAND_MIN_INTERVAL_SEC`(기본 1초) 연타 제한을 같이 걸었다. **긴급정지 창구만 그 제한을 안 받는다** — 세우는 것도 푸는 것도 막히면 안 된다 |
| 조회 GET 계열·`/healthz` | 없음 | 화면이 바로 읽는 자리. 여기 키를 걸면 대시보드가 통째로 멈춘다 |
| `GET /api/weather/forecast` | 없음 | 기상 칩을 눌렀을 때 주는 오늘·내일 예보 한 장(2026-08-03 신설). 조회 GET 계열과 **같은** `require_agent`라 지금은 익명이고 켜면 요원이 된다 |
| `GET /api/camera/stream` | **역할 게이트(플래그 무관 · 요원)** | ⚠ **지금도 로그인을 요구하는 유일한 화면 창구다**(2026-08-03 신설, `require_agent_always`). 로그인과 함께 생긴 창구라 보존할 예전 거동이 없다 — 젯슨 미리보기를 로그인 뒤로 넣는 게 이 창구의 존재 이유다. 켜기 전에 여기서 401이 나면 로그인 배선이 깨진 게 아니라 **원래 그렇다** |
| `/dev/telemetry` | 앱 층 없음 + **nginx `auth_basic`** | 앱 게이트(`require_admin`)는 플래그가 꺼진 동안 판정을 안 해서, 배포에서는 nginx 조각이 실질 자물쇠다. 그 페이지에 신원 입력 상자가 있다 |
| `/docs`·`/redoc`·`/openapi.json` | **라우트 없음(404)** | 쓰기 창구 지도를 통째로 담는 자리라 껐다. 앱+nginx 두 겹. 명세는 `app.openapi()`로 읽는다 |
| `POST /api/auth/login` | 없음(세션을 발급하는 창구다) | 서버 방어는 **아이디별** 잠금(`LOGIN_FAIL_MAX_ATTEMPTS` 5회·`LOGIN_FAIL_LOCK_SEC` 60초)뿐이라 아이디를 갈아 가며 두드리면 안 걸린다. 그 구멍은 nginx가 IP 단위로 메운다 — `zone=c207_login` **10r/m·burst 20**, 초과는 429(`deploy/nginx-c207-ratelimit.conf`). ⚠ `location = /api/auth/login` 정확 일치라 `GET /api/auth/me`는 같은 통에 안 들어간다(묶으면 새로고침만으로 429다) |
| `/api/staff/*`·`/api/users/*` | **역할 게이트(플래그와 무관)** | 로그인과 함께 새로 생긴 창구라 보존할 예전 거동이 없다. 요원 = `GET /api/staff/by-tag/{tag_id}` 하나, 팀장급 = 나머지 명부, 관리자 = 계정 5종(`GET /api/users/{id}/audit` 포함). 자세한 건 아래 표 |
| `/ws/robot` | `WS_REQUIRE_API_KEY_ROBOT`(기본 꺼짐) | 켜면 핸드셰이크에 **기기 키**(`API_KEY`)를 요구한다. Origin 판정은 안 탄다 — 젯슨은 그 헤더를 안 싣는다 |
| `/ws/dashboard` | **Origin 판정(늘)** + `WS_REQUIRE_API_KEY_DASHBOARD`(기본 꺼짐) | 키 스위치를 켜면 **대시보드 키**(`DASHBOARD_API_KEY`)를 요구한다(기기 키는 안 받는다). 그와 별개로 **핸드셰이크 Origin 판정이 플래그 밖에서 늘 돈다** — 아래 참고 |

⭐ **대시보드 WS 핸드셰이크 Origin 판정은 플래그와 무관하게 늘 돈다**(2026-08-02, `app/security.py`의
`ws_origin_allowed`). WebSocket에는 CORS가 없어서 브라우저가 남의 페이지가 연 `new WebSocket(...)`도
그냥 붙여 주고, 운영 환경에서는 대시보드와 API를 동일한 site 아래에 배치한다. 다른 도메인의
주소는 오리진만 다르고 **같은 site**라 쿠키까지 실린다. 안 막으면 남의 팀 페이지가 관제 스트림을
통째로 구독한다. 판정 함수는 HTTP Origin 가드와 **같은 것**(`origin_allowed`)이라 자기 오리진·로컬·
`AUTH_ALLOWED_ORIGINS`가 통과 집합이고, **Origin이 없으면 통과**라 젯슨·`websockets` 클라이언트·
`wscat`은 하나도 안 바뀐다. 거절은 키 거절과 같은 방식(accept 뒤 close 1008)이다.

⚠ 위 표는 **`AUTH_REQUIRE_LOGIN=false`(기본값)일 때** 모습이다. 켜면 아래 표로 바뀐다.

### 로그인을 켠 뒤 역할 게이트 (`AUTH_REQUIRE_LOGIN=true`)

계약 정본은 `docs/로그인_설계초안_2026-08-01.md` §3 매트릭스이고, 역할 셋은 `agent`(일반
보안요원) · `leader`(팀장급) · `admin`(관리자)다. 판정은 **계정 역할 하나**로만 한다 — 명부
직급(`staff.rank`)은 "대상이 어느 급인가"이지 행위자의 급이 아니다(§2.5).

⭐ **제1 불변** — 이 값이 `false`인 동안 아래 창구가 **글자 그대로 위 표 모습**이다. 게이트를
갈아 끼운 게 아니라 갈림길을 놓은 것이고, 갈림은 `app/security.py`의 `key_gate_or`와
`ws_dashboard_allowed` 두 자리에 모여 있다. 롤백은 이 값 하나만 되돌리면 된다.

| 창구 | 켜기 전 | 켠 뒤 |
| --- | --- | --- |
| `GET /healthz` | 없음 | **없음(익명 유지)** — 젠킨스·모니터링이 부른다 |
| 조회 GET 계열 (`/api/dashboard/snapshot`·`/api/robots`·`/api/events`·`/api/alerts`·`/api/eta`) | 없음 | 요원 ← `/api/events`의 태그 UID 평문 노출이 여기서 닫힌다(-238) |
| 통계 5종 (`/api/stats/*`) | 없음 | 요원 |
| `GET /api/weather/forecast` | 없음 | 요원 — 조회 GET 계열과 같은 게이트다(별도 자물쇠를 안 만든 이유는 `app/routers/weather.py` 머리에 있다) |
| `GET /api/camera/stream` | **요원**(플래그 무관) | **요원** — 켜고 끄고가 안 바뀌는 자리다. 8/3에 로그인과 함께 생겨서 `require_agent_always`다 |
| `POST /api/shuttle-calls` | 없음 | 요원 |
| `POST /api/alerts/{id}/status`·`/ack`·`/identify`, `GET /api/alerts/{id}/identify` | `X-API-Key` | 요원 |
| `DELETE /api/alerts/{id}/identify` | `X-API-Key` | **팀장** — 감사 기록을 되돌리는 유일한 갈래라 상향(§8 결정 5) |
| `/api/assistant/*` 3종 | `X-API-Key` | 요원 (키와 세션을 **둘 다 걸지 않는다** — 둘 다면 브라우저가 여전히 키를 들어야 한다). 인증과 별개로 앱 층 60회/60초 상한이 늘 붙는다 |
| `/api/dispatch/*` 6종 (복귀 제외) | `DISPATCH_REQUIRE_API_KEY`(기본 꺼짐) | 요원 |
| `POST /api/dispatch/commands/return` | `DISPATCH_REQUIRE_API_KEY`(기본 꺼짐) | **요원 세션 또는 기기 키** — ⚠ 위 assistant 줄의 "둘 다 걸지 않는다" 원칙의 **첫 예외**다 |
| `GET /dev/telemetry` | 앱 층 없음(배포는 nginx `auth_basic`) | **관리자** — 텔레메트리는 관리자·개발자 전용(범위 정본 §2). nginx 겹은 켠 뒤에도 그대로 둔다. ⚠ 켜면 자물쇠가 **두 겹**이 된다 — basic 창을 지나고도 관리자 세션이 없으면 앱이 401이라, 켠 걸 모르는 사람은 페이지가 죽은 줄로 읽는다 |
| 인입 3종 | `X-API-Key` | **`X-API-Key` 그대로** — 젯슨·라파이는 사람이 아니라 세션을 못 쥔다 |
| `GET /api/staff/by-tag/{tag_id}` | **요원**(플래그 무관) | **요원** — 이벤트 문맥 신원 한 건. 아래 "태그로 신원 짚기" 절 |
| `/ws/dashboard` | **Origin 판정** + `DASHBOARD_API_KEY`(기본 꺼짐) | **Origin 판정 + 세션 쿠키**(요원). Origin은 두 구간 다 돌고, 켠 뒤 대시보드 키는 폴백으로 **안 남는다** — 남기면 세션 우회 뒷문이다(§5.1) |
| `/ws/robot` | `WS_REQUIRE_API_KEY_ROBOT`(기본 꺼짐) | **안 바뀐다** — 스위치가 꺼진 지금 배포에서는 켠 뒤에도 **익명**이다. ⚠ 로그인을 켜면 "이제 다 잠겼다"는 인상이 생기는데, 경고 행을 만들고 명령을 받는 이 채널만은 그대로 열려 있다. 잠그는 건 별건 스위치이고 젯슨 `.env`에 같은 `API_KEY`를 넣는 배선이 먼저다(§5.2·§6.2 6단계) |
| `GET /api/users/{id}/audit` | **관리자** | **관리자** — 그 계정에 벌어진 변경 기록(읽기만). 남는 사건은 생성(`create`)·역할/활성 수정(`update`, 해제는 `deactivate`)·비밀번호 재설정(`update` + `after.password_reset=true`) 셋이고 비밀번호는 원문도 해시도 안 실린다. 계정 창구와 같은 급이고 응답은 `GET /api/staff/{id}/audit`과 같은 `StaffAuditOut` 배열이다 |

⭐ **복귀 창구가 "키와 세션을 둘 다 받는" 유일한 자리다**(2026-08-04 사용자 확정). 위 assistant
줄이 못박은 원칙("키와 세션을 둘 다 걸지 않는다")의 첫 예외이고, 예외를 둔 이유는 부르는 쪽이
브라우저가 아니라 **기기**라서다 — 라파이 게이트 단말이 두 번째 3초 터치에서 이 창구를 직접
부르는데(`hardware/rpi/gate_server.py` `call_return_service`) 그 기기는 세션을 못 쥔다. 폴백이
없으면 `AUTH_REQUIRE_LOGIN`을 켜는 순간 현장 버튼이 401로 죽는다. 사용자 결정은 "라파이는 키를
계속 받게 열어둔다"였다. assistant 줄의 원칙이 여전히 서는 이유도 같다 — 거기서 키를 같이 걸면
**브라우저가** 키를 들어야 하는데, 여기서 키를 드는 건 브라우저가 아니다.

그 예외를 좁게 가두는 장치가 셋이다.

- **라우터를 갈랐다.** 복귀만 `device_router`에 붙고 나머지 여섯은 `router`에 그대로 남는다
  (`app/routers/dispatch.py`). 라우터 레벨 의존성을 유지해서, 새 창구를 어느 쪽에 붙여도 게이트가
  자동으로 걸리고 기본값(`@router`)이 좁은 쪽이다. `tests/test_dispatch_key_gate.py`가
  `device_router`의 창구 목록을 닫힌 집합으로 못박아서, 하나를 더 붙이면 시험이 빨개진다.
- **키 대조가 스위치와 무관하다.** `DISPATCH_REQUIRE_API_KEY`가 아니라 `API_KEY` 실값을 본다
  (`security.device_key_ok`). 그 스위치는 기본이 꺼짐이라 폴백으로 쓰면 "키를 계속 받는다"가
  "익명도 통과"로 뒤집힌다.
- **자물쇠가 아닌 키는 안 받는다.** 빈 값과 저장소 기본키(`dev-local-key`)는 거절이다
  (`security.secret_is_usable`, WS 로봇 채널과 같은 겹). ⚠ 인입 3종은 **반대로** 기본키를 받는다 —
  로컬 개발이 `.env.example` 값으로 도는 자리라 일부러 열어 뒀다.

⚠ 통과한 요청은 **누가 눌렀는지 안 남는다.** 공용 기기 키라 사람을 못 가려서, 서버가 남기는 건
`c207.security`의 WARNING 한 줄(메서드·경로)뿐이다. 켠 뒤에 EC2에서 `API_KEY`를 바꾸면 라파이
`.env`의 값도 같이 바꿔야 한다 — 절차는 `deploy/README.md` "로그인 켜기(롤아웃)" 5단계에 있다.

⚠ 로그인과 함께 새로 생긴 창구(`/api/staff/*`·`/api/users/*`)는 **플래그와 무관하게 늘 판정한다.**
제1 불변은 "지금 있는 창구의 거동을 안 바꾼다"는 약속이라 없던 창구에는 안 걸리는데, 열어 두면
롤아웃 1~4단계 내내 익명이 관리자 계정을 만들 수 있다.

⭐ **교차 오리진 상태변경은 403이다**(§1.2 · 플래그와 무관). `POST`·`PUT`·`PATCH`·`DELETE`에
`Origin` 헤더가 실려 있고 그 값이 자기 오리진이 아니면 자른다. **`Origin`이 없으면 통과다** —
기기·서버 호출(젯슨 인입·`curl`·젠킨스)이 그 갈래라 인입은 하나도 안 바뀐다. **로컬 개발은
설정이 필요 없다** — `localhost`·`127.0.0.1`은 포트·스킴과 무관하게 기본 허용이라 로컬
정적 서버를 몇 번 포트로 띄우든 그냥 돈다(화면이 뜬 주소가 Origin 으로 실려서,
안 열면 로컬의 모든 상태변경이 403이 된다). 그 밖의 오리진에서 화면을 띄울 때만
`AUTH_ALLOWED_ORIGINS`에 적는다. WebSocket 핸드셰이크는 이 판정을 안 탄다.

⚠ 이 한 겹은 `AUTH_REQUIRE_LOGIN` **밖에서 늘 돈다** — 끄는 스위치가 없다. 플래그가 꺼진
구간에서도 "브라우저가 교차 오리진에서 보낸 쓰기"만은 403이라는 뜻이고, 그게 제1 불변의
유일한 예외다.

⭐ **자칭 문자열 제거** — 켠 구간에서는 `POST /status`의 `actor`와 `POST /identify`의
`identified_by`를 **본문에서 안 읽고 세션 실명으로 덮는다.** 공용 키 하나로는 서버가 "정말
그 사람인가"를 못 판정해서 감사 기록 위조가 그대로 통했다. `/ack`도 세션 실명이 `acked_by`에
찍힌다. 칸은 스키마에서 안 지운다 — 지우면 그 칸을 싣던 옛 클라이언트가 422를 맞는다.

⚠ **세션 쿠키 이름이 배포와 로컬에서 다르다**(2026-08-02). https(secure) 배포에서는
`__Host-c207_session`, http로 도는 로컬 개발에서는 접두 없는 옛 이름이다. `__Host-` 접두는
Secure + `Path=/` + Domain 없음이 필수라 http에서는 브라우저가 아예 안 받는데, 배포 도메인이
형제 팀과 같은 site라 접두가 없으면 남이 같은 이름 쿠키를 심어 세션 고정을 걸 수 있다.
**이름을 직접 읽지 마라** — 심는 쪽은 `auth.session_cookie_name()`, 읽는 쪽은
`auth.read_session_token()`이 두 이름을 다 본다. 한쪽 이름만 보면 배포에서 대시보드 WS가
통째로 거절된다. 한 계정이 동시에 들 수 있는 세션은 `SESSION_MAX_PER_USER`(기본 5)장이고,
넘으면 **오래된 것부터** 무효화된다 — 로그인 INSERT와 같은 커밋에 실린다.

401과 403은 다르다(§7.2). **401** = 세션이 없다·만료됐다 → 화면이 로그인으로 보낸다.
**403** = 세션은 멀쩡한데 급이 모자란다 → "권한이 없습니다" 안내만 하고 **로그아웃시키지
않는다.** 403을 401처럼 다루면 요원이 관리자 창구를 한 번 건드릴 때마다 로그아웃된다.

배포 순서는 설계 확정본 §6.2다 — 끄고 배포 → 계정 심기 → 로그인 실측 → 프론트 배포 → 켜기.
**2단계(계정 심기)를 건너뛰고 켜면 아무도 못 들어가는 잠금이 난다.**

### 태그로 신원 짚기 — `GET /api/staff/by-tag/{tag_id}`

요원에게 열린 유일한 명부 창구다. 목록 열람이 아니라 **이벤트 문맥 조회**라서 요원 급이고,
응답은 `tag_id`·`name`·`student_no` 셋이다(`app/routers/staff.py`).

⭐ **이 창구가 무엇과 맞물리나 — 통과 행의 `tag_id`다.** 태그와 명부를 잇는 길은 역추적
규칙 하나다. 경고의 `(source_type, source_id)`가 이벤트 한 줄의 `(kind, id)`고
(S15P11C207-192), 그 이벤트 행이 `tag_id`를 들고 있다.

⚠ **2026-08-02 전까지 이 창구는 미태깅 경고에서 구조적으로 늘 404였다.** 인입 스키마
(`GatePassEventIn.tag_id`)에는 칸이 있는데 라우터가 그 값을 몸통에 안 넘겨서, 미태깅 통과
행의 UID가 영영 NULL이었다. 그래서 "활성 경고에 붙은 태그"가 미태깅 쪽에서는 한 건도 안
잡혔다. 지금은 `app/routers/ingest.py`가 `tag_id=body.tag_id`를 넘기고
`app/credit/state_machine.py`가 판정과 **무관하게** 행에 남긴다 — 미태깅·퇴장·판정 보류에도
기기가 읽은 UID가 그대로 찍힌다(F22). 통과 행 `tag_id`의 출처는 그래서 둘이다.

| 판정 | 저장되는 값 |
| --- | --- |
| 정상 통과(크레딧 소비) | **소비한 크레딧의 UID**. 서버가 직접 만든 사실이라 기기 관측값보다 이쪽이 이긴다 |
| 미태깅·퇴장·판정 보류 | **기기가 그 통과에서 읽은 UID**. 기기가 안 실어 보내면 예전처럼 NULL |

⚠ **판정에는 안 쓴다.** 크레딧 소비는 그대로 게이트 FIFO다 — 기기가 보낸 UID로 크레딧을
고르면 소비 규칙이 두 벌로 갈린다. 그리고 **라파 `gate_server.py`의 두 emit 갈래는 아직 이
칸을 안 싣는다.** 기기가 실어 보내기 전까지 미태깅 쪽 UID는 여전히 NULL이고, 그때는 이
창구가 계속 404를 낸다 — 창구 결함이 아니라 인입에 값이 없는 것이다.

⚠ **활성 경고(`ack=false`)에 붙은 태그만 통과한다**(§8 결정 6). 이 제한이 없으면 요원이
`/api/events`의 UID 평문을 한 건씩 순회해 명부 전체를 긁는다. 실패는 갈래를 안 가리고 전부
같은 404다 — ①없는 태그 ②명부에 없는 태그 ③활성 경고가 없는 태그가 밖에서 똑같이 보인다.
해제된(`is_active=false`) 행은 돌려준다. 해제는 삭제가 아니라 "더 이상 등록 인원이 아니다"라,
그 사람 태그로 경고가 났을 때야말로 관제가 이름을 알아야 한다(§2.3).

### WS 핸드셰이크 인증 (S15P11C207-238)

스위치가 셋이고 전부 기본 꺼짐이라, 아무것도 안 넣으면 지금 배포 거동 그대로다.

| 환경변수 | 하는 일 |
| --- | --- |
| `WS_REQUIRE_API_KEY` | 상위 스위치. 켜면 아래 둘과 상관없이 두 채널이 다 켜진다(예전 이름 하위 호환) |
| `WS_REQUIRE_API_KEY_ROBOT` | `/ws/robot`만 켠다 |
| `WS_REQUIRE_API_KEY_DASHBOARD` | `/ws/dashboard`만 켠다 |
| `API_KEY` | 기기 키. 인입 HTTP와 `/ws/robot`이 쓴다 |
| `DASHBOARD_API_KEY` | 대시보드 키. `/ws/dashboard`만 쓴다. **`API_KEY`와 다른 값을 넣는다** |

키를 싣는 창구는 셋이고 **동급**이다 — `X-API-Key` 헤더 / `Authorization: Bearer <키>` /
`?api_key=` 쿼리. 젯슨 web_bridge는 Bearer로 싣고, 브라우저 WebSocket API는 핸드셰이크에 임의
헤더를 못 실어 쿼리뿐이다. 같은 키면 한글이 섞여도 둘레에 공백이 붙어도 세 창구가 같은 답을
낸다(`tests/test_ws_auth_live.py`가 실 uvicorn으로 확인한다).

**키를 왜 둘로 갈랐나.** 브라우저가 기기 키를 들면 개발자 도구에서 그대로 보인다. 그 한 장이
새면 인입·명령 HTTP 창구까지 통째로 열리니, 대시보드 채널은 기기 키를 아예 안 받는다. 대신
`DASHBOARD_API_KEY`도 화면에 노출되는 값이라 **완전한 인증이 아니라 피해 범위를 화면 읽기로
좁히는 임시 자물쇠**다. 진짜 답은 세션·역할 구분(M-6)이고 팀 결정 자리다.

**닫히는 쪽으로 가는 경우 셋.** ①`DASHBOARD_API_KEY`를 안 넣고 대시보드 스위치만 켰을 때
②`API_KEY`가 비었을 때 ③`API_KEY`가 저장소 기본값(`dev-local-key`) 그대로일 때. 셋 다 자물쇠가
아니라서 통과시키면 켜 놓고 잠긴 줄 아는 보안극장이 된다. 서버 로그에 이유가 `ERROR`로 남는다.

거절은 **accept 뒤 close 1008**이다. accept 전에 닫으면 전선에는 `HTTP 403`만 나가고 브라우저
JS는 그 상태 코드를 못 봐서(onclose 1006) 인증 실패와 네트워크 끊김을 구분 못 한다.

**켜는 순서(롤아웃).** 스위치가 코드에 있다고 켤 수 있는 게 아니다.

1. **로봇 채널 먼저.** EC2 env에 `API_KEY`를 실값으로 두고(기본키면 닫힌다), 젯슨 `web_bridge`와
   라파이가 그 키를 실는지 확인한 다음 `WS_REQUIRE_API_KEY_ROBOT=true`. 젯슨은 Bearer로 이미
   싣고 있으니 값만 맞추면 된다. 라파이는 인입 HTTP라 이 스위치와 무관하다.
2. **대시보드는 프론트 작업이 먼저다.** 지금 화면(`frontend/index.html`)이 만드는 WS 주소에
   키를 실을 자리가 없다. 이 상태에서 스위치만 켜면 화면이 조용히 죽는다.
   ⭐ **둘 중 하나는 이미 됐다(2026-08-04 실측)** — 새 화면은 닫힘 코드 1008을 보고 재시도를
   멈춘다. 남은 건 ①키를 쿼리에 싣는 것 하나다.
3. **되돌리기.** 스위치 하나를 `false`로 되돌리고 컨테이너를 재기동하면 즉시 예전 상태다. 키
   교체는 서버·기기 양쪽을 같이 바꿔야 해서 순간 단절이 생긴다 — 시연 중에는 하지 않는다.

플래그가 못 덮는 자리 둘. **인입 HTTP는 이 스위치와 무관하게 늘 `X-API-Key`를 요구한다**(키를
바꾸면 라파이가 같이 멈춘다). 그리고 **nginx 액세스 로그에 쿼리 키가 통째로 남는다** — 대시보드
채널을 켜면 로그 보존 정책도 같이 봐야 한다.

**⚠ 브라우저에 키를 두는 건 완전한 인증이 아니다.** 페이지를 여는 사람이 키를 보게 된다. 지금 한 건 "인터넷의 아무나 쓰던 상태"를 막는 최소 방어까지고, 화면 경계를 어디에 둘지(nginx `auth_basic` / 서명 쿠키 세션)는 팀·사용자 결정이다. 그때 `identified_by`를 요청 본문 대신 세션에서 채우면 "누가 확인했나"가 자칭이 아니게 된다 — 지금은 부르는 쪽이 아무 이름이나 적을 수 있다.

## 기기·관리자 창구 넷 (2026-08-04~05 추가)

전체검토 L13이 "새 창구가 README에 통째로 없다"고 짚은 자리다. 아래 넷을 여기 모은다.

### 소리 신호를 나르는 창구 둘 — 같은 큐, 다른 옷

라파이에 블루투스 스피커를 달아 셔틀 도착 안내를 낸다. 라파이는 서버로 **보내기만** 해서
서버가 먼저 말을 걸 길이 없으니, 1초마다 물어 가져간다. 설계 근거는 `app/device_sound.py`
모듈 머리에 있다.

| 창구 | 응답 겉봉 | 누가 쓰나 |
| --- | --- | --- |
| `GET /api/sound-signals?device_id=` | `{"device_id","signals":[{seq,kind,gate_no,event_id}]}` | 처음 만든 계약. 지금 쓰는 기기는 없다 |
| ⭐ `GET /api/rpi/commands/poll?device_id=&gate_no=` | `{"commands":[{command_id,type,gate_no,event_id}]}` | **실기기가 쓰는 쪽**(`hardware/rpi/rpi_commands.py`) |
| ⭐ `POST /api/rpi/commands/{command_id}/ack` | `{"acked":true,"command_id"}` | 위 폴러가 소리를 낸 뒤 부른다 |

둘 다 `X-API-Key`이고 `AUTH_REQUIRE_LOGIN`을 켜도 안 막힌다(인입과 같은 게이트다).

**왜 둘인가** — 라파이 담당이 실기기 폴러를 모의 서버로 개발해 올렸는데(커밋 `0283d6e`)
그 폴러가 부르는 이름이 서버에 0건이었다. 붙이면 404만 받고 시연에서 소리가 안 나는
상태였다. 서버를 그쪽에 맞추기로 정했고(2026-08-05), 먼저 만든 계약도 시험이 붙어 있어
그대로 뒀다.

⚠ **한 기기가 둘을 번갈아 부르면 소리가 절반만 난다.** 같은 큐를 보되 커서가 `device_id`
별이라, 한쪽이 가져가면 다른 쪽은 빈손이다. 실기기는 `/api/rpi/commands/poll` 하나만 쓴다.

⭐ **ack 는 큐를 안 건드린다.** 확인 응답이 유실되면 신호가 되살아나 스피커가 무한히
울린다 — 그래서 "꺼내면 그 기기 몫으로는 끝"이고 ack 는 받아 두기만 한다. 그래도 창구를
만든 이유는 둘이다. 없으면 폴러가 매번 에러를 찍고, ack 로그가 **"기기가 실제로 소리를
냈다"는 유일한 신호**라 시연 전 확인에 쓴다.

### 명부 조회 이력 — `GET /api/staff/access-log?limit=` (팀장 이상)

"누가 명부를 봤나"가 어디에도 안 남던 자리다. 태그 UID 가 요청 줄에 평문으로 쌓이는 것을
막으려고 액세스 로그를 양쪽 다 껐는데(2026-08-02), 그러면서 조회 기록도 같이 사라졌다.
남기는 쪽으로 정했다(2026-08-05).

응답은 **배열 그대로**다(`GET /api/staff/{id}/audit`과 같은 모양). `action` 이 셋이다.

| `action` | 뜻 | 채워지는 칸 |
| --- | --- | --- |
| `list` | 명부 목록을 봤다 | `result_count` |
| `detail` | 명부 한 건을 봤다 | `target_staff_id` |
| `by_tag` | 이벤트 UID 로 신원을 봤다 | `target_staff_id` · `tag_prefix` |

⛔ **태그 UID 원문이 안 실린다.** `tag_prefix` 가 앞 네 글자 + 말줄임(`AABB…`)이다. 원문을
담으면 액세스 로그를 끈 것이 헛일이 된다.

⚠ **실패한 조회(404)는 안 남는다.** 정보가 안 나갔으니 "봤다"가 아니고, 남기면 없는 태그로
시도한 요청이 쌓여 열거 시도의 저장소가 된다. 그리고 **이 창구 자체도 이력을 안 남긴다** —
이력을 볼 때마다 이력이 늘면 감사 대상이 자기 조회에 파묻힌다.

표는 `staff_access_log`(마이그레이션 0012)이고 **append-only** 다. `staff_audit` 과 같은
계약이라 지우는 창구를 안 만들고, `tools/purge_test_data.py` 의 보존 목록에도 넣었다.

### 시연 자료 비우기 — `POST /api/admin/purge-demo-data` (⛔ 관리자만)

발표 직전에 시험·리허설 자국을 비운다. 화면(시연 도구 안 "이벤트·알림 초기화")이 부른다.

```
본문  {"confirm": "PURGE"}          ← 없거나 다르면 400
응답  {"deleted": {표별 건수}, "total": N, "sequences_reset": true}
```

- **관리자만.** 팀장도 안 된다 — 되돌릴 수 없는 자리라 명부 수정보다 위다.
- `require_admin_always` 라 `AUTH_REQUIRE_LOGIN` 과 무관하게 늘 판정한다.
- 지우는 목록은 `tools.purge_test_data.PURGE_ORDER` 를 **그대로 import** 한다. 두 자리에
  적으면 "스크립트로 지운 것과 버튼으로 지운 것이 다른" 상태가 되고 발표 당일에야 드러난다.
- 지운 뒤 시퀀스를 1부터 다시 시작하게 한다. 발표 첫 통과가 1번으로 뜬다.

⛔ **백업을 못 뜬다.** 스크립트는 지우기 전 JSONL 을 뜨는데 창구는 못 한다 — 컨테이너
파일 시스템이라 같이 사라지고, 응답으로 내려보내면 카드 UID 가 평문으로 실린다. 자료를
정말 남겨야 하면 창구 말고 스크립트를 쓴다.

## 셔틀 가상 호출 — `POST /api/shuttle-calls` (팀 확정 대기)

관제 화면이 버튼으로 셔틀 도착 각본을 시작하는 자리다. 셔틀은 실기기를 안 만들기로 확정됐는데 기기용 인입 `POST /api/shuttle-arrivals`가 `X-API-Key`를 요구해서 화면이 각본을 시작할 길이 없었다. 화면이 그 키를 들면 브라우저 소스에 키가 그대로 노출되고, 그 키 하나로 태깅·통과 인입과 신원 입력까지 다 열린다. 그래서 **셔틀 하나만 키 없이 부르는 창구를 따로** 뒀다.

요청 본문은 셋이다. `gate_no`(필수, 1~9999), `shuttle_no`(옵션, 32자), `room_id`(옵션, 32자 — 안건①). **`event_id`와 `signal_ts`는 받지 않는다.** 화면이 `event_id`를 정하면 같은 값을 다시 보내 남의 신호를 덮거나 멱등 키를 골라 적재를 조용히 막을 수 있고, `signal_ts`는 감사 축이라 부르는 쪽이 정하면 못 믿는다(신원 입력의 `identified_at`과 같은 규칙). 서버가 만드는 `event_id` 형식은 `webcall-{gate}-b{boot8}-{unix_ms}-{seq}`라 기기 event_id와 한눈에 갈라지고, 안건②(`EVENT_ID_REQUIRE_BOOT_ID`)를 켜도 형식 때문에 막히지 않는다.

```bash
curl -X POST 'http://127.0.0.1:8000/api/shuttle-calls' \
  -H 'Content-Type: application/json' \
  -d '{"gate_no": 2, "shuttle_no": "SHUTTLE-A"}'
```

응답은 `event_id`·`stored`·`gate_no`·`shuttle_no`·`signal_ts`·`notified_robot_count`·`command_id`·`resent` 여덟이다(정본 `app/schemas.py`의 `ShuttleCallOut`). `resent`는 "중복인데 아직 못 나간 명령을 다시 냈나"인데 **이 창구에서는 늘 `false`**다 — 서버가 `event_id`를 매 호출 새로 만들어서 중복 갈래가 구조적으로 안 생긴다. 그래도 칸을 두는 건 기기 창구(`ShuttleIngestAck`)와 계약을 같게 맞추려는 거다.

⚠ **`notified_robot_count`는 이 응답 시점에 늘 `0`이다.** 5초 대기 창이 붙으면서(2026-07-31 셔틀 출동 설계 §2.2, `app/dispatch.py`의 `begin_dispatch`) 명령이 응답보다 뒤에 나가기 때문이지 배달에 실패한 게 아니다. 창을 여는 갈래에서 `DispatchStart`는 `notified_robot_count=0`으로 돌아오고, `shuttle_dispatch_hold_sec`(기본 5.0초)이 켜져 있는 한 그게 정상 값이다. **그래서 "0이면 로봇이 없다"로 읽으면 안 된다** — 예전 판 문서가 그렇게 적혀 있었다.

- **로봇이 실제로 받았나는 WS `shuttle_dispatch_result`의 같은 칸으로 판단한다.** 대기 창이 마감되거나 요원이 고르는 순간 그 메시지 한 장이 확정 결과(`source`·`destination`·`notified_robot_count`)를 싣고 나간다. 두 메시지는 `arrival_id`·`command_id`로 묶는다.
- **`source` 낱말은 여덟이다**(`app/dispatch.py`의 `DispatchSource`) — `auto`(5초가 그냥 지남) · `manual`(요원이 창 안에서 고름) · `immediate`(즉시 이동·복귀 버튼) · `hold_cancelled`(요원이 "대기"를 고름 — 명령 안 나감) · `emergency_blocked`(긴급정지 중이라 신호를 버림 — 명령 안 나감) · **`hold_disabled`** · `resend`(통지 유실 재전송 복구) · **`emergency_stop`**(요원이 긴급정지 버튼을 누름 — 2026-08-06에 `immediate`에서 갈라냈다. 화면이 그것을 "즉시 이동으로 대신 보냈습니다"로 옮겨 적던 자리다).
  - ⭐ **`hold_disabled`는 2026-08-02에 새로 생겼다.** `SHUTTLE_DISPATCH_HOLD_SEC`을 0 이하로 내려 대기 창이 꺼진 배포에서는 신호를 받은 그 자리에서 바로 쏘는데, 예전에는 그 갈래만 확정 메시지를 **아예 안 냈다** — 계약 §0의 "어느 갈래로 닫히든 결과 한 장"을 이 하나가 안 지켰다. 창을 거친 `auto`·`manual`과 낱말을 갈라 둔 이유는 화면이 "카운트다운이 있었나"를 이 값으로 알기 때문이다. **기본값이 5.0초라 지금 라이브에서는 안 나온다.**
  - ⚠ **창을 안 여는 갈래에서도 `shuttle_arrival`이 먼저 나가고 결과가 뒤따른다.** 확정이 신호 접수와 같은 자리에서 나더라도 방송은 `app/shuttle_call.record_shuttle_arrival`이 순서대로 낸다 — 계약 §2.1이 "곧이어 따라온다"라 화면이 arrival을 먼저 받는다고 짜여 있다.
- 0이 진짜 배달 실패를 뜻하는 갈래는 창을 안 여는 둘뿐이다 — `SHUTTLE_DISPATCH_HOLD_SEC`을 0 이하로 내린 배포(`source=hold_disabled`)와 통지 유실 재전송 복구(`source=resend`).
- 응답 200과 "출동했습니다"는 여전히 다른 사실이다. 화면은 확정 메시지를 받기 전까지 "대기 중"으로 그려라(셔틀 명령 유실은 조용하다).

- **같은 (게이트, 룸)을 5초 안에 다시 부르면 429**이고 `Retry-After`에 남은 초가 실린다. 화면 버튼은 연타되고 시연 중엔 여러 사람이 같은 버튼을 누른다 — 그때마다 목적지 명령이 새로 나가면 출동 중인 로봇이 명령을 다시 받아 도착 판정(mission_status 에지)이 흔들린다. 인증이 없으니 이게 유일한 유량 제한이다.
- **쿨다운 눈금은 적재가 성공한 뒤에만 찍는다.** 먼저 찍으면 422·500으로 실패한 호출이 다음 5초를 삼켜서, 화면이 다시 눌러도 429만 돌아온다. 창 안에서 다시 부를 때도 마지막 호출 시각을 밀지 않는다 — 밀면 연타가 창을 계속 끌어 누르는 걸 멈출 때까지 안 열린다.
- **창 길이는 `app/shuttle_call.py`의 `SHUTTLE_CALL_COOLDOWN_SEC`(5.0초) 상수다** — 설정이 아니다. 창 하나짜리 값이라 튜닝 여지가 작아서 그렇게 뒀고, 설정으로 올릴지는 팀 결정이다.
- **적재 몸통은 `app/shuttle_call.py`의 `record_shuttle_arrival` 하나다.** 기기 인입과 화면 호출이 같은 함수를 타야 계약이 안 갈라진다. 서버가 자기 HTTP 인입을 다시 부르는 길은 안 골랐다 — 자기 주소를 알아야 하고(배포마다 다르다), 워커가 하나라 요청 안에서 자기 서버를 부르면 그대로 서고, 실패 지점이 둘로 늘어난다. `routers/ingest.py`는 예전에 자기 몸통을 따로 들고 있었지만 지금은 `record_shuttle_arrival`을 한 번 부르는 얇은 창구다(`app/routers/ingest.py`). 두 창구가 다시 갈라지지 않는지는 `tests/test_shuttle_call.py`가 결과를 나란히 대조해 잡는다.
- 인메모리 쿨다운은 재기동하면 비고, 시험은 `reset_shuttle_call_state()`로 케이스마다 비운다.

## 시험 자료 정리 도구 — `tools/purge_test_data.py`

시연 전에 쌓인 가짜 행을 백업 뜨고 지운다. 2026-07-30 실서버에 통과 494건·태깅 12건·경고 18건이 있는데 통과 중 297건이 미태깅 판정이고 대부분이 ToF 고착(지라 S15P11C207-227)으로 생긴 가짜다 — 시연 첫 화면에 미처리 사고 18건이 그대로 뜬다.

```bash
cd backend
python -m tools.purge_test_data --before 2026-07-30            # 미리보기(기본값)
python -m tools.purge_test_data --before 2026-07-30 --apply    # 실제로 지운다(백업 자동)
python -m tools.purge_test_data --device-prefix test- --apply  # 시험 기기 것만
python -m tools.purge_test_data --restore ~/c207-purge-backups/purge_20260730-101500-4213.jsonl
```

범위 옵션은 `--before`·`--after`(둘 다 ISO 시각, offset 없으면 UTC)·`--device-prefix`(여러 번)·`--gate`(여러 번)·`--all`이고 **AND로 묶인다**(여러 축을 주면 범위가 좁아진다). 그밖에 `--database-url`(안 주면 `.env`·환경변수), `--backup-dir`, `--restore`가 있다.

파괴 도구라 안전선이 여섯이다.

1. **미리보기가 기본값이다** — `--apply`가 없으면 세어서 보여주고 끝난다.
2. **범위 옵션이 없으면 아무것도 안 지운다**(종료 코드 2). 진짜 전부 지우려면 `--all`을 명시한다.
3. **지우기 전에 자동 백업**(JSONL). 파일 줄 수가 지울 행 수와 다르면 삭제를 아예 안 한다(종료 코드 4). 백업은 **덮지 않고**(같은 이름이면 종료 코드 4로 선다) 권한 0600으로 만든다 — 카드 UID와 사후 신원 문자열이 원문으로 들어간다. 기본 자리는 `~/c207-purge-backups`다. **배포 폴더 안에 두면 안 된다** — 다음 배포의 `rsync --delete`가 지운다(배포 영수증을 `~/c207-deploy-state`에 두는 것과 같은 이유).
4. **시험 DB가 아니면 확인 문구를 손으로 타이핑**해야 한다. DB 이름이 `_test`로 안 끝나면 `DELETE <db이름>`을 그대로 입력하라고 요구하고, 입력이 터미널이 아니면(파이프·CI) 거절한다(종료 코드 3).
5. **지운 뒤 남은 행 수를 표로 보고**한다.
6. **지우는 사이에 새 경고가 끼어들면 통째로 되돌린다**(종료 코드 5). 지우는 건 백업에 담긴 id뿐이라, 조회 뒤에 들어온 경고가 지워질 이벤트를 가리키면 그 경고만 아무것도 안 가리키는 채로 남는다. 삭제 트랜잭션 안에서 다시 훑어 하나라도 있으면 롤백한다(같이 지우면 백업에 없어서 되돌릴 길이 없다). 인입을 멈추고 다시 돌려라.

되돌리기는 백업 파일 하나로 한다. `INSERT ... ON CONFLICT DO NOTHING`이라 여러 번 돌려도 같은 결과이고, 원래 id를 그대로 넣어 경고의 `source_id` → 이벤트 역추적이 살아난다(넣은 뒤 시퀀스를 최대 id로 다시 맞춘다). ⚠ 지운 뒤 새 행이 그 id를 차지했으면 그 행은 안 덮고 건너뛴다 — 되돌릴 거면 지운 직후가 안전하다. ⚠ **이벤트가 건너뛰어졌는데 그 이벤트를 가리키던 경고는 들어가는 갈래가 있다.** 그러면 경고의 `source_id`가 지금 그 자리를 차지한 다른 행을 가리키거나 아무것도 안 가리킨다. 끝에 찍히는 "건너뛴 행(이미 있음)" 표가 0이 아니면 그 표부터 눈으로 확인해라.

지우는 순서는 `alert` → `gate_pass_event` → `tagging_event` → `shuttle_arrival`이다(경고를 먼저 지운다 — 이벤트만 지우면 아무것도 안 가리키는 경고가 남아 화면이 빈 사건을 그린다). 기기·게이트로 좁힌 실행에서는 경고를 직접 고르지 않고 **지워질 이벤트를 가리키는 경고만 따라 지운다**(경고엔 `device_id`도 `gate_no`도 없다). `shuttle_arrival`엔 `device_id` 칸이 없어 `event_id` 접두로 가른다.

## 통계에서 시험 자료 빼기 (기본 꺼짐)

`GET /api/stats/untagged`·`GET /api/stats/today`가 세는 모집단을 설정으로 좁힌다. **기본은 꺼짐이라 켜지 않으면 집계가 한 건도 안 달라진다.** 시연에서 "센서 오검출까지 센 숫자"를 보여줄지 "사람이 실제로 지나간 숫자"를 보여줄지가 아직 안 정해져서 스위치로 뒀다(안건 ①②와 같은 방식).

| 환경변수 | 기본값 | 무엇을 하나 |
|---|---|---|
| `STATS_EXCLUDE_TEST_DATA` | `false` | 아래 두 축을 켜는 스위치. 꺼져 있으면 두 축을 채워도 아무 일도 안 일어난다 |
| `STATS_TEST_DEVICE_PREFIXES` | `test-` | 시험 기기 `device_id` 접두(콤마로 여럿). 셔틀은 `device_id`가 없어 `event_id` 접두로 본다 |
| `STATS_EXCLUDE_BEFORE` | 없음 | 이 시각(ISO) 앞 자료를 안 센다. 경고(`active_alerts`)에 걸리는 축은 이거 하나다. **오프셋을 안 적으면 KST(+09:00)로 읽는다** |

⚠ **`STATS_EXCLUDE_BEFORE`에 오프셋을 적어라** — `2026-07-30T00:00:00+09:00` 꼴로. 오프셋을 뺀 `2026-07-30T00:00:00`은 에러가 안 나고 그냥 통과하는데, 보정이 없으면 그 값이 **서버 프로세스의 로컬 시간대**로 읽혀 기계마다 컷이 달라진다(2026-07-30 실측: 한국 로케일 개발 기계에서는 KST, `TZ=UTC`인 도커 컨테이너에서는 UTC — **아홉 시간 어긋난 컷**이고 그날 아침 9시간이 통계에서 빠진다). 손으로는 재현이 안 되고 배포한 서버에서만 어긋나는 종류라, 서버가 오프셋 없는 값을 **KST로 못박고 경고 로그를 남긴다**(`app/stats.py`의 `_assume_kst`). 그래도 뜻을 코드가 짐작하게 두지 말고 값에 직접 적는 편이 낫다. 정리 도구(`tools/purge_test_data.py`)의 `--before`에는 이 보정이 없다.

⚠ **이 스위치를 켜면 미처리 경고 수가 창구마다 갈린다.** 시각 컷을 타는 자리는 `GET /api/stats/today`의 `active_alerts`뿐이고, `GET /api/dashboard/snapshot`의 `counts.active_alerts`(`routers/query.py`의 `count_active_alerts`)와 관제 보조(`assistant_data.py`)는 `apply_stats_filter`를 안 타서 전 기간을 그대로 센다. 같은 화면 안에서 숫자가 둘로 갈리니 **켤 때 그 두 자리도 같이 통일해라.** 기본이 꺼짐이라 지금은 세 자리 값이 같고, 켤지 말지는 팀 결정 대기다.

⚠ **기기 접두 축은 실서버에서 아직 아무것도 안 거른다.** 통과 494건·태깅 12건의 `device_id`가 전부 `raspberry01`이다(2026-07-30 EC2 실측) — ToF 오검출도 진짜 라파이가 올린 거라 기기 이름으로는 진짜와 안 갈린다. **지금 그 오검출을 빼는 축은 `STATS_EXCLUDE_BEFORE` 하나뿐이다.** 접두 축은 앞으로 손시험·시뮬을 `test-` 접두로 쌓기로 하면 산다(정리 도구가 제안하는 축과 같다).

⚠ **기본 접두에 `webcall-`은 안 넣었다.** 그건 화면 셔틀 가상 호출 접두인데, 셔틀이 실기기를 안 만들기로 확정돼서 `webcall-`이 시연의 유일한 셔틀 경로다. 시험 취급하면 시연 중 셔틀 건수가 통째로 0으로 나온다. 정리 도구가 그 접두를 지우기 대상으로 함께 제안하는 건 "시연 전에 쌓인 걸 비운다"는 다른 목적이다.

⚠ `device_id`가 NULL인 행은 켜도 **남는다**. SQL에서 `device_id NOT LIKE 'test-%'`는 NULL에 거짓을 줘서, 가드가 없으면 기기 이름을 안 보내던 예전 인입이 통계에서 통째로 사라진다.

스위치는 요청 파라미터가 아니라 서버 설정이다 — 부르는 쪽이 고를 수 있으면 같은 화면의 두 카드가 서로 다른 모집단을 센다. 값 셋은 `app/stats.py`의 `StatsFilterSettings`가 읽고(환경변수와 `backend/.env` 둘 다), 조건을 붙이는 자리는 `apply_stats_filter` 함수 하나다. ⚠ `config.Settings`가 아니라 거기 따로 둔 건 `app/config.py`가 이번 수리 범위 밖이었기 때문이고, 팀이 정하면 필드 셋을 `Settings`로 옮기면 된다.

## 미태깅 통계 — `GET /api/stats/untagged` (팀 확정 대기)

관리자 화면이 "언제 몰리나"를 보는 자리다. 조사 정본이 시계열 예측 모델을 접었고(표본이 시연 며칠치라 과적합밖에 안 된다), 대신 집계 두 가지로 간다 — 일자별 추이 + 이동평균, 요일×시간대 히트맵.

파라미터는 셋이고 전부 선택이다. `days`(오늘 포함 최근 며칠, 기본 7 · 1~90), `window`(이동평균 구간 크기, 기본 3 · 1~30), `gate_no`(비우면 전체 게이트 합). 범위를 벗어나면 조용히 자르지 않고 **422**다(`/api/eta`의 `samples`와 같은 정책). `window > days`도 422다 — 이동평균이 통째로 `null`이 되는 값을 몰래 줄이면 응답의 `window`와 실제 계산이 갈라진다.

```bash
curl 'http://127.0.0.1:8000/api/stats/untagged?days=14&window=7&gate_no=2'
```

응답은 화면이 그대로 그릴 수 있게 빈 칸까지 채워 나간다.

| 칸 | 내용 |
|---|---|
| `range` | `days`·`start_date`·`end_date`(포함)·`timezone`. 날짜는 전부 KST 로컬 날짜다. |
| `daily[]` | `date`·`count`·`moving_avg`. 건수가 0인 날도 빠짐없이 `days`개 들어온다(빼면 화면이 그 날짜를 건너뛴 채 선을 잇는다). |
| `heatmap` | `weekday_labels`(월~일)·`matrix[요일][시각]`·`max_count`. 늘 7행 24열로 꽉 차 있고 열은 0시~23시다. |
| `peak` | 가장 많이 몰린 칸(`weekday` 0=월 … 6=일). 구간에 미태깅이 없으면 `null`. |
| `total` | 구간 전체 건수. |

`moving_avg`는 뒤쪽 `window`일 단순평균이고, **창이 덜 찬 앞머리는 `null`**이다. 부분 평균으로 채우면 첫날 값이 실측과 같아서 선이 실측을 따라가다 갑자기 꺾인다. `null`과 `0.0`은 다른 뜻이다 — 앞머리는 "아직 못 그림", 그 뒤 `0.0`은 "정말 0건".

세는 대상은 **미태깅 판정이 붙은 빔 통과**(`gate_pass_event.verdict='untagged'`)이지 `alert` 행이 아니다. 경고는 쿨다운(`ALERT_COOLDOWN_SEC`)으로 삼켜지니까, 사람이 몰릴수록 실제 미태깅보다 적게 세어 통계가 거꾸로 눕는다. 시각 축은 `observed_at`(기기 관측)이다 — 서버 수신 시각을 쓰면 백로그·배속 재생이 "새벽 3시 폭증"으로 보인다.

⚠ 버킷은 UTC가 아니라 **KST 로컬 날짜·시각**으로 자른다. UTC로 자르면 하루 경계가 오전 9시에 놓여 "7월 27일 통계"에 26일 저녁 아홉 시간이 섞인다. 변환은 전부 Postgres `timezone()`이 맡는다 — 파이썬 `zoneinfo`는 윈도우에 IANA 표가 없어 기기마다 갈리고, 양쪽에서 따로 변환하면 서버와 DB가 서로 다른 날짜로 자를 수 있다. 시간대는 `app/stats.py`의 `STATS_TIMEZONE` 상수 하나다.

## 오늘 집계 — `GET /api/stats/today` (팀 확정 대기)

개발자 텔레메트리 화면 맨 위 카드가 읽는 자리다. 예전엔 `/api/events?limit=500`으로 받은 목록을 **화면이 세서** 그렸는데, 하루 건수가 조회 상한 500을 넘으면 실제보다 적게 나왔다. 세는 자리를 서버로 옮겨 상한을 없앴다.

파라미터가 없다 — 하루 경계는 서버가 정한다. 구간은 `/api/stats/untagged?days=1`과 **같은 함수**(`app/stats.py`의 `_resolve_range`)로 잡아서, 두 API가 서로 다른 "오늘"을 쓰는 일이 없다.

```bash
curl 'http://127.0.0.1:8000/api/stats/today'
```

```json
{
  "date": "2026-07-29",
  "timezone": "Asia/Seoul",
  "tagging": 128,
  "gate_pass": {
    "total": 96, "normal": 71, "untagged": 18,
    "exit": 5, "beam_incomplete": 2, "other": 0
  },
  "shuttle_arrival": 7,
  "active_alerts": 12
}
```

| 칸 | 내용 |
|---|---|
| `date`·`timezone` | 집계한 하루(KST 로컬 날짜)와 그 기준 시간대. 화면은 브라우저 시계 대신 이 날짜를 쓴다. |
| `tagging` | 오늘 태그 인식 건수(`observed_at` 기준). |
| `gate_pass` | 오늘 게이트 통과. `total`은 판정 다섯 칸의 합이라 화면이 따로 안 더해도 된다. |
| `gate_pass.other` | 판정이 아직 안 붙은 행(`null`)과 사전에 없는 새 판정. 새 판정이 생겨도 `total`과 칸 합이 안 갈라진다. |
| `shuttle_arrival` | 오늘 셔틀 도착 신호(`signal_ts` 기준). |
| `active_alerts` | 아직 확인 안 한 경고 **전수**. |

⚠ **`active_alerts`만 오늘 범위가 아니다.** ack=false인 경고 전부라 어제 것도 들어간다 — "오늘 몇 건 났나"가 아니라 "지금 몇 건이 남아 있나"이고, 스냅샷 `counts.active_alerts`와 같은 값이다. 화면 카드도 "전체 기준"이라 적는다.

시각 축은 계열마다 "기기가 본 시각"이다 — 태깅·통과는 `observed_at`, 셔틀은 `signal_ts`. 서버 수신 시각으로 자르면 백로그로 늦게 올라온 어제 이벤트가 오늘에 붙는다. KST 버킷 규칙은 위 미태깅 통계와 똑같다(Postgres `timezone()`).

화면은 이 API가 없거나 실패해도 죽지 않는다 — 예전처럼 불러온 이벤트에서 세는 폴백으로 내려앉고, 그때는 "조회 상한에 걸려 실제보다 적을 수 있습니다"를 안내 줄에 그대로 적는다(`app/static/telemetry.html`의 `countFromServer`·`countFromEvents`).

## 경고 대응 통계 — `GET /api/stats/alert-response` (프론트 요구 N2 · 팀 확정 대기)

AI 탭 "경고 대응 통계" 카드가 읽는 자리다. 경고가 난 뒤 관제가 **얼마 만에** 확인하고 종결했나를 구간 전체와 종류별로 센다.

파라미터는 `days` 하나다(오늘 포함 최근 며칠, 기본 7 · 1~90). 범위를 벗어나면 조용히 자르지 않고 **422**다(`/api/stats/untagged`와 같은 정책).

```bash
curl 'http://127.0.0.1:8000/api/stats/alert-response?days=7'
```

```json
{
  "range": {"days": 7, "start_date": "2026-07-22", "end_date": "2026-07-28", "timezone": "Asia/Seoul"},
  "overall": {
    "total": 12,
    "ack": {"count": 9, "avg_seconds": 41.33, "median_seconds": 22.0},
    "resolve": {"count": 5, "avg_seconds": 610.4, "median_seconds": 480.0}
  },
  "by_type": [
    {"type": "untagged", "total": 10,
     "ack": {"count": 8, "avg_seconds": 35.5, "median_seconds": 20.0},
     "resolve": {"count": 4, "avg_seconds": 550.0, "median_seconds": 460.0}},
    {"type": "robot_offline", "total": 2,
     "ack": {"count": 1, "avg_seconds": 88.0, "median_seconds": 88.0},
     "resolve": {"count": 1, "avg_seconds": 852.0, "median_seconds": 852.0}}
  ]
}
```

| 칸 | 내용 |
|---|---|
| `range` | 미태깅 통계와 같은 모양·같은 함수(`_resolve_range`)다. 날짜는 KST 로컬 날짜고 `end_date`는 구간에 포함된다. |
| `overall` | 구간 전체 한 줄. `by_type`을 합쳐 만들지 **않는다** — 중앙값은 부분집합에서 다시 만들 수 없어 같은 조건으로 한 번 더 질의한다. |
| `by_type[]` | 종류(`alert.type`)별 같은 모양 + `type`. 건수 많은 순이고 같으면 종류 이름 오름차순이다(동점이어도 응답 순서가 안 흔들리게). |
| `*.total` | 구간 안에 **생긴** 경고 수. |
| `*.ack` | `created_at` → `acked_at` 소요시간(초). `count`는 확인 시각이 찍힌 경고 수다. |
| `*.resolve` | `created_at` → `resolved_at` 소요시간(초). |

⚠ **미확인·미종결은 0초로 안 채운다.** 확인 시각이 없는 경고는 `total`에만 남고 `ack.count`에서 빠진다 — 둘의 차가 곧 "아직 안 본 건"이다. 0으로 채우면 "빨리 봤다"로 읽혀 대응 통계가 거꾸로 눕는다. 표본이 하나도 없으면 `count=0`에 `avg_seconds`·`median_seconds`가 **`null`**이다(`0.0`이 아니다 — `moving_avg`가 null과 0.0을 가르는 것과 같은 이유). SQL 집계가 NULL 행을 건너뛰므로 0으로 나누는 자리가 아예 안 생긴다.

⚠ **구간을 자르는 축은 `created_at` 하나다.** 확인 시각으로 자르면 어제 난 경고를 오늘 확인한 건이 "오늘 난 경고"로 세어져 종류별 건수가 `/api/alerts` 목록과 갈라진다. 평균·중앙값은 Postgres `avg`·`percentile_cont(0.5)`가 내고, 소수 둘째 자리에서 반올림한다.

## 시간 버킷 집계 — `GET /api/stats/gate-pass-buckets` · `GET /api/stats/alert-buckets` (프론트 요구 N12 · 팀 확정 대기)

이벤트 탭 추이 차트 두 칩이 읽는 자리다. 버킷팅을 서버에서 하는 이유는 오늘 집계와 같다 — 화면이 `/api/events`·`/api/alerts` 목록을 받아 직접 자르면 조회 상한(500건)에 걸려 바쁜 시간대가 실제보다 낮게 그려진다.

칸 크기는 파라미터가 아니라 창구마다 고정(15분·24시간)이다. 부르는 쪽이 고를 수 있으면 같은 차트의 두 칩이 서로 다른 격자를 그린다(`STATS_EXCLUDE_TEST_DATA`를 요청 파라미터로 안 뺀 것과 같은 이유).

### 15분 버킷 × 판정 4갈래 — `GET /api/stats/gate-pass-buckets`

파라미터는 `hours` 하나다(지금이 든 칸까지 거슬러 몇 시간, 기본 24 · 1~72). 범위 밖은 **422**다. 72시간이 상한인 건 그 위로 가면 한 응답이 칸 288개를 넘겨 차트가 못 그리기 때문이다.

```bash
curl 'http://127.0.0.1:8000/api/stats/gate-pass-buckets?hours=24'
```

```json
{
  "range": {
    "start": "2026-07-27T05:15:00Z", "end": "2026-07-28T05:15:00Z",
    "bucket_seconds": 900, "bucket_count": 96, "timezone": "Asia/Seoul"
  },
  "total": 143,
  "buckets": [
    {"start": "2026-07-27T05:15:00Z",
     "counts": {"total": 3, "normal": 2, "untagged": 1, "exit": 0, "beam_incomplete": 0, "other": 0}}
  ]
}
```

`buckets[].counts`는 `/api/stats/today`의 `gate_pass`와 **같은 모양·같은 함수**다(`app/stats.py`의 `_gate_pass_counts`). 사전 밖 판정과 미판정(`null`)은 `other`로 모이고, `total`은 다섯 칸의 합이다. 건수가 0인 칸도 빠짐없이 `bucket_count`개 실린다 — 빼면 화면이 그 시간대를 건너뛴 채 선을 이어 그려서 조용하던 15분이 그래프에서 사라진다. 시각 축은 `observed_at`(기기 관측)이다.

### 24시간 버킷 × 심각도 3갈래 — `GET /api/stats/alert-buckets`

파라미터는 `days` 하나다(오늘 포함 최근 며칠, 기본 7 · 1~90). 범위 밖은 **422**다.

```bash
curl 'http://127.0.0.1:8000/api/stats/alert-buckets?days=7'
```

```json
{
  "range": {"days": 7, "start_date": "2026-07-22", "end_date": "2026-07-28", "timezone": "Asia/Seoul"},
  "total": 21,
  "buckets": [
    {"date": "2026-07-22", "counts": {"total": 4, "info": 1, "warning": 1, "high": 2, "other": 0}}
  ]
}
```

심각도 사전은 `info`·`warning`·`high` 셋이고, 값 셋은 경고를 만드는 자리에서 그대로 가져왔다(`credit/state_machine.py`의 `high`, `robot_channel.py`의 `warning`·`info`, `dispatch.py`의 `info`). 사전 밖 값과 `null`은 `other`로 모여서, 새 심각도가 생겨도 `total`과 칸 합이 안 갈라진다. 시각 축은 `created_at`이다(경고엔 기기 관측 시각이 따로 없다). 건수가 0인 날도 `days`개 다 실린다.

### ⚠ 시간대와 버킷 경계 규칙 (두 창구 공통 계약)

- **경계는 시작 이상 끝 미만**이다. 칸의 첫 초는 그 칸이고 다음 눈금 정각은 다음 칸이다 — 이 저장소 다른 집계와 같다.
- **15분 칸은 KST 벽시계 눈금**(`:00`·`:15`·`:30`·`:45`)에 붙는다. 한국은 DST가 없고 오프셋이 `+09:00` 정시라 그 눈금이 UTC 15분 눈금과 **같은 자리**다. 그래서 이 격자만은 시간대 변환 없이 epoch 나눗셈으로 잡는다(변환이 없으니 파이썬과 DB가 갈릴 자리도 없다). 시연이 DST 있는 지역으로 옮겨가면 이 전제가 깨지고 DB `timezone()` 변환을 타야 한다.
- **창의 끝은 "지금"이 든 칸의 다음 눈금**이라, 진행 중인 칸이 마지막 자리에 들어온다. 지금 시각을 그대로 끝으로 쓰면 마지막 칸이 15분이 아닌 토막이 돼서 차트가 늘 우하향으로 보인다.
- **24시간 칸은 KST 로컬 날짜 경계**(`00:00` KST)다. 지금부터 24시간씩 거꾸로 세면 칸이 자정에 안 맞아 "어제"가 두 칸에 걸친다. 이쪽 변환은 다른 집계와 똑같이 전부 Postgres `timezone()`이 맡고, 구간은 미태깅 통계와 **같은 함수**(`_resolve_range`)로 잡는다.
- 시간대는 두 창구 다 `app/stats.py`의 `STATS_TIMEZONE` 상수 하나이고, 응답 `range.timezone`에 그대로 실린다.

### 시험 자료 거르기와의 관계 (N2·N12 세 창구 공통)

세 창구 다 `apply_stats_filter` 한 함수로만 `STATS_EXCLUDE_TEST_DATA`를 받는다(경고 계열은 기기 축이 없어 시각 컷만, 통과 계열은 기기 축까지). ⚠ 위 "통계에서 시험 자료 빼기" 절이 적은 **창구끼리 미처리 경고 수가 갈리는 결함은 여기 안 번진다** — 그 어긋남은 `/api/stats/today`의 `active_alerts`가 전 기간을 세면서 시각 컷만 타는 데서 오는데, 이 세 창구는 애초에 구간을 자르는 집계뿐이라 전 기간을 세는 칸이 없다. 그래서 견줄 상대가 안 생긴다.

## 대시보드 붙이는 법

화면은 **REST 스냅샷으로 초기 상태를 채우고, 그 뒤 변화는 WebSocket으로 받는다.** 두 경로가 같은 데이터를 서로 다른 시점에 나를 뿐이라, 붙이는 순서가 정해져 있다.

1. 먼저 `/ws/dashboard`를 연다.
2. 그다음 `GET /api/dashboard/snapshot`을 읽어 화면을 채운다.
3. 스냅샷을 읽는 동안 밀려온 메시지는 중복을 걸러 합친다. **거르는 키가 계열마다 다르다.**
4. 스냅샷이 **낡은 값으로 새 값을 덮지 않게** `last_event_at`으로 가린다 — 바로 아래 절.

#### 순서 어긋남 가리기 — `last_event_at`

스냅샷을 읽는 동안 들어온 사건은 WS로 **먼저** 도착한다. 그런데 뒤늦게 온 스냅샷이 그 자리를 예전 값으로 덮으면, 화면은 방금 받은 새 값을 잃는다. 중복 키만으로는 못 막는다 — 그건 "같은 줄인가"만 알려 주고 "어느 쪽이 새 값인가"는 안 알려 준다.

그래서 스냅샷이 **기준 시각**을 같이 준다. 계약은 한 줄이다.

> WS 봉투의 `timestamp`가 스냅샷 `last_event_at`보다 **크면** 그 소식은 스냅샷에 없다. 스냅샷이 그 자리를 덮으면 안 된다.

- `last_event_at`은 **이 스냅샷이 담은 마지막 사건의 서버 수신 시각**이고 `recent_events[0].received_at`과 같은 값이다. 서버 현재 시각(`server_time`)이 **아니다** — 조회를 끝낸 시각을 실으면 조회 사이에 커밋된 사건까지 "이미 담겼다"로 읽혀서 계약이 뒤집힌다.
- 담긴 게 없으면 `null`이다. 그때는 WS로 온 걸 전부 살리면 된다.
- 틀리는 방향이 한쪽으로만 기울어 있다. 스냅샷에 **든** 사건도 방송 시각이 더 늦어서 "새 소식"으로 한 번 더 올 수 있는데, 그건 위 중복 키로 걸러진다. 반대로 **없는** 사건이 가려지는 일은 없다. 경고는 재방송이 없어서 한 번 가려지면 영영 사라지므로, 안전한 쪽을 이렇게 잡았다.
- ⚠ 두 값의 시계가 다르다. `last_event_at`은 DB 시계(`now()`)고 봉투 `timestamp`는 API 프로세스 시계다. 지금 배포는 API와 DB가 같은 호스트의 컨테이너라 커널 시계를 공유한다. **DB를 딴 호스트로 떼면 NTP가 전제다.**
- ⚠ 사건 id가 아니라 시각인 이유 — 인입 3표가 시퀀스를 따로 써서 표를 가로지르는 단조 증가 id가 없고, WS 이벤트 `data`에는 행 `id`가 아예 안 실린다(`event_id`만). 봉투에 `seq`를 새로 넣는 길은 로봇 전선(`/ws/robot`) 봉투까지 같이 바꾸는 계약 변경이라 백엔드 혼자 정할 자리가 아니다.

중복 키는 이렇게 갈린다.

- **이벤트 피드** — `tagging_event`·`gate_pass_event`·`shuttle_arrival` 메시지의 `data.event_id`를 스냅샷 `recent_events[].event_id`와 맞춘다.
- **경고** — `untagged_alert` 메시지에는 `event_id`가 없다. `data.alert_id`를 스냅샷 `active_alerts[].id`와 맞춘다.
- **경고 확인** — `alert_ack` 메시지가 오면 그 `data.alert_id` 줄을 확인 처리로 바꾼다. 대시보드 두 대가 열려 있어도 한쪽에서 누른 확인이 다른 쪽에 그대로 퍼진다(S15P11C207-81). 이미 확인한 경고를 또 눌러도 응답은 200이지만 메시지는 안 나간다 — 화면 둘이 서로의 재확인을 받아 같은 줄을 다시 그리는 걸 막는 자리다.
- **신원 입력** — `alert_identified` 메시지가 오면 그 `data.alert_id` 줄의 신원 상태를 `data.identified`로 바꾼다. 같은 이유다 — 한쪽에서 적은 신원이 다른 쪽에 안 보이면 둘째 요원이 눌러 보고 409를 받고서야 안다. 신원을 지운 경우도 이 메시지로 오고 그때는 `identified: false`다.

WS를 나중에 열면 그 사이 이벤트가 통째로 사라진다. 서버는 재전송 버퍼를 두지 않는다.

### 초기 로드 — `GET /api/dashboard/snapshot` (팀 확정 대기)

⚠ **이 엔드포인트는 아직 확정 계약이 아니다.** 아키텍처 02절 "대시보드가 읽는 것" 조회 API 목록에 없는 신규 엔드포인트라, 백엔드가 초기 로드용으로 먼저 낸 것이다. 목록 편입 여부는 팀 확정 대기다. 확정 전까지는 목록 API 3종(`/api/robots`·`/api/events`·`/api/alerts`)으로도 같은 화면을 채울 수 있게 붙여 두는 편이 안전하다.

인증이 없다(조회 계열 공통, 학내 시연 전제). 응답은 이렇게 생겼다.

| 칸 | 내용 |
| --- | --- |
| `server_time` | 서버 UTC 시각. 화면이 기기 시계와의 차이를 보정하는 데 쓴다 |
| `last_event_at` | **이 스냅샷이 담은 마지막 사건의 시각**(`recent_events[0].received_at`과 같은 값, 없으면 `null`). WS 메시지를 "스냅샷보다 나중 소식"으로 가리는 기준선이다 — 위 "순서 어긋남 가리기" 절 |
| `ws_protocol_version` | `/ws/dashboard` 메시지의 `version`과 같은 값. 다르면 화면이 구버전이다 |
| `robots[]` | `id · name · mode · battery · network_status · updated_at · position · led_status · mission_status`. 앞 여섯은 `Robot` 행 값이고 뒤 셋은 서버 메모리 최신값이라 재기동 뒤엔 `null`이다. **그릴 값이 하나도 없는 행은 빠진다** — 아래 ⚠ 참고 |
| `active_alerts[]` | 아직 확인 안 한(`ack=false`) 경고. `id · type · severity · source_type · source_id · ack · created_at · identified · identified_at · status · acked_at · resolved_at · resolution`, 최근 50건(정본 `app/schemas.py`의 `AlertOut`). 뒤 넷은 사건 수명주기 칸이다 — `status`는 `OPEN`·`ACKED`·`IDENTIFIED`·`RESOLVED` 중 하나이고 `resolution`은 종결 사유다(위 "사건 수명주기 창구" 절). 신원 원문·메모·확인한 요원(`identified_by`)은 안 실리고, **`acked_by`·`resolved_by`도 같은 이유로 안 싣는다** |
| `recent_events[]` | 인입 3계열 통합 피드 최근 30건 |
| `counts` | `robots · active_alerts · recent_events`. `active_alerts`는 상한과 무관한 전체 수다 |
| `identify_report_enabled` | 신원 보고 채널(상위 보고선 웹훅)이 켜져 있나. 꺼져 있으면 화면이 "카드가 나갔습니다"라고 적으면 안 된다 |

⚠ **`robots[]`는 `robot` 테이블 전 행이 아니다.** 모드·배터리·통신·위치·LED·임무 상태가 전부 비어 그릴 값이 하나도 없는 행은 뺀다(`routers/query.py`의 `_has_nothing_to_show`). `GET /api/robots`와 `counts.robots`도 같은 함수를 타서 세 자리가 같은 모집단을 본다. 그런 행이 생기는 자리는 하나다 — 셔틀 통지를 DB에 남기려고 `robot_channel._ensure_robot_pk`가 이름만 있는 행을 만든다(그 행이 없으면 `notified_robot_id`가 NULL로 남아 재전송 판정이 같은 신호를 다시 내보내 로봇이 두 번 출동한다). **기록은 남기고 화면에서만 가리는 구조라 둘은 짝이다** — 한쪽만 지우면 두 번 출동이나 빈 로봇 카드 중 하나가 돌아온다. 그 로봇이 상태를 한 번 올리면 같은 행에 값이 채워져 카드가 그때 나타난다. `mission_status`를 이 잣대에 넣어도 통지 기록용 행은 안 뜬다 — 그 행은 상태 보고를 한 번도 안 밟아서 임무 상태 칸이 영영 `null`이다.

##### `robots[].mission_status`는 "마지막으로 보고된 상태"다 (프론트 요구 N10)

이 칸은 로봇이 마지막으로 올린 임무 상태(`ARRIVED`·`WEATHER_BLOCKED`·`RETURN_COMPLETE`)다. 예전엔 이 값이 WS `robot_state`로만 흘러서, 화면을 새로 고치면 개요 탭 "로봇 상태" 타일이 빈 채로 시작했다. 계약은 셋이다.

- **`null`이 정상 답이다.** 위치·LED와 같은 인메모리 자리라 **컨테이너를 다시 띄우면 비고**, 다음 상태 보고가 채운다. 로봇이 임무 상태를 한 번도 안 올렸을 때도 `null`이다. 칸 자체는 늘 있으니 화면은 "칸 없음"이 아니라 "값 없음"만 다루면 된다.
- **`null`은 "없음"이 아니라 "보고 안 함"이다.** 주행 중엔 이 칸을 비우고 올리는 로봇이 있어서, 안 실린 보고는 직전 값을 **안 덮는다**(`status_summary`의 절대 상태 규칙과 같다). 지우면 게이트로 달려가는 동안 타일이 깜빡인다.
- **알림을 내는 에지와는 다른 자리다.** 알림 에지 상태(`robot_channel._mission_states`)는 `MISSION_STATE_TTL_SEC`로 걷히는데 — 한참 조용했던 로봇의 `ARRIVED`를 진짜 도착으로 다시 세야 하니까 — 이 표시 값은 안 걷힌다. 둘이 갈리는 순간은 TTL을 넘겨 조용한 로봇 하나뿐이고, 그때 알림 쪽은 비고 화면 쪽은 마지막 보고값을 든다. 표시를 에지 상태에서 읽으면 화면이 알림용 손잡이에 딸려 흔들리고, 조회(GET) 한 번이 청소라는 상태 변경을 밟아야 값이 맞아떨어진다.

`recent_events[]` 한 줄은 `kind`(`tagging`/`gate_pass`/`shuttle_arrival`)로 갈리고, 계열마다 안 쓰는 칸은 `null`로 온다 — 공통은 `id · event_id · gate_no · observed_at · received_at`, 태깅은 `tag_id`, 통과는 `direction · verdict`에 더해 태운 크레딧의 `tag_id`(크레딧을 안 태운 판정이면 `null`), 셔틀은 `shuttle_no`다. `id`는 계열 테이블의 행 PK라 `active_alerts[].source_id`와 `(kind, id)`로 짝지어 경고를 낸 이벤트를 되짚는다(S15P11C207-192). 계열마다 시퀀스가 따로라 `id` 하나만으로는 유일하지 않다.

정렬은 `received_at` 내림차순에 행 `id` 내림차순 tiebreaker를 얹는다 — 같은 트랜잭션에 들어온 행들의 순서가 흔들리지 않게 한다(S15P11C207-191·193, 경고 목록도 `created_at`·`id` 같은 꼴).

목록 API 3종은 같은 데이터를 더 길게 볼 때 쓴다. `GET /api/events?limit=` (1~500, 기본 50, `?kind=`로 계열을 추린다), `GET /api/alerts?active=true&limit=` (1~500, 기본 100 — `active` 기본 false면 확인한 것까지 섞어 **최근 100건(기본 `limit`)**만 온다. 더 보려면 `limit`을 올린다), `GET /api/robots`. 창 밖 `limit`은 조용히 깎지 않고 `422`로 막는다. 스냅샷과 목록은 같은 조회 함수를 쓰므로 값이 갈리지 않는다.

#### `GET /api/events` 계열 축 — `kind`

인입 3계열 중 하나만 뽑는 축이다(프론트 요구 N8). 통과 이벤트 표(이벤트 탭 2차 구성)가 통과 100건을 보려고 합본 500건을 받아 화면에서 거르던 자리를 없앤다.

값은 **응답 한 줄의 `kind`와 같은 글자**다 — `tagging` · `gate_pass` · `shuttle_arrival`. 셔틀은 `shuttle`이 아니다(경고 `source_type`과 같은 글자여야 `(kind, id)` 역추적이 붙는다).

| 준 것 | 결과 |
| --- | --- |
| `?kind=gate_pass` | 그 계열만. `limit`·정렬의 뜻은 그대로라 **"그 계열에서 최근 `limit`건"**이다 — 다른 계열이 자리를 안 차지한다 |
| 안 줌 | 예전 그대로 3계열 합본(기존 계약 불변) |
| 모르는 `kind`(빈 글자 포함) | `422` |

⚠ **모르는 값을 `/api/alerts`의 `source_type`과 일부러 다르게 다룬다.** 저쪽은 모르는 글자도 빈 목록에 `200`이고 여기는 `422`다. 이유는 두 축의 값 공간이 다르기 때문이다 — 경고의 `source_type`은 자유 문자열이고 이벤트 3계열 말고 `robot` 출처가 실제로 쓰인다(`robot_channel`). 그래서 아는 글자로 잠그면 정상 질의가 막힌다. 반면 이벤트의 `kind`는 UNION 가지 딱 셋이 만들어 내는 값이라 그 밖의 글자는 **답이 있을 수 없는 질의**다. 빈 목록으로 조용히 돌려주면 화면이 오타를 "그 계열은 오늘 없다"로 그린다.

#### `GET /api/alerts` 축 넷 — `active` · `type` · `status` · `source_type`+`source_id`

넷을 같이 주면 교집합이다. `active`는 "관제가 확인을 눌렀나"(`ack`)를, `status`는 "어디까지 진행됐나"(`OPEN`·`ACKED`·`IDENTIFIED`·`RESOLVED`, 여러 번 붙일 수 있다)를 본다. 두 축은 독립이라 한쪽으로 다른 쪽을 갈아 쓰면 종 배지 숫자가 달라진다(`tests/test_alert_status_filter.py`).

`source_type`·`source_id`는 **발화 이벤트로 경고를 되짚는 축**이다(프론트 요구 N13). 이벤트 한 줄의 `(kind, id)`가 경고의 `(source_type, source_id)`라는 규칙(S15P11C207-192)을 질의로 연 것이라, 이벤트 표가 "처리" 열을 그릴 때 경고 목록을 최근순 상한만큼 받아 뒤질 필요가 없다. 그 상한(최대 500) 밖으로 밀린 경고도 이 축이면 바로 집힌다.

| 준 것 | 결과 |
| --- | --- |
| 짝(`?source_type=gate_pass&source_id=12`) | 그 이벤트가 낸 경고만. 없으면 빈 목록에 `200`(404가 아니다 — "그 이벤트는 경고를 안 냈다"가 정상 답이다) |
| `source_type`만 | 그 종류 전체 |
| `source_id`만 | `422`. 계열마다 시퀀스가 따로라 id 하나로는 행이 안 정해진다 — 통과 12번과 태깅 12번이 같이 걸려 화면이 남의 사건 처리 상태를 그린다 |
| `source_id` 0 이하 · 32자 넘는 `source_type` | `422`(`limit`·`samples`와 같은 정책 — 조용히 안 깎는다) |
| 모르는 `source_type` | 빈 목록에 `200`. 아는 글자 목록으로 안 좁힌다 — 경고는 이벤트 3계열 말고 `robot`도 쓴다 |

### 실시간 — `/ws/dashboard` 이벤트 목록

**개발자 텔레메트리 페이지와 사용자 대시보드가 같은 소켓을 쓴다.** 채널을 나누지 않았고 구독 필터도 없다 — 붙은 클라이언트 전부에게 모든 메시지가 그대로 간다. 필요한 종류만 골라 쓰는 건 지금은 화면 몫이다.

메시지는 `{type, version, robot_id, timestamp, data}` 다섯 칸 고정이고, `robot_id`는 로봇에서 난 이벤트가 아니면 `null`이다. 붙는 순간 `hello` 한 장이 먼저 온다.

| `type` | 언제 | `data` |
| --- | --- | --- |
| `hello` | 접속 직후 1회 | `channel`(=`dashboard`) · `clients` |
| `tagging_event` | 태깅 인입이 새로 저장됐을 때 | `event_id` · `gate_no` · `tag_id` · `observed_at` |
| `gate_pass_event` | 빔 통과 판정이 끝났을 때(정상 통과도 온다) | `event_id` · `pass_id` · `gate_no` · `direction` · `status` · `verdict` · `matched_tagging_event_id` · `tag_id` · `low_confidence` · `device_result` |
| `untagged_alert` | 미태깅 판정 + 쿨다운 통과 | `alert_id` · `gate_no` · `source_type` · `source_id` · `low_confidence` |
| `shuttle_arrival` | 셔틀 도착 신호 인입 | `arrival_id` · `event_id` · `gate_no` · `shuttle_no` · `notified_robot_count` · `command_id` · `pending` |
| `shuttle_dispatch_result` | 그 신호의 출동 목적지가 확정됐을 때 | `arrival_id` · `event_id` · `gate_no` · `source` · `command` · `destination` · `emergency_stop` · `command_id` · `notified_robot_count` · `decided_at` |
| `alert_ack` | 경고 확인이 실제로 뒤집혔을 때(`POST /api/alerts/{id}/ack`) | `alert_id` · `type` · `severity` · `ack` |
| `alert_identified` | 경고에 신원을 적었거나 지웠을 때 | `alert_id` · `type` · `severity` · `identified` · `identified_at` · `corrected` |
| `alert_status` | 사건 수명주기가 실제로 전진했을 때 | `alert_id` · `type` · `severity` · `status` · `ack` · `resolution` · `resolved_at` |

인입 3계열 메시지에는 `CREDIT_SCOPE_ROOM`을 켤 때만 **`room_id` 칸이 하나 더 붙는다**. 꺼진 기본 상태에서는 위 표 그대로다(dev 계약을 안 흔들려는 임시 가드 — 상시 계약으로 올릴지는 팀 확정 대기). `boot_id`는 DB에만 남고 메시지에는 안 실린다.

읽을 때 걸리는 자리 일곱.

- **`shuttle_arrival`의 `pending`은 대기 창 한 장이고, 확정은 `shuttle_dispatch_result`로 따로 온다.** 창을 열었으면 `arrival_id`·`command_id`·`hold_sec`·`opened_at`·`deadline`·`remain_sec`·`choices`·`default_destination`이 들어 있어 화면이 그 한 장으로 카운트다운까지 그린다. 창을 안 연 갈래(`SHUTTLE_DISPATCH_HOLD_SEC` 0 이하·긴급정지 차단)에서는 `null`이다. 두 메시지는 `arrival_id`·`command_id`로 묶는다.
  ⭐ **`arrival_id`는 2026-08-04부터 `shuttle_arrival` `data` 맨 위에도 실린다.** 그전에는 `pending` 안에만 있어서, 창을 안 연 갈래(`pending`이 `null`)에서는 묶을 값이 통째로 사라졌다. 지금은 창을 열든 안 열든 늘 있고, 값은 셔틀 계열 경고(`shuttle_arrival_expired`·`robot_arrival`·`robot_departure`)의 `source_id`와도 같은 도착 행 PK다.

- **`tag_id`는 그 통과에 붙은 카드 UID다(S15P11C207-241 · F22).** 출처가 둘이라 판정마다 뜻이 다르다 — 정상 통과에서는 **태운 크레딧의 UID**이고, 미태깅·퇴장·판정 보류에서는 **기기가 그 통과에서 읽은 UID**다(2026-08-02부터. 그전에는 이 갈래가 늘 `null`이라 `/api/staff/by-tag`가 구조적으로 404였다). 기기가 UID를 안 실어 보내면 여전히 `null`이고, 지금 라파는 안 싣는다. 칸은 늘 실리니 값이 `null`인지로 갈라 읽어라. ⚠ **"누가 지나갔나"의 증거가 아니다.** 빔은 사람을 못 알아봐서 크레딧은 그 게이트에서 먼저 발급된 순(FIFO)으로 고른다 — 두 사람이 찍은 순서와 지나간 순서가 엇갈리면 적히는 이름도 엇갈린다. 화면 문구도 "이 통과가 태운 카드" 수준으로 적는 게 안전하다.
- **`device_result`는 기기가 낸 1차 판단이라 서버 판정이 아니다.** 라파이가 `POST /api/gate-pass-events` 본문에 옵션으로 실어 보내는 참고값이고(자유 문자열 32자 상한 — 값 낱말은 아직 미정), 안 보내는 기기면 `null`이다. DB엔 안 남고 이 메시지로만 흐른다. 화면이 판정으로 쓸 값은 언제나 `verdict`다.
- **중복 인입은 안 나간다.** 새 행이 실제로 생겼을 때만 밀어내므로 라파이 재전송이 화면에 같은 줄을 두 번 그리지 않는다.
- ⭐ **`untagged_alert`와 `gate_pass_event`는 `pass_id`로 묶는다**(2026-08-04). `gate_pass_event`의 `data.pass_id`가 그 통과가 저장된 행의 DB id이고, 뒤따르는 `untagged_alert`의 `data.source_id`가 **같은 값**이다. 이 둘을 `===`로 맞춰라. 칸은 판정과 무관하게 늘 실리니(정상 통과에도 온다) 값의 유무로 갈라 읽을 필요가 없다.
  ⚠ **도착 순서로 짐작하지 마라.** 예전에는 잇는 칸이 없어서 화면이 "직전 `gate_pass_event`와 같은 건"으로 짐작했는데, 게이트가 늘거나 통과 두 건이 동시에 들어오면 엉뚱한 통과에 경고가 붙는다. 지금 구현이 한 요청 안에서 두 장을 잇달아 보내는 건 계약이 아니라 부산물이다.
  ⚠ **`event_id`로는 못 잇는다.** 그건 라파이가 만든 업무 키라 `untagged_alert`에는 아예 안 실린다. 값 계약은 `tests/test_ws_pass_id_link.py`가 못박는다.
- **경고엔 쿨다운이 있다.** 같은 게이트·같은 종류는 `ALERT_COOLDOWN_SEC`(기본 10초) 안에 한 번만 나간다. 화면이 놓친 게 아니라 서버가 안 보낸 거다.
- **클라이언트가 보내는 프레임은 서버가 무시한다.** 구독 제어 메시지는 아직 없다.

이 표는 인입 3계열과 경고·출동 계열까지다. 로봇에서 나는 `robot_state`·`robot_status`·`robot_arrival`·`robot_departure`·`robot_return_complete`·`robot_weather_blocked`도 같은 소켓으로 오니까 위쪽 "대시보드 WS 메시지 종류" 표를 같이 봐야 한다(그 표가 전 종류 목록이다).

이 표는 `tests/test_dashboard_ws.py`가 그대로 검증한다. 문서와 코드가 갈라지면 시험이 깨진다.

## 아직 아닌 것 (다음 단계·팀 확정 대기)

- 로봇 상태 POST 엔드포인트는 두지 않는다 — 로봇 상태는 outbound WebSocket으로만 받는다(M-4).
- 명령은 셋을 발신한다 — 셔틀 도착 목적지(`cmd_destination`)·충전소 복귀(`return_to_charge`)·긴급정지(`emergency_stop`). 창구는 `POST /api/dispatch/commands/move`·`/return`·`/emergency-stop`이다(`app/dispatch.py`). **계약(`schemas/command.schema.json`)에는 있는데 서버가 아직 안 내는 건 셋이다** — `weather_status`·`control_mode`·`cmd_manual`. 날씨는 지금 목적지 판정의 *입력*일 뿐이고 로봇으로 내려보내는 자리가 없다. 젯슨 숙제 문서가 `weather_status`를 받는 쪽으로 적어 둔 건 계약 기준이라 아직 안 온다.
- UID↔학번 매핑 없음(M-5).
- **WS 정식 인증은 아직 결정 전이다(M-6).** 스위치 셋(`WS_REQUIRE_API_KEY`·`..._ROBOT`·`..._DASHBOARD`)으로 켤 수 있게 코드는 넣었지만 **전부 기본이 꺼짐**이라, 지금은 익명 클라이언트가 `/ws/robot`에 붙어 `robot_state` 프레임 한 장만 보내도 Alert 행 생성·ETA 표본·명령 수신까지 열린다. 켜는 순서와 그 전에 끝나야 할 배선은 위 "WS 핸드셰이크 인증" 절의 롤아웃에 있다. 대시보드 쪽은 **프론트가 키를 실을 수 있게 고쳐지기 전까지 못 켠다**.
  ⭐ **다만 대시보드 채널의 교차 오리진 구독은 2026-08-02에 닫혔다**(`ws_origin_allowed`, 플래그 밖에서 늘 돈다). 남의 팀 페이지가 스트림을 통째로 받던 갈래가 이걸로 막혔고, 남은 건 "브라우저를 안 거친 익명 접속"이다 — 그건 여전히 위 스위치 몫이다.
- ~~`GET /dev/telemetry`와 조회 GET 계열은 인증이 없다~~ · ~~브라우저 쪽 실질 인증이 없다~~ — **2026-08-01에 답이 나왔다(사용자 확정).** 세션 로그인으로 간다. 코드는 다 들어갔고 위 "로그인을 켠 뒤 역할 게이트" 표가 켠 뒤 모습이다. 켠 뒤에는 조회 GET 계열도 텔레메트리도 로그인 뒤로 들어가고, `identified_by`도 자칭이 아니라 세션 실명으로 찍힌다.
  **다만 `AUTH_REQUIRE_LOGIN`은 아직 기본 꺼짐이라, 조회 GET 계열은 지금 배포에서도 익명이다.** 텔레메트리 쪽은 사정이 달라졌다 — 앱 게이트는 여전히 잠자지만 **2026-08-02부터 nginx `auth_basic`이 그 페이지 앞에 걸려 있어서** 익명에게 열려 있지 않다(`deploy/nginx-c207-api.conf`).
  ⭐ **롤아웃 2·3단계는 2026-08-02에 끝났다.** 계정을 운영 DB에 심었고(관리자 셋·팀장급 하나·요원 둘), 켜기 전 실측 게이트도 통과했다 — 로그인 200 · `GET /api/auth/me` 200 · `GET /api/staff` 200 · 팀장급 role 확인 · 로그아웃 204. **남은 건 둘이다** — 프론트 -350~-352 배포(4단계)와 `AUTH_REQUIRE_LOGIN=true`(5단계). 절차는 `deploy/README.md`의 "로그인 켜기(롤아웃)"에 있다.
- ⭐ **관제 보조에는 nginx 밖에 앱 층 상한이 하나 더 있다**(2026-08-02 추가). `/api/assistant/*`는 60초에 60회를 넘기면 앱이 직접 429에 `Retry-After`를 실어 끊는다(`app/routers/assistant.py`의 `_quota_gate`). 눈금 단위는 로그인이 켜지면 사람, 꺼져 있으면 부른 자리(IP)다. ⚠ **이 상한은 `AUTH_REQUIRE_LOGIN`과 무관하게 항상 돈다** — nginx 존만 보고 "앞단만 막혀 있다"고 읽으면 안 된다. GMS 크레딧이 팀 공용이라 앞단 하나로는 부족하다고 봐서 두 겹으로 뒀다.
- **nginx 속도 제한은 쓰기·관제 보조·로그인 갈래에 있다**(2026-08-01 두 갈래, 2026-08-02 로그인 추가). 쓰기(`~ ^/api/alerts/.`)는 10r/s·burst 20, 관제 보조(`~ ^/api/assistant/.`)는 1r/s·burst 5, 로그인(`= /api/auth/login`)은 10r/m·burst 20이고 넘치면 전부 429다. `/ws/`에는 IP당 동시 연결 30(`limit_conn c207_ws`)이 따로 걸린다. **조회 갈래는 여전히 제한이 없다** — 화면이 주기로 부르는 자리라 걸면 관제 화면이 스스로 429를 맞는다. `/docs`·`/redoc`·`/openapi.json`은 제한이 아니라 **404로 끊겨 있고**, `/dev/telemetry`는 `auth_basic` 뒤에 있다. 배포 자리 설정이라 반영은 손 3단계다(`deploy/README.md`의 nginx 절 참고 — 젠킨스 배포가 nginx를 안 건드린다).
- **신원 기록에 보관 기간·자동 파기가 없다.** 지우는 길은 `DELETE /api/alerts/{id}/identify` 하나뿐이고, 안 지우면 `alert` 행과 함께 영구히 남는다(EC2 DB 백업에도 같이 들어간다). "시연이 끝나면 파기한다" 같은 규칙을 정할지, 파기 잡을 만들지는 팀 결정이다.
- **신원 정정 이력이 안 남는다.** `overwrite`로 고쳐 적으면 앞 값은 어디에도 안 남는다(이력 테이블 없음). 흔적은 상위 보고선 카드뿐인데 그 웹훅이 꺼진 배포에서는 흔적이 0이다. 이력 테이블을 새로 팔지는 스키마 결정이라 팀 몫이고, 지금은 "안 보낸 칸은 안 지운다"까지만 막아 뒀다.
- 저장소 루트의 팀 개인 메모 `.md` 6개 중 CRLF로 들어와 있는 건 `eunbin_1.md` 하나다(나머지 다섯은 LF, `git ls-files --eol`로 실측). `.gitattributes`가 없어 줄바꿈은 커밋된 그대로 나가고, 줄바꿈 정규화는 팀 합의 뒤에 한다.
- ETA는 실측 구간 기록만 쓴다 — 기상청 보정과 LLM 안내 문장은 P1이다.
- **며칠치 추이·히트맵을 내는 건 여전히 미태깅 계열 하나다.** 2026-08-01에 버킷 창구 둘(`/api/stats/gate-pass-buckets`·`/api/stats/alert-buckets`)이 붙어 게이트 통과 15분 추이와 경고 심각도 일별 추이는 생겼지만, 이동평균과 요일×시간대 히트맵은 `GET /api/stats/untagged`에만 있다. 그리고 `untagged`가 세는 건 통과 행(`verdict='untagged'`)이라, 쿨다운에 삼켜진 뒤 실제로 몇 번 울렸는지(경고 발생 수)는 이 응답으로 못 본다 — 경고 쪽 추이는 `/api/stats/alert-buckets`로 따로 봐야 하고, 둘을 한 응답에 나란히 싣는 건 팀 확정 대기다. 게이트별 순위표(어느 게이트가 제일 많나)도 지금은 `gate_no`를 바꿔 가며 여러 번 부르는 수밖에 없고, **버킷 창구 둘에는 `gate_no` 필터가 아예 없다**(요구에 없어 안 넣었다 — 필요해지면 `untagged`와 같은 모양으로 붙인다).
- **통계 시간대는 KST 고정이다.** `app/stats.py`의 `STATS_TIMEZONE` 상수라 설정으로 못 바꾼다. 시연 현장이 한 곳이라 이렇게 뒀고, 설정으로 빼면 오타난 지역 이름이 기동 때 안 걸리고 질의 시점에 터진다(파이썬 쪽에 IANA 표가 없어 미리 검증할 방법이 없다).
- 배속 데모에서는 미태깅 경고가 묶일 수 있다 — `ALERT_COOLDOWN_SEC`(기본 10초)이 벽시계라, 시뮬이 시간을 빨리 감으면 시뮬 시간으로 한참 떨어진 통과 두 건이 벽시계로는 쿨다운 한 창에 들어온다.
- 시뮬 `ts`와 서버 `now()`를 섞어 쓰는 자리가 둘 있다 — ETA의 `eta_at`(신호 `signal_ts` + 실측 소요시간)과 미짝 신호 만료 청소다. 지금은 라파이 실기기 기준이라 문제가 없는데, 시뮬이 셔틀 신호를 내기 시작하면 시계 정책을 정해야 한다(월요일 팀 안건). 크레딧 만료 청소·퇴장 회수도 서버 시각을 한쪽 축으로 쓴다 — 이벤트 시각과 이른 쪽(청소)·늦은 쪽(회수)으로 견주니까 배속 인입이 유효 크레딧을 닫거나 만료분을 회수하지는 않는데, 시뮬 시간이 서버 시계를 앞서가면 청소가 그만큼 늦게 돌고 회수는 그만큼 덜 잡는다. 같은 안건에 **offset 없는(naive) 시각 해석**도 묶여 있다 — 인입 스키마가 offset 없는 ISO도 통과시키고, 서버는 그걸 UTC로 보는데(`_as_utc`) DB에 들어간 값은 그 서버의 TZ로 읽혀서 해석이 갈릴 수 있다. 지금 기기·시뮬은 offset을 붙여 보내니까 코드는 안 건드렸고, 월요일에 "인입 시각은 offset 필수"로 정하면 이 갈림이 통째로 없어진다.
- **복귀 보고 없는 연속 재출동은 안 잡는다.** `ARRIVAL_EDGE_ON_RECOMMAND`를 켜면 명령 상관을 근거로 잡히지만, 명령이 브로드캐스트인 한 게이트에 서 있는 로봇의 주기 보고가 가짜 도착이 되는 대가가 붙는다. 그래서 기본은 꺼짐이고, 월요일에 `mission_status` enum에 중간 상태(이동 중)를 넣기로 정하면 값 변화로 깔끔하게 잡힌다.
- ~~**`untagged_alert`와 `gate_pass_event`를 묶을 키가 없다.**~~ — **2026-08-04에 메웠다.** `gate_pass_event` `data`에 통과 행 id `pass_id`가, `shuttle_arrival` `data`에 도착 행 id `arrival_id`가 실린다. 기존 칸은 한 글자도 안 바꾸고 더하기만 했다. 계약은 위 WS 표와 "읽을 때 걸리는 자리"에 있다.
- **로봇 위치·LED·임무 상태는 DB에 안 남는다.** 값 자체는 스냅샷·`/api/robots`·`robot_status` 메시지에 다 실리는데, 출처가 `Robot` 테이블이 아니라 서버 메모리다(워커 1개 전제. 크레딧 쿨다운·미션 에지 추적과 같은 방식). **서버를 재기동하면 세 칸이 `null`로 비고 다음 상태 보고가 다시 채운다** — 의도된 거동이다. 컬럼 추가는 팀 확정 대기이고, 이력 조회(지나온 경로)가 필요해지면 그때 정한다.

## 로컬 실행

```bash
cd backend
python -m venv .venv         # ⚠ 파이썬 3.12로 판다. 3.14면 asyncpg 휠 빌드에서 깨진다
.venv/Scripts/activate      # Windows. bash면 source .venv/Scripts/activate
pip install -r requirements-dev.txt   # 개발·시험용. 서버만 돌릴 거면 requirements.txt
cp .env.example .env         # DATABASE_URL·API_KEY 채우기
alembic upgrade head
uvicorn app.main:app --port 8000
```

**의존성 파일이 둘이다.** `requirements.txt`는 운영 런타임만이고 그 파일이 운영 이미지에 들어간다(`backend/Dockerfile`). 시험 도구(`pytest`·`pytest-asyncio`·`pytest-xdist`·`jsonschema`)는 `requirements-dev.txt`에만 있고, 그 파일이 첫 줄에서 `-r requirements.txt`로 운영 목록을 끌어온다 — 개발 환경은 dev 하나만 설치하면 된다.

⚠ **시험 도구를 `requirements.txt`로 옮기지 마라.** 운영 이미지에 `pytest`가 들어 있으면 `docker exec c207-backend pytest` 한 줄로 운영 DB 스키마가 drop된다. Dockerfile이 빌드 끝에 `pytest`가 없는지 실제로 확인해서, 여기에 얹은 도구가 운영 이미지로 새지 않는다.

## 더미로 관통 확인

```bash
# 터미널 A: 대시보드 WS 수신 대기 (또는 브라우저로 http://127.0.0.1:8000/dev/telemetry)
python -m tools.ws_probe --seconds 25 --expect 5
# 터미널 B: 로봇 붙이기 — 상태 3건 올리고 마지막에 게이트 도착 보고
python -m tools.dummy_robot --states 3 --interval 2 --linger 6 --mission ARRIVED --telemetry
# (주기 보고 재현) 매 보고에 ARRIVED를 실어도 도착 알림은 한 번만 나야 한다
python -m tools.dummy_robot --states 5 --interval 1 --mission ARRIVED --mission-repeat
# 터미널 C: 셔틀 도착 신호 (터미널 B의 로봇이 cmd_destination 명령을 받는다)
# ⚠ 키는 환경변수로 넣는다. --api-key로 주면 같은 호스트의 다른 사용자가 `ps`로 읽는다.
C207_API_KEY=dev-local-key python -m tools.dummy_publisher --scenario shuttle
# 그다음 구간 기록이 쌓였는지 확인
curl 'http://127.0.0.1:8000/api/eta?gate_no=2'
```

## 시험

```bash
pip install -r requirements-dev.txt   # pytest는 여기에만 있다(운영 목록엔 없다)
pytest        # 멱등·healthz·스키마 검증(422)·인증(401)·크레딧 9시나리오·로봇 채널·ETA·미태깅 통계·개발자 페이지·대시보드 스냅샷·WS 메시지
```

테스트 DB는 `TEST_DATABASE_URL`로 지정하고, 없으면 `postgres@127.0.0.1:5433/c207_test`로 붙는다. 매 케이스 앞에 인입·알림·로봇 테이블을 TRUNCATE하고 인메모리 쿨다운·로봇 채널 상태도 같이 리셋한다(`tests/conftest.py`). 인메모리 상태를 새로 들이면 그 리셋 훅에 같이 넣어야 앞 케이스 오염이 다음 케이스로 안 샌다.

통계 시험(`tests/test_stats_untagged.py`·`test_stats_today.py`·`test_stats_alert_response.py`·`test_stats_buckets.py`)은 `app.stats._now_utc`를 monkeypatch로 못박아 기준 시각을 고정한다 — 안 고정하면 구간이 돌리는 날마다 밀려서 경계 검사가 무의미해진다. 시각을 고정한 뒤에는 KST 날짜와 UTC 날짜가 갈라지는 시각(전날 15시 UTC 이후)을 일부러 골라 심어서, 버킷을 UTC로 자르면 시험이 깨지게 해 뒀다. 버킷 시험은 기준 시각을 **눈금에서 7분 지난 자리**(14:07 KST)로 잡는다 — 눈금 정각으로 잡으면 "진행 중인 칸이 마지막 자리에 들어오나"가 우연히 통과한다.

WebSocket 시험은 두 갈래다. DB를 타는 판정·중계는 매니저에 가짜 소켓을 꽂아 세션 이벤트 루프에서 보고(`tests/test_robot_channel.py`), 실 소켓 왕복은 `TestClient.websocket_connect`를 별 스레드로 밀어 DB를 안 타는 경로만 본다(`tests/test_robot_ws.py`). 한 엔진을 두 이벤트 루프가 같이 쓰면 asyncpg가 깨지기 때문이다.

병렬로 돌리려면 `pytest -n auto`(pytest-xdist)를 쓴다. 워커마다 별도 프로세스가 같은 DB에 붙어 스키마를 drop/create하고 매 케이스 앞에 TRUNCATE까지 하면 워커끼리 서로의 행을 지우기 때문에, 워커 안에서는(`PYTEST_XDIST_WORKER` 환경변수가 있을 때) `TEST_DATABASE_URL`의 DB 이름에 워커 접미사를 자동으로 붙이고 없으면 관리 커넥션으로 만든다(`c207_test` → `c207_test_gw0`, `tests/conftest.py`의 `_ensure_worker_database`). 직렬 실행(`pytest` 그대로)에서는 접미사도 DB 생성도 없다.
