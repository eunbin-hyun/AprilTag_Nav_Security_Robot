"""셔틀 도착 신호 적재 몸통 + 화면 가상 호출 전용 부품.

## 왜 이 모듈이 따로 있나

셔틀은 실기기를 안 만들고 화면 버튼으로 가상 호출한다(사용자 확정). 그런데 기기용 인입
`POST /api/shuttle-arrivals`는 X-API-Key를 요구한다 — 화면이 그 키를 들면 브라우저에 키가
노출되고, 그 키 하나로 태깅·통과 인입과 신원 입력까지 전부 열린다. 그래서 셔틀 하나만
키 없이 부르는 창구(`POST /api/shuttle-calls`)를 따로 뒀다.

## 왜 "서버가 자기 키로 자기 인입을 HTTP로 부르는" 구조를 안 골랐나

기본안이던 자기 HTTP 호출은 세 가지를 새로 만든다.
- 자기 주소를 알아야 한다. 컨테이너 안에서 공개 도메인으로 나가면 nginx·
  Cloudflare를 한 바퀴 돌고, `http://127.0.0.1:8000`로 가면 포트·프리픽스가 배포마다 다르다.
- 워커가 하나다. 요청 안에서 자기 서버로 또 요청을 걸면 앞 요청이 워커를 잡은 채로 뒤
  요청을 기다리는 모양이 되고, 타임아웃이 없으면 그대로 선다.
- 실패 지점이 둘로 늘어난다. DB는 성공인데 자기 호출만 타임아웃 나는 갈래가 생긴다.

그래서 **몸통을 함수 하나로 빼고 두 창구가 같은 함수를 부르는 구조**로 갔다. 갈래가 늘 때
계약이 갈라지는 걸 막는 방식이고(같은 이유로 조회 목록과 스냅샷도 fetch 함수를 공유한다),
자기 주소·워커·두 번째 실패 지점이 아예 안 생긴다.

## 두 창구가 진짜로 한 몸통을 탄다 (2026-07-30)

예전에는 `routers/ingest.py`가 자기 몸통을 따로 들고 있었고, 그쪽에만 예외 갈래 셋(중복
재전송 복구·본문 충돌 409·응답 헤더)이 있었다. 화면 호출은 event_id를 서버가 만드니 그
갈래를 아예 안 타서 결함이 안 드러났을 뿐, 계약은 갈라진 상태였다. 지금은 셋 다 이 파일
`record_shuttle_arrival`로 올라왔고 기기 인입이 그 함수를 부른다.

⚠ **갈래를 이 함수 밖으로 다시 빼지 마라.** 기기 쪽 몸통을 한 줄 호출로 갈아 끼울 때
예외 갈래를 같이 안 올리면 "통지 유실 복구"(P0-4-c)가 조용히 사라진다. 칸 집합만 대조하는
시험은 그 소실을 못 잡는다 — 갈래별 시험이 `tests/test_shuttle_call.py`에 따로 있다.
"""
from __future__ import annotations

import datetime as dt
import logging
import secrets
import threading
import time
from dataclasses import dataclass

from fastapi import HTTPException, Response
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.credit.idempotency import conflicting_fields
from app.device_sound import KIND_BUS_ARRIVAL, push_sound
from app.dispatch import (
    begin_dispatch,
    # 창을 안 연 갈래의 확정 결과는 `shuttle_arrival`을 낸 **뒤에** 여기서 한 번만 낸다
    # (계약 §2.1 순서). 방송 자체의 정의 자리는 app/dispatch.py 하나다.
    broadcast_outcome,
    pending_window_open,
    resend_dispatch,
    # room_id 칸 규칙의 정의 자리는 app/dispatch.py 하나다 — 알림과 확정 결과가 같은 규칙을
    # 지나야 플래그를 켠 화면이 두 메시지를 묶을 수 있다.
    with_room as _with_room,
)
from app.models import ShuttleArrival
from app.ws import make_envelope, manager

logger = logging.getLogger("c207.shuttle")

# 셔틀 통지 결과를 응답 헤더로도 드러낸다. 0이면 붙은 로봇이 없어 명령이 못 나갔다는 뜻이다.
#
# 이제 본문에도 같은 값이 실린다(IngestAck.notified_robot_count). 그런데도 헤더를 남기는
# 이유는 라파 `hardware/rpi`가 나중에 읽을 수 있는 자리라서다 — 지우는 건 계약 축소다.
# `routers/ingest.py`가 이 이름을 여기서 가져다 쓴다(정의 자리는 여기 하나다).
SHUTTLE_NOTIFY_HEADER = "X-Shuttle-Notified-Robots"

