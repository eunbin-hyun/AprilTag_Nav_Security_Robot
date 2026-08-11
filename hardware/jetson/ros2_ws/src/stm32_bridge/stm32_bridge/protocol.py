"""UART V2 frame protocol (순수 Python, ROS import 금지).

명세: Jetson-STM32 주행 제어 UART 연동 명세 V2 §4, §5, §6, §7.
"""
import struct

SOF = b'\xAA\x55'
VERSION = 0x02
MAX_PAYLOAD = 64

# ── Message IDs (명세 §5) ──────────────────────────────────
CMD_DRIVE = 0x10
CMD_STOP = 0x11
CMD_RESET_FAULT = 0x12
TELEMETRY_DRIVE = 0x80
FAULT_EVENT = 0x81
COMMAND_RESULT = 0x82
# 0x83 은 STM32 파트 예약이며 payload 가 아직 확정되지 않았다 (V3 §7 "미구현").
# BNO085 를 STM32 측에 UART-RVC 로 장착하기로 확정됐으므로(2026-07-31) 곧
# 채워진다. FIXED_LENGTHS 에 넣지 않는 이유는 길이를 모르기 때문이다 — 넣으면
# 2026-08-02 BNO085 통합으로 38 byte 확정. FIXED_LENGTHS 에 등록한다.
TELEMETRY_IMU = 0x83
TELEMETRY_RANGE = 0x84
TELEMETRY_ODOMETRY = 0x85
DIAG_ECHO_REQUEST = 0xF0
DIAG_ECHO_RESPONSE = 0xF1

# 고정 payload 길이 (명세 §5: 다르면 반드시 폐기)
FIXED_LENGTHS = {
    CMD_DRIVE: 5,
    CMD_STOP: 1,
    CMD_RESET_FAULT: 4,
    TELEMETRY_DRIVE: 26,
    FAULT_EVENT: 14,
    COMMAND_RESULT: 4,
    TELEMETRY_IMU: 38,
    TELEMETRY_RANGE: 13,
    TELEMETRY_ODOMETRY: 36,
}

# ── CMD_STOP reason (명세 §6.2) ────────────────────────────
STOP_OPERATOR = 0
STOP_MISSION_COMPLETE = 1
STOP_OBSTACLE = 2
STOP_REMOTE_REQUEST = 3
STOP_INTERNAL = 4
STOP_ROS_COMMAND_TIMEOUT = 5
STOP_LINK_SHUTDOWN = 6

# ── drive_state (명세 §8.1) ────────────────────────────────
STATE_BOOT = 0
STATE_SELF_TEST = 1
STATE_READY = 2
STATE_DRIVING = 3
STATE_SAFE_STOP = 4
STATE_FAULT = 5
STATE_ESTOP = 6

STATE_NAMES = {
    0: 'BOOT', 1: 'SELF_TEST', 2: 'READY', 3: 'DRIVING',
    4: 'SAFE_STOP', 5: 'FAULT', 6: 'ESTOP',
}

# ── fault bits (명세 §9.1) ─────────────────────────────────
# STM32 파트 확정 표 (2026-07-29 수신). bit 17~31 은 예약이며 항상 0.
FAULT_COMM_TIMEOUT = 1 << 0
FAULT_CRC_ERROR = 1 << 1
FAULT_BAD_COMMAND = 1 << 2
FAULT_ENCODER_INVALID = 1 << 3
FAULT_MOTOR_STALL = 1 << 4
FAULT_DIRECTION_FAULT = 1 << 5
FAULT_CONTROL_OVERRUN = 1 << 6
FAULT_IMU_LOST = 1 << 7
FAULT_RANGE_LOST = 1 << 8
FAULT_STEERING_INVALID = 1 << 9
FAULT_ESTOP_ACTIVE = 1 << 10
FAULT_INTERNAL_ERROR = 1 << 11
FAULT_COMMAND_LIMIT = 1 << 12
FAULT_UART_RX_OVERFLOW = 1 << 13
FAULT_UART_TX_ERROR = 1 << 14
FAULT_SENSOR_STALE = 1 << 15
FAULT_OBSTACLE_NEAR = 1 << 16
FAULT_RESERVED_MASK = 0xFFFE0000        # bit 17~31

# fault_action 등급 (명세 §9.2)
ACT_REPORT_ONLY = 'REPORT_ONLY'
ACT_SAFE_STOP = 'SAFE_STOP'
ACT_OUTPUT_DISABLE = 'OUTPUT_DISABLE'
ACT_LATCHED_STOP = 'LATCHED_STOP'

