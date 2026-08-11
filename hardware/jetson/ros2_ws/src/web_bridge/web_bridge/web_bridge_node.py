"""ROS 2 토픽을 EC2 WebSocket 서버로 중계하는 노드."""
import asyncio
import json
import os
import queue
import threading

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
import websockets

from .messages import (command_ack_message, decode_server_message,
                       failure_reason, mission_status_from_route_state,
                       mission_status_message, odom_message, pong_message,
                       status_summary, tag_target_message,
                       unavailable_odom_message)


class WebSocketClient:
    """EC2 백엔드에 재접속하는 WebSocket 클라이언트."""

    def __init__(self, url, token, logger, on_command):
        self._url, self._token, self._logger = url, token, logger
        self._on_command = on_command
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._queue = None
        self._stopping = None

    def start(self):
        """백엔드 연결 스레드를 시작한다."""
        self._thread.start()

    def publish(self, message):
        """전송 큐에 JSON 메시지를 넣는다."""
        if self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._enqueue(json.dumps(message)),
                                             self._loop)

    def close(self):
        """백엔드 연결을 종료한다."""
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stopping.set)
            self._thread.join(timeout=2.0)

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._queue = asyncio.Queue(maxsize=100)
        self._stopping = asyncio.Event()
        try:
            self._loop.run_until_complete(self._connect_forever())
        finally:
            self._loop.close()

    async def _enqueue(self, payload):
        if self._queue.full():
            self._queue.get_nowait()
            self._logger.warning('WebSocket queue full; dropped oldest message')
        self._queue.put_nowait(payload)

    async def _connect_forever(self):
        retry_seconds = 1
        headers = {'Authorization': f'Bearer {self._token}'}
        while not self._stopping.is_set():
            try:
                async with websockets.connect(
                        self._url, extra_headers=headers, ping_interval=20) as ws:
                    self._logger.info(f'connected to backend WebSocket: {self._url}')
                    retry_seconds = 1
                    sender = asyncio.create_task(self._send_forever(ws))
                    receiver = asyncio.create_task(self._receive_forever(ws))
                    done, pending = await asyncio.wait(
                        (sender, receiver), return_when=asyncio.FIRST_COMPLETED)
                    for task in pending:
                        task.cancel()
                    await asyncio.gather(*pending, return_exceptions=True)
                    for task in done:
                        task.result()
            except Exception as error:
                if self._stopping.is_set():
                    break
                self._logger.warning(
                    f'backend WebSocket disconnected ({error}); retry in '
                    f'{retry_seconds} s')
                await asyncio.sleep(retry_seconds)
                retry_seconds = min(retry_seconds * 2, 30)

    async def _send_forever(self, ws):
        while not self._stopping.is_set():
            try:
                payload = await asyncio.wait_for(self._queue.get(), 1.0)
            except asyncio.TimeoutError:
                continue
            await ws.send(payload)

    async def _receive_forever(self, ws):
        async for payload in ws:
            try:
                message_type, data = decode_server_message(payload)
            except ValueError as error:
                self._logger.warning(f'ignoring invalid server message: {error}')
                continue
            if message_type == 'ping':
                await self._enqueue(json.dumps(pong_message()))
                continue
            self._on_command(data)


