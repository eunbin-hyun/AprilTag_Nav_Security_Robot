"""LLM 없이도 화면이 안 비게 하는 규칙 기반 문장.

키가 없거나 호출이 실패하면 여기 문장이 그대로 응답에 실린다. 그래서 이 문장들은
"LLM이 죽었다"는 사과문이 아니라, 집계한 사실을 그대로 읽어 주는 관제 문장이다.
문구는 사용자에게 보이는 제품 문구라 합니다체로 쓴다.

⚠ 여기 함수들은 LLM을 안 부르고 딕셔너리만 읽는다 — 시험이 실호출 없이 전 갈래를 덮는다.
"""
from __future__ import annotations

import datetime as dt

# ⭐ 낱말 정본은 화면이다 — `docs/AI전달_2026-08-06_프론트발_LLM용_관제용어정본.md`.
# 화면 `index.html`에서 상수 스물여덟 개를 뽑아 만든 표라 추측이 없다.
#
# ⛔ 예전에 여기가 `untagged`를 "미태깅", `beam_incomplete`를 "빔 미완"으로 적었다.
# 화면은 같은 값을 "무단 통과"·"한쪽 감지"로 그린다. 그대로 뒀으면 요원이 브리핑과 화면을
# 대조하며 "같은 것인가"를 판단해야 했다(2026-08-07 정정).
VERDICT_LABEL = {
    "normal": "정상 통과",
    "untagged": "무단 통과",
    "beam_incomplete": "한쪽 감지",
    "exit": "퇴장",
}
KIND_LABEL = {
    "tagging": "태깅",
    "gate_pass": "게이트 통과",
    "shuttle_arrival": "셔틀 도착",
}
# 알림 종류. ⚠ **판정(VERDICT_LABEL)과 다른 축이다.** `Alert.type`에는 `untagged`처럼
# 판정과 같은 낱말도 오지만 `robot_mission_failed`처럼 판정에 없는 것도 온다.
#
# 서버가 지금 넣는 값이 어디 있는지 —
#   `robot_channel.py`의 `*_ALERT_TYPE` 상수 넷 + 같은 파일 `MISSION_NOTICES` 안에
#   **리터럴로** 박힌 `robot_weather_blocked`(그 줄만 상수가 아니다) + `dispatch.py`의
#   `ARRIVAL_EXPIRED_ALERT_TYPE` + `credit/state_machine.py`의 `ALERT_TYPE_UNTAGGED`.
#   `*_ALERT_TYPE`만 grep 하면 `robot_weather_blocked` 하나를 놓친다.
#
# ⚠ 아래 열 중 셋(`beam_incomplete`·`shuttle`·`shuttle_signal_discarded`)은 **서버가 지금
# 안 넣는다.** 화면 쪽 어휘라 대비로만 둔다 — 지우지 마라. 그래서 닫힌 집합을 잴 때는
# "서버 상수 → 이 표" 한 방향만 재고 반대는 재지 마라.
ALERT_LABEL = {
    "untagged": "무단 통과",
    "beam_incomplete": "센서 한쪽 감지",
    "shuttle": "셔틀 출동",
    "robot_arrival": "로봇 게이트 도착",
    "robot_departure": "로봇 게이트 출발",
    "robot_return_complete": "로봇 복귀 완료",
    "robot_weather_blocked": "날씨로 출동 보류",
    "robot_mission_failed": "로봇 주행 실패",
    "shuttle_arrival_expired": "셔틀 신호 만료",
    "shuttle_signal_discarded": "셔틀 신호 종료",
}


# ⛔ 통신 상태 낱말은 `CommStatus` 셋(`WS_OK`·`POLLING_GRACE`·`SERVER_DOWN`)뿐이다.
# 예전에 여기가 `"online"`과 비교해서 **어떤 로봇이 붙어 있어도 늘 0**이었다. 챗봇이
# "0대가 온라인입니다"를 계속 말했는데 그게 사실이 아니라 죽은 비교였다(2026-08-07 정정).
# `collect_daily_facts`는 같은 결함을 8/6에 고쳤는데 여기만 남아 있었다.
#
# ⚠ 백엔드 enum 을 import 하지 않는다 — 이 패키지는 백엔드를 모르는 게 계약이다
# (모듈 머리). 대신 `backend/tests/test_assistant_fallback.py` 가 `CommStatus` 를
# import 해서 이 상수와 대조한다. 값이 바뀌면 거기서 빨개진다.
#
# ⚠ **짧은 옛 이름도 같이 받는다**(`OK` = `WS_OK`). 화면도 둘 다 받는다
# (`index.html:13093` — 한쪽만 보다가 정상 신호를 "통신 정지"로 떨어뜨린 실사고가 있었다).
ONLINE_STATUS = frozenset({"WS_OK", "OK"})

