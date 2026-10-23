"""Fixed-grid publishing timers and the inverter data provider."""

import os
import sys
import threading
import time
from unittest.mock import patch

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


def test_status0_timer_runs_on_the_grid_only():
    """The startup STATUS0 is sent from on_connected(); the timer must not send
    one immediately, since timers can start before the CONNACK has arrived."""
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
    time.sleep(0.02)
    assert dev.status_ticks == []                       # nothing immediately
    assert _wait_for(lambda: len(dev.status_ticks) >= 2)
    dev.stop_timers()
    assert dev.status_ticks[0] - start > 0.04           # first one after an interval

    sent = []
    dev.publish_status0_data = lambda: sent.append(1)
    dev.on_connected()
    assert _wait_for(lambda: sent == [1])
    dev.stop_timers()


def test_on_connected_returns_immediately_and_sends_on_a_worker():
    """Host discovery can block; the hook must not hold the MQTT thread."""
    dev = TickDevice(0.5)
    sender = {}
    release = threading.Event()

    def slow_publish():
        sender["thread"] = threading.current_thread()
        release.wait(timeout=2.0)
        sender["done"] = True

    dev.publish_status0_data = slow_publish
    start = time.monotonic()
    dev.on_connected()
    assert time.monotonic() - start < 0.1              # returned at once
    assert _wait_for(lambda: "thread" in sender)
    assert sender["thread"] is not threading.current_thread()
    release.set()
    dev.stop_timers()                                   # joins the worker
    assert sender.get("done") is True


def test_net_info_is_collected_once_and_cached(monkeypatch):
    import qilowatt.base_device as bd

    calls = {"dns": 0, "mac": 0}

    def fake_gethostbyname(name):
        calls["dns"] += 1
        return "10.0.0.5"

    def fake_mac(*args, **kwargs):
        calls["mac"] += 1
        return "aa:bb:cc:dd:ee:ff"

    monkeypatch.setattr(bd.socket, "gethostbyname", fake_gethostbyname)
    monkeypatch.setattr(bd.getmac, "get_mac_address", fake_mac)
    dev = TickDevice(0.5)
    first = dev.get_status0_data().to_dict()["StatusNET"]
    second = dev.get_status0_data().to_dict()["StatusNET"]
    assert first["IPAddress"] == "10.0.0.5" and first["Mac"] == "aa:bb:cc:dd:ee:ff"
    assert second == first
    assert calls == {"dns": 1, "mac": 1}


def test_net_info_is_refreshed_after_ttl_and_on_connect(monkeypatch):
    import qilowatt.base_device as bd

    addresses = iter(["10.0.0.5", "10.0.0.6", "10.0.0.7"])
    monkeypatch.setattr(bd.socket, "gethostbyname", lambda name: next(addresses))
    monkeypatch.setattr(bd.getmac, "get_mac_address", lambda *a, **k: "aa:bb:cc:dd:ee:ff")
    dev = TickDevice(0.5)
    dev.publish_status0_data = lambda: None             # keep on_connected's worker trivial

    assert dev.get_status0_data().to_dict()["StatusNET"]["IPAddress"] == "10.0.0.5"
    assert dev.get_status0_data().to_dict()["StatusNET"]["IPAddress"] == "10.0.0.5"   # cached

    dev._net_info_at -= bd.NET_INFO_TTL + 1              # lifetime expired
    assert dev.get_status0_data().to_dict()["StatusNET"]["IPAddress"] == "10.0.0.6"

    dev.on_connected()                                   # reconnect clears the cache
    dev.stop_timers()
    assert dev.get_status0_data().to_dict()["StatusNET"]["IPAddress"] == "10.0.0.7"


