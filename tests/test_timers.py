"""Fixed-grid publishing timers and the inverter data provider."""

import os
import sys
import threading
import time
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.base_device import BaseDevice
from qilowatt.devices.inverter import InverterDevice
from qilowatt.models import EnergyData, MetricsData


class TickDevice(BaseDevice):
    """Device that records when each SENSOR message is handed to the publish callback."""

    def __init__(self, interval, slow_on=None, slow_for=2.5, fail_on=None, clock=time.monotonic):
        super().__init__("DEV", sensor_interval=interval, state_interval=3600, status0_interval=3600)
        self.wire = []          # publish-callback timestamps (what the broker sees)
        self.slow_on = slow_on
        self.slow_for = slow_for
        self.fail_on = fail_on
        self.clock = clock
        self._n = 0
        self.set_publish_callback(lambda topic, data: self.wire.append(self.clock()))

    def handle_command(self, payload):
        pass

    def get_sensor_data(self):
        n = self._n
        self._n += 1
        if self.fail_on is not None and n == self.fail_on:
            raise RuntimeError("boom")
        if self.slow_on is not None and n == self.slow_on:
            time.sleep(self._sensor_interval * self.slow_for)
        return {"n": n}

    def get_state_data(self):
        return {}


def _gaps(times):
    return [b - a for a, b in zip(times, times[1:])]


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
    assert _wait_for(lambda: len(dev.wire) >= 12)
    dev.stop_timers()

    # The n-th message is due at start + n * interval, independent of the time
    # spent in earlier publishes: no accumulated drift.
    for n, t in enumerate(dev.wire[:12], start=1):
        assert abs((t - start) - n * interval) < interval * 0.6, (n, t - start)
    assert all(gap > interval * 0.9 for gap in _gaps(dev.wire[:12])), _gaps(dev.wire[:12])


def test_slow_publish_is_not_followed_by_a_close_message():
    """A publish that takes 2.5 intervals must be followed by a full gap, measured
    from when the delayed message actually went out."""
    interval = 0.05
    dev = TickDevice(interval, slow_on=2, slow_for=2.5)
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.wire) >= 6)
    dev.stop_timers()

    gaps = _gaps(dev.wire[:6])
    assert all(gap > interval * 0.9 for gap in gaps), gaps
    assert gaps[1] > interval * 2.4, gaps          # the slow one itself


def test_publish_just_shorter_than_the_interval_still_keeps_spacing():
    """Regression: an action lasting 0.95 interval finishes just before the next
    slot; the old code published again a few ms later."""
    interval = 0.05
    dev = TickDevice(interval, slow_on=1, slow_for=0.95)
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.wire) >= 5)
    dev.stop_timers()
    gaps = _gaps(dev.wire[:5])
    assert all(gap > interval * 0.9 for gap in gaps), gaps


def test_late_wakeup_skips_slots_instead_of_bursting(monkeypatch):
    """A suspended host wakes up 2.3 intervals late: the overdue slot is published
    at once and the next message waits a full interval, not the few ms until the
    next grid slot."""
    import qilowatt.base_device as bd

    interval = 0.05
    offset = [0.0]
    real = time.monotonic
    fake_clock = lambda: real() + offset[0]
    monkeypatch.setattr(bd.time, "monotonic", fake_clock)

    dev = TickDevice(interval, clock=fake_clock)
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.wire) >= 1)
    offset[0] += interval * 2.3          # the clock jumps while the timer sleeps
    assert _wait_for(lambda: len(dev.wire) >= 4)
    dev.stop_timers()

    gaps = _gaps(dev.wire[:4])
    assert all(gap > interval * 0.9 for gap in gaps), gaps


def test_failing_publish_does_not_stop_the_timer():
    dev = TickDevice(0.03, fail_on=1)
    dev._start_sensor_timer()
    assert _wait_for(lambda: len(dev.wire) >= 3)
    dev.stop_timers()
    assert dev._sensor_timer_thread is None
    assert dev._n >= 4                   # the failing call happened and was skipped


