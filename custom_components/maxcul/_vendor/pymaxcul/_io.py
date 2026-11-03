""" This module implements the low level logic of talking to the serial CUL device"""
import queue
from collections import deque
import re
import threading
import time
import logging
from serial import Serial, SerialException

from ._telnet import TelnetSerial, TelnetException

LOGGER = logging.getLogger(__name__)

MAX_QUEUED_COMMANDS = 10
READLINE_TIMEOUT = 0.0
SERIAL_REOPEN_INTERVAL = 24 * 60 * 60

COMMAND_REQUEST_BUDGET = 'X'

MIN_REQUIRED_BUDGET = 1500
COMMAND_CHARACTER_WEIGHT = 30

CUL_VERSION_PATTERN = re.compile(r"^V\s+\d+(?:\.\d+)*(?:\s+.+)?$")
FHT_ID_PATTERN = re.compile(r"^[0-9A-Fa-f]{4}$")


class CulIoThread(threading.Thread):
    """Low-level serial communication thread base"""

    # pylint: disable=too-many-instance-attributes
    def __init__(
        self,
        device_path,
        baudrate,
        sent_callback=None,
        connection_callback=None,
        fht_id_callback=None,
    ):
        super().__init__()
        self.read_queue = queue.Queue()
        self._send_queue = deque([], MAX_QUEUED_COMMANDS)
        self._device_path = device_path
        self._baudrate = baudrate
        self._stop_requested = threading.Event()
        self._cul_version = None
        self._hardware_revision = None
        self._com_port = None
        self._remaining_budget = 0
        self._sent_callback = sent_callback
        self._connection_callback = connection_callback
        self._fht_id_callback = fht_id_callback
        self._fht_id = None
        self._connection_status = None
        self._connection_error = None
        self._connection_ready = threading.Event()
        self._waiting_for_budget = False
        self._last_serial_reopen = time.monotonic()

    @property
    def cul_version(self):
        """Returns the version reported from the CUL stick"""
        return self._cul_version

    @property
    def hardware_revision(self):
        """Return the hardware revision reported by the CUL, if supported."""
        return self._hardware_revision

    @property
    def fht_id(self):
        """Return the FHT ID reported by the CUL."""
        return self._fht_id

    @property
    def connection_error(self):
        """Return the last reason the CUL could not be reached."""
        return self._connection_error

    def wait_for_connection(self, timeout=None):
        """Wait for the first CUL reachability result."""
        if not self._connection_ready.wait(timeout):
            return None
        return self._connection_status

    @property
    def has_send_budget(self):
        """Ask CUL if we have enough budget of the 1 percent rule left"""
        return self._remaining_budget >= MIN_REQUIRED_BUDGET

    def enqueue_command(self, command):
        """Pushes a new command to be sent to the CUL stick onto the queue"""
        self._send_queue.appendleft(command)

    def stop(self, timeout=None):
        """Stops the loop of this thread and waits for it to exit"""
        self._stop_requested.set()
        self.join(timeout)

    def run(self):
        try:
            self._init_cul()
            while not self._stop_requested.is_set():
                self._loop()
        finally:
            if self._com_port is not None:
                try:
                    self._com_port.close()
                except (OSError, SerialException, TelnetException):
                    LOGGER.exception(
                        "Unable to close CUL connection for %s",
                        self._device_path,
                    )
                finally:
                    self._com_port = None

    def _loop(self):
        now = time.monotonic()

        self._receive_messages()

        if self._remaining_budget < MIN_REQUIRED_BUDGET:
            if not self._waiting_for_budget:
                LOGGER.debug("Unable to send messages, budget to low.")
            self._writeline(COMMAND_REQUEST_BUDGET)
            self._waiting_for_budget = True
            time.sleep(1)
        else:
            self._waiting_for_budget = False
            self._send_pending_message()

        # Use elapsed time rather than matching a single wall-clock second.
        if now - self._last_serial_reopen >= SERIAL_REOPEN_INTERVAL:
            LOGGER.debug("Reopening serial device")
            self._reopen_serial_device()
            self._last_serial_reopen = time.monotonic()

        time.sleep(0.2)

    def _receive_messages(self):
        # Drain all currently available input before the next transmit attempt.
        while self._receive_message():
            pass

    def _receive_message(self):
        # Process pending received messages (if any)
        line = self._readline()
        if line is not None:
            if line.startswith("21"):
                self._remaining_budget = int(line[3:].strip()) * 10 or 1
                LOGGER.debug(
                    "Got pending budget: %sms", self._remaining_budget)
            elif line.startswith("ZERR"):
                LOGGER.warning(
                    "Received error message from CUL stick: '%s'",
                    line
                )
            elif line.startswith("Z"):
                LOGGER.debug(
                    "Received new moritz message: %s", line)
                self.read_queue.put(line)
            elif FHT_ID_PATTERN.fullmatch(line):
                self._fht_id = line.upper()
                if self._fht_id_callback is not None:
                    self._fht_id_callback(self._fht_id)
            else:
                LOGGER.debug("Got unhandled response from CUL: '%s'", line)
        return line is not None

    def _send_pending_message(self):
        try:
            pending_message = self._send_queue.pop()
        except IndexError:
            return
        try:
            command = pending_message.encode_message()
            if self._remaining_budget > len(command) * COMMAND_CHARACTER_WEIGHT:
                self._writeline(command)
                if self._sent_callback:
                    self._sent_callback(pending_message)
                if command.startswith("Zs"):
                    self._remaining_budget -= COMMAND_CHARACTER_WEIGHT * len(command)
            else:
                self._send_queue.append(pending_message)
                self._writeline(COMMAND_REQUEST_BUDGET)
        except Exception as err:
            LOGGER.error(
                "Exception <%s> was raised while encoding message %s. Please consider reporting this as a bug.",
                err,
                pending_message)

    def _init_cul(self):
        if not self._open_serial_device():
            if self._connection_status is None:
                self._report_connection_status(
                    False, "CUL did not return a valid firmware version"
                )
            self._stop_requested.set()
            return

        if self._com_port is None:
            LOGGER.error("No version from CUL, cannot communicate")
            if self._connection_status is None:
                self._report_connection_status(
                    False, "CUL did not return a valid firmware version"
                )
            self._stop_requested.set()

    def _open_serial_device(self):
        if self._device_path.startswith("telnet://"):
            return self._open_telnet_connection()
        else:
            return self._open_serial_port()

    def _open_serial_port(self):
        try:
            self._com_port = Serial(
                self._device_path,
                self._baudrate,
                timeout=READLINE_TIMEOUT,
                exclusive=True,
            )
        except SerialException as err:
            if is_serial_port_in_use(err):
                self._report_connection_status(
                    False,
                    "Serial device is locked by another process; stop the other "
                    "application or container",
                )
                LOGGER.error(
                    "Serial device %s is already in use by another process: %s",
                    self._device_path,
                    err,
                )
            else:
                LOGGER.error("Unable to open serial device <%s>", err)
            return False

        return self._initialize_serial_device()

    def _open_telnet_connection(self):
        try:
            self._com_port = TelnetSerial(
                self._device_path,
                timeout=0.5
            )
        except TelnetException as err:
            LOGGER.error("Unable to open telnet connection <%s>", err)
            return False

        return self._initialize_serial_device()

    def _initialize_serial_device(self):
        # was required for my nanoCUL
        time.sleep(2)
        # get CUL FW version
        for _ in range(10):
            self._writeline("V")
            time.sleep(1)
            response = self._readline()
            self._cul_version = (
                response if is_cul_version_response(response) else None
            )
            if self._cul_version:
                LOGGER.debug("CUL reported version %s", self._cul_version)
                break
            else:
                LOGGER.info("No version from CUL reported?")
        if self._cul_version is None:
            self._com_port.close()
            self._com_port = None
            self._report_connection_status(
                False, "CUL did not return a valid firmware version to command V"
            )
            return False
        self._hardware_revision = None
        self._writeline("VH")
        for _ in range(10):
            time.sleep(0.1)
            hardware_revision = self._readline()
            if hardware_revision:
                if re.fullmatch(r"CUL_[A-Z0-9_]+", hardware_revision):
                    self._hardware_revision = hardware_revision
                break
        # enable reporting of message strength
        self._writeline("X21")
        time.sleep(0.3)
        # receive Moritz messages
        self._writeline("Zr")
        time.sleep(0.3)
        # disable FHT mode by setting station to 0000
        self._writeline("T01")
        time.sleep(0.3)
        # request first budget
        self._writeline(COMMAND_REQUEST_BUDGET)
        time.sleep(0.3)
        self._report_connection_status(True, None)
        return True

    def _reopen_serial_device(self):
        self._report_connection_status(False, "CUL connection lost; reconnecting")
        if self._com_port:
            try:
                self._com_port.close()
            except Exception:
                pass
            self._com_port = None
        self._remaining_budget = 0

        for timeout in [5, 10, 20, 40, 80, 160]:
            if self._open_serial_device():
                return True
            time.sleep(timeout)
        self._report_connection_status(
            False, "Unable to reopen the CUL device after repeated attempts"
        )
        return False

    def _report_connection_status(self, connected, error):
        """Notify the connection wrapper when CUL reachability changes."""
        if (
            connected == self._connection_status
            and (connected or error == self._connection_error)
        ):
            return
        self._connection_status = connected
        self._connection_error = error
        self._connection_ready.set()
        if self._connection_callback is not None:
            self._connection_callback(connected, error)

    def _writeline(self, command):
        """Sends given command to CUL. Invalidates has_send_budget if command starts with Zs"""
        if not command == COMMAND_REQUEST_BUDGET:
            LOGGER.debug("Writing command %s", command)
        try:
            self._com_port.write((command + "\r\n").encode())
        except (SerialException, TelnetException) as err:
            LOGGER.error(
                "Error writing to serial device <%s>. Try reopening it.", err)
            if self._reopen_serial_device():
                self._writeline(command)
            else:
                LOGGER.error("Unable to reopen serial device, quitting")
                self._stop_requested.set()

    def _readline(self):
        try:
            line = self._com_port.readline()
            line = line.decode('utf-8').strip()
            if line:
                return line
            return None
        except (SerialException, TelnetException) as err:
            LOGGER.error(
                "Error reading from serial device <%s>. Try reopening it.", err)
            if self._reopen_serial_device():
                return self._readline()
            else:
                LOGGER.error("Unable to reopen serial device, quitting")
                self._stop_requested.set()
                return None


def is_cul_version_response(response):
    """Return whether a line contains a recognizable CUL firmware version."""
    return bool(response and CUL_VERSION_PATTERN.fullmatch(response.strip()))


def is_serial_port_in_use(error):
    """Identify pyserial's explicit exclusive-port-lock failure."""
    message = str(error).casefold()
    return "exclusive" in message and "lock" in message
