"""조회 API + healthz. 대시보드가 읽는 계열이라 인증 없이 연다(학내 시연 전제).

⚠ 인증 경계 — "읽기는 열고 쓰기는 잠근다"가 이 파일의 규칙이다. GET 계열은 화면이 붙는
자리라 그대로 열어 두고, 상태를 바꾸는 갈래(ack·identify·신원 삭제)와 신원 원문을 읽는
갈래에는 인입 API와 같은 X-API-Key를 건다. 인터넷에 열린 채로 두면 아무나 감사 기록에
위조 신원을 쓰고 상위 보고선으로 카드를 쏠 수 있다(2026-07-30 검토 실측).

X-API-Key는 브라우저에서 부르는 창구에선 완전한 인증이 아니다 — 페이지를 여는 사람이
키를 보게 된다. 진짜 경계(nginx auth_basic이나 서명 쿠키 세션)를 어디에 둘지는 팀·사용자
결정 자리라 여기선 "아무나 쓰던 상태"를 막는 최소 방어까지만 한다.

⭐ 2026-08-01 — 그 "진짜 경계"가 세션 로그인으로 정해졌다(로그인 설계 확정본 §3). 이 파일의
창구는 healthz 하나만 빼고 전부 요원 세션 뒤로 들어가고, 신원 기록 삭제만 팀장급이다.
단 **`AUTH_REQUIRE_LOGIN`이 켜졌을 때만** 그렇다 — 꺼진 동안은 조회가 익명이고 쓰기가 기기
키라, 위 문단이 적은 예전 모습이 글자 그대로 남는다(§6.2 1단계).


목록 3종(robots·events·alerts)과 초기 로드 스냅샷이 같은 fetch 함수를 쓴다.
목록으로 본 값과 스냅샷으로 본 값이 갈라지면 화면이 두 그림을 그리게 되니까
쿼리를 두 벌 두지 않는다.

응답의 실데이터 칸은 이미 있는 DB 컬럼을 노출한다. 로봇 위치·LED는 예외로, 컬럼을 만들지
않고 로봇 보고에서 받은 값을 서버 메모리에서 얹는다(robot_channel.RobotPresentation).
보고가 없거나 서버가 재기동됐으면 null이라 기존 화면 계약은 그대로다.

"아키텍처 2026-07-23 §05 표 컬럼만"은 아니다 — updated_at·created_at은 models.py엔
있어도 §05 표엔 안 적혔고, server_time·ws_protocol_version·counts는 DB 컬럼이 아니라
응답에서 계산하는 값이다. 둘 중 뭘 정본으로 맞출지는 팀 확정 대기다.

GET /api/dashboard/snapshot도 §02 조회 API 목록 밖 신규 엔드포인트라 확정 계약이 아니다.
"""
from __future__ import annotations

import datetime as dt
import logging
from enum import Enum

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import String, cast, func, literal, null, select, text, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.alert_lifecycle import (
    AlertStatus,
    apply_transition,
    demote_after_identity_cleared,
    normalize_status,
    rank,
)
from app import gate_sensing
from app.config import get_settings
from app.credit.state_machine import ALERT_TYPE_UNTAGGED
from app.db import get_db
from app.dispatch import weather_snapshot
from app.eta import SAMPLE_LIMIT_MAX, estimate_eta
from app.notify import send_identify_report
from app.robot_channel import SHUTTLE_SOURCE_TYPE, RobotPresentation, get_presentation
from app.models import (
    Alert,
    GatePassEvent,
    Robot,
    ShuttleArrival,
    Staff,
    StaffAudit,
    TaggingEvent,
)
from app.schemas import (
    GATE_NO_MAX,
    GATE_NO_MIN,
    AlertIdentifyIn,
    AlertIdentityOut,
    AlertOut,
    AlertStatusIn,
    DashboardSnapshot,
    EtaOut,
    EventOut,
    HealthOut,
    RobotOut,
    ShuttleCallIn,
    ShuttleCallOut,
    SnapshotCounts,
)
from app.schemas import STUDENT_NO_MAX_LEN
from app.shuttle_call import (
    SHUTTLE_CALL_COOLDOWN_SEC,
    in_call_cooldown,
    mark_call,
    next_web_call_event_id,
    record_shuttle_arrival,
    unmark_call,
    web_call_boot_id,
)
from app.auth import AuthActor, require_agent, require_leader
from app.security import key_gate_or, require_api_key
from app.ws import make_envelope, manager

logger = logging.getLogger("c207.query")

router = APIRouter(tags=["query"])

# ── 역할 게이트 (로그인 설계 확정본 2026-08-01 §3 매트릭스) ──────────────────
#
# ⭐ 제1 불변 — `AUTH_REQUIRE_LOGIN=false`면 아래 셋이 전부 예전 거동이다. 조회 계열은
# `require_agent`가 판정을 안 해서 익명 그대로고, 쓰기 계열은 `key_gate_or`가 옛
# `require_api_key`를 그대로 태운다. 켜야 비로소 세션 판정으로 넘어간다.
#
# ⚠ `healthz`만 켠 뒤에도 익명이다 — 젠킨스·모니터링이 부르는 자리라 로그인 뒤로 넣으면
# 헬스체크가 통째로 죽는다(§3 표 첫 줄).

# 신원 원문을 읽고 쓰는 갈래 + 수명주기 전이. 켜지면 요원, 꺼지면 기기 키.
_agent_or_key = key_gate_or(require_agent, require_api_key)
# 신원 기록 삭제 하나만 팀장급이다(§8 결정 5 확정 — 감사 기록을 지우는 갈래라 상향).
_leader_or_key = key_gate_or(require_leader, require_api_key)

DEFAULT_EVENT_LIMIT = 50
MAX_EVENT_LIMIT = 500
DEFAULT_ALERT_LIMIT = 100
MAX_ALERT_LIMIT = 500
# 스냅샷은 첫 화면용이라 목록 API보다 짧게 자른다. 더 보려면 목록 API로 간다.
#
# ⭐ **이벤트가 경고보다 많아야 한다** (2026-08-05 프론트 지적). 화면은 경고 한 줄의
# `source_type: "gate_pass"` · `source_id`로 통과 행을 찾아 처리 상태·신원 칸을 채우는데,
# 그 통과 행이 `recent_events` 범위 밖이면 다리가 끊겨 칸이 빈다. 예전에는 경고를 50개까지
# 주면서 이벤트는 30개만 줘서 **오래된 경고부터 짝을 잃었다** — 프론트가 알림 다섯 중 하나가
# 안 이어지는 것을 실측했다(`source_id` 574인데 범위가 603~632였다).
#
# 80으로 잡은 근거 — 경고 상한(50)을 덮고도 여유가 있다. 경고 하나가 통과 하나를 가리키므로
# 50이 최소인데, 그 사이에 경고를 안 만든 통과·태깅도 끼므로 넉넉히 잡았다. 더 늘리면 첫
# 화면 응답만 무거워지고 화면은 어차피 목록 API로 더 본다.
SNAPSHOT_EVENT_LIMIT = 80
SNAPSHOT_ALERT_LIMIT = 50


class EventKind(str, Enum):
    """`/api/events` 한 줄의 `kind`. UNION 가지가 딱 이 셋이라 닫힌 낱말이다(프론트 요구 N8).

    ⚠ 값은 응답에 실리는 글자 그대로다 — 셔틀은 `shuttle`이 아니라 `shuttle_arrival`이다.
    경고의 `source_type`과 같은 글자여야 (kind, id) 역추적이 붙으므로(S15P11C207-192)
    라벨을 손으로 적지 않고 `SHUTTLE_SOURCE_TYPE` 상수를 그대로 쓴다. 예전에 이벤트 쪽만
    `shuttle`로 적혀 갈라졌던 자리다.

    ⚠ `/api/alerts`의 `source_type`은 일부러 이렇게 안 잠근다. 저쪽은 자유 문자열이고
    이벤트 3계열 말고 `robot` 출처가 실재해서(robot_channel), 닫으면 정상 질의가 422가 된다.
    여기는 UNION 가지 밖의 값이 아예 존재할 수 없어 닫힌 목록이 서버 구조 그대로다.
    """

    tagging = "tagging"
    gate_pass = "gate_pass"
    shuttle_arrival = SHUTTLE_SOURCE_TYPE


def _null_str():
    """UNION 자리 맞춤용 빈 칸. 계열마다 안 쓰는 칸을 타입까지 정해서 채운다."""
    return cast(null(), String)


def _alert_out(alert: Alert) -> AlertOut:
    """Alert 행 → 조회 응답. 경고를 내보내는 자리는 전부 이 함수 하나를 탄다.

    갈래마다 손으로 칸을 채우면 새 칸이 붙을 때 한 자리를 빠뜨린다 — 목록엔 나오는데
    ack 응답엔 없는 칸이 생기면 화면이 확인 버튼 한 번에 값을 잃는다.

    ⚠ 신원 원문·메모·확인한 요원은 일부러 안 싣는다(AlertOut docstring 참고).
    """
    return AlertOut(
        id=alert.id,
        type=alert.type,
        severity=alert.severity,
        source_type=alert.source_type,
        source_id=alert.source_id,
        ack=alert.ack,
        created_at=alert.created_at,
        identified=alert.identified_at is not None,
        identified_at=alert.identified_at,
        # normalize_status를 타는 이유 — 마이그레이션을 안 거친 사본 DB나 손으로 넣은 행이
        # 낯선 값을 들고 있으면 enum 검증에서 목록 API가 통째로 500이 된다.
        status=normalize_status(alert.status),
        acked_at=alert.acked_at,
        resolved_at=alert.resolved_at,
        resolution=alert.resolution,
    )