# ⛔⛔ **`WS_OK`를 그대로 믿으면 안 된다**(2026-08-07 프론트 2차 실측).
# 로봇이 8시간 51분째 안 붙었는데 `network_status`가 `WS_OK`로 남아 있었고, 그 판에서
# 브리핑이 "1대가 온라인입니다"라고 적었다. 같은 시각 화면은 "신호 지연"이라 적었다 —
# **두 곳이 갈렸다.** 서버가 오래된 값을 내릴지는 백엔드 답 대기다(프론트 48차).
#
# 그때까지 우리가 방어한다. 문턱은 **화면과 같은 25초**다 — 계약상 신호 주기 상한이 20초라
# 여유 5초를 더한 값이고, 상단바 칩·로봇 카드·텔레메트리가 그 값을 함께 쓴다
# (`index.html:6287` `HEARTBEAT_MAX_S + 5`). 우리만 다른 문턱을 쓰면 또 갈린다.
#
# ✅ **셋이 25초로 맞았다**(2026-08-07 해결). 한때 갈려 있었다 — 서버 집계가
# `ws_heartbeat_sec(15) + 5` = 20초를 썼는데, 그 설정은 **서버→로봇** 찌르는 주기고
# 화면 것은 **로봇→서버** 보고 주기 상한이라 방향이 반대인 다른 물건이었다.
# 백엔드가 전용 설정 `robot_stale_sec = 25.0`을 새로 빼서 풀렸다.
#
# ⚠ 설정을 따로 뺀 까닭 — 하트비트 주기를 조절하는 것과 신선도 문턱을 정하는 것은 다른
# 결정이다. 묶어 두면 주기를 바꾸는 순간 브리핑 판정이 조용히 따라 움직인다.
STALE_SEC = 25


def _hhmm(iso: str | None) -> str:
    """ISO 시각 문자열에서 시:분만 뽑는다. 못 읽으면 '시각 미상'."""
    if not iso:
        return "시각 미상"
    try:
        return dt.datetime.fromisoformat(iso).strftime("%H:%M")
    except ValueError:
        return "시각 미상"


def _gate_label(gate_no: int | None) -> str:
    return f"{gate_no}번 게이트" if gate_no is not None else "게이트 미상"


def _is_up(robot: dict, now: dt.datetime | None = None) -> bool:
    """이 로봇이 **지금** 서버에 붙어 있나.

    낱말만 보면 안 된다 — 값이 `WS_OK`라도 마지막 보고가 몇 시간 전이면 붙어 있는 게 아니다.
    `updated_at`을 못 읽으면 낱말만 믿는다(값이 아예 없는 판에서 전부 0대로 떨어뜨리면
    그것도 틀린 사실이다).
    """
    if str(robot.get("network_status") or "").upper() not in ONLINE_STATUS:
        return False
    raw = robot.get("updated_at")
    if not raw:
        return True
    try:
        seen = dt.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    if seen.tzinfo is None:
        return True   # 시간대를 모르면 견줄 수 없다. 낱말만 믿는다.
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - seen).total_seconds() <= STALE_SEC