def test_stop_timers_never_joins_an_unstarted_worker():
    """Regression: on_connected() on one thread, stop_timers() on another. The
    worker must be created and started under the lock stop_timers() takes, so
    join() can never see a thread that has not been started."""
    import qilowatt.base_device as bd

    dev = TickDevice(0.5)
    dev.publish_status0_data = lambda: None
    in_start = threading.Event()
    release_start = threading.Event()
    real_start = threading.Thread.start

    def slow_start(thread):
        if thread.name.endswith("Status0OnConnect"):
            in_start.set()
            release_start.wait(timeout=2.0)
        return real_start(thread)

    errors = []

    def stopper():
        try:
            dev.stop_timers()
        except Exception as exc:           # the old code raised RuntimeError here
            errors.append(exc)

    with patch.object(threading.Thread, "start", slow_start):
        connector = threading.Thread(target=dev.on_connected)
        real_start(connector)
        assert in_start.wait(timeout=2.0)
        stop_thread = threading.Thread(target=stopper)
        real_start(stop_thread)
        time.sleep(0.05)                   # stop_timers() is now waiting on the lock
        release_start.set()
        connector.join(timeout=2.0)
        stop_thread.join(timeout=2.0)

    assert errors == [], errors
    assert not connector.is_alive() and not stop_thread.is_alive()


def test_net_info_survives_invalidation_during_the_cache_check(monkeypatch):
    """Regression: on_connected() clears the cache from another thread. Simulate
    it landing between the None check and the return by clearing the cache from
    inside the clock call the check makes."""
    import qilowatt.base_device as bd

    monkeypatch.setattr(bd.socket, "gethostbyname", lambda name: "10.0.0.5")
    monkeypatch.setattr(bd.getmac, "get_mac_address", lambda *a, **k: "aa:bb:cc:dd:ee:ff")
    dev = TickDevice(0.5)
    dev.get_status0_data()                               # populate the cache

    real_monotonic = time.monotonic

    def clock_that_invalidates():
        dev._net_info = None                             # the reconnect's clear
        return real_monotonic()

    monkeypatch.setattr(bd.time, "monotonic", clock_that_invalidates)
    net = dev.get_status0_data().to_dict()["StatusNET"]  # raised TypeError before
    assert net["IPAddress"] == "10.0.0.5"


def test_reconnect_while_worker_is_finishing_still_sends_status0():
    """Regression: a reconnect arriving while the previous worker is still
    finishing (e.g. a dropped attempt on the old connection) must not lose
    its STATUS0 until the hourly timer."""
    dev = TickDevice(0.5)
    attempts = []
    first_attempt_started = threading.Event()
    release_first_attempt = threading.Event()

    def publish():
        attempts.append(threading.current_thread().name)
        if len(attempts) == 1:
            first_attempt_started.set()
            release_first_attempt.wait(timeout=2.0)   # "dropped" send still in flight

    dev.publish_status0_data = publish
    dev.on_connected()                                   # first connection
    assert first_attempt_started.wait(timeout=2.0)
    dev.on_connected()                                   # reconnect while worker busy
    release_first_attempt.set()
    assert _wait_for(lambda: len(attempts) == 2)
    dev.stop_timers()
    assert len(attempts) == 2                            # exactly one resend, no burst
    assert len({name for name in attempts}) == 1         # same single worker


def test_net_info_failure_falls_back_and_retries(monkeypatch):
    import qilowatt.base_device as bd

    attempts = {"dns": 0}

    def flaky_gethostbyname(name):
        attempts["dns"] += 1
        if attempts["dns"] == 1:
            raise OSError("Name or service not known")
        return "10.0.0.5"

    monkeypatch.setattr(bd.socket, "gethostbyname", flaky_gethostbyname)
    monkeypatch.setattr(bd.getmac, "get_mac_address", lambda *a, **k: "aa:bb:cc:dd:ee:ff")
    dev = TickDevice(0.5)
    first = dev.get_status0_data().to_dict()["StatusNET"]     # must not raise
    assert first["IPAddress"] == "0.0.0.0"
    second = dev.get_status0_data().to_dict()["StatusNET"]
    assert second["IPAddress"] == "10.0.0.5"
    assert attempts["dns"] == 2


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
