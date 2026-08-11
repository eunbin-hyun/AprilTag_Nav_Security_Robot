"""FastAPI 앱 진입점. 인입·조회·통계·관제 보조·개발자 페이지 라우터 + WebSocket 2종을 붙인다.

uvicorn 워커 1개 전제 — 커넥션 목록을 인메모리로 들기 때문에 워커를 늘리면 브로드캐스트가 갈라진다.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

# ⚠ `from app import auth`로 들이지 마라. 바로 아래 `from app.routers import (... auth ...)`가
#    같은 이름을 덮어써서 `auth`가 **라우터 모듈**을 가리키게 된다. 그러면 lifespan의
#    워밍업이 매 기동마다 AttributeError로 죽는다(8/2 실 컨테이너 로그에서 잡혔다 —
#    lifespan이 예외를 삼켜서 로그를 안 보면 티가 안 났다).
from app import auth as auth_core
from app.config import get_settings
# ⚠ 같은 이유로 `from app import dispatch`·`from app import weather`도 쓰지 마라 —
#    아래 `from app.routers import (... dispatch ...)`가 이름을 덮는다. 함수만 직접 들인다.
from app.dispatch import handle_weather_reading
from app.robot_channel import handle_robot_frame, on_robot_disconnect
from app.routers import (
    account,
    admin,
    assistant,
    auth,
    camera,
    demo,
    device_sound,
    devpage,
    dispatch,
    ingest,
    query,
    rpi_commands,
    staff,
    stats,
    weather,
)
from app.weather import start_weather_poller, stop_weather_poller
from app.security import (
    OriginGuardMiddleware,
    start_ws_dashboard_recheck,
    ws_api_key_ok,
    ws_dashboard_allowed,
    ws_origin_allowed,
)
from app.ws import (
    make_envelope,
    make_robot_envelope,
    manager,
    robot_heartbeat,
    robot_manager,
)

logger = logging.getLogger("c207.ws")

# WS 핸드셰이크를 키로 막을 때 쓰는 닫기 코드. 1008 = policy violation.
WS_UNAUTHORIZED_CODE = 1008

async def _warm_up_assistant() -> None:
    """관제 보조 프롬프트를 미리 태운다. 실패해도 조용히 넘어간다.

    ⚠ **`ai_bridge`를 여기서 늦게 들인다** — 모듈 머리에서 들이면 AI 갈래가 못 뜨는 날
    백엔드가 통째로 안 뜬다. 데우기는 없어도 서비스가 도는 기능이라 그 위험을 질 자리가 아니다.

    ⚠ 캐시를 들고 있는 것은 백엔드가 아니라 **llama-server**다. 그래서 모델 서버만 다시
    뜨면 백엔드가 멀쩡해도 캐시가 비고, 이 배선으로는 안 잡힌다 — 그때는 `ai/ops/warm.sh`다.
    """
    try:
        from app import ai_bridge

        # ⚠ **`info`가 아니라 `warning`이다.** 실서버 로그 설정이 앱 로거의 INFO를 안 내보내서,
        # 8/7 밤에 "데우기가 돌았나"를 볼 길이 없었다. 기동 때 한 번 찍히는 줄이라 시끄러울
        # 일이 없고, **볼 수 없으면 다음에도 같은 자리에서 헤맨다.**
        logger.warning("관제 보조 데우기 끝: %s", await ai_bridge.warm_up(ai_bridge.build_config()))
    except Exception:  # pragma: no cover - 데우기가 안 돼도 서비스는 돈다
        logger.exception("관제 보조 데우기가 실패했다 — 요원의 첫 질문이 대신 캐시를 채운다")


def _setup_app_logging() -> None:
    """`c207.*` 로거에 핸들러와 레벨을 붙인다.

    ⛔ 2026-08-09 백지검토 — **운영에서 앱 INFO 로그가 한 줄도 안 나오고 있었다.**
    앱이 basicConfig·dictConfig를 안 부르고 컨테이너도 `--log-level` 없이 떠서,
    `c207.*` 로거의 유효 레벨이 root 기본값(WARNING)이었다. 실서버 로그를 재면 INFO가
    uvicorn 자신의 `connection open/closed` 뿐이고 앱이 남긴 줄은 0건이다. 출동 대기 창·
    자동 발사·카메라 시작·로봇 재등록·셔틀 재전송이 전부 INFO라, 시연 중 무엇이 어긋나면
    되짚을 자료가 없는 자리였다.

    ⚠ 바로 위 `_warm_up_assistant`가 이 결함을 우회하려고 `info`를 `warning`으로 올려
       뒀다. 그런 우회가 저장소에 여럿 있는데(auth·dispatch·robot_channel 등), 이 배선이
       배포된 뒤에 되돌릴 자리다 — 지금 같이 내리면 배포 사이 창에서 그 줄들이 사라진다.
    ⚠ uvicorn은 자기 로거만 설정하고 root는 안 건드리므로 여기서 `c207` 하나만 잡는다.
       root에 붙이면 SQLAlchemy·httpx 로그까지 딸려 나온다.
    ⚠ 핸들러가 이미 있으면 다시 안 붙인다 — 같은 프로세스에서 lifespan이 두 번 도는
       시험 환경에서 같은 줄이 두 벌로 찍힌다.
    레벨은 `C207_LOG_LEVEL`로 조절한다(기본 INFO).
    """
    level = os.getenv("C207_LOG_LEVEL", "INFO").upper()
    app_logger = logging.getLogger("c207")
    if not app_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     [%(name)s] %(message)s"))
        app_logger.addHandler(handler)
    app_logger.setLevel(getattr(logging, level, logging.INFO))
    # ⛔ 2026-08-09 — 여기 `app_logger.propagate = False` 를 뒀다가 **시험을 깨뜨려 걷었다.**
    #    caplog(pytest)는 root 에 핸들러를 붙여 잡는지라, propagate 를 끄면 `c207.*` 로그가
    #    root 로 안 올라가 caplog 가 한 줄도 못 본다. 실제로 `test_verdict_hardening.py` 의
    #    "미래 스큐가 한도를 넘으면 경고 로그가 남는다" 가 전량 병렬에서 이것 때문에 깨졌다
    #    (단독 실행은 lifespan 을 안 타서 통과해 더 헷갈리는 자리였다).
    #    끄려던 까닭은 "uvicorn 핸들러로 한 번 더 올라가 두 벌로 찍힌다" 였는데 **틀린 걱정이었다** —
    #    uvicorn 의 LOGGING_CONFIG 에는 root 설정이 없고 uvicorn·uvicorn.error·uvicorn.access
    #    셋만 잡는다(2026-08-09 실측). root 에 핸들러가 없으니 올라가도 찍힐 자리가 없다.


# ⛔ 문서 창구 셋을 끈다(로그인 설계 확정본 §8 결정 2, 사용자 확정).
# `/docs`·`/redoc`·`/openapi.json`은 쓰기 창구 지도를 통째로 담는다 — 신원 입력·명령 전송
# 창구까지 이름·본문 모양이 다 적혀 있어서 익명에게 열어 둘 자리가 아니다. None을 주면
# FastAPI가 그 라우트를 **아예 안 단다**(권한 판정이 아니라 라우트 부재라 404다).
#
# nginx 조각에서도 같은 셋을 404로 끊었다(deploy/nginx-c207-api.conf). 두 자리를 다 닫는
# 이유는 nginx가 젠킨스 배포 밖이라서다 — 그 파일만 고치면 EC2에는 아무 일도 안 일어나고,
# 손 배포 전까지 앱 층이 유일한 방어다.
#
# ⚠ 스키마가 사라지는 게 아니라 HTTP 창구만 없다. 시험·도구가 명세를 읽어야 하면
# `app.openapi()`를 직접 부른다(딕셔너리가 그대로 나온다).
@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """기동 때 한 번만 하는 준비. 지금은 계정 열거 방지용 더미 해시 워밍업 하나다.

    ⚠ 이걸 안 하면 **재기동 뒤 첫 요청에서만** 없는 아이디가 있는 아이디보다 느리다.
    더미 해시는 lru_cache라 처음 부를 때 scrypt를 한 번 더 태우는데, 그 한 번이 곧
    "이 아이디는 없다"를 시간으로 알려주는 오라클이다(백지검토 코덱스 X15).

    실패해도 기동은 막지 않는다 — 워밍업은 방어를 **빠르게** 만들 뿐이고, 안 되면 첫
    요청이 캐시를 채워서 그 뒤로는 같아진다.
    """
    _setup_app_logging()

    try:
        await auth_core.dummy_password_hash_async()
    except Exception:  # pragma: no cover - 기동을 막을 이유가 없다
        logger.exception("더미 해시 워밍업이 실패했다 — 첫 로그인 요청이 대신 채운다")

    # 날씨 주기 조회(2026-08-03 사용자 확정 — 화면 기상 칩을 계속 갱신한다).
    # 한 바퀴가 끝나면 handle_weather_reading이 받아 기본 목적지를 고치고 대시보드에 민다.
    # WEATHER_POLL_INTERVAL_SEC을 0 이하로 두면 안 띄우고 예전 lazy 갱신만 남는다(되돌릴 자리).
    # 여기서 터져도 기동은 막지 않는다 — 날씨는 관제 본업이 아니고, 못 받으면 화면이
    # 마지막 성공값에 stale 표시를 달고 간다.
    try:
        start_weather_poller(handle_weather_reading)
    except Exception:  # pragma: no cover - 기동을 막을 이유가 없다
        logger.exception("날씨 주기 조회를 못 띄웠다 — 부를 때만 갱신하는 예전 갈래로 간다")

    # 관제 보조 프롬프트 데우기(2026-08-07 AI 갈래 15차 요청). 캐시가 빈 첫 질문이 7.5초인데
    # 두 번째부터 0.7초다 — 시연에서 요원의 첫 질문이 그 7.5초를 문다.
    #
    # ⭐ **기다리지 않고 배경으로 던진다.** 데우기가 10초쯤 걸려서 여기서 await하면 기동이
    # 그만큼 늦고, 젠킨스가 컨테이너를 띄우고 재는 헬스 검사에 걸릴 자리다. 데우기는 첫
    # 질문 전에만 끝나면 되는 일이라 기동을 붙잡을 값어치가 없다.
    #
    # ⚠ **작업 참조를 들고 있어야 한다** — asyncio는 참조가 없는 태스크를 도중에 거둬 간다.
    warm_up_task: asyncio.Task | None = None
    if get_settings().assistant_warm_up_on_start:
        warm_up_task = asyncio.create_task(_warm_up_assistant())

    yield

    # ⚠ 세우고 **접힐 때까지 기다린다.** 안 기다리면 종료 뒤에도 살아 있다가 시험에서 다음
    #   케이스로 샌다(날씨 폴러와 같은 자리다).
    if warm_up_task is not None and not warm_up_task.done():
        warm_up_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await warm_up_task

    # ⚠ 세우고 접힐 때까지 기다린다. 안 기다리면 종료 뒤에도 다음 await에서 한 번 더
    #    깨어나 시험에서는 다음 케이스로 새고 라이브에서는 종료 로그 뒤에 조회가 한 줄 더 찍힌다.
    await stop_weather_poller()


app = FastAPI(
    title="C207 싸큐리티 관제 서버",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# 교차 오리진 상태변경 차단(로그인 설계 확정본 §1.2). SameSite=Lax 위에 얹는 보강 한 겹이고,
# Origin이 실려 있을 때만 판정한다 — 기기·서버 호출(젯슨 인입·curl·젠킨스)은 그 헤더가 없어서
# 그대로 통과한다. HTTP만 보고 WebSocket 핸드셰이크는 안 건드린다(security.py 그 절 참고).
app.add_middleware(OriginGuardMiddleware)


# 검증 오류 본문에 원문을 되비추면 안 되는 칸. 소문자로 견준다.
_SECRET_BODY_FIELDS = frozenset({"password", "new_password", "api_key", "token", "secret"})
_SECRET_REDACTED = "***"


@app.exception_handler(RequestValidationError)
async def _scrub_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 본문에서 비밀 칸의 `input` 원문을 지운다.

    ⚠ **실측으로 새던 자리다.** pydantic 오류 dict는 실패한 값을 `input`에 그대로 담고
    FastAPI 기본 처리기가 그걸 응답에 싣는다. 그래서 상한(200자)을 넘긴 비밀번호로 로그인을
    때리면 **비밀번호 원문이 422 응답 본문에 통째로 돌아왔다.** 응답은 nginx 액세스 로그·
    브라우저 개발자 도구·에러 리포터에 남는 자리라, 한 번 새면 여러 곳에 굳는다.

    지우는 건 `input` 하나뿐이다. `loc`·`msg`·`type`은 그대로 둬서 화면이 "어느 칸이 왜
    틀렸나"를 여전히 읽는다 — 방어를 얹느라 오류 안내를 못 쓰게 만들지 않는다.

    ⚠ `ctx`도 비운다. `ctx`에 패턴·경계값이 실리는데 그 안에 원문이 섞이는 검증기가 있다.
    """
    scrubbed = []
    for err in exc.errors():
        item = dict(err)
        loc = item.get("loc") or ()
        if any(isinstance(part, str) and part.lower() in _SECRET_BODY_FIELDS for part in loc):
            if "input" in item:
                item["input"] = _SECRET_REDACTED
            item.pop("ctx", None)
        scrubbed.append(item)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder({"detail": scrubbed}),
    )


