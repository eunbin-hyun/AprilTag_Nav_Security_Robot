"""
rc522_test.py — RC522(MFRC522) RFID 단독 테스트 (라즈베리파이 SPI)

배선
    RC522        라즈베리파이
    ---------------------------------
    3.3V    ->   3.3V      (1번 핀)   ※ 5V 금지. 칩이 손상됩니다
    GND     ->   GND       (6번 핀)
    RST     ->   GPIO25    (22번 핀)
    MISO    ->   GPIO9     (21번 핀)
    MOSI    ->   GPIO10    (19번 핀)
    SCK     ->   GPIO11    (23번 핀)
    SDA(SS) ->   GPIO8/CE0 (24번 핀)   ※ I2C 아님. SPI 칩 셀렉트입니다
    IRQ     ->   연결 안 함

사전 준비
    sudo raspi-config nonint do_spi 0
    sudo reboot
    ls /dev/spidev0.*        →  /dev/spidev0.0 이 보여야 함

    source .venv/bin/activate
    pip install spidev gpiozero lgpio

실행
    python3 rc522_test.py

외부 라이브러리는 spidev 와 gpiozero 뿐입니다. 흔히 쓰는 mfrc522 패키지는
내부적으로 RPi.GPIO 에 의존하는데 라즈베리파이 5에서 동작하지 않아 쓰지 않았습니다.
"""

import time

import spidev
from gpiozero import DigitalOutputDevice

# ── MFRC522 레지스터 주소 ────────────────────────────────
CommandReg    = 0x01
ComIEnReg     = 0x02
ComIrqReg     = 0x04
ErrorReg      = 0x06
Status2Reg    = 0x08
FIFODataReg   = 0x09
FIFOLevelReg  = 0x0A
ControlReg    = 0x0C
BitFramingReg = 0x0D
ModeReg       = 0x11
TxControlReg  = 0x14
TxASKReg      = 0x15
TModeReg      = 0x2A
TPrescalerReg = 0x2B
TReloadRegH   = 0x2C
TReloadRegL   = 0x2D
VersionReg    = 0x37

# ── 명령 / 상태 ──────────────────────────────────────────
PCD_IDLE       = 0x00
PCD_TRANSCEIVE = 0x0C
PCD_RESETPHASE = 0x0F

PICC_REQIDL    = 0x26   # REQA: 대기 상태 카드 탐색
PICC_ANTICOLL  = 0x93   # 충돌 방지 (UID 읽기)

MI_OK      = 0
MI_NOTAGERR = 1
MI_ERR     = 2

RST_PIN = 25            # BCM 번호 (물리 22번 핀)
DEBOUNCE_SEC = 2.0      # 같은 카드 재태깅 무시 시간


