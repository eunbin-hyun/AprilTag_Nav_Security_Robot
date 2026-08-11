"""Parser 강건성 시험. 명세 §16.2 PS-01..08."""
from stm32_bridge.parser import FrameParser
from stm32_bridge.protocol import (
    pack_cmd_drive, pack_frame, CMD_DRIVE, DIAG_ECHO_REQUEST,
)

FRAME = pack_cmd_drive(0x2A, 500, -1250, True)      # GF-01, 13 bytes


def collect(parser, data, chunk=None):
    """data를 통째로 또는 chunk 단위로 feed하고 frame 모음 반환."""
    frames = []
    if chunk is None:
        frames += parser.feed(data)
    else:
        for i in range(0, len(data), chunk):
            frames += parser.feed(data[i:i + chunk])
    return frames


# ── PS-01: 1 byte씩 분할 ──────────────────────────────────

def test_ps01_byte_by_byte():
    p = FrameParser()
    frames = collect(p, FRAME, chunk=1)
    assert len(frames) == 1
    msg_id, seq, payload = frames[0]
    assert (msg_id, seq) == (CMD_DRIVE, 0x2A)
    assert p.stats.valid_frames == 1


# ── PS-02: 여러 frame 한 read ─────────────────────────────

def test_ps02_multiple_frames_one_read():
    p = FrameParser()
    f2 = pack_cmd_drive(0x2B, 0, 0, False)
    frames = collect(p, FRAME + f2 + FRAME)
    assert [f[1] for f in frames] == [0x2A, 0x2B, 0x2A]


# ── PS-03: SOF 앞·사이 잡음 ───────────────────────────────

def test_ps03_noise_resync():
    p = FrameParser()
    noise = b'\x00\xFF\x13\x37'
    frames = collect(p, noise + FRAME + noise + FRAME)
    assert len(frames) == 2
    assert p.stats.noise_bytes >= len(noise)


# ── PS-04: payload 안 AA 55 보존 (GF-03) ──────────────────

def test_ps04_sof_inside_payload():
    p = FrameParser()
    echo = pack_frame(DIAG_ECHO_REQUEST, 0x10, bytes.fromhex('1234AA55'))
    frames = collect(p, echo, chunk=1)          # 분할까지 겹쳐서
    assert len(frames) == 1
    assert frames[0][2] == bytes.fromhex('1234AA55')


# ── PS-05: CRC 1 bit 손상 후 다음 frame 복구 ──────────────

def test_ps05_crc_corruption_recovers():
    p = FrameParser()
    bad = bytearray(FRAME)
    bad[-1] ^= 0x01                              # CRC_HIGH 1 bit 손상
    frames = collect(p, bytes(bad) + FRAME)
    assert len(frames) == 1                      # 손상 frame 미적용
    assert frames[0][1] == 0x2A
    assert p.stats.crc_errors >= 1


# ── PS-06: LENGTH 초과 거부 ───────────────────────────────

def test_ps06_length_overflow_rejected():
    p = FrameParser()
    evil = b'\xAA\x55\x02\x10\x00\xFF' + b'\x00' * 20   # LENGTH=255
    frames = collect(p, evil + FRAME)
    assert len(frames) == 1                      # 정상 frame만
    assert p.stats.length_errors >= 1


# ── PS-07: 중간에 끊긴 frame 100 ms 후 폐기 ───────────────

def test_ps07_incomplete_frame_timeout():
    p = FrameParser(incomplete_timeout_s=0.1)
    p.feed(FRAME[:7], now=1000.0)                # header+1만 도착
    p.check_timeout(now=1000.05)                 # 50 ms: 아직 유지
    assert p.stats.incomplete_dropped == 0
    p.check_timeout(now=1000.2)                  # 200 ms: 폐기
    assert p.stats.incomplete_dropped == 1
    frames = p.feed(FRAME, now=1000.3)           # 이후 정상 frame 복구
    assert len(frames) == 1


# ── PS-08: buffer overflow에도 복구 ───────────────────────

def test_ps08_buffer_overflow_recovers():
    p = FrameParser(max_buffer=256)
    frames = collect(p, b'\x00' * 2000)          # 잡음 폭주
    assert frames == []
    frames = collect(p, FRAME)                   # 이후 정상 수신
    assert len(frames) == 1


# ── 고정 길이 불일치 (schema) ─────────────────────────────

def test_fixed_length_mismatch_discarded():
    p = FrameParser()
    wrong = pack_frame(CMD_DRIVE, 1, b'\x00' * 4)   # CMD_DRIVE인데 4 byte
    frames = collect(p, wrong + FRAME)
    assert len(frames) == 1
    assert p.stats.schema_errors == 1
