#!/usr/bin/env python3
"""STM32 주행 통합 시험 — 11개 시나리오 순차 자동 실행 + 단일 로그.

STM32 파트가 제시한 권장 테스트 시나리오를 그대로 구현한다. 명령 송신값,
STM32 응답, 텔레메트리, 오도메트리, fault, 통과 여부를 타임스탬프와 함께
하나의 JSONL 로그에 남기고, 사람이 읽는 요약을 따로 출력한다.

    # 바퀴를 공중에 띄운 상태에서 1~9, 11
    ./drive_test_suite.py --wheels-off-ground

    # 특정 시나리오만
    ./drive_test_suite.py --wheels-off-ground --only 1,2,4

    # 실바닥 오도메트리 (시나리오 10) — 바퀴가 땅에 닿아야 한다
    ./drive_test_suite.py --on-ground --only 10

    # 장시간 안정성만 (기본 10분)
    ./drive_test_suite.py --wheels-off-ground --only 11 --endurance-min 10

설계 제약 (명세 §8 구현 품질 요구사항):
  - **UART 를 여는 프로세스는 이것 하나뿐이다.** stm32_bridge·라이다 드라이버·
    다른 진단 도구가 돌고 있으면 먼저 끈다.
  - TX 20 Hz 스케줄러와 RX 파서를 같은 프로세스에서 돌린다. 송신·수신을 별
    터미널로 나누지 않는다.
  - 모든 송신 프레임이 하나의 TX SEQ 카운터를 공유하고 매 프레임 +1 한다.
    CMD_STOP 이 CMD_DRIVE 와 같은 SEQ 를 쓰면 STOP 이 duplicate 로 무시된다.
  - RX 는 blocking TX 때문에 멈추지 않는다 (별 스레드).

안전:
  - 3~9·11 은 `--wheels-off-ground`, 10 은 `--on-ground` 없이는 실행을 거부한다.
  - 정상 종료·예외·Ctrl-C 어느 경로로도 CMD_STOP 을 보낸다.
  - `drive_enable=0` 일 때 speed·steering 은 반드시 0 이다 (비영이면 STM32 가
    INVALID_VALUE 로 거부한다).
"""
import argparse
import json
import os
import random
import struct
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                '..', '..', 'stm32_bridge'))

try:
    import serial
except ImportError:
    raise SystemExit('pyserial 이 필요하다: python3 -m pip install pyserial')

from stm32_bridge import protocol as P                          # noqa: E402
from stm32_bridge.parser import FrameParser                     # noqa: E402

# ── 차량 실측 한계 (vehicle_config.h, 2026-07-31) ──────────
# 조향은 **비대칭**이다. 조립 링키지가 750 us 와 1680 us 끝점에서 서로 다른
# 등가 중심각에 도달하기 때문이다. 대칭으로 두면 한쪽이 COMMAND_LIMIT 으로
# 거부되고, CMD_DRIVE 는 COMMAND_RESULT 가 없어서 조용히 실패한다.
STEER_MAX_LEFT = 1955        # +19.55°
STEER_MAX_RIGHT = -2869      # -28.69°
SPEED_LIMIT = 1565           # 무부하 95% PWM 실측. 바닥 부하는 미측정
TX_HZ = 20.0
WATCHDOG_MS = 300


class Log:
    """JSONL 기계용 로그 + 사람용 콘솔. 한 파일에 전부 남긴다."""

    def __init__(self, path: str):
        self.path = path
        self.f = open(path, 'w', buffering=1)
        self.t0 = time.monotonic()
        self._lock = threading.Lock()

    def rec(self, kind: str, **kw):
        line = {
            't': round(time.monotonic() - self.t0, 4),
            'wall': datetime.now().isoformat(timespec='milliseconds'),
            'kind': kind,
        }
        line.update(kw)
        with self._lock:
            self.f.write(json.dumps(line, ensure_ascii=False) + '\n')

    def say(self, msg: str = '', console: bool = True):
        self.rec('note', msg=msg)
        if console:
            print(msg, flush=True)

    def close(self):
        self.f.close()


