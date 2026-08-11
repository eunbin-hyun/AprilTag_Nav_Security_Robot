"""초음파 장애물 HOLD·복구 정책 (순수 Python, ROS 무의존).

`0x80 TELEMETRY_DRIVE` 한 프레임을 먹여서 "지금 주행 명령을 보내도 되는가" 를
돌려준다. UART 를 직접 소유하는 도구(`drive_probe` 등)가 그대로 쓸 수 있다.

정책은 `robot_navigation/route_logic.py` 의 장애물 복구와 **같은 규칙**이다:

  1. bit16=1 -> HOLD. clear 타이머 정지. neutral 을 20 Hz 로 계속 보낸다
  2. bit16=0 -> `clear_hold_s` 동안 연속 유지되어야 한다 (debounce)
  3. debounce 통과 후 STM32 가 `READY(2)` 가 되기를 `ready_timeout_s` 까지 기다림
  4. 두 조건을 **모두** 만족하면 재개. 어느 쪽이 먼저 와도 다른 쪽을 기다린다
  5. bit16 을 뺀 차단 fault·통신단절은 복구 대기에 **숨기지 않고** 즉시 중단
  6. 복구 경로에서 `CMD_STOP` 을 보내지 않는다 — `CMD_STOP` 은 STM32 의
     `rearm_required` 를 다시 세워 복구를 스스로 막는다
  7. 예외 하나: **이미 HOLD 중** 이고 차단 fault 가 `SENSOR_STALE`(bit15)
     **단독** 이면 `sensor_stale_grace_s` 동안만 중단하지 않는다. 장애물 경계에서
     초음파 측정이 순간 끊기면 bit16 이 내려가고 bit15 가 대신 뜬다 (현장 로그
     0x8100 = bit8+bit15). 그 한 프레임으로 중단하면 STM32 가 스스로 READY 로
     돌아와도 재개하지 못한다. 상한을 넘기면 기존대로 중단한다

⚠ `route_logic` 과 이 모듈은 같은 규칙의 **두 구현**이다. 한쪽을 고치면 다른
  쪽도 고쳐야 한다. 합치지 않은 이유: `route_logic` 은 stm32_bridge 에 의존하지
  않는다는 제약이 있어서 protocol 상수를 못 쓴다 (변환은 `route_runner` 담당).
  규칙을 바꿀 때는 `test_obstacle_gate.py` 와
  `robot_navigation/test/test_obstacle_recovery.py` 를 같이 돌릴 것.

왜 필요한가: STM32 는 `SAFE_STOP` 에서 **비영 명령을 거부하고 watchdog 도
갱신하지 않는다.** 장애물이 뜬 뒤에도 `enable=1` 을 계속 보내면 아무것도
적용되지 않고, 도구는 "명령은 보냈는데 왜 안 움직이나" 상태로 조용히 망가진다.
neutral 로 바꿔 보내야 rearm 이 진행된다.
"""
from . import protocol as P

# 하위상태 (진단·시험용). route_logic 의 HOLD_SUB_* 와 같은 의미다.
RUN = 'RUN'
HOLD_PRESENT = 'OBSTACLE_PRESENT'
HOLD_DEBOUNCE = 'OBSTACLE_CLEAR_DEBOUNCE'
HOLD_REARM_WAIT = 'STM32_REARM_WAIT'


