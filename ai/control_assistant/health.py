"""관제 보조가 지금 어떤 상태인지 한 묶음으로 돌려준다.

## ⛔ 왜 필요한가 — "눈에 보인다"가 마지막 남은 조건이다

이 프로젝트의 차별점은 **온프레미스**다. 인터넷으로 나가는 호출이 0건이고 우리 GPU 가
답을 쓴다. 그런데 **그것이 화면에 안 보인다.** 요원도 심사위원도 답만 보지, 그 답이
어디서 왔는지 모른다.

⭐ 여기 값들은 전부 **이미 알고 있던 것**이다 — 어디에도 새로 물어보지 않는다.
llama-server 에 상태를 묻는 호출 하나(`/props`)만 더하고, 나머지는 우리가 세던 값이다.

## ⚠ 이 함수가 하지 않는 것

**GPU 온도·전력·사용률을 안 잰다.** 그 값은 GPU 서버에서만 읽히는데 지금 EC2 로 열린
길이 llama-server 포트 하나뿐이다. 길을 더 여는 것은 보안 결정이라 여기서 안 한다.

⚠ 실패해도 예외를 안 던진다. 상태 조회가 서비스를 멈추면 안 된다.
"""
from __future__ import annotations

import time

import httpx

from .client import CONNECT_TIMEOUT_SEC, MAX_HISTORY_MESSAGES
from .config import AssistantConfig
from .guards import _FAIL_THRESHOLD, _OPEN_SEC

# 상태를 물을 때 이보다 오래 기다리지 않는다. 화면 한 칸을 채우는 값이라 급하지 않다.
PROBE_TIMEOUT_SEC = 4.0


async def snapshot(cfg: AssistantConfig) -> dict:
    """지금 상태 한 묶음. 화면이 "이 답은 어디서 왔나"를 그릴 재료다."""
    out = {
        "onprem": _is_local(cfg.base_url),
        "model": cfg.model,
        "endpoint": _mask(cfg.base_url),
        "read_timeout_sec": cfg.timeout_sec,
        "connect_timeout_sec": CONNECT_TIMEOUT_SEC,
        "max_tokens": cfg.max_tokens,
        "history_messages": MAX_HISTORY_MESSAGES,
        "breaker": {"fail_threshold": _FAIL_THRESHOLD, "open_sec": _OPEN_SEC},
        "reachable": None,
        "probe_ms": None,
        "context_per_slot": None,
        "slots": None,
    }
    if not cfg.enabled:
        out["reachable"] = False
        return out

    # ⛔ **`/props` 는 `/v1` 밑이 아니라 루트에 있다**(2026-08-07 실측 — `/v1/props` 로
    # 물었더니 40ms 만에 못 닿는 것으로 떨어졌다). base_url 이 `…/v1` 로 끝나므로 떼어 낸다.
    # ⚠ `/chat/completions` 는 `/v1` 밑이 맞다. 창구마다 자리가 다르다.
    root = cfg.base_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    url = f"{root}/props"
    timeout = httpx.Timeout(PROBE_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC)
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        out["probe_ms"] = round((time.monotonic() - started) * 1000)
        if resp.status_code != 200:
            out["reachable"] = False
            return out
        body = resp.json()
    except (httpx.HTTPError, ValueError):
        # ⚠ 못 닿아도 나머지 값은 그대로 쓸모가 있다. 설정은 우리가 아는 것이다.
        out["reachable"] = False
        out["probe_ms"] = round((time.monotonic() - started) * 1000)
        return out

    out["reachable"] = True
    gen = body.get("default_generation_settings") or {}
    out["context_per_slot"] = gen.get("n_ctx")
    out["slots"] = body.get("total_slots")
    return out


def _is_local(base_url: str) -> bool:
    """우리가 띄운 서버인가. `client._is_local_endpoint` 와 같은 잣대다."""
    return "gms.ssafy.io" not in base_url


def _mask(base_url: str) -> str:
    """화면에 내보낼 주소. ⚠ 내부 IP 를 그대로 보이지 않는다.

    온프레미스라는 사실만 전하면 되고, 역터널 주소는 요원이 알 필요가 없다.
    """
    return "우리 GPU 서버 (사내망)" if _is_local(base_url) else "외부 프록시"
