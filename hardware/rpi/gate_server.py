#!/usr/bin/env python3
"""
게이트 감지 통합 스크립트 (서버 전송 버전)

  - RFID(RC522, SPI)  : 카드 태깅 감지 -> /api/tagging-events
  - ToF(VL53L1X x2, I2C): 빔 A/B 통과 감지 -> /api/gate-pass-events

.env 파일 또는 환경변수로 접속 정보를 설정한다 (.env.example 참고).
  SERVER_BASE_URL=http://localhost:8000
  API_KEY=...        # 서버 인입 API 공용 키. X-API-Key 헤더로 실어 보낸다.
  DEVICE_ID=raspberry01
  GATE_NO=5
"""

import io
import math
import os
import queue
import shlex
import signal
import struct
import subprocess
import threading
import time
import wave
from datetime import datetime, timezone, timedelta

import requests

import board
import busio
import digitalio
import adafruit_vl53l1x

from mfrc522 import SimpleMFRC522
import RPi.GPIO as GPIO  # rpi-lgpio 가 이 이름으로 동작

from rpi_commands import RpiCommandPoller

# ---------------------------------------------------------------- 설정

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICE_ID = "raspberry01"     # .env 의 DEVICE_ID 로 덮어쓸 수 있다
GATE_NO = 5                   # .env 의 GATE_NO 로 덮어쓸 수 있다
KST = timezone(timedelta(hours=9))

# 서버
ENV_FILE = ".env"
SERVER_BASE_URL = "http://localhost:8000"
TAGGING_ENDPOINT = "/api/tagging-events"
GATE_PASS_ENDPOINT = "/api/gate-pass-events"
RETURN_ENDPOINT = "/api/dispatch/commands/return"
HTTP_TIMEOUT_S = 3.0
API_KEY_HEADER = "X-API-Key"  # 서버 인입 API 공용 인증 헤더 (backend/app/security.py)
SEND_RETRY_S = 1.0            # 재시도 첫 간격. 실패마다 2배로 늘린다
SEND_RETRY_MAX_S = 30.0       # 재시도 간격 상한
COMMAND_POLL_INTERVAL_S = 1.0

# ToF
XSHUT_A_PIN = board.D17
XSHUT_B_PIN = board.D27
SENSOR_A_ADDR = 0x30          # 빔 A 주소
SENSOR_B_ADDR = 0x31          # 빔 B 주소
TOF_DISTANCE_MODE = 1         # short mode: 근거리 게이트 감지에 더 안정적
TOF_TIMING_BUDGET_MS = 20     # 센서값 20ms마다 바뀜
MIN_VALID_DISTANCE_CM = 1.0   # 이 거리 이하는 너무 가까운 이상값으로 보고 무시
BEAM_THRESHOLD_CM = 20        # 이 거리 이내면 "빔 차단"으로 판정
BLOCK_CONFIRM_SAMPLES = 2     # 연속 N회 차단 거리일 때만 차단 확정
OPEN_CONFIRM_SAMPLES = 5      # 연속 N회 열림 거리일 때만 열림 확정
STALE_BLOCK_S = 1.0           # 차단 거리값이 이 시간 동안 고정되면 stale 로 간주
STALE_RESET_DELTA_CM = 3.0    # stale 중 거리 변화가 이 이상이면 새 감지로 간주
MATCH_WINDOW_S = 2            # A/B 빔을 한 통과로 묶는 시간창
DEBOUNCE_S = 1                # 같은 빔 재트리거 최소 간격
AUTO_RELEASE_AFTER_TRIGGER = True
TOF_POLL_S = 0.05             # 폴링 주기 50ms
TOF_MONITOR_S = 1.0           # 센서값 상태 출력 주기

# RFID
RFID_POLL_S = 0.1
TAG_COOLDOWN_S = 1.5          # 같은 카드 연속 인식 방지

