r"""오도메트리 검증 도구 — 고정 시간 프로파일 기반 5개 모드 단일 파일.

    straight            지정 속도·시간 직진 후 CSV 저장
    straight-calibrate  실측 거리로 "한 변" 주행시간을 역산해 프로파일 저장
    turn-calibrate      실측 회전각으로 90° 주행시간을 역산해 좌/우 따로 저장
    turn-test           저장된 프로파일로 주행하고 odom yaw 를 실측과 대조
    square-test         직선+회전 프로파일로 4회 반복, 누적 오도메트리 폐합오차

## 설계 원칙 — 오도메트리는 **판정 대상**이지 종료 조건이 아니다

모든 구간의 종료 조건은 **고정 시간**이다. odom yaw 가 90° 가 되면 멈추는 식으로
짜면 "오도메트리가 맞는가" 를 오도메트리로 판정하는 순환이 된다. 그래서 거리·각도
도달 판정을 일절 쓰지 않는다. 기존 `scripts/odom_square_test.py` 는 반대로 odom·IMU
도달로 구간을 끊는다 — 그쪽은 주행 재현성 시험이고 이 파일은 센서 검증이라 목적이
다르다. 둘을 섞지 말 것.

## 통신은 기존 코드를 그대로 재사용한다

`scripts/drive_test_suite.py` 의 `Link`(UART 소유자 · TX 20 Hz 스케줄러 · RX 파서 ·
rearm) 와 `Log`(JSONL) 를 import 한다. 패킷 인코딩·CRC·프레임 파서·serial 연결은
`stm32_bridge.protocol` / `stm32_bridge.parser` 를 그대로 쓴다. 새 메시지 ID 도, 별도
프로토콜도 추가하지 않는다 — 시험 도구가 두 벌이 되면 SEQ 규칙이나 안전 규약이
한쪽만 고쳐진다.

SEQ 는 `protocol.SeqCounter` 가 `(v + 1) & 0xFF` 로 매 송신마다 올린다 (중립 포함).
명령 반영은 텔레메트리 `last_drive_seq` 로 확인한다 — `CMD_DRIVE` 는
`COMMAND_RESULT` 가 없어서 그것이 유일한 수락 확인 경로다. 구간 시작 시 1 초 안에
반영되지 않으면 중단한다.

## `RESET_ODOM` 은 쓰지 않는다 — 프로토콜에 없다

`protocol.py` 의 Jetson→STM32 명령은 `CMD_DRIVE(0x10)`, `CMD_STOP(0x11)`,
`CMD_RESET_FAULT(0x12)` **셋뿐**이다. 오도메트리를 0 으로 되돌리는 명령은 없다.

각 시험 시작 시 **첫 번째 `pose_valid` 인 `0x85`** 를 기준값으로 저장해 차분한다.
무효 프레임을 원점으로 잡으면 이후 모든 상대값이 근거 없는 값에서 출발한다
(강행하려면 `--allow-invalid-pose`).

    relative_x_mm        = x_mm - start_x_mm
    relative_y_mm        = y_mm - start_y_mm
    relative_distance_mm = distance_mm - start_distance_mm
    relative_yaw_mdeg    = wrap(yaw_mdeg - start_yaw_mdeg)   ±180000 정규화
    return_error_mm      = sqrt((final_x-start_x)² + (final_y-start_y)²)

CSV 는 원본 필드(`x_mm` 등)를 그대로 두고 파생 열을 따로 둔다 — 원본을 덮어쓰면
나중에 "STM32 가 뭘 보냈나" 를 되짚을 수 없다. `fwd_mm`/`lat_mm` 는 같은 변위를
출발 방향으로 역회전한 전방·좌횡 성분이다 (직진 시험의 좌우 이탈량).

⚠ MCU 가 시험 중 재부팅하면 원점이 무의미해진다. `mcu_time_ms` 역행을 감시해
그 경우 즉시 중단한다.

## `CMD_STOP` 을 쓰지 않는다

구간 사이에도, 최종 종료에도 쓰지 않는다. `CMD_STOP` 은 STM32 의 `rearm_required`
를 다시 세워 다음 구간 진입을 스스로 막는다. 대신 중립 `CMD_DRIVE`
(`speed=0, steering=0, enable=0`)를 20 Hz 로 계속 보낸다 — 송신을 멈추면 watchdog
이 `COMM_TIMEOUT` 을 올리므로 멈추면 안 된다.

구간 사이에는 조건 셋을 **모두** 확인한 뒤 다음 구간을 시작한다.

    drive_state == READY(2)
    |measured_speed_mm_s| <= STOP_SPEED_MM_S
    안정화 대기시간 경과

최종 종료도 중립 `CMD_DRIVE` 를 1 초 이상 반복하는 것으로 확정한다.

## 조향 명령값은 실측으로만 확정한다

`--steering` 은 **절대 명령값**이고 트림을 더하지 않는다. 시작 후보는 좌 `+500` /
우 `-500` (현재 주행 로직이 쓰는 값)이며 실차 결과에 따라 700·1000 등으로 올린다.
좌우 최대가 비대칭(좌 +1955 / 우 -2869)이라 좌우 조건은 **독립으로** 저장한다.

⚠ 이 값을 실제 바퀴각으로 환산하지 않는다. STM32 의 7점 LUT 를 거치므로 대응은
실측으로만 확정된다. 회전 구간에서 굴렀는데 yaw 변화가 5° 미만이면 "그 명령이 이
차에서 사실상 직진" 이라는 뜻이므로 경고를 띄운다.

직진 구간의 `--steer-trim` 기본값은 `500` 이다 (`tag_drive.launch.py` 의
`steer_trim_cdeg`, 2026-08-04 실측 직진값). `0` 을 주면 그대로 0 을 보낸다.

## 안전

- 시작 전 `drive_state` 와 fault bit 를 해석해서 출력한다
- ARM 실패 시 원인·fault 를 출력하고 **차를 움직이지 않는다**
- 중단은 **상태**로 판정한다: `SAFE_STOP(4)`·`FAULT(5)`·`ESTOP(6)` 진입,
  telemetry 단절, MCU 재부팅. `active_fault_bits != 0` 자체는 중단 사유가 아니다 —
  `READY`/`DRIVING` 이면 fault bit 를 CSV·터미널에 기록만 하고 계속한다
- Ctrl-C·예외·정상종료 어느 경로로든 `finally` 에서 중립을 1 초 이상 반복 송신
- 장애물 감지·안전 로직을 우회하지 않는다. bit16 이 뜨면 STM32 가 SAFE_STOP 하고
  이 도구는 그것을 **중단 사유로 보고**한다 (고정 시간 시험이라 재개하지 않는다)

## 포트 점유

`route_runner` 와 동시에 띄우면 포트 충돌이 난다. 실행 시 안내를 출력한다.

  ros2 node list 로 route_runner 가 없는지 먼저 확인할 것
"""
import argparse
import csv
import json
import math
import os
import signal
import sys
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, '..', 'scripts'))

# drive_test_suite 가 import 시점에 stm32_bridge 경로를 sys.path 에 넣는다.
from drive_test_suite import (                                  # noqa: E402
    Link, Log, SPEED_LIMIT, STEER_MAX_LEFT, STEER_MAX_RIGHT)
from stm32_bridge import protocol as P                          # noqa: E402

