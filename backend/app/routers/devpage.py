"""개발자용 텔레메트리 페이지 (지라 S15P11C207-157).

FastAPI가 정적 파일 하나를 직접 서빙한다. 프론트 프레임워크를 안 쓴 이유는 프론트 스택이
팀 확정 대기(동인 목업 package.json 확인 전)라, 확정 전에 스택을 하나 더 늘리지 않으려는
것이다. 확정 뒤 보안 요원 페이지는 그 스택으로 짜고 이 페이지는 개발용으로 남는다.

페이지는 /ws/dashboard를 구독해 인입 이벤트를 그대로 그린다. 서버는 데이터를 새로
만들지 않는다 — 이미 있는 대시보드 브로드캐스트를 화면에 붙이는 게 전부다.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from app.auth import require_admin_always

router = APIRouter(tags=["dev"])

# 임포트 시점에 한 번 읽는다. 페이지가 한 장이라 캐시·템플릿 엔진이 필요 없다.
_PAGE_HTML = (
    Path(__file__).resolve().parent.parent / "static" / "telemetry.html"
).read_text(encoding="utf-8")


@router.get(
    "/dev/telemetry",
    response_class=HTMLResponse,
    dependencies=[Depends(require_admin_always)],
)
async def telemetry_page() -> HTMLResponse:
    """텔레메트리 페이지. **관리자 전용**이다(로그인 범위 정본 §2 — 요원·팀장은 못 본다).

    ## 자물쇠가 하나로 줄었다 (2026-08-06 사용자 결정)

    예전에는 겹이 둘이었다 — nginx `auth_basic`(계정 `c207dev`)과 앱 `require_admin`이다.
    **사용자가 "기존 계정을 지우고 명부에서 관리자 이상으로 들어가게 하자"로 정해서 앞의
    겹을 걷었다.** 별도 계정을 하나 더 관리하는 값이 컸다 — 비밀번호가 문서에 없어(일부러
    안 적었다) 실제로 못 들어가는 일이 났고, 역할이 바뀌어도 그 파일은 안 따라간다.

    ⭐ **그래서 게이트를 `require_admin_always`로 올렸다.** 예전 `require_admin`은
    `AUTH_REQUIRE_LOGIN=false`면 **판정을 아예 안 한다** — nginx 겹을 걷은 채로 그 플래그를
    되돌리면 이 페이지가 익명에게 통째로 열린다. 여기엔 신원 입력 상자가 있어서, 열 수 있는
    사람이 상위 보고선 채널로 가짜 신원 카드를 밀어 넣는다.

    ⚠ **대가가 하나 있다 — 플래그를 되돌리면 우리도 못 들어간다.** 롤백은 비상 상황이고
    그때 이 페이지가 급하지 않다고 봤다. 급하면 플래그를 다시 켜면 된다.

    ⚠ `always=True` 게이트는 원래 "로그인과 함께 새로 생긴 창구 전용"이고 기존 창구에는
    쓰지 말라고 적혀 있다(`auth.py`). 그 금지는 **롤아웃 1~4단계 중** 기존 거동을 안 깨뜨리려는
    약속이고, 5단계(`AUTH_REQUIRE_LOGIN=true`)가 끝난 지금은 거동이 같다. 그리고 이 자리는
    기기·화면이 부르는 API가 아니라 사람이 여는 화면 한 장이라 계약이 걸린 곳도 아니다.

    ⚠ 게이트는 이 HTML 한 장에만 걸린다. 페이지가 읽는 값은 `/ws/dashboard`와
    `/api/stats/*`에서 오고, 그 둘은 각자 요원 게이트를 따로 쥔다 — 여기 게이트가 그쪽까지
    덮는다고 읽으면 안 된다.
    """
    return HTMLResponse(_PAGE_HTML)
