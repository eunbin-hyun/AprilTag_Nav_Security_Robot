#!/usr/bin/env python3
"""첫 실차 주행 단계 0~2 용 구동 시험 도구.

route_runner 는 태그가 있어야 움직이므로 벤치·바닥 기초 시험에는 이 도구를 쓴다.
UART 를 직접 소유한다 — route_runner 와 동시에 띄우면 포트 충돌이 난다.

  # 단계 0 — 속도 계단 (⚠ 바퀴를 공중에 띄울 것)
  python3 drive_probe.py --port /dev/ttyUSB0 --staircase 100,150,200,250,313

  # 단계 0 — 조향 sweep (⚠ 바퀴 공중, 타이로드 연결 상태)
  python3 drive_probe.py --port /dev/ttyUSB0 --sweep

  # 단계 0 — 조향각 스캔: 실제 조향각의 정점(사점)을 찾는다 (⚠ 바퀴 공중)
  #   명령을 키웠는데 각도가 되돌아오면 그 앞이 진짜 한계다
  python3 drive_probe.py --port /dev/ttyUSB0 --steer-scan 0,400,800,1200,1600,1955

  # 회전반경 실측 — 180도 원호를 돌고 멈춘다 (⚠ 바닥, 원 지름만큼 공간)
  #   출발·도착 뒷축중앙에 테이프 -> 현을 줄자로 재고
  #   steering_angle_calc.py --chord-arc 로 환산 (yaw 를 안 쓴다)
  python3 drive_probe.py --port /dev/ttyUSB0 --arc 200,-1800,180

  # 단계 1 — 바닥 직진 3 m (20% = 313 mm/s, 5초)
  python3 drive_probe.py --port /dev/ttyUSB0 --hold 313,0,5

  # 단계 2 — 우측 최대 조향 원 (반경 자동 추정)
  python3 drive_probe.py --port /dev/ttyUSB0 --hold 200,-2869,15

실행 전 워크스페이스 source 필요 (stm32_bridge 를 import 한다):

  source ~/ros_ws/install/setup.bash

Ctrl-C 는 어느 시점에든 안전하다 — neutral 을 5회 쏜 뒤 종료한다.

초음파 장애물이 뜨면 `stm32_bridge.obstacle_gate.ObstacleGate` 정책대로
neutral 로 바꿔 보내며 기다리고, 해제·rearm 후 같은 명령으로 재개한다.
**HOLD 로 보낸 시간은 구간 시간에서 제외한다.** 다른 차단 fault 나 통신단절은
복구 대기에 숨기지 않고 즉시 중단한다. 복구 경로에서 CMD_STOP 을 보내지 않는다.

⚠ 이 도구는 **조향 트림을 적용하지 않는다.** `route_runner` 의
  `steer_trim_cdeg` 는 여기 없다. 바닥 직진 시험은 실측 중립값을 직접 넣을 것:
  `--hold 400,500,5` (2026-08-04 실측: 500 cdeg 가 직진).
"""
import argparse
import math
import select
import sys
import time

try:
    import serial
except ImportError:
    sys.exit('pyserial 이 없습니다: pip3 install pyserial')

try:
    from stm32_bridge import protocol as P
    from stm32_bridge.obstacle_gate import ObstacleGate
    from stm32_bridge.parser import FrameParser
except ImportError:
    sys.exit('stm32_bridge 를 찾을 수 없습니다. 워크스페이스를 source 하십시오:\n'
             '  source ~/ros_ws/install/setup.bash')

RATE_HZ = 20.0
TICK = 1.0 / RATE_HZ
ARM_TIMEOUT_S = 4.0
# 실측 한계 (vehicle_config.h 2026-07-31). 도구 자체 clamp — 넘는 값은 거부.
STEER_MIN = -2869
STEER_MAX = 1955
HARD_SPEED_LIMIT = 1565