# ── 조회 서비스 (목록 API와 스냅샷이 같이 쓴다) ──────────────────────────────

def _has_nothing_to_show(robot: Robot, shown: RobotPresentation) -> bool:
    """이 행으로 그릴 게 하나도 없나 — 모드·배터리·통신·위치·LED·임무 상태가 전부 빈 카드인가.

    **통지 기록용으로만 생긴 행을 화면에서 가리는 잣대다.** `robot_channel._ensure_robot_pk`가
    셔틀 통지를 남기려고 `Robot(name=...)`만 넣고 지나가는 자리가 있어서(그 행이 없으면
    `notified_robot_id`가 NULL로 남아 재전송 판정이 같은 신호를 다시 내보낸다), 거르지 않으면
    상태를 한 번도 안 올린 로봇이 관제 목록과 스냅샷에 빈 카드로 뜬다. 발표 화면에 가짜
    로봇이 서는 건 그 기록보다 비싼 값이라, **기록은 DB에 남기고 화면에서만 가린다.**

    행을 안 만드는 쪽으로 되돌리는 길은 안 골랐다 — 그러면 두 번 출동이 되살아난다.

    잣대를 "이름만 있는 행"이 아니라 "그릴 값이 없는 행"으로 잡은 이유는 되살아나는 조건을
    한 줄로 만들기 위해서다. 그 로봇이 나중에 상태를 올리면 `_upsert_robot`이 같은 행에 값을
    채우고, 모드·배터리·통신 중 하나만 실려도 카드가 목록에 나타난다. 위치·LED·임무 상태만
    올리는 보고도 인메모리 최신값이 차서 같이 나타난다(그 셋은 DB 컬럼이 아니다).

    ⚠ 남는 틈 하나 — `status_summary`도 `mission_status`도 없는 `robot_state`만 올리는 로봇은
    이 잣대로 계속 가려진다. 그 카드엔 이름 말곤 그릴 값이 없어서 어차피 빈 칸이고, 원본 프레임
    중계(`robot_state`)와 카드 메시지(`robot_status`)는 그대로 나간다. 이름만이라도 목록에
    세우고 싶어지면 그때는 화면 계약을 같이 손대야 하는 결정이라 여기서 혼자 정하지 않는다.

    ⚠ `mission_status`를 이 잣대에 넣어도 통지 기록용 행은 안 뜬다 — `_ensure_robot_pk`는
    `record_presentation`을 안 밟아서 그 칸이 영영 None이다. 상태를 실제로 올린 로봇만 는다.
    """
    return (
        robot.mode is None
        and robot.battery is None
        and robot.network_status is None
        and shown.position is None
        and shown.led_status is None
        and shown.mission_status is None
    )


async def fetch_robots(session: AsyncSession) -> list[RobotOut]:
    """로봇 카드 목록. DB 행에 인메모리 최신값(위치·LED·임무 상태)을 얹어 돌려준다.

    칸을 `robot_status` 메시지 카드와 똑같이 맞춘다 — 어긋나면 화면이 스냅샷으로 그린 카드와
    메시지로 갈아끼운 카드가 두 그림이 된다. Robot.name이 로봇 보고의 robot_id다.

    그릴 값이 하나도 없는 행은 뺀다(`_has_nothing_to_show`). 목록 API와 스냅샷이 이 함수
    하나를 같이 쓰니 두 창구가 같은 모집단을 본다 — 스냅샷 `counts.robots`도 이 결과를 센다.
    """
    rows = (await session.execute(select(Robot).order_by(Robot.id))).scalars().all()
    out: list[RobotOut] = []
    for r in rows:
        shown = get_presentation(r.name)
        if _has_nothing_to_show(r, shown):
            continue
        out.append(
            RobotOut(
                id=r.id,
                name=r.name,
                mode=r.mode,
                battery=r.battery,
                network_status=r.network_status,
                updated_at=r.updated_at,
                position=shown.position,
                led_status=shown.led_status,
                mission_status=shown.mission_status,
            )
        )
    return out


async def fetch_events(
    session: AsyncSession, limit: int, kind: EventKind | None = None
) -> list[EventOut]:
    """인입 3계열을 서버 수신 시각 최근 순으로 합쳐 돌려준다.

    kind를 주면 그 계열 가지 하나만 읽는다(프론트 요구 N8). limit과 정렬의 뜻은 그대로라
    **"그 계열에서 최근 limit건"**이다 — 다른 계열이 자리를 차지하지 않는다. 필터 없이
    limit만 좁히면 통과 100건을 보려고 500건을 받아야 하는 구조가 그대로 남는다.

    ⚠ 가지마다 칸에 라벨을 붙여 둔다. UNION은 첫 가지의 이름을 쓰니 셋을 합칠 때는 없어도
    되지만, kind 하나면 그 가지가 통째로 subquery가 되므로 라벨이 빠진 가지에서는
    `row.kind`가 익명 이름으로 나가 터진다.
    """
    tagging = select(
        literal(EventKind.tagging.value).label("kind"),
        TaggingEvent.id.label("id"),
        TaggingEvent.event_id.label("event_id"),
        TaggingEvent.gate_no.label("gate_no"),
        TaggingEvent.observed_at.label("observed_at"),
        TaggingEvent.received_at.label("received_at"),
        TaggingEvent.tag_id.label("tag_id"),
        _null_str().label("direction"),
        _null_str().label("verdict"),
        _null_str().label("shuttle_no"),
    )
    gate = select(
        literal(EventKind.gate_pass.value).label("kind"),
        GatePassEvent.id.label("id"),
        GatePassEvent.event_id.label("event_id"),
        GatePassEvent.gate_no.label("gate_no"),
        GatePassEvent.observed_at.label("observed_at"),
        GatePassEvent.received_at.label("received_at"),
        # tag_id 자리. 통과 행도 이제 소비한 크레딧의 카드 UID를 들고 있다(S15P11C207-241).
        # 소비가 없는 판정(미태깅·퇴장·보류)이면 컬럼 자체가 NULL이라 예전 응답과 같은 모양이다.
        GatePassEvent.tag_id.label("tag_id"),
        GatePassEvent.direction.label("direction"),
        GatePassEvent.verdict.label("verdict"),
        _null_str().label("shuttle_no"),
    )
    shuttle = select(
        # 경고 source_type과 같은 글자여야 (kind, id) 역추적 매칭이 붙는다.
        # 예전 라벨 "shuttle"은 source_type("shuttle_arrival")과 갈라져 셔틀만 매칭이 안 붙었다.
        literal(EventKind.shuttle_arrival.value).label("kind"),
        ShuttleArrival.id.label("id"),
        ShuttleArrival.event_id.label("event_id"),
        ShuttleArrival.gate_no.label("gate_no"),
        ShuttleArrival.signal_ts.label("observed_at"),
        ShuttleArrival.received_at.label("received_at"),
        _null_str().label("tag_id"),
        _null_str().label("direction"),
        _null_str().label("verdict"),
        ShuttleArrival.shuttle_no.label("shuttle_no"),
    )
    branches = {
        EventKind.tagging: tagging,
        EventKind.gate_pass: gate,
        EventKind.shuttle_arrival: shuttle,
    }
    if kind is None:
        unioned = union_all(*branches.values()).subquery()
    else:
        unioned = branches[EventKind(kind)].subquery()
    # 시각 단일 키면 한 트랜잭션에 들어온 행들의 순서가 안 정해진다(S15P11C207-191).
    # 알림 계열과 같은 꼴로 id 내림차순 tiebreaker를 붙여 결정성을 준다.
    stmt = (
        select(unioned)
        .order_by(unioned.c.received_at.desc(), unioned.c.id.desc())
        .limit(limit)
    )
    rows = (await session.execute(stmt)).all()
    owners = await staff_owner_by_tag(session, {r.tag_id for r in rows if r.tag_id})
    return [
        EventOut(
            kind=row.kind,
            event_id=row.event_id,
            gate_no=row.gate_no,
            observed_at=row.observed_at,
            received_at=row.received_at,
            id=row.id,
            tag_id=row.tag_id,
            direction=row.direction,
            verdict=row.verdict,
            shuttle_no=row.shuttle_no,
            staff_name=owners.get(row.tag_id, (None, None))[0] if row.tag_id else None,
            staff_student_no=(
                owners.get(row.tag_id, (None, None))[1] if row.tag_id else None
            ),
        )
        for row in rows
    ]


async def staff_owner_by_tag(
    session: AsyncSession, tag_ids: set[str]
) -> dict[str, tuple[str | None, str | None]]:
    """카드 UID → (이름, 학번). 명부에 없는 UID는 결과에 아예 안 들어간다(호출 쪽이 null로 쓴다).

    ⭐ **UNION 안에서 조인하지 않는다.** 세 가지가 칸 수를 맞춰야 하는 구조라 조인을 넣으면
    셔틀 가지에도 더미 칸이 생기고, 가지마다 조인 조건이 달라 읽기가 나빠진다. UNION을 그대로
    두고 뒤에서 **UID 묶음 한 번**으로 채운다 — 쿼리가 하나 늘 뿐 N+1이 아니다.

    ⚠ **`is_active`를 안 본다.** 해제된 사람이 카드를 댄 사건이 오히려 더 봐야 할 값이라,
    이름을 지우면 화면이 "명부 밖 카드"로 그려 그 사실을 감춘다.

    ⚠ 학번을 같이 돌려주는 것은 2026-08-05 사용자 확정이다(`schemas.EventOut.staff_name` 주석).
    """
    if not tag_ids:
        return {}
    rows = (
        await session.execute(
            select(Staff.tag_id, Staff.name, Staff.student_no).where(
                Staff.tag_id.in_(tag_ids)
            )
        )
    ).all()
    return {tag_id: (name, student_no) for tag_id, name, student_no in rows if tag_id}


