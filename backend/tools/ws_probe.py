"""대시보드 WebSocket 수신 확인용 최소 클라이언트.

/ws/dashboard에 붙어 정해진 시간 동안 받은 메시지를 그대로 출력한다.
1단계 관통 실측(태깅 POST -> DB 적재 -> 대시보드 push)에서 마지막 수신 확인에 쓴다.

사용 예:
  python -m tools.ws_probe --url ws://127.0.0.1:8000/ws/dashboard --seconds 5 --expect 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import websockets


async def probe(url: str, seconds: float, expect: int) -> int:
    received: list[dict] = []
    try:
        async with websockets.connect(url) as ws:
            print(f"[connected] {url}")
            deadline = asyncio.get_event_loop().time() + seconds
            while asyncio.get_event_loop().time() < deadline:
                remaining = deadline - asyncio.get_event_loop().time()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    msg = {"raw": raw}
                received.append(msg)
                print(f"[recv] {json.dumps(msg, ensure_ascii=False)}")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {type(exc).__name__}: {exc}")
        return 2

    # hello 메시지는 기대 개수에서 제외하고 실제 이벤트만 센다.
    events = [m for m in received if m.get("type") not in ("hello",)]
    print(f"\n총 수신 {len(received)}건(이벤트 {len(events)}건, 기대 {expect}건 이상)")
    return 0 if len(events) >= expect else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="대시보드 WS 수신 프로브")
    p.add_argument("--url", default="ws://127.0.0.1:8000/ws/dashboard")
    p.add_argument("--seconds", type=float, default=5.0)
    p.add_argument("--expect", type=int, default=1)
    args = p.parse_args(argv)
    return asyncio.run(probe(args.url, args.seconds, args.expect))


if __name__ == "__main__":
    sys.exit(main())