def daily_summary(facts: dict) -> str:
    """일일 요약 폴백. 건수·판정·경고·최다 게이트·최다 시간대를 문단 셋으로 읽어 준다.

    ⚠ **문단을 빈 줄로 가른다.** 화면이 빈 줄로 잘라 `<p>`로 감싸고(정본 §7-3), 정본 §7-2가
    문단 2~3개를 요구한다. 예전에는 일곱 문장이 한 문단으로 붙어 나가서, LLM이 붙은 판과
    안 붙은 판이 서로 다른 모양이었다(2026-08-07 정정).

    ⚠ `alert_brief`·`chat_answer`는 정본이 각각 "한두 문장"·"2~4문장"이라 문단 하나가 맞다.
    여기만 갈린다.
    """
    events = facts.get("events") or {}
    verdicts = facts.get("verdicts") or {}
    alerts = facts.get("alerts") or {}
    total = events.get("total", 0)

    # 첫 문단 — 날짜와 건수
    head = [
        f"{facts.get('date', '오늘')} 관제 요약입니다.",
        f"전체 이벤트는 {total}건이고, 태깅 {events.get('tagging', 0)}건 ·"
        f" 게이트 통과 {events.get('gate_pass', 0)}건 ·"
        f" 셔틀 도착 {events.get('shuttle_arrival', 0)}건입니다.",
    ]

    # 둘째 문단 — 판정과 경고
    untagged = verdicts.get("untagged", 0)
    body = [
        f"통과 판정 가운데 무단 통과가 {untagged}건 나왔습니다."
        if untagged
        else "무단 통과로 판정된 통과는 없었습니다.",
    ]

    # ⛔⛔ **최다 판정을 한 번도 안 적고 있었다**(2026-08-09 백지검토).
    # LLM 경로는 `prompts._annotate_verdicts` 가 "한쪽 감지가 200건으로 가장 많았습니다"를
    # 코드로 계산해 넣는데, 폴백에는 그 계산이 없어 **무단 통과만** 봤다. 그래서 200건이
    # 전부 한쪽 감지인 날에는 위 문장이 "무단 통과로 판정된 통과는 없었습니다"로 끝나고
    # 요원은 그 200건의 정체를 한 글자도 못 들었다(`daily_summary({"verdicts":
    # {"beam_incomplete": 200}, …})` 실행으로 확인 — 두 경로가 갈려 있었다).
    #
    # ⭐ 지어내는 것이 아니라 **있는 값에서 가장 큰 하나를 골라 이름을 붙이는 것**이다.
    # 낱말표는 같은 파일 `VERDICT_LABEL` 을 그대로 쓴다.
    # ⚠ 무단 통과가 최다면 바로 위 문장이 이미 그 이름과 수를 적었으니 겹쳐 적지 않는다.
    # ⚠ 조사를 안 붙인다 — "퇴장가"·"한쪽 감지이" 같은 자리가 나온다.
    if isinstance(verdicts, dict):
        top = max(
            ((k, n) for k, n in verdicts.items()
             if isinstance(n, int) and not isinstance(n, bool) and n > 0),
            key=lambda p: p[1],
            default=None,
        )
        if top and top[0] != "untagged":
            body.append(
                f"판정 가운데 가장 많았던 것은 {VERDICT_LABEL.get(top[0], top[0])}"
                f" {top[1]}건입니다."
            )

    body.append(
        f"경고는 모두 {alerts.get('total', 0)}건이고 아직 확인하지 않은 경고가"
        f" {alerts.get('active', 0)}건 남아 있습니다."
    )

    # 셋째 문단 — 눈에 띄는 게이트·시간대·로봇
    tail = []
    gates = facts.get("gates") or []
    hottest = max(gates, key=lambda g: g.get("untagged", 0), default=None)
    if hottest and hottest.get("untagged"):
        tail.append(
            f"무단 통과가 가장 많았던 곳은 {_gate_label(hottest.get('gate_no'))}로"
            f" {hottest['untagged']}건입니다."
        )

    peak = facts.get("peak_hour")
    if peak and peak.get("events"):
        tail.append(f"이벤트가 가장 몰린 시간대는 {peak.get('hour')}시로 {peak['events']}건입니다.")

    robots = facts.get("robots") or {}
    if robots.get("total"):
        tail.append(
            f"등록된 로봇은 {robots['total']}대이고 그 가운데 {robots.get('online', 0)}대가"
            " 서버에 붙어 있습니다."
        )

    paras = [" ".join(p) for p in (head, body, tail) if p]
    return "\n\n".join(paras)


