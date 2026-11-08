import pytest

from app.monitoring import system_health


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("throttled=0x0", False),
        ("throttled=0x1", True),
        ("throttled=0x10000", False),
    ],
)
def test_read_undervoltage_active_uses_current_status_bit(monkeypatch, output, expected):
    monkeypatch.setattr(system_health, "_run_cmd", lambda _: output)

    assert system_health.read_undervoltage_active() is expected


@pytest.mark.parametrize("output", [None, "unexpected", "throttled=not-hex"])
def test_read_undervoltage_active_returns_unknown_for_invalid_output(monkeypatch, output):
    monkeypatch.setattr(system_health, "_run_cmd", lambda _: output)

    assert system_health.read_undervoltage_active() is None


class _Database:
    def __init__(self):
        self.state = {}

    def set_state(self, key, value):
        self.state[key] = value


def test_core_voltage_measurement_does_not_trigger_killswitch(monkeypatch):
    database = _Database()
    monkeypatch.setattr(system_health, "read_temperature_celsius", lambda: 45.0)
    monkeypatch.setattr(system_health, "read_memory_percent", lambda: 50.0)
    monkeypatch.setattr(system_health, "_run_cmd", lambda _: "throttled=0x0")

    result = system_health.SystemHealthMonitor(database).check(now=1.0)

    assert result["undervoltage_active"] is False
    assert result["killswitch_activated"] is False
    assert database.state == {}


def test_current_undervoltage_activates_killswitch(monkeypatch):
    database = _Database()
    monkeypatch.setattr(system_health, "read_temperature_celsius", lambda: 45.0)
    monkeypatch.setattr(system_health, "read_memory_percent", lambda: 50.0)
    monkeypatch.setattr(system_health, "_run_cmd", lambda _: "throttled=0x1")

    result = system_health.SystemHealthMonitor(database).check(now=1.0)

    assert result["undervoltage_active"] is True
    assert result["killswitch_activated"] is True
    assert database.state == {"killswitch": "1"}
