"""Fixed-grid publishing timers and the inverter data provider."""

import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.base_device import BaseDevice
from qilowatt.devices.inverter import InverterDevice
from qilowatt.models import EnergyData, MetricsData


class TickDevice(BaseDevice):
    """Device whose sensor publishes are just timestamps."""

    def __init__(self, interval, slow_on=None, fail_on=None):
        super().__init__("DEV", sensor_interval=interval, state_interval=3600, status0_interval=3600)
        self.ticks = []
        self.slow_on = slow_on
        self.fail_on = fail_on
        self.set_publish_callback(lambda topic, data: None)

    def handle_command(self, payload):
        pass

    def get_sensor_data(self):
        n = len(self.ticks)
        self.ticks.append(time.monotonic())
        if self.fail_on is not None and n == self.fail_on:
            raise RuntimeError("boom")
        if self.slow_on is not None and n == self.slow_on:
            time.sleep(self._sensor_interval * 2.5)
        return {"n": n}

    def get_state_data(self):
        return {}


def _energy():
    return EnergyData(Power=[1.0], Today=0.0, Total=0.0, Current=[0.0], Voltage=[230.0], Frequency=50.0)


def _metrics(soc):
    return MetricsData(PvPower=[0.0], PvVoltage=[0.0], PvCurrent=[0.0], LoadPower=[0.0], BatterySOC=[soc],
                       LoadCurrent=[0.0], BatteryPower=[0.0], BatteryCurrent=[0.0], BatteryVoltage=[48.0],
                       GridExportLimit=0.0, BatteryTemperature=[20.0], InverterTemperature=30.0)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_sensor_publishes_land_on_fixed_grid_without_drift():
    interval = 0.05
    dev = TickDevice(interval)
    start = time.monotonic()
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.ticks) >= 12)
    dev.stop_timers()

    # The n-th publish is due at start + n * interval, independent of the time
    # spent in earlier publishes: no accumulated drift.
    for n, t in enumerate(dev.ticks[:12], start=1):
        assert abs((t - start) - n * interval) < interval * 0.6, (n, t - start)
    gaps = [b - a for a, b in zip(dev.ticks, dev.ticks[1:12])]
    assert all(gap > interval * 0.5 for gap in gaps), gaps


def test_slow_publish_skips_missed_slots_instead_of_bursting():
    interval = 0.05
    dev = TickDevice(interval, slow_on=2)
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.ticks) >= 6)
    dev.stop_timers()

    gaps = [b - a for a, b in zip(dev.ticks, dev.ticks[1:6])]
    # Publish #2 took 2.5 intervals; the next one must not follow immediately
    assert all(gap > interval * 0.5 for gap in gaps), gaps
    assert gaps[2] > interval * 2, gaps


def test_failing_publish_does_not_stop_the_timer():
    dev = TickDevice(0.03, fail_on=1)
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.ticks) >= 4)
    dev.stop_timers()
    assert dev._sensor_timer_thread is None


def test_status0_is_sent_at_startup_then_on_the_grid():
    class StatusDevice(TickDevice):
        def __init__(self):
            super().__init__(0.5)
            self._status0_interval = 0.05
            self.status_ticks = []
        def publish_status0_data(self):
            self.status_ticks.append(time.monotonic())

    dev = StatusDevice()
    start = time.monotonic()
    dev._start_status0_timer()
    assert _wait_for(lambda: len(dev.status_ticks) >= 3)
    dev.stop_timers()
    assert dev.status_ticks[0] - start < 0.03          # immediately at startup
    assert dev.status_ticks[1] - start > 0.04          # then after one interval


def test_data_provider_is_pulled_right_before_each_publish(monkeypatch):
    dev = InverterDevice("DEV")
    monkeypatch.setattr(dev, "start_timers", lambda: None)
    calls = []

    def provider():
        calls.append(1)
        return _energy(), _metrics(soc=len(calls))

    dev.set_data_provider(provider)
    assert dev._data_initialized is True

    first = dev.get_sensor_data()
    second = dev.get_sensor_data()
    assert len(calls) == 2
    assert first["METRICS"]["BatterySOC"] == [1]
    assert second["METRICS"]["BatterySOC"] == [2]


def test_data_provider_failure_falls_back_to_last_data(monkeypatch):
    dev = InverterDevice("DEV")
    monkeypatch.setattr(dev, "start_timers", lambda: None)
    state = {"fail": False}

    def provider():
        if state["fail"]:
            raise RuntimeError("inverter integration not loaded")
        return _energy(), _metrics(soc=42)

    dev.set_data_provider(provider)
    assert dev.get_sensor_data()["METRICS"]["BatterySOC"] == [42]

    state["fail"] = True
    assert dev.get_sensor_data()["METRICS"]["BatterySOC"] == [42]


def test_data_provider_with_no_data_yet_skips_the_slot(monkeypatch):
    dev = InverterDevice("DEV")
    monkeypatch.setattr(dev, "start_timers", lambda: None)
    published = []
    dev.set_publish_callback(lambda topic, data: published.append(data))
    dev.set_data_provider(lambda: (_ for _ in ()).throw(RuntimeError("not ready")))

    dev.publish_sensor_data()
    assert published == []

    dev.set_data_provider(lambda: (_energy(), _metrics(soc=7)))
    dev.publish_sensor_data()
    assert len(published) == 1 and published[0]["METRICS"]["BatterySOC"] == [7]


def test_set_data_provider_starts_timers_once():
    dev = InverterDevice("DEV", sensor_interval=3600, state_interval=3600, status0_interval=3600)
    started = []
    dev.start_timers = lambda: started.append(1)
    dev.set_data_provider(lambda: (_energy(), _metrics(1)))
    dev.set_data_provider(lambda: (_energy(), _metrics(2)))
    dev.set_energy_data(_energy())
    dev.set_metrics_data(_metrics(3))
    assert started == [1]


def test_push_api_unchanged_without_provider(monkeypatch):
    dev = InverterDevice("DEV")
    monkeypatch.setattr(dev, "start_timers", lambda: None)
    assert dev.get_sensor_data() == {}
    dev.set_energy_data(_energy())
    dev.set_metrics_data(_metrics(soc=5))
    assert dev.get_sensor_data()["METRICS"]["BatterySOC"] == [5]
