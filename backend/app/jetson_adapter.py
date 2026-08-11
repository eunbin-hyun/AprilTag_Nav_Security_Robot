"""젯슨 web_bridge 프레임 → 서버 robot_state payload 어댑터 (지라 S15P11C207-338).

젯슨(`hardware/jetson/ros2_ws/src/web_bridge/web_bridge/messages.py`)과 서버
(`robot_channel.handle_robot_frame`)의 WS 계약이 어긋나 있다. 서버는 `type == "robot_state"`에
본문이 `payload`인 프레임만 읽는데, 젯슨은 type을 `status_summary`·`odom`·`imu` 셋으로 갈라
보내고 본문은 `data`에 싣는다. 그래서 어댑터를 붙이기 전엔 젯슨이 올린 프레임이 전부
`unknown_frame_type` 에러로 되돌아가고 상태가 한 건도 안 남았다.

**"서버를 젯슨에 맞춘다"가 확정 방향이다(2026-07-31 사용자 결정).** 젯슨 펌웨어·브리지를
고치지 않고 서버 수신부 앞에 이 어댑터를 세워 세 종류를 robot_state payload로 접는다.
부르는 자리는 `handle_robot_frame` 한 곳뿐이다.

## 세 프레임을 한 상태로 합친다 (⚠ 이 모듈의 핵심)

젯슨은 한 로봇의 상태를 **세 갈래로 쪼개** 따로 올린다(status_summary·odom·imu가 각자
1Hz). 서버 `RobotStateIn`은 반대로 "한 번에 오는 절대 상태"고, 화면(`frontend/index.html`)은
받은 프레임으로 그 로봇 칸을 **통째로 갈아끼운다**. 그래서 쪼갠 프레임을 그대로 접어 올리면
odom 프레임이 IMU를 0으로, imu 프레임이 위치를 0으로 매초 번갈아 지운다(실측 결함 3·4).

그래서 이 모듈은 로봇별 최신값을 들고 있다가 **어느 프레임이 오든 합쳐진 한 장**을 돌려준다.
- 상태 값(배터리·모드·통신)은 마지막 status_summary 값을 계속 싣는다. Robot 행에 이미 같은
  값이 남아 있으니(`_upsert_robot`) 새 사실을 지어내는 게 아니라 같은 사실을 다시 싣는 것이다.
- odom·imu·position도 마지막 값을 계속 싣는다. 그래서 imu 프레임 한 장이 지도 점을 지우지
  않고, odom 프레임 한 장이 통신 신호를 0%로 떨어뜨리지 않는다.
- 인메모리라 서버 재기동으로 사라진다(`RobotPresentation`과 같은 규칙). 다음 세 프레임이면
  다시 찬다. 시험 위생은 `reset_jetson_adapter_state()`가 맡고,
  `robot_channel.reset_robot_channel_state()`가 같이 부른다.

## 접는 규칙

`schemas.py`는 **안 고친다**(정본 `schemas/robot_state.schema.json`과 프론트 TS 타입이 같은
파일을 본다). 그래서 젯슨 칸을 서버 칸에 끼워 맞추는 부담을 전부 여기서 진다.

- 공통 — 젯슨 envelope는 robot_id가 **최상위에만** 있다. 서버 `RobotStateIn.robot_id`는
  payload 안이 필수라 최상위 값을 payload로 복사한다. 앞뒤 공백은 털어서 싣는다 — 검사만
  털고 원본을 실으면 `"  RB1-01  "`이 `"RB1-01"`과 별개 Robot 행으로 갈라진다(실측 결함 7).
- `status_summary.data` → `payload.status_summary`.
  - `battery`는 서버가 `int` 0~100이다. 젯슨은 지금 `None`만 보내는데(STM32 연동 전),
    나중에 실수로 오면 pydantic이 소수점을 거부해 프레임 전체가 깨진다. 반올림·범위 밖 버림을
    여기서 흡수한다.
  - `operation_mode`는 젯슨 쪽이 ROS 파라미터 자유 문자열이고 기본값이 `"unknown"`이다.
    서버 `RobotMode` enum에 맞는 값만 싣고, 아니면 안 싣는다(status_summary는 절대 상태라
    **안 실린 칸은 "보고 안 함"이지 "없음"이 아니다** — `_upsert_robot`이 그 규칙으로 덮는다).
  - ⛔ **`comm_status`는 안 받는다**(2026-08-06). 이름만 같고 뜻이 다르다 — 젯슨은
    `/stm32/connected`를 실어 **STM32 시리얼 링크**를 말하는데 서버 `CommStatus`는
    **로봇↔서버 통신**이다. 그대로 받던 동안 **UART 케이블이 빠지면 화면이 "통신 정지"라
    말하고 명령 버튼까지 잠겼다** — 웹 연결은 멀쩡한데도. 프론트·팀원 교차 검증이 따로
    같은 자리를 짚었다.
    ⭐ 이제 서버가 직접 판정한다 — **이 프레임이 왔다는 사실 자체가 통신이 살아 있다는
    증거**다. 젯슨 원본은 `sensor_health.stm32_link`로 남긴다(버리지 않는다).
  - 서버 스키마에 자리가 없는 `drive_state`·`operation_state`·`camera_status`는
    `payload.sensor_health`에 실어 개발자 텔레메트리로 흘린다.
- `mission_status.data.status` → `payload.mission_status` (F23). 칸 이름이 갈린다 — 젯슨은
  본문 안에서 `status`, 서버는 payload 최상위 `mission_status`다.
  ⚠ **누적 상태로 안 든다.** 나머지 셋과 반대다. 이 값은 절대 상태가 아니라 **에지**라서
  (`robot_channel._apply_mission_edge`), odom·imu 프레임마다 마지막 ARRIVED를 다시 실으면
  프레임마다 에지 판정 질의가 한 번씩 더 돈다. 안 싣는 게 값을 지우지도 않는다 — 서버는
  "안 실린 칸은 보고 안 함"으로 읽어서 화면 타일도 Robot 행도 예전 값을 그대로 든다.
- `odom.data` → `payload.odom` + `payload.status_summary.position {x, y, theta}`.
  서버가 지도에 점을 찍는 값은 `status_summary.position` 한 곳뿐이라
  (`record_presentation` → `_robot_card`), odom만 실으면 개발자 화면엔 뜨는데 관제 지도에는
  로봇이 안 나타난다.
  ⚠ `valid: false`면 position은 안 접는다. 위치는 인메모리 **최신값**이라 한 번 잘못 박히면
  다음 유효 보고가 올 때까지 지도에 그대로 남는다 — 못 믿을 좌표는 아예 안 싣는 쪽이 싸다.
  odom 원본은 그래도 `payload.odom`에 남아서 개발자 화면(`app/static/telemetry.html`의 원문
  토글)에서는 valid 여부까지 보인다.
- `imu.data` → `payload.imu`.

## 원본 dict를 그대로 흘리지 않는다

`odom`·`imu`·`sensor_health`는 서버 스키마가 자유 dict라 pydantic이 뭐가 들었든 통과시키고,
그 값이 대시보드 전원에게 그대로 중계된다. `/ws/robot`은 `ws_require_api_key`가 기본 꺼짐이라
인증 없는 peer도 같은 길을 탄다 — 실측으로 `odom.robot_id="EVIL"`이 화면까지 살아 나갔다
(결함 8). 그래서 계약에 있는 칸만 종류별로 받아 다시 조립한다. 모르는 칸은 버린다.
젯슨이 칸을 늘리면 여기 `_ODOM_NUMBERS` 같은 목록에 이름을 더해야 화면까지 간다.

## 상대 좌표 원점 (결함 5)

젯슨 odom은 `position_type: relative_estimate` / `origin: robot_boot`이다 — **로봇이 부팅한
자리가 (0,0)**인 상대 좌표다. 서버 `RobotPosition`은 평면도 절대 좌표라, 젯슨이 재부팅하면
좌표가 (0,0)으로 되돌아가 지도의 로봇이 원점으로 순간이동한다.

부팅 세션이 바뀐 걸 알아채면(seq가 되돌아가거나 origin 문자열이 바뀌면) **그 순간의 마지막
지도 좌표를 새 세션의 원점으로 잡는다**(`_reanchor`). 그래서 재부팅 뒤에도 로봇이 있던
자리에서 이어 그린다. 진짜 절대 정합(AprilTag·측량 원점 기준)은 이 어댑터가 못 만드는 값이라
팀 결정 대기다 — 지금은 "순간이동을 막는다"까지만 한다.
`payload.odom`에는 젯슨 원본 좌표를 그대로 두고, 보정은 지도 칸(position)에만 건다.

## 칸 하나가 프레임 전체를 못 죽인다 (F24)

접기 규칙 전체가 같은 원칙 하나로 돈다 — **모르는 값은 그 칸만 빼고 나머지는 올린다.**
`_map_mode`·`_map_battery`가 이미 그렇게 굴고, mission_status도 같다.
enum 밖 값이 그대로 오면 `RobotStateIn` 검증이 프레임을 통째로 거절한다 — 같이 실린
배터리·위치까지 버려진다. 그래서 enum 밖 값은 여기서 걸러 내고 `warning` 로그로만 흔적을
남긴다.
⚠ **`EN_ROUTE`는 이제 enum 안이다**(2026-08-06에 열었다 · `schemas.MissionStatus` 넷).
예전에 여기 "enum은 셋뿐"이라 적혀 있었는데 낡은 문장이라 2026-08-07에 고쳤다 —
그 문장을 보고 "젯슨이 EN_ROUTE를 보내면 버려진다"고 판단하면 틀린다.
초안 모듈 `app/robot/mission_state.py`가 적어 둔 설계 결정("낯선 상태값은 무시하고 기존
상태를 유지한다")과 같은 규칙이라 나중에 안건③이 확정돼도 방향이 안 어긋난다.

## 안 접는 것

프레임 모양이 깨졌으면(본문이 dict가 아니거나 robot_id가 없으면) 지금처럼 `error` 봉투로
답한다. 다만 **세 종류가 `unknown_frame_type`으로 빠지는 길은 없다** — 그게 이 작업의 목적이다.

명령 수신(젯슨이 소켓을 안 읽는다)은 젯슨 브리지 몫이라 범위 밖이다. 하트비트는 서버 쪽
절반만 여기 짝이 있다 — 프레임이 왔다는 사실 자체를 살아 있다는 증거로 세는 자리는
`robot_channel.handle_robot_frame`이다(결함 1).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from app.schemas import CommStatus, MissionStatus, RobotMode

logger = logging.getLogger("c207.jetson")

# 젯슨 web_bridge가 보내는 프레임 종류. 이 넷은 절대 unknown_frame_type으로 안 빠진다.
# mission_status는 F23에서 늘렸다 — 계약(`hardware/jetson/ros2_ws/docs/
# jetson_to_web_interface_v2.md` §9)에는 처음부터 있었는데 여기 없어서 도착 알림·ETA가
# 통째로 안 살았다.
JETSON_FRAME_TYPES = frozenset({"status_summary", "odom", "imu", "mission_status"})

# ⚠ **`imu`는 계약에만 있고 실제로는 안 온다**(2026-08-06 실기기 확인). 젯슨 쪽에
# `imu_message()`가 정의돼 있는데 **부르는 자리가 0건**이고, 실주행 로그에도 흔적이 없다.
# 받는 갈래는 그대로 둔다 — 젯슨이 켜는 날 서버를 안 고쳐도 되게. 다만 **"IMU가 온다"를
# 전제로 무언가를 판단하지 마라.** 화면의 IMU 칸이 빈 것은 결함이 아니라 이 사실이다.

# ⛔ 젯슨 comm_status 매핑(`_COMM_STATUS_MAP`)은 2026-08-06에 걷었다. 이름만 같고 뜻이
# 달랐다 — 젯슨은 STM32 시리얼 링크, 서버는 로봇↔서버 통신이다. 그 값은 이제
# `sensor_health.stm32_link`로 가고, 통신 상태는 서버가 "프레임이 왔다"로 직접 판정한다
# (`_apply_status_summary`).

# 깨진 프레임을 돌려줄 때 쓰는 사유. 기존 invalid_robot_state와 같은 결로 맞춘다.
INVALID_JETSON_FRAME = "invalid_jetson_frame"

# 중계 허용 칸 — 종류별로 여기 있는 이름만 다시 조립한다(모듈 머리 "원본 dict를 …" 참고).
# ⭐ `reason`은 2026-08-06에 더했다(팀원 교차 검증). 젯슨이 좌표를 못 실을 때 그 칸에
# 사유를 담아 보내는데(`unavailable_odom_message`) 여기 없어서 통째로 버려졌다 —
# 화면이 "위치 확인 안 됨"만 말하고 **왜 그런지를 못 말했다.** 실패 사유를 살린 것과 같은
# 계열이다(`_read_mission_status`).
_ODOM_TEXTS = ("frame", "position_type", "origin", "reason")
_ODOM_NUMBERS = ("x", "y", "yaw", "linear_x", "angular_z")
_IMU_NUMBERS = ("yaw_rate", "linear_accel_x")
_OPERATION_STATE_KEYS = ("checkpoint", "segment", "direction", "phase", "description")

# 화면이 읽을 이름 ← 젯슨 이름. 젯슨 원본 이름도 같이 남겨서 개발자 화면에서 원문 대조가
# 끊기지 않게 한다.
#
# ⚠ **지금 이 넷을 읽는 자리가 화면에 0건이다**(2026-08-06 전수 확인 — `theta`·`linear_vel`·
# `angular_vel`·`acc_x` 모두 `frontend/index.html`에 없다). 예전 주석은 "안 실으면 화면 칸이
# 0으로 고정된다"고 적었는데 **그 전제가 지금은 사실이 아니다.**
#
# ⏸ 그래도 안 걷는다 — 값이 몇 바이트고 화면이 언제든 쓸 수 있는데, 걷었다가 다시 넣으면
# 그 사이에 붙인 화면 코드가 조용히 0을 읽는다. **"쓰는 자리가 생기면 그때 이 주석을 지워라."**
_ODOM_ALIASES = {"theta": "yaw", "linear_vel": "linear_x", "angular_vel": "angular_z"}
_IMU_ALIASES = {"acc_x": "linear_accel_x"}

# 자유 문자열 칸의 길이 상한. 화면 카드 한 줄에 들어갈 만큼만 받는다.
# ⚠ **화면 폭이 근거라서 못 올린다.** `odom`의 `frame`·`position_type`·`origin`·`reason`이
# 이 값을 쓰는데 그 넷은 카드에 한 줄로 그려진다.
_TEXT_MAX_LEN = 64

# 주행 실패 사유(`mission_status.reason`)만 따로 길게 받는다.
# ⭐ **2026-08-07 신설.** 젯슨이 사유를 문장으로 조립해 보내기 시작했다 — "태그를 한 번도
# 못 봤다 (단계 3/5 태그 4 코너 진입, 목표 태그 4, 주행 2.42 m)"가 **53자**라 64까지 여유가
# 11자뿐이었다. 태그 번호가 두 자리가 되거나 단계 이름이 길어지면 뒤가 소리 없이 날아가고,
# **잘린 문장은 잘린 줄도 모르고 읽힌다.**
# ⚠ **`_TEXT_MAX_LEN`을 통째로 올리면 안 된다**(2026-08-07에 그렇게 했다가 시험이 잡았다).
# 저쪽은 화면 카드 폭이 근거고 이쪽은 되짚기가 목적이라 근거가 서로 다르다.
# ⚠ 이 값이 가는 자리는 화면 카드가 아니라 `payload["log"]` → `robot_status_log.log`(0017)다.
# DB 칸은 `sa.Text`라 길이 제한이 없다 — 자르는 자리는 여기 하나뿐이다.
_REASON_MAX_LEN = 200
# 로봇별 누적 상태를 몇 대까지 들고 있나. `/ws/robot`이 인증 없이 열려 있어서 상한이 없으면
# 아무 peer나 robot_id를 바꿔 가며 올려 메모리를 채울 수 있다(가장 오래 안 온 로봇부터 버린다).
MAX_TRACKED_ROBOTS = 64


@dataclass
class AdaptResult:
    """접기 결과. 둘 중 하나만 채워진다."""
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


def is_jetson_frame(frame: Any) -> bool:
    """이 프레임이 젯슨 web_bridge가 보낸 세 종류 중 하나인가."""
    return isinstance(frame, dict) and frame.get("type") in JETSON_FRAME_TYPES


# ── 값 거르기 ──────────────────────────────────────────────────────────────

def _number(value: Any) -> float | None:
    """실수로 쓸 수 있는 값만 통과시킨다.

    bool은 `isinstance(True, int)`가 참이라 그냥 두면 좌표 자리에 1.0으로 들어앉는다.
    NaN·inf는 JSON으로 못 나가서(브로드캐스트가 통째로 터진다) 여기서 막는다.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _text(value: Any, limit: int = _TEXT_MAX_LEN) -> str | None:
    """자유 문자열 칸. 길이만 자르고 내용은 안 따진다(현장 값 표기를 서버가 못 정한다).

    ⚠ `limit`을 넘기는 자리는 주행 실패 사유 하나뿐이다(`_read_mission_status`). 화면 카드에
    그려지는 칸은 기본값을 그대로 쓴다 — 근거가 다르다(`_REASON_MAX_LEN` 주석).
    """
    if not isinstance(value, str):
        return None
    return value[:limit]


