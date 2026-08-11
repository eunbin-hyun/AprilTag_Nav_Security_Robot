"""경로 주행 상태머신 (순수 Python, ROS import 금지).

ROS·UART 없이 단위시험하기 위해 분리했다. `route_runner.py` 가 이것을 감싸서
UART 소유와 ROS 입출력을 담당하고, 이 모듈은 "지금 어떤 명령을 내야 하는가"
만 결정한다.

경로는 **단계 리스트**로 정의한다. 상태를 하드코딩하지 않으므로 경로 변경이
리스트 수정으로 끝난다.

    출발 (1 -> 2 -> 3)          복귀 (3 -> 2 -> 1)
    drive  태그2까지            turn   180° U턴 (우측, 반경 작게)
    turn   -90° 우회전          drive  태그4까지
    drive  태그3까지            turn   +90° 좌회전
    stop                        drive  태그1까지
                                stop

태그 배치 (robot_bringup/apriltags/README.md):
    ID 1 충전스테이션 · ID 2 코너 출발면 · ID 3 도착지 · ID 4 코너 복귀면

제어:
  - 태그가 보이면 **경로 추종 시각 서보** (tag_geometry.steering_for_path)
  - 태그를 놓치면 **heading 유지** (단계 진입 시 yaw 를 목표로)
  - 회전은 **고정 조향 + yaw 적분 종료 판정**

2 m 레그면 160 mm 태그가 전 구간에서 보인다 (2 m 에서 106 px @720p). 그래서
heading 유지는 순간 소실용 백업이고, 주 모드는 시각 서보다. BNO085 미적용으로
yaw 가 조향 명령 추정치인 현 상황에서 이 구조가 중요하다.
"""
import math
from dataclasses import dataclass, field, replace

from robot_perception.tag_geometry import (
    SteeringConfig, TagTarget, clamp_steering, steering_for_heading_hold,
    steering_for_path, turn_steering, wrap_pi,
)

# ── 상태 ───────────────────────────────────────────────────
IDLE = 'IDLE'
RUNNING = 'RUNNING'
ARRIVED = 'ARRIVED'          # 출발 미션 완료 (도착지)
DOCKED = 'DOCKED'            # 복귀 미션 완료 (충전스테이션)
HOLD = 'HOLD'                # 장애물·웹정지. 해제되면 같은 단계로 복귀
FAULT = 'FAULT'              # latch. 사람이 해제해야 한다

TERMINAL = (ARRIVED, DOCKED)

# ── 장애물 HOLD 복구 하위상태 ──────────────────────────────
# HOLD 는 그대로 두고 그 안의 진행 단계를 명시한다. 별도 enum 상태를 만들면
# 웹·로그·`_hold_from` 복귀 로직이 전부 갈라지므로 하위상태로 표현한다.
#
#   OBSTACLE_PRESENT       bit16=1. clear 타이머 정지
#   OBSTACLE_CLEAR_DEBOUNCE bit16=0 이 obstacle_clear_hold_s 동안 연속 유지되길 대기
#   STM32_REARM_WAIT       debounce 통과. STM32 가 READY(2) 가 되기를 대기
#
# ⚠ "그냥 stm32_ready=True 로 덮어쓰기" 를 하지 않는 이유: 그러면 이 구간에서
#   다른 fault·통신단절까지 같이 가려진다. 복구 대기는 **장애물 때문에만**
#   허용되고, 나머지 차단 사유는 그대로 즉시 FAULT 다.
HOLD_SUB_PRESENT = 'OBSTACLE_PRESENT'
HOLD_SUB_DEBOUNCE = 'OBSTACLE_CLEAR_DEBOUNCE'
HOLD_SUB_REARM_WAIT = 'STM32_REARM_WAIT'


@dataclass(frozen=True)
class Step:
    """미션의 한 단계.

    kind='drive'  태그를 향해 주행. trigger_m 안으로 들어오면 종료.
    kind='turn'   고정 조향 회전. turn_deg 만큼 돌면 종료.
    kind='stop'   정지하고 미션 종료.
    """

    kind: str
    name: str = ''
    tag_id: int = 0                # drive: 목표 태그
    trigger_m: float = 0.6         # drive: 이 거리 안으로 오면 종료
    max_m: float = 2.0             # drive: 이 거리를 넘으면 FAULT_OVERRUN
    decel_m: float = 1.2           # drive: 여기서부터 v_approach 로 감속
    turn_deg: float = 0.0          # turn: + 좌 / - 우
    radius_m: float = 0.6          # turn: 회전반경 (이론 시간·조향 계산용)
    steering_cdeg: int = 0         # turn: 0 이 아니면 radius 대신 이 조향 고정
    # drive: 거리와 **함께** 요구하는 정렬 조건. 0 이면 거리만 본다.
    #   코너 진입은 어차피 yaw 로 회전하므로 정렬을 안 본다 (0).
    #   종점(도착·도킹)은 자세가 결과물이므로 둘 다 본다.
    align_cross_m: float = 0.0
    align_head_deg: float = 0.0


