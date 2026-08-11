#!/usr/bin/env python3
"""
게이트 감지 통합 스크립트 (콘솔 출력 버전)

  - RFID(RC522, SPI)  : 카드 태깅 감지 -> tagging 이벤트
  - ToF(VL53L1X x2, I2C): 빔 A/B 통과 감지 -> gate_pass 이벤트 (방향 판정 포함)

서버 전송은 아직 붙이지 않고, 생성된 이벤트를 콘솔에 출력만 한다.
이벤트 딕셔너리 구조는 서버 스펙(/api/tagging-events, /api/gate-pass-events)과
동일하게 맞춰 두었으므로, 나중에 전송 스레드에서 그대로 POST 하면 된다.

배선
  RC522(SPI0)          VL53L1X x2 (I2C1)
    SCK  -> GPIO11       SDA   -> GPIO2  (공유)
    MISO -> GPIO9        SCL   -> GPIO3  (공유)
    MOSI -> GPIO10       XSHUT A -> GPIO17
    SDA(CS) -> GPIO8     XSHUT B -> GPIO27
    RST  -> GPIO25       VIN -> 3.3V / GND 공통
"""

import queue
import signal
import threading
import time
from datetime import datetime, timezone, timedelta

import board
import busio
import digitalio
import adafruit_vl53l1x

from mfrc522 import SimpleMFRC522
import RPi.GPIO as GPIO  # rpi-lgpio 가 이 이름으로 동작

# ---------------------------------------------------------------- 설정

DEVICE_ID = "raspberry01"
GATE_NO = 5
KST = timezone(timedelta(hours=9))

# ToF
XSHUT_A_PIN = board.D17
XSHUT_B_PIN = board.D27
SENSOR_A_ADDR = 0x30          # 빔 A 주소
SENSOR_B_ADDR = 0x31          # 빔 B 주소
TOF_DISTANCE_MODE = 1         # short mode: 근거리 게이트 감지에 더 안정적
TOF_TIMING_BUDGET_MS = 50
BEAM_THRESHOLD_CM = 10        # 이 거리 이내면 "빔 차단"으로 판정
BLOCK_CONFIRM_SAMPLES = 2     # 연속 N회 차단 거리일 때만 차단 확정
OPEN_CONFIRM_SAMPLES = 2      # 연속 N회 열림 거리일 때만 열림 확정
STALE_BLOCK_S = 1.0           # 차단 거리값이 이 시간 동안 고정되면 stale 로 간주
STALE_RESET_DELTA_CM = 5.0    # stale 중 거리 변화가 이 이상이면 새 감지로 간주
MATCH_WINDOW_S = 0.4          # 동시성 창 400ms
DEBOUNCE_S = 0.2             # 같은 빔 재트리거 최소 간격
AUTO_RELEASE_AFTER_TRIGGER = True
TOF_POLL_S = 0.02             # 폴링 주기 20ms
TOF_MONITOR_S = 1.0           # 센서값 상태 출력 주기

# RFID
RFID_POLL_S = 0.1
TAG_COOLDOWN_S = 1.5          # 같은 카드 연속 인식 방지

# ---------------------------------------------------------------- 공용

