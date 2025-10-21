import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.client import QilowattMQTTClient
from qilowatt.exceptions import AuthenticationError
from qilowatt.base_device import BaseDevice


class DummyDevice(BaseDevice):
    def __init__(self):
        super().__init__(device_id="DEVICE123")
        self.stop_called = False

    def handle_command(self, payload: bytes) -> None:  # pragma: no cover - not used in tests
        pass

    def get_sensor_data(self):  # pragma: no cover - not used in tests
        return {}

    def get_state_data(self):  # pragma: no cover - not used in tests
        return {}

    def stop_timers(self) -> None:
        self.stop_called = True


created_threads = []


class ImmediateTimer:
    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self._thread = None

    def start(self):
        def runner():
            if not self.cancelled:
                self.callback()

        self._thread = threading.Thread(target=runner)
        self._thread.daemon = True
        self._thread.start()
        created_threads.append(self._thread)

    def cancel(self):
        self.cancelled = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=0.2)


@pytest.fixture
def patched_environment(monkeypatch):
    created_threads.clear()
    mock_client = MagicMock()
    mock_client.is_connected.return_value = False
    mock_client.reconnect.side_effect = None

    with patch("qilowatt.client.mqtt.Client", return_value=mock_client):
        monkeypatch.setattr("qilowatt.client.threading.Timer", ImmediateTimer)
        yield mock_client


def wait_for_threads():
    for thread in list(created_threads):
        thread.join(timeout=0.2)
    created_threads.clear()


def test_authentication_failure_triggers_retry(patched_environment):
    device = DummyDevice()
    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
        max_auth_retries=2,
        auth_retry_delay=0.01,
    )

    client.connect()
    client._on_connect(patched_environment, None, None, 5)

    wait_for_threads()

    assert patched_environment.reconnect.called
    assert client.last_error() is None

    client.disconnect()


def test_authentication_failure_stops_after_max_attempts(patched_environment):
    device = DummyDevice()
    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
        max_auth_retries=1,
        auth_retry_delay=0.01,
    )

    client.connect()
    client._on_connect(patched_environment, None, None, 5)

    wait_for_threads()

    client._on_connect(patched_environment, None, None, 5)

    assert isinstance(client.last_error(), AuthenticationError)
    assert device.stop_called is True

    client.disconnect()