@dataclass(frozen=True)
class RouteConfig:
    """주행 파라미터. 실측·시운전 후 조정한다."""

    wheelbase_m: float = 0.135
    v_cruise: int = 200            # mm/s
    v_approach: int = 150
    v_turn: int = 200
    # 구간 거리 상한 (FAULT_OVERRUN). 시운전 중에는 넉넉하게 잡는다 —
    # 태그를 손으로 옮기며 시험하면 예상보다 많이 주행한다.
    leg_max_m: float = 3.0
    turn_eps_deg: float = 3.0      # 회전 종료 허용 오차 (= 기본 stop lead)
    # 이론 소요시간의 배수. 이론은 Step.radius_m(모델 반경)으로 계산하는데,
    # 실측 반경이 모델의 1.65 배다 (2026-08-05: 모델 0.415 / 실측 0.685 m).
    # 2.5 로는 실제 필요시간 대비 여유가 1.52 배뿐이라 반경이 조금만 더
    # 나빠져도 **정상 회전이 타임아웃**된다. 4.0 이면 여유 2.43 배이고,
    # 거리 가드(turn_max_m)가 먼저 걸려 그쪽이 실질 상한이 된다.
    turn_timeout_factor: float = 4.0
    # ── 상대 yaw 회전 (JETSON_YAW90 인수인계 2026-08-03) ──
    # stop lead: 목표보다 이만큼 먼저 정지 명령 → 관성 오버슈트 보정.
    # 좌우 기구 한계가 비대칭이라 방향별로 따로 조정할 수 있다.
    # 0 이하면 turn_eps_deg 를 쓴다 (기존 미션 회전과 동일 동작).
    stop_lead_left_deg: float = 0.0
    stop_lead_right_deg: float = 0.0
    # 마무리 구간 속도 [mm/s]. 90(인수인계 권장값)은 실바닥에서 검증된 적이
    # 없다 — 2026-08-05 실측으로 확인된 최저 주행 속도는 명령 150(실측
    # 122 mm/s)이고, 성공한 30°/90° 회전은 전부 150 이상으로 돌렸다.
    # 90 이면 마지막 15° 에서 바퀴가 멈춰 회전이 덜 된 채로 끝날 수 있다.
    v_turn_slow: int = 150
    slow_remaining_deg: float = 15.0   # 남은 각도가 이하면 감속
    turn_fixed_steering_cdeg: int = 1800   # 상대 회전용 좌우 대칭 초기값
    # U턴은 공간을 아껴야 하므로 더 깊게 꺾는다. 한계(2869)까지 가지 않는
    # 이유: 트림이 얹혀 전선이 먼저 포화하고, 깊은 조향은 아직 실측이 없다.
    uturn_steering_cdeg: int = 2600
    # 코너 진입 레그를 끝내는 거리 [m] — **방향별로 다르다.**
    # 90° 회전은 코너에서 회전반경 R 만큼 앞에서 시작해야 접선이 맞는데,
    # 트림 +500 때문에 좌우 R 이 1.5 배 차이난다 (2026-08-05 실측):
    #     좌 전선 +19.55°(한계 포화)    → R 0.63 m
    #     우 전선 -13.00°(트림이 5° 깎음) → R 0.98 m
    # 그래서 한 값으로 통일할 수 없다. 양쪽 1.0 으로 두면 좌회전이 반경의
    # 1.6 배 앞에서 돌기 시작해 코너를 안쪽으로 파고들고, 회전 후 다음 태그를
    # 재획득하지 못한다 (e2e 시뮬레이터에서 복귀가 태그1 을 놓치고 FAULT).
    # 예전 0.60 은 모델 반경 0.42 가정이라 양쪽 다 늦게 돌았다.
    # 부수 이득: 태그는 0.66 m 근처에서 화면을 이탈하는데(2026-08-03 실측)
    # 이 거리에서 끊으면 태그가 아직 보이는 상태로 회전에 들어간다.
    #
    # ⚠ 뒷차축 기준이고 그래야 맞다. 회전 원호를 그리는 점이 뒷차축
    #   중심이므로(자전거 모델의 무슬립 조건이 성립하는 점) 코너 진입은
    #   뒷차축이 코너에서 R 만큼 앞일 때다. 앞머리 기준으로 바꾸면 틀어진다.
    corner_trigger_right_m: float = 1.00   # 우 90° 실측 반경 0.98 m
    # 좌 90° 실측 반경은 0.63 m 지만 진입 거리는 1.00 m 로 둔다 (2026-08-06
    # 지시). 반경보다 0.37 m 앞에서 돌기 시작하므로 접선이 맞지 않고 코너를
    # 크게 도는 대신, 태그4 가 화각을 벗어나기(≈0.66 m) 전에 회전에 들어간다.
    # 접선을 되찾으려면 0.65 로 되돌린다.
    corner_trigger_left_m: float = 1.00    # 좌 90° 진입 (실측 반경 0.63 m)
    # 종점 레그에서 정지하는 거리 [m]. corner_trigger_* 과 **같은 뒷차축
    # 기준**이다 — 파라미터 기준을 하나로 통일한다. 앞머리 기준으로 받으면
    # 로그(뒷차축)와 설정(앞머리)이 갈려서 매번 환산해야 한다.
    #
    # 도착(태그3)과 도크(태그1)를 나눠 둔 이유: 둘의 요구가 다르다. 도착은
    # 가까이 붙어야 하고, 도크는 충전 접점 여유가 있어야 한다. 파라미터가
    # 둘인 것은 유지한다 — 한 값을 공유하면 도착을 조이는 순간 도크도 조용히
    # 같이 조여진다. 예전 0.50 공통값은 앞머리가 0.23 m 까지 붙어 가까웠다.
    #
    # 2026-08-06 지시로 도크도 0.65 로 내렸다 (앞머리 여유 1.00 → 0.65 와
    # 같은 0.38 m). 값은 지금 도착과 같지만 파라미터는 계속 따로다.
    # 충전 접점 여유가 부족하면 dock_trigger_m 만 1.00 으로 되돌린다.
    arrive_trigger_m: float = 0.65   # 태그3 도착 (앞머리 여유 0.38 m)
    dock_trigger_m: float = 0.65     # 태그1 도크 (앞머리 여유 0.38 m)
    # 태그를 놓친 뒤 엔코더로 채워도 되는 최대 거리 [m].
    # 코너 진입 1.0 m 에서 태그가 화각을 벗어나면(반화각 25.8° → |cross|
    # 0.48 m 초과) 단계가 끝나지 못해 코너를 지나친다. 마지막으로 본
    # 거리에서 남은 만큼만 추측항법으로 채운다. 이 값을 넘게 남았으면
    # 멀리서 놓친 것이므로 믿지 않는다 (heading 드리프트가 쌓인다).
    tag_lost_coast_max_m: float = 0.8
    # ── 종점 정렬 조건 ────────────────────────────────────
    # 종점(태그3 도착·태그1 도킹)은 거리만 맞으면 **비뚤어진 채로도 완료**
    # 된다. 코너는 yaw 로 회전하니 정렬이 덜 중요하지만 종점은 자세가
    # 결과물이다. 그래서 종점만 거리 + 정렬을 함께 요구한다.
    #
    # ⚠ 이 조건은 정렬을 **고치는 장치가 아니다.** 거리 시정수가 0.6 m 라
    #   마지막 10 cm 로는 head 5° 가 4.1° 로밖에 안 준다 (모델 계산).
    #   정렬은 접근 구간에서 끝나야 한다. 이 조건의 역할은 ① 정렬될 때까지
    #   완료를 보류해 접근 구간을 끝까지 쓰게 하고 ② 안 되면 완료하되
    #   **실패를 드러내는** 것이다. 반복 실패하면 손댈 곳은 kp_heading·
    #   카메라 캘리브레이션·태그 배치다.
    align_cross_m: float = 0.04      # 허용 횡오차 [m]
    align_head_deg: float = 5.0      # 허용 접근각 [deg] (공칭 내부파라미터 고려)
    align_hold_s: float = 0.2        # 이 시간 연속 만족해야 정렬로 본다
    # 정렬을 기다리며 trigger 보다 더 다가갈 수 있는 거리 [m].
    #
    # ⚠ 이 값은 앞머리 여유와 **디바운스 시간창을 동시에** 지배한다.
    #   0 으로 두면 하한 = trigger 가 되어 along<=trigger 인 첫 tick 에
    #   하한 분기가 걸리고, align_hold_s 를 볼 기회가 없어 **단일 표본
    #   판정**이 된다 (heading 이 떨리므로 오판정한다).
    #   150 mm/s 에서 0.05 m = 0.33 s → hold 0.2 s 대비 1.67 배 여유.
    #   0.10 은 여유가 3.3 배지만 앞머리를 0.28 m 까지 깎는다 — 0.23 m 를
    #   "너무 가깝다" 고 판단한 기준(2026-08-05)에 근접한다.
    #   도착 = 성공으로 정했으므로 더 기어들어가 얻는 것도 없다.
    align_extra_m: float = 0.05
    # drive 단계 보정 상한 [cdeg]. 좌우를 **같은 크기**로 맞춘다.
    # 트림 +500 이 좌측 실효 한계를 +1455 로 깎아서, 제한이 없으면 우측만
    # -2869 까지 써서 보정 권한이 2배 비대칭이 된다. 게인이 아니라 한계만
    # 맞추므로 선형 구간의 감쇠비는 좌우가 같게 유지된다. 0 이면 무제한.
    correction_limit_cdeg: int = 1455
    # ── 표시용 ────────────────────────────────────────────
    # 뒷차축 중심 → 차 앞머리 [m] (실측). 판정에는 쓰지 않는다 — 로그·JSON 에
    # "앞머리 여유가 얼마인가" 를 같이 보여주기 위한 값이다.
    front_overhang_m: float = 0.27
    # 모델 반경 대비 실측 반경의 배수. 타이어 슬립·유격으로 자전거 모델보다
    # 항상 크게 돈다 — 2026-08-05 실측 좌 1.65 / 우 1.69 로 **방향 무관 상수**.
    # 회전 타임아웃의 이론시간을 이걸로 보정한다. 완료 판정에는 쓰지 않는다
    # (그건 0x83 yaw 만 쓴다).
    understeer_factor: float = 1.67
    # 상대 회전 이동거리 가드 (yaw 불변 시 무한 원호 방지).
    # 실측 2026-08-05: 전선 -1800 에서 R 0.685 m -> 90° 에 1.08 m 필요.
    # 예전 0.90 은 R 0.42 가정이라 정상 회전도 걸렸다.
    turn_max_m: float = 2.5
    yaw_jump_limit_deg: float = 30.0   # 단일 표본 비현실 점프 → 중단
    wrong_way_limit_deg: float = 5.0   # 반대 방향 진행 허용 한계
    fusion_grace_s: float = 0.7    # 움직임 시작 후 IMU 융합 대기
    require_imu_fused: bool = True  # 주행 중 IMU_FUSED=1 요구 (정지 중 0 은 정상)
    # ★ 융합이 **연속** 이 시간 이상 끊길 때만 FAULT 한다.
    #   실장비 BNO085 는 IMU_FUSED 를 깜빡인다 — 2026-08-05 회전 시험 실측:
    #   켜짐 196 ms → 꺼짐 → 켜짐 542 ms → 꺼짐 순으로 토글했고, 디바운스가
    #   없던 예전 코드는 꺼진 지 34 ms 만에 FAULT 했다. 한 표본 dropout 으로
    #   미션을 죽이면 회전 시험 자체가 불가능하다.
    #   0.8 s 근거: 관측된 최장 dropout 542 ms + 여유. 이 시간 동안은 모델 yaw
    #   로 버틴다. ⚠ 그래서 **조향 트림이 0 이어야 안전하다** — 트림이 걸려
    #   있으면 STM32 COMMAND_ESTIMATE yaw 가 부풀려져(전선값을 조향각으로 믿음)
    #   버티는 동안 엉뚱하게 센다. 트림과 이 디바운스는 짝으로 봐야 한다.
    #   150 mm/s × 0.8 s = 12 cm 를 융합 없이 도는 셈이므로 더 늘리지 말 것.
    fusion_dropout_grace_s: float = 0.8
    # 장애물 clear 후 이 시간 동안 안정적으로 비어 있어야 재개한다.
    # 초음파가 시간적으로 flicker 하면 해제→재출발→재정지가 반복되므로
    # 한 프레임 clear 로는 출발하지 않는다 (내일과제 §2.3).
    obstacle_clear_hold_s: float = 0.5
    # bit16 clear·debounce 통과 후 STM32 가 READY(2) 로 오기를 기다리는 상한.
    # STM32 는 SAFE_STOP 을 주기적으로 재계산하므로 neutral 을 계속 보내면
    # 곧 READY 가 된다. 이 시간을 넘기면 우리가 모르는 이유로 막힌 것이므로
    # 조용히 기다리지 않고 FAULT 로 올려 원인을 노출한다.
    obstacle_ready_timeout_s: float = 2.0
    # ★ 장애물 HOLD 중 SENSOR_STALE(bit15) **단독** 을 유예하는 상한.
    #   장애물 감지 경계에서 초음파 측정이 순간적으로 무효·정체되면 bit16 이
    #   내려가고 bit15 가 대신 뜬다 (현장 로그 0x8100 = bit8+bit15). bit15 는
    #   ARM 차단 대상이라 그 한 프레임에 FAULT 로 굳고, FAULT 훅이 CMD_STOP 을
    #   보내 STM32 의 rearm_required 를 다시 세워 자동 복구를 스스로 막았다.
    #   0.25 s 는 "측정 한두 주기 놓친 것" 과 "센서가 죽은 것" 을 가르는 값이다.
    #   ⚠ 늘리지 말 것 — 늘리면 진짜 센서 고장을 그만큼 오래 못 본다.
    sensor_stale_grace_s: float = 0.25
    # 상태머신이 이 시간 이상 tick 하지 않으면 호출자가 neutral 을 강제한다.
    command_stale_s: float = 0.2


