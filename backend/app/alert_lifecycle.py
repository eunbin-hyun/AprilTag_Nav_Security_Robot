"""사건(Alert) 수명주기 축. 상태 낱말·전이 규칙·불변식을 한 자리에 모은다.

## 왜 필요한가

경고 행에 있던 대응 흔적은 `ack` 불리언 하나와 신원 칸 넷뿐이었다. 둘이 서로 독립인
불리언이라 "어디까지 진행됐나"를 가리키는 값이 아예 없었다. 실서버 경고 18건이 전부
`ack=false`이고 그중 id 18은 신원까지 적힌 채 활성인데, 그게 잘못된 상태인지 정상인지
판정할 축조차 없다. 요원이 각본 ⑤단계를 끝까지 밟아도 사건이 안 닫힌다.

## 상태 넷

| 상태 | 뜻 | 누가 올리나 |
|---|---|---|
| `OPEN` | 경고가 났고 아무 대응 흔적이 없다 | 상태머신이 Alert를 만들 때(서버 기본값) |
| `ACKED` | 관제가 확인을 눌렀다. 대응 중 | `POST /api/alerts/{id}/ack`, 상태 창구 |
| `IDENTIFIED` | 안쪽 요원이 붙잡아 신원까지 적었다 | `POST /api/alerts/{id}/identify` |
| `RESOLVED` | 사건이 닫혔다. 닫은 사유가 같이 남는다 | 상태 창구(`resolution` 필수) |

## `ack`와 이 축의 관계 — 묶지 않는다

`ack`는 **"관제 화면에서 확인을 눌렀나"** 하나만 가리키는 값으로 그대로 둔다. 신원 입력이
`ack`를 안 건드리는 예전 규칙도 그대로다. 관제실에서 경고를 확인하는 사람과 게이트 안쪽에서
붙잡아 신원을 적는 요원은 **다른 사람**이라, 한쪽 조작이 다른 쪽 기록을 대신 찍으면 감사
기록이 거짓이 된다(이 계약은 test_alert_identify.py가 이미 못박아 놨다).

그래서 "어디까지 진행됐나"는 `status`가, "관제가 봤나"는 `ack`가 따로 답한다.
- `status`는 전진 전용 순위 축이라 `ack=false`인데 `IDENTIFIED`인 상태가 **정의된 값**이다
  (관제는 아직 안 눌렀고 요원이 신원을 적은 단계). 예전에는 같은 상태가 우연이었다.
- `acked_at`이 관제 확인의 증거다. 새로 생기는 행에서 `ack == (acked_at is not None)`이다.

## 전이 규칙 (설계 근거를 같이 적는다)

1. **축이 하나이고 전진만 된다.** OPEN(0) < ACKED(1) < IDENTIFIED(2) < RESOLVED(3)이고,
   낮은 쪽으로 내리는 요청은 409다. 감사 기록이라 되돌리기를 기본값으로 열면 "닫힌 사건이
   다시 열렸다"가 조용히 일어난다.
2. **건너뛰기는 된다.** OPEN에서 바로 RESOLVED로 갈 수 있다. 센서 오검출로 쌓인 경고
   18건을 하나하나 ack부터 밟게 하면 정리를 아예 못 한다.
3. **`ACKED`로 가는 전이만 `ack`·`acked_at`을 찍는다.** RESOLVED로 건너뛴 전이는 `ack`를
   안 건드린다 — 관제가 안 본 채로 일괄 종결한 건과 확인까지 한 건은 다른 사실이다.
4. **`IDENTIFIED`는 상태 창구로 못 올린다(422).** 신원 원문 없이 그 상태로 올리면 감사
   기록이 빈 채로 종결 후보가 된다. 그 상태를 올릴 자격이 있는 건 신원 창구뿐이다.
5. **신원을 지우면 `IDENTIFIED`는 한 단계 내려온다.** 유일한 후퇴이고, 신원이 없는데
   IDENTIFIED로 남는 거짓말을 막는 자리다. 내려갈 자리는 `acked_at`이 있으면 `ACKED`,
   없으면 `OPEN`이다(관제 확인 증거를 그대로 따른다). 이미 RESOLVED면 안 건드린다.
6. **RESOLVED로 갈 때 `resolution`이 필수다.** 왜 닫았는지 없이 닫으면 센서 오검출과
   진짜 미태깅 사건이 같은 모양으로 사라진다. 오검출 비율은 정리 도구와 통계가 읽는 값이다.
7. **같은 상태로 다시 보내면 200 멱등이고 메시지는 안 나간다**(ack 재확인 규칙과 같다).

## 예전 18행이 어느 상태로 읽히나 (마이그레이션 0005 backfill)

`resolved_at`은 새 칸이라 예전 행은 절대 RESOLVED가 아니다. 그래서 이 순서로 읽는다.

- `identified_at IS NOT NULL` → `IDENTIFIED` (실서버 id 18이 여기 온다)
- 위가 아니고 `ack = true` → `ACKED`
- 나머지 → `OPEN` (실서버 나머지 17건)

`ack` 값은 마이그레이션이 한 행도 안 바꾼다 — 예전 조작 기록을 사후에 고쳐 쓰지 않는다.
`acked_at`·`resolved_at`도 NULL로 둔다. 예전 행이 언제 확인됐는지 서버가 모른다 —
`created_at`으로 채우면 감사 기록에 없는 시각을 지어내는 셈이다. 그래서 예전 ACKED 행은
`ack=true`인데 `acked_at`이 NULL인 유일한 예외다(0005 뒤에 눌린 확인은 둘 다 채워진다).
"""
from __future__ import annotations

