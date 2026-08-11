"""pydantic v2 요청/응답 스키마. 인입 계약은 아키텍처 2026-07-23 02절을 따른다."""
from __future__ import annotations

import datetime as dt
import json
import re
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.alert_lifecycle import AlertResolution, AlertStatus, DIRECT_TRANSITIONS
from app.eventid import parse_event_id

# ⚠ app.config를 여기서 모듈 단위로 import하면 안 된다. config가 Destination enum을 쓰려고
# 이 모듈을 import하기 때문에 순환이 난다(config → schemas → config). 설정이 필요한 자리는
# 함수 안에서 늦게 불러온다.


# 셔틀 번호 상한. DB 컬럼(ShuttleArrival.shuttle_no)이 String(32)이라 같은 값으로 맞춘다.
SHUTTLE_NO_MAX_LEN = 32
# 기기 식별자 상한. DB 컬럼(TaggingEvent.device_id·GatePassEvent.device_id)이 둘 다
# String(64)이라 같은 값으로 맞춘다. 상한이 컬럼보다 크면 검증을 지나 asyncpg
# StringDataRightTruncation으로 500이 나서 의미가 없다. 실기기가 지금 보내는 값은
# `hardware/rpi/gate_server.py:40` DEVICE_ID="raspberry01" 11자라 넉넉히 통과한다.
DEVICE_ID_MAX_LEN = 64
# 게이트 번호 범위. 화면 셔틀 호출과 기기 인입 3종이 같이 쓴다.
# - 음수·0을 그대로 받으면 없는 게이트로 호출·사건이 남는다.
# - 상한이 없으면 int32를 넘는 값이 asyncpg DataError(value out of int32 range)로 500이 된다
#   (컬럼이 Integer다). 실기기 기본값은 `gate_server.py:41` GATE_NO=5다.
GATE_NO_MIN = 1
GATE_NO_MAX = 9999


class Direction(str, Enum):
    A_TO_B = "A_TO_B"  # 입장
    B_TO_A = "B_TO_A"  # 퇴장


class PassStatus(str, Enum):
    complete = "complete"
    incomplete = "incomplete"


# ── 인입 요청 ────────────────────────────────────────────────────────────

class EventIdIn(BaseModel):
    """[초안·팀 확정 대기] 안건② 인입 3종 공통 머리.

    boot_id를 따로 안 보내도 event_id 안에 들어 있으면 서버가 뽑아서 채운다.
    강제 전환은 event_id_require_boot_id 플래그를 켤 때만 걸린다(기본 꺼짐).

    ❗결정 필요(회의 안건②) — 본문 boot_id를 "입력값"으로 볼지 "event_id 파생값"으로만
    볼지 아직 안 정했다. 파생값으로 좁히면 본문 필드를 아예 없애고 event_id 하나만
    계약으로 두면 되고, 입력값으로 두면 event_id랑 어긋날 때 뭘 믿을지까지 정해야 한다.
    초안은 입력값을 받되 형식만 최소로 거른다 — 빈 값·공백뿐인 값은 422로 막고,
    hex 여부까지 강제하진 않는다(현장 기기가 이미 다른 표기를 쓸 수 있어서다).
    """

    # 모르는 칸은 422로 돌려준다. 여기 한 줄이 `TaggingEventIn`·`GatePassEventIn`·
    # `ShuttleArrivalIn` 셋에 그대로 상속된다(pydantic v2가 model_config를 MRO로 합친다).
    #
    # 왜 ignore면 안 되나 — pydantic 기본은 모르는 칸을 조용히 버린다. 그래서 `observed_at`을
    # `observed`로 잘못 적은 퍼블리셔는 "칸 이름이 틀렸다"가 아니라 "observed_at이 없다"는
    # 422를 받고, `gate_no`를 `gate`로 적으면 아예 422도 안 나고 게이트 없는 사건이 그대로
    # 쌓인다. 어느 쪽이든 원인을 응답만 보고는 못 짚는다(전체검토 L12).
    #
    # ⚠ 실기기 프레임을 막지 않는지 확인하고 켰다. 라파 `hardware/rpi/gate_server.py`가 내는
    # 세 갈래(`emit_gate_pass`·`emit_tagged_entry`·태깅)와 `tools/dummy_publisher.py`의 세
    # 본문 모두 여기 정의된 칸만 쓴다(`send_payload`가 내부 키 `type`을 빼고 보낸다).
    # 칸을 새로 보내려면 모델에 먼저 더해야 한다 — 그게 이 설정의 값이다.
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=128)
    boot_id: str | None = Field(default=None, min_length=1, max_length=16)

    @field_validator("boot_id")
    @classmethod
    def _normalize_boot_id(cls, v: str | None) -> str | None:
        """빈 값·공백만 있는 값을 막고, 나머지는 소문자로 정규화해 저장한다."""
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("boot_id는 비어 있거나 공백만 있을 수 없습니다.")
        return v.lower()

    @model_validator(mode="after")
    def _derive_boot_id(self):
        from app.config import get_settings  # 순환 import 회피 (모듈 머리 주석 참고)

        parts = parse_event_id(self.event_id)
        if get_settings().event_id_require_boot_id and (parts is None or parts.legacy):
            raise ValueError(
                "event_id에 boot_id가 없습니다. {device_id}-b{boot8}-{unix_ms}-{seq} 형식으로 보내 주세요."
            )
        if self.boot_id is None and parts is not None and parts.boot_id is not None:
            self.boot_id = parts.boot_id
        return self


class TaggingEventIn(EventIdIn):
    # 상한만 둔다 — 선택 칸인 건 그대로다(실기기가 지금 보내는 프레임을 막으면 안 된다).
    device_id: str | None = Field(default=None, max_length=DEVICE_ID_MAX_LEN)
    gate_no: int | None = Field(default=None, ge=GATE_NO_MIN, le=GATE_NO_MAX)
    # [초안·팀 확정 대기] 안건①. 안 보내면 None이라 기존 퍼블리셔가 그대로 통과한다.
    room_id: str | None = Field(default=None, max_length=32)
    tag_id: str = Field(min_length=1, max_length=64)
    observed_at: dt.datetime


class GateSensingIn(BaseModel):
    """게이트 센서를 지금 켰나 껐나. 라파이가 **터치로 바꾸는 순간에만** 한 번씩 보낸다.

    ⚠ **`sensing`은 필수다.** 선택으로 두면 안 실려 온 요청이 "꺼짐"으로 기록돼서, 안 보낸
    기기가 화면에 빨갛게 뜬다 — "모른다"와 "안 잰다"는 화면에서 다른 색이다.

    ⚠ 이벤트가 아니라 **상태**라 `event_id`가 없다. 같은 값이 두 번 와도 덮어쓰면 그만이다.
    """
    device_id: str = Field(min_length=1, max_length=DEVICE_ID_MAX_LEN)
    sensing: bool
    # 있으면 좋고 없어도 된다 — 지금은 안 쓰지만 게이트가 늘면 어느 자리인지 봐야 한다.
    gate_no: int | None = Field(default=None, ge=GATE_NO_MIN, le=GATE_NO_MAX)


class GateSensingAck(BaseModel):
    """받은 뒤 서버가 아는 값. 라파이가 자기가 보낸 것과 맞는지 볼 수 있다."""
    device_id: str
    sensing: bool


class GatePassEventIn(EventIdIn):
    # 상한만 둔다 — 선택 칸인 건 그대로다(`TaggingEventIn`과 같은 폭).
    device_id: str | None = Field(default=None, max_length=DEVICE_ID_MAX_LEN)
    gate_no: int | None = Field(default=None, ge=GATE_NO_MIN, le=GATE_NO_MAX)
    # [초안·팀 확정 대기] 안건①.
    room_id: str | None = Field(default=None, max_length=32)
    # 한쪽 빔만 끊기고 창이 지나면 direction=null·status=incomplete로 그대로 올라온다.
    direction: Direction | None = None
    status: PassStatus | None = None
    beam_a_ts: dt.datetime | None = None
    beam_b_ts: dt.datetime | None = None
    observed_at: dt.datetime
    # 기기가 현장에서 낸 1차 판단(하드웨어 요청 2026-07-28). 값 낱말이 아직 안 정해져서
    # 자유 문자열로 받고 길이만 본다 — 서버가 먼저 enum을 박으면 기기 프레임이 통째로 막힌다.
    # ⚠ 참고값이라 서버 verdict를 대체하지 않고 DB에도 안 남는다. 메시지 device_result로만 흐른다.
    result: str | None = Field(default=None, max_length=32)
    # 기기가 그 통과에서 **실제로 읽은** 카드 UID(F22). 선택 칸이다 — 빔(ToF)은 사람을 못
    # 알아보니 라파 `gate_server.py`의 두 emit 갈래는 아직 이 칸을 안 싣는다. 실려 오면
    # 판정과 무관하게 통과 행에 남아서, 미태깅 경고에서도 `/api/staff/by-tag/{tag_id}`가
    # 그 사건을 짚을 수 있다(안 실려 오면 예전처럼 NULL이라 기존 거동은 안 바뀐다).
    # ⚠ 판정에는 안 쓴다. 크레딧 소비는 그대로 게이트 FIFO다 — 기기가 보낸 UID로 크레딧을
    # 고르면 소비 규칙이 두 벌로 갈린다. 길이 상한은 `TaggingEventIn.tag_id`와 같은 64다.
    tag_id: str | None = Field(default=None, max_length=64)


class ShuttleArrivalIn(EventIdIn):
    # 화면 창구 `ShuttleCallIn.gate_no`와 같은 범위다 — 같은 사실의 계약이 창구마다 갈리면
    # 안 된다. 다만 여기는 기기 인입이라 선택 칸인 건 그대로 둔다(화면 쪽은 필수).
    gate_no: int | None = Field(default=None, ge=GATE_NO_MIN, le=GATE_NO_MAX)
    # [초안·팀 확정 대기] 안건①.
    room_id: str | None = Field(default=None, max_length=32)
    # 값 형식은 팀 결정 대기라 자유 문자열이고 상한만 둔다. 32는 DB 컬럼 폭과 같은 값이다 —
    # 상한이 없으면 33자를 보낸 요청이 검증을 지나 asyncpg StringDataRightTruncation으로
    # 500이 된다(422로 돌려주는 게 맞는 자리다).
    shuttle_no: str | None = Field(default=None, max_length=SHUTTLE_NO_MAX_LEN)
    signal_ts: dt.datetime


# ── 시연 도구 (2026-08-05 프론트 22차) ───────────────────────────────────

class DemoVerdict(str, Enum):
    """시연 도구 버튼 넷. 화면 버튼과 1:1이다.

    ⚠ 이 값은 **"어떤 입력을 만들까"**이지 저장될 판정이 아니다. 실제 판정은 서버가
    내리고 응답 `verdict`로 돌아온다 — 크레딧·쿨다운 상태에 따라 갈릴 수 있다.
    """

    normal = "normal"
    untagged = "untagged"
    beam_incomplete = "beam_incomplete"
    exit = "exit"


class DemoGatePassIn(BaseModel):
    """`POST /api/demo/gate-pass` 요청."""

    model_config = ConfigDict(extra="forbid")

    verdict: DemoVerdict
    gate_no: int | None = Field(default=None, ge=GATE_NO_MIN, le=GATE_NO_MAX)


class DemoGatePassOut(BaseModel):
    """시연 판정 결과.

    ⭐ `requested`와 `verdict`가 **다를 수 있다.** 요청은 "이런 장면을 만들어 달라"이고
    응답은 서버가 실제 판정기로 내린 결과다. 화면은 `verdict`를 그린다.
    """

    requested: DemoVerdict
    verdict: str | None
    event_id: str
    stored: bool
    # `normal`일 때만 채워진다(크레딧을 만들려고 태깅을 먼저 심는다).
    tagging_event_id: str | None = None
    gate_no: int | None = None
    is_demo: bool = True


# ── 인입 응답 ────────────────────────────────────────────────────────────

class IngestAck(BaseModel):
    """멱등 인입 결과. duplicate=True면 이미 있던 이벤트(409가 아니라 200으로 성공 취급)."""
    event_id: str
    stored: bool  # 이번 요청으로 새 행이 생겼나
    duplicate: bool
    # 빔 통과 인입만 채운다(정상/미태깅/빔 미완/퇴장). 태깅·셔틀은 판정이 없어 None.
    verdict: str | None = None