# 터치 센서와 LED (물리 핀 번호, GPIO.BOARD 기준) 및 스피커 알림음
#   터치 OUT -> 물리 29번(BCM GPIO5), LED -> 물리 31번(BCM GPIO6)
#   GPIO 부저를 쓸 경우 예: BUZZER_PIN -> 물리 33번(BCM GPIO13)
TOUCH_PIN = 29
LED_PIN = 31
# BUZZER_PIN = 33
TOUCH_HOLD_S = 3.0
TOUCH_POLL_S = 0.02
SOUND_DURATION_S = 0.15
LED_DURATION_S = 0.3
SOUND_FREQUENCY_HZ = 1000
SOUND_COMMAND = "ffplay -nodisp -autoexit -loglevel error -f wav -i -"
FILE_SOUND_COMMAND = "ffplay -nodisp -autoexit -loglevel error"
TAG_SOUND_FILE = "tag.wav"
BUS_ARRIVAL_SOUND_FILE = "bus_arrival.wav"
ROBOT_DEPARTURE_SOUND_FILE = "robot_departure.wav"
ROBOT_ARRIVAL_SOUND_FILE = "robot_arrival.wav"
ROBOT_RETURN_COMPLETE_SOUND_FILE = "robot_return_complete.wav"

# 로컬 1차 판단 (오프라인/네트워크 장애 대비)
# 태깅 지점이 빔 A 위치와 겹쳐 있어, 태깅 직후에는 빔 A를 무시하고 빔 B 확인만 기다린다.
# 그 사이 빔 B가 걸리면 정상 태깅자로 보고 빔 A를 다시 활성화한다. 태깅 없이 빔 A -> 빔 B
# 순서로 통과가 완성되면 미태깅으로 본다. 최종 판정은 여전히 서버 크레딧 상태머신이 내리며
# (backend/app/credit/state_machine.py), 이 값은 참고용으로 result 필드에 실어 보낸다
# (서버 GatePassEventIn.result, 최대 32자. 대시보드 봉투 device_result 로만 흐른다).
TAG_ARM_TIMEOUT_S = 3.0       # 태깅 후 이 시간 안에 빔 B가 없으면 자동으로 빔 A 재활성화
                               # (서버 credit_ttl_sec 기본값과 동일하게 맞춘다)

# ---------------------------------------------------------------- 공용

event_q: "queue.Queue[dict]" = queue.Queue()
send_q: "queue.Queue[dict]" = queue.Queue()
stop_event = threading.Event()
sensing_enabled = threading.Event()  # 시작은 IDLE; 첫 3초 터치에서 켠다
sound_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
led_q: "queue.Queue[None]" = queue.Queue(maxsize=1)
_seq_lock = threading.Lock()
_seq = 0
INVALID_READING = object()


class TagGate:
    """태깅 직후 빔 A를 무시하고 빔 B 확인을 기다리는 상태.

    RFID·ToF 두 스레드가 같이 건드리므로 lock 으로 보호한다. 타임아웃이 지나면
    빔 B가 안 걸렸어도 자동으로 해제한다 (안 그러면 태깅 1회로 이후 미태깅자가
    빔 A 무시 상태를 그대로 이용해 정상 통과로 오판될 수 있다).
    """

    def __init__(self, timeout_s: float):
        self._lock = threading.Lock()
        self._armed_at = None
        self._timeout_s = timeout_s

    def arm(self, now: float) -> None:
        with self._lock:
            self._armed_at = now

    def is_armed(self, now: float) -> bool:
        with self._lock:
            if self._armed_at is None:
                return False
            if now - self._armed_at > self._timeout_s:
                self._armed_at = None
                return False
            return True

    def disarm(self) -> None:
        with self._lock:
            self._armed_at = None


tag_gate = TagGate(TAG_ARM_TIMEOUT_S)


def load_env(path=ENV_FILE):
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def server_base_url() -> str:
    return os.environ.get("SERVER_BASE_URL", SERVER_BASE_URL).rstrip("/")


def api_key() -> str:
    """서버 인입 API 공용 키. 코드에 하드코딩하지 않고 .env/환경변수에서만 읽는다."""
    return os.environ.get("API_KEY", "").strip()


def apply_env_overrides() -> None:
    """장치 식별값을 .env 로 덮어쓴다. load_env() 뒤에 한 번 부른다."""
    global DEVICE_ID, GATE_NO

    device = os.environ.get("DEVICE_ID", "").strip()
    if device:
        DEVICE_ID = device

    gate_no = os.environ.get("GATE_NO", "").strip()
    if gate_no:
        try:
            GATE_NO = int(gate_no)
        except ValueError:
            print(f"[설정] GATE_NO 값이 정수가 아닙니다: {gate_no!r}. 기본값 {GATE_NO}을(를) 씁니다.")


