from __future__ import annotations

from dataclasses import dataclass

from sienna_diag.config import settings
from sienna_diag.safety_policy import SafetyPolicy

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None


@dataclass
class AdapterResponse:
    command: str
    raw: str


class OBDLinkAdapter:
    """Read-only OBDLink adapter wrapper.

    Defaults to ISO 9141-2 init (`ATSP3`) unless caller explicitly marks CAN capture
    in upstream metadata. This class itself only sends whitelisted setup commands and
    whitelisted read-only OBD mode requests.
    """

    def __init__(self) -> None:
        self.port = settings.obdlink_port
        self.baud = settings.obdlink_baud
        self.timeout = settings.obdlink_timeout_seconds
        self._serial = None
        self.connected = False

    def connect(self) -> None:
        if not settings.enable_hardware:
            self.connected = False
            return
        if serial is None:
            raise RuntimeError("pyserial is required when ENABLE_HARDWARE=true")

        self._serial = serial.Serial(self.port, self.baud, timeout=self.timeout)
        for cmd in ["ATZ", "ATE0", "ATL0", "ATS0", "ATH0", "ATSP3"]:
            self._send_adapter_command(cmd)
        self.connected = True

    def close(self) -> None:
        if self._serial is not None and self._serial.is_open:
            self._serial.close()
        self.connected = False

    def _send_adapter_command(self, command: str) -> AdapterResponse:
        decision = SafetyPolicy.validate_adapter_command(command)
        if not decision.allowed:
            raise PermissionError(decision.reason)

        if not settings.enable_hardware:
            return AdapterResponse(command=command, raw="MOCK:OK")

        payload = f"{command}\r".encode("ascii")
        self._serial.write(payload)
        raw = self._serial.read_until(b">").decode("ascii", errors="replace")
        return AdapterResponse(command=command, raw=raw)

    def query(self, command: str) -> AdapterResponse:
        decision = SafetyPolicy.validate_obd_command(command)
        if not decision.allowed:
            raise PermissionError(decision.reason)

        if not settings.enable_hardware:
            return AdapterResponse(command=command, raw=f"MOCK:{command}:41 00 BE 3E A8 13")

        payload = f"{command}\r".encode("ascii")
        self._serial.write(payload)
        raw = self._serial.read_until(b">").decode("ascii", errors="replace")
        return AdapterResponse(command=command, raw=raw)

    def mode_status(self) -> str:
        return "hardware" if settings.enable_hardware else "mock"

    def connection_status(self) -> str:
        if not settings.enable_hardware:
            return "mock-ready"
        return "connected" if self.connected else "disconnected"
