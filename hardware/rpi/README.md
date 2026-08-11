# 라즈베리파이 게이트 클라이언트

프로그램은 IDLE(센싱 중지) 상태로 시작합니다. 첫 번째 3초 터치에서 RFID(RC522)와 ToF(VL53L1X ×2) 센싱을 시작하고, 두 번째 3초 터치에서 센싱을 먼저 중지한 뒤 중앙 서버에 복귀를 요청합니다. 서버가 Jetson에 `return_to_charge` 명령을 전달하므로 라즈베리파이에 ROS 2를 설치할 필요가 없습니다.

## 배선

### RFID — RC522 (SPI0)

| RC522 핀 | BCM GPIO | 물리 핀 |
| --- | --- | --- |
| SCK | GPIO11 | 23번 | - 흰색
| MISO | GPIO9 | 21번 | - 노란색
| MOSI | GPIO10 | 19번 | - 주황색
| SDA (CS) | GPIO8 | 24번 | - 갈색
| RST | GPIO25 | 22번 | - 회색
| VCC | 3.3V | 1번 또는 17번 |
| GND | GND | 공통 GND |

### ToF 거리센서 — VL53L1X ×2 (I2C1, SDA/SCL 공유)

| 신호 | BCM GPIO | 물리 핀 | 비고 |
| --- | --- | --- | --- |
| SDA (공유) | GPIO2 | 3번 | 두 센서 공통 |
| SCL (공유) | GPIO3 | 5번 | 두 센서 공통 |
| XSHUT A | GPIO17 | 11번 | 부팅 후 주소 0x30으로 재설정 |
| XSHUT B | GPIO27 | 13번 | 부팅 후 주소 0x31으로 재설정 |
| VIN | 3.3V | 공통 | 두 센서 공통 |
| GND | GND | 공통 | 두 센서 공통 |

두 센서 모두 공장 기본주소가 0x29라, XSHUT으로 한쪽씩 켜면서 주소를 분리해야 I2C 버스 공유가 가능합니다.

### 터치 센서 / 상태 LED

| 장치 | BCM GPIO | 물리 핀 | 비고 |
| --- | --- | --- | --- |
| 터치 센서 OUT | GPIO5 | 29번 | 터치 시 HIGH 출력 타입 기준 |
| 터치 센서 VCC | 3.3V | - | |
| 터치 센서 GND | GND | 30번 등 | LED GND와 공통 |
| LED (+) | GPIO6 | 31번 | **220~330Ω 저항 직렬 필수** |
| LED (-) | GND | 30번 등 | 터치센서 GND와 공통 |

### 스피커 모듈 (알림음)

- **현재 방식**: 블루투스 스피커 → 라즈베리파이 시스템 기본 오디오 출력으로 페어링/연결, GPIO 배선 없음. `ffplay` 설치 필요 (`sudo apt install ffmpeg`)
- **대체 옵션 (미사용, 코드에 주석 처리)**: 마더보드용 액티브 부저를 GPIO로 직결하는 경우
  - `BUZZER_PIN` → BCM GPIO13 (물리 33번) — `gate_server.py` 주석 예시
  - 5V 부저는 GPIO 직결 대신 **트랜지스터 경유 구동 권장** (전류 초과 방지)

**공통 사항**

- I2C, SPI는 `raspi-config`에서 활성화해야 함
- 3.3V/GND는 여러 장치가 공통으로 물려도 무방 (전류 여유 내에서)
- 물리 핀 번호는 40핀 헤더 기준 BCM↔물리 매핑을 적용한 것

센싱 중 LED는 계속 켜지고 RFID 태깅 시 0.3초 꺼졌다 다시 켜집니다. 센싱을 종료하는 두 번째 3초 터치에서 `POST {SERVER_BASE_URL}/api/dispatch/commands/return`을 호출하며, `.env`의 `API_KEY`가 있으면 `X-API-Key` 헤더로 전송합니다. 성공 로그의 `notified_robot_count`가 1 이상이면 연결된 Jetson에 명령이 전달된 것입니다.

- `gate_server.py` — 감지 + 서버 전송. **실기기에서 돌리는 본체입니다.**
- `gate_test.py` — 전송 없이 콘솔과 실제 GPIO로 IDLE/SENSING 전환, LED, RFID 스피커 알림을 확인하는 실기기 시험본입니다. 두 번째 터치에서도 실제 복귀 요청은 보내지 않습니다. 현재 ToF 튜닝값은 `gate_server.py`와 맞춰 둔 기준값입니다.
- `monitor_tof.py` — ToF 센서 단독 확인용.
- `tests/test_sender_contract.py` — 전송부 계약 시험. 하드웨어 없이 PC에서 돕니다.