import datetime as dt
from enum import Enum


class AlertStatus(str, Enum):
    OPEN = "OPEN"
    ACKED = "ACKED"
    IDENTIFIED = "IDENTIFIED"
    RESOLVED = "RESOLVED"


class AlertResolution(str, Enum):
    """사건을 닫은 사유. 자유 문자열이 아니라 닫힌 낱말로 둔다.

    인증 없는 목록 API(GET /api/alerts)에 실리는 값이라, 자유 문자열이면 요원이 적은
    문장이 그대로 인터넷에 나간다. 통계가 "오검출 몇 %"를 세는 축이기도 해서 낱말이
    흔들리면 집계가 안 된다.
    """

    confirmed = "confirmed"            # 실제 미태깅이었다(신원 확인까지 끝났거나 현장 확인)
    false_positive = "false_positive"  # 센서 오검출이었다(ToF 고착 등)
    duplicate = "duplicate"            # 같은 사건이 여러 경고로 났다
    other = "other"                    # 위 셋에 안 맞는다(메모는 identify_note에 남긴다)


# 전이는 이 순위 하나로 판정한다. 상태를 늘릴 때 이 표에만 넣으면 규칙이 따라온다.
STATUS_RANK: dict[str, int] = {
    AlertStatus.OPEN.value: 0,
    AlertStatus.ACKED.value: 1,
    AlertStatus.IDENTIFIED.value: 2,
    AlertStatus.RESOLVED.value: 3,
}

# 상태 창구(POST /api/alerts/{id}/status)로 직접 올릴 수 있는 상태.
# IDENTIFIED가 빠진 이유는 모듈 머리 규칙 4번.
DIRECT_TRANSITIONS: tuple[str, ...] = (
    AlertStatus.ACKED.value,
    AlertStatus.RESOLVED.value,
)


def normalize_status(raw: str | None) -> str:
    """DB에서 읽은 값을 상태 낱말로 정규화한다. 모르는 값·NULL은 OPEN으로 읽는다.

    컬럼이 NOT NULL DEFAULT 'OPEN'이라 정상 경로에선 NULL이 안 오는데, 마이그레이션을
    거치지 않은 사본 DB나 손으로 넣은 행을 읽을 때 판정이 예외로 터지면 목록 API가
    통째로 500이 된다. 모르는 값은 "아직 아무 일도 안 일어난 것"으로 보는 쪽이 안전하다.
    """
    if raw in STATUS_RANK:
        return raw  # type: ignore[return-value]
    return AlertStatus.OPEN.value


def rank(raw: str | None) -> int:
    return STATUS_RANK[normalize_status(raw)]


def is_forward(current: str | None, to: str) -> bool:
    """to가 current보다 뒤 단계인가. 같은 단계는 False(멱등이라 전이가 아니다)."""
    return rank(to) > rank(current)


def apply_transition(
    alert,
    *,
    to: str,
    actor: str | None = None,
    resolution: str | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """경고 하나를 to 상태로 전진시킨다. 실제로 바뀌었으면 True.

    커밋도 브로드캐스트도 안 한다 — 부르는 쪽(라우터)이 트랜잭션과 메시지를 쥔다.
    같은 단계나 뒤 단계로 부르면 아무것도 안 바꾸고 False다(후퇴 거절은 라우터 몫).

    `ACKED`로 가는 전이만 `ack`·`acked_at`·`acked_by`를 찍는다(모듈 머리 규칙 3번).
    신원 입력이나 종결 건너뛰기는 관제 확인 기록을 대신 찍지 않는다.

    ⚠ `to=ACKED`는 status가 이미 뒤 단계여도 **ack 기록은 찍는다.** 신원이 적힌 경고를
    관제가 확인 누른 건 실제로 일어난 일이라, status가 안 움직인다고 그 사실을 버리면
    "확인 버튼을 눌렀는데 아무 일도 안 일어나는" 화면이 된다. status만 전진 전용이다.

    ⚠ actor는 본문에 실려 온 자칭 문자열이다. X-API-Key가 공용 키 하나라 서버가 "정말
    그 사람인가"를 못 판정한다(코덱스 §6-4와 같은 자리). 감사에 쓸 값이 아니라 "화면에서
    누가 눌렀다고 적었나"까지가 지금의 뜻이다. 진짜 주체는 요원 로그인이 생겨야 잡힌다.
    """
    when = now or dt.datetime.now(dt.timezone.utc)
    changed = False

    if to == AlertStatus.ACKED.value and not alert.ack:
        alert.ack = True
        alert.acked_at = when
        alert.acked_by = actor
        changed = True

    if is_forward(alert.status, to):
        alert.status = to
        if to == AlertStatus.RESOLVED.value:
            alert.resolved_at = when
            alert.resolved_by = actor
            alert.resolution = resolution
        changed = True

    return changed


def demote_after_identity_cleared(alert) -> bool:
    """신원을 지운 뒤 상태를 정리한다. IDENTIFIED였으면 한 단계 내리고 True.

    내려갈 자리는 `acked_at`이 있으면 ACKED, 없으면 OPEN이다 — 관제 확인의 증거를 그대로
    따른다. 유일한 후퇴이고(모듈 머리 규칙 5번), 이미 RESOLVED인 사건은 안 건드린다.
    신원을 정정하려고 지운 것과 닫힌 사건을 되열는 건 다른 조작이다.
    """
    if normalize_status(alert.status) != AlertStatus.IDENTIFIED.value:
        return False
    alert.status = (
        AlertStatus.ACKED.value if alert.acked_at is not None else AlertStatus.OPEN.value
    )
    return True