class GatePassIngestAck(IngestAck):
    """빔 통과 인입(`POST /api/gate-pass-events`) 응답. 어긋난 주장 사유 칸이 하나 더 붙는다.

    왜 필요한가 — 1차 수리는 방향 주장과 빔 시각이 어긋나면 판정을 내리면서 사유를
    `PassEvaluation.beam_conflict`에만 담고 아무 데도 안 내보냈다. 주석은 "호출자가 짚는
    자리"라 적혀 있었는데 실제로 읽는 자리가 시험뿐이라, 운영에서 **어느 기기가 이상한
    주장을 보내는지 알 방법이 로그 grep밖에 없었다**(수리 1차 검증 P3).

    ⚠ `IngestAck`에 직접 안 넣고 갈랐다. 넣으면 태깅·셔틀 응답에도 없는 칸이 붙는다
    (`ShuttleIngestAck` 주석의 같은 이유).

    ⚠ 라우터가 `response_model_exclude_none=True`로 내보낸다. 어긋남이 없는 평소 응답은
    칸이 아예 안 붙어서 dev의 ack 계약(`test_verdict_hardening.py`가 dict를 통째로 대조한다)이
    한 글자도 안 바뀐다. WS `gate_pass_event`가 `room_id`를 다루는 방식과 같은 규칙이다 —
    "안 실린 칸은 안 덮는다".
    """

    # 어긋남이 없으면 None → 응답에서 통째로 빠진다. 값 종류는
    # `app/credit/state_machine.py`의 `BEAM_CONFLICT_*` 상수 넷이다.
    beam_conflict: str | None = None


class ShuttleIngestAck(IngestAck):
    """셔틀 기기 인입(`POST /api/shuttle-arrivals`) 응답. 통지 결과 칸이 하나 더 붙는다.

    왜 이 칸이 생겼나 — 같은 사실을 두 창구가 다른 방식으로 내보내고 있었다. 기기 인입은
    응답 **헤더**(X-Shuttle-Notified-Robots)로, 화면 창구 `POST /api/shuttle-calls`는 응답
    **본문**(`ShuttleCallOut.notified_robot_count`)으로 줬다. 응답 스키마에 칸이 없어서
    헤더로 우회한 것이었는데, 계약이 둘이면 읽는 쪽이 창구마다 다른 코드를 들어야 하고
    한쪽만 고쳐지는 드리프트가 난다. 그래서 본문 칸을 올렸다.

    ⚠ 헤더는 그대로 남긴다. 라파(hardware/rpi)가 나중에 읽을 수 있는 자리라 지우는 건 계약
    축소다. 두 창구가 같은 본문 칸을 갖게 하는 것까지가 이번 정리의 몫이다.

    ⚠ 왜 `IngestAck`에 직접 안 넣고 갈랐나 — 넣으면 태깅·빔 통과 응답에도 `null` 칸이 하나
    붙는다. 실제로 `tests/test_verdict_hardening.py`의 중복 통과 시험이 빔 통과 ack를 dict
    통째로 대조해서(`second.json() == {...}`) 그 칸 때문에 깨졌다(실측). 그 파일은 다른 조
    소유라 못 고치고, 애초에 판정만 하는 창구에 통지 수 칸을 다는 건 없는 값을 계약에 올리는
    셈이라 셔틀 응답만 갈랐다. `IngestAck` 자체로 올리려면 그 시험의 기대 dict에 키를 같이
    더해야 한다.

    ⚠ 두 칸 다 **nullable이 아니다.** 1차에는 `notified_robot_count`가 `int | None = None`
    이었는데 화면 쪽 `ShuttleCallOut`은 `int` 필수여서, 같은 사실의 계약이 창구마다 한 칸
    어긋났다(교차 검증 지적). 라우터는 두 창구 다 몸통이 돌려준 값을 늘 채우니(`ingest.py`·
    `routers/query.py`가 `ShuttleRecordResult`에서 그대로 옮긴다) 없을 수 있는 값이 아니다.
    OpenAPI만 보고 짜는 프론트가 기기 창구에만 None 갈래를 들 이유가 없어야 한다.
    계약을 좁혔지만 나가는 JSON은 그대로다 — 값이 원래 항상 int였다(전량 실측으로 확인).
    """

    # 0이면 붙은 로봇이 없어 명령이 못 나갔다는 뜻이다(셔틀 명령 유실은 조용하다).
    notified_robot_count: int
    # 중복 신호인데 아직 못 나간 명령을 다시 냈나(통지 유실 복구, P0-4-c). 몸통
    # `ShuttleRecordResult.resent`를 그대로 옮긴 값이고, 복구가 일어난 사실을 로그·헤더 말고
    # 본문으로도 드러내는 자리다.
    resent: bool = False


# ── 로봇 outbound WS: 상태 수신 ───────────────────────────────────────────
# 필드 집합의 정본은 schemas/robot_state.schema.json이다. 여기는 그 파일의 pydantic 판이고,
# 둘이 어긋나면 JSON Schema 파일을 따른다(프론트 TS 타입도 같은 파일을 본다).
# 로봇 상태는 POST가 아니라 이 채널로만 받는다(아키텍처 2026-07-23 M-4).

class RobotMode(str, Enum):
    OUTDOOR_TAGGING = "OUTDOOR_TAGGING"
    INDOOR_TAGGING = "INDOOR_TAGGING"
    CHARGING_STATION = "CHARGING_STATION"
    MANUAL = "MANUAL"


class CommStatus(str, Enum):
    """3단 연결 상태. 소켓 close가 아니라 하트비트 연속 미스로 판정한다."""
    WS_OK = "WS_OK"
    POLLING_GRACE = "POLLING_GRACE"
    SERVER_DOWN = "SERVER_DOWN"


class MissionStatus(str, Enum):
    ARRIVED = "ARRIVED"
    WEATHER_BLOCKED = "WEATHER_BLOCKED"
    RETURN_COMPLETE = "RETURN_COMPLETE"
    # ⭐ 주행 중 실패(2026-08-05 젯슨 §6-5). 태그를 못 보고 거리 상한을 넘거나 회전이
    # 시간 초과로 끝나면 **로봇은 멈췄는데 서버는 영원히 도착을 기다린다.** 그 갈래를
    # 말할 낱말이 없어서 젯슨이 알릴 방법 자체가 없었다.
    FAILED = "FAILED"
    # ⭐ 이동 중(2026-08-06 · 프론트 38차 §1-1 · 젯슨 회신 §7). **양쪽이 같이 요청했다.**
    #
    # ⛔ 이게 없던 동안 무슨 일이 났나 — 나머지 넷은 전부 "끝났다" 계열이라 **출발을 담을
    # 낱말이 없었다.** 그래서 도착해서 찍힌 ARRIVED가 다음 도착까지 안 지워졌고, 화면이 그
    # 값으로 태깅·ToF 판정을 켜서 **복귀 중에도 태깅이 켜져 있고 주행 중에도 ToF가 켜져
    # 있었다**(사용자가 실기기 시연에서 짚은 자리다).
    #
    # ⚠ **이 값은 알림을 안 만든다**(`robot_channel._NO_NOTICE_STATUSES`). 나머지 넷은 사건인데
    # 이건 진행 중 상태라서다. 출발 알림은 `ARRIVED에서 벗어나는 전이` 쪽이 이미 내고 있다.
    # ⚠ 출발 **소리**도 여기서 안 낸다 — 명령을 실제로 보낸 순간에 이미 나간다(`dispatch`).
    EN_ROUTE = "EN_ROUTE"


class RobotPosition(BaseModel):
    """평면도 SVG 좌표. GPS가 아니다."""
    x: float | None = None
    y: float | None = None
    theta: float | None = None


class RobotStatusSummary(BaseModel):
    battery: int | None = Field(default=None, ge=0, le=100)
    mode: RobotMode | None = None
    comm_status: CommStatus | None = None
    position: RobotPosition | None = None
    # 정본 robot_state.schema.json에 길이 제한이 없다. 서버가 32자로 자르면 긴 LED 문구 하나
    # 때문에 상태 프레임 전체가 검증에 걸려 배터리·모드까지 통째로 버려진다.
    led_status: str | None = None


class TagTargetIn(BaseModel):
    """젯슨이 올리는 **에이프릴태그 검출값**. 로봇 상태가 아니라 **지도 위치 보정값**이다.

    ⭐ **왜 따로 받나** — 로봇 좌표(`position.x`·`y`)는 **로봇을 켠 그 자리가 원점**이라 껐다
    켤 때마다 원점이 바뀐다. 그래서 평면도에 그리려면 "늘 도킹 자리에 같은 방향으로 놓고 켠다"를
    전제로 깔아야 하고, 시연에서 로봇을 조금만 다른 데 놓으면 화면이 어긋난다. 태그는 자리가
    고정이라 **절대 기준점**이다 — "3번 태그가 0.82m 앞에 보인다"만 오면 어디서 켰든 정확한
    자리에 그릴 수 있다.

    ⚠ **칸 이름은 젯슨이 정본이다**(`hardware/jetson/.../web_bridge/messages.py`
    `tag_target_message`). 프론트가 "봉투 이름과 칸 이름은 젯슨 쪽에 맞추겠다"고 해서 서버가
    가운데서 이름을 바꾸지 않는다 — 바꾸면 계약이 세 벌로 갈린다.

    ⚠ `valid=False`면 검출이 실패한 보고다. 그때 세 값은 `None`이고 `reason`에 사유가 온다.
    화면이 "태그를 놓쳤다"를 그리는 근거라 **버리지 않고 그대로 흘린다.**

    ⚠ **DB에 안 쌓는다.** 초당 5장이 오는 값이고 화면이 지금 자리를 그리는 데만 쓴다.
    쌓으려면 표를 늘려야 해서 사용자 결정 자리다.

    ⛔ **`extra="forbid"`라 젯슨이 새 칸을 더하면 프레임이 통째로 거절된다.** 여기만 그렇고
    `RobotStatusSummary`·`RobotPosition`은 모르는 칸을 조용히 무시한다. 2026-08-07에 젯슨이
    `front_overhang_m`을 더했다고 알려 와서 열었다 — **알려 오기 전에 붙었으면 초당 다섯 번
    `invalid_tag_target`이 나가고 화면이 로봇을 절대 좌표로 못 그렸다.** 같은 모양의 사고가
    `unknown_frame_type`으로 한 번 있었다(`robot_channel.py`의 `tag_target` 갈래 주석).
    **젯슨이 이 봉투에 칸을 더한다고 하면 여기부터 열어라.**
    """

    model_config = ConfigDict(extra="forbid")

    tag_id: int | str
    along: float | None = None
    cross_track: float | None = None
    heading_error: float | None = None
    valid: bool = True
    reason: str | None = Field(default=None, max_length=120)
    # ⭐ 태그 거리를 어느 면에서 쟀나(로봇 앞머리가 뒷차축보다 얼마나 나와 있나). 젯슨이
    # `/route/state`에서 캐시해 싣는다(2026-08-07 회신 §4).
    # ⚠ **`None`을 그대로 둔다** — 젯슨이 `route_runner`가 없으면 칸 자체를 뺀다. `0.0`으로
    # 채우면 "앞머리 기준으로 쟀는데 돌출이 0"과 "기준면을 모른다"가 같은 값이 된다.
    front_overhang_m: float | None = None


# `RobotStateIn`의 자유 dict 세 칸(odom·imu·sensor_health) 한 칸당 크기 상한. 넘치면 그 칸만
# 표식으로 바뀐다(`RobotStateIn._cap_free_dict`가 까닭을 들고 있다). 익명 WS가 JSONB 표를
# 부풀리는 길을 막는 자리라 "정상값의 몇 배"가 아니라 **두 자릿수 위**로 잡았다.
_ROBOT_FREE_DICT_MAX_BYTES = 32 * 1024