@dataclass(frozen=True)
class Command:
    """UART 로 나갈 명령."""

    speed_mm_s: int = 0
    steering_cdeg: int = 0
    enable: bool = False

    @property
    def is_neutral(self) -> bool:
        return not self.enable and self.speed_mm_s == 0 and self.steering_cdeg == 0


NEUTRAL = Command()


@dataclass
class Inputs:
    """한 tick 의 입력. 없는 값은 valid=False 로 표현한다."""

    now: float = 0.0
    odom_valid: bool = False
    yaw_rad: float = 0.0
    distance_m: float = 0.0
    tag: TagTarget = field(default_factory=TagTarget)
    stm32_ready: bool = False      # READY 또는 DRIVING
    imu_fused: bool = False        # ODOM IMU_FUSED (정지 중 0 은 정상)
    speed_mm_s: float = 0.0        # ODOM linear_speed (융합 대기 판정용)
    arm_blocked: bool = False      # neutral 로 해소되지 않는 fault 활성
    obstacle: bool = False         # OBSTACLE_NEAR
    # sensor_stale_only: 차단 fault 가 SENSOR_STALE(bit15) **하나뿐**인가.
    #   REPORT_ONLY 인 RANGE_LOST(bit8) 는 애초에 차단 집합에 없으므로 같이 떠
    #   있어도 참이다. bit 판정은 runner 가 한다 (여기서 protocol 을 안 쓴다).
    sensor_stale_only: bool = False
    stop_requested: bool = False   # 웹·운영자 정지
    # ── 장애물 복구 판정에 필요한 추가 입력 ─────────────────
    # telemetry_ok: 0x80 이 timeout 안에 들어오는가. 통신단절을 장애물 복구
    #   대기로 숨기지 않기 위해 **분리된 입력**이 필요하다. 예전에는 통신단절이
    #   stm32_ready=False 로만 나타나서, 그 검사를 완화하면 단절이 가려졌다.
    #   기본 False — 이 dataclass 의 기존 관례대로 "확인되지 않은 것은 무효".
    telemetry_ok: bool = False
    telemetry_age_s: float = 0.0   # 진단 로그용
    # stm32_state_ready: state == READY(2) **정확히**. stm32_ready 는 DRIVING
    #   도 포함하므로 rearm 완료 판정에 쓸 수 없다. route_logic 이 protocol 을
    #   import 하지 않기 위해 bool 로 받는다.
    stm32_state_ready: bool = False
    stm32_state_name: str = ''     # 진단 로그용
    fault_bits: int = 0            # 진단 로그용 (active_fault_bits 원본)
    blocking_fault_bits: int = 0   # 진단 로그용 (bit16 제외 차단 fault)
    last_drive_seq: int = 0        # 진단 로그용 (STM32 가 마지막 수용한 SEQ)
    # ── yaw 출처 ────────────────────────────────────────────
    # yaw_is_imu: yaw_rad 가 0x83 IMU quaternion 에서 왔는가.
    #   True 면 STM32 의 IMU_FUSED 여부가 무의미하므로 회전 융합 가드를
    #   건너뛴다 — 우리가 IMU 를 직접 읽고 있기 때문이다.
    #   0x85 융합 yaw 는 모델 성분 25% 때문에 실측 1.275 배 부풀려진다
    #   (2026-08-05: 0x83 -114.7° vs 0x85 -146.2°).
    yaw_is_imu: bool = False
    yaw_source: str = ''           # 'imu' | 'odom' (진단 로그용)


# ── 미션 정의 ──────────────────────────────────────────────

def turn_step(name: str, direction: int, deg: float, cfg: RouteConfig,
              steer_mag_cdeg: int = 0) -> Step:
    """회전 스텝. 조향을 고정값으로 명시하고 거리 가드를 걸어준다."""
    # radius_m 만 주고 turn_steering() 으로 조향을 역산하면 안 된다는 것이
    # 실측으로 드러났다 (2026-08-05). 모델은 반경 0.6 m 에 12.68° 를 주지만,
    # 전선에는 조향 트림(+500)이 얹혀 **우회전이 -7.68° 로 얕아지고** 실제
    # 반경은 모델의 1.67 배가 된다 — 호길이 2.63 m 가 되어 Step 기본 거리
    # 가드 2.0 m 에서 69° 만 돌고 FAULT 한다.
    #
    # 그래서 실장비에서 검증된 고정 조향(±1800)을 쓰고, radius_m 은 타임아웃
    # 이론시간 계산에만 남긴다. 그 radius_m 도 understeer_factor 로 보정한다 —
    # 모델값 그대로면 이론시간이 1.67 배 짧게 나와 정상 회전이 타임아웃된다.
    # 완료 판정에는 radius_m 을 절대 쓰지 않는다 (0x83 yaw 누적만 쓴다).
    if direction not in (+1, -1):
        raise ValueError('direction 은 +1(좌) 또는 -1(우)')
    mag = abs(steer_mag_cdeg or cfg.turn_fixed_steering_cdeg)
    r = (cfg.understeer_factor * cfg.wheelbase_m
         / math.tan(math.radians(mag / 100.0)))
    return Step('turn', name, turn_deg=direction * abs(deg),
                radius_m=r, steering_cdeg=direction * mag,
                max_m=cfg.turn_max_m)


