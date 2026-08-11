#!/usr/bin/env python3
"""정사각형 주행으로 STM32 오도메트리 x·y 를 검증한다.

## 왜 정사각형인가

`/stm32/odom` 의 x·y 는 STM32 가 **yaw 를 적분해서** 만든 추측항법 값이다
(`x += ds·cos(yaw)`). 그래서 yaw 가 틀리면 위치도 같이 틀린다 — "yaw 만
틀리고 x·y 는 맞다" 는 성립하지 않는다. 직선 한 번으로는 그게 안 드러난다.
정사각형은 네 변에서 오차가 **누적**되므로, 출발점으로 못 돌아온 거리
(폐합 오차)가 곧 오차의 총합이다.

## 전제 — yaw 는 0x83 IMU 로 확정됐다

**어느 yaw 를 믿을지는 이미 끝난 논의다** (2026-08-06 확정). 0x85 융합 yaw 는
조향 모델 성분 때문에 부풀려지고, 회전 판정은 0x83 quaternion 을 쓴다. 그래서
이 도구는 **두 yaw 중 어느 쪽이 맞는지 고르지 않는다.** 회전 종료 판정도 0x83
하나로만 하고, 0x83 을 못 쓰는 상태면 시험을 시작하지 않는다 — 조용히 0x85 로
폴백하면 확정 사항을 어긴 결과가 통과로 찍힌다.

## 그래서 무엇을 재나

기준선은 **엔코더 거리 + 0x83 yaw 로 재적분한 궤적**이다. 그게 지금 우리가
믿기로 한 값이고, 웹으로 내보낼 값이다.

1. **재적분 궤적의 폐합 오차** — 0x83 기준으로 네 변을 돌고 출발점에 왔나.
   이 시험의 본 판정이다.
2. **직진 구간의 0x83 드리프트** — 직진 명령 중 실제로 얼마나 틀어졌나.
   기계 정렬(서보 중립·토우)과 노면 슬립이 여기 나온다.
3. **회전 정확도** — 90° 명령에 0x83 기준 실제로 몇 도 돌았나. 과회전이면
   `--turn-lead-deg` 로 선행 정지를 준다.
4. **STM32 x·y 오염량 (참고 측정, 판정 아님)** — STM32 가 보고한 x·y 는 0x85
   yaw 로 적분된 값이라 우리 기준선과 갈라진다. 그 벌어진 폭을 숫자로 남긴다.
   `/stm32/odom` 을 0x83 기반으로 바꾸는 작업의 before/after 근거다.
   ⚠ 트림을 걸면 STM32 는 전선에 실린 트림값을 조향각으로 믿고 적분해서
   **직진을 곡선으로 오해한다** (route_runner.py 의 트림 경고 주석). 트림
   +500·축간거리 0.135 m 면 약 37°/m 다.

## ⚠ 궤적이 예쁜 정사각형으로 그려져도 통과가 아니다

센서가 자기 오차를 못 보면 화면에는 정사각형이 뜨고 차는 마름모를 그린다.
0x83 을 믿기로 했다고 해서 0x83 이 자동으로 진실이 되는 것은 아니다 —
BNO085 게인 오차나 바닥 슬립은 IMU 도 못 잡는다. 그래서 마지막에 줄자
실측값을 물어보고 대조한다. `--measured-*` 를 안 주면 그 항목은 "미검증" 으로
남는다 — 통과로 세지 않는다.

## 실행

    # ① 먼저 가짜 STM32 로 리허설 (차 없이, 로직·판정·로그 경로 확인)
    socat pty,raw,echo=0,link=/tmp/ttyJETSON \\
          pty,raw,echo=0,link=/tmp/ttySTM32 &
    ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32 &
    ./odom_square_test.py --port /tmp/ttyJETSON --on-ground --no-prompt

    # ② 실장비 — 트림을 걸고 (실주행과 같은 조건)
    ./odom_square_test.py --port /dev/ttyUSB0 --on-ground --steer-trim 500

    # ③ 실장비 — 트림 없이 (STM32 적분 자체가 깨끗한지)
    ./odom_square_test.py --port /dev/ttyUSB0 --on-ground --steer-trim 0

②와 ③의 "직진 중 yaw 드리프트" 를 비교하면 트림이 만든 오염분이 분리된다.

⚠ **가짜 STM32 는 오염을 재현하지 못한다.** fake 는 명령 조향각으로 물리와
  오도메트리를 **같은 모델**로 적분하고 0x83 yaw 에도 같은 값을 싣는다. 그래서
  오염량이 항상 0 으로 나오고 트림을 걸면 차도 같이 휜다. 리허설은 코드 경로
  검증까지다 — 숫자는 실장비에서만 뜻이 있다.

## 안전

  - `--on-ground` 없이는 실행을 거부한다. 바퀴가 땅에 닿아야 한다.
  - 필요 공간은 아래 `space_hint()` 가 계산해서 시작 전에 출력한다.
  - 정상 종료·예외·Ctrl-C 어느 경로로도 neutral 과 CMD_STOP 을 보낸다.
  - UART 를 여는 프로세스는 이것 하나뿐이다. `route_runner`·`stm32_bridge`·
    `drive_test_suite` 가 떠 있으면 먼저 끈다.
"""
import argparse
import math
import os
import struct
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# drive_test_suite 가 import 시점에 stm32_bridge 경로를 sys.path 에 넣는다.
# UART 소유자(Link)·JSONL 로그(Log)·각도 wrap 을 그대로 재사용한다 — 시험
# 도구가 두 벌이 되면 SEQ 규칙이나 종료 시 CMD_STOP 같은 안전 규약이 한쪽만
# 고쳐진다.
from drive_test_suite import (                                  # noqa: E402
    Link, Log, P_wrap, SPEED_LIMIT, STEER_MAX_LEFT, STEER_MAX_RIGHT)
