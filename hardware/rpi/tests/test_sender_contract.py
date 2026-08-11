#!/usr/bin/env python3
"""gate_server.py 의 전송부 계약 시험 (하드웨어 없이 PC 에서 돈다).

gate_server 는 최상단에서 board/mfrc522/RPi.GPIO 를 import 하므로 PC 에서는 그대로 못 읽는다.
그래서 sys.modules 에 가짜 모듈을 먼저 끼우고 import 한 뒤, sender_worker 만 떼어 돌린다.
받는 쪽은 이 프로세스 안에서 띄우는 아주 작은 모의 서버다. 포트는 0(임의 빈 포트)으로 열고
finally 에서 shutdown 하므로 고아 프로세스가 포트를 무는 일이 없다.

실행:
    python3 hardware/rpi/tests/test_sender_contract.py
"""

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

# --- 하드웨어 모듈 스텁 (gate_server import 보다 먼저) -----------------------
for _name in ("board", "busio", "digitalio", "adafruit_vl53l1x", "mfrc522", "RPi", "RPi.GPIO"):
    sys.modules.setdefault(_name, MagicMock())

TEST_API_KEY = "test-key-not-a-real-secret"
os.environ["API_KEY"] = TEST_API_KEY

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gate_server  # noqa: E402


# --- 모의 서버 --------------------------------------------------------------

class MockState:
    def __init__(self):
        self.requests = []          # (path, headers, body) 기록
        self.codes = []             # 순서대로 돌려줄 상태 코드
        self.lock = threading.Lock()

    def next_code(self):
        with self.lock:
            return self.codes.pop(0) if self.codes else 200