async def reject_unauthorized(websocket: WebSocket) -> None:
    """키가 안 맞는 핸드셰이크를 1008로 닫는다. **accept한 뒤에** 닫는 게 요점이다.

    ⚠ 예전에는 accept 전에 `close(1008)`을 불렀다. ASGI 규약상 그건 핸드셰이크 거절이라
      전선에는 `HTTP 403`만 나가고 1008은 아무 데도 안 실린다(실 uvicorn 실측:
      `InvalidStatus: server rejected WebSocket connection: HTTP 403`). starlette TestClient는
      그 자리에서 1008을 합성해 돌려줘서 시험만 초록이었다 — 계약이 시험 안에서만 살아 있었다.

    403으로 잘리면 브라우저 WebSocket API가 상태 코드를 JS에 안 준다(onclose code 1006).
    그러면 화면이 "인증 실패"와 "네트워크 끊김"을 구분 못 해 무한 재접속을 돈다. accept 후
    1008로 닫으면 코드가 그대로 올라가서 화면이 재시도를 멈출 수 있다.

    accept해도 커넥션 목록에는 안 넣는다 — 곧장 닫으므로 브로드캐스트 대상이 되지 않는다.
    """
    await websocket.accept()
    await websocket.close(code=WS_UNAUTHORIZED_CODE, reason="unauthorized")