def now_iso() -> str:
    """timestamptz 형식 (예: 2026-07-22T14:30:12.540+09:00)"""
    return datetime.now(KST).isoformat(timespec="milliseconds")


def ts_iso(t: float) -> str:
    """time.time() 값을 timestamptz 문자열로"""
    return datetime.fromtimestamp(t, KST).isoformat(timespec="milliseconds")


def next_event_id() -> str:
    """멱등 키: {device_id}-{unix_ms}-{seq}"""
    global _seq
    with _seq_lock:
        _seq = (_seq + 1) % 10000
        return f"{DEVICE_ID}-{int(time.time() * 1000)}-{_seq:04d}"


# ---------------------------------------------------------------- ToF 초기화

def init_tof():
    """
    두 센서 모두 XSHUT 로 끈 뒤 하나씩 켜면서 주소를 분리한다.
    (VL53L1X 는 공장 기본 주소가 둘 다 0x29 라 그대로 두면 충돌)
    """
    i2c = busio.I2C(board.SCL, board.SDA)

    xshut_a = digitalio.DigitalInOut(XSHUT_A_PIN)
    xshut_b = digitalio.DigitalInOut(XSHUT_B_PIN)
    for x in (xshut_a, xshut_b):
        x.switch_to_output(value=False)
    time.sleep(0.05)

    xshut_a.value = True
    time.sleep(0.05)
    sensor_a = adafruit_vl53l1x.VL53L1X(i2c, address=0x29)
    sensor_a.set_address(SENSOR_A_ADDR)

    xshut_b.value = True
    time.sleep(0.05)
    sensor_b = adafruit_vl53l1x.VL53L1X(i2c, address=0x29)
    sensor_b.set_address(SENSOR_B_ADDR)

    for s in (sensor_a, sensor_b):
        s.distance_mode = TOF_DISTANCE_MODE
        s.timing_budget = TOF_TIMING_BUDGET_MS
        s.start_ranging()

    return sensor_a, sensor_b, (xshut_a, xshut_b)


# ---------------------------------------------------------------- ToF 스레드