class MFRC522:
    def __init__(self, bus=0, device=0, speed=1_000_000, rst_pin=RST_PIN):
        self.rst = DigitalOutputDevice(rst_pin, initial_value=True)
        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed
        self.spi.mode = 0
        time.sleep(0.05)
        self._init_chip()

    # ── 저수준 SPI ──────────────────────────────────────
    def _write(self, addr, val):
        self.spi.xfer2([(addr << 1) & 0x7E, val])

    def _read(self, addr):
        return self.spi.xfer2([((addr << 1) & 0x7E) | 0x80, 0])[1]

    def _set_bits(self, addr, mask):
        self._write(addr, self._read(addr) | mask)

    def _clear_bits(self, addr, mask):
        self._write(addr, self._read(addr) & (~mask))

    # ── 초기화 ──────────────────────────────────────────
    def _init_chip(self):
        self._write(CommandReg, PCD_RESETPHASE)
        time.sleep(0.05)

        # 타이머: 자동 시작, 25ms 타임아웃
        self._write(TModeReg, 0x8D)
        self._write(TPrescalerReg, 0x3E)
        self._write(TReloadRegL, 30)
        self._write(TReloadRegH, 0)

        self._write(TxASKReg, 0x40)   # 100% ASK 변조
        self._write(ModeReg, 0x3D)    # CRC 초기값 0x6363
        self.antenna_on()

    def antenna_on(self):
        if not (self._read(TxControlReg) & 0x03):
            self._set_bits(TxControlReg, 0x03)

    def get_version(self):
        """0x91 또는 0x92면 정상. 0x00/0xFF면 배선 문제"""
        return self._read(VersionReg)

    # ── 카드 통신 ───────────────────────────────────────
    def _transceive(self, send_data):
        back_data = []
        back_bits = 0
        status = MI_ERR

        self._write(ComIEnReg, 0x77 | 0x80)
        self._clear_bits(ComIrqReg, 0x80)
        self._set_bits(FIFOLevelReg, 0x80)      # FIFO 비우기
        self._write(CommandReg, PCD_IDLE)

        for b in send_data:
            self._write(FIFODataReg, b)

        self._write(CommandReg, PCD_TRANSCEIVE)
        self._set_bits(BitFramingReg, 0x80)     # 전송 시작

        # 응답 대기 (타이머 IRQ 또는 수신 완료 IRQ)
        i = 2000
        while True:
            n = self._read(ComIrqReg)
            i -= 1
            if i == 0 or (n & 0x01) or (n & 0x30):
                break

        self._clear_bits(BitFramingReg, 0x80)

        if i == 0:
            return MI_ERR, [], 0

        if self._read(ErrorReg) & 0x1B:
            return MI_ERR, [], 0

        status = MI_OK
        if n & 0x01:                            # 타이머 만료 = 카드 없음
            status = MI_NOTAGERR

        count = self._read(FIFOLevelReg)
        last_bits = self._read(ControlReg) & 0x07
        back_bits = (count - 1) * 8 + last_bits if last_bits else count * 8

        count = max(1, min(count, 16))
        for _ in range(count):
            back_data.append(self._read(FIFODataReg))

        return status, back_data, back_bits

    def request(self):
        """카드가 필드 안에 있는지 확인"""
        self._write(BitFramingReg, 0x07)        # 마지막 바이트 7비트만 전송
        status, data, bits = self._transceive([PICC_REQIDL])
        if status != MI_OK or bits != 0x10:
            return MI_NOTAGERR
        return MI_OK

    def anticoll(self):
        """UID 읽기. (status, uid 리스트) 반환"""
        self._write(BitFramingReg, 0x00)
        status, data, _ = self._transceive([PICC_ANTICOLL, 0x20])

        if status != MI_OK or len(data) != 5:
            return MI_ERR, []

        # 마지막 바이트는 BCC(체크섬) — 앞 4바이트의 XOR과 같아야 함
        checksum = data[0] ^ data[1] ^ data[2] ^ data[3]
        if checksum != data[4]:
            return MI_ERR, []

        return MI_OK, data[:4]

    def close(self):
        self.spi.close()
        self.rst.close()


def main():
    reader = MFRC522()

    # 1. 배선 확인 — 여기서 걸러집니다
    ver = reader.get_version()
    if ver in (0x00, 0xFF):
        print(f"[FAIL] RC522 응답 없음 (VersionReg=0x{ver:02X})")
        print("       3.3V 전원, SPI 활성화, MOSI/MISO 배선을 확인하세요.")
        reader.close()
        return
    print(f"[OK] MFRC522 감지됨 (VersionReg=0x{ver:02X})")
    print("[OK] 준비 완료. 카드를 태그하세요. (Ctrl+C 종료)\n")

    # 2. 태깅 루프
    last_uid = None
    last_time = 0.0
    count = 0

    try:
        while True:
            if reader.request() != MI_OK:
                time.sleep(0.05)
                continue

            status, uid = reader.anticoll()
            if status != MI_OK:
                continue

            uid_hex = "".join(f"{b:02X}" for b in uid)
            now = time.time()

            # 연속 태깅 블록(디바운스)
            if uid_hex == last_uid and (now - last_time) < DEBOUNCE_SEC:
                continue

            last_uid, last_time = uid_hex, now
            count += 1
            print(f"[{count:3d}] UID={uid_hex}")

    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        reader.close()


if __name__ == "__main__":
    main()