event_q: "queue.Queue[dict]" = queue.Queue()
stop_event = threading.Event()
_seq_lock = threading.Lock()
_seq = 0
INVALID_READING = object()


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

    * 반드시 스레드 시작 전에 메인에서 한 번만 호출할 것.
    """
    i2c = busio.I2C(board.SCL, board.SDA)

    xshut_a = digitalio.DigitalInOut(XSHUT_A_PIN)
    xshut_b = digitalio.DigitalInOut(XSHUT_B_PIN)
    for x in (xshut_a, xshut_b):
        x.switch_to_output(value=False)      # 둘 다 끔
    time.sleep(0.05)

    # 1) 빔 A 만 켜서 주소 변경
    xshut_a.value = True
    time.sleep(0.05)
    sensor_a = adafruit_vl53l1x.VL53L1X(i2c, address=0x29)
    sensor_a.set_address(SENSOR_A_ADDR)

    # 2) 빔 B 만 기본 주소에서 잡은 뒤 별도 주소로 변경
    xshut_b.value = True
    time.sleep(0.05)
    sensor_b = adafruit_vl53l1x.VL53L1X(i2c, address=0x29)
    sensor_b.set_address(SENSOR_B_ADDR)

    for s in (sensor_a, sensor_b):
        s.distance_mode = TOF_DISTANCE_MODE
        s.timing_budget = TOF_TIMING_BUDGET_MS
        s.start_ranging()

    # xshut 핸들은 GC 되지 않도록 같이 반환
    return sensor_a, sensor_b, (xshut_a, xshut_b)


# ---------------------------------------------------------------- ToF 스레드

def tof_worker(sensor_a, sensor_b):
    """
    빔 A/B 트리거 순서로 통과 방향을 판정한다.

      1. 한쪽 빔 감지 -> 시각 기록, 400ms 타이머 시작
      2. 창 안에 반대쪽도 감지 -> complete, 시각 선후로 direction 결정
         창 만료                -> incomplete, direction = None
    """
    # 빔별 상태: 현재 차단 여부 / 마지막 트리거 시각
    blocked = {"A": False, "B": False}
    last_trigger = {"A": 0.0, "B": 0.0}
    latest_dist = {"A": None, "B": None}
    sample_state = {
        "A": {"block_count": 0, "open_count": 0, "near_since": None, "stale": False, "stale_dist": None},
        "B": {"block_count": 0, "open_count": 0, "near_since": None, "stale": False, "stale_dist": None},
    }
    last_error_log = {"A": 0.0, "B": 0.0}
    last_monitor = 0.0

    # 매칭 대기 중인 미완성 통과 (없으면 None)
    pending = None   # {"beam": "A"/"B", "ts": float}

    def read(name, sensor):
        """거리(cm), INVALID_READING, 또는 아직 새 데이터가 없으면 None"""
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

    def emit(beam_a_ts, beam_b_ts, status):
        if status == "complete":
            direction = "A_TO_B" if beam_a_ts < beam_b_ts else "B_TO_A"
            observed = max(beam_a_ts, beam_b_ts)
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

    while not stop_event.is_set():
        now = time.time()

        for name, sensor in (("A", sensor_a), ("B", sensor_b)):
            dist = read(name, sensor)
            if dist is None:
                continue

            if dist is INVALID_READING:
                is_blocked = classify_invalid_sample(name)
            else:
                latest_dist[name] = dist
                is_blocked = classify_sample(name, dist, now)

            # 열림 -> 차단으로 바뀌는 순간만 트리거 (엣지 검출 + 디바운스)
            if is_blocked and not blocked[name]:
                if now - last_trigger[name] < DEBOUNCE_S:
                    continue
                last_trigger[name] = now
                blocked[name] = True

                if pending is None:
                    pending = {"beam": name, "ts": now}
                elif pending["beam"] != name:
                    # 반대쪽 빔이 창 안에 들어옴 -> complete
                    if name == "B":
                        emit(pending["ts"], now, "complete")
                    else:
                        emit(now, pending["ts"], "complete")
                    pending = None
                else:
                    # 같은 빔이 또 트리거됨 -> 이전 건은 버리고 갱신
                    pending = {"beam": name, "ts": now}

                if AUTO_RELEASE_AFTER_TRIGGER:
                    release_beam(name)

            elif not is_blocked:
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

        # 창 만료 처리 -> incomplete
        if pending and (now - pending["ts"]) > MATCH_WINDOW_S:
            if pending["beam"] == "A":
                emit(pending["ts"], None, "incomplete")
            else:
                emit(None, pending["ts"], "incomplete")
            pending = None

        time.sleep(TOF_POLL_S)


# ---------------------------------------------------------------- RFID 스레드

def rfid_worker():
    reader = SimpleMFRC522()
    last_uid = None
    last_time = 0.0

    while not stop_event.is_set():
        try:
            uid, _text = reader.read_no_block()
        except Exception as e:
            print(f"[RFID] 읽기 오류: {e}")
            time.sleep(0.5)
            continue

        if uid:
            now = time.time()
            uid_hex = format(uid, "X")

            # 같은 카드를 계속 대고 있을 때 중복 이벤트 방지
            if uid_hex == last_uid and (now - last_time) < TAG_COOLDOWN_S:
                time.sleep(RFID_POLL_S)
                continue

            last_uid, last_time = uid_hex, now
            event_q.put({
                "type": "tagging",
                "event_id": next_event_id(),
                "device_id": DEVICE_ID,
                "gate_no": GATE_NO,
                "tag_id": uid_hex,
                "observed_at": ts_iso(now),
            })

        time.sleep(RFID_POLL_S)


# ---------------------------------------------------------------- 출력 스레드

def output_worker():
    """
    지금은 콘솔 출력만.
    나중에 여기서 requests.post(...) 로 서버 전송하면 된다.
    """
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

        print(f"         {ev}")
        event_q.task_done()


# ---------------------------------------------------------------- 메인

def main():
    print("센서 초기화 중...")
    sensor_a, sensor_b, _xshut = init_tof()
    print(f"ToF 준비 완료 (빔 A=0x{SENSOR_A_ADDR:02X}, 빔 B=0x{SENSOR_B_ADDR:02X})")

    threads = [
        threading.Thread(target=tof_worker, args=(sensor_a, sensor_b), daemon=True),
        threading.Thread(target=rfid_worker, daemon=True),
        threading.Thread(target=output_worker, daemon=True),
    ]
    for t in threads:
        t.start()

    print("감지 시작. 종료하려면 Ctrl+C\n")

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
        # cleanup 은 프로그램 종료 시 딱 한 번만.
        # 중간에 호출하면 ToF 의 XSHUT 설정까지 날아간다.
        GPIO.cleanup()
        print("\n종료했습니다.")


if __name__ == "__main__":
    main()
