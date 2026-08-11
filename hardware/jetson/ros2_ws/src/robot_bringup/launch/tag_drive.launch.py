"""AprilTag 주행 — 상태머신과 STM32 통신. **카메라는 여기서 안 띄운다.**

★ 카메라·태그 인식은 `tag_perception.launch.py` 가 상시 실행으로 담당한다.
  이 launch 는 `/tag/target` 을 **구독**할 뿐이다. 그래서 주행을 몇 번이든
  재시작해도 카메라 화면(:8090)이 끊기지 않는다.

    # 먼저 (한 번만, 또는 systemd 로 부팅 자동 실행)
    ros2 launch robot_bringup tag_perception.launch.py

    # STM32 미연결 (기본) — 가짜 STM32 로 오도메트리를 채우고 명령만 확인
    ros2 launch robot_bringup tag_drive.launch.py

    # 실장비 STM32 연결
    ros2 launch robot_bringup tag_drive.launch.py use_fake:=false \\
        stm32_port:=/dev/ttyUSB0

    # 태그 하나로 체인만 검증 (코스 불필요)
    ros2 launch robot_bringup tag_drive.launch.py approach_tag_id:=1
    ros2 service call /route/approach std_srvs/srv/Trigger

기동되는 것:
    socat + fake_stm32        use_fake:=true 일 때만. 오도메트리 공급원.
    route_runner              상태머신 + UART 송신 + 명령 로그

확인 방법:
    카메라 화면   http://localhost:8090   (tag_perception 이 제공)
    명령 로그     이 터미널에 CMD_DRIVE 가 바뀔 때마다 출력
    상태 요약     ros2 run 없이 -> scripts/route_monitor.py --tags
    미션 시작     ros2 service call /route/start std_srvs/srv/Trigger

⚠ **`tag_perception.launch.py` 가 떠 있어야 태그가 보인다.** 안 띄우면
  `/tag/target` 이 없어서 모든 drive 단계가 "태그를 한 번도 못 봤다" 로
  FAULT_OVERRUN 이 된다. 카메라 위치(`cam_*`)·태그 크기(`tag_size_m`)·
  노출·`normal_flip` 인자도 전부 그쪽으로 옮겼다.

⚠ **STM32 가 없으면 오도메트리도 없다.** 상태머신은 yaw·주행거리가 있어야
  회전 종료와 거리 상한을 판정하므로, `use_fake:=true` 로 가짜 STM32 를 함께
  띄운다. 이 상태에서도 검출·제어·명령 생성은 전부 실제 경로를 탄다 —
  UART 상대만 가짜다.

⚠ `ROS_DISCOVERY_SERVER` 가 설정돼 있으면 토픽·서비스가 보이지 않는다.
  Discovery Server 를 쓰지 않기로 했으므로 `.bashrc` 에서 주석 처리하거나
  터미널마다 `unset ROS_DISCOVERY_SERVER` 를 한다. systemd 로 인식 노드를
  띄울 때도 같은 설정이어야 한다 — 한쪽만 Discovery Server 를 쓰면 서로를
  못 본다.
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    use_fake = LaunchConfiguration('use_fake')
    stm32_port = LaunchConfiguration('stm32_port')
    approach_tag = LaunchConfiguration('approach_tag_id')
    leg_max = LaunchConfiguration('leg_max_m')

    args = [
        DeclareLaunchArgument('use_fake', default_value='true',
                              description='가짜 STM32(socat+fake_stm32) 사용'),
        # use_fake:=true 면 socat 이 만드는 가상 포트를 쓴다.
        DeclareLaunchArgument('stm32_port', default_value='/tmp/ttyJETSON'),
        # 카메라 인자(cam_device/cam_x/cam_y/cam_z/tag_size_m/exposure/
        # preview_port/normal_flip)는 tag_perception.launch.py 로 옮겼다.
        # 여기 남겨 두면 카메라·8090·노드이름·TF 가 중복된다.
        # /route/approach 가 향할 태그. 코스를 깔지 않고 태그 하나로
        # "인식 -> 접근 -> 정지" 체인만 검증할 때 쓴다.
        DeclareLaunchArgument('approach_tag_id', default_value='2'),
        # 태그를 못 본 채 이만큼 주행하면 FAULT_OVERRUN 으로 멈춘다.
        # 실제 구간은 2 m 지만 시운전 중에는 여유를 둔다.
        DeclareLaunchArgument('leg_max_m', default_value='3.0'),
        # 속도 [mm/s]. 기본값은 노드 기본과 동일 (fake·e2e 재현성 유지).
        #
        # ⚠ 예전 주석의 "313 이하로는 출발하지 못한다 (2026-08-03 실측)" 은
        #   **폐기했다.** 그 측정은 모터가 제대로 돌지 않던 시기(전원·드라이버
        #   문제, 이후 해결) 것이다. 2026-08-05 실측으로 반증됐다:
        #       명령 150 → 실측 122~171 mm/s
        #       명령 200 → 실측 195~220 mm/s   (duty 195~340‰)
        #   이 속도로 30°/90° 회전이 ±1° 오차로 완료됐다. 낡은 주석을 믿고
        #   "차가 못 움직여서 보정이 안 된다" 로 오진한 사례가 있어 남긴다.
        DeclareLaunchArgument('v_cruise_mm_s', default_value='200'),
        DeclareLaunchArgument('v_approach_mm_s', default_value='150'),
        DeclareLaunchArgument('v_turn_mm_s', default_value='200'),
        # 접근 정지 판정 거리 [m, 뒷차축 기준]. 앞머리는 이보다 0.27 m 가깝다.
        DeclareLaunchArgument('approach_trigger_m', default_value='0.7'),
        # ── 조향 중립 트림 [cdeg]. **실측 2026-08-04: +500 이 직진이다.**
        # cdeg 0 은 우측 약 5° 로, 트림 없이 달리면 "직진" 이 계속 우측으로
        # 밀려 태그를 놓치고 이탈한다 (그 증상을 실제로 봤다).
        # 기계 재트림이 정답이지만, 그 전까지 이 값으로 주행 가능하다.
        DeclareLaunchArgument('steer_trim_cdeg', default_value='500'),
        # 조향 게인. 기본값은 노드 기본과 동일 (e2e 재현성 유지).
        # 실차 튜닝: 과보정(지그재그·이탈)이면 낮추고, 복귀가 굼뜨면 올린다.
        # 축간거리 0.135 m 라 작은 각도로도 곡률이 크다 — 낮은 쪽부터 시작.
        DeclareLaunchArgument('kp_cross', default_value='4000.0'),
        DeclareLaunchArgument('kp_heading', default_value='1200.0'),
        DeclareLaunchArgument('kp_yaw_hold', default_value='1500.0'),
        # ── 상대 yaw 회전 (JETSON_YAW90). 30° 저속 시험 → 90° 순서.
        # v_turn_slow: 인수인계 권장값 90 은 실바닥에서 검증된 적이 없다.
        # 성공한 30°/90° 회전(2026-08-05)은 전부 150 이상으로 돌렸고,
        # 명령 150 일 때 실측이 122 mm/s 였다. 90 이면 마지막 15° 에서
        # 바퀴가 멈출 수 있어 150 으로 올렸다.
        DeclareLaunchArgument('turn_target_deg', default_value='90.0'),
        DeclareLaunchArgument('turn_steering_cdeg', default_value='1800'),
        DeclareLaunchArgument('v_turn_slow_mm_s', default_value='150'),
        DeclareLaunchArgument('stop_lead_left_deg', default_value='3.0'),
        DeclareLaunchArgument('stop_lead_right_deg', default_value='3.0'),
        # 실측 2026-08-05: 전선 -1800 에서 회전반경 0.685 m (순수 IMU 기준).
        # 90° 에 1.08 m 가 필요하므로 예전 0.90 은 **정상 회전도** 거리 가드에
        # 걸린다. 2.5 로 올려 실측 반경 + 여유를 준다.
        DeclareLaunchArgument('turn_max_m', default_value='2.5'),
        # 코너 진입 거리 — 회전반경 R 만큼 앞에서 시작해야 접선이 맞는다.
        # 트림 +500 이 좌우 R 을 1.5 배 벌려놔서 실측 R 은 좌 0.63 / 우 0.98 m
        # 다. 좌(태그4 진입)는 2026-08-06 지시로 R 보다 앞선 1.00 m 를 쓴다 —
        # 태그가 보이는 동안 돌지만 접선은 어긋나고, 예전 e2e 에서는 회전 후
        # 태그1 재획득에 실패한 적이 있다. 되돌리려면 0.65.
        DeclareLaunchArgument('corner_trigger_right_m', default_value='1.00'),
        DeclareLaunchArgument('corner_trigger_left_m', default_value='1.00'),
        # 종점 정지 거리. corner_trigger_* 과 같은 **뒷차축 기준**이다.
        # 도착(태그3)은 가까이, 도크(태그1)는 충전 접점 여유를 둔다.
        # 도크는 2026-08-06 지시로 0.65 (앞머리 여유 0.38 m). 접점 여유가
        # 모자라면 dock_trigger_m 만 1.00 으로 되돌린다.
        DeclareLaunchArgument('arrive_trigger_m', default_value='0.65'),
        DeclareLaunchArgument('dock_trigger_m', default_value='0.65'),
        # 표시용 — 로그·JSON 에 앞머리 여유를 같이 찍는다 (판정에 미사용).
        DeclareLaunchArgument('front_overhang_m', default_value='0.27'),
        # 코너 진입에서 태그가 화각을 벗어나면(반화각 25.8° → 1.0 m 에서
        # |cross| 0.48 m 초과) 단계가 안 끝나 코너를 지나친다. 마지막으로
        # 본 거리에서 남은 만큼을 엔코더로 채워 넘어간다. 0 이면 비활성.
        DeclareLaunchArgument('tag_lost_coast_max_m', default_value='0.8'),
        # drive 보정 상한. 트림 +500 이 좌측 실효 한계를 +1455 로 깎으므로
        # 우측도 같은 크기로 맞춰 보정 권한 비대칭(2배)을 없앤다.
        DeclareLaunchArgument('correction_limit_cdeg', default_value='1455'),
        # 종점(태그3 도착·태그1 도킹) 정렬 조건 — 거리와 **함께** 요구한다.
        # 코너 진입은 yaw 로 회전하므로 적용하지 않는다.
        # ⚠ 정렬을 '고치는' 장치가 아니다. 거리 시정수가 0.6 m 라 마지막
        #   10 cm 로는 거의 못 고친다. 정렬될 때까지 완료를 보류하고, 안
        #   되면 완료하되 실패를 드러내는 역할이다 (aligned=false + WARN).
        # align_head_deg 5.0 은 공칭 내부파라미터를 고려한 값이다.
        # 캘리브레이션 후 조일 수 있다. 0 이면 거리만 본다 (예전 동작).
        DeclareLaunchArgument('align_cross_m', default_value='0.04'),
        DeclareLaunchArgument('align_head_deg', default_value='5.0'),
        DeclareLaunchArgument('align_hold_s', default_value='0.2'),
        DeclareLaunchArgument('align_extra_m', default_value='0.05'),
        # 통신단절 latch FAULT 문턱 [s]. 넘으면 사람이 reset_fault 를 해야
        # 재개되므로 느슨하게 잡는다. 장애물 정지는 STM32 가 자율 수행하고
        # 우리 프레임 끊김은 STM32 watchdog 이 잡으므로 안전하다.
        DeclareLaunchArgument('telemetry_timeout_s', default_value='0.5'),
        # 회전 판정용 0x83 yaw 허용 지연 [s]. 위와 **달라야 한다** —
        # 오래된 yaw 로 판정하면 과회전한다 (30°/s × 0.3 s = 9°).
        DeclareLaunchArgument('imu_stale_s', default_value='0.3'),
        # yaw 출처: auto|imu|odom. auto 는 0x83 IMU quaternion 우선.
        # 0x85 융합 yaw 는 모델 성분 25% 때문에 실측 1.275 배 부풀려진다 —
        # 그걸로 90° 를 판정하면 물리적으로 71° 에서 멈춘다.
        DeclareLaunchArgument('yaw_source', default_value='auto'),
        # `/stm32/odom` 의 pose 출처. tick = 주행이 그 tick 에 쓴 값
        # (0x83 yaw + 엔코더 재적분), stm32 = 0x85 원본 (예전 동작).
        # ⚠ tick 은 0x83 이 있어야 발행한다. BNO085 가 빠졌거나 accuracy 가
        #   낮으면 **관제 지도에 로봇이 안 뜬다** — 폴백하면 두 yaw 의 원점이
        #   달라 지도가 튀기 때문에 일부러 안 보낸다. 시연 중 그 상황이면
        #   odom_pose_source:=stm32 로 되돌린다 (주행에는 영향 없다).
        DeclareLaunchArgument('odom_pose_source', default_value='tick'),
        # 회전 단계는 융합 yaw 를 요구한다. BNO085 gyro accuracy 가 0 이라
        # IMU_FUSED 가 0 으로 머무르면 0.7 s 유예 후 FAULT 로 멈춘다.
        # ⚠ false 로 끄면 STM32 COMMAND_ESTIMATE yaw 로 회전을 판정하는데,
        #   조향 트림이 걸려 있으면 그 yaw 가 드리프트한다 — 회전량이 틀린다.
        DeclareLaunchArgument('require_imu_fused', default_value='true'),
        # 융합 dropout 허용 시간. 실장비는 IMU_FUSED 를 깜빡이므로 한 표본
        # dropout 으로 회전을 죽이면 안 된다 (실측 최장 542 ms).
        # ⚠ 이 시간 동안은 모델 yaw 로 버티므로 steer_trim_cdeg 가 0 이어야
        #   안전하다 — 트림이 걸리면 모델 yaw 가 부풀려져 있다.
        DeclareLaunchArgument('fusion_dropout_grace_s', default_value='0.8'),
        # 장애물 clear 후 재개까지 안정 대기 (flicker 방지)
        DeclareLaunchArgument('obstacle_clear_hold_s', default_value='0.5'),
    ]

    fake_on = IfCondition(use_fake)

    # 가상 UART 한 쌍. -d -d 로 링크 생성 로그를 남긴다.
    socat = ExecuteProcess(
        cmd=['socat', 'pty,raw,echo=0,link=/tmp/ttyJETSON',
             'pty,raw,echo=0,link=/tmp/ttySTM32'],
        output='log', condition=fake_on)

    # socat 이 심볼릭 링크를 만들 때까지 기다렸다가 붙는다. 바로 띄우면
    # /tmp/ttySTM32 가 아직 없어서 실패한다.
    fake = ExecuteProcess(
        cmd=['bash', '-c',
             'for i in $(seq 1 40); do [ -e /tmp/ttySTM32 ] && break; '
             'sleep 0.1; done; '
             'exec ros2 run stm32_bridge fake_stm32 --port /tmp/ttySTM32'],
        output='screen', condition=fake_on)

    # camera_static_tf 와 tag_localizer_cv 는 tag_perception.launch.py 로
    # 옮겼다 (상시 실행). 여기서 다시 띄우면 /dev/video0 점유·8090 포트·
    # 노드 이름·TF 가 전부 중복된다.

    # route_runner 는 UART 를 직접 소유한다. 포트는 한 프로세스만 열 수
    # 있으므로 stm32_monitor 나 drive_test_suite 를 동시에 띄우면 안 된다.
    runner = Node(
        package='robot_navigation', executable='route_runner',
        name='route_runner', output='screen',
        parameters=[{
            'serial_port': PythonExpression(
                ["'/tmp/ttyJETSON' if '", use_fake, "'.lower() == 'true' "
                 "else '", stm32_port, "'"]),
            'log_commands': True,
            'approach_tag_id': ParameterValue(approach_tag, value_type=int),
            'v_cruise_mm_s': ParameterValue(
                LaunchConfiguration('v_cruise_mm_s'), value_type=int),
            'v_approach_mm_s': ParameterValue(
                LaunchConfiguration('v_approach_mm_s'), value_type=int),
            'v_turn_mm_s': ParameterValue(
                LaunchConfiguration('v_turn_mm_s'), value_type=int),
            'approach_trigger_m': ParameterValue(
                LaunchConfiguration('approach_trigger_m'), value_type=float),
            'kp_cross': ParameterValue(
                LaunchConfiguration('kp_cross'), value_type=float),
            'kp_heading': ParameterValue(
                LaunchConfiguration('kp_heading'), value_type=float),
            'kp_yaw_hold': ParameterValue(
                LaunchConfiguration('kp_yaw_hold'), value_type=float),
            'steer_trim_cdeg': ParameterValue(
                LaunchConfiguration('steer_trim_cdeg'), value_type=int),
            'require_imu_fused': ParameterValue(
                LaunchConfiguration('require_imu_fused'), value_type=bool),
            'fusion_dropout_grace_s': ParameterValue(
                LaunchConfiguration('fusion_dropout_grace_s'),
                value_type=float),
            'yaw_source': LaunchConfiguration('yaw_source'),
            'odom_pose_source': LaunchConfiguration('odom_pose_source'),
            'turn_max_m': ParameterValue(
                LaunchConfiguration('turn_max_m'), value_type=float),
            'corner_trigger_right_m': ParameterValue(
                LaunchConfiguration('corner_trigger_right_m'),
                value_type=float),
            'corner_trigger_left_m': ParameterValue(
                LaunchConfiguration('corner_trigger_left_m'),
                value_type=float),
            'arrive_trigger_m': ParameterValue(
                LaunchConfiguration('arrive_trigger_m'), value_type=float),
            'dock_trigger_m': ParameterValue(
                LaunchConfiguration('dock_trigger_m'), value_type=float),
            'front_overhang_m': ParameterValue(
                LaunchConfiguration('front_overhang_m'), value_type=float),
            'tag_lost_coast_max_m': ParameterValue(
                LaunchConfiguration('tag_lost_coast_max_m'),
                value_type=float),
            'correction_limit_cdeg': ParameterValue(
                LaunchConfiguration('correction_limit_cdeg'), value_type=int),
            'align_cross_m': ParameterValue(
                LaunchConfiguration('align_cross_m'), value_type=float),
            'align_head_deg': ParameterValue(
                LaunchConfiguration('align_head_deg'), value_type=float),
            'align_hold_s': ParameterValue(
                LaunchConfiguration('align_hold_s'), value_type=float),
            'align_extra_m': ParameterValue(
                LaunchConfiguration('align_extra_m'), value_type=float),
            'telemetry_timeout_s': ParameterValue(
                LaunchConfiguration('telemetry_timeout_s'), value_type=float),
            'imu_stale_s': ParameterValue(
                LaunchConfiguration('imu_stale_s'), value_type=float),
            'turn_target_deg': ParameterValue(
                LaunchConfiguration('turn_target_deg'), value_type=float),
            'turn_steering_cdeg': ParameterValue(
                LaunchConfiguration('turn_steering_cdeg'), value_type=int),
            'v_turn_slow_mm_s': ParameterValue(
                LaunchConfiguration('v_turn_slow_mm_s'), value_type=int),
            'stop_lead_left_deg': ParameterValue(
                LaunchConfiguration('stop_lead_left_deg'), value_type=float),
            'stop_lead_right_deg': ParameterValue(
                LaunchConfiguration('stop_lead_right_deg'), value_type=float),
            'obstacle_clear_hold_s': ParameterValue(
                LaunchConfiguration('obstacle_clear_hold_s'),
                value_type=float),
            'approach_max_m': ParameterValue(leg_max, value_type=float),
            'leg_max_m': ParameterValue(leg_max, value_type=float),
        }])

    return LaunchDescription(args + [socat, fake, runner])