class Probe:
    def __init__(self, port, baud, ticks_per_m):
        self.ser = serial.Serial(port, baud, timeout=0)
        self.parser = FrameParser()
        self.seq = P.SeqCounter()
        self.ticks_per_m = ticks_per_m
        self.t = None                 # 최신 0x80 TELEMETRY_DRIVE
        self.t_at = None
        self.imu = None               # 최신 0x83 TELEMETRY_IMU
        self.odom = None              # 최신 0x85 TELEMETRY_ODOMETRY

    # ── 저수준 ────────────────────────────────────────────

    def pump_rx(self):
        data = self.ser.read(512)
        if data:
            for msg_id, _seq, pl in self.parser.feed(data):
                if msg_id == P.TELEMETRY_DRIVE:
                    self.t = P.unpack_telemetry(pl)
                    self.t_at = time.monotonic()
                elif msg_id == P.TELEMETRY_IMU:
                    # 0x83 은 회전반경 실측의 핵심이다. BNO085 의
                    # quaternion yaw(정확도 3)와 원시 gyro_z 가 들어 있고,
                    # 둘 다 **조향 트림·STM32 융합 판단과 무관**하다.
                    # 0x80 의 yaw_cdeg 나 0x85 의 융합/모델 yaw 는 트림 때문에
                    # 부풀려질 수 있어서 반경 측정에 못 쓴다.
                    self.imu = P.unpack_telemetry_imu(pl)
                elif msg_id == P.TELEMETRY_ODOMETRY:
                    # distance_mm 은 엔코더 적분값이다 (tick 환산 불필요).
                    self.odom = P.unpack_telemetry_odometry(pl)
        self.parser.check_timeout()

    def measure_gyro_bias(self, seconds=2.0):
        """정지 상태에서 gyro_z 평균을 재 바이어스로 쓴다 [mdeg/s].

        BNO085 accuracy 가 0(unreliable)이라 바이어스가 남아 있을 수 있다.
        10 s 적분에 0.5°/s 바이어스면 5° 오차 = 120° 회전의 4% 다. 정지
        평균을 빼면 그 대부분이 사라진다.

        대기 중에도 neutral 을 계속 보낸다 — 멈추면 watchdog 이 SAFE_STOP 을
        만들고 이후 주행 명령이 거부된다.
        """
        print(f'  자이로 바이어스 측정 {seconds:.0f}s (차를 움직이지 마십시오)...',
              end='', flush=True)
        end = time.monotonic() + seconds
        vals = []
        next_tx = time.monotonic()
        while time.monotonic() < end:
            now = time.monotonic()
            if now >= next_tx:
                self.send(0, 0, False)
                next_tx += TICK
            self.pump_rx()
            if self.imu is not None:
                vals.append(self.imu['gyro_z_mdeg_s'])
            time.sleep(0.002)
        if not vals:
            print(' ⚠ 0x83 IMU 프레임 미수신 — 자이로 적분을 쓸 수 없다')
            return None
        bias = sum(vals) / len(vals)
        spread = (max(vals) - min(vals)) / 1000.0
        print(f' {bias/1000.0:+.3f} °/s  (표본 {len(vals)}, 편차 {spread:.3f} °/s)')
        return bias

    def send(self, speed, steer, enable):
        self.ser.write(P.pack_cmd_drive(self.seq.next(), int(speed),
                                        int(steer), enable))

    def neutral_burst(self, n=5):
        for _ in range(n):
            self.ser.write(P.pack_neutral(self.seq.next()))
            time.sleep(TICK)

    def hold_neutral_until_enter(self):
        """Enter 를 누를 때까지 neutral 을 20 Hz 로 계속 보낸다.

        사람을 기다리는 동안 송신을 멈추면 STM32 watchdog(0.3 s)이
        COMM_TIMEOUT -> SAFE_STOP 을 만든다. SAFE_STOP 에서는 비영 명령이
        거부되므로, 그 뒤 주행 명령을 보내도 차가 움직이지 않는다.
        """
        next_tx = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_tx:
                self.send(0, 0, False)
                next_tx += TICK
                if next_tx <= now:
                    next_tx = now + TICK
            self.pump_rx()
            if select.select([sys.stdin], [], [], 0.002)[0]:
                sys.stdin.readline()
                return

    # ── 시퀀스 ────────────────────────────────────────────

    def arm(self):
        """neutral 반복 → READY/DRIVING 대기. 실패 시 fault 를 해독해 보여준다.

        DRIVING 도 성공으로 본다 — 직전 세션이 남긴 DRIVING 상태에서 neutral
        은 target 만 0 으로 만들고 상태는 유지하므로, READY 만 기다리면
        연속 실행 시 영원히 실패한다 (fake 자체검증에서 실측된 함정).
        """
        print('  arming (neutral 20 Hz)...', flush=True)
        end = time.monotonic() + ARM_TIMEOUT_S
        while time.monotonic() < end:
            self.send(0, 0, False)
            self.pump_rx()
            if self.t and self.t['drive_state'] in (P.STATE_READY,
                                                    P.STATE_DRIVING):
                print(f"  {P.STATE_NAMES[self.t['drive_state']]} — arm 완료")
                return True
            time.sleep(TICK)
        st = P.STATE_NAMES.get(self.t['drive_state'], '?') if self.t else '수신 없음'
        print(f'\n  ARM 실패: drive_state={st}')
        if self.t:
            print(f"  fault 0x{self.t['active_fault_bits']:08X} = "
                  + P.describe_faults(self.t['active_fault_bits']))
        else:
            print('  telemetry 자체가 없습니다 — 포트·전원·배선 확인')
        return False

    def run_cmd(self, speed, steer, seconds, label='', quiet=False,
                gyro_bias=None):
        """(speed, steer) 를 **주행 시간** seconds 동안 20 Hz 유지.

        초음파 장애물이 뜨면 `ObstacleGate` 정책에 따라 neutral 로 바꿔 보내며
        기다리고, 해제·rearm 이 끝나면 같은 명령으로 재개한다. **HOLD 로 보낸
        시간은 seconds 에서 제외한다** — 앞을 누가 지나갔다는 이유로 3 초짜리
        계단이 1 초 주행으로 끝나면 측정이 틀어진다.

        장애물 HOLD 중에도 송신을 멈추지 않는 것이 중요하다. 멈추면 STM32
        watchdog 이 COMM_TIMEOUT 을 올려 복구가 더 어려워진다. 그리고 이 경로
        에서는 `CMD_STOP` 을 보내지 않는다 (rearm_required 를 다시 세운다).
        """
        t0 = time.monotonic()
        enc0 = self.t['encoder_count'] if self.t else None
        yaw0 = self.t['yaw_cdeg'] if self.t else None
        samples = []                  # (주행경과, measured, duty)
        # 구간 중 한 번이라도 뜬 fault·state 를 누적한다. 진행 줄은 마지막
        # 프레임만 보여주므로, 도중에 떴다 사라진 fault 를 놓치지 않기 위함.
        faults_seen = 0
        states_seen = []
        gate = ObstacleGate()
        hold_logged = None
        # ── 회전반경 실측용 적분 (yaw 출처를 셋 다 따로 모은다) ──
        #   imu_quat : 0x83 quaternion yaw   (정확도 3, 트림 무관)  ← 기준
        #   imu_gyro : 0x83 gyro_z 적분      (바이어스 보정, 트림 무관)
        #   odom_yaw : 0x85 융합/모델 yaw    (트림 오염 가능)  ← 비교용
        q_prev = q_sum = None
        g_sum = 0.0
        g_prev_ms = None
        o_prev = o_sum = None
        dist0 = None
        next_tx = t0
        last_draw = 0.0
        while True:
            now = time.monotonic()
            # 주행 경과 = 벽시계 - HOLD 누적
            active = (now - t0) - gate.hold_elapsed_s(now)
            if active >= seconds:
                break
            self.pump_rx()
            age = (now - self.t_at) if self.t_at is not None else 999.0
            may_drive = gate.update(now, self.t, age)
            if gate.aborted:
                if not quiet:
                    print()
                print(f'  ⚠ 중단: {gate.abort_reason}')
                break
            if now >= next_tx:
                # HOLD 중에는 neutral. STM32 는 SAFE_STOP 에서 비영 명령을
                # 거부하므로 enable=1 을 계속 보내면 아무 일도 안 일어난다.
                if may_drive:
                    self.send(speed, steer, True)
                else:
                    self.send(0, 0, False)
                next_tx += TICK
                if next_tx <= now:
                    next_tx = now + TICK
            # ── 각도·거리 적분. HOLD 중에는 정지 상태이므로 건너뛴다.
            if may_drive and self.imu is not None:
                q = self.imu['yaw_mdeg']
                if q_prev is not None:
                    d = (q - q_prev + 180000) % 360000 - 180000
                    q_sum = (q_sum or 0.0) + d
                q_prev = q
                ms = self.imu['mcu_time_ms']
                if g_prev_ms is not None:
                    dt = ((ms - g_prev_ms) & 0xFFFFFFFF) / 1000.0
                    if 0.0 < dt < 0.5:      # 비정상 dt 는 버린다
                        g_sum += (self.imu['gyro_z_mdeg_s']
                                  - (gyro_bias or 0.0)) * dt
                g_prev_ms = ms
            if may_drive and self.odom is not None:
                o = self.odom['yaw_mdeg']
                if o_prev is not None:
                    d = (o - o_prev + 180000) % 360000 - 180000
                    o_sum = (o_sum or 0.0) + d
                o_prev = o
                if dist0 is None:
                    dist0 = self.odom['distance_mm']
            if self.t:
                if may_drive:
                    # HOLD 중 표본은 넣지 않는다 — measured=0 이 섞이면
                    # 정착 평균이 오염된다.
                    samples.append((active, self.t['measured_speed_mm_s'],
                                    self.t['motor_duty_permille']))
                faults_seen |= self.t['active_fault_bits']
                st = self.t['drive_state']
                if not states_seen or states_seen[-1] != st:
                    states_seen.append(st)
                if gate.substate != hold_logged:
                    hold_logged = gate.substate
                    if gate.holding:
                        if not quiet:
                            print()
                        print(f'  ⏸ 장애물 {gate.substate} — neutral 유지 '
                              f'(CMD_STOP 안 보냄)')
                    elif gate.hold_count:
                        print(f'  ▶ 재개 (HOLD {gate.hold_elapsed_s(now):.1f}s '
                              f'제외됨)')
                if not quiet and now - last_draw > 0.2:
                    last_draw = now
                    fb = self.t['active_fault_bits']
                    print(f'\r  {label} target={speed if may_drive else 0:>5} '
                          f"measured={self.t['measured_speed_mm_s']:>5} "
                          f"duty={self.t['motor_duty_permille']:>5}‰ "
                          f"enc={self.t['encoder_count']:>8} "
                          f"state={P.STATE_NAMES.get(st, '?')}"
                          + ('' if may_drive else f' [{gate.substate}]')
                          + (f' fault=0x{fb:08X}' if fb else '')
                          + '   ', end='', flush=True)
            time.sleep(0.002)
        if not quiet:
            print()
        enc1 = self.t['encoder_count'] if self.t else None
        yaw1 = self.t['yaw_cdeg'] if self.t else None
        return {
            'samples': samples,
            'd_enc': (enc1 - enc0) if None not in (enc0, enc1) else None,
            'd_yaw_cdeg': (yaw1 - yaw0) if None not in (yaw0, yaw1) else None,
            'faults_seen': faults_seen,
            'states_seen': [P.STATE_NAMES.get(s, '?') for s in states_seen],
            'hold_s': round(gate.hold_elapsed_s(time.monotonic()), 2),
            'hold_count': gate.hold_count,
            'aborted': gate.abort_reason,
            # 회전각 [deg] 세 가지 + 오도메트리 거리 [m]
            'imu_quat_deg': (q_sum / 1000.0) if q_sum is not None else None,
            'imu_gyro_deg': (g_sum / 1000.0) if g_prev_ms is not None else None,
            'odom_yaw_deg': (o_sum / 1000.0) if o_sum is not None else None,
            'odom_dist_m': (((self.odom['distance_mm'] - dist0) / 1000.0)
                            if (self.odom is not None and dist0 is not None)
                            else None),
        }


