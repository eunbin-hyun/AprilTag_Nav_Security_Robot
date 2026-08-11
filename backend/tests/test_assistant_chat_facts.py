"""챗봇이 "오늘 몇 건인가"에 답할 수 있나 (2026-08-07 · AI 갈래 6차 요청).

⛔ **최근 목록만 실려 있었다.** `collect_chat_facts`가 알림 10건·이벤트 15건만 싣고 오늘
집계는 통째로 빠져 있었다. 그래서 챗봇이 "오늘 무단 통과가 몇 건인가요"에 못 답했다 —
모델이 셀 수 있는 건 상한에 걸린 열댓 건뿐이고 그건 오늘 전체가 아니다.

⚠ 이 계열은 **틀린 줄 모르기 쉽다.** 모델이 "3건입니다"라고 자신 있게 답하는데 그게
목록에 실린 것만 센 값이다. 오류도 빈 답도 아니라서 요원이 되짚어 볼 계기가 없다.

⭐ 그래서 **상한을 넘겨 심는다.** 15건 상한에 20건을 부으면 목록을 세는 쪽과 집계를 읽는
쪽의 답이 갈리고, 집계가 빠지면 여기가 빨개진다.
"""
import datetime as dt

import pytest

from app.assistant_data import CHAT_EVENT_LIMIT, collect_chat_facts, today_kst
from app.db import get_session
from app.models import DailyDefaultDestination, GatePassEvent, ShuttleArrival

_KST = dt.timezone(dt.timedelta(hours=9))


def _kst_yesterday_noon(now: dt.datetime) -> dt.datetime:
    """KST 기준 어제 정오. 어느 시각에 돌려도 "어제"에 들어간다.

    ⚠ 상대 시간(`now - timedelta(...)`)으로는 KST 날짜 경계를 못 맞춘다. `now` 가 UTC 라
    한국 날짜와 아홉 시간 어긋나고, 정오 이전에 돌리면 하루가 더 밀린다.
    """
    kst_today = now.astimezone(_KST).date()
    yesterday = kst_today - dt.timedelta(days=1)
    return dt.datetime.combine(yesterday, dt.time(12, 0), tzinfo=_KST)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_OVER_LIMIT = CHAT_EVENT_LIMIT + 5


async def _seed_untagged_passes(count: int) -> None:
    """무단 통과를 `count` 건 심는다 — 상한을 넘겨야 목록과 집계가 갈린다."""
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as s:
        s.add_all(
            [
                GatePassEvent(
                    event_id=f"chat-facts-untagged-{i}",
                    gate_no=1,
                    direction="A_TO_B",
                    status="complete",
                    verdict="untagged",
                    observed_at=now - dt.timedelta(seconds=i),
                    received_at=now - dt.timedelta(seconds=i),
                )
                for i in range(count)
            ]
        )
        await s.commit()


async def test_오늘_집계가_최근목록_상한에_안_잘린다():
    """⭐ 이 시험의 핵심 — 목록을 세면 15, 집계를 읽으면 20이다."""
    await _seed_untagged_passes(_OVER_LIMIT)

    async with get_session() as s:
        facts = await collect_chat_facts(s)

    assert facts["today"]["verdicts"]["untagged"] == _OVER_LIMIT, (
        "오늘 집계가 상한에 잘렸다 — 최근 목록을 세는 쪽으로 되돌아간 것이다"
    )
    assert len(facts["events"]["recent"]) == CHAT_EVENT_LIMIT, (
        "최근 목록은 상한을 지켜야 한다 — 여기까지 늘리면 컨텍스트가 부풀어"
    )


async def test_집계와_최근목록이_같은_묶음에_다_실린다():
    """⚠ 둘 중 하나만 남기면 안 된다 — 집계는 "몇 건"에, 목록은 "무슨 일"에 답한다."""
    await _seed_untagged_passes(3)

    async with get_session() as s:
        facts = await collect_chat_facts(s)

    for key in ("verdicts", "events", "alerts", "gates", "peak_hour", "date"):
        assert key in facts["today"], f"오늘 집계에서 {key}가 빠졌다"
    for key in ("robots", "alerts", "events"):
        assert key in facts, f"기존 묶음에서 {key}가 사라졌다"

    assert facts["today"]["date"] == today_kst().isoformat(), (
        "집계가 어느 날 것인지 모델이 알아야 한다 — 자정을 넘기면 '오늘'이 갈린다"
    )


async def test_아무_일도_없으면_집계가_비어야_한다():
    """⚠ 늘 0이던 결함을 고치면서 **진짜 0인 경우까지 잃으면 안 된다.**"""
    async with get_session() as s:
        facts = await collect_chat_facts(s)

    assert facts["today"]["verdicts"] == {}
    assert facts["events"]["recent"] == []