app.include_router(query.router)
app.include_router(stats.router)
app.include_router(ingest.router)
app.include_router(devpage.router)
app.include_router(assistant.router)
app.include_router(dispatch.router)
# 출동 창구 중 **기기가 부르는 하나**(복귀)만 별도 라우터다. 로그인을 켠 뒤에도 라파이가
# 기기 키로 복귀를 부를 수 있어야 해서 게이트가 갈린다(routers/dispatch.py device_router).
app.include_router(dispatch.device_router)
# 로그인 창구 셋. AUTH_REQUIRE_LOGIN과 무관하게 늘 산다 — 켜기 전에 계정을 심고 실제로
# 들어가 보는 게 롤아웃 3단계다(로그인 설계 확정본 §6.2).
app.include_router(auth.router)
# N14 명부·계정 창구. 역할 게이트는 auth.py 하나가 쥔다(로그인 설계 확정본 §3).
app.include_router(staff.router)
app.include_router(account.router)
# 실시간 카메라 프록시. 젯슨 미리보기(MJPEG)를 서버가 받아 넘겨 로그인 뒤로 넣는다
# (2026-08-03 사용자 확정 — 젯슨 8090은 관문이 0이라 직결하지 않는다).
app.include_router(camera.router)
# 시간대별 예보. 관제 화면의 기상 칩을 누르면 이 창구를 부른다(08-03 사용자 확정).
# 지금 날씨는 WS 로 계속 가고, 이 창구는 눌렀을 때만 오늘·내일 예보를 준다.
app.include_router(weather.router)
# 기기 스피커 폴링. 라파이가 1초마다 "울릴 거 있어?"를 묻는 자리라 인입과 같은 기기 키 게이트다.
app.include_router(device_sound.router)
# ⭐ 같은 큐를 실기기 폴러가 부르는 이름으로도 낸다. 실기기(`hardware/rpi/rpi_commands.py`)가
# 모의 서버로 개발돼 `/api/rpi/commands/*`를 부르는데 서버에 그 라우트가 0건이라 붙이면
# 404만 받는 자리였다. 서버를 그쪽에 맞추기로 사용자가 정했다(2026-08-05).
# ⚠ 두 창구가 같은 큐를 보되 커서는 device_id별로 따로 든다 — 한 기기가 둘을 같이 쓰면
#   같은 신호를 두 번 받는다. 실기기는 이쪽 하나만 쓴다.
app.include_router(rpi_commands.router)
# ⛔ 관리자 전용. 지금은 시연 자료 비우기 하나이고 되돌릴 수 없는 창구라, 플래그와 무관하게
# 늘 막히는 `require_admin_always` 를 쓴다(모듈 머리에 근거).
app.include_router(admin.router)
# ⭐ 시연 도구 창구(2026-08-05 프론트 22차). 관리자만 부른다 — 실제 통과 기록을 만드는
#    일이라 명부 수정과 같은 급으로 잠갔다.
app.include_router(demo.router)


