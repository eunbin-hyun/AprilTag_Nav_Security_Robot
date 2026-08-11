"""stm32_bridge ROS 2 노드.

- /cmd_vel 구독 -> Ackermann 변환 -> 20 Hz CMD_DRIVE 반복 송신
- 링크 상태 머신으로 arm/rearm 관리 (V2.2 §1)
- telemetry 의 drive_state / last_drive_seq 를 소비해 명령 수락을 확인 (V2.2 §2)
- 필수 파라미터 미설정 시 기동 실패 (V2.2 §4, 명세 §20.5)

임시 사항 (robot_interfaces 패키지 생성 전):
- /stm32/telemetry 를 std_msgs/String(JSON) 으로 발행한다.
  robot_interfaces/Stm32Telemetry 가 생기면 교체한다. TODO 표시 참조.
"""
import json
import struct
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from std_msgs.msg import Bool, String

import serial

from . import protocol as P
from .parser import FrameParser
from .ackermann import twist_to_drive, NEUTRAL

# 아래 3개는 명세 §20.2 파라미터의 "기본값"이다. 임의값이 아니라 STM32 측과
# 합의된 규격값이며, 실차 정지거리 측정 후 재확정 대상이다 (UART 명세 §10.1, §19).
# 재확정은 공동 리뷰를 거쳐 사람이 YAML 에 기록한다. 자동 반영 경로는 없다.
DEFAULT_CMD_RATE_HZ = 20.0
DEFAULT_CMD_VEL_TIMEOUT_MS = 200     # 명세 §10: Jetson ROS command watchdog
DEFAULT_UART_RX_TIMEOUT_MS = 300     # 명세 §10.4: connected 판정
STOP_REPEAT = 3               # CMD_STOP 3회 (명세 §6.2), 20Hz tick = 50ms 간격
# mcu_time_ms 역행 판정 임계 (명세 §13.5). uint32 wrap 은 delta 가 작게
# 나오므로 이 값을 넘으면 시간이 뒤로 간 것이다. telemetry 가 1시간 넘게
# 정상 수신되지 않는 상황은 이미 connected=false 이므로 오검출되지 않는다.
REBOOT_BACKWARD_MS = 3600_000

# ── 링크 상태 (V2.2 §1.4) ────────────────────────────────
LINK_DISCONNECTED = 0
LINK_QUIET = 1
LINK_ARMING = 2
LINK_ARMED = 3
LINK_STOPPING = 4
LINK_BLOCKED = 5

LINK_NAMES = {
    LINK_DISCONNECTED: 'DISCONNECTED',
    LINK_QUIET: 'QUIET',
    LINK_ARMING: 'ARMING',
    LINK_ARMED: 'ARMED',
    LINK_STOPPING: 'STOPPING',
    LINK_BLOCKED: 'BLOCKED',
}

# V2.2 §4: 미설정 시 기동을 거부하는 파라미터. 0 / 0.0 을 미설정으로 본다.
REQUIRED_PARAMS = ('wheelbase_m', 'max_speed_mm_s', 'max_steering_cdeg')