def _flag(value: Any) -> bool | None:
    """bool만 받는다. `valid`가 "true"·1로 오면 판정이 흐려지니 아예 안 싣는다."""
    return value if isinstance(value, bool) else None


def _map_battery(value: Any) -> int | None:
    """젯슨 battery → 서버 `int` 0~100. 못 쓰는 값은 안 싣는다(원본은 sensor_health에 남는다)."""
    number = _number(value)
    if number is None:
        return None
    battery = round(number)
    return battery if 0 <= battery <= 100 else None


def _map_mode(value: Any) -> str | None:
    """젯슨 operation_mode(자유 문자열) → 서버 RobotMode. 모르는 값이면 안 싣는다."""
    if not isinstance(value, str):
        return None
    try:
        return RobotMode(value.strip().upper()).value
    except ValueError:
        return None


def _map_mission_status(value: Any) -> str | None:
    """젯슨 mission_status.data.status → 서버 MissionStatus. 모르는 값이면 안 싣는다(F24).

    enum을 늘리는 게 아니라 **그 칸만 버리는** 쪽이다. 계약 밖 낱말이 실제로 오면 여기서
    안 막을 경우 `RobotStateIn` 검증이 배터리·위치까지 실린 프레임을 통째로 거절한다
    (모듈 머리 F24).

    ⚠ **`EN_ROUTE`는 여기서 안 버린다** — 2026-08-06에 enum에 넣었다(`MissionStatus` 넷).
    젯슨은 2026-08-07부터 `RUNNING`을 `EN_ROUTE`로 올린다. 예전에 이 자리에 "젯슨 숙제
    문서가 EN_ROUTE 송신을 막아 뒀다"고 적혀 있었는데 낡은 문장이라 지웠다.
    """
    if not isinstance(value, str):
        return None
    try:
        return MissionStatus(value.strip().upper()).value
    except ValueError:
        return None