class RobotStateIn(BaseModel):
    """로봇이 outbound WS 메시지의 `data` 자리에 실어 올리는 상태.

    내용물 칸은 두 전선 모두 `data`다(2026-08-04 통일 · `robot_channel.py`가 `frame["data"]`를
    읽고, 대시보드로 중계할 때도 같은 이름이다).
    """

    # 모르는 칸은 프레임을 거절한다(전체검토 L12). 예전에는
    # `{"robot_id":"ROBOT-1","batery":42,"mision_status":"ARRIVED"}`이 통과해서 robot_id 빼고
    # 전부 None인 상태가 저장되고 서버는 `state_ack`를 정상으로 돌려줬다 — 오타를 알아챌
    # 자리가 어디에도 없었다. 이제 `invalid_robot_state` error 봉투가 나가서 로봇 쪽에 보인다.
    #
    # ⚠ 젯슨 실경로는 안 막힌다. `jetson_adapter._merged_payload`가 여기 있는 칸
    # (robot_id·mission_status·status_summary·sensor_health·odom·imu)만 넣어 만든 dict를
    # 넘기고, 계약 밖 값은 그 앞에서 이미 걸러져 `sensor_health`로 흘러간다.
    #
    # ⚠ 안쪽 `RobotStatusSummary`·`RobotPosition`은 그대로 ignore다. 거기까지 좁히면
    # `status_summary` 한 칸의 오타가 배터리·위치까지 실린 프레임을 통째로 버린다 —
    # 문서가 센 네 모델(인입 3종 + 이 모델)이 이번 몫이다.
    model_config = ConfigDict(extra="forbid")

    robot_id: str = Field(min_length=1, max_length=64)
    status_summary: RobotStatusSummary | None = None
    odom: dict | None = None          # P1 · 1Hz 다운샘플
    imu: dict | None = None           # P1 · 1Hz 다운샘플
    sensor_health: dict | None = None
    log: str | None = None
    mission_status: MissionStatus | None = None

    @field_validator("odom", "imu", "sensor_health")
    @classmethod
    def _cap_free_dict(cls, v: dict | None, info) -> dict | None:
        """자유 dict 세 칸의 크기 상한. 넘치면 **그 칸만** 표식으로 갈아 끼운다.

        ⛔ 왜 필요한가 — 이 셋은 상한 없는 `dict`인데 `robot_channel._status_log_row`가 그대로
        `RobotStatusLog`의 JSONB 컬럼에 넣는다(models.py). 그리고 `/ws/robot`은 인증도 Origin
        검사도 기본으로 꺼져 있어서(main.py:339~351 · config.py:37) **아무나 붙어 한 프레임에
        수 MB짜리 dict를 실을 수 있다.** nginx `client_max_body_size`는 HTTP 본문에만 걸리고 WS
        프레임엔 안 걸린다. 그 표는 실기기 주행에서 초당 6.2행으로 자란다고 이미 실측돼 있어
        (models.py:73~78), 한 프레임당 몇 MB면 디스크가 순식간에 찬다.

        ⚠ **거절이 아니라 잘라내기다.** 검증 실패로 던지면 `robot_channel`이 프레임을 통째로
        버려서 배터리·위치까지 같이 사라진다 — 시연 중에 화면이 멈춘다. 값이 큰 그 칸만 버리고
        나머지는 살린다. `command_log._clip`이 이미 쓰는 수법과 같은 결이다.

        ⚠ 상한을 32KB로 **넉넉히** 잡은 이유는 실기기를 막지 않기 위해서다. 젯슨이 싣는 값은
        `jetson_adapter._merged_payload`가 만드는 납작한 dict(좌표·자세·센서 플래그)라 수백
        바이트 수준이고, 32KB는 그보다 두 자릿수 위다. 정상 프레임은 이 자리를 못 밟는다.
        """
        if v is None:
            return v
        try:
            size = len(json.dumps(v, ensure_ascii=False, default=str).encode("utf-8"))
        except (TypeError, ValueError):
            # 잴 수 없으면 건드리지 않는다. 여기서 던지면 프레임이 통째로 죽는데, 그건 이
            # 가드가 막으려는 것보다 큰 손해다(어차피 JSONB 적재에서 드러난다).
            return v
        if size <= _ROBOT_FREE_DICT_MAX_BYTES:
            return v
        return {"_truncated": True, "_bytes": size, "_field": info.field_name}


# ── 로봇 outbound WS: 명령 발신 ───────────────────────────────────────────
# 정본은 schemas/command.schema.json. 상대 변화가 아니라 절대 상태로 지시해
# 재전송·순서 역전을 흡수한다. 명령 실행 로직은 로봇 몫이다.

class CommandType(str, Enum):
    # ⛔ **`reset_fault`가 없다 — 비상정지를 관제 화면으로 못 푼다**(2026-08-09 백지검토, 코드
    #   대조 판정 · 실기기 미시연). 긴급정지는 세울 때도 풀 때도 `emergency_stop`으로 나가고
    #   젯슨이 그것을 `/route/abort`에 물려 `route_runner`를 FAULT로 latch한다
    #   (`web_bridge_node.py:172~176` `_route_clients` 키는 start·return·abort 셋뿐).
    #   그 latch를 푸는 길은 `/route/reset_fault` 하나인데(`route_runner.py:1405~1432`) 여기에도
    #   젯슨 매핑에도 그 이름이 없다. FAULT면 `route_logic._preflight`가 start를 거절한다.
    #
    #   ⚠ **일부러 안 고쳤다.** 제대로 하려면 이 enum + `web_bridge_node._route_clients` +
    #   명령을 내는 서버 자리 + 화면 버튼 넷을 같이 고치고 **젯슨을 재배포**해야 한다. 여기만
    #   값을 늘리면 아무도 안 쓰는 죽은 계약이고, 서버가 먼저 새 이름을 보내면 지금 실기기에
    #   깔린 젯슨이 그 키를 못 찾는다. 발표(2026-08-10) 전 우회는 젯슨에서
    #   `ros2 service call /route/reset_fault std_srvs/srv/Trigger` 한 줄이다.
    cmd_destination = "cmd_destination"
    weather_status = "weather_status"
    control_mode = "control_mode"
    cmd_manual = "cmd_manual"
    return_to_charge = "return_to_charge"
    emergency_stop = "emergency_stop"


class Destination(str, Enum):
    INDOOR_TAGGING = "INDOOR_TAGGING"
    OUTDOOR_TAGGING = "OUTDOOR_TAGGING"
    CHARGING_STATION = "CHARGING_STATION"


class CommandOut(BaseModel):
    """서버 → 로봇 명령. command_id는 ACK 대조용, ttl_ms는 유효 시간.

    칸은 정본 `schemas/command.schema.json`을 따른다. 지금 서버가 실제로 내는 명령은
    `cmd_destination`·`return_to_charge`·`emergency_stop` 셋이라 그 셋의 칸만 들고 있다
    (`weather_status`·`control_mode`·`cmd_manual`은 계약에만 있고 내는 자리가 없다).
    보낼 때는 `exclude_none=True`로 떠서 안 쓴 칸이 프레임에 안 실린다 — 정본이
    `additionalProperties:false`라 빈 칸을 실어 보내면 로봇 쪽 검증에 걸린다.
    그 규칙을 `as_frame()` 한 자리에 뒀다(부르는 쪽이 매번 기억할 일이 아니다).
    """
    command_id: str
    type: CommandType
    ttl_ms: int | None = None
    cmd_destination: Destination | None = None
    return_to_charge: bool | None = None
    emergency_stop: bool | None = None

    def as_frame(self) -> dict:
        """전선에 실을 모양. **안 쓴 칸은 빼고** 뜬다(위 docstring의 계약)."""
        return self.model_dump(mode="json", exclude_none=True)


# ── 조회 응답 ────────────────────────────────────────────────────────────
# 실데이터 칸은 전부 이미 있는 DB 컬럼을 그대로 노출한다 — 새 수집 항목은 안 만든다.
# 로봇 위치·LED는 예외다. 컬럼을 만들지 않고(마이그레이션 무변경) 로봇이 보고한 값을
# 서버 메모리에서 그대로 얹어 내보낸다 — 지어내는 값이 아니라 인바운드로 받은 값이다.
#
# 다만 "아키텍처 2026-07-23 §05 표 컬럼만"은 아니다. 표 밖인 칸이 이만큼 있다.
#  - updated_at(Robot)·created_at(Alert) — models.py엔 있는데 §05 표엔 안 적힌 컬럼.
#  - server_time·ws_protocol_version·counts — DB 컬럼이 아니라 응답에서 계산하는 값.
# 표를 정본으로 맞출지 이 응답을 정본으로 맞출지는 팀 확정 대기다.

class RobotOut(BaseModel):
    id: int
    name: str
    mode: str | None = None
    battery: int | None = None
    network_status: str | None = None
    # 이 값이 얼마나 오래됐나를 화면이 판단할 수 있게 같이 준다(로봇 카드 "N초 전").
    updated_at: dt.datetime | None = None
    # 아래 셋은 DB 컬럼이 아니라 서버 메모리에 든 최신값이다(robot_channel.RobotPresentation).
    # 보고가 없었거나 서버가 재기동됐으면 null이라 기존 화면 계약은 그대로다.
    position: RobotPosition | None = None
    led_status: str | None = None
    # 로봇이 마지막으로 보고한 임무 상태(S15P11C207 프론트 요구 N10). 지금까지 이 값은 WS
    # `robot_state`로만 흘러서, 화면을 새로 고치면 개요 탭 "로봇 상태" 타일이 비었다.
    # ⚠ 여기 실리는 값은 "마지막으로 보고된 상태"이지 알림을 내는 에지가 아니다. 에지 추적
    # 상태(`robot_channel._mission_states`)와는 수명이 달라서 일부러 갈라 뒀다 — 자세한 건
    # RobotPresentation.mission_status 주석에 있다.
    mission_status: MissionStatus | None = None


class EventOut(BaseModel):
    """인입 3계열 통합 이벤트 한 줄. 계열마다 안 쓰는 칸은 null로 온다."""
    kind: str  # tagging / gate_pass / shuttle_arrival
    event_id: str
    gate_no: int | None = None
    observed_at: dt.datetime | None = None
    received_at: dt.datetime | None = None
    # 계열 테이블의 행 PK. AlertOut.source_id가 이 값이라 (kind, id)로 발화 이벤트를
    # 되짚는다(S15P11C207-192). 계열마다 시퀀스가 따로라 kind와 짝으로만 유일하다.
    id: int
    # tagging은 찍은 카드 UID, gate_pass는 **그 통과가 태운 크레딧의** 카드 UID다
    # (S15P11C207-241). 크레딧을 안 태운 판정(미태깅·퇴장·보류)과 셔틀 계열은 null이다.
    # ⚠ gate_pass 쪽 값은 "누가 지나갔나"의 증거가 아니다 — 크레딧은 게이트 FIFO로 고르므로
    # 줄이 엉키면 이 이름도 같이 엉킨다. 빔은 사람을 못 알아본다.
    tag_id: str | None = None
    direction: str | None = None    # gate_pass 전용 (A_TO_B / B_TO_A)
    verdict: str | None = None      # gate_pass 전용 (normal / untagged / beam_incomplete / exit)
    shuttle_no: str | None = None   # shuttle 전용
    # ⭐ 카드 주인. `tag_id`를 명부에서 찾아 채운다. 명부에 없으면 둘 다 null이고 화면이
    # "명부 밖 카드"로 그린다(프론트 11차 §4-2, 사용자가 근본안으로 확정).
    #
    # **왜 봉투에 싣나** — 예전에는 화면이 로그인 직후 `GET /api/staff`를 통째로 받아 이름을
    # 붙였다. 그래서 ①명부 탭을 안 열어도 조회 이력에 `action:"list"`가 쌓여 감사 표가 뜻을
    # 잃었고 ②명부 목록은 요원에게 403이라 **정작 이름이 제일 필요한 급이 못 봤다.**
    #
    # ⚠ **학번을 싣는 것은 2026-08-05 사용자 확정이다.** 팀장이 "계정 비밀번호가 `c207`+학번이라
    # 자격 구성요소"라는 이유로 한 번 뺐는데, 사용자가 **"이건 상관없어 비밀번호라고 생각하지마"**
    # 로 그 전제를 접었다. 되돌리려면 사용자 결정이 먼저다.
    #
    # ⚠ 노출 범위는 안 늘어난다 — 요원 세션은 이미 통과 이벤트를 본다. 명부 줄에 붙는 값이지
    # 새 창구가 아니다.
    staff_name: str | None = None
    staff_student_no: str | None = None


