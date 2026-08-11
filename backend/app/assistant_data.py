"""관제 보조가 쓸 사실을 DB에서 모은다. LLM은 여기 결과만 보고 문장을 쓴다.

**여기가 진실이고 LLM은 옮겨 적는 자리다.** 그래서 수치는 전부 SQL 집계로 뽑고 프롬프트엔
이미 계산된 값만 실어 보낸다 — LLM한테 원시 행을 던져 세게 하면 건수가 틀린다.

프라이버시 — 태그 UID 원문은 프롬프트로 나가지 않는다. 여기서 mask_tag_id로 가린 사본만
사실 딕셔너리에 담는다(준비패키지 §2 정직 라인).

시각은 화면·프롬프트 모두 KST로 맞춘다. DB는 timestamptz라 값 자체는 안 흔들리고,
"오늘 하루"의 경계만 KST 자정으로 잡으면 된다.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_bridge import mask_tag_id
from app import auth
from app.config import get_settings
from app.models import (
    Alert,
    DailyDefaultDestination,
    GatePassEvent,
    Robot,
    ShuttleArrival,
    TaggingEvent,
)
from app.schemas import CommStatus

KST = dt.timezone(dt.timedelta(hours=9))
# 같은 게이트 이력을 얼마나 뒤로 볼까. 요원이 "방금 전 상황"으로 읽는 폭이다.
BRIEF_WINDOW_MIN = 60
# 챗봇 컨텍스트 상한. 늘리면 프롬프트가 커지고 답이 늘어져서 짧게 잡는다.
CHAT_EVENT_LIMIT = 15
CHAT_ALERT_LIMIT = 10

_SOURCE_MODELS = {
    "gate_pass": GatePassEvent,
    "shuttle_arrival": ShuttleArrival,
    "tagging": TaggingEvent,
}


def today_kst() -> dt.date:
    return dt.datetime.now(KST).date()


def _kst_window(day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    """KST 기준 그 날 00:00 ~ 다음 날 00:00. 끝은 열린 구간이다."""
    start = dt.datetime(day.year, day.month, day.day, tzinfo=KST)
    return start, start + dt.timedelta(days=1)


def _iso_kst(value: dt.datetime | None) -> str | None:
    """timestamptz를 KST 표기 ISO 문자열로. tz가 없는 값은 UTC로 본다(DB 계약상 안 온다)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(KST).isoformat()


# ── ① 일일 요약 ──────────────────────────────────────────────────────────

