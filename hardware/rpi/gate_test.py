#!/usr/bin/env python3
"""
게이트 감지 통합 스크립트 (서버 전송 버전)

  - RFID(RC522, SPI)  : 카드 태깅 감지 -> /api/tagging-events
  - ToF(VL53L1X x2, I2C): 빔 A/B 통과 감지 -> /api/gate-pass-events

.env 파일 또는 환경변수로 서버 주소를 설정할 수 있다.
  SERVER_BASE_URL=http://localhost:8080
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

# ---------------------------------------------------------------- 설정

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEVICE_ID = "raspberry01"
GATE_NO = 5
KST = timezone(timedelta(hours=9))

# 서버
ENV_FILE = ".env"
SERVER_BASE_URL = "http://localhost:8080"
TAGGING_ENDPOINT = "/api/tagging-events"
GATE_PASS_ENDPOINT = "/api/gate-pass-events"
HTTP_TIMEOUT_S = 3.0
SEND_RETRY_S = 1.0

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
TOF_POLL_S = 0.05             # 50ms마다 센서값 읽기
# RFID
RFID_POLL_S = 0.1
TAG_COOLDOWN_S = 1.5          # 같은 카드 연속 인식 방지

# 터치 센서와 상태 LED (GPIO.BOARD 물리 핀 번호)
TOUCH_PIN = 29                 # BCM GPIO5
LED_PIN = 31                   # BCM GPIO6
TOUCH_HOLD_S = 3.0
TOUCH_POLL_S = 0.02
LED_TAG_BLINK_S = 0.3
SOUND_DURATION_S = 0.15
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
# (backend/app/credit/state_machine.py), 이 값은 참고용으로 local_verdict 필드에 실어 보낸다.
TAG_ARM_TIMEOUT_S = 3.0       # 태깅 후 이 시간 안에 빔 B가 없으면 자동으로 빔 A 재활성화
                               # (서버 credit_ttl_sec 기본값과 동일하게 맞춘다)

# ---------------------------------------------------------------- 공용

event_q: "queue.Queue[dict]" = queue.Queue()
send_q: "queue.Queue[dict]" = queue.Queue()
stop_event = threading.Event()
sensing_enabled = threading.Event()  # 시험도 IDLE 상태로 시작
sound_q: "queue.Queue[str]" = queue.Queue(maxsize=1)
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

    def emit(beam_a_ts, beam_b_ts, status, local_verdict=None):
        if not sensing_enabled.is_set():
            return
        if status == "complete":
            direction = "A_TO_B" if beam_a_ts < beam_b_ts else "B_TO_A"
            observed = max(beam_a_ts, beam_b_ts)
            gap_ms = abs(beam_b_ts - beam_a_ts) * 1000
            print(f"[디버그] A-B 간격 = {gap_ms:.0f}ms")
            if local_verdict is None:
                local_verdict = "untagged" if direction == "A_TO_B" else "exit"
            if local_verdict == "untagged":
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
            "processed_at": now_iso(),
            "status": status,
            "local_verdict": local_verdict,
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
            "processed_at": now_iso(),
            "status": "complete",
            "local_verdict": "normal",
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
            GPIO.output(LED_PIN, GPIO.LOW)
            stop_event.wait(LED_TAG_BLINK_S)
            GPIO.output(LED_PIN, GPIO.HIGH if sensing_enabled.is_set() else GPIO.LOW)
            event_q.put({
                "type": "tagging",
                "event_id": next_event_id(),
                "device_id": DEVICE_ID,
                "gate_no": GATE_NO,
                "tag_id": uid_hex,
                "observed_at": ts_iso(now),
            })

        time.sleep(RFID_POLL_S)


# ---------------------------------------------------------------- 터치 센서 / 상태 LED / 스피커

def request_sound(kind: str = "tag"):
    """알림음 재생을 예약한다."""
    try:
        sound_q.put_nowait(kind)
    except queue.Full:
        pass


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


# ---------------------------------------------------------------- 터치 센서 / 상태 LED

def init_touch_and_led():
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(TOUCH_PIN, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(LED_PIN, GPIO.OUT, initial=GPIO.LOW)


def toggle_sensing():
    if sensing_enabled.is_set():
        sensing_enabled.clear()
        tag_gate.disarm()
        GPIO.output(LED_PIN, GPIO.LOW)
        print("[테스트] 센싱 중지 — 실제 복귀 서버 요청은 보내지 않습니다.")
    else:
        sensing_enabled.set()
        GPIO.output(LED_PIN, GPIO.HIGH)
        print("[테스트] ToF/RFID 센싱 시작")


def touch_worker():
    pressed_at = None
    triggered = False
    while not stop_event.is_set():
        now = time.monotonic()
        if GPIO.input(TOUCH_PIN) == GPIO.HIGH:
            if pressed_at is None:
                pressed_at = now
            elif not triggered and now - pressed_at >= TOUCH_HOLD_S:
                triggered = True
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
            if ev.get("local_verdict") == "untagged":
                print(f"[로컬판단] ⚠ 미태깅 통과 의심 (오프라인 대비용, 서버 최종판정 아님) at={ev['observed_at']}")

        print(f"         {ev}")
        event_q.task_done()



# ---------------------------------------------------------------- 메인

def main():
    load_env()
    print(f"서버 주소: {server_base_url()}")
    print("센서 초기화 중...")
    init_touch_and_led()
    sensor_a, sensor_b, _xshut = init_tof()
    print(f"ToF 준비 완료 (빔 A=0x{SENSOR_A_ADDR:02X}, 빔 B=0x{SENSOR_B_ADDR:02X})")

    threads = [
        threading.Thread(target=tof_worker, args=(sensor_a, sensor_b), daemon=True),
        threading.Thread(target=rfid_worker, daemon=True),
        threading.Thread(target=touch_worker, daemon=True),
        threading.Thread(target=sound_worker, daemon=True),
        threading.Thread(target=output_worker, daemon=True),
    ]
    for t in threads:
        t.start()

    print("초기 상태=IDLE. 3초 터치로 센싱을 시작/중지하고 RFID 스피커 알림을 시험합니다. 종료하려면 Ctrl+C\n")

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