async def fetch_alerts(
    session: AsyncSession,
    *,
    active_only: bool,
    limit: int,
    type_: str | None = None,
    statuses: list[str] | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
) -> list[AlertOut]:
    """알림 목록. active_only면 아직 확인 안 한(ack=false) 것만.

    type_을 주면 그 종류만 추린다. 미태깅 경고만 모아 보는 자리가 없어서, 요원이 신원을
    적을 경고를 실시간 피드에서 눈으로 찾아야 했다 — 피드는 오래된 행부터 버리므로 조금만
    지나면 손댈 방법이 아예 사라진다.

    statuses를 주면 그 수명주기 단계만 추린다. active_only와는 다른 축이다 — active_only는
    "관제가 확인을 눌렀나"(ack)를 보고, statuses는 "어디까지 진행됐나"를 본다. 둘을 같이
    주면 교집합이다. 예를 들어 신원까지 적혔지만 관제는 아직 안 누른 경고는
    `?active=true&status=IDENTIFIED`로 뽑힌다(alert_lifecycle 모듈 머리 "ack와 이 축의 관계").

    source_type·source_id는 발화 이벤트로 역참조하는 축이다. 이벤트 한 줄의 (kind, id)가
    경고의 (source_type, source_id)라는 규칙(S15P11C207-192)을 질의로 그대로 연 것이다.
    이게 없으면 화면이 이벤트 표에 처리 상태를 그리려고 경고를 최근순 상한(limit)만큼 받아
    스스로 뒤져야 해서, 그 창 밖으로 밀린 경고는 "기록 없음"으로 그려진다(프론트 요구 N13).

    ⚠ 빈 글자 source_type은 안 준 것과 같게 본다(type_과 같은 규칙). 짝을 강제하는 판정은
    창구(list_alerts)에 있다 — 여기는 준 축만 걸러 주는 자리다.

    이벤트 쪽과 같은 이유로 id 내림차순 tiebreaker를 붙인다(S15P11C207-193).
    """
    stmt = select(Alert).order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit)
    if active_only:
        stmt = stmt.where(Alert.ack.is_(False))
    if type_:
        stmt = stmt.where(Alert.type == type_)
    if statuses:
        stmt = stmt.where(Alert.status.in_(statuses))
    if source_type:
        stmt = stmt.where(Alert.source_type == source_type)
    if source_id is not None:
        stmt = stmt.where(Alert.source_id == source_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [_alert_out(a) for a in rows]


async def count_active_alerts(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Alert).where(Alert.ack.is_(False))
    return (await session.execute(stmt)).scalar_one()


# ── 엔드포인트 ──────────────────────────────────────────────────────────────

@router.get("/healthz", response_model=HealthOut)
async def healthz(session: AsyncSession = Depends(get_db)) -> HealthOut:
    try:
        await session.execute(text("SELECT 1"))
        db_state = "up"
    except Exception:
        db_state = "down"
    return HealthOut(
        status="ok",
        db=db_state,
        identify_report_enabled=get_settings().mattermost_identify_active,
    )


@router.get(
    "/api/dashboard/snapshot",
    response_model=DashboardSnapshot,
    dependencies=[Depends(require_agent)],
)
async def dashboard_snapshot(
    session: AsyncSession = Depends(get_db),
) -> DashboardSnapshot:
    """대시보드가 붙는 순간 한 번 읽는 초기 로드 묶음(§02 목록 밖 신규 — 팀 확정 대기).

    로봇 카드·미태깅 경고·이벤트 피드를 한 번에 채운다. 그 뒤 변화는 /ws/dashboard로 받는다.
    스냅샷을 먼저 읽고 WS를 열면 그 사이 이벤트를 놓치므로, 화면은 WS를 먼저 열고
    이 스냅샷을 읽은 뒤 중복을 걸러내는 순서를 권한다.

    중복 키는 계열마다 다르다 — 이벤트 메시지는 payload.event_id를 recent_events[].event_id와,
    untagged_alert는 payload에 event_id가 없으므로 payload.alert_id를 active_alerts[].id와 맞춘다.

    ⭐ `weather` 칸은 `weather_update` WS 메시지의 payload와 **같은 모양**이다. 그 메시지는
    값이 바뀔 때만 나가서, 시연 중 탭을 새로 열면 다음 변화까지 상단 기상 칩이 비어 있었다
    (2026-08-03 교차 검증 W2). 서버가 아직 날씨를 한 번도 못 받았으면 null이다.

    ## ⭐ 조회 순서를 바꾸지 마라 — 이벤트가 맨 앞이다

    `last_event_at`(기준 시각)이 **이벤트 조회 결과에서** 나온다. 그래서 이벤트를 맨 먼저
    읽어야 아래 불변식이 선다.

        스냅샷에 안 담긴 변화는 **반드시** 방송 시각 > last_event_at 이다.

    근거는 두 줄이다. ①이벤트 조회가 처음이라 그 읽기 시점 `T`가 뒤따르는 로봇·경고·날씨
    읽기보다 이르고, `last_event_at`은 그 조회가 실제로 돌려준 행의 값이라 `T`보다 이르다.
    ②서버는 **커밋한 뒤에** 방송하고 봉투 `timestamp`를 그때 찍는다(ingest·robot_channel 다
    같다). 그래서 스냅샷이 못 본 변화는 커밋이 `T` 뒤 → 방송이 `T` 뒤 → `last_event_at` 뒤다.

    순서를 뒤집어 로봇을 먼저 읽으면 이 사슬이 끊긴다. 로봇 조회 뒤·이벤트 조회 앞의 틈에서
    커밋된 로봇 보고는 스냅샷에 없는데, 그 틈에 들어온 이벤트가 기준 시각을 그 방송 시각보다
    뒤로 밀어 올려서 화면이 "이미 스냅샷에 있다"로 잘못 가린다. 로봇 카드는 다음 보고가
    덮어 주지만 경고는 다시 안 방송돼서 그대로 사라진다.

    ⚠ 여기서 세 계열을 한 트랜잭션 스냅샷으로 묶지는 않았다(READ COMMITTED 그대로라
    조회마다 읽기 시점이 다르다). 위 불변식은 "이벤트가 맨 앞"이라는 순서만으로 서고,
    격리 수준을 올리면 이 창구 하나 때문에 세션 거동이 달라져서 안 건드렸다.
    """
    settings = get_settings()
    # ⚠ 이벤트가 맨 앞이어야 한다(위 docstring "조회 순서를 바꾸지 마라").
    events = await fetch_events(session, SNAPSHOT_EVENT_LIMIT)
    robots = await fetch_robots(session)
    alerts = await fetch_alerts(session, active_only=True, limit=SNAPSHOT_ALERT_LIMIT)
    active_total = await count_active_alerts(session)
    return DashboardSnapshot(
        server_time=dt.datetime.now(dt.timezone.utc),
        # 목록이 `received_at desc, id desc` 정렬이라 첫 줄이 곧 최대값이다. 여기서 max()를
        # 다시 돌면 정렬 계약이 바뀌었을 때 조용히 다른 뜻이 되므로 첫 줄을 그대로 쓴다.
        last_event_at=events[0].received_at if events else None,
        ws_protocol_version=settings.ws_protocol_version,
        robots=robots,
        active_alerts=alerts,
        recent_events=events,
        counts=SnapshotCounts(
            robots=len(robots),
            active_alerts=active_total,
            recent_events=len(events),
        ),
        identify_report_enabled=settings.mattermost_identify_active,
        weather=await weather_snapshot(session),
        # ⚠ DB를 안 탄다 — 인메모리 마지막 값이다. 못 받았으면 null이고 화면은 그것을
        #   "아직 못 받았습니다"로 그린다(`app/gate_sensing` 모듈 머리 참고).
        gate_sensing=gate_sensing.snapshot(),
        gate_sensing_at=gate_sensing.snapshot_at(),
    )


@router.get(
    "/api/robots",
    response_model=list[RobotOut],
    dependencies=[Depends(require_agent)],
)
async def list_robots(session: AsyncSession = Depends(get_db)) -> list[RobotOut]:
    return await fetch_robots(session)


# ⭐ 이벤트 목록에 태그 UID가 평문으로 실린다. -238이 지적한 그 노출이 요원 게이트로 닫힌다.
@router.get(
    "/api/events",
    response_model=list[EventOut],
    dependencies=[Depends(require_agent)],
)
async def list_events(
    limit: int = Query(DEFAULT_EVENT_LIMIT, ge=1, le=MAX_EVENT_LIMIT),
    kind: EventKind | None = Query(
        default=None,
        description=(
            "인입 계열로 추린다. 응답 한 줄의 kind와 같은 글자다"
            ' ("tagging"·"gate_pass"·"shuttle_arrival" — 셔틀은 "shuttle"이 아니다).'
            " 모르는 글자는 422다."
        ),
    ),
    session: AsyncSession = Depends(get_db),
) -> list[EventOut]:
    """이벤트 목록. kind를 주면 그 계열만 온다(프론트 요구 N8).

    - 필터를 걸어도 limit·정렬(`received_at desc, id desc`)의 뜻은 그대로다.
      **"그 계열에서 최근 limit건"**이라 다른 계열이 자리를 안 차지한다.
    - 모르는 kind는 422다. UNION 가지가 딱 셋이라 그 밖의 값은 답이 있을 수 없는 질의고,
      빈 목록으로 조용히 돌려주면 화면이 오타를 "그 계열은 없다"로 그린다.
      `/api/alerts`의 `source_type`을 빈 200으로 여는 것과 일부러 다르다 —
      저쪽은 자유 문자열이고 `robot` 출처가 실재해서 닫으면 정상 질의를 막는다(EventKind 참고).
    - kind를 안 주면 예전 그대로 3계열 합본이다(기존 계약 불변).
    """
    return await fetch_events(session, limit, kind)


@router.get(
    "/api/alerts",
    response_model=list[AlertOut],
    dependencies=[Depends(require_agent)],
)
async def list_alerts(
    active: bool = Query(False, description="true면 아직 확인 안 한 알림만"),
    type: str | None = Query(
        default=None,
        max_length=32,
        description='경고 종류로 추린다. 미태깅만 보려면 "untagged".',
    ),
    # 파이썬 이름을 status로 두면 이 함수 안에서 fastapi의 status 모듈이 가려진다.
    # alias로 질의 문자열 이름만 status로 둔다.
    status_: list[AlertStatus] | None = Query(
        default=None,
        alias="status",
        description=(
            "수명주기 단계로 추린다. 여러 번 붙일 수 있다"
            " (예: ?status=OPEN&status=ACKED = 아직 안 닫힌 사건)."
        ),
    ),
    source_type: str | None = Query(
        default=None,
        max_length=32,
        description=(
            "발화 이벤트 종류로 추린다. 이벤트 한 줄의 kind와 같은 글자다"
            ' ("gate_pass"·"tagging"·"shuttle_arrival").'
        ),
    ),
    source_id: int | None = Query(
        default=None,
        ge=1,
        description="발화 이벤트의 행 id. source_type과 짝으로만 받는다(혼자 오면 422).",
    ),
    limit: int = Query(DEFAULT_ALERT_LIMIT, ge=1, le=MAX_ALERT_LIMIT),
    session: AsyncSession = Depends(get_db),
) -> list[AlertOut]:
    """경고 목록. 축 넷(ack·종류·수명주기·발화 이벤트)을 얹으면 교집합이다.

    발화 이벤트 축(source_type·source_id) 계약 — 프론트 요구 N13.
    - 짝으로 주면 그 이벤트가 낸 경고만 온다. 이벤트 표가 (kind, id)로 처리 상태를 되짚는
      자리라, 상한(limit) 밖으로 밀린 경고도 이 축이면 바로 집힌다.
    - `source_type`만 주면 그 종류 전체다. 축 하나짜리 조회로도 뜻이 서니까 막지 않는다.
    - `source_id`만 주면 422다. 계열마다 시퀀스가 따로라 id 하나로는 행이 안 정해진다 —
      통과 12번과 태깅 12번이 같이 걸려 화면이 남의 사건 처리 상태를 그린다. 그걸 조용히
      섞어 주는 대신 부르는 쪽을 멈춰 세운다.
    - 짝이 맞아도 경고가 없으면 빈 목록이다(200). "그 이벤트는 경고를 안 냈다"가 정상 답이라
      404가 아니다.
    - 범위 밖 값(`source_id` 0 이하, 32자 넘는 `source_type`)은 조용히 자르지 않고 422다
      (`/api/eta`의 samples·`/api/stats/untagged`와 같은 정책).

    ⚠ `source_type`을 아는 글자 목록으로 좁히지 않는다. 경고의 source_type은 이벤트 3계열
    말고 "robot"도 쓴다(robot_channel). enum으로 잠그면 그 줄을 뽑는 정상 질의가 422가 된다.
    모르는 글자는 그냥 안 맞아서 빈 목록으로 나간다 — `type`과 같은 규칙이다.
    """
    # ⚠ 게이트와 질의가 **같은 값**을 봐야 한다. 앞뒤 공백을 안 걷으면 `""`는 422인데
    # `" "`는 truthy라 게이트를 그냥 지나고, 질의는 `source_type == " "`로 걸러 빈 목록 200이
    # 된다 — 같은 뜻의 입력이 창구마다 다른 답을 받는다(fetch_alerts의 "빈 글자는 안 준 것과
    # 같게 본다"와도 어긋난다). 걷어 낸 값으로 판정과 질의를 함께 돌린다.
    source_type = source_type.strip() if source_type is not None else None
    if source_id is not None and not source_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="source_id는 source_type과 함께 보내야 합니다.",
        )
    return await fetch_alerts(
        session,
        active_only=active,
        limit=limit,
        type_=type,
        statuses=[s.value for s in status_] if status_ else None,
        source_type=source_type,
        source_id=source_id,
    )