DEFAULT_PROFILES = os.path.join(_HERE, 'odom_profiles.json')
# 실운영 직진값. `tag_drive.launch.py` steer_trim_cdeg (2026-08-04 실측).
DEFAULT_STEER_TRIM = 500
TELEMETRY_TIMEOUT_S = 0.5      # 0x80 무수신 상한. 20 Hz 이므로 10 프레임분
SETTLE_S = 1.0                 # 구간 사이 안정화 대기
# 정지 판정 임계값. 20 mm/s 는 route_logic 이 '움직인다' 로 보는 문턱과
# 같은 자리다. 0 을 요구하면 잔진동 때문에 영원히 못 넘어간다.
STOP_SPEED_MM_S = 20
NEUTRAL_TAIL_S = 1.2           # 종료 후 neutral 반복 송신 (요구: 최소 1 초)
SAMPLE_HZ = 20.0               # CSV 표본 주기

# CSV 열. 앞쪽은 스펙이 요구한 **실제 패킷 필드**, 뒤쪽 rel_* 는 파생값이다.
CSV_FIELDS = [
    'host_time_s', 'mcu_time_ms', 'test_mode', 'segment_index',
    'segment_type', 'direction', 'target_speed_mm_s', 'measured_speed_mm_s',
    'motor_duty_permille', 'steering_cmd_cdeg', 'steering_cdeg',
    'encoder_count', 'x_mm', 'y_mm', 'yaw_mdeg', 'distance_mm',
    'linear_speed_mm_s', 'yaw_rate_mdeg_s', 'curvature_micro_per_m',
    'drive_state', 'active_fault_bits', 'status_flags', 'steering_source',
    'last_drive_seq',
    # ── 파생 (RESET_ODOM 이 없어 소프트웨어 원점으로 계산) ──
    'relative_x_mm', 'relative_y_mm', 'relative_distance_mm',
    'relative_yaw_mdeg', 'relative_yaw_deg', 'fwd_mm', 'lat_mm',
]


# ══════════ 각도 ══════════

def wrap_mdeg(mdeg: int) -> float:
    """m-deg 를 (-180000, 180000] 으로. 차분 전에 반드시 통과시킨다."""
    return (mdeg + 180000) % 360000 - 180000


# ══════════ 중단 사유 ══════════

class Abort(Exception):
    """시험을 즉시 끝내야 하는 상태. 메시지가 곧 보고 사유다."""


# ══════════ 소프트웨어 원점 ══════════

class Origin:
    """`RESET_ODOM` 대신 쓰는 소프트웨어 원점.

    시험 시작 시점의 0x85 를 기억하고 이후 값에서 뺀다. STM32 의 누적값을
    건드리지 않으므로 원본 로그가 그대로 남는다.
    """

    def __init__(self, odom: dict):
        self.x0 = odom['x_mm']
        self.y0 = odom['y_mm']
        self.yaw0 = odom['yaw_mdeg']
        self.dist0 = odom['distance_mm']
        self.mcu0 = odom['mcu_time_ms']
        # 출발 방향을 x축으로 삼기 위한 역회전. 앞 시험이 차를 돌려놓은 상태에서
        # 시작하면 단순 차분은 **전역 좌표계** 변위가 되어, 직진 시험인데
        # rel_y 가 수백 mm 로 나온다 (실제로 그렇게 나와서 잡았다).
        self._c = math.cos(-math.radians(self.yaw0 / 1000.0))
        self._s = math.sin(-math.radians(self.yaw0 / 1000.0))

    def rel(self, odom: dict) -> dict:
        """원점 기준 상대값. 두 벌을 낸다.

        `relative_*` 는 **명세 정의 그대로의 단순 차분**이다 (odom 전역 좌표계).

            relative_x_mm        = x_mm - start_x_mm
            relative_y_mm        = y_mm - start_y_mm
            relative_distance_mm = distance_mm - start_distance_mm
            relative_yaw_mdeg    = wrap(yaw_mdeg - start_yaw_mdeg)  ±180000

        `fwd_mm`/`lat_mm` 는 그 변위를 **출발 방향으로 역회전**한 전방·좌횡
        성분이다. 직진 시험에서 좌우 이탈량을 읽으려면 이쪽이 필요하다 — 앞
        시험이 차를 돌려놓은 상태에서 시작하면 전역 차분만으로는 직진인데
        relative_y 가 수백 mm 로 나온다 (실측으로 확인했다).

        폐합오차 sqrt(dx²+dy²) 는 회전이 거리를 보존하므로 두 벌이 같다.
        """
        dx = odom['x_mm'] - self.x0
        dy = odom['y_mm'] - self.y0
        return {
            'relative_x_mm': dx,
            'relative_y_mm': dy,
            'relative_distance_mm': odom['distance_mm'] - self.dist0,
            'relative_yaw_mdeg': wrap_mdeg(odom['yaw_mdeg'] - self.yaw0),
            'relative_yaw_deg': round(
                wrap_mdeg(odom['yaw_mdeg'] - self.yaw0) / 1000.0, 3),
            'fwd_mm': round(dx * self._c - dy * self._s, 1),
            'lat_mm': round(dx * self._s + dy * self._c, 1),
        }

    def return_error_mm(self, odom: dict) -> float:
        """복귀 오차 = sqrt((final_x - start_x)² + (final_y - start_y)²)."""
        dx = odom['x_mm'] - self.x0
        dy = odom['y_mm'] - self.y0
        return math.sqrt(dx * dx + dy * dy)


# ══════════ 프로파일 ══════════

def load_profiles(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f'프로파일을 읽을 수 없습니다 ({path}): {e}')
    if not isinstance(data, dict):
        raise SystemExit(f'프로파일 최상위가 object 가 아닙니다: {path}')
    return data


def save_profile(path: str, key: str, profile: dict):
    """`key` 만 갱신한다. **다른 프로파일을 지우지 않는다.**

    좌회전 보정을 하고 우회전 보정을 하면 앞의 것이 사라지는 일이 없어야 한다.
    """
    data = load_profiles(path)
    data[key] = profile
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write('\n')
    print(f'\n  프로파일 저장: {path} [{key}]')
    print(f'  {json.dumps(profile, ensure_ascii=False)}')
    others = [k for k in data if k != key]
    if others:
        print(f'  (유지된 다른 프로파일: {", ".join(sorted(others))})')


def require_profile(profiles: dict, key: str, hint: str) -> dict:
    if key not in profiles:
        print(f'\n⛔ 프로파일 "{key}" 가 없습니다. 차를 움직이지 않고 끝냅니다.')
        print('   먼저 아래를 실행해 조건을 만드십시오:\n')
        print(f'     {hint}\n')
        raise SystemExit(2)
    return profiles[key]


# ══════════ 상태·안전 ══════════

def describe_state(drive: dict) -> str:
    st = P.STATE_NAMES.get(drive['drive_state'], f"?({drive['drive_state']})")
    fb = drive['active_fault_bits']
    out = f"state={st} fault=0x{fb:08X}"
    if fb:
        out += f' [{P.describe_faults(fb)}]'
    return out