def tof_worker(sensor_a, sensor_b):
    blocked = {"A": False, "B": False}
    last_trigger = {"A": 0.0, "B": 0.0}
    latest_dist = {"A": None, "B": None}
    sample_state = {
        "A": {"block_count": 0, "open_count": 0, "near_since": None, "stale": False, "stale_dist": None},
        "B": {"block_count": 0, "open_count": 0, "near_since": None, "stale": False, "stale_dist": None},
    }
    last_error_log = {"A": 0.0, "B": 0.0}
    last_monitor = 0.0
    pending = None

    def read(name, sensor):
        try:
            if sensor.data_ready:
                range_status = getattr(sensor, "range_status", 0)
                d = sensor.distance
                sensor.clear_interrupt()
                if range_status not in (0, None):
                    return INVALID_READING
                return d
        except OSError as e:
            now = time.time()
            if now - last_error_log[name] > 1.0:
                print(f"[ToF] 빔 {name} 읽기 오류: {e}")
                last_error_log[name] = now
            return INVALID_READING
        return None

    def emit(beam_a_ts, beam_b_ts, status, result=None):
        if not sensing_enabled.is_set():
            return
        if status == "complete":
            direction = "A_TO_B" if beam_a_ts < beam_b_ts else "B_TO_A"
            observed = max(beam_a_ts, beam_b_ts)
            gap_ms = abs(beam_b_ts - beam_a_ts) * 1000
            print(f"[디버그] A-B 간격 = {gap_ms:.0f}ms")
            if result is None:
                result = "untagged" if direction == "A_TO_B" else "exit"
            if result == "untagged":
                request_sound("untagged")
        else:
            direction = None
            observed = beam_a_ts if beam_a_ts else beam_b_ts

        event_q.put({
            "type": "gate_pass",
            "event_id": next_event_id(),
            "device_id": DEVICE_ID,
            "gate_no": GATE_NO,
            "direction": direction,
            "beam_a_ts": ts_iso(beam_a_ts) if beam_a_ts else None,
            "beam_b_ts": ts_iso(beam_b_ts) if beam_b_ts else None,
            "observed_at": ts_iso(observed),
            "status": status,
            "result": result,
        })

    def emit_tagged_entry(beam_b_ts):
        """태깅 후 빔 B 확인으로 확정되는 정상 입장. 빔 A는 무시했으므로 기록하지 않는다."""
        if not sensing_enabled.is_set():
            return
        event_q.put({
            "type": "gate_pass",
            "event_id": next_event_id(),
            "device_id": DEVICE_ID,
            "gate_no": GATE_NO,
            "direction": "A_TO_B",
            "beam_a_ts": None,
            "beam_b_ts": ts_iso(beam_b_ts),
            "observed_at": ts_iso(beam_b_ts),
            "status": "complete",
            "result": "normal",
        })

    def classify_sample(name, dist, now):
        state = sample_state[name]
        raw_blocked = dist < BEAM_THRESHOLD_CM

        if raw_blocked:
            if state["stale"]:
                stale_dist = state["stale_dist"]
                if stale_dist is not None and abs(dist - stale_dist) >= STALE_RESET_DELTA_CM:
                    state["near_since"] = now
                    state["stale"] = False
                    state["stale_dist"] = None
            elif state["near_since"] is None:
                state["near_since"] = now
                state["stale"] = False
            elif (now - state["near_since"]) > STALE_BLOCK_S:
                state["stale"] = True
                state["stale_dist"] = dist
        else:
            state["near_since"] = None
            state["stale"] = False
            state["stale_dist"] = None

        if raw_blocked and not state["stale"]:
            state["block_count"] += 1
            state["open_count"] = 0
        else:
            state["open_count"] += 1
            state["block_count"] = 0

        if state["block_count"] >= BLOCK_CONFIRM_SAMPLES:
            return True
        if state["open_count"] >= OPEN_CONFIRM_SAMPLES:
            return False
        return blocked[name]

    def classify_invalid_sample(name):
        state = sample_state[name]
        state["near_since"] = None
        state["stale"] = False
        state["stale_dist"] = None
        state["open_count"] += 1
        state["block_count"] = 0
        if state["open_count"] >= OPEN_CONFIRM_SAMPLES:
            if blocked[name] and state["open_count"] == OPEN_CONFIRM_SAMPLES:
                print(f"[ToF] 빔 {name} 경고: 연속 인식 오류로 차단 해제됨 (너무 가까울 수 있음)")
            return False
        return blocked[name]

    def release_beam(name):
        state = sample_state[name]
        blocked[name] = False
        state["block_count"] = 0
        state["open_count"] = 0
        state["near_since"] = None
        state["stale"] = False
        state["stale_dist"] = None

    sensing_was_enabled = False
    while not stop_event.is_set():
        if not sensing_enabled.is_set():
            if sensing_was_enabled:
                for beam_name in ("A", "B"):
                    release_beam(beam_name)
                pending = None
                tag_gate.disarm()
                sensing_was_enabled = False
            stop_event.wait(TOF_POLL_S)
            continue

        if not sensing_was_enabled:
            for beam_name in ("A", "B"):
                release_beam(beam_name)
                last_trigger[beam_name] = 0.0
            pending = None
            tag_gate.disarm()
            sensing_was_enabled = True

        now = time.time()

        for name, sensor in (("A", sensor_a), ("B", sensor_b)):
            if name == "A" and tag_gate.is_armed(now):
                continue  # 태깅 직후에는 빔 A를 읽지 않는다 (태깅 동작이 빔 A를 건드림)

            dist = read(name, sensor)
            if dist is None:
                continue

            if dist is INVALID_READING:
                is_blocked = classify_invalid_sample(name)
            elif dist <= MIN_VALID_DISTANCE_CM:
                is_blocked = classify_invalid_sample(name)
            else:
                latest_dist[name] = dist
                is_blocked = classify_sample(name, dist, now)

            if is_blocked and not blocked[name]:
                if now - last_trigger[name] < DEBOUNCE_S:
                    continue
                last_trigger[name] = now
                blocked[name] = True
                print(f"[ToF] 빔 {name} 차단 dist={latest_dist[name]:.1f}cm at={ts_iso(now)}")

                if name == "B" and tag_gate.is_armed(now):
                    # 태깅 후 첫 빔 B 확인 -> 정상 태깅 입장, 빔 A 재활성화
                    tag_gate.disarm()
                    pending = None
                    emit_tagged_entry(now)
                    if AUTO_RELEASE_AFTER_TRIGGER:
                        release_beam(name)
                elif pending is None:
                    # 짝이 될 반대편 빔이 걸리거나 타임아웃될 때까지는 릴리즈하지 않고 붙잡아둔다.
                    pending = {"beam": name, "ts": now}
                elif pending["beam"] != name:
                    other = pending["beam"]
                    if name == "B":
                        emit(pending["ts"], now, "complete")
                    else:
                        emit(now, pending["ts"], "complete")
                    pending = None
                    if AUTO_RELEASE_AFTER_TRIGGER:
                        release_beam(other)
                        release_beam(name)
                else:
                    pending = {"beam": name, "ts": now}

            elif not is_blocked:
                if blocked[name]:
                    dist = latest_dist[name]
                    dist_str = f"{dist:.1f}cm" if dist is not None else "----"
                    print(f"[ToF] 빔 {name} 열림 dist={dist_str} at={ts_iso(now)}")
                blocked[name] = False

        if now - last_monitor > TOF_MONITOR_S:
            def fmt(name):
                dist = latest_dist[name]
                if dist is None:
                    return f"{name}=----"
                state = "BLOCK" if blocked[name] else "open"
                stale_dist = sample_state[name]["stale_dist"]
                stale = f"/stale@{stale_dist:.1f}" if stale_dist is not None else ""
                return f"{name}={dist:5.1f}cm/{state}{stale}"

            pending_beam = pending["beam"] if pending else "-"
            print(f"[ToF] {fmt('A')}  {fmt('B')}  pending={pending_beam}")
            last_monitor = now

        if pending and (now - pending["ts"]) > MATCH_WINDOW_S:
            if pending["beam"] == "A":
                emit(pending["ts"], None, "incomplete")
            else:
                emit(None, pending["ts"], "incomplete")
            if AUTO_RELEASE_AFTER_TRIGGER:
                release_beam(pending["beam"])
            pending = None

        time.sleep(TOF_POLL_S)


