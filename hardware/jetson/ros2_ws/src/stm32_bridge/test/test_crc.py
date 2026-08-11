"""프로토콜 단위 시험. 명세 §16.1 UT-01/02/04, §15 golden frames."""
import struct

from stm32_bridge import protocol as P
from stm32_bridge.protocol import (
    crc16_ccitt_false, pack_frame, pack_cmd_drive, pack_telemetry,
    unpack_telemetry, pack_telemetry_range, unpack_telemetry_range,
    seq_classify, SeqCounter,
    DIAG_ECHO_REQUEST, DIAG_ECHO_RESPONSE, STATE_DRIVING,
    RANGE_INVALID_MM,
)


# ── UT-01: CRC 표준 시험 벡터 ─────────────────────────────

def test_ut01_standard_vector():
    assert crc16_ccitt_false(b'123456789') == 0x29B1


# ── Golden frames (명세 §15) ──────────────────────────────

def test_gf01_cmd_drive():
    # SEQ=0x2A, 500 mm/s, -1250 cdeg, enable=1
    frame = pack_cmd_drive(0x2A, 500, -1250, True)
    assert frame == bytes.fromhex('AA5502102A05F4011EFB01847A')


def test_gf02_neutral():
    frame = pack_cmd_drive(0x00, 0, 0, False)
    assert frame == bytes.fromhex('AA55021000050000000000A0A0')


def test_gf03_echo_request():
    # payload 안에 AA 55 포함
    frame = pack_frame(DIAG_ECHO_REQUEST, 0x10, bytes.fromhex('1234AA55'))
    assert frame == bytes.fromhex('AA5502F010041234AA55CD15')


def test_gf04_echo_response():
    # response SEQ=0x03, payload 첫 byte = request SEQ 0x10
    frame = pack_frame(DIAG_ECHO_RESPONSE, 0x03,
                       bytes.fromhex('101234AA55'))
    assert frame == bytes.fromhex('AA5502F103051012 34AA55DD5C'.replace(' ', ''))


def test_gf08_telemetry_range_real_capture():
    """실장비 STM32 가 실제로 보낸 TELEMETRY_RANGE frame 과 byte 단위 일치.

    2026-07-29 CP2102 실측 캡처. 초음파 미장착 상태이므로 4채널 전부
    0xFFFF + valid_mask=0 이다. 이 시험이 깨지면 구현이 실물과 어긋난 것이다.
    """
    captured = bytes.fromhex(
        'AA550284840DF87E2400FFFFFFFFFFFFFFFF004548')
    frame = pack_telemetry_range(
        0x84, mcu_time_ms=2391800,
        front_left=RANGE_INVALID_MM, front_right=RANGE_INVALID_MM,
        rear_left=RANGE_INVALID_MM, rear_right=RANGE_INVALID_MM,
        valid_mask=0x00)
    assert frame == captured

    r = unpack_telemetry_range(captured[6:-2])
    assert r['mcu_time_ms'] == 2391800
    assert r['valid_mask'] == 0
    # valid bit 이 0 이므로 0xFFFF 를 거리로 노출하지 않는다
    assert r['ranges_mm'] == [None, None, None, None]
    assert r['front_left'] == 0xFFFF


def test_telemetry_range_partial_valid():
    """FL·RR 만 유효한 경우 valid bit 매핑 (bit0=FL,1=FR,2=RL,3=RR)."""
    frame = pack_telemetry_range(
        1, mcu_time_ms=1000, front_left=350, front_right=RANGE_INVALID_MM,
        rear_left=RANGE_INVALID_MM, rear_right=1200, valid_mask=0b1001)
    r = unpack_telemetry_range(frame[6:-2])
    assert r['ranges_mm'] == [350, None, None, 1200]
    assert r['rear_right'] == 1200


# ── fault bit 표 (명세 §9.1, STM32 파트 확정 2026-07-29) ──

def test_fault_arm_blocking_excludes_comm_timeout():
    """COMM_TIMEOUT 은 SAFE_STOP 등급이지만 ARM 을 막지 않는다.

    rearm 절차(neutral 재송신) 자체가 그것을 해소하기 때문이다. 등급만으로
    판정하면 정상 rearm 을 영구 차단으로 오판한다.
    """
    assert P.arm_blocking_faults(P.FAULT_COMM_TIMEOUT) == 0
    assert not P.FAULT_ARM_BLOCKING_MASK & P.FAULT_COMM_TIMEOUT


