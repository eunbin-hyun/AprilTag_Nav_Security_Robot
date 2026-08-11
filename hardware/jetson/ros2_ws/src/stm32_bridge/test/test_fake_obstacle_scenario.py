"""fake_stm32 초음파 장애물 시나리오를 실제 PTY 로 검증한다.

`jetson_ultrasonic_hold_recovery_prompt.md` §fake STM32 확장이 요구하는 순서를
on-wire 로 고정한다.

    DRIVING
    -> SAFE_STOP + bit16=1
    -> neutral 유지
    -> bit16=0
    -> 짧은 SAFE_STOP + bit16=0 구간 (시나리오 B)
    -> READY
    -> 새 CMD_DRIVE -> DRIVING

`SAFE_STOP + bit16=0` 구간이 **실제로 관측되는지**가 핵심이다. 그 구간이
예전에 Jetson 을 영구 FAULT 로 보냈다. 상태머신 쪽 회귀시험은
`robot_navigation/test/test_obstacle_recovery.py` 가 담당하고, 여기서는
STM32 계약(rearm 은 neutral 로만, CMD_STOP 없이 복구)을 확인한다.

socat 이 없으면 skip 한다.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

import serial

from stm32_bridge import protocol as P
from stm32_bridge.parser import FrameParser

pytestmark = pytest.mark.skipif(shutil.which('socat') is None,
                                reason='socat 이 없다')

JET = '/tmp/ttyJETSON_pytest_obst'
STM = '/tmp/ttySTM32_pytest_obst'

OBSTACLE_AT = 0.8       # s
OBSTACLE_HOLD = 0.6     # s
READY_DELAY_MS = 400    # 시나리오 B 를 확정적으로 만든다
RUN_S = 3.2


@pytest.fixture
def link():
    socat = subprocess.Popen(
        ['socat', f'pty,raw,echo=0,link={JET}', f'pty,raw,echo=0,link={STM}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        if os.path.exists(JET) and os.path.exists(STM):
            break
        time.sleep(0.1)
    else:
        socat.terminate()
        pytest.skip('socat PTY 링크가 생성되지 않았다')
    time.sleep(0.3)
    fake = subprocess.Popen(
        [sys.executable, '-m', 'stm32_bridge.fake_stm32', '--port', STM,
         '--obstacle-at', str(OBSTACLE_AT),
         '--obstacle-hold-s', str(OBSTACLE_HOLD),
         '--obstacle-ready-delay-ms', str(READY_DELAY_MS)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.6)
    try:
        yield fake
    finally:
        fake.terminate()
        try:
            fake.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            fake.kill()
        socat.terminate()


def _drive_until_recovered(fake):
    """route_runner 의 TX 정책만 흉내낸다: neutral 20 Hz, CMD_STOP 없음."""
    ser = serial.Serial(JET, 115200, timeout=0)
    parser = FrameParser()
    seq = P.SeqCounter()
    seqs, frames = [], []
    phase = 'neutral'
    t0 = tick = time.monotonic()
    try:
        while time.monotonic() - t0 < RUN_S:
            now = time.monotonic()
            if now >= tick:
                s = seq.next()
                seqs.append(s)
                if phase == 'driving':
                    ser.write(P.pack_cmd_drive(s, 200, 0, True))
                else:
                    ser.write(P.pack_cmd_drive(s, 0, 0, False))
                tick += 0.05
                if tick <= now:
                    tick = now + 0.05
            d = ser.read(4096)
            if d:
                for mid, _sq, pl in parser.feed(d):
                    if mid != P.TELEMETRY_DRIVE:
                        continue
                    t = P.unpack_telemetry(pl)
                    bit16 = bool(t['active_fault_bits']
                                 & P.FAULT_OBSTACLE_NEAR)
                    frames.append((t['drive_state'], bit16,
                                   t['last_drive_seq']))
                    # 장애물이 보이면 neutral, READY 면 다시 주행
                    if bit16:
                        phase = 'neutral'
                    elif t['drive_state'] == P.STATE_READY:
                        phase = 'driving'
            time.sleep(0.002)
    finally:
        ser.close()
    return seqs, frames


def test_fake_reproduces_obstacle_hold_and_rearm(link):
    seqs, frames = _drive_until_recovered(link)
    assert frames, 'telemetry 를 한 프레임도 못 받았다'

    combos = [(st, b) for st, b, _ in frames]
    # bit16=1 + SAFE_STOP 구간이 있어야 한다
    assert (P.STATE_SAFE_STOP, True) in combos, combos[:10]
    # ★ 시나리오 B: bit16=0 인데 아직 SAFE_STOP — 이 구간이 관측되어야 한다
    assert (P.STATE_SAFE_STOP, False) in combos, (
        'SAFE_STOP + bit16=0 구간이 관측되지 않았다 — '
        'obstacle_ready_delay_ms 가 동작하지 않는다')
    # 복구: READY 를 거쳐 DRIVING 으로 돌아온다
    i_obst = combos.index((P.STATE_SAFE_STOP, True))
    after = combos[i_obst:]
    assert (P.STATE_READY, False) in after, after[-10:]
    assert (P.STATE_DRIVING, False) in after[after.index(
        (P.STATE_READY, False)):], '재개되지 않았다'


def test_fake_recovery_needs_no_cmd_stop(link):
    """CMD_STOP 없이 neutral 만으로 SAFE_STOP -> READY 가 된다."""
    _seqs, frames = _drive_until_recovered(link)
    out = link.stdout
    # fake 를 종료시켜 로그를 회수한다
    link.terminate()
    log = link.communicate(timeout=5)[0] if out else ''
    assert 'CMD_STOP' not in log, f'CMD_STOP 이 오갔다:\n{log}'
    assert any(st == P.STATE_READY for st, _b, _s in frames), (
        'CMD_STOP 없이 READY 로 복구되지 못했다')


def test_fake_tracks_every_neutral_seq(link):
    """복구 중 보낸 neutral 의 SEQ 가 last_drive_seq 로 반영된다."""
    seqs, frames = _drive_until_recovered(link)
    assert len(seqs) > 20
    # 매 송신마다 +1 (uint8 wrap 포함)
    assert all((b - a) & 0xFF == 1 for a, b in zip(seqs, seqs[1:]))
    # 장애물 구간에서도 last_drive_seq 가 갱신된다 (watchdog 이 살아 있다)
    obstacle_seqs = [s for st, b, s in frames
                     if b and st == P.STATE_SAFE_STOP]
    assert len(set(obstacle_seqs)) > 1, (
        f'장애물 HOLD 중 last_drive_seq 가 멈췄다: {set(obstacle_seqs)}')