class Stm32Bridge(Node):
    def __init__(self):
        super().__init__('stm32_bridge')

        # ── 파라미터 ─────────────────────────────────────
        self.declare_parameter('serial_port', '/tmp/ttyJETSON')
        self.declare_parameter('baud_rate', 115200)
        # TBD 값들 (명세 §20.1). 0 이면 기동 실패 (V2.2 §4)
        self.declare_parameter('wheelbase_m', 0.0)
        self.declare_parameter('max_speed_mm_s', 0)
        self.declare_parameter('max_steering_cdeg', 0)
        self.declare_parameter('v_epsilon_m_s', 0.02)
        self.declare_parameter('omega_epsilon_rad_s', 0.02)
        # 주기·timeout (명세 §20.2). 기본값 = 규격값
        self.declare_parameter('command_rate_hz', DEFAULT_CMD_RATE_HZ)
        self.declare_parameter('ros_command_timeout_ms',
                               DEFAULT_CMD_VEL_TIMEOUT_MS)
        self.declare_parameter('uart_rx_timeout_ms',
                               DEFAULT_UART_RX_TIMEOUT_MS)
        # 링크 상태 머신 (명세 §20.2, V2.2 §1.7)
        self.declare_parameter('rearm_quiet_ms', 350)
        self.declare_parameter('arming_timeout_ms', 1000)

        # ── V2.2 §4: TBD 보호. 조용한 neutral 대신 기동 실패 ──
        missing = [n for n in REQUIRED_PARAMS
                   if not self.get_parameter(n).value]
        if missing:
            self.get_logger().error(
                '필수 파라미터 미설정으로 기동할 수 없습니다 (명세 §20.5): '
                + ', '.join(missing))
            self.get_logger().error(
                '실측값을 확정해 -p 로 지정하십시오. '
                '0 을 안전 기본값으로 두고 구동하지 않습니다.')
            raise RuntimeError(f'unconfigured parameters: {missing}')

        self.cmd_rate_hz = self.get_parameter('command_rate_hz').value
        self.cmd_vel_timeout_s = (
            self.get_parameter('ros_command_timeout_ms').value / 1000.0)
        self.telemetry_timeout_s = (
            self.get_parameter('uart_rx_timeout_ms').value / 1000.0)
        self.rearm_quiet_s = self.get_parameter('rearm_quiet_ms').value / 1000.0
        self.arming_timeout_s = (
            self.get_parameter('arming_timeout_ms').value / 1000.0)
        # V2.2 §5.2: 검출 지연 허용 범위 = timeout ~ timeout + 1/command_rate_hz
        self.get_logger().info(
            f'rate={self.cmd_rate_hz} Hz  '
            f'cmd_vel_timeout={self.cmd_vel_timeout_s*1000:.0f} ms  '
            f'검출 허용범위 {self.cmd_vel_timeout_s*1000:.0f}~'
            f'{(self.cmd_vel_timeout_s + 1.0/self.cmd_rate_hz)*1000:.0f} ms')

        port = self.get_parameter('serial_port').value
        baud = self.get_parameter('baud_rate').value

        # ── 시리얼 ───────────────────────────────────────
        try:
            self.ser = serial.Serial(port, baud, timeout=0)
        except serial.SerialException as e:
            self.get_logger().fatal(f'cannot open {port}: {e}')
            raise
        self.get_logger().info(f'serial open: {port} @ {baud}')

        self.parser = FrameParser()
        self.tx_seq = P.SeqCounter()

        # ── 명령 상태 ────────────────────────────────────
        self.last_cmd = None                  # (Twist, monotonic)
        self.was_driving = False              # enable=1 을 보낸 적 있는가
        self.stop_active = False              # /safety/stop_active (V2.1 §2.3)

        # ── 링크 상태 (V2.2 §1) ──────────────────────────
        self.link = LINK_DISCONNECTED
        self.link_t = time.monotonic()
        self.arm_seq = None                   # ARMING 진입 후 최초 송신 SEQ
        self.arm_n_sent = 0                   # ARMING 중 실제 송신한 프레임 수
        self.arm_window_warned = False
        self.stop_remaining = 0
        self.stop_reason = P.STOP_ROS_COMMAND_TIMEOUT
        self.arming_warned = False

        # ── 관측 상태 ────────────────────────────────────
        self.last_telemetry_t = None
        self.stm32_state = None
        self.prev_mcu_ms = None               # §13.5 reboot 검출용
        self.prev_encoder = None
        self.ever_armed = False               # V2.2 §6: 부팅 후 최초 1회
        self.connected = False
        self.estop = False
        self.last_range_t = None              # §3.8, connected 판정과 무관
        self.prev_faults = None               # §9.1 변화 감지용
        self.warned_blocking = 0              # ARM 차단 경고 중복 방지
        self.fault_rsv_warned = False

        # ── 진단 카운터 (V2.2 §2.4) ──────────────────────
        self.arming_timeouts = 0
        self.rearm_count = 0
        self.unknown_msg_count = 0
        self.range_schema_errors = 0
        self.range_rsv_warned = False

        # ── 토픽 ─────────────────────────────────────────
        self.sub_cmd = self.create_subscription(
            Twist, '/cmd_vel', self.on_cmd_vel, 1)
        # V2.1 §2.3: bridge 는 stop_active 만 구독한다
        self.sub_stop = self.create_subscription(
            Bool, '/safety/stop_active', self.on_stop_active, 1)
        # TODO(robot_interfaces): Stm32Telemetry 로 교체
        self.pub_telemetry = self.create_publisher(
            String, '/stm32/telemetry', 5)
        # TODO(robot_interfaces): sensor_msgs/Range 4개 또는 전용 msg 로 교체.
        # 명세 §3.8: near_stop_mm 실측 전까지 "송신만" 이며 판정에 쓰지 않는다.
        # 이 토픽을 safety 경로에 연결하기 전에 반드시 공동 리뷰를 거친다.
        self.pub_range = self.create_publisher(
            String, '/stm32/range', 5)
        self.pub_connected = self.create_publisher(
            Bool, '/stm32/connected', 1)
        self.pub_estop = self.create_publisher(
            Bool, '/safety/estop_state', 1)

        # ── 타이머 ───────────────────────────────────────
        self.create_timer(1.0 / self.cmd_rate_hz, self.tx_tick)
        self.create_timer(0.005, self.rx_tick)               # 수신 polling
        self.create_timer(0.5, self.status_tick)             # 2 Hz heartbeat

    # ── 링크 상태 전이 (V2.2 §1.5) ───────────────────────

    def set_link(self, new: int, why: str = ''):
        if new == self.link:
            return
        self.get_logger().info(
            f'link {LINK_NAMES[self.link]} -> {LINK_NAMES[new]}'
            + (f'  ({why})' if why else ''))
        self.link = new
        self.link_t = time.monotonic()
        if new == LINK_ARMING:
            self.arm_seq = None
            self.arm_n_sent = 0
            self.arming_warned = False
            self.arm_window_warned = False
        if new == LINK_STOPPING:
            self.stop_remaining = STOP_REPEAT

    def enter_stopping(self, reason: int, why: str):
        """CMD_STOP 송신 시작. 이미 STOPPING 이면 우선순위 높은 reason 만 반영.

        V2.2 §3.3: 값이 작을수록 우선순위가 높다 (정책 계층 우선).
        V2.2 §3.4: 재진입해서 3회 카운터를 리셋하지 않는다.
        """
        if self.link == LINK_STOPPING:
            if reason < self.stop_reason:
                self.get_logger().warn(
                    f'stop_reason {self.stop_reason} -> {reason} ({why})')
                self.stop_reason = reason
            return
        self.stop_reason = reason
        self.set_link(LINK_STOPPING, why)

    # ── 구독 콜백 ────────────────────────────────────────

    def on_cmd_vel(self, msg: Twist):
        self.last_cmd = (msg, time.monotonic())

    def on_stop_active(self, msg: Bool):
        if msg.data != self.stop_active:
            self.get_logger().info(f'/safety/stop_active -> {msg.data}')
        self.stop_active = msg.data

    # ── 송신 (20 Hz) ─────────────────────────────────────

    def tx_tick(self):
        now = time.monotonic()

        if self.link == LINK_DISCONNECTED:
            # 이 구현은 포트를 닫지 않으므로 곧바로 rearm 절차로 진입한다.
            if self.ser.is_open:
                self.set_link(LINK_QUIET, 'serial available')
            return                                   # 송신 없음 (명세 §13.1-5)

        if self.link == LINK_QUIET:
            if now - self.link_t >= self.rearm_quiet_s:
                self.set_link(LINK_ARMING, 'quiet elapsed')
            return                                   # 송신 없음

        if self.link == LINK_ARMING:
            seq = self.tx_seq.next()
            if self.arm_seq is None:
                self.arm_seq = seq
            if self.arm_n_sent < 256:
                self.arm_n_sent += 1
            elif not self.arm_window_warned:
                # SEQ 가 한 바퀴 돌아 수락 판정 창이 포화됐다. 더 넓히면
                # 낡은 값을 구분할 수 없으므로 창을 고정하고 크게 알린다.
                self.arm_window_warned = True
                self.get_logger().error(
                    'ARMING 중 256 프레임을 보냈으나 수락되지 않았습니다. '
                    'SEQ 판정 창 포화 — 수신 측이 세션 리셋을 하지 않는 '
                    '문제일 수 있습니다 (V2.3 D2)')
            self.send(P.pack_neutral(seq))
            if (not self.arming_warned
                    and now - self.link_t > self.arming_timeout_s):
                self.arming_warned = True
                self.arming_timeouts += 1
                self.get_logger().warn(
                    f'ARMING {self.arming_timeout_s*1000:.0f} ms 초과 — '
                    f'STM32 state='
                    f'{P.STATE_NAMES.get(self.stm32_state, "?")}, '
                    'neutral 수락이 확인되지 않습니다')
            return

        if self.link == LINK_BLOCKED:
            # FAULT/ESTOP: neutral 은 출력 0 유지 용도로만 (명세 §6.1)
            self.send(P.pack_neutral(self.tx_seq.next()))
            return

        if self.link == LINK_STOPPING:
            if self.stop_remaining > 0:
                self.send(P.pack_cmd_stop(self.tx_seq.next(),
                                          self.stop_reason))
                self.stop_remaining -= 1
            else:
                self.set_link(LINK_ARMING, 'stop sequence complete')
            return

        # ── LINK_ARMED ──
        if self.stop_active:
            # TODO(robot_interfaces): SafetyStatus.primary_reason 매핑 (V2.2 §3.3)
            self.enter_stopping(P.STOP_OPERATOR, 'safety stop_active')
            return

        fresh = (self.last_cmd is not None
                 and now - self.last_cmd[1] <= self.cmd_vel_timeout_s)

        if not fresh:
            if self.was_driving:
                # 캐시 폐기 + CMD_STOP (명세 §10.3)
                age_ms = (now - self.last_cmd[1]) * 1000.0
                self.was_driving = False
                self.last_cmd = None
                self.enter_stopping(
                    P.STOP_ROS_COMMAND_TIMEOUT,
                    f'/cmd_vel timeout '
                    f'{self.cmd_vel_timeout_s*1000:.0f} ms, '
                    f'age={age_ms:.1f} ms')
            else:
                # ARMED 이지만 아직 구동 명령이 없음 -> neutral 유지
                self.send(P.pack_neutral(self.tx_seq.next()))
            return

        twist = self.last_cmd[0]
        cmd = twist_to_drive(
            twist.linear.x, twist.angular.z,
            wheelbase_m=self.get_parameter('wheelbase_m').value,
            max_speed_mm_s=self.get_parameter('max_speed_mm_s').value,
            max_steering_cdeg=self.get_parameter('max_steering_cdeg').value,
            v_eps=self.get_parameter('v_epsilon_m_s').value,
            w_eps=self.get_parameter('omega_epsilon_rad_s').value)
        if cmd is None:                    # 제자리 회전 거부 (명세 §9.5)
            self.get_logger().warn(
                'in-place rotation rejected (Ackermann)',
                throttle_duration_sec=1.0)
            cmd = NEUTRAL
        self.send(P.pack_cmd_drive(self.tx_seq.next(), cmd.speed_mm_s,
                                   cmd.steering_cdeg, cmd.enable))
        if cmd.enable:
            self.was_driving = True

    def send(self, frame: bytes):
        try:
            self.ser.write(frame)
        except serial.SerialException as e:
            self.get_logger().error(f'serial write failed: {e}',
                                    throttle_duration_sec=1.0)
            self.ever_armed = False
            self.set_link(LINK_DISCONNECTED, 'serial write error')

    # ── 수신 ─────────────────────────────────────────────

    def rx_tick(self):
        try:
            data = self.ser.read(512)
        except serial.SerialException as e:
            self.get_logger().error(f'serial read failed: {e}',
                                    throttle_duration_sec=1.0)
            self.ever_armed = False
            self.set_link(LINK_DISCONNECTED, 'serial read error')
            return
        if data:
            for msg_id, seq, payload in self.parser.feed(data):
                self.on_frame(msg_id, seq, payload)
        self.parser.check_timeout()

        # connected 판정 (V2.2 §6: 링크 유효성 + 최초 1회 ARMED 이력)
        now = time.monotonic()
        ok = (self.last_telemetry_t is not None
              and now - self.last_telemetry_t <= self.telemetry_timeout_s
              and self.ever_armed)
        if ok != self.connected:
            self.connected = ok
            self.get_logger().warn(f'/stm32/connected -> {ok}')
            self.pub_connected.publish(Bool(data=ok))

    def on_frame(self, msg_id: int, seq: int, payload: bytes):
        if msg_id == P.TELEMETRY_DRIVE:
            t = P.unpack_telemetry(payload)
            self.last_telemetry_t = time.monotonic()
            # TODO(robot_interfaces): Stm32Telemetry 발행으로 교체
            self.pub_telemetry.publish(String(data=json.dumps(t)))

            self.mirror_telemetry(t)

            estop = bool(t['active_fault_bits'] & P.FAULT_ESTOP_ACTIVE)
            if estop != self.estop:
                self.estop = estop
                self.pub_estop.publish(Bool(data=estop))

        elif msg_id == P.FAULT_EVENT:
            f = P.unpack_fault_event(payload)
            # latched 는 active 의 누적이 아니라 명세 §9.3 의 4개 bit 만
            # 나타낸다. active=0xA300 / latched=0 은 의도된 조합이므로
            # 불일치로 취급하지 않는다.
            self.get_logger().warn(
                f"FAULT_EVENT state={P.STATE_NAMES.get(f['drive_state'], '?')}"
                f" action={f['fault_action']}"
                f"\n  active  0x{f['active_fault_bits']:08X}"
                f" = {P.describe_faults(f['active_fault_bits'])}"
                f"\n  latched 0x{f['latched_fault_bits']:08X}"
                f" = {P.describe_faults(f['latched_fault_bits'])}")

        elif msg_id == P.COMMAND_RESULT:
            r = P.unpack_command_result(payload)
            self.get_logger().info(
                f"COMMAND_RESULT req=0x{r['request_msg_id']:02X} "
                f"seq={r['request_seq']} result={r['result_code']}")

        elif msg_id == P.TELEMETRY_RANGE:
            self.on_telemetry_range(payload)

        else:
            # 미구현 msg_id 를 조용히 버리지 않는다. TELEMETRY_RANGE(0x84) 가
            # 실장비에서 10 Hz 로 오는데도 fake 에 없어서 오래 드러나지 않았던
            # 전례가 있다. 파서는 FIXED_LENGTHS 에 없는 ID 를 통과시키므로
            # 여기서 세지 않으면 프레임이 흔적 없이 사라진다.
            self.unknown_msg_count += 1
            self.get_logger().warn(
                f'미구현 msg_id=0x{msg_id:02X} len={len(payload)} '
                f'(누적 {self.unknown_msg_count}회) — 명세 §5 확인 필요',
                throttle_duration_sec=5.0)

    def on_telemetry_range(self, payload: bytes):
        """초음파 4채널 수신 (명세 §3.8).

        판정에는 쓰지 않는다. near_stop_mm 실측 전까지는 발행만 한다.
        connected 판정도 갱신하지 않는다 — 그 근거는 TELEMETRY_DRIVE 이며
        (§10.4), RANGE 는 10 Hz 로 주기가 달라 섞으면 timeout 기준이 흐려진다.
        """
        try:
            r = P.unpack_telemetry_range(payload)
        except (ValueError, struct.error) as e:
            self.range_schema_errors += 1
            self.get_logger().warn(f'TELEMETRY_RANGE 해석 실패: {e}',
                                   throttle_duration_sec=5.0)
            return

        self.last_range_t = time.monotonic()

        if r['valid_mask'] & P.RANGE_VALID_RESERVED and not self.range_rsv_warned:
            self.range_rsv_warned = True
            self.get_logger().warn(
                f"TELEMETRY_RANGE valid_mask=0x{r['valid_mask']:02X}: "
                '예약 bit 4~7 이 0 이 아니다 (명세 §3.8). STM32 측 확인 필요')

        # TODO(robot_interfaces): sensor_msgs/Range 로 교체
        self.pub_range.publish(String(data=json.dumps(r)))

    # ── STM32 상태 미러링 (V2.2 §2.3) ────────────────────

    def detect_reboot(self, t) -> str:
        """STM32 재부팅 근거를 찾는다 (명세 §13.5). 근거 문자열 또는 ''.

        주 근거는 mcu_time_ms 역행이다. drive_state 의 BOOT/SELF_TEST 는
        §8.2 상 수 ms 만에 지나가는 과도 상태라 20 Hz telemetry 로는
        놓치기 쉬우므로 보조 근거로만 쓴다.

        encoder_count 초기화는 근거로 쓰지 않는다. 후진 중 누적값이 0 을
        지나는 정상 상황과 구분되지 않기 때문이다. 로그로만 남긴다.
        """
        mcu = t['mcu_time_ms']
        if self.prev_mcu_ms is not None:
            # uint32 wrap 은 delta 가 작게 나온다. 역행이면 거대해진다.
            delta = (mcu - self.prev_mcu_ms) & 0xFFFFFFFF
            if delta > REBOOT_BACKWARD_MS:
                return (f'mcu_time_ms 역행 {self.prev_mcu_ms} -> {mcu}')
        if t['drive_state'] in (P.STATE_BOOT, P.STATE_SELF_TEST):
            return f"drive_state={P.STATE_NAMES[t['drive_state']]}"
        return ''

    def report_faults(self, bits: int):
        """active_fault_bits 변화를 이름으로 기록한다 (명세 §9.1).

        0xA300 같은 raw mask 는 사람이 읽을 수 없으므로 반드시 이름으로
        남긴다. 사라진 bit 도 같이 알려야 "해소됐는지" 판단할 수 있다.
        """
        if bits == self.prev_faults:
            return
        if self.prev_faults is None and bits == 0:
            self.prev_faults = 0      # 정상 기동은 알릴 것이 없다
            return
        prev = self.prev_faults or 0
        gained, lost = bits & ~prev, prev & ~bits
        parts = []
        if gained:
            parts.append(f'+[{P.describe_faults(gained)}]')
        if lost:
            parts.append(f'-[{P.describe_faults(lost)}]')
        self.get_logger().warn(
            f'fault 0x{bits:08X} ' + ' '.join(parts))

        if bits & P.FAULT_RESERVED_MASK and not self.fault_rsv_warned:
            self.fault_rsv_warned = True
            self.get_logger().error(
                f'active_fault_bits 예약 bit 17~31 이 0 이 아닙니다 '
                f'(0x{bits & P.FAULT_RESERVED_MASK:08X}) — 명세 §9.1 위반')
        self.prev_faults = bits

    def warn_arm_blocked(self, blocking: int):
        """ARM 이 fault 때문에 불가능함을 한 번만, 조치까지 알린다.

        이 경고가 없으면 ARMING 이 무한 반복되다가 SEQ 판정 창 포화 오류가
        먼저 떠서 세션 리셋 문제로 오진하게 된다 (실제 원인은 fault).
        """
        if blocking == self.warned_blocking:
            return
        self.warned_blocking = blocking
        todo = []
        for b in range(32):
            if blocking & (1 << b):
                e = P.FAULT_TABLE.get(b)
                if e:
                    todo.append(f'{e[0]} -> {e[2]}')
        self.get_logger().error(
            f'ARM 불가: neutral 로 해소되지 않는 fault 가 활성입니다 '
            f'[{P.describe_faults(blocking)}]. 필요한 조치: '
            + '; '.join(todo)
            + '. neutral 재송신으로는 풀리지 않으므로 재시도하지 않습니다')

    def mirror_telemetry(self, t):
        ds = t['drive_state']
        last_drive_seq = t['last_drive_seq']
        faults = t['active_fault_bits']

        # ── STM32 reboot 검출 (명세 §13.5). 다른 판정보다 먼저 ──
        why = self.detect_reboot(t)
        if why:
            enc = t['encoder_count']
            corr = ''
            if self.prev_encoder is not None and abs(self.prev_encoder) > 1000 \
                    and abs(enc) < 100:
                corr = f', encoder {self.prev_encoder} -> {enc} 초기화'
            self.get_logger().warn(f'STM32 reboot detected ({why}{corr})')
            # 새 세션을 기준으로 다시 잡는다 (명세 §13.5: 기존 pose 에
            # 시간 점프를 만들지 않는다)
            self.prev_mcu_ms = t['mcu_time_ms']
            self.prev_encoder = enc
            self.stm32_state = ds
            self.ever_armed = False           # 명세 §10.4: rearm 미완료
            self.was_driving = False
            self.set_link(LINK_DISCONNECTED, 'STM32 reboot')
            return

        self.prev_mcu_ms = t['mcu_time_ms']
        self.prev_encoder = t['encoder_count']

        prev, self.stm32_state = self.stm32_state, ds
        if ds != prev and prev is not None:
            self.get_logger().info(
                f'STM32 drive_state -> {P.STATE_NAMES.get(ds, ds)}')

        self.report_faults(faults)
        blocking = P.arm_blocking_faults(faults)

        # BLOCKED 진입 근거는 두 가지이며 둘 다 필요하다.
        #   (1) drive_state 가 FAULT/ESTOP
        #   (2) drive_state 는 SAFE_STOP 이지만 neutral 로 해소되지 않는
        #       fault 가 활성 -- 실장비 0xA300(bit 9 STEERING_INVALID,
        #       bit 15 SENSOR_STALE) 이 정확히 이 경우다. (1)만 보면 ARMING
        #       에서 20 Hz neutral 을 무한 반복하다가 SEQ 창 포화 오류로
        #       오진한다.
        if ds in (P.STATE_FAULT, P.STATE_ESTOP):
            self.warned_blocking = 0
            self.set_link(LINK_BLOCKED, f'STM32 {P.STATE_NAMES[ds]}')
            return
        if blocking:
            self.warn_arm_blocked(blocking)
            self.set_link(
                LINK_BLOCKED,
                f'차단 fault 활성 [{P.describe_faults(blocking)}]')
            return

        # BLOCKED 이탈은 "차단 fault 가 모두 사라졌을 때" 뿐이다. 예전에는
        # FAULT/ESTOP 이 아니기만 하면 ARMING 으로 내려갔는데, 그러면
        # SAFE_STOP + 차단 fault 상태에서 BLOCKED <-> ARMING 을 무한 왕복한다.
        if self.link == LINK_BLOCKED:
            self.warned_blocking = 0
            self.set_link(LINK_ARMING, '차단 fault 해소')
            return

        if self.link == LINK_ARMING:
            # V2.2 §2.2 (V2.3 D1 수정): last_drive_seq 가 "이번 ARMING 중에
            # 실제로 보낸 SEQ" 인지 확인한다. 단순히 arm_seq 보다 앞선지만
            # 보면 이전 세션의 낡은 last_drive_seq 를 수락하게 된다.
            if ds == P.STATE_READY and self.arm_seq is not None:
                delta = (last_drive_seq - self.arm_seq) & 0xFF
                if delta < self.arm_n_sent:
                    self.rearm_count += 1
                    self.ever_armed = True
                    self.set_link(
                        LINK_ARMED,
                        f'neutral accepted seq={last_drive_seq} '
                        f'({delta + 1}/{self.arm_n_sent} 프레임째, '
                        f'rearm #{self.rearm_count})')
            return

        if self.link == LINK_ARMED and ds == P.STATE_SAFE_STOP:
            # STM32 가 독자적으로 정지했다. 이후 비영 명령은 거부되므로
            # 즉시 rearm 경로로 내려간다 (V2.2 §2.3)
            self.was_driving = False
            self.set_link(LINK_ARMING, 'STM32 entered SAFE_STOP')

    # ── 상태 heartbeat (2 Hz) ────────────────────────────

    def status_tick(self):
        self.pub_connected.publish(Bool(data=self.connected))
        self.pub_estop.publish(Bool(data=self.estop))


def main(args=None):
    rclpy.init(args=args)
    try:
        node = Stm32Bridge()
    except RuntimeError:
        rclpy.shutdown()
        raise
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        # 정상 종료 (명세 §13.4): CMD_STOP 3회 + neutral 후 close
        try:
            for _ in range(3):
                node.ser.write(P.pack_cmd_stop(node.tx_seq.next(),
                                               P.STOP_LINK_SHUTDOWN))
                time.sleep(0.05)
            node.ser.write(P.pack_neutral(node.tx_seq.next()))
            node.ser.close()
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
