"""시연 도구 창구 — 관제 화면이 발표 장면을 서버에 실제로 남긴다.

    POST /api/demo/gate-pass   관리자   시연용 게이트 판정 한 건

## 왜 만들었나 (2026-08-05 프론트 22차 · 사용자 확정)

시연 도구 버튼 넷(정상 통과·무단 통과·한쪽 감지·퇴장)이 **화면에만 줄을 만들고 서버로는
아무것도 안 보냈다.** 새로고침해서 스냅샷을 다시 받으면 통째로 사라졌다. 사용자가
"기록이 남아야 한다"고 두 번 짚은 자리다 — 리허설에서 장면을 쌓아 두고 다시 열어도
남아 있어야 한다.

## ⭐ 판정을 우회하지 않는다

이 창구는 `verdict`를 **받지 않는다.** 대신 그 판정이 나오도록 **입력을 만들어** 실제
인입 경로(`issue_credit`·`evaluate_pass`)에 그대로 넣는다.

| 버튼 | 만드는 입력 |
| --- | --- |
| `normal` | 태깅 한 건(크레딧 발급) → 빔 둘 다 찍힌 통과 |
| `untagged` | 태깅 없이 빔 둘 다 찍힌 통과 |
| `beam_incomplete` | 빔 하나만 찍힌 통과 |
| `exit` | 방향이 반대(B→A)인 통과 |

⛔ **`verdict`를 직접 저장하면 판정 규칙이 두 벌이 된다.** 리허설에서 본 것과 발표
당일에 볼 것이 갈라지고, 그 차이는 발표장에서야 드러난다. 그래서 실제 판정기를 그대로
탄다 — 크레딧 소비도, 경고 생성도, 화면 방송도, 매터모스트 카드도 같은 길이다.

⚠ 그래서 `normal`을 눌러도 **판정이 `normal`이 아닐 수 있다.** 크레딧이 이미 소비된
상태이거나 쿨다운 창에 걸리면 다른 결과가 나온다. 그게 진짜 거동이고, 응답에 실제
판정을 실어 화면이 그대로 보여 준다.

## 지우기

만든 행은 전부 `is_demo=true`다(마이그레이션 0014). `POST /api/admin/purge-demo-data`가
지금은 표를 통째로 비우지만, 진짜 자료가 쌓인 뒤에는 이 표시로 골라 지울 수 있다.

## 권한

**관리자 이상만**(`require_admin_always`). 시연 자료를 만드는 것은 실제 통과 기록을
만드는 것과 같아서, 명부 수정과 같은 급으로 잠근다. 플래그와 무관하게 늘 판정한다 —
이 창구는 로그인과 함께 생긴 자리라 보존할 기존 거동이 없다.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.db import get_db
from app.routers.ingest import ingest_gate_pass_event, ingest_tagging_event
from app.schemas import (
    DemoGatePassIn,
    DemoGatePassOut,
    DemoVerdict,
    Direction,
    GatePassEventIn,
    PassStatus,
    TaggingEventIn,
)

logger = logging.getLogger("c207.demo")

router = APIRouter(prefix="/api/demo", tags=["demo"])

# 시연으로 만든 행이 달고 가는 기기 이름. `is_demo` 칸이 진짜 표식이고 이쪽은 로그·화면에서
# "어디서 왔나"를 읽는 값이다. 실기기 이름과 안 겹치게 접두를 둔다.
DEMO_DEVICE_ID = "demo-console"
# 시연 태깅이 쓰는 카드 UID. 실제 카드와 안 겹치게 접두를 두고, 뒤에 난수를 붙여
# 크레딧이 매번 새로 발급되게 한다(같은 UID를 재사용하면 앞 크레딧에 걸린다).
DEMO_TAG_PREFIX = "DEMO"


def _event_id(kind: str) -> str:
    """멱등 열쇠. 시연은 같은 버튼을 여러 번 누르는 것이 정상이라 매번 새로 뜬다."""
    return f"demo-{kind}-{secrets.token_hex(8)}"


@router.post("/gate-pass", response_model=DemoGatePassOut)
async def demo_gate_pass(
    body: DemoGatePassIn,
    actor: auth.AuthActor | None = Depends(auth.require_admin_always),
    session: AsyncSession = Depends(get_db),
) -> DemoGatePassOut:
    """시연용 게이트 판정 한 건을 **실제 인입 경로로** 만든다.

    응답의 `verdict`는 **서버가 실제로 내린 판정**이다. 요청한 갈래와 다를 수 있다
    (위 모듈 docstring — 크레딧·쿨다운 상태에 따라 갈린다). 화면은 그 값을 그대로 쓴다.
    """
    now = dt.datetime.now(dt.timezone.utc)
    gate_no = body.gate_no
    tag_id: str | None = None
    tagging_event_id: str | None = None

    # ── 정상 통과 — 태깅을 먼저 심어 크레딧을 만든다 ──────────────────────
    if body.verdict is DemoVerdict.normal:
        tag_id = f"{DEMO_TAG_PREFIX}{secrets.token_hex(6).upper()}"
        tagging_event_id = _event_id("tag")
        await ingest_tagging_event(
            TaggingEventIn(
                event_id=tagging_event_id,
                device_id=DEMO_DEVICE_ID,
                gate_no=gate_no,
                tag_id=tag_id,
                # ⚠ 통과보다 **앞선** 시각이어야 크레딧이 그 통과를 덮는다.
                observed_at=now - dt.timedelta(seconds=2),
            ),
            session=session,
            is_demo=True,
        )

    # ── 통과 한 건 ───────────────────────────────────────────────────────
    direction = (
        Direction.B_TO_A if body.verdict is DemoVerdict.exit else Direction.A_TO_B
    )
    if body.verdict is DemoVerdict.beam_incomplete:
        # 한쪽만 찍힌다. B 빔이 없으니 판정기가 미완성으로 읽는다.
        beam_a, beam_b = now, None
        status = PassStatus.incomplete
    else:
        beam_a, beam_b = now - dt.timedelta(milliseconds=300), now
        status = PassStatus.complete

    ack = await ingest_gate_pass_event(
        GatePassEventIn(
            event_id=_event_id("pass"),
            device_id=DEMO_DEVICE_ID,
            gate_no=gate_no,
            direction=direction,
            status=status,
            beam_a_ts=beam_a,
            beam_b_ts=beam_b,
            observed_at=now,
            tag_id=tag_id,
        ),
        session=session,
        is_demo=True,
    )

    logger.info(
        "시연 판정을 만들었다 — 요청 %s / 실제 %s (gate=%s, actor=%s)",
        body.verdict.value, ack.verdict, gate_no,
        actor.username if actor else "(플래그 off)",
    )
    return DemoGatePassOut(
        requested=body.verdict,
        verdict=ack.verdict,
        event_id=ack.event_id,
        stored=ack.stored,
        tagging_event_id=tagging_event_id,
        gate_no=gate_no,
        is_demo=True,
    )
