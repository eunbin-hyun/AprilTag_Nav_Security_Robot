#!/usr/bin/env python3
"""Local smoke test for RpiCommandPoller using an in-process mock server."""

from __future__ import annotations

import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mock_rpi_command_server import CommandState, make_handler
from rpi_commands import RpiCommandPoller


def main():
    state = CommandState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    host, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    arrivals = []
    announcements = []
    stop_event = threading.Event()
    poller = RpiCommandPoller(
        base_url=f"http://{host}:{port}",
        device_id="raspberry01",
        gate_no=1,
        api_key="test-key",
        stop_event=stop_event,
        on_arrival_sound=lambda kind: arrivals.append(kind),
        on_announcement=lambda text, _command: announcements.append(text),
        timeout_s=1.0,
        poll_interval_s=0.1,
    )
    poller_thread = threading.Thread(target=poller.run, daemon=True)
    poller_thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline and not arrivals:
        time.sleep(0.05)

    stop_event.set()
    poller_thread.join(timeout=1.0)

    assert len(arrivals) == 1, f"expected one arrival callback, got {len(arrivals)}"
    assert arrivals == ["bus_arrival"], arrivals
    assert state.acked == ["mock-bus-1"], f"expected ACK for mock-bus-1, got {state.acked}"
    print("OK: polled bus_arrival and sent ACK")

    state.pending = [{"command_id": "mock-robot-departure-1", "type": "robot_departure"}]
    state.acked = []
    arrivals.clear()
    stop_event.clear()
    poller_thread = threading.Thread(target=poller.run, daemon=True)
    poller_thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline and not arrivals:
        time.sleep(0.05)

    stop_event.set()
    poller_thread.join(timeout=1.0)

    assert arrivals == ["robot_departure"], arrivals
    assert state.acked == ["mock-robot-departure-1"], state.acked
    print("OK: polled robot_departure and sent ACK")

    state.pending = [{"command_id": "mock-robot-1", "type": "robot_arrival"}]
    state.acked = []
    arrivals.clear()
    stop_event.clear()
    poller_thread = threading.Thread(target=poller.run, daemon=True)
    poller_thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline and not arrivals:
        time.sleep(0.05)

    stop_event.set()
    poller_thread.join(timeout=1.0)

    assert arrivals == ["robot_arrival"], arrivals
    assert state.acked == ["mock-robot-1"], state.acked
    print("OK: polled robot_arrival and sent ACK")

    state.pending = [{"command_id": "mock-mission-1", "type": "arrived_at_destination"}]
    state.acked = []
    stop_event.clear()
    poller_thread = threading.Thread(target=poller.run, daemon=True)
    poller_thread.start()

    deadline = time.time() + 3.0
    while time.time() < deadline and not announcements:
        time.sleep(0.05)

    stop_event.set()
    poller_thread.join(timeout=1.0)

    assert announcements == ["지정 위치로 도착했습니다."], announcements
    assert state.acked == ["mock-mission-1"], state.acked
    print("OK: polled arrived_at_destination and sent ACK")

    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    main()