class ObstacleGate:
    """장애물 HOLD·복구 판정기.

    `update()` 를 매 tick 호출하고 반환된 `send_drive` 로 무엇을 보낼지 정한다.
    """

    def __init__(self, clear_hold_s: float = 0.5,
                 ready_timeout_s: float = 2.0,
                 telemetry_timeout_s: float = 0.5,
                 max_hold_s: float = 30.0,
                 sensor_stale_grace_s: float = 0.25):
        self.clear_hold_s = clear_hold_s
        self.ready_timeout_s = ready_timeout_s
        self.telemetry_timeout_s = telemetry_timeout_s
        # 장애물이 영원히 안 치워지는 경우의 상한. 없으면 도구가 무한정 멈춘다.
        self.max_hold_s = max_hold_s
        # HOLD 중 bit15 단독을 유예하는 상한 (§7). route_logic 의
        # `sensor_stale_grace_s` 와 **같은 값** 이어야 한다.
        self.sensor_stale_grace_s = sensor_stale_grace_s

        self.substate = RUN
        self.abort_reason = None     # 설정되면 호출자가 구간을 중단해야 한다
        self.hold_total_s = 0.0      # HOLD 로 보낸 누적 시간
        self.hold_count = 0          # HOLD 진입 횟수
        self._clear_since = None
        self._ready_wait_since = None
        self._hold_since = None
        self._last_now = None
        self._sensor_stale_since = None   # bit15 단독 유예 시작 시각

    # ── 조회 ───────────────────────────────────────────────
    @property
    def holding(self) -> bool:
        return self.substate != RUN

    @property
    def aborted(self) -> bool:
        return self.abort_reason is not None

    def hold_elapsed_s(self, now: float) -> float:
        """지금까지의 HOLD 누적 (진행 중인 HOLD 포함)."""
        if self._hold_since is None:
            return self.hold_total_s
        return self.hold_total_s + max(0.0, now - self._hold_since)

    # ── 갱신 ───────────────────────────────────────────────
    def update(self, now: float, tel: dict, tel_age_s: float) -> bool:
        """한 tick 판정. 주행 명령을 보내도 되면 True.

        tel: `unpack_telemetry()` 결과. None 이면 아직 수신 없음.
        tel_age_s: 마지막 0x80 수신 후 경과 시간.

        반환 False 면 **neutral 을 보내야 한다** (송신을 멈추면 안 된다 —
        멈추면 STM32 watchdog 이 COMM_TIMEOUT 을 올린다).
        `aborted` 가 참이면 구간을 끝내야 한다.
        """
        self._last_now = now
        if self.aborted:
            return False

        # ── 통신단절이 최우선. 복구 대기가 이것을 숨기면 안 된다.
        if tel is None:
            self._abort('telemetry 미수신 — 포트·전원·배선 확인')
            return False
        if tel_age_s > self.telemetry_timeout_s:
            self._abort(f'telemetry 단절 {tel_age_s * 1000:.0f} ms')
            return False

        faults = tel['active_fault_bits']
        obstacle = bool(faults & P.FAULT_OBSTACLE_NEAR)
        # bit16 만 분리한다. 나머지 차단 fault 는 그대로 중단 사유다.
        other = P.arm_blocking_faults(faults & ~P.FAULT_OBSTACLE_NEAR)
        if other:
            if self._sensor_stale_grace(now, other):
                return False              # 유예 중 — neutral 만 보낸다
            self._abort(f'차단 fault 활성 0x{other:08X} '
                        f'[{P.describe_faults(other)}]')
            return False
        # 차단 fault 가 없어진 tick 에서 유예 타이머를 버린다. 안 버리면 다음
        # flicker 가 지난 번 시작 시각과 비교돼 첫 프레임에 상한을 넘긴다.
        self._sensor_stale_since = None

        state = tel['drive_state']

        if obstacle:
            if not self.holding:
                self.hold_count += 1
                self._hold_since = now
            self.substate = HOLD_PRESENT
            # 재등장 -> 두 타이머 모두 리셋
            self._clear_since = None
            self._ready_wait_since = None
            if self.hold_elapsed_s(now) > self.max_hold_s:
                self._abort(
                    f'장애물이 {self.max_hold_s:.0f}s 넘게 치워지지 않았다')
            return False

        if not self.holding:
            return True                       # 정상 주행

        # ── 복구: debounce -> READY 대기 ─────────────────
        if self._clear_since is None:
            self._clear_since = now
        if now - self._clear_since < self.clear_hold_s:
            self.substate = HOLD_DEBOUNCE     # READY 여도 아직 기다린다
            return False

        if state != P.STATE_READY:
            self.substate = HOLD_REARM_WAIT
            if self._ready_wait_since is None:
                self._ready_wait_since = now
            waited = now - self._ready_wait_since
            if waited > self.ready_timeout_s:
                self._abort(
                    f'장애물 해제 후 {waited:.1f}s 안에 STM32 가 READY 로 '
                    f'오지 않았다 (state={P.STATE_NAMES.get(state, "?")}, '
                    f'fault=0x{faults:08X})')
            return False

        # 두 조건 충족 -> 재개
        self._resume(now)
        return True

    # ── 내부 ───────────────────────────────────────────────
    def _sensor_stale_grace(self, now: float, other: int) -> bool:
        """bit15 단독을 짧게 유예한다 (§7). 유예 중이면 True.

        조건을 좁게 잡는 것이 요점이다 — 넓히면 실제 센서 고장을 도구가 조용히
        무시한다. **이미 HOLD 중** 이고 차단 fault 가 **bit15 하나뿐** 일 때만
        참이다. 통신단절은 `update()` 가 이 함수보다 먼저 보므로 여기 오지 않는다.
        """
        if not self.holding or other != P.FAULT_SENSOR_STALE:
            self._sensor_stale_since = None
            return False
        if self._sensor_stale_since is None:
            self._sensor_stale_since = now
        if now - self._sensor_stale_since > self.sensor_stale_grace_s:
            return False                  # 상한 초과 — 호출자가 중단한다
        # bit15 가 떠 있는 동안은 "장애물이 없다" 고 볼 수 없다. 두 타이머를
        # 버려서 bit15 가 사라진 시점부터 debounce 를 새로 센다.
        self.substate = HOLD_PRESENT
        self._clear_since = None
        self._ready_wait_since = None
        return True

    def _resume(self, now: float):
        if self._hold_since is not None:
            self.hold_total_s += max(0.0, now - self._hold_since)
            self._hold_since = None
        self.substate = RUN
        self._clear_since = None
        self._ready_wait_since = None
        self._sensor_stale_since = None

    def _abort(self, why: str):
        self.abort_reason = why
        if self._hold_since is not None and self._last_now is not None:
            self.hold_total_s += max(0.0, self._last_now - self._hold_since)
            self._hold_since = None
