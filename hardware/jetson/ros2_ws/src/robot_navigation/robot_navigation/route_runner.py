"""route_runner — UART 를 직접 소유하는 주행 노드.

구조:
    TX 20 Hz 스케줄러 스레드   최신 명령을 반복 송신 + **자체 watchdog**
    RX 파서 스레드              텔레메트리·오도메트리·fault
    상태머신 타이머 20 Hz       route_logic.RouteMachine.tick() 호출
    ROS 입출력                  /tag/target, /route/*, /stm32/*

왜 UART 를 이 노드가 직접 소유하는가:
  포트는 한 프로세스만 열 수 있다. `/cmd_vel` 같은 중간 토픽을 두면 브리지
  노드를 따로 띄워야 하고, 그러면 200 ms watchdog 을 하나 더 만드는 셈이다.
  Nav2·teleop 을 쓰지 않으므로 Twist 추상화의 이득이 없다. `CMD_DRIVE` 는
  각도를 받으므로 조향각을 직접 만드는 것이 왕복 변환보다 정확하다.

⚠ **TX 스케줄러 자체 watchdog 이 핵심 안전장치다.**
  스케줄러는 최신 명령을 무한히 반복한다. 상태머신 타이머가 예외로 죽거나
  멈추면 로봇이 마지막 속도로 계속 간다 — STM32 watchdog 은 프레임이 계속
  오니까 걸리지 않는다. 그래서 상태머신이 `command_stale_s` 동안 명령을
  갱신하지 않으면 스케줄러가 neutral 을 강제한다.

⚠ 상태머신은 serial write 를 직접 하지 않는다. 명령만 갱신하고 스케줄러가
  보낸다 (명세 §8 구현 품질 요구사항).

실행:
    # fake 상대 (실차·카메라 없이)
    socat pty,raw,echo=0,link=/tmp/ttyJETSON pty,raw,echo=0,link=/tmp/ttySTM32 &
    ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32 &
    ros2 run robot_navigation route_runner --ros-args -p serial_port:=/tmp/ttyJETSON

    # 실장비
    ros2 run robot_navigation route_runner --ros-args -p serial_port:=/dev/ttyUSB0

    # 미션 시작·중단
    ros2 service call /route/start std_srvs/srv/Trigger
    ros2 service call /route/return std_srvs/srv/Trigger
    ros2 service call /route/abort std_srvs/srv/Trigger
    ros2 service call /route/reset_fault std_srvs/srv/Trigger
"""
import json
import math
import threading
import time

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import Imu
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger

import serial

from stm32_bridge import protocol as P
from stm32_bridge.parser import FrameParser

from robot_perception.tag_geometry import SteeringConfig

from .imu_convert import DEFAULT_IMU_FRAME, imu_si
from .odom_convert import (BASE_FRAME, DeadReckon, ODOM_FRAME, odom_si,
                           yaw_to_quaternion)
from .route_logic import (
    FAULT, RUNNING, Inputs, NEUTRAL, RouteConfig, RouteMachine, approach_mission,
    relative_turn_mission,
    outbound_mission, return_mission, tag_target_from_dict,
)

TX_HZ = 20.0
TICK_HZ = 20.0

# 직진 중 조향이 이 안이면 '보정' 이라고도 붙이지 않는다 (deg).
STRAIGHT_DEG = 1.0
# 단계를 모를 때만 쓰는 폴백 경계. 이 위면 회전으로 본다.
CORRECT_DEG = 5.0


def _motion(cmd, step) -> str:
    """CMD_DRIVE 를 사람이 읽는 말로 요약한다.

    ⚠ 조향각이 아니라 **단계 종류**로 판단한다.
      태그를 보며 달릴 때 경로를 크게 벗어나면 보정 조향도 10° 를 넘을 수
      있다. 각도만 보면 그게 코너 회전과 똑같이 찍혀서, 로그만 보고는
      "코너를 도는 중" 인지 "직진하다 크게 틀어진 중" 인지 구분할 수 없다.
      상태머신은 둘을 이미 구분하고 있으므로 그 판단을 그대로 쓴다.
    """
    if not cmd.enable or cmd.speed_mm_s == 0:
        return '정지'
    deg = cmd.steering_cdeg / 100.0
    side = '좌' if deg > 0 else '우'

    if step is not None and step.kind == 'turn':
        # 제자리 목표각을 채우는 의도된 회전. U턴은 따로 부른다.
        kind = 'U턴' if abs(step.turn_deg) >= 150.0 else '코너회전'
        return f'{kind} {side}{abs(step.turn_deg):.0f}°'

    head = '후진' if cmd.speed_mm_s < 0 else '직진'
    if step is not None and step.kind == 'drive':
        # 경로 추종 중. 조향은 벗어난 만큼 되돌리는 보정일 뿐이다.
        if abs(deg) < STRAIGHT_DEG:
            return head
        return f'{head}({side}보정 {abs(deg):.1f}°)'

    # 단계 정보가 없을 때만 각도로 추정한다 (정상 경로에서는 오지 않는다)
    if abs(deg) < STRAIGHT_DEG:
        return head
    if abs(deg) < CORRECT_DEG:
        return f'{head}({side}보정 {abs(deg):.1f}°)'
    return f'{side}회전?'


