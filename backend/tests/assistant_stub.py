"""관제 보조 시험용 LLM 목.

⚠ httpx.AsyncClient.post를 전역으로 갈아 끼우면 **시험 클라이언트(ASGI httpx)까지 같이
바뀐다** — 라우터 시험에서 요청 자체가 목으로 빨려 들어간다. 그래서 갈아 끼우는 자리를
LLM 클라이언트 모듈이 보는 httpx 이름 하나로 좁힌다.
"""
from __future__ import annotations

import httpx

from ai.control_assistant import client as llm_client, pipeline


class _Shim:
    """llm_client가 보는 httpx 자리.

    ⚠ **클라이언트가 `httpx.` 로 쓰는 이름을 전부 들고 있어야 한다.** 예외 이름만 있으면
    되는 줄 알았는데, 연결·읽기 상한을 가르면서 `httpx.Timeout` 을 쓰기 시작하자 시험
    다섯이 `AttributeError` 로 무너졌다(2026-08-07). 클라이언트가 새 이름을 쓰면 여기도
    같이 늘려야 한다.
    """

    TimeoutException = httpx.TimeoutException
    HTTPError = httpx.HTTPError
    Timeout = httpx.Timeout

    def __init__(self, handler) -> None:
        outer = self

        class _Client:
            def __init__(self, *args, **kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> bool:
                return False

            async def post(self, url, *, json, headers):
                outer.calls.append({"url": url, "json": json, "headers": headers})
                return handler()

        self.AsyncClient = _Client
        self.calls: list[dict] = []


def stub_llm(monkeypatch, handler) -> _Shim:
    """handler()가 돌려주는 httpx.Response(또는 던지는 예외)로 LLM 응답을 흉내낸다.

    돌려받은 객체의 .calls에 실제로 나간 요청이 쌓인다(프롬프트 검사용).

    ⛔⛔ **회로 차단기를 여기서 닫는다.** 차단기는 파이프라인 모듈에 붙은 **프로세스 상태**라
    케이스 사이에 샌다. 실패를 흉내내는 케이스가 연달아 돌면 차단기가 열리고, 그 다음 케이스는
    **LLM 을 아예 안 불러서** `shim.calls` 가 빈 채로 `IndexError` 가 난다.

    2026-08-07 에 실제로 그랬다 — 차단기를 넣으면서 `test_assistant_guards.py` 에만 초기화
    fixture 를 달았고, `test_assistant_fallback.py` 일곱 건이 조용히 빨개졌다. 실패 원인이
    "LLM 을 안 불렀다"라 표면 증상(IndexError)이 진짜 원인을 안 가리켰다.

    ⭐ 시험 파일마다 fixture 를 베끼는 대신 **stub 을 거는 자리 하나**에서 닫는다. 새 시험
    파일이 생겨도 stub 을 쓰는 한 자동으로 안전하다.
    """
    pipeline.reset_breaker()
    shim = _Shim(handler)
    monkeypatch.setattr(llm_client, "httpx", shim)
    return shim


def ok(content: str, finish_reason: str = "stop"):
    """정상 응답 handler.

    ⚠ `finish_reason` 을 실어 둔다. 답이 잘렸는지를 클라이언트가 이 값으로 판정하므로,
    안 실으면 "상한에 걸려 끊긴 답" 케이스를 흉내낼 수 없다.
    """
    return lambda: httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}]
    })