async def test_어제와_오늘이_섞이지_않는다():
    """⭐ "어제는 어땠어"에 답하려면 어제 집계가 따로 서야 한다(AI 갈래 7차 요청).

    ⛔ **날짜 경계를 못 지키면 조용히 틀린다.** 어제 것이 오늘로 새면 "오늘 3건"이 되고,
    오늘 것이 어제로 새면 "어제 3건"이 된다. 둘 다 오류 없이 그럴듯한 숫자라서 아무도
    안 걸린다. ⭐ 그래서 **양쪽에 다른 수를 심고 서로 안 섞이는지** 함께 잰다.
    """
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as s:
        s.add_all(
            [
                GatePassEvent(
                    event_id=f"chat-facts-day-{tag}-{i}",
                    gate_no=1,
                    verdict="untagged",
                    observed_at=at,
                    received_at=at,
                )
                for tag, at, count in (
                    ("today", now, 2),
                    # ⛔ **KST 어제 정오로 못박는다**(2026-08-07 18:20 에 깨져서 고침).
                    #
                    # 예전에는 `now - timedelta(days=1, hours=12)` 였는데, 주석은 "24시간만
                    # 빼면 자정 근처에 그저께로 넘어간다"였지만 **반대였다.** 24시간을 빼면
                    # 어느 시각에 돌리든 어제 같은 시각이고, 36시간을 빼면 **정오 이전에
                    # 돌릴 때 그저께로 넘어간다.** 18:20 에 돌리니 8/6 06:20 이 나왔다.
                    #
                    # ⚠ `now` 가 UTC 라 상대 시간으로는 KST 날짜 경계를 못 맞춘다.
                    # KST 로 옮겨 어제 정오에 세우면 어느 시각에 돌려도 안전하다.
                    ("yday", _kst_yesterday_noon(now), 3),
                )
                for i in range(count)
            ]
        )
        await s.commit()

    async with get_session() as s:
        facts = await collect_chat_facts(s)

    assert facts["today"]["verdicts"].get("untagged") == 2, "어제 것이 오늘로 샜다"
    assert facts["yesterday"]["verdicts"].get("untagged") == 3, "오늘 것이 어제로 샜거나 어제가 안 실렸다"
    assert facts["yesterday"]["date"] == (today_kst() - dt.timedelta(days=1)).isoformat()
    assert "robots" not in facts["yesterday"], (
        "지금 붙어 있는 대수는 어제 집계가 아니다 — 넣으면 '어제 1대가 붙어 있었다'로 읽힌다"
    )


async def test_날씨와_셔틀_시각이_실린다():
    """⭐ "비 와요?"·"셔틀 언제 왔어요"에 답하려면 필요한 둘(관제 보조 갈래 10차 요청).

    ⚠ **집계 수와 시각은 다른 물음에 답한다.** `today.events.shuttle_arrival`은 "몇 건"이고
    `recent_shuttles`는 "언제"다. 하나만 실으면 나머지 물음에 못 답한다.
    """
    now = dt.datetime.now(dt.timezone.utc)
    async with get_session() as s:
        s.add(
            DailyDefaultDestination(
                service_date=today_kst(),
                destination="OUTDOOR_TAGGING",
                source="auto",
                weather_status="CLEAR",
                weather_desc="맑음",
                weather_ok=True,
            )
        )
        s.add(
            ShuttleArrival(
                event_id="chat-facts-shuttle-1",
                gate_no=1,
                shuttle_no="12-3",
                # ⚠ 이 표만 `signal_ts`다(다른 표는 `observed_at`). 밖으로 나가는 이름은
                #   `observed_at`으로 맞춰 두니 아래 확인은 그 이름으로 한다.
                signal_ts=now,
                received_at=now,
            )
        )
        await s.commit()

    async with get_session() as s:
        facts = await collect_chat_facts(s)

    assert facts["weather"]["status"] == "CLEAR"
    assert facts["weather"]["dispatch_ok"] is True
    assert facts["weather"]["decided_by"] == "auto"

    assert len(facts["recent_shuttles"]) == 1
    assert facts["recent_shuttles"][0]["shuttle_no"] == "12-3"
    assert facts["recent_shuttles"][0]["received_at"], "시각이 없으면 '언제 왔나'에 못 답한다"


async def test_날씨가_아직_없으면_None이다():
    """⚠ 아침에 아무도 화면을 안 켜면 그날 행이 없다. **지어내지 말고 없다고 해야 한다.**"""
    async with get_session() as s:
        facts = await collect_chat_facts(s)

    assert facts["weather"] is None
    assert facts["recent_shuttles"] == []


async def test_묻는_사람이_실린다():
    """⭐ 등급별로 자료를 자르는 대신 **누가 묻는지**를 싣는다(2026-08-07 사용자 지시).

    ⚠ 자르지 않은 근거는 `_viewer` 머리에 있다 — 지금 실리는 것 중 가릴 것이 없다.
    신원은 애초에 안 싣고, 태그 번호는 이미 가려 나가며, 나머지는 요원이 화면에서 그대로
    본다. **챗봇만 더 좁히면 화면과 말이 갈린다.**
    """

    class _Actor:
        id = 1
        username = "owner1"
        display_name = "총괄이"
        role = "owner"

    async with get_session() as s:
        facts = await collect_chat_facts(s, _Actor())

    v = facts["viewer"]
    assert v["role"] == "owner"
    assert v["role_label"] == "총괄 관리자"
    assert v["display_name"] == "총괄이"
    assert v["can_manage_staff"] is True
    assert v["can_manage_accounts"] is True
    assert v["can_open_devpage"] is True


async def test_요원은_관리_권한이_거짓이다():
    """⚠ 참·거짓을 나란히 심는다 — 한쪽만 재면 늘 참이어도 통과한다."""

    class _Agent:
        id = 2
        username = "agent1"
        display_name = "요원이"
        role = "agent"

    async with get_session() as s:
        facts = await collect_chat_facts(s, _Agent())

    v = facts["viewer"]
    assert v["role_label"] == "일반 보안요원"
    assert v["can_manage_staff"] is False
    assert v["can_manage_accounts"] is False


async def test_로그인_안_했으면_viewer가_없다():
    """⛔ 기기 키 갈래에서는 누가 묻는지 모른다 — 챗봇이 등급을 지어내면 안 된다."""
    async with get_session() as s:
        facts = await collect_chat_facts(s)

    assert facts["viewer"] is None
