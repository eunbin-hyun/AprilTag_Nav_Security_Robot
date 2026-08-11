"""Mattermost Incoming Webhook 알림. 미태깅 경고를 보안 채널로 밀어낸다 (S15P11C207-82).

채널은 둘이다. 미태깅 경고는 MATTERMOST_WEBHOOK_URL(보안 채널)로, 요원이 붙잡아 확인한
신원은 MATTERMOST_IDENTIFY_WEBHOOK_URL(상위 보고선)로 나간다. 둘 다 비면 조용히 스킵이고,
설정도 발사 판정도 따로 논다 — 한쪽만 켠 배포가 정상 상태다.

- 스위치는 웹훅 URL 하나다(MATTERMOST_WEBHOOK_URL). 비어 있으면 조용히 스킵이 기본이고,
  넣으면 그때부터 실제로 POST가 나간다. URL을 지우지 않고 발사만 멈추려면
  MATTERMOST_ENABLED=false로 명시한다.
- 반드시 DB 커밋 뒤에 호출한다. 워커가 1개라 동기 대기가 트랜잭션을 붙잡으면
  전체 처리가 멈춘다(아키텍처 2026-07-23 §05).
- 웹훅이 느리거나 죽어도 판정은 이미 커밋됐으므로 여기 실패는 삼키고 로그만 남긴다.
  예외가 안 나는 실패(4xx·5xx 응답)도 성공으로 세지 않는다 — 아무도 못 받은 경고를
  '보냈다'로 기록하면 발사 여부를 보는 눈이 통째로 멀어진다.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import httpx

from app.config import get_settings

logger = logging.getLogger("c207.notify")

MATTERMOST_TIMEOUT_S = 3.0
KST = dt.timezone(dt.timedelta(hours=9))

# 카드 한 줄에 그대로 넣으면 안 되는 문자. 스키마에서 이미 막지만(schemas.IDENTITY_FORBIDDEN_RE)
# 여기서 한 겹 더 막는다 — 카드를 만드는 자리가 입력 검사보다 오래 살아남고, 나중에 다른
# 경로(배치 재발송·관리 도구)에서 부를 때 그 경로가 검사를 안 탈 수 있다.
_CARD_UNSAFE_RE = re.compile("[\x00-\x1f\x7f\x80-\x9f]")

# 신원 보고 카드 색 띠. 사용자가 2026-07-30에 붉은색으로 확정했다 — 신원이 확인됐다는 것이
# 곧 "미태깅이 났다"는 사실이라, 해결을 뜻하는 초록으로 바꾸지 않는다.
IDENTIFY_CARD_COLOR = "#f65b4e"


def _quoted(value: str) -> str:
    """요원이 손으로 적은 값을 카드에 넣을 때 쓰는 무해화.

    인라인 코드(`...`)로 감싼다. 매터모스트는 코드 스팬 안의 마크다운도 @멘션도 안 살린다 —
    `[링크](http://evil)`도 `@channel`도 글자 그대로 찍힌다.

    왜 필요한가 — 미태깅 카드는 줄을 "\\n"으로 이어붙인 한 덩어리다. 신원 칸에 개행을 끼워
    넣으면 "- 게이트: 99번" 같은 위조된 줄이 진짜 줄 사이에 그대로 붙어서, 받는 쪽 감사
    기록이 통째로 조작된다. 개행은 스키마가 막지만 이 함수가 두 번째 문이다.
    감싸기를 깨는 백틱은 홑따옴표로 바꾼다.

    ⚠ 신원 카드는 2026-07-30부터 색 띠 카드(attachments)라 값이 **별 필드 칸**에 들어간다.
    그래서 줄 위조 길은 막혔는데, **필드 값도 마크다운과 @멘션을 살리니** 이 함수를 계속
    쓴다. 값이 회색 코드 스팬으로 찍혀 오히려 도드라진다.
    """
    flat = _CARD_UNSAFE_RE.sub(" ", value).replace("`", "'")
    return f"`{flat}`"


def _kst_text(value: dt.datetime | None) -> str:
    """timestamptz를 사람이 읽는 KST 표기로. tz가 없으면 UTC로 본다(DB 계약상 안 온다)."""
    if value is None:
        return "미상"
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(KST).strftime("%Y-%m-%d %H:%M:%S KST")


async def _post_card(url: str, card: dict, *, what: str) -> bool:
    """카드 하나를 웹훅에 밀어넣는다. 성공(2xx·3xx)일 때만 True.

    실패를 삼키는 자리가 여기 하나뿐이라, 새 카드 종류를 붙일 때 예외 처리를 다시 안 짠다.
    ⚠ 로그에 card 본문을 찍지 마라 — 신원 카드가 여기로도 온다.
    """
    try:
        async with httpx.AsyncClient(timeout=MATTERMOST_TIMEOUT_S) as client:
            response = await client.post(url, json=card)
    except Exception as exc:  # noqa: BLE001 - 알림 실패가 판정을 되돌리면 안 된다
        # 예외 종류를 같이 찍는다 — httpx 타임아웃은 str(exc)가 빈 문자열이라 이유를 안 찍으면
        # "발사 실패: " 뒤가 비어 있는 줄만 쌓이고, 죽은 URL인지 느린 채널인지 못 가른다.
        logger.warning(
            "Mattermost %s 웹훅 발사 실패: %s(%r)", what, type(exc).__name__, str(exc)
        )
        return False

    if response.status_code >= 400:
        # 폐기된 웹훅 URL·거부된 본문이 여기로 온다. 예외가 안 나므로 직접 걸러야 한다.
        logger.warning(
            "Mattermost %s 웹훅이 %s를 돌려줬다 — 카드가 채널에 안 붙었다",
            what,
            response.status_code,
        )
        return False
    return True


def _build_card(*, gate_no: int | None, alert_id: int | None, source_id: int | None,
                low_confidence: bool) -> dict:
    """미태깅 경고 카드. 실제 문구는 배포 때 팀 톤에 맞춰 다듬는다."""
    gate_label = f"{gate_no}번" if gate_no is not None else "미상"
    lines = [
        "#### :rotating_light: 무단 통과 경고",
        f"- 게이트: {gate_label}",
        f"- 경고 ID: {alert_id if alert_id is not None else '-'}",
        f"- 발화 통과 이벤트 ID: {source_id if source_id is not None else '-'}",
    ]
    if low_confidence:
        lines.append("- :warning: 미소비 크레딧 과다 — 판정 신뢰도 낮음")
    return {"text": "\n".join(lines)}


async def send_untagged_alert(
    *,
    gate_no: int | None,
    alert_id: int | None,
    source_id: int | None,
    low_confidence: bool,
) -> bool:
    """미태깅 카드를 웹훅으로 보낸다. 비활성이면 아무것도 안 하고 False.

    반환값은 '실제로 발사했나'다(시험·관통 실측에서 no-op 여부 확인에 쓴다).
    """
    settings = get_settings()
    if not settings.mattermost_active:
        logger.debug("Mattermost 비활성 — 미태깅 카드 발사 생략 (gate=%s)", gate_no)
        return False

    card = _build_card(
        gate_no=gate_no, alert_id=alert_id, source_id=source_id, low_confidence=low_confidence
    )
    return await _post_card(settings.mattermost_webhook_url, card, what="미태깅")


def _build_identify_card(
    *,
    gate_no: int | None,
    alert_id: int,
    identified_person: str,
    identified_student_no: str | None = None,
    identified_by: str | None,
    identified_at: dt.datetime | None,
    note: str | None,
    corrected: bool,
) -> dict:
    """신원 확인 보고 카드. **색 띠 카드(attachments) · 필드 두 칸** 양식.

    ⚠ 신원은 가리지 않고 그대로 싣는다. 이 채널의 존재 이유가 "누구였는지"를 상위 보고선에
    알리는 거라, 가리면 보고가 통째로 쓸모없어진다(태그 UID를 가리는 assistant 브리핑과는
    목적이 다르다). 대신 노출 범위를 이 채널 하나로 묶어 둔다 — 인증 없는 목록 API에는
    안 싣고, 서버 로그에도 안 찍는다.

    양식은 2026-07-30에 사용자가 시안 셋을 실물로 채널에서 보고 고른 1번이다. 팀의 젠킨스
    알림과 같은 색 띠 카드에, 값은 두 칸으로 갈라 넣는다. 색은 붉은색 고정이다 — 신원이
    확인됐다는 것이 곧 "미태깅이 났다"는 사실이라 해결 색(초록)으로 바꾸지 않는다.

    ⏸ **글씨 크기는 그대로 둔다**(2026-08-06 사용자 확정). "작다"는 지적을 받았지만 이건
    매터모스트 필드의 기본 렌더라 백틱과 무관하다(백틱이 없는 "게이트" 칸도 같은 크기다).
    키우려면 값을 `text`로 올려야 하는데 그러면 사용자가 시안에서 고른 **두 칸 정렬이 깨지고**,
    백틱을 빼고 볼드를 씌우는 쪽은 아래 이모지·멘션 방어를 통째로 잃는다.

    ⚠ 필드 값도 `_quoted`로 감싼다. attachments 필드는 **마크다운과 @멘션을 그대로 살린다** —
    필드가 별 칸이라 개행으로 줄을 위조하는 길은 막혔지만, `[링크](http://evil)`이나
    `@channel` 주입은 그대로 남는다. 감싸면 회색 코드 스팬으로 찍혀서 값이 오히려 도드라진다.
    ⚠ `fallback`은 카드를 못 그리는 클라이언트와 푸시 미리보기가 읽는 자리다. 신원이 보이게
    적되 코드 스팬은 안 붙인다(알림 텍스트에서는 백틱이 글자로 보인다). 제어문자만 걷는다.
    """
    gate_label = f"{gate_no}번" if gate_no is not None else "미상"
    title = "무단 통과자 신원 확인" + (" (정정)" if corrected else "")
    fields = [
        {"short": True, "title": "신원", "value": _quoted(identified_person)},
        {"short": True, "title": "게이트", "value": gate_label},
        # ⛔ **시각도 `_quoted`로 감싼다.** 안 감싸면 매터모스트가 `00:11:27`의 `:11:`을
        # 이모지 문법으로 읽어 그림으로 바꾼다 — 분이 통째로 사라진다(2026-08-06 사용자가
        # 카드 사진으로 짚었다). 값도 표기도 멀쩡한데 렌더링에서 먹히는 갈래라, 코드만
        # 봐서는 안 보인다. 이 칸만 맨 텍스트였던 것이 원인이다.
        {"short": True, "title": "확인 시각", "value": _quoted(_kst_text(identified_at))},
        {
            "short": True,
            "title": "확인한 요원",
            "value": _quoted(identified_by) if identified_by else "미기재",
        },
    ]
    # ⭐ 학번(2026-08-06 · 프론트 27차 §3-2). **이름 바로 뒤가 아니라 여기다** — 위 넷은
    # 사용자가 시안으로 고른 두 칸 배치라 그 사이에 끼우면 짝이 어긋난다.
    # 빈 필드를 넣지 않고 아예 뺀다 — 두 칸 정렬이라 빈 칸이 남으면 줄이 어긋나 보인다.
    if identified_student_no:
        fields.append(
            {"short": True, "title": "학번", "value": _quoted(identified_student_no)}
        )
    if note:
        fields.append({"short": False, "title": "메모", "value": _quoted(note)})
    return {
        "attachments": [
            {
                "fallback": f"{title} — {_CARD_UNSAFE_RE.sub(' ', identified_person)}",
                "color": IDENTIFY_CARD_COLOR,
                "title": title,
                "fields": fields,
                "footer": f"C207 보안로봇 · 경고 {alert_id}번",
            }
        ]
    }


async def send_identify_report(
    *,
    gate_no: int | None,
    alert_id: int,
    identified_person: str,
    identified_student_no: str | None = None,
    identified_by: str | None,
    identified_at: dt.datetime | None,
    note: str | None,
    corrected: bool = False,
) -> bool:
    """신원 확인 카드를 두 번째 웹훅(상위 보고선)으로 보낸다. 비활성이면 아무것도 안 하고 False.

    미태깅 카드와 설정·발사 판정을 나눠 둔다 — 보안 채널만 켠 배포, 보고선만 켠 배포가
    둘 다 정상 상태다. 여기 실패는 삼킨다(신원 입력 자체는 이미 커밋됐다).
    """
    settings = get_settings()
    if not settings.mattermost_identify_active:
        # ⚠ 신원 값을 로그에 찍지 마라. 경고 번호만 남긴다.
        logger.debug("신원 보고 웹훅 비활성 — 카드 발사 생략 (alert=%s)", alert_id)
        return False

    card = _build_identify_card(
        gate_no=gate_no,
        alert_id=alert_id,
        identified_person=identified_person,
        identified_student_no=identified_student_no,
        identified_by=identified_by,
        identified_at=identified_at,
        note=note,
        corrected=corrected,
    )
    return await _post_card(settings.mattermost_identify_webhook_url, card, what="신원 보고")
