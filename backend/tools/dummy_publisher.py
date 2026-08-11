"""더미 퍼블리셔 (지라 S15P11C207-83).

실장비 없이 태깅·빔 통과·셔틀 도착 이벤트를 시나리오로 서버에 POST하는 CLI다.
event_id는 실장비 규칙과 같게 {device_id}-{unix_ms}-{seq}로 만든다.

재시도 규칙(아키텍처 2026-07-23 §02):
- 400·401: 재시도 없음(스키마·인증 오류라 재시도해도 같다). 로그만 남긴다.
- 409: 성공 취급(멱등 중복). 단 이 서버는 중복을 200으로 돌려주므로 실제로는 409를 볼 일이 드물다.
- 5xx·무응답(타임아웃/연결 실패): 재시도 대상.
- 타임아웃 1초, 재시도 2회.

사용 예:
  C207_API_KEY=dev-local-key python -m tools.dummy_publisher \\
      --base-url http://127.0.0.1:8000 --scenario normal --gate 1 --tag-id 20250001
  python -m tools.dummy_publisher --scenario tagging --count 3

키 넣는 길은 셋이고 앞에 적은 것이 이긴다(`resolve_api_key`의 실제 순서다).
  --api-key-stdin   표준입력 첫 줄로 받는다. `ps`에도 셸 이력에도 안 남는다(제일 안전).
  --api-key         명령줄 인자. 콕 집어 준 값이라 환경변수를 이긴다. ⚠ 같은 호스트의 다른
                    사용자가 `ps`로 읽는다 — 개발 키(dev-local-key)에만 쓴다.
  C207_API_KEY      환경변수. 같은 셸에서 여러 번 부를 때 편하다. 진짜 키는 이쪽이나
                    `--api-key-stdin`으로 넣는다.
셋 다 안 주면 예전과 같이 `dev-local-key`로 돈다(로컬 개발 기본값).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from typing import Any

DEFAULT_API_KEY = "dev-local-key"

import httpx

TIMEOUT_S = 1.0
MAX_RETRIES = 2
RETRY_BACKOFF_S = 0.2

# 빔 A와 빔 B가 끊기는 사이 간격. 사람이 게이트를 지나는 실제 폭(수백 ms)을 흉내 낸다.
# 0으로 두면 이동 시간 없는 통과가 되고, 서버 정합 검사가 어긋난 주장으로 본다.
BEAM_TRANSIT_SEC = 0.2


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def make_event_id(device_id: str, seq: int) -> str:
    return f"{device_id}-{int(time.time() * 1000)}-{seq}"


class Publisher:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-API-Key": api_key}
        self.client = httpx.Client(timeout=TIMEOUT_S)

    def close(self) -> None:
        self.client.close()

    def post(self, path: str, body: dict[str, Any]) -> bool:
        """멱등·재시도 규칙에 맞춰 한 이벤트를 올린다. 성공(2xx/409)이면 True."""
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.client.post(url, json=body, headers=self.headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # 무응답 계열 — 재시도 대상
                if attempt <= MAX_RETRIES:
                    print(f"  [무응답 {type(exc).__name__}] {path} 재시도 {attempt}/{MAX_RETRIES}")
                    time.sleep(RETRY_BACKOFF_S)
                    continue
                print(f"  [실패] {path} 무응답, 재시도 소진: {exc}")
                return False

            code = resp.status_code
            if 200 <= code < 300:
                print(f"  [OK {code}] {path} event_id={body['event_id']} -> {resp.text[:120]}")
                return True
            if code == 409:
                print(f"  [409 멱등중복=성공취급] {path} event_id={body['event_id']}")
                return True
            if code in (400, 401):
                print(f"  [{code} 재시도안함] {path}: {resp.text[:160]}")
                return False
            if 500 <= code < 600:
                if attempt <= MAX_RETRIES:
                    print(f"  [5xx {code}] {path} 재시도 {attempt}/{MAX_RETRIES}")
                    time.sleep(RETRY_BACKOFF_S)
                    continue
                print(f"  [실패] {path} 5xx 재시도 소진")
                return False
            # 그 밖의 코드는 재시도하지 않고 실패로
            print(f"  [{code} 예상밖] {path}: {resp.text[:160]}")
            return False

    # ── 이벤트 빌더 ──────────────────────────────────────────────────────

    def send_tagging(self, device_id: str, gate_no: int, tag_id: str, seq: int) -> bool:
        body = {
            "event_id": make_event_id(device_id, seq),
            "device_id": device_id,
            "gate_no": gate_no,
            "tag_id": tag_id,
            "observed_at": now_iso(),
        }
        return self.post("/api/tagging-events", body)

    def send_gate_pass(
        self, device_id: str, gate_no: int, direction: str | None, status: str, seq: int
    ) -> bool:
        """빔 통과 한 건. 빔 두 개는 **다른 순간**에 끊긴 값으로 만든다.

        예전엔 두 칸에 `now_iso()`를 각각 불렀는데, 그러면 이동 시간이 0초인 통과가 된다.
        굵은 시계(윈도우)에서는 두 값이 아예 같은 눈금으로 접혀서, 라파 판별식
        (`A_TO_B if beam_a_ts < beam_b_ts else B_TO_A`)으로는 A_TO_B라 적힐 수 없는 자료가
        나갔다. 서버 정합 검사(S15P11C207-243)가 그걸 어긋난 주장으로 보는 게 맞고, 고칠
        자리는 시늉 도구 쪽이다 — 실기기 빔 두 개는 사람이 지나가는 수백 ms 사이에 끊긴다.
        """
        first = dt.datetime.now(dt.timezone.utc)
        second = first + dt.timedelta(seconds=BEAM_TRANSIT_SEC)
        if status != "complete":
            beam_a, beam_b, observed = first, None, first
        elif direction == "B_TO_A":
            # 퇴장은 빔 B가 먼저 끊긴다.
            beam_a, beam_b, observed = second, first, second
        else:
            beam_a, beam_b, observed = first, second, second
        body = {
            "event_id": make_event_id(device_id, seq),
            "device_id": device_id,
            "gate_no": gate_no,
            "direction": direction,
            "status": status,
            "beam_a_ts": beam_a.isoformat(),
            "beam_b_ts": beam_b.isoformat() if beam_b else None,
            "observed_at": observed.isoformat(),
        }
        return self.post("/api/gate-pass-events", body)

    def send_shuttle(self, device_id: str, gate_no: int, shuttle_no: str, seq: int) -> bool:
        body = {
            "event_id": make_event_id(device_id, seq),
            "gate_no": gate_no,
            "shuttle_no": shuttle_no,
            "signal_ts": now_iso(),
        }
        return self.post("/api/shuttle-arrivals", body)


def run_scenario(pub: Publisher, args: argparse.Namespace) -> int:
    dev = args.device_id
    gate = args.gate
    ok = 0
    total = 0

    def tick(result: bool) -> None:
        nonlocal ok, total
        total += 1
        ok += 1 if result else 0

    if args.scenario in ("tagging", "normal"):
        for i in range(args.count):
            print(f"[태깅] {i + 1}/{args.count}")
            tick(pub.send_tagging(dev, gate, args.tag_id, i))

    if args.scenario in ("gate-pass", "normal"):
        for i in range(args.count):
            print(f"[빔 통과 complete/A_TO_B] {i + 1}/{args.count}")
            tick(pub.send_gate_pass(dev, gate, "A_TO_B", "complete", i))

    if args.scenario == "untagged":
        # 태깅 없이 빔만 통과(2단계 미태깅 판정 입력). 지금은 적재만 된다.
        for i in range(args.count):
            print(f"[빔 통과, 태깅 없음] {i + 1}/{args.count}")
            tick(pub.send_gate_pass(dev, gate, "A_TO_B", "complete", i))

    if args.scenario == "incomplete":
        for i in range(args.count):
            print(f"[빔 미완 incomplete/null] {i + 1}/{args.count}")
            tick(pub.send_gate_pass(dev, gate, None, "incomplete", i))

    if args.scenario in ("shuttle", "normal"):
        print("[셔틀 도착]")
        tick(pub.send_shuttle(dev, args.shuttle_gate, args.shuttle_no, 0))

    print(f"\n결과: {ok}/{total} 성공")
    return 0 if ok == total else 1


def resolve_api_key(args: argparse.Namespace, stdin_line: str | None = None) -> str:
    """키를 어디서 받을지 정한다. --api-key-stdin > --api-key > C207_API_KEY > 기본값."""
    if args.api_key_stdin:
        line = stdin_line if stdin_line is not None else sys.stdin.readline()
        key = line.strip()
        if not key:
            raise SystemExit("--api-key-stdin인데 표준입력 첫 줄이 비었다.")
        return key
    if args.api_key:
        return args.api_key
    return os.environ.get("C207_API_KEY") or DEFAULT_API_KEY


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C207 더미 이벤트 퍼블리셔")
    p.add_argument("--base-url", default="http://127.0.0.1:8000")
    # ⚠ 기본값을 여기 두지 않는다. 안 주면 환경변수를 보고, 그것도 없을 때만 기본 키다.
    #   argparse 기본값을 박아 두면 "환경변수를 넣었는데 왜 안 먹나"가 생긴다.
    p.add_argument("--api-key", default=None, help="⚠ ps에 노출된다. C207_API_KEY나 --api-key-stdin을 권한다")
    p.add_argument(
        "--api-key-stdin",
        action="store_true",
        help="표준입력 첫 줄에서 키를 읽는다(ps에 안 남는다)",
    )
    p.add_argument(
        "--scenario",
        default="normal",
        choices=["normal", "tagging", "gate-pass", "untagged", "incomplete", "shuttle"],
    )
    p.add_argument("--device-id", default="raspberry01")
    p.add_argument("--gate", type=int, default=1)
    p.add_argument("--tag-id", default="20250001")
    p.add_argument("--shuttle-gate", type=int, default=2)
    p.add_argument("--shuttle-no", default="SHUTTLE-A")
    p.add_argument("--count", type=int, default=1)
    args = p.parse_args(argv)

    pub = Publisher(args.base_url, resolve_api_key(args))
    try:
        return run_scenario(pub, args)
    finally:
        pub.close()


if __name__ == "__main__":
    sys.exit(main())