class WebBridge(Node):
    """관제 메시지를 생성하고 EC2 WebSocket 서버로 전달한다."""

    def __init__(self):
        super().__init__('web_bridge')
        self.declare_parameter('robot_id', 'RB1-01')
        self.declare_parameter(
            'backend_url', os.environ.get('WEB_BRIDGE_URL', ''))
        # ⚠ 서버 RobotMode enum(OUTDOOR_TAGGING·INDOOR_TAGGING·CHARGING_STATION·MANUAL)에
        # 있는 값만 실어야 한다. 'UNSET' 같은 값은 백엔드 어댑터(`_map_mode`)가 통째로 버려서
        # 로봇 카드의 모드 칸이 영영 빈다 — 기본값 자체를 실제 운용 모드로 둔다.
        self.declare_parameter('operation_mode', 'OUTDOOR_TAGGING')
        self.declare_parameter('status_snapshot_sec', 15.0)
        self.declare_parameter('status_max_rate_hz', 2.0)
        # 개발자용 `odom` 송신 상한 [Hz]. 백엔드 요청이 5 Hz 다 (2026-08-06
        # 백엔드발 §4). `_allowed` 를 거치는 것은 odom 하나뿐이라 이 값이 곧
        # 관제 지도의 위치 갱신률이다 — 1 Hz 로는 로봇이 뚝뚝 끊겨 보인다.
        self.declare_parameter('developer_rate_hz', 5.0)
        self.declare_parameter('odom_timeout_sec', 3.0)
        self.declare_parameter('tag_rate_hz', 5.0)
        self.declare_parameter('tag_timeout_sec', 0.5)
        self.robot_id = self.get_parameter('robot_id').value
        self.operation_mode = self.get_parameter('operation_mode').value
        self._seq, self._connected, self._telemetry = 0, False, None
        self._mission_status = None
        self._status_pending = False
        self._last_status_send_time = None
        self._last_odom_time = None
        self._last_odom_received_time = None
        self._odom_invalid_reason = None
        self._last_odom_invalid_send_time = None
        self._last_tag_send_time = None
        self._last_tag_received_time = None
        self._last_tag_id = None
        self._tag_visible = False
        # `along` 의 기준면. route_runner 만 아는 차량 치수라 `/route/state` 로 받는다.
        self._front_overhang_m = None
        self._start_time = self._now_seconds()
        self._commands = queue.Queue(maxsize=100)
        status_rate = self.get_parameter('status_max_rate_hz').value
        developer_rate = self.get_parameter('developer_rate_hz').value
        tag_rate = self.get_parameter('tag_rate_hz').value
        self._snapshot_period = self.get_parameter('status_snapshot_sec').value
        self._odom_timeout = self.get_parameter('odom_timeout_sec').value
        self._tag_timeout = self.get_parameter('tag_timeout_sec').value
        if min(status_rate, developer_rate, tag_rate, self._snapshot_period,
               self._odom_timeout, self._tag_timeout) <= 0.0:
            raise RuntimeError('rates and timeouts must be greater than zero')
        self._status_period = 1.0 / status_rate
        self._developer_period = 1.0 / developer_rate
        self._tag_period = 1.0 / tag_rate
        backend_url = self.get_parameter('backend_url').value
        token = os.environ.get('WEB_BRIDGE_TOKEN')
        if not backend_url or not token:
            raise RuntimeError(
                'WEB_BRIDGE_URL and WEB_BRIDGE_TOKEN must be set in environment')
        if not backend_url.startswith(('ws://', 'wss://')):
            raise RuntimeError('WEB_BRIDGE_URL must start with ws:// or wss://')
        self.client = WebSocketClient(
            backend_url, token, self.get_logger(), self._enqueue_command)
        self._route_clients = {
            'cmd_destination': self.create_client(Trigger, '/route/start'),
            'return_to_charge': self.create_client(Trigger, '/route/return'),
            'emergency_stop': self.create_client(Trigger, '/route/abort'),
        }
        self.client.start()
        self.create_subscription(String, '/stm32/telemetry', self._on_telemetry, 10)
        self.create_subscription(Bool, '/stm32/connected', self._on_connected, 10)
        self.create_subscription(Odometry, '/stm32/odom', self._on_odom, 10)
        self.create_subscription(String, '/route/state', self._on_route_state, 10)
        self.create_subscription(String, '/tag/target', self._on_tag_target, 5)
        self.create_timer(self._status_period, self._flush_status)
        self.create_timer(self._snapshot_period, self._request_status)
        self.create_timer(min(0.5, self._odom_timeout), self._check_odom)
        self.create_timer(min(0.1, self._tag_timeout), self._check_tag)
        self.create_timer(0.05, self._dispatch_commands)

    def destroy_node(self):
        """웹소켓 연결을 먼저 종료한다."""
        self.client.close()
        return super().destroy_node()

    def _next_seq(self):
        self._seq += 1
        return self._seq

    def _enqueue_command(self, command):
        try:
            self._commands.put_nowait(command)
        except queue.Full:
            self.get_logger().error(
                f'command queue full; dropped {command["command_id"]}')

    def _dispatch_commands(self):
        while True:
            try:
                command = self._commands.get_nowait()
            except queue.Empty:
                return
            command_type = command['type']
            client = self._route_clients[command_type]
            if not client.service_is_ready():
                # 거절 사유는 관제 화면에 **그대로** 뜬다 (2026-08-06 백엔드발 §2).
                # 그래서 한글로 쓴다. 서비스 이름은 남긴다 — 화면을 보는 사람이
                # 그대로 읽어 전해 주면 어느 노드가 안 떴는지 바로 안다.
                reason = f'{client.srv_name} 서비스가 없다 — route_runner 확인'
                self.get_logger().error(f'{command_type} 수신했으나 {reason}')
                self.client.publish(command_ack_message(
                    command['command_id'], False, reason))
                continue
            future = client.call_async(Trigger.Request())
            future.add_done_callback(
                lambda result, cmd=command: self._on_command_result(cmd, result))

    def _on_command_result(self, command, future):
        try:
            response = future.result()
        except Exception as error:
            reason = f'ROS 서비스 호출 실패: {error}'
            self.get_logger().error(f'{command["type"]} {reason}')
            self.client.publish(command_ack_message(
                command['command_id'], False, reason))
            return
        self.client.publish(command_ack_message(
            command['command_id'], response.success, response.message))
        log = (self.get_logger().info if response.success else
               self.get_logger().warning)
        log(f'{command["type"]} {command["command_id"]}: {response.message}')

    def _send_status(self):
        self._status_pending = False
        self._last_status_send_time = self._now_seconds()
        self.client.publish(status_summary(
            self.robot_id, self._next_seq(), self._connected, self._telemetry,
            self.operation_mode))

    def _request_status(self, immediate=False):
        now = self._now_seconds()
        if (immediate or self._last_status_send_time is None or
                now - self._last_status_send_time >= self._status_period):
            self._send_status()
        else:
            self._status_pending = True

    def _flush_status(self):
        if self._status_pending:
            self._send_status()

    def _on_connected(self, msg):
        if self._connected != msg.data:
            self._connected = msg.data
            self._request_status(immediate=True)

    def _on_telemetry(self, msg):
        try:
            telemetry = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().warning('ignoring invalid /stm32/telemetry JSON')
            return
        if telemetry != self._telemetry:
            previous_state = (None if self._telemetry is None else
                              self._telemetry.get('drive_state'))
            self._telemetry = telemetry
            drive_state = telemetry.get('drive_state')
            critical = drive_state in (5, 6) and drive_state != previous_state
            self._request_status(immediate=critical)

    def _on_route_state(self, msg):
        """주행 상태머신 보고에서 미션 상태를 뽑아 **바뀐 순간에만** 올린다.

        ⚠ `/route/state` 는 2 Hz 로 계속 온다. 매번 올리면 서버가 같은 값을 초당 두 장씩
        받아 도착 판정(에지)을 그때마다 다시 돌린다. 그래서 값이 바뀔 때만 보낸다.

        ⚠ 옮길 값이 없는 상태(IDLE·HOLD)로 넘어가면 기억을 지운다. 안 지우면
        도착 → 재출동 → 또 도착이 같은 값이라 두 번째 도착이 한 번도 안 나간다.
        """
        try:
            route = json.loads(msg.data)
        except json.JSONDecodeError:
            self.get_logger().warning('ignoring invalid /route/state JSON')
            return
        if not isinstance(route, dict):
            return
        # 기준면 갱신은 **에지 판정보다 앞에** 둔다. 아래 `return` 이 2 Hz 중 거의
        # 모든 프레임을 걸러내므로, 뒤에 두면 미션 상태가 바뀌는 순간에만 갱신돼
        # 주행 내내 `tag_target` 에 기준면이 안 실린다.
        overhang = route.get('front_overhang_m')
        if isinstance(overhang, (int, float)) and not isinstance(overhang, bool):
            self._front_overhang_m = float(overhang)
        status = mission_status_from_route_state(route.get('state'))
        if status == self._mission_status:
            return
        self._mission_status = status
        if status is None:
            return
        reason = failure_reason(route) if status == 'FAILED' else None
        self.client.publish(mission_status_message(
            self.robot_id, self._next_seq(), status, reason=reason))

    def _on_odom(self, msg):
        now = self._now_seconds()
        self._last_odom_received_time = now
        recovered = self._odom_invalid_reason is not None
        self._odom_invalid_reason = None
        if recovered or self._allowed('_last_odom_time', now):
            self._last_odom_time = now
            self.client.publish(odom_message(self.robot_id, self._next_seq(), msg))

    def _on_tag_target(self, msg):
        try:
            target = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self.get_logger().warning('ignoring invalid /tag/target JSON')
            return
        if not self._valid_tag_target(target):
            self.get_logger().warning('ignoring invalid /tag/target fields')
            return

        now = self._now_seconds()
        became_visible = not self._tag_visible
        self._last_tag_received_time = now
        self._last_tag_id = target['tag_id']
        self._tag_visible = True
        if (became_visible or self._last_tag_send_time is None or
                now - self._last_tag_send_time >= self._tag_period):
            self._last_tag_send_time = now
            self.client.publish(tag_target_message(
                self.robot_id, self._next_seq(), target,
                front_overhang_m=self._front_overhang_m))

    @staticmethod
    def _valid_tag_target(target):
        if not isinstance(target, dict):
            return False
        tag_id = target.get('tag_id')
        if not isinstance(tag_id, int) or isinstance(tag_id, bool):
            return False
        if target.get('valid') is not True:
            return False
        for key in ('along', 'cross_track', 'heading_error'):
            value = target.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return False
        return True

    def _check_tag(self):
        if not self._tag_visible or self._last_tag_received_time is None:
            return
        now = self._now_seconds()
        if now - self._last_tag_received_time < self._tag_timeout:
            return
        self._tag_visible = False
        self._last_tag_send_time = now
        self.client.publish(tag_target_message(
            self.robot_id, self._next_seq(), {'tag_id': self._last_tag_id},
            valid=False, reason='tag_timeout',
            front_overhang_m=self._front_overhang_m))

    def _allowed(self, attribute, now=None):
        if now is None:
            now = self._now_seconds()
        previous = getattr(self, attribute)
        if previous is not None and now - previous < self._developer_period:
            return False
        setattr(self, attribute, now)
        return True

    def _check_odom(self):
        now = self._now_seconds()
        if self._last_odom_received_time is None:
            if now - self._start_time < self._odom_timeout:
                return
            reason = 'odom_not_received'
        else:
            if now - self._last_odom_received_time < self._odom_timeout:
                return
            reason = 'odom_timeout'

        transitioned = reason != self._odom_invalid_reason
        snapshot_due = (self._last_odom_invalid_send_time is None or
                        now - self._last_odom_invalid_send_time >=
                        self._snapshot_period)
        if transitioned or snapshot_due:
            self._odom_invalid_reason = reason
            self._last_odom_invalid_send_time = now
            self.client.publish(unavailable_odom_message(
                self.robot_id, self._next_seq(), reason))

    def _now_seconds(self):
        return self.get_clock().now().nanoseconds / 1_000_000_000.0


def main(args=None):
    """web_bridge_node 콘솔 진입점."""
    rclpy.init(args=args)
    node = WebBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