# 해제 방식. "무엇을 해야 풀리는가" 이며 진단 메시지에 그대로 쓴다.
CLR_REARM = 'neutral 재수신·rearm'
CLR_AUTO = '원인 해소 시 자동'
CLR_RESET = 'CMD_RESET_FAULT'
CLR_CALIB = '실측 보정 필요(사람)'
CLR_PHYSICAL = '물리 복귀 후 reset'

# bit -> (이름, 조치, 해제방식)
FAULT_TABLE = {
    0:  ('COMM_TIMEOUT',     ACT_SAFE_STOP,      CLR_REARM),
    1:  ('CRC_ERROR',        ACT_REPORT_ONLY,    CLR_RESET),
    2:  ('BAD_COMMAND',      ACT_REPORT_ONLY,    CLR_RESET),
    3:  ('ENCODER_INVALID',  ACT_SAFE_STOP,      CLR_CALIB),
    4:  ('MOTOR_STALL',      ACT_LATCHED_STOP,   CLR_RESET),
    5:  ('DIRECTION_FAULT',  ACT_LATCHED_STOP,   CLR_RESET),
    6:  ('CONTROL_OVERRUN',  ACT_OUTPUT_DISABLE, CLR_RESET),
    7:  ('IMU_LOST',         ACT_REPORT_ONLY,    CLR_AUTO),
    8:  ('RANGE_LOST',       ACT_REPORT_ONLY,    CLR_AUTO),
    9:  ('STEERING_INVALID', ACT_SAFE_STOP,      CLR_CALIB),
    10: ('ESTOP_ACTIVE',     ACT_OUTPUT_DISABLE, CLR_PHYSICAL),
    11: ('INTERNAL_ERROR',   ACT_OUTPUT_DISABLE, CLR_RESET),
    12: ('COMMAND_LIMIT',    ACT_REPORT_ONLY,    CLR_RESET),
    13: ('UART_RX_OVERFLOW', ACT_REPORT_ONLY,    CLR_RESET),
    14: ('UART_TX_ERROR',    ACT_REPORT_ONLY,    CLR_RESET),
    15: ('SENSOR_STALE',     ACT_SAFE_STOP,      CLR_AUTO),
    16: ('OBSTACLE_NEAR',    ACT_SAFE_STOP,      CLR_AUTO),
}

# latch 되는 bit (명세 §9.3). latched_fault_bits 는 active 의 누적 기록이
# 아니라 이 4개만 나타낸다. active=0xA300, latched=0 은 의도된 조합이다.
FAULT_LATCHED_MASK = (FAULT_MOTOR_STALL | FAULT_DIRECTION_FAULT
                      | FAULT_ESTOP_ACTIVE | FAULT_INTERNAL_ERROR)

# CMD_RESET_FAULT(0x12) 로 해제를 요청할 수 있는 bit.
# - 통신·명령 계열 (CLR_RESET): 무조건 대상
# - ESTOP_ACTIVE(bit10): STM32 회신(2026-08-04)으로 확정 — <200mm 초음파
#   E-STOP 은 래치되며 "거리 650mm 이상 + neutral + 출력 0" 에서
#   CMD_RESET_FAULT 로만 풀린다. 전제조건 판정은 STM32 가 하므로 Jetson 은
#   활성 시 마스크에 포함해 요청만 한다 (미충족이면 STM32 가 거부).
# - 나머지 latch 계열(MOTOR_STALL·DIRECTION_FAULT·INTERNAL_ERROR)은
#   STM32 팀 확인 전까지 제외. 자동해제(CLR_AUTO)·보정필요(CLR_CALIB)·
#   rearm 계열은 reset 대상이 아니다 — 강제로 지우면 원인이 가려진다.
RESETTABLE_FAULT_MASK = (FAULT_CRC_ERROR | FAULT_BAD_COMMAND
                         | FAULT_COMMAND_LIMIT | FAULT_UART_RX_OVERFLOW
                         | FAULT_UART_TX_ERROR | FAULT_ESTOP_ACTIVE)