# 사후 신원 확인 입력 상한. 자유 문자열이라 길이와 문자 종류로만 막는다.
# 64자는 "이름 + 소속" 정도가 들어가는 폭이고, 문장을 적어 넣는 걸 막는 선이기도 하다.
IDENTITY_MAX_LEN = 64
IDENTITY_NOTE_MAX_LEN = 200
# 학번 상한. ⚠ **`models.Staff.student_no`·`Alert.identified_student_no`와 같은 폭이다** —
# 한쪽이 좁으면 명부에 있는 값을 신원 칸이 못 받아, 나중에 둘을 맞대는 조회가 조용히 빈다.
STUDENT_NO_MAX_LEN = 32

# 신원 입력에 못 들어가는 문자. 표기를 강제하진 않지만 이 부류는 값이 아니라 "구조"라서 막는다.
#  - 개행이 통과하면 매터모스트 보고 카드에 줄을 끼워 넣을 수 있다. 카드가 `\n`으로 이어붙인
#    한 덩어리라, 신원 한 칸에 개행 + "- 게이트: 99번"을 넣으면 위조된 줄이 진짜 줄 사이에
#    그대로 붙고 @channel 멘션까지 나간다(감사 보고선 위조).
#  - NUL(\x00)은 Postgres text가 아예 못 받아서 asyncpg가 CharacterNotInRepertoireError를
#    던지고 500이 난다. 422로 돌려주는 게 맞는 자리다.
#  - 양방향 재정렬(U+202A~U+202E)·격리(U+2066~U+2069)·제로폭 문자는 화면에 보이는 글자와
#    저장된 글자가 갈라지게 만든다. 감사 기록에 그런 값이 남으면 눈으로 대조할 수가 없다.
IDENTITY_FORBIDDEN_RE = re.compile(
    "["
    "\x00-\x1f\x7f"          # C0 제어문자(개행·탭·NUL 포함) + DEL
    "\x80-\x9f"              # C1 제어문자
    "\u200b-\u200f"        # 제로폭·방향 표시
    "\u2028\u2029"         # 줄 구분자·문단 구분자
    "\u202a-\u202e"        # 양방향 재정렬
    "\u2066-\u2069"        # 양방향 격리
    "\ufeff"                 # BOM
    "]"
)


class AlertOut(BaseModel):
    """경고 조회 응답.

    ⚠ 신원 칸은 "적혔나·언제 적혔나"까지만 싣는다. 원문(identified_person)·메모(identify_note)는
    물론이고 확인한 요원(identified_by)도 안 싣는다. 이 스키마를 쓰는 GET /api/alerts·스냅샷은
    `AUTH_REQUIRE_LOGIN`이 꺼진 동안 인증이 없어서(query.py 머리 주석) 실으면 누구나 읽는다.
    켠 뒤에도 안 싣는다 — 요원 급이면 다 읽는 목록이라 노출 폭이 여전히 넓다. identified_by는 우리 쪽 사람
    이름이라 붙잡힌 사람보다 덜 민감해 보이지만, identified_at과 짝을 이루면 "누가 몇 시에
    근무했나"가 인터넷에 그대로 나간다.

    신원 원문·요원 이름이 필요한 자리는 인증이 걸린 GET /api/alerts/{id}/identify다
    (AlertIdentityOut). 요원 명부가 생기기 전까지 이 갈래를 유지한다.
    """

    id: int
    type: str
    severity: str | None = None
    source_type: str | None = None
    source_id: int | None = None
    ack: bool
    created_at: dt.datetime | None = None
    identified: bool = False               # 신원이 적혔나
    identified_at: dt.datetime | None = None

    # ── 사건 수명주기 (0005) ────────────────────────────────────────────
    # ⚠ ack와 status는 **서로 다른 축이다.** 파생 관계가 아니다 — 여기 예전 주석이
    #    "ack == status != OPEN"을 불변식이라 적어 뒀는데 그건 틀린 문장이었다.
    #    정본은 app/alert_lifecycle.py(모듈 머리 "ack와 이 축의 관계 — 묶지 않는다")다.
    #    - status는 "어디까지 진행됐나"를 가리키는 전진 전용 순위 축이다
    #      (OPEN < ACKED < IDENTIFIED < RESOLVED).
    #    - ack는 "관제 화면에서 확인을 눌렀나" 하나만 가리킨다. 신원 입력은 ack를 안 건드리고,
    #      RESOLVED로 건너뛴 전이도 ack를 안 찍는다(정본 전이 규칙 3번).
    #    그래서 `ack=false`인데 `IDENTIFIED`인 행은 결함이 아니라 **정의된 값이다** — 관제는
    #    아직 안 눌렀고 안쪽 요원이 신원만 적은 단계다. 실서버 경고 id 18이 실제로 그 모양이고,
    #    마이그레이션 0005는 ack를 한 행도 안 고친다(예전 조작 기록을 사후에 고쳐 쓰지 않는다).
    #    프론트는 단계 표시는 status로 읽고, "관제가 봤나"는 ack를 따로 읽어라. 한쪽에서
    #    다른 쪽을 유도하면 안 눌린 확인이 눌린 것으로 그려진다.
    # ⚠ acked_by·resolved_by는 일부러 안 싣는다. 요원 이름이라 identified_by와 같은 이유다
    #    (이 스키마를 쓰는 목록·스냅샷은 플래그가 꺼진 동안 인증이 없고, 켠 뒤에도 요원 급이면
    #    전부 읽는 자리라 노출 폭이 넓다).
    status: AlertStatus = AlertStatus.OPEN
    acked_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None
    resolution: AlertResolution | None = None


class AlertIdentityOut(BaseModel):
    """신원 원문 조회 응답 — 인증이 걸린 창구에서만 나간다.

    ⚠ **어떤 인증인지가 2026-08-02에 바뀌었다**(2026-08-06 실측 정정). 예전에는 여기에
    "인증(X-API-Key)"이라고 적혀 있었는데, 로그인이 켜진 지금은 `key_gate_or`가 세션
    게이트만 태우고 **X-API-Key는 아예 안 본다** — 기기 키 폴백을 켠 창구(라파이 복귀)만
    예외다. 그래서 컨테이너 안에서 키를 들고 불러도 401이고, 그게 정상이다.

    이 창구가 없으면 신원이 write-only가 된다. 매터모스트 보고 웹훅이 꺼진 배포에서는
    적어 넣은 값을 읽을 수 있는 사람이 아무도 없는 채로 개인정보만 쌓인다(운영 실측).
    409를 맞은 요원이 "이미 뭐라고 적혀 있나"를 보고 덮어쓸지 판단하는 자리이기도 하다.

    ⚠ **칸 이름이 입력(`AlertIdentifyIn`)과 다르다.** 입력은 `person`·`note`인데 여기는
    `identified_person`·`identify_note`다 — 모델 칸 이름을 그대로 쓰기 때문이다. 2026-08-06에
    프론트에 입력 이름으로 잘못 알렸다가 정정했다(프론트 15차 §7). 새 칸을 붙일 때도 이
    규칙을 따른다.

    ⚠ **`corrected`는 없다.** "고쳐 적은 적이 있나"는 쓰기 시점에만 아는 값이라 DB에 안 남는다.
    그 값이 필요하면 `alert_identified` WS 봉투를 봐야 한다.
    """

    alert_id: int
    identified: bool
    identified_person: str | None = None
    # ⭐ 학번(2026-08-06). 입력 칸 이름은 `student_no`다 — 위 "칸 이름이 입력과 다르다" 참고.
    identified_student_no: str | None = None
    identified_by: str | None = None
    identified_at: dt.datetime | None = None
    identify_note: str | None = None
    # ⭐ 서버가 명부 행에 실제로 이었나(2026-08-06 · 프론트 31차 §4-2).
    #
    # ⚠ **화면이 자기 명부 사본으로 판단하면 서버와 다른 말을 한다.** 같은 학번이 명부에
    # 여럿이면 서버는 일부러 안 잇는데(`_link_staff_by_student_no`), 화면은 그 사실을
    # 알 길이 없어 아무나 하나를 골라 이름을 말할 수 있다. 그 어긋남을 막는 칸이다.
    #
    # ⚠ 이름·행 번호는 안 싣는다. 화면이 필요하다고 한 것은 "이었나" 하나다. 명부 내용을
    # 여기 얹으면 이 창구가 학번에서 이름을 뽑는 두 번째 길이 된다(`by-student-no`와 같은 잣대).
    #
    # 학번이 비면 이을 대상이 없으니 false다 — 화면은 학번 유무를 아니까 둘을 조합해
    # "학번은 있는데 안 이어졌다(= 명부에 없거나 여럿이다)"를 가려낼 수 있다.
    staff_linked: bool = False


def _clean_identity_text(v: object) -> object:
    """신원 입력 공통 전처리 — 앞뒤 공백을 털고 구조 문자를 거절한다.

    mode="before"로 도는 이유가 둘이다.
    - 길이 검사(max_length)가 털기 **뒤에** 걸려야 한다. 앞에 걸리면 붙여넣기로 딸려온 공백
      60칸 때문에 실사용 값 5자가 422로 막힌다.
    - 제어문자는 DB에 닿기 전에 걸러야 한다. NUL은 Postgres text가 못 받아서 asyncpg
      예외가 500으로 그대로 올라온다.
    """
    if not isinstance(v, str):
        return v
    v = v.strip()
    if IDENTITY_FORBIDDEN_RE.search(v):
        raise ValueError(
            "줄바꿈·제어문자·서식 제어문자는 넣을 수 없습니다. 한 줄로 적어 주세요."
        )
    return v


class AlertIdentifyIn(BaseModel):
    """미태깅 경고 하나에 사후 확인된 신원을 붙이는 요청.

    ⚠ person에 실명·학번 같은 걸 넣을지는 팀 결정이다. 시연에서는 가명("가명-1")을 권한다.
    서버는 표기를 안 따진다 — 현장에서 요원이 손으로 적는 값이라 형식을 강제하면 입력이
    막힌다. 자르는 건 길이와 "구조 문자"(개행·제어문자·양방향 재정렬)까지다.

    identified_at은 요청으로 안 받는다. 감사 기록의 시각을 부르는 쪽이 정하면 못 믿는다.

    안 보낸 칸과 빈 값으로 보낸 칸은 다르다. overwrite로 고쳐 적을 때 안 보낸 칸은 앞
    값을 그대로 두고, 빈 값으로 보낸 칸만 지운다(model_fields_set으로 가른다 — 라우터 참고).
    """

    person: str = Field(min_length=1, max_length=IDENTITY_MAX_LEN)
    # ⭐ 학번(2026-08-06 사용자 확정 · 프론트 27차). 이름과 나눠 받는다 — 이름은 동명이인이
    # 있고 명부 대조도 학번으로 한다. 그전까지 화면이 `person` 한 칸에 "이름 · 학번"을 이어
    # 보내고 **숫자를 되뽑아** 썼는데, 이름에 숫자가 섞이면 흔들리는 임시방편이었다.
    #
    # ⚠ **필수로 안 만든다.** 현장에서 이름만 알아낸 갈래를 막으면 입력이 통째로 멈춘다.
    # ⚠ **형식도 안 따진다.** 위 클래스 머리의 "형식을 강제하면 입력이 막힌다"와 같은 잣대다 —
    #   학번 규칙이 학교마다 다르고 요원이 손으로 적는 값이다. 자르는 건 길이와 구조 문자까지다.
    student_no: str | None = Field(default=None, max_length=STUDENT_NO_MAX_LEN)
    identified_by: str | None = Field(default=None, max_length=IDENTITY_MAX_LEN)
    note: str | None = Field(default=None, max_length=IDENTITY_NOTE_MAX_LEN)
    # 이미 신원이 적힌 경고를 고쳐 적을 때만 true. 기본은 409로 막는다(router 주석 참고).
    overwrite: bool = False

    @field_validator("person", "student_no", "identified_by", "note", mode="before")
    @classmethod
    def _strip_and_reject_structure(cls, v: object) -> object:
        return _clean_identity_text(v)

    @field_validator("student_no", "identified_by", "note")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        """옵션 칸의 빈 값·공백은 None으로 본다.

        화면 입력 상자를 비워 두면 ""가 그대로 올라온다. 그걸 422로 막으면 "안 적어도 된다"는
        계약이 거짓말이 되므로, 여기서 안 적은 것과 같게 만든다.
        """
        return v or None