def alert_brief(facts: dict) -> str:
    """이상 상황 브리핑 폴백. 발생 시각·게이트·최근 이력을 한두 문장으로.

    경고 수는 관제 전체 기준이다(Alert에 게이트 컬럼이 없어 게이트로 못 좁힌다).
    통과·무단 통과 수만 게이트를 알 때 그 게이트로 좁혀 말한다.
    """
    alert = facts.get("alert") or {}
    recent = facts.get("recent_context") or {}
    source = facts.get("source") or {}

    # ⚠ 알림 표를 먼저 본다. `Alert.type`은 판정과 다른 축이라 `robot_mission_failed`처럼
    # 판정 표에 없는 값이 온다. 예전에 판정 표만 봐서 그런 값이 **영어 그대로** 화면에
    # 나갔다(2026-08-07 정정). 둘 다 없으면 원값을 적는다 — 그럴듯한 한국어를 지어내면
    # 요원이 없는 사실을 믿는다(프론트 용어 정본 §0).
    raw_type = alert.get("type", "")
    type_label = ALERT_LABEL.get(raw_type) or VERDICT_LABEL.get(raw_type) or raw_type or "경고"
    head = (
        f"{_hhmm(alert.get('created_at'))}에 {_gate_label(facts.get('gate_no'))}에서"
        f" {type_label} 경고가 발생했습니다."
    )

    parts = [head]
    if source.get("event_id"):
        parts.append(
            f"발화 이벤트는 {KIND_LABEL.get(source.get('type'), source.get('type') or '미상')}"
            f" {source['event_id']}입니다."
        )

    window = recent.get("window_min", 60)
    prior = recent.get("alerts_all_gates", 0)
    if prior:
        parts.append(
            f"최근 {window}분 사이 관제 전체에서 경고가 {prior}건 더 있었습니다."
        )
    else:
        parts.append(f"최근 {window}분 사이 다른 경고는 없었습니다.")

    passes = recent.get("passes", 0)
    if passes:
        scope = "같은 게이트" if recent.get("gate_scoped_passes") else "전체 게이트"
        parts.append(
            f"{scope} 통과는 {passes}건이고 그 가운데 무단 통과가"
            f" {recent.get('untagged', 0)}건입니다."
        )

    if alert.get("ack"):
        parts.append("이 경고는 이미 확인 처리되었습니다.")
    return " ".join(parts)


def _verdict_counts(events: dict) -> dict[str, int]:
    """최근 이벤트에서 판정별 건수를 센다. **자료에 있는 것만 센다 — 지어내지 않는다.**"""
    out: dict[str, int] = {}
    for e in (events.get("recent") or []):
        if not isinstance(e, dict):
            continue
        v = e.get("verdict")
        if v:
            out[v] = out.get(v, 0) + 1
    return out


def _asked_about(question: str | None) -> set[str]:
    """질문이 어느 갈래인가. `facts_select` 의 규칙을 그대로 쓴다.

    ⚠ 규칙을 여기에 다시 적지 않는다. 두 자리가 갈리면 조용히 어긋난다.
    """
    if not question:
        return set()
    from . import facts_select
    return {name for name, pat in facts_select._PATTERNS.items() if pat.search(question)}