STATE = MockState()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        with STATE.lock:
            STATE.requests.append({
                "path": self.path,
                "api_key": self.headers.get("X-API-Key"),
                "body": json.loads(raw.decode("utf-8")) if raw else {},
                "at": time.time(),
            })
        code = STATE.next_code()
        response_body = ({"command_id": "return-test-1", "notified_robot_count": 1}
                         if self.path == "/api/dispatch/commands/return"
                         else {"stored": 200 <= code < 300})
        payload = json.dumps(response_body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass  # 시험 출력이 지저분해지지 않게 접근 로그를 끈다


# --- 시험 도구 --------------------------------------------------------------

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {name}" + (f" - {detail}" if detail else ""))


def tagging_event(event_id):
    return {
        "type": "tagging",
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 5,
        "tag_id": "A1B2C3",
        "observed_at": "2026-07-29T14:30:10.000+09:00",
    }


def gate_pass_event(event_id, result="untagged"):
    return {
        "type": "gate_pass",
        "event_id": event_id,
        "device_id": "raspberry01",
        "gate_no": 5,
        "direction": "A_TO_B",
        "beam_a_ts": "2026-07-29T14:30:12.100+09:00",
        "beam_b_ts": "2026-07-29T14:30:12.540+09:00",
        "observed_at": "2026-07-29T14:30:12.540+09:00",
        "status": "complete",
        "result": result,
    }


class FakeSensor:
    """ToF 센서 흉내. 미리 적어 둔 거리값을 순서대로 돌려주고 끝나면 마지막 값을 반복한다."""

    def __init__(self, distances):
        self._distances = list(distances)
        self._last = self._distances[-1]
        self.data_ready = True
        self.range_status = 0

    @property
    def distance(self):
        if self._distances:
            self._last = self._distances.pop(0)
        return self._last

    def clear_interrupt(self):
        pass


def drain(timeout=8.0):
    """send_q 가 빌 때까지 기다린다."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gate_server.send_q.unfinished_tasks == 0:
            return True
        time.sleep(0.05)
    return False


def reset():
    with STATE.lock:
        STATE.requests.clear()
        STATE.codes.clear()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    os.environ["SERVER_BASE_URL"] = f"http://127.0.0.1:{port}"
    gate_server.SEND_RETRY_S = 0.2      # 시험을 빨리 돌리려고 백오프 첫 간격만 줄인다
    gate_server.SEND_RETRY_MAX_S = 1.0

    extra_threads = []
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    sender = threading.Thread(target=gate_server.sender_worker, daemon=True)
    sender.start()

    try:
        # ① 헤더에 X-API-Key 가 실리고, 본문에 type 이 없다
        reset()
        gate_server.send_q.put(tagging_event("ev-001"))
        drain()
        req = STATE.requests[0] if STATE.requests else None
        check("① X-API-Key 헤더가 실려 나간다",
              req is not None and req["api_key"] == TEST_API_KEY,
              f"받은 값={req['api_key'] if req else '요청 없음'!r}")
        check("① 본문에 내부 라우팅 키 type 이 없다",
              req is not None and "type" not in req["body"],
              f"본문 키={sorted(req['body']) if req else []}")
        check("① 엔드포인트가 /api/tagging-events 다",
              req is not None and req["path"] == "/api/tagging-events",
              f"path={req['path'] if req else None}")

        # ② gate_pass 본문에 result 가 실리고 local_verdict 는 없다
        reset()
        gate_server.send_q.put(gate_pass_event("ev-002", "untagged"))
        drain()
        body = STATE.requests[0]["body"] if STATE.requests else {}
        check("② 본문에 result 가 실린다",
              body.get("result") == "untagged", f"result={body.get('result')!r}")
        check("② 본문에 local_verdict 가 없다",
              "local_verdict" not in body, f"본문 키={sorted(body)}")
        check("② 엔드포인트가 /api/gate-pass-events 다",
              STATE.requests and STATE.requests[0]["path"] == "/api/gate-pass-events")

        # ③ 4xx(401)는 재시도 없이 버리고 큐가 계속 흐른다
        reset()
        with STATE.lock:
            STATE.codes.extend([401, 200])   # 첫 이벤트 401, 다음 이벤트 200
        gate_server.send_q.put(gate_pass_event("ev-401", "untagged"))
        gate_server.send_q.put(tagging_event("ev-after-401"))
        flushed = drain()
        time.sleep(0.6)                      # 몰래 재시도가 있으면 여기서 요청이 더 쌓인다
        ids = [r["body"].get("event_id") for r in STATE.requests]
        check("③ 401 을 받으면 재시도하지 않는다",
              ids.count("ev-401") == 1, f"ev-401 요청 수={ids.count('ev-401')}")
        check("③ 401 뒤 이벤트가 막히지 않고 흐른다",
              flushed and "ev-after-401" in ids, f"요청 순서={ids}")

        # ④ 5xx 는 백오프로 재시도한다
        reset()
        with STATE.lock:
            STATE.codes.extend([500, 500, 200])
        gate_server.send_q.put(tagging_event("ev-500"))
        flushed = drain()
        times = [r["at"] for r in STATE.requests]
        gaps = [round(b - a, 3) for a, b in zip(times, times[1:])]
        backoff_grows = len(gaps) >= 2 and gaps[1] > gaps[0] * 1.5
        check("④ 5xx 를 받으면 재시도한다",
              flushed and len(STATE.requests) == 3, f"요청 수={len(STATE.requests)}")
        check("④ 재시도 간격이 지수로 늘어난다",
              backoff_grows, f"간격(초)={gaps} (첫 간격 {gate_server.SEND_RETRY_S})")

        # ⑤ 복귀 요청은 서버의 정식 dispatch 경로와 기기 키를 사용한다.
        reset()
        gate_server.call_return_service()
        return_req = STATE.requests[0] if STATE.requests else None
        check("⑤ 복귀 요청이 dispatch return 경로로 나간다",
              return_req is not None and return_req["path"] == "/api/dispatch/commands/return")
        check("⑤ 복귀 요청에 X-API-Key가 실린다",
              return_req is not None and return_req["api_key"] == TEST_API_KEY)
        check("⑤ 복귀 요청 본문은 비어 있다",
              return_req is not None and return_req["body"] == {})

        # ⑥ 터치 상태는 IDLE -> SENSING -> IDLE+복귀 순서로 바뀐다.
        gate_server.sensing_enabled.clear()
        with patch.object(gate_server, "call_return_service") as return_call:
            gate_server.toggle_sensing()
            check("⑥ 첫 터치는 센싱을 시작하고 복귀하지 않는다",
                  gate_server.sensing_enabled.is_set() and not return_call.called)
            gate_server.toggle_sensing()
            check("⑥ 두 번째 터치는 센싱을 끄고 복귀를 한 번 요청한다",
                  not gate_server.sensing_enabled.is_set() and return_call.call_count == 1)

        # ⑦ 이벤트를 손으로 넣지 않고 실제 생성 경로(tof_worker -> output_worker -> 전송)를
        #    태워서 본문 필드를 본다. ②처럼 result 키를 시험이 직접 먹이면 수리 전 코드도
        #    통과해 버려서(자가 시딩) 필드 이름 수리를 증명하지 못한다.
        gate_server.sensing_enabled.set()
        reset()
        blocked_cm = gate_server.BEAM_THRESHOLD_CM - 5      # 차단으로 읽히는 거리
        open_cm = gate_server.BEAM_THRESHOLD_CM + 40        # 열림으로 읽히는 거리
        sensor_a = FakeSensor([blocked_cm, blocked_cm] + [open_cm] * 50)
        sensor_b = FakeSensor([open_cm] * 4 + [blocked_cm, blocked_cm] + [open_cm] * 50)
        tof = threading.Thread(target=gate_server.tof_worker, args=(sensor_a, sensor_b), daemon=True)
        out = threading.Thread(target=gate_server.output_worker, daemon=True)
        extra_threads.extend([tof, out])
        tof.start()
        out.start()
        drain()
        time.sleep(0.3)
        bodies = [r["body"] for r in STATE.requests if r["path"] == "/api/gate-pass-events"]
        emitted = bodies[0] if bodies else {}
        check("⑦ 실제 생성 경로가 만든 본문에 result 가 실린다",
              emitted.get("result") == "untagged", f"result={emitted.get('result')!r}")
        check("⑦ 실제 생성 경로가 만든 본문에 local_verdict 가 없다",
              bool(emitted) and "local_verdict" not in emitted, f"본문 키={sorted(emitted)}")
        check("⑦ 실제 생성 경로가 만든 본문에 type 이 없다",
              bool(emitted) and "type" not in emitted, f"본문 키={sorted(emitted)}")
    finally:
        gate_server.stop_event.set()
        sender.join(timeout=3.0)
        for t in extra_threads:
            t.join(timeout=3.0)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3.0)
        alive = [t.name for t in ([sender, server_thread] + extra_threads) if t.is_alive()]
        print(f"\n[정리] 모의 서버 포트 {port} 닫음. "
              f"살아남은 스레드={alive or '없음'}")

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n결과: {len(RESULTS) - len(failed)}/{len(RESULTS)} 통과")
    if failed:
        print("실패: " + ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