@app.websocket("/ws/dashboard")
async def ws_dashboard(websocket: WebSocket) -> None:
    """대시보드 push 채널. 서버가 인입 이벤트를 메시지로 밀어낸다.

    수신은 하트비트/구독 제어 용도로만 열어 두고, 붙는 순간 hello 메시지를 한 번 보낸다.

    WS_REQUIRE_API_KEY_DASHBOARD(또는 상위 스위치 WS_REQUIRE_API_KEY)가 켜져 있으면 키 없는
    접속을 1008로 닫는다(기본은 꺼짐). 이 채널이 받는 키는 기기 키(API_KEY)가 아니라
    DASHBOARD_API_KEY다 — 브라우저가 기기 키를 들면 개발자 도구에 그대로 보이기 때문이다.

    ⭐ `AUTH_REQUIRE_LOGIN`을 켜면 판정이 **세션 쿠키**로 갈아탄다(로그인 설계 확정본 §5.1).
    쿠키는 같은 오리진 핸드셰이크에 자동으로 실리니 쿼리스트링 키가 필요 없어지고, 그 값이
    nginx 액세스 로그에 통째로 남던 문제도 같이 사라진다. 켠 뒤에는 DASHBOARD_API_KEY를
    폴백으로 **안 남긴다** — 남기면 그게 세션을 우회하는 뒷문이다. 갈림은 security.py
    `ws_dashboard_allowed` 한 자리에 있다.

    ⭐ 켜진 구간에서는 붙은 뒤에도 **주기마다 세션을 다시 본다**(백지검토 F3). 핸드셰이크
    한 번으로 끝내면 해제·강등된 계정의 열린 탭이 계속 push를 받아, 설계가 세션을 고른
    근거("강등·해제가 다음 요청부터 먹힌다")가 WS에서만 깨진다. 꺼진 구간에서는 태스크를
    아예 안 띄운다(제1 불변) — `start_ws_dashboard_recheck`가 None을 돌려준다.
    """
    if not await ws_dashboard_allowed(websocket):
        await reject_unauthorized(websocket)
        return
    # ⚠ `manager.connect`와 재검증 태스크 띄우기를 **try 안에** 둔다. 밖에 두면 둘 중
    # 하나가 터졌을 때 `finally`를 못 타서, 목록에 들어간 소켓이 안 걷히거나 태스크가
    # 미아로 남는다(2026-08-02 2차 검토 G3). `manager.disconnect`는 `discard`라 연결
    # 전에 불려도 안전하고, `recheck`는 미리 None으로 둬서 finally가 이름을 늘 본다.
    recheck: asyncio.Task | None = None
    try:
        await manager.connect(websocket)
        recheck = start_ws_dashboard_recheck(websocket)
        await websocket.send_json(
            make_envelope("hello", {"channel": "dashboard", "clients": manager.dashboard_count})
        )
        while True:
            # 클라이언트가 보내는 건 무시하고 연결만 유지(핑퐁은 프로토콜 계층이 처리).
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("대시보드 WS 처리 중 예외 — 연결을 닫는다")
    finally:
        # ⚠ 어떤 경로로 빠져나가도 목록에서 빼야 한다. except 두 곳에만 두면 취소(CancelledError)
        # 같은 경로에서 죽은 커넥션이 목록에 남아 broadcast가 계속 그쪽으로 send를 시도한다.
        #
        # 재검증 태스크도 같은 자리에서 걷는다 — 안 걷으면 끊긴 소켓을 붙잡고 60초마다
        # 세션을 조회하는 태스크가 탭 수만큼 남는다(로봇 채널 하트비트와 같은 규칙).
        if recheck is not None:
            recheck.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recheck
        manager.disconnect(websocket)