def outbound_mission(cfg: RouteConfig = None) -> list:
    """충전스테이션 -> 코너(우회전) -> 도착지."""
    cfg = cfg or RouteConfig()
    m = cfg.leg_max_m
    return [
        Step('drive', 'LEG1 도크→코너', tag_id=2,
             trigger_m=cfg.corner_trigger_right_m,
             decel_m=cfg.corner_trigger_right_m + 0.6, max_m=m),
        turn_step('TURN 우 90°', -1, 90.0, cfg),
        Step('drive', 'LEG2 코너→도착', tag_id=3,
             trigger_m=cfg.arrive_trigger_m,
             decel_m=cfg.arrive_trigger_m + 0.6, max_m=m,
             align_cross_m=cfg.align_cross_m,
             align_head_deg=cfg.align_head_deg),
        Step('stop', 'ARRIVED'),
    ]


def return_mission(cfg: RouteConfig = None) -> list:
    """도착지 -> (U턴) -> 코너(좌회전) -> 충전스테이션.

    U턴이 필요한 이유: 레그를 거꾸로 가려면 180° 돌아야 한다. 안 돌면 후진
    주행이 되는데 후방 센서가 없고 카메라가 앞만 봐서 태그를 못 본다.
    U턴은 깊은 조향(uturn_steering_cdeg)으로 공간을 아낀다 — 태그를 볼 필요가
    없는 동작이라 반경을 줄이는 것이 이득이다. 90° 보다 호가 길어서, 얕게 꺾으면
    거리 가드(turn_max_m)를 넘긴다: 전선 -13.00° 면 호가 3.07 m 다.
    """
    cfg = cfg or RouteConfig()
    m = cfg.leg_max_m
    return [
        turn_step('UTURN 우 180°', -1, 180.0, cfg,
                  steer_mag_cdeg=cfg.uturn_steering_cdeg),
        Step('drive', 'RET_LEG2 도착→코너', tag_id=4,
             trigger_m=cfg.corner_trigger_left_m,
             decel_m=cfg.corner_trigger_left_m + 0.6, max_m=m),
        turn_step('TURN 좌 90°', +1, 90.0, cfg),
        Step('drive', 'RET_LEG1 코너→도크', tag_id=1,
             trigger_m=cfg.dock_trigger_m,
             decel_m=cfg.dock_trigger_m + 0.6, max_m=m,
             align_cross_m=cfg.align_cross_m,
             align_head_deg=cfg.align_head_deg),
        Step('stop', 'DOCKED'),
    ]


def relative_turn_mission(direction: int, target_deg: float,
                          cfg: RouteConfig = None) -> list:
    """상대 yaw 회전만 수행하는 최소 미션 (JETSON_YAW90).

    direction: +1 좌(CCW) / -1 우(CW). 아커만이라 제자리 회전이 불가능하고
    전진 원호를 그리며 yaw 를 바꾼다. 카메라·태그 불필요 — 완료 판정은
    0x85 오도메트리의 융합 yaw 누적만 쓴다.
    """
    cfg = cfg or RouteConfig()
    name = f"YAW {'좌' if direction > 0 else '우'} {abs(target_deg):.0f}°"
    return [
        turn_step(name, direction, target_deg, cfg),
        Step('stop', 'TURN_DONE'),
    ]


class AssumeClearChecker:
    """회전 경로 공간검사 — 항상 허용 (공간 확보는 운영 전제).

    LiDAR 도입 시 이 두 메서드를 가진 객체로 교체하면 된다. UART·yaw
    누적·상태머신은 바뀌지 않는다 (JETSON_YAW90 §9).
    """

    def start_allowed(self, status: dict) -> bool:          # noqa: ARG002
        return True

    def continue_allowed(self, status: dict) -> bool:       # noqa: ARG002
        return True


def approach_mission(tag_id: int, trigger_m: float = 0.6,
                     max_m: float = 3.0) -> list:
    """태그 하나만 놓고 접근 -> 정지. 체인 검증용 최소 미션.

    코스를 다 깔지 않고 "카메라가 태그를 보고 주행 명령을 만드는가" 만
    확인한다. 회전이 없으므로 yaw 정확도에 의존하지 않는다.
    """
    return [
        Step('drive', f'태그 {tag_id} 접근', tag_id=tag_id,
             trigger_m=trigger_m, max_m=max_m, decel_m=trigger_m + 0.6),
        Step('stop', 'ARRIVED'),
    ]


# ── 상태머신 ───────────────────────────────────────────────