# 같은 게이트를 짧은 시간에 다시 부르는 걸 삼키는 창(초). 화면 버튼은 연타되고, 시연
# 중에는 여러 사람이 같은 버튼을 누른다 — 그때마다 로봇에 목적지 명령이 새로 나가면
# 로봇이 출동 도중에 명령을 다시 받아 도착 판정(mission_status 에지)이 흔들린다.
#
# 값을 config.Settings에 안 넣은 이유 — 이번 사이클에 다섯 조가 config.py를 같이 만져서
# Settings에 줄을 더하면 텍스트 충돌이 난다. 창 하나짜리 상수라 튜닝 여지도 작다.
# 설정으로 올릴 거면 팀 결정 뒤에 한 줄 옮기면 된다.
SHUTTLE_CALL_COOLDOWN_SEC = 5.0

# 화면 호출 event_id의 device 자리 접두. 기기 event_id와 한눈에 갈라지고, 정리 도구의
# --device-id 범위 옵션으로 시험 자료만 골라 지울 수 있는 축이기도 하다.
WEB_CALL_DEVICE_PREFIX = "webcall"

# 프로세스가 살아 있는 동안 고정인 boot 표식. event_id를 안건② 4토막 형식
# ({device}-b{boot8}-{unix_ms}-{seq})으로 만들어, 나중에 event_id_require_boot_id를
# 켜도 화면 호출이 형식 때문에 막히지 않게 한다.
_BOOT_ID = secrets.token_hex(4)

# seq는 같은 밀리초에 두 번 눌렸을 때만 쓰이는 tiebreaker다. 락으로 감싸는 이유는
# 워커가 하나여도 asyncio가 아니라 스레드에서 부를 수 있어서다(BackgroundTasks·시험).
_seq_lock = threading.Lock()
_seq = 0

# (gate_no, 룸키) → 마지막 호출의 단조 시각. DB 밖 상태라 재기동하면 비어 있다.
_last_call: dict[tuple[int, str | None], float] = {}


def _call_key(gate_no: int, room_id: str | None) -> tuple[int, str | None]:
    """쿨다운 키. 룸 축은 **안건① 플래그가 켜졌을 때만** 넣는다.

    ⚠ `room_id`는 요청이 고르는 칸이라, 플래그가 꺼진 지금도 키에 넣으면 값만 바꿔 보내는
    것으로 5초 문이 그대로 열린다 — 연타 차단이 아무 일도 안 하게 된다. 계약 1.1·1.7 표도
    "같은 게이트 두 번째 호출을 입구에서 삼킨다"라 게이트 하나가 기준이다.
    (크레딧 쿨다운의 `_cooldown_room`과 같은 규칙이다 — 두 자리가 같은 잣대를 써야 한다.)
    """
    return (gate_no, room_id if get_settings().credit_scope_room else None)


def web_call_boot_id() -> str:
    """화면 호출 event_id에 들어간 boot 표식.

    행의 boot_id 칸에도 같은 값을 채운다 — 기기 인입이 "event_id 안에 boot_id가 있으면
    서버가 뽑아서 채운다"는 규칙(EventIdIn)을 따르는데, 화면 호출만 event_id엔 있고 칸은
    비어 있으면 같은 자료를 두 규칙으로 읽게 된다.
    """
    return _BOOT_ID


def _mono() -> float:
    """단조 시계. 시험이 갈아끼우는 자리라 함수로 뺀다(쿨다운의 _wall_now와 같은 방식)."""
    return time.monotonic()


def reset_shuttle_call_state() -> None:
    """인메모리 호출 쿨다운을 비운다.

    시험 위생용이고, conftest.py의 `_clean` 훅이 매 케이스 앞에 부른다 — 안 비우면 앞
    케이스의 호출이 다음 케이스의 호출을 429로 삼킨다. 예전에는 시험 파일이 자기 autouse
    픽스처로 불렀는데 그건 그 파일 하나만 지켜서, 셔틀 호출을 부르는 다음 파일이 원인 모를
    429로 깨졌다(tests/test_shuttle_cooldown_hygiene.py가 그 갈래를 못박는다).
    """
    _last_call.clear()


