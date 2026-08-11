"""web_bridge 통합 테스트를 위한 가짜 ROS 토픽 발행 노드."""
import json
import math

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import Imu
from std_msgs.msg import Bool, String


class FakeWebTopics(Node):
    """STM32 연결·텔레메트리·odom·IMU 토픽을 가짜로 발행한다."""

    def __init__(self):
        super().__init__('fake_web_topics')
        self.declare_parameter('rate_hz', 10.0)
        self.declare_parameter('linear_speed_m_s', 0.25)
        self.rate_hz = self.get_parameter('rate_hz').value
        self.linear_speed = self.get_parameter('linear_speed_m_s').value
        if self.rate_hz <= 0.0:
            raise RuntimeError('rate_hz must be greater than zero')
        self._start_time = self.get_clock().now()
        self._sequence = 0

        self._connected_pub = self.create_publisher(
            Bool, '/stm32/connected', 10)
        self._telemetry_pub = self.create_publisher(
            String, '/stm32/telemetry', 10)
        self._odom_pub = self.create_publisher(Odometry, '/stm32/odom', 10)
        self._imu_pub = self.create_publisher(Imu, '/stm32/imu', 10)
        self.create_timer(1.0 / self.rate_hz, self._publish)
        self.get_logger().info(
            'publishing fake /stm32/{connected,telemetry,odom,imu} topics')

    def _publish(self):
        now = self.get_clock().now()
        elapsed = (now - self._start_time).nanoseconds / 1_000_000_000.0
        self._sequence += 1

        self._connected_pub.publish(Bool(data=True))
        self._telemetry_pub.publish(String(data=json.dumps({
            'mcu_time_ms': int(elapsed * 1000),
            'measured_speed_mm_s': int(self.linear_speed * 1000),
            'drive_state': 3,
            'active_fault_bits': 0,
        })))
        self._odom_pub.publish(self._odom(now, elapsed))
        self._imu_pub.publish(self._imu(now, elapsed))

    def _odom(self, now, elapsed):
        message = Odometry()
        message.header.stamp = now.to_msg()
        message.header.frame_id = 'odom'
        message.child_frame_id = 'base_link'
        message.pose.pose.position.x = self.linear_speed * elapsed
        message.pose.pose.orientation.w = 1.0
        message.twist.twist.linear.x = self.linear_speed
        return message

    def _imu(self, now, elapsed):
        message = Imu()
        message.header.stamp = now.to_msg()
        message.header.frame_id = 'imu_link'
        message.angular_velocity.z = 0.02 * math.sin(elapsed)
        message.linear_acceleration.x = 0.1 * math.cos(elapsed)
        return message


def main(args=None):
    """fake_web_topics 콘솔 진입점."""
    rclpy.init(args=args)
    try:
        node = FakeWebTopics()
    except RuntimeError:
        rclpy.shutdown()
        raise
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
