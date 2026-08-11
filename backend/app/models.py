"""DB 모델. 정본 1.4 표 + 아키텍처 2026-07-23 05절을 따른다(어긋나면 아키텍처 우선).

- 인입 3테이블(TaggingEvent·GatePassEvents·ShuttleArrival)은 event_id UNIQUE로 멱등.
- 시각은 전부 timestamptz. 기기 관측(observed_at)과 서버 수신(received_at)을 분리.
- 크레딧 상태 컬럼은 TaggingEvent 단일 정본. 별도 토큰 큐 테이블은 두지 않는다.
- 판정(크레딧 소비·미태깅)은 이번 스코프 밖(2단계). 여기선 컬럼 자리만 두고 verdict/credit_state는 미판정 상태로 적재만.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _tstz() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True))


# ── 축 테이블 ────────────────────────────────────────────────────────────

class Robot(Base):
    __tablename__ = "robot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ⭐ 이름이 이 표의 사실상 열쇠다. 로봇이 붙을 때 이름으로 찾고 없으면 만드는데
    # (`robot_channel._upsert_robot`·`_ensure_robot_pk`), UNIQUE 가 없으면 같은 이름 요청
    # 둘이 겹칠 때 행이 둘 생기고 화면에 로봇 카드가 두 장 뜬다(전체검토 L5). 제약을
    # DB 에 두는 이유는 파이썬 쪽 "찾고 없으면 만들기"가 두 요청 사이에서 원자가 아니라서다.
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mode: Mapped[str | None] = mapped_column(String(32))
    battery: Mapped[int | None] = mapped_column(Integer)
    network_status: Mapped[str | None] = mapped_column(String(32))
    # onupdate가 없으면 상태 UPDATE가 붙어도 시각이 INSERT 때 값에 얼어붙는다(S15P11C207-190).
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RobotStatusLog(Base):
    """주행 시계열. 로봇이 올린 보고를 프레임마다 한 행씩 쌓는다(0017부터).

    ⭐ **되짚기가 목적이다.** 2026-08-06 첫 주행에서 실패 5건이 났는데 사유도 그때 위치도
    안 남아 원인을 못 봤다. 반대로 빔 판정 문제는 `gate_pass_event`가 남아 있어서 "짝이 안
    맞는 게 아니라 한쪽만 온다"까지 좁힐 수 있었다. 그 차이가 이 표를 살린 이유다.

    ⚠ 0001이 만든 뒤로 0017까지는 INSERT가 한 자리도 없는 빈 표였다("[뼈대·미배선]").
    **예전 문서가 그렇게 적고 있으면 낡은 것이다.**

    ⚠ **`robot` 표와 역할이 다르다.** 그쪽은 현재 상태 한 줄을 계속 덮어쓰고(`_upsert_robot`),
    여기는 지우지 않고 쌓는다. "지금 어디 있나"는 저쪽, "그때 어디 있었나"는 이쪽이다.

    쓰는 자리는 `robot_channel._status_log_row` 하나다.

    ⛔ **빈도를 열세 배 낮춰 적고 있었다.** 예전 글은 "실측 2초에 한 번 · 하루 8시간이면
    만 사천 행"이었는데, 2026-08-07 실기기 주행에서 재니 **초당 6.2행**이다(28분에 10,464행).
    하루 8시간이면 **약 18만 행**이다. 젯슨이 `odom`을 5~6Hz로 올리고 그 프레임마다 한 줄을
    쓰기 때문이고, **계약 주석의 "1Hz 다운샘플"은 실제로 안 걸리고 있다.**

    ⚠ 그래서 이 표는 **빠르게 자란다.** 읽는 창구를 낼 때는 `limit` 상한과 기간 제한을 같이
    걸어라. 발표 전 비우기 목록(`tools/purge_test_data.py`)에 들어 있는 이유이기도 하다.
    """

    __tablename__ = "robot_status_log"

    # ⭐ 되짚을 때 쓰는 축이다 — "그 로봇의 그 시각 앞뒤"를 훑는 질의가 유일한 소비자라
    # (robot_id, ts) 복합이다. ts 단독 인덱스는 안 만든다. 로봇이 한두 대라 선택도가 없고,
    # 어느 질의든 robot_id가 먼저 걸린다.
    __table_args__ = (Index("ix_robot_status_log_robot_ts", "robot_id", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robot.id", ondelete="CASCADE"))
    imu: Mapped[dict | None] = mapped_column(JSONB)
    odom: Mapped[dict | None] = mapped_column(JSONB)
    comm_status: Mapped[str | None] = mapped_column(String(32))
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    # ── 0017에서 더한 칸 넷 ─────────────────────────────────────────────
    # ⚠ `odom`은 젯슨 원본(로봇이 켜진 자리가 원점인 상대 좌표)이고 `position`은 서버가
    # 재부팅 오프셋을 보정한 지도 좌표다(`jetson_adapter` 머리 "상대 좌표 원점"). 둘이 다른
    # 값이라 나란히 남긴다 — 하나만 남기면 보정이 맞았는지 나중에 못 가린다.
    mission_status: Mapped[str | None] = mapped_column(String(32))
    position: Mapped[dict | None] = mapped_column(JSONB)
    # ⭐ 젯슨이 "왜 멈췄나"를 싣는 칸이다(`RobotStateIn.log`). 계약에는 처음부터 있었는데
    # 서버가 안 받아 적고 있었다.
    log: Mapped[str | None] = mapped_column(Text)
    sensor_health: Mapped[dict | None] = mapped_column(JSONB)


class DeviceCommandLog(Base):
    """서버가 기기에 낸 명령과 그 기기가 돌려준 응답(0018부터).

    ⭐ **왜 만드나** — 2026-08-06 로봇이 처음 붙은 날, 서버가 낸 명령이 **DB에 한 줄도 안
    남고 있었다.** 출동·복귀·긴급정지·즉시이동도, 젯슨이 그걸 받았는지 거절했는지도, 게이트
    스피커가 소리를 실제로 냈는지도 전부 로그 파일에만 있었다. 로그는 컨테이너를 갈면
    사라지고 질의도 안 된다 — "아까 그 복귀 명령이 로봇에 닿긴 했나"를 되짚을 수가 없었다.

    ⚠ **두 채널이 한 표에 산다.** `channel`로 가른다.
      - `robot` — 서버 → 젯슨. `issued_at`에 행이 나고 `command_ack`가 오면 그 행이 갱신된다.
      - `gate`  — 게이트 스피커. ⚠ **발행 행이 없고 ack 행만 난다.** 소리 큐가 인메모리
        동기 함수(`device_sound.push_sound`)라 발행 자리에서 DB를 못 만진다. 대신 라파이가
        "울렸다"고 보내는 ack를 남긴다 — **큐에 넣은 것보다 실제로 울린 것이 더 값진 기록이다.**

    ⚠ `command_id`에 UNIQUE를 안 건다. 기기가 여러 대면 같은 소리 신호에 각자 ack를 보내
    같은 이름으로 행이 여럿 난다(지금은 라파이 한 대지만 설계가 그렇다).
    """

    __tablename__ = "device_command_log"

    # 되짚는 축 둘이다 — ack가 왔을 때 그 명령을 찾는 `command_id`, "그 무렵 무슨 명령이
    # 오갔나"를 훑는 `issued_at`.
    __table_args__ = (
        Index("ix_device_command_log_command_id", "command_id"),
        Index("ix_device_command_log_issued_at", "issued_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    command_id: Mapped[str] = mapped_column(String(64))
    command_type: Mapped[str | None] = mapped_column(String(32))
    # 로봇 이름(`jetson01`)이나 게이트 기기 id다. 어느 쪽인지는 `channel`이 안다.
    target: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSONB)
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ack_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # 젯슨 계약의 `result`다(`accepted`·`rejected`). 게이트 ack에는 `played`를 적는다.
    ack_result: Mapped[str | None] = mapped_column(String(16))
    ack_detail: Mapped[str | None] = mapped_column(Text)


# ── 인입 3테이블 (event_id 멱등) ──────────────────────────────────────────

class TaggingEvent(Base):
    __tablename__ = "tagging_event"

    # 크레딧 창 질의 축(마이그레이션 0009). 소비 FIFO·미소비 개수·만료 일괄·회수가 전부
    # 이 축이다. ⚠ `gate_no`는 일부러 뺐다 — 상태기계가 `IS NOT DISTINCT FROM`으로 거는데
    # Postgres는 그 연산자를 인덱스 조건으로 못 써서, 앞에 세우면 인덱스가 통째로 안 잡힌다
    # (0009 머리에 실측 근거). 이 인덱스는 마이그레이션 쪽과 이름·칸 순서가 같아야 한다.
    #
    # `ix_tagging_event_received_at_id`는 이벤트 피드의 정렬 축이다(마이그레이션 0010).
    # 3표가 각자 있어야 UNION이 `Merge Append ← Index Scan Backward ×3`으로 붙는다.
    # 빠진 가지는 `Sort ← Seq Scan`이 되고 그 표 크기만큼 손해다(0010 머리에 실측표).
    __table_args__ = (
        Index(
            "ix_tagging_event_credit_window",
            "credit_state",
            "expires_at",
            "observed_at",
        ),
        Index("ix_tagging_event_received_at_id", "received_at", "id"),
        # 낱말 정본은 `app/credit/state_machine.py`의 `CreditState`다. 늘리거나 바꾸면
        # 여기와 마이그레이션 0013을 같이 고쳐야 한다.
        CheckConstraint(
            "credit_state IN ('issued', 'consumed', 'expired', 'recovered')",
            name="ck_tagging_event_credit_state",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_id: Mapped[str | None] = mapped_column(String(64))
    # [초안·팀 확정 대기] 안건②. event_id에서 뽑거나 본문으로 받는다. 예전 형식이면 NULL.
    boot_id: Mapped[str | None] = mapped_column(String(16))
    gate_no: Mapped[int | None] = mapped_column(Integer)
    # [초안·팀 확정 대기] 안건①. 옵션 컬럼이라 안 보내면 NULL 그대로다.
    room_id: Mapped[str | None] = mapped_column(String(32), index=True)
    tag_id: Mapped[str] = mapped_column(String(64), index=True)
    # ⭐ 시연 도구가 만든 행인가 (2026-08-05 프론트 22차 · 사용자 확정).
    # 진짜 자료와 갈라 보고 **그 표시만으로 골라 지우려고** 둔다. `device_id` 이름
    # 규칙으로 가르면 기기가 늘거나 이름이 바뀌는 날 조용히 갈라진다.
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True)
    # `/api/stats/today`가 이 칸으로 오늘 구간을 자른다(`stats.today_counts`). 위
    # `credit_window` 인덱스에도 이 칸이 있지만 **셋째 칸**이라 앞칸 술어 없이는 못 탄다 —
    # 단일 축이 따로 필요하다(마이그레이션 0013 · L29).
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # 크레딧 생애주기 컬럼(단일 정본). 판정은 2단계 몫이라 지금은 적재만 하고 미판정으로 둔다.
    credit_state: Mapped[str] = mapped_column(String(16), server_default="issued")
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_pass_id: Mapped[int | None] = mapped_column(BigInteger)


class GatePassEvent(Base):
    __tablename__ = "gate_pass_event"

    # 이벤트 피드 정렬 축(마이그레이션 0010). `observed_at` 인덱스와 다른 칸이다 —
    # 피드는 기기 관측 시각이 아니라 **서버 수신 시각**으로 줄을 세운다.
    __table_args__ = (
        Index("ix_gate_pass_event_received_at_id", "received_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    device_id: Mapped[str | None] = mapped_column(String(64))
    # [초안·팀 확정 대기] 안건②.
    boot_id: Mapped[str | None] = mapped_column(String(16))
    gate_no: Mapped[int | None] = mapped_column(Integer)
    # [초안·팀 확정 대기] 안건①. 크레딧 소비 범위를 룸까지 좁힐 때 쓴다.
    room_id: Mapped[str | None] = mapped_column(String(32), index=True)
    direction: Mapped[str | None] = mapped_column(String(8))  # A_TO_B / B_TO_A / null
    status: Mapped[str | None] = mapped_column(String(16))    # complete / incomplete
    beam_a_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    beam_b_ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # 인덱스는 통계 세 자리(시간대별 미태깅·판정 분포·구간 버킷)가 이 칸으로 구간을 자르기
    # 때문이다(마이그레이션 0009). `(verdict, observed_at)` 복합도 재 봤는데 미태깅 집계
    # 하나만 받고 나머지 둘은 verdict를 안 걸어서 Seq Scan 그대로였다.
    observed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # 서버 상태머신이 크레딧 판정 뒤 채운다(2단계). 지금은 null로 적재.
    matched_tagging_event_id: Mapped[int | None] = mapped_column(BigInteger)
    # 소비한 크레딧의 카드 UID(S15P11C207-241 · 마이그레이션 0007). 빔은 사람을 못 알아보니
    # 이 값은 **누가 지나갔나가 아니라 어느 크레딧을 태웠나**다 — 소비는 그대로 게이트 FIFO다.
    # matched_tagging_event_id의 표시용 사본이라 소비가 없는 판정(미태깅·퇴장·보류)에서는 NULL이다.
    # 조인 없이 통과 한 줄만 읽어도 UID가 보이게 하려고 값을 복사해 둔다(조회·WS가 이 칸을 쓴다).
    tag_id: Mapped[str | None] = mapped_column(String(64))
    verdict: Mapped[str | None] = mapped_column(String(16))  # normal / untagged / beam_incomplete
    # ⭐ 시연 도구가 만든 행인가 (2026-08-05 프론트 22차 · 사용자 확정).
    # 진짜 자료와 갈라 보고 **그 표시만으로 골라 지우려고** 둔다. `device_id` 이름
    # 규칙으로 가르면 기기가 늘거나 이름이 바뀌는 날 조용히 갈라진다.
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True)


class ShuttleArrival(Base):
    __tablename__ = "shuttle_arrival"

    # 이벤트 피드 정렬 축(마이그레이션 0010). 셋 중 행이 제일 적어 이득도 제일 작지만
    # (실측 1.48 → 0.055 ms) 셔틀 행도 시연 내내 쌓여서 같이 깐다.
    __table_args__ = (
        Index("ix_shuttle_arrival_received_at_id", "received_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # [초안·팀 확정 대기] 안건②.
    boot_id: Mapped[str | None] = mapped_column(String(16))
    gate_no: Mapped[int | None] = mapped_column(Integer)
    # [초안·팀 확정 대기] 안건①. 셔틀은 판정이 없어 기록·대상 지정 용도로만 둔다.
    room_id: Mapped[str | None] = mapped_column(String(32), index=True)
    shuttle_no: Mapped[str | None] = mapped_column(String(32))
    # 셔틀의 "기기가 본 시각"이다. `/api/stats/today`가 태깅·통과의 `observed_at`과 짝으로
    # 이 칸을 잘라서, 축 셋 중 여기만 인덱스가 없으면 같은 집계 안에서 이 표만 Seq Scan이
    # 된다(마이그레이션 0013 · L29). 0009가 미룬 `gate_no`·`notified_robot_id`는 그대로 둔다 —
    # 짝짓기 질의가 `id NOT IN (…)` 반조인이라 어차피 표를 한 바퀴 돈다.
    signal_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    notified_robot_id: Mapped[int | None] = mapped_column(Integer)


# ── 알림 ─────────────────────────────────────────────────────────────────

class Staff(Base):
    """요원 명부(로그인 설계 확정본 §2.3 · 마이그레이션 0008에서 칸이 늘었다).

    ⚠ 기존 `role` 칸은 안 건드린다. `alert.staff_id`가 이 테이블을 물고 있어서, 뜻이
    `rank`와 겹친다고 지우면 손댈 자리가 번진다. 직급은 `rank`로 새로 팠고, `role` 폐기
    여부는 설계 §8 결정 필요 1번에 남아 있다.

    `rank`와 `app_user.role`은 **다른 것**이다. `rank`는 "이 행이 어느 급인가"(명찰)고,
    권한 판정에 쓰는 행위자 급은 오직 `app_user.role`(장부)에서 읽는다. 그래야 두 장부가
    어긋날 여지가 구조에서 사라진다(§2.5).

    `app_user_id`는 "본인 항목" 판정 열쇠다. 이름 문자열 비교는 안 쓴다 — 동명이인 한
    명이면 남의 행이 잠긴다(§2.5 · 확정 D). 계정과 명부를 1:1로 강제하지도 않는다.
    명부는 출입자 전체고 계정은 팀원 6명이라, 억지로 묶으면 대부분이 계정 없는 행이 된다.
    """

    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))

    # ── 0008에서 붙은 칸 ────────────────────────────────────────────────
    # RFID UID. 이벤트 → 신원을 잇는 열쇠라 유니크다. 아직 안 붙은 행은 NULL이고,
    # Postgres 유니크 인덱스는 NULL을 여러 개 허용하므로 빈 명부도 안 걸린다.
    tag_id: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    student_no: Mapped[str | None] = mapped_column(String(32))
    # 직급. RANK 사다리(agent < leader < admin < owner) 값이다. 판정은 app/auth.py 한 자리.
    # ⭐ owner(총괄 관리자)는 2026-08-05에 얹혔다 — 사용자 확정·프론트 20차.
    rank: Mapped[str | None] = mapped_column(String(16))
    # 해제는 삭제가 아니라 이 칸을 내린다 — 지우면 alert.staff_id가 끊기고 기록이 샌다.
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    app_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class Alert(Base):
    __tablename__ = "alert"

    # 마이그레이션 0009. 둘 다 실제로 도는 질의를 EXPLAIN으로 확인하고 고른 축이다.
    #  - ix_alert_source        역추적 규칙 `(source_type, source_id) = 이벤트 (kind, id)`를
    #                           쓰는 자리 둘(fetch_alerts 필터 · _tag_is_on_active_alert 조인).
    #  - ix_alert_created_at_id 목록의 정렬 축. **오름차순으로 만든다** — Postgres가 btree를
    #                           거꾸로 훑어서 `ORDER BY created_at DESC, id DESC`가 그대로
    #                           탄다(Index Scan Backward). DESC 인덱스는 얻는 게 없다.
    # ⚠ `type` 단독 인덱스는 일부러 안 만든다. 어느 필터를 걸어도 끝에 정렬+LIMIT이 붙어서
    #   옵티마이저가 늘 위 정렬 인덱스를 고른다 — 깔아 두고 재도 한 번도 안 뽑혔다.
    # 낱말 정본은 `app/alert_lifecycle.py`의 `AlertStatus`·`AlertResolution`이다. 늘리거나
    # 바꾸면 아래 CHECK와 마이그레이션 0013을 같이 고쳐야 한다.
    # ⚠ `resolution`은 nullable인데 SQL CHECK는 결과가 NULL이면 통과라 따로 안 적어도 NULL이
    #   그대로 들어간다(닫히지 않은 사건이 전부 그 자리다).
    __table_args__ = (
        Index("ix_alert_source", "source_type", "source_id"),
        Index("ix_alert_created_at_id", "created_at", "id"),
        # ⭐ 학번으로 경고를 찾는 창구(`GET /api/alerts/by-student-no/{...}`)가 쓴다.
        # **부분 인덱스다** — 이 칸은 거의 다 NULL 이라(신원을 적고 학번까지 적은 행만
        # 값이 있다) 전체 인덱스는 빈 행까지 담아 쓸데없이 커진다. 0016 과 짝이다.
        Index(
            "ix_alert_identified_student_no",
            "identified_student_no",
            postgresql_where=text("identified_student_no IS NOT NULL"),
        ),
        CheckConstraint(
            "status IN ('OPEN', 'ACKED', 'IDENTIFIED', 'RESOLVED')",
            name="ck_alert_status",
        ),
        CheckConstraint(
            "resolution IN ('confirmed', 'false_positive', 'duplicate', 'other')",
            name="ck_alert_resolution",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str | None] = mapped_column(String(16))
    # source_type + source_id(nullable)로 발화 이벤트(빔 통과·태깅·카메라)까지 역추적.
    source_type: Mapped[str | None] = mapped_column(String(32))
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    ack: Mapped[bool] = mapped_column(Boolean, server_default="false")
    # ⭐ 시연 도구가 만든 행인가 (2026-08-05 프론트 22차 · 사용자 확정).
    # 진짜 자료와 갈라 보고 **그 표시만으로 골라 지우려고** 둔다. `device_id` 이름
    # 규칙으로 가르면 기기가 늘거나 이름이 바뀌는 날 조용히 갈라진다.
    is_demo: Mapped[bool] = mapped_column(Boolean, server_default="false", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # ── 사건 수명주기 (마이그레이션 0005) ────────────────────────────────
    # 상태 낱말·전이 규칙·불변식은 app/alert_lifecycle.py에 있다(설계 근거도 거기).
    #
    # ⚠ ack는 이 축의 파생 사본이 **아니다.** `ack == (status != 'OPEN')`은 틀린 불변식이라
    #   걷어냈다 — 관제 확인(ack)과 안쪽 요원의 신원 입력(IDENTIFIED)은 다른 사람이 남기는
    #   다른 사실이라, `ack=false`인데 status=IDENTIFIED가 **정의된 값**이다
    #   (alert_lifecycle.py "ack와 이 축의 관계 — 묶지 않는다").
    #   실제 계약은 둘이다. ①새로 생기는 행에서 `ack == (acked_at is not None)`,
    #   ②ACKED로 가는 전이만 ack·acked_at을 찍는다(OPEN에서 RESOLVED로 건너뛴 종결은
    #   ack를 안 건드린다 — 관제가 안 본 채 일괄 종결한 건과 확인한 건은 다른 사실이다).
    #
    # server_default를 두는 게 중요하다. Alert를 만드는 자리가 상태머신
    # (credit/state_machine.py)과 로봇 채널(robot_channel.py) 둘인데 그 파일들은
    # 이번 수리 범위 밖이다. DB 기본값이면 그 INSERT를 한 줄도 안 고쳐도 OPEN으로 들어온다.
    status: Mapped[str] = mapped_column(String(16), server_default="OPEN", index=True)
    # 언제·누가 손댔나. ⚠ acked_by·resolved_by는 본문에 실려 온 자칭 문자열이고
    # 인증 없는 목록 응답(AlertOut)엔 안 싣는다(identified_by와 같은 이유).
    acked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    acked_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    # 왜 닫았나. AlertResolution 낱말 넷 중 하나(confirmed/false_positive/duplicate/other).
    resolution: Mapped[str | None] = mapped_column(String(16))

    # ── 사후 신원 확인 (미태깅 통과 대응 흐름) ──────────────────────────
    # 미태깅 통과는 태그를 안 댔으니 시스템에 신원 근거가 없다. 게이트에서 막지 않고
    # 흘려보낸 뒤, 안쪽 보안 요원이 붙잡아 확인한 신원을 관제 화면에서 직접 적어 넣는다.
    # 그 입력이 여기 남고, 별도 매터모스트 채널(상위 보고선)로 카드가 나간다.
    #
    # staff_id를 안 쓰고 identified_by를 새로 판 이유 — 이 칸을 만들 때 staff 테이블이
    # 빈 뼈대였다. FK를 쓰면 요원이 신원을 적을 때마다 없는 staff 행을 먼저 만들어야 해서
    # 시연 흐름이 끊긴다.
    #
    # ⚠ **2026-08-05에 그 전제가 바뀌었다.** 명부에 조회·등록·수정 창구가 다 생겼고
    # (`app/routers/staff.py`) 실서버에 여섯 행이 들어갔다(`tools/seed_staff.py`).
    # 그래도 이 칸은 그대로 둔다 — **명부에 없는 사람도 적어야 하는 자리**라서다(외부인·방문객).
    # `staff_id`로 승격할지는 사용자 결정 자리이고, 하더라도 `identified_by`는 표시용 사본으로
    # 남겨야 명부에서 지워진 사람의 기록이 안 사라진다.
    #
    # ⚠ 개인정보 — identified_person은 자유 문자열이고 길이만 제한한다. 실명·학번을
    # 넣을지는 팀 결정이며, 시연에서는 가명("가명-1")을 권한다. 이 값은 로그에 통째로
    # 안 찍고, 인증 없는 목록 API(GET /api/alerts) 응답에도 안 싣는다.
    identified_person: Mapped[str | None] = mapped_column(String(64))
    # ⭐ 학번(2026-08-06 사용자 확정 · 프론트 27차). **이름과 나눠 든다** — 이름은 동명이인이
    # 있고 명부 대조(`staff.student_no`)도 학번으로 한다. 폭 32는 그 칸과 맞춘 값이다.
    # ⚠ 옵션이다. 현장에서 이름만 알아낸 갈래를 막으면 입력이 통째로 멈춘다.
    identified_student_no: Mapped[str | None] = mapped_column(String(32))
    identified_by: Mapped[str | None] = mapped_column(String(64))
    identified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    identify_note: Mapped[str | None] = mapped_column(String(200))


# ── 뼈대 3종 (P1 후순위, 마이그레이션 범위만 확보) ─────────────────────────

class StickerInspection(Base):
    __tablename__ = "sticker_inspection"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tagging_event_id: Mapped[int | None] = mapped_column(BigInteger)
    attached: Mapped[bool | None] = mapped_column(Boolean)
    confidence: Mapped[float | None] = mapped_column(Float)
    image_ref: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class TemporaryPass(Base):
    __tablename__ = "temporary_pass"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    student_no: Mapped[str | None] = mapped_column(String(32))
    kiosk_id: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class Kiosk(Base):
    __tablename__ = "kiosk"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str | None] = mapped_column(String(32))


# ── 그날의 기본 목적지 (셔틀출동 설계 2026-07-31 §2.1 · 마이그레이션 0006) ──

class DailyDefaultDestination(Base):
    """날짜(KST) 하나에 기본 목적지 하나. 셔틀이 왔는데 아무도 안 고를 때 갈 곳이다.

    **DB에 두는 이유는 재기동이다.** 아침에 정한 값이 인메모리면 서버가 한 번만 다시 떠도
    그날의 결정이 사라진다(설계 §2.1-4).

    `source`가 계약의 핵심이다 — `manual`이면 요원이 손으로 고른 값이라 그날 안에는 자동
    날씨 조회가 덮지 않는다. 자동 조회는 **행이 아예 없을 때만** 행을 만든다.

    날씨 칸 셋은 판정 근거를 남기려고 같이 적는다. `weather_ok=False`면 조회에 실패해
    마지막 성공값이나 UNKNOWN으로 정했다는 뜻이고, 화면이 그 사실을 표시한다.

    ⚠ `service_date`는 **KST 날짜**다. UTC로 읽으면 한국 아침 9시 전에 날짜가 하루 밀린다.
    변환은 오프셋을 더한 tz 변환으로만 한다(문자열 치환 금지 — app/dispatch.py `kst_today`).
    """
    __tablename__ = "daily_default_destination"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    # Destination enum 값(INDOOR_TAGGING / OUTDOOR_TAGGING). 문자열로 두는 건 다른 인입
    # 테이블과 같은 방식이다 — DB enum 타입을 만들면 값 하나 늘 때마다 마이그레이션이 붙는다.
    destination: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(8))  # auto / manual
    weather_status: Mapped[str | None] = mapped_column(String(16))
    weather_desc: Mapped[str | None] = mapped_column(String(64))
    weather_ok: Mapped[bool | None] = mapped_column(Boolean)
    decided_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


# ── 로그인 (로그인 설계 확정본 §2 · 마이그레이션 0008) ──────────────────────

class AppUser(Base):
    """로그인 계정. 권한 판정의 유일한 정본이다(§2.5).

    테이블 이름을 `user`로 안 하는 이유는 Postgres 예약어라 쿼리마다 따옴표가 붙어서다.

    `role`은 `agent`·`leader`·`admin`·`owner` 넷뿐이고 전선에도 이 소문자 영문 그대로
    나간다. ⭐ `owner`(총괄 관리자)는 2026-08-05에 얹혔다(사용자 확정·프론트 20차) —
    `String(32)`이라 마이그레이션 없이 값만 늘었다.
    한글 표시("일반 보안요원")는 화면 몫이고, 사다리 숫자(1·2·3)는 안 내보낸다 — 화면이
    숫자를 비교하기 시작하면 판정이 두 자리로 갈라진다(§7.3).

    `is_active`가 false면 로그인이 거절되고 **살아 있는 세션도 그 자리에서 죽는다**
    (auth.resolve_session이 매 요청 계정을 같이 본다). 강등·해제가 다음 요청부터 먹혀야
    한다는 게 세션을 고른 이유라(§1.1), 계정 상태를 세션 조회에서 안 보면 그 근거가 샌다.
    """

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 로그인 입력값. 영문 소문자+숫자 3~20자(§8 확정 A). 규칙 정본은 app/auth.py USERNAME_RE.
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # scrypt 저장 형식(파라미터까지 실린다). 만드는·읽는 자리는 app/auth.py 하나뿐이다.
    password_hash: Mapped[str] = mapped_column(String(255))
    # 한글 실명. 감사 기록(staff_audit·identified_by·acked_by)과 화면에 찍히는 이름이다.
    display_name: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class AuthSession(Base):
    """불투명 세션 토큰 한 장. 인메모리가 아니라 DB에 두는 이유는 셋이다(§1.3).

    재배포 생존(젠킨스가 컨테이너를 갈아 끼운다) · 워커 확장 여지 · 새 의존성 0개.

    ⚠ `token_hash`는 **원문이 아니라 sha256(token)**이다. DB 덤프 한 번에 살아 있는 세션이
    통째로 털리는 걸 막는다. 유니크 인덱스라 조회 비용은 원문과 같다.

    무효화는 행을 지우지 않고 `revoked_at`을 찍는다 — "언제 끊겼나"가 감사 자료다.
    끊긴 **이유**를 담는 칸은 없다(로그아웃·상한 회수·관리자 무효화가 같은 모양으로 남는다).
    청소는 만료 뒤 7일 지난 행만 지우고, 별도 크론 없이 로그인 창구가 훑는다.
    """

    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # 절대 만료. 어떤 활동으로도 안 늘어난다(§1.4).
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    # 유휴 만료의 기준점. 활동하면 밀리는데, 매 요청 UPDATE는 안 한다(§1.4 쓰기 폭주 방어).
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(200))


class StaffAudit(Base):
    """명부 변경 감사 기록. **append-only**다 — 수정·삭제 창구를 아예 안 만든다(§4).

    INSERT는 명부 변경과 **같은 트랜잭션**이라, 변경만 남고 기록이 빠지는 갈라짐이 없다.

    ⚠ `staff_id`에 FK를 안 건다. 명부 행이 사라지면(해제가 아니라 실제 삭제) 감사 기록까지
    같이 끌려가거나 INSERT가 막히는데, 둘 다 감사가 아니다. 무결성보다 기록 보존이 위인
    테이블이라 느슨한 정수로 둔다.

    행위자 이름을 FK만이 아니라 **문자열로도 복사해 둔다** — 계정이 나중에 바뀌거나
    지워져도 기록이 그때 그대로 남아야 감사다.
    """

    __tablename__ = "staff_audit"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    staff_id: Mapped[int] = mapped_column(Integer, index=True)
    # create / update / deactivate / reactivate / delete / purge_access_log
    # ⚠ **`purge_access_log`가 정확히 16자다.** 낱말을 하나 더 늘릴 때 길이를 먼저 세라 —
    #    넘치면 INSERT가 터지는데 그 자리가 "지운 사실을 남기는" 갈래라 조용히 안 남는 게
    #    아니라 요청 자체가 500이 된다. 더 긴 낱말이 필요하면 칸 폭부터 늘린다.
    action: Mapped[str] = mapped_column(String(16))
    # 계정이 지워져도 기록은 남아야 하니 SET NULL이고, 아래 문자열 사본이 진짜 증거다.
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    actor_username: Mapped[str | None] = mapped_column(String(32))
    actor_display_name: Mapped[str | None] = mapped_column(String(64))
    actor_role: Mapped[str | None] = mapped_column(String(16))
    # 등록(create)에는 이전 값이 없다.
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class StaffAccessLog(Base):
    """명부를 **누가 봤나** 기록. 변경 감사(`StaffAudit`)와 일부러 갈라 둔 표다.

    ## 왜 `staff_audit`에 안 얹었나

    셋이 안 맞는다. ①목록 조회는 대상 `staff_id`가 없는데 그쪽은 필수다. ②조회는 "이후
    값"이 없는데 `after`가 필수다. ③조회는 화면을 열 때마다 나서 변경보다 훨씬 잦다 —
    한 표에 섞으면 변경 이력이 조회 기록에 파묻힌다.

    ## ⛔ 무엇을 안 남기나 — 태그 UID 원문

    `by-tag` 조회는 경로에 태그 UID가 실린다. 그 값을 여기 그대로 적으면 **유출을 막으려고
    액세스 로그를 끈 것이 헛일이 된다**(§3 · 2026-08-02 실측). 그래서 UID 는 앞 네 글자만
    남기고 뒤를 가린다(`abcd…`). 사고를 되짚을 때 "어느 태그 계열을 봤나"는 남고 값 자체는
    안 남는 절충이다.

    조회 대상이 특정되는 갈래(`detail`)는 `target_staff_id`만 남긴다 — 그 번호로 명부를
    되짚는 건 어차피 명부 권한자만 할 수 있다.

    ## append-only

    `StaffAudit`과 같은 계약이다. 수정·삭제 창구를 아예 안 만든다. 지우는 길은 DB 직접
    접근뿐이고, 그건 웹에서 누구도 못 한다(관리자 포함).
    """

    __tablename__ = "staff_access_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # list / detail / by_tag. 어느 창구로 봤나.
    action: Mapped[str] = mapped_column(String(16))
    # 단건 조회일 때만 찬다. 목록·태그 조회는 NULL 이다.
    target_staff_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # by_tag 일 때 태그 UID 앞 네 글자 + 말줄임. 딴 갈래는 NULL.
    tag_prefix: Mapped[str | None] = mapped_column(String(16))
    # 목록 조회가 몇 줄을 가져갔나. 딴 갈래는 NULL.
    result_count: Mapped[int | None] = mapped_column(Integer)
    # 행위자. `StaffAudit`과 같은 이유로 FK 와 문자열 사본을 같이 든다 — 계정이 바뀌거나
    # 지워져도 기록이 그때 그대로 남아야 감사다.
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    actor_username: Mapped[str | None] = mapped_column(String(32))
    actor_display_name: Mapped[str | None] = mapped_column(String(64))
    actor_role: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


# ── 관제 챗봇 대화 (2026-08-07 사용자 확정 · 마이그레이션 0019) ──────────────

class AssistantDailyBrief(Base):
    """일일 요약을 만들어 두는 자리 (2026-08-07 · 관제 보조 갈래 요청).

    ⭐ **왜 만드나** — 요원이 화면을 열 때마다 LLM 을 부르고 있었다. 한 번에 7초대라
    개요 탭이 그만큼 비어 있었다. **이 브리핑은 요원이 누를 때 만들 이유가 없다** — 같은
    날 같은 자료면 답도 같다.

    ⚠ **오늘치는 자료가 계속 는다.** 그래서 오늘은 수명(`assistant_brief_ttl_sec`)을 두고
    지나면 다시 만든다. **지난 날짜는 자료가 안 바뀌니 영구히 재사용한다.**

    ⛔ **폴백 문장은 저장하지 않는다.** LLM 이 죽어서 나온 규칙 문장을 캐시에 앉히면 그
    문장이 수명 동안 계속 나간다 — 그 사이 LLM 이 살아나도 요원은 계속 규칙 문장을 본다.
    """

    __tablename__ = "assistant_daily_brief"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 하루에 한 줄이다. 같은 날 두 줄이 생기면 어느 것이 최신인지 판정이 갈린다.
    service_date: Mapped[dt.date] = mapped_column(Date, unique=True, index=True)
    summary: Mapped[str] = mapped_column(Text)
    # ⚠ 어느 모델이 만든 문장인지 남긴다. 모델을 바꾼 뒤에도 옛 문장이 캐시에 남아 있으면
    #    "왜 말투가 다른가"를 되짚을 단서가 이것뿐이다.
    model: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ChatConversation(Base):
    """관제 챗봇 대화 한 묶음. 계정마다 갈린다.

    ⭐ **왜 만드나** — 그전까지 대화가 **브라우저 메모리에만** 쌓였다. 새로고침하면 통째로
    사라지고 다른 기기에서 로그인하면 안 보였다. 화면에는 "새 채팅" 버튼과 목록이 이미
    있어서 겉보기에는 되는 것처럼 읽혔다.

    사용자 확정(2026-08-07) — "서버로 해야지."

    ⛔ **브라우저 저장소로 흉내 내지 않는 까닭.** `localStorage` 는 **공용 PC 에서 다음 사람에게
    그대로 넘어간다.** 관제 PC 는 요원이 교대로 쓰는 자리다. 같은 판단을 이미 한 번 했다 —
    화면이 API 키를 `sessionStorage` 에 두는 이유가 그것이다(`telemetry.html` 주석).
    대화 내용은 "요원이 무엇을 물었나"라서 키보다 덜 민감하지도 않다.

    ⚠ **소유자 밖 조회를 막는 것이 이 표의 유일한 보안 계약이다.** 조회 창구는 반드시
    `user_id == actor.id` 를 걸어야 한다. 계정마다 갈리는 것이 요구사항의 본체라,
    그 조건이 빠지면 기능이 아니라 사고가 된다.
    """

    __tablename__ = "chat_conversation"

    # 목록 질의가 "내 것을 최근 순으로"라 (user_id, updated_at) 복합이다. updated_at 단독은
    # 안 만든다 — 어느 질의든 user_id 가 먼저 걸린다.
    __table_args__ = (
        Index("ix_chat_conversation_user_updated", "user_id", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # ⚠ 계정을 지우면 대화도 같이 지운다. 감사 기록(`staff_audit`)과 달리 이건 개인 자료라
    #    남겨 둘 근거가 없다.
    user_id: Mapped[int] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    # 목록에 보일 이름. 첫 질문에서 뽑아 넣고 사용자가 고칠 수 있게 둔다.
    title: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # 마지막 메시지 시각. 목록 정렬 축이라 메시지를 붙일 때마다 갱신한다.
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class ChatMessage(Base):
    """대화 한 줄. 사람이 물은 것과 서버가 답한 것을 같은 표에 `role` 로 갈라 담는다.

    ⚠ **`generated_by` 를 같이 남기는 까닭** — 응답이 LLM 이 만든 문장인지 규칙 문장(폴백)인지가
    나중에 "그때 왜 이런 답이 나왔나"를 되짚는 유일한 단서다. 지금도 응답 `meta` 로 화면에
    나가는데 그건 그 순간만 남는다.

    ⛔ **이전 대화를 프롬프트에 실을 계획이면 여기가 곧 입력이다.** 요원이 넣은 문장이
    다음 요청의 system 자리에 섞이지 않게 갈라야 한다(프롬프트 주입).
    """

    __tablename__ = "chat_message"

    # 한 대화를 시간 순으로 읽는 질의 하나뿐이라 (conversation_id, id) 로 충분하다.
    __table_args__ = (
        Index("ix_chat_message_conversation_id", "conversation_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("chat_conversation.id", ondelete="CASCADE")
    )
    # `user` 또는 `assistant`. enum 을 안 쓰는 까닭은 값이 둘뿐이고 늘 서버가 적어서다.
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    # 응답 줄에만 찬다. `llm` 또는 `fallback`.
    generated_by: Mapped[str | None] = mapped_column(String(16))
    # 폴백일 때 그 사유(`daily_budget`·`empty_response`·`circuit_open` 등).
    fallback_reason: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