from stm32_bridge import protocol as P                          # noqa: E402

# 축간거리 [m]. 트림 드리프트 예측값을 찍는 데만 쓴다 (판정에는 미사용).
WHEELBASE_M = 0.135


# ══════════ UART ══════════

class SquareLink(Link):
    """`Link` + TELEMETRY_IMU(0x83) 수집.

    `drive_test_suite` 의 Link 는 2026-08-03 에 쓴 것이라 0x83 분기가 없다.
    그때는 BNO085 프레임을 아무도 안 봤다. 지금은 회전 판정이 0x83 으로
    옮겨갔으므로 이 시험의 비교 기준 자체가 그 프레임이다.
    """

    def __init__(self, *args, **kwargs):
        # ⚠ super().__init__ 이 rx 스레드를 띄운다. 그 전에 칸을 만들어 둬야
        #    첫 0x83 이 AttributeError 로 죽지 않는다.
        self.imu = None
        self.imu_t = None
        super().__init__(*args, **kwargs)

    def _on_frame(self, mid, seq, pl):
        if mid != P.TELEMETRY_IMU:
            super()._on_frame(mid, seq, pl)
            return
        self.counts[mid] = self.counts.get(mid, 0) + 1
        try:
            m = P.unpack_telemetry_imu(pl)
        except (ValueError, struct.error) as e:
            self.log.rec('rx_decode_error', msg_id=mid, err=str(e))
            return
        with self.state_lock:
            self.imu = m
            self.imu_t = time.monotonic()
        self.log.rec('rx', msg='TELEMETRY_IMU', **m)

    def snap3(self):
        """(drive, odom, imu) 한 벌. 세 값의 시각이 어긋나면 안 되므로 한 번에."""
        with self.state_lock:
            return (dict(self.drive) if self.drive else None,
                    dict(self.odom) if self.odom else None,
                    dict(self.imu) if self.imu else None)


def imu_yaw_usable(imu) -> bool:
    """0x83 yaw 를 판정에 써도 되나 (route_runner 와 같은 기준)."""
    return (imu is not None
            and P.imu_yaw_usable(imu['status_flags'],
                                 imu['quaternion_accuracy']))


def wire_steering(cdeg: int, trim: int) -> int:
    """전선에 실제로 나가는 조향각. route_runner 와 같은 규칙이다.

    트림을 더한 뒤 비대칭 한계로 포화시킨다 (좌 +1955 / 우 -2869). 포화를
    빼먹으면 "18° 로 돌렸는데 반경이 왜 이러냐" 로 헤맨다.
    """
    return max(STEER_MAX_RIGHT, min(STEER_MAX_LEFT, int(cdeg) + int(trim)))


def turn_geometry(a):
    """(회전반경 [m], 코너 하나의 호 길이 [m]) — 자전거 모델 공칭값.

    ⚠ 공칭이다. 트림이 걸리면 좌우 반경이 갈라지고(실측 좌 0.63 / 우 0.98 m)
      타이어 슬립도 더해진다. 그래서 이 값은 "필요 공간 안내" 와 "총 주행거리
      기대값" 에만 쓰고 통과 판정의 정밀 기준으로는 쓰지 않는다.
    """
    steer_deg = abs(wire_steering(a.sign * a.turn_steering,
                                  a.steer_trim)) / 100.0
    if steer_deg < 0.1:
        return 99.0, 99.0
    radius = WHEELBASE_M / math.tan(math.radians(steer_deg))
    return radius, radius * math.radians(a.turn_deg)


# ══════════ 표본 ══════════

def sample(link: SquareLink, t0: float, phase: str):
    """지금 상태 한 줄. 궤적 재적분과 플롯이 이 표본만 본다."""
    drive, odom, imu = link.snap3()
    if odom is None:
        return None
    return {
        't': round(time.monotonic() - t0, 4),
        'phase': phase,
        'x_mm': odom['x_mm'],
        'y_mm': odom['y_mm'],
        'yaw_mdeg': odom['yaw_mdeg'],
        'dist_mm': odom['distance_mm'],
        'speed_mm_s': odom['linear_speed_mm_s'],
        'steering_cdeg': odom['steering_cdeg'],
        'odom_flags': odom['status_flags'],
        # IMU 는 없을 수 있다. None 을 그대로 남겨야 "값이 0" 과 구분된다.
        'imu_yaw_mdeg': imu['yaw_mdeg'] if imu else None,
        'imu_usable': imu_yaw_usable(imu),
        'drive_state': drive['drive_state'] if drive else None,
        'fault_bits': drive['active_fault_bits'] if drive else None,
    }


def yaw_of(s: dict):
    """판정에 쓸 yaw [m-deg] = **0x83 IMU 하나뿐이다.** 못 쓰면 None.

    ⚠ 0x85 폴백을 두지 않는다. yaw 출처는 2026-08-06 에 0x83 으로 확정됐고,
      IMU 가 흔들릴 때 조용히 0x85 로 내려가면 "확정 사항을 어긴 주행" 이
      통과로 찍힌다. 부르는 쪽은 None 을 받으면 시험을 멈춰야 한다.
    """
    if s['imu_yaw_mdeg'] is not None and s['imu_usable']:
        return s['imu_yaw_mdeg']
    return None


# ══════════ 주행 단계 ══════════