# ---------------------------------------------------------------- RFID 스레드

def rfid_worker():
    reader = SimpleMFRC522()
    last_uid = None
    last_time = 0.0
    sensing_was_enabled = False

    while not stop_event.is_set():
        if not sensing_enabled.is_set():
            if sensing_was_enabled:
                last_uid = None
                last_time = 0.0
                sensing_was_enabled = False
            stop_event.wait(RFID_POLL_S)
            continue
        sensing_was_enabled = True

        try:
            uid, _text = reader.read_no_block()
        except Exception as e:
            print(f"[RFID] 읽기 오류: {e}")
            time.sleep(0.5)
            continue

        if uid and sensing_enabled.is_set():
            now = time.time()
            uid_hex = format(uid, "X")

            if uid_hex == last_uid and (now - last_time) < TAG_COOLDOWN_S:
                time.sleep(RFID_POLL_S)
                continue

            last_uid, last_time = uid_hex, now
            tag_gate.arm(now)
            request_sound("tag")
            event_q.put({
                "type": "tagging",
                "event_id": next_event_id(),
                "device_id": DEVICE_ID,
                "gate_no": GATE_NO,
                "tag_id": uid_hex,
                "observed_at": ts_iso(now),
            })

        time.sleep(RFID_POLL_S)


# ---------------------------------------------------------------- 터치 센서 / 스피커

def init_touch_and_led():
    """mfrc522가 사용하는 BOARD 번호 체계와 동일하게 터치와 LED를 준비한다."""
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(TOUCH_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)
    # 부저 연결 후 GPIO로 직접 삐 소리를 낼 때 주석 해제:
    # GPIO.setup(BUZZER_PIN, GPIO.OUT, initial=GPIO.LOW)


def request_sound(kind: str = "tag"):
    """스피커와 LED 알림을 예약한다. 대기 중인 중복 요청은 버린다."""
    for feedback_q, value in ((sound_q, kind), (led_q, None)):
        try:
            feedback_q.put_nowait(value)
        except queue.Full:
            pass


def led_worker():
    """센싱 중 켜진 LED를 RFID 태깅 시 잠깐 껐다 켜서 피드백을 준다."""
    while not stop_event.is_set():
        try:
            led_q.get(timeout=0.3)
        except queue.Empty:
            continue
        try:
            GPIO.output(LED_PIN, GPIO.LOW)
            stop_event.wait(LED_DURATION_S)
        finally:
            GPIO.output(LED_PIN, GPIO.HIGH if sensing_enabled.is_set() else GPIO.LOW)
            led_q.task_done()


