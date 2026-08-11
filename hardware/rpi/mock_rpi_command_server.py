#!/usr/bin/env python3
"""Tiny local server that sends one bus arrival command to an RPi client."""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class CommandState:
    def __init__(self):
        self.pending = [{"command_id": "mock-bus-1", "type": "bus_arrival"}]
        self.acked = []


def make_handler(state: CommandState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            print(f"[mock] {self.address_string()} - {fmt % args}")

        def send_json(self, status: int, payload) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/api/rpi/commands/poll":
                self.send_json(404, {"detail": "not found"})
                return

            query = parse_qs(parsed.query)
            print(f"[mock] poll device_id={query.get('device_id')} gate_no={query.get('gate_no')}")
            commands = list(state.pending)
            self.send_json(200, {"commands": commands})

        def do_POST(self):
            parsed = urlparse(self.path)
            prefix = "/api/rpi/commands/"
            suffix = "/ack"
            if parsed.path.startswith(prefix) and parsed.path.endswith(suffix):
                command_id = parsed.path[len(prefix):-len(suffix)]
                state.acked.append(command_id)
                state.pending = [cmd for cmd in state.pending if cmd.get("command_id") != command_id]
                print(f"[mock] ack command_id={command_id}")
                self.send_json(200, {"acked": True, "command_id": command_id})
                return

            self.send_json(404, {"detail": "not found"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Mock RPi command polling server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18080)
    args = parser.parse_args()

    state = CommandState()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    print(f"[mock] serving at http://{args.host}:{args.port}")
    print("[mock] first poll returns bus_arrival; ACK removes it")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[mock] stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