def _wrap_angle(radian: float) -> float:
    """각도를 -π~π로 접는다. 원점 보정을 여러 번 걸어도 값이 안 불어나게 한다."""
    return math.atan2(math.sin(radian), math.cos(radian))


# ── 프레임별 정규화 (계약에 있는 칸만 다시 조립) ────────────────────────────

def _clean_odom(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _ODOM_TEXTS:
        value = _text(data.get(key))
        if value is not None:
            out[key] = value
    for key in _ODOM_NUMBERS:
        value = _number(data.get(key))
        if value is not None:
            out[key] = value
    valid = _flag(data.get("valid"))
    if valid is not None:
        out["valid"] = valid
    for alias, source in _ODOM_ALIASES.items():
        if source in out:
            out[alias] = out[source]
    _log_dropped("odom", data, out)
    return out


def _clean_imu(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _IMU_NUMBERS:
        value = _number(data.get(key))
        if value is not None:
            out[key] = value
    valid = _flag(data.get("valid"))
    if valid is not None:
        out["valid"] = valid
    for alias, source in _IMU_ALIASES.items():
        if source in out:
            out[alias] = out[source]
    _log_dropped("imu", data, out)
    return out


def _clean_operation_state(value: Any) -> dict[str, str] | None:
    """젯슨 operation_state(경로 진행 상황). 다섯 칸 다 자유 문자열이다."""
    if not isinstance(value, dict):
        return None
    out = {}
    for key in _OPERATION_STATE_KEYS:
        text = _text(value.get(key))
        if text is not None:
            out[key] = text
    return out or None


def _log_dropped(kind: str, data: dict[str, Any], kept: dict[str, Any]) -> None:
    """계약에 없어서 버린 칸을 로그로만 남긴다 — 중계 메시지에는 안 싣는다.

    버린 이름을 payload에 되싣으면 결함 8을 그대로 되살린다(보낸 쪽이 정하는 문자열이
    대시보드까지 간다). 이름을 알아야 하는 사람은 서버 로그를 본다.
    """
    extra = [k for k in data if k not in kept]
    if extra:
        logger.debug("젯슨 %s 프레임에서 계약에 없는 칸 %d개를 버렸다: %s", kind, len(extra), extra)


# ── 로봇별 누적 상태 ───────────────────────────────────────────────────────

@dataclass
class JetsonRobotState:
    """한 로봇의 최신값. 세 프레임을 한 장으로 합치는 근거다(모듈 머리 참고)."""
    summary: dict[str, Any] = field(default_factory=dict)   # battery·mode·comm_status
    health: dict[str, Any] = field(default_factory=dict)    # sensor_health로 나갈 칸
    position: dict[str, Any] | None = None                  # 지도 좌표(원점 보정 뒤)
    odom: dict[str, Any] | None = None
    imu: dict[str, Any] | None = None
    # 원점 보정 상태 — 부팅 세션이 바뀐 걸 알아채는 근거와, 그때 잡은 오프셋.
    last_seq: int | None = None
    origin: str | None = None
    offset_x: float = 0.0
    offset_y: float = 0.0
    offset_theta: float = 0.0
    # ⛔ 재부팅을 알아챘는데 **아직 재정박을 못 건** 상태(2026-08-06).
    #
    # 예전에는 `last_seq`를 조기 반환보다 먼저 갱신했다. 그래서 재부팅 직후 첫 프레임이
    # 좌표를 못 실은 것(`position_type: unavailable`·`valid: false`)이면, 재정박은 건너뛰는데
    # **신호만 소비돼서** 다음 프레임부터는 seq가 늘어 재부팅으로 안 보였다. 재정박 기회를
    # 영영 잃고 지도의 로봇이 (0,0) 근처로 순간이동했다.
    #
    # ⚠ 젯슨이 실제로 그 갈래를 탄다 — `0x83` IMU를 못 쓰면 `/stm32/odom`을 아예 안 내고
    # `unavailable` 프레임만 보낸다(젯슨 회신 §4). 재부팅과 겹치기 쉬운 자리다.
    pending_reanchor: bool = False


_states: dict[str, JetsonRobotState] = {}


def reset_jetson_adapter_state() -> None:
    """누적 상태를 비운다(시험 위생 · "서버 재기동"과 같은 자리).

    `robot_channel.reset_robot_channel_state()`가 같이 부른다 — 인메모리 상태를 비우는 훅이
    두 곳으로 갈라지면 한쪽만 부르는 시험에서 앞 케이스 값이 샌다.
    """
    _states.clear()


def _state_for(robot_id: str) -> JetsonRobotState:
    """그 로봇의 누적 상태(없으면 만든다). 오래된 로봇부터 버려 개수를 묶는다."""
    state = _states.pop(robot_id, None)
    if state is None:
        state = JetsonRobotState()
        while len(_states) >= MAX_TRACKED_ROBOTS:
            oldest, _ = next(iter(_states.items()))
            del _states[oldest]
            logger.info("젯슨 누적 상태 상한(%d) 초과 — %s 몫을 버렸다", MAX_TRACKED_ROBOTS, oldest)
    _states[robot_id] = state   # 다시 넣어 "가장 최근에 온 로봇"을 뒤로 민다
    return state


# ── 프레임별 접기 ──────────────────────────────────────────────────────────

def _apply_status_summary(state: JetsonRobotState, data: dict[str, Any]) -> None:
    """status_summary 프레임 하나를 누적 상태에 반영한다.

    매핑에 실패한 원본값(`*_raw`)도 sensor_health에 남긴다. 안 그러면 "왜 모드가 비었나"를
    화면에서 되짚을 근거가 서버 로그 말고는 없다. 반대로 매핑에 성공하면 그 자리를 지운다 —
    누적 상태라 안 지우면 옛 실패 흔적이 계속 따라다닌다.
    """
    battery = _map_battery(data.get("battery"))
    mode = _map_mode(data.get("operation_mode"))

    # ⛔ **젯슨 `comm_status`는 통신 상태 칸에 안 싣는다**(2026-08-06 · 프론트 40·42차 ·
    # 팀원 교차 검증 · 셋이 따로 같은 자리를 짚었다).
    #
    # 이름만 같고 뜻이 다르다. 젯슨은 `/stm32/connected` 구독값을 실어서 **STM32 시리얼
    # 링크**를 말하는데, 서버 `CommStatus`는 **로봇↔서버 통신**이다("소켓 close가 아니라
    # 하트비트 연속 미스로 판정한다"). 그래서 **UART 케이블이 빠지면 화면이 "통신 정지"라
    # 말하고 명령 버튼까지 잠겼다** — 정작 웹 연결은 멀쩡한데도.
    #
    # ⭐ **서버가 직접 판정한다.** 이 프레임이 도착했다는 사실 자체가 로봇↔서버 통신이
    # 살아 있다는 증거다(`handle_robot_frame`이 같은 근거로 하트비트를 센다). 지어내는
    # 값이 아니라 서버가 확실히 아는 사실이라, 젯슨 말을 들을 이유가 없다.
    #
    # ⚠ 끊기면 이 값이 갱신을 멈춘다 — 화면은 `updated_at` 신선도로 그걸 읽는다.
    # ⚠ 젯슨 원본은 아래에서 `sensor_health.stm32_link`로 남긴다. 버리지 않는다.
    for key, value in (
        ("battery", battery),
        ("mode", mode),
        ("comm_status", CommStatus.WS_OK.value),
    ):
        if value is not None:
            state.summary[key] = value

    # ⭐ 젯슨이 말한 STM32 링크는 개발자 텔레메트리로 흘린다. 이름을 갈라야 두 뜻이 안 섞인다.
    stm32_link = _text(data.get("comm_status"))
    if stm32_link is not None:
        state.health["stm32_link"] = stm32_link

    state.health["drive_state"] = _text(data.get("drive_state"))
    state.health["camera_status"] = _text(data.get("camera_status"))
    state.health["operation_state"] = _clean_operation_state(data.get("operation_state"))
    for key in ("drive_state", "camera_status", "operation_state"):
        if state.health[key] is None:
            del state.health[key]

    # ⚠ `comm_status`는 여기 없다 — 이제 매핑을 안 하고 원본을 `stm32_link`로 그대로 남긴다.
    # 매핑 실패 흔적(`*_raw`)을 남기는 자리라, 매핑 자체가 없으면 적을 것도 없다.
    raws = (
        ("operation_mode_raw", "operation_mode", mode),
        ("battery_raw", "battery", battery),
    )
    for raw_key, source_key, mapped in raws:
        state.health.pop(raw_key, None)
        original = data.get(source_key)
        if mapped is None and original is not None:
            state.health[raw_key] = _text(original) if isinstance(original, str) else original


def _reanchor(state: JetsonRobotState, data: dict[str, Any], seq: Any) -> None:
    """부팅 세션이 바뀌었으면 지금 있는 자리를 새 원점으로 잡는다(모듈 머리 "상대 좌표 원점").

    바뀐 걸 아는 근거가 둘이다 — seq가 되돌아갔거나(젯슨은 부팅마다 0부터 센다), origin
    문자열이 달라졌거나. 절대 좌표라고 밝힌 프레임(`position_type`이 relative가 아님)에는
    보정을 안 건다 — 그때는 젯슨이 이미 공통 원점을 쓴다는 뜻이다.
    """
    origin = _text(data.get("origin"))
    position_type = (_text(data.get("position_type")) or "").lower()
    seq_number = _number(seq)
    rebooted = (
        state.last_seq is not None
        and seq_number is not None
        and seq_number < state.last_seq
    ) or (state.origin is not None and origin != state.origin)

    if seq_number is not None:
        state.last_seq = int(seq_number)
    state.origin = origin

    # ⛔ **재부팅을 알아챈 사실을 따로 기억한다**(2026-08-06 · 팀원 교차 검증에서 잡힘).
    # 예전에는 여기서 그냥 돌아갔는데, `last_seq`는 위에서 이미 갱신돼 있어서 **신호만
    # 소비되고 재정박은 영영 못 걸었다.** 재부팅 직후 첫 프레임이 좌표를 못 실으면
    # (`unavailable`·`valid: false`) 다음 프레임부터는 seq가 늘어 재부팅으로 안 보인다.
    if rebooted:
        state.pending_reanchor = True

    # 절대 좌표라고 밝힌 프레임에는 보정을 안 건다 — 젯슨이 이미 공통 원점을 쓴다는 뜻이다.
    if not position_type.startswith("relative"):
        return
    if not state.pending_reanchor:
        return
    x, y = _number(data.get("x")), _number(data.get("y"))
    if x is None or y is None or state.position is None:
        # ⚠ 좌표를 못 실은 프레임이다. 깃발을 **그대로 든 채** 다음 유효 프레임을 기다린다.
        return
    state.pending_reanchor = False
    state.offset_x = state.position.get("x", 0.0) - x
    state.offset_y = state.position.get("y", 0.0) - y
    yaw = _number(data.get("yaw"))
    if yaw is not None and "theta" in state.position:
        state.offset_theta = _wrap_angle(state.position["theta"] - yaw)
    state.health["odom_reanchored"] = {
        "x": state.offset_x, "y": state.offset_y, "theta": state.offset_theta,
    }
    logger.info(
        "젯슨 부팅 세션이 바뀌어 지도 원점을 다시 잡았다 (offset x=%.3f y=%.3f)",
        state.offset_x, state.offset_y,
    )


def _apply_odom(state: JetsonRobotState, data: dict[str, Any], seq: Any) -> None:
    """odom 프레임 하나를 누적 상태에 반영한다(원본 + 지도 좌표)."""
    _reanchor(state, data, seq)
    odom = _clean_odom(data)
    state.odom = odom

    # valid가 명시로 거짓이면 좌표를 안 믿는다. 위치는 인메모리 최신값이라 한 번 박히면 남는다.
    if odom.get("valid") is False:
        return
    x, y = odom.get("x"), odom.get("y")
    if x is None or y is None:
        return
    position: dict[str, Any] = {"x": x + state.offset_x, "y": y + state.offset_y}
    yaw = odom.get("yaw")
    # theta는 정본 robot_state.schema.json에서 `"type": "number"`다(null 허용이 아니다).
    # 못 읽은 yaw를 null로 채우면 pydantic은 통과해도 스키마 계약이 깨진다 — 아예 안 싣는다.
    if yaw is not None:
        position["theta"] = _wrap_angle(yaw + state.offset_theta)
    elif state.position is not None and "theta" in state.position:
        position["theta"] = state.position["theta"]
    state.position = position


def _read_mission_status(data: dict[str, Any]) -> tuple[str | None, str | None]:
    """mission_status 프레임에서 상태값과 **사유**를 뽑는다(F23). 없으면 (None, None).

    누적 상태에 안 든다(모듈 머리 "접는 규칙") — 그래서 `state`를 안 받는다.
    돌려준 값은 이번 payload에만 실린다.
    모르는 값은 `warning`으로 남긴다 — 젯슨이 계약 밖 낱말을 쓰기 시작한 걸 알아채는
    자리가 여기뿐이라, 조용히 버리면 "도착 알림이 안 뜬다"까지 가서야 드러난다.

    ⭐ **`reason`을 2026-08-06부터 받는다**(젯슨 회신 §3). 그날까지는 뽑고도 버렸다 —
    담을 자리가 없었기 때문이다. 주행 기록 표를 살리면서(마이그레이션 0017) `log` 칸이
    생겼고, 이제 그 값이 **그 시각의 위치·IMU와 한 행에 남는다.**

    ⚠ 이게 그 표를 살린 이유다 — 8/6 주행 실패 5건이 **사유가 하나도 안 남아** 왜 멈췄는지
    못 봤다. 젯슨은 `FAULT_OVERRUN — 태그 2를 못 봤다` 같은 글자를 이미 싣고 있었다.
    """
    raw = data.get("status")
    mission_status = _map_mission_status(raw)
    if mission_status is None:
        logger.warning(
            "젯슨 mission_status 값 %r은 서버 enum(%s) 밖이라 그 칸만 버렸다 —"
            " 같이 실린 배터리·위치는 그대로 올린다.",
            raw, [s.value for s in MissionStatus],
        )
    # ⚠ 여기만 상한이 다르다(`_REASON_MAX_LEN` 200). 젯슨이 단계·목표 태그·주행거리를 넣어
    # 문장으로 조립해 보내는데 화면 카드 폭(64)으로 자르면 뒤가 소리 없이 날아간다.
    reason = _text(data.get("reason"), _REASON_MAX_LEN)
    # ⚠ 상태를 못 읽어도 사유는 살린다. "모르는 상태로 멈췄다"가 되짚을 때 제일 값진 기록이다.
    _log_dropped("mission_status", data, {"status": raw, "reason": data.get("reason")})
    return mission_status, reason


def _merged_payload(
    robot_id: str,
    state: JetsonRobotState,
    mission_status: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """누적 상태 한 벌을 서버 robot_state payload로 편다(세 프레임이 한 장으로 합쳐진다).

    `mission_status`와 `reason`만 누적이 아니라 이번 프레임 값이다 — 에지라서 그렇다(모듈 머리).
    """
    payload: dict[str, Any] = {"robot_id": robot_id}
    if mission_status is not None:
        payload["mission_status"] = mission_status
    # ⭐ 젯슨이 실은 사유를 계약의 `log` 칸으로 옮긴다(2026-08-06). 이름이 다른 까닭은
    # 계약이 먼저 `log`로 서 있었고 젯슨 프레임이 `reason`으로 왔기 때문이다 — 가운데서
    # 한 번만 옮기고, 서버 안쪽은 `log` 한 이름으로만 흐른다.
    if reason is not None:
        payload["log"] = reason
    summary = dict(state.summary)
    if state.position is not None:
        summary["position"] = dict(state.position)
    if summary:
        payload["status_summary"] = summary
    if state.health:
        payload["sensor_health"] = dict(state.health)
    if state.odom is not None:
        payload["odom"] = dict(state.odom)
    if state.imu is not None:
        payload["imu"] = dict(state.imu)
    return payload


def adapt_jetson_frame(frame: dict[str, Any]) -> AdaptResult:
    """젯슨 프레임 하나를 서버 `robot_state` payload로 접는다.

    부르기 전에 `is_jetson_frame`으로 걸러야 한다. 돌려주는 payload는 그대로
    `RobotStateIn.model_validate`에 넘길 수 있는 dict고, 이번 프레임만이 아니라 그 로봇의
    **최신 상태 한 벌**이 실린다(모듈 머리 "세 프레임을 한 상태로 합친다").
    """
    frame_type = frame.get("type")
    robot_id = frame.get("robot_id")
    if not isinstance(robot_id, str) or not robot_id.strip():
        return AdaptResult(
            error={
                "reason": INVALID_JETSON_FRAME,
                "type": frame_type,
                "detail": ["robot_id가 최상위에 없다"],
            }
        )
    # 검사만 털고 원본을 실으면 공백이 붙은 이름이 별개 Robot 행으로 갈라진다.
    robot_id = robot_id.strip()

    data = frame.get("data")
    if not isinstance(data, dict):
        return AdaptResult(
            error={
                "reason": INVALID_JETSON_FRAME,
                "type": frame_type,
                "detail": ["data가 객체가 아니다"],
            }
        )

    state = _state_for(robot_id)
    mission_status: str | None = None
    mission_reason: str | None = None
    if frame_type == "status_summary":
        _apply_status_summary(state, data)
    elif frame_type == "odom":
        _apply_odom(state, data, frame.get("seq"))
    elif frame_type == "mission_status":
        mission_status, mission_reason = _read_mission_status(data)
    else:  # imu — is_jetson_frame이 앞에서 네 종류로 좁혀 준다.
        state.imu = _clean_imu(data)

    # 이번 프레임이 무엇이었나는 개발자 화면이 순서를 읽는 근거라 매번 갈아 끼운다.
    state.health["source"] = "jetson"
    state.health["frame_type"] = frame_type
    for key in ("seq", "timestamp"):
        value = frame.get(key)
        state.health.pop(key, None)
        if isinstance(value, str):
            state.health[key] = _text(value)
        elif _number(value) is not None:
            state.health[key] = value

    return AdaptResult(
        payload=_merged_payload(robot_id, state, mission_status, mission_reason)
    )
