#!/usr/bin/env python3
"""fake_stm32 의 장애물 시나리오를 실제 UART(socat PTY)로 검증한다.

route_runner 를 띄우지 않고, 그 TX 정책(20 Hz neutral 유지, CMD_STOP 미송신)만
그대로 흉내내어 fake 가 SAFE_STOP -> READY 로 복구하는지 확인한다. 동시에
fake 가 받은 CMD_STOP 개수를 센다.
"""
import subprocess
import sys
import time

import serial

from stm32_bridge import protocol as P
from stm32_bridge.parser import FrameParser

JET, STM = '/tmp/ttyJETSON_wire', '/tmp/ttySTM32_wire'


def main():
    socat = subprocess.Popen(
        ['socat', f'pty,raw,echo=0,link={JET}', f'pty,raw,echo=0,link={STM}'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import os
    for _ in range(80):
        if os.path.exists(JET) and os.path.exists(STM):
            break
        time.sleep(0.1)
    time.sleep(0.3)

    fake = subprocess.Popen(
        [sys.executable, '-m', 'stm32_bridge.fake_stm32', '--port', STM,
         '--obstacle-at', '1.5', '--obstacle-hold-s', '1.0',
         '--obstacle-ready-delay-ms', '600'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(0.6)

    ser = serial.Serial(JET, 115200, timeout=0)
    parser = FrameParser()
    seq = P.SeqCounter()
    seqs_sent = []
    obs = {'seen_bit16_1': 0, 'seen_bit16_0_safestop': 0, 'seen_ready': 0,
           'seen_driving_after': 0}
    t0 = time.monotonic()
    phase = 'arming'
    tick = t0
    last_state, last_bit16 = None, None
    trace = []

    while time.monotonic() - t0 < 6.0:
        now = time.monotonic()
        if now >= tick:
            # route_runner 와 같은 정책: 주행 중엔 enable=1, 그 외 neutral.
            # CMD_STOP 은 어떤 경우에도 보내지 않는다.
            s = seq.next()
            seqs_sent.append(s)
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
                st = t['drive_state']
                bit16 = bool(t['active_fault_bits'] & P.FAULT_OBSTACLE_NEAR)
                if (st, bit16) != (last_state, last_bit16):
                    trace.append((round(now - t0, 2),
                                  P.STATE_NAMES[st], int(bit16)))
                    last_state, last_bit16 = st, bit16
                if bit16 and st == P.STATE_SAFE_STOP:
                    obs['seen_bit16_1'] += 1
                if not bit16 and st == P.STATE_SAFE_STOP:
                    obs['seen_bit16_0_safestop'] += 1
                if st == P.STATE_READY:
                    obs['seen_ready'] += 1
                    if phase == 'arming':
                        phase = 'driving'
                    elif phase == 'held':
                        phase = 'driving'
                if st == P.STATE_DRIVING and obs['seen_bit16_1']:
                    obs['seen_driving_after'] += 1
                if bit16:
                    phase = 'held'          # 장애물 → neutral 로 전환
        time.sleep(0.002)

    ser.close()
    fake.terminate()
    out = fake.communicate(timeout=5)[0]
    socat.terminate()

    print('=== fake 로그 ===')
    for line in out.splitlines()[:40]:
        print(' ', line)
    print('\n=== state/bit16 전이 (on-wire telemetry) ===')
    for t, st, b in trace:
        print(f'  t={t:>5}s  state={st:<9} bit16={b}')
    print('\n=== 관측 요약 ===')
    for k, v in obs.items():
        print(f'  {k:<24} {v} 프레임')
    stop_logs = [x for x in out.splitlines() if 'CMD_STOP' in x]
    print(f'\n  fake 가 수신한 CMD_STOP: {len(stop_logs)} 건 {stop_logs}')
    print(f'  보낸 CMD_DRIVE 수: {len(seqs_sent)}')
    strictly_inc = all((b - a) & 0xFF == 1
                       for a, b in zip(seqs_sent, seqs_sent[1:]))
    print(f'  SEQ 매 송신 +1 (uint8 wrap 포함): {strictly_inc}')

    ok = (obs['seen_bit16_1'] > 0 and obs['seen_bit16_0_safestop'] > 0
          and obs['seen_ready'] > 0 and obs['seen_driving_after'] > 0
          and not stop_logs and strictly_inc)
    print('\n결과:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