def make_tone_wav(pattern: list[tuple[float, float]]) -> bytes:
    """외부 음원 파일 없이 재생할 짧은 WAV 데이터를 만든다."""
    sample_rate = 44100
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for frequency, duration_s in pattern:
            frame_count = int(sample_rate * duration_s)
            for index in range(frame_count):
                fade = min(1.0, index / 200, (frame_count - index) / 200)
                if frequency <= 0:
                    sample = 0
                else:
                    sample = int(12000 * fade * math.sin(2 * math.pi * frequency * index / sample_rate))
                frames.extend(struct.pack("<h", sample))
        wav.writeframes(frames)
    return output.getvalue()


def make_siren_wav(duration_s: float = 1.0) -> bytes:
    """미태깅 경고용 사이렌 WAV 데이터를 만든다."""
    sample_rate = 44100
    frame_count = int(sample_rate * duration_s)
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        phase = 0.0
        for index in range(frame_count):
            t = index / sample_rate
            sweep = (math.sin(2 * math.pi * 3.0 * t) + 1.0) / 2.0
            frequency = 650 + 750 * sweep
            phase += 2 * math.pi * frequency / sample_rate
            fade = min(1.0, index / 800, (frame_count - index) / 800)
            sample = int(15000 * fade * math.sin(phase))
            frames.extend(struct.pack("<h", sample))
        wav.writeframes(frames)
    return output.getvalue()


def tag_sound_wav() -> bytes:
    return make_tone_wav([(SOUND_FREQUENCY_HZ, SOUND_DURATION_S)])


def untagged_sound_wav() -> bytes:
    return make_siren_wav()


# GPIO active buzzer를 쓸 때는 아래 함수를 주석 해제하고,
# sound_worker() 안의 스피커 재생 대신 buzz_once()를 호출하면 된다.
#
# def buzz_once():
#     GPIO.output(BUZZER_PIN, GPIO.HIGH)
#     stop_event.wait(SOUND_DURATION_S)
#     GPIO.output(BUZZER_PIN, GPIO.LOW)
#
#
def sound_command() -> list[str]:
    return shlex.split(os.environ.get("SOUND_COMMAND", SOUND_COMMAND).strip())


def file_sound_command() -> list[str]:
    return shlex.split(os.environ.get("FILE_SOUND_COMMAND", FILE_SOUND_COMMAND).strip())


def resolve_sound_path(path: str) -> str:
    if not path or os.path.isabs(path):
        return path
    return os.path.join(BASE_DIR, path)


def sound_file_for(kind: str) -> str:
    sound_files = {
        "tag": os.environ.get("TAG_SOUND_FILE", TAG_SOUND_FILE),
        "bus_arrival": os.environ.get("BUS_ARRIVAL_SOUND_FILE", BUS_ARRIVAL_SOUND_FILE),
        "robot_departure": os.environ.get("ROBOT_DEPARTURE_SOUND_FILE", ROBOT_DEPARTURE_SOUND_FILE),
        "robot_arrival": os.environ.get("ROBOT_ARRIVAL_SOUND_FILE", ROBOT_ARRIVAL_SOUND_FILE),
        "robot_return_complete": os.environ.get(
            "ROBOT_RETURN_COMPLETE_SOUND_FILE",
            ROBOT_RETURN_COMPLETE_SOUND_FILE,
        ),
    }
    return resolve_sound_path(sound_files.get(kind, "").strip())