class RouteMachine:
    """단계 리스트를 순서대로 실행한다. `tick()` 이 명령을 돌려준다."""

    def __init__(self, cfg: RouteConfig = None,
                 steer: SteeringConfig = None):
        self.cfg = cfg or RouteConfig()
        self.steer = steer or SteeringConfig()
        self.state = IDLE
        self.steps = []
        self.index = 0
        self.mission = ''
        self.fault_reason = ''
        self.last_command = NEUTRAL
        self.last_tick = None
        # 단계 진입 시 기준값
        self._entry_dist = 0.0
        self._entry_yaw = 0.0
        self._entry_t = 0.0
        self._turned = 0.0          # 누적 회전량 [rad]. unwrap 해서 쌓는다
        self._prev_yaw = None
        self._last_yaw_step = 0.0   # 직전 tick 의 yaw 변화량 [rad] (점프 가드)
        self._moving_since = None   # 속도 ≥20mm/s 가 된 시각 (융합 대기)
        self._fusion_lost_since = None   # IMU_FUSED=0 이 시작된 시각
        self.clearance = AssumeClearChecker()   # 교체 가능 (LiDAR 등)
        self._hold_from = None      # HOLD 진입 전 상태
        self.hold_reason = None     # 'obstacle' | 'operator'
        self._obstacle_clear_since = None   # clear debounce 시작 시각
        self._tag_seen = False      # 이 단계에서 태그를 한 번이라도 봤는가
        # drive 단계가 방금 어느 모드였나: 'servo'(횡보정 O) / 'hold'(횡보정 X)
        self.drive_mode = ''
        # 태그를 마지막으로 본 (along[m], 그때까지 주행거리[m]).
        # 소실 후 엔코더로 trigger 를 채우는 데 쓴다.
        self._sight = None
        # 단계가 왜 넘어갔나:
        #   'tag'         태그 판정 (정렬 조건이 있으면 그것도 만족)
        #   'coast'       태그 소실 후 엔코더로 채워서
        #   'align_floor' 정렬이 안 됐지만 거리 하한에 도달해 완료
        self.advance_reason = ''
        # 종점 정렬 결과. None 이면 아직 판정하지 않았다.
        self.aligned = None
        self.align_miss = None      # 실패 시 (cross[m], head[deg])
        self._aligned_since = None
        # 방금 조향을 만든 입력과 그 출력:
        # (cross_track[m], heading_error[rad], steering[cdeg]).
        # servo 가 아니면 None — 그때는 cross 를 쓰지 않았다는 뜻이다.
        self.servo_input = None
        # ── 장애물 HOLD 복구 ────────────────────────────────
        self.hold_substate = None   # HOLD_SUB_* (진단·시험용)
        self._ready_wait_since = None    # debounce 통과 후 READY 대기 시작
        self._hold_entered_at = None     # 현재 HOLD 진입 시각
        self._hold_elapsed_s = 0.0  # 이 단계에서 HOLD 로 보낸 누적 시간
        self._sensor_stale_since = None   # bit15 단독 유예 시작 시각

    # ── 조회 ───────────────────────────────────────────────
    @property
    def step(self):
        if self.state in (IDLE, FAULT) or self.index >= len(self.steps):
            return None
        return self.steps[self.index]

    @property
    def traveled_m(self) -> float:
        return abs(self._last_dist - self._entry_dist) if hasattr(
            self, '_last_dist') else 0.0

    @property
    def turned_deg(self) -> float:
        return math.degrees(self._turned)

    def active_elapsed_s(self, now: float) -> float:
        """단계 진입 후 **HOLD 를 뺀** 경과 시간.

        회전 타임아웃이 이걸 써야 한다. 벽시계로 재면 장애물 앞에서 오래 서
        있었다는 이유만으로 타임아웃이 터진다 (문서 §6).
        """
        held = self._hold_elapsed_s
        if self._hold_entered_at is not None:
            held += max(0.0, now - self._hold_entered_at)
        return max(0.0, now - self._entry_t - held)

    def describe(self) -> str:
        s = self.step
        base = f'{self.state}'
        if self.mission:
            base += f' [{self.mission} {self.index + 1}/{len(self.steps)}]'
        if s:
            base += f' {s.name}'
        if self.fault_reason:
            base += f' — {self.fault_reason}'
        return base

    # ── 미션 제어 ──────────────────────────────────────────
    def start(self, steps: list, name: str, inp: Inputs) -> str:
        """미션 시작. 실패 사유 문자열 또는 '' 반환."""
        why = self._preflight(inp)
        if why:
            return why
        self.steps = list(steps)
        self.mission = name
        self.index = 0
        self.fault_reason = ''
        # ★ 이전 미션의 **결과**를 반드시 지운다. aligned/align_miss 는
        #   미션이 끝난 뒤 웹·로그가 읽어야 하므로 _advance 에서 지우지
        #   않는데, 그래서 다음 미션 시작 시점에 지워야 하는 짝이 필요하다.
        #   안 지우면 2차 미션이 1차 결과를 물려받아 그대로 보고한다
        #   (실행으로 확인: 1차 aligned=False → 2차 시작 직후에도 False).
        self.aligned = None
        self.align_miss = None
        self.advance_reason = ''
        self._aligned_since = None
        self._sight = None
        self.state = RUNNING
        self._enter_step(inp)
        return ''

    def _preflight(self, inp: Inputs) -> str:
        """출발 전제조건. 거부 사유를 그대로 웹에 표시할 수 있게 만든다."""
        if self.state == FAULT:
            return f'FAULT 상태다 ({self.fault_reason}). 먼저 해제해야 한다'
        if self.state == RUNNING:
            return '이미 주행 중이다'
        if not inp.stm32_ready:
            return 'STM32 가 READY 가 아니다 (링크·재무장 확인)'
        if inp.arm_blocked:
            return 'neutral 로 해소되지 않는 fault 가 활성이다'
        if not inp.odom_valid:
            return '오도메트리가 유효하지 않다 (pose_valid=false)'
        if inp.stop_requested:
            return '정지 요청이 활성이다 (/safety/stop_active)'
        if not self.clearance.start_allowed(self.status()):
            return '회전·주행 경로 공간이 확보되지 않았다 (clearance)'
        return ''

    def abort(self, why: str = '운영자 중단'):
        self.state = FAULT
        self.fault_reason = why
        self.last_command = NEUTRAL

    def reset_fault(self) -> bool:
        """FAULT latch 해제. IDLE 로 돌아간다."""
        if self.state != FAULT:
            return False
        self.state = IDLE
        self.fault_reason = ''
        self.steps = []
        self.mission = ''
        return True

    # ── 단계 전이 ──────────────────────────────────────────
    def _enter_step(self, inp: Inputs):
        self._entry_dist = inp.distance_m
        self._last_dist = inp.distance_m
        self._entry_yaw = inp.yaw_rad
        self._entry_t = inp.now
        self._turned = 0.0
        self._prev_yaw = inp.yaw_rad
        self._tag_seen = False
        self._last_yaw_step = 0.0
        self._moving_since = None
        self._fusion_lost_since = None
        # HOLD 누적은 단계별로 센다. 단계가 바뀌면 timeout 기준도 새로 잡힌다.
        self._hold_elapsed_s = 0.0
        self._hold_entered_at = None

    def _advance(self, inp: Inputs):
        # 단계가 바뀌면 이전 단계의 서보 입력·최종 관측은 무효다.
        # 남겨두면 진단이 지난 단계의 cross 를 현재 것으로 오해하고,
        # _sight 가 남으면 다음 레그가 시작 즉시 coast 조건을 만족한다.
        self.servo_input = None
        self._sight = None
        # 정렬 타이머는 단계별이다. aligned/align_miss 는 **결과**라서 남긴다 —
        # 미션이 끝난 뒤 웹·로그가 읽어야 한다.
        self._aligned_since = None
        self.index += 1
        if self.index >= len(self.steps):
            self.state = FAULT
            self.fault_reason = '단계 리스트가 stop 없이 끝났다'
            return
        s = self.steps[self.index]
        if s.kind == 'stop':
            self.state = DOCKED if self.mission == 'return' else ARRIVED
            self.last_command = NEUTRAL
            return
        self._enter_step(inp)

    def _fault(self, why: str) -> Command:
        self.state = FAULT
        self.fault_reason = why
        self.last_command = NEUTRAL
        return NEUTRAL

    # ── HOLD 진입·복구·이탈 ────────────────────────────────
    def _enter_hold(self, reason: str, inp: Inputs):
        self._hold_from = RUNNING
        self.state = HOLD
        self.hold_reason = reason
        self._hold_entered_at = inp.now
        self.hold_substate = (HOLD_SUB_PRESENT if reason == 'obstacle'
                              else None)

    def _exit_hold(self, inp: Inputs):
        """HOLD → 원래 상태. 단계는 다시 시작하지 않는다.

        yaw 기준값을 지금 값으로 동기화한다. HOLD 동안 `_prev_yaw` 가 멈춰
        있었으므로, 그냥 나가면 정지 구간 전체의 yaw 변화가 **재개 첫 tick 의
        단일 변화량**으로 잡혀 `yaw_jump_limit_deg` 가스가 오발한다.

        정지 중 yaw 변화를 회전 진행량(`_turned`)에 넣지 않는 이유: 차는 멈춰
        있었으므로 그 변화는 센서 드리프트·정착이고 실제 선회가 아니다.
        거리(`_last_dist`)는 반대로 **그대로 둔다** — HOLD 중 관성으로 밀린
        거리는 실제 이동이고, `max_m` 안전 가드가 그것까지 봐야 한다.
        """
        if self._hold_entered_at is not None:
            self._hold_elapsed_s += max(0.0, inp.now - self._hold_entered_at)
            self._hold_entered_at = None
        self.state = self._hold_from or RUNNING
        self._hold_from = None
        self.hold_reason = None
        self.hold_substate = None
        self._obstacle_clear_since = None
        self._ready_wait_since = None
        self._sensor_stale_since = None
        self._prev_yaw = inp.yaw_rad
        self._last_yaw_step = 0.0

    def _tick_sensor_stale_grace(self, inp: Inputs):
        """bit15 단독 차단 fault 를 짧게 유예한다. 유예 중이면 Command, 아니면 None.

        `None` 을 돌려주면 호출자가 기존대로 FAULT 로 올린다. 유예 조건을 좁게
        잡는 것이 이 함수의 요점이다 — 넓히면 실제 센서 고장을 자동으로 무시하는
        코드가 된다:

          - **이미 장애물 HOLD 에 들어와 있을 때만.** 정상 주행 중 새로 뜬 bit15
            는 유예 없이 즉시 FAULT 다 (그때는 초음파 flicker 라는 근거가 없다).
          - **차단 fault 가 bit15 하나뿐일 때만.** STEERING_INVALID·MOTOR_STALL·
            ESTOP 이 섞여 있으면 유예하지 않는다.
          - 통신단절은 `tick()` 이 이 함수보다 **먼저** 보므로 여기 오지 않는다.

        유예 중에는 neutral 만 나간다. `_fault()` 를 거치지 않으므로 runner 의
        FAULT 훅도 안 돌고, 따라서 `CMD_STOP` 이 나가지 않는다 — 그것이 STM32 의
        `rearm_required` 를 다시 세우지 않는 근거다.
        """
        if not (self.state == HOLD and self.hold_reason == 'obstacle'):
            self._sensor_stale_since = None
            return None
        if not inp.sensor_stale_only:
            self._sensor_stale_since = None
            return None
        if self._sensor_stale_since is None:
            self._sensor_stale_since = inp.now
        if inp.now - self._sensor_stale_since > self.cfg.sensor_stale_grace_s:
            return None                    # 상한 초과 — 진짜 고장으로 다룬다
        # bit15 가 떠 있는 동안은 "장애물이 없다" 고 볼 수 없다. clear·READY
        # 타이머를 둘 다 버려서, bit15 가 사라진 시점부터 debounce 를 새로 센다.
        self.hold_substate = HOLD_SUB_PRESENT
        self._obstacle_clear_since = None
        self._ready_wait_since = None
        self.last_command = NEUTRAL
        return NEUTRAL

    def _tick_obstacle_recovery(self, inp: Inputs):
        """장애물 HOLD 복구. 계속 기다려야 하면 Command, 재개 가능이면 None.

        두 조건을 **모두** 만족해야 재개한다 (문서 §2):
          - bit16=0 이 `obstacle_clear_hold_s` 동안 연속 유지
          - 최신 0x80 의 state 가 READY(2)

        어느 쪽이 먼저 와도 다른 쪽을 기다린다. 그 사이 계속 neutral 을 보내
        STM32 의 rearm 과 통신 watchdog 을 갱신한다 — `CMD_STOP` 은 보내지
        않는다 (§4). CMD_STOP 은 `rearm_required` 를 다시 세워서 복구를 스스로
        막는다.
        """
        if self._obstacle_clear_since is None:
            self._obstacle_clear_since = inp.now
        clear_elapsed = inp.now - self._obstacle_clear_since
        if clear_elapsed < self.cfg.obstacle_clear_hold_s:
            # READY 가 먼저 왔어도 debounce 가 끝나기 전엔 재개하지 않는다.
            self.hold_substate = HOLD_SUB_DEBOUNCE
            self.last_command = NEUTRAL
            return NEUTRAL

        # debounce 통과. 이제 READY(2) 만 기다린다.
        if not inp.stm32_state_ready:
            self.hold_substate = HOLD_SUB_REARM_WAIT
            if self._ready_wait_since is None:
                self._ready_wait_since = inp.now
            waited = inp.now - self._ready_wait_since
            if waited > self.cfg.obstacle_ready_timeout_s:
                return self._fault(
                    f'장애물 해제 후 {waited:.1f}s 안에 STM32 가 READY 로 '
                    f'오지 않았다 (state={inp.stm32_state_name or "?"}, '
                    f'fault=0x{inp.fault_bits:08X})')
            self.last_command = NEUTRAL
            return NEUTRAL

        # 두 조건 충족 — 재개한다. odom 은 여기서 다시 엄격하게 본다.
        if not inp.odom_valid:
            return self._fault('장애물 해제 후 오도메트리가 무효다 '
                               '(pose_valid=false)')
        return None

    # ── 매 tick ────────────────────────────────────────────
    def tick(self, inp: Inputs) -> Command:
        self.last_tick = inp.now

        if self.state in (IDLE, FAULT) or self.state in TERMINAL:
            self.last_command = NEUTRAL
            return NEUTRAL

        # ── 즉시 중단 사유. 장애물 HOLD 보다 먼저 본다 — 장애물과 치명
        #    fault 가 동시에 있으면 HOLD 에 가려 fault 판단이 늦어지면 안 된다
        #    (내일과제 §2.4). runner 가 arm_blocked 에서 OBSTACLE_NEAR 를
        #    분리해 주므로 여기 걸리는 것은 진짜 차단 fault 다.
        #
        # ★ 통신단절을 가장 먼저, 무조건 본다. 장애물 복구 대기가
        #   `stm32_ready` 검사를 완화하므로, 단절이 그 완화에 숨지 않도록
        #   별도 입력(`telemetry_ok`)으로 분리해 검사한다.
        if not inp.telemetry_ok:
            return self._fault(
                f'STM32 telemetry 단절 ({inp.telemetry_age_s * 1000:.0f} ms '
                '무수신)')
        if inp.arm_blocked:
            grace = self._tick_sensor_stale_grace(inp)
            if grace is not None:
                return grace
            return self._fault(
                f'ARM 차단 fault 활성 — 재시도하지 않는다 '
                f'(0x{inp.blocking_fault_bits:08X})')
        # 차단 fault 가 없어진 tick 에서 유예 타이머를 버린다. 안 버리면 다음
        # flicker 가 **지난 번 시작 시각**과 비교돼 첫 프레임에 상한을 넘긴다.
        self._sensor_stale_since = None

        obstacle_recovery = (self.state == HOLD
                             and self.hold_reason == 'obstacle')

        # ★ `not stm32_ready` 는 장애물 HOLD 복구 중에만 통과시킨다.
        #   STM32 가 장애물로 SAFE_STOP 에 가 있고 bit16 이 막 내려간 순간,
        #   예전 코드는 이 검사에 먼저 걸려 **영구 FAULT** 로 굳었다.
        #   그 한 프레임은 정상적인 복구 구간이다. 다만 무한정 기다리지는
        #   않는다 — 아래 `_tick_obstacle_recovery` 가 상한을 건다.
        if not inp.stm32_ready and not obstacle_recovery:
            return self._fault(
                f'STM32 가 READY/DRIVING 이 아니다 '
                f'({inp.stm32_state_name or "?"})')
        # odom 무효도 복구 대기 중에는 유예한다 — SAFE_STOP 구간에서 STM32 가
        # odom status 를 잠시 내리는 경우가 있고, 그것 때문에 복구가 FAULT 로
        # 바뀌면 §1 의 취약점을 옆문으로 되살리는 셈이다. 복구가 끝나면
        # (아래에서 RUNNING 으로 나갈 때) 다시 엄격하게 본다.
        if not inp.odom_valid and not obstacle_recovery:
            return self._fault('오도메트리 무효 (pose_valid=false)')

        # ── 정지 요청·장애물: HOLD. 해제되면 같은 단계로 복귀한다.
        #    latch 하지 않는다 — 사람이 앞을 지나갈 때마다 수동 재무장을
        #    요구하게 되면 시연이 불가능하다. 사유를 구분하는 이유: 장애물은
        #    debounce 후 자동 재개, 운영자 정지는 해제 즉시 재개.
        if inp.obstacle:
            if self.state == RUNNING:
                self._enter_hold('obstacle', inp)
            # 재등장 → clear·READY 타이머 둘 다 리셋
            self._obstacle_clear_since = None
            self._ready_wait_since = None
            self.hold_substate = HOLD_SUB_PRESENT
            self.last_command = NEUTRAL
            return NEUTRAL
        if inp.stop_requested:
            if self.state == RUNNING:
                self._enter_hold('operator', inp)
            self.last_command = NEUTRAL
            return NEUTRAL

        if self.state == HOLD:
            if self.hold_reason == 'obstacle':
                cmd = self._tick_obstacle_recovery(inp)
                if cmd is not None:
                    return cmd
            # 운영자 정지는 debounce 없이 즉시 재개 (기존 동작 유지).
            self._exit_hold(inp)

        # yaw 누적. wrap 된 값을 그대로 쓰면 180° U턴 판정이 경계에서 깨진다.
        if self._prev_yaw is not None:
            self._last_yaw_step = wrap_pi(inp.yaw_rad - self._prev_yaw)
            self._turned += self._last_yaw_step
        self._prev_yaw = inp.yaw_rad
        self._last_dist = inp.distance_m

        s = self.step
        if s is None:
            return self._fault('실행할 단계가 없다')
        if s.kind == 'drive':
            return self._tick_drive(s, inp)
        if s.kind == 'turn':
            return self._tick_turn(s, inp)
        return self._fault(f'알 수 없는 단계 종류: {s.kind}')

    def _tick_drive(self, s: Step, inp: Inputs) -> Command:
        traveled = abs(inp.distance_m - self._entry_dist)

        # 태그를 못 봤는데 계속 직진하는 것이 이 설계의 가장 위험한 실패
        # 모드다. 거리 상한을 반드시 감시한다.
        #
        # 원인을 구분해서 알린다. "미검출" 로 뭉뚱그리면, 태그가 멀쩡히
        # 보이는데도 검출 문제로 오해하게 된다 — 실제로 태그는 잡혔지만
        # 접근하지 못한 경우가 흔하다.
        if traveled > s.max_m:
            tag = inp.tag
            if tag.valid and tag.tag_id == s.tag_id:
                why = (f'태그 {s.tag_id} 는 보이지만 {tag.along:.2f} m 로 '
                       f'멀다 (접근 기준 {s.trigger_m:.2f} m)')
            elif self._tag_seen:
                why = f'태그 {s.tag_id} 를 봤다가 놓쳤다'
            else:
                why = f'태그 {s.tag_id} 를 한 번도 못 봤다'
            return self._fault(
                f'FAULT_OVERRUN — {traveled:.2f} m 주행 (상한 {s.max_m:.2f} m), '
                f'{why}')

        tag = inp.tag
        usable = tag.valid and tag.tag_id == s.tag_id
        # ★ 두 모드는 **횡방향 보정 여부가 다르다.** servo 는 cross_track 을
        #   0 으로 만들지만, hold 는 진입 yaw 만 유지하고 cross_track 을 아예
        #   무시한다. 목표 태그가 아닌 태그가 보이거나(코너에서 태그4 가
        #   먼저 잡히는 경우) 검출이 끊기면 조용히 hold 로 떨어져서, 로그만
        #   보면 "보정을 안 한다" 로 보인다. 그래서 모드를 밖으로 낸다.
        self.drive_mode = 'servo' if usable else 'hold'
        if not usable:
            self.servo_input = None
        if usable:
            self._tag_seen = True
            done, why = self._arrival(s, tag, inp)
            if done:
                self.advance_reason = why
                self._advance(inp)
                return self.last_command if self.state in TERMINAL else \
                    self.tick(inp)
            # 거리에 따라 감속. trigger 근처에서 v_approach 가 되게 한다.
            if tag.along >= s.decel_m:
                speed = self.cfg.v_cruise
            else:
                r = max(0.0, (tag.along - s.trigger_m)
                        / max(1e-6, s.decel_m - s.trigger_m))
                speed = int(self.cfg.v_approach
                            + r * (self.cfg.v_cruise - self.cfg.v_approach))
            steering = steering_for_path(tag.cross_track, tag.heading_error,
                                         self.steer)
            # 이 조향을 만든 **입력**을 그대로 남긴다. 진단이 /route/state 의
            # 태그 값을 따로 읽어 재계산하면 두 값이 최대 한 tick 어긋나서
            # 전이 구간마다 수백 cdeg 차이가 난다 — 거짓 경고가 된다.
            # 입력과 출력이 한 메시지에 같이 있어야 검산이 항등식이 된다.
            # 입력과 **그 입력이 만든 출력**을 한 튜플로 묶는다. 출력을
            # 따로(cmd) 짝지으면 단계 전이 tick 에서 이전 단계의 입력과 새
            # 단계의 명령이 붙어 거짓 불일치가 난다 (실측 str=0 vs 기대=+407).
            self.servo_input = (tag.cross_track, tag.heading_error, steering)
            # 소실 대비로 "마지막에 본 거리 / 그때까지 간 거리" 를 남긴다.
            self._sight = (tag.along, self.traveled_m)
        else:
            # 태그 소실 — 단계 진입 시 heading 을 유지하며 전진한다.
            # overrun 감시는 계속 돌아간다.
            #
            # ★ 다만 **trigger 직전에 놓친 경우**는 그냥 직진하면 안 된다.
            #   코너 진입 1.0 m 에서 태그가 화각을 벗어나는 일이 실제로
            #   생긴다 (반화각 25.8° → 1.0 m 에서 |cross|>0.48 m 면 프레임
            #   이탈). 그러면 이 단계가 끝나지 못하고 코너를 지나쳐 직진해
            #   FAULT_OVERRUN 이 된다 (2026-08-05 현장).
            #
            #   "안 보이면 바로 회전" 은 위험하다 — 멀리서 놓쳤을 때도
            #   돌아버린다. 대신 **마지막으로 본 거리에서 남은 만큼을
            #   엔코더로 채우고** 넘어간다. 엔코더 거리는 실측으로 신뢰할
            #   수 있다 (1.374 m vs 오도메트리 1.372 m).
            if self._coast_done(s):
                self.advance_reason = 'coast'
                self._advance(inp)
                return self.last_command if self.state in TERMINAL else \
                    self.tick(inp)
            speed = self.cfg.v_approach
            steering = steering_for_heading_hold(
                wrap_pi(self._entry_yaw - inp.yaw_rad), self.steer)

        steering = self._limit_correction(steering)
        cmd = Command(int(speed), clamp_steering(steering, self.steer), True)
        self.last_command = cmd
        return cmd

    def _arrival(self, s: Step, tag: TagTarget, inp: Inputs):
        """단계를 끝낼 때가 됐는가. (완료여부, 사유) 를 준다."""
        if tag.along > s.trigger_m:
            self._aligned_since = None
            return False, ''
        # 정렬 조건이 없는 단계(코너 진입)는 예전대로 거리만 본다.
        if s.align_cross_m <= 0.0 and s.align_head_deg <= 0.0:
            return True, 'tag'
        head_deg = math.degrees(tag.heading_error)
        ok = (abs(tag.cross_track) <= s.align_cross_m
              and abs(head_deg) <= s.align_head_deg)
        # ★ 거리 하한을 **먼저** 본다. 여기는 앞머리가 태그에 닿기 전 한계라
        #   정렬 여부와 무관하게 끝낸다. 뒤에 두면 하한을 지난 뒤에도 정렬
        #   유지시간을 기다리며 태그로 더 파고든다.
        if tag.along <= s.trigger_m - self.cfg.align_extra_m:
            self.aligned = ok
            if not ok:
                self.align_miss = (tag.cross_track, head_deg)
            return True, 'tag' if ok else 'align_floor'
        if ok:
            # 한 표본으로 판정하지 않는다 — 공칭 내부파라미터라 heading 이
            # 떨린다. 조건을 연속으로 유지해야 정렬로 인정한다.
            if self._aligned_since is None:
                self._aligned_since = inp.now
            if inp.now - self._aligned_since >= self.cfg.align_hold_s:
                self.aligned = True
                return True, 'tag'
            return False, ''
        # 정렬이 안 됐다 — 하한까지 접근 구간을 조금 더 쓴다.
        self._aligned_since = None
        return False, ''

    def _coast_done(self, s: Step) -> bool:
        """태그를 놓친 뒤 엔코더로 trigger 까지 채웠는가."""
        if self._sight is None:
            return False
        last_along, at = self._sight
        remaining = last_along - s.trigger_m
        # 멀리서 놓친 경우는 추측항법을 믿지 않는다. heading 드리프트가
        # 쌓여서 코너 위치가 어긋나고, 그러면 회전 자체가 무의미해진다.
        if remaining > self.cfg.tag_lost_coast_max_m:
            return False
        return (self.traveled_m - at) >= max(0.0, remaining)

    def _limit_correction(self, steering: int) -> int:
        """보정을 좌우 같은 크기로 제한한다 (drive 단계 전용)."""
        # 조향 트림(+500)이 좌측 실효 한계를 +1455 로 깎기 때문에, 제한이
        # 없으면 우측만 -2869 까지 쓸 수 있어 보정 권한이 2배 비대칭이 된다.
        # 게인을 방향별로 다르게 하면 선형 구간의 감쇠비까지 좌우가 달라지므로,
        # **게인은 그대로 두고 한계만 맞춘다.** 0 이면 제한하지 않는다.
        lim = self.cfg.correction_limit_cdeg
        if lim <= 0:
            return steering
        return max(-lim, min(lim, steering))

    def _tick_turn(self, s: Step, inp: Inputs) -> Command:
        left = s.turn_deg > 0
        target = abs(s.turn_deg)
        lead = (self.cfg.stop_lead_left_deg if left
                else self.cfg.stop_lead_right_deg)
        if lead <= 0.0:
            lead = self.cfg.turn_eps_deg
        # 목표 방향으로의 서명 진행량 [deg]. 반대로 돌면 음수.
        progress = math.degrees(self._turned) * (1.0 if left else -1.0)

        # ── 가드 (JETSON_YAW90 §11). 완료 판정보다 먼저 본다 —
        #    비현실적 점프가 목표를 "넘겨준" 것을 완료로 믿으면 안 된다.
        jump = abs(math.degrees(self._last_yaw_step))
        if jump > self.cfg.yaw_jump_limit_deg:
            return self._fault(
                f'비현실적 yaw 점프 {jump:.1f}°/tick — parser·타임스탬프·'
                f'MCU 재부팅 확인 필요')
        if progress < -self.cfg.wrong_way_limit_deg:
            return self._fault(
                f'명령 반대 방향으로 {-progress:.1f}° 진행 — '
                f'조향·yaw 부호 확인 필요')
        if self.traveled_m > s.max_m:
            return self._fault(
                f'회전 거리 가드 — {self.traveled_m:.2f} m 이동 '
                f'(상한 {s.max_m:.2f} m), yaw 진행 {progress:+.1f}° 뿐')
        # 정지 중 IMU_FUSED=0 은 정상(융합 최소속도 미달). 움직이기 시작해
        # 유예가 지나도 0 이면 yaw 를 믿을 수 없다.
        #
        # ★ 단, **연속** dropout 만 FAULT 로 본다. 실장비는 IMU_FUSED 를
        #   깜빡이고, 한 표본으로 죽이면 회전이 시작 직후 반드시 실패한다
        #   (2026-08-05 실측: 34 ms dropout 에 FAULT). 자세한 근거는
        #   RouteConfig.fusion_dropout_grace_s 주석에 있다.
        #
        # ★ yaw 를 0x83 IMU quaternion 에서 직접 읽고 있으면 이 가드 자체가
        #   무의미하다 — STM32 가 융합했는지는 우리 yaw 정확도와 무관하다.
        #   그래서 `yaw_is_imu` 면 건너뛴다.
        if abs(inp.speed_mm_s) >= 20.0 and not inp.yaw_is_imu:
            if self._moving_since is None:
                self._moving_since = inp.now
            if (self.cfg.require_imu_fused
                    and inp.now - self._moving_since > self.cfg.fusion_grace_s):
                if inp.imu_fused:
                    self._fusion_lost_since = None
                else:
                    if self._fusion_lost_since is None:
                        self._fusion_lost_since = inp.now
                    lost = inp.now - self._fusion_lost_since
                    if lost > self.cfg.fusion_dropout_grace_s:
                        return self._fault(
                            f'IMU_FUSED=0 이 {lost:.2f}s 연속 — 융합 yaw 없이 '
                            f'회전 불가 (허용 '
                            f'{self.cfg.fusion_dropout_grace_s:.2f}s)')
        else:
            self._moving_since = None
            self._fusion_lost_since = None

        if progress >= target - lead:
            self._advance(inp)
            return self.last_command if self.state in TERMINAL else \
                self.tick(inp)

        if not self.clearance.continue_allowed(self.status()):
            return self._fault('회전 중 경로 공간 차단 (clearance)')

        # 이론 소요시간 = 호길이 / 속도. 그 배수를 넘으면 뭔가 잘못됐다.
        v = self.cfg.v_turn / 1000.0
        theory = (math.radians(target) * s.radius_m) / max(1e-6, v)
        # HOLD 로 보낸 시간은 제외한다 (문서 §6) — 장애물 앞에 오래 서 있었다는
        # 이유만으로 회전 타임아웃이 터지면 자동 재개가 무의미해진다.
        active = self.active_elapsed_s(inp.now)
        if active > theory * self.cfg.turn_timeout_factor:
            return self._fault(
                f'회전 타임아웃 — {self.turned_deg:+.1f}° / 목표 {s.turn_deg:+.1f}° '
                f'(주행 {active:.1f}s, HOLD 제외, 이론 {theory:.1f}s)')

        # 마무리 구간 감속 — 오버슈트를 줄여 stop lead 를 작게 유지한다
        remaining = target - progress
        speed = (self.cfg.v_turn_slow
                 if remaining <= self.cfg.slow_remaining_deg
                 else self.cfg.v_turn)
        if s.steering_cdeg:
            steering = clamp_steering(s.steering_cdeg, self.steer)
        else:
            steering = turn_steering(s.radius_m, self.cfg.wheelbase_m,
                                     left=left, cfg=self.steer)
        cmd = Command(speed, steering, True)
        self.last_command = cmd
        return cmd

    # ── 웹·진단용 요약 ─────────────────────────────────────
    def status(self) -> dict:
        s = self.step
        return {
            'state': self.state,
            'mission': self.mission,
            'step_index': self.index,
            'step_total': len(self.steps),
            'step_name': s.name if s else '',
            'step_kind': s.kind if s else '',
            'target_tag_id': s.tag_id if s and s.kind == 'drive' else 0,
            'traveled_m': round(self.traveled_m, 3),
            'turned_deg': round(self.turned_deg, 2),
            'tag_seen': self._tag_seen,
            'drive_mode': self.drive_mode,
            'advance_reason': self.advance_reason,
            'aligned': self.aligned,
            'align_miss_cross_m': (None if self.align_miss is None
                                   else round(self.align_miss[0], 3)),
            'align_miss_head_deg': (None if self.align_miss is None
                                    else round(self.align_miss[1], 2)),
            'fault_reason': self.fault_reason,
            'hold_reason': self.hold_reason or '',
            'hold_substate': self.hold_substate or '',
            'hold_elapsed_s': round(self._hold_elapsed_s, 3),
            'command': {
                'speed_mm_s': self.last_command.speed_mm_s,
                'steering_cdeg': self.last_command.steering_cdeg,
                'enable': self.last_command.enable,
            },
        }


def tag_target_to_dict(t: TagTarget) -> dict:
    """TagTarget -> JSON 직렬화용 dict.

    `robot_interfaces` 전용 메시지가 생기기 전 임시 형식이다. 제어가 소비하는
    값이므로 변환을 이 한 곳에만 두고 시험으로 고정한다 — 여러 곳에서 키를
    직접 쓰면 오타가 런타임에만 드러난다.
    """
    return {
        'tag_id': t.tag_id, 'valid': t.valid, 'reason': t.reason,
        'd_x': t.d_x, 'd_y': t.d_y, 'range_m': t.range_m,
        'bearing': t.bearing, 'cross_track': t.cross_track,
        'heading_error': t.heading_error, 'along': t.along,
        'normal_yaw': t.normal_yaw, 'decision_margin': t.decision_margin,
        'pixel_width': t.pixel_width, 'age_s': t.age_s,
    }


def tag_target_from_dict(d: dict) -> TagTarget:
    """dict -> TagTarget. 없는 키는 기본값으로 둔다."""
    return replace(TagTarget(), **{k: v for k, v in d.items()
                                   if k in TagTarget.__dataclass_fields__})