서버로 나가는 계약은 `docs/rpi_to_server.md`에 있고, 진짜 계약은 `backend/app/schemas.py`입니다.

---

## 1. 사전 준비 (venv 밖에서 먼저)

```bash
sudo apt update
sudo apt install swig liblgpio-dev python3-dev
```

`raspi-config`에서 I2C와 SPI를 켜 둡니다.

## 2. 설치

```bash
cd hardware/rpi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

라즈베리파이 5에서는 `RPi.GPIO`가 안 돕니다. `rpi-lgpio`가 같은 이름으로 대체하므로 둘이 같이 깔려 있으면 충돌합니다. 이미 있으면 지웁니다.

```bash
pip uninstall RPi.GPIO -y
```

## 3. .env 만들기

```bash
cp .env.example .env
nano .env
```

네 값을 채웁니다. `API_KEY`는 서버 `.env`의 `API_KEY`와 **글자까지 똑같아야** 합니다. 다르면 전 요청이 401입니다. 실제 키는 팀 비밀 저장소에서 받고, `.env`는 커밋하지 않습니다(`.gitignore` 제외 대상).

```
SERVER_BASE_URL=https://robot.example.com
API_KEY=팀-비밀-저장소에서-받은-키
DEVICE_ID=raspberry01
GATE_NO=5
```

## 4. 실행

```bash
cd hardware/rpi          # .env를 실행 폴더에서 읽으므로 이 안에서 실행합니다
source .venv/bin/activate
python3 gate_server.py
```

기동하면 맨 위에 이렇게 찍힙니다.

```
서버 주소: https://robot.example.com
장치 식별: device_id=raspberry01  gate_no=5
인증: X-API-Key 헤더를 실어 보냅니다.
```

`API_KEY`가 비어 있으면 대신 느낌표 줄로 둘러싼 경고가 뜹니다. 그 상태로 두면 모든 전송이 401로 버려집니다.

카드를 대거나 빔을 지나면 콘솔에 `[태깅]` / `[통과]` 줄이 뜨고, 전송 결과가 `[전송] OK 200 ...`으로 따라 붙습니다.

### 부팅 시 자동 실행

라즈베리파이가 켜질 때 `gate_server.py`를 자동으로 실행하려면 `systemd` 서비스를 설치합니다.

```bash
cd hardware/rpi
chmod +x scripts/install_gate_server_service.sh
./scripts/install_gate_server_service.sh
```

서비스는 `/home/c207/Desktop/S15P11C207/hardware/rpi`를 작업 폴더로 사용하고, `.venv/bin/python`으로 `gate_server.py`를 실행합니다. 실행 상태와 로그는 아래 명령으로 확인합니다.

```bash
sudo systemctl status gate-server.service
sudo journalctl -u gate-server.service -f
```

자동 실행을 끄려면 아래처럼 비활성화합니다.

```bash
sudo systemctl disable --now gate-server.service
```

라즈베리파이는 1초마다 서버의 `RPI_COMMAND_POLL_ENDPOINT`를 조회합니다. 서버가 아래처럼 응답하면 명령 타입에 맞는 음원을 블루투스 스피커로 재생합니다.

```json
{"commands":[{"command_id":"cmd-1","type":"bus_arrival"}]}
```

지원하는 도착 안내 명령 타입과 기본 음원 파일명은 아래와 같습니다. `.env`에서 각 경로를 바꿀 수 있습니다.

| 명령 타입 | 기본 음원 파일 | `.env` 변수 |
| --- | --- | --- |
| `tag` | `tag.wav` | `TAG_SOUND_FILE` |
| `bus_arrival` | `bus_arrival.wav` | `BUS_ARRIVAL_SOUND_FILE` |
| `robot_departure` | `robot_departure.wav` | `ROBOT_DEPARTURE_SOUND_FILE` |
| `robot_arrival` | `robot_arrival.wav` | `ROBOT_ARRIVAL_SOUND_FILE` |
| `robot_return_complete` | `robot_return_complete.wav` | `ROBOT_RETURN_COMPLETE_SOUND_FILE` |

로컬에서 폴링 흐름만 확인하려면 가짜 서버를 띄웁니다.

```bash
python3 mock_rpi_command_server.py --port 18080
```

다른 터미널에서 `.env`의 `SERVER_BASE_URL`을 `http://127.0.0.1:18080`로 바꾼 뒤 `gate_server.py` 또는 `test_withouttouch.py`를 실행하면 첫 폴링에서 셔틀 도착 명령을 받고 ACK까지 보냅니다.

