#!/usr/bin/env python3
"""Jetson-to-STM32 UART5 Protocol V3.0 (wire version 0x02) echo test.

Wiring through a 3.3 V USB-TTL adapter:

    adapter TXD -> STM32 PD2  (UART5_RX, P1-40)
    adapter RXD <- STM32 PC12 (UART5_TX, P1-43)
    adapter GND -> STM32 GND
    adapter VCC -> not connected

The response header uses STM32's own TX sequence. The request sequence is
returned as the first response payload byte, followed by the request data.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time


SOF = b"\xAA\x55"
PROTOCOL_VERSION = 0x02
MAX_PAYLOAD = 64
MAX_ECHO_PAYLOAD = 31
MSG_DIAG_ECHO_REQUEST = 0xF0
MSG_DIAG_ECHO_RESPONSE = 0xF1
MSG_CMD_DRIVE = 0x10
MSG_TELEMETRY_DRIVE = 0x80
MSG_FAULT_EVENT = 0x81
MSG_COMMAND_RESULT = 0x82
GOLDEN_ECHO_REQUEST = bytes.fromhex(
    "AA 55 02 F0 10 04 12 34 AA 55 CD 15"
)
GOLDEN_ECHO_RESPONSE = bytes.fromhex(
    "AA 55 02 F1 03 05 10 12 34 AA 55 DD 5C"
)


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def encode_frame(message_id: int, sequence: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload exceeds {MAX_PAYLOAD} bytes")

    body = bytes(
        (
            PROTOCOL_VERSION,
            message_id & 0xFF,
            sequence & 0xFF,
            len(payload),
        )
    ) + payload
    crc = crc16_ccitt_false(body)
    return SOF + body + crc.to_bytes(2, byteorder="little")


def pop_frame(buffer: bytearray) -> tuple[int, int, bytes] | None:
    while True:
        sof_index = buffer.find(SOF)
        if sof_index < 0:
            if buffer[-1:] == SOF[:1]:
                del buffer[:-1]
            else:
                buffer.clear()
            return None

        if sof_index > 0:
            del buffer[:sof_index]

        if len(buffer) < 6:
            return None

        payload_length = buffer[5]
        if payload_length > MAX_PAYLOAD:
            del buffer[:2]
            continue

        frame_length = 8 + payload_length
        if len(buffer) < frame_length:
            return None

        raw_frame = bytes(buffer[:frame_length])

        body = raw_frame[2 : 6 + payload_length]
        received_crc = int.from_bytes(
            raw_frame[6 + payload_length : frame_length],
            byteorder="little",
        )
        if crc16_ccitt_false(body) != received_crc:
            del buffer[:1]
            continue
        if raw_frame[2] != PROTOCOL_VERSION:
            del buffer[:1]
            continue

        del buffer[:frame_length]
        return raw_frame[3], raw_frame[4], raw_frame[6 : 6 + payload_length]


def protocol_self_test() -> None:
    golden_cases = (
        (
            "GF-01 CMD_DRIVE",
            encode_frame(
                MSG_CMD_DRIVE,
                0x2A,
                struct.pack("<hhB", 500, -1250, 1),
            ),
            bytes.fromhex(
                "AA 55 02 10 2A 05 F4 01 1E FB 01 84 7A"
            ),
        ),
        (
            "GF-02 neutral",
            encode_frame(MSG_CMD_DRIVE, 0x00, struct.pack("<hhB", 0, 0, 0)),
            bytes.fromhex(
                "AA 55 02 10 00 05 00 00 00 00 00 A0 A0"
            ),
        ),
        (
            "GF-03 echo request",
            encode_frame(
                MSG_DIAG_ECHO_REQUEST,
                0x10,
                bytes.fromhex("12 34 AA 55"),
            ),
            GOLDEN_ECHO_REQUEST,
        ),
        (
            "GF-04 echo response",
            encode_frame(
                MSG_DIAG_ECHO_RESPONSE,
                0x03,
                bytes.fromhex("10 12 34 AA 55"),
            ),
            GOLDEN_ECHO_RESPONSE,
        ),
        (
            "GF-05 TELEMETRY_DRIVE",
            encode_frame(
                MSG_TELEMETRY_DRIVE,
                0x05,
                struct.pack(
                    "<IhhhhhihBBI",
                    1000,
                    500,
                    480,
                    480,
                    684,
                    0,
                    12345,
                    0,
                    3,
                    0x2A,
                    0,
                ),
            ),
            bytes.fromhex(
                "AA 55 02 80 05 1A E8 03 00 00 F4 01 E0 01 E0 01 "
                "AC 02 00 00 39 30 00 00 00 00 03 2A 00 00 00 00 D8 31"
            ),
        ),
        (
            "GF-06 FAULT_EVENT",
            encode_frame(
                MSG_FAULT_EVENT,
                0x09,
                struct.pack("<IIIBB", 5000, 1, 1, 2, 4),
            ),
            bytes.fromhex(
                "AA 55 02 81 09 0E 88 13 00 00 01 00 00 00 "
                "01 00 00 00 02 04 8A 74"
            ),
        ),
        (
            "GF-07 COMMAND_RESULT",
            encode_frame(
                MSG_COMMAND_RESULT,
                0x07,
                struct.pack("<BBBB", 0x11, 0x2A, 0, 4),
            ),
            bytes.fromhex(
                "AA 55 02 82 07 04 11 2A 00 04 55 58"
            ),
        ),
    )

    for name, actual, expected in golden_cases:
        if actual != expected:
            raise SystemExit(
                f"{name} golden-frame test failed:\n"
                f"expected: {expected.hex(' ').upper()}\n"
                f"actual:   {actual.hex(' ').upper()}"
            )

    encoded_request = golden_cases[2][1]
    encoded_response = golden_cases[3][1]

    parser_input = bytearray(b"\x00\xAA" + encoded_response)
    parsed = pop_frame(parser_input)
    if parsed != (
        MSG_DIAG_ECHO_RESPONSE,
        0x03,
        bytes.fromhex("10 12 34 AA 55"),
    ):
        raise SystemExit("Stream parser self-test failed")

    print("Protocol self-test: PASS")
    print("Golden frames GF-01..GF-07: PASS")
    print(f"GF-03 request:  {encoded_request.hex(' ').upper()}")
    print(f"GF-04 response: {encoded_response.hex(' ').upper()}")


def run_echo_test(
    port_name: str,
    text: str,
    timeout_s: float,
    sequence: int,
) -> None:
    try:
        import serial
    except ImportError as error:
        raise SystemExit(
            "pyserial is required.\n"
            "Install it on Jetson with: python3 -m pip install pyserial"
        ) from error

    payload = text.encode("utf-8")
    if len(payload) > MAX_ECHO_PAYLOAD:
        raise SystemExit(
            f"Echo text is {len(payload)} bytes; maximum is "
            f"{MAX_ECHO_PAYLOAD} bytes."
        )
    if timeout_s <= 0.0:
        raise SystemExit("--timeout must be greater than zero")

    request = encode_frame(MSG_DIAG_ECHO_REQUEST, sequence, payload)
    receive_buffer = bytearray()
    deadline = time.monotonic() + timeout_s

    try:
        with serial.Serial(
            port=port_name,
            baudrate=115200,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
        ) as port:
            port.reset_input_buffer()
            port.write(request)
            port.flush()

            print(f"Port: {port_name} (115200 8N1)")
            print(f"TX:   {request.hex(' ').upper()}")

            while time.monotonic() < deadline:
                chunk = port.read(max(port.in_waiting, 1))
                if chunk:
                    receive_buffer.extend(chunk)

                while True:
                    frame = pop_frame(receive_buffer)
                    if frame is None:
                        break

                    message_id, received_sequence, received_payload = frame
                    if message_id != MSG_DIAG_ECHO_RESPONSE:
                        continue
                    expected_payload = bytes((sequence & 0xFF,)) + payload
                    if received_payload != expected_payload:
                        raise SystemExit(
                            "Echo response payload mismatch:\n"
                            f"expected: {expected_payload!r}\n"
                            f"actual:   {received_payload!r}"
                        )

                    response = encode_frame(
                        message_id,
                        received_sequence,
                        received_payload,
                    )
                    print(f"RX:   {response.hex(' ').upper()}")
                    print(f"STM32 TX sequence: {received_sequence}")
                    print(f"Request sequence echoed in payload: {received_payload[0]}")
                    print(f"Text: {received_payload[1:].decode('utf-8')}")
                    print("Jetson <-> STM32 UART5 echo: PASS")
                    return
    except serial.SerialException as error:
        raise SystemExit(
            f"Cannot use serial port {port_name}: {error}\n"
            "Check the USB-TTL connection, /dev device name, and dialout "
            "group permission."
        ) from error

    raise SystemExit(
        f"No valid echo response from {port_name} within {timeout_s:.1f}s.\n"
        "Check TX/RX crossover, common GND, 3.3 V logic, STM32 firmware, "
        "and the selected serial device."
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jetson-to-STM32 UART5 Protocol V3.0 echo test"
    )
    parser.add_argument(
        "--port",
        default="/dev/ttyUSB0",
        help="serial device (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--text",
        default="JETSON",
        help="UTF-8 echo text, at most 31 encoded bytes",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="response timeout in seconds (default: 2.0)",
    )
    parser.add_argument(
        "--sequence",
        type=int,
        default=0,
        help="request sequence number 0-255 (default: 0)",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="verify framing and CRC without opening a serial port",
    )
    return parser


def main() -> int:
    args = build_argument_parser().parse_args()

    if args.self_test:
        protocol_self_test()
        return 0
    if not 0 <= args.sequence <= 255:
        raise SystemExit("--sequence must be between 0 and 255")

    run_echo_test(
        port_name=args.port,
        text=args.text,
        timeout_s=args.timeout,
        sequence=args.sequence,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