def print_preflight(link: Link):
    """시작 전 상태·fault 를 해석해서 보여준다."""
    print('\n── 사전 점검 ' + '─' * 48)
    drive, odom = link.snap()
    if drive is None:
        raise Abort('0x80 TELEMETRY_DRIVE 무수신 — 포트·전원·배선을 확인하십시오')
    print(f'  {describe_state(drive)}')
    print(f"  target={drive['target_speed_mm_s']:>5} "
          f"measured={drive['measured_speed_mm_s']:>5} mm/s  "
          f"steer_cmd={drive['steering_cmd_cdeg']:>5} "
          f"enc={drive['encoder_count']}")
    blocking = P.arm_blocking_faults(drive['active_fault_bits'])
    if blocking:
        print(f'\n⛔ ARM 을 막는 fault 가 있습니다: 0x{blocking:08X}')
        print(f'   [{P.describe_faults(blocking)}]')
        print('   해제 방식은 fault 별로 다릅니다 (docs/로그_판독_가이드.md):')
        for b in range(32):
            if blocking & (1 << b):
                e = P.FAULT_TABLE.get(b)
                if e:
                    print(f'     bit{b:<2} {e[0]:<18} 조치={e[1]:<16} 해제={e[2]}')
        if blocking & P.RESETTABLE_FAULT_MASK:
            print('   → CMD_RESET_FAULT 대상 bit 가 있습니다: '
                  'ros2 service call /route/reset_fault std_srvs/srv/Trigger')
        raise Abort(f'차단 fault 활성 0x{blocking:08X}')
    if odom is None:
        print('  ⚠ 0x85 TELEMETRY_ODOMETRY 를 아직 못 받았습니다.')
    else:
        pv = P.odom_pose_valid(odom['status_flags'], odom['steering_source'])
        src = P.ODOM_STEERING_SOURCE_NAMES.get(
            odom['steering_source'], '?')
        print(f"  odom x={odom['x_mm']} y={odom['y_mm']} "
              f"dist={odom['distance_mm']} mm "
              f"yaw={odom['yaw_mdeg'] / 1000.0:+.2f}°")
        print(f"  status=0x{odom['status_flags']:04X} steering_source={src} "
              f"pose_valid={pv}")
        if not pv:
            print('  ⚠ pose_valid=false — 좌표를 신뢰할 수 없는 상태입니다. '
                  '그래도 기록은 남기지만 판정에 쓰지 마십시오.')
    print('─' * 60)


class Guard:
    """매 표본에서 중단 조건을 본다. 안전 로직을 우회하지 않는다."""

    def __init__(self, link: Link):
        self.link = link
        self._last_rx = time.monotonic()
        self._prev_mcu = None
        self._fault_seen = 0        # 이미 알린 fault bit (중복 출력 방지)

    def check(self, drive: dict, odom: dict, target_speed: int):
        now = time.monotonic()
        if drive is None:
            if now - self._last_rx > TELEMETRY_TIMEOUT_S:
                raise Abort('0x80 무수신 — telemetry 단절')
            return
        self._last_rx = now

        mcu = drive['mcu_time_ms']
        if self._prev_mcu is not None and mcu < self._prev_mcu:
            raise Abort(f'MCU 재부팅 감지 (mcu_time_ms {self._prev_mcu} → {mcu}) '
                        '— 소프트웨어 원점이 무의미해졌습니다')
        self._prev_mcu = mcu

        # ── 중단은 **상태**로 판정한다. `active_fault_bits != 0` 자체는
        #    중단 사유가 아니다 — REPORT_ONLY 계열(CRC_ERROR·RANGE_LOST·
        #    IMU_LOST 등)은 주행을 막지 않고, 그것으로 끊으면 정상 주행이
        #    수시로 중단된다. STM32 가 조치를 취해야 할 fault 면 스스로
        #    SAFE_STOP 으로 내려온다.
        st = drive['drive_state']
        if st == P.STATE_SAFE_STOP:
            fb = drive['active_fault_bits']
            why = 'SAFE_STOP(4) 진입'
            if fb & P.FAULT_OBSTACLE_NEAR:
                why += ' — 초음파 장애물(bit16). 경로를 비우고 다시 실행하십시오'
            elif fb:
                why += f' — fault 0x{fb:08X} [{P.describe_faults(fb)}]'
            raise Abort(why)
        if st in (P.STATE_FAULT, P.STATE_ESTOP):
            raise Abort(f'{P.STATE_NAMES.get(st)}({st}) 진입 — '
                        f'{describe_state(drive)}')

        # READY/DRIVING 인데 fault bit 가 떠 있으면 **기록만** 한다. CSV 의
        # active_fault_bits 열에 매 표본 남고, 터미널에는 새로 뜬 bit 만 한 번.
        fb = drive['active_fault_bits']
        new_bits = fb & ~self._fault_seen
        if new_bits:
            self._fault_seen |= new_bits
            kind = ('차단' if P.arm_blocking_faults(new_bits)
                    else 'REPORT_ONLY')
            print(f'\n  ⚠ fault 0x{new_bits:08X} [{P.describe_faults(new_bits)}]'
                  f' — {kind}. state={P.STATE_NAMES.get(st)} 라 계속합니다.')
        # 데드밴드는 중단 사유가 아니다 — 구간 끝에서 `run_segment` 가 경고한다.
        # 저속에서 안 도는 것은 결함일 수도, 최소 제어속도 미달일 수도 있다.


# ══════════ CSV ══════════

class Recorder:
    """표본을 CSV 로 흘려 쓴다. 예외가 나도 지금까지의 표본은 남는다."""

    def __init__(self, path: str, mode: str):
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.path = path
        self.mode = mode
        self.f = open(path, 'w', newline='', encoding='utf-8')
        self.w = csv.DictWriter(self.f, fieldnames=CSV_FIELDS)
        self.w.writeheader()
        self.rows = 0
        self.t0 = time.monotonic()

    def write(self, drive, odom, origin, *, segment_index, segment_type,
              direction, target_speed, steering_cmd):
        row = {k: '' for k in CSV_FIELDS}
        row['host_time_s'] = round(time.monotonic() - self.t0, 4)
        row['test_mode'] = self.mode
        row['segment_index'] = segment_index
        row['segment_type'] = segment_type
        row['direction'] = direction or ''
        row['target_speed_mm_s'] = target_speed
        row['steering_cmd_cdeg'] = steering_cmd
        if drive:
            row['mcu_time_ms'] = drive['mcu_time_ms']
            row['measured_speed_mm_s'] = drive['measured_speed_mm_s']
            row['motor_duty_permille'] = drive['motor_duty_permille']
            row['encoder_count'] = drive['encoder_count']
            row['drive_state'] = drive['drive_state']
            row['active_fault_bits'] = drive['active_fault_bits']
            row['last_drive_seq'] = drive['last_drive_seq']
        if odom:
            row['x_mm'] = odom['x_mm']
            row['y_mm'] = odom['y_mm']
            row['yaw_mdeg'] = odom['yaw_mdeg']
            row['distance_mm'] = odom['distance_mm']
            row['linear_speed_mm_s'] = odom['linear_speed_mm_s']
            row['yaw_rate_mdeg_s'] = odom['yaw_rate_mdeg_s']
            row['curvature_micro_per_m'] = odom['curvature_micro_per_m']
            row['status_flags'] = odom['status_flags']
            row['steering_source'] = odom['steering_source']
            row['steering_cdeg'] = odom['steering_cdeg']
            if origin is not None:
                row.update(origin.rel(odom))
        self.w.writerow(row)
        self.f.flush()
        self.rows += 1

    def close(self):
        try:
            self.f.close()
        except Exception:                                       # noqa: BLE001
            pass


def write_summary(csv_path: str, summary: dict) -> str:
    """CSV 옆에 `*_summary.json` 을 쓴다."""
    base = os.path.splitext(os.path.abspath(csv_path))[0]
    path = base + '_summary.json'
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(f'  요약 저장: {path}')
    return path


# ══════════ 구간 실행 ══════════

def clamp_steer(cdeg: int) -> int:
    """좌우 **비대칭** 한계로 자른다. 넘기면 COMMAND_LIMIT 으로 조용히 거부된다."""
    return max(STEER_MAX_RIGHT, min(STEER_MAX_LEFT, int(cdeg)))


