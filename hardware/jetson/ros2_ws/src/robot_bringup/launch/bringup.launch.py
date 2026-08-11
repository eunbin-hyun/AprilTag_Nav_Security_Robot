"""Jetson 필수 노드 통합 기동.

    ros2 launch robot_bringup bringup.launch.py                  # 전부
    ros2 launch robot_bringup bringup.launch.py use_lidar:=false # bridge 만
    ros2 launch robot_bringup bringup.launch.py \
        stm32_port:=/tmp/ttyJETSON                               # socat 개발

여기 넣지 않는 것과 이유:
- Discovery Server: launch 재시작마다 서버가 죽으면 PC RViz 포함 전체가
  서로를 잃는다. systemd 로 상시 유지한다 (systemd/ 참고).
- RViz: PC 쪽 관측 도구. rviz/ 의 설정 파일을 PC 에서 -d 로 연다.

전제: ydlidar_ros2_driver 워크스페이스가 source 되어 있어야 한다
(~/ydlidar_x4pro/ros2_ws). 이 저장소에는 서드파티라 포함하지 않는다.
"""
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.conditions import IfCondition
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    bringup_share = get_package_share_directory('robot_bringup')

    use_lidar = LaunchConfiguration('use_lidar')
    use_bridge = LaunchConfiguration('use_bridge')
    stm32_port = LaunchConfiguration('stm32_port')
    lidar_params = LaunchConfiguration('lidar_params')

    # X4 Pro 는 기본 ydlidar.yaml(2채널·230400)로는 기동 실패한다.
    # 반드시 X4-Pro.yaml(단일채널·128000)을 쓴다.
    try:
        default_lidar_params = os.path.join(
            get_package_share_directory('ydlidar_ros2_driver'),
            'params', 'X4-Pro.yaml')
    except Exception:                                    # noqa: BLE001
        # 라이다 워크스페이스 미 source 상태. use_lidar:=false 로는 쓸 수 있다.
        default_lidar_params = ''

    args = [
        DeclareLaunchArgument('use_lidar', default_value='true'),
        DeclareLaunchArgument('use_bridge', default_value='true'),
        # udev 규칙(/dev/stm32) 적용 후 기본값 교체 예정.
        # 개발(socat)은 stm32_port:=/tmp/ttyJETSON 으로 덮어쓴다.
        DeclareLaunchArgument('stm32_port', default_value='/dev/stm32'),
        DeclareLaunchArgument('lidar_params',
                              default_value=default_lidar_params),
    ]

    lidar = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(
            get_package_share_directory('ydlidar_ros2_driver')
            if default_lidar_params else bringup_share,
            'launch', 'ydlidar_launch.py')),
        launch_arguments={'params_file': lidar_params}.items(),
        condition=IfCondition(use_lidar),
    ) if default_lidar_params else None

    bridge = Node(
        package='stm32_bridge',
        executable='bridge_node',
        name='stm32_bridge',
        output='screen',
        parameters=[
            os.path.join(bringup_share, 'config', 'bridge_params.yaml'),
            {'serial_port': stm32_port},
        ],
        condition=IfCondition(use_bridge),
    )

    nodes = [n for n in (lidar, bridge) if n is not None]
    return LaunchDescription(args + nodes)