async def collect_daily_facts(session: AsyncSession, day: dt.date) -> dict:
    """하루치 집계. 건수·판정·경고·게이트별·최다 시간대·로봇 현황."""
    start, end = _kst_window(day)

    counts: dict[str, int] = {}
    for label, model, ts_col in (
        ("tagging", TaggingEvent, TaggingEvent.received_at),
        ("gate_pass", GatePassEvent, GatePassEvent.received_at),
        ("shuttle_arrival", ShuttleArrival, ShuttleArrival.received_at),
    ):
        stmt = select(func.count()).select_from(model).where(ts_col >= start, ts_col < end)
        counts[label] = (await session.execute(stmt)).scalar_one()
    counts["total"] = sum(counts.values())

    # 통과 판정 분포. 미판정(null)은 세지 않는다 — "판정이 안 붙었다"와 "정상"은 다른 말이다.
    verdict_rows = (
        await session.execute(
            select(GatePassEvent.verdict, func.count())
            .where(
                GatePassEvent.received_at >= start,
                GatePassEvent.received_at < end,
                GatePassEvent.verdict.is_not(None),
            )
            .group_by(GatePassEvent.verdict)
        )
    ).all()
    verdicts = {row[0]: row[1] for row in verdict_rows}

    alert_total = (
        await session.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.created_at >= start, Alert.created_at < end)
        )
    ).scalar_one()
    alert_active = (
        await session.execute(
            select(func.count())
            .select_from(Alert)
            .where(Alert.created_at >= start, Alert.created_at < end, Alert.ack.is_(False))
        )
    ).scalar_one()
    alert_type_rows = (
        await session.execute(
            select(Alert.type, func.count())
            .where(Alert.created_at >= start, Alert.created_at < end)
            .group_by(Alert.type)
        )
    ).all()

    # 게이트별 통과·미태깅. 미태깅 0인 게이트도 남긴다 — "여긴 조용했다"도 사실이다.
    gate_rows = (
        await session.execute(
            select(
                GatePassEvent.gate_no,
                func.count(),
                func.count().filter(GatePassEvent.verdict == "untagged"),
            )
            .where(GatePassEvent.received_at >= start, GatePassEvent.received_at < end)
            .group_by(GatePassEvent.gate_no)
            .order_by(GatePassEvent.gate_no)
        )
    ).all()

    peak = await _peak_hour(session, start, end)

    robot_total = (await session.execute(select(func.count()).select_from(Robot))).scalar_one()
    # ⛔ **`"online"`은 계약에 없는 값이었다**(2026-08-06 · 팀원 교차 검증에서 잡힘).
    # 통신 상태 낱말은 `CommStatus` 셋(`WS_OK`·`POLLING_GRACE`·`SERVER_DOWN`)뿐이라 이
    # 비교는 **어떤 로봇이 붙어 있어도 늘 0**이었다. 브리핑이 "온라인 로봇 0대"를 계속
    # 말하는데 그게 사실이 아니라 죽은 비교였다.
    #
    # ⚠ 낱말을 글자로 박지 않고 enum에서 가져온다. 값이 바뀌면 여기가 조용히 틀리는 게 아니라
    # import 자리에서 드러난다.
    #
    # ⛔ **낱말만 보면 안 된다 — 시각을 같이 본다**(2026-08-07 · 프론트 48차가 잡음).
    # `network_status`는 **마지막 프레임을 받았을 때 찍히고 스스로 안 내려간다.** 로봇이
    # 꺼져도 `WS_OK`가 그대로 남는다. 실제로 로봇이 끊긴 지 **8시간 51분** 뒤에도 브리핑이
    # "1대가 온라인"이라 말하고 있었고, 같은 순간 화면은 "신호 지연"이라 적어 **두 곳이
    # 다른 말을 했다.**
    #
    # ⭐ 문턱은 `robot_stale_sec`(25초) 하나다 — 화면 `STALE_S` 와 같은 값이다.
    #
    # ⛔ **처음에 `ws_heartbeat_sec + 5`(20초)로 잡았다가 AI 갈래가 잡았다.** 주석에는
    # "화면과 같은 식"이라 적어 놓고 실제로는 **방향이 반대인 설정**을 썼다 —
    # `ws_heartbeat_sec` 은 서버가 로봇을 찌르는 주기고, 화면이 보는 건 로봇 보고가
    # 얼마나 뜸한가다. 5초가 어긋나서 **20~25초 구간에서 일일 요약과 챗봇이 다른 대수를
    # 말했다.** 갈림을 없애려던 수리가 갈림을 한 칸 늘렸다.
    # ⭐ **"같은 식"이 아니라 "같은 값"을 봐야 한다** — 식이 같아 보여도 넣는 설정이 다르면
    # 결과가 갈린다.
    seen_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
        seconds=get_settings().robot_stale_sec
    )
    robot_online = (
        await session.execute(
            select(func.count())
            .select_from(Robot)
            .where(
                Robot.network_status == CommStatus.WS_OK.value,
                Robot.updated_at >= seen_cutoff,
            )
        )
    ).scalar_one()

    return {
        "date": day.isoformat(),
        "timezone": "KST",
        "events": counts,
        "verdicts": verdicts,
        "alerts": {
            "total": alert_total,
            "active": alert_active,
            "by_type": {row[0]: row[1] for row in alert_type_rows},
        },
        "gates": [
            {"gate_no": row[0], "passes": row[1], "untagged": row[2]} for row in gate_rows
        ],
        "peak_hour": peak,
        "robots": {"total": robot_total, "online": robot_online},
    }