class AlertStatusIn(BaseModel):
    """경고 하나를 다음 단계로 전진시키는 요청.

    전이 규칙과 그 근거는 app/alert_lifecycle.py 모듈 머리에 있다. 요약하면 축이 하나고
    전진만 되고, RESOLVED로 갈 때 사유가 필수다.

    `to`에 IDENTIFIED를 넣으면 422다(enum 밖). 신원 원문 없이 그 상태로 올리면 감사 기록이
    빈 채로 종결 후보가 되므로, 그 상태는 신원 창구만 올린다.

    ⚠ actor는 **`AUTH_REQUIRE_LOGIN`이 꺼진 구간에서만** 자칭 문자열이다. 그때는 공용
    X-API-Key라 서버가 "정말 그 사람인가"를 못 판정해서, "화면에서 누가 눌렀다고 적었나"
    까지가 뜻이다. 켠 구간에서는 이 칸을 **본문에서 안 읽고 세션 실명으로 덮는다**
    (`app/routers/query.py`). 칸은 스키마에서 안 지운다 — 지우면 그 칸을 싣던 옛
    클라이언트가 422를 맞는다.
    """

    # enum을 DIRECT_TRANSITIONS로 좁힌 리터럴 대신 AlertStatus를 쓰고 라우터에서 걸러도
    # 되는데, 스키마에서 자르면 생성된 OpenAPI 명세가 "여기 올 수 있는 값"을 정확히 담는다.
    # ⚠ 그 명세를 HTTP로 볼 창구는 없다(2026-08-02에 /docs·/redoc·/openapi.json을 껐다).
    # 읽어야 하면 `app.openapi()`를 직접 부른다.
    to: AlertStatus = Field(description="올릴 상태. ACKED 또는 RESOLVED.")
    actor: str | None = Field(default=None, max_length=IDENTITY_MAX_LEN)
    resolution: AlertResolution | None = Field(
        default=None, description="RESOLVED로 갈 때 필수. 왜 닫았나."
    )

    @field_validator("actor", mode="before")
    @classmethod
    def _strip_and_reject_structure(cls, v: object) -> object:
        return _clean_identity_text(v)

    @field_validator("actor")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v or None

    @model_validator(mode="after")
    def _check_transition(self):
        if self.to.value not in DIRECT_TRANSITIONS:
            raise ValueError(
                f"이 창구로는 {', '.join(DIRECT_TRANSITIONS)}만 올릴 수 있습니다."
            )
        if self.to is AlertStatus.RESOLVED and self.resolution is None:
            raise ValueError("종결하려면 resolution(종결 사유)을 함께 보내 주세요.")
        if self.to is not AlertStatus.RESOLVED and self.resolution is not None:
            raise ValueError("resolution은 종결(RESOLVED)일 때만 보낼 수 있습니다.")
        return self


# ── 화면 셔틀 가상 호출 (기기 없이 버튼으로 각본 ①을 시작하는 창구) ─────────

class ShuttleCallIn(BaseModel):
    """관제 화면이 누르는 셔틀 가상 호출 요청.

    셔틀은 실기기를 안 만들기로 확정됐다(화면 버튼으로 가상 호출). 그런데 기기용 인입
    `POST /api/shuttle-arrivals`는 X-API-Key를 요구해서, 화면이 그 키를 들면 브라우저에
    키가 그대로 노출된다 — 그 키 하나로 태깅·통과 인입과 신원 입력까지 다 열린다.
    그래서 셔틀 하나만 키 없이 부를 수 있는 별 창구를 두고, 서버가 자기 몫으로 적재한다.

    화면이 못 정하는 값은 받지 않는다.
    - `event_id` — 서버가 만든다. 화면이 정하면 같은 값을 다시 보내 남의 신호를 덮거나,
      멱등 키를 골라 적재를 조용히 막을 수 있다.
    - `signal_ts` — 서버 시각을 쓴다. 감사 축의 시각을 부르는 쪽이 정하면 못 믿는다
      (신원 입력의 identified_at과 같은 규칙).
    """

    gate_no: int = Field(
        ge=GATE_NO_MIN,
        le=GATE_NO_MAX,
        description="셔틀이 도착한 게이트. 화면 버튼이 어느 게이트인지 반드시 정한다.",
    )
    # 값 형식은 팀 결정 대기라 자유 문자열이고 상한만 둔다(기기 인입과 같은 상한).
    shuttle_no: str | None = Field(default=None, max_length=SHUTTLE_NO_MAX_LEN)
    # [초안·팀 확정 대기] 안건①. 기기 인입과 같은 폭.
    room_id: str | None = Field(default=None, max_length=32)


class ShuttleCallOut(BaseModel):
    """가상 호출 결과. 화면이 "정말 로봇에 갔나"까지 볼 수 있게 배달 결과를 같이 싣는다.

    notified_robot_count가 0이면 붙어 있는 로봇이 없어 명령이 못 갔다는 뜻이다 — 이 값이
    없으면 화면은 200만 보고 "출동했다"고 그린다(셔틀 명령 유실은 조용하다).

    ⚠ **대기 창이 열리는 새 행 갈래에서는 이 값이 늘 0이다**(2026-07-31 셔틀출동 설계
    §2.2). 명령이 5초 뒤에 나가기 때문이지 배달에 실패한 게 아니다. "로봇이 받았나"는 이
    응답이 아니라 WS `shuttle_dispatch_result`의 같은 칸으로 판단해라. 0이 배달 실패를
    뜻하는 건 창을 안 여는 갈래(`SHUTTLE_DISPATCH_HOLD_SEC` 0 이하·재전송 복구)뿐이다.

    칸 이름·nullable 여부를 기기 창구 `ShuttleIngestAck`와 같게 맞춘다. 같은 사실을 두 창구가
    다른 계약으로 주면 읽는 쪽이 창구마다 다른 코드를 들고, 한쪽만 고쳐지는 드리프트가 난다.
    """

    event_id: str
    stored: bool                     # 이번 호출로 새 신호 행이 생겼나
    gate_no: int
    shuttle_no: str | None = None
    signal_ts: dt.datetime
    notified_robot_count: int
    # ⚠ 배달 증거가 **아니다.** "이 도착 신호에 매인 명령 이름"이고, 붙은 로봇이 0대여도
    #    채워진다(`robot_channel.notify_shuttle_arrival`이 보내기 전에 정한다). 배달 여부를
    #    가리키는 칸은 위 `notified_robot_count` 하나다.
    #    값이 `shuttle-{arrival_id}`로 결정적인 게 계약이다 — 통지가 못 나갔던 신호를 나중에
    #    재전송할 때 같은 이름으로 다시 내야 로봇 쪽에서 같은 명령으로 접힌다
    #    (`shuttle_call.record_shuttle_arrival` docstring). 그래서 0대일 때 비우면 안 된다.
    #    부르는 쪽이 그 이름으로 뒤에 올 ARRIVED를 되짚을 수 있어야 한다.
    # ⚠ `None`은 "배달 못 했다"가 아니라 **"명령을 매길 도착 행 자체를 못 찾았다"**는 뜻이다
    #    (`shuttle_call._recover_duplicate`의 경합 갈래). 0대와 None은 서로 다른 사실이라
    #    0대일 때 None으로 눌러 담으면 두 사실이 한 값으로 접힌다.
    command_id: str | None = None
    # 기기 창구와 같은 칸(ShuttleIngestAck.resent). 지금 이 창구에서는 늘 False다 — event_id를
    # 서버가 매 호출 새로 만들어서(webcall-…-{unix_ms}-{seq}) 중복 갈래를 아예 못 만든다.
    #
    # 그래도 칸을 두는 이유는 계약이 창구마다 갈라지는 걸 막으려는 것이다. 화면이 "복구된
    # 호출인가"를 창구별로 다르게 읽어야 하면 그게 곧 드리프트다.
    #
    # 값은 `routers/query.py`의 `call_shuttle`이 몸통(`ShuttleRecordResult.resent`)에서 그대로
    # 옮긴다. 기본값 False에 기대지 않는 이유는 그게 지금 우연히 맞을 뿐이어서다 — event_id를
    # 부르는 쪽이 정하게 바뀌면 이 창구만 조용히 거짓을 내보낸다.
    resent: bool = False


class HealthOut(BaseModel):
    """상태 확인. 배포 뒤 "보고선이 실제로 켜졌나"를 여기서 본다.

    신원 보고 채널이 꺼진 채로 요원이 신원을 적으면 카드가 아무 데도 안 간다. 그 상태를
    화면도 운영도 알 길이 없었다(운영 실측 — 웹훅 미설정인데 DB엔 신원이 쌓여 있었다).
    값 자체가 아니라 "켜졌나"만 노출하므로 URL·토큰은 안 샌다.
    """

    status: str
    db: str
    identify_report_enabled: bool = False   # 신원 보고 웹훅이 실제로 발사 가능한 상태인가


class EtaOut(BaseModel):
    """예상 도착 시간.

    기록이 없을 때 계약 — HTTP 200에 method="none"·samples=0·eta_sec=null·eta_at=null이다.
    404도 500도 아니다. "아직 데이터가 없다"는 정상 상태로 다룬다.
    """
    gate_no: int | None = None
    method: str                      # ewma / none
    samples: int
    eta_sec: float | None = None     # 셔틀 신호에서 로봇 도착까지 예상 소요 초
    eta_at: dt.datetime | None = None  # 아직 도착이 안 찍힌 셔틀 신호가 있을 때만 채운다
    alpha: float | None = None       # EWMA 계수(method=none이면 None)
    measured_sec: list[float] = Field(default_factory=list)  # 반영한 실측값(오래된 것부터)
    message: str                     # 화면에 그대로 띄우는 안내 문구


# ── 대시보드 초기 로드 스냅샷 ─────────────────────────────────────────────

class SnapshotCounts(BaseModel):
    """리스트가 상한에 잘려도 총량은 알 수 있게 따로 센다."""
    robots: int
    active_alerts: int   # ack=false 전체 수(리스트 상한과 무관)
    recent_events: int   # 이번 응답에 실린 이벤트 수


class WeatherChip(BaseModel):
    """상단 기상 칩 한 벌. 값의 정본은 `dispatch.weather_payload` **한 자리**다.

    이 모델은 그 함수가 내는 여덟 칸을 OpenAPI에 적어 두려고만 있다. 예전엔 스냅샷 칸이
    `dict`라 스펙이 `{"type":"object"}` 한 줄뿐이어서, 프론트가 칸 이름을 알려면 서버 코드를
    읽어야 했다(전체검토 L15).

    ⚠ **`extra="allow"`다.** 여기서 칸을 세지 않는다 — 세면 `weather_payload`에 칸이 늘 때
    스냅샷에서만 조용히 빠져서, 화면이 WS 메시지와 스냅샷을 다른 모양으로 받는다. 그 갈림을
    막으려고 한 벌로 둔 계약이라(`weather_payload` 주석 · 2026-08-03 교차 검증 W2) 모르는
    칸은 그대로 통과시키고 아는 칸만 스펙에 적는다.
    """

    model_config = ConfigDict(extra="allow")

    status: str          # WeatherStatus 여섯 값(CLEAR·CLOUDY·LIGHT_RAIN·HEAVY_RAIN·SNOW·UNKNOWN)
    description: str     # 원문 문구("Sunny"). 사람에게 보여줄 값이고 판정에는 안 쓴다.
    temp_c: int | None
    precip_mm: float | None
    ok: bool             # False면 이번 조회가 실패해 마지막 성공값이나 UNKNOWN으로 갔다.
    stale: bool          # 마지막 성공값을 대신 쓴 건가.
    # ⭐ 값의 출처 — `kma`·`wttr`·`unknown` 셋(2026-08-06 · 프론트 31차 §4-1). 화면은
    # `wttr`일 때만 대체 출처 표식을 단다. `stale`과 다른 축이다 — 저건 "이번 조회가
    # 실패했나"이고 이건 "그 값을 처음 받아온 곳"이라, 기상청 캐시는 `kma`로 남는다.
    source: str = "unknown"
    # ISO-8601 **문자열**이다(`dt.datetime`으로 받으면 재직렬화되면서 WS 메시지와 표기가
    # 갈린다). 값은 조회 시각이 아니라 관측 시각이다(`WeatherReading.observed_at`).
    observed_at: str | None = None
    # 그날 기본 목적지 한 벌(`dispatch.DefaultDestination.as_payload`). 중첩이 한 겹 더 있어
    # 여기선 안 편다 — 펴면 정본이 두 자리가 된다.
    default: dict | None = None


