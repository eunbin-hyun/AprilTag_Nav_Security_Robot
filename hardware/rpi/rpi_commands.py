"""Server-to-RPi command polling helpers."""

from __future__ import annotations

import os
import threading
from typing import Callable

import requests


DEFAULT_POLL_ENDPOINT = "/api/rpi/commands/poll"
DEFAULT_ACK_ENDPOINT = "/api/rpi/commands/{command_id}/ack"
DEFAULT_POLL_INTERVAL_S = 1.0
API_KEY_HEADER = "X-API-Key"
ARRIVAL_COMMAND_TYPES = {
    "bus_arrival",
    "robot_departure",
    "robot_arrival",
    "robot_return_complete",
}
ANNOUNCEMENT_COMMAND_TYPES = {
    "departing_to_destination",
    "arrived_at_destination",
    "charging_started",
}
ANNOUNCEMENT_TEXT = {
    "departing_to_destination": "지정 위치로 출발합니다.",
    "arrived_at_destination": "지정 위치로 도착했습니다.",
    "charging_started": "충전을 시작합니다.",
}


def env_value(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def normalize_commands(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("commands"), list):
        return payload["commands"]
    if isinstance(payload.get("command"), dict):
        return [payload["command"]]
    if payload.get("type") or payload.get("command_type"):
        return [payload]
    return []


def command_type(command) -> str:
    return str(command.get("type") or command.get("command_type") or command.get("name") or "").strip()


def command_id(command):
    return command.get("command_id") or command.get("id")


class RpiCommandPoller:
    def __init__(
        self,
        *,
        base_url: str,
        device_id: str,
        gate_no: int,
        api_key: str,
        stop_event: threading.Event,
        on_arrival_sound: Callable[[str], None],
        on_announcement: Callable[[str, dict], None] | None = None,
        timeout_s: float,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        poll_endpoint: str | None = None,
        ack_endpoint: str | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.gate_no = gate_no
        self.api_key = api_key
        self.stop_event = stop_event
        self.on_arrival_sound = on_arrival_sound
        self.on_announcement = on_announcement
        self.timeout_s = timeout_s
        self.poll_interval_s = poll_interval_s
        self.poll_endpoint = poll_endpoint or env_value("RPI_COMMAND_POLL_ENDPOINT", DEFAULT_POLL_ENDPOINT)
        self.ack_endpoint = ack_endpoint or env_value("RPI_COMMAND_ACK_ENDPOINT", DEFAULT_ACK_ENDPOINT)

    def run(self) -> None:
        session = requests.Session()
        if self.api_key:
            session.headers[API_KEY_HEADER] = self.api_key

        while not self.stop_event.is_set():
            try:
                resp = session.get(
                    self.base_url + self.poll_endpoint,
                    params={"device_id": self.device_id, "gate_no": self.gate_no},
                    timeout=self.timeout_s,
                )
                if 200 <= resp.status_code < 300:
                    self._handle_commands(session, resp.json())
                elif resp.status_code not in (204, 404):
                    print(f"[명령] 폴링 실패({resp.status_code}): {resp.text[:200]}")
            except ValueError as e:
                print(f"[명령] 응답 JSON 파싱 실패: {e}")
            except requests.RequestException as e:
                print(f"[명령] 폴링 오류: {e}")

            self.stop_event.wait(self.poll_interval_s)

    def _handle_commands(self, session: requests.Session, payload) -> None:
        for command in normalize_commands(payload):
            ctype = command_type(command)
            if ctype in ARRIVAL_COMMAND_TYPES:
                print(f"[명령] 도착 안내 음원 재생: {ctype}")
                self.on_arrival_sound(ctype)
                self._ack(session, command)
            elif ctype in ANNOUNCEMENT_COMMAND_TYPES:
                text = str(command.get("message") or ANNOUNCEMENT_TEXT[ctype])
                print(f"[명령] 안내 수신: {text}")
                if self.on_announcement is not None:
                    self.on_announcement(text, command)
                self._ack(session, command)
            elif ctype:
                print(f"[명령] 알 수 없는 명령 무시: {ctype}")

    def _ack(self, session: requests.Session, command) -> None:
        cid = command_id(command)
        if not cid or not self.ack_endpoint:
            return
        endpoint = self.ack_endpoint.format(command_id=cid)
        try:
            resp = session.post(self.base_url + endpoint, timeout=self.timeout_s)
            if resp.status_code >= 400:
                print(f"[명령] ACK 실패({resp.status_code}) command_id={cid}: {resp.text[:200]}")
        except requests.RequestException as e:
            print(f"[명령] ACK 오류 command_id={cid}: {e}")