def settled_mean(samples, window_frac=0.4):
    """구간 마지막 40% 표본의 평균 (과도 응답 제외)."""
    if not samples:
        return None
    dur = samples[-1][0]
    tail = [m for (ts, m, _d) in samples if ts >= dur * (1 - window_frac)]
    return sum(tail) / len(tail) if tail else None


def do_staircase(pr, steps, step_s):
    print(f'\n=== 속도 계단: {steps} (각 {step_s}s) ===')
    rows = []
    aborted = None
    for sp in steps:
        r = pr.run_cmd(sp, 0, step_s, label=f'[{sp:>4}]')
        mean = settled_mean(r['samples'])
        rows.append((sp, mean, r))
        if r['aborted']:
            # 중단 사유가 있으면 다음 단으로 넘어가지 않는다 — 원인을 안고
            # 계단을 계속 올리면 상황만 나빠진다.
            aborted = r['aborted']
            break
    pr.neutral_burst()

    # Δenc 를 같이 낸다. measured=0 이어도 Δenc 가 늘면 바퀴는 돌았다는
    # 뜻이므로 "엔코더 죽음" 과 "모터 안 돎" 이 여기서 갈린다.
    print('\n  목표    정착측정    추종률   Δenc   판정')
    deadband_edge = None
    for sp, mean, r in rows:
        d_enc = r['d_enc']
        enc_s = '—' if d_enc is None else f'{d_enc:+d}'
        if mean is None:
            print(f'  {sp:>4}        —        —  {enc_s:>6}   표본 없음')
            continue
        ratio = mean / sp if sp else 0
        verdict = ('추종' if ratio >= 0.8 else
                   '부분' if ratio >= 0.3 else '정지(데드밴드?)')
        if verdict == '추종' and deadband_edge is None:
            deadband_edge = sp
        print(f'  {sp:>4}   {mean:>7.0f}    {ratio:>5.0%}  {enc_s:>6}   '
              f'{verdict}')
        if r['faults_seen']:
            print(f"         fault 0x{r['faults_seen']:08X} = "
                  + P.describe_faults(r['faults_seen']))
        if len(r['states_seen']) > 1:
            print('         state ' + ' → '.join(r['states_seen']))
        if r['hold_count']:
            print(f"         장애물 HOLD {r['hold_count']}회 "
                  f"{r['hold_s']}s (주행시간에서 제외됨)")
    if aborted:
        print(f'\n  ⚠ 계단 중단: {aborted}')
        print('    남은 단은 실행하지 않았다 — 원인 해소 후 다시 돌릴 것')
        return
    if deadband_edge:
        print(f'\n  → 최소 제어 가능 속도(추종 시작): 약 {deadband_edge} mm/s')
        print('    route_runner 의 v_approach_mm_s 는 이보다 커야 합니다.')
    else:
        print('\n  ⚠ 어느 단도 추종하지 않음 — 모터 전원·듀티·배선 확인')


