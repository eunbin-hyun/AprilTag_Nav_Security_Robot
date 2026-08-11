"""web_bridge의 서버 수신 기능을 시험하는 가상 WebSocket 서버."""
import argparse
import asyncio
import json

import websockets


COMMANDS = ('cmd_destination', 'return_to_charge', 'emergency_stop')


def command_frame(command_type, command_id, ttl_ms):
    """서버 명령 계약에 맞는 시험 프레임을 만든다."""
    payload = {
        'command_id': command_id,
        'type': command_type,
        'ttl_ms': ttl_ms,
    }
    if command_type == 'cmd_destination':
        payload['cmd_destination'] = 'OUTDOOR_TAGGING'
    return {'type': 'command', 'payload': payload}


class TestServer:
    """연결마다 ping을 보내고 선택한 명령은 프로세스당 한 번만 보낸다."""

    def __init__(self, command, command_id, ttl_ms, expected_token):
        self.command = command
        self.command_id = command_id
        self.ttl_ms = ttl_ms
        self.expected_token = expected_token
        self.command_sent = False

    async def handler(self, ws, path):
        """Jetson 연결을 받아 시험 프레임을 송수신한다."""
        authorization = ws.request_headers.get('Authorization')
        print(f'connected path={path} auth={authorization!r}')
        if (self.expected_token is not None and
                authorization != f'Bearer {self.expected_token}'):
            print('FAIL: Authorization Bearer token mismatch')
            await ws.close(code=1008, reason='invalid token')
            return

        await self._send(ws, {'type': 'ping', 'payload': {}})
        expected_ack = None
        if self.command is not None and not self.command_sent:
            self.command_sent = True
            expected_ack = self.command_id
            await self._send(ws, command_frame(
                self.command, self.command_id, self.ttl_ms))

        pong_received = False
        ack_received = expected_ack is None
        async for raw_message in ws:
            print(f'<- {raw_message}')
            try:
                message = json.loads(raw_message)
            except json.JSONDecodeError:
                continue
            if message == {'type': 'pong', 'payload': {}}:
                pong_received = True
                print('PASS: pong received')
            if (message.get('type') == 'command_ack' and
                    message.get('payload', {}).get('command_id') ==
                    expected_ack):
                ack_received = True
                print(f'PASS: command_ack received ({expected_ack})')
            if pong_received and ack_received:
                print('PASS: requested WebSocket checks completed')
                expected_ack = None

    @staticmethod
    async def _send(ws, message):
        payload = json.dumps(message)
        print(f'-> {payload}')
        await ws.send(payload)


def parse_args():
    """명령행 인자를 읽는다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--command', choices=COMMANDS)
    parser.add_argument('--command-id', default='ws-test-1')
    parser.add_argument('--ttl-ms', type=int, default=30000)
    parser.add_argument(
        '--expected-token',
        help='설정하면 Authorization Bearer 토큰까지 검사한다')
    args = parser.parse_args()
    if args.ttl_ms <= 0:
        parser.error('--ttl-ms must be greater than zero')
    return args


async def main():
    """가상 WebSocket 서버를 실행한다."""
    args = parse_args()
    server = TestServer(
        args.command, args.command_id, args.ttl_ms, args.expected_token)
    print(f'listening on ws://{args.host}:{args.port}')
    if args.command:
        print(f'WARNING: {args.command} will be delivered once')
    else:
        print('ping/pong only; no robot command will be sent')
    async with websockets.serve(server.handler, args.host, args.port):
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