@router.get(
    "/api/eta", response_model=EtaOut, dependencies=[Depends(require_agent)]
)
async def get_eta(
    gate_no: int | None = Query(default=None, ge=GATE_NO_MIN, le=GATE_NO_MAX),
    samples: int | None = Query(default=None, ge=1, le=SAMPLE_LIMIT_MAX),
    session: AsyncSession = Depends(get_db),
) -> EtaOut:
    """예상 도착 시간 (S15P11C207-158).

    셔틀 도착 신호에서 로봇 도착까지 걸린 최근 기록을 EWMA로 눌러 낸다.
    기록이 없으면 200에 빈 응답(method="none"·samples=0·eta_sec=null)이다.
    samples는 1~SAMPLE_LIMIT_MAX 범위다 — 벗어나면 조용히 자르지 않고 422로 돌려준다.

    ⚠ gate_no 범위는 기기 인입 3종·화면 셔틀 호출과 **같은 값**이다(app.schemas의
    GATE_NO_MIN·GATE_NO_MAX). 상한이 없으면 이 값이 `app/eta.py`의
    `ShuttleArrival.gate_no == gate_no`로 그대로 들어가고, 컬럼이 Integer라 int32를 넘는
    값은 asyncpg DataError로 **500**이 된다. 인입만 막고 조회를 열어 두면 같은 성질의
    결함이 창구만 바꿔 남는다.
    """
    return await estimate_eta(session, gate_no=gate_no, limit=samples)


# ── 화면 셔틀 가상 호출 ────────────────────────────────────────────────────