def chat_answer(facts: dict, question: str | None = None) -> str:
    """관제 챗봇 폴백. 조회한 현재 상태를 읽어 준다.

    ## ⛔ 질문을 아예 안 보던 것을 고쳤다 (2026-08-07 밤, 사용자가 실사용에서 잡음)

    예전 머리말이 **"질문 해석은 아예 안 한다"** 였다. 없는 사실을 지어내지 않으려는
    판단이었는데, 결과가 이랬다.

        요원 — "가장 최근에 누가 무단통과했어?"
        폴백 — "로봇은 1대가 등록되어 있고 그 가운데 0대가 서버에 붙어 있습니다…"

    ⛔ **무단 통과를 물었는데 로봇 대수를 답했다.** 사실은 다 맞는데 질문과 무관하다.

    ⭐ 지금은 **질문 갈래에 맞는 문장을 앞에 놓는다.** 지어내는 것이 아니라 **있는 값의
    순서만 바꾸고, 셀 수 있는 것을 센다.** `events.recent` 안의 `verdict` 를 세는 것은
    조회 결과를 요약하는 것이지 새 사실이 아니다.

    ⚠ **질문이 없거나 갈래가 안 잡히면 예전 그대로다.** 좁히다 빠뜨리는 쪽이 더 나쁘다.
    ⚠ 첫 문장이 **사과문이면 안 된다**(모듈 머리 계약). 예전 문구는 "지금은 자동 요약을
    만들지 못해…"로 시작해서 키가 없는 판에서 첫 화면이 실패 고백이었다.
    """
    # ⛔ `robots` 는 창구마다 모양이 다르다. 챗봇은 **로봇 목록**이고 일일 요약은
    # `{"total": n, "online": n}` 집계다. 예전에는 목록을 가정해서, 일일 요약 모양이
    # 들어오면 `_is_up` 이 문자열에 `.get` 을 불러 **500 이 났다**(2026-08-07 시험이 잡음).
    # ⚠ 폴백은 다른 게 다 터졌을 때 마지막으로 남는 자리라 여기서 터지면 화면이 통째로 빈다.
    robots = facts.get("robots")
    if isinstance(robots, dict):
        total, online = robots.get("total", 0), robots.get("online", 0)
    else:
        robots = robots or []
        total, online = len(robots), sum(1 for r in robots if isinstance(r, dict) and _is_up(r))

    alerts = facts.get("alerts") or {}
    events = facts.get("events") or {}
    recent = events.get("recent") or []

    # 문장을 갈래마다 만들어 두고, 질문에 맞는 것을 앞에 놓는다.
    robot_line = f"로봇은 {total}대가 등록되어 있고 그 가운데 {online}대가 서버에 붙어 있습니다."
    alert_line = f"확인하지 않은 경고는 {alerts.get('active', 0)}건입니다."
    if recent:
        newest = recent[0]
        event_line = (
            f"가장 최근 이벤트는 {_hhmm(newest.get('observed_at'))}"
            f" {KIND_LABEL.get(newest.get('kind'), newest.get('kind') or '미상')}이고"
            f" 최근 이벤트 {len(recent)}건을 함께 조회했습니다."
        )
    else:
        event_line = "최근 이벤트는 조회되지 않았습니다."

    asked = _asked_about(question)

    # ⛔⛔ **집계·날씨·셔틀 갈래가 통째로 빠져 있었다**(2026-08-09 백지검토).
    #
    # `facts_select._PATTERNS` 는 갈래를 여섯으로 가르는데 여기는 경고·이벤트·로봇 셋만
    # 봤다. 그래서 챗봇 재료에 늘 실리는 `today`·`yesterday`·`weather`·`recent_shuttles`
    # 를 한 번도 안 읽었다. 실행으로 확인한 모습이 이렇다 —
    #
    #     요원 — "오늘 무단 통과 몇 건이야?"
    #     폴백 — "확인하지 않은 경고는 37건입니다. … 로봇은 1대…"
    #             (같은 재료의 today.verdicts.untagged=43 을 한 번도 안 읽는다)
    #
    # 모델이 한 번 흔들리는 순간 시연에서 그대로 밟는 자리다.
    #
    # ⭐ 새 사실을 만들지 않는다 — **이미 실려 온 값을 옮겨 적을 뿐**이다.
    # ⚠ 갈래 하나를 골라 나머지를 버리지 않고 **앞에 얹기만** 한다. 낱말이 겹쳐 갈래가
    #   둘 잡히는 물음이 흔해서(예 "오늘 비 와?" → 집계·날씨), 하나를 고르면 엉뚱한
    #   갈래가 이긴다. 좁히다 빠뜨리는 쪽이 더 나쁘다는 이 모듈의 잣대 그대로다.
    #
    # ⛔ **어제를 물어도 오늘 값을 답하던 것을 고쳤다** (2026-08-09 저녁 백지검토).
    #   `facts_select._TOPIC_KEYS["집계"]` 가 `today` 와 `yesterday` 를 **둘 다** 싣는데
    #   여기서 `today` 만 읽었다. 그래서 "어제 무단 통과 몇 건이야?" 와 "오늘 무단 통과
    #   몇 건이야?" 에 **글자 하나 안 다른 같은 답**이 나갔다(실행 재현).
    #   ⚠ 이것은 갈래 판정을 여기 복제하는 것이 아니다 — **날짜 범위는 `facts_select` 에
    #   아예 없는 개념**이고, 여기서는 이미 실려 온 두 묶음 중 어느 것을 먼저 읽을지만 고른다.
    #   ⚠ 어제 줄에는 날짜를 같이 적는다. 날짜가 없으면 요원이 오늘 값으로 읽는다.
    def _day_line(day: object, label: str, with_date: bool) -> str | None:
        if not isinstance(day, dict):
            return None
        d_events = day.get("events") or {}
        d_verdicts = day.get("verdicts") or {}
        d_alerts = day.get("alerts") or {}
        date = day.get("date")
        # ⚠ 날짜를 괄호로 붙이면 앞말이 숫자라 "은"이 어색해진다. 그때만 "는"으로 바꾼다.
        dated = bool(with_date and date)
        head = f"{label}({date})는" if dated else f"{label}은"
        return (
            f"{head} 이벤트가 모두 {d_events.get('total', 0)}건이고 그 가운데 게이트 통과가"
            f" {d_events.get('gate_pass', 0)}건입니다."
            f" 무단 통과 판정은 {d_verdicts.get('untagged', 0)}건이고 {label} 경고는"
            f" {d_alerts.get('total', 0)}건입니다."
        )

    today_line = _day_line(facts.get("today"), "오늘", False)
    # 어제를 물었을 때만 앞에 얹는다. 오늘 줄은 버리지 않는다(위 "앞에 얹기만" 잣대).
    yesterday_line = (
        _day_line(facts.get("yesterday"), "어제", True)
        if question and "어제" in question
        else None
    )
    agg_line = " ".join(x for x in (yesterday_line, today_line) if x) or None

    # ⚠ 날씨 행은 아침에 아무도 화면을 안 켜면 아예 없다(그때 `weather` 가 null 이다).
    #   없는 것을 지어내면 요원이 로봇을 실외로 내보낸다.
    weather = facts.get("weather")
    weather_line = None
    if isinstance(weather, dict):
        desc = weather.get("description") or weather.get("status") or "미상"
        ok = weather.get("dispatch_ok")
        if ok is True:
            tail = "로봇을 실외로 내보낼 수 있는 상태입니다."
        elif ok is False:
            tail = "실외 출동은 보류 상태입니다."
        else:
            tail = "실외 출동 가능 여부는 실려 오지 않았습니다."
        weather_line = f"오늘 날씨는 {desc}이고 {tail}"
    elif "날씨" in asked:
        weather_line = "오늘 날씨는 지금 실려 오지 않았습니다."

    shuttles = facts.get("recent_shuttles")
    shuttle_line = None
    if isinstance(shuttles, list) and shuttles and isinstance(shuttles[0], dict):
        newest = shuttles[0]
        shuttle_line = (
            f"가장 최근 셔틀 도착은"
            f" {_hhmm(newest.get('observed_at') or newest.get('received_at'))}이고"
            f" 최근 셔틀 {len(shuttles)}건을 함께 조회했습니다."
        )
    elif "셔틀" in asked:
        shuttle_line = "최근 셔틀 도착 기록은 조회되지 않았습니다."

    # ⭐ 판정을 물었으면 **조회한 이벤트 안에서 세어** 준다. 새 사실이 아니라 요약이다.
    verdict_line = None
    if asked & {"경고", "이벤트"}:
        counts = _verdict_counts(events)
        if counts:
            parts = [f"{VERDICT_LABEL.get(k, k)} {n}건"
                     for k, n in sorted(counts.items(), key=lambda p: -p[1])]
            verdict_line = f"조회한 최근 이벤트 {len(recent)}건의 판정은 {', '.join(parts)}입니다."

    # ⚠ 첫 문장이 사과문이면 안 된다. 폴백이라는 사실은 `meta.generated_by` 가 말한다.
    lines = ["질문하신 시점의 관제 상태를 그대로 알려드립니다."]
    # 좁은 갈래(날씨·셔틀)를 먼저, 그 다음 집계다. 아래 경고·로봇 문장은 어느 물음에
    # 붙어도 말이 되는 값이라 뒤에 남긴다.
    for name, line in (("날씨", weather_line), ("셔틀", shuttle_line), ("집계", agg_line)):
        if name in asked and line:
            lines.append(line)
    if asked & {"경고", "이벤트"}:
        lines.append(alert_line)
        if verdict_line:
            lines.append(verdict_line)
        lines.append(event_line)
        lines.append(robot_line)
    elif "로봇" in asked:
        lines.append(robot_line)
        lines.append(alert_line)
        lines.append(event_line)
    else:
        # 갈래가 안 잡히면 예전 순서 그대로다 — 좁히다 빠뜨리는 쪽이 더 나쁘다.
        lines.append(robot_line)
        lines.append(alert_line)
        lines.append(event_line)
    return " ".join(lines)