async def _peak_hour(session: AsyncSession, start, end) -> dict | None:
    """이벤트가 가장 몰린 KST 시각(0~23). 인입 3계열을 합쳐 센다."""
    tally: dict[int, int] = {}
    for model, ts_col in (
        (TaggingEvent, TaggingEvent.received_at),
        (GatePassEvent, GatePassEvent.received_at),
        (ShuttleArrival, ShuttleArrival.received_at),
    ):
        # timestamptz를 KST로 옮긴 뒤 시각을 뽑는다. 서버 로캘에 안 흔들리게 존을 못박는다.
        hour = func.date_part("hour", func.timezone("Asia/Seoul", ts_col))
        rows = (
            await session.execute(
                select(hour, func.count())
                .select_from(model)
                .where(ts_col >= start, ts_col < end)
                .group_by(hour)
            )
        ).all()
        for h, n in rows:
            tally[int(h)] = tally.get(int(h), 0) + n
    if not tally:
        return None
    # 동률이면 이른 시각을 고른다(브리핑이 매번 다른 시간을 집는 걸 막는다).
    hour = min(tally, key=lambda h: (-tally[h], h))
    return {"hour": hour, "events": tally[hour]}


# ── ② 이상 상황 브리핑 ───────────────────────────────────────────────────

async def collect_alert_facts(session: AsyncSession, alert: Alert) -> dict:
    """경고 한 건 + 발화 이벤트 + 같은 게이트 최근 이력."""
    source = await _load_source(session, alert)
    gate_no = source.get("gate_no") if source else None

    since = alert.created_at - dt.timedelta(minutes=BRIEF_WINDOW_MIN)
    # ⚠ Alert 테이블엔 게이트 컬럼이 없어 경고 수는 "관제 전체" 기준이다 — 같은 게이트로
    #   좁혀 말하면 거짓 브리핑이 된다. 개수는 잘리지 않게 count로 정확히 세고,
    #   맥락 목록(prior_alerts)만 최근 5건으로 자른다.
    prior_count_stmt = select(func.count()).select_from(Alert).where(
        Alert.id != alert.id, Alert.created_at >= since, Alert.created_at <= alert.created_at
    )
    prior_count = (await session.execute(prior_count_stmt)).scalar_one()
    prior_stmt = (
        select(Alert)
        .where(Alert.id != alert.id, Alert.created_at >= since, Alert.created_at <= alert.created_at)
        .order_by(Alert.created_at.desc(), Alert.id.desc())
        .limit(5)
    )
    prior = (await session.execute(prior_stmt)).scalars().all()

    pass_stmt = select(func.count(), func.count().filter(GatePassEvent.verdict == "untagged")).where(
        GatePassEvent.received_at >= since, GatePassEvent.received_at <= alert.created_at
    )
    if gate_no is not None:
        pass_stmt = pass_stmt.where(GatePassEvent.gate_no == gate_no)
    passes, untagged = (await session.execute(pass_stmt)).one()

    return {
        "alert": {
            "id": alert.id,
            "type": alert.type,
            "severity": alert.severity,
            "ack": alert.ack,
            # ⛔ **수명주기 낱말을 같이 싣는다**(2026-08-09 백지검토 P2).
            # 프롬프트는 OPEN·ACKED·IDENTIFIED·RESOLVED 넷을 가르치는데 재료는 `ack`
            # 하나뿐이었다. 계약상 **OPEN에서 RESOLVED로 건너뛴 종결은 ack를 안 찍으므로**
            # (app/models.py Alert 수명주기 주석 · app/alert_lifecycle.py), 이미 닫힌 경고가
            # "아직 확인 안 됨"으로 브리핑됐다. `ack == (status != 'OPEN')`은 틀린 불변식이라
            # 여기서 파생시키면 안 되고, 두 값을 나란히 줘야 한다.
            # ⚠ 질의가 안 는다 — `alert` 행을 이미 통째로 들고 있다.
            "status": alert.status,
            # 왜 닫았나(confirmed/false_positive/duplicate/other). 안 닫힌 건은 None이다.
            "resolution": alert.resolution,
            "created_at": _iso_kst(alert.created_at),
        },
        "gate_no": gate_no,
        "source": source,
        "recent_context": {
            "window_min": BRIEF_WINDOW_MIN,
            # 경고 수는 관제 전체 기준(게이트 구분 불가), 통과·미태깅 수는
            # gate_no를 알 때만 그 게이트로 좁힌다.
            "alerts_all_gates": prior_count,
            "passes": passes,
            "untagged": untagged,
            "gate_scoped_passes": gate_no is not None,
        },
        "prior_alerts": [
            {
                "id": a.id,
                "type": a.type,
                "created_at": _iso_kst(a.created_at),
                "minutes_before": round(
                    (alert.created_at - a.created_at).total_seconds() / 60, 1
                ),
            }
            for a in prior
        ],
    }


