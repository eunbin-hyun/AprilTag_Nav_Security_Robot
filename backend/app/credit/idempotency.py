"""멱등 인입에서 "같은 event_id인데 본문이 다른" 요청을 잡아내는 자리.

event_id UNIQUE + ON CONFLICT DO NOTHING은 재시도 중복을 안전하게 흡수하는데, 본문이
달라진 재전송까지 **조용히 버린다**. 실측(P0-4-b)에서 gate9 B_TO_A를 gate1 A_TO_B와 같은
event_id로 보내니 200 duplicate가 떨어지고 저장된 행은 1차 본문 그대로였다. 기기 쪽에서
event_id 카운터가 재부팅으로 되감기면(안건② `event_id_require_boot_id=False`) 서로 다른
사건이 한 행으로 접히는데 서버는 아무 말도 안 한다.

그래서 중복 판정 뒤에 저장된 행과 들어온 본문을 견줘서, 어긋난 칸 이름을 돌려준다.
호출자는 그 목록이 비어 있지 않으면 409로 거절한다(인입 계약 — 409는 클라이언트가
성공 취급하고 재시도하지 않는 코드다).

⚠ 해시가 아니라 칸별 비교다. 해시로 접으면 "무엇이 달랐나"를 못 알려줘서 기기 쪽 디버깅이
안 되고, 저장 안 되는 참고 칸(`result`)까지 해시에 섞이면 정상 재전송이 409로 막힌다.

⚠ 자리 관련 — 이 helper는 크레딧 도메인이 아니라 인입 공통 관심사인데, 팀 파일 소유
경계(A조 = `app/credit/**` + `app/routers/ingest.py`) 안에 두려고 여기 뒀다. 나중에
`app/` 공통으로 올리는 건 소유가 정리된 뒤에 한다.
"""
from __future__ import annotations

import datetime as dt
from typing import Any


def as_utc(ts: dt.datetime) -> dt.datetime:
    """tz 없는 시각을 UTC로 본다. 인입 스키마가 naive도 통과시켜서 단위를 맞춰야 한다."""
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=dt.timezone.utc)


def _same(stored: Any, incoming: Any) -> bool:
    """저장값과 들어온 값이 같은가. 시각은 순간(instant)으로 견준다.

    DB에서 올라온 값은 timestamptz라 aware인데 들어온 본문은 naive일 수 있다. 그대로
    비교하면 어긋난 칸으로 오판해서 정상 재전송이 409로 막힌다.
    """
    if isinstance(stored, dt.datetime) and isinstance(incoming, dt.datetime):
        return as_utc(stored) == as_utc(incoming)
    if isinstance(stored, dt.datetime) or isinstance(incoming, dt.datetime):
        return False
    return stored == incoming


def conflicting_fields(row: Any, incoming: dict[str, Any]) -> list[str]:
    """저장된 행과 들어온 본문에서 어긋난 칸 이름(정렬)을 돌려준다. 같으면 빈 목록.

    `incoming`에 넣는 칸은 **DB에 실제로 저장되는 칸만**이다. 저장 안 되는 참고 칸을 넣으면
    비교 대상이 없어 늘 어긋난 것으로 잡힌다.
    """
    return sorted(
        field for field, value in incoming.items() if not _same(getattr(row, field), value)
    )
