"""UART frame parser (순수 Python, ROS import 금지).

명세 §4.5 Parser 요구사항 구현:
- 지속 buffer 누적, AA 55 검색, 앞 잡음 폐기
- VERSION/LENGTH 검사, CRC 검사
- 실패 시 후보 첫 byte 하나만 폐기 후 재검색 (payload 내 AA 55 대응)
- 불완전 frame 약 100 ms 후 폐기
- "한 read = 한 frame" 가정 금지
"""
import time

from .protocol import (SOF, VERSION, MAX_PAYLOAD, FIXED_LENGTHS,
                       crc16_ccitt_false)

HEADER_LEN = 6          # SOF(2) + VER + ID + SEQ + LEN
OVERHEAD = 8            # header 6 + CRC 2


class ParserStats:
    """진단 카운터 (명세 §14.2)."""

    def __init__(self):
        self.valid_frames = 0
        self.crc_errors = 0
        self.version_errors = 0
        self.length_errors = 0
        self.schema_errors = 0
        self.noise_bytes = 0
        self.resyncs = 0
        self.incomplete_dropped = 0


class FrameParser:
    """수신 byte를 feed()에 넣으면 완성 frame 목록을 돌려준다."""

    def __init__(self, incomplete_timeout_s: float = 0.1,
                 max_buffer: int = 1024):
        self.buf = bytearray()
        self.stats = ParserStats()
        self._timeout = incomplete_timeout_s
        self._max_buffer = max_buffer
        self._candidate_since = None  # 불완전 frame 타이머

    def feed(self, data: bytes, now: float = None):
        """bytes를 넣고 [(msg_id, seq, payload), ...]를 반환."""
        if now is None:
            now = time.monotonic()
        self.buf += data
        if len(self.buf) > self._max_buffer:          # overflow 방어 (PS-08)
            drop = len(self.buf) - self._max_buffer
            del self.buf[:drop]
            self.stats.noise_bytes += drop
            self.stats.resyncs += 1

        frames = []
        while True:
            i = self.buf.find(SOF)
            if i < 0:
                # SOF 없음. 마지막 byte가 0xAA면 다음 SOF 후보로 보존 (§4.5-8)
                if self.buf[-1:] == b'\xAA':
                    self.stats.noise_bytes += len(self.buf) - 1
                    self.buf = bytearray(b'\xAA')
                else:
                    self.stats.noise_bytes += len(self.buf)
                    self.buf.clear()
                self._candidate_since = None
                break

            if i > 0:                                  # SOF 앞 잡음 폐기 (PS-03)
                self.stats.noise_bytes += i
                del self.buf[:i]

            if len(self.buf) < HEADER_LEN:             # header 대기
                self._arm_timeout(now)
                break

            version, msg_id, seq, length = self.buf[2:HEADER_LEN]

            if version != VERSION:
                self.stats.version_errors += 1
                self._resync()
                continue
            if length > MAX_PAYLOAD:                   # PS-06
                self.stats.length_errors += 1
                self._resync()
                continue

            total = OVERHEAD + length
            if len(self.buf) < total:                  # frame 완성 대기 (PS-01)
                self._arm_timeout(now)
                break

            body = bytes(self.buf[2:HEADER_LEN + length])
            rx_crc = int.from_bytes(self.buf[HEADER_LEN + length:total],
                                    'little')
            if crc16_ccitt_false(body) != rx_crc:      # PS-05
                self.stats.crc_errors += 1
                self._resync()
                continue

            # 고정 길이 메시지 schema 검사 (명세 §5)
            expected = FIXED_LENGTHS.get(msg_id)
            if expected is not None and length != expected:
                self.stats.schema_errors += 1
                del self.buf[:total]                   # CRC는 맞으므로 frame째 폐기
                self._candidate_since = None
                continue

            frames.append((msg_id, seq, bytes(self.buf[HEADER_LEN:
                                                       HEADER_LEN + length])))
            self.stats.valid_frames += 1
            del self.buf[:total]
            self._candidate_since = None

        return frames

    def check_timeout(self, now: float = None):
        """주기적으로 호출. 불완전 frame이 timeout을 넘기면 폐기 (§4.5-9)."""
        if now is None:
            now = time.monotonic()
        if (self._candidate_since is not None
                and now - self._candidate_since > self._timeout
                and len(self.buf) > 0):
            self.stats.incomplete_dropped += 1
            self._resync()
            self._candidate_since = None

    # ── 내부 ───────────────────────────────────────────────

    def _resync(self):
        """후보 SOF의 첫 byte 하나만 폐기하고 재검색 (§4.5-7)."""
        del self.buf[:1]
        self.stats.resyncs += 1
        self._candidate_since = None

    def _arm_timeout(self, now: float):
        if self._candidate_since is None:
            self._candidate_since = now
