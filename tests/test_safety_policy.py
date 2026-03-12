from sienna_diag.safety_policy import SafetyPolicy


def test_rejects_non_hex_obd_command() -> None:
    decision = SafetyPolicy.validate_obd_command("01ZZ")
    assert not decision.allowed
    assert "hexadecimal" in decision.reason


def test_rejects_odd_length_obd_command() -> None:
    decision = SafetyPolicy.validate_obd_command("010")
    assert not decision.allowed
    assert "even number" in decision.reason


def test_normalizes_whitespace_and_lowercase_obd_command() -> None:
    decision = SafetyPolicy.validate_obd_command(" 01 0c ")
    assert decision.allowed