@router.post(
    "/api/shuttle-calls",
    response_model=ShuttleCallOut,
    dependencies=[Depends(require_agent)],
)
async def call_shuttle(
    body: ShuttleCallIn, session: AsyncSession = Depends(get_db)
) -> ShuttleCallOut:
    """관제 화면이 누르는 셔틀 가상 호출. 각본 ①을 화면에서 시작하는 자리다 (C6).

    셔틀은 실기기를 안 만들기로 확정됐는데, 기기용 인입 `POST /api/shuttle-arrivals`가
    X-API-Key를 요구해서 화면이 각본을 시작할 방법이 없었다. 화면이 그 키를 들면 브라우저에
    키가 그대로 노출되고, 그 키 하나로 태깅·통과 인입과 신원 입력까지 전부 열린다.

    ⚠ **인입 창구 중에서는 이 창구만 인증이 없다.** 태깅·통과 인입은 그대로 키가 걸려
    있다. 여는 폭을 셔틀 하나로 좁힌 게 이 설계의 핵심이라, 여기에 태깅·통과를 얹지 마라.
    (`AUTH_REQUIRE_LOGIN`을 켜면 이 창구도 요원 세션 뒤로 들어간다 — 화면이 누르는 자리라
    기기 키가 아니라 사람 세션이 맞는 잣대다. 태깅·통과 인입은 켠 뒤에도 기기 키 그대로다.)

    ⚠ 2026-07-31에 출동 창구(`/api/dispatch/*`, routers/dispatch.py)가 늘면서 **키 없는
    자리가 여기 하나만은 아니게 됐다.** 그쪽은 기본값이 키 없음이지만 스위치가 있다
    (`DISPATCH_REQUIRE_API_KEY`). 여는 폭도 더 넓다 — 로봇 조작이다.

    남용 방어.
    - 같은 (게이트, 룸)을 SHUTTLE_CALL_COOLDOWN_SEC 안에 다시 부르면 429이고 Retry-After에
      남은 초가 실린다. 화면 버튼은 연타되고 시연 중엔 여러 사람이 같은 버튼을 누른다 —
      그때마다 로봇에 목적지 명령이 새로 나가면 출동 중인 로봇이 명령을 다시 받아 도착
      판정(mission_status 에지)이 흔들린다.
    - 인증이 없으니 이게 유일한 유량 제한이다. 진짜 경계(프록시 인증·세션 쿠키)를 어디에
      둘지는 여전히 팀 결정이다(모듈 머리 "인증 경계").

    event_id와 signal_ts는 서버가 정한다(ShuttleCallIn docstring 참고).

    ⚠ 쿨다운 눈금은 **검사 직후에 찍고, 실패한 갈래에서 되돌린다.** 적재가 끝난 뒤에 찍으면
    검사와 눈금 사이가 await 여러 개(DB 커밋·WS 방송·대기 창 생성)만큼 벌어져서, 연타 두
    건이 둘 다 429를 안 맞고 서로 다른 event_id로 도착 행·대기 창을 각각 만든다 — 연타
    차단이 막으려던 바로 그 상태다. 되돌리기가 있어서 "실패한 호출이 다음 5초를 삼킨다"는
    반대쪽 함정도 안 열린다(`unmark_call`).
    """
    remain = in_call_cooldown(body.gate_no, body.room_id)
    if remain > 0:
        # ⚠ 로그 머리를 대기 창(`[출동 대기 창]`)과 갈라 적는다. 두 값이 **정확히 같은 5.0초**라
        # 연타 시험에서 무엇이 막았는지 헷갈린다(설계 §6.1). 여기는 입구에서 두 번째 호출을
        # 통째로 삼키는 자리고, 대기 창은 이미 받아들인 신호 하나를 붙드는 자리다.
        logger.info(
            "[게이트 연타 차단] 게이트 %s 호출을 쿨다운이 막았다"
            " — SHUTTLE_CALL_COOLDOWN_SEC=%.1f초, 남은 %.1f초. 신호는 적재되지 않았다.",
            body.gate_no, SHUTTLE_CALL_COOLDOWN_SEC, remain,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"같은 게이트를 너무 자주 호출했습니다. {remain:.1f}초 뒤에 다시 시도해 주세요.",
            headers={"Retry-After": str(max(1, int(remain + 0.999)))},
        )

    # 검사 직후에 찍는다(위 docstring). 아래 적재가 터지면 되돌린다.
    mark_call(body.gate_no, body.room_id)
    event_id = next_web_call_event_id(body.gate_no)
    signal_ts = dt.datetime.now(dt.timezone.utc)
    try:
        result = await record_shuttle_arrival(
            session,
            event_id=event_id,
            boot_id=web_call_boot_id(),
            gate_no=body.gate_no,
            signal_ts=signal_ts,
            room_id=body.room_id,
            shuttle_no=body.shuttle_no,
        )
    except Exception:
        # 신호가 안 실렸다 — 눈금을 남기면 화면이 다시 눌러도 5초 동안 429만 돌아온다.
        unmark_call(body.gate_no, body.room_id)
        raise
    logger.info(
        "화면 셔틀 호출 게이트 %s (event_id=%s, 로봇 %s대에 전달)",
        body.gate_no,
        event_id,
        result.notified_robot_count,
    )
    return ShuttleCallOut(
        event_id=event_id,
        stored=result.stored,
        gate_no=body.gate_no,
        shuttle_no=body.shuttle_no,
        signal_ts=signal_ts,
        notified_robot_count=result.notified_robot_count,
        command_id=result.command_id,
        # 몸통(ShuttleRecordResult)이 정본이다. 이 창구에서는 늘 False로 나가는데(서버가
        # event_id를 매 호출 새로 만들어서 중복 갈래가 구조적으로 안 생긴다) 그래도 몸통 값을
        # 옮긴다 — 기본값에 기대면 나중에 event_id를 부르는 쪽이 정하게 바뀌는 순간 이 창구만
        # 조용히 거짓을 내보낸다.
        resent=result.resent,
    )


# ── 사건 수명주기 ──────────────────────────────────────────────────────────
# 상태 낱말·전이 규칙의 정본은 app/alert_lifecycle.py다. 여기는 창구 셋(상태·확인·신원)이
# 그 규칙 하나를 같이 타게 묶는 자리다 — 창구마다 상태를 직접 대입하면 갈래가 늘 때 한
# 자리가 전이 규칙을 빠뜨린다.
#
# ⚠ 앞서 이 자리에 "불변식 ack == status != OPEN"이라 적혀 있었는데 **틀린 문장이었다.**
# 정본(alert_lifecycle.py 모듈 머리 "ack와 이 축의 관계")이 정반대를 명시한다 — ack=false인데
# IDENTIFIED인 상태가 정의된 값이다. 마이그레이션 0005도 ack를 한 행도 안 고쳤고, 실서버
# id 18이 실제로 그 "불변식"을 깨고 있다. 두 축을 묶어 읽으면(예: !ack를 status == OPEN으로
# 바꿔 쓰면) 관제 화면 종 배지 숫자가 달라진다. ack는 "관제가 봤나", status는 "어디까지
# 진행됐나"다.


async def _broadcast_status(alert: Alert) -> None:
    """수명주기가 바뀐 걸 붙어 있는 대시보드 전부에 알린다.

    ⚠ payload에 acked_by·resolved_by를 싣지 마라. /ws/dashboard는 인증이 없고, 그 값은
    우리 쪽 요원 이름이다(AlertOut이 같은 이유로 안 싣는다).
    """
    await manager.broadcast(
        make_envelope(
            "alert_status",
            {
                "alert_id": alert.id,
                "type": alert.type,
                "severity": alert.severity,
                "status": normalize_status(alert.status),
                "ack": alert.ack,
                "resolution": alert.resolution,
                "resolved_at": (
                    alert.resolved_at.isoformat() if alert.resolved_at else None
                ),
            },
        )
    )


@router.post("/api/alerts/{alert_id}/status", response_model=AlertOut)
async def transition_alert(
    alert_id: int,
    body: AlertStatusIn,
    session: AsyncSession = Depends(get_db),
    actor: AuthActor | None = Depends(_agent_or_key),
) -> AlertOut:
    """경고 하나를 다음 단계로 전진시킨다 (C10 사건 수명주기).

    이게 없으면 사건이 열렸다가 닫히는 축이 아예 없다. 실서버 경고 18건이 전부 ack=false로
    남아 있고, 그중 하나는 신원까지 적힌 채로 활성이었다 — 요원이 각본 ⑤단계를 끝까지 밟아도
    화면에서 사건이 안 닫힌다.

    계약.
    - 상태를 바꾸는 갈래라 X-API-Key를 요구한다(모듈 머리 "인증 경계").
      `AUTH_REQUIRE_LOGIN`을 켜면 그 자리가 요원 세션으로 바뀐다(§3).
    - ⭐ 켜진 구간에서는 본문 `actor`를 **무시하고 세션 실명으로 덮는다.** 자칭 문자열은
      서버가 "정말 그 사람인가"를 못 판정해서 감사 기록 위조가 그대로 통했다(§3). 칸은
      스키마에서 안 지운다 — 지우면 그 칸을 싣던 옛 클라이언트가 422를 맞는다.
    - 없는 경고 번호면 404. ack·identify와 같은 규칙이다.
    - 올릴 수 있는 상태는 ACKED와 RESOLVED뿐이다. IDENTIFIED는 422다 — 신원 원문 없이 그
      상태로 올리면 감사 기록이 빈 채로 종결 후보가 된다(신원 창구가 올린다).
    - RESOLVED로 갈 때 resolution이 필수다(422). 왜 닫았는지 없이 닫으면 센서 오검출과
      진짜 미태깅 사건이 같은 모양으로 사라진다.
    - 뒤 단계에서 앞 단계로 내리는 요청은 409다. 감사 기록이라 되돌리기를 기본값으로 열면
      "닫힌 사건이 다시 열렸다"가 조용히 일어난다. 재개 창구가 필요한지는 팀 결정이다.
    - 같은 단계로 다시 보내면 200 멱등이고 메시지는 안 나간다(ack 재확인 규칙과 같다).

    동시성 — 신원 갈래와 같은 SELECT ... FOR UPDATE를 탄다. 잠금이 없으면 두 요청이
    "읽고 → 판정하고 → 쓰는" 사이에 끼어들어 둘 다 새 전이로 메시지를 낸다.

    브로드캐스트는 커밋 뒤에 한다 — 워커가 1개라 send가 트랜잭션을 붙잡으면 전체가 선다(§05).
    """
    alert = await _lock_alert(session, alert_id)
    target = body.to.value

    if rank(target) < rank(alert.status):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"이미 {normalize_status(alert.status)} 단계입니다."
                f" {target}(으)로 되돌릴 수 없습니다."
            ),
        )

    changed = apply_transition(
        alert,
        to=target,
        # 세션이 있으면 그 실명이 이긴다. 없으면(플래그 off) 예전처럼 본문 자칭값이다.
        actor=actor.display_name if actor is not None else body.actor,
        resolution=body.resolution,
    )
    await session.commit()
    if changed:
        logger.info("경고 %s 수명주기 %s로 전진", alert.id, target)
        await _broadcast_status(alert)
    return _alert_out(alert)


