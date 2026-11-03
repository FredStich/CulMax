from __future__ import annotations

import socket

import pytest

from custom_components.maxcul._vendor.pymaxcul import _communication, _io, _telnet
from custom_components.maxcul._vendor.pymaxcul._messages import (
    WallThermostatControlMessage,
)


class FakeSocket:
    def __init__(self, chunks: list[bytes | Exception]) -> None:
        self.chunks = iter(chunks)
        self.sent: list[bytes] = []
        self.timeout: float | None = None
        self.closed = False

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout

    def recv(self, _size: int) -> bytes:
        chunk = next(self.chunks, TimeoutError())
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        self.closed = True


def test_telnet_serial_handles_negotiation_and_reads_lines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_socket = FakeSocket([bytes((255, 251, 1)) + b"V\r\n"])
    monkeypatch.setattr(
        _telnet.socket, "create_connection", lambda *_args, **_kwargs: fake_socket
    )

    connection = _telnet.TelnetSerial("telnet://cul.local:2323", timeout=0.5)

    assert connection.readline() == b"V\r\n"
    assert fake_socket.sent == [bytes((255, 254, 1))]
    assert fake_socket.timeout == 0.5

    connection.write(b"X21\r\n")
    connection.close()

    assert fake_socket.sent[-1] == b"X21\r\n"
    assert fake_socket.closed


def test_telnet_serial_rejects_requested_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_socket = FakeSocket([bytes((255, 253, 3)) + b"ready\n"])
    monkeypatch.setattr(
        _telnet.socket, "create_connection", lambda *_args, **_kwargs: fake_socket
    )
    connection = _telnet.TelnetSerial("telnet://cul.local:2323")

    assert connection.readline() == b"ready\n"
    assert fake_socket.sent == [bytes((255, 252, 3))]


def test_telnet_serial_returns_partial_line_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_socket = FakeSocket([b"partial", socket.timeout()])
    monkeypatch.setattr(
        _telnet.socket, "create_connection", lambda *_args, **_kwargs: fake_socket
    )
    connection = _telnet.TelnetSerial("telnet://cul.local:2323", timeout=0.5)

    assert connection.readline() == b"partial"


def test_telnet_serial_rejects_invalid_address() -> None:
    with pytest.raises(_telnet.TelnetException, match="Invalid CUL address"):
        _telnet.TelnetSerial("telnet://cul.local")


def test_io_thread_returns_line_after_reconnecting() -> None:
    class ReconnectedPort:
        def __init__(self) -> None:
            self.attempts = 0

        def readline(self) -> bytes:
            self.attempts += 1
            if self.attempts == 1:
                raise _telnet.TelnetException("connection interrupted")
            return b"Zmessage\r\n"

    io_thread = _io.CulIoThread.__new__(_io.CulIoThread)
    io_thread._com_port = ReconnectedPort()
    io_thread._reopen_serial_device = lambda: True

    assert io_thread._readline() == "Zmessage"


@pytest.mark.parametrize("fht_id", ["1034", "0000"])
def test_io_thread_reports_fht_id_response(fht_id: str) -> None:
    io_thread = _io.CulIoThread.__new__(_io.CulIoThread)
    io_thread._fht_id = None
    io_thread._fht_id_callback = lambda value: received.append(value)
    io_thread._readline = lambda: fht_id
    received = []

    assert io_thread._receive_message()
    assert io_thread.fht_id == fht_id
    assert received == [fht_id]


def test_io_thread_closes_connection_when_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OpenPort:
        closed = False

        def close(self) -> None:
            self.closed = True

    io_thread = _io.CulIoThread("/dev/ttyACM0", 38400)
    port = OpenPort()

    def initialize_connection() -> None:
        io_thread._com_port = port
        io_thread._stop_requested.set()

    monkeypatch.setattr(io_thread, "_init_cul", initialize_connection)

    io_thread.run()

    assert port.closed
    assert io_thread._com_port is None


def test_paired_devices_use_unique_membership_entries() -> None:
    connection = _communication.MaxConnection(paired_devices=[123, 123])

    assert connection._paired_devices == {123}

    connection.add_paired_device(456)

    assert connection._paired_devices == {123, 456}


def test_wall_thermostat_control_message_is_acknowledged() -> None:
    device_id = 0x6A285
    connection = _communication.MaxConnection(paired_devices=[device_id])
    acknowledgements = []
    connection._send_ack = acknowledgements.append
    message = WallThermostatControlMessage(
        sender_id=device_id,
        desired_temperature=4.5,
        temperature=23.4,
    )

    connection._handle_message(message, 242)

    assert acknowledgements == [message]