def run_segment(link: Link, rec: Recorder, guard: Guard, origin, *,
                speed: int, steering: int, duration: float,
                segment_index: int, segment_type: str, direction=None,
                label: str = ''):
    """**고정 시간** 동안 명령을 유지하며 표본을 기록한다.

    종료 조건은 시간뿐이다. odom 거리·각도를 보지 않는다 — 그것이 검증 대상이다.
    """
    speed = int(speed)
    steering = clamp_steer(steering)
    if abs(speed) > SPEED_LIMIT:
        raise Abort(f'속도 {speed} 가 실측 한계 {SPEED_LIMIT} mm/s 를 넘습니다')

    print(f'\n▶ [{segment_index}] {label or segment_type} — '
          f'speed={speed} steer={steering} {duration:.2f}s')
    link.set_cmd(speed, steering, enable=(speed != 0 or steering != 0))
    # CMD_DRIVE 는 COMMAND_RESULT 가 없다. 수락 확인 경로는 텔레메트리의
    # last_drive_seq 가 이번 세션에서 보낸 SEQ 로 바뀌는지 보는 것뿐이다.
    # 안 바뀌면 STM32 가 프레임을 버리고 있는데 시험은 계속 도는 상태가 된다.
    if not link.wait_accepted(timeout=1.0):
        raise Abort('CMD_DRIVE 가 수락되지 않았습니다 — last_drive_seq 가 '
                    '이번 세션 SEQ 로 바뀌지 않습니다 (CRC·배선·펌웨어 확인)')

    d0, o0 = link.snap()
    period = 1.0 / SAMPLE_HZ
    t_start = time.monotonic()
    nxt = t_start
    last_draw = 0.0
    while True:
        now = time.monotonic()
        if now - t_start >= duration:
            break
        drive, odom = link.snap()
        guard.check(drive, odom, speed)
        if now >= nxt:
            nxt += period
            if nxt <= now:
                nxt = now + period
            rec.write(drive, odom, origin, segment_index=segment_index,
                      segment_type=segment_type, direction=direction,
                      target_speed=speed, steering_cmd=steering)
            if drive and now - last_draw > 0.25:
                last_draw = now
                r = origin.rel(odom) if (origin and odom) else {}
                print(f"\r  t={now - t_start:5.2f}s "
                      f"measured={drive['measured_speed_mm_s']:>5} mm/s "
                      f"duty={drive['motor_duty_permille']:>5}‰ "
                      f"dist={r.get('relative_distance_mm', '-'):>6} mm "
                      f"yaw={r.get('relative_yaw_deg', '-'):>8}°   ",
                      end='', flush=True)
        time.sleep(0.002)
    print()

    d1, o1 = link.snap()
    if speed and d1 and abs(d1['measured_speed_mm_s']) < 5:
        print(f'  ⚠ 목표 {speed} mm/s 인데 측정 속도가 계속 0 근처였습니다. '
              '모터 데드밴드 의심 — 속도를 올려 다시 재십시오.')
    moved = None
    if o0 and o1:
        moved = o1['distance_mm'] - o0['distance_mm']
        turned = wrap_mdeg(o1['yaw_mdeg'] - o0['yaw_mdeg']) / 1000.0
        print(f'  구간 결과: odom Δdist={moved:+} mm  Δyaw={turned:+.2f}°')
        if segment_type == 'turn' and abs(turned) < 5.0 and moved:
            # 조향 명령과 실제 바퀴각의 대응은 STM32 7점 LUT 를 거쳐 결정되므로
            # 미리 알 수 없다. 굴러갔는데 안 돌았다면 그 명령이 이 차에서
            # 사실상 직진이라는 뜻이다 — 값을 키워 다시 재야 한다.
            print(f'  ⚠ 조향 {steering} cdeg 로 {moved} mm 굴렀는데 yaw 가 '
                  f'{turned:+.2f}° 뿐입니다. 이 차에서 그 명령은 거의 직진일 '
                  '수 있습니다 — --steering 을 키워 다시 재십시오.')
    return d1, o1, moved


def settle(link: Link, rec: Recorder, guard: Guard, origin, *,
           seconds: float, segment_index: int, direction=None,
           timeout_s: float = 5.0, require_ready: bool = False):
    """다음 구간을 시작해도 되는지 **조건 셋을 모두** 확인한다.

        drive_state 가 READY(2) 또는 DRIVING(3) 이고 target_speed_mm_s == 0
        |measured_speed_mm_s| <= STOP_SPEED_MM_S
        안정화 대기시간 `seconds` 경과

    ⚠ **`READY(2)` 하나만 요구할 수 없다.** 명세 §3.1 상태표에 `DRIVING` 에서
      `READY` 로 돌아가는 전이가 없다 — `DRIVING` 을 벗어나는 길은 `CMD_STOP`,
      watchdog timeout, fault 셋뿐이고 그 셋은 모두 `SAFE_STOP` 으로 간다. 그래서
      "구간 사이 READY 확인" 과 "CMD_STOP 금지" 는 이 펌웨어에서 동시에 만족할 수
      없다 (실측으로 확인: neutral 을 5 초 보내도 state=DRIVING, measured=0).

      대신 **의도에 맞는 조건**을 쓴다: 상태가 정상 주행 계열이고, 목표 속도가 0
      이며, 실측 속도가 임계값 이하 — 즉 "무장된 채 멈춰 서 있다". `--require-ready`
      로 문자 그대로의 READY 를 요구할 수 있지만 그때는 timeout 이 날 것이다.

    `CMD_STOP` 을 쓰지 않는다 — 그것은 STM32 의 `rearm_required` 를 다시 세워
    다음 구간 진입을 스스로 막는다. neutral `CMD_DRIVE`(0,0,enable=0)를 20 Hz 로
    계속 보내며 기다린다 (송신을 멈추면 watchdog 이 COMM_TIMEOUT 을 올린다).

    셋을 다 못 채우면 `timeout_s` 뒤 중단한다 — 조용히 다음 구간으로 넘어가면
    아직 굴러가는 차에서 다음 구간이 시작된다.
    """
    want = 'READY' if require_ready else 'READY|DRIVING+target0'
    print(f'  ⏸ 정지 확인 ({want} · |v|≤{STOP_SPEED_MM_S} mm/s · '
          f'{seconds:.1f}s 안정화)')
    link.neutral()
    period = 1.0 / SAMPLE_HZ
    t0 = time.monotonic()
    nxt = t0
    ok_since = None          # 세 조건 중 앞의 둘이 만족된 시각
    while True:
        now = time.monotonic()
        drive, odom = link.snap()
        guard.check(drive, odom, 0)
        if now >= nxt:
            nxt += period
            if nxt <= now:
                nxt = now + period
            rec.write(drive, odom, origin, segment_index=segment_index,
                      segment_type='settle', direction=direction,
                      target_speed=0, steering_cmd=0)
        if drive is None:
            ready = stopped = False
        elif require_ready:
            ready = drive['drive_state'] == P.STATE_READY
            stopped = abs(drive['measured_speed_mm_s']) <= STOP_SPEED_MM_S
        else:
            ready = (drive['drive_state'] in (P.STATE_READY, P.STATE_DRIVING)
                     and drive['target_speed_mm_s'] == 0)
            stopped = abs(drive['measured_speed_mm_s']) <= STOP_SPEED_MM_S
        if ready and stopped:
            if ok_since is None:
                ok_since = now
            if now - ok_since >= seconds:
                return
        else:
            ok_since = None
        if now - t0 > timeout_s:
            st = describe_state(drive) if drive else '0x80 무수신'
            v = drive['measured_speed_mm_s'] if drive else '?'
            hint = ('  — 명세 §3.1 에 DRIVING→READY 전이가 없다. '
                    '--require-ready 를 뺄 것' if require_ready else '')
            raise Abort(
                f'{timeout_s:.1f}s 안에 정지 조건을 못 채웠습니다 '
                f'(정지판정={ready} |v|={v} mm/s · {st}){hint}')
        time.sleep(0.002)