@router.post("/api/alerts/{alert_id}/ack", response_model=AlertOut)
async def ack_alert(
    alert_id: int,
    session: AsyncSession = Depends(get_db),
    actor: AuthActor | None = Depends(_agent_or_key),
) -> AlertOut:
    """경고 하나를 확인 처리하고, 붙어 있는 대시보드 전부에 알린다 (S15P11C207-81).

    상태를 바꾸는 갈래라 X-API-Key를 요구한다(모듈 머리 "인증 경계" 참고).
    `AUTH_REQUIRE_LOGIN`을 켜면 요원 세션으로 바뀌고, 그때는 `acked_by`에 세션 실명이
    찍힌다 — 이 창구엔 본문 자체가 없어서 켜기 전엔 누가 눌렀는지 적을 자리가 아예 없었다.

    대시보드가 두 대 열려 있으면 한쪽에서 확인한 경고가 다른 쪽엔 그대로 남아 있었다.
    확인이 실제로 뒤집힌 순간에만 메시지를 낸다 — 이미 확인한 경고를 또 눌러도 응답은 200에
    ack=true지만 메시지는 안 나간다. 재확인까지 밀어내면 화면 둘이 서로의 재확인을 받아 같은
    줄을 계속 다시 그린다(멱등 인입이 중복을 안 밀어내는 것과 같은 규칙).

    수명주기가 생긴 뒤로 이 창구는 `POST /status`에 to=ACKED를 보낸 것과 같은 전이를 탄다.
    이미 IDENTIFIED·RESOLVED인 경고를 확인 처리해도 status는 안 내려간다 — 프론트가 목록에서
    아무 줄이나 확인 버튼을 눌러도 닫힌 사건이 되열리지 않는다. 다만 `ack` 불리언은 그
    경우에도 true가 된다(관제가 실제로 누른 건 사실이니까). 그래서 뒤 단계 경고를 확인하면
    status는 그대로인데 ack만 뒤집히고, 그때도 `alert_ack` 메시지는 나간다.

    ⚠ 메시지는 예전 `alert_ack` 한 장 그대로다. `alert_status`를 같이 안 내는 이유는 프론트
    계약 시험(test_dashboard_ws.py)이 이 창구가 내는 메시지 집합을 `["alert_ack"]`로 정확히
    못박아 놨기 때문이다. 세 창구(확인·신원·상태)를 `alert_status` 한 종류로 합치는 건
    프론트 전환과 같이 가야 하는 팀 결정이라 여기선 축만 이어 두고 메시지는 안 건드렸다.

    브로드캐스트는 커밋 뒤에 한다 — 워커가 1개라 send가 트랜잭션을 붙잡으면 전체가 선다(§05).
    """
    alert = await _lock_alert(session, alert_id)
    newly_acked = not alert.ack
    # status 전진과 ack 기록을 같은 함수가 쥔다(alert_lifecycle.apply_transition).
    # 여기서 손으로 ack=True를 대입하면 전이 규칙이 두 자리로 갈라진다.
    apply_transition(
        alert,
        to=AlertStatus.ACKED.value,
        actor=actor.display_name if actor is not None else None,
    )
    await session.commit()
    if newly_acked:
        await manager.broadcast(
            make_envelope(
                "alert_ack",
                {
                    "alert_id": alert.id,
                    "type": alert.type,
                    "severity": alert.severity,
                    "ack": True,
                },
            )
        )
    return _alert_out(alert)


# ── 사후 신원 확인 ──────────────────────────────────────────────────────────

# 경고를 낳은 인입 이벤트를 되짚어 게이트 번호를 얻는 표. assistant_data._SOURCE_MODELS와
# 같은 그림이지만 여기 필요한 건 gate_no 하나라 따로 든다.
_SOURCE_MODELS = {
    "gate_pass": GatePassEvent,
    "tagging": TaggingEvent,
    "shuttle_arrival": ShuttleArrival,
}


async def _lock_alert(session: AsyncSession, alert_id: int) -> Alert:
    """경고 행을 잠그고 가져온다. 없으면 404.

    신원 칸을 만지는 갈래가 셋(입력·정정·삭제)이라 잠그는 자리를 한 함수로 묶는다. 갈래마다
    session.get으로 읽으면 새 갈래가 붙을 때 한 자리가 잠금을 빠뜨린다 — 그 자리가 곧
    409 게이트를 뚫는 경합 창이 된다.

    SELECT ... FOR UPDATE라 같은 경고를 동시에 만지는 요청은 앞 요청이 커밋할 때까지 선다.
    """
    alert = (
        await session.execute(select(Alert).where(Alert.id == alert_id).with_for_update())
    ).scalar_one_or_none()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다.")
    return alert


def _require_untagged(alert: Alert) -> None:
    """미태깅 경고가 아니면 막는다. 신원 칸은 미태깅 통과 대응 흐름 전용이다."""
    if alert.type != ALERT_TYPE_UNTAGGED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="미태깅 경고에만 신원을 적을 수 있습니다.",
        )


async def _link_staff_by_student_no(session: AsyncSession, alert: Alert) -> None:
    """적어 넣은 학번으로 명부 행을 찾아 `alert.staff_id`에 잇는다.

    ⭐ **이 연결이 뜻하는 것** — "이 경고의 통과자가 명부의 그 사람이다". 지금까지는 요원이
    화면에서 눈으로 대조하고 끝이라 서버에 아무 기록도 안 남았다.

    ⚠ **학번을 지우면 연결도 끊는다.** 안 끊으면 "학번은 비었는데 명부에는 이어져 있는"
    행이 남아, 나중에 그 연결을 근거로 신원을 되짚을 때 근거가 사라진 채로 이름만 나온다.

    ⚠ **해제된(`is_active=false`) 행도 잇는다.** 해제는 삭제가 아니라 "더 이상 등록 인원이
    아니다"라서, 그 사람 학번으로 경고가 났을 때야말로 관제가 이름을 알아야 한다
    (`staff.read_staff_by_tag`가 같은 잣대를 쓴다).

    ⚠ **같은 학번이 여럿이면 안 잇는다.** 명부 `student_no`에는 유니크 제약이 없다(태그와
    다르다). 둘 중 아무나 고르면 **엉뚱한 사람이 무단 통과자로 기록된다** — 못 고르는 것이
    잘못 고르는 것보다 낫다.

    ⚠ 로그에 학번을 안 찍는다. 남기는 건 명부 행 번호까지다(`by-tag`와 같은 규칙).
    """
    student_no = alert.identified_student_no
    if not student_no:
        alert.staff_id = None
        return
    rows = (
        await session.execute(
            select(Staff.id).where(Staff.student_no == student_no).limit(2)
        )
    ).scalars().all()
    if len(rows) != 1:
        # 못 찾았거나 여럿이다. 앞서 이어 둔 값이 있으면 걷는다 — 학번을 고쳤는데 옛
        # 연결이 남으면 그 경고가 계속 딴 사람을 가리킨다.
        alert.staff_id = None
        if len(rows) > 1:
            logger.warning(
                "학번이 명부에 여럿이라 경고 %s를 안 이었다 — 명부를 정리해야 한다",
                alert.id,
            )
        return
    alert.staff_id = rows[0]
    logger.info("경고 %s를 명부 %s에 학번으로 이었다", alert.id, rows[0])


async def _broadcast_identified(alert: Alert, *, corrected: bool) -> None:
    """신원 상태가 바뀐 걸 붙어 있는 대시보드 전부에 알린다.

    ack가 S15P11C207-81에서 고친 것과 같은 문제다 — 대시보드가 두 대 열려 있으면 한쪽에서
    적은 신원이 다른 쪽엔 안 보여서, 둘째 요원이 같은 건에 붙었다가 409를 맞는다. 실수로
    고쳐 적기를 켜면 앞 기록이 사라진다.

    ⚠ payload에 신원 원문·요원 이름을 싣지 마라. `/ws/dashboard`는 `AUTH_REQUIRE_LOGIN`이
    꺼진 동안 인증이 없다(켜면 세션 쿠키를 본다 — `security.py`의 `ws_dashboard_allowed`).
    **켠 뒤에도 이 값은 안 싣는다** — 화면에 뿌릴 이유가 없고, 브로드캐스트는 붙어 있는 모든
    세션이 함께 받는 자리라 급이 낮은 요원에게도 그대로 간다.

    ⚠ `staff_linked`는 위 금지에 안 걸린다. 이름도 명부 행 번호도 아닌 불리언 하나이고,
    이미 싣고 있는 `identified`와 같은 급이다(적혔나 / 이었나). 화면이 자기 명부 사본으로
    이름을 말하다 서버 판정과 어긋나는 것을 막는 값이라 실시간으로 가야 뜻이 있다.
    """
    await manager.broadcast(
        make_envelope(
            "alert_identified",
            {
                "alert_id": alert.id,
                "type": alert.type,
                "severity": alert.severity,
                "identified": alert.identified_at is not None,
                "identified_at": (
                    alert.identified_at.isoformat() if alert.identified_at else None
                ),
                "corrected": corrected,
                # ⭐ 2026-08-06 · 프론트 31차 §4-2. 같은 학번이 명부에 여럿이면 서버는
                # 일부러 안 잇는데, 화면은 그 사실을 몰라 아무나 하나를 골라 말할 수 있다.
                "staff_linked": alert.staff_id is not None,
            },
        )
    )


async def _source_gate_no(session: AsyncSession, alert: Alert) -> int | None:
    """경고의 발화 이벤트에서 게이트 번호를 되짚는다. 못 찾으면 None(보고는 그대로 나간다).

    경고 행에는 게이트 번호가 없다 — (source_type, source_id)가 이벤트의 (kind, id)라는
    역추적 규칙을 그대로 쓴다.
    """
    model = _SOURCE_MODELS.get(alert.source_type or "")
    if model is None or alert.source_id is None:
        return None
    row = await session.get(model, alert.source_id)
    return None if row is None else row.gate_no