class RouteRunner(Node):
    def __init__(self):
        super().__init__('route_runner')

        self.declare_parameter('serial_port', '/dev/ttyUSB0')
        self.declare_parameter('baud_rate', 115200)
        self.declare_parameter('wheelbase_m', 0.135)
        self.declare_parameter('v_cruise_mm_s', 200)
        self.declare_parameter('v_approach_mm_s', 150)
        self.declare_parameter('v_turn_mm_s', 200)
        # 조향 한계는 비대칭이다 (vehicle_config.h 실측). 대칭으로 두면 한쪽이
        # STM32 에서 COMMAND_LIMIT 으로 거부되고 CMD_DRIVE 는 응답이 없어서
        # 조용히 실패한다.
        self.declare_parameter('min_steering_cdeg', -2869)
        self.declare_parameter('max_steering_cdeg', 1955)
        self.declare_parameter('kp_cross', 4000.0)
        self.declare_parameter('kp_heading', 1200.0)
        self.declare_parameter('kp_yaw_hold', 1500.0)
        self.declare_parameter('tag_stale_s', 0.4)
        self.declare_parameter('command_stale_s', 0.2)
        # ★ 이 값은 **통신단절 latch FAULT 의 문턱**이다 (route_logic §통신단절).
        #   넘으면 사람이 /route/reset_fault 를 해야 재개된다 — 일시적 공백에
        #   그러면 과하다. 그래서 느슨하게 잡는다 (0.3 → 0.5).
        #   느슨하게 해도 안전하다: 장애물 정지는 STM32 가 SAFE_STOP 으로
        #   자율 수행하고, 우리 프레임이 끊기면 STM32 watchdog 이 따로 잡는다.
        #   이 값은 "STM32 상태를 아는가" 에만 쓰인다.
        #   ⚠ 예전에는 이 값이 IMU yaw 신선도까지 겸했다. 두 요구가 반대라
        #     (여기는 느슨, yaw 는 촘촘) 겸하면 어느 쪽도 못 맞춘다 —
        #     0.5 로 올리면 회전 중 15° 를 못 보고 지나친다. imu_stale_s 로
        #     분리했다.
        self.declare_parameter('telemetry_timeout_s', 0.5)
        # 회전 판정에 쓰는 0x83 yaw 의 허용 지연 [s]. 오래된 yaw 로 판정하면
        # 그 시점에 멈춘 것처럼 보여 과회전한다. 실측 회전 각속도 30°/s 이므로
        # 0.3 s = 9° 다. 통신단절 문턱과 **다른 값이어야 한다.**
        self.declare_parameter('imu_stale_s', 0.3)
        # 명령이 바뀔 때마다 터미널에 찍는다. STM32 가 연결되지 않은 상태에서
        # "지금 어떤 CMD_DRIVE 가 나가려 하는지" 를 눈으로 확인하는 용도다.
        self.declare_parameter('log_commands', True)
        # 로그 속도 제한. 태그가 보이면 조향이 매 프레임 미세하게 달라져
        # "바뀔 때마다" 가 곧 20 Hz 폭주가 된다. 반대로 정속 직진 중에는
        # 아무것도 안 찍혀 노드가 죽은 것과 구분되지 않는다. 그래서
        # 최소 간격으로 상한을 두고, 심장박동으로 하한을 둔다.
        self.declare_parameter('log_min_interval_s', 0.25)
        self.declare_parameter('log_period_s', 1.0)
        # 구간 거리 상한(FAULT_OVERRUN). 시운전 중에는 넉넉하게 잡는다.
        self.declare_parameter('leg_max_m', 3.0)
        # IMU 좌표계 이름. BNO085 축은 STM32 가 차체 기준으로 맞춰 보내므로
        # base_link 와 축이 같다. 병진 오프셋은 조립 후 실측해서 별도
        # 프레임 + static TF 로 옮길 것.
        self.declare_parameter('imu_frame_id', DEFAULT_IMU_FRAME)
        # 단일 태그 접근 시험용. 코스를 깔지 않고 체인만 검증할 때 쓴다.
        # 실행 중에도 바꿀 수 있다: ros2 param set /route_runner approach_tag_id 3
        self.declare_parameter('approach_tag_id', 2)
        # 0.7: 근접 태그 소실 실측(2026-08-03) 반영. 0.66 m 에서 태그가
        # 프레임을 이탈해 0.6 판정에 못 닿고 지나쳤다. 소실 전에 멈추도록
        # 여유를 둔다. 태그를 카메라 높이(0.23 m)에 재부착 후 재평가.
        self.declare_parameter('approach_trigger_m', 0.7)
        # ── 상대 yaw 회전 (JETSON_YAW90). 30° 저속 시험 후 90° 로 올린다.
        self.declare_parameter('turn_target_deg', 90.0)
        self.declare_parameter('turn_steering_cdeg', 1800)
        # 150: 90 은 실바닥 미검증이고 최저 주행 명령이 150(실측 122 mm/s)이다.
        self.declare_parameter('v_turn_slow_mm_s', 150)
        self.declare_parameter('slow_remaining_deg', 15.0)
        self.declare_parameter('stop_lead_left_deg', 3.0)
        self.declare_parameter('stop_lead_right_deg', 3.0)
        # 2.5: 실측 R 0.63~0.99 m 이면 90° 에 1.0~1.6 m, 180° U턴에 1.9 m 가
        # 필요하다. 예전 0.90 은 모델 반경 0.42 가정이라 정상 회전도 거리
        # 가드에 걸렸다 (런치 기본값과 일치시켜 둔다 — 여기만 낮으면
        # `ros2 run` 으로 직접 띄울 때만 조용히 실패한다).
        self.declare_parameter('turn_max_m', 2.5)
        self.declare_parameter('require_imu_fused', True)
        # 융합이 연속 이 시간 이상 끊길 때만 FAULT. 실장비 BNO085 는
        # IMU_FUSED 를 깜빡인다 (2026-08-05 실측 최장 dropout 542 ms).
        self.declare_parameter('fusion_dropout_grace_s', 0.8)
        # CMD_RESET_FAULT 응답(fault bit 해제) 대기 상한 (내일과제 §1)
        self.declare_parameter('reset_timeout_s', 2.0)
        self.declare_parameter('obstacle_clear_hold_s', 0.5)
        self.declare_parameter('obstacle_ready_timeout_s', 2.0)
        # 장애물 HOLD·복구 구간 상세 추적 (문서 §7). 기본 off — 20 Hz 로 찍으면
        # 기존 상태줄이 묻힌다. 재현 시에만 켜고, 그때는 50 ms 급으로 남긴다.
        self.declare_parameter('debug_obstacle_recovery', False)
        self.declare_parameter('approach_max_m', 3.0)
        # 코너 진입 레그 종료 거리 [m] — **방향별로 다르다.** 회전반경 R
        # 만큼 앞에서 돌기 시작해야 접선이 맞고, 트림 때문에 좌우 R 이 1.5 배
        # 차이난다 (실측 좌 0.63 / 우 0.98 m, 2026-08-05).
        # 좌(태그4 진입)는 2026-08-06 지시로 반경보다 앞선 1.00 m 를 쓴다.
        self.declare_parameter('corner_trigger_right_m', 1.00)
        self.declare_parameter('corner_trigger_left_m', 1.00)
        # 종점 정지 거리 [m] — corner_trigger_* 과 같은 **뒷차축 기준**.
        # 도착(태그3)과 도크(태그1)를 따로 둔다: 도착은 가까이 붙어야 하고
        # 도크는 충전 접점 여유가 필요하다. 한 값이면 한쪽을 조일 때 다른
        # 쪽도 조용히 같이 조여진다. 도크는 2026-08-06 지시로 0.65 로 내렸다
        # (앞머리 여유 0.38 m). 값은 같아도 파라미터는 계속 따로다.
        self.declare_parameter('arrive_trigger_m', 0.65)
        self.declare_parameter('dock_trigger_m', 0.65)
        # 표시용. 로그·JSON 에 앞머리 여유를 같이 찍기 위한 실측 상수.
        self.declare_parameter('front_overhang_m', 0.27)
        # 태그를 놓친 뒤 엔코더로 채워도 되는 최대 거리 [m]. 코너 진입에서
        # 태그가 화각을 벗어나 단계가 안 끝나는 것을 막는다. 0 이면 비활성.
        self.declare_parameter('tag_lost_coast_max_m', 0.8)
        # drive 보정 상한 [cdeg]. 좌우를 같은 크기로 맞춘다 (트림이 좌측
        # 실효 한계를 +1455 로 깎으므로). 0 이면 무제한.
        self.declare_parameter('correction_limit_cdeg', 1455)
        # 종점(도착·도킹) 정렬 조건. 거리와 **함께** 요구한다. 코너 진입은
        # yaw 로 회전하므로 적용하지 않는다. 0 이면 거리만 본다 (예전 동작).
        self.declare_parameter('align_cross_m', 0.04)
        self.declare_parameter('align_head_deg', 5.0)
        self.declare_parameter('align_hold_s', 0.2)
        self.declare_parameter('align_extra_m', 0.10)
        # ── 조향 중립 트림 [cdeg] ────────────────────────────
        # 링키지·서보 혼이 기계적으로 어긋나 cdeg 0 이 직진이 아닐 때 쓴다.
        # 2026-08-04 실측: **+500 이 직진**이었다 (0 은 우측 약 5°).
        # 트림을 안 넣으면 "직진" 명령이 계속 우측으로 밀려 코스를 이탈한다.
        #
        # ⚠ 이건 기계 오정렬의 소프트웨어 우회다. 정답은 서보 혼·타이로드를
        #   다시 트림해 0 = 직진으로 맞추는 것이다. 트림은 그만큼의 좌조향
        #   여유를 먹는다 (좌측이 이미 약한 쪽이라 손해가 크다).
        self.declare_parameter('steer_trim_cdeg', 0)
        # ── yaw 출처 ─────────────────────────────────────────
        # 'auto' : 0x83 IMU quaternion 을 쓸 수 있으면 그걸, 아니면 0x85 폴백
        # 'imu'  : 0x83 강제 (못 쓰면 WARN 후 폴백)
        # 'odom' : 0x85 강제 (예전 동작. 회귀 비교·롤백용)
        #
        # 기본이 auto 인 이유 — 2026-08-05 실측:
        #   0x83 quaternion  -114.7°  (gyro_z 적분 -116.0° 와 1.1% 일치)
        #   0x85 융합/모델   -146.2°  = 1.275 배 부풀려짐
        # 0x85 로 90° 를 판정하면 물리적으로 71° 에서 멈춘다.
        self.declare_parameter('yaw_source', 'auto')
        # ── `/stm32/odom` 의 pose 를 무엇으로 채우나 ──────────
        # 'tick'  : **주행이 그 tick 에 실제로 쓴 값** (0x83 yaw + 엔코더 거리
        #           재적분). 관제 지도가 차와 같은 것을 보게 된다.
        # 'stm32' : 0x85 가 준 x·y·yaw 를 그대로 (예전 동작. 롤백용)
        #
        # ⚠ 'tick' 에서는 **0x83 을 못 쓰면 발행하지 않는다.** 주행은 0x85 로
        #   폴백해 계속 가지만(gather 는 안 건드린다), 웹으로는 안 보낸다 —
        #   두 yaw 는 원점이 달라서 섞이는 순간 지도의 로봇이 튄다.
        #   그래서 BNO085 가 빠졌거나 accuracy 가 낮으면 지도에 로봇이 안
        #   뜬다. 시연 중 그 상황이면 `odom_pose_source:=stm32` 로 되돌린다.
        self.declare_parameter('odom_pose_source', 'tick')

        g = self.get_parameter
        self.steer = SteeringConfig(
            min_cdeg=g('min_steering_cdeg').value,
            max_cdeg=g('max_steering_cdeg').value,
            kp_cross=g('kp_cross').value,
            kp_heading=g('kp_heading').value,
            kp_yaw_hold=g('kp_yaw_hold').value)
        if self.steer.min_cdeg >= 0 or self.steer.max_cdeg <= 0:
            raise RuntimeError(
                f'조향 한계가 잘못됐다: min={self.steer.min_cdeg} '
                f'max={self.steer.max_cdeg} (min<0<max 여야 한다)')
        self.steer_trim_cdeg = int(g('steer_trim_cdeg').value)
        if not self.steer.min_cdeg < self.steer_trim_cdeg < self.steer.max_cdeg:
            raise RuntimeError(
                f'steer_trim_cdeg={self.steer_trim_cdeg} 가 조향 한계 '
                f'{self.steer.min_cdeg}..{self.steer.max_cdeg} 안에 없다')
        self.cfg = RouteConfig(
            wheelbase_m=g('wheelbase_m').value,
            v_cruise=g('v_cruise_mm_s').value,
            v_approach=g('v_approach_mm_s').value,
            v_turn=g('v_turn_mm_s').value,
            leg_max_m=g('leg_max_m').value,
            command_stale_s=g('command_stale_s').value,
            v_turn_slow=g('v_turn_slow_mm_s').value,
            slow_remaining_deg=g('slow_remaining_deg').value,
            stop_lead_left_deg=g('stop_lead_left_deg').value,
            stop_lead_right_deg=g('stop_lead_right_deg').value,
            turn_fixed_steering_cdeg=g('turn_steering_cdeg').value,
            turn_max_m=g('turn_max_m').value,
            corner_trigger_right_m=g('corner_trigger_right_m').value,
            corner_trigger_left_m=g('corner_trigger_left_m').value,
            arrive_trigger_m=g('arrive_trigger_m').value,
            dock_trigger_m=g('dock_trigger_m').value,
            front_overhang_m=g('front_overhang_m').value,
            tag_lost_coast_max_m=g('tag_lost_coast_max_m').value,
            correction_limit_cdeg=g('correction_limit_cdeg').value,
            align_cross_m=g('align_cross_m').value,
            align_head_deg=g('align_head_deg').value,
            align_hold_s=g('align_hold_s').value,
            align_extra_m=g('align_extra_m').value,
            obstacle_clear_hold_s=g('obstacle_clear_hold_s').value,
            obstacle_ready_timeout_s=g('obstacle_ready_timeout_s').value,
            require_imu_fused=g('require_imu_fused').value,
            fusion_dropout_grace_s=g('fusion_dropout_grace_s').value)
        self.debug_obstacle_recovery = g('debug_obstacle_recovery').value
        self.yaw_source_mode = str(g('yaw_source').value).lower()
        if self.yaw_source_mode not in ('auto', 'imu', 'odom'):
            raise RuntimeError(
                f"yaw_source 는 auto|imu|odom 이어야 한다: "
                f"{self.yaw_source_mode!r}")
        self._yaw_source_logged = None
        self.odom_pose_source = str(g('odom_pose_source').value).lower()
        if self.odom_pose_source not in ('tick', 'stm32'):
            raise RuntimeError(
                f"odom_pose_source 는 tick|stm32 여야 한다: "
                f'{self.odom_pose_source!r}')
        if self.odom_pose_source == 'tick' and self.yaw_source_mode == 'odom':
            # 'tick' 은 0x83 을 요구하는데 yaw_source 가 0x85 강제면 게이트가
            # 영원히 닫힌다 — 지도에 로봇이 한 번도 안 뜬다. 조용히 그러느니
            # 뜨는 자리에서 막는다.
            raise RuntimeError(
                'odom_pose_source=tick 과 yaw_source=odom 은 같이 못 쓴다 '
                '(tick 은 0x83 이 있어야 발행한다). 0x85 로 지도를 그리려면 '
                'odom_pose_source:=stm32 로 두어라')
        self._dbg_obstacle_t = 0.0
        self._dbg_obstacle_was_active = False
        self._last_tx = None           # _forced_neutral 이후에 채운다
        self.cmd_stop_tx_count = 0     # CMD_STOP 송신 누적 (복구 무송신 증거)
        self.log_commands = g('log_commands').value
        self.log_min_interval_s = g('log_min_interval_s').value
        self.log_period_s = g('log_period_s').value
        self._logged_cmd = None
        self._logged_t = 0.0
        self.tag_stale_s = g('tag_stale_s').value
        self.telemetry_timeout_s = g('telemetry_timeout_s').value
        self.imu_stale_s = g('imu_stale_s').value

        self.machine = RouteMachine(self.cfg, self.steer)

        # ── 시리얼 ───────────────────────────────────────
        port = g('serial_port').value
        try:
            self.ser = serial.Serial(port, g('baud_rate').value, timeout=0)
        except serial.SerialException as e:
            self.get_logger().fatal(f'{port} 를 열 수 없다: {e}')
            raise
        # 열자마자 쌓여 있던 낡은 프레임을 버린다. 그대로 읽으면 옛 mcu_time 이
        # 재부팅 오탐을 만들고, 큰 read 가 파서 버퍼 상한에 걸려 noise 로
        # 집계된다.
        self.ser.reset_input_buffer()
        self.get_logger().info(f'serial open: {port}')
        if self.steer_trim_cdeg:
            # 트림은 서보를 맞춰주지만 STM32 오도메트리를 망친다. yaw 가
            # steering_source=COMMAND_ESTIMATE 로 계산되므로, STM32 는 전선에
            # 실린 값을 그대로 조향각으로 믿고 적분한다 — 직진(전선 = 트림값)을
            # 곡선으로 오해한다. 회전 판정은 0x83 IMU quaternion 을 쓰므로
            # 영향이 없지만, 0x85 yaw 와 yaw_source:=odom 폴백은 오염된다.
            drift = math.degrees(
                math.tan(math.radians(self.steer_trim_cdeg / 100.0))
                / self.cfg.wheelbase_m)
            # 트림과 같은 부호의 방향은 그만큼 조향 여유를 잃는다.
            side = '좌' if self.steer_trim_cdeg > 0 else '우'
            lim = (self.steer.max_cdeg if self.steer_trim_cdeg > 0
                   else self.steer.min_cdeg)
            eff = lim - self.steer_trim_cdeg
            self.get_logger().warn(
                f'조향 트림 {self.steer_trim_cdeg:+d} cdeg 적용 — 서보 중립은 '
                f'맞지만 STM32 COMMAND_ESTIMATE yaw 가 직진 중 약 '
                f'{drift:+.0f}°/m 드리프트한다 (0x85 오염). 회전 판정은 0x83 '
                f'을 쓰므로 무해 — yaw_source:=odom 으로 내리면 다시 문제가 '
                f'된다. {side}측 실효 최대 조향은 {eff:+d}cd'
                f'({eff / 100.0:+.2f}°) 로 줄어든다 (기계 재트림이 근본 해결)')

        self.parser = FrameParser()
        self.tx_seq = P.SeqCounter()
        self._seq_lock = threading.Lock()
        self._ser_lock = threading.Lock()

        # ── 공유 상태 ────────────────────────────────────
        self._lock = threading.Lock()
        self._cmd = NEUTRAL
        self._cmd_t = 0.0              # 마지막 명령 갱신 시각
        self._forced_neutral = False
        # 전선에 마지막으로 나간 프레임. /stm32/command 로 그대로 발행한다.
        # ★ steering_cdeg 는 **트림·clamp 적용 후** 값이다. route_logic 의
        #   command(=/route/state)는 트림 전 제어층 출력이라 다르다. 둘을
        #   같이 담아야 "트림이 얼마나 먹었나 / 포화했나" 를 숫자로 볼 수 있다.
        self._last_tx = self._tx_record(0, 0, 0, 0)
        self.drive = None              # 최신 TELEMETRY_DRIVE
        self.odom = None               # 최신 TELEMETRY_ODOMETRY
        self.imu = None                # 최신 TELEMETRY_IMU
        self.last_imu_t = None
        self.last_tel_t = None
        # ★ ID 별로 따로 담는다. tag_localizer_cv 는 **보이는 태그를 전부**
        #   /tag/target 에 발행한다 (선택 로직이 없다). 단일 슬롯에 받으면
        #   마지막에 도착한 프레임이 이긴다 — trackers 순서가 1→2→3→4 라
        #   코너에서 태그2·4 가 같이 보이면 **매 프레임 4 가 2 를 덮었다.**
        #   그러면 LEG1 은 목표(2)를 못 받아 횡보정이 영구히 꺼진다.
        self.tags = {}                 # {tag_id: (TagTarget, 수신시각)}
        self.stop_active = False
        # CMD_RESET_FAULT 진행 상태 (내일과제 §1.3). pending 중 telemetry 로
        # 요청 bit 해제를 확인해야 Jetson FAULT 를 푼다 — Jetson 만 IDLE 이고
        # STM32 는 fault 인 불일치를 막는다.
        self.reset_pending = False
        self.reset_requested_mask = 0
        self.reset_requested_at = None
        self._prev_faults = None

        # ── ROS ──────────────────────────────────────────
        self.create_subscription(String, '/tag/target', self.on_tag, 5)
        self.create_subscription(Bool, '/safety/stop_active',
                                 self.on_stop_active, 1)
        self.pub_state = self.create_publisher(String, '/route/state', 5)
        self.pub_tel = self.create_publisher(String, '/stm32/telemetry', 5)
        self.pub_odom = self.create_publisher(String, '/stm32/odom_json', 5)
        # 웹 관제(web_bridge)가 지도에 점을 찍는 값이 이것 하나다. JSON 쪽은
        # 진단·시험용이라 유지하고, 표준 메시지를 따로 낸다.
        self.pub_odom_msg = self.create_publisher(Odometry, '/stm32/odom', 10)
        self._odom_valid = None        # 유효/무효 전이 로그용
        self._odom_fused = None        # IMU 융합 여부 전이 로그용
        # tick 발행용 추측항법. 주행이 쓰는 값(0x83 yaw + 엔코더 거리)으로
        # 위치를 다시 세운다 (`_publish_odom_tick` 설명).
        self._dr = DeadReckon()
        self._dr_gate = None           # 발행 게이트 전이 로그용
        # IMU. 위치·방향에는 쓰지 않는다 (0x85 만 쓴다). 센서 상태 보고용이다.
        self.pub_imu = self.create_publisher(Imu, '/stm32/imu', 10)
        self.pub_imu_json = self.create_publisher(String, '/stm32/imu_json', 5)
        self.imu_frame_id = g('imu_frame_id').value
        self._imu_healthy = None
        self.pub_connected = self.create_publisher(Bool, '/stm32/connected', 1)
        # Jetson -> STM32 방향. 나머지 /stm32/* 는 전부 받는 쪽이라 "우리가
        # 무엇을 보냈는가" 는 텍스트 로그에만 있었다. 궤적을 나중에 해석하려면
        # 명령 이력이 토픽으로 남아야 한다 (rosbag 에 이거 하나면 충분하다).
        self.pub_cmd = self.create_publisher(String, '/stm32/command', 5)

        self.create_service(Trigger, '/route/start', self.srv_start)
        self.create_service(Trigger, '/route/return', self.srv_return)
        self.create_service(Trigger, '/route/approach', self.srv_approach)
        self.create_service(Trigger, '/route/turn_left', self.srv_turn_left)
        self.create_service(Trigger, '/route/turn_right',
                            self.srv_turn_right)
        self.create_service(Trigger, '/route/abort', self.srv_abort)
        self.create_service(Trigger, '/route/reset_fault', self.srv_reset)

        self._stop_threads = threading.Event()
        threading.Thread(target=self._rx_loop, daemon=True).start()
        threading.Thread(target=self._tx_loop, daemon=True).start()
        self.create_timer(1.0 / TICK_HZ, self.tick)
        self.create_timer(0.5, self.publish_status)

    # ── 송신 ─────────────────────────────────────────────
    def _next_seq(self) -> int:
        with self._seq_lock:
            return self.tx_seq.next()

    def _write(self, frame: bytes):
        """serial write 를 한 곳으로 모은다. 두 스레드가 동시에 쓰면 바이트가
        섞여 프레임이 손상된다."""
        with self._ser_lock:
            try:
                self.ser.write(frame)
            except serial.SerialException as e:
                self.get_logger().error(f'serial write 실패: {e}',
                                        throttle_duration_sec=1.0)

    def set_command(self, cmd):
        if not cmd.enable and (cmd.speed_mm_s or cmd.steering_cdeg):
            self.get_logger().error(
                f'잘못된 명령 (enable=0 인데 비영): {cmd}. neutral 로 대체')
            cmd = NEUTRAL
        if self.log_commands:
            self._log_command(cmd)
        with self._lock:
            self._cmd = cmd
            self._cmd_t = time.monotonic()

    def _log_command(self, cmd):
        """STM32 로 나가는 명령을 터미널에 찍는다. 속도 제한이 붙어 있다.

        규칙 세 가지다.
          - enable 이 바뀌면 무조건 즉시 찍는다. 출발·정지는 놓치면 안 된다.
          - 그 외의 변화는 log_min_interval_s 안에 두 번 찍지 않는다.
            억눌린 변화는 다음 기회에 그때의 최신값으로 나간다.
          - 아무 변화가 없어도 log_period_s 마다 한 줄 찍는다. 살아 있다는
            신호이자, 정속 구간에서도 지금 값을 확인할 수 있게 한다.
        """
        now = time.monotonic()
        prev = self._logged_cmd
        since = now - self._logged_t
        armed = prev is None or cmd.enable != prev.enable
        if not armed:
            if cmd != prev:
                if since < self.log_min_interval_s:
                    return                      # 억눌림. 다음 tick 에 다시 온다
            elif since < self.log_period_s:
                return                          # 변화 없음. 심장박동 대기
        self._logged_cmd = cmd
        self._logged_t = now

        pair = self._tag_for_step()
        want = self._target_tag_id()
        others = [k for k in self._fresh_tag_ids() if k != want]
        with self._lock:
            drive = self.drive
            odom = self.odom
            imu = self.imu

        # ① 지금 어느 단계인가, 그 안에서 얼마나 갔나
        #    멈춘 이유는 전이 순간에만 찍히면 스크롤로 흘러가버린다. 정지는
        #    "정상 완료" 와 "고장" 이 똑같이 en=0 으로 보이므로, 왜 멈췄는지를
        #    매 줄에 붙여 놓는다.
        st = self.machine.state
        step = self.machine.step
        if st == FAULT:
            where = f"[FAULT] {self.machine.fault_reason}"
        elif st in ('ARRIVED', 'DOCKED'):
            where = f"[{st}] 미션 정상 완료 — 다시 하려면 /route/* 재호출"
        elif st == 'HOLD':
            where = "[HOLD] 정지요청·장애물 — 해제되면 자동 재개"
        elif step is None:
            where = f"[{st}]"
        elif step.kind == 'turn':
            where = (f"[{st}/{step.name}] "
                     f"{self.machine.turned_deg:+6.1f}/"
                     f"{step.turn_deg:+.0f}deg")
        else:
            goal = f"/{step.max_m:.1f}" if step.max_m else ''
            where = (f"[{st}/{step.name}] "
                     f"{self.machine.traveled_m:5.2f}{goal}m")

        # ② 무엇을 보내는가. 숫자만 있으면 우회전인지 좌회전인지 한눈에
        #    안 들어오므로 말로도 찍는다.
        out = (f"{_motion(cmd, step):<20} {cmd.speed_mm_s:>+5}mm/s "
               f"(str={cmd.steering_cdeg:>+6}cd "
               f"{cmd.steering_cdeg / 100.0:>+6.2f}deg en={int(cmd.enable)})")
        # 트림이 걸려 있으면 전선에 실제로 나가는 값도 같이 보여준다.
        # 제어 출력만 보고 "0 인데 왜 휘냐" 로 헤매는 것을 막는다.
        if self.steer_trim_cdeg and cmd.enable:
            want = cmd.steering_cdeg + self.steer_trim_cdeg
            wire = max(self.steer.min_cdeg,
                       min(self.steer.max_cdeg, want))
            # 포화하면 str= 의 각도는 실제로 나가지 않았다. 표시하지 않으면
            # "18° 로 돌렸는데 반경이 왜 이러냐" 로 헤맨다.
            sat = '' if wire == want else ',포화'
            out += (f" wire={wire:>+6}cd({wire / 100.0:+.2f}deg"
                    f" trim{self.steer_trim_cdeg:+}{sat})")

        # ③ STM32 가 실제로 무엇을 하고 있는가. 명령과 실측이 갈리면
        #    (예: spd=+150 인데 meas=0) 바퀴가 안 도는 것이다 — 이 대비가
        #    없으면 터미널만 보고는 알 수 없다.
        if drive is None:
            act = "STM32 --(무응답)"
        else:
            sname = P.STATE_NAMES.get(drive['drive_state'], '?')
            act = (f"STM32 {sname} meas={drive['measured_speed_mm_s']:>+5}mm/s "
                   f"duty={drive['motor_duty_permille']:>+5}‰ "
                   f"strfb={drive['steering_feedback_cdeg']:>+6}cd")
            if drive['active_fault_bits']:
                act += f" FAULT={P.describe_faults(drive['active_fault_bits'])}"
            if odom:
                # IMU 융합 여부를 붙인다. yaw 정확도가 여기 달려 있고,
                # U턴 성공 여유가 10도 뿐이라 눈으로 확인할 수 있어야 한다.
                fuse = 'fused' if odom['status_flags'] & P.ODOM_IMU_FUSED \
                    else 'model'
                act += (f" odom={odom['distance_mm'] / 1000.0:6.2f}m "
                        f"yaw={odom['yaw_mdeg'] / 1000.0:+7.1f}deg({fuse})")
                # ★ 실제 회전 판정에 쓰는 yaw. 0x85 와 다를 수 있고, 그
                #   차이가 곧 모델 오염의 크기다.
                if (imu is not None
                        and P.imu_yaw_usable(imu['status_flags'],
                                             imu['quaternion_accuracy'])
                        and self.yaw_source_mode != 'odom'):
                    act += f" YAW={imu['yaw_mdeg'] / 1000.0:+7.1f}deg(imu)"
                else:
                    act += ' YAW=(odom 폴백)'
            if imu is not None:
                ok = 'OK' if P.imu_healthy(imu['status_flags']) else 'BAD'
                act += (f" imu={ok}/g{imu['gyro_accuracy']}"
                        f" gz={imu['gyro_z_mdeg_s'] / 1000.0:+7.2f}deg/s")

        # ④ 태그가 보이는가
        if pair is not None and pair[0].valid:
            t = pair[0]
            # along 은 뒷차축 기준, front 는 앞머리 기준. 판정에 쓰는 값은
            # along 이지만 눈으로 보는 여유는 front 라서 둘 다 찍는다.
            tag = (f"TAG{t.tag_id} along={t.along:.2f}m"
                   f"(앞머리 {t.along - self.cfg.front_overhang_m:+.2f}m) "
                   f"cross={t.cross_track:+.3f}m "
                   f"head={math.degrees(t.heading_error):+.1f}deg")
        else:
            tag = "TAG --"
        # ★ drive 단계에서 **횡보정이 켜져 있는지** 를 찍는다. servo 는
        #   cross_track 을 0 으로 만들지만 hold 는 그걸 무시하고 진입 yaw 만
        #   유지한다. 이 표시가 없으면 hold 로 떨어진 것과 "보정할 필요가
        #   없다고 판단한 것" 을 구분할 수 없다.
        if step is not None and step.kind == 'drive':
            mode = self.machine.drive_mode
            if mode == 'servo':
                tag += "  [횡보정 ON]"
            elif mode == 'hold':
                tag += (f"  [횡보정 OFF — 태그{step.tag_id} 미확보, "
                        f"heading 유지만]")
            # 목표는 못 보는데 다른 태그는 보이는 상황을 드러낸다. 이게
            # 안 보이면 "카메라가 아무것도 못 본다" 와 구분되지 않는다.
            if others:
                tag += f" (그밖에 보이는 태그 {others} — 무시)"

        self.get_logger().info(f"{where} | {out} | {act} | {tag}")

    def _tx_loop(self):
        period = 1.0 / TX_HZ
        nxt = time.monotonic()
        while not self._stop_threads.is_set():
            now = time.monotonic()
            if now >= nxt:
                with self._lock:
                    cmd, cmd_t = self._cmd, self._cmd_t
                # ★ 자체 watchdog. 상태머신이 멈추면 마지막 명령을 무한히
                #    반복하게 되므로 여기서 끊는다. STM32 watchdog 은 프레임이
                #    계속 오니까 걸리지 않는다.
                stale = (cmd_t > 0.0
                         and now - cmd_t > self.cfg.command_stale_s)
                if stale and not cmd.is_neutral:
                    if not self._forced_neutral:
                        self._forced_neutral = True
                        self.get_logger().error(
                            f'상태머신이 {now - cmd_t:.2f}s 동안 명령을 '
                            '갱신하지 않았다 — neutral 강제')
                    cmd = NEUTRAL
                elif not stale:
                    self._forced_neutral = False
                # ★ 조향 중립 트림을 여기서, 딱 한 번 더한다. 제어층
                #   (route_logic/tag_geometry)은 0 = 직진을 가정하고 계산하고,
                #   기계 오정렬 보정은 전선 나가기 직전에만 얹는다. clamp_steering
                #   안에 넣으면 route_logic 이 이중 clamp 하는 지점에서 트림이
                #   두 번 더해진다.
                #   enable=0 프레임은 건드리지 않는다 — set_command 가
                #   "enable=0 인데 비영" 을 거부하므로 arming 이 깨진다.
                steer_out = cmd.steering_cdeg
                if cmd.enable and self.steer_trim_cdeg:
                    want = steer_out + self.steer_trim_cdeg
                    steer_out = max(self.steer.min_cdeg,
                                    min(self.steer.max_cdeg, want))
                    if steer_out != want:
                        # 트림이 요청 조향을 한계 밖으로 밀어냈다. 서보는
                        # 한계에 붙어 있으므로 실제 곡률이 계획값보다 작다.
                        # 트림과 같은 부호의 방향이 먼저 포화한다 —
                        # 곡률 LUT 를 뜰 때 그 방향 상단 노드가 겹쳐버린다.
                        limit = (self.steer.max_cdeg if want > steer_out
                                 else self.steer.min_cdeg)
                        self.get_logger().warn(
                            f'조향 포화 — 요청 {cmd.steering_cdeg:+d}cd + 트림 '
                            f'{self.steer_trim_cdeg:+d} = {want:+d}cd 가 한계 '
                            f'{limit:+d}cd 를 넘어 잘렸다. 이 방향 실효 최대는 '
                            f'{limit - self.steer_trim_cdeg:+d}cd '
                            f'({(limit - self.steer_trim_cdeg) / 100.0:+.2f}°)',
                            throttle_duration_sec=5.0)
                # SEQ 를 먼저 뽑아 기록한다 — 진단 로그가 "우리가 방금 보낸
                # SEQ" 와 telemetry 의 last_drive_seq 를 짝지어 볼 수 있어야
                # 한다 (문서 §7).
                seq = self._next_seq()
                # 기록만 하고 발행은 하지 않는다 — 이 루프는 안전장치라
                # (자체 watchdog + 20 Hz 송신) rmw 지연을 송신 주기에
                # 실으면 안 된다. 발행은 상태 타이머가 한 tick 뒤에 한다.
                self._last_tx = self._tx_record(
                    cmd.speed_mm_s, steer_out, int(cmd.enable), seq,
                    cmd.steering_cdeg)
                self._write(P.pack_cmd_drive(
                    seq, cmd.speed_mm_s, steer_out, cmd.enable))
                nxt += period
                if nxt <= now:          # 밀림 보정 (캐치업 폭주 방지)
                    nxt = now + period
            time.sleep(0.002)

    def _tx_record(self, speed: int, steer_out: int, enable: int, seq: int,
                   control_steer: int = 0, kind: str = 'drive',
                   reason: int = -1) -> dict:
        """전선에 나간 프레임 한 개를 진단용 dict 로 만든다."""
        # saturated 를 여기서 계산해 두는 이유: 소비자가 트림·한계를 다시
        # 알아야 판정할 수 있게 만들면 규약이 갈린다. 포화는 "요청한 곡률이
        # 나가지 않았다" 는 뜻이라 궤적 해석에 직접 쓰인다.
        want = control_steer + self.steer_trim_cdeg
        rec = {
            'kind': kind,
            'seq': seq,
            'speed_mm_s': speed,
            'steering_cdeg': steer_out,          # ★ 전선 실제값
            'steering_deg': round(steer_out / 100.0, 2),
            'enable': enable,
            'control_steering_cdeg': control_steer,   # 트림 전 제어층 출력
            'trim_cdeg': self.steer_trim_cdeg,
            'saturated': bool(enable and self.steer_trim_cdeg
                              and steer_out != want),
            'forced_neutral': bool(self._forced_neutral),
            # 키는 **항상** 있어야 한다. tick 이 신선한 값으로 덮어쓰고,
            # CMD_STOP 경로는 None 으로 남는다 (그 시점 ack 는 의미가 없다).
            # 없는 키로 두면 소비자가 kind 마다 다르게 방어해야 한다.
            'mcu_ack_seq': None,
        }
        if reason >= 0:
            rec['reason'] = reason
        return rec

    def _check_reset_result(self, drive: dict, now: float):
        """reset pending 중 telemetry 로 해제를 확인한다 (내일과제 §1.3-6)."""
        if not self.reset_pending:
            return
        remaining = drive['active_fault_bits'] & self.reset_requested_mask
        if remaining == 0:
            self.reset_pending = False
            self.reset_requested_mask = 0
            self.reset_requested_at = None
            jet = self.machine.reset_fault()   # FAULT 였다면 IDLE 로
            self.set_command(NEUTRAL)          # 자동 재출발 금지
            self.get_logger().info(
                'STM32 fault 해제 확인'
                + (' — Jetson FAULT 도 해제, IDLE' if jet else '')
                + '. 재출발은 별도 명령으로')
            return
        if now - self.reset_requested_at >= float(
                self.get_parameter('reset_timeout_s').value):
            self.reset_pending = False
            self.get_logger().error(
                f'STM32 fault reset 시간초과 — 잔여: '
                f'[{P.describe_faults(remaining)}]. STM32 쪽 원인 확인 필요')

    def send_reset_fault(self, fault_mask: int):
        """CMD_RESET_FAULT(0x12) 송신. 주행 재시작 명령이 아니다 —
        보내기 전 neutral 로 고정하고, 해제 확인은 telemetry 로 한다."""
        if not fault_mask:
            return
        self.set_command(NEUTRAL)
        self._write(P.pack_cmd_reset_fault(self._next_seq(), fault_mask))

    def send_stop(self, reason: int):
        """CMD_STOP 송신. **장애물 HOLD·복구 경로에서는 호출되지 않는다.**

        호출 지점은 네 곳뿐이다 — FAULT 전이, 미션 완료 전이, `/route/abort`,
        노드 종료. 장애물 복구는 FAULT 로 전이하지 않으므로(그게 이번 수정의
        핵심) 자동 경로에서 CMD_STOP 이 나갈 길이 없다. `CMD_STOP` 은 STM32 의
        `rearm_required` 를 다시 세워서 복구를 스스로 막기 때문이다.

        누적 횟수를 남긴다 — 현장에서 "복구 중 STOP 안 나갔다" 를 로그로
        확인할 수 있어야 한다.
        """
        self.cmd_stop_tx_count += 1
        st = self.machine.status()
        self.get_logger().warn(
            f'CMD_STOP 송신 #{self.cmd_stop_tx_count} reason={reason} '
            f"(state={st['state']} hold={st['hold_reason'] or '-'}"
            f"/{st['hold_substate'] or '-'})")
        seq = self._next_seq()
        self._write(P.pack_cmd_stop(seq, reason))
        # CMD_STOP 도 같은 토픽에 남긴다. 20 Hz drive 스트림에 드물게 섞이므로
        # kind 로 구분한다 — 왜 멈췄는지를 로그 없이 재구성할 수 있어야 한다.
        try:
            self.pub_cmd.publish(String(data=json.dumps(
                self._tx_record(0, 0, 0, seq, kind='stop', reason=reason))))
        except Exception:                                   # noqa: BLE001
            pass    # 종료 중이면 컨텍스트가 이미 닫혀 있다 (진단용이라 무해)

    # ── 수신 ─────────────────────────────────────────────
    def _rx_loop(self):
        while not self._stop_threads.is_set():
            try:
                d = self.ser.read(4096)
            except serial.SerialException as e:
                self.get_logger().error(f'serial read 실패: {e}',
                                        throttle_duration_sec=1.0)
                time.sleep(0.05)
                continue
            if d:
                for mid, _seq, pl in self.parser.feed(d):
                    self._on_frame(mid, pl)
            self.parser.check_timeout()
            time.sleep(0.002)

    def _on_frame(self, mid: int, pl: bytes):
        try:
            if mid == P.TELEMETRY_DRIVE:
                t = P.unpack_telemetry(pl)
                with self._lock:
                    self.drive = t
                    self.last_tel_t = time.monotonic()
                self.pub_tel.publish(String(data=json.dumps(t)))
                self._report_faults(t['active_fault_bits'])
                self._check_reset_result(t, time.monotonic())
            elif mid == P.TELEMETRY_ODOMETRY:
                o = P.unpack_telemetry_odometry(pl)
                with self._lock:
                    self.odom = o
                self.pub_odom.publish(String(data=json.dumps(o)))
                # 'tick' 모드에서는 여기서 안 낸다 — `tick` 이 주행이 쓴 값으로
                # 낸다(`_publish_odom_tick`). rx 스레드에서 ROS 발행을 줄이는
                # 것 자체가 목적이기도 하다: 이 루프가 rmw 를 기다리는 동안
                # `ser.read()` 가 멈추고, 여기서 예외가 나면 수신이 통째로
                # 죽는다(`_on_frame` 은 ValueError·KeyError 만 잡는다).
                if self.odom_pose_source == 'stm32':
                    self._publish_odom_msg(o)
            elif mid == P.TELEMETRY_IMU:
                m = P.unpack_telemetry_imu(pl)
                with self._lock:
                    self.imu = m
                    self.last_imu_t = time.monotonic()
                self.pub_imu_json.publish(String(data=json.dumps(m)))
                self._publish_imu_msg(m)
            elif mid == P.FAULT_EVENT:
                f = P.unpack_fault_event(pl)
                self.get_logger().warn(
                    f"FAULT_EVENT state="
                    f"{P.STATE_NAMES.get(f['drive_state'], '?')} "
                    f"action={f['fault_action']} "
                    f"active={P.describe_faults(f['active_fault_bits'])}")
            elif mid == P.COMMAND_RESULT:
                r = P.unpack_command_result(pl)
                self.get_logger().info(
                    f"COMMAND_RESULT req=0x{r['request_msg_id']:02X} "
                    f"result={r['result_code']}")
        except (ValueError, KeyError) as e:
            self.get_logger().warn(f'0x{mid:02X} 해석 실패: {e}',
                                   throttle_duration_sec=5.0)

    def _publish_odom_tick(self, inp):
        """`/stm32/odom` 을 **주행이 이 tick 에 쓴 값**으로 낸다 (기본 경로).

        ## 무엇이 실리나

        - yaw   : `inp.yaw_rad` — 회전 판정에 쓴 그 값(0x83 IMU quaternion)
        - x·y   : 엔코더 누적거리 + 그 yaw 로 재적분 (`DeadReckon`)
        - 선속도: `inp.speed_mm_s` (0x85 엔코더. 값이 기존과 같다)
        - 각속도: **0x85 `yaw_rate` 그대로.** 주행이 안 쓰는 값이라 손대지
          않는다. 순간값이라 적분 오염과도 무관하다.

        ## 왜 x·y 를 다시 세우나

        STM32 의 x·y 는 **자기 융합 yaw 로 적분한 값**이다. 그 yaw 가 부풀려져
        있으면(2026-08-05 실측 1.275 배) 위치도 같이 틀어진다. 특히 조향 트림이
        걸리면 STM32 는 전선에 실린 트림값을 조향각으로 믿고 **직진을 곡선으로
        오해한다** (트림 +500 에서 약 37 deg/m). 그래서 yaw 만 바꾸고 x·y 를
        그대로 두면 방향과 위치가 서로 다른 좌표계가 된다.

        ## 게이트 — 폴백하지 않는다

        `odom_valid`(0x85 pose 신뢰)와 `yaw_source == 'imu'`(0x83 사용 중)가
        **둘 다** 참일 때만 낸다. 주행은 0x83 을 못 쓰면 0x85 로 폴백해 계속
        가지만(`gather` 는 안 건드린다), 웹으로는 안 보낸다 — 두 yaw 는 원점이
        달라서 섞이는 순간 지도의 로봇이 튀고, 그 뒤 적분이 전부 틀어진다.

        게이트가 닫히면 적분을 멈추되 위치는 버리지 않는다(`DeadReckon.pause`).
        """
        gate = bool(inp.odom_valid and inp.yaw_source == 'imu')
        if gate != self._dr_gate:
            self._dr_gate = gate
            if gate:
                self.get_logger().info(
                    '/stm32/odom 발행 시작 — 주행이 쓰는 값 '
                    '(0x83 yaw + 엔코더 재적분)')
            else:
                self.get_logger().warn(
                    f'/stm32/odom 중단 (odom_valid={inp.odom_valid} '
                    f"yaw_source={inp.yaw_source or '없음'}). 주행은 계속하지만 "
                    '웹 지도에 위치가 갱신되지 않는다')
        if not gate:
            self._dr.pause()
            return

        x, y, yaw = self._dr.update(inp.distance_m, inp.yaw_rad)
        with self._lock:
            odom = self.odom
        m = Odometry()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = ODOM_FRAME
        m.child_frame_id = BASE_FRAME
        m.pose.pose.position.x = x
        m.pose.pose.position.y = y
        qx, qy, qz, qw = yaw_to_quaternion(yaw)
        m.pose.pose.orientation.x = qx
        m.pose.pose.orientation.y = qy
        m.pose.pose.orientation.z = qz
        m.pose.pose.orientation.w = qw
        m.twist.twist.linear.x = inp.speed_mm_s / 1000.0
        # 각속도만 0x85 원본에서 가져온다 (위 docstring). odom 이 아직 안 왔으면
        # 0 으로 둔다 — 게이트가 열렸다는 건 이미 왔다는 뜻이라 사실상 안 걸린다.
        m.twist.twist.angular.z = (
            math.radians(odom['yaw_rate_mdeg_s'] / 1000.0) if odom else 0.0)
        # covariance 는 0 으로 둔다. ROS 관례에서 전부 0 은 "모름" 이다.
        self.pub_odom_msg.publish(m)

    def _publish_odom_msg(self, o: dict):
        """오도메트리를 nav_msgs/Odometry 로 낸다. **유효할 때만 낸다.**

        ⚠ `odom_pose_source:=stm32` 일 때만 쓰는 **롤백 경로**다. 기본값은
        `tick` 이고 그때는 `_publish_odom_tick` 이 낸다. 이 함수는 0x85 가 준
        x·y·yaw 를 그대로 싣는다 — 조향 트림이 걸려 있으면 위치가 휜다.

        Odometry 에는 "이 값 믿어도 되나" 를 담을 칸이 없다. 그런데 서버는
        받은 위치를 인메모리 최신값으로 들고 있어서, 한 번 잘못 박히면 다음
        유효 보고가 올 때까지 지도에 그대로 남는다. 틀린 위치보다 없는
        위치가 낫다 — 무효면 발행하지 않는다.
        """
        valid = P.odom_pose_valid(o['status_flags'], o['steering_source'])
        if valid != self._odom_valid:
            self._odom_valid = valid
            if valid:
                self.get_logger().info('오도메트리 유효 — /stm32/odom 발행 시작')
            else:
                self.get_logger().warn(
                    '오도메트리 무효 — /stm32/odom 중단 '
                    f"(flags=0x{o['status_flags']:04X} "
                    f"steering_source={o['steering_source']}). "
                    '웹 지도에 위치가 갱신되지 않는다')
        if not valid:
            return

        # IMU 융합 여부는 degraded 상태 표시용이다. **판정 조건이 아니다** —
        # IMU 가 없어도 STM32 는 엔코더+조향 모델로 자동 복귀하므로 주행은
        # 계속한다 (인수인계 2026-08-03 §5).
        fused = bool(o['status_flags'] & P.ODOM_IMU_FUSED)
        if fused != self._odom_fused:
            self._odom_fused = fused
            if fused:
                self.get_logger().info('오도메트리 IMU 융합 시작 (IMU_FUSED=1)')
            else:
                self.get_logger().warn(
                    '오도메트리 IMU 융합 해제 (IMU_FUSED=0) — '
                    '엔코더+조향 모델로 degraded 주행. yaw 정확도가 낮아진다')

        si = odom_si(o)
        m = Odometry()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = ODOM_FRAME
        m.child_frame_id = BASE_FRAME
        m.pose.pose.position.x = si['x']
        m.pose.pose.position.y = si['y']
        qx, qy, qz, qw = si['quat']
        m.pose.pose.orientation.x = qx
        m.pose.pose.orientation.y = qy
        m.pose.pose.orientation.z = qz
        m.pose.pose.orientation.w = qw
        m.twist.twist.linear.x = si['vx']
        m.twist.twist.angular.z = si['wz']
        # covariance 는 0 으로 둔다. ROS 관례에서 전부 0 은 "모름" 이다.
        # 추측항법 불확실도를 모델링하지 않았으므로 숫자를 지어내지 않는다.
        self.pub_odom_msg.publish(m)

    def _publish_imu_msg(self, d: dict):
        """TELEMETRY_IMU 를 sensor_msgs/Imu 로 낸다.

        ⚠ **이 값은 위치·방향 판단에 쓰지 않는다.** 위치와 yaw 는 0x85 만
          쓴다. STM32 가 이미 gyro Z 를 엔코더+조향 모델과 융합해서 0x85 에
          실어 보내므로, 여기서 다시 융합하면 이중 융합이 된다.
          이 토픽은 센서 건강 상태 보고와 모니터링용이다.

        유효하지 않은 항목은 covariance[0] = -1 로 표시한다. ROS 관례에서
        그것이 "이 항목은 사용 불가" 를 뜻한다. 값을 그냥 실으면 소비자가
        쓰레기를 믿는다.
        """
        flags = d['status_flags']
        healthy = P.imu_healthy(flags)
        if healthy != self._imu_healthy:
            self._imu_healthy = healthy
            txt = (f"status={P.describe_imu_status(flags)} "
                   f"accuracy=gyro:{d['gyro_accuracy']}"
                   f"/quat:{d['quaternion_accuracy']}")
            if healthy:
                self.get_logger().info(f'IMU 정상 — {txt}')
            else:
                self.get_logger().warn(
                    f'IMU 이상 — {txt}. 주행은 계속한다 '
                    '(STM32 가 엔코더+조향 모델로 복귀)')

        si = imu_si(d)
        m = Imu()
        m.header.stamp = self.get_clock().now().to_msg()
        m.header.frame_id = self.imu_frame_id
        qx, qy, qz, qw = si['quat']
        m.orientation.x, m.orientation.y = qx, qy
        m.orientation.z, m.orientation.w = qz, qw
        m.angular_velocity.x, m.angular_velocity.y, m.angular_velocity.z = \
            si['gyro']
        m.linear_acceleration.x, m.linear_acceleration.y, \
            m.linear_acceleration.z = si['accel']
        # game rotation vector 는 자기장을 안 쓴다 — 절대 북쪽 방위가 아니다.
        # heading 으로 쓰면 안 되므로 유효성 표시를 정확히 붙인다.
        if not flags & P.IMU_QUATERNION_VALID:
            m.orientation_covariance[0] = -1.0
        if not flags & P.IMU_GYRO_VALID:
            m.angular_velocity_covariance[0] = -1.0
        if not flags & P.IMU_LINEAR_ACCEL_VALID:
            m.linear_acceleration_covariance[0] = -1.0
        self.pub_imu.publish(m)

    def _report_faults(self, bits: int):
        if bits == self._prev_faults:
            return
        prev = self._prev_faults or 0
        self._prev_faults = bits
        if prev == 0 and bits == 0:
            return
        gained, lost = bits & ~prev, prev & ~bits
        parts = []
        if gained:
            parts.append(f'+[{P.describe_faults(gained)}]')
        if lost:
            parts.append(f'-[{P.describe_faults(lost)}]')
        self.get_logger().warn(f'fault 0x{bits:08X} ' + ' '.join(parts))

    # ── 구독 콜백 ────────────────────────────────────────
    def on_tag(self, msg: String):
        try:
            t = tag_target_from_dict(json.loads(msg.data))
        except (json.JSONDecodeError, TypeError) as e:
            self.get_logger().warn(f'/tag/target 해석 실패: {e}',
                                   throttle_duration_sec=5.0)
            return
        # 무효 검출은 담지 않는다. "아무것도 안 보인다" 프레임은 tag_id=0 으로
        # 오는데(course_sim), 그걸 저장하면 tags_visible 에 태그 0 이 끼고
        # 목표 미확보 판단도 흐려진다. 목표가 안 오면 gather 의 나이 검사가
        # 알아서 hold 로 떨어뜨린다 — 무효 프레임을 받아둘 필요가 없다.
        if not t.valid or not t.tag_id:
            return
        with self._lock:
            self.tags[t.tag_id] = (t, time.monotonic())

    def on_stop_active(self, msg: Bool):
        if msg.data != self.stop_active:
            self.get_logger().warn(f'/safety/stop_active -> {msg.data}')
        self.stop_active = msg.data

    # ── 입력 수집 ────────────────────────────────────────
    def _target_tag_id(self) -> int:
        """지금 단계가 향하는 태그 ID. drive 가 아니면 0."""
        step = self.machine.step
        return step.tag_id if step is not None and step.kind == 'drive' else 0

    def _fresh_tag_ids(self) -> list:
        """지금 실제로 보이는 태그 ID. 표시·진단용."""
        # self.tags 는 한 번 본 태그를 지우지 않으므로(제어 경로는 gather 에서
        # 나이를 검사한다) 목록을 그냥 내면 1분 전에 스친 태그도 "보임" 으로
        # 찍힌다. 그래서 여기서만 신선도로 거른다.
        now = time.monotonic()
        with self._lock:
            return sorted(k for k, (_, at) in self.tags.items()
                          if now - at <= self.tag_stale_s)

    def _tag_for_step(self):
        """현재 단계의 목표 태그만 꺼낸다. 다른 태그는 무시한다."""
        # 여기서 걸러야 하는 이유: 인지 노드는 미션을 모르므로 보이는 태그를
        # 전부 발행한다. 목표가 아닌 태그의 위치를 제어에 쓰면 코너에서
        # 태그4 를 보고 태그2 를 향해 조향하는 일이 생긴다.
        want = self._target_tag_id()
        if not want:
            return None
        with self._lock:
            return self.tags.get(want)

    def gather(self) -> Inputs:
        now = time.monotonic()
        with self._lock:
            drive, odom, tel_t = self.drive, self.odom, self.last_tel_t
            imu, imu_t = self.imu, self.last_imu_t
        tagpair = self._tag_for_step()
        inp = Inputs(now=now, stop_requested=self.stop_active)

        connected = (tel_t is not None
                     and now - tel_t <= self.telemetry_timeout_s)
        # 오래된 IMU 프레임으로 yaw 를 잡으면 회전이 그 시점에 멈춘 것처럼
        # 보여 과회전한다. **통신단절 문턱과 다른 값을 쓴다** — 주기가 같다는
        # 것(둘 다 20 Hz)과 허용 지연이 같다는 것은 다른 얘기다.
        imu_ok = (imu_t is not None
                  and now - imu_t <= self.imu_stale_s)
        inp.telemetry_ok = bool(drive and connected)
        inp.telemetry_age_s = (now - tel_t) if tel_t is not None else 0.0
        if drive and connected:
            faults = drive['active_fault_bits']
            obstacle = bool(faults & P.FAULT_OBSTACLE_NEAR)
            state = drive['drive_state']
            # OBSTACLE_NEAR 는 차단 fault 판정에서 분리한다 — 같이 넣으면
            # 장애물이 곧 FAULT 로 승격돼 HOLD·자동재개가 불가능해진다.
            # 의도를 마스크에 그대로 적는다 (문서 §3).
            other_blocking = P.arm_blocking_faults(
                faults & ~P.FAULT_OBSTACLE_NEAR)
            inp.obstacle = obstacle
            inp.arm_blocked = bool(other_blocking)
            # bit15 단독인가. 장애물 HOLD 중 초음파 측정이 순간 끊기면 bit16 이
            # 내려가고 이것이 대신 뜬다 (현장 로그 0x8100 = bit8+bit15). bit8
            # RANGE_LOST 는 REPORT_ONLY 라 `other_blocking` 에 애초에 없으므로
            # 같이 떠 있어도 이 판정은 참이다. 유예 여부·시간은 route_logic 이
            # 정한다 — 여기서는 bit → 뜻 변환만 한다.
            # ⚠ bit15 를 `arm_blocking_faults` 마스크에서 빼는 것이 아니다.
            #   전역으로 빼면 주행 중 센서 고장까지 조용히 무시된다.
            inp.sensor_stale_only = (other_blocking == P.FAULT_SENSOR_STALE)
            inp.fault_bits = faults
            inp.blocking_fault_bits = other_blocking
            inp.stm32_state_name = P.STATE_NAMES.get(state, f'?({state})')
            inp.last_drive_seq = drive['last_drive_seq']
            # rearm 완료 판정용 — READY(2) 정확히. DRIVING 을 포함하면
            # "아직 안 멈춘 것" 을 "복구됨" 으로 오판한다.
            inp.stm32_state_ready = (state == P.STATE_READY)
            # 장애물 때문에 STM32 가 SAFE_STOP 에 가 있는 것도 "준비됨" 으로
            # 본다 (HOLD 중 neutral 이 계속 나가므로 해제 즉시 rearm 된다).
            # ⚠ bit16 이 내려간 직후의 SAFE_STOP 은 이 예외에 걸리지 않는다 —
            #   그 구간은 route_logic 의 장애물 복구 하위상태가 담당한다.
            inp.stm32_ready = (
                state in (P.STATE_READY, P.STATE_DRIVING)
                or (obstacle and state == P.STATE_SAFE_STOP))
        if odom:
            # pose 를 쓰기 전 반드시 확인한다 (명세 §9.2)
            inp.odom_valid = P.odom_pose_valid(odom['status_flags'],
                                               odom['steering_source'])
            inp.yaw_rad = math.radians(odom['yaw_mdeg'] / 1000.0)
            inp.yaw_source = 'odom'
            inp.distance_m = odom['distance_mm'] / 1000.0
            inp.imu_fused = bool(odom['status_flags'] & P.ODOM_IMU_FUSED)
            inp.speed_mm_s = float(odom['linear_speed_mm_s'])
        # ★ yaw 는 **0x83 IMU quaternion** 을 우선한다 (2026-08-05 실측 근거).
        #   0x85 융합 yaw 는 모델 성분(조향 명령 기반 자전거모델) 25% 를 섞고,
        #   그 모델이 실제보다 크게 세서 융합값이 1.275 배 부풀려진다. 그 결과
        #   ODOM 90° 에서 멈추면 물리적으로 71° 밖에 안 돈다.
        #   거리(distance_m)는 그대로 0x85 를 쓴다 — 엔코더 적분이고 정확하다.
        if self.yaw_source_mode != 'odom' and imu is not None and imu_ok:
            if P.imu_yaw_usable(imu['status_flags'],
                                imu['quaternion_accuracy']):
                inp.yaw_rad = math.radians(imu['yaw_mdeg'] / 1000.0)
                inp.yaw_source = 'imu'
                # IMU yaw 를 직접 읽으므로 STM32 의 융합 여부는 무의미하다.
                # 이 플래그로 route_logic 의 IMU_FUSED 가드를 건너뛴다.
                inp.yaw_is_imu = True
            elif self.yaw_source_mode == 'imu':
                # 강제 모드인데 못 쓰는 상태 — 조용히 0x85 로 떨어지면
                # 원인을 모른 채 부정확한 회전을 하게 된다.
                self.get_logger().warn(
                    'yaw_source=imu 인데 0x83 quaternion 을 쓸 수 없다 '
                    f"(status=0x{imu['status_flags']:02X} "
                    f"quat_accuracy={imu['quaternion_accuracy']}) — "
                    '0x85 로 폴백. 회전각이 부풀려질 수 있다',
                    throttle_duration_sec=5.0)
        # 오래된 태그는 없는 것으로 취급한다. tag_localizer 가 죽어도
        # 마지막 값을 계속 믿으면 안 된다.
        if tagpair and now - tagpair[1] <= self.tag_stale_s:
            inp.tag = tagpair[0]
        return inp

    # ── 주기 처리 ────────────────────────────────────────
    def tick(self):
        inp = self.gather()
        prev_state = self.machine.state
        try:
            cmd = self.machine.tick(inp)
        except Exception as e:                              # noqa: BLE001
            self.get_logger().fatal(f'상태머신 예외: {e!r} — 중단')
            self.machine.abort(f'상태머신 예외: {type(e).__name__}')
            self.set_command(NEUTRAL)
            return
        # yaw 출처가 바뀌면 한 번 남긴다. 회전 정확도가 여기 달려 있어서
        # 로그만 보고도 어느 출처로 판정했는지 알 수 있어야 한다.
        if inp.yaw_source and inp.yaw_source != self._yaw_source_logged:
            self._yaw_source_logged = inp.yaw_source
            if inp.yaw_source == 'imu':
                self.get_logger().info(
                    'yaw 출처 = 0x83 IMU quaternion (트림·모델 오염 없음)')
            else:
                self.get_logger().warn(
                    'yaw 출처 = 0x85 융합/모델 — 모델 성분 때문에 회전각이 '
                    '부풀려질 수 있다 (실측 1.275배). 0x83 을 쓸 수 없는 상태다')
        self.set_command(cmd)
        # 관제 지도용 pose. **명령을 세운 뒤에 낸다** — 발행이 실패해도 이번
        # tick 의 주행 명령은 이미 나가 있어야 한다.
        #
        # ⚠ try 로 감싸는 이유: 여기서 예외가 새면 rclpy 타이머 콜백이 터져
        #   노드가 죽고, tx 스레드(daemon)까지 같이 내려가 STM32 watchdog 이
        #   차를 세운다. 웹 지도 한 장 때문에 주행이 멈추면 안 된다.
        if self.odom_pose_source == 'tick':
            try:
                self._publish_odom_tick(inp)
            except Exception as e:                          # noqa: BLE001
                self.get_logger().error(
                    f'/stm32/odom 발행 실패: {e!r} — 주행은 계속한다',
                    throttle_duration_sec=5.0)
        # 전선에 나간 마지막 CMD_DRIVE 를 tick 마다(20 Hz) 낸다. TX 루프에서
        # 직접 발행하지 않는 이유는 그 루프가 안전장치라서다 (§_tx_loop) —
        # rmw 지연을 송신 주기에 실으면 안 된다. publish_status(2 Hz)에 얹지
        # 않는 이유는 프레임당 하나가 목적이기 때문이다. 다른 /stm32/* JSON
        # 토픽도 이미 20 Hz 로 나가므로 부하 관점에서도 같은 급이다.
        #
        # mcu_ack_seq 가 핵심: 우리가 보낸 seq 와 STM32 가 받았다고 보고한
        # last_drive_seq 를 **한 메시지에서** 비교할 수 있어, 프레임이 실제로
        # 도착하는지 토픽 하나로 판정된다 (bit13 UART_RX_OVERFLOW 추적).
        tx = dict(self._last_tx)
        tx['mcu_ack_seq'] = inp.last_drive_seq if inp.telemetry_ok else None
        # 횡보정 검산용. 이 tick 이 낸 조향과 **그것을 만든 입력**을 같은
        # 메시지에 담는다. 진단이 태그 값을 따로 읽어 재계산하면 두 값이 최대
        # 한 tick 어긋나 전이 구간마다 수백 cdeg 차이가 나고, 그게 거짓
        # "게인 이상" 경고가 된다. 여기 있으면 검산이 항등식이 된다.
        si = self.machine.servo_input
        tx['servo_cross_m'] = None if si is None else round(si[0], 4)
        tx['servo_head_deg'] = (None if si is None
                                else round(math.degrees(si[1]), 2))
        # cmd 가 아니라 si[2] 를 쓴다 — 단계 전이 tick 에서 cmd 는 이미 다음
        # 단계의 명령이라 입력과 짝이 맞지 않는다.
        tx['servo_steering_cdeg'] = None if si is None else si[2]
        self.pub_cmd.publish(String(data=json.dumps(tx)))
        self._log_obstacle_recovery(inp, cmd)
        if self.machine.state != prev_state:
            self.get_logger().info(
                f'{prev_state} -> {self.machine.describe()}')
            if self.machine.state == FAULT:
                # FAULT 전이 1회 요약 (문서 §7). 원인 추적에 필요한 값을
                # 한 줄에 모아 둔다 — 20 Hz 상태줄에 밀려 올라가도 이건 남는다.
                self.get_logger().error(
                    f'FAULT 전이: reason={self.machine.fault_reason!r} '
                    f'stm32_state={inp.stm32_state_name or "?"} '
                    f'active_fault=0x{inp.fault_bits:08X} '
                    f'bit16={int(inp.obstacle)} '
                    f'other_blocking=0x{inp.blocking_fault_bits:08X} '
                    f'telemetry_age_ms={inp.telemetry_age_s * 1000:.0f} '
                    f'last_drive_seq={inp.last_drive_seq}')
                self.send_stop(P.STOP_INTERNAL)
            elif self.machine.state in ('ARRIVED', 'DOCKED'):
                self.send_stop(P.STOP_MISSION_COMPLETE)
                # 정렬 실패로 완료된 경우를 **반드시 드러낸다.** 거리만 맞고
                # 자세가 틀린 채 끝나면 로그상 성공과 구분되지 않는다.
                if self.machine.aligned is False:
                    cx, hd = self.machine.align_miss or (0.0, 0.0)
                    self.get_logger().warn(
                        f'정렬 미달로 완료 — cross {cx:+.3f} m / '
                        f'head {hd:+.1f}° (허용 '
                        f'{self.cfg.align_cross_m:.2f} m / '
                        f'{self.cfg.align_head_deg:.1f}°). 거리 하한까지 '
                        f'갔지만 자세가 맞지 않았다. 반복되면 kp_heading·'
                        f'카메라 캘리브레이션·태그 배치를 봐야 한다')
                elif self.machine.aligned:
                    self.get_logger().info('정렬 조건 만족하고 완료')
                # 상대 회전 완료 리포트 (JETSON_YAW90 §7 DONE):
                # stop lead 조정은 이 signed error 를 근거로 한다.
                if self.machine.mission.startswith('turn_'):
                    st = self.machine.status()
                    tgt = float(self.get_parameter('turn_target_deg').value)
                    sign = 1.0 if 'left' in self.machine.mission else -1.0
                    err = st['turned_deg'] - sign * tgt
                    self.get_logger().info(
                        f"회전 완료 — 최종 yaw {st['turned_deg']:+.1f}° "
                        f"(목표 {sign * tgt:+.0f}°, signed error {err:+.1f}°), "
                        f"이동 {st['traveled_m']:.3f} m. "
                        f"오버슈트가 반복되면 해당 방향 stop_lead 를 "
                        f"error 만큼 늘린다")
            self.publish_status()

    def _log_obstacle_recovery(self, inp: Inputs, cmd):
        """장애물 HOLD·복구 구간 한 줄 추적 (문서 §7).

        `debug_obstacle_recovery:=true` 일 때만 찍는다. 기본 off 인 이유는
        20 Hz 로 남기면 기존 상태줄이 전부 묻히기 때문이다. 켜면 50 ms 급
        (= tick 마다) 로 남겨 복구 구간을 프레임 단위로 재구성할 수 있다.
        """
        if not self.debug_obstacle_recovery:
            return
        st = self.machine.status()
        active = bool(st['hold_substate']) or inp.obstacle
        if not active:
            # 구간이 끝난 직후 한 줄만 더 남기고 조용해진다.
            if self._dbg_obstacle_was_active:
                self._dbg_obstacle_was_active = False
                self.get_logger().info(
                    f"[obstacle] 복구 종료 -> state={st['state']} "
                    f"step={st['step_index']} {st['step_name']} "
                    f"hold_elapsed_s={st['hold_elapsed_s']} "
                    f"cmd_stop_tx_total={self.cmd_stop_tx_count}")
            return
        self._dbg_obstacle_was_active = True
        m = self.machine
        clear_ms = (0.0 if m._obstacle_clear_since is None
                    else (inp.now - m._obstacle_clear_since) * 1000.0)
        ready_ms = (0.0 if m._ready_wait_since is None
                    else (inp.now - m._ready_wait_since) * 1000.0)
        tx = self._last_tx
        speed, steer = tx['speed_mm_s'], tx['steering_cdeg']
        enable, seq = tx['enable'], tx['seq']
        self.get_logger().info(
            f'[obstacle] t={inp.now:.3f} '
            f'active_fault=0x{inp.fault_bits:08X} bit16={int(inp.obstacle)} '
            f'other_blocking=0x{inp.blocking_fault_bits:08X} '
            f'obstacle_hold={int(m.hold_reason == "obstacle")} '
            f"substate={st['hold_substate'] or '-'} "
            f'clear_elapsed_ms={clear_ms:.0f} '
            f'ready_wait_elapsed_ms={ready_ms:.0f} '
            f'stm32_state={inp.stm32_state_name or "?"} '
            f'tx_speed={speed} tx_steering={steer} tx_enable={enable} '
            f'tx_seq={seq} last_drive_seq={inp.last_drive_seq} '
            f'cmd_stop_tx_total={self.cmd_stop_tx_count}')

    def publish_status(self):
        st = self.machine.status()
        now = time.monotonic()
        with self._lock:
            drive, tel_t = self.drive, self.last_tel_t
        connected = (tel_t is not None
                     and now - tel_t <= self.telemetry_timeout_s)
        st['stm32_connected'] = connected
        st['stm32_state'] = (P.STATE_NAMES.get(drive['drive_state'], '?')
                             if drive else 'unknown')
        st['stm32_faults'] = (P.describe_faults(drive['active_fault_bits'])
                              if drive else '')

        # 오도메트리·IMU 상태를 웹·진단이 그대로 쓸 수 있게 풀어서 낸다.
        # 비트마스크만 주면 소비자마다 다시 해석해야 하고 그때 규약이 갈린다.
        with self._lock:
            odom, imu, imu_t = self.odom, self.imu, self.last_imu_t
        st['odom_valid'] = bool(
            odom and P.odom_pose_valid(odom['status_flags'],
                                       odom['steering_source']))
        st['odom_imu_fused'] = bool(
            odom and odom['status_flags'] & P.ODOM_IMU_FUSED)
        st['odom_input_invalid'] = bool(
            odom and odom['status_flags'] & P.ODOM_INPUT_INVALID)
        st['odom_status'] = (P.describe_odom_status(odom['status_flags'])
                             if odom else '')
        st['odom_steering_source'] = (
            P.ODOM_STEERING_SOURCE_NAMES.get(odom['steering_source'], '?')
            if odom else '')
        imu_fresh = imu_t is not None and now - imu_t <= 0.5
        st['imu_present'] = bool(imu and imu_fresh)
        st['imu_healthy'] = bool(
            imu and imu_fresh and P.imu_healthy(imu['status_flags']))
        st['imu_status'] = (P.describe_imu_status(imu['status_flags'])
                            if imu else '')
        st['imu_lost_fault'] = bool(
            drive and drive['active_fault_bits'] & P.FAULT_IMU_LOST)

        # 태그 관측을 그대로 낸다. tag_seen(bool)만 있으면 "몇 m 에서 멈췄나" 를
        # JSON 만 보고 알 수 없어 /tag/target 과 시각을 맞춰야 했다.
        # along 은 **뒷차축 중심 → 태그면** 거리다 (앞머리는 0.27 m 더 가깝다).
        # target_tag_id 는 route_logic.status() 가 이미 넣는다. 여기서는
        # "무엇이 보이는가" 만 더한다 — 목표 미확보와 전체 미검출을 구분한다.
        tagpair = self._tag_for_step()
        st['tags_visible'] = self._fresh_tag_ids()
        if tagpair is not None:
            t, t_at = tagpair
            st['tag_id'] = t.tag_id
            st['tag_valid'] = bool(t.valid)
            st['tag_age_s'] = round(now - t_at, 3)
            st['tag_along_m'] = round(t.along, 3)
            # 같은 거리를 앞머리 기준으로도 낸다. 두 기준을 섞어 쓰다 헷갈리는
            # 것을 막으려면 **둘 다 보여주는 것**이 답이다 — 하나로 통일하면
            # 회전 기하(뒷차축)나 충돌 여유(앞머리) 중 하나가 틀린다.
            st['tag_front_m'] = round(t.along - self.cfg.front_overhang_m, 3)
            st['tag_cross_m'] = round(t.cross_track, 3)
            st['tag_head_deg'] = round(math.degrees(t.heading_error), 2)
        else:
            st['tag_id'] = 0
            st['tag_valid'] = False
            st['tag_age_s'] = None
            st['tag_along_m'] = None
            st['tag_front_m'] = None
            st['tag_cross_m'] = None
            st['tag_head_deg'] = None
        st['front_overhang_m'] = self.cfg.front_overhang_m

        self.pub_state.publish(String(data=json.dumps(st, ensure_ascii=False)))
        self.pub_connected.publish(Bool(data=connected))

    # ── 서비스 ───────────────────────────────────────────
    def _start(self, steps, name, res):
        why = self.machine.start(steps, name, self.gather())
        res.success = (why == '')
        res.message = why or f'{name} 미션 시작'
        # 한 줄에서 info/warn 을 번갈아 부르면 안 된다. rclpy 로거는 호출
        # 위치(파일·줄)를 키로 심각도를 캐싱해서, 같은 위치에 다른 심각도가
        # 오면 ValueError: Logger severity cannot be changed between calls
        # 로 죽는다. 반드시 서로 다른 줄에서 호출한다.
        if res.success:
            self.get_logger().info(res.message)
        else:
            if self.machine.state == FAULT:
                res.message += (' → ros2 service call /route/reset_fault '
                                'std_srvs/srv/Trigger')
            self.get_logger().warn(res.message)
        return res

    def srv_start(self, _req, res):
        return self._start(outbound_mission(self.cfg), 'outbound', res)

    def srv_return(self, _req, res):
        return self._start(return_mission(self.cfg), 'return', res)

    def srv_approach(self, _req, res):
        """태그 하나만 향해 접근. 코스 없이 체인을 검증한다."""
        tid = int(self.get_parameter('approach_tag_id').value)
        trig = float(self.get_parameter('approach_trigger_m').value)
        mx = float(self.get_parameter('approach_max_m').value)
        return self._start(approach_mission(tid, trig, mx),
                           f'approach(tag {tid})', res)

    def srv_turn_left(self, _req, res):
        """상대 yaw +90°(좌). 태그·카메라 불필요 (JETSON_YAW90)."""
        deg = float(self.get_parameter('turn_target_deg').value)
        return self._start(relative_turn_mission(+1, deg, self.cfg),
                           f'turn_left({deg:.0f}°)', res)

    def srv_turn_right(self, _req, res):
        """상대 yaw -90°(우)."""
        deg = float(self.get_parameter('turn_target_deg').value)
        return self._start(relative_turn_mission(-1, deg, self.cfg),
                           f'turn_right({deg:.0f}°)', res)

    def srv_abort(self, _req, res):
        self.machine.abort('운영자 중단 (/route/abort)')
        self.set_command(NEUTRAL)
        self.send_stop(P.STOP_OPERATOR)
        res.success = True
        res.message = '중단했다. /route/reset_fault 로 해제한다'
        self.get_logger().warn(res.message)
        return res

    def srv_reset(self, _req, res):
        """STM32 로 CMD_RESET_FAULT 를 보내고 telemetry 확인을 시작한다.

        success=True 는 "요청 전송 성공" 이지 해제 성공이 아니다 — 해제는
        _check_reset_result() 가 telemetry 에서 bit 소거를 확인해야 확정된다.
        STM32 에 reset 대상 fault 가 없으면 기존처럼 Jetson FAULT 만 푼다.
        """
        now = time.monotonic()
        if self.machine.state == RUNNING:
            res.success = False
            res.message = '주행 중이다 — 먼저 /route/abort'
            return res

        with self._lock:
            drive, tel_t = self.drive, self.last_tel_t
        if drive is None or tel_t is None:
            res.success = False
            res.message = 'STM32 telemetry 가 없다 — 링크 확인'
            return res
        if now - tel_t > self.telemetry_timeout_s:
            res.success = False
            res.message = 'STM32 telemetry 가 오래됐다 — 링크 확인'
            return res

        active = drive['active_fault_bits']
        reset_mask = active & P.RESETTABLE_FAULT_MASK
        if reset_mask == 0:
            # STM32 쪽에 지울 것이 없다 — Jetson 상태머신만 정리
            jet = self.machine.reset_fault()
            leftover = P.describe_faults(active) if active else '없음'
            res.success = jet
            res.message = (f'STM32 reset 대상 fault 없음 (활성: {leftover}); '
                           + ('Jetson FAULT 해제, IDLE' if jet
                              else f'Jetson 도 FAULT 아님 ({self.machine.state})'))
            return res

        self.send_reset_fault(reset_mask)
        self.reset_pending = True
        self.reset_requested_mask = reset_mask
        self.reset_requested_at = now
        res.success = True
        res.message = (f'STM32 reset 요청 전송: '
                       f'[{P.describe_faults(reset_mask)}] — telemetry 확인 중')
        return res

    # ── 종료 ─────────────────────────────────────────────
    def shutdown(self):
        """어느 경로로 끝나도 정지 명령을 보낸다."""
        self.set_command(NEUTRAL)
        time.sleep(0.15)
        try:
            for _ in range(3):
                self.send_stop(P.STOP_LINK_SHUTDOWN)
                time.sleep(0.05)
            self._write(P.pack_cmd_drive(self._next_seq(), 0, 0, False))
        except Exception:                                   # noqa: BLE001
            pass
        self._stop_threads.set()
        time.sleep(0.1)
        try:
            self.ser.close()
        except Exception:                                   # noqa: BLE001
            pass


def main(args=None):
    rclpy.init(args=args)
    node = None
    try:
        node = RouteRunner()
    except Exception:                                       # noqa: BLE001
        rclpy.shutdown()
        raise
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.shutdown()
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