class DashboardSnapshot(BaseModel):
    """대시보드가 붙는 순간 한 번 읽는 상태 묶음.

    이걸로 화면을 먼저 채우고, 그 뒤 변화는 /ws/dashboard 메시지로 받는다.
    """
    server_time: dt.datetime      # 서버 UTC 기준 시각. 화면이 시계 차이를 보정하는 데 쓴다.
    ws_protocol_version: str      # /ws/dashboard 메시지의 version과 같은 값
    # ⭐ 이 스냅샷이 담은 **마지막 사건의 시각**. 서버 현재 시각(`server_time`)이 아니다.
    #
    # 값은 `recent_events[0].received_at`과 같은 축·같은 값이고, 이벤트가 하나도 없으면 null이다.
    # 화면이 WS 메시지를 스냅샷보다 나중 소식으로 가릴 때 쓰는 기준선이라 계약은 딱 한 줄이다 —
    # **WS 봉투의 `timestamp`가 이 값보다 크면 스냅샷에 없는 소식이니 스냅샷이 덮으면 안 된다.**
    #
    # 왜 서버 현재 시각이면 안 되나 — 응답을 만드는 시각은 조회를 **끝낸** 뒤라, 조회 사이에
    # 커밋된 사건(스냅샷에 안 담긴 사건)의 방송 시각보다 늦어진다. 그러면 화면이 그 사건을
    # "스냅샷에 이미 있다"로 잘못 읽고 낡은 값으로 덮는다. 담은 것의 시각이라야 가릴 수 있다.
    #
    # ⚠ 두 값의 시계가 다르다 — 이 칸은 DB 시계(`now()`, 인입 트랜잭션 시작 시각)고 WS 봉투
    #   `timestamp`는 API 프로세스 시계(방송 직전)다. 같은 사건이면 늘 방송이 나중이라 부등호
    #   방향은 안전한 쪽으로 기울지만(스냅샷에 든 사건이 "새 소식"으로 한 번 더 와서 event_id로
    #   걸러지는 쪽), 두 시계가 크게 벌어지면 이 기준선이 무너진다. 지금 배포는 API와 DB가
    #   같은 호스트의 컨테이너라 커널 시계를 공유한다. DB를 딴 호스트로 떼면 NTP가 전제다.
    last_event_at: dt.datetime | None = None
    robots: list[RobotOut]
    active_alerts: list[AlertOut]
    recent_events: list[EventOut]
    counts: SnapshotCounts
    # 신원 보고 채널이 켜져 있나. 꺼져 있으면 화면이 "카드가 안 나갑니다"라고 못박아야 한다 —
    # "설정돼 있으면 나갑니다" 같은 얼버무림은 요원이 보고를 보냈다고 믿게 만든다.
    identify_report_enabled: bool = False
    # 상단 기상 칩. `weather_update` WS 메시지의 payload와 **같은 모양**이고 만드는 함수도
    # 하나다(`dispatch.weather_payload`) — 화면이 두 벌을 따로 파싱하지 않게 한 계약을 쓴다.
    # 값을 만드는 자리는 여전히 그 함수 하나이고, `WeatherChip`은 그 여덟 칸을 OpenAPI에
    # 적어 두기만 한다(`extra="allow"`라 칸이 늘어도 안 잘린다 — 그 모델 주석 참고).
    # 서버가 아직 날씨를 한 번도 못 받았으면 null이다.
    weather: WeatherChip | None = None
    # ⭐ 게이트 센서가 지금 재고 있나(2026-08-07). 라파이가 터치로 켜고 끄는 그 상태다.
    #
    # ⛔ **null은 "괜찮다"가 아니라 "아직 못 받았다"이다.** 서버가 한 번도 못 받은 상태이고
    # 화면은 초록도 빨강도 아닌 색으로 그린다. 거짓이면 **"게이트 센서가 꺼져 있습니다"**다 —
    # 그때는 태깅도 통과도 서버에 안 들어와서 통과 수 0이 "한산하다"가 아니라 "안 잰다"이다.
    #
    # ⚠ 게이트가 여럿이면 **하나라도 꺼져 있을 때 거짓**이다(`gate_sensing.snapshot`).
    gate_sensing: bool | None = None
    # ⭐ 그 값을 마지막으로 받은 시각(2026-08-08 교차 검증). 값이 인메모리 "마지막 값"이라
    # 라파이 전원이 빠지면 옛 상태가 굳는데, 화면이 "언제 기준"인지를 적을 재료가 없었다.
    # gate_sensing이 null이면 이 칸도 null이다.
    gate_sensing_at: dt.datetime | None = None


# ── 미태깅 통계 (조사 정본 2026-07-16: 집계만, 예측 모델 없음) ──────────────

class StatsRange(BaseModel):
    """집계 구간. 날짜는 timezone 기준 로컬 날짜이고 end_date는 구간에 포함된다."""
    days: int
    start_date: dt.date
    end_date: dt.date
    timezone: str


class StatsDailyPoint(BaseModel):
    """추이 그래프 점 하나. 건수가 0인 날도 빠짐없이 실린다."""
    date: dt.date
    count: int
    moving_avg: float | None = None  # 창이 덜 찬 앞머리는 null(선을 거기서부터 그린다)


class StatsHeatmap(BaseModel):
    """요일 × 시간대 히트맵. matrix[요일][시각]이고 늘 7행 24열로 꽉 차 있다.

    행 순서는 weekday_labels와 같은 월~일이고, 열은 0시부터 23시까지다.
    max_count는 색 농도를 맞추는 기준값이라 화면이 따로 최댓값을 훑지 않아도 된다.
    """
    weekday_labels: list[str]
    matrix: list[list[int]]
    max_count: int


class StatsPeak(BaseModel):
    """가장 많이 몰린 칸. weekday는 0=월 … 6=일이다."""
    weekday: int
    hour: int
    count: int


class GatePassHeatmapOut(BaseModel):
    """요일 × 시간대 **통과** 히트맵 (`GET /api/stats/gate-pass-heatmap`).

    ⭐ `UntaggedStatsOut.heatmap`과 격자 모양은 같지만 **세는 것이 다르다** — 이쪽은 판정을
    안 가린 통과 전체다. "언제 사람이 몰리나"를 보는 카드라 정상 통과까지 들어가야 뜻이 선다.

    ⚠ 프론트가 요구한 모양 그대로 **평평하다**(2026-08-05 프론트 2차). 미태깅 쪽은
    `heatmap` 안에 중첩돼 있는데, 그 모양을 그대로 쓰면 화면이 카드마다 다른 깊이를
    다뤄야 한다.

    `matrix`는 늘 7행 24열로 꽉 차 있다. `peak`는 다 0이면 null이다.
    """

    range: StatsRange
    gate_no: int | None = None  # null이면 전체 게이트 합
    weekday_labels: list[str]
    matrix: list[list[int]]
    max_count: int
    peak: StatsPeak | None = None


class UntaggedStatsOut(BaseModel):
    """미태깅 통계 한 벌.

    구간에 미태깅이 하나도 없을 때 계약 — HTTP 200에 total=0이고, daily는 0으로 채워진
    날짜가 그대로 days개, heatmap.matrix도 0으로 꽉 찬 7×24, peak만 null이다.
    """
    range: StatsRange
    gate_no: int | None = None  # null이면 전체 게이트 합
    window: int                 # 이동평균 창 크기(일)
    total: int
    daily: list[StatsDailyPoint]
    heatmap: StatsHeatmap
    peak: StatsPeak | None = None


# ── 오늘 집계 (개발자 텔레메트리 화면 카드) ────────────────────────────────

class TodayGatePassCounts(BaseModel):
    """오늘 게이트 통과를 판정별로 가른 수.

    total은 아래 다섯 칸의 합이라 화면이 따로 더하지 않아도 된다. other는 판정이 아직
    안 붙었거나(null) 여기 사전에 없는 새 판정이 생겼을 때 들어간다 — 새 판정이 나와도
    total과 칸 합이 갈라지지 않게 받아 두는 자리다.
    """
    total: int
    normal: int
    untagged: int
    exit: int
    beam_incomplete: int
    other: int


class TodayStatsOut(BaseModel):
    """오늘(KST) 하루치 종류별 건수.

    화면이 목록을 받아 세면 조회 상한(500건)에 걸려 실제보다 적게 나온다. 그래서 세는
    자리를 서버로 옮겼다 — 이 응답은 상한이 없는 총계다.

    시각 축은 계열마다 "기기가 본 시각"이다(태깅·통과는 observed_at, 셔틀은 signal_ts).
    서버 수신 시각으로 자르면 백로그로 늦게 올라온 어제 이벤트가 오늘로 붙는다.

    ⚠ active_alerts만 오늘 범위가 아니다. 아직 확인 안 한 경고 전수라 어제 것도 들어간다 —
    "오늘 몇 건 났나"가 아니라 "지금 몇 건이 남아 있나"를 보는 값이고, 화면 카드도 그렇게 적는다.
    """
    date: dt.date       # 집계한 하루(timezone 기준 로컬 날짜)
    timezone: str
    tagging: int
    gate_pass: TodayGatePassCounts
    shuttle_arrival: int
    active_alerts: int


# ── 경고 대응 통계 (프론트 요구 N2 — AI 탭 "경고 대응 통계" 카드) ──────────

class StatsDurationSummary(BaseModel):
    """소요시간 한 무리의 요약. 표본이 없으면 count=0이고 나머지는 null이다.

    빈 갈래를 0으로 채우지 않는다 — "0초 만에 확인했다"와 "확인한 게 한 건도 없다"는
    다른 사실이라 화면이 갈라 적어야 한다(`StatsDailyPoint.moving_avg`가 null과 0.0을
    가르는 것과 같은 이유). 나누는 자리가 아예 안 생기니 0으로 나눌 일도 없다.
    """
    count: int                          # 그 시각이 찍힌 경고 수(= 표본 수)
    avg_seconds: float | None = None    # 소수 둘째 자리에서 반올림
    median_seconds: float | None = None


class AlertResponseSummary(BaseModel):
    """경고 한 무리의 대응 통계.

    total은 구간 안에 **생긴**(created_at) 경고 수다. ack·resolve의 count는 그중 확인·종결
    시각이 찍힌 수라 total보다 작거나 같다 — 차이가 "아직 안 본 건"이다.
    """
    total: int
    ack: StatsDurationSummary       # created_at → acked_at
    resolve: StatsDurationSummary   # created_at → resolved_at


class AlertResponseByType(AlertResponseSummary):
    """경고 종류 하나의 대응 통계. type은 `alert.type` 원문이다."""
    type: str


class AlertResponseStatsOut(BaseModel):
    """경고 대응 통계 한 벌.

    구간에 경고가 없을 때 계약 — HTTP 200에 overall.total=0, 세 count 전부 0, 평균·중앙값은
    null, by_type은 빈 배열이다. by_type은 건수 많은 순이고 같으면 종류 이름 오름차순이다
    (동점이어도 응답 순서가 안 흔들리게).
    """
    range: StatsRange
    overall: AlertResponseSummary
    by_type: list[AlertResponseByType]
    # ⭐ 이 구간에서 아직 확인 안 된 경고 수(2026-08-07 · 화면 58차).
    #
    # ⛔ **스냅샷 `active_alerts`와 다른 값이다.** 그쪽은 종류도 기간도 안 가리는 "종
    # 아이콘에 뜰 전부"이고, 이쪽은 이 응답의 구간·종류로 좁힌 값이다. 교대 결산이 "남음"에
    # 스냅샷 값을 쓰고 있어서 "발생 40 · 처리 38 · 남음"이 서로 다른 모집단을 세고 있었다.
    remaining: int = 0
    # 무엇으로 좁혔나. 안 좁혔으면 `null`이다 — 화면이 "전 종류"와 "무단 통과만"을 같은
    # 칸에 그릴 때 무엇끼리 견주는지 알 수 있어야 한다.
    types: list[str] | None = None


# ── 시간 버킷 집계 (프론트 요구 N12 — 이벤트 탭 추이 차트) ─────────────────