async def _load_source(session: AsyncSession, alert: Alert) -> dict | None:
    """경고를 낳은 인입 이벤트를 되짚는다. 못 찾으면 None(브리핑은 그래도 나간다)."""
    model = _SOURCE_MODELS.get(alert.source_type or "")
    if model is None or alert.source_id is None:
        return None
    row = await session.get(model, alert.source_id)
    if row is None:
        return None
    observed = getattr(row, "observed_at", None) or getattr(row, "signal_ts", None)
    return {
        "type": alert.source_type,
        "id": row.id,
        "event_id": row.event_id,
        "gate_no": row.gate_no,
        "observed_at": _iso_kst(observed),
        "direction": getattr(row, "direction", None),
        "verdict": getattr(row, "verdict", None),
        # 원문이 아니라 가린 사본만 나간다.
        "tag_id": mask_tag_id(getattr(row, "tag_id", None)),
    }


# ── ③ 관제 챗봇 컨텍스트 ─────────────────────────────────────────────────

async def _today_weather(session: AsyncSession, day: dt.date) -> dict | None:
    """오늘 날씨. **새로 조회하지 않고 DB에 앉은 값만 읽는다.**

    ⛔ 여기서 기상청을 부르면 안 된다 — 그 사슬은 상류가 느리면 요청 하나가 십수 초
    매달린다(`ensure_today_default` 머리). 챗봇은 답이 빨라야 하는 자리라,
    **오늘 기본 목적지를 정할 때 이미 조회해 둔 값**을 그대로 쓴다.

    ⚠ 아침에 아무도 화면을 안 켰으면 행이 없다. 그때는 `None`이고, 모델은 "날씨 정보가
    안 실렸다"로 답한다 — 지어내는 것보다 낫다.
    """
    row = (
        await session.execute(
            select(DailyDefaultDestination).where(
                DailyDefaultDestination.service_date == day
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "status": row.weather_status,
        "description": row.weather_desc,
        # ⚠ "출동해도 되나"다. 날씨가 맑아도 다른 이유로 거짓일 수 있다.
        "dispatch_ok": row.weather_ok,
        "default_destination": row.destination,
        "decided_by": row.source,   # auto = 날씨가 정함 · manual = 요원이 고름
    }


async def _recent_shuttles(session: AsyncSession, limit: int = 5) -> list[dict]:
    """최근 셔틀 도착. **시각을 준다** — 집계 수만으로는 "언제 왔나"에 못 답한다."""
    rows = (
        await session.execute(
            select(ShuttleArrival)
            .order_by(ShuttleArrival.received_at.desc(), ShuttleArrival.id.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        {
            "shuttle_no": r.shuttle_no,
            "gate_no": r.gate_no,
            # ⚠ 이 표만 칸 이름이 `signal_ts`다 — 태깅·통과는 `observed_at`인데 여기는
            #   다르다. 밖으로 나가는 이름은 다른 표와 맞춰 둔다.
            "observed_at": _iso_kst(r.signal_ts),
            "received_at": _iso_kst(r.received_at),
        }
        for r in rows
    ]


def _day_slice(facts: dict) -> dict:
    """일일 집계에서 챗봇이 쓸 칸만 뽑는다.

    ⚠ `robots`는 뺀다 — 그날치 집계가 아니라 지금 붙어 있는 대수라서, 어제 묶음에 넣으면
    "어제는 1대가 붙어 있었다"로 읽힌다. 로봇 현황은 최상위 `robots`가 따로 싣는다.
    """
    return {k: facts[k] for k in ("date", "verdicts", "events", "alerts", "gates", "peak_hour")}


# 요원이 읽을 한글 이름. ⚠ 전선에 나가는 값은 소문자 영문이고 이 표는 **챗봇이 답을
# 사람 말로 적기 위한 것**이다(`app.auth` 머리 "한글 표시는 화면 몫").
# ⛔ 급을 늘리면 **여기도 같이 늘려야 한다.** 안 늘리면 그 급이 챗봇 답에 소문자 영문
# 그대로 실려 나간다("trainee 님이 물으셨습니다"). 2026-08-08에 교육생을 넣으면서 화면
# 사다리 넷은 맞췄는데 이 표만 빠져 골든셋 확장 팀원이 잡았다 — 사다리 낱말을 추가할 때
# 손대야 하는 자리 목록에 이 표가 들어 있어야 한다(app.auth.RANK 옆 주석 참고).
_ROLE_LABELS = {
    auth.ROLE_TRAINEE: "교육생",
    auth.ROLE_AGENT: "일반 보안요원",
    auth.ROLE_LEADER: "팀장급",
    auth.ROLE_ADMIN: "관리자",
    auth.ROLE_OWNER: "총괄 관리자",
}


def _viewer(actor: object | None) -> dict | None:
    """지금 묻는 사람이 누구인가. 로그인 안 했으면 `None`이다.

    ⭐ **왜 좁히기가 아니라 알려 주기인가**(2026-08-07 사용자 지시 "알아서 조정").
    등급별로 자료를 잘라 내려고 지금 실리는 것을 전부 훑었는데 **가릴 것이 없었다.**

      - **묻는 사람 본인의 표시 이름 하나만 싣는다**(`display_name`). 제3자 신원은 한 글자도
        안 나간다 — 이벤트·경고에 붙는 사람 정보는 태그 번호뿐이고 그건 아래 줄이 가린다.
        ⛔ 2026-08-09 백지검토 정정 — 이 줄이 "개인 신원은 애초에 안 싣는다"였는데 **같은
        함수가 실명을 담고 있었다**(`display_name`은 app/models.py 가 "한글 실명"이라 못박은
        칸이다). 칸을 빼는 대신 이 문장을 사실대로 고친 까닭은 **프롬프트가 그 이름으로
        요원을 부르기 때문**이다(ai/control_assistant/prompts.py "display_name 이
        이름입니다", 골든셋도 그 칸을 쥐고 있다). 모델은 사내 GPU에서 도는 온프레미스라
        이름이 바깥 업체로 나가지도 않는다(`/api/assistant/health`가 그 사실을 드러낸다).
      - 태그 번호는 `mask_tag_id`로 이미 가려 나간다.
      - 나머지(로봇·알림·이벤트·집계·날씨·셔틀)는 **요원이 관제 화면에서 그대로 보는 것**이다.
        챗봇만 더 좁히면 화면과 말이 갈리고, 요원이 "화면엔 있는데 왜 못 답하나"를 묻는다.

    ⛔ **그래서 없는 칸막이를 지어내지 않는다.** 대신 **누가 묻는지**를 실어서 챗봇이
    "총괄 관리자시니 개발자 화면까지 보실 수 있습니다"처럼 답하게 한다 — 관제 보조 갈래가
    6차에서 실제로 요청한 것이 이쪽이다.

    ⚠ **`can_*`는 손으로 적지 않고 사다리로 계산한다.** 창구 게이트(`require_leader`·
    `require_admin`)와 같은 잣대라야, 게이트를 옮길 때 챗봇 말이 조용히 낡지 않는다.
    """
    role = getattr(actor, "role", None)
    if not role:
        return None
    rank = auth.RANK.get(role, 0)
    return {
        "role": role,
        "role_label": _ROLE_LABELS.get(role, role),
        "display_name": getattr(actor, "display_name", None),
        # 명부를 고칠 수 있나 — `require_leader` 와 같은 급이다.
        "can_manage_staff": rank >= auth.RANK[auth.ROLE_LEADER],
        # 계정을 만들고 지울 수 있나 · 개발자 화면을 열 수 있나 — `require_admin` 급이다.
        "can_manage_accounts": rank >= auth.RANK[auth.ROLE_ADMIN],
        "can_open_devpage": rank >= auth.RANK[auth.ROLE_ADMIN],
    }


async def collect_chat_facts(session: AsyncSession, actor: object | None = None) -> dict:
    """질문과 무관하게 같은 묶음을 싣는다 — 무엇을 근거로 답했는지 응답이 항상 같은 모양이다.

    질문에 따라 조회 범위를 바꾸려면 툴콜링이 필요한데, 그건 정직 라인 밖이라 안 붙인다.

    ⚠ 최근 목록(`alerts.recent`·`events.recent`)만으로는 "오늘 무단 통과가 몇 건인가"에 못
    답한다 — 상한에 걸린 열댓 건은 오늘 전체가 아니다. 그래서 `today`에 일일 요약과 **같은
    집계**를 같이 싣는다. 모델이 목록을 세는 대신 이 값을 읽게 하려는 것이다.

    ⭐ `yesterday`도 같이 싣는다(AI 갈래 7차 요청) — 시연 자료가 어제 것이고 "어제는
    어땠어"가 실제로 나오는 질문이다. **그저께 이전은 안 싣는다** — 컨텍스트가 부풀고,
    거기까지 물으면 일일 요약 창구를 날짜 인자로 부르는 편이 맞다.
    """
    day = today_kst()
    today = await collect_daily_facts(session, day)
    yesterday = await collect_daily_facts(session, day - dt.timedelta(days=1))
    weather = await _today_weather(session, day)
    shuttles = await _recent_shuttles(session)
    robots = (await session.execute(select(Robot).order_by(Robot.id))).scalars().all()
    alerts = (
        await session.execute(
            select(Alert).order_by(Alert.created_at.desc(), Alert.id.desc()).limit(CHAT_ALERT_LIMIT)
        )
    ).scalars().all()
    active = (
        await session.execute(
            select(func.count()).select_from(Alert).where(Alert.ack.is_(False))
        )
    ).scalar_one()

    gate_rows = (
        await session.execute(
            select(GatePassEvent)
            .order_by(GatePassEvent.received_at.desc(), GatePassEvent.id.desc())
            .limit(CHAT_EVENT_LIMIT)
        )
    ).scalars().all()
    tag_rows = (
        await session.execute(
            select(TaggingEvent)
            .order_by(TaggingEvent.received_at.desc(), TaggingEvent.id.desc())
            .limit(CHAT_EVENT_LIMIT)
        )
    ).scalars().all()

    recent = [
        {
            "kind": "gate_pass",
            "gate_no": r.gate_no,
            "observed_at": _iso_kst(r.observed_at),
            "received_at": _iso_kst(r.received_at),
            "direction": r.direction,
            "verdict": r.verdict,
        }
        for r in gate_rows
    ] + [
        {
            "kind": "tagging",
            "gate_no": r.gate_no,
            "observed_at": _iso_kst(r.observed_at),
            "received_at": _iso_kst(r.received_at),
            "tag_id": mask_tag_id(r.tag_id),
        }
        for r in tag_rows
    ]
    recent.sort(key=lambda e: e["received_at"] or "", reverse=True)
    recent = recent[:CHAT_EVENT_LIMIT]

    return {
        "robots": [
            {
                "name": r.name,
                "mode": r.mode,
                "battery": r.battery,
                "network_status": r.network_status,
                "updated_at": _iso_kst(r.updated_at),
            }
            for r in robots
        ],
        "alerts": {
            "active": active,
            "recent": [
                {
                    "id": a.id,
                    "type": a.type,
                    "severity": a.severity,
                    "ack": a.ack,
                    # ⛔ 위 `collect_alert_facts`와 같은 까닭이다 — ack만 보면 종결된 건을
                    #    "미확인"이라 답한다.
                    # ⚠ 여기는 `resolution`까지는 안 싣는다. 목록이 열 건이라 거의 다 None인
                    #    칸을 열 번 실으면 프롬프트만 길어지고(프리필이 지연의 절반이다,
                    #    ai/control_assistant/facts_select.py 머리), "닫혔나"는 status로 이미
                    #    답이 된다. 왜 닫았나까지는 경고 한 건 브리핑이 싣는다.
                    "status": a.status,
                    "created_at": _iso_kst(a.created_at),
                }
                for a in alerts
            ],
        },
        "events": {"recent": recent},
        # ⚠ 위 `alerts`·`events`는 최근 목록이고 아래는 날짜별 집계다 — 이름이 겹치니
        # 중첩을 보고 갈라라.
        "today": _day_slice(today),
        "yesterday": _day_slice(yesterday),
        # ⚠ 없으면 `None`이다 — 아침에 아무도 화면을 안 켜면 그날 행이 아직 없다.
        "weather": weather,
        # ⚠ 위 `today.events.shuttle_arrival`은 **몇 건**이고 이쪽은 **언제**다.
        "recent_shuttles": shuttles,
        # ⚠ 로그인 안 한 요청(기기 키 갈래)에서는 `None`이다 — 그때는 "누가 묻는지 모른다"가
        #   사실이라, 챗봇이 등급 이야기를 지어내면 안 된다.
        "viewer": _viewer(actor),
    }
