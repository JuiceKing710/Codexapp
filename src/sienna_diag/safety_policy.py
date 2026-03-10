from __future__ import annotations

from dataclasses import dataclass


SAFE_MODES = {
    "01",  # current data
    "02",  # freeze frame data
    "03",  # stored DTCs
    "07",  # pending DTCs
    "09",  # vehicle info
}

BLOCKED_MODES = {
    "04",  # clear DTCs/reset
    "05",  # oxygen sensor monitoring test results (legacy write-risk contexts)
    "06",  # on-board monitoring test (allowable on many cars but blocked by strict prototype policy)
    "08",  # control operation
    "0A",  # permanent DTCs (read-only but blocked for conservative policy)
    "10",  # diagnostic session control
    "11",  # ECU reset
    "27",  # security access
    "2E",  # write data by identifier
    "31",  # routine control
    "34",  # request download
    "36",  # transfer data
    "37",  # request transfer exit
}

# Documented baseline ELM/OBDLink setup commands used in this prototype.
DOCUMENTED_ADAPTER_SETUP = {
    "ATZ",
    "ATE0",
    "ATL0",
    "ATS0",
    "ATH0",
    "ATSP3",  # ISO 9141-2
}


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    reason: str


class SafetyPolicy:
    @staticmethod
    def validate_obd_command(command: str) -> SafetyDecision:
        normalized = command.strip().replace(" ", "").upper()
        if len(normalized) < 2:
            return SafetyDecision(False, "Command too short")

        mode = normalized[:2]
        if mode in BLOCKED_MODES:
            return SafetyDecision(False, f"Blocked mode {mode}: prohibited for this read-only prototype")
        if mode not in SAFE_MODES:
            return SafetyDecision(False, f"Unsupported mode {mode}: only read-only standard modes are allowed")

        return SafetyDecision(True, "Read-only OBD mode allowed")

    @staticmethod
    def validate_adapter_command(command: str) -> SafetyDecision:
        normalized = command.strip().upper()
        if normalized in DOCUMENTED_ADAPTER_SETUP:
            return SafetyDecision(True, "Documented setup command allowed")
        return SafetyDecision(False, "Only documented adapter setup commands are allowed")