class StatsBucketRange(BaseModel):
    """버킷 격자. 경계는 start 이상 end 미만이고 buckets 길이는 bucket_count와 같다."""
    start: dt.datetime      # 첫 버킷이 시작하는 시각
    end: dt.datetime        # 마지막 버킷이 끝나는 시각(이 시각은 구간 밖)
    bucket_seconds: int
    bucket_count: int
    timezone: str           # 버킷 격자를 맞춘 기준 시간대


class GatePassBucketPoint(BaseModel):
    """15분 칸 하나. start는 그 칸이 시작하는 시각이다."""
    start: dt.datetime
    counts: TodayGatePassCounts


class GatePassBucketsOut(BaseModel):
    """게이트 통과 15분 버킷. 건수가 0인 칸도 빠짐없이 실린다.

    빈 칸을 빼면 화면이 그 시간대를 건너뛴 채 선을 이어 그려서, 조용하던 15분이 그래프에서
    사라진다(`UntaggedStatsOut.daily`와 같은 이유).
    """
    range: StatsBucketRange
    total: int
    buckets: list[GatePassBucketPoint]


class AlertSeverityCounts(BaseModel):
    """경고를 심각도로 가른 수. total은 아래 네 칸의 합이다.

    other는 심각도가 안 붙었거나(null) 사전 셋(info·warning·high) 밖 값이 생겼을 때 들어간다 —
    새 심각도가 나와도 total과 칸 합이 갈라지지 않게 받아 두는 자리다
    (`TodayGatePassCounts.other`와 같은 수법).
    """
    total: int
    info: int
    warning: int
    high: int
    other: int


class AlertBucketPoint(BaseModel):
    """24시간 칸 하나. date는 timezone 기준 로컬 날짜다."""
    date: dt.date
    counts: AlertSeverityCounts


class AlertBucketsOut(BaseModel):
    """경고 심각도 24시간 버킷. 건수가 0인 날도 빠짐없이 days개 실린다."""
    range: StatsRange
    total: int
    buckets: list[AlertBucketPoint]


# ── 관제 보조 LLM (P1 #7 — 준비패키지 §2 AI 활용 지도) ─────────────────────
# 세 응답 모두 generated_by를 싣는다. 화면이 "AI가 쓴 문장"과 "규칙으로 만든 문장"을
# 구분해 표시해야 심사에서 과대포장이 안 된다(정직 라인).

class AssistantMeta(BaseModel):
    """어떻게 만들어진 문장인가. fallback_reason은 폴백일 때만 채운다."""
    generated_by: str            # "llm" | "fallback"
    fallback_reason: str | None = None
    model: str | None = None     # LLM이 쓴 경우에만


class DailySummaryOut(BaseModel):
    date: dt.date
    summary: str
    meta: AssistantMeta
    facts: dict                  # 문장의 근거. 화면이 수치를 따로 그릴 때 쓴다.


class AlertBriefOut(BaseModel):
    alert_id: int
    brief: str
    meta: AssistantMeta
    facts: dict


class ChatIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    # ⭐ 2026-08-07 신설. 이어서 물을 대화. **비우면 서버가 새 대화를 연다.**
    # ⚠ 남의 대화 번호를 넣으면 404다 — 있으면서 없는 척하지 않고, 없는 것과 같게 답한다
    #   (있다/없다가 새면 번호를 훑어 남의 대화 존재를 알아낼 수 있다).
    conversation_id: int | None = None


class ChatOut(BaseModel):
    question: str
    answer: str
    meta: AssistantMeta
    context: dict                # 답의 근거로 실제로 넘긴 조회 데이터
    # ⭐ 이번 문답이 어느 대화에 담겼나. 새로 연 경우 화면이 이 값을 들고 이어 묻는다.
    # ⚠ 대화 저장에 실패해도 답변은 그대로 나간다 — 그때는 None이다(§ 라우터 주석).
    conversation_id: int | None = None


# ── 관제 챗봇 대화 목록·상세 (2026-08-07 사용자 확정 · 마이그레이션 0019) ────

class ConversationOut(BaseModel):
    """대화 목록 한 줄. 본문은 안 싣는다 — 목록은 자주 열리고 본문은 열 때만 필요하다."""

    id: int
    title: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    message_count: int


class ChatMessageOut(BaseModel):
    """대화 한 줄. `role`은 `user`·`assistant` 둘뿐이다."""

    id: int
    role: str
    content: str
    generated_by: str | None = None
    fallback_reason: str | None = None
    created_at: dt.datetime


class ConversationDetailOut(BaseModel):
    id: int
    title: str | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    messages: list[ChatMessageOut]


# ── 인증 (로그인 설계 확정본 2026-08-01 §7 — 프론트 접점 계약) ──────────────

class LoginIn(BaseModel):
    """로그인 입력. 길이만 막고 **모양은 안 막는다.**

    아이디 규칙(영문 소문자+숫자 3~20자)은 계정을 *만드는* 창구가 지킨다. 여기서 규칙을
    걸면 규칙 밖 아이디가 401이 아니라 422를 받아서, 응답 코드가 "그런 아이디는 형식부터
    없다"를 알려 주는 셈이 된다. 로그인은 무엇이 들어와도 조회에 실패해 같은 401로
    떨어져야 한다(계정 열거 방지 — §2.6·§7.1).

    상한은 DB 칸 폭(`app_user.username` 32자)과 맞춘다. 비밀번호 상한 200은 임의로 긴
    입력이 scrypt를 태우는 걸 막는 자리다.

    ⚠ **길이 규칙은 handler보다 먼저 도니까 "무엇이 들어와도 401"이 문자 그대로는 아니다.**
    빈 값·상한 초과는 401이 아니라 422로 떨어지고, 401의 `detail`은 문자열인데 422의
    `detail`은 배열이라 모양도 다르다. 프론트는 두 모양을 다 받아야 한다. 열거 방어는 안
    깨진다 — 422는 "그 길이는 못 받는다"만 말하고 계정이 있는지는 어느 쪽 응답에도 안 실린다.
    """

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)


class AuthUserOut(BaseModel):
    """로그인 성공·`GET /api/auth/me` 공통 응답.

    `role`은 `agent`·`leader`·`admin`·`owner` 소문자 영문 넷뿐이다. 한글 표시는 화면 몫이고
    사다리 숫자는 안 내보낸다(§7.3). 비밀번호 해시는 **어떤 응답에도 안 싣는다**(§3).
    """

    # ⭐ 명부 행의 `app_user_id`와 대조할 값. 화면이 "이 명부 줄이 내 계정인가"를 판정하는 데
    # 쓴다(프론트 1차 A-2 요구). 예전에는 대조할 값이 아예 없어서 `username`으로 맞춰야 했는데,
    # 명부(`StaffOut.app_user_id`)는 계정 id를 들고 있어 짝이 안 맞았다.
    # ⚠ 사다리 숫자(권한 등급)와는 다르다 — 그건 여전히 안 내보낸다(§7.3).
    id: int
    username: str
    display_name: str
    role: str


# ── 명부·계정 (로그인 설계 확정본 2026-08-01 §2.3·§2.4·§3 — N14 명부 / 계정 창구) ──


class RoleName(str, Enum):
    """역할·직급 낱말 셋. 계정 `role`과 명부 `rank`가 **같은 사다리 값**을 쓴다(§2.3·§2.5).

    ⚠ 판정 사다리의 정본은 `app.auth.RANK`고 여기는 전선 계약(422 게이트)만 맡는다.
    설계 §4가 "스키마는 `rank`가 enum 안인지만 본다"로 층을 갈라 놓은 자리다. 두 자리가
    어긋나면 `tests/test_staff_endpoints.py`의 낱말 대조 시험이 잡는다 — 여기서 `app.auth`를
    직접 import하지 못하는 이유는 순환이다(schemas ← config ← auth).

    한글 표시("일반 보안요원"·"팀장급"·"관리자")는 화면 몫이고 사다리 숫자는 전선에 안
    내보낸다(§7.3).
    """

    # ⭐ 교육생(2026-08-08 사용자 요청). 제일 낮은 급이다 — 명부 출입자 대부분이 이 급이고,
    #   계정으로 로그인해도 관제 탭 문턱(agent) 아래라 본인 창구만 쓴다.
    trainee = "trainee"
    agent = "agent"
    leader = "leader"
    admin = "admin"
    # ⭐ 총괄 관리자(2026-08-05 사용자 확정). `admin` 위 한 급이다 — 사다리 정본은
    #   `app.auth.RANK`이고 거기서 맨 위다(교육생이 생기며 숫자는 5로 밀렸다).
    owner = "owner"


class StaffOut(BaseModel):
    """명부 한 줄. 팀장급만 본다(§3 — 신원 목록은 명부 권한자만).

    ⚠ 기존 `staff.role` 칸은 **안 싣는다.** 뜻이 `rank`와 겹치는데 폐기 여부가 아직 설계 §8
    결정 필요 1번이라, 둘을 같이 내보내면 화면이 어느 칸을 믿을지 갈라진다. 칸 자체는
    `alert.staff_id` 때문에 DB에 그대로 둔다.

    `rank`를 enum이 아니라 `str`로 받는 이유 — 마이그레이션을 안 거친 사본 DB나 손으로 넣은
    행이 낯선 값을 들고 있으면 enum 검증에서 목록 창구가 통째로 500이 된다(`AlertOut.status`가
    `normalize_status`를 타는 것과 같은 이유). 들어오는 값은 아래 In 스키마가 enum으로 막는다.
    """

    id: int
    name: str
    tag_id: str | None
    student_no: str | None
    rank: str | None
    is_active: bool
    app_user_id: int | None
    created_at: dt.datetime
    updated_at: dt.datetime | None


def _normalize_tag_id(value: str | None) -> str | None:
    """명부에 적는 카드 UID를 **대문자로 통일**한다(2026-08-06 · 팀원 교차 검증).

    ⛔ **왜 필요한가** — 명부 대조가 `Staff.tag_id == tag_id` 정확 비교뿐이라, 대소문자 하나만
    달라도 조용히 "명부 밖 카드"가 된다. 통과 이벤트에 이름이 영영 안 뜨는데 **에러도 경고도
    안 난다.** 사람이 손으로 넣는 값이라 실제로 갈릴 자리다.

    ⭐ **대문자로 맞추는 까닭** — 기기가 그렇게 보낸다. 8/6 실서버 태깅 11건이 전부 대문자였다.
    받는 값을 손대는 대신 **적는 값을 그쪽에 맞춘다** — 기기 원본은 증거라 안 건드리는 게 맞다.

    ⚠ **지금이 넣기 제일 좋은 때다.** 명부에 카드가 심긴 행이 0건이라 이미 저장된 값을
    옮길 필요가 없다. 카드를 실제로 지정하기 시작하면 그때는 마이그레이션이 붙는다.

    ⚠ 앞뒤 공백도 턴다 — 손입력에서 제일 흔한 오염이다.
    """
    if value is None:
        return None
    return value.strip().upper()


class StaffCreateIn(BaseModel):
    """명부 등록 입력. 길이 상한은 DB 칸 폭과 같은 값이다.

    `rank`를 안 보내면 직급 없는 행이 되고, 그런 행은 **관리자만** 만들 수 있다 —
    `assert_can_write`가 직급을 모르는 대상을 닫는 쪽으로 판정하기 때문이다(§4). 명부는
    출입자 전체라 직급이 없는 행(학생 등)이 정상이라서 칸 자체는 열어 둔다(§2.5).
    """

    name: str = Field(min_length=1, max_length=64)
    tag_id: str | None = Field(default=None, min_length=1, max_length=64)
    student_no: str | None = Field(default=None, min_length=1, max_length=32)
    rank: RoleName | None = None
    app_user_id: int | None = Field(default=None, ge=1)

    _norm_tag = field_validator("tag_id")(_normalize_tag_id)


