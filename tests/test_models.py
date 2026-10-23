import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.devices.inverter import InverterDevice
from qilowatt.models import EnergyData, MetricsData


def _metrics(**overrides):
    base = dict(
        PvPower=[200.0, 200.0],
        PvVoltage=[300.0, 300.0],
        PvCurrent=[8.0, 8.0],
        LoadPower=[500.0, 500.0, 500.0],
        BatterySOC=[80],
        LoadCurrent=[10.0, 10.0, 10.0],
        BatteryPower=[400.0],
        BatteryCurrent=[15.0],
        BatteryVoltage=[48.0],
        GridExportLimit=5000.0,
        BatteryTemperature=[30.0],
        InverterTemperature=40.0,
    )
    base.update(overrides)
    return MetricsData(**base)


def _energy():
    return EnergyData(
        Power=[100.0, 100.0, 100.0],
        Today=1.0,
        Total=2.0,
        Current=[1.0, 1.0, 1.0],
        Voltage=[230.0, 230.0, 230.0],
        Frequency=50.0,
    )


def test_metrics_generator_fields_default_to_three_zero_phases():
    """Callers that predate the generator fields must still construct MetricsData."""
    metrics = _metrics()
    assert metrics.GenVoltage == [0.0, 0.0, 0.0]
    assert metrics.GenPower == [0.0, 0.0, 0.0]
    assert metrics.GenCurrent == [0.0, 0.0, 0.0]


def test_metrics_generator_defaults_are_not_shared_between_instances():
    first = _metrics()
    second = _metrics()
    first.GenPower[0] = 1234.0
    assert second.GenPower == [0.0, 0.0, 0.0]


def test_metrics_generator_fields_accept_explicit_values():
    metrics = _metrics(
        GenVoltage=[231.0, 232.0, 233.0],
        GenPower=[1000.0, 1100.0, 1200.0],
        GenCurrent=[4.3, 4.7, 5.2],
    )
    assert metrics.GenVoltage == [231.0, 232.0, 233.0]
    assert metrics.GenPower == [1000.0, 1100.0, 1200.0]
    assert metrics.GenCurrent == [4.3, 4.7, 5.2]


def test_sensor_payload_always_contains_generator_fields(monkeypatch):
    """The SENSOR payload must carry the Gen* keys even when the caller omits them."""
    device = InverterDevice(device_id="DEVICE123")
    # Keep the test free of background publishing threads.
    monkeypatch.setattr(device, "start_timers", lambda: None)

    device.set_energy_data(_energy())
    device.set_metrics_data(_metrics())

    metrics = device.get_sensor_data()["METRICS"]
    assert metrics["GenVoltage"] == [0.0, 0.0, 0.0]
    assert metrics["GenPower"] == [0.0, 0.0, 0.0]
    assert metrics["GenCurrent"] == [0.0, 0.0, 0.0]
    # Existing keys are untouched by the reordering.
    assert metrics["BatterySOC"] == [80]
    assert metrics["InverterTemperature"] == 40.0