# ARM 을 막는 bit. "조치 등급이 REPORT_ONLY 가 아닌 것" 이 아니라
# "neutral 재송신으로 해소되지 않는 것" 이 기준이다. COMM_TIMEOUT 은
# SAFE_STOP 등급이지만 rearm 절차 자체가 그것을 해소하므로 제외한다.
# 이 mask 가 걸려 있으면 neutral 을 몇 번 보내도 READY 로 가지 않는다.
FAULT_ARM_BLOCKING_MASK = 0
for _b, (_n, _a, _c) in FAULT_TABLE.items():
    if _a != ACT_REPORT_ONLY and _c != CLR_REARM:
        FAULT_ARM_BLOCKING_MASK |= 1 << _b
del _b, _n, _a, _c


def describe_faults(bits: int) -> str:
    """fault mask 를 사람이 읽을 수 있는 문자열로. 0xA300 은 못 읽는다."""
    if not bits:
        return 'none'
    out = []
    for b in range(32):
        if not bits & (1 << b):
            continue
        e = FAULT_TABLE.get(b)
        out.append(f'bit{b} {e[0]}({e[1]})' if e else f'bit{b} 예약/미정의')
    return ', '.join(out)


def arm_blocking_faults(bits: int) -> int:
    """neutral 로 해소되지 않아 ARM 을 막는 bit 만 남긴다."""
    return bits & FAULT_ARM_BLOCKING_MASK