class StaffPatchIn(BaseModel):
    """명부 수정 입력. **보낸 칸만** 바뀐다(`AlertIdentifyIn`과 같은 규칙).

    안 보낸 칸과 null로 보낸 칸을 `model_fields_set`으로 가른다 — 안 그러면 이름만 고치려던
    요청이 태그·학번을 같이 지운다.

    `is_active`는 여기 없다. 해제는 전용 창구(`POST /{id}/deactivate`)가 맡고 감사 기록의
    action도 거기서 갈린다(§3).
    """

    name: str | None = Field(default=None, min_length=1, max_length=64)
    tag_id: str | None = Field(default=None, min_length=1, max_length=64)
    student_no: str | None = Field(default=None, min_length=1, max_length=32)
    rank: RoleName | None = None

    _norm_tag = field_validator("tag_id")(_normalize_tag_id)
    app_user_id: int | None = Field(default=None, ge=1)

    # DB가 NOT NULL인 칸. 여기에 null을 **명시로** 보내면 422다 — 안 막으면 검증을 지나
    # UPDATE에서 IntegrityError 500이 난다(검증 F3). 나머지 칸(tag_id·student_no·
    # app_user_id)의 null은 "지우기"라는 뜻이 있어 그대로 받는다.
    #
    # ⚠ `rank`는 여기 없다. 실제 칸이 nullable이고(`models.Staff.rank`), 직급 없는 행(학생
    # 등)이 명부의 정상 갈래다(`StaffCreateIn` docstring). 목록에 넣어 두면 직급을 잘못 넣은
    # 학생 행을 null로 되돌릴 길이 422로 막힌다 — 등록으로는 만들 수 있는 상태를 수정으로는
    # 못 만드는 갈라짐이다. 급 판정은 여기가 아니라 `auth.assert_can_write`가 한다(그쪽은
    # 직급을 모르는 대상을 닫는 쪽으로 본다).
    _NOT_NULLABLE = ("name",)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("바꿀 항목을 하나 이상 보내 주세요.")
        for field in self._NOT_NULLABLE:
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field}은(는) 비울 수 없습니다. 값을 보내 주세요.")
        return self


class StaffTagOut(BaseModel):
    """`GET /api/staff/by-tag/{tag_id}` 응답. **이름·학번 한 건만** 나간다(§3).

    목록 열람이 아니라 이벤트 문맥 신원 조회라서 요원에게 여는 창구다. 직급·계정 링크·활성
    여부는 안 싣는다 — 그건 명부 권한자가 보는 값이다.
    """

    tag_id: str
    name: str
    student_no: str | None


class StaffAuditOut(BaseModel):
    """명부 감사 기록 한 줄(§2.4). 읽기 전용이고 고치는 창구는 아예 없다(§4).

    행위자 이름이 FK와 문자열로 두 번 실린다. 계정이 바뀌거나 지워져도 기록은 그때 그대로
    남아야 감사라서, `actor_user_id`가 NULL이 돼도 문자열 셋이 증거로 남는다.
    """

    id: int
    staff_id: int
    action: str
    actor_user_id: int | None
    actor_username: str | None
    actor_display_name: str | None
    actor_role: str | None
    before: dict | None
    after: dict
    created_at: dt.datetime


class StaffAccessLogOut(BaseModel):
    """명부 **조회** 이력 한 줄. 변경 감사(`StaffAuditOut`)와 다른 표다.

    ⛔ **태그 UID 원문이 없다.** `tag_prefix`가 앞 네 글자 + 말줄임(`abcd…`)이다. 원문을
    실으면 UID 유출을 막으려고 액세스 로그를 끈 것이 헛일이 된다 — 되짚을 때 "어느 태그
    계열을 봤나"는 남고 값 자체는 안 남는 절충이다.

    `action`이 셋이다.
    - `list` — 명부 목록을 봤다. `result_count`에 몇 줄인지 실린다
    - `detail` — 명부 한 건을 봤다. `target_staff_id`가 찬다
    - `by_tag` — 이벤트에 찍힌 UID로 신원을 봤다. `tag_prefix`와 `target_staff_id`가 찬다
    """

    id: int
    action: str
    target_staff_id: int | None
    tag_prefix: str | None
    result_count: int | None
    actor_user_id: int | None
    actor_username: str | None
    actor_display_name: str | None
    actor_role: str | None
    created_at: dt.datetime


class AppUserOut(BaseModel):
    """계정 한 줄. 관리자만 본다(§3).

    ⚠ `password_hash`를 **어떤 응답에도 안 싣는다**(§3). API 키·DB 접속 문자열을 돌려주는
    창구도 만들지 않는다(범위 정본 §1).

    `role`이 `str`인 이유는 `StaffOut.rank`와 같다 — 낯선 값 하나가 목록 창구를 500으로
    만들면 안 된다.
    """

    id: int
    username: str
    display_name: str
    role: str
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime | None


class AppUserCreateIn(BaseModel):
    """계정 생성 입력.

    아이디 **모양** 판정은 여기가 아니라 창구가 `app.auth.USERNAME_RE`로 한다(정본 한 자리).
    여기 상한 32는 DB 칸 폭이다. 비밀번호 하한 8자는 정본 밖 값이라 설계 §8에 없다 —
    바꾸려면 이 한 줄이다.
    """

    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=200)
    display_name: str = Field(min_length=1, max_length=64)
    role: RoleName


class AppUserPatchIn(BaseModel):
    """계정 역할·활성 수정. 보낸 칸만 바뀐다.

    아이디·실명은 안 받는다 — 아이디는 감사 기록에 복사돼 나간 값이고(§2.4), 실명 변경은
    지금 필요한 자리가 없어서 창구를 안 만든다.
    """

    role: RoleName | None = None
    is_active: bool | None = None

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if not self.model_fields_set:
            raise ValueError("바꿀 항목을 하나 이상 보내 주세요.")
        # 둘 다 DB NOT NULL이라 null 명시 전송은 422다(검증 F3 — 안 막으면 role은
        # AttributeError, is_active는 IntegrityError로 둘 다 500이 난다).
        for field in ("role", "is_active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field}은(는) 비울 수 없습니다. 값을 보내 주세요.")
        return self


class PasswordResetIn(BaseModel):
    """관리자가 계정 비밀번호를 다시 정한다. 옛 비밀번호는 안 받는다 — 관리자 전용 창구고,
    본인이 잊었을 때 푸는 자리라 옛 값을 물으면 그 자리가 막힌다."""

    password: str = Field(min_length=8, max_length=200)


class OwnPasswordChangeIn(BaseModel):
    """본인이 자기 비밀번호를 바꾼다(`POST /api/auth/password` · 2026-08-08 설정 창).

    관리자 재설정(`PasswordResetIn`)과 반대로 **지금 비밀번호를 반드시 받는다** — 세션
    쿠키를 훔친 쪽이 비밀번호까지 갈아 계정을 영구히 가져가는 것을 막는 마지막 겹이다.
    새 비밀번호 길이 규칙(8~200)은 재설정 창구와 같은 값이다 — 화면(PW_MIN)도 8을 본다.
    """

    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


# ── 기기 스피커 폴링 (`GET /api/sound-signals`) ────────────────────────────

class SoundSignalOut(BaseModel):
    """울릴 소리 하나.

    `kind`가 문자열인 이유는 `StaffOut.rank`와 같다 — 서버가 소리 종류를 하나 늘렸을 때
    낯선 값 하나가 기기 쪽 파싱을 깨면 안 된다. 기기는 모르는 종류를 그냥 건너뛰면 된다.
    """

    # 서버가 매긴 일련번호. 기기가 로그에서 "이 소리가 저 도착의 것"을 되짚는 자리다.
    seq: int
    kind: str
    # 어느 게이트에서 난 일인가. 지금 서버는 이걸로 안 거른다(기기가 여럿이어도 전부 받는다) —
    # 왜 안 거르는지는 `app/device_sound.py` 모듈 머리 "기기가 여럿일 때"에 있다.
    gate_no: int | None = None
    # 그 도착 신호의 event_id. 서버 로그·DB 행과 이어 보는 열쇠라 싣는다.
    event_id: str | None = None


class SoundSignalsOut(BaseModel):
    """폴링 한 번의 답. 울릴 게 없으면 `signals`가 빈 목록이다(그게 평소 모습이다).

    `device_id`를 되비추는 이유는 기기가 자기 것을 받았는지 확인할 자리가 있어야 해서다 —
    프록시가 쿼리를 갈아치우거나 기기 설정이 어긋나면 그 사실이 답에 드러난다.
    """

    device_id: str
    signals: list[SoundSignalOut]


# ── 라파이 명령 폴링 (`GET /api/rpi/commands/poll`) ─────────────────────────
#
# 위 `SoundSignalsOut`과 **같은 큐를 다른 옷으로** 내보낸다. 창구가 둘이 된 사연과 왜 두
# 계약을 같이 두는지는 `app/routers/rpi_commands.py` 모듈 머리에 있다. 요약하면 실기기
# 폴러(`hardware/rpi/rpi_commands.py`)가 모의 서버로 개발돼 이 이름을 부르고, 서버를 그쪽에
# 맞추기로 사용자가 정했다(2026-08-05).


class RpiCommandOut(BaseModel):
    """기기가 실행할 명령 하나. 칸 이름이 실기기 폴러가 읽는 그대로다.

    ⚠ **`command_id`가 문자열인 것과 `type`이라는 이름은 계약이다.** 폴러가
    `command.get("command_id") or command.get("id")`로 읽고 ack 경로에 그 값을 문자열로
    끼운다(`rpi_commands.command_id`·`_ack`). 숫자로 바꾸거나 이름을 고치면 ack 경로가
    깨지는데, 폴러는 ack 실패를 로그만 찍고 넘어가서 **조용히 어긋난다.**
    """

    # `sound-<seq>` 꼴. 접두사에 뜻은 없고 로그에서 계열이 읽히게 하는 것뿐이다.
    command_id: str
    # 폴러가 이 값으로 음원을 고른다. `shuttle_arrival`이면 셔틀 도착 안내를 낸다
    # (`rpi_commands.ARRIVAL_COMMAND_TYPES`). 서버 큐의 종류 이름이 이미 그 값과 같아서
    # 변환 없이 나간다 — 두 쪽이 우연히 같은 게 아니라 맞춰 둔 것이니 한쪽만 바꾸지 마라.
    type: str
    gate_no: int | None = None
    event_id: str | None = None


class RpiCommandsOut(BaseModel):
    """폴링 한 번의 답. 울릴 게 없으면 `commands`가 빈 목록이다(그게 평소 모습이다).

    ⚠ 칸 이름이 `commands`인 것도 계약이다 — 폴러의 `normalize_commands`가 이 이름을 먼저
    본다. 다른 이름으로 싸면 폴러가 빈 목록으로 읽고 **아무 소리도 안 나는데 오류도 없다.**
    """

    commands: list[RpiCommandOut]


class RpiCommandAckOut(BaseModel):
    """ack 응답. 폴러는 상태 코드만 보고 본문을 안 읽지만, 사람이 curl로 찔러 볼 때
    무엇이 접수됐는지 보이게 되비춘다."""

    acked: bool
    command_id: str


# ── 시연 자료 비우기 (`POST /api/admin/purge-demo-data`) ────────────────────


class PurgeAccessLogOut(BaseModel):
    """명부 조회 이력을 비운 결과.

    ⭐ **지웠다는 사실 자체는 `staff_audit`에 남는다.** 그 표는 append-only 계약이라
    (로그인 설계 §4) 지우는 창구가 없다. 이 설계가 없으면 **관리자가 자기 조회 흔적을 지워도
    아무 데도 안 남아** 감사 표가 구실을 잃는다 — 프론트도 12차 §3에서 그 위험을 짚었다.
    """

    deleted: int
    # 감사에 남긴 줄의 번호. 화면이 "지운 기록은 감사에 #N 으로 남았습니다"로 알린다.
    audit_id: int


class PurgeDemoDataIn(BaseModel):
    """되돌릴 수 없는 창구라 확인 문구를 본문으로 받는다.

    ⚠ **화면이 두 단계로 묻더라도 서버가 한 번 더 받는다.** 화면 판정만 믿으면 창구를
    직접 부르는 자리에서 그냥 지워진다.
    """

    confirm: str


class PurgeDemoDataOut(BaseModel):
    """지운 결과. 화면이 `total` 을 읽어 "N건을 비웠습니다"로 알린다.

    표별 건수를 같이 주는 이유는 예상과 다른 값이 나왔을 때 어느 표가 어긋났는지 그
    자리에서 보이게 하려는 것이다.
    """

    deleted: dict[str, int]
    total: int
    sequences_reset: bool