ToF 통과 판정은 현재 현장 테스트에서 잘 감지된 값으로 맞춰져 있습니다.

```text
TOF_TIMING_BUDGET_MS = 20
BEAM_THRESHOLD_CM = 20
OPEN_CONFIRM_SAMPLES = 5
STALE_RESET_DELTA_CM = 3.0
MATCH_WINDOW_S = 2
DEBOUNCE_S = 1
TOF_POLL_S = 0.05
```

---

## 5. 전송이 되는지 확인하는 방법

### 서버에 실제로 쌓였는지 (`/api/events`)

인증이 필요 없는 조회 API입니다. 라즈베리파이든 노트북이든 어디서나 됩니다.

```bash
curl -s "https://robot.example.com/api/events?limit=5"
```

방금 지나간 이벤트가 목록 맨 앞에 있으면 인입까지 성공한 것입니다.

### 화면으로 실시간 확인 (`/dev/telemetry`)

브라우저로 `https://robot.example.com/dev/telemetry`를 열어 둡니다. 대시보드 웹소켓 봉투가 그대로 흐르는 개발용 화면입니다. 카드를 대면 `tagging_event` 봉투가, 빔을 지나면 `gate_pass_event` 봉투가 바로 올라옵니다.

기기 1차 판단은 `gate_pass_event` 봉투의 `device_result` 자리에 실립니다. 서버 판정인 `verdict`와는 다른 칸이니 헷갈리지 않게 봅니다. `device_result`가 비어 있으면 본문 필드 이름이 서버 계약(`result`)과 어긋난 것입니다.

### PC에서 전송부만 미리 확인 (하드웨어 불필요)

```bash
python3 hardware/rpi/tests/test_sender_contract.py
```

가짜 하드웨어 모듈을 끼우고 임의 포트에 모의 서버를 띄워서, 헤더·본문 필드·4xx 폐기·5xx 백오프를 확인합니다. 전부 통과하면 종료 코드가 0입니다.

---

## 6. 잘 안 될 때

**401이 뜬다** — `[전송] 버림 401 ... → X-API-Key(.env 의 API_KEY)를 확인해 주세요.` 가 찍힙니다. 401은 다시 보내도 같은 답이라 재시도하지 않고 버립니다.

1. `.env`의 `API_KEY`와 서버 `.env`의 `API_KEY`를 눈으로 대조합니다. 따옴표나 뒤에 붙은 공백이 흔한 원인입니다.
2. `hardware/rpi` 밖에서 실행하면 `.env`를 못 읽어 키가 빈 채로 돕니다. 기동 첫 줄의 `서버 주소`와 `인증` 표시를 확인합니다.
3. 헤더가 실리는지는 이렇게 직접 찔러 봅니다.

```bash
curl -i -X POST "https://robot.example.com/api/tagging-events" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"event_id":"manual-1","device_id":"raspberry01","gate_no":5,
       "tag_id":"A1B2C3","observed_at":"2026-07-29T14:30:10.000+09:00"}'
```

200 `{"stored":true}`가 정상이고, 같은 `event_id`를 또 보내면 200 `{"duplicate":true}`가 옵니다.

**연결이 안 된다** — `[전송] 오류 ... 재시도합니다.` 가 반복되면 서버까지 못 닿는 것입니다. 재시도 간격은 1초에서 시작해 2배씩 늘고 30초에서 멈춥니다.

1. `curl -s https://robot.example.com/healthz` 로 서버가 살아 있는지 봅니다.
2. `SERVER_BASE_URL` 오타와 포트를 봅니다. 로컬 백엔드는 8000이고, 예전 기본값이던 8080이 아닙니다.
3. 라즈베리파이의 네트워크와 DNS를 봅니다(`ping robot.example.com`).

**200은 뜨는데 화면에 기기 판단이 안 보인다** — 서버가 모르는 필드를 조용히 버리기 때문입니다. 콘솔 `[통과]` 줄 밑 본문에 `'result'` 키가 있는지 보고, `/dev/telemetry`의 `device_result`와 맞춰 봅니다.

**센서가 안 읽힌다** — `python3 monitor_tof.py` 로 거리값만 따로 봅니다. I2C·SPI 활성화와 XSHUT 배선(D17/D27)을 확인합니다.

**복귀 요청이 나가는지 확인하고 싶다** — 실제 백엔드 서버를 켜고 두 번째 3초 터치를 합니다. 콘솔에 `[복귀] 서버 요청 완료 command_id=... notified_robot_count=...`가 뜨면 라즈베리파이에서 복귀 API 발행은 성공한 것입니다. 401이면 `API_KEY`, 연결 오류면 `SERVER_BASE_URL`과 네트워크를 확인합니다.
