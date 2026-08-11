"""서버가 낸 명령과 기기 응답을 DB에 남긴다(0018).

⭐ **왜 있나** — 2026-08-06 로봇이 처음 붙은 날, 서버가 낸 명령이 **DB에 한 줄도 안 남고
있었다.** 출동·복귀·긴급정지·즉시이동도, 젯슨이 받았는지 거절했는지도, 게이트 스피커가
소리를 실제로 냈는지도 전부 로그 파일에만 있었다. 로그는 컨테이너를 갈면 사라지고 질의도
안 된다 — "아까 그 복귀 명령이 로봇에 닿긴 했나"를 되짚을 수가 없었다.

## ⚠ 부르는 자리를 여기 하나로 모은다

명령을 내는 자리가 셋이고(셔틀 출동·단독 명령·게이트 소리) ack를 받는 자리가 둘이다. 각자
INSERT를 적으면 칸 이름과 낱말이 곧 갈라진다 — 실제로 이 저장소에서 그 계열 결함이 여러 번
났다. 그래서 창구를 둘로 좁힌다: `record_issued`·`record_ack`.

## ⛔ 기록이 명령을 막으면 안 된다

**본 흐름과 다른 트랜잭션**으로 쓰고 예외를 통째로 삼킨다. 로봇을 세우는 긴급정지가 로그
INSERT 실패로 안 나가면 그게 훨씬 큰 사고다. 대신 삼킨 자리는 `warning`으로 남긴다.

⚠ 이 판단은 `robot_status_log`와 **반대다.** 그쪽은 알림과 같은 트랜잭션이라 커밋이 터지면
같이 되돌아간다 — 거기는 "알림은 남았는데 그때 위치가 없는 짝"을 막는 게 목적이라서다.
여기는 기록이 본 행동을 막지 않는 게 목적이다.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from sqlalchemy import select

from app.db import get_session
from app.models import DeviceCommandLog

logger = logging.getLogger(__name__)

CHANNEL_ROBOT = "robot"
CHANNEL_GATE = "gate"

# 젯슨 계약의 `result` 낱말과 게이트 쪽 낱말. 늘리면 `models.DeviceCommandLog.ack_result`
# 길이(16)를 같이 봐라.
ACK_ACCEPTED = "accepted"
ACK_REJECTED = "rejected"
ACK_PLAYED = "played"

_DETAIL_MAX = 500

# ⛔ **DB 칸 길이와 같은 숫자다**(`models.DeviceCommandLog`: `command_id` String(64) ·
# `ack_result` String(16)). 넘겨서 넣으면 asyncpg가 `StringDataRightTruncation`으로 터지고
# 아래 `except`가 그걸 통째로 삼켜서, 하필 "명령이 닿았나"를 남기려던 그 한 건만 조용히
# 사라진다(2026-08-09 백지검토 ②). `detail`만 `_clip`으로 막혀 있고 이 둘은 안 막혀 있었다.
#
# ⚠ 자를 값이 실제로 온다 — `result`·`command_id` 둘 다 **로봇이 보낸 원문 그대로**이고
# (`robot_channel._log_command_ack`가 `str(...)`만 씌워 넘긴다) 로봇 채널 인증은 기본
# 꺼짐이다. 게이트 ack 창구도 `command_id`를 `max_length=128`로 받아 여기로 넘긴다
# (`routers/rpi_commands.ack_command` — 라우트 128 vs 칸 64로 어긋나 있다).
#
# ⚠ 지금 우리가 내는 값은 전부 짧아서(`_next_command_id`) **정상 갈래 거동은 안 바뀐다.**
_RESULT_MAX = 16
_COMMAND_ID_MAX = 64


def _clip(detail: Any) -> str | None:
    """ack 본문을 글자로 줄인다.

    ⚠ 로봇 채널 인증이 기본 꺼짐이라 아무나 붙어 긴 글자를 올릴 수 있다
    (`robot_channel._log_command_ack`가 같은 이유로 `repr`+자르기를 한다). DB 칸이 Text라
    길이 제한이 없어서 여기서 잘라야 한다.
    """
    if detail is None:
        return None
    text = detail if isinstance(detail, str) else repr(detail)
    return text[:_DETAIL_MAX]


async def record_issued(
    *,
    channel: str,
    command_id: str,
    command_type: str | None = None,
    target: str | None = None,
    payload: dict | None = None,
) -> None:
    """명령을 낸 사실을 남긴다. 실패해도 부른 쪽을 안 막는다."""
    # ⚠ `record_ack`와 **같은 자를 쓴다.** 한쪽만 자르면 64자를 넘는 이름이 발행 행과 ack 행에서
    # 서로 다른 값이 돼 둘이 영영 안 붙는다(ack가 발행 행을 못 찾아 새 행을 하나 더 만든다).
    command_id = command_id[:_COMMAND_ID_MAX]
    try:
        async with get_session() as session:
            session.add(
                DeviceCommandLog(
                    channel=channel,
                    command_id=command_id,
                    command_type=command_type,
                    target=target,
                    payload=payload,
                    issued_at=dt.datetime.now(dt.timezone.utc),
                )
            )
            await session.commit()
    except Exception:
        logger.warning("명령 기록을 못 남겼다 command_id=%s", command_id, exc_info=True)


async def record_ack(
    *,
    channel: str,
    command_id: str,
    result: str | None = None,
    detail: Any = None,
    target: str | None = None,
) -> None:
    """기기가 돌려준 응답을 남긴다.

    ⚠ **발행 행이 있으면 갱신하고 없으면 새로 만든다.** 게이트 소리는 발행 행이 아예 없고
    (큐가 인메모리 동기 함수라 그 자리에서 DB를 못 만진다), 로봇 명령도 TTL이 지난 늦은 ack가
    올 수 있다. 어느 쪽이든 **ack가 왔다는 사실은 버리지 않는다** — 모르는 번호였다는 것 자체가
    되짚을 값이다.

    ⚠ 같은 `command_id` 행이 여럿이면 **아직 ack가 안 붙은 가장 최근 것**을 고른다. 기기가
    여러 대일 때 각자 ack를 보내면 행이 그만큼 난다.
    """
    # ⛔ 자르기를 **조회 앞에서** 한다. 넣을 때만 자르면 발행 행은 자른 이름으로 들어가 있는데
    # 여기 WHERE는 안 자른 원문으로 찾아서 못 붙는다(`record_issued`가 같은 자를 쓴다).
    command_id = command_id[:_COMMAND_ID_MAX]
    try:
        async with get_session() as session:
            row = (
                await session.execute(
                    select(DeviceCommandLog)
                    .where(
                        DeviceCommandLog.command_id == command_id,
                        DeviceCommandLog.channel == channel,
                        DeviceCommandLog.ack_at.is_(None),
                    )
                    .order_by(DeviceCommandLog.id.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

            if row is None:
                row = DeviceCommandLog(
                    channel=channel, command_id=command_id, target=target
                )
                session.add(row)
            elif target is not None:
                row.target = target

            row.ack_at = dt.datetime.now(dt.timezone.utc)
            # ⚠ 로봇이 보낸 원문이라 길이를 모른다. String(16)을 넘기면 이 INSERT가 터진다.
            row.ack_result = result[:_RESULT_MAX] if result is not None else None
            row.ack_detail = _clip(detail)
            await session.commit()
    except Exception:
        logger.warning("ack 기록을 못 남겼다 command_id=%s", command_id, exc_info=True)