@router.post("/api/alerts/{alert_id}/identify", response_model=AlertOut)
async def identify_alert(
    alert_id: int,
    body: AlertIdentifyIn,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_db),
    actor: AuthActor | None = Depends(_agent_or_key),
) -> AlertOut:
    """미태깅 경고에 사후 확인된 신원을 붙이고, 상위 보고선 채널로 카드를 보낸다.

    미태깅 통과는 태그를 안 댔으니 시스템에 신원 근거가 없다. 게이트에서 물리로 막지 않고
    흘려보낸 뒤 안쪽 보안 요원이 붙잡아 확인하고, 그 결과를 여기로 적어 넣는다.

    계약.
    - X-API-Key가 있어야 한다. 감사 기록에 쓰는 갈래를 인터넷에 열어 두면 아무나 위조
      신원을 적고 상위 보고선으로 카드를 쏜다. `AUTH_REQUIRE_LOGIN`을 켜면 요원 세션이
      그 자리를 맡는다(§3).
    - ⭐ 켜진 구간에서는 본문 `identified_by`를 **무시하고 세션 실명으로 덮는다.** 공용 키
      하나로는 "정말 그 사람인가"가 안 서서 남의 이름을 적어 넣는 길이 열려 있었다. 덮는
      값은 상위 보고선 카드에도 그대로 실린다 — 카드가 곧 감사 기록이라 두 값이 갈리면 안 된다.
    - 없는 경고 번호면 404. ack와 같은 규칙이다.
    - 미태깅(type="untagged") 경고가 아니면 400. 카드 제목이 "미태깅 통과자 신원 확인"으로
      고정이라, 로봇 도착 알림 같은 딴 경고에 붙으면 상위 보고선이 엉뚱한 사건을 받는다.
      화면에서만 미태깅 줄로 좁혀 두면 계약이 화면 쪽에만 있는 셈이다.
    - 이미 신원이 적힌 경고는 409다. overwrite=true를 명시해야 고쳐 적힌다. 뒤엣값이
      조용히 이기게 두면, 두 요원이 서로 다른 신원을 적었을 때 앞 기록이 흔적 없이
      사라진다 — 이건 상위 보고선으로 나가는 감사 기록이라 그 소실이 제일 위험하다.
      오타 정정 경로는 필요하니 막지 않고 명시 플래그로 연다. 정정으로 들어온 건은
      보고 카드에도 "(정정)"으로 찍혀서 받는 쪽이 두 번째 값임을 안다.
    - overwrite여도 **안 보낸 칸은 안 건드린다**. 요청 본문에 실린 칸만 갈아 끼운다.
      네 칸을 무조건 대입하면 person만 담아 정정했을 때 요원 이름과 메모가 같이 지워진다.
    - ack는 안 건드린다. "확인했다(ack)"와 "누구였는지 적었다(identify)"는 다른 사건이라
      묶으면 한쪽만 하려던 조작이 다른 쪽까지 끌고 간다. 관제실에서 확인을 누르는 사람과
      게이트 안쪽에서 붙잡아 신원을 적는 요원은 다른 사람이다.
    - 대신 수명주기(status)를 IDENTIFIED로 전진시킨다. 이게 C10이 없다고 지적한 그 축이다 —
      "어디까지 진행됐나"는 status가 답하고 "관제가 봤나"는 ack가 답한다. 이미 RESOLVED면
      안 내린다(전진 전용).
    - 응답에 신원 원문을 안 되돌려준다. 방금 부른 쪽이 이미 아는 값이라 실을 이유가 없고,
      응답 본문은 프록시 로그·브라우저 캐시에 남을 자리가 더 많다. 다시 읽어야 하면
      GET /api/alerts/{alert_id}/identify(같은 인증)를 쓴다.

    동시성 — 경고 행을 SELECT ... FOR UPDATE로 잠그고 409를 판정한다. 잠금이 없으면
    "읽고 → 검사하고 → 쓰는" 사이에 다른 요청이 끼어들어 409 게이트를 통째로 지나간다
    (실측 — 같은 경고에 동시 6건을 쏘니 200이 다섯, DB엔 임의의 한 명만 남고 보고선엔
    서로 다른 신원 카드가 "(정정)" 표시 없이 다섯 장 붙었다).

    웹훅은 커밋 뒤에, 그것도 응답을 돌려준 **뒤에** 부른다(BackgroundTasks). 요청 안에서
    기다리면 느린 채널 하나가 요원의 화면을 그 시간만큼 붙잡는다(실측 3.12초). 그래서
    웹훅이 죽어도 입력은 이미 남아 있고 응답은 200이다.

    ⚠ 로그에 신원 값을 찍지 마라. 여기서 남기는 건 경고 번호까지다.
    """
    alert = await _lock_alert(session, alert_id)
    _require_untagged(alert)

    corrected = alert.identified_at is not None
    if corrected and not body.overwrite:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="이미 신원이 적힌 알림입니다. 고쳐 적으려면 overwrite를 true로 보내세요.",
        )

    # 안 보낸 칸은 앞 값을 그대로 둔다. 빈 값으로 **보낸** 칸만 지운다.
    sent = body.model_fields_set
    alert.identified_person = body.person
    # ⭐ 학번은 **보낸 때만** 건드린다(2026-08-06). 안 보낸 칸을 덮으면 정정으로 이름만
    # 고쳐 보낸 요청이 앞서 적힌 학번을 지운다 — `note`와 같은 잣대다.
    if "student_no" in sent:
        alert.identified_student_no = body.student_no
        # ⭐ **명부와 잇는다**(프론트 27차 §3-2). 지금까지 화면이 눈으로만 대조했다.
        #
        # ⚠ **조회 창구를 따로 안 만든 것이 설계다.** "이 학번이 명부에 있나"를 묻는 창구를
        # 열면 요원이 학번을 순회해 **명부를 통째로 긁는다** — `by-tag`가 "활성 경고에 붙은
        # 태그만"으로 막은 바로 그 자리인데, 학번에는 그런 제약을 걸 근거가 없다(요원이
        # 명부에 없는 사람의 학번을 칠 수도 있어야 한다).
        #
        # 여기서 하면 열거가 실질적으로 막힌다 — 적으려면 미태깅 경고가 있어야 하고,
        # 같은 건에 다시 적으려면 `overwrite`라 **시도가 전부 감사에 남는다.**
        await _link_staff_by_student_no(session, alert)
    if actor is not None:
        # 세션이 있으면 "누가 적었나"는 본문이 아니라 서버가 안다. 안 보냈든 남의 이름을
        # 보냈든 상관없이 세션 실명으로 덮는다 — 정정으로 들어온 건도 실제로 고쳐 적은
        # 사람이 새 값이라, 여기서 앞 값을 남기면 기록이 엉뚱한 사람을 가리킨다.
        alert.identified_by = actor.display_name
    elif "identified_by" in sent:
        alert.identified_by = body.identified_by
    if "note" in sent:
        alert.identify_note = body.note
    alert.identified_at = dt.datetime.now(dt.timezone.utc)
    # 수명주기 전진. 이미 RESOLVED면 apply_transition이 아무것도 안 바꾼다(전진만 된다).
    apply_transition(
        alert,
        to=AlertStatus.IDENTIFIED.value,
        actor=alert.identified_by,
        now=alert.identified_at,
    )
    gate_no = await _source_gate_no(session, alert)
    await session.commit()

    logger.info(
        "경고 %s에 신원이 적혔다(정정=%s, 신원 길이=%s자)",
        alert.id,
        corrected,
        len(body.person),
    )
    await _broadcast_identified(alert, corrected=corrected)
    # 카드에는 병합 뒤 값을 싣는다 — 요청 본문만 보면 정정 때 안 보낸 칸이 "미기재"로 나간다.
    background.add_task(
        send_identify_report,
        gate_no=gate_no,
        alert_id=alert.id,
        identified_person=alert.identified_person,
        identified_student_no=alert.identified_student_no,
        identified_by=alert.identified_by,
        identified_at=alert.identified_at,
        note=alert.identify_note,
        corrected=corrected,
    )
    return _alert_out(alert)


