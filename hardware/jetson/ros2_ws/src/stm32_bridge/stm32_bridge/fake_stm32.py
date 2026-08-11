"""Fake STM32: socat PTY 반대편에서 UART V2 프로토콜을 실제로 구현한 가짜 장치.

명세 §8 상태 머신 + §10 watchdog(300 ms) + §13.1 부팅 절차의 최소 구현.
랜덤 값이 아니라 물리적으로 그럴싸한 응답(1차 지연 속도, 엔코더 적분)을 만든다.

실행:
    socat -d -d pty,raw,echo=0,link=/tmp/ttyJETSON \\
                pty,raw,echo=0,link=/tmp/ttySTM32 &
    python3 -m stm32_bridge.fake_stm32           # 또는 ros2 run stm32_bridge fake_stm32
"""
import argparse
import math
import sys
import time

import serial

from . import protocol as P
from .parser import FrameParser

TELEMETRY_HZ = 20.0
# 명세 §3.8: TELEMETRY_RANGE 는 10 Hz. TELEMETRY_DRIVE 와 주기가 다르며
# SEQ 는 같은 송신 카운터를 공유한다 (실장비 실측으로 확인: DRIVE/RANGE 가
# 하나의 카운터를 번갈아 쓰며 1씩 증가).
RANGE_HZ = 10.0
WATCHDOG_S = 0.300            # 명세 §10: STM32 UART drive watchdog
SPEED_TAU = 0.25              # 속도 1차 지연 시상수 [s] (PID 응답 흉내)
# 실측값 (vehicle_config.h, 2026-07-31): 823 count/rev / 201.06 mm 둘레.
# 임의값을 쓰면 브리지 쪽 tick/m 환산 오류가 시험에서 드러나지 않는다.
FAKE_TICKS_PER_M = 4094       # 823 / 0.20106
FAKE_WHEELBASE_M = 0.135      # STM32 VEHICLE_WHEELBASE_M 과 동일
# IMU 융합 최소 속도. vehicle_config.h VEHICLE_IMU_FUSION_MIN_SPEED_MPS 와 동일.
FAKE_IMU_MIN_FUSION_SPEED_MPS = 0.02
# 명세 §8.2 BOOT -> SELF_TEST -> SAFE_STOP. 실물처럼 수십 ms 만에 지나간다.
# 20 Hz telemetry(50 ms 주기) 로는 이 두 상태를 대개 관측하지 못한다는 사실
# 자체가 시험 대상이다 (Jetson 은 mcu_time_ms 역행으로 재부팅을 잡아야 한다).
BOOT_MS = 10
SELF_TEST_MS = 10
# V2.3 §2.4 SEQ 세션 리셋. 유효 frame 이 이 시간 이상 없으면 last_rx_seq 를
# 무효화한다. 명세 §13.1-5 의 quiet 구간(350 ms)과 같은 값을 쓴다.
SESSION_IDLE_S = 0.350