@pytest.mark.parametrize("bad", [0, -1, 0.0, float("inf"), float("nan")])
def test_invalid_intervals_are_rejected_before_any_thread_starts(bad):
    with pytest.raises(ValueError):
        InverterDevice("DEV", sensor_interval=bad)
    with pytest.raises(ValueError):
        InverterDevice("DEV", state_interval=bad)
    with pytest.raises(ValueError):
        InverterDevice("DEV", status0_interval=bad)


def test_non_numeric_interval_is_rejected():
    with pytest.raises(TypeError):
        InverterDevice("DEV", sensor_interval="10")


class Status0Device(TickDevice):
    """TickDevice with a fast STATUS0 interval and recorded STATUS0 sends."""

    def __init__(self, status0_interval=0.05):
        super().__init__(0.5)
        self._status0_interval = status0_interval
        self.status_sends = []            # (monotonic time, thread name)
        self.block_send = None            # Event: block inside a send while set

    def publish_status0_data(self):
        self.status_sends.append((time.monotonic(), threading.current_thread().name))
        if self.block_send is not None:
            self.block_send.wait(timeout=2.0)


def test_status0_without_a_connect_is_sent_on_the_grid_only():
    """No immediate send any more: timers may start before the CONNACK has
    arrived, and a message sent then would be dropped."""
    dev = Status0Device()
    start = time.monotonic()
    dev._start_status0_timer()
    time.sleep(0.02)
    assert dev.status_sends == []
    assert _wait_for(lambda: len(dev.status_sends) >= 2)
    dev.stop_timers()
    assert dev.status_sends[0][0] - start > 0.04


def test_on_connected_sends_status0_at_once_on_the_timer_thread():
    dev = Status0Device(status0_interval=3600)
    dev._start_status0_timer()
    start = time.monotonic()
    dev.on_connected()                                  # returns immediately
    assert time.monotonic() - start < 0.05
    assert _wait_for(lambda: len(dev.status_sends) == 1)
    dev.stop_timers()
    assert dev.status_sends[0][1].endswith("Status0Timer")
    assert dev.status_sends[0][1] != threading.current_thread().name


def test_connect_before_timers_start_is_served_when_they_start():
    dev = Status0Device(status0_interval=3600)
    dev.on_connected()                                  # CONNACK before data/timers
    dev._start_status0_timer()
    assert _wait_for(lambda: len(dev.status_sends) == 1)
    dev.stop_timers()


def test_every_reconnect_sends_status0_again():
    dev = Status0Device(status0_interval=3600)
    dev._start_status0_timer()
    for n in range(1, 4):
        dev.on_connected()
        assert _wait_for(lambda: len(dev.status_sends) == n)
    dev.stop_timers()
    assert len(dev.status_sends) == 3


def test_reconnect_during_an_in_flight_send_is_not_lost():
    dev = Status0Device(status0_interval=3600)
    dev.block_send = threading.Event()
    dev._start_status0_timer()
    dev.on_connected()
    assert _wait_for(lambda: len(dev.status_sends) == 1)   # blocked inside the send
    dev.on_connected()                                       # reconnect meanwhile
    dev.block_send.set()
    assert _wait_for(lambda: len(dev.status_sends) == 2)
    time.sleep(0.05)
    dev.stop_timers()
    assert len(dev.status_sends) == 2                        # exactly one more, no burst


def test_stop_timers_wakes_a_waiting_status0_loop_promptly():
    dev = Status0Device(status0_interval=3600)
    dev._start_status0_timer()
    start = time.monotonic()
    dev.stop_timers()
    assert time.monotonic() - start < 1.0
    assert dev._status0_timer_thread is None
    assert dev.status_sends == []


def test_net_info_failure_falls_back_to_placeholders(monkeypatch):
    import qilowatt.base_device as bd

    monkeypatch.setattr(bd.socket, "gethostbyname", lambda name: (_ for _ in ()).throw(OSError("no dns")))
    monkeypatch.setattr(bd.getmac, "get_mac_address", lambda *a, **k: None)
    net = TickDevice(0.5).get_status0_data().to_dict()["StatusNET"]    # must not raise
    assert net["IPAddress"] == "0.0.0.0" and net["Mac"] == ""


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
