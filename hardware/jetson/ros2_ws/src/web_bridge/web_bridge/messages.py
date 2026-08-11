"""웹 관제 WebSocket 메시지 생성기."""
from datetime import datetime, timezone


DRIVE_STATES = {
    0: 'BOOT', 1: 'SELF_TEST', 2: 'READY', 3: 'DRIVING',
    4: 'SAFE_STOP', 5: 'FAULT', 6: 'ESTOP',
}
COMMAND_TYPES = {
    'cmd_destination', 'return_to_charge', 'emergency_stop',
}

# `/route/state` 의 state → 서버 mission_status (젯슨_웹_협의 §3 표).
# 여기 없는 상태(IDLE·HOLD)는 "보낼 값이 없음" 이라 아예 안 보낸다.
#
# ⚠ HOLD 를 EN_ROUTE 로 옮기지 마라. 서버 enum 에 "장애물 앞에 서 있다" 를 담을
# 낱말이 없어서, 옮기면 멈춰 있는 로봇이 화면에 "이동 중" 으로 뜬다. 비워 두면
# HOLD 에 들었다 나올 때 EN_ROUTE 가 다시 한 장 나가는데, 그것이 실제로 일어난
# 일이다 (초음파가 순간 끊기면 그만큼 반복된다 — 에지라 초당 두 장을 넘지 않는다).
MISSION_STATUS_BY_ROUTE_STATE = {
    'RUNNING': 'EN_ROUTE',
    'ARRIVED': 'ARRIVED',
    'DOCKED': 'RETURN_COMPLETE',
    'FAULT': 'FAILED',
}

# 그 상태를 알린 지점 (협의 §3 checkpoint 표: A=충전스테이션, C=도착지).
# ⚠ EN_ROUTE·FAILED 에는 넣지 않는다. 이 표는 낱말이 A·C 둘뿐인 enum 이고 "가는
# 중" 도 "멈춘 자리" 도 그 둘 중 하나가 아니다. 없는 낱말을 지어 넣으면 서버가
# 도착·복귀로 잘못 짝짓는다. 멈춘 자리는 `failure_reason()` 이 글로 싣는다.
MISSION_CHECKPOINTS = {'ARRIVED': 'C', 'RETURN_COMPLETE': 'A'}


def utc_timestamp():
    """현재 UTC 시각을 ISO 8601 문자열로 반환한다."""
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace(
        '+00:00', 'Z')


def envelope(message_type, robot_id, seq, data):
    """공통 WebSocket envelope를 만든다."""
    return {
        'type': message_type, 'robot_id': robot_id,
        'timestamp': utc_timestamp(), 'seq': seq, 'data': data,
    }


def decode_server_message(payload):
    """서버 JSON을 검증하고 ``(종류, 내용)`` 튜플로 반환한다."""
    import json

    try:
        message = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as error:
        raise ValueError('invalid JSON') from error
    if not isinstance(message, dict):
        raise ValueError('message must be a JSON object')

    message_type = message.get('type')
    if message_type == 'ping':
        return 'ping', {}
    if message_type != 'command':
        raise ValueError(f'unsupported message type: {message_type!r}')

    command = message.get('data')
    if not isinstance(command, dict):
        raise ValueError('command data must be an object')
    command_id = command.get('command_id')
    command_type = command.get('type')
    ttl_ms = command.get('ttl_ms')
    if not isinstance(command_id, str) or not command_id:
        raise ValueError('command_id must be a non-empty string')
    if command_type not in COMMAND_TYPES:
        raise ValueError(f'unsupported command type: {command_type!r}')
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
        raise ValueError('ttl_ms must be a positive integer')
    return 'command', command


def pong_message():
    """서버의 애플리케이션 레벨 ping 응답을 만든다."""
    return {'type': 'pong', 'data': {}}


def command_ack_message(command_id, accepted, reason):
    """ROS 서비스의 실제 처리 결과를 서버 명령 응답으로 만든다."""
    return {
        'type': 'command_ack',
        'version': '1',
        'data': {
            'command_id': command_id,
            'result': 'accepted' if accepted else 'rejected',
            'reason': reason,
        },
    }


def mission_status_from_route_state(state):
    """`/route/state` 의 state 를 서버 mission_status 로 옮긴다. 옮길 값이 없으면 None."""
    if not isinstance(state, str):
        return None
    return MISSION_STATUS_BY_ROUTE_STATE.get(state.strip().upper())


def failure_reason(route):
    """`/route/state` 에서 실패 사유 + **멈춘 자리**를 한 줄로 짓는다.

    서버가 `reason` 을 그 시각의 좌표·IMU 와 한 행에 남기므로, 사유만 보내면
    "왜" 는 알아도 "어디서" 를 되짚을 수 없다. checkpoint 표에는 실패를 담을
    낱말이 없어서(A·C 둘뿐) 글로 싣는다.

    ROS 를 안 쓰는 순수 함수로 둔다 — 문자열 조립은 틀려도 예외가 안 나는 종류라
    시험으로 고정해야 하는 자리다.
    """
    reason = route.get('fault_reason') or '주행 실패'
    where = []
    name, index, total = (route.get('step_name'), route.get('step_index'),
                          route.get('step_total'))
    if name:
        # step_index 는 0 기반이다 (route_logic.status). 사람이 읽는 자리라 +1.
        if _is_number(index) and _is_number(total):
            where.append(f'단계 {int(index) + 1}/{int(total)} {name}')
        else:
            where.append(f'단계 {name}')
    tag_id = route.get('target_tag_id')
    if _is_number(tag_id) and tag_id:
        # 0 은 "이 단계에 목표 태그가 없다" 는 뜻이라 싣지 않는다.
        where.append(f'목표 태그 {int(tag_id)}')
    traveled = route.get('traveled_m')
    if _is_number(traveled):
        where.append(f'주행 {float(traveled):.2f} m')
    return f"{reason} ({', '.join(where)})" if where else reason


