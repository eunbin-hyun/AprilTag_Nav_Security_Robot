"""AprilTag 인식 + 카메라 미리보기 상시 실행.

주행과 분리해서 항상 띄워 둔다. `tag_drive.launch.py` 를 재시작하거나 종료해도
카메라 화면(:8090)과 `/tag/target` 은 유지된다.

    # 상시 실행 (부팅 자동 실행은 systemd/tag-perception.service)
    ros2 launch robot_bringup tag_perception.launch.py

    # 카메라 장치를 고정 경로로 (재부팅 후 video 번호가 바뀔 수 있다)
    ros2 launch robot_bringup tag_perception.launch.py \\
        cam_device:=/dev/v4l/by-id/usb-046d_Brio_100_2515ZBE1XJY8-video-index0

기동되는 것:
    camera_static_tf    base_link -> camera_optical_frame
    tag_localizer_cv    카메라 + 태그 검출 + /tag/target + 미리보기(HTTP)

⚠ **두 노드를 함께 옮겨야 한다.** `tag_localizer_cv` 는 태그 pose 를 base_link
  기준으로 바꾸려고 tf2 를 쓴다. static TF 를 주행 launch 에 두고 오면, 주행을
  안 띄운 동안 `/tag/target` 이 TF 없음으로 계속 무효가 된다.

⚠ **카메라는 한 프로세스만 열 수 있다.** 이 launch 가 떠 있는 동안
  `tag_localizer_cv` 를 따로 실행하거나 `scripts/tag_drive.py` 를 돌리면
  `/dev/video0` 점유 충돌이 난다. 8090 포트도 같다.
      점유 확인:  fuser -v /dev/video0   ·   ss -ltnp | grep 8090
"""
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    cam_device = LaunchConfiguration('cam_device')
    cam_x = LaunchConfiguration('cam_x')
    cam_y = LaunchConfiguration('cam_y')
    cam_z = LaunchConfiguration('cam_z')

    args = [
        DeclareLaunchArgument('cam_device', default_value='/dev/video0'),
        #    z = 지면->렌즈중심, x = 뒷축중심->렌즈(전방 +)
        # cam_z 실측 2026-08-04: 0.215 m. **태그 중심 높이도 같은 0.215 m** 로
        # 맞춰 놓았다 — 같게 두면 태그가 화면 수직 중앙에 오고 근접에서
        # 프레임 이탈 여유가 최대가 된다. 태그 높이를 바꾸면 이 값도 같이 바꿀 것.
        # ⚠ cam_x 는 아직 임시값이다 (조립 후 미실측). 뒷차축 중심에서
        #    렌즈까지 전방 거리를 재서 교체할 것 — 이 오차는 정지 위치에
        #    1:1 로 실린다.
        DeclareLaunchArgument('cam_x', default_value='0.12'),
        DeclareLaunchArgument('cam_y', default_value='0.0'),
        DeclareLaunchArgument('cam_z', default_value='0.215'),
        # 실측 2026-08-04: 채택 인쇄물 13.5×13.2 cm (프린터 급지축 축소).
        # 평균 0.1335. 태그를 재인쇄하면 검은 사각형 변을 재서 갱신할 것 —
        # 이 값이 틀리면 모든 보고 거리가 같은 비율로 틀어진다.
        DeclareLaunchArgument('tag_size_m', default_value='0.1335'),
        DeclareLaunchArgument('exposure', default_value='400'),
        DeclareLaunchArgument('preview_port', default_value='8090'),
        # 관제 화면 송출률 [fps]. 인터페이스 계약 §10.1 이 5~10 을 요구한다.
        # ⚠ 카메라 프레임률·태그 인식은 이 값과 무관하다 — 그걸 낮추면 주행
        #   제어 주기가 같이 느려진다. JPEG 굽는 횟수만 줄인다. 0 = 제한 없음
        DeclareLaunchArgument('preview_fps', default_value='7.0'),
        # minimal = DETECT/NO TAG 를 색으로만, debug = 초점거리·태그크기 포함
        DeclareLaunchArgument('preview_overlay', default_value='minimal'),
        DeclareLaunchArgument('preview_jpeg_quality', default_value='80'),
        # 송출 전 가로폭 상한 [px]. 0 = 원본. 대역폭을 줄이는 가장 큰 레버다.
        # 960 은 화면 쪽 찬성값이다 (2026-08-06 백엔드발 §4). 640(360p)은 태그가
        # 안 읽힐 수 있다는 것이 사용자 판단이라 그 위로 잡았다.
        # ⚠ 태그 인식은 이 값과 무관하다 — 원본 프레임으로 검출한 뒤 굽기 전에만
        #   줄인다. 관제 화면에서 태그가 읽히는지는 눈으로 한 번 확인할 것.
        DeclareLaunchArgument('preview_max_width', default_value='960'),
        # 태그 법선 축 규약. 정면 1 m 에서 along 이 음수로 나오면 true.
        # (2026-08-04 확인: false 가 맞다 — along +1.90, head +1.1°)
        DeclareLaunchArgument('normal_flip', default_value='false'),
    ]

    # 광학 좌표계 규약: 부모 x전방/y좌/z상 -> 자식 z전방/x우/y하.
    # rpy = (-pi/2, 0, -pi/2). 이 회전을 빠뜨리면 태그 좌우·상하가 뒤집혀
    # 조향이 반대로 나가고 원인 찾기가 매우 어렵다.
    static_tf = Node(
        package='tf2_ros', executable='static_transform_publisher',
        name='camera_static_tf',
        arguments=['--x', cam_x, '--y', cam_y, '--z', cam_z,
                   '--roll', '-1.5707963', '--pitch', '0',
                   '--yaw', '-1.5707963',
                   '--frame-id', 'base_link',
                   '--child-frame-id', 'camera_optical_frame'],
        output='log')

    localizer = Node(
        package='robot_perception', executable='tag_localizer_cv',
        name='tag_localizer_cv', output='screen',
        parameters=[{
            'device': cam_device,
            'tag_size_m': ParameterValue(
                LaunchConfiguration('tag_size_m'), value_type=float),
            'exposure': ParameterValue(
                LaunchConfiguration('exposure'), value_type=int),
            'preview_port': ParameterValue(
                LaunchConfiguration('preview_port'), value_type=int),
            'normal_flip': ParameterValue(
                LaunchConfiguration('normal_flip'), value_type=bool),
            'preview_fps': ParameterValue(
                LaunchConfiguration('preview_fps'), value_type=float),
            'preview_overlay': ParameterValue(
                LaunchConfiguration('preview_overlay'), value_type=str),
            'preview_jpeg_quality': ParameterValue(
                LaunchConfiguration('preview_jpeg_quality'), value_type=int),
            'preview_max_width': ParameterValue(
                LaunchConfiguration('preview_max_width'), value_type=int),
        }])

    return LaunchDescription(args + [static_tf, localizer])