class FakeStm32:
    def __init__(self, port: str, verbose: bool = True,
                 boot_faults: int = 0, sensor_init_ms: int = 2000,
                 imu_on: bool = True, imu_accuracy: int = 3,
                 obstacle_at: float = None, obstacle_hold_s: float = 2.0,
                 obstacle_ready_delay_ms: int = 0):
        # write_timeout=0 (논블로킹 쓰기)가 중요하다. PTY 는 백프레셔가 있어서
        # 아무도 읽지 않으면 버퍼가 차고 write() 가 블록되는데, 실제 UART 는
        # 그런 게 없다 — 수신자가 없으면 바이트가 그냥 사라진다. 블로킹을
        # 그대로 두면 fake 의 단일 루프가 굶어서 상태 전이·명령 처리가 멈추고,
        # 풀린 뒤 캐치업 폭주가 난다 (2026-07-31 실측: 410 Hz).
        self.ser = serial.Serial(port, 115200, timeout=0, write_timeout=0)
        self.tx_dropped = 0
        # BNO085. --no-imu 로 끄면 0x83 을 안 보내고 IMU_FUSED 도 안 세운다 —
        # 센서 없는 상태에서 브리지가 degraded 경로를 타는지 확인할 수 있다.
        self.imu_on = imu_on
        self.imu_accuracy = max(0, min(3, imu_accuracy))
        self.parser = FrameParser()
        self.tx_seq = P.SeqCounter()
        self.verbose = verbose

        self.t0 = time.monotonic()
        self.state = P.STATE_BOOT              # 명세 §8.2, §13.1-1 출력 disable
        # 기본 0. 실장비는 부팅부터 0xA300 이 상주하지만, 그것을 기본값으로
        # 두면 기존 LS/PC 시험이 전부 ARM 불가로 바뀐다. --boot-faults 로
        # 명시적으로 켜서 차단 경로를 시험한다.
        self.active_faults = boot_faults
        self.last_drive_seq = 0
        self.last_rx_seq = None               # SEQ duplicate/old 판정용
        self.last_rx_t = None                 # V2.3 §2.4 세션 경계 판정용
        self.last_valid_drive = None          # watchdog 기준 시각
        self._rearm_denied = 0                # 거부 로그 중복 방지
        # 초음파 fault 를 켠 채로 시작했으면 이 시각에 자동 해제한다
        self.sensor_init_ms = (
            sensor_init_ms
            if boot_faults & (P.FAULT_RANGE_LOST | P.FAULT_SENSOR_STALE)
            else None)

        # "물리" 상태
        self.target_speed = 0                 # mm/s
        self.measured_speed = 0.0             # mm/s
        self.steering_cmd = 0                 # cdeg
        self.encoder = 0                      # ticks (float 누적 후 int 보고)
        self._enc_f = 0.0

        # 오도메트리 (명세 §9.2 좌표계: +x 전방, +y 좌, +yaw 좌회전)
        # 출발점 기준이며 리셋 시 0 으로 초기화된다. UART pose reset 은 없다.
        self.pose_x = 0.0                     # m
        self.pose_y = 0.0
        self.pose_yaw = 0.0                   # rad
        self.pose_dist = 0.0                  # m, signed 누적
        self.yaw_rate = 0.0                   # rad/s
        self.curvature = 0.0                  # 1/m

        # 초음파 장애물 시나리오 (obstacle_step 참고)
        self.obstacle_at = obstacle_at
        self.obstacle_hold_s = obstacle_hold_s
        self.obstacle_ready_delay_ms = obstacle_ready_delay_ms
        self._obstacle_fired = False
        self._obstacle_cleared = False
        self._obstacle_clear_t = None

    # ── 유틸 ──────────────────────────────────────────────

    def mcu_ms(self) -> int:
        return int((time.monotonic() - self.t0) * 1000) & 0xFFFFFFFF

    def log(self, msg: str):
        if self.verbose:
            print(f'[fake_stm32 {self.mcu_ms():>8}ms '
                  f'{P.STATE_NAMES[self.state]:>9}] {msg}', flush=True)

    def send(self, frame: bytes):
        """논블로킹 송신. 수신자가 없으면 버리고 센다 (실제 UART 와 동일)."""
        try:
            self.ser.write(frame)
        except serial.SerialTimeoutException:
            self.tx_dropped += 1
            if self.tx_dropped in (1, 100, 1000) or self.tx_dropped % 5000 == 0:
                self.log(f'TX 버림 누적 {self.tx_dropped} '
                         f'(수신자가 읽지 않음 — 정상 동작)')

    def set_state(self, new: int):
        if new != self.state:
            self.log(f'-> {P.STATE_NAMES[new]}')
            self.state = new

    # ── 수신 처리 ─────────────────────────────────────────

    def on_frame(self, msg_id: int, seq: int, payload: bytes):
        # V2.3 §2.4: 세션 경계 판정. SESSION_IDLE_S 이상 유효 frame 이 없었으면
        # 송신 측이 재시작한 것으로 보고 last_rx_seq 를 무효화한다. 이렇게
        # 해야 송신 카운터가 0 으로 되돌아가도 'old' 로 거부하지 않는다.
        now = time.monotonic()
        if (self.last_rx_seq is not None and self.last_rx_t is not None
                and now - self.last_rx_t >= SESSION_IDLE_S):
            self.log(f'SEQ 세션 리셋 (유효 frame {now - self.last_rx_t:.3f}s '
                     f'없음, last_rx_seq={self.last_rx_seq} 무효화)')
            self.last_rx_seq = None
        self.last_rx_t = now

        # SEQ 판정 (명세 §4.4). 첫 frame은 무조건 new.
        if self.last_rx_seq is not None:
            cls = P.seq_classify(seq, self.last_rx_seq)
            if cls != 'new':
                if msg_id in (P.CMD_STOP, P.CMD_RESET_FAULT):
                    # 단발 명령 중복 -> 이전 결과 재통지 성격으로 DUPLICATE
                    self.send(P.pack_command_result(
                        self.tx_seq.next(), request_msg_id=msg_id,
                        request_seq=seq, result_code=P.RESULT_DUPLICATE,
                        drive_state=self.state))
                return                        # 중복/과거는 재실행·watchdog 갱신 금지
        self.last_rx_seq = seq

        if msg_id == P.CMD_DRIVE:
            self.on_cmd_drive(seq, payload)
        elif msg_id == P.CMD_STOP:
            self.on_cmd_stop(seq, payload)
        elif msg_id == P.CMD_RESET_FAULT:
            self.on_cmd_reset(seq, payload)
        elif msg_id == P.DIAG_ECHO_REQUEST:
            self.send(P.pack_frame(P.DIAG_ECHO_RESPONSE, self.tx_seq.next(),
                                   bytes([seq]) + payload))
        else:
            self.send(P.pack_command_result(
                self.tx_seq.next(), request_msg_id=msg_id, request_seq=seq,
                result_code=P.RESULT_UNSUPPORTED, drive_state=self.state))

    def on_cmd_drive(self, seq: int, payload: bytes):
        cmd = P.unpack_cmd_drive(payload)

        # 검증 (명세 §6.1): enable=0인데 비영 목표 -> INVALID_VALUE
        if cmd['enable'] not in (0, 1) or (
                cmd['enable'] == 0 and
                (cmd['speed_mm_s'] != 0 or cmd['steering_cdeg'] != 0)):
            self.log(f'INVALID_VALUE drive seq={seq}')
            return                            # watchdog 갱신 안 함

        is_neutral = (cmd['enable'] == 0)

        # 상태별 처리 (명세 §6.1 표)
        if self.state in (P.STATE_BOOT, P.STATE_SELF_TEST):
            return                            # 출력 disable, 명령 미적용
        if self.state in (P.STATE_FAULT, P.STATE_ESTOP):
            if not is_neutral:
                return                        # 비영 명령 거부
            # neutral은 출력 0 유지 용도로만
        elif self.state == P.STATE_SAFE_STOP:
            if is_neutral:
                # rearm: COMM_TIMEOUT 해소 + neutral -> READY.
                # 단 neutral 로 해소되지 않는 fault 가 남아 있으면 READY 로
                # 가지 않는다. 실장비가 0xA300(bit 9, 15)에서 SAFE_STOP 에
                # 머무는 동작을 그대로 흉내낸다.
                self.active_faults &= ~P.FAULT_COMM_TIMEOUT
                # 장애물 해제 직후 rearm 을 일부러 늦추는 시나리오 B.
                # bit16 은 이미 0 이지만 state 는 아직 SAFE_STOP 이다.
                if self._obstacle_rearm_blocked():
                    self.last_drive_seq = seq
                    self.last_valid_drive = time.monotonic()
                    return
                blocking = P.arm_blocking_faults(self.active_faults)
                if blocking:
                    # 20 Hz 로 거부되므로 변화가 있을 때만 남긴다
                    if blocking != self._rearm_denied:
                        self._rearm_denied = blocking
                        self.log(f'rearm 거부: 차단 fault '
                                 f'[{P.describe_faults(blocking)}]')
                    # ★ 차단 사유가 **장애물뿐**이면 neutral 은 그래도
                    #   "수락된 유효 프레임" 이다 (문서 STM32 계약: 장애물 STOP
                    #   은 rearm_required 를 세우지 않고, 수락된 neutral 이
                    #   watchdog 과 last_drive_seq 를 갱신한다).
                    #   여기서 동결하면 Jetson 이 HOLD 중 자기 명령이 도달하는지
                    #   확인할 수단을 잃는다. 보정 계열(bit9/15) 차단은 기존대로
                    #   동결한다 — 그건 실장비 0xA300 동작이다.
                    if blocking == P.FAULT_OBSTACLE_NEAR:
                        self.last_drive_seq = seq
                        self.last_valid_drive = time.monotonic()
                    return                    # 상태는 SAFE_STOP 유지
                self._rearm_denied = 0
                self.set_state(P.STATE_READY)
            else:
                return                        # NOT_ARMED: 비영 거부, watchdog 미갱신
        elif self.state == P.STATE_READY:
            if not is_neutral:
                self.set_state(P.STATE_DRIVING)
        # DRIVING: 새 목표 그대로 적용 (같은 값도 새 SEQ면 heartbeat)

        self.target_speed = cmd['speed_mm_s'] if cmd['enable'] else 0
        self.steering_cmd = cmd['steering_cdeg'] if cmd['enable'] else 0
        self.last_drive_seq = seq
        self.last_valid_drive = time.monotonic()   # watchdog 갱신 (§4.6)

    def on_cmd_stop(self, seq: int, payload: bytes):
        reason = payload[0]
        self.log(f'CMD_STOP reason={reason}')
        self.target_speed = 0
        self.steering_cmd = 0
        if self.state == P.STATE_DRIVING:
            self.set_state(P.STATE_SAFE_STOP)
        self.send(P.pack_command_result(
            self.tx_seq.next(), request_msg_id=P.CMD_STOP, request_seq=seq,
            result_code=P.RESULT_ACCEPTED, drive_state=self.state))

    def on_cmd_reset(self, seq: int, payload: bytes):
        """CMD_RESET_FAULT: 요청 마스크 중 reset 가능 bit 만 지운다.

        실장비 의미론을 흉내낸다 — CLR_AUTO(장애물·센서)·CLR_CALIB(조향)·
        rearm 계열은 reset 으로 지워지지 않고, 통신·명령 계열만 지워진다.
        자동 재주행은 없다 (별도 출발 명령 필요).
        """
        import struct as _struct
        (mask,) = _struct.unpack('<I', payload)
        cleared = mask & P.RESETTABLE_FAULT_MASK & self.active_faults
        refused = mask & self.active_faults & ~P.RESETTABLE_FAULT_MASK
        self.active_faults &= ~cleared
        self.log(f'CMD_RESET_FAULT mask=0x{mask:08X} '
                 f'해제=0x{cleared:08X} 거부=0x{refused:08X} '
                 f'잔여=0x{self.active_faults:08X}')
        self.send(P.pack_command_result(
            self.tx_seq.next(), request_msg_id=P.CMD_RESET_FAULT,
            request_seq=seq, result_code=P.RESULT_ACCEPTED,
            drive_state=self.state))

    # ── 주기 처리 ─────────────────────────────────────────

    def boot_step(self):
        """명세 §8.2: BOOT -> SELF_TEST -> SAFE_STOP 자동 진행."""
        if self.state not in (P.STATE_BOOT, P.STATE_SELF_TEST):
            return
        el = self.mcu_ms()
        if self.state == P.STATE_BOOT and el >= BOOT_MS:
            self.set_state(P.STATE_SELF_TEST)
        elif (self.state == P.STATE_SELF_TEST
              and el >= BOOT_MS + SELF_TEST_MS):
            self.set_state(P.STATE_SAFE_STOP)

    def sensor_init_step(self):
        """초음파 SENSOR_INIT 종료를 흉내낸다 (STM32 파트 확인 사항).

        bit 8 RANGE_LOST 와 bit 15 SENSOR_STALE 은 부팅 직후 SENSOR_INIT
        때문에 켜지는 것이 정상이고, 센서가 정상 측정되면 자동으로 내려간다
        (해제 방식 CLR_AUTO). 이걸 재현해야 BLOCKED -> ARMING 복귀 경로를
        실장비 없이 시험할 수 있다.

        bit 9 STEERING_INVALID 는 사람이 보정해야 하므로 내리지 않는다.
        """
        if self.sensor_init_ms is None:
            return
        if self.mcu_ms() < self.sensor_init_ms:
            return
        self.sensor_init_ms = None
        auto = P.FAULT_RANGE_LOST | P.FAULT_SENSOR_STALE
        if self.active_faults & auto:
            self.active_faults &= ~auto
            self.log(f'SENSOR_INIT 완료 -> 초음파 fault 해제, '
                     f'남은 fault 0x{self.active_faults:08X} '
                     f'[{P.describe_faults(self.active_faults)}]')

    def obstacle_step(self):
        """초음파 장애물 시나리오 재현 (bit16 1 -> 0, SAFE_STOP -> READY).

        실장비를 손으로 가리지 않고 HOLD·자동재개 경로를 시험하기 위한 것이다.
        재현하는 순서:

            DRIVING
            -> obstacle_at 초에 bit16=1 + SAFE_STOP
            -> Jetson 이 neutral 을 20 Hz 로 계속 보냄
            -> obstacle_at + obstacle_hold_s 에 bit16=0
            -> (ready_delay_ms 동안 rearm 을 거부해 SAFE_STOP + bit16=0 유지)
            -> neutral 수용 -> READY
            -> Jetson 이 새 CMD_DRIVE(enable=1) 를 보냄 -> DRIVING

        `ready_delay_ms` 가 시나리오 B(지연/rearm 상황)를 **확정적으로** 만든다.
        0 이면 시나리오 A 다 — bit16 이 내려간 뒤 첫 neutral 에 바로 READY 가
        되므로 `SAFE_STOP + bit16=0` 이 한 프레임도 안 보일 수 있다.
        rearm 을 막는 동안에도 bit16 은 이미 0 이라는 점이 핵심이다: Jetson 이
        "bit16=0 인데 아직 SAFE_STOP" 을 정상 복구 구간으로 다뤄야 한다.
        """
        if self.obstacle_at is None:
            return
        el = self.mcu_ms() / 1000.0
        if not self._obstacle_fired and el >= self.obstacle_at:
            self._obstacle_fired = True
            self.active_faults |= P.FAULT_OBSTACLE_NEAR
            self.target_speed = 0
            self.steering_cmd = 0
            if self.state == P.STATE_DRIVING:
                self.set_state(P.STATE_SAFE_STOP)
            self.log('장애물 감지 (bit16=1) -> SAFE_STOP')
            self.send(P.pack_fault_event(
                self.tx_seq.next(), mcu_time_ms=self.mcu_ms(),
                active_fault_bits=self.active_faults,
                latched_fault_bits=0,
                fault_action=2,               # SAFE_STOP (§9.2)
                drive_state=self.state))
        elif (self._obstacle_fired and not self._obstacle_cleared
                and el >= self.obstacle_at + self.obstacle_hold_s):
            self._obstacle_cleared = True
            self.active_faults &= ~P.FAULT_OBSTACLE_NEAR
            self._rearm_denied = 0
            self._obstacle_clear_t = time.monotonic()
            self.log(f'장애물 해제 (bit16=0), rearm 지연 '
                     f'{self.obstacle_ready_delay_ms} ms')

    def _obstacle_rearm_blocked(self) -> bool:
        """장애물 해제 직후 rearm 을 일부러 늦춘다 (시나리오 B 강제)."""
        if self._obstacle_clear_t is None:
            return False
        if self.obstacle_ready_delay_ms <= 0:
            return False
        held = (time.monotonic() - self._obstacle_clear_t) * 1000.0
        if held >= self.obstacle_ready_delay_ms:
            self._obstacle_clear_t = None
            return False
        return True

    def watchdog(self):
        """300 ms 동안 유효 CMD_DRIVE 없으면 COMM_TIMEOUT -> SAFE_STOP (§13.2)."""
        if self.state != P.STATE_DRIVING or self.last_valid_drive is None:
            return
        if time.monotonic() - self.last_valid_drive > WATCHDOG_S:
            self.log('watchdog expired -> COMM_TIMEOUT')
            self.target_speed = 0
            self.steering_cmd = 0
            self.active_faults |= P.FAULT_COMM_TIMEOUT
            self.set_state(P.STATE_SAFE_STOP)
            self.send(P.pack_fault_event(
                self.tx_seq.next(), mcu_time_ms=self.mcu_ms(),
                active_fault_bits=self.active_faults,
                latched_fault_bits=self.active_faults,
                fault_action=2,               # SAFE_STOP (§9.2)
                drive_state=self.state))

    def physics(self, dt: float):
        """1차 지연 속도 응답 + 엔코더·자전거모델 pose 적분.

        pose 를 적분해서 돌려주는 것이 중요하다. 이게 없으면 회전 종료 판정
        (Δyaw >= 90°)을 실차 없이 시험할 수 없다. 실장비도 조향 명령값으로
        같은 자전거모델을 적분하므로(steering_source=COMMAND_ESTIMATE) 계산
        방식이 동일하다.
        """
        alpha = min(1.0, dt / SPEED_TAU)
        self.measured_speed += alpha * (self.target_speed
                                        - self.measured_speed)
        v = self.measured_speed / 1000.0                      # m/s
        self._enc_f += v * FAKE_TICKS_PER_M * dt
        self.encoder = int(self._enc_f)

        # 자전거 모델. 조향 + 가 좌회전이고 +yaw 도 좌회전이라 부호가 같다.
        delta = math.radians(self.steering_cmd / 100.0)
        self.curvature = math.tan(delta) / FAKE_WHEELBASE_M
        self.yaw_rate = v * self.curvature
        self.pose_yaw += self.yaw_rate * dt
        # ±pi wrap. 실장비도 -180~+180 부근에서 wrap 한다 (명세 §9.2).
        if self.pose_yaw > math.pi:
            self.pose_yaw -= 2.0 * math.pi
        elif self.pose_yaw < -math.pi:
            self.pose_yaw += 2.0 * math.pi
        self.pose_x += v * math.cos(self.pose_yaw) * dt
        self.pose_y += v * math.sin(self.pose_yaw) * dt
        self.pose_dist += v * dt                              # signed

    def send_telemetry(self):
        # DRIVING 이 아니면 모터 출력이 차단된다 -> duty 0. measured_speed 는
        # 관성으로 계속 감쇠하는 게 물리적으로 맞지만 duty 는 즉시 0 이다.
        # 이걸 구분하지 않으면 watchdog 정지 후에도 duty 가 남아서, 실장비라면
        # 결함인 상태를 fake 가 정상으로 보이게 만든다 (시나리오 7이 잡아냄).
        duty = 0
        if self.state == P.STATE_DRIVING:
            duty = int(max(-1000, min(1000, self.measured_speed)))
        self.send(P.pack_telemetry(
            self.tx_seq.next(),
            mcu_time_ms=self.mcu_ms(),
            target_speed_mm_s=int(self.target_speed),
            measured_speed_mm_s=int(self.measured_speed),
            motor_duty_permille=duty,
            steering_cmd_cdeg=self.steering_cmd,
            steering_feedback_cdeg=0,          # 피드백 센서 없음 -> 0 (명세 §7.1)
            encoder_count=self.encoder,
            yaw_cdeg=0,
            drive_state=self.state,
            last_drive_seq=self.last_drive_seq,
            active_fault_bits=self.active_faults))

    def send_odometry(self):
        """TELEMETRY_ODOMETRY (명세 §9.2). 20 Hz, TELEMETRY_DRIVE 와 같은 주기.

        status_flags 와 steering_source 를 실장비와 똑같이 보낸다
        (2026-07-31 실측: 0x000F, COMMAND_ESTIMATE).

        IMU_FUSED 조건은 실장비 규칙을 그대로 흉내 낸다 (V3 §3, BNO085 통합):
        gyro accuracy 1 이상 + gyro 신선 + 차량 속도 0.02 m/s 이상.
        --no-imu 로 띄우면 IMU 자체가 없으므로 항상 0 이고, 브리지는
        degraded 경로를 타게 된다.
        """
        st = (P.ODOM_VALID | P.ODOM_ENCODER_CALIBRATED
              | P.ODOM_GEOMETRY_CALIBRATED | P.ODOM_STEERING_ESTIMATED)
        if self.imu_on and self._imu_fusable():
            st |= P.ODOM_IMU_FUSED
        self.send(P.pack_telemetry_odometry(
            self.tx_seq.next(),
            mcu_time_ms=self.mcu_ms(),
            x_mm=int(round(self.pose_x * 1000.0)),
            y_mm=int(round(self.pose_y * 1000.0)),
            yaw_mdeg=int(round(math.degrees(self.pose_yaw) * 1000.0)),
            distance_mm=int(round(self.pose_dist * 1000.0)),
            linear_speed_mm_s=int(self.measured_speed),
            yaw_rate_mdeg_s=int(round(math.degrees(self.yaw_rate) * 1000.0)),
            steering_cdeg=self.steering_cmd,
            curvature_micro_per_m=int(round(self.curvature * 1e6)),
            status_flags=st,
            steering_source=P.ODOM_STEERING_COMMAND_ESTIMATE,
            last_drive_seq=self.last_drive_seq))

    def _imu_fusable(self) -> bool:
        """STM32 가 gyro 를 융합에 쓰는 조건 (vehicle_config.h 값 그대로)."""
        return (self.imu_accuracy >= P.IMU_MIN_FUSION_ACCURACY
                and abs(self.measured_speed) / 1000.0
                >= FAKE_IMU_MIN_FUSION_SPEED_MPS)

    def send_imu(self):
        """TELEMETRY_IMU (0x83, 38 bytes, 20 Hz).

        game rotation vector 를 흉내 낸다 — z축 회전만 있는 쿼터니언이다.
        절대 북쪽 방위가 아니므로 heading 으로 쓰면 안 된다.
        """
        if not self.imu_on:
            return
        half = self.pose_yaw / 2.0
        q_k = int(round(math.sin(half) * P.IMU_Q14_SCALE))
        q_r = int(round(math.cos(half) * P.IMU_Q14_SCALE))
        flags = (P.IMU_CONNECTED | P.IMU_GYRO_VALID
                 | P.IMU_LINEAR_ACCEL_VALID | P.IMU_QUATERNION_VALID)
        self.send(P.pack_telemetry_imu(
            self.tx_seq.next(),
            mcu_time_ms=self.mcu_ms(),
            quat_i=0, quat_j=0, quat_k=q_k, quat_real=q_r,
            gyro_x_mdeg_s=0, gyro_y_mdeg_s=0,
            gyro_z_mdeg_s=int(round(math.degrees(self.yaw_rate) * 1000.0)),
            accel_x_mm_s2=0, accel_y_mm_s2=0, accel_z_mm_s2=0,
            yaw_mdeg=int(round(math.degrees(self.pose_yaw) * 1000.0)),
            gyro_accuracy=self.imu_accuracy,
            accel_accuracy=self.imu_accuracy,
            quat_accuracy=self.imu_accuracy,
            status_flags=flags))

    def send_range(self):
        """초음파 telemetry (명세 §3.8).

        실장비를 그대로 흉내낸다: 초음파 미장착이므로 4채널 전부
        0xFFFF + valid_mask=0. 값을 꾸며내면 bridge 가 있지도 않은 센서를
        믿게 되므로 일부러 무효로 보낸다. 센서가 붙으면 이 함수를 고친다.
        """
        self.send(P.pack_telemetry_range(
            self.tx_seq.next(),
            mcu_time_ms=self.mcu_ms(),
            front_left=P.RANGE_INVALID_MM,
            front_right=P.RANGE_INVALID_MM,
            rear_left=P.RANGE_INVALID_MM,
            rear_right=P.RANGE_INVALID_MM,
            valid_mask=0x00))

    # ── 메인 루프 ─────────────────────────────────────────

    def run(self):
        self.log(f'listening on {self.ser.port}')
        period = 1.0 / TELEMETRY_HZ
        range_period = 1.0 / RANGE_HZ
        next_tx = time.monotonic()
        next_range = time.monotonic()
        last = time.monotonic()
        while True:
            data = self.ser.read(256)
            if data:
                for f in self.parser.feed(data):
                    self.on_frame(*f)
            self.parser.check_timeout()

            now = time.monotonic()
            self.boot_step()
            self.sensor_init_step()
            self.obstacle_step()
            self.physics(now - last)
            last = now
            self.watchdog()

            if now >= next_tx:
                self.send_telemetry()
                # 실장비는 DRIVE 와 ODOMETRY 를 같은 20 Hz 로 보낸다
                # (실측 2026-07-31: 6초에 DRIVE 122 / ODOM 122)
                self.send_odometry()
                self.send_imu()
                next_tx += period
                # 밀림 보정. 이게 없으면 한 번 지연된 뒤로 매 루프(2 ms)마다
                # 조건이 참이 되어 400 Hz 넘게 쏟아내는 캐치업 폭주가 된다.
                # 기동 직후 아무도 PTY 를 읽지 않아 write() 가 블록되면 바로
                # 촉발되고, 그 뒤 영구히 회복되지 않는다 (2026-07-31 실측).
                if next_tx <= now:
                    next_tx = now + period
            if now >= next_range:
                self.send_range()
                next_range += range_period
                if next_range <= now:
                    next_range = now + range_period
            time.sleep(0.002)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', default='/tmp/ttySTM32')
    ap.add_argument('-q', '--quiet', action='store_true')
    ap.add_argument('--boot-faults', default='0',
                    help='부팅 시 상주할 active_fault_bits (예: 0xA300 = '
                         '실장비 현재 상태. bit 9/15 는 ARM 을 차단한다)')
    ap.add_argument('--sensor-init-ms', type=int, default=2000,
                    help='초음파 fault(bit 8/15) 자동 해제 시점 [ms]')
    ap.add_argument('--no-imu', action='store_true',
                    help='BNO085 미장착 상태. 0x83 을 안 보내고 IMU_FUSED=0')
    ap.add_argument('--imu-accuracy', type=int, default=3, choices=(0, 1, 2, 3),
                    help='BNO085 accuracy. 0 이면 융합 조건에 미달한다')
    ap.add_argument('--obstacle-at', type=float, default=None,
                    help='이 시각[s]에 bit16 OBSTACLE_NEAR 를 세우고 SAFE_STOP '
                         '으로 간다 (초음파 앞을 손으로 가리는 것과 동일)')
    ap.add_argument('--obstacle-hold-s', type=float, default=2.0,
                    help='bit16 을 유지할 시간 [s]. 이후 0 으로 내린다')
    ap.add_argument('--obstacle-ready-delay-ms', type=int, default=0,
                    help='bit16 해제 후 rearm 을 거부할 시간 [ms]. >0 이면 '
                         'SAFE_STOP + bit16=0 구간(시나리오 B)이 확정적으로 '
                         '관측된다. 0 이면 시나리오 A')
    args = ap.parse_args(argv)
    boot_faults = int(args.boot_faults, 0)
    try:
        FakeStm32(args.port, verbose=not args.quiet,
                  boot_faults=boot_faults,
                  sensor_init_ms=args.sensor_init_ms,
                  imu_on=not args.no_imu,
                  imu_accuracy=args.imu_accuracy,
                  obstacle_at=args.obstacle_at,
                  obstacle_hold_s=args.obstacle_hold_s,
                  obstacle_ready_delay_ms=args.obstacle_ready_delay_ms).run()
    except serial.SerialException as e:
        print(f'serial error: {e}\nsocat이 떠 있는지 확인하세요.',
              file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    sys.exit(main())