def next_web_call_event_id(gate_no: int) -> str:
    """화면 호출용 event_id를 만든다. 형식은 `webcall-{gate}-b{boot8}-{unix_ms}-{seq}`.

    화면이 event_id를 정하면 같은 값을 다시 보내 남의 신호를 덮거나, 멱등 키를 골라
    적재를 조용히 막을 수 있다. 그래서 서버가 만든다.

    unix_ms만으로도 거의 안 겹치는데 seq를 붙인 이유는 같은 밀리초 연타다 — 겹치면
    두 번째 호출이 "재시도 중복"으로 조용히 사라진다(그게 안건②가 고치려는 그 함정이다).
    """
    global _seq
    with _seq_lock:
        _seq += 1
        seq = _seq
    unix_ms = int(time.time() * 1000)
    return f"{WEB_CALL_DEVICE_PREFIX}-{gate_no}-b{_BOOT_ID}-{unix_ms}-{seq}"


def in_call_cooldown(gate_no: int, room_id: str | None) -> float:
    """이 게이트가 아직 쿨다운 창 안이면 남은 초, 아니면 0.0.

    창 안이면 마지막 호출 시각을 밀지 않는다 — 밀면 연타가 창을 계속 뒤로 끌어서
    누르는 걸 멈출 때까지 영원히 안 열린다.
    """
    last = _last_call.get(_call_key(gate_no, room_id))
    if last is None:
        return 0.0
    remain = SHUTTLE_CALL_COOLDOWN_SEC - (_mono() - last)
    return remain if remain > 0 else 0.0


def mark_call(gate_no: int, room_id: str | None) -> None:
    _last_call[_call_key(gate_no, room_id)] = _mono()


def unmark_call(gate_no: int, room_id: str | None) -> None:
    """방금 찍은 눈금을 지운다. **호출이 실제로 안 받아들여졌을 때만** 부른다.

    검사와 적재 사이가 비면 화면 연타 두 건이 둘 다 429를 안 맞고 각각 도착 행·대기 창을
    만든다 — 연타 차단이 막으려던 바로 그 상태다. 그래서 눈금은 검사 직후에 찍고, 적재가
    터진 갈래에서만 이걸로 되돌린다(`dispatch.rollback_command_throttle`과 같은 방식).
    """
    _last_call.pop(_call_key(gate_no, room_id), None)


