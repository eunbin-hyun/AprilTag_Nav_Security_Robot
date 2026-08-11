"""실시간 카메라 프록시 — 젯슨 MJPEG를 로그인 뒤로 넣는다.

## 왜 프록시인가

젯슨은 이미 MJPEG를 내주고 있다(`hardware/jetson/ros2_ws/src/robot_perception/
robot_perception/tag_localizer_cv.py`의 `_start_preview`). 카메라 장치는 한 프로세스만 열 수
있어서 AprilTag 검출 노드가 그걸 쥔 채 미리보기까지 같이 내주는 구조다 — **우리가 스트리밍
프로세스를 새로 띄울 자리가 없다.**

그런데 그 서버는 인증이 0이라 주소만 알면 누구나 본다. 그래서 서버가 받아 넘겨서 로그인
뒤로 넣는다(사용자 확정). 발표장 안에서만 보는 직결 방식은 채택하지 않았다.

## 계약 셋

- 상류 경로는 `/stream` 하나다. 루트(`/`)는 젯슨이 주는 안내 HTML이라 안 탄다. 주소를
  잘못 넣어 그 HTML이 200으로 오면 **502로 끊는다** — 안 끊으면 화면이 깨진 이미지만
  띄우고 배선 실수가 조용해진다.
- 상류 Content-Type은 `multipart/x-mixed-replace; boundary=frameboundary`다. 값을 여기서
  다시 적지 않고 **상류가 준 헤더를 그대로 흘린다** — 경계 문자열이 바뀌면 두 자리를 같이
  고쳐야 하는 함정이 생기고, 한쪽만 고쳐지면 화면이 프레임을 못 자른다.
- 화면 오버레이(태그 검출 박스)는 젯슨이 그린 그대로 둔다(사용자 확정 — 발표에서 기술이
  보이는 그림이라).

## ⚠ img·video 태그는 인증 헤더를 못 붙인다 — 그래서 쿠키에 얹는다

이 저장소 로그인은 **세션 쿠키**다(`app/auth.py` `SESSION_COOKIE_NAME`). SPA와 API가 같은
오리진이라(`app/security.py` `request_self_origin` 주석의 nginx 실측) `<img
src="/api/camera/stream">` 한 줄이면 브라우저가 쿠키를 알아서 싣는다. 주소에 토큰을 싣는
방식(짧은 수명 토큰)은 **필요 없다** — 쿼리스트링 값은 nginx 액세스 로그에 통째로 남아서,
같은 일을 하면서 로그에 자격만 흘리는 쪽이 된다.

## ⚠ 이 창구는 `AUTH_REQUIRE_LOGIN`과 무관하게 늘 로그인을 요구한다

`require_agent_always`를 쓴다. 제1 불변("지금 있는 창구의 거동을 안 바꾼다")은 **기존** 창구를
지키는 약속이라 새 창구에는 안 걸리고(`app/auth.py` `_role_dependency` always 주석), 여기서
플래그를 따라가면 플래그가 꺼진 동안 공개 도메인에 카메라가 그대로 열린다 — 사용자가 안
고른 직결 방식보다 넓게 열린다(젯슨은 LAN 안인데 우리 서버는 인터넷에 있다).
되돌릴 자리는 아래 `_gate` 한 줄이다.

## 동시 접속 상한

젯슨이 매 프레임 JPEG를 인코딩하는데 여러 명이 붙으면 주행 중 태그 검출이 밀린다. 기본 3명
이고 넘치면 503이다. 세는 자리는 이 모듈의 카운터 하나다 — 이벤트 루프가 한 줄이라
증감 사이에 await가 없으면 잠금 없이도 갈라지지 않는다(그래서 `_try_acquire_slot`·
`_release_slot`에는 await가 없다).

자리를 **어떻게 돌려주느냐**가 상한만큼 중요하다. 두 겹을 건다.
  - 총 수명 상한(`DEFAULT_MAX_STREAM_SEC`). 안 걸면 붙어 놓고 안 읽는 탭이 자리를 영영 문다.
  - 반납은 **상류를 닫고 나서**다(`_UpstreamLink.aclose`). 먼저 돌려주면 닫는 동안 다음
    요청이 그 자리를 잡아 젯슨 쪽 실제 커넥션이 상한+1이 된다.

## ⚠ 상한은 **워커 1개 전제**다 — 깨지면 상한이 워커 수만큼 곱해진다

카운터가 프로세스 안 변수라 워커마다 따로 센다. `--workers 4`면 상한 3이 사실상 12가 되고,
젯슨은 12명분 JPEG를 인코딩하다 주행 중 태그 검출이 밀린다(상한을 둔 이유가 그거다).
지금 배포는 전부 워커 1개다 — `backend/Dockerfile` CMD, `deploy/docker-compose.backend.yml`의
`--workers 1`, `backend/README.md` 첫 줄("uvicorn 워커 1개 전제").

전제를 **코드가 스스로 본다**: 첫 요청에서 `_note_worker_assumption()`이 한 번 돈다. 환경변수가
워커 여러 개를 가리키면 WARNING을 남기고, 아니면 pid를 INFO로 남긴다 — 로그에 pid가 여러 개면
그것도 전제가 깨졌다는 신호다. **거절은 안 한다.** 워커 수를 확실히 알아낼 길이 없어서
(uvicorn `--workers`는 자식 프로세스 argv에 안 남는다) 잘못 짚으면 시연장에서 카메라가 통째로
막힌다 — 오탐의 값이 미탐보다 크다. 진짜로 워커를 늘려야 하면 카운터를 프로세스 밖(레디스·DB)
으로 옮겨야 하고, 그건 이 파일 하나로 안 끝난다.

## 설정

값을 `Settings`에서 이름으로 찾고, 없으면 아래 기본값을 쓴다. 이번 사이클에 여러 조가
`app/config.py`를 같이 만져서 줄을 더하면 텍스트 충돌이 나기 때문이다(`app/security.py`
`WS_SESSION_RECHECK_SEC`와 같은 판단). 팀장이 필드를 넣으면 이 파일은 한 글자도 안 고치고
그 값을 따라간다.

⚠ 다만 **상류 주소만은 값을 여기 다시 안 적는다.** `Settings`의 필드 기본값을 그대로 읽는다
(`DEFAULT_STREAM_URL`) — 두 벌로 두면 한쪽만 바뀌는 날 아래 "배선 안 됨" 판정이 통째로 뜻을
잃는다. 발표장 망이 바뀌면 주소가 그대로 틀어지는데 502가 뜬 뒤에야 알게 되니, 첫 요청에서
`_note_stream_target()`이 어느 주소로 나가는지를 남긴다 — 현장 값이 들어와 있으면 INFO,
기본값 그대로면 WARNING이다.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from collections.abc import AsyncIterator

import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.auth import AuthActor, require_agent_always
from app.config import Settings, get_settings
from app.ws import make_envelope, manager

logger = logging.getLogger("c207.camera")

router = APIRouter(prefix="/api/camera", tags=["camera"])

# 젯슨 미리보기 주소. 포트 기본값 8090은 `tag_drive.launch.py`의 `preview_port` 그대로다.
# ⚠ 호스트는 현장 IP라 배포마다 다르다 — 시연 전에 반드시 실측값으로 덮어쓴다.
#
# ⭐ 문자열을 여기 다시 안 적고 `Settings`의 **필드 기본값**을 읽는다. 두 벌로 두면 한쪽만
#    바뀌는 날 `_note_stream_target`의 "배선 안 됨" 판정(두 값이 같으냐)이 통째로 뜻을 잃어서,
#    경고가 영영 안 뜨거나 늘 뜬다. 고칠 자리는 `app/config.py`의 `camera_stream_url` 하나다.
#
# ⚠ `get_settings()`가 아니라 필드 기본값인 게 핵심이다. 인스턴스를 읽으면 환경변수로 덮어쓴
#    값이 들어와서 "기본값 그대로냐"를 물을 수 없다.
DEFAULT_STREAM_URL = Settings.model_fields["camera_stream_url"].default
# ⚠ **`config.py`의 `camera_max_viewers`와 같은 값이어야 한다.** 두 벌로 갈리면 설정을 안 준
# 판(시험·로컬)과 준 판(운영)이 다른 상한으로 돌아 "몇 명까지 되나"가 두 답이 된다.
# `DEFAULT_STREAM_URL`이 필드 기본값을 읽어 가는 것과 같은 이유인데, 이쪽은 `_setting`이
# 이미 그 필드를 먼저 보므로 여기 값은 **필드가 통째로 사라진 판의 안전망**이다.
DEFAULT_MAX_VIEWERS = 6
# 연결·읽기를 따로 건다. 연결은 "젯슨이 꺼져 있다"를 빨리 알아채는 값이고, 읽기는 붙긴 했는데
# 프레임이 안 오는 상태를 끊는 값이다. 젯슨은 프레임 사이를 0.01초로 도니 5초면 넉넉하다.
DEFAULT_CONNECT_TIMEOUT_SEC = 3.0
DEFAULT_READ_TIMEOUT_SEC = 5.0

# 스트림 하나의 총 수명(초). 넘기면 상류를 닫아 자리를 되돌린다. 0 이하면 상한을 끈다.
#
# ⭐ 왜 따로 필요한가 — 위 타임아웃 넷은 전부 **상류행**이다. 화면이 붙어 놓고 본문을 안 읽으면
#    (절전 든 탭이 그렇다) 우리가 `yield`에서 막혀 상류를 안 읽으니 read 타임아웃도 안 뛰고,
#    자리가 잡힌 채로 남는다. 셋이 그러면 나머지 전원 503이고 재기동 말고 푸는 길이 없다.
#    nginx도 못 막는다 — `deploy/nginx-c207-api.conf`의 `location /api/`에는 유량 제한이 없다.
#
# 900초로 잡은 근거 — 발표 한 판(10분 안팎)보다 길어서 보는 중에는 안 끊기고, 잊힌 탭은 15분
# 안에 자리를 돌려준다. 화면은 그림이 멈추면 다시 무는 쪽이라 끊겨도 저절로 이어진다.
DEFAULT_MAX_STREAM_SEC = 900.0

# ⭐ 밀린 프레임 버리기 (실시간 우선) ─────────────────────────────────────────
#
# **왜 있나** — 2026-08-05 프론트 실측: 상류가 초당 20.2장(프레임 76KB · 12Mbps)을 보내는데
# 브라우저 `<img>`는 초당 7~8장만 그린다. 매초 12장씩 밀려 5초면 60장 넘게 뒤처진다. 프레임은
# 꾸준히 오는데 **전부 옛 장면**이라, 사용자가 "영상이 5초쯤 늦다"고 본 자리가 그것이다.
#
# ⭐ **버퍼링 문제가 아니다.** 서버는 헤더까지 50ms에 받는다. 밀리는 곳은 브라우저 디코더 큐다.
# 그래서 "서버가 마지막으로 받은 시각"을 알려 주는 창구로는 이 지연을 **못 잰다** — 그 값은
# 늘 0.05초인데 눈으로는 5초가 늦다.
#
# 고치는 자리가 셋인데 각각 임자가 다르다.
#   젯슨이 덜 보낸다     — 현은빈. `tag_localizer_cv.py`의 `cv2.imencode`를 두세 장에 한 번.
#                          팀 문서 `jetson_to_web_interface_v2.md` §10.1이 이미 "5~10 fps로
#                          제한한다"고 정해 뒀다. 요청은 나갔고 언제 반영될지는 모른다.
#   **서버가 밀린 것을 버린다 — 여기다.** 젯슨을 안 기다려도 되고 화면도 안 고쳐도 된다.
#   화면이 최신만 그린다 — 프론트. multipart 파싱과 `ImageBitmap` 변환을 직접 해야 해서
#                          발표까지 남은 기간에는 새 결함 위험이 크다고 판단해 접었다.
#
# **어떻게 하나** — 상류를 읽는 곁 태스크가 프레임 경계로 잘라 **최신 한 장만** 슬롯에 둔다.
# 클라이언트가 느리면 그 슬롯이 덮어써지므로 밀린 장은 자연히 버려진다. 상류 읽기는 클라이언트
# 속도와 무관하게 계속 돌아서 젯슨 쪽 소켓 버퍼도 안 고인다.
#
# ⚠ **끄면 예전 거동 그대로다**(조각을 받는 대로 흘린다). 되돌릴 자리를 남겨 둔다.
DEFAULT_REALTIME_DROP = True

# 슬롯이 빌 때 클라이언트 쪽을 얼마나 기다렸다 다시 보나. 상류가 조용하면 이 주기로 깨어나
# "상류가 끝났는지"만 확인한다. 너무 짧으면 헛도는 깨어남이 늘고, 너무 길면 마지막 프레임이
# 그만큼 늦게 나간다.
FRAME_WAIT_TICK_SEC = 0.5

# ⭐ 영상 지연을 화면이 잴 수 있게 파트 헤더에 끼우는 시각 (2026-08-06 사용자 요청) ────────
#
# **왜 서버가 낼 수 있나** — 위 "밀린 프레임 버리기"를 넣기 전에는 못 냈다. 그때는 서버가
# 받은 시각이 최신인데 화면은 60장 뒤처진 옛 장을 그리고 있어서, 서버 시각을 줘 봐야 늘
# 0.05초였다(모듈 머리에 그렇게 적혀 있고 그 판단은 그 시점에 맞았다). 지금은 서버도 최신
# 한 장만 주므로 **화면이 그리는 장이 곧 서버가 방금 슬롯에 넣은 장**이다. 그 장에 시각을
# 붙이면 화면이 빼서 "서버 → 화면" 지연을 낸다.
#
# ⚠ **촬영 시각이 아니다.** 못 재는 몫은 젯슨에서 서버까지(실측 약 50ms)이고 이름도 그래서
# `X-Server-Frame-Time`이다. 화면이 이 값을 촬영 시각으로 읽으면 상류가 밀리는 날 지연을
# 실제보다 작게 본다.
#
# ⚠ **WS 봉투로 따로 흘리지 않은 이유** — 시각이 프레임과 같이 움직여야 짝이 안 어긋난다.
# 따로 흘리면 화면이 "방금 받은 시각"과 "지금 그리는 장"을 잘못 짝지어 지연이 튄다.
FRAME_TIME_HEADER = b"X-Server-Frame-Time"

# ⭐ 같은 시각을 WS 로도 흘린다 (프론트 28차 요청) ────────────────────────────
#
# **왜 둘 다 내나** — 화면이 `<img src=...>` 한 줄로 그려서 **파트 헤더를 못 읽는다.**
# 읽으려면 `fetch`로 multipart를 직접 자르고 blob으로 그리는 방식으로 통째로 바꿔야 하는데,
# 그 변경이 실패하면 **영상이 아예 안 나온다.** 시연에서 제일 눈에 띄는 자리라 프론트가
# 헤더 대신 이 길을 골랐다(28차 §1-1).
#
# ⚠ **헤더는 그대로 둔다.** 로봇이 붙어 실제 스트림을 눈으로 본 뒤에 파싱을 짜겠다고 했고,
# 그때는 헤더가 더 정확하다(프레임과 시각이 같이 움직여 짝이 안 어긋난다).
#
# ⚠ 어긋남이 **한 주기 안**이라는 것이 이 길을 쓸 수 있는 근거다. 서버가 밀린 장을 버리고
# 최신 한 장만 두므로 슬롯의 시각이 곧 화면이 보는 장의 시각이고, 1초 오차는 "5초 밀렸나
# 실시간인가"를 가르는 데 지장이 없다.
FRAME_TIME_WS_TYPE = "camera_frame_time"
FRAME_TIME_WS_INTERVAL_SEC = 1.0

# 마지막으로 WS 로 시각을 흘린 때(단조 시계). ⚠ **모듈 전역이다** — 시청자마다 들면 상한
# 셋이 붙었을 때 같은 시각이 초당 세 번 나간다. 어느 스트림이 넣었든 슬롯의 최신 시각은
# 같으므로 한 자리에서 스로틀한다.
_last_frame_time_push = 0.0

# 상류가 Content-Type을 안 주는 예외 상황에서만 쓰는 값. 정상 경로는 상류 헤더를 그대로 흘린다.
FALLBACK_CONTENT_TYPE = "multipart/x-mixed-replace; boundary=frameboundary"

# OpenAPI 200 본문에 적는 미디어 타입. 경계 문자열을 뗀 값이다(스펙 키에는 파라미터를 안 적는다).
# 문자열을 세 번째로 다시 적지 않으려고 위 값에서 잘라 쓴다.
STREAM_MEDIA_TYPE = FALLBACK_CONTENT_TYPE.split(";")[0].strip()

# 취소 중에도 정리에 주는 유예. 커넥션 하나 닫기는 밀리초 단위라 넉넉하다.
CLOSE_GRACE_SEC = 2.0

_UPSTREAM_DOWN = "카메라에 연결하지 못했습니다. 로봇이 켜져 있는지 확인해 주세요."
_TOO_MANY_VIEWERS = "지금은 카메라를 볼 수 있는 인원이 가득 찼습니다. 잠시 뒤 다시 시도해 주세요."

# 지금 흐르고 있는 스트림 수. `reset_camera_state()`가 시험 위생용으로 비운다.
_active_viewers = 0


def _setting(name: str, default):
    """`Settings`에 그 이름이 있으면 그 값, 없으면 기본값. 모듈 머리 "설정" 절 참고."""
    return getattr(get_settings(), name, default)


def _stamp_frame(frame: bytes, at: dt.datetime) -> bytes:
    """파트 헤더 끝에 `X-Server-Frame-Time` 한 줄을 끼운다.

    프레임 한 장은 `--경계\\r\\n헤더들\\r\\n\\r\\n<JPEG>` 모양이다. 헤더 블록이 끝나는 빈 줄
    **앞에** 끼워야 본문(JPEG)을 안 건드린다.

    ⚠ **모양이 다르면 그대로 돌려준다.** 상류가 헤더를 안 붙이거나 줄바꿈이 다른 판에서
    억지로 끼우면 그림이 통째로 깨진다 — 지연 표시 하나 때문에 영상을 잃는 건 손해다.
    ⚠ `Content-Length`는 JPEG 길이라 헤더가 늘어도 안 건드린다.
    """
    for sep in (b"\r\n", b"\n"):
        end = frame.find(sep + sep)
        if end < 0:
            continue
        stamp = FRAME_TIME_HEADER + b": " + at.isoformat().encode("ascii") + sep
        return frame[: end + len(sep)] + stamp + frame[end + len(sep) :]
    return frame


async def _push_frame_time(at: dt.datetime) -> None:
    """슬롯의 시각을 대시보드로 흘린다. **주기 안에 여러 번 불려도 한 번만 나간다.**

    ⚠ **주기 태스크를 안 만든다.** 카메라가 안 도는 동안에는 보낼 값 자체가 없어서, 별도
    태스크는 빈 시각을 들고 헛도는 자리가 된다. 프레임이 들어오는 그 자리에서 스로틀하면
    "영상이 흐르는 동안만 나간다"가 저절로 지켜진다 — 화면은 안 오는 구간을 "영상 대기"로
    적는다(28차 §1-2).

    ⚠ 실패를 삼킨다. 이건 **표시용 곁가지**라, 여기서 터지면 영상 중계가 같이 죽는다.
    """
    global _last_frame_time_push
    now = time.monotonic()
    if now - _last_frame_time_push < FRAME_TIME_WS_INTERVAL_SEC:
        return
    _last_frame_time_push = now
    try:
        await manager.broadcast(
            make_envelope(FRAME_TIME_WS_TYPE, {"server_frame_time": at.isoformat()})
        )
    except Exception:  # noqa: BLE001 - 표시용 곁가지가 영상을 죽이면 안 된다
        logger.debug("프레임 시각 중계에 실패했다 — 영상은 그대로 간다", exc_info=True)


def reset_frame_time_throttle() -> None:
    """시험 위생. 모듈 전역이라 앞 케이스가 민 시각이 다음 케이스를 조용히 막는다."""
    global _last_frame_time_push
    _last_frame_time_push = 0.0


def _try_acquire_slot(limit: int) -> bool:
    """자리를 하나 잡는다. 가득 찼으면 False다(await가 없어야 갈라지지 않는다)."""
    global _active_viewers
    if _active_viewers >= limit:
        return False
    _active_viewers += 1
    return True


def _release_slot() -> None:
    global _active_viewers
    _active_viewers = max(0, _active_viewers - 1)


def active_viewers() -> int:
    """지금 몇 명이 보고 있나. 시험과 로그가 읽는다."""
    return _active_viewers


def reset_camera_state() -> None:
    """자리 카운터를 비운다. 시험 위생용(DB 밖 상태라 TRUNCATE로 안 지워진다)."""
    global _active_viewers, _worker_assumption_logged, _stream_target_logged
    _active_viewers = 0
    _worker_assumption_logged = False
    _stream_target_logged = False
    reset_frame_time_throttle()


# ── 워커 1개 전제를 코드가 스스로 본다 (모듈 머리 "⚠ 상한은 워커 1개 전제다" 절) ──────

# 워커 수가 드러나는 환경변수들. uvicorn은 `--workers`가 없을 때 WEB_CONCURRENCY를 읽고,
# gunicorn·PaaS도 같은 이름을 쓴다. 자식 프로세스 argv로는 못 알아내서(spawn이라 argv가
# 부트스트랩으로 바뀐다) 여기까지가 알아낼 수 있는 전부다.
_WORKER_ENV_NAMES = ("WEB_CONCURRENCY", "UVICORN_WORKERS", "GUNICORN_WORKERS")

_worker_assumption_logged = False


def worker_count_hint(env: dict[str, str] | None = None) -> int | None:
    """환경변수가 가리키는 워커 수. 안 적혀 있거나 숫자가 아니면 None."""
    source = os.environ if env is None else env
    for name in _WORKER_ENV_NAMES:
        raw = source.get(name)
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            logger.debug("%s 값이 숫자가 아니다 — %r", name, raw)
    return None


def _note_worker_assumption() -> None:
    """첫 요청에서 한 번만 돈다. 워커가 여럿이면 경고, 아니면 pid를 남긴다.

    기동 시점(import)이 아니라 첫 요청인 이유는 로깅 설정 때문이다 — import는 uvicorn이
    로깅을 잡기 전일 수 있어서 그때 남긴 줄은 조용히 사라진다.
    """
    global _worker_assumption_logged
    if _worker_assumption_logged:
        return
    _worker_assumption_logged = True
    workers = worker_count_hint()
    if workers is not None and workers > 1:
        logger.warning(
            "카메라 동시 접속 상한이 워커 %d개에 곱해진다 — 상한 %s가 사실상 %d명이 된다. "
            "이 카운터는 프로세스 안 변수라 워커마다 따로 센다(워커 1개 전제).",
            workers,
            _setting("camera_max_viewers", DEFAULT_MAX_VIEWERS),
            int(_setting("camera_max_viewers", DEFAULT_MAX_VIEWERS)) * workers,
        )
        return
    logger.info("카메라 프록시 pid=%d — 자리 상한은 이 프로세스 안에서만 센다", os.getpid())


# ── 상류 주소가 배선된 값인지 스스로 본다 (모듈 머리 "설정" 절) ─────────────────────

_stream_target_logged = False


def _note_stream_target() -> None:
    """첫 요청에서 한 번, 지금 어느 주소로 나가는지 남긴다.

    `CAMERA_STREAM_URL`을 아무도 안 넣었으면 `Settings` 필드 기본값(사설 IP)으로 나간다. 그
    상태가 조용하면 발표장 망이 바뀐 날 화면이 502로 뜬 뒤에야 원인을 찾게 된다. **거절은 안 한다** —
    기본값으로도 지금 배포는 돌고 있어서, 막으면 시연장에서 카메라가 통째로 죽는다
    (워커 전제 경고와 같은 판단이다).
    """
    global _stream_target_logged
    if _stream_target_logged:
        return
    _stream_target_logged = True
    # ⚠ `_setting`의 기본값을 None으로 두면 안 된다. 설정 필드가 배선되는 순간 그 값이
    #    늘 truthy라 아래 경고가 영영 안 뜨고, "배선 안 됨"을 잡겠다는 장치가 배선과 동시에
    #    죽는다(2026-08-03 재검증 F4). 그래서 **파일 기본값과 같은지**로 판정한다 —
    #    설정이 없거나 기본값 그대로면 아직 아무도 안 정한 상태다.
    configured = _setting("camera_stream_url", DEFAULT_STREAM_URL)
    if configured and configured != DEFAULT_STREAM_URL:
        logger.info("카메라 상류 주소 — %s (설정 camera_stream_url)", configured)
        return
    logger.warning(
        "카메라 상류 주소가 기본값 그대로다 — %s. 발표장 망이 바뀌면 여기부터 틀어지니 "
        "현장 주소를 camera_stream_url로 넣어라.",
        configured or DEFAULT_STREAM_URL,
    )


def _new_client(timeout: httpx.Timeout) -> httpx.AsyncClient:
    """상류로 나가는 클라이언트 한 개. **시험이 갈아 끼우는 자리다.**

    스트림 하나에 클라이언트 하나를 쓰고 스트림이 끝날 때 같이 닫는다 — 공용 클라이언트를
    두면 어느 스트림이 끝났을 때 커넥션을 반납해도 되는지가 흐려진다.
    """
    return httpx.AsyncClient(timeout=timeout)


async def _aclose_quietly(*closeables) -> None:
    """닫되 닫기가 실패해도 삼킨다(`app/ws.py` `_close_quietly`와 같은 판단) — **취소만 빼고.**

    하나가 터져도 나머지를 이어서 닫는다. 좀비 커넥션을 막는 게 이 함수의 목적이라서다.

    ⚠ `CancelledError`는 **다시 던진다.** 예전에는 `BaseException`으로 통째로 먹었는데,
    그러면 서버 종료 중에 이 정리를 돌리던 태스크가 취소돼도 취소를 안 존중해서 종료가 그만큼
    늦어진다. 취소는 "그만두라"는 신호지 "실패"가 아니다.

    ⭐ 그런데 **취소를 다시 던지는 것만으로는 좀비가 안 막힌다.** 취소 스코프 안에서는 `await`가
    계속 취소로 튀어서(anyio 취소는 한 번이 아니라 스코프를 나갈 때까지 걸린다) 정리가 아예
    시작을 못 한다. 그래서 닫기 한 번에 `shield`를 씌워 유예 시간만큼 지켜 준다 — 취소는
    "그만두라"지 "정리도 하지 말라"가 아니다. 정리가 끝나면 바깥 취소가 그대로 이어서 튄다.

    ⚠ **httpx 0.28 함정 — 두 번째 시도로는 못 되살린다.** `Response.aclose()`는 `is_closed`를
    **`await` 앞에서** 세운다(`httpx/_models.py`). 첫 시도가 그 `await`에서 취소로 끊기면 응답은
    "닫힘"으로 표시된 채 밑단 스트림은 안 닫혀서, 나중에 다시 불러도 곧장 되돌아온다. 그러니
    "background가 이어서 닫아 준다"에 기댈 수 없다 — **첫 시도를 끝내는 것**이 유일한 방어라
    shield가 여기 있다(2026-08-03 실측 재현: 이 shield를 빼면 좀비 커넥션이 그대로 남는다).
    """
    cancelled: BaseException | None = None
    for closeable in closeables:
        try:
            # 유예를 넘기면 포기한다 — 종료를 영영 붙잡느니 커넥션 하나를 흘리는 쪽이 낫다.
            with anyio.move_on_after(CLOSE_GRACE_SEC, shield=True):
                await closeable.aclose()
        except asyncio.CancelledError as exc:
            # 남은 것도 한 번씩은 불러 본다 — 취소 스코프 밖이면 그건 정상적으로 닫힌다.
            cancelled = exc
            logger.debug("상류 정리가 취소됐다 — 남은 것을 닫고 다시 던진다", exc_info=True)
        except Exception:  # noqa: BLE001 - 좀비 커넥션을 막는 게 우선이다
            logger.debug("상류 정리 중 예외 — 무시하고 나머지를 닫는다", exc_info=True)
    if cancelled is not None:
        raise cancelled


def _boundary_marker(content_type: str | None) -> bytes | None:
    """`multipart/...; boundary=x` 에서 프레임 구분자 `--x` 를 뽑는다. 못 뽑으면 None.

    ⚠ **None 이면 부르는 쪽이 예전 거동(조각 그대로 흘리기)으로 되돌아간다.** 경계를 모르면
    프레임을 자를 수 없고, 어림짐작으로 자르면 반쪽 JPEG 가 나가서 화면이 깨진다. 못 자를
    바에는 밀리더라도 온전한 그림을 보내는 쪽이 낫다.

    ⚠ 따옴표를 감싼 판(`boundary="x"`)도 받는다. RFC 2046 이 허용하고 실제로 그렇게 보내는
    구현이 있다.
    """
    if not content_type:
        return None
    for part in content_type.split(";"):
        key, _, value = part.partition("=")
        if key.strip().lower() != "boundary":
            continue
        marker = value.strip().strip('"')
        return f"--{marker}".encode() if marker else None
    return None


class _UpstreamLink:
    """젯슨 커넥션 한 벌 + 그 스트림이 잡고 있는 자리 하나.

    ⭐ **닫기를 몇 번 불러도 한 번만 먹는다.** 정리를 부르는 자리가 둘이라서다 — 본문
    제너레이터의 `finally`와, 응답이 끝난 뒤 starlette이 부르는 background 태스크. 한쪽만
    두면 어느 한 갈래가 새고, 막으면 자리 카운터가 남의 자리까지 깎는다.

    ⚠ **"한 번만"의 대상이 둘로 갈린다.** 자리 반납은 딱 한 번이어야 하지만, 상류 커넥션은
    *닫힐 때까지* 다시 시도해야 한다 — 아래 `aclose` 주석 참고.
    """

    def __init__(
        self, client: httpx.AsyncClient, upstream: httpx.Response, viewer: str
    ) -> None:
        self._client = client
        self._upstream = upstream
        self._viewer = viewer
        self._slot_released = False
        self._upstream_closed = False
        # ⭐ 밀린 프레임 버리기용 슬롯. **한 장만 담는다** — 클라이언트가 느리면 다음 장이
        # 덮어써서 밀린 것이 자연히 버려진다(모듈 머리 "밀린 프레임 버리기" 절).
        self._latest_frame: bytes | None = None
        # ⭐ 그 장을 슬롯에 넣은 시각. 프레임과 **같은 자리에서 같이** 갱신해야 짝이 안
        # 어긋난다(위 `FRAME_TIME_HEADER` 절). 덮어써진 장의 시각도 같이 버려진다.
        self._latest_at: dt.datetime | None = None
        self._frame_ready = asyncio.Event()
        self._pump_done = False
        self._dropped = 0

    async def aclose(self) -> None:
        """상류 커넥션을 **먼저** 닫고, 자리는 그 뒤에 딱 한 번 반납한다.

        ⭐ **순서가 계약이다.** 자리를 먼저 돌려주면 그 뒤 `_aclose_quietly`가 도는 동안(닫기
        유예가 최대 `CLOSE_GRACE_SEC`다) 다음 요청이 그 자리를 잡아서, 젯슨 쪽 **실제 커넥션이
        상한+1**이 된다. 상한을 둔 이유가 젯슨 인코딩 부하라 세는 대상이 어긋나는 자리다 —
        화면 새로고침 연타로 밟는다.

        ⚠ 그래서 반납을 `finally`에 둔다. `_aclose_quietly`는 취소를 다시 던지므로 그냥 뒤로
        옮기기만 하면 취소 갈래에서 자리가 영영 안 돌아온다.

        플래그를 둘로 가른 이유는 둘의 성질이 달라서다. 자리 반납은 **두 번 하면 남의 자리까지
        비우니** 한 번이어야 하고, 커넥션 닫기는 **덜 닫힌 채 넘어가면 좀비가 남으니** 못 끝냈으면
        다음 정리가 한 번 더 밀어 봐야 한다(예전에는 플래그 하나가 둘을 같이 막았다).

        ⚠ 다만 그 재시도에 기대면 안 된다 — `_aclose_quietly` 주석의 httpx `is_closed` 함정
        때문에 응답 쪽은 두 번째 호출이 빈 호출이다. 진짜 방어는 그 함수의 shield다.
        """
        try:
            if not self._upstream_closed:
                await _aclose_quietly(self._upstream, self._client)
                self._upstream_closed = True
        finally:
            # 플래그 세우기와 반납 사이에 await가 없어야 두 갈래가 겹쳐도 한 번만 먹는다.
            if not self._slot_released:
                self._slot_released = True
                _release_slot()

    async def _close_after(self, max_sec: float) -> None:
        """수명 상한을 재는 곁 태스크. 넘기면 상류를 닫아 자리를 되돌린다.

        시간을 `relay` 안에서 재면 정작 잡아야 할 갈래를 못 잡는다 — 화면이 붙어 놓고 본문을
        안 읽으면 그 제너레이터는 `yield`에 멈춘 채라 다음 줄이 아예 안 돈다. 그래서 따로 돈다.
        """
        await asyncio.sleep(max_sec)
        logger.info(
            "카메라 스트림이 수명 상한(%.0f초)에 닿았다 — %s를 끊고 자리를 돌려준다",
            max_sec,
            self._viewer,
        )
        await self.aclose()

    async def relay(self) -> AsyncIterator[bytes]:
        """상류 바이트를 받는 대로 흘린다. 끝나면 자리와 상류 커넥션을 반드시 되돌린다.

        ⭐ `aiter_raw`라 **프레임을 통째로 모으지 않는다.** 도착한 조각을 그대로 내보낸다.

        ⭐ `finally`가 이 창구의 핵심이다. 안 닫으면 젯슨 쪽 스레드가 계속 프레임을 써 넣는
        좀비 커넥션이 접속마다 쌓인다.

        ⚠ 클라이언트가 창을 닫을 때 이 `finally`가 **바로 안 돌 수도 있다.** starlette은
        끊김을 보면 `stream_response`를 취소하는데(starlette/responses.py), 그때 이
        제너레이터는 `yield`에 멈춘 채 남아 파이썬이 거둬 갈 때에야 닫힌다. 그래서 정리를
        background 태스크에도 같이 걸어 둔다 — 그쪽은 취소 직후에 확실히 돈다. 두 번 불려도
        `aclose`가 한 번만 먹는다.

        읽는 중에 상류가 끊기거나 읽기 타임아웃이 나면 **그 자리에서 스트림을 끝낸다.**
        상태줄은 이미 200으로 나간 뒤라 502로 못 바꾸고, 빈 화면을 오래 물고 있느니 끊는
        쪽이 정직하다.

        ⏱ 총 수명 상한은 곁 태스크(`_close_after`)가 잰다. 이 안에서 시간을 재면 정작 잡아야
        할 갈래(안 읽는 화면)를 못 잡는다 — 그때 이 제너레이터는 `yield`에 멈춰 있다.
        """
        max_sec = float(_setting("camera_max_stream_sec", DEFAULT_MAX_STREAM_SEC))
        reaper = asyncio.create_task(self._close_after(max_sec)) if max_sec > 0 else None

        # ⭐ 실시간 갈래를 고른다. 경계를 못 뽑으면(`_boundary_marker`가 None) 프레임을 자를 수
        #    없으므로 예전 거동으로 되돌아간다 — 반쪽 JPEG를 내보내느니 밀리는 쪽이 낫다.
        boundary = (
            _boundary_marker(self._upstream.headers.get("content-type"))
            if _setting("camera_realtime_drop", DEFAULT_REALTIME_DROP)
            else None
        )
        pump = asyncio.create_task(self._pump_frames(boundary)) if boundary else None

        try:
            if pump is None:
                async for chunk in self._upstream.aiter_raw():
                    yield chunk
            else:
                async for frame in self._newest_frames():
                    yield frame
        except (httpx.HTTPError, httpx.StreamError) as exc:
            # ⚠ `StreamError`는 `HTTPError`가 **아니다**(RuntimeError 계열이다). 수명 상한이
            #    상류를 닫고 나면 다음 읽기가 그쪽으로 튀므로 같이 받는다.
            logger.info(
                "카메라 상류가 끊겼다 — %s: %s (%s)", type(exc).__name__, exc, self._viewer
            )
        finally:
            if reaper is not None:
                reaper.cancel()
            if pump is not None:
                pump.cancel()
                if self._dropped:
                    logger.info(
                        "밀린 프레임 %d장을 버렸다 — %s (실시간 우선)",
                        self._dropped,
                        self._viewer,
                    )
            await self.aclose()

    async def _pump_frames(self, boundary: bytes) -> None:
        """상류를 **클라이언트 속도와 무관하게** 계속 읽어 최신 프레임 한 장만 남긴다.

        ⭐ 이 태스크가 실시간의 핵심이다. `relay`가 `yield`에서 막혀 있어도 이쪽은 계속 도니
        젯슨 쪽 소켓 버퍼가 안 고이고, 늦은 장은 슬롯에서 덮어써져 사라진다.

        ⚠ **경계 하나로는 프레임이 안 끝난다.** 다음 경계가 와야 앞 장이 완성된다. 그래서 버퍼
        안에서 경계를 둘 찾아 그 사이를 한 장으로 본다. 마지막 경계 뒤는 아직 오는 중이라 남긴다.

        ⚠ 첫 경계 앞 구간(전문 preamble)은 버린다 — 프레임이 아니다.
        """
        buf = bytearray()
        try:
            async for chunk in self._upstream.aiter_raw():
                buf.extend(chunk)
                last = buf.rfind(boundary)
                if last <= 0:
                    continue
                # ⚠ **마지막 경계 바로 앞 경계**를 찾아야 한 장이다. `find`(맨 앞)를 쓰면 그
                #    사이에 낀 장이 전부 한 덩어리로 묶여 나가서, 화면이 옛 장면부터 차례로
                #    보게 된다(=실시간이 아니다). 조각 하나에 여러 장이 실려 오는 판에서 밟는다.
                prev = buf.rfind(boundary, 0, last)
                if prev < 0:
                    # 경계가 하나뿐이다 — 아직 한 장이 안 닫혔다. 다음 조각을 기다린다.
                    first = buf.find(boundary)
                    if first > 0:
                        del buf[:first]  # preamble만 걷어낸다
                    continue
                # 이번에 건너뛴 장 수 — 버퍼에 쌓여 있던 완성분과, 클라이언트가 아직 안
                # 가져간 앞 장을 같이 센다.
                self._dropped += buf.count(boundary, 0, prev)
                if self._latest_frame is not None:
                    self._dropped += 1
                self._latest_frame = bytes(buf[prev:last])
                self._latest_at = dt.datetime.now(dt.timezone.utc)
                del buf[:last]
                self._frame_ready.set()
                # ⚠ **꺼내는 자리가 아니라 넣는 자리에서 흘린다.** 화면이 느려 장을 안
                # 가져가는 동안에도 "영상은 흐르고 있다"를 알려야, 화면이 그 구간을
                # 끊김으로 오판하지 않는다.
                await _push_frame_time(self._latest_at)
        except (httpx.HTTPError, httpx.StreamError) as exc:
            logger.info(
                "카메라 상류가 끊겼다(펌프) — %s: %s (%s)",
                type(exc).__name__,
                exc,
                self._viewer,
            )
        except asyncio.CancelledError:
            raise
        else:
            # ⭐ **상류가 정상으로 끝났으면 버퍼에 남은 마지막 장을 내보낸다.**
            # 위 루프는 "다음 경계가 와야 앞 장이 닫힌다"로 자르므로, 마지막 장은 뒤에 올
            # 경계가 없어 늘 버퍼에 남는다. 여기서 안 흘리면 **매 스트림이 마지막 한 장을
            # 통째로 잃는다** — 젯슨이 잠깐 끊겼다 붙는 판에서 그 장이 화면의 마지막 그림이라
            # 눈에 띈다(2026-08-05 `test_agent_gets_frames`가 이 자리를 잡았다).
            if len(buf) > len(boundary) and buf.startswith(boundary):
                self._latest_frame = bytes(buf)
                self._latest_at = dt.datetime.now(dt.timezone.utc)
                # ⚠ **이 갈래에도 넣는다.** 위 루프만 챙기면 상류가 한 장만 보내고 끊긴
                # 판에서 시각이 한 번도 안 나간다 — 화면이 그림은 받았는데 지연은 "모름"이다.
                await _push_frame_time(self._latest_at)
        finally:
            self._pump_done = True
            # ⚠ 깨워 주지 않으면 `_newest_frames`가 상류가 끝난 줄 모르고 계속 기다린다.
            self._frame_ready.set()

    async def _newest_frames(self) -> AsyncIterator[bytes]:
        """슬롯에서 최신 프레임을 꺼내 내보낸다. 없으면 새 장이 올 때까지 기다린다."""
        while True:
            try:
                await asyncio.wait_for(self._frame_ready.wait(), FRAME_WAIT_TICK_SEC)
            except (TimeoutError, asyncio.TimeoutError):
                # 상류가 조용한 구간이다. 끝났는지만 보고 다시 기다린다.
                if self._pump_done and self._latest_frame is None:
                    return
                continue
            self._frame_ready.clear()
            frame, self._latest_frame = self._latest_frame, None
            at, self._latest_at = self._latest_at, None
            if frame is not None:
                # 시각은 **꺼내는 자리에서** 찍는다. 넣을 때 미리 끼우면 덮어써질 장에도
                # 헛일을 하고, 여기서 하면 실제로 나가는 장만 한 번 손댄다.
                yield _stamp_frame(frame, at) if at is not None else frame
            elif self._pump_done:
                return


# 이 창구의 자물쇠. 플래그와 무관하게 늘 판정한다 — 모듈 머리 참고.
_gate = require_agent_always


@router.get(
    "/stream",
    summary="젯슨 카메라 MJPEG 프록시",
    response_class=StreamingResponse,
    # ⚠ 이 선언이 없으면 OpenAPI 200에 content가 **통째로 빈다.** `StreamingResponse`는
    #    `media_type`이 None이라 FastAPI가 기본 본문을 못 만들고, 프론트는 무엇이 오는지를
    #    스펙으로 알 길이 없다(실측: responses가 `{"description": ...}` 한 줄뿐이었다).
    responses={
        200: {
            "description": "MJPEG 스트림. Content-Type은 상류가 준 값을 그대로 흘린다.",
            "content": {STREAM_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def stream_camera(actor: AuthActor | None = Depends(_gate)) -> StreamingResponse:
    """로그인한 요원에게 젯슨 미리보기를 그대로 흘린다.

    ⭐ **상류에 먼저 붙어 보고 나서** 응답을 만든다. StreamingResponse를 돌려준 뒤에는 상태줄이
    이미 200으로 나가서 502로 바꿀 길이 없다 — 젯슨이 꺼져 있으면 화면이 영원히 빈 채로 도는
    자리다. 헤더까지 받아 본 뒤에 200이 아니면 여기서 502로 끊는다.

    ⚠ 응답을 넘기기 **전에** 나가는 갈래는 정리를 부를 자리가 여기 하나뿐이다. `relay`의
    `finally`도 background 태스크도 응답이 만들어진 뒤에야 생기기 때문이다. 그래서 아래
    `except BaseException` 한 곳에서 자리와 잡다 만 커넥션을 같이 되돌린다 — 특히 취소는
    `except Exception`에 안 걸려서, 502 갈래에만 정리를 두면 취소 갈래가 통째로 샌다.
    """
    _note_worker_assumption()
    _note_stream_target()
    url = _setting("camera_stream_url", DEFAULT_STREAM_URL)
    limit = int(_setting("camera_max_viewers", DEFAULT_MAX_VIEWERS))
    viewer = actor.username if actor else "unknown"

    if not _try_acquire_slot(limit):
        logger.warning("카메라 동시 접속 상한(%d) 초과 — %s를 돌려보낸다", limit, viewer)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_TOO_MANY_VIEWERS,
            headers={"Retry-After": "5"},
        )

    client: httpx.AsyncClient | None = None
    upstream: httpx.Response | None = None
    try:
        timeout = httpx.Timeout(
            connect=float(_setting("camera_connect_timeout_sec", DEFAULT_CONNECT_TIMEOUT_SEC)),
            read=float(_setting("camera_read_timeout_sec", DEFAULT_READ_TIMEOUT_SEC)),
            write=float(_setting("camera_connect_timeout_sec", DEFAULT_CONNECT_TIMEOUT_SEC)),
            pool=float(_setting("camera_connect_timeout_sec", DEFAULT_CONNECT_TIMEOUT_SEC)),
        )
        client = _new_client(timeout)
        try:
            upstream = await client.send(client.build_request("GET", url), stream=True)
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 502 한 갈래다
            logger.warning("카메라 상류에 못 붙었다 — %s: %s", type(exc).__name__, exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_UPSTREAM_DOWN
            ) from exc
        if upstream.status_code != status.HTTP_200_OK:
            logger.warning("카메라 상류가 %d를 냈다 — 502로 끊는다", upstream.status_code)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_UPSTREAM_DOWN
            )
        # ⭐ 200이라고 다 MJPEG는 아니다. 상류 주소에서 경로(`/stream`)를 빼면 젯슨은 **안내
        # HTML을 200으로** 준다(`tag_localizer_cv.py` `_start_preview`의 루트 갈래). 그때
        # 여기서 안 끊으면 `text/html`이 200으로 흘러 화면엔 깨진 이미지만 뜨고, 502를
        # 기다리는 사람은 원인을 못 찾는다 — 배선 실수가 조용해지는 자리라 여기서 자른다.
        #
        # ⚠ 헤더가 **아예 없는** 판은 통과시킨다. 아래 응답이 그 경우에 쓰는 기본값
        # (`FALLBACK_CONTENT_TYPE`)을 이미 들고 있어서, 여기서 막으면 그 갈래가 죽는다.
        upstream_type = upstream.headers.get("content-type", "").strip()
        if upstream_type and not upstream_type.lower().startswith("multipart/"):
            logger.warning(
                "카메라 상류가 200인데 MJPEG가 아니다 — %r. 상류 주소에 /stream 경로가"
                " 빠졌는지 확인해라. 502로 끊는다",
                upstream_type[:80],
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_UPSTREAM_DOWN
            )
    except BaseException:
        # 스트림이 시작 못 했으니 `relay`의 finally도 background도 안 돈다 — 여기가 유일한
        # 정리 자리다. `Exception`이 아니라 `BaseException`인 이유는 취소 때문이다:
        # `client.send`에 멈춰 있을 때 오는 `CancelledError`는 위 `except Exception`에 안 걸려서,
        # 502 갈래에만 정리를 두면 젯슨 쪽 커넥션이 그대로 남는다(연결 타임아웃 3초 동안 탭을
        # 닫으면 바로 밟는다).
        #
        # 취소를 존중하는 것과 좀비를 막는 것을 둘 다 세우는 방법은 `_aclose_quietly`가 이미
        # 쥐고 있다 — 닫기 한 번을 shield로 지켜 끝내고, 취소는 그대로 다시 던진다.
        #
        # ⭐ 순서는 `_UpstreamLink.aclose`와 **같은 계약**이다 — 상류를 닫고 나서 자리를
        #    돌려준다. 먼저 돌려주면 닫는 동안(유예 최대 `CLOSE_GRACE_SEC`) 다음 요청이 그
        #    자리를 잡아 젯슨 쪽 실제 커넥션이 상한+1이 된다. 반납이 `finally`인 이유도 같다:
        #    `_aclose_quietly`가 취소를 다시 던지므로 그냥 뒤에 두면 취소 갈래가 자리를 문다.
        try:
            await _aclose_quietly(*[c for c in (upstream, client) if c is not None])
        finally:
            _release_slot()
        raise

    logger.info("카메라 스트림 시작 — %s (지금 %d명)", viewer, active_viewers())
    link = _UpstreamLink(client, upstream, viewer)
    return StreamingResponse(
        link.relay(),
        media_type=upstream.headers.get("content-type", FALLBACK_CONTENT_TYPE),
        # 끊김이 나면 starlette이 본문 태스크를 취소하고 이걸 부른다. 제너레이터의 finally가
        # 늦게 도는 갈래를 이게 메운다(`_UpstreamLink.relay` 주석).
        background=BackgroundTask(link.aclose),
        headers={
            "Cache-Control": "no-store",
            # ⚠ nginx는 프록시 응답을 기본으로 버퍼링한다. 이 헤더가 없으면 프레임이 버퍼에
            # 고여 화면이 몇 초씩 밀린다(배포 앞단이 nginx다 — deploy/nginx-c207-api.conf).
            "X-Accel-Buffering": "no",
        },
    )