@app.websocket("/ws/robot")
async def ws_robot(websocket: WebSocket) -> None:
    """로봇 outbound 채널. 로봇이 서버로 접속을 열고, 로봇 상태는 이 채널로만 올라온다(M-4).

    프레임 처리는 robot_channel이 맡는다 — 상태 반영·대시보드 중계·mission_status 알림 변환.
    ⚠ 하트비트 태스크는 finally에서 반드시 취소한다. 안 하면 죽은 소켓에 계속 send를
    시도하는 태스크가 남는다.

    WS_REQUIRE_API_KEY_ROBOT(또는 상위 스위치 WS_REQUIRE_API_KEY)가 켜져 있으면 키 없는
    접속을 1008로 닫는다(기본은 꺼짐 — 켜는 시점은 팀 결정이다. config.py 주석 참고).

    ⛔ **Origin 가드를 먼저 탄다**(2026-08-08 사용자 확정 · 백지검토 ②). 이 채널이 익명인데
    Origin 검사까지 없어서, 남의 웹페이지가 `new WebSocket("wss://…/ws/robot")` 으로 붙어
    로봇 ID를 주장하면 **진짜 젯슨 소켓이 닫히고**(ws.py의 claim_conflict) 그 뒤 출동 명령이
    그쪽으로 갔다. 대시보드 채널엔 같은 가드가 이미 있는데 로봇 채널만 빠져 있었다.
    ⚠ 기기는 Origin 헤더를 안 싣고 `ws_origin_allowed` 가 "없으면 통과"라, **이 한 줄로
    브라우저 갈래만 닫히고 젯슨·라파이·시험 클라이언트는 그대로 붙는다.** WS 인증 전체를
    켜는 것(위 스위치)은 발표 뒤로 정해진 별개 결정이다.
    """
    if not ws_origin_allowed(websocket):
        await reject_unauthorized(websocket)
        return
    if not await ws_api_key_ok(websocket, "robot"):
        await reject_unauthorized(websocket)
        return
    settings = get_settings()
    await robot_manager.connect(websocket)
    heartbeat = asyncio.create_task(
        robot_heartbeat(
            websocket, settings.ws_heartbeat_sec, settings.ws_heartbeat_max_miss
        )
    )
    try:
        await websocket.send_json(
            make_robot_envelope(
                "registered",
                {"channel": "robot", "heartbeat_sec": settings.ws_heartbeat_sec},
            )
        )
        while True:
            try:
                frame = await websocket.receive_json()
            except json.JSONDecodeError:
                # JSON이 아닌 프레임 하나로 연결을 끊지 않는다(로봇 재접속 루프 방지).
                await websocket.send_json(
                    make_robot_envelope("error", {"reason": "invalid_json"})
                )
                continue
            await handle_robot_frame(websocket, frame)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("로봇 WS 처리 중 예외 — 연결을 닫는다")
    finally:
        heartbeat.cancel()
        # 취소가 실제로 끝날 때까지 한 틱 기다린다. 안 기다리면 루프가 같이 닫히는 판에서
        # "Task was destroyed but it is pending"이 뜨고, 로그를 보는 사람이 그걸 진짜 누수로
        # 읽는다. `robot_heartbeat`는 finally가 없어 취소 뒤 소켓을 다시 안 만진다.
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        # 에지 추적 상태는 커넥션이 끊겨도 안 버린다 — 로봇은 Wi-Fi가 한 번 끊겼다 붙어도
        # 같은 자리에 서 있어서, 버리면 재접속 첫 보고가 가짜 도착·가짜 ETA 표본이 된다.
        # 수명은 TTL 청소가 맡는다(on_robot_disconnect가 그 청소를 한 번 훑는다).
        robot_id = robot_manager.robot_id_of(websocket)
        robot_manager.disconnect(websocket)
        on_robot_disconnect(robot_id)