def do_sweep(pr, dwell_s):
    seq = [0, STEER_MAX // 2, STEER_MAX, STEER_MAX // 2, 0,
           STEER_MIN // 2, STEER_MIN, STEER_MIN // 2, 0]
    print(f'\n=== 조향 sweep: {seq} (각 {dwell_s}s, 속도 0) ===')
    print('  ⚠ +값에서 바퀴가 "좌회전" 방향으로 꺾이는지 눈으로 확인하십시오.')
    for st in seq:
        pr.run_cmd(0, st, dwell_s, label=f'[{st:>5}]', quiet=True)
        fb = pr.t['steering_feedback_cdeg'] if pr.t else None
        cmd = pr.t['steering_cmd_cdeg'] if pr.t else None
        print(f'  cmd={st:>5}  STM32수신={cmd!s:>6}  feedback={fb!s:>6}')
    pr.neutral_burst()
    print('\n  feedback 이 전부 0 이면 AS5600 미보정 상태입니다 (참고용).')


def do_steer_scan(pr, values, dwell_s):
    """조향 각도 스캔 — 실제 조향각의 정점(사점)을 찾는다.

    `--sweep` 은 0/±977/±1955 네 점만 보므로, 명령을 키웠는데 조향각이
    되돌아오는 over-center 지점이 어디인지 알 수 없다. 이 모드는 임의의
    값 목록을 훑으면서 **각 값에서 계속 송신한 채 멈춰** 실측 각도를
    적을 시간을 준다.

    송신을 멈추면 STM32 watchdog(0.3 s)이 SAFE_STOP 으로 떨어뜨려 서보가
    중립으로 돌아간다 — 측정 중에도 20 Hz 송신을 유지해야 한다.
    """
    print(f'\n=== 조향 스캔: {values} ===')
    print('  ⚠ 바퀴 공중, 속도 0. 타이로드 연결 상태여야 한다.')
    print('  각 단계에서 바퀴 실제 각도를 재고 Enter → 다음 단계.')
    print('  명령을 키웠는데 각도가 줄어드는 지점이 사점(over-center)이다.\n')
    rows = []
    for i, st in enumerate(values, 1):
        pr.run_cmd(0, st, dwell_s, label=f'[{st:>6}]', quiet=True)
        cmd = pr.t['steering_cmd_cdeg'] if pr.t else None
        fb = pr.t['steering_feedback_cdeg'] if pr.t else None
        fbits = pr.t['active_fault_bits'] if pr.t else 0
        line = f'  cmd={st:>6}  STM32수신={cmd!s:>6}  feedback={fb!s:>6}'
        if fbits:
            line += f'  fault=0x{fbits:08X} = ' + P.describe_faults(fbits)
        # 이 각도를 유지한 채 멈춘다는 것을 화면에 명시한다. 차량 앞에서
        # 각도기를 들고 있을 때 "다음으로 어떻게 가는지" 를 헤매지 않도록.
        line += f'   [{i}/{len(values)}] ← 좌·우 바퀴 각도 재고 Enter'
        print(line, end='', flush=True)
        # 측정하는 동안 계속 송신한다.
        next_tx = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= next_tx:
                pr.send(0, st, True)
                next_tx += TICK
            pr.pump_rx()
            if select.select([sys.stdin], [], [], 0.002)[0]:
                sys.stdin.readline()
                break
        rows.append((st, cmd, fb))
    pr.neutral_burst()

    print('\n  명령    STM32수신   feedback')
    for st, cmd, fb in rows:
        print(f'  {st:>6}   {cmd!s:>7}   {fb!s:>7}')
    print('\n  STM32수신 이 명령과 다르면 COMMAND_LIMIT 으로 잘린 것이다.')
    print('  feedback 이 전부 0 인 것은 정상이다 (물리 조향 센서 미장착).')
    print('  실측 각도의 정점보다 안쪽으로 min/max_steering_cdeg 를 내릴 것.')


def do_arc(pr, speed, steer, target_deg, ticks_per_m, max_m):
    """목표 회전각까지 원호를 돌고 멈춘다 — 회전반경 실측용.

    왜 이 모드가 필요한가: `--hold` 는 고정 시간이라 180° 지점을 눈으로 잡아
    표시해야 한다. 여기서는 도구가 알아서 멈추므로 출발·도착 자리에 테이프만
    붙이면 된다.

    ★ **정지 각도가 정확하지 않아도 된다.** 텔레메트리 yaw 로 멈추지만, 그
      yaw 가 부풀려져 있을 수 있다 (트림 때문에 모델 yaw 가 틀어진다). 그래서
      반경은 yaw 로 계산하지 않고 **호길이(엔코더) + 현(줄자)** 두 값으로
      푼다:

          s = R·θ                (엔코더 주행거리)
          c = 2R·sin(θ/2)        (출발-도착 직선거리)

      미지수가 R·θ 두 개, 식이 두 개다. yaw 를 안 쓰고 둘 다 나온다.
      환산은 `steering_angle_calc.py --chord-arc` 가 해준다.

    max_m 이동거리 가드를 둔다 — 조향이 안 먹으면 목표각에 영원히 못 닿는다.
    """
    print(f'\n=== 원호 주행: speed={speed} steer={steer} 목표 {target_deg}° ===')
    print(f'  ⚠ 바닥. 이동거리 상한 {max_m} m. Ctrl-C 언제든 안전.')
    print('  ① 지금 **뒷축 중앙** 자리에 테이프를 붙이십시오.')
    print('  ② 붙였으면 Enter → 주행 시작', end='', flush=True)
    # ★ 대기 중에도 neutral 을 20 Hz 로 계속 보낸다. 그냥 input() 으로 멈추면
    #   STM32 watchdog(0.3 s)이 COMM_TIMEOUT -> SAFE_STOP 을 만들고, 그 상태에서
    #   비영 명령은 **거부**되어 주행이 시작되지 않는다 (명령은 나가는데 차가
    #   안 움직이는 증상. 2026-08-05 실장비에서 실제로 발생).
    pr.hold_neutral_until_enter()
    gate = ObstacleGate()
    t0 = time.monotonic()
    enc0 = pr.t['encoder_count'] if pr.t else None
    turned_cdeg = 0.0
    prev_yaw = pr.imu['yaw_mdeg'] if pr.imu is not None else None
    next_tx = t0
    last_draw = 0.0
    stop_why = '목표 도달'
    while True:
        now = time.monotonic()
        pr.pump_rx()
        age = (now - pr.t_at) if pr.t_at is not None else 999.0
        may_drive = gate.update(now, pr.t, age)
        if gate.aborted:
            stop_why = f'중단: {gate.abort_reason}'
            break
        if now >= next_tx:
            pr.send(speed, steer, True) if may_drive else pr.send(0, 0, False)
            next_tx += TICK
            if next_tx <= now:
                next_tx = now + TICK
        # ★ yaw 출처는 **0x83 IMU quaternion** 이다. 0x80 의 yaw_cdeg 는
        #   fake 가 항상 0 으로 보내고 실장비 의미도 확인되지 않았다.
        #   0x85 의 융합/모델 yaw 는 조향 트림 때문에 부풀려질 수 있다.
        if pr.imu is not None:
            y = pr.imu['yaw_mdeg']
            if prev_yaw is not None:
                d = (y - prev_yaw + 180000) % 360000 - 180000
                turned_cdeg += d / 10.0        # mdeg -> cdeg
            prev_yaw = y
        if pr.t:
            dist = ((pr.t['encoder_count'] - enc0) / ticks_per_m
                    if enc0 is not None else 0.0)
            if abs(turned_cdeg) >= target_deg * 100:
                break
            if abs(dist) > max_m:
                stop_why = (f'⚠ 이동거리 가드 {max_m} m 초과 — yaw 진행 '
                            f'{turned_cdeg/100:+.1f}° 뿐. 조향이 안 먹는다')
                break
            if now - last_draw > 0.2:
                last_draw = now
                print(f'\r  yaw {turned_cdeg/100:>+7.1f}° / {target_deg:>+5.0f}°'
                      f'   이동 {dist:5.2f} m'
                      + ('' if may_drive else f'  [{gate.substate}]')
                      + '   ', end='', flush=True)
        time.sleep(0.002)
    print()
    pr.neutral_burst()

    enc1 = pr.t['encoder_count'] if pr.t else None
    dist_m = ((enc1 - enc0) / ticks_per_m
              if None not in (enc0, enc1) else None)
    print(f'\n  정지 사유: {stop_why}')
    print(f'  텔레메트리 yaw 변화 : {turned_cdeg/100:+.1f}°  (신뢰도 미확인)')
    if dist_m is not None:
        print(f'  엔코더 호길이       : {dist_m:.3f} m   ← 이 값을 적으십시오')
        if abs(turned_cdeg) > 100:
            r_yaw = abs(dist_m) / math.radians(abs(turned_cdeg) / 100.0)
            print(f'  (yaw 기준 참고 반경 : {r_yaw:.3f} m — 교차검증용)')
    print('\n  ③ 지금 **뒷축 중앙** 자리에 테이프를 붙이고, 출발 테이프까지')
    print('     직선거리(현)를 줄자로 재십시오.')
    print('  ④ 환산:')
    d = f'{dist_m:.3f}' if dist_m is not None else '<호길이>'
    print('     python3 steering_angle_calc.py --chord-arc <<EOF')
    print(f'     {steer}  {d}  <현mm>')
    print('     EOF')


def do_hold(pr, speed, steer, seconds, ticks_per_m):
    print(f'\n=== 고정 주행: speed={speed} steer={steer} '
          f'(주행 {seconds}s, 장애물 HOLD 제외) ===')
    bias = None
    if steer != 0:
        # 조향이 있으면 회전반경을 재려는 것이다. 자이로 적분을 쓰려면
        # 정지 바이어스를 먼저 알아야 한다 (accuracy 0 이라 남아 있다).
        bias = pr.measure_gyro_bias(2.0)
    r = pr.run_cmd(speed, steer, seconds, label='', gyro_bias=bias)
    pr.neutral_burst()
    if r['hold_count']:
        print(f"\n  장애물 HOLD {r['hold_count']}회 {r['hold_s']}s "
              '— 주행시간에서 제외했다. 이동거리는 실제 굴러간 값이다.')
    if r['aborted']:
        print(f"\n  ⚠ 중단: {r['aborted']}")

    dist_enc = (r['d_enc'] / ticks_per_m) if r['d_enc'] is not None else None
    dist_odom = r['odom_dist_m']
    print()
    if dist_enc is not None:
        print(f'  이동 거리 (엔코더 tick) : {dist_enc:+.3f} m  '
              f'(Δtick={r["d_enc"]:+d})')
    if dist_odom is not None:
        print(f'  이동 거리 (0x85 odom)   : {dist_odom:+.3f} m')
    dist = dist_odom if dist_odom is not None else dist_enc
    if dist is None or steer == 0:
        return

    # ── 회전반경: 각도 출처 세 개를 나란히 낸다 ─────────────
    # 반경은 트림·융합과 무관한 IMU 값을 기준으로 삼는다.
    print('\n  회전각 출처별 반경 (호길이 '
          f'{abs(dist):.3f} m 기준)')
    print('    출처                    회전각      반경      비고')
    rows = [
        ('0x83 IMU quaternion', r['imu_quat_deg'], '★ 기준 (트림 무관, 정확도 3)'),
        ('0x83 gyro_z 적분', r['imu_gyro_deg'],
         '트림 무관 (바이어스 보정됨)' if bias is not None else '바이어스 미보정'),
        ('0x85 융합/모델 yaw', r['odom_yaw_deg'], '트림 오염 가능 — 비교용'),
    ]
    ref = None
    for name, deg, note in rows:
        if deg is None or abs(deg) < 5.0:
            print(f'    {name:<22} {"—":>8}    {"—":>8}   '
                  + ('데이터 없음' if deg is None else f'{deg:+.1f}° — 너무 작다'))
            continue
        R = abs(dist) / math.radians(abs(deg))
        if ref is None:
            ref = R
        print(f'    {name:<22} {deg:>+8.1f}°  {R:>7.3f} m   {note}')

    if ref is not None:
        eq = math.degrees(math.atan(0.135 / ref))
        print(f'\n  → 실측 반경 {ref:.3f} m  (자전거모델 등가 조향각 {eq:.2f}°)')
        print(f'     명령 {steer} cdeg, 트림 500 가정 시 의도각 '
              f'{(steer - 500) / 100:+.2f}°')
        print('     세 출처가 크게 다르면 그 자체가 진단 정보다 —')
        print('     0x85 만 다르면 트림 때문에 모델 yaw 가 부풀려진 것이다.')
    else:
        print('\n  ⚠ 회전각을 못 읽었다. 0x83/0x85 프레임 수신을 확인하거나,')
        print('    줄자 방식으로 재고 steering_angle_calc.py --chord-arc 를 쓸 것.')


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', required=True)
    ap.add_argument('--baud', type=int, default=115200)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--staircase', metavar='V1,V2,...',
                      help='속도 계단 [mm/s]. ⚠ 바퀴 공중')
    mode.add_argument('--sweep', action='store_true',
                      help='조향 sweep. ⚠ 바퀴 공중')
    mode.add_argument('--steer-scan', metavar='C1,C2,...',
                      help='조향각 스캔 [cdeg]. 각 값에서 송신 유지한 채 '
                           '멈춰 실측 각도를 재게 한다 (사점 탐색). ⚠ 바퀴 공중')
    mode.add_argument('--hold', metavar='SPEED,STEER,SEC',
                      help='고정 (속도,조향각cdeg,초) 유지 — 바닥 주행용')
    mode.add_argument('--arc', metavar='SPEED,STEER,DEG',
                      help='목표 회전각까지 원호 주행 후 정지 — 회전반경 '
                           '실측용. 출발·도착에 테이프를 붙이고 현을 잰다')
    ap.add_argument('--step-s', type=float, default=3.0)
    ap.add_argument('--dwell-s', type=float, default=1.5)
    ap.add_argument('--max-speed', type=int, default=313,
                    help='속도 상한 [mm/s]. 기본 313 = 최대출력 1565 의 20%%')
    ap.add_argument('--ticks-per-m', type=float, default=4093.8)
    ap.add_argument('--arc-max-m', type=float, default=4.0,
                    help='--arc 이동거리 가드 [m]. 조향이 안 먹으면 여기서 멈춘다')
    ap.add_argument('--yes', action='store_true', help='시작 확인 생략')
    a = ap.parse_args()

    cap = min(a.max_speed, HARD_SPEED_LIMIT)

    # 명령 준비 + 한계 검사 (시작 전에 전부 거른다)
    if a.staircase:
        steps = [int(s) for s in a.staircase.split(',')]
        bad = [s for s in steps if not 0 <= s <= cap]
        if bad:
            sys.exit(f'속도 {bad} 가 상한 {cap} mm/s 를 넘습니다 '
                     '(--max-speed 로 올릴 수 있으나 첫 시험엔 권장 안 함)')
        banner = '⚠ 바퀴를 공중에 띄운 상태인지 확인하십시오 (속도 계단).'
    elif a.sweep:
        banner = '⚠ 바퀴 공중 + 조향이 움직여도 걸리는 것이 없는지 확인하십시오.'
    elif a.steer_scan:
        scan = [int(s) for s in a.steer_scan.split(',')]
        bad = [s for s in scan if not STEER_MIN <= s <= STEER_MAX]
        if bad:
            sys.exit(f'조향 {bad} 가 {STEER_MIN}..{STEER_MAX} cdeg 범위 밖입니다')
        banner = '⚠ 바퀴 공중 + 조향이 움직여도 걸리는 것이 없는지 확인하십시오.'
    elif a.arc:
        try:
            sp, st, deg = a.arc.split(',')
            sp, st, deg = int(sp), int(st), float(deg)
        except ValueError:
            sys.exit('--arc 형식: SPEED,STEER,DEG (예: 200,-1800,180)')
        if not 0 <= abs(sp) <= cap:
            sys.exit(f'|speed| ≤ {cap} 이어야 합니다')
        if not STEER_MIN <= st <= STEER_MAX:
            sys.exit(f'steer 는 {STEER_MIN}..{STEER_MAX} cdeg (비대칭 실측 한계)')
        if not 10.0 <= deg <= 350.0:
            sys.exit('DEG 는 10~350 사이여야 합니다 (180 권장 — 각도 오차에 '
                     '가장 둔감하다)')
        banner = ('⚠ 바닥 원호 주행입니다. 원 지름만큼 공간을 확보하고, '
                  '한 명이 따라붙으십시오.')
    else:
        try:
            sp, st, sec = a.hold.split(',')
            sp, st, sec = int(sp), int(st), float(sec)
        except ValueError:
            sys.exit('--hold 형식: SPEED,STEER,SEC (예: 313,0,5)')
        if not 0 <= abs(sp) <= cap:
            sys.exit(f'|speed| ≤ {cap} 이어야 합니다 (20%% 저속 시험)')
        if not STEER_MIN <= st <= STEER_MAX:
            sys.exit(f'steer 는 {STEER_MIN}..{STEER_MAX} cdeg (비대칭 실측 한계)')
        banner = ('⚠ 바닥 주행입니다. 한 명이 차 옆에 따라붙고, '
                  '이상하면 차를 들어올리십시오.')

    print(banner)
    if not a.yes:
        input('Enter 를 누르면 시작합니다 (Ctrl-C 취소): ')

    try:
        pr = Probe(a.port, a.baud, a.ticks_per_m)
    except serial.SerialException as e:
        sys.exit(f'포트를 열 수 없습니다: {e}')

    try:
        if not pr.arm():
            return 1
        if a.staircase:
            do_staircase(pr, steps, a.step_s)
        elif a.sweep:
            do_sweep(pr, a.dwell_s)
        elif a.steer_scan:
            do_steer_scan(pr, scan, a.dwell_s)
        elif a.arc:
            do_arc(pr, sp, st, deg, a.ticks_per_m, a.arc_max_m)
        else:
            do_hold(pr, sp, st, sec, a.ticks_per_m)
    except KeyboardInterrupt:
        print('\n  중단 — neutral 송신 후 종료')
    finally:
        try:
            pr.neutral_burst()
            pr.ser.close()
        except Exception:                                  # noqa: BLE001
            pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