# ── COMMAND_RESULT result_code (명세 §7.3) ─────────────────
RESULT_ACCEPTED = 0
RESULT_INVALID_STATE = 1
RESULT_OUT_OF_RANGE = 2
RESULT_FAULT_ACTIVE = 3
RESULT_UNSUPPORTED = 4
RESULT_INVALID_VALUE = 5
RESULT_DUPLICATE = 6
RESULT_NOT_ARMED = 7


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE. poly=0x1021, init=0xFFFF, no reflect, xorout=0.

    표준 시험 벡터: b"123456789" -> 0x29B1 (명세 §4.3)
    """
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def pack_frame(msg_id: int, seq: int, payload: bytes = b'') -> bytes:
    """[AA][55][VER][ID][SEQ][LEN][PAYLOAD][CRC_L][CRC_H] (명세 §4.1)"""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f'payload {len(payload)} > {MAX_PAYLOAD}')
    body = bytes([VERSION, msg_id, seq & 0xFF, len(payload)]) + payload
    crc = crc16_ccitt_false(body)
    return SOF + body + struct.pack('<H', crc)  # little-endian -> CRC_LOW 먼저


# ── Jetson -> STM32 ────────────────────────────────────────

def pack_cmd_drive(seq: int, speed_mm_s: int, steering_cdeg: int,
                   enable: bool) -> bytes:
    """CMD_DRIVE(0x10). struct <hhB (명세 §6.1)"""
    payload = struct.pack('<hhB', speed_mm_s, steering_cdeg,
                          1 if enable else 0)
    return pack_frame(CMD_DRIVE, seq, payload)


def pack_neutral(seq: int) -> bytes:
    """neutral = (0, 0, enable=0)"""
    return pack_cmd_drive(seq, 0, 0, False)


def pack_cmd_stop(seq: int, reason: int) -> bytes:
    """CMD_STOP(0x11). struct <B (명세 §6.2)"""
    return pack_frame(CMD_STOP, seq, struct.pack('<B', reason))


def pack_cmd_reset_fault(seq: int, fault_mask: int) -> bytes:
    """CMD_RESET_FAULT(0x12). struct <I (명세 §6.3)"""
    return pack_frame(CMD_RESET_FAULT, seq, struct.pack('<I', fault_mask))


# ── STM32 -> Jetson (fake_stm32가 pack, bridge가 unpack) ───

TELEMETRY_STRUCT = '<IhhhhhihBBI'  # 26 bytes (명세 §7.1)


def pack_telemetry(seq: int, *, mcu_time_ms: int, target_speed_mm_s: int,
                   measured_speed_mm_s: int, motor_duty_permille: int,
                   steering_cmd_cdeg: int, steering_feedback_cdeg: int,
                   encoder_count: int, yaw_cdeg: int, drive_state: int,
                   last_drive_seq: int, active_fault_bits: int) -> bytes:
    payload = struct.pack(
        TELEMETRY_STRUCT,
        mcu_time_ms & 0xFFFFFFFF, target_speed_mm_s, measured_speed_mm_s,
        motor_duty_permille, steering_cmd_cdeg, steering_feedback_cdeg,
        encoder_count, yaw_cdeg, drive_state, last_drive_seq,
        active_fault_bits)
    return pack_frame(TELEMETRY_DRIVE, seq, payload)


def unpack_telemetry(payload: bytes) -> dict:
    if len(payload) != FIXED_LENGTHS[TELEMETRY_DRIVE]:
        raise ValueError(f'telemetry length {len(payload)} != 26')
    f = struct.unpack(TELEMETRY_STRUCT, payload)
    return {
        'mcu_time_ms': f[0],
        'target_speed_mm_s': f[1],
        'measured_speed_mm_s': f[2],
        'motor_duty_permille': f[3],
        'steering_cmd_cdeg': f[4],
        'steering_feedback_cdeg': f[5],
        'encoder_count': f[6],
        'yaw_cdeg': f[7],
        'drive_state': f[8],
        'last_drive_seq': f[9],
        'active_fault_bits': f[10],
    }


def pack_fault_event(seq: int, *, mcu_time_ms: int, active_fault_bits: int,
                     latched_fault_bits: int, fault_action: int,
                     drive_state: int) -> bytes:
    """FAULT_EVENT(0x81). struct <IIIBB (명세 §7.2)"""
    payload = struct.pack('<IIIBB', mcu_time_ms & 0xFFFFFFFF,
                          active_fault_bits, latched_fault_bits,
                          fault_action, drive_state)
    return pack_frame(FAULT_EVENT, seq, payload)


def unpack_fault_event(payload: bytes) -> dict:
    f = struct.unpack('<IIIBB', payload)
    return {
        'mcu_time_ms': f[0],
        'active_fault_bits': f[1],
        'latched_fault_bits': f[2],
        'fault_action': f[3],
        'drive_state': f[4],
    }


def pack_command_result(seq: int, *, request_msg_id: int, request_seq: int,
                        result_code: int, drive_state: int) -> bytes:
    """COMMAND_RESULT(0x82). struct <BBBB (명세 §7.3)"""
    payload = struct.pack('<BBBB', request_msg_id, request_seq,
                          result_code, drive_state)
    return pack_frame(COMMAND_RESULT, seq, payload)


def unpack_command_result(payload: bytes) -> dict:
    f = struct.unpack('<BBBB', payload)
    return {
        'request_msg_id': f[0],
        'request_seq': f[1],
        'result_code': f[2],
        'drive_state': f[3],
    }


# ── TELEMETRY_RANGE (명세 §3.8) ────────────────────────────
# 초음파 4채널. 무효·미장착 채널은 valid bit=0 + 거리 0xFFFF 로 보낸다.
# valid_mask bit 4~7 은 예약이며 0 이어야 한다.
RANGE_STRUCT = '<IHHHHB'      # 13 bytes
RANGE_INVALID_MM = 0xFFFF
RANGE_CHANNELS = ('front_left', 'front_right', 'rear_left', 'rear_right')
RANGE_VALID_RESERVED = 0xF0


def pack_telemetry_range(seq: int, *, mcu_time_ms: int, front_left: int,
                         front_right: int, rear_left: int, rear_right: int,
                         valid_mask: int) -> bytes:
    """TELEMETRY_RANGE(0x84). struct <IHHHHB (명세 §3.8)"""
    payload = struct.pack(RANGE_STRUCT, mcu_time_ms & 0xFFFFFFFF,
                          front_left, front_right, rear_left, rear_right,
                          valid_mask)
    return pack_frame(TELEMETRY_RANGE, seq, payload)


def unpack_telemetry_range(payload: bytes) -> dict:
    if len(payload) != FIXED_LENGTHS[TELEMETRY_RANGE]:
        raise ValueError(f'telemetry_range length {len(payload)} != 13')
    f = struct.unpack(RANGE_STRUCT, payload)
    mask = f[5]
    d = {'mcu_time_ms': f[0], 'valid_mask': mask}
    for i, name in enumerate(RANGE_CHANNELS):
        d[name] = f[1 + i]
    # 편의 필드: valid bit 이 0 인 채널은 거리값을 쓰지 말아야 하므로 None.
    # 소비자가 0xFFFF 를 실제 65535 mm 로 오해하는 것을 막는다.
    d['ranges_mm'] = [f[1 + i] if mask & (1 << i) else None for i in range(4)]
    return d


# ── TELEMETRY_ODOMETRY (V3 §9.2) ───────────────────────────
# 좌표계: +x 차량 전방, +y 차량 좌측, +yaw 좌회전(CCW). 출발점 기준이며
# STM32 가 리셋되면 0 으로 초기화된다. UART pose reset 명령은 없다.
# ── 0x83 TELEMETRY_IMU (BNO085, 38 bytes, 20 Hz) ──────────
IMU_STRUCT = '<IhhhhiiihhhiBBBB'      # 38 bytes

IMU_CONNECTED = 1 << 0
IMU_GYRO_VALID = 1 << 1
IMU_LINEAR_ACCEL_VALID = 1 << 2
IMU_QUATERNION_VALID = 1 << 3
IMU_STALE = 1 << 4
IMU_SPI_ERROR = 1 << 5
IMU_PROTOCOL_ERROR = 1 << 6
IMU_STATUS_RESERVED_MASK = 0x80       # bit 7 예약, 항상 0

IMU_STATUS_NAMES = {
    0: 'CONNECTED', 1: 'GYRO_VALID', 2: 'LINEAR_ACCEL_VALID',
    3: 'QUATERNION_VALID', 4: 'STALE', 5: 'SPI_ERROR',
    6: 'PROTOCOL_ERROR',
}

# quaternion 은 Q14 고정소수점이다. 1.0 == 16384.
IMU_Q14_SCALE = 16384.0

# BNO085 accuracy 0~3. STM32 는 1 이상일 때만 융합에 쓴다 (vehicle_config.h).
IMU_ACCURACY_NAMES = {0: 'unreliable', 1: 'low', 2: 'medium', 3: 'high'}
IMU_MIN_FUSION_ACCURACY = 1


def pack_telemetry_imu(seq: int, *, mcu_time_ms: int, quat_i: int,
                       quat_j: int, quat_k: int, quat_real: int,
                       gyro_x_mdeg_s: int, gyro_y_mdeg_s: int,
                       gyro_z_mdeg_s: int, accel_x_mm_s2: int,
                       accel_y_mm_s2: int, accel_z_mm_s2: int,
                       yaw_mdeg: int, gyro_accuracy: int,
                       accel_accuracy: int, quat_accuracy: int,
                       status_flags: int) -> bytes:
    """TELEMETRY_IMU(0x83). fake_stm32 전용."""
    return pack_frame(TELEMETRY_IMU, seq, struct.pack(
        IMU_STRUCT, mcu_time_ms & 0xFFFFFFFF, quat_i, quat_j, quat_k,
        quat_real, gyro_x_mdeg_s, gyro_y_mdeg_s, gyro_z_mdeg_s,
        accel_x_mm_s2, accel_y_mm_s2, accel_z_mm_s2, yaw_mdeg,
        gyro_accuracy, accel_accuracy, quat_accuracy, status_flags))


def unpack_telemetry_imu(payload: bytes) -> dict:
    if len(payload) != FIXED_LENGTHS[TELEMETRY_IMU]:
        raise ValueError(f'telemetry_imu length {len(payload)} != 38')
    f = struct.unpack(IMU_STRUCT, payload)
    return {
        'mcu_time_ms': f[0],
        'quaternion_i_q14': f[1],
        'quaternion_j_q14': f[2],
        'quaternion_k_q14': f[3],
        'quaternion_real_q14': f[4],
        'gyro_x_mdeg_s': f[5],
        'gyro_y_mdeg_s': f[6],
        'gyro_z_mdeg_s': f[7],
        'linear_accel_x_mm_s2': f[8],
        'linear_accel_y_mm_s2': f[9],
        'linear_accel_z_mm_s2': f[10],
        'yaw_mdeg': f[11],
        'gyro_accuracy': f[12],
        'linear_accel_accuracy': f[13],
        'quaternion_accuracy': f[14],
        'status_flags': f[15],
    }


def describe_imu_status(flags: int) -> str:
    """IMU status bit 를 사람이 읽는 문자열로."""
    if not flags:
        return 'none'
    out = [IMU_STATUS_NAMES.get(b, f'bit{b}')
           for b in range(8) if flags & (1 << b)]
    return '|'.join(out)


def imu_healthy(status_flags: int) -> bool:
    """센서를 믿어도 되는 최소 조건 (인수인계 §9.2).

    "IMU 가 살아 있나" 를 보고·표시하기 위한 판정이다. 회전 판정에 yaw 를
    쓸 수 있는지는 `imu_yaw_usable()` 로 따로 본다.
    """
    return (bool(status_flags & IMU_CONNECTED)
            and bool(status_flags & IMU_GYRO_VALID)
            and not status_flags & IMU_STALE)


# quaternion yaw 를 회전 판정에 쓰기 위한 최소 accuracy.
# BNO085 accuracy 0~3. 2(medium) 이상을 요구한다 — 실장비 실측이 3(high) 이다.
IMU_MIN_YAW_ACCURACY = 2


def imu_yaw_usable(status_flags: int, quaternion_accuracy: int) -> bool:
    """`0x83` 의 quaternion yaw 를 회전 완료 판정에 써도 되는가.

    왜 `0x85` 대신 이걸 쓰는가 — 2026-08-05 실측:

        0x83 quaternion yaw : -114.7°  (gyro_z 적분 -116.0° 와 1.1% 일치)
        0x85 융합/모델 yaw  : -146.2°  ← **1.275 배 부풀려짐**

    `0x85` 는 모델 yaw(조향 명령 기반 자전거모델)를 25% 섞는다. 그 모델은
    명령각이 그대로 실현된다고 가정하는데 실제 동적 유효율이 62% 라서, 융합값이
    실제 회전보다 크게 나온다. 그 결과 ODOM 90° 에서 멈추면 물리적으로 71° 다.

    quaternion yaw 는 센서가 직접 재는 값이라 조향 트림·모델 오차·STM32 의
    융합 판단(IMU_FUSED 깜빡임)에 전부 영향받지 않는다.
    """
    return (bool(status_flags & IMU_CONNECTED)
            and bool(status_flags & IMU_QUATERNION_VALID)
            and not status_flags & IMU_STALE
            and quaternion_accuracy >= IMU_MIN_YAW_ACCURACY)


ODOMETRY_STRUCT = '<IiiiihihiHBB'     # 36 bytes

# status_flags (V3 §9.2)
ODOM_VALID = 1 << 0
ODOM_ENCODER_CALIBRATED = 1 << 1
ODOM_GEOMETRY_CALIBRATED = 1 << 2
ODOM_STEERING_ESTIMATED = 1 << 3
ODOM_IMU_FUSED = 1 << 4
ODOM_INPUT_INVALID = 1 << 5
ODOM_STATUS_RESERVED_MASK = 0xFFC0    # bit 6~15 예약, 항상 0

ODOM_STATUS_NAMES = {
    0: 'VALID',
    1: 'ENCODER_CALIBRATED',
    2: 'GEOMETRY_CALIBRATED',
    3: 'STEERING_ESTIMATED',
    4: 'IMU_FUSED',
    5: 'INPUT_INVALID',
}

# steering_source (V3 §9.2)
ODOM_STEERING_NONE = 0
ODOM_STEERING_SENSOR = 1
ODOM_STEERING_COMMAND_ESTIMATE = 2

ODOM_STEERING_SOURCE_NAMES = {
    ODOM_STEERING_NONE: 'NONE',
    ODOM_STEERING_SENSOR: 'SENSOR',
    ODOM_STEERING_COMMAND_ESTIMATE: 'COMMAND_ESTIMATE',
}

# pose 를 쓰기 위해 반드시 켜져 있어야 하는 bit (V3 §9.2 의 required)
ODOM_REQUIRED_MASK = (ODOM_VALID | ODOM_ENCODER_CALIBRATED
                      | ODOM_GEOMETRY_CALIBRATED | ODOM_STEERING_ESTIMATED)


def describe_odom_status(flags: int) -> str:
    """status_flags 를 사람이 읽을 수 있는 문자열로."""
    if not flags:
        return 'none'
    out = []
    for b in range(16):
        if flags & (1 << b):
            n = ODOM_STATUS_NAMES.get(b)
            out.append(n if n else f'bit{b} 예약/미정의')
    return ', '.join(out)


def pack_telemetry_odometry(seq: int, *, mcu_time_ms: int, x_mm: int,
                            y_mm: int, yaw_mdeg: int, distance_mm: int,
                            linear_speed_mm_s: int, yaw_rate_mdeg_s: int,
                            steering_cdeg: int, curvature_micro_per_m: int,
                            status_flags: int, steering_source: int,
                            last_drive_seq: int) -> bytes:
    """TELEMETRY_ODOMETRY(0x85). struct <IiiiihihiHBB (V3 §9.2)"""
    payload = struct.pack(
        ODOMETRY_STRUCT, mcu_time_ms & 0xFFFFFFFF, x_mm, y_mm, yaw_mdeg,
        distance_mm, linear_speed_mm_s, yaw_rate_mdeg_s, steering_cdeg,
        curvature_micro_per_m, status_flags, steering_source, last_drive_seq)
    return pack_frame(TELEMETRY_ODOMETRY, seq, payload)


def unpack_telemetry_odometry(payload: bytes) -> dict:
    if len(payload) != FIXED_LENGTHS[TELEMETRY_ODOMETRY]:
        raise ValueError(f'telemetry_odometry length {len(payload)} != 36')
    f = struct.unpack(ODOMETRY_STRUCT, payload)
    return {
        'mcu_time_ms': f[0],
        'x_mm': f[1],
        'y_mm': f[2],
        'yaw_mdeg': f[3],
        'distance_mm': f[4],
        'linear_speed_mm_s': f[5],
        'yaw_rate_mdeg_s': f[6],
        'steering_cdeg': f[7],
        'curvature_micro_per_m': f[8],
        'status_flags': f[9],
        'steering_source': f[10],
        'last_drive_seq': f[11],
    }


def odom_pose_valid(status_flags: int, steering_source: int) -> bool:
    """pose 를 소비해도 되는지 (V3 §9.2 의 pose_valid 조건 그대로).

    bit 0~3 이 모두 1, bit 5 INPUT_INVALID 가 0, steering_source 가
    COMMAND_ESTIMATE 여야 한다. IMU_FUSED(bit 4)는 조건이 아니다 — 지금은
    항상 0 이지만 BNO085 가 들어오면 1 이 되며, 그때도 이 판정은 그대로
    통과해야 한다.

    실제 조향 센서가 장착되면 steering_source 가 SENSOR(1) 로 바뀌고 bit 3
    STEERING_ESTIMATED 가 내려간다. 그때는 이 함수를 명세 개정과 함께
    수정해야 한다 — 조용히 통과시키면 근거 없는 pose 를 믿게 된다.
    """
    return ((status_flags & ODOM_REQUIRED_MASK) == ODOM_REQUIRED_MASK
            and not status_flags & ODOM_INPUT_INVALID
            and steering_source == ODOM_STEERING_COMMAND_ESTIMATE)


def unpack_cmd_drive(payload: bytes) -> dict:
    f = struct.unpack('<hhB', payload)
    return {'speed_mm_s': f[0], 'steering_cdeg': f[1], 'enable': f[2]}


# ── DIAG_ECHO (V3 §7, 인수인계 기준 GF-03/GF-04) ────────────
# 길이가 가변(요청 0~31, 응답 1~32)이라 FIXED_LENGTHS 에 넣지 않는다.
MAX_ECHO_PAYLOAD = 31


def pack_diag_echo_request(seq: int, data: bytes = b'') -> bytes:
    """DIAG_ECHO_REQUEST(0xF0). payload 는 그대로 되돌아온다."""
    if len(data) > MAX_ECHO_PAYLOAD:
        raise ValueError(f'echo payload {len(data)} > {MAX_ECHO_PAYLOAD}')
    return pack_frame(DIAG_ECHO_REQUEST, seq, data)


def pack_diag_echo_response(seq: int, *, request_seq: int,
                            data: bytes) -> bytes:
    """DIAG_ECHO_RESPONSE(0xF1). payload = [요청 SEQ][요청 data...]

    header 의 SEQ 는 STM32 자체 TX 카운터이므로 request_seq 와 다르다.
    요청 payload 안의 AA 55 도 그대로 보존된다.
    """
    return pack_frame(DIAG_ECHO_RESPONSE, seq, bytes([request_seq & 0xFF])
                      + data)


def unpack_diag_echo_response(payload: bytes) -> dict:
    if not payload:
        raise ValueError('echo response payload 가 비어 있다')
    return {'request_seq': payload[0], 'data': payload[1:]}


# ── SEQ (명세 §4.4) ────────────────────────────────────────

def seq_classify(new: int, last: int) -> str:
    """duplicate / new / old 판정."""
    d = (new - last) & 0xFF
    if d == 0:
        return 'duplicate'
    if 1 <= d <= 127:
        return 'new'
    return 'old'


class SeqCounter:
    """송신자별 독립 uint8 카운터. 매 송신마다 1 증가, 255 다음 0."""

    def __init__(self, start: int = 0):
        self._v = start & 0xFF

    def next(self) -> int:
        v = self._v
        self._v = (self._v + 1) & 0xFF
        return v