def sound_worker():
    """기본 오디오 출력 장치로 알림음을 재생한다."""
    sounds = {
        "tag": tag_sound_wav(),
        "untagged": untagged_sound_wav(),
    }
    while not stop_event.is_set():
        try:
            kind = sound_q.get(timeout=0.3)
        except queue.Empty:
            continue
        try:
            # GPIO active buzzer를 쓸 때는 아래 줄을 주석 해제하고,
            # 그 아래 sound command 블록은 주석 처리한다.
            # buzz_once()
            path = sound_file_for(kind)
            if path:
                if not os.path.exists(path):
                    print(f"[스피커] 음원 파일을 찾지 못했습니다({kind}): {path!r}")
                else:
                    command = file_sound_command()
                    if command:
                        result = subprocess.run(command + [path], capture_output=True, timeout=10, check=False)
                        if result.returncode != 0:
                            detail = result.stderr.decode(errors="replace").strip()
                            print(f"[스피커] 음원 재생 실패({kind}, {result.returncode}): {detail}")
                    continue

            command = sound_command()
            if command:
                wav_data = sounds.get(kind, sounds["tag"])
                result = subprocess.run(command, input=wav_data, capture_output=True, timeout=3, check=False)
                if result.returncode != 0:
                    detail = result.stderr.decode(errors="replace").strip()
                    print(f"[스피커] 재생 실패({result.returncode}): {detail}")
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[스피커] 재생 오류: {e}")
        finally:
            sound_q.task_done()


def call_return_service():
    """중앙 서버에 복귀를 요청한다. 서버가 Jetson에 return_to_charge를 전달한다."""
    url = server_base_url() + RETURN_ENDPOINT
    headers = {}
    key = api_key()
    if key:
        headers[API_KEY_HEADER] = key
    try:
        response = requests.post(url, headers=headers, timeout=HTTP_TIMEOUT_S)
    except requests.RequestException as e:
        print(f"[복귀] 서버 요청 오류: {e}")
        return

    if 200 <= response.status_code < 300:
        try:
            result = response.json()
        except ValueError:
            result = {}
        command_id = result.get("command_id", "-")
        robot_count = result.get("notified_robot_count", 0)
        print(f"[복귀] 서버 요청 완료 command_id={command_id} notified_robot_count={robot_count}")
    else:
        print(f"[복귀] 서버 요청 실패({response.status_code}): {response.text[:200]}")


def toggle_sensing():
    """터치 확정 1회를 처리한다. 시작하거나, 중지한 뒤 서버에 복귀를 요청한다."""
    if sensing_enabled.is_set():
        sensing_enabled.clear()
        tag_gate.disarm()
        GPIO.output(LED_PIN, GPIO.LOW)
        print("[터치] 센싱 중지 — 복귀를 요청합니다.")
        call_return_service()
    else:
        sensing_enabled.set()
        GPIO.output(LED_PIN, GPIO.HIGH)
        print("[터치] ToF/RFID 센싱을 시작합니다.")


def touch_worker():
    """첫 3초 터치는 센싱 시작, 다음 3초 터치는 센싱 중지 후 복귀 요청."""
    pressed_at = None
    triggered = False
    while not stop_event.is_set():
        now = time.monotonic()
        if GPIO.input(TOUCH_PIN) == GPIO.HIGH:
            if pressed_at is None:
                pressed_at = now
            elif not triggered and now - pressed_at >= TOUCH_HOLD_S:
                triggered = True
                print(f"[터치] {TOUCH_HOLD_S:.1f}초 길게 누름 감지")
                toggle_sensing()
        else:
            pressed_at = None
            triggered = False
        stop_event.wait(TOUCH_POLL_S)


# ---------------------------------------------------------------- 서버 전송 스레드

def endpoint_for(ev):
    if ev["type"] == "tagging":
        return TAGGING_ENDPOINT
    if ev["type"] == "gate_pass":
        return GATE_PASS_ENDPOINT
    raise ValueError(f"unknown event type: {ev['type']}")


def output_worker():
    while not stop_event.is_set():
        try:
            ev = event_q.get(timeout=0.3)
        except queue.Empty:
            continue

        if ev["type"] == "tagging":
            print(f"[태깅]   tag_id={ev['tag_id']}  at={ev['observed_at']}")
        else:
            arrow = {"A_TO_B": "입장", "B_TO_A": "퇴장"}.get(ev["direction"], "불완전")
            print(f"[통과]   {arrow:4s} status={ev['status']:10s} at={ev['observed_at']}")
            if ev.get("result") == "untagged":
                print(f"[로컬판단] ⚠ 미태깅 통과 의심 (오프라인 대비용, 서버 최종판정 아님) at={ev['observed_at']}")

        print(f"         {ev}")
        send_q.put(ev)
        event_q.task_done()


def send_payload(ev):
    """전송 본문을 만든다. type 은 엔드포인트를 고르는 내부 키라 본문에서 뺀다."""
    return {k: v for k, v in ev.items() if k != "type"}