def reject_payload_conflict(event_id: str, fields: list[str]) -> None:
    """같은 event_id에 다른 본문이 온 셔틀 요청을 409로 거절한다.

    인입 계약(routers/ingest.py 모듈 머리)이 409를 "성공 취급, 재시도 없음"으로 못박아 뒀다.
    조용히 버리면 서로 다른 사건이 한 행으로 접히고 기기 쪽에서 알아챌 방법이 없다.

    ⚠ 라파 `hardware/rpi/gate_server.py:790~802` sender_worker는 409를 재시도하지 않고 버린다
    (2026-08-04 실물 재측정 — 4xx는 버리고 5xx·네트워크만 백오프 재시도한다). 그 기기 큐가
    막히지는 않지만 그 이벤트는 영영 안 올라온다. `routers/ingest.py`의 같은 함수 주석 참고.

    ⚠ `routers/ingest.py`의 `_reject_payload_conflict`와 본문 모양이 같다. 한 자리로 합치려면
    중립 모듈이 필요한데(ingest가 이 파일을 부르는 방향이라 거꾸로는 못 부른다), 그 파일들이
    다른 조 소유라 이번엔 나누지 않았다. 둘 중 하나를 고치면 나머지도 같이 고쳐라.
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


@dataclass
class ShuttleRecordResult:
    stored: bool                     # 이번 요청으로 새 행이 생겼나(event_id 멱등)
    arrival_id: int | None
    notified_robot_count: int
    command_id: str | None
    resent: bool = False             # 중복인데 아직 못 나간 명령을 다시 냈나(통지 유실 복구)


async def record_shuttle_arrival(
    session: AsyncSession,
    *,
    event_id: str,
    gate_no: int | None,
    signal_ts: dt.datetime,
    boot_id: str | None = None,
    room_id: str | None = None,
    shuttle_no: str | None = None,
    response: Response | None = None,
) -> ShuttleRecordResult:
    """셔틀 도착 신호를 적재하고, 출동 대기 창을 열고, 화면에 알린다.

    기기 인입(`POST /api/shuttle-arrivals`)과 화면 가상 호출(`POST /api/shuttle-calls`)이
    같이 타는 몸통이다. 예외 갈래까지 여기 다 있어야 창구가 늘어도 계약이 안 갈라진다.

    ⚠ **명령은 이 함수 안에서 안 나간다**(2026-07-31 셔틀출동 설계 §2.2). 예전에는 여기서
    바로 쐈는데, 지금은 `app/dispatch.begin_dispatch`가 5초 창을 열고 그 안에 선택이 오면
    그 목적지로, 안 오면 그날 기본값으로 나간다. 그래서 새 행 갈래의 `notified_robot_count`는
    늘 0이다 — 확정 결과는 WS `shuttle_dispatch_result`로 따로 나간다.
    `shuttle_dispatch_hold_sec`를 0 이하로 두면 예전처럼 이 자리에서 즉시 발사한다.

    ⚠ 통지 실패는 **재전송으로 복구된다.** 예전엔 새 행이 생겼을 때만 통지해서, WS 전송이
    실패하면(로봇이 그 순간 떨어져 있으면) 행은 커밋됐는데 명령은 사라지고 같은 요청을 다시
    보내도 duplicate로 튕겼다. 수동 재발행 경로도 없어서 시연자가 알 방법이 없었다(실측 P0-4-c).

    지금은 통지 여부의 정본을 DB `notified_robot_id`로 본다.
    - 새 행이면 통지한다.
    - 중복인데 아직 아무 로봇에도 안 갔으면(`notified_robot_id IS NULL`) 다시 통지한다.
    - 이미 갔으면 아무것도 안 한다 — 재전송이 로봇을 두 번 출동시키면 안 된다.
    `command_id`가 `shuttle-{arrival_id}`로 결정적이라 재발행분도 로봇 쪽에서 같은 명령으로
    접힌다.

    중복인데 본문이 어긋나면 409다(`reject_payload_conflict`). 조용히 버리면 서로 다른 사건이
    한 행으로 접힌다.

    `response`를 주면 통지 결과를 헤더(`SHUTTLE_NOTIFY_HEADER`)에도 싣는다. 헤더를 라우터가
    아니라 여기서 찍는 이유는 갈래별 값(새 행·재전송·이미 통지됨)이 다 이 함수 안에서
    갈라져서다 — 라우터가 다시 계산하면 창구마다 어긋난다. 409로 나가는 응답엔 헤더가 안
    붙는다(예외가 여기서 올라가고, 예전 거동도 같았다).
    """
    stmt = (
        pg_insert(ShuttleArrival)
        .values(
            event_id=event_id,
            boot_id=boot_id,
            gate_no=gate_no,
            room_id=room_id,
            shuttle_no=shuttle_no,
            signal_ts=signal_ts,
        )
        .on_conflict_do_nothing(index_elements=["event_id"])
        .returning(ShuttleArrival.id)
    )
    arrival_id = (await session.execute(stmt)).scalar_one_or_none()
    await session.commit()

    if arrival_id is None:
        result = await _recover_duplicate(
            session,
            event_id=event_id,
            incoming={
                "boot_id": boot_id,
                "gate_no": gate_no,
                "room_id": room_id,
                "shuttle_no": shuttle_no,
                "signal_ts": signal_ts,
            },
        )
    else:
        # ⭐ 게이트 스피커에 "울려라"를 넣는 자리. **여기가 도착이 정확히 한 번 잡히는 곳이다.**
        #
        # 위 INSERT가 `on_conflict_do_nothing ... RETURNING id`라 `arrival_id`는 행이 진짜로
        # 생겼을 때만 값이 있다. 기기 재전송·화면 연타는 전부 위 `_recover_duplicate` 갈래로
        # 빠져서 이 자리를 안 지난다. 그리고 두 창구(기기 인입 `POST /api/shuttle-arrivals`와
        # 화면 호출 `POST /api/shuttle-calls`)가 이 함수 하나로 모여서, 여기 한 줄이면 창구가
        # 늘어도 소리 갈래가 안 갈라진다(모듈 머리 "두 창구가 진짜로 한 몸통을 탄다").
        #
        # ⚠ **출동 갈래(`begin_dispatch`) 앞에 둔다.** 소리는 게이트에 서 있는 사람한테
        # "셔틀이 왔습니다"를 알리는 거라 로봇이 나가고 말고와 무관하다. 뒤로 미루거나
        # `dispatch.notify_shuttle_arrival` 쪽에 붙이면 두 가지가 어긋난다 — 긴급정지 갈래는
        # 신호를 버려서 아예 안 울리고, 대기 창 갈래는 5초 뒤에야 울린다. 그리고 그 함수는
        # 재전송(`resend_dispatch`)도 타서 같은 도착에 두 번 불린다.
        push_sound(KIND_BUS_ARRIVAL, gate_no=gate_no, event_id=event_id)
        start = await begin_dispatch(
            session,
            arrival_id=arrival_id,
            gate_no=gate_no,
            event_id=event_id,
            shuttle_no=shuttle_no,
            room_id=room_id,
        )
        if start.pending is None and start.notified_robot_count == 0:
            logger.warning(
                "셔틀 도착 통지가 로봇에 못 갔다 — 붙은 로봇이 없다"
                " (event_id=%s gate=%s). 같은 요청을 다시 보내면 재발행된다.",
                event_id, gate_no,
            )
        await manager.broadcast(
            make_envelope(
                "shuttle_arrival",
                _with_room(
                    {
                        "event_id": event_id,
                        # 이 도착이 저장된 행의 PK. 셔틀 계열 경고(`shuttle_arrival_expired`·
                        # `robot_arrival`·`robot_departure`)가 전부 `source_id`에 이 값을 싣고
                        # `shuttle_dispatch_result`도 `arrival_id`로 이 값을 싣는데, 정작 도착
                        # 알림에는 없었다. `pending.arrival_id`로만 새어 나와서 창을 안 연
                        # 갈래(긴급정지 차단·`SHUTTLE_DISPATCH_HOLD_SEC` 0 이하)에서는 화면이
                        # 두 메시지를 못 묶었다 — 그 갈래가 `pending=null`이다.
                        # 이 자리는 새 행 갈래(`arrival_id is not None`) 안이라 늘 int다.
                        "arrival_id": arrival_id,
                        "gate_no": gate_no,
                        "shuttle_no": shuttle_no,
                        # 0이면 로봇이 안 붙어 있어 명령이 못 갔다는 뜻이다(화면에서 확인 가능).
                        # ⚠ 대기 창이 열렸을 때도 0이다 — 아직 아무 명령도 안 나갔으니까.
                        # 그때는 `pending`이 차 있고, 확정 결과는 5초 뒤 별도 메시지
                        # (`shuttle_dispatch_result`)로 나간다.
                        "notified_robot_count": start.notified_robot_count,
                        "command_id": start.command_id,
                        # 대기 창 정보(마감 시각·남은 초·버튼 셋). 창을 안 열었으면 null이다.
                        "pending": start.pending,
                    },
                    room_id,
                ),
            )
        )
        # ⚠ 순서가 계약이다(§2.1 "곧이어 shuttle_dispatch_result가 따라온다"). 창을 안 연
        # 갈래(긴급정지 차단·대기 창 꺼짐)는 신호를 받은 자리에서 이미 결론이 나는데, 그
        # 결과를 dispatch 안에서 바로 방송하면 화면이 아직 모르는 신호의 결과를 먼저 받는다.
        if start.outcome is not None:
            await broadcast_outcome(start.outcome)
        result = ShuttleRecordResult(
            stored=True,
            arrival_id=arrival_id,
            notified_robot_count=start.notified_robot_count,
            command_id=start.command_id,
        )

    if response is not None:
        response.headers[SHUTTLE_NOTIFY_HEADER] = str(result.notified_robot_count)
    return result


async def _recover_duplicate(
    session: AsyncSession, *, event_id: str, incoming: dict
) -> ShuttleRecordResult:
    """중복 셔틀 신호를 만났을 때 본문을 견주고, 아직 못 나간 명령이면 다시 낸다.

    ⚠ 여기서는 WS 메시지를 다시 안 밀어낸다. 그 event_id의 `shuttle_arrival` 메시지는 1차에서
    이미 나갔고 화면이 event_id로 dedupe하니(telemetry.html `dedupKey`) 두 번째 장은 조용히
    버려진다. 복구 사실은 응답 헤더와 서버 로그로 드러낸다 — 화면에도 보이게 하려면 화면 쪽
    dedupe 규칙을 같이 고쳐야 한다.

    ⭐ **행을 잠그고 읽는다**(`with_for_update`). 재전송 판정은 "읽고(notified_robot_id가
    NULL인가) → 고치고(명령을 내고) → 쓰는(그 로봇을 적는)" 흐름이라, 잠그지 않으면 같은
    event_id로 동시에 들어온 재시도 둘이 **둘 다 NULL을 보고** 각자 명령을 낸다 — 로봇이 두 번
    출동한다. 실제 쓰기는 여기가 아니라 `robot_channel.notify_shuttle_arrival`이 하니
    (`_mark_notified`) 이 함수 안에서 원자적으로 묶을 방법이 없고, 잠금이 그 자리를 메운다.
    잠금은 그 커밋에서 풀린다.
    """
    row = (
        await session.execute(
            select(ShuttleArrival)
            .where(ShuttleArrival.event_id == event_id)
            .with_for_update()
        )
    ).scalars().one_or_none()
    if row is None:
        # 경합 — 딴 트랜잭션이 넣는 중이다. 중복이라는 사실만 알리고 넘어간다.
        logger.warning("중복 판정인데 저장된 셔틀 행을 못 찾았다 (event_id=%s)", event_id)
        return ShuttleRecordResult(
            stored=False, arrival_id=None, notified_robot_count=0, command_id=None
        )

    diffs = conflicting_fields(row, incoming)
    if diffs:
        logger.warning(
            "같은 event_id에 다른 셔틀 본문이 왔다 — 거절한다 (event_id=%s 어긋난 칸=%s)",
            event_id, diffs,
        )
        reject_payload_conflict(event_id, diffs)

    if row.notified_robot_id is not None:
        # 이미 로봇이 받았다. 다시 내면 두 번 출동한다.
        # 통지 수에는 1을 싣는다 — "적어도 한 대가 받았다"는 뜻이다. DB에 남는 건 대표 로봇
        # 한 대뿐이라 원래 몇 대가 받았는지는 여기서 되짚을 수 없다.
        return ShuttleRecordResult(
            stored=False,
            arrival_id=row.id,
            notified_robot_count=1,
            command_id=f"shuttle-{row.id}",
        )

    # ⚠ 대기 창이 아직 도는 중이면 재전송을 아예 안 태운다(2026-07-31 검증 P1). 창이
    # 살아 있다는 건 "명령이 아직 나갈 예정"이지 "유실됐다"가 아니다. 예전에는 여기서 창을
    # 닫고 즉시 쏴서, 화면은 카운트다운을 계속 그리는데 로봇은 이미 떠난 상태가 됐다.
    # 창은 마감 때 스스로 발사하고, 재시도가 마감을 뒤로 밀지도 않는다.
    if pending_window_open(row.id):
        logger.info(
            "셔틀 도착 재시도가 왔지만 대기 창이 아직 열려 있다 — 창 마감을 그대로 기다린다"
            " (event_id=%s arrival_id=%s)",
            event_id, row.id,
        )
        return ShuttleRecordResult(
            stored=False,
            arrival_id=row.id,
            notified_robot_count=0,
            command_id=f"shuttle-{row.id}",
            resent=False,
        )

    # ⚠ 재전송은 대기 창을 다시 안 연다. 재전송이 왔다는 건 "명령이 안 나갔다"는 뜻이지
    # "사람이 다시 골라야 한다"는 뜻이 아니고, 5초를 또 기다리면 기기 쪽 재시도가 창을 계속
    # 새로 열어 명령이 영영 안 나갈 수 있다(app/dispatch.resend_dispatch 주석).
    delivered, command_id = await resend_dispatch(
        session, arrival_id=row.id, gate_no=row.gate_no
    )
    logger.info(
        "셔틀 도착 재전송 — 아직 못 나간 명령을 다시 냈다 (event_id=%s command_id=%s 통지=%d대)",
        event_id, command_id, delivered,
    )
    return ShuttleRecordResult(
        stored=False,
        arrival_id=row.id,
        notified_robot_count=delivered,
        command_id=command_id,
        resent=True,
    )
