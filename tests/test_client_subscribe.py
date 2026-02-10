import os
import sys
import threading
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.client import QilowattMQTTClient
from qilowatt.base_device import BaseDevice


class DummyDevice(BaseDevice):
    def __init__(self):
        super().__init__(device_id="DEVICE123")
        self.stop_called = False

    def handle_command(self, payload: bytes) -> None:
        pass

    def get_sensor_data(self):
        return {}

    def get_state_data(self):
        return {}

    def stop_timers(self) -> None:
        self.stop_called = True


created_timers = []


class ManualTimer:
    """Timer that can be manually triggered for testing."""

    def __init__(self, delay, callback):
        self.delay = delay
        self.callback = callback
        self.cancelled = False
        self.started = False

    def start(self):
        self.started = True
        created_timers.append(self)

    def cancel(self):
        self.cancelled = True

    def fire(self):
        """Manually trigger the timer callback."""
        if not self.cancelled and self.started:
            self.callback()


@pytest.fixture
def patched_environment(monkeypatch):
    created_timers.clear()
    mock_client = MagicMock()
    mock_client.is_connected.return_value = True
    mock_client.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)

    with patch("qilowatt.client.mqtt.Client", return_value=mock_client):
        monkeypatch.setattr("qilowatt.client.threading.Timer", ManualTimer)
        yield mock_client


def fire_pending_timers():
    """Fire all pending timers (one batch only)."""
    # Copy and clear first, so new timers added during fire() are preserved
    timers_to_fire = list(created_timers)
    created_timers.clear()
    for timer in timers_to_fire:
        timer.fire()


def test_subscription_confirmed_sets_connected(patched_environment):
    """Test that connection callback is only fired after subscription is confirmed."""
    device = DummyDevice()
    connection_states = []

    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
    )
    client.add_connection_callback(lambda connected: connection_states.append(connected))

    # Simulate successful connection (rc=0)
    client._on_connect(patched_environment, None, MagicMock(), 0, None)

    # Connection should not be reported yet (waiting for SUBACK)
    assert client._connected is False
    assert client._subscribed is False
    assert len(connection_states) == 0

    # Simulate SUBACK received with matching mid
    client._on_subscribe(patched_environment, None, 1, [0], None)

    # Now connection should be reported
    assert client._connected is True
    assert client._subscribed is True
    assert connection_states == [True]

    client.disconnect()


def test_subscription_timeout_triggers_retry(patched_environment):
    """Test that subscription timeout triggers a retry."""
    device = DummyDevice()
    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
    )

    # Simulate successful connection
    client._on_connect(patched_environment, None, MagicMock(), 0, None)

    # First subscribe should have been called
    assert patched_environment.subscribe.call_count == 1

    # Simulate timeout by firing the timer
    fire_pending_timers()

    # Should have retried
    assert patched_environment.subscribe.call_count == 2

    client.disconnect()


def test_subscription_retry_limit(patched_environment):
    """Test that subscription gives up after max retries."""
    device = DummyDevice()
    connection_states = []

    # Make subscribe always succeed in sending but we never call on_subscribe
    patched_environment.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)

    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
    )
    client._max_subscribe_retries = 2
    client.add_connection_callback(lambda connected: connection_states.append(connected))

    # Simulate successful connection (attempt 1)
    client._on_connect(patched_environment, None, MagicMock(), 0, None)
    assert patched_environment.subscribe.call_count == 1

    # Fire timeout (attempt 2)
    fire_pending_timers()
    assert patched_environment.subscribe.call_count == 2

    # Fire timeout again (gives up after max retries)
    fire_pending_timers()

    # After max retries, should still mark as connected (for publishing)
    # but subscribed should be False
    assert client._connected is True
    assert client._subscribed is False
    assert patched_environment.subscribe.call_count == 2

    client.disconnect()


def test_disconnect_resets_subscription_state(patched_environment):
    """Test that disconnect resets subscription state."""
    device = DummyDevice()
    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
    )

    # Simulate successful connection and subscription
    client._on_connect(patched_environment, None, MagicMock(), 0, None)
    client._on_subscribe(patched_environment, None, 1, [0], None)

    assert client._connected is True
    assert client._subscribed is True

    # Disconnect
    client.disconnect()

    assert client._connected is False
    assert client._subscribed is False


def test_reconnect_resubscribes(patched_environment):
    """Test that reconnect triggers resubscription."""
    device = DummyDevice()
    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
    )

    # First connection and subscription
    client._on_connect(patched_environment, None, MagicMock(), 0, None)
    assert patched_environment.subscribe.call_count == 1
    client._on_subscribe(patched_environment, None, 1, [0], None)

    # Clear timers from first connection
    created_timers.clear()

    # Simulate disconnect
    client._on_disconnect(patched_environment, None, MagicMock(), 7, None)
    assert client._subscribed is False

    # Return new mid for second subscription
    patched_environment.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 2)

    # Simulate reconnect
    client._on_connect(patched_environment, None, MagicMock(), 0, None)
    assert patched_environment.subscribe.call_count == 2
    assert client._subscribed is False  # Not yet confirmed

    # Confirm second subscription
    client._on_subscribe(patched_environment, None, 2, [0], None)
    assert client._subscribed is True

    client.disconnect()


def test_subscribe_failure_retries(patched_environment):
    """Test that subscribe failure (not timeout) also retries."""
    device = DummyDevice()

    # Make first subscribe fail, then succeed
    call_count = [0]
    def mock_subscribe(topic):
        call_count[0] += 1
        if call_count[0] == 1:
            return (mqtt.MQTT_ERR_NO_CONN, None)
        return (mqtt.MQTT_ERR_SUCCESS, 1)

    patched_environment.subscribe.side_effect = mock_subscribe

    client = QilowattMQTTClient(
        mqtt_username="user",
        mqtt_password="pass",
        device=device,
    )

    # Simulate successful connection
    # First call fails immediately and retries synchronously
    client._on_connect(patched_environment, None, MagicMock(), 0, None)

    # First call fails, should retry immediately (synchronously)
    assert patched_environment.subscribe.call_count == 2

    # Confirm subscription (second subscribe returned mid=1)
    client._on_subscribe(patched_environment, None, 1, [0], None)
    assert client._subscribed is True

    client.disconnect()