def sender_worker():
    session = requests.Session()
    key = api_key()
    if key:
        session.headers[API_KEY_HEADER] = key
    base_url = server_base_url()

    while not stop_event.is_set():
        try:
            ev = send_q.get(timeout=0.3)
        except queue.Empty:
            continue

        url = base_url + endpoint_for(ev)
        payload = send_payload(ev)
        delay = SEND_RETRY_S

        while not stop_event.is_set():
            try:
                resp = session.post(url, json=payload, timeout=HTTP_TIMEOUT_S)
                code = resp.status_code
                if 200 <= code < 300:
                    print(f"[전송]   OK {code} {ev['type']} event_id={ev['event_id']}")
                    break
                if 400 <= code < 500:
                    # 요청이 잘못된 것이라 다시 보내도 같은 답이 옵니다. 경고만 남기고 버립니다.
                    # (전송 스레드가 하나라 401 하나를 무한 재시도하면 뒤 이벤트가 전부 막힙니다.)
                    hint = f"  → {API_KEY_HEADER}(.env 의 API_KEY)를 확인해 주세요." if code in (401, 403) else ""
                    print(f"[전송]   버림 {code} {ev['type']} event_id={ev['event_id']}"
                          f" body={resp.text[:200]}{hint}")
                    break
                print(f"[전송]   실패 {code} {ev['type']} event_id={ev['event_id']}"
                      f" body={resp.text[:200]} — {delay:.1f}초 뒤 재시도합니다.")
            except requests.RequestException as e:
                print(f"[전송]   오류 {ev['type']} event_id={ev['event_id']}: {e}"
                      f" — {delay:.1f}초 뒤 재시도합니다.")

            if stop_event.wait(delay):
                break
            delay = min(delay * 2, SEND_RETRY_MAX_S)

        send_q.task_done()


# ---------------------------------------------------------------- 메인

def main():
    load_env()
    apply_env_overrides()
    print(f"서버 주소: {server_base_url()}")
    print(f"장치 식별: device_id={DEVICE_ID}  gate_no={GATE_NO}")
    if api_key():
        print(f"인증: {API_KEY_HEADER} 헤더를 실어 보냅니다.")
    else:
        line = "!" * 68
        print(line)
        print("[경고] API_KEY가 비어 있습니다.")
        print("       서버 인입 API는 X-API-Key 헤더를 요구하므로 모든 전송이 401로 거절됩니다.")
        print(f"       {ENV_FILE} 파일에 API_KEY=... 를 넣고 다시 실행해 주세요 (.env.example 참고).")
        print(line)
    print("센서 초기화 중...")
    init_touch_and_led()
    print(f"터치 센서/LED 준비 완료 (물리 핀 {TOUCH_PIN}/{LED_PIN}), 초기 상태=IDLE")
    sensor_a, sensor_b, _xshut = init_tof()
    print(f"ToF 준비 완료 (빔 A=0x{SENSOR_A_ADDR:02X}, 빔 B=0x{SENSOR_B_ADDR:02X})")

    threads = [
        threading.Thread(target=tof_worker, args=(sensor_a, sensor_b), daemon=True),
        threading.Thread(target=rfid_worker, daemon=True),
        threading.Thread(target=touch_worker, daemon=True),
        threading.Thread(target=sound_worker, daemon=True),
        threading.Thread(target=led_worker, daemon=True),
        threading.Thread(target=output_worker, daemon=True),
        threading.Thread(target=sender_worker, daemon=True),
        threading.Thread(
            target=RpiCommandPoller(
                base_url=server_base_url(),
                device_id=DEVICE_ID,
                gate_no=GATE_NO,
                api_key=api_key(),
                stop_event=stop_event,
                on_arrival_sound=lambda kind: request_sound(kind),
                timeout_s=HTTP_TIMEOUT_S,
                poll_interval_s=COMMAND_POLL_INTERVAL_S,
            ).run,
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()

    print("3초 터치로 ToF/RFID 센싱을 시작합니다. 종료하려면 Ctrl+C\n")

    def handle_sigint(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        while not stop_event.is_set():
            time.sleep(0.2)
    finally:
        stop_event.set()
        for t in threads:
            t.join(timeout=1.0)
        GPIO.output(LED_PIN, GPIO.LOW)
        GPIO.cleanup()
        print("\n종료했습니다.")


if __name__ == "__main__":
    main()