@router.get(
    "/api/alerts/by-student-no/{student_no}",
    response_model=list[AlertOut],
    dependencies=[Depends(_agent_or_key)],
)
async def read_alerts_by_student_no(
    student_no: str,
    limit: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_db),
) -> list[AlertOut]:
    """한 학번으로 적힌 경고를 최근 것부터 준다(프론트 27차 §3-2 · 사용자 확정).

    ⭐ **"이 사람이 전에도 무단 통과했나"**를 요원이 그 자리에서 보는 창구다. 이름으로는
    동명이인이 섞여서 못 하고, 학번을 나눠 든 뒤에야 열린 자리다.

    ⛔ **인증이 필수다**(`_agent_or_key`). 익명에 열면 **"이 학번이 무단 통과했나"를 아무나
    물어볼 수 있다** — 목록 창구(`GET /api/alerts`)가 신원 칸을 통째로 빼는 것과 같은
    잣대이고, 그쪽은 플래그가 꺼진 구간에 익명이라 여기에 붙일 수 없었다.

    ⚠ **응답에 신원 원문은 안 실린다**(`AlertOut`). 부르는 쪽이 이미 학번을 알고 물었으니
    이름까지 얹을 이유가 없고, 얹으면 이 창구 하나로 "학번 → 이름"이 뽑힌다.

    ⚠ 실패를 갈래로 안 가른다 — 없는 학번도 빈 목록이다. 404 로 가르면 "그 학번은 있는데
    경고가 없다"와 "그 학번 자체가 없다"가 밖에서 갈려 열거 실마리가 된다.
    """
    key = student_no.strip()
    if not key or len(key) > STUDENT_NO_MAX_LEN:
        return []
    rows = (
        await session.execute(
            select(Alert)
            .where(Alert.identified_student_no == key)
            .order_by(Alert.created_at.desc(), Alert.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    # ⚠ 로그에 학번을 안 찍는다. 몇 건이 나갔나까지다(`by-tag`와 같은 규칙).
    logger.info("학번으로 경고 %s건을 조회했다", len(rows))
    return [_alert_out(row) for row in rows]


@router.get(
    "/api/alerts/{alert_id}/identify",
    response_model=AlertIdentityOut,
    dependencies=[Depends(_agent_or_key)],
)
async def read_identity(
    alert_id: int, session: AsyncSession = Depends(get_db)
) -> AlertIdentityOut:
    """적어 넣은 신원 원문을 되읽는다. 인증이 걸린 유일한 자리다.

    이게 없으면 신원이 write-only가 된다 — 보고 웹훅이 꺼진 배포에서는 적어 넣은 값을 읽을
    수 있는 사람이 아무도 없는 채로 개인정보만 쌓인다(운영 실측). 409를 맞은 요원이
    "이미 뭐라고 적혀 있나"를 보고 덮어쓸지 판단하는 자리이기도 하다.

    ⚠ 이 응답만 원문을 싣는다. 인증 없는 목록·스냅샷에는 여전히 안 나간다.
    """
    alert = await session.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="알림을 찾을 수 없습니다.")
    return AlertIdentityOut(
        alert_id=alert.id,
        identified=alert.identified_at is not None,
        identified_person=alert.identified_person,
        identified_student_no=alert.identified_student_no,
        identified_by=alert.identified_by,
        identified_at=alert.identified_at,
        identify_note=alert.identify_note,
        staff_linked=alert.staff_id is not None,
    )


def _masked_identity(
    person: str | None, note: str | None, student_no: str | None = None
) -> dict:
    """감사에 남길 신원 사본. **원문은 안 담는다** — 있었다는 사실과 크기만 담는다.

    `staff_audit`에는 삭제 창구가 없다(계약 §4 — append-only는 창구 부재로 지킨다). 그래서
    지운 원문을 그대로 복사하면, 파기했다고 믿은 신원이 감사 표에 영구히 남아 이 창구가
    유일한 파기 수단이라는 말이 거짓이 된다. 지우는 행위를 기록하려고 지운 값을 보존하면
    앞뒤가 안 맞는다.

    그래도 "무엇이 지워졌나"는 남는다 — 이름은 첫 글자와 길이, 메모는 길이다. 감사 목적
    (빈 값을 지운 척한 기록인가 · 어느 건을 지웠나 · 되짚을 때 당사자가 맞나)에는 이 정도가
    닿고, 이 값만으로 사람을 되짚지는 못한다.

    칸 이름을 그대로 두고 값 모양만 바꾼다. 값이 dict라 옛 행(문자열)과 새 행이 읽는 쪽에서
    한눈에 갈린다 — 마스킹한 값을 원문으로 착각할 여지가 없다.
    """
    return {
        "identified_person": (
            None if not person else {"initial": person[0], "length": len(person)}
        ),
        # ⛔ **학번은 첫 글자도 안 남긴다**(2026-08-06). 이름의 첫 글자는 수만 명 중 하나를
        # 좁힐 뿐이지만, 학번은 앞자리가 **입학 연도·학과**라 몇 글자만으로 사람이 좁혀진다.
        # 길이만 남겨도 "지운 척한 빈 값인가"는 가려진다.
        "identified_student_no": None if not student_no else {"length": len(student_no)},
        "identify_note": None if not note else {"length": len(note)},
    }


@router.delete("/api/alerts/{alert_id}/identify", response_model=AlertOut)
async def clear_identity(
    alert_id: int,
    session: AsyncSession = Depends(get_db),
    actor: AuthActor | None = Depends(_leader_or_key),
) -> AlertOut:
    """잘못 적은 신원을 지운다(칸 넷을 NULL로 되돌린다).

    ⭐ 이 창구만 **팀장급**이다(§8 결정 5 확정). 감사 기록을 되돌리는 유일한 갈래라
    "기록 수정·삭제 창구를 안 만든다"는 정본 §5-4와 긴장이 있어서, 창구는 남기되 급을
    올렸다. 켜기 전에는 예전대로 기기 키 하나다.

    이 갈래가 없으면 되돌릴 길이 alembic downgrade(컬럼 통째 drop)뿐이었다. person은
    빈 값을 못 받으니 "다른 값으로 덮기"만 되고 NULL 복귀 경로가 아예 없었다.

    보관 기간·일괄 파기 규칙은 아직 없다 — 시연이 끝나면 이 창구로 지우는 게 지금의 유일한
    파기 수단이다. 자동 파기 잡을 둘지는 팀 결정이다(backend/README.md "아직 아닌 것").

    ⭐ **지운 행위 자체를 감사 기록에 남긴다**(§8 결정 5 후반). 이 창구는 신원 기록을
    되돌리는 유일한 갈래라, 흔적이 없으면 "누가 언제 무엇을 지웠나"가 통째로 사라진다.
    `staff_audit`에 같은 트랜잭션으로 얹고, 명부 행이 아니라서 `staff_id=0`을 쓴다
    (`routers/staff.ACCOUNT_AUDIT_STAFF_ID`와 같은 규칙 — 진짜 대상은 본문에 있다).

    이미 비어 있는 경고를 지워도 200이다(멱등). 지운 사실은 대시보드에도 알린다.
    ⚠ 감사 행은 실제로 지운 값이 있었을 때(`had`)만 남긴다 — 멱등 재호출까지 세면 기록이
    "지운 적 없는 삭제"로 부풀어 감사 값이 떨어진다.

    수명주기는 IDENTIFIED였을 때만 ACKED로 내려온다. 수명주기 축에서 유일한 후퇴이고,
    신원이 없는데 IDENTIFIED로 남는 거짓말을 막는 자리다. 이미 RESOLVED인 사건은 안
    건드린다 — 신원을 정정하려고 지우는 것과 닫힌 사건을 되열는 건 다른 조작이다.

    ⭐ **게이트와 행위자를 한 의존성에서 같이 받는다.** 예전에는 게이트가 `_leader_or_key`,
    감사에 적는 행위자가 `require_leader`로 갈려 있었다. 플래그가 꺼진 구간에서
    `require_leader`는 판정을 아예 안 하고 쿠키 주인만 알려 주므로, 기기 키로 통과한 요청에
    요원 쿠키가 얹혀 있으면 **급 판정을 한 번도 안 거친 사람**이 감사에 행위자로 실렸다.
    지금은 갈래 둘이 한 함수에서 나온다 — 세션 갈래는 팀장 판정을 통과한 사람이고, 키
    갈래는 `None`(= 사람 아님)이라 행위자 네 칸이 NULL로 남는다. 그 NULL 자체가 "로그인
    전에 기기 키로 벌어진 일"이라는 기록이다(`staff.add_audit` 계약).

    ⚠ 감사 사본(`before`)에는 **신원 원문을 안 담는다**(`_masked_identity`).
    """
    # ⚠ 상수를 여기서 늦게 부른다. `app/routers/staff.py`가 이 파일의 `EventKind`를 임포트해서
    # 위쪽에 적으면 순환이 된다. 그래도 리터럴 0을 손으로 적는 것보다 낫다 — 0은 계정 감사와
    # 신원 삭제 기록이 같이 쓰는 "명부 행 아님" 표식이라, 그 값이 바뀌면 이 자리만 남는다.
    from app.routers.staff import ACCOUNT_AUDIT_STAFF_ID  # noqa: PLC0415 - 순환 회피

    alert = await _lock_alert(session, alert_id)
    had = alert.identified_at is not None
    before = {
        # 요원 실명은 "누가 적었나"라 그대로 둔다 — 감사 행의 행위자 칸과 같은 계열이고,
        # 지우려는 대상(통과자 신원)이 아니다.
        "identified_by": alert.identified_by,
        **_masked_identity(
            alert.identified_person, alert.identify_note, alert.identified_student_no
        ),
    }
    alert.identified_person = None
    # ⛔ **학번도 같이 지운다**(2026-08-06). 이 창구가 신원 파기의 유일한 수단인데 한 칸이라도
    # 남으면 "지웠다"가 거짓이 된다. 새 신원 칸을 붙일 때 여기를 같이 안 고치면 그 값만
    # 조용히 살아남는다.
    alert.identified_student_no = None
    # ⛔ **명부 연결도 끊는다.** 학번으로 이어 둔 값이라, 학번을 지우고 연결을 남기면
    # **파기했다는 신원이 명부 행을 통해 그대로 되짚힌다.** 지우는 뜻이 무너진다.
    alert.staff_id = None
    alert.identified_by = None
    alert.identify_note = None
    alert.identified_at = None
    demote_after_identity_cleared(alert)
    if had:
        # 커밋 전에 얹는다 — 변경과 기록이 한 트랜잭션이라 한쪽만 남는 갈라짐이 없다.
        session.add(
            StaffAudit(
                staff_id=ACCOUNT_AUDIT_STAFF_ID,
                action="identity_clear",
                actor_user_id=actor.id if actor else None,
                actor_username=actor.username if actor else None,
                actor_display_name=actor.display_name if actor else None,
                actor_role=actor.role if actor else None,
                before=before,
                after={"target": "alert_identity", "alert_id": alert.id},
            )
        )
    await session.commit()
    if had:
        logger.info("경고 %s의 신원 기록을 지웠다", alert.id)
        await _broadcast_identified(alert, corrected=False)
    return _alert_out(alert)
