"""Synchronous Telnet-backed byte stream for network CUL devices."""

from __future__ import annotations

import socket
from urllib.parse import urlparse

IAC = 255
DONT = 254
DO = 253
WONT = 252
WILL = 251
SB = 250
SE = 240


class TelnetException(OSError):
    """Raised when the network CUL stream cannot be read or written."""


class TelnetSerial:
    """Expose a Telnet connection using the small interface used by pyserial."""

    def __init__(self, device_path: str, timeout: float | None = None) -> None:
        parsed_path = urlparse(device_path)
        if parsed_path.hostname is None or parsed_path.port is None:
            raise TelnetException(f"Invalid CUL address: {device_path}")

        try:
            self._socket = socket.create_connection(
                (parsed_path.hostname, parsed_path.port), timeout=timeout
            )
        except OSError as err:
            raise TelnetException(str(err)) from err
        self._socket.settimeout(timeout)
        self._buffer = bytearray()
        self._telnet_state = "data"
        self._telnet_command = 0

    def close(self) -> None:
        self._socket.close()

    def readline(self) -> bytes:
        try:
            while b"\n" not in self._buffer:
                try:
                    data = self._socket.recv(1024)
                except TimeoutError:
                    break
                if not data:
                    raise TelnetException("CUL connection closed")
                self._consume_telnet_data(data)

            newline = self._buffer.find(b"\n")
            end = newline + 1 if newline >= 0 else len(self._buffer)
            line = bytes(self._buffer[:end])
            del self._buffer[:end]
            return line
        except OSError as err:
            if isinstance(err, TelnetException):
                raise
            raise TelnetException(str(err)) from err

    def write(self, data: bytes) -> None:
        try:
            self._socket.sendall(data)
        except OSError as err:
            raise TelnetException(str(err)) from err

    def _consume_telnet_data(self, data: bytes) -> None:
        for value in data:
            if self._telnet_state == "data":
                if value == IAC:
                    self._telnet_state = "command"
                else:
                    self._buffer.append(value)
            elif self._telnet_state == "command":
                if value == IAC:
                    self._buffer.append(IAC)
                    self._telnet_state = "data"
                elif value in (DO, DONT, WILL, WONT):
                    self._telnet_command = value
                    self._telnet_state = "option"
                elif value == SB:
                    self._telnet_state = "subnegotiation"
                else:
                    self._telnet_state = "data"
            elif self._telnet_state == "option":
                if self._telnet_command == DO:
                    self._socket.sendall(bytes((IAC, WONT, value)))
                elif self._telnet_command == WILL:
                    self._socket.sendall(bytes((IAC, DONT, value)))
                self._telnet_state = "data"
            elif self._telnet_state == "subnegotiation":
                if value == IAC:
                    self._telnet_state = "subnegotiation_iac"
            elif self._telnet_state == "subnegotiation_iac":
                self._telnet_state = (
                    "data" if value == SE else "subnegotiation"
                )