def mission_status_message(robot_id, seq, status, reason=None):
    """미션 상태 보고 메시지를 만든다 (인터페이스 v2 §9).

    서버는 이 값으로 도착 알림·셔틀 신호 짝짓기·ETA 표본을 만든다. 이 생성기가 없어서
    관제 화면에 ARRIVED 도 ETA 도 한 번도 안 떴다.
    """
    data = {'status': status}
    checkpoint = MISSION_CHECKPOINTS.get(status)
    if checkpoint is not None:
        data['checkpoint'] = checkpoint
    if reason:
        data['reason'] = reason
    return envelope('mission_status', robot_id, seq, data)


def status_summary(robot_id, seq, connected, telemetry, operation_mode):
    """보안요원 화면용 통합 상태 메시지를 만든다."""
    drive_state = 'UNKNOWN'
    if telemetry is not None:
        drive_state = DRIVE_STATES.get(telemetry.get('drive_state'), 'UNKNOWN')
    return envelope('status_summary', robot_id, seq, {
        # ⛔ **여기를 실측값 읽기로 바꾸지 마라.** 2026-08-04 사용자 확정이다
        # (`docs/HANDOFF_2026-08-04_프론트시안_사이클.md` §9-3 "배터리는 시연 표시다 —
        # 되돌리지 마라"). 라즈베리파이와 젯슨에 보조배터리를 각각 물려서 쓰기 때문에
        # **로봇이 잔량을 읽을 수단이 아예 없다.** 실측 원본이 안 생기는 칸이라 화면이
        # 시연 표시로 그리고, 서버가 진짜 잔량을 싣게 되는 날 그 값이 시연값을 이기게
        # 화면에 이미 짜 뒀다. 여기서 `telemetry.get('battery')` 를 읽으면 STM32
        # TELEMETRY_DRIVE(0x80, 26바이트)에 칸 자체가 없어 늘 None 이고, 그 None 이
        # 시연 표시를 덮어 로봇 카드가 영영 빈다.
        # ⚠ 2026-08-05에 잔여 결함 수리(D5)가 이 자리를 실측 읽기로 바꿨다가 되돌렸다.
        'battery': None,
        'operation_mode': operation_mode,
        'drive_state': drive_state,
        'comm_status': 'connected' if connected else 'disconnected',
        'operation_state': {
            'checkpoint': 'UNKNOWN', 'segment': 'UNKNOWN',
            'direction': 'UNKNOWN', 'phase': 'UNKNOWN',
            'description': '경로 상태 미연동',
        },
        'camera_status': 'unavailable',
    })


def odom_message(robot_id, seq, msg):
    """nav_msgs/Odometry를 개발자용 상대 위치 메시지로 변환한다."""
    position = msg.pose.pose.position
    orientation = msg.pose.pose.orientation
    return envelope('odom', robot_id, seq, {
        'frame': msg.header.frame_id or 'odom',
        'position_type': 'relative_estimate',
        'x': position.x, 'y': position.y,
        'yaw': _yaw_from_quaternion(orientation.x, orientation.y,
                                    orientation.z, orientation.w),
        'linear_x': msg.twist.twist.linear.x,
        'angular_z': msg.twist.twist.angular.z,
        'valid': True,
    })


def unavailable_odom_message(robot_id, seq, reason):
    """오도메트리 미수신 또는 timeout 상태를 만든다."""
    return envelope('odom', robot_id, seq, {
        'frame': 'odom',
        'position_type': 'unavailable',
        'x': None, 'y': None, 'yaw': None,
        'linear_x': None, 'angular_z': None,
        'valid': False, 'reason': reason,
    })


def tag_target_message(robot_id, seq, target, valid=True, reason=None,
                       front_overhang_m=None):
    """Apriltag 검출값을 지도 위치 보정용 메시지로 변환한다.

    `front_overhang_m` 은 `along` 의 **기준면**이다. `along` 은 뒷차축 기준이라
    앞머리까지의 거리는 `along - front_overhang_m` 이다. 이 값이 없으면 화면이 두
    기준 중 어느 것으로 그리는지 알 수 없어 태그와의 여유가 그만큼 틀린다.
    모르면(=route_runner 미수신) 아예 넣지 않는다 — 0 으로 채우면 앞머리 기준을
    뒷차축 기준과 구분할 수 없다.
    """
    data = {
        'tag_id': target['tag_id'],
        'along': target.get('along') if valid else None,
        'cross_track': target.get('cross_track') if valid else None,
        'heading_error': target.get('heading_error') if valid else None,
        'valid': valid,
    }
    if reason is not None:
        data['reason'] = reason
    if front_overhang_m is not None:
        data['front_overhang_m'] = front_overhang_m
    return envelope('tag_target', robot_id, seq, data)


def imu_message(robot_id, seq, msg):
    """sensor_msgs/Imu를 개발자용 IMU 메시지로 변환한다."""
    return envelope('imu', robot_id, seq, {
        'yaw_rate': msg.angular_velocity.z,
        'linear_accel_x': msg.linear_acceleration.x,
        'valid': True,
    })


def _is_number(value):
    """숫자인가. bool 을 뺀다 — `True` 는 `int` 라서 1 로 실려 나간다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _yaw_from_quaternion(x, y, z, w):
    """쿼터니언에서 Z축 yaw(rad)를 계산한다."""
    import math
    return math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