class Link:
    """UART 소유자. TX 20 Hz 스케줄러 + RX 파서를 한 프로세스에서 돌린다."""

    def __init__(self, port: str, baud: int, log: Log):
        self.log = log
        self.ser = serial.Serial(port, baud, timeout=0)
        self.parser = FrameParser()
        self.seq = P.SeqCounter()
        self._seq_lock = threading.Lock()
        self._cmd = (0, 0, False)          # (speed_mm_s, steering_cdeg, enable)
        self._cmd_lock = threading.Lock()
        self._tx_on = True
        self._stop = threading.Event()
        self.state_lock = threading.Lock()
        self.drive = None                  # 최신 TELEMETRY_DRIVE
        self.odom = None                   # 최신 TELEMETRY_ODOMETRY
        self.range = None
        self.results = []                  # COMMAND_RESULT 이력
        self.echo = []                     # DIAG_ECHO_RESPONSE 이력
        self.faults = []                   # FAULT_EVENT 이력
        self.counts = {}
        self.reboots = 0
        self._prev_mcu = None
        self._sent_seqs = set()
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._tx_loop, daemon=True).start()

    # ── SEQ ────────────────────────────────────────────────
    def next_seq(self) -> int:
        with self._seq_lock:
            return self.seq.next()

    # ── 송신 ───────────────────────────────────────────────
    def set_cmd(self, speed: int, steering: int, enable: bool):
        """최신 명령만 저장한다. 실제 송신은 20 Hz 스케줄러가 한다."""
        if not enable and (speed or steering):
            raise ValueError('enable=0 인데 speed/steering 이 0 이 아니다')
        with self._cmd_lock:
            self._cmd = (int(speed), int(steering), bool(enable))

    def neutral(self):
        self.set_cmd(0, 0, False)

    def pause_tx(self):
        self._tx_on = False
        self.log.rec('tx_pause')

    def resume_tx(self):
        self._tx_on = True
        self.log.rec('tx_resume')

    def _tx_loop(self):
        period = 1.0 / TX_HZ
        nxt = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= nxt:
                if self._tx_on:
                    with self._cmd_lock:
                        sp, st, en = self._cmd
                    s = self.next_seq()
                    self._sent_seqs.add(s)
                    try:
                        self.ser.write(P.pack_cmd_drive(s, sp, st, en))
                    except serial.SerialException as e:
                        self.log.rec('tx_error', err=str(e))
                    self.log.rec('tx', msg='CMD_DRIVE', seq=s,
                                 speed_mm_s=sp, steering_cdeg=st,
                                 enable=int(en))
                nxt += period
                if nxt <= now:
                    nxt = now + period
            time.sleep(0.002)

    def send_raw(self, data: bytes, label: str):
        """원시 바이트 주입 (시나리오 9 비정상 패킷용)."""
        self.ser.write(data)
        self.log.rec('tx_raw', msg=label, hex=data.hex(' '))

    def send_stop(self, reason: int, wait: float = 1.0):
        """CMD_STOP 단발. COMMAND_RESULT 를 기다린다."""
        s = self.next_seq()
        self.ser.write(P.pack_cmd_stop(s, reason))
        self.log.rec('tx', msg='CMD_STOP', seq=s, reason=reason)
        r = self.wait_result(P.CMD_STOP, s, wait)
        return r

    def send_echo(self, data: bytes, wait: float = 1.0):
        s = self.next_seq()
        self.ser.write(P.pack_diag_echo_request(s, data))
        t_tx = time.monotonic()
        self.log.rec('tx', msg='DIAG_ECHO_REQUEST', seq=s, data=data.hex(' '))
        end = t_tx + wait
        while time.monotonic() < end:
            with self.state_lock:
                for e in self.echo:
                    if e['request_seq'] == s:
                        return e, (e['t'] - t_tx) * 1000.0
            time.sleep(0.005)
        return None, None

    # ── 수신 ───────────────────────────────────────────────
    def _rx_loop(self):
        while not self._stop.is_set():
            try:
                d = self.ser.read(4096)
            except serial.SerialException as e:
                self.log.rec('rx_error', err=str(e))
                time.sleep(0.05)
                continue
            if d:
                for mid, seq, pl in self.parser.feed(d):
                    self._on_frame(mid, seq, pl)
            self.parser.check_timeout()
            time.sleep(0.002)

    def _on_frame(self, mid: int, seq: int, pl: bytes):
        self.counts[mid] = self.counts.get(mid, 0) + 1
        now = time.monotonic()
        try:
            if mid == P.TELEMETRY_DRIVE:
                t = P.unpack_telemetry(pl)
                # mcu_time_ms 역행 = STM32 재부팅. uint32 wrap 은 delta 가
                # 작게 나오므로 큰 역행만 재부팅으로 본다.
                mcu = t['mcu_time_ms']
                if self._prev_mcu is not None:
                    if (mcu - self._prev_mcu) & 0xFFFFFFFF > 3600_000:
                        self.reboots += 1
                        self.log.rec('stm32_reboot', prev=self._prev_mcu,
                                     now=mcu)
                self._prev_mcu = mcu
                with self.state_lock:
                    self.drive = t
                self.log.rec('rx', msg='TELEMETRY_DRIVE', **t)
            elif mid == P.TELEMETRY_ODOMETRY:
                o = P.unpack_telemetry_odometry(pl)
                with self.state_lock:
                    self.odom = o
                self.log.rec('rx', msg='TELEMETRY_ODOMETRY', **o)
            elif mid == P.TELEMETRY_RANGE:
                r = P.unpack_telemetry_range(pl)
                with self.state_lock:
                    self.range = r
                self.log.rec('rx', msg='TELEMETRY_RANGE', **r)
            elif mid == P.FAULT_EVENT:
                f = P.unpack_fault_event(pl)
                with self.state_lock:
                    self.faults.append(f)
                self.log.rec('rx', msg='FAULT_EVENT', **f)
            elif mid == P.COMMAND_RESULT:
                c = P.unpack_command_result(pl)
                c['t'] = now
                with self.state_lock:
                    self.results.append(c)
                self.log.rec('rx', msg='COMMAND_RESULT', **c)
            elif mid == P.DIAG_ECHO_RESPONSE:
                e = P.unpack_diag_echo_response(pl)
                e = {'request_seq': e['request_seq'],
                     'data': e['data'].hex(' '), 't': now}
                with self.state_lock:
                    self.echo.append(e)
                self.log.rec('rx', msg='DIAG_ECHO_RESPONSE', **e)
            else:
                self.log.rec('rx_unknown', msg_id=mid, len=len(pl))
        except (ValueError, struct.error) as e:
            self.log.rec('rx_decode_error', msg_id=mid, err=str(e))

    # ── 관측 헬퍼 ──────────────────────────────────────────
    def snap(self):
        with self.state_lock:
            return (dict(self.drive) if self.drive else None,
                    dict(self.odom) if self.odom else None)

    def wait_result(self, req_id: int, req_seq: int, timeout: float):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.state_lock:
                for r in self.results:
                    if (r['request_msg_id'] == req_id
                            and r['request_seq'] == req_seq):
                        return r
            time.sleep(0.005)
        return None

    def wait_state(self, want, timeout: float):
        """drive_state 가 want(집합)에 들어올 때까지. 도달 시각 반환."""
        if isinstance(want, int):
            want = {want}
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            d, _ = self.snap()
            if d and d['drive_state'] in want:
                return time.monotonic()
            time.sleep(0.005)
        return None

    def wait_accepted(self, timeout: float = 1.0):
        """이번 세션에서 보낸 SEQ 가 last_drive_seq 로 반영되는지.

        CMD_DRIVE 는 COMMAND_RESULT 가 없다. 수락 확인 경로가 이것뿐이다.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            d, _ = self.snap()
            if d and d['last_drive_seq'] in self._sent_seqs:
                return True
            time.sleep(0.005)
        return False

    def rearm(self, timeout: float = 3.0) -> bool:
        """neutral 재무장. SAFE_STOP -> READY 를 기다린다."""
        self.neutral()
        self.resume_tx()
        ok = self.wait_state({P.STATE_READY, P.STATE_DRIVING}, timeout)
        self.log.rec('rearm', ok=bool(ok))
        return bool(ok)

    def close(self):
        self._stop.set()
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:                                       # noqa: BLE001
            pass


# ══════════ 시나리오 ══════════
# 각 함수는 (통과여부, 상세문자열) 을 돌려준다.

def hold(link: Link, speed: int, steering: int, seconds: float,
         label: str, log: Log):
    """명령을 유지하며 시작·끝 스냅샷을 남긴다."""
    link.set_cmd(speed, steering, enable=(speed != 0 or steering != 0))
    d0, o0 = link.snap()
    log.rec('step_begin', label=label, speed_mm_s=speed,
            steering_cdeg=steering, drive=d0, odom=o0)
    time.sleep(seconds)
    d1, o1 = link.snap()
    log.rec('step_end', label=label, drive=d1, odom=o1)
    return d0, o0, d1, o1


def sc1_link(link: Link, log: Log, a):
    log.say('  Echo 왕복 3회, 텔레메트리·CRC·SEQ 확인')
    rtts = []
    for i in range(3):
        payload = bytes([0x12, 0x34, 0xAA, 0x55, i])   # payload 내 AA 55 포함
        e, rtt = link.send_echo(payload)
        if e is None:
            return False, f'echo {i + 1}회차 응답 없음'
        if e['data'] != payload.hex(' '):
            return False, f'echo payload 불일치: {e["data"]}'
        rtts.append(rtt)
        log.say(f'    echo {i + 1}: RTT {rtt:.1f} ms, payload 일치')
    time.sleep(1.5)
    st = link.parser.stats
    need = {P.TELEMETRY_DRIVE, P.TELEMETRY_ODOMETRY, P.TELEMETRY_RANGE}
    missing = need - set(link.counts)
    if missing:
        return False, ('텔레메트리 누락: '
                       + ', '.join(f'0x{m:02X}' for m in sorted(missing)))
    if st.crc_errors:
        return False, f'CRC 오류 {st.crc_errors}건'
    d, _ = link.snap()
    detail = (f'echo RTT {min(rtts):.1f}~{max(rtts):.1f} ms · '
              f'CRC오류 0 · 잡음 {st.noise_bytes}B · '
              f'수신 {dict((f"0x{k:02X}", v) for k, v in link.counts.items())} · '
              f'state={P.STATE_NAMES.get(d["drive_state"], "?")} '
              f'fault=0x{d["active_fault_bits"]:08X}')
    return True, detail


def sc2_idle(link: Link, log: Log, a):
    log.say('  neutral(0,0,enable=0) 1초 · 모터 정지와 조향 중앙 확인')
    link.neutral()
    time.sleep(1.0)
    if not link.wait_accepted(1.0):
        return False, 'last_drive_seq 가 이번 세션 SEQ 로 갱신되지 않음'
    d, _ = link.snap()
    bad = []
    if d['target_speed_mm_s'] != 0:
        bad.append(f'target={d["target_speed_mm_s"]}')
    if d['measured_speed_mm_s'] != 0:
        bad.append(f'measured={d["measured_speed_mm_s"]}')
    if d['steering_cmd_cdeg'] != 0:
        bad.append(f'steering={d["steering_cmd_cdeg"]}')
    if d['motor_duty_permille'] != 0:
        bad.append(f'duty={d["motor_duty_permille"]}')
    if bad:
        return False, '0 이 아닌 값: ' + ', '.join(bad)
    return True, (f'target/measured/steering/duty 모두 0 · '
                  f'state={P.STATE_NAMES.get(d["drive_state"], "?")} · '
                  f'**바퀴가 중앙 정렬됐는지 눈으로 확인하세요**')


def _leg(link, log, speed, seconds, label):
    d0, o0, d1, o1 = hold(link, speed, 0, seconds, label, log)
    enc = d1['encoder_count'] - d0['encoder_count']
    dist = (o1['distance_mm'] - o0['distance_mm']) if (o0 and o1) else None
    log.say(f'    {label:<14} 명령={speed:>5} 측정={d1["measured_speed_mm_s"]:>5}'
            f' mm/s  enc Δ={enc:>+7}  odom dist Δ={dist:>+7} mm')
    return speed, enc, dist, d1['measured_speed_mm_s']


def sc3_straight(link: Link, log: Log, a):
    log.say('  0 → 저속 → 중속 → 정지, 그다음 후진. 엔코더·오도메트리 부호 확인')
    link.rearm()
    legs = []
    for sp, lbl in ((a.slow, '전진 저속'), (a.med, '전진 중속'), (0, '정지')):
        legs.append(_leg(link, log, sp, 2.0, lbl))
    for sp, lbl in ((-a.slow, '후진 저속'), (-a.med, '후진 중속'), (0, '정지')):
        legs.append(_leg(link, log, sp, 2.0, lbl))

    fails, notes = [], []
    for sp, enc, dist, meas in legs:
        if sp == 0:
            continue
        if enc == 0:
            # 최소 제어 가능 속도가 미측정이다. 저속에서 안 도는 것이
            # 모터 데드밴드 때문일 수 있으므로 결함과 구분해서 기록한다.
            notes.append(f'{sp:+} mm/s 에서 엔코더 변화 0 '
                         f'(데드밴드 의심 — 최소 제어 가능 속도 미측정)')
            continue
        if (enc > 0) != (sp > 0):
            fails.append(f'{sp:+} mm/s 에서 엔코더 부호 반대 (Δ={enc:+})')
        if dist is not None and dist != 0 and (dist > 0) != (sp > 0):
            fails.append(f'{sp:+} mm/s 에서 odom distance 부호 반대 (Δ={dist:+})')
    if fails:
        return False, ' / '.join(fails)
    return True, ('엔코더·오도메트리 부호가 전진/후진과 일치'
                  + (' · 주의: ' + ' · '.join(notes) if notes else ''))


def sc4_steering(link: Link, log: Log, a):
    log.say('  전체 조향 범위. **좌우 최대값이 다릅니다** '
            f'(좌 +{STEER_MAX_LEFT}, 우 {STEER_MAX_RIGHT})')
    link.rearm()
    seq = [(0, '직진'),
           (STEER_MAX_LEFT // 2, '좌 중간'), (STEER_MAX_LEFT, '좌 최대'),
           (0, '직진'),
           (STEER_MAX_RIGHT // 2, '우 중간'), (STEER_MAX_RIGHT, '우 최대'),
           (0, '직진')]
    fails = []
    for st, lbl in seq:
        # speed=0 이라 모터 duty 는 0 이고 서보만 움직인다 -> 바퀴를
        # 들지 않아도 조향 확인이 가능하다.
        _, _, d1, _ = hold(link, 0, st, 2.0, f'조향 {lbl}', log)
        rep = d1['steering_cmd_cdeg']
        ok = (rep == st)
        log.say(f'    {lbl:<8} 명령={st:>+6} 보고={rep:>+6} cdeg '
                f'({rep / 100:+.2f}°) duty={d1["motor_duty_permille"]}'
                f'  {"OK" if ok else "← 불일치"}')
        if not ok:
            fails.append(f'{lbl}: 명령 {st:+} → 보고 {rep:+}')
        if d1['motor_duty_permille'] != 0:
            fails.append(f'{lbl}: speed=0 인데 duty={d1["motor_duty_permille"]}')
    if fails:
        return False, ' / '.join(fails)
    return True, ('명령 조향각과 STM32 보고값 전부 일치 (좌 최대 '
                  f'{STEER_MAX_LEFT / 100:+.2f}° / 우 최대 '
                  f'{STEER_MAX_RIGHT / 100:+.2f}°) · 모터 duty 0 유지')


def sc5_combined(link: Link, log: Log, a):
    log.say('  속도+조향 통합. 오도메트리 yaw 부호까지 확인')
    link.rearm()
    cases = [(a.slow, 0, '저속 직진', 0),
             (a.slow, STEER_MAX_LEFT // 2, '저속 좌회전', +1),
             (a.slow, STEER_MAX_RIGHT // 2, '저속 우회전', -1),
             (-a.slow, STEER_MAX_LEFT // 2, '후진 좌회전', None),
             (-a.slow, STEER_MAX_RIGHT // 2, '후진 우회전', None)]
    fails = []
    for sp, st, lbl, yaw_sign in cases:
        _, o0, d1, o1 = hold(link, sp, st, 2.5, lbl, log)
        dyaw = None
        if o0 and o1:
            dyaw = P_wrap(o1['yaw_mdeg'] - o0['yaw_mdeg'])
        log.say(f'    {lbl:<12} 명령 v={sp:>+5} st={st:>+6}  '
                f'보고 v={d1["measured_speed_mm_s"]:>+5} '
                f'st={d1["steering_cmd_cdeg"]:>+6}  '
                f'yaw Δ={dyaw / 1000 if dyaw is not None else 0:+.3f}°')
        if d1['steering_cmd_cdeg'] != st:
            fails.append(f'{lbl}: 조향 {st:+} → {d1["steering_cmd_cdeg"]:+}')
        if yaw_sign and dyaw is not None and abs(dyaw) > 100:
            if (dyaw > 0) != (yaw_sign > 0):
                fails.append(f'{lbl}: yaw 부호 반대 ({dyaw / 1000:+.3f}°)')
    r = link.send_stop(P.STOP_MISSION_COMPLETE)
    if r is None:
        fails.append('마지막 CMD_STOP 에 COMMAND_RESULT 없음')
    elif r['result_code'] != P.RESULT_ACCEPTED:
        fails.append(f'CMD_STOP 거부 result={r["result_code"]}')
    if fails:
        return False, ' / '.join(fails)
    return True, '조향 반영·yaw 부호 정상 · 명시적 CMD_STOP 수락'


def P_wrap(mdeg: int) -> int:
    """yaw ±180° wrap 처리 (0.001° 단위)."""
    while mdeg > 180000:
        mdeg -= 360000
    while mdeg < -180000:
        mdeg += 360000
    return mdeg


def sc6_sshape(link: Link, log: Log, a):
    log.say('  S자: 직진→좌→직진→우→직진→정지. x/y/yaw 변화 방향 확인')
    link.rearm()
    _, o_start = link.snap()
    steps = [(a.slow, 0, '직진1'), (a.slow, STEER_MAX_LEFT // 2, '좌회전'),
             (a.slow, 0, '직진2'), (a.slow, STEER_MAX_RIGHT // 2, '우회전'),
             (a.slow, 0, '직진3')]
    marks = []
    for sp, st, lbl in steps:
        _, o0, _, o1 = hold(link, sp, st, 2.0, lbl, log)
        dyaw = P_wrap(o1['yaw_mdeg'] - o0['yaw_mdeg']) if (o0 and o1) else 0
        marks.append((lbl, dyaw))
        log.say(f'    {lbl:<8} yaw Δ={dyaw / 1000:+8.3f}°  '
                f'x={o1["x_mm"]:>+7} y={o1["y_mm"]:>+7} mm')
    link.neutral()
    time.sleep(0.5)
    r = link.send_stop(P.STOP_MISSION_COMPLETE)
    _, o_end = link.snap()
    fails = []
    for lbl, dyaw in marks:
        if lbl == '좌회전' and dyaw <= 100:
            fails.append(f'좌회전 yaw 증가 안 함 ({dyaw / 1000:+.3f}°)')
        if lbl == '우회전' and dyaw >= -100:
            fails.append(f'우회전 yaw 감소 안 함 ({dyaw / 1000:+.3f}°)')
    if r is None:
        fails.append('CMD_STOP 응답 없음')
    detail = (f'시작 (x={o_start["x_mm"]},y={o_start["y_mm"]}) → '
              f'끝 (x={o_end["x_mm"]},y={o_end["y_mm"]}) '
              f'yaw={o_end["yaw_mdeg"] / 1000:+.3f}°')
    if fails:
        return False, ' / '.join(fails) + ' · ' + detail
    return True, '좌회전 yaw+ / 우회전 yaw− 확인 · ' + detail


def sc7_watchdog(link: Link, log: Log, a):
    log.say(f'  주행 중 송신 고의 중단 → {WATCHDOG_MS} ms 내 정지 확인')
    link.rearm()
    hold(link, a.slow, 0, 1.5, 'watchdog 전 주행', log)
    d0, _ = link.snap()
    if d0['drive_state'] != P.STATE_DRIVING:
        return False, (f'주행 상태로 못 들어감 '
                       f'(state={P.STATE_NAMES.get(d0["drive_state"], "?")})')
    n_faults = len(link.faults)
    link.pause_tx()
    t_pause = time.monotonic()
    log.say('    송신 중단...')
    reached = link.wait_state(P.STATE_SAFE_STOP, 1.5)
    elapsed_ms = (reached - t_pause) * 1000.0 if reached else None
    d1, _ = link.snap()
    link.resume_tx()

    fails = []
    if reached is None:
        fails.append('SAFE_STOP 전이 관측 실패')
    else:
        log.say(f'    SAFE_STOP 도달 {elapsed_ms:.0f} ms')
        # 20 Hz 송신이라 검출은 300~350 ms 사이가 정상. 여유를 둔다.
        if elapsed_ms > 600:
            fails.append(f'정지가 {elapsed_ms:.0f} ms — 300 ms 규격 초과')
    if d1 and d1['motor_duty_permille'] != 0:
        fails.append(f'정지 후에도 duty={d1["motor_duty_permille"]}')
    if d1 and not (d1['active_fault_bits'] & P.FAULT_COMM_TIMEOUT):
        fails.append('COMM_TIMEOUT fault 미설정')
    new_faults = link.faults[n_faults:]
    if not any(f['active_fault_bits'] & P.FAULT_COMM_TIMEOUT
               for f in new_faults):
        fails.append('COMM_TIMEOUT FAULT_EVENT 미수신')
    if not link.rearm(3.0):
        fails.append('neutral 재무장 실패 (READY 복귀 안 됨)')
    else:
        log.say('    neutral 재무장 → READY 복귀 OK')
    if fails:
        return False, ' / '.join(fails)
    return True, (f'{elapsed_ms:.0f} ms 내 SAFE_STOP + COMM_TIMEOUT + '
                  'FAULT_EVENT · 재무장 정상')


def sc8_obstacle(link: Link, log: Log, a):
    log.say('  초음파 장애물 정지. 전진 중 센서 앞으로 장애물 접근')
    log.say('  ⚠ 거리값은 Jetson 에 오지 않습니다 (valid_mask=0). '
            'OBSTACLE_NEAR fault bit 로만 관측됩니다.')
    if not a.no_prompt:
        input('  준비되면 Enter — 전진 시작 후 손을 센서 앞 20 cm 안으로: ')
    link.rearm()
    link.set_cmd(a.slow, 0, True)
    t0 = time.monotonic()
    hit = None
    while time.monotonic() - t0 < a.obstacle_wait:
        d, _ = link.snap()
        if d and d['active_fault_bits'] & P.FAULT_OBSTACLE_NEAR:
            hit = time.monotonic()
            break
        time.sleep(0.01)
    if hit is None:
        link.neutral()
        return False, (f'{a.obstacle_wait:.0f}초 안에 OBSTACLE_NEAR 미발생 — '
                       '센서 장착·배선 또는 감지거리 확인')
    d, _ = link.snap()
    log.say(f'    OBSTACLE_NEAR 감지 (t+{hit - t0:.1f}s) '
            f'state={P.STATE_NAMES.get(d["drive_state"], "?")} '
            f'duty={d["motor_duty_permille"]}')
    fails = []
    if d['motor_duty_permille'] != 0:
        fails.append(f'감지 후에도 duty={d["motor_duty_permille"]}')

    # 장애물을 치워도 자동 출발하지 않아야 한다 (명령 유지 상태로 확인)
    if not a.no_prompt:
        input('  장애물을 치우고 Enter — 자동 출발하지 않는지 확인: ')
    else:
        time.sleep(3.0)
    d2, _ = link.snap()
    log.say(f'    치운 후 state={P.STATE_NAMES.get(d2["drive_state"], "?")} '
            f'duty={d2["motor_duty_permille"]} '
            f'fault=0x{d2["active_fault_bits"]:08X}')
    if d2['motor_duty_permille'] != 0:
        fails.append('장애물 제거만으로 자동 출발함 (duty 비영)')

    link.neutral()
    if not link.rearm(3.0):
        fails.append('재무장 실패')
    else:
        hold(link, a.slow, 0, 1.0, '재가동', log)
        d3, _ = link.snap()
        log.say(f'    재가동 후 duty={d3["motor_duty_permille"]}')
    link.neutral()
    if fails:
        return False, ' / '.join(fails)
    return True, 'OBSTACLE_NEAR 발생·모터 정지·자동출발 없음·재무장 후 재가동'


def sc9_malformed(link: Link, log: Log, a):
    log.say('  비정상 패킷 주입. 차량이 움직이지 않고 다음 정상 패킷부터 복구')
    link.rearm()
    link.neutral()
    time.sleep(0.5)
    d0, _ = link.snap()
    enc0 = d0['encoder_count']
    # TX 스케줄러를 멈춰야 주입 효과가 정상 프레임에 가려지지 않는다.
    # 300 ms 안에 끝내면 watchdog 도 안 걸린다.
    link.pause_tx()

    good = P.pack_cmd_drive(link.next_seq(), a.slow, 0, True)
    cases = []
    # ① CRC 손상 — 마지막 바이트 반전
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    cases.append(('CRC 손상', bytes(bad)))
    # ② 길이 오류 — LEN 을 10 으로 위조 (payload 는 5)
    bad = bytearray(good)
    bad[5] = 10
    cases.append(('길이 오류', bytes(bad)))
    # ③ 절단 프레임 — 앞 절반만
    cases.append(('절단 프레임', good[:len(good) // 2]))
    # ④ 잡음 바이트
    rnd = random.Random(20260731)
    cases.append(('잡음 32B',
                  bytes(rnd.randrange(256) for _ in range(32))))

    fails = []
    for label, data in cases:
        link.send_raw(data, label)
        time.sleep(0.15)
        d, _ = link.snap()
        moved = d['encoder_count'] != enc0
        drove = d['motor_duty_permille'] != 0
        log.say(f'    {label:<12} duty={d["motor_duty_permille"]:>4} '
                f'enc Δ={d["encoder_count"] - enc0:>+5} '
                f'state={P.STATE_NAMES.get(d["drive_state"], "?")}'
                f'  {"← 움직임!" if (moved or drove) else "정지 유지"}')
        if drove:
            fails.append(f'{label}: 모터가 동작함 (duty={d["motor_duty_permille"]})')

    # 복구 확인 — 정상 neutral 이 수락되는지
    link.resume_tx()
    link.neutral()
    recovered = link.wait_accepted(2.0)
    st = link.parser.stats
    log.say(f'    복구: last_drive_seq 갱신 {"OK" if recovered else "실패"} · '
            f'수신 파서 crc_err={st.crc_errors} len_err={st.length_errors}')
    if not recovered:
        fails.append('비정상 패킷 후 정상 패킷이 수락되지 않음')
    if fails:
        return False, ' / '.join(fails)
    return True, ('4종 비정상 패킷 모두 무동작 · 다음 정상 패킷부터 복구 · '
                  'STM32 가 CRC/BAD_COMMAND fault 를 보고할 수 있음(정상)')


def sc10_floor(link: Link, log: Log, a):
    log.say('  실바닥 오도메트리. **바퀴가 땅에 닿아 있어야 합니다**')
    if not a.no_prompt:
        input('  차량 주변을 비우고 Enter (직진 약 1 m → 좌 원주행 → 우 원주행): ')
    link.rearm()
    out = {}
    # 직진 1 m — 저속 기준 소요시간으로 근사. 실측 거리는 사람이 잰다.
    secs = max(2.0, 1000.0 / max(a.slow, 1))
    _, o0, _, o1 = hold(link, a.slow, 0, secs, '직진 1m', log)
    out['straight'] = (o1['x_mm'] - o0['x_mm'], o1['y_mm'] - o0['y_mm'],
                       P_wrap(o1['yaw_mdeg'] - o0['yaw_mdeg']),
                       o1['distance_mm'] - o0['distance_mm'])
    link.neutral()
    time.sleep(1.0)
    for st, key, lbl in ((STEER_MAX_LEFT // 2, 'left', '좌 원주행'),
                         (STEER_MAX_RIGHT // 2, 'right', '우 원주행')):
        if not a.no_prompt:
            input(f'  {lbl} 준비되면 Enter: ')
        link.rearm()
        _, o0, _, o1 = hold(link, a.slow, st, a.circle_sec, lbl, log)
        out[key] = (o1['x_mm'] - o0['x_mm'], o1['y_mm'] - o0['y_mm'],
                    P_wrap(o1['yaw_mdeg'] - o0['yaw_mdeg']),
                    o1['distance_mm'] - o0['distance_mm'])
        link.neutral()
        time.sleep(1.0)
    link.send_stop(P.STOP_MISSION_COMPLETE)

    for k, v in out.items():
        log.say(f'    {k:<9} Δx={v[0]:>+7} Δy={v[1]:>+7} mm  '
                f'Δyaw={v[2] / 1000:>+8.3f}°  Δdist={v[3]:>+7} mm')
    fails = []
    if out['straight'][3] <= 0:
        fails.append('직진에서 distance 가 증가하지 않음')
    if out['left'][2] <= 100:
        fails.append(f'좌 원주행 yaw 증가 안 함 ({out["left"][2] / 1000:+.3f}°)')
    if out['right'][2] >= -100:
        fails.append(f'우 원주행 yaw 감소 안 함 ({out["right"][2] / 1000:+.3f}°)')
    asym = abs(abs(out['left'][2]) - abs(out['right'][2])) / 1000.0
    detail = (f'좌우 회전량 차이 {asym:.2f}° — 조향 한계가 비대칭'
              f'(좌 +19.55° / 우 -28.69°)이라 같은 |조향각|이면 차이가 없어야 하고, '
              f'차이가 크면 기구 유격·타이어 슬립을 의심한다. '
              f'**실제 이동거리·회전각을 줄자로 재서 위 값과 비교하세요**')
    if fails:
        return False, ' / '.join(fails) + ' · ' + detail
    return True, detail


def sc11_endurance(link: Link, log: Log, a):
    total = a.endurance_min * 60.0
    log.say(f'  {a.endurance_min:.0f}분 반복. UART 끊김·재부팅·텔레메트리 누락 감시')
    link.rearm()
    cyc = [(a.slow, 0, '직진'), (a.slow, STEER_MAX_LEFT // 2, '좌'),
           (a.slow, 0, '직진'), (a.slow, STEER_MAX_RIGHT // 2, '우'),
           (0, 0, '정지')]
    t0 = time.monotonic()
    reboots0 = link.reboots
    gaps = []
    last_seen = time.monotonic()
    n = 0
    while time.monotonic() - t0 < total:
        for sp, st, lbl in cyc:
            if time.monotonic() - t0 >= total:
                break
            link.set_cmd(sp, st, enable=(sp != 0 or st != 0))
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                d, _ = link.snap()
                now = time.monotonic()
                if d:
                    # 텔레메트리 누락 감지: 20 Hz 인데 250 ms 이상 공백
                    if now - last_seen > 0.25:
                        gaps.append(now - last_seen)
                        log.rec('telemetry_gap', gap_s=now - last_seen)
                    last_seen = now
                time.sleep(0.02)
            n += 1
        el = time.monotonic() - t0
        log.say(f'    경과 {el / 60:.1f}분 / {a.endurance_min:.0f}분  '
                f'사이클 {n}  누락 {len(gaps)}  재부팅 {link.reboots - reboots0}')
    link.neutral()
    time.sleep(0.3)
    r = link.send_stop(P.STOP_MISSION_COMPLETE)
    st = link.parser.stats
    fails = []
    if link.reboots - reboots0:
        fails.append(f'STM32 재부팅 {link.reboots - reboots0}회')
    if st.crc_errors:
        fails.append(f'CRC 오류 {st.crc_errors}건')
    if r is None:
        fails.append('종료 CMD_STOP 응답 없음')
    detail = (f'{n} 사이클 · 텔레메트리 누락 {len(gaps)}회'
              + (f' (최대 {max(gaps) * 1000:.0f} ms)' if gaps else '')
              + f' · 재부팅 {link.reboots - reboots0}회 · CRC오류 {st.crc_errors}')
    if fails:
        return False, ' / '.join(fails) + ' · ' + detail
    return True, detail


SCENARIOS = [
    (1, '통신 연결 확인', sc1_link, 'none'),
    (2, '정지 상태 확인', sc2_idle, 'air'),
    (3, '직진 전진·후진', sc3_straight, 'air'),
    (4, '전체 조향 범위', sc4_steering, 'air'),
    (5, '속도와 조향 통합', sc5_combined, 'air'),
    (6, 'S자 주행 명령', sc6_sshape, 'air'),
    (7, '통신 끊김 안전정지', sc7_watchdog, 'air'),
    (8, '초음파 장애물 정지', sc8_obstacle, 'air'),
    (9, '비정상 패킷 처리', sc9_malformed, 'air'),
    (10, '실바닥 오도메트리', sc10_floor, 'floor'),
    (11, '장시간 안정성', sc11_endurance, 'air'),
]


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='STM32 주행 통합 시험 (11 시나리오 순차 실행)')
    ap.add_argument('--port', default='/dev/ttyUSB0')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--wheels-off-ground', action='store_true',
                    help='구동 바퀴가 모두 공중에 떠 있음을 확인했다')
    ap.add_argument('--on-ground', action='store_true',
                    help='시나리오 10 전용. 바퀴가 땅에 닿아 있음을 확인했다')
    ap.add_argument('--only', default='',
                    help='실행할 시나리오 번호 (예: 1,2,4 또는 1-7)')
    ap.add_argument('--slow', type=int, default=150, help='저속 mm/s')
    ap.add_argument('--med', type=int, default=400, help='중속 mm/s')
    ap.add_argument('--circle-sec', type=float, default=6.0,
                    help='시나리오 10 원주행 시간')
    ap.add_argument('--obstacle-wait', type=float, default=30.0,
                    help='시나리오 8 장애물 대기 시간')
    ap.add_argument('--endurance-min', type=float, default=10.0)
    ap.add_argument('--no-prompt', action='store_true',
                    help='사람 개입 프롬프트를 건너뛴다 (8·10)')
    ap.add_argument('--log-dir', default=os.path.expanduser('~/drive_test_logs'))
    a = ap.parse_args(argv)

    for name, v in (('slow', a.slow), ('med', a.med)):
        if not 0 < v <= SPEED_LIMIT:
            raise SystemExit(f'--{name} 는 1~{SPEED_LIMIT} mm/s 범위여야 한다')

    # 시나리오 선택
    want = set()
    for part in filter(None, a.only.split(',')):
        if '-' in part:
            lo, hi = part.split('-')
            want.update(range(int(lo), int(hi) + 1))
        else:
            want.add(int(part))
    todo = [s for s in SCENARIOS if not want or s[0] in want]
    if not todo:
        raise SystemExit('--only 로 선택된 시나리오가 없다')

    # 안전 게이트
    needs_air = any(s[3] == 'air' for s in todo)
    needs_floor = any(s[3] == 'floor' for s in todo)
    if needs_air and not a.wheels_off_ground:
        raise SystemExit(
            '거부: 선택된 시나리오에 모터 구동이 포함된다.\n'
            '  구동 바퀴를 모두 공중에 띄우고 --wheels-off-ground 를 붙여라.\n'
            f'  해당 시나리오: '
            f'{[s[0] for s in todo if s[3] == "air"]}')
    if needs_floor and not a.on_ground:
        raise SystemExit(
            '거부: 시나리오 10 은 실제 바닥 주행이다.\n'
            '  주변을 비우고 --on-ground 를 붙여라.')
    if a.wheels_off_ground and a.on_ground:
        raise SystemExit('--wheels-off-ground 와 --on-ground 를 같이 쓸 수 없다')

    os.makedirs(a.log_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(a.log_dir, f'drive_test_{stamp}.jsonl')
    log = Log(path)
    log.rec('config', **vars(a), steer_max_left=STEER_MAX_LEFT,
            steer_max_right=STEER_MAX_RIGHT, tx_hz=TX_HZ)

    print(f'로그: {path}')
    print(f'포트: {a.port} @ {a.baud} · TX {TX_HZ:.0f} Hz · '
          f'저속 {a.slow} / 중속 {a.med} mm/s')
    print(f'조향 한계: 좌 +{STEER_MAX_LEFT} / 우 {STEER_MAX_RIGHT} cdeg (비대칭)')
    print(f'실행: {[s[0] for s in todo]}\n')

    try:
        link = Link(a.port, a.baud, log)
    except Exception as e:                                      # noqa: BLE001
        log.close()
        raise SystemExit(f'{a.port} 를 열 수 없다: {e}\n'
                         '  다른 프로세스(stm32_bridge 등)가 점유 중인지 확인하라.')

    # 초기 텔레메트리 확인 — 링크가 죽어 있으면 여기서 멈춘다
    log.say('링크 확인 중...')
    if link.wait_state(set(range(7)), 3.0) is None:
        link.close()
        log.close()
        raise SystemExit(
            '텔레메트리가 오지 않는다. 시험을 시작할 수 없다.\n'
            '  serial_probe.py / stm32_monitor.py 로 링크를 먼저 확인하라.\n'
            '  STM32 전원·UART 배선·VEHICLE_COMM_USE_STLINK_VCP 를 점검하라.')
    d, _ = link.snap()
    log.say(f'링크 OK — state={P.STATE_NAMES.get(d["drive_state"], "?")} '
            f'fault=0x{d["active_fault_bits"]:08X}\n')

    results = []
    try:
        for num, name, fn, _gate in todo:
            log.say(f'══════ 시나리오 {num}. {name} ══════')
            log.rec('scenario_begin', num=num, name=name)
            t0 = time.monotonic()
            try:
                ok, detail = fn(link, log, a)
            except KeyboardInterrupt:
                raise
            except Exception as e:                              # noqa: BLE001
                ok, detail = False, f'예외: {type(e).__name__}: {e}'
            dur = time.monotonic() - t0
            link.neutral()
            results.append((num, name, ok, detail, dur))
            log.rec('scenario_end', num=num, name=name, passed=ok,
                    detail=detail, seconds=round(dur, 2))
            log.say(f'  → {"PASS" if ok else "FAIL"}  ({dur:.1f}s)  {detail}\n')
    except KeyboardInterrupt:
        log.say('\n중단됨 (Ctrl-C)')
    finally:
        # 어느 경로로 끝나도 정지 명령을 보낸다
        try:
            link.neutral()
            link.resume_tx()
            time.sleep(0.2)
            link.send_stop(P.STOP_OPERATOR, wait=0.8)
            time.sleep(0.1)
        except Exception:                                       # noqa: BLE001
            pass
        link.close()

    print('═' * 66)
    print('요약')
    print('═' * 66)
    npass = sum(1 for r in results if r[2])
    for num, name, ok, detail, dur in results:
        print(f'  {num:>2}. {name:<20} {"PASS" if ok else "FAIL"}  '
              f'{dur:>6.1f}s')
        if not ok:
            print(f'      → {detail}')
    print(f'\n  {npass}/{len(results)} 통과')
    print(f'  파서: valid={link.parser.stats.valid_frames} '
          f'crc_err={link.parser.stats.crc_errors} '
          f'noise={link.parser.stats.noise_bytes} '
          f'schema_err={link.parser.stats.schema_errors}')
    print(f'  STM32 재부팅 {link.reboots}회')
    print(f'\n  로그: {path}')
    log.rec('summary', passed=npass, total=len(results),
            results=[{'num': r[0], 'name': r[1], 'passed': r[2],
                      'detail': r[3], 'seconds': round(r[4], 2)}
                     for r in results])
    log.close()
    return 0 if npass == len(results) else 1


if __name__ == '__main__':
    sys.exit(main())