def neutral_tail(link: Link, seconds: float = NEUTRAL_TAIL_S):
    """종료 후 속도 0·조향 0 을 최소 1 초 반복 송신한다.

    TX 스케줄러가 20 Hz 로 계속 보내므로 `neutral()` 후 기다리면 된다.
    송신을 멈추면 STM32 watchdog 이 COMM_TIMEOUT 을 올린다.
    """
    try:
        link.neutral()
        link.resume_tx()
        time.sleep(seconds)
    except Exception:                                           # noqa: BLE001
        pass


def prompt_enter(msg: str):
    print(f'\n{msg}')
    try:
        input('  준비되면 Enter (중단은 Ctrl-C) ... ')
    except EOFError:
        print('  (비대화 입력 — 계속 진행)')


def ask_float(msg: str, *, allow_blank=False):
    while True:
        try:
            s = input(msg).strip()
        except EOFError:
            if allow_blank:
                return None
            raise Abort('입력이 필요한데 표준입력이 닫혔습니다')
        if not s and allow_blank:
            return None
        try:
            return float(s)
        except ValueError:
            print('  숫자로 입력하십시오.')


def ask_yes(msg: str) -> bool:
    while True:
        try:
            s = input(msg).strip().lower()
        except EOFError:
            return False
        if s in ('y', 'yes', 'ㅇ'):
            return True
        if s in ('n', 'no', ''):
            return False


# ══════════ 세션 ══════════

