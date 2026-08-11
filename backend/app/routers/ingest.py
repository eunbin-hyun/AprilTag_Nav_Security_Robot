"""인입 API 3종. 전부 X-API-Key 인증 + event_id 멱등.

멱등은 event_id UNIQUE + INSERT ... ON CONFLICT DO NOTHING RETURNING id로 건다.
- 새 행이 실제로 생겼을 때만(RETURNING이 값을 줄 때만) 판정·크레딧 소비·브로드캐스트로 넘어간다.
  중복 재시도가 크레딧 소비·알림을 두 번 먹지 않게 하는 자리다.
- 400·401은 재시도 없음(스키마·인증), 409도 성공 취급(재시도 없음)이다.
- ⚠ **409는 실제로 나간다.** 같은 event_id를 그냥 다시 보낸 재시도는 200으로 접히지만,
  같은 event_id에 **다른 본문**이 오면(payload mismatch) `_reject_payload_conflict`가 409를
  던진다. 조용히 접으면 서로 다른 사건이 한 행이 되기 때문이다.
- ⚠ 기기 쪽 재시도는 이 계약과 **맞는다**(2026-08-04 실물 재측정). 라파
  `hardware/rpi/gate_server.py:790~802` sender_worker는 4xx를 경고만 찍고 버리고, 5xx·네트워크
  오류만 백오프 재시도한다. `tools/dummy_publisher.py`도 400·401은 안 재시도하고 5xx·무응답만
  다시 보낸다. 그러니 409·422 한 건이 그 기기 큐를 막지는 않는다.
- ⚠ 다만 **5xx는 막는다.** 라파 전송 스레드가 하나라, 500이 계속 나는 이벤트 한 건이 백오프
  재시도를 도는 동안 뒤 이벤트가 전부 밀린다. 500이 날 수 있는 갈래를 422로 접는 게 값이
  있는 이유가 이거다(예전 주석은 이 자리를 "4xx도 무한 재시도"라고 반대로 적어 뒀었다).

태깅 인입은 크레딧을 발급하고, 빔 통과 인입은 크레딧 상태머신으로 판정한다(2단계).
셔틀은 판정이 없어 단순 멱등 적재만 하고, 그 몸통은 `app/shuttle_call.py`에 있다 —
화면 가상 호출(`POST /api/shuttle-calls`)이 같은 함수를 타서 계약이 안 갈라지게 한다.

## 로그 자리 (2026-07-30)

이 파일엔 로거가 없다. 예전에는 `c207.ingest` 로거를 여기 두고 셔틀 통지 실패·재전송 복구·
본문 충돌을 여기서 찍었는데, 그 갈래가 전부 몸통 `app/shuttle_call.py`로 올라가면서 로그도
**`c207.shuttle`**로 옮겨갔다. 셔틀 인입 로그를 찾을 때 `c207.ingest`로 걸러도 안 나온다.
태깅·빔 통과 갈래는 로그를 남기지 않는다(판정은 DB Alert 행과 WS 메시지로 드러난다).
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import gate_sensing
from app.config import get_settings
from app.credit.state_machine import evaluate_pass, issue_credit
from app.db import get_db
from app.notify import send_untagged_alert
from app.schemas import (
    GatePassEventIn,
    GatePassIngestAck,
    GateSensingAck,
    GateSensingIn,
    IngestAck,
    ShuttleArrivalIn,
    ShuttleIngestAck,
    TaggingEventIn,
)
from app.routers.query import staff_owner_by_tag
from app.security import require_api_key
from app.shuttle_call import SHUTTLE_NOTIFY_HEADER, record_shuttle_arrival
from app.ws import make_envelope, manager

router = APIRouter(prefix="/api", tags=["ingest"], dependencies=[Depends(require_api_key)])

# SHUTTLE_NOTIFY_HEADER는 셔틀 몸통(app/shuttle_call.py)에 정의돼 있고 여기서 이름만 다시
# 내보낸다. 헤더를 찍는 자리가 몸통이라 정의도 거기 있어야 두 창구가 같은 값을 쓴다.
__all__ = ["SHUTTLE_NOTIFY_HEADER", "router"]


def _reject_payload_conflict(event_id: str, fields: list[str]) -> None:
    """같은 event_id에 다른 본문이 온 요청을 409로 거절한다.

    인입 계약(모듈 머리 주석)이 409를 "성공 취급, 재시도 없음"으로 못박아 뒀다. 조용히 버리면
    서로 다른 사건이 한 행으로 접히고 기기 쪽에서 알아챌 방법이 없다.

    ⚠ 라파 sender_worker는 409를 재시도하지 않고 버린다(`gate_server.py:790~802`, 2026-08-04
    실물 재측정). 그 기기 큐가 막히지는 않는다는 뜻이다 — 대신 그 이벤트는 기기 쪽 표준출력에
    "버림 409"로만 남고 영영 안 올라온다. 그래서 이 자리는 "재시도가 막힌다"가 아니라
    "event_id를 새로 발급하지 않으면 사건 하나가 조용히 사라진다"가 대가다.

    ⚠ 셔틀 갈래는 이 함수를 안 쓴다. 셔틀 몸통이 `app/shuttle_call.py`로 올라가면서 같은
    모양의 `reject_payload_conflict`가 거기 생겼다(ingest가 그 파일을 부르는 방향이라
    거꾸로는 못 부른다). 응답 본문 모양이 갈라지면 안 되니 둘 중 하나를 고치면 나머지도
    같이 고쳐라.
    """
    raise HTTPException(
        status_code=409,
        detail={
            "reason": "event_id_payload_mismatch",
            "event_id": event_id,
            "conflicting_fields": fields,
            "message": "같은 event_id로 다른 본문이 들어왔습니다. event_id를 새로 발급해 주세요.",
        },
    )


def _with_room(payload: dict, room_id: str | None) -> dict:
    """[초안·팀 확정 대기] 안건① 메시지에 room_id 칸을 붙인다. 플래그가 꺼져 있으면 안 붙인다.

    dev의 WS 메시지 계약 시험(test_dashboard_ws.py)이 종류마다 칸 집합을 정확히 못박고 있고,
    화면도 "안 실린 칸은 안 덮는다" 규칙으로 읽는다. 초안이 칸을 무조건 하나 더 실으면
    플래그가 꺼진 상태에서도 계약이 달라져 "기본값이면 dev와 거동 같음"이 깨진다.
    그래서 룸 범위를 실제로 쓸 때만 칸이 생기게 한다 — 확정되면 이 가드를 걷어내고
    칸을 상시 계약으로 올린다.
    """
    if get_settings().credit_scope_room:
        return {**payload, "room_id": room_id}
    return payload


@router.post("/gate-sensing-state", response_model=GateSensingAck)
async def ingest_gate_sensing_state(body: GateSensingIn) -> GateSensingAck:
    """라파이가 터치로 센서를 켜고 끌 때 그 사실을 알린다.

    ⭐ **DB를 안 탄다.** 마지막 값만 메모리에 들고 대시보드 스냅샷에 싣는다 — 재시작하면
    "모른다"로 돌아가는 것이 맞다는 판단이다(`app/gate_sensing` 모듈 머리 참고).

    ⚠ **이 창구가 없어도 라파이는 안 깨진다.** 못 보내면 화면이 "센서 상태를 아직 못
    받았습니다"로 그릴 뿐이라, 그쪽이 붙이기 전에도 나머지 인입은 그대로 돈다.

    ⭐ 08-08 교차 검증 — **받은 즉시 대시보드에도 흘린다.** 예전에는 30초 스냅샷이 유일한
    인입이라, 요원이 터치로 센서를 깨워도 화면 빨간 줄("센서 꺼짐 · 터치로 켜 주세요")이
    최대 30초 늦게 풀렸다. 싣는 값은 기기 하나가 아니라 `snapshot()`(전체를 모은 값)이다 —
    화면이 두 계산을 들면 스냅샷과 이 봉투가 서로 다른 말을 한다.
    """
    gate_sensing.record(body.device_id, body.sensing)
    at = gate_sensing.snapshot_at()
    await manager.broadcast(
        make_envelope(
            "gate_sensing",
            {
                "sensing": gate_sensing.snapshot(),
                "at": at.isoformat() if at else None,
            },
        )
    )
    return GateSensingAck(device_id=body.device_id, sensing=body.sensing)


@router.post("/tagging-events", response_model=IngestAck)
async def ingest_tagging_event(
    body: TaggingEventIn,
    session: AsyncSession = Depends(get_db),
    *,
    # ⭐ 시연 창구(`routers/demo.py`)가 이 함수를 그대로 부른다. 기기 인입은 늘 거짓이다 —
    # 요청 본문에서 안 받아서 밖에서 조작할 수 없다.
    is_demo: bool = False,
) -> IngestAck:
    result = await issue_credit(
        session,
        is_demo=is_demo,
        event_id=body.event_id,
        device_id=body.device_id,
        gate_no=body.gate_no,
        room_id=body.room_id,
        boot_id=body.boot_id,
        tag_id=body.tag_id,
        observed_at=body.observed_at,
    )
    if result.stored:
        await manager.broadcast(
            make_envelope(
                "tagging_event",
                _with_room(
                    {
                        "event_id": body.event_id,
                        "gate_no": body.gate_no,
                        "tag_id": body.tag_id,
                        "observed_at": body.observed_at.isoformat(),
                    },
                    body.room_id,
                ),
            )
        )
    return IngestAck(event_id=body.event_id, stored=result.stored, duplicate=not result.stored)


@router.post(
    "/gate-pass-events",
    response_model=GatePassIngestAck,
    # 어긋남 사유는 있을 때만 싣는다. 평소 응답 모양이 dev 계약 그대로 남는다
    # (`GatePassIngestAck` 주석 — 그 이유로 IngestAck을 직접 안 늘렸다).
    response_model_exclude_none=True,
)
async def ingest_gate_pass_event(
    body: GatePassEventIn,
    session: AsyncSession = Depends(get_db),
    # ⭐ **매터모스트 카드를 응답 뒤로 미는 자리다**(2026-08-09 백지검토 수리 · 아래 발사
    #   지점 주석에 까닭을 적었다). FastAPI 가 타입만 보고 꽂아 준다.
    #
    #   ⚠ **기본값 `None` 이 일부러다.** `routers/demo.py:127` 이 이 함수를 창구가 아니라
    #   순수 파이썬으로 직접 부른다 — 필수 인자로 두면 그 갈래가 TypeError 로 죽는다.
    #   기본값을 줘도 HTTP 로 들어올 때는 진짜 객체가 꽂히는 것을 배포에 걸린 판
    #   (`fastapi==0.115.6`)으로 실측했다: 창구 호출은 `BackgroundTasks`, 직접 호출은 `None`,
    #   OpenAPI 파라미터에도 안 샌다(특수 타입이라 요청 칸으로 안 센다).
    background: BackgroundTasks = None,  # type: ignore[assignment]
    *,
    # ⭐ 시연 창구가 부를 때만 참이다(위 태깅 인입과 같은 규칙 — 본문에서 안 받는다).
    is_demo: bool = False,
) -> GatePassIngestAck:
    ev = await evaluate_pass(
        session,
        is_demo=is_demo,
        event_id=body.event_id,
        device_id=body.device_id,
        gate_no=body.gate_no,
        room_id=body.room_id,
        boot_id=body.boot_id,
        direction=body.direction.value if body.direction else None,
        status=body.status.value if body.status else None,
        beam_a_ts=body.beam_a_ts,
        beam_b_ts=body.beam_b_ts,
        observed_at=body.observed_at,
        # 기기가 그 통과에서 실제로 읽은 카드 UID(F22). 판정에는 안 쓰고 행에 기록만 한다 —
        # 이 줄이 빠지면 스키마 칸이 몸통까지 안 가서 미태깅 행의 UID가 영영 NULL이다.
        tag_id=body.tag_id,
    )
    if ev.payload_conflict:
        _reject_payload_conflict(body.event_id, ev.conflicting_fields)
    if ev.stored:
        # 판정 결과를 대시보드로 밀어낸다(정상 통과도 화면에 찍힌다 — 시연 ③).
        #
        # 어긋난 주장 사유(-243)는 **있을 때만** 칸을 붙인다. dev의 WS 계약 시험
        # (`test_dashboard_ws.py`)이 종류마다 칸 집합을 통째로 못박고 있어서 무조건 실으면
        # 평소 메시지 계약이 달라진다. `_with_room`이 room_id를 다루는 규칙과 같다.
        conflict_field = (
            {"beam_conflict": ev.beam_conflict} if ev.beam_conflict else {}
        )
        # ⭐ 카드 주인 이름. 스냅샷 `recent_events`와 **같은 이름·같은 규칙**으로 싣는다
        # (`query.staff_owner_by_tag`). 한쪽만 실으면 새로고침 전후로 이름이 나타났다
        # 사라져서, 프론트가 11차 §4-2에서 "둘 다 실어 주셔야 합니다"라고 못박은 자리다.
        #
        # ⚠ 학번을 같이 싣는 것은 2026-08-05 사용자 확정이다
        # (`schemas.EventOut.staff_name` 주석에 근거를 적어 뒀다).
        owners = await staff_owner_by_tag(session, {ev.tag_id} if ev.tag_id else set())
        owner = owners.get(ev.tag_id, (None, None)) if ev.tag_id else (None, None)
        await manager.broadcast(
            make_envelope(
                "gate_pass_event",
                _with_room(
                    {
                        "event_id": body.event_id,
                        # 이 통과가 저장된 행의 PK. **뒤따르는 `untagged_alert`의 `source_id`와
                        # 같은 값이다** — 둘을 잇는 유일한 열쇠라 화면이 "직전 메시지"라는
                        # 도착 순서 추측을 안 해도 된다. 순서 추측은 게이트가 늘거나 통과 두
                        # 건이 동시에 들어오면 엉뚱한 통과에 경고를 붙인다.
                        # 이름은 `Credit.consumed_by_pass_id` 컬럼과 `PassEvaluation.pass_id`가
                        # 이미 쓰는 낱말을 그대로 가져왔다(README도 이 이름으로 예고해 뒀다).
                        # room_id·beam_conflict와 달리 **늘 싣는다** — 있고 없고로 갈리면
                        # 화면이 칸의 유무를 먼저 검사해야 한다. `ev.stored` 안이라 늘 int다.
                        "pass_id": ev.pass_id,
                        "gate_no": body.gate_no,
                        "direction": body.direction.value if body.direction else None,
                        "status": body.status.value if body.status else None,
                        "verdict": ev.verdict,
                        "matched_tagging_event_id": ev.matched_tagging_event_id,
                        # 태운 크레딧의 카드 UID(S15P11C207-241). room_id·beam_conflict와
                        # 달리 **늘 싣는다** — 화면이 "정상 통과"에 이름을 붙이려면 칸이
                        # 있고 없고가 아니라 값이 null인지로 갈려야 한다. 크레딧을 안 태운
                        # 판정에서는 null이다.
                        "tag_id": ev.tag_id,
                        # 명부에 없는 카드면 둘 다 null이다. 화면이 그때 "명부 밖 카드"로 그린다.
                        "staff_name": owner[0],
                        "staff_student_no": owner[1],
                        "low_confidence": ev.low_confidence,
                        # 기기 1차 판단은 참고값이라 판정과 나란히 싣기만 한다(DB엔 안 남는다).
                        # verdict 자리에 섞으면 화면이 서버 판정과 기기 판정을 못 가른다.
                        "device_result": body.result,
                        **conflict_field,
                    },
                    body.room_id,
                ),
            )
        )
        # 미태깅이면 팝업(untagged_alert)과 Mattermost 카드가 나간다 — 시연 ④.
        # 커밋 뒤 호출이라 트랜잭션을 잡지 않는다. Mattermost는 기본 비활성이라 no-op.
        #
        # ⚠ 판정을 채널마다 따로 본다. 예전엔 `if ev.notify:` 하나가 둘을 같이 감싸서, 쿨다운
        # 한 번에 화면 팝업과 상위 보고 카드가 한꺼번에 빠졌다. Alert 행은 이제 언제나 남으니
        # (state_machine 참고) 여기서 빠지는 건 "울림"뿐이고 사건 기록·신원 입력 대상은 산다.
        if ev.notify_dashboard:
            await manager.broadcast(
                make_envelope(
                    "untagged_alert",
                    _with_room(
                        {
                            "alert_id": ev.alert_id,
                            "gate_no": body.gate_no,
                            "source_type": "gate_pass",
                            "source_id": ev.pass_id,
                            "low_confidence": ev.low_confidence,
                        },
                        body.room_id,
                    ),
                )
            )
        if ev.notify_chat:
            # ⛔ **응답을 돌려준 뒤에 쏜다**(2026-08-09 백지검토 수리). 예전에는 여기서 그대로
            #   `await` 했는데, 매터모스트 상한(`notify.MATTERMOST_TIMEOUT_S = 3.0`)과 라파
            #   전송 상한(`hardware/rpi/gate_server.py:53 HTTP_TIMEOUT_S = 3.0`)이 **같은 값**
            #   이다. 채널이 느리면 서버는 저장까지 다 끝냈는데 기기는 타임아웃으로 읽고
            #   백오프 재시도로 넘어간다. 라파 전송이 스레드 하나짜리 큐라
            #   (`gate_server.sender_worker`) 그동안 뒤 이벤트가 전부 밀린다.
            #
            #   ⭐ 같은 저장소의 신원 카드가 이미 이 모양이다 —
            #   `routers/query.py:1129 background.add_task(send_identify_report, ...)`.
            #   README(backend/README.md:188)도 "응답을 돌려준 뒤에 부른다"를 계약으로 적어
            #   뒀고 거기 실측이 3.12초다. 새 개념이 아니라 그 규칙을 이 갈래에도 맞추는 것이다.
            #
            #   ⚠ 발사 실패가 응답에 안 드러나지만 **거동 차이가 없다** — 예전에도 반환값을
            #   안 썼고, 실패는 `notify._post_card` 가 삼키고 로그로 남긴다.
            if background is not None:
                background.add_task(
                    send_untagged_alert,
                    gate_no=body.gate_no,
                    alert_id=ev.alert_id,
                    source_id=ev.pass_id,
                    low_confidence=ev.low_confidence,
                )
            else:
                # 시연 창구(`routers/demo.py`)가 순수 파이썬으로 부른 갈래다. 실을 자리가 없어서
                # 예전처럼 인라인으로 기다린다 — 그쪽은 라파 큐를 안 막으므로 거동이 그대로다.
                await send_untagged_alert(
                    gate_no=body.gate_no,
                    alert_id=ev.alert_id,
                    source_id=ev.pass_id,
                    low_confidence=ev.low_confidence,
                )
    return GatePassIngestAck(
        event_id=body.event_id,
        stored=ev.stored,
        duplicate=not ev.stored,
        verdict=ev.verdict,
        # 중복 재전송분도 저장 행에서 사유를 다시 뽑아 채운다(state_machine
        # `_describe_duplicate_pass`). 라파 sender가 2xx까지 무한 재전송이라 운영에서 실제로
        # 보이는 응답은 대부분 재전송분이다 — 거기서 비면 이 칸은 사실상 없는 칸이 된다.
        beam_conflict=ev.beam_conflict,
    )


@router.post("/shuttle-arrivals", response_model=ShuttleIngestAck)
async def ingest_shuttle_arrival(
    body: ShuttleArrivalIn, response: Response, session: AsyncSession = Depends(get_db)
) -> ShuttleIngestAck:
    """셔틀 도착 신호를 기록하고 출동 대기 창을 연다(-156).

    ⚠ **이 응답 시점에는 목적지 명령이 아직 안 나갔다**(2026-07-31 셔틀출동 설계 §2.2).
    예전에는 여기서 바로 쐈지만 지금은 `app/dispatch.py`가 5초를 붙들고, 그 사이 관제 화면
    선택이 오면 그 목적지로, 안 오면 그날 기본값으로 낸다. 그래서 새 행 갈래의
    `notified_robot_count`는 늘 0이고, 확정 결과는 WS `shuttle_dispatch_result`로 나간다.
    `SHUTTLE_DISPATCH_HOLD_SEC`를 0 이하로 두면 예전처럼 이 자리에서 즉시 발사한다.

    ⚠ 통지 실패는 **재전송으로 복구된다.** 예전엔 새 행이 생겼을 때만 통지해서, WS 전송이
    실패하면(로봇이 그 순간 떨어져 있으면) 행은 커밋됐는데 명령은 사라지고 같은 요청을 다시
    보내도 duplicate로 튕겼다. 수동 재발행 경로도 없어서 시연자가 알 방법이 없었다(실측 P0-4-c).

    통지 여부의 정본·재전송 규칙·본문 충돌 409·응답 헤더는 전부 몸통
    (`app/shuttle_call.record_shuttle_arrival`)이 쥔다. 여기서 다시 구현하면 화면 창구와
    갈래가 갈라진다 — 예전에 이 함수가 자기 몸통을 들고 있어서 생긴 자리다.
    """
    result = await record_shuttle_arrival(
        session,
        event_id=body.event_id,
        boot_id=body.boot_id,
        gate_no=body.gate_no,
        room_id=body.room_id,
        shuttle_no=body.shuttle_no,
        signal_ts=body.signal_ts,
        # 통지 결과를 헤더에도 싣는다. 값 계산은 몸통이 한다(갈래별로 값이 다르다).
        response=response,
    )
    return ShuttleIngestAck(
        event_id=body.event_id,
        stored=result.stored,
        duplicate=not result.stored,
        notified_robot_count=result.notified_robot_count,
        # 통지 유실 복구가 일어난 사실. 예전엔 서버 로그와 헤더에만 있어서 기기·화면 어느
        # 쪽도 "이번 응답이 복구분인가"를 못 봤다. 몸통 값을 그대로 옮긴다.
        resent=result.resent,
    )