def test_fault_real_hardware_0xa300():
    """실장비 상주 fault 의 차단 요인은 bit 9 와 15 뿐이다."""
    blocking = P.arm_blocking_faults(0x0000A300)
    assert blocking == P.FAULT_STEERING_INVALID | P.FAULT_SENSOR_STALE
    # REPORT_ONLY 인 8·13 은 차단하지 않는다
    assert not blocking & (P.FAULT_RANGE_LOST | P.FAULT_UART_RX_OVERFLOW)
    d = P.describe_faults(0x0000A300)
    assert 'STEERING_INVALID' in d and 'UART_RX_OVERFLOW' in d


def test_fault_latched_mask_is_not_active_accumulation():
    """latched 는 active 누적이 아니라 §9.3 의 4개 bit 만 나타낸다.

    active=0xA300 / latched=0 이 의도된 조합임을 고정한다.
    """
    assert P.FAULT_LATCHED_MASK == (
        P.FAULT_MOTOR_STALL | P.FAULT_DIRECTION_FAULT
        | P.FAULT_ESTOP_ACTIVE | P.FAULT_INTERNAL_ERROR)
    assert 0x0000A300 & P.FAULT_LATCHED_MASK == 0


def test_describe_faults_flags_reserved_bits():
    assert P.describe_faults(0) == 'none'
    assert '예약' in P.describe_faults(1 << 20)


# ── UT-02: pack/unpack 왕복 ───────────────────────────────

def test_telemetry_roundtrip():
    frame = pack_telemetry(
        7, mcu_time_ms=123456, target_speed_mm_s=500,
        measured_speed_mm_s=480, motor_duty_permille=350,
        steering_cmd_cdeg=-1250, steering_feedback_cdeg=-1230,
        encoder_count=98765, yaw_cdeg=-4500, drive_state=STATE_DRIVING,
        last_drive_seq=42, active_fault_bits=0)
    payload = frame[6:-2]
    t = unpack_telemetry(payload)
    assert t['measured_speed_mm_s'] == 480
    assert t['encoder_count'] == 98765
    assert t['yaw_cdeg'] == -4500
    assert t['drive_state'] == STATE_DRIVING
    assert t['last_drive_seq'] == 42


# ── UT-03: int16 경계값 ───────────────────────────────────

def test_int16_boundaries():
    frame = pack_cmd_drive(1, -32768, 32767, True)
    speed, steer, en = struct.unpack('<hhB', frame[6:-2])
    assert (speed, steer, en) == (-32768, 32767, 1)


# ── UT-04: SEQ wrap 판정 ──────────────────────────────────

def test_ut04_seq_wrap():
    assert seq_classify(255, 254) == 'new'
    assert seq_classify(0, 255) == 'new'          # wrap
    assert seq_classify(126, 255) == 'new'        # diff 127
    assert seq_classify(127, 255) == 'old'        # diff 128
    assert seq_classify(5, 5) == 'duplicate'
    assert seq_classify(5, 6) == 'old'


def test_seq_counter_wraps():
    c = SeqCounter(254)
    assert [c.next(), c.next(), c.next()] == [254, 255, 0]


def test_resettable_mask_excludes_auto_and_calibration_faults():
    """CMD_RESET_FAULT 대상은 통신·명령 계열뿐이다 (내일과제 §1.3-2).

    자동해제(장애물·센서)·보정필요(조향)·rearm(COMM_TIMEOUT) 을 reset 으로
    지우면 원인이 가려진 채 주행하게 된다.
    """
    m = P.RESETTABLE_FAULT_MASK
    # bit10(ESTOP_ACTIVE) 포함: STM32 회신(2026-08-04) — <200mm E-STOP 래치는
    # CMD_RESET_FAULT 로만 해제 (전제조건 판정은 STM32 몫).
    assert m == (P.FAULT_CRC_ERROR | P.FAULT_BAD_COMMAND
                 | P.FAULT_COMMAND_LIMIT | P.FAULT_UART_RX_OVERFLOW
                 | P.FAULT_UART_TX_ERROR | P.FAULT_ESTOP_ACTIVE)
    for banned in (P.FAULT_OBSTACLE_NEAR, P.FAULT_SENSOR_STALE,
                   P.FAULT_RANGE_LOST, P.FAULT_STEERING_INVALID,
                   P.FAULT_ENCODER_INVALID, P.FAULT_COMM_TIMEOUT,
                   P.FAULT_MOTOR_STALL, P.FAULT_DIRECTION_FAULT,
                   P.FAULT_INTERNAL_ERROR):
        assert not m & banned, P.describe_faults(banned)


def test_pack_cmd_reset_fault_frame():
    """0x12 + <I little-endian mask. 실장비 0x2006(bit1·2·13) 예시."""
    frame = P.pack_cmd_reset_fault(0x07, 0x00002006)
    assert frame[3] == 0x12                    # msg id
    assert frame[5] == 4                       # payload len
    assert frame[6:10] == bytes.fromhex('06200000')   # little-endian