class Session:
    """UART 를 열고 사전점검·ARM 까지 마친 상태를 들고 있는다."""

    def __init__(self, args, mode: str):
        self.mode = mode
        self.args = args
        log_dir = os.path.join(os.path.expanduser('~'), 'drive_test_logs')
        os.makedirs(log_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        self.log = Log(os.path.join(log_dir, f'odom_validation_{mode}_{stamp}.jsonl'))
        print(f'  JSONL: {self.log.path}')
        print(f'  포트: {args.port} @ {args.baud} 8-N-1')
        print('  ⚠ route_runner 와 동시에 띄우면 포트 충돌이 납니다.')
        print('    ros2 node list 로 route_runner 가 없는지 확인하십시오.')
        self.link = Link(args.port, args.baud, self.log)
        self.guard = None
        self.origin = None

    def arm(self):
        """0x80 수신 → 사전점검 → neutral 재무장."""
        end = time.monotonic() + 3.0
        while time.monotonic() < end and self.link.snap()[0] is None:
            time.sleep(0.02)
        print_preflight(self.link)
        print('\n  neutral 재무장 중 ...')
        if not self.link.rearm(timeout=3.0):
            drive, _ = self.link.snap()
            extra = describe_state(drive) if drive else '0x80 무수신'
            raise Abort(f'ARM 실패 — READY/DRIVING 으로 오지 않았습니다 ({extra})')
        drive, _ = self.link.snap()
        print(f'  ARM 완료 — {describe_state(drive)}')
        self.guard = Guard(self.link)

    def set_origin(self, require_valid: bool = True):
        """소프트웨어 원점 확정 (RESET_ODOM 대체).

        **첫 번째 `pose_valid` 인 0x85** 를 기준으로 삼는다. 무효 프레임을
        원점으로 잡으면 이후 모든 상대값이 근거 없는 값에서 출발한다.
        """
        end = time.monotonic() + 3.0
        odom = None
        skipped = 0
        while time.monotonic() < end:
            _, cand = self.link.snap()
            if cand:
                if not require_valid or P.odom_pose_valid(
                        cand['status_flags'], cand['steering_source']):
                    odom = cand
                    break
                skipped += 1
            time.sleep(0.02)
        if odom is None:
            _, cand = self.link.snap()
            if cand is None:
                raise Abort(
                    '0x85 TELEMETRY_ODOMETRY 무수신 — 원점을 정할 수 없습니다')
            raise Abort(
                f'0x85 가 계속 pose_valid=false 입니다 '
                f"(status=0x{cand['status_flags']:04X} "
                f"steering_source={cand['steering_source']}) — "
                '원점을 정할 근거가 없습니다. --allow-invalid-pose 로 강행 가능')
        if skipped:
            print(f'  (무효 pose {skipped} 프레임을 건너뛰고 원점을 잡았습니다)')
        self.origin = Origin(odom)
        print(f'  소프트웨어 원점 확정 (RESET_ODOM 명령이 없어 차분 방식): '
              f"x={odom['x_mm']} y={odom['y_mm']} "
              f"yaw={odom['yaw_mdeg'] / 1000.0:+.2f}° "
              f"dist={odom['distance_mm']}")
        self.log.rec('origin', **{k: odom[k] for k in
                                  ('x_mm', 'y_mm', 'yaw_mdeg', 'distance_mm',
                                   'mcu_time_ms')})

    def close(self):
        """종료. **`CMD_STOP` 을 보내지 않는다.**

        `CMD_STOP` 은 STM32 의 `rearm_required` 를 다시 세워 다음 실행이 재무장을
        거쳐야 하게 만든다. 중립 `CMD_DRIVE`(0,0,enable=0)를 1 초 이상 반복하는
        것으로 정지를 확정한다 — 그것이 이 도구의 기본 종료 방식이다.
        """
        neutral_tail(self.link)
        self.link.close()
        self.log.close()


# ══════════ 모드 1: straight ══════════

def cmd_straight(args):
    trim = args.steer_trim
    s = Session(args, 'straight')
    rec = None
    aborted = None
    try:
        s.arm()
        prompt_enter(f'직선 주행 {args.duration:.2f}s @ {args.speed} mm/s '
                     f'(조향 {trim} cdeg). 진행 경로를 비우십시오.')
        s.set_origin(not args.allow_invalid_pose)
        rec = Recorder(args.output, 'straight')
        run_segment(s.link, rec, s.guard, s.origin,
                    speed=args.speed, steering=trim, duration=args.duration,
                    segment_index=0, segment_type='straight',
                    label='직진')
        settle(s.link, rec, s.guard, s.origin, seconds=SETTLE_S,
               require_ready=args.require_ready, segment_index=0)
        _, odom = s.link.snap()
        r = s.origin.rel(odom) if odom else {}
        print('\n── 결과 ' + '─' * 52)
        print(f"  odom 이동거리   : {r.get('relative_distance_mm')} mm")
        print(f"  relative_x/y_mm : {r.get('relative_x_mm')} / "
              f"{r.get('relative_y_mm')}  (전역 차분)")
        print(f"  전방 / 좌횡     : {r.get('fwd_mm')} / {r.get('lat_mm')} mm  "
              '← 좌횡이 곧 직진 이탈량')
        print(f"  odom yaw 변화   : {r.get('relative_yaw_deg')}° "
              f"({r.get('relative_yaw_mdeg')} mdeg) "
              '— 직진이면 0 에 가까워야 합니다')
        print(f'  CSV             : {args.output} ({rec.rows} 행)')
        write_summary(args.output, {
            'test_mode': 'straight', 'aborted': None,
            'target_speed_mm_s': args.speed, 'steering_cmd_cdeg': trim,
            'duration_s': args.duration, 'csv_rows': rec.rows, **r,
        })
    except Abort as e:
        aborted = str(e)
        print(f'\n⛔ 중단: {aborted}')
        if rec:
            write_summary(args.output, {'test_mode': 'straight',
                                        'aborted': aborted,
                                        'csv_rows': rec.rows})
    finally:
        s.close()
        if rec:
            rec.close()
    return 1 if aborted else 0


# ══════════ 모드 2: straight-calibrate ══════════

def cmd_straight_calibrate(args):
    trim = args.steer_trim
    duration = args.duration
    s = Session(args, 'straight-calibrate')
    rec = None
    aborted = None
    try:
        s.arm()
        print(f'\n목표 거리 {args.target_distance:.0f} mm 를 만드는 주행시간을 '
              '찾습니다.')
        print('  출발선에 뒷축 중앙을 맞추고, 도착 후 줄자로 실제 이동거리를 '
              '재서 입력하십시오.')
        attempt = 0
        while True:
            attempt += 1
            prompt_enter(f'[{attempt}회차] {duration:.2f}s @ {args.speed} mm/s '
                         f'(조향 {trim} cdeg)')
            s.set_origin(not args.allow_invalid_pose)
            out = args.output or f'straight_cal_{attempt:02d}.csv'
            if rec:
                rec.close()
            rec = Recorder(out, 'straight-calibrate')
            run_segment(s.link, rec, s.guard, s.origin,
                        speed=args.speed, steering=trim, duration=duration,
                        segment_index=attempt, segment_type='straight',
                        label=f'보정 {attempt}회차')
            settle(s.link, rec, s.guard, s.origin, seconds=SETTLE_S,
                   require_ready=args.require_ready, segment_index=attempt)
            _, odom = s.link.snap()
            r = s.origin.rel(odom) if odom else {}
            print(f"\n  odom 이동거리: {r.get('relative_distance_mm')} mm "
                  f"(x={r.get('fwd_mm')} y={r.get('lat_mm')})")

            measured = ask_float('  실제 측정거리 [mm]: ')
            if measured <= 0:
                print('  0 이하는 쓸 수 없습니다. 다시 재십시오.')
                continue
            rec_time = duration * args.target_distance / measured
            print(f'\n  현재 시간     : {duration:.2f}초')
            print(f'  실제 측정거리 : {measured:.0f} mm')
            print(f'  목표 거리     : {args.target_distance:.0f} mm')
            print(f'  추천 시간     : {rec_time:.2f}초')
            if r.get('relative_distance_mm') is not None and measured:
                err = r['relative_distance_mm'] - measured
                print(f'  odom 오차     : {err:+.0f} mm '
                      f'({err / measured * 100:+.1f}%)')

            if ask_yes('\n  이 조건을 straight 프로파일로 저장합니까? [y/N] '):
                save_profile(args.profiles, 'straight', {
                    'speed_mm_s': int(args.speed),
                    'steering_cdeg': int(trim),
                    'duration_s': round(duration, 3),
                    'target_distance_mm': int(args.target_distance),
                    'measured_distance_mm': int(measured),
                    'measured_at': datetime.now().isoformat(
                        timespec='seconds'),
                })
                break
            if ask_yes(f'  추천 시간 {rec_time:.2f}초 로 다시 시험합니까? [y/N] '):
                duration = rec_time
                continue
            print('  저장하지 않고 끝냅니다.')
            break
    except Abort as e:
        aborted = str(e)
        print(f'\n⛔ 중단: {aborted}')
    finally:
        s.close()
        if rec:
            rec.close()
    return 1 if aborted else 0


# ══════════ 모드 3: turn-calibrate ══════════

def cmd_turn_calibrate(args):
    direction = args.direction
    steering = clamp_steer(args.steering)
    if steering != args.steering:
        print(f'  ⚠ 조향 {args.steering} → {steering} cdeg 로 잘렸습니다 '
              f'(좌 최대 +{STEER_MAX_LEFT} / 우 최대 {STEER_MAX_RIGHT}).')
    if direction == 'left' and steering <= 0:
        raise SystemExit('좌회전은 조향이 양수여야 합니다 (예: --steering 1000)')
    if direction == 'right' and steering >= 0:
        raise SystemExit('우회전은 조향이 음수여야 합니다 (예: --steering -1000)')

    duration = args.duration
    s = Session(args, f'turn-calibrate-{direction}')
    rec = None
    aborted = None
    try:
        s.arm()
        print(f'\n{direction} 90° 회전 조건을 찾습니다.')
        print('  ⚠ 종료 조건은 **고정 시간**입니다. odom yaw 로 멈추지 않습니다 '
              '— 그것이 검증 대상이기 때문입니다.')
        attempt = 0
        while True:
            attempt += 1
            prompt_enter(
                f'[{attempt}회차] 차량을 **시작 방향선에 정렬**하십시오.\n'
                f'  회전 반경만큼 공간이 필요합니다. '
                f'{duration:.2f}s @ {args.speed} mm/s, 조향 {steering} cdeg')
            s.set_origin(not args.allow_invalid_pose)
            out = args.output or f'turn_cal_{direction}_{attempt:02d}.csv'
            if rec:
                rec.close()
            rec = Recorder(out, f'turn-calibrate-{direction}')
            run_segment(s.link, rec, s.guard, s.origin,
                        speed=args.speed, steering=steering, duration=duration,
                        segment_index=attempt, segment_type='turn',
                        direction=direction, label=f'{direction} 회전')
            settle(s.link, rec, s.guard, s.origin, seconds=SETTLE_S,
                   require_ready=args.require_ready, segment_index=attempt, direction=direction)
            _, odom = s.link.snap()
            r = s.origin.rel(odom) if odom else {}
            print(f"\n  odom yaw 변화 : {r.get('relative_yaw_deg')}°  "
                  f"(참고용 — 판정 기준이 아닙니다)")
            print(f"  odom 이동거리 : {r.get('relative_distance_mm')} mm")

            actual = ask_float('  실제 측정 회전각 [deg, 부호 없이 크기]: ')
            if abs(actual) < 1e-6:
                print('  0° 는 쓸 수 없습니다.')
                continue
            rec_time = duration * 90.0 / abs(actual)
            print(f'\n  현재 시간     : {duration:.2f}초')
            print(f'  실제 회전각   : {abs(actual):.1f}°')
            print(f'  추천 시간     : {rec_time:.2f}초')

            if ask_yes(f'\n  이 조건을 {direction} 프로파일로 저장합니까? [y/N] '):
                save_profile(args.profiles, direction, {
                    'speed_mm_s': int(args.speed),
                    'steering_cdeg': int(steering),
                    'duration_s': round(duration, 3),
                    'measured_angle_deg': round(abs(actual), 2),
                    'measured_at': datetime.now().isoformat(
                        timespec='seconds'),
                })
                break
            if ask_yes(f'  추천 시간 {rec_time:.2f}초 로 다시 시험합니까? [y/N] '):
                duration = rec_time
                continue
            print('  저장하지 않고 끝냅니다.')
            break
    except Abort as e:
        aborted = str(e)
        print(f'\n⛔ 중단: {aborted}')
    finally:
        s.close()
        if rec:
            rec.close()
    return 1 if aborted else 0


# ══════════ 모드 4: turn-test ══════════

def cmd_turn_test(args):
    direction = args.direction
    profiles = load_profiles(args.profiles)
    prof = require_profile(
        profiles, direction,
        f'python3 tools/odom_validation.py turn-calibrate '
        f'--port {args.port} --direction {direction} '
        f'--speed 100 --steering {"1000" if direction == "left" else "-1000"} '
        f'--duration 4.0')
    speed = int(prof['speed_mm_s'])
    steering = clamp_steer(prof['steering_cdeg'])
    duration = float(prof['duration_s'])
    print(f'\n  프로파일 [{direction}]: speed={speed} steer={steering} '
          f'duration={duration:.2f}s '
          f"(보정 시 실측 {prof.get('measured_angle_deg', '?')}°)")

    s = Session(args, f'turn-test-{direction}')
    rec = None
    aborted = None
    try:
        s.arm()
        prompt_enter('차량을 **시작 방향선에 정렬**하십시오. '
                     '저장된 고정 조건으로 1회 주행합니다.')
        s.set_origin(not args.allow_invalid_pose)
        rec = Recorder(args.output, f'turn-test-{direction}')
        run_segment(s.link, rec, s.guard, s.origin,
                    speed=speed, steering=steering, duration=duration,
                    segment_index=0, segment_type='turn',
                    direction=direction, label=f'{direction} 90° 검증')
        settle(s.link, rec, s.guard, s.origin, seconds=SETTLE_S,
               require_ready=args.require_ready, segment_index=0, direction=direction)
        _, odom = s.link.snap()
        drive, _ = s.link.snap()
        r = s.origin.rel(odom) if odom else {}
        odom_yaw = r.get('relative_yaw_deg')
        print(f'\n  odom yaw: {odom_yaw}°')

        actual = ask_float('  정지 후 실제 측정 회전각 [deg, 부호 없이 크기]: ')
        # 부호가 달라도 절대오차가 정상 계산되게 **크기**로 비교한다.
        # 좌회전은 yaw 가 +, 우회전은 - 로 나오므로 그대로 빼면 180° 오차가 된다.
        odom_mag = abs(odom_yaw) if odom_yaw is not None else None
        actual_mag = abs(actual)
        summary = {
            'test_mode': f'turn-test-{direction}',
            'direction': direction,
            'reference_deg': 90.0,
            'profile': prof,
            'odom_yaw_deg': odom_yaw,
            'odom_yaw_magnitude_deg': odom_mag,
            'actual_angle_deg': actual_mag,
            'odom_distance_mm': r.get('relative_distance_mm'),
            'final_x_mm': r.get('fwd_mm'),
            'final_y_mm': r.get('lat_mm'),
            'active_fault_bits': drive['active_fault_bits'] if drive else None,
            'csv_rows': rec.rows,
            'aborted': None,
        }
        if odom_mag is not None and actual_mag > 0:
            signed = odom_mag - actual_mag
            summary['signed_error_deg'] = round(signed, 3)
            summary['absolute_error_deg'] = round(abs(signed), 3)
            summary['error_rate_percent'] = round(
                abs(signed) / actual_mag * 100.0, 3)
            print('\n── 결과 ' + '─' * 52)
            print('  목표 기준각   : 90°')
            print(f'  실제 측정각   : {actual_mag:.1f}°')
            print(f'  오도메트리 yaw: {odom_yaw:+.1f}° (크기 {odom_mag:.1f}°)')
            print(f'  부호 오차     : {signed:+.1f}°')
            print(f'  절대오차      : {abs(signed):.1f}°')
            print(f"  오차율        : {summary['error_rate_percent']:.1f}% "
                  '(기준 = 실제 측정각)')
        print(f'  CSV           : {args.output} ({rec.rows} 행)')
        write_summary(args.output, summary)
    except Abort as e:
        aborted = str(e)
        print(f'\n⛔ 중단: {aborted}')
        if rec:
            write_summary(args.output, {
                'test_mode': f'turn-test-{direction}', 'aborted': aborted,
                'csv_rows': rec.rows})
    finally:
        s.close()
        if rec:
            rec.close()
    return 1 if aborted else 0


# ══════════ 모드 5: square-test ══════════

def cmd_square_test(args):
    direction = args.direction
    profiles = load_profiles(args.profiles)
    straight = require_profile(
        profiles, 'straight',
        f'python3 tools/odom_validation.py straight-calibrate '
        f'--port {args.port} --speed 100 --duration 10.0 '
        f'--target-distance 1000')
    turn = require_profile(
        profiles, direction,
        f'python3 tools/odom_validation.py turn-calibrate '
        f'--port {args.port} --direction {direction} '
        f'--speed 100 --steering {"1000" if direction == "left" else "-1000"} '
        f'--duration 4.0')

    sp_s, st_s = int(straight['speed_mm_s']), clamp_steer(
        straight['steering_cdeg'])
    du_s = float(straight['duration_s'])
    sp_t, st_t = int(turn['speed_mm_s']), clamp_steer(turn['steering_cdeg'])
    du_t = float(turn['duration_s'])

    print(f'\n  직진 프로파일 : speed={sp_s} steer={st_s} {du_s:.2f}s '
          f"(목표 {straight.get('target_distance_mm', '?')} mm)")
    print(f'  {direction} 회전   : speed={sp_t} steer={st_t} {du_t:.2f}s')
    print('  ⚠ 아커만 조향이라 날카로운 정사각형이 아니라 모서리가 둥근 '
          '폐곡선이 됩니다. 폐합오차는 그 전제로 읽으십시오.')
    print('  ⚠ RESET_ODOM 은 **시작 시 한 번만** (소프트웨어 원점). 구간 사이에는 '
          '원점을 다시 잡지 않습니다.')

    s = Session(args, f'square-test-{direction}')
    rec = None
    aborted = None
    completed = 0
    try:
        try:
            s.arm()
            prompt_enter('출발점에 뒷축 중앙을 맞추고 바닥에 표시하십시오. '
                         f'한 변 × 4 + {direction} 회전 × 4 를 연속 주행합니다.')
            s.set_origin(not args.allow_invalid_pose)
            rec = Recorder(args.output, f'square-test-{direction}')
            for lap in range(4):
                run_segment(s.link, rec, s.guard, s.origin,
                            speed=sp_s, steering=st_s, duration=du_s,
                            segment_index=lap * 2, segment_type='straight',
                            direction=direction, label=f'{lap + 1}변 직진')
                settle(s.link, rec, s.guard, s.origin, seconds=SETTLE_S,
                       require_ready=args.require_ready,
                       segment_index=lap * 2, direction=direction)
                completed += 1
                run_segment(s.link, rec, s.guard, s.origin,
                            speed=sp_t, steering=st_t, duration=du_t,
                            segment_index=lap * 2 + 1, segment_type='turn',
                            direction=direction, label=f'{lap + 1}번 회전')
                settle(s.link, rec, s.guard, s.origin, seconds=SETTLE_S,
                       require_ready=args.require_ready,
                       segment_index=lap * 2 + 1, direction=direction)
                completed += 1
        except Abort as e:
            aborted = str(e)
            print(f'\n⛔ 중단: {aborted}')
        # 중단이든 완주든 여기까지 왔으면 남은 표본으로 요약을 낸다. Ctrl-C 는
        # 여기서 잡지 않고 밖의 finally 가 포트를 닫게 통과시킨다.
        neutral_tail(s.link)
        _report_square(args, s, rec, summary_base={
            'direction': direction,
            'straight_profile': straight,
            'turn_profile': turn,
            'completed_segments': completed,
            'aborted': aborted,
        })
    finally:
        if rec:
            rec.close()
        s.close()
    return 1 if aborted else 0


def _report_square(args, s, rec, summary_base):
    """사각형 시험 결과 출력·저장. 중단된 경우에도 있는 값으로 낸다."""
    direction = summary_base['direction']
    drive, odom = s.link.snap()
    r = s.origin.rel(odom) if (s.origin and odom) else {}
    # 명세 정의 그대로: sqrt((final_x - start_x)² + (final_y - start_y)²).
    # 회전은 거리를 보존하므로 fwd/lat 로 재도 같은 값이다.
    fx, fy = r.get('relative_x_mm'), r.get('relative_y_mm')
    closure = (s.origin.return_error_mm(odom)
               if (s.origin and odom) else None)
    summary = dict(summary_base)
    summary.update({
        'test_mode': f'square-test-{direction}',
        'start_x_mm': s.origin.x0 if s.origin else None,
        'start_y_mm': s.origin.y0 if s.origin else None,
        'final_x_mm': odom['x_mm'] if odom else None,
        'final_y_mm': odom['y_mm'] if odom else None,
        'relative_x_mm': fx,
        'relative_y_mm': fy,
        'fwd_mm': r.get('fwd_mm'),
        'lat_mm': r.get('lat_mm'),
        'odom_return_error_mm': (round(closure, 1)
                                 if closure is not None else None),
        'final_yaw_deg': r.get('relative_yaw_deg'),
        'final_yaw_mdeg': r.get('relative_yaw_mdeg'),
        'total_distance_mm': r.get('relative_distance_mm'),
        'active_fault_bits': drive['active_fault_bits'] if drive else None,
        'csv_rows': rec.rows if rec else 0,
    })
    completed = summary['completed_segments']
    aborted = summary['aborted']
    print('\n── 결과 ' + '─' * 52)
    print(f"  final_x_mm            : {summary['final_x_mm']} "
          f"(start {summary['start_x_mm']})")
    print(f"  final_y_mm            : {summary['final_y_mm']} "
          f"(start {summary['start_y_mm']})")
    print(f"  relative_x/y_mm       : {fx} / {fy}")
    print(f"  fwd/lat_mm            : {r.get('fwd_mm')} / {r.get('lat_mm')}"
          '   ← 출발 방향 기준 전방/좌횡')
    print(f"  odom_return_error_mm  : {summary['odom_return_error_mm']}")
    print(f"  final_yaw_deg         : {summary['final_yaw_deg']}")
    print(f"  total_distance_mm     : {summary['total_distance_mm']}")
    print(f"  completed_segments    : {completed} / 8")
    print(f"  aborted               : {aborted}")
    fb = summary['active_fault_bits']
    print(f"  active_fault_bits     : "
          f"0x{fb:08X}" if fb is not None else '  active_fault_bits     : -')
    if fb:
        print(f'                          [{P.describe_faults(fb)}]')

    try:
        print('\n  실측값을 입력하십시오 (Enter 로 건너뜀).')
        m_close = ask_float('  실제 시작점–종료점 거리 [mm]: ', allow_blank=True)
        m_yaw = ask_float('  실제 최종 방향 오차 [deg]: ', allow_blank=True)
        if m_close is not None:
            summary['measured_return_error_mm'] = m_close
            if closure is not None:
                summary['return_error_diff_mm'] = round(closure - m_close, 1)
                print(f'  odom 폐합 − 실측 = {closure - m_close:+.1f} mm')
        if m_yaw is not None:
            summary['measured_final_heading_error_deg'] = m_yaw
    except Abort:
        pass

    if rec:
        write_summary(args.output, summary)
        rec.close()
    s.close()
    return 1 if aborted else 0


# ══════════ CLI ══════════

def _add_common(ap, *, suppress: bool):
    """공통 옵션. 서브커맨드 **앞뒤 어디에 써도** 되게 두 곳에 붙인다.

    서브파서 쪽은 `SUPPRESS` 로 둔다 — 기본값을 실어 보내면 앞에서 준 값을
    뒤늦게 덮어써서 `--port` 를 앞에 쓴 사람이 조용히 기본 포트로 붙는다.
    """
    d = (lambda v: argparse.SUPPRESS) if suppress else (lambda v: v)
    ap.add_argument('--port', default=d('/dev/ttyUSB0'))
    ap.add_argument('--baud', type=int, default=d(115200))
    ap.add_argument('--profiles', default=d(DEFAULT_PROFILES),
                    help=f'프로파일 JSON (기본 {DEFAULT_PROFILES})')
    ap.add_argument('--require-ready', action='store_true',
                    default=d(False),
                    help='구간 사이 정지 판정에 drive_state==READY(2) 를 문자 '
                         '그대로 요구한다. 명세 §3.1 에 DRIVING→READY 전이가 '
                         '없어 timeout 이 날 수 있다')
    ap.add_argument('--allow-invalid-pose', action='store_true',
                    default=d(False),
                    help='pose_valid=false 인 0x85 도 원점으로 쓴다 (권장 안 함)')
    ap.add_argument('--steer-trim', type=int, default=d(DEFAULT_STEER_TRIM),
                    help='직진 구간 조향값 [cdeg]. 실측 직진값 500 이 기본. '
                         '0 을 주면 스펙 문구 그대로 0 cdeg 로 보낸다')


def build_parser():
    ap = argparse.ArgumentParser(
        prog='odom_validation.py',
        description='오도메트리 검증 — 고정 시간 프로파일 기반 5개 모드',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    _add_common(ap, suppress=False)
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)
    sub = ap.add_subparsers(dest='mode', required=True)

    p = sub.add_parser('straight', parents=[common],
                       help='지정 속도·시간 직진 후 CSV 저장')
    p.add_argument('--speed', type=int, required=True, help='mm/s')
    p.add_argument('--duration', type=float, required=True, help='초')
    p.add_argument('--output', default='straight.csv')
    p.set_defaults(func=cmd_straight)

    p = sub.add_parser('straight-calibrate', parents=[common],
                       help='실측 거리로 주행시간 역산')
    p.add_argument('--speed', type=int, required=True)
    p.add_argument('--duration', type=float, required=True)
    p.add_argument('--target-distance', type=float, required=True, help='mm')
    p.add_argument('--output', default=None)
    p.set_defaults(func=cmd_straight_calibrate)

    p = sub.add_parser('turn-calibrate', parents=[common],
                       help='실측 회전각으로 90° 시간 역산')
    p.add_argument('--direction', choices=('left', 'right'), required=True)
    p.add_argument('--speed', type=int, required=True)
    p.add_argument('--steering', type=int, required=True,
                   help='절대 조향 명령 [cdeg]. 좌는 +, 우는 −. 트림을 더하지 '
                        '않는다. 시작 후보는 좌 +500 / 우 -500 (현재 주행 로직이 '
                        '쓰는 값). 실차 결과에 따라 700·1000 등으로 올린다. '
                        '이 값을 실제 바퀴각으로 환산하지 않는다 — STM32 의 '
                        '7점 LUT 를 거치므로 대응은 실측으로만 확정된다')
    p.add_argument('--duration', type=float, required=True)
    p.add_argument('--output', default=None)
    p.set_defaults(func=cmd_turn_calibrate)

    p = sub.add_parser('turn-test', parents=[common],
                       help='저장된 프로파일로 회전 odom 검증')
    p.add_argument('--direction', choices=('left', 'right'), required=True)
    p.add_argument('--output', default=None)
    p.set_defaults(func=cmd_turn_test)

    p = sub.add_parser('square-test', parents=[common],
                       help='직선+회전 4회 누적 폐합오차')
    p.add_argument('--direction', choices=('left', 'right'), required=True)
    p.add_argument('--output', default=None)
    p.set_defaults(func=cmd_square_test)
    return ap


def main(argv=None):
    ap = build_parser()
    a = ap.parse_args(argv)
    if a.mode == 'turn-test' and not a.output:
        a.output = f'turn_{a.direction}.csv'
    if a.mode == 'square-test' and not a.output:
        a.output = f'square_{a.direction}.csv'

    # Ctrl-C 를 예외로 바꿔 finally 의 neutral 경로를 반드시 지나게 한다.
    def _sigint(_sig, _frm):
        raise KeyboardInterrupt
    signal.signal(signal.SIGINT, _sigint)

    print('═' * 60)
    print(f'  오도메트리 검증 — {a.mode}')
    print('═' * 60)
    try:
        return a.func(a)
    except KeyboardInterrupt:
        print('\n\n⛔ Ctrl-C — neutral 로 정지합니다.')
        return 130
    except Abort as e:
        print(f'\n⛔ 중단: {e}')
        return 1
    except SystemExit:
        raise
    except Exception as e:                                       # noqa: BLE001
        print(f'\n⛔ 예외: {type(e).__name__}: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