def blocking_faults(s: dict) -> int:
    """이 표본의 fault 중 **주행을 실제로 막는** 것만.

    ⚠ "fault 비트가 하나라도 있으면 중단" 으로 두면 안 된다. 2026-08-06 실장비
      첫 시도가 그것 때문에 한 발짝도 못 나갔다 — `UART_RX_OVERFLOW`(bit13)가
      켜져 있었는데 그건 `ACT_REPORT_ONLY` 라 주행과 무관하다. 판정 기준은
      route_runner 와 같은 `arm_blocking_faults` 다 (neutral 로 안 풀리는 것).
    """
    return P.arm_blocking_faults(s.get('fault_bits') or 0)


def clear_stale_faults(link: SquareLink, log: Log):
    """묵은 REPORT_ONLY 계열 fault 를 시작 전에 한 번 지운다.

    `UART_RX_OVERFLOW`·`CRC_ERROR` 같은 비트는 주행을 안 막지만 켜진 채로
    남아서 "시험 중에 새로 뜬 것" 과 구분이 안 된다. 지우고 시작한다.
    neutral 상태에서만 부른다 (rearm 직후가 그 자리다).
    """
    d, _o, _i = link.snap3()
    bits = (d['active_fault_bits'] if d else 0) & P.RESETTABLE_FAULT_MASK
    if not bits:
        return
    seq = link.next_seq()
    link.ser.write(P.pack_cmd_reset_fault(seq, bits))
    result = link.wait_result(P.CMD_RESET_FAULT, seq, 1.0)
    log.say(f'  묵은 fault 해제 요청: {P.describe_faults(bits)} → '
            f'{"수락" if result and result["result_code"] == 0 else "미확인"}')
    log.rec('reset_fault', mask=bits, result=result)


def wait_driving(link: SquareLink, log: Log, seconds: float) -> bool:
    """새 명령이 전선에 실려 STM32 가 DRIVING 으로 올라올 때까지 기다린다.

    ⚠ 이게 없으면 **명령이 한 프레임도 안 나간 채 시험이 끝난다.** TX 는 20 Hz
      스케줄러가 보내는데(`Link._tx_loop`), `set_cmd` 직후 곧바로 판정 루프에
      들어가 첫 반복에서 break 하면 스케줄러가 새 명령을 집기 전에 neutral 로
      되돌아간다. 2026-08-06 실장비에서 정확히 그 일이 났다 — 로그의 CMD_DRIVE
      35장이 전부 `speed=0 enable=0` 이었다.
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        d, _o, _i = link.snap3()
        if d and d['drive_state'] == P.STATE_DRIVING:
            return True
        time.sleep(0.02)
    return False


def wait_stopped(link: SquareLink, log: Log, seconds: float):
    """neutral 을 보내고 관성이 멎기를 기다린다.

    각 변의 시작·끝 스냅샷을 **정지 상태에서** 찍어야 구간 경계가 흐려지지
    않는다. 달리는 중에 찍으면 다음 구간에 앞 구간의 이동이 섞인다.
    """
    link.neutral()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.02)
    log.rec('settled')


def run_leg(link: SquareLink, log: Log, a, samples: list, t0: float,
            index: int, target_mm: int):
    """직진 한 변. odom distance_mm 가 target 만큼 늘면 끝낸다.

    ⚠ 종료 판정에 distance_mm(엔코더)를 쓰고 x·y 는 안 쓴다. x·y 가 맞는지가
      이 시험의 질문이라, 그 값으로 종료를 판정하면 순환 논리가 된다. 엔코더
      거리는 yaw 오염과 독립이다.
    """
    phase = f'leg{index}'
    steer = wire_steering(0, a.steer_trim)
    start = sample(link, t0, phase)
    if start is None:
        raise RuntimeError('오도메트리가 아직 안 왔다 — STM32 연결을 확인하라')
    log.rec('leg_begin', index=index, target_mm=target_mm,
            wire_steering_cdeg=steer, snap=start)
    link.set_cmd(a.speed, steer, enable=True)
    if not wait_driving(link, log, a.spinup_sec):
        d, _o, _i = link.snap3()
        state = P.STATE_NAMES.get(d['drive_state'], '?') if d else '무응답'
        raise RuntimeError(
            f'변 {index}: {a.spinup_sec:.1f} s 안에 DRIVING 으로 안 올라온다 '
            f'(지금 {state}, fault={P.describe_faults(d["active_fault_bits"]) if d else "?"}). '
            f'바퀴가 떠 있거나 모터 전원·드라이버를 확인하라')

    deadline = time.monotonic() + a.leg_timeout
    last = start
    reason = 'target'
    while True:
        s = sample(link, t0, phase)
        if s is not None:
            samples.append(s)
            last = s
            if s['dist_mm'] - start['dist_mm'] >= target_mm:
                break
            blocked = blocking_faults(s)
            if blocked:
                reason = f'fault:{P.describe_faults(blocked)}'
                break
        if time.monotonic() > deadline:
            reason = 'timeout'
            break
        time.sleep(0.02)

    wait_stopped(link, log, a.settle_sec)
    end = sample(link, t0, phase) or last
    samples.append(end)
    log.rec('leg_end', index=index, reason=reason, snap=end)
    return start, end, reason


def run_turn(link: SquareLink, log: Log, a, samples: list, t0: float,
             index: int, sign: int):
    """제자리가 아닌 **원호 회전** 한 번. 조향을 고정하고 yaw 로 종료한다.

    Ackermann 이라 제자리 회전이 없다 — 코너는 반경 R 의 호가 된다. 그래서
    정사각형의 모서리는 둥글고, 그건 정상이다. 판정은 "네 번 돌아 원점으로
    돌아왔나" 로 한다.

    종료 yaw 는 **0x83 하나다** — route_runner 의 회전 판정과 같은 기준이라야
    시험이 실주행을 대변한다.
    """
    phase = f'turn{index}'
    steer = wire_steering(sign * a.turn_steering, a.steer_trim)
    start = sample(link, t0, phase)
    log.rec('turn_begin', index=index, sign=sign, wire_steering_cdeg=steer,
            target_deg=a.turn_deg, snap=start)
    if yaw_of(start) is None:
        raise RuntimeError(
            f'코너 {index}: 0x83 yaw 를 쓸 수 없다 (BNO085 미장착이거나 '
            f'quaternion_accuracy 부족). 회전 판정 기준이 0x83 으로 확정돼 '
            f'있어서 0x85 로 대신 돌지 않는다 — 캘리브레이션을 먼저 하라')

    link.set_cmd(a.speed_turn, steer, enable=True)
    if not wait_driving(link, log, a.spinup_sec):
        d, _o, _i = link.snap3()
        state = P.STATE_NAMES.get(d['drive_state'], '?') if d else '무응답'
        raise RuntimeError(
            f'코너 {index}: {a.spinup_sec:.1f} s 안에 DRIVING 으로 안 올라온다 '
            f'(지금 {state})')
    target_mdeg = int(a.turn_deg * 1000) - int(a.turn_lead_deg * 1000)
    # wrap 을 넘겨도 누적이 끊기지 않게 증분을 더한다. ±180° 부근에서
    # 한 번만 빼면 그 코너가 통째로 0° 로 보고된다.
    turned = 0
    prev = yaw_of(start)
    deadline = time.monotonic() + a.turn_timeout
    reason = 'target'
    last = start
    # 0x83 이 끊긴 시각. 실장비는 IMU_FUSED 를 깜빡이므로(실측 최장 542 ms)
    # 한 표본 dropout 으로 회전을 죽이면 안 된다 — route_runner 와 같은 유예를
    # 준다. 유예를 넘기면 **0x85 로 대신 돌지 않고 멈춘다.**
    lost_since = None
    while True:
        s = sample(link, t0, phase)
        if s is not None:
            samples.append(s)
            last = s
            cur = yaw_of(s)
            if cur is None:
                if lost_since is None:
                    lost_since = time.monotonic()
                elif time.monotonic() - lost_since > a.imu_dropout_grace:
                    reason = 'imu_lost'
                    break
            else:
                lost_since = None
                if prev is not None:
                    turned += P_wrap(cur - prev)
                prev = cur
            if abs(turned) >= target_mdeg:
                break
            blocked = blocking_faults(s)
            if blocked:
                reason = f'fault:{P.describe_faults(blocked)}'
                break
        if time.monotonic() > deadline:
            reason = 'timeout'
            break
        time.sleep(0.02)

    wait_stopped(link, log, a.settle_sec)
    end = sample(link, t0, phase) or last
    samples.append(end)
    log.rec('turn_end', index=index, reason=reason,
            turned_deg=turned / 1000.0, snap=end)
    return start, end, reason, turned


# ══════════ 분석 ══════════

def delta(start: dict, end: dict) -> dict:
    """한 구간의 증분. 각도는 wrap 을 접어서 뺀다."""
    dx = (end['x_mm'] - start['x_mm']) / 1000.0
    dy = (end['y_mm'] - start['y_mm']) / 1000.0
    d = {
        'dx_m': dx,
        'dy_m': dy,
        'move_m': math.hypot(dx, dy),
        'dist_m': (end['dist_mm'] - start['dist_mm']) / 1000.0,
        'dyaw_deg': P_wrap(end['yaw_mdeg'] - start['yaw_mdeg']) / 1000.0,
        'course_deg': math.degrees(math.atan2(dy, dx)),
    }
    if start['imu_yaw_mdeg'] is not None and end['imu_yaw_mdeg'] is not None:
        d['dimu_deg'] = P_wrap(
            end['imu_yaw_mdeg'] - start['imu_yaw_mdeg']) / 1000.0
    else:
        d['dimu_deg'] = None
    return d


def integrate(samples: list):
    """엔코더 거리 증분 + **0x83 yaw** 로 궤적을 적분한다 — 이게 기준선이다.

    STM32 가 준 x·y 와 같은 재료를 다르게 조립한 궤적이고, 우리가 믿기로 한
    조합이다. 시작 yaw 를 빼서 상대화한다 — 0x83 은 game rotation vector 라
    절대 방위가 아니고 부팅 시점 기준이라 0x85 와 원점이 다르다.
    """
    pts = [(0.0, 0.0)]
    if not samples:
        return pts
    base = yaw_of(samples[0])
    if base is None:
        return pts
    x = y = 0.0
    for prev, cur in zip(samples, samples[1:]):
        ds = (cur['dist_mm'] - prev['dist_mm']) / 1000.0
        if abs(ds) < 1e-6:
            continue                    # 정지 구간. 방향만 바뀐다
        yaw = yaw_of(cur)
        if yaw is None:
            continue
        rad = math.radians(P_wrap(yaw - base) / 1000.0)
        x += ds * math.cos(rad)
        y += ds * math.sin(rad)
        pts.append((x, y))
    return pts


def reported_path(samples: list):
    """STM32 가 보고한 x·y 를 시작점 기준 상대 좌표로."""
    if not samples:
        return [(0.0, 0.0)]
    x0, y0 = samples[0]['x_mm'] / 1000.0, samples[0]['y_mm'] / 1000.0
    return [(s['x_mm'] / 1000.0 - x0, s['y_mm'] / 1000.0 - y0)
            for s in samples]


def ascii_plot(paths, width: int = 61, height: int = 25):
    """궤적 여러 개를 한 격자에 겹쳐 그린다.

    matplotlib 을 안 쓴다 — 이 시험은 차 옆에 쪼그려 앉아 ssh 로 돌린다.
    사진으로 남길 그림이 아니라 "지금 이게 정사각형이냐" 를 즉시 보는 용도다.
    """
    pts = [p for _, _, path in paths for p in path]
    if len(pts) < 2:
        return ['(표본이 부족해서 궤적을 그릴 수 없다)']
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    lo_x, hi_x = min(xs), max(xs)
    lo_y, hi_y = min(ys), max(ys)
    span = max(hi_x - lo_x, hi_y - lo_y, 0.1) * 1.1
    cx, cy = (lo_x + hi_x) / 2.0, (lo_y + hi_y) / 2.0
    # 문자 셀이 가로로 좁아서(대략 1:2) 세로 배율을 절반으로 둔다.
    grid = [[' '] * width for _ in range(height)]

    def put(px, py, ch):
        # +x 전방을 위로, +y 좌를 왼쪽으로. 위에서 내려다본 그림이다.
        col = int((width - 1) / 2 - (py - cy) / span * (width - 1))
        row = int((height - 1) / 2 - (px - cx) / span * (height - 1))
        if 0 <= row < height and 0 <= col < width:
            grid[row][col] = ch

    for _, ch, path in paths:
        for x, y in path:
            put(x, y, ch)
    for _, _, path in paths:
        put(path[0][0], path[0][1], 'S')
        put(path[-1][0], path[-1][1], 'E')
    scale = f'  격자 한 변 ≈ {span:.2f} m  (S=출발 E=도착, 위=전방 왼쪽=좌)'
    return [''.join(r) for r in grid] + [scale]


# ══════════ 판정 ══════════

class Verdict:
    """판정 한 벌. 미검증 항목은 통과로 세지 않는다."""

    def __init__(self):
        self.rows = []

    def add(self, name, ok, detail):
        self.rows.append((name, ok, detail))

    def report(self, log: Log):
        log.say('')
        log.say('── 판정 ' + '─' * 62)
        for name, ok, detail in self.rows:
            mark = {True: 'PASS', False: 'FAIL', None: '미검증'}[ok]
            log.say(f'  [{mark:^6}] {name}')
            log.say(f'           {detail}')
        failed = [n for n, ok, _ in self.rows if ok is False]
        skipped = [n for n, ok, _ in self.rows if ok is None]
        log.say('')
        if failed:
            log.say(f'결과: FAIL — {len(failed)}개 실패 ({", ".join(failed)})')
        elif skipped:
            log.say(f'결과: 조건부 통과 — {len(skipped)}개 미검증 '
                    f'({", ".join(skipped)})')
        else:
            log.say('결과: PASS')
        return not failed


def analyse(log: Log, a, legs, turns, samples) -> bool:
    v = Verdict()
    side = a.side_m

    # ⓪ 0x83 표본 건전성. 기준선이 이 값 하나라 결손율을 먼저 본다.
    usable = sum(1 for s in samples if s['imu_usable'])
    ratio_ok = usable / max(len(samples), 1)
    v.add('0x83 표본 건전성',
          ratio_ok >= a.min_imu_ratio,
          f'{usable}/{len(samples)} 표본 ({ratio_ok * 100:.1f}%, 최소 '
          f'{a.min_imu_ratio * 100:.0f}%). yaw 기준선이 0x83 하나라 여기가 '
          f'뚫리면 아래 숫자 전부가 구멍 난 궤적 위에서 나온 것이다')

    # ① 각 변의 직진성. 기준은 0x83 이고, 0x85 는 오염량 참고로만 찍는다.
    log.say('')
    log.say('── 직진 구간 ' + '─' * 57)
    log.say('  변   이동거리   엔코더    Δyaw(0x83)   Δyaw(0x85)   코스각')
    drifts_imu, drifts_odom = [], []
    for i, (s, e, _r) in enumerate(legs, 1):
        d = delta(s, e)
        drifts_odom.append(d['dyaw_deg'])
        if d['dimu_deg'] is not None:
            drifts_imu.append(d['dimu_deg'])
        imu_txt = ('  ---  ' if d['dimu_deg'] is None
                   else f"{d['dimu_deg']:+7.2f}")
        log.say(f"  {i}   {d['move_m']:7.3f}m  {d['dist_m']:7.3f}m  "
                f"{imu_txt}°   {d['dyaw_deg']:+9.2f}°   "
                f"{d['course_deg']:+7.2f}°")
    if drifts_imu:
        worst_imu = max(abs(x) for x in drifts_imu)
        v.add('직진 중 0x83 yaw 드리프트',
              worst_imu <= a.max_leg_drift_deg,
              f'최대 {worst_imu:+.2f}° '
              f'({worst_imu / max(side, 1e-6):+.1f}°/m, 허용 '
              f'{a.max_leg_drift_deg}°). 이건 차가 **실제로** 휜 양이다 — '
              f'서보 중립·토우 정렬과 노면 슬립이 여기 나온다')
    else:
        v.add('직진 중 0x83 yaw 드리프트', None, '0x83 표본이 없다')

    # ② 회전 정확도. 명령한 각도만큼 실제로 돌았나 (0x83 기준).
    log.say('')
    log.say('── 회전 구간 ' + '─' * 57)
    log.say('  코너  실제(0x83)   목표    오차     보고(0x85)')
    turn_errs = []
    for i, (s, e, _r, _t) in enumerate(turns, 1):
        d = delta(s, e)
        if d['dimu_deg'] is None:
            log.say(f"  {i}       ---              ---     "
                    f"{d['dyaw_deg']:+8.2f}°")
            continue
        err = abs(d['dimu_deg']) - a.turn_deg
        turn_errs.append(err)
        log.say(f"  {i}    {d['dimu_deg']:+8.2f}°  {a.turn_deg:5.1f}°  "
                f"{err:+6.2f}°   {d['dyaw_deg']:+8.2f}°")
    if turn_errs:
        worst_turn = max(abs(x) for x in turn_errs)
        avg_turn = sum(turn_errs) / len(turn_errs)
        v.add('회전 정확도 (0x83 기준)',
              worst_turn <= a.max_turn_error_deg,
              f'최대 오차 {worst_turn:.2f}° · 평균 {avg_turn:+.2f}° '
              f'(허용 {a.max_turn_error_deg}°). 평균이 일관되게 +면 관성 '
              f'과회전이다 — `--turn-lead-deg {max(1.0, avg_turn):.0f}` 으로 '
              f'선행 정지를 주고 다시 돌린다')
    else:
        v.add('회전 정확도 (0x83 기준)', None, '0x83 회전량을 못 읽었다')

    # ③ 폐합. 기준선(엔코더 + 0x83 재적분)이 본 판정이다.
    first, last = samples[0], samples[-1]
    rep = reported_path(samples)
    imu_path = integrate(samples)
    limit = side * a.closure_ratio
    reported_close = math.hypot((last['x_mm'] - first['x_mm']) / 1000.0,
                                (last['y_mm'] - first['y_mm']) / 1000.0)
    if len(imu_path) > 2:
        close = math.hypot(*imu_path[-1])
        v.add('폐합 오차 (엔코더 + 0x83 재적분)',
              close <= limit,
              f'{close:.3f} m (변 {side:.2f} m 의 {close / side * 100:.1f}%, '
              f'허용 {limit:.3f} m). 우리가 믿기로 한 조합으로 계산한 궤적이 '
              f'출발점에 얼마나 가까이 돌아왔나 — 이 시험의 본 판정이다')
    else:
        close = reported_close
        v.add('폐합 오차 (엔코더 + 0x83 재적분)', None,
              '0x83 표본이 부족해 재적분을 못 했다')

    # ④ 엔코더 총 거리. yaw 와 무관한 축이라 따로 본다.
    #    ⚠ 기대값에 **회전 호 길이를 반드시 더한다.** 직진분만 세면 정상 주행도
    #      70% 초과로 뜬다 (반경 0.42 m 코너 넷이면 호만 2.6 m 다).
    total = (last['dist_mm'] - first['dist_mm']) / 1000.0
    radius, arc = turn_geometry(a)
    straight = side * a.laps * 4
    want = (side + arc) * a.laps * 4
    err = abs(total - want) / max(want, 1e-6)
    v.add('엔코더 총 주행거리',
          total >= straight and err <= a.max_total_error,
          f'{total:.3f} m / 기대 {want:.3f} m (직진 {straight:.3f} + 호 '
          f'{arc * a.laps * 4:.3f}, 반경 공칭 {radius:.3f} m, 오차 '
          f'{err * 100:.1f}%, 허용 {a.max_total_error * 100:.0f}%). '
          f'직진분 {straight:.3f} m 미만이면 엔코더 스케일이 확실히 틀렸고, '
          f'초과분이 큰 것은 실제 반경이 공칭보다 크다는 뜻이다 (트림·슬립)')

    # ⑤ 궤적 그림 + STM32 x·y 오염량 (참고 측정 — 판정 아님).
    log.say('')
    log.say('── 궤적 ' + '─' * 62)
    log.say('  i = 엔코더 + 0x83 재적분 (기준선)   o = STM32 가 보고한 x·y')
    plots = []
    if len(imu_path) > 2:
        plots.append(('imu', 'i', imu_path))
    plots.append(('reported', 'o', rep))
    for line in ascii_plot(plots):
        log.say('  ' + line)
    if len(imu_path) > 2:
        gap = math.hypot(rep[-1][0] - imu_path[-1][0],
                         rep[-1][1] - imu_path[-1][1])
        log.say(f'  종점 차이 {gap:.3f} m · 기준선 폐합 {close:.3f} m · '
                f'STM32 보고 폐합 {reported_close:.3f} m')
        # ⚠ 판정이 아니라 **측정**이다. STM32 x·y 가 0x85 yaw 로 적분된다는 건
        #   이미 아는 사실이고, 여기서 재는 건 그 오염이 몇 m 인가다. 이 숫자가
        #   `/stm32/odom` 을 0x83 기반으로 바꾸는 작업의 before 값이 된다.
        v.add('STM32 x·y 오염량 (참고)', None,
              f'기준선과 종점이 {gap:.3f} m 갈라졌다 (보고 폐합 '
              f'{reported_close:.3f} m vs 기준선 {close:.3f} m). '
              f'`/stm32/odom` 이 아직 0x85 yaw 로 적분된 x·y 를 싣고 있어서 '
              f'생기는 차이다 — 0x83 기반으로 바꾼 뒤 다시 재면 줄어야 한다')
    log.rec('paths', reported=rep[::5], imu=imu_path[::5],
            reported_closure_m=reported_close, baseline_closure_m=close)

    # ⑥ 줄자 실측. 이게 있어야 "진짜 맞는지" 가 판정된다.
    if a.measured_closure_m is None:
        v.add('실측 폐합 오차', None,
              '줄자값을 안 줬다. 출발 표시와 최종 위치 사이를 재서 '
              '--measured-closure-m 으로 다시 돌리면 절대 오차가 판정된다')
    else:
        diff = abs(a.measured_closure_m - close)
        v.add('실측 폐합 오차',
              a.measured_closure_m <= limit,
              f'실측 {a.measured_closure_m:.3f} m vs 기준선 {close:.3f} m '
              f'(차이 {diff:.3f} m, 허용 {limit:.3f} m). 차이가 크면 0x83 이 '
              f'자기 오차를 못 보고 있다는 뜻이다 — 화면의 정사각형이 거짓이고 '
              f'IMU 게인이나 바닥 슬립을 의심해야 한다')
    if a.measured_side_m is None:
        v.add('실측 변 길이', None,
              '줄자값을 안 줬다 (--measured-side-m)')
    else:
        moved = [delta(s, e)['dist_m'] for s, e, _ in legs]
        avg_leg = sum(moved) / len(moved)
        err_s = abs(avg_leg - a.measured_side_m) / max(a.measured_side_m, 1e-6)
        v.add('실측 변 길이',
              err_s <= a.max_dist_error,
              f'실측 {a.measured_side_m:.3f} m vs 엔코더 평균 '
              f'{avg_leg:.3f} m (오차 {err_s * 100:.1f}%). 여기가 틀리면 '
              f'yaw 이전에 엔코더 스케일부터 잡아야 한다')

    return v.report(log)


# ══════════ 실행 ══════════

def space_hint(a) -> str:
    """필요한 바닥 넓이. 시작 전에 사람에게 보여 준다."""
    radius, _arc = turn_geometry(a)
    steer_deg = abs(wire_steering(a.sign * a.turn_steering,
                                  a.steer_trim)) / 100.0
    need = a.side_m + 2 * radius + 0.5
    return (f'회전반경 약 {radius:.2f} m (조향 {steer_deg:.2f}°) → '
            f'최소 {need:.1f} m × {need:.1f} m 의 빈 바닥이 필요하다')


def run(link: SquareLink, log: Log, a):
    samples, legs, turns = [], [], []
    t0 = time.monotonic()
    target_mm = int(a.side_m * 1000)

    if not link.rearm(timeout=5.0):
        d, _o, _i = link.snap3()
        raise RuntimeError(
            'STM32 가 READY/DRIVING 으로 안 올라온다 '
            f'(지금 {P.STATE_NAMES.get(d["drive_state"], "?") if d else "무응답"}, '
            f'fault={P.describe_faults(d["active_fault_bits"]) if d else "?"}). '
            'neutral 을 보내도 SAFE_STOP 이면 fault 가 latch 된 상태다')
    if not a.no_reset_fault:
        clear_stale_faults(link, log)

    for lap in range(a.laps):
        for corner in range(4):
            index = lap * 4 + corner + 1
            log.say(f'  변 {index}/{a.laps * 4} 직진 {a.side_m:.2f} m …')
            legs.append(run_leg(link, log, a, samples, t0, index, target_mm))
            if legs[-1][2] != 'target':
                raise RuntimeError(
                    f'변 {index} 이 {legs[-1][2]} 로 끝났다 — 중단한다')
            log.say(f'  코너 {index}/{a.laps * 4} 회전 {a.turn_deg:.0f}° …')
            turns.append(run_turn(link, log, a, samples, t0, index, a.sign))
            if turns[-1][2] != 'target':
                raise RuntimeError(
                    f'코너 {index} 이 {turns[-1][2]} 로 끝났다 — 중단한다')
    return legs, turns, samples


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='정사각형 주행으로 STM32 오도메트리 x·y 를 검증한다',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('## 실행')[1] if '## 실행' in __doc__ else '')
    ap.add_argument('--port', default='/dev/ttyUSB0',
                    help='STM32 UART. 가짜는 /tmp/ttyJETSON')
    ap.add_argument('--baud', type=int, default=115200)
    ap.add_argument('--on-ground', action='store_true',
                    help='필수. 바퀴가 땅에 닿아 있고 주변이 비었음을 확인')
    ap.add_argument('--no-prompt', action='store_true')
    ap.add_argument('--side-m', type=float, default=1.0, help='한 변 [m]')
    ap.add_argument('--laps', type=int, default=1, help='정사각형 바퀴 수')
    # ⚠ 기본 100 은 **차체 무게 때문에 100 이하로 달려야 한다**는 2026-08-06
    #   현장 판단이다. 예전 기록과 충돌하는 값이라 근거를 같이 남긴다 —
    #   `tag_drive.launch.py` 주석의 2026-08-05 실측은 "명령 150 → 실측
    #   122~171 mm/s" 였고, 90 은 "마지막 15° 에서 바퀴가 멈출 수 있다" 는
    #   이유로 v_turn_slow 를 150 으로 올렸다. 100 에서 회전이 안 끝나면 이
    #   시험은 코너를 `timeout` 으로 실패시킨다 — 조용히 넘어가지 않는다.
    ap.add_argument('--speed', type=int, default=100, help='직진 [mm/s]')
    ap.add_argument('--speed-turn', type=int, default=100,
                    help='회전 [mm/s]. 마지막 각도에서 바퀴가 서면 올린다')
    ap.add_argument('--turn-deg', type=float, default=90.0)
    ap.add_argument('--turn-steering', type=int, default=1800,
                    help='회전 조향 크기 [cdeg]. 부호는 --direction 이 정한다')
    ap.add_argument('--turn-lead-deg', type=float, default=0.0,
                    help='관성 선행 정지 [deg]. 과회전하면 3 정도로 올린다')
    ap.add_argument('--direction', choices=('right', 'left'), default='right')
    ap.add_argument('--steer-trim', type=int, default=0,
                    help='조향 중립 트림 [cdeg]. 실주행 조건은 500. '
                         '0 과 500 을 각각 돌려 비교하는 것이 이 시험의 요점')
    # yaw 출처는 인자가 아니다 — 0x83 으로 확정됐다 (모듈 머리 "전제").
    ap.add_argument('--imu-dropout-grace', type=float, default=0.8,
                    help='회전 중 0x83 끊김 허용 시간 [s]. route_runner 의 '
                         'fusion_dropout_grace_s 와 같은 값. 넘기면 0x85 로 '
                         '대신 돌지 않고 시험을 멈춘다')
    ap.add_argument('--settle-sec', type=float, default=1.0)
    ap.add_argument('--spinup-sec', type=float, default=2.0,
                    help='명령 후 DRIVING 으로 올라오기를 기다리는 시간')
    ap.add_argument('--no-reset-fault', action='store_true',
                    help='시작 전 묵은 REPORT_ONLY fault 해제를 건너뛴다')
    # 100 mm/s 기준 여유. 변 1 m = 10 s, 코너 호 0.65 m = 6.5 s 다.
    ap.add_argument('--leg-timeout', type=float, default=40.0)
    ap.add_argument('--turn-timeout', type=float, default=40.0)
    ap.add_argument('--closure-ratio', type=float, default=0.15,
                    help='폐합 허용 = 변 길이 × 이 비율')
    ap.add_argument('--max-leg-drift-deg', type=float, default=5.0,
                    help='직진 한 변에서 허용하는 0x83 yaw 변화')
    ap.add_argument('--max-turn-error-deg', type=float, default=5.0,
                    help='코너 하나의 0x83 회전량이 목표에서 벗어나도 되는 폭')
    ap.add_argument('--min-imu-ratio', type=float, default=0.9,
                    help='0x83 을 쓸 수 있었던 표본의 최소 비율')
    ap.add_argument('--max-dist-error', type=float, default=0.10,
                    help='줄자 실측 변 길이 대비 허용 오차')
    ap.add_argument('--max-total-error', type=float, default=0.25,
                    help='총 주행거리 기대값(직진+공칭 호) 대비 허용 오차. '
                         '반경이 트림·슬립으로 흔들려서 넉넉히 둔다')
    ap.add_argument('--measured-closure-m', type=float, default=None,
                    help='줄자로 잰 출발점~최종위치 거리 [m]')
    ap.add_argument('--measured-side-m', type=float, default=None,
                    help='줄자로 잰 실제 한 변 [m]')
    ap.add_argument('--log-dir',
                    default=os.path.expanduser('~/drive_test_logs'))
    a = ap.parse_args(argv)

    if not a.on_ground:
        raise SystemExit(
            '거부: 이것은 실제 바닥 주행 시험이다.\n'
            '  바퀴를 땅에 대고 주변을 비운 뒤 --on-ground 를 붙여라.')
    if not 0 < a.speed <= SPEED_LIMIT or not 0 < a.speed_turn <= SPEED_LIMIT:
        raise SystemExit(f'속도는 1~{SPEED_LIMIT} mm/s 범위여야 한다')
    if a.side_m <= 0 or a.laps <= 0:
        raise SystemExit('--side-m 과 --laps 는 양수여야 한다')
    a.sign = -1 if a.direction == 'right' else 1
    if not STEER_MAX_RIGHT < a.sign * a.turn_steering < STEER_MAX_LEFT:
        raise SystemExit(
            f'--turn-steering 이 조향 한계를 넘는다 '
            f'(좌 +{STEER_MAX_LEFT} / 우 {STEER_MAX_RIGHT} cdeg)')

    os.makedirs(a.log_dir, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = os.path.join(a.log_dir, f'odom_square_{stamp}.jsonl')
    log = Log(path)
    log.rec('config', **{k: v for k, v in vars(a).items()},
            wheelbase_m=WHEELBASE_M)

    log.say(f'정사각형 오도메트리 시험 — 변 {a.side_m:.2f} m × {a.laps} 바퀴 '
            f'({a.direction})')
    log.say(f'  포트 {a.port} · 직진 {a.speed} mm/s · 회전 {a.speed_turn} mm/s '
            f'· 트림 {a.steer_trim:+d} cdeg')
    log.say(f'  {space_hint(a)}')
    log.say(f'  로그 {path}')
    if not a.no_prompt:
        input('  주변을 비우고 출발점을 바닥에 표시한 뒤 Enter: ')

    link = None
    ok = False
    try:
        link = SquareLink(a.port, a.baud, log)
        time.sleep(0.5)                 # 첫 텔레메트리를 기다린다
        legs, turns, samples = run(link, log, a)
        ok = analyse(log, a, legs, turns, samples)
    except KeyboardInterrupt:
        log.say('\n중단됨 (Ctrl-C) — 정지 명령을 보낸다')
    except (RuntimeError, OSError) as e:
        log.say(f'\n실패: {e}')
        log.rec('error', err=str(e))
    finally:
        if link is not None:
            try:
                link.neutral()
                time.sleep(0.2)
                link.send_stop(P.STOP_MISSION_COMPLETE)
            except Exception:                               # noqa: BLE001
                pass                    # 이미 닫힌 포트다. 더 할 게 없다
            link.close()
        log.say('')
        log.say(f'로그: {path}')
        log.say('줄자로 출발 표시~최종 위치를 재서 다음처럼 다시 돌리면 '
                '절대 오차까지 판정된다:')
        log.say(f'  {os.path.basename(__file__)} … '
                f'--measured-closure-m <잰값>')
        log.close()
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
