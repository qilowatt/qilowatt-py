import os
import sys
import time
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.client import QilowattMQTTClient
from qilowatt.base_device import BaseDevice


class DummyDevice(BaseDevice):
    def __init__(self):
        super().__init__(device_id="DEVICE123")
        self.stop_called = False
        self.status0_sent = 0

    def on_connected(self):
        # The real hook hands STATUS0 to a worker thread; count synchronously
        # here so the client tests can assert right after the transition
        self.status0_sent += 1

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


def _paho_invokes_on_disconnect(client, mock_client):
    """Make the mocked paho disconnect() call back into _on_disconnect synchronously,
    as the real one does once the network loop has been stopped."""
    def side_effect(*args, **kwargs):
        client._on_disconnect(mock_client, None, MagicMock(), 0, None)
        return mqtt.MQTT_ERR_SUCCESS
    mock_client.disconnect.side_effect = side_effect


def test_disconnect_stops_loop_even_without_connack(patched_environment):
    """TCP/TLS up but the broker never answered: the loop thread must still stop,
    and no "disconnected" notification is emitted for a connection that never came up."""
    mock_client = patched_environment
    mock_client.is_connected.return_value = False  # no CONNACK ever arrived
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []
    client.add_connection_callback(states.append)
    _paho_invokes_on_disconnect(client, mock_client)

    client.connect()
    mock_client.loop_start.assert_called_once()
    assert client.connected is False

    client.disconnect()

    mock_client.loop_stop.assert_called_once()
    mock_client.disconnect.assert_called_once()
    assert client._shutdown is True
    assert device.stop_called is True
    assert states == []


def test_disconnect_notifies_connected_client_exactly_once(patched_environment):
    """paho's synchronous _on_disconnect plus our own notification must not double up."""
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []
    client.add_connection_callback(states.append)
    _paho_invokes_on_disconnect(client, mock_client)

    client._on_connect(mock_client, None, MagicMock(), 0, None)
    client._on_subscribe(mock_client, None, 1, [0], None)
    assert states == [True]

    client.disconnect()

    assert states == [True, False]
    assert client.connected is False


def test_unexpected_disconnect_still_notifies_once(patched_environment):
    """A broker-initiated drop on a connected client notifies listeners (once)."""
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []
    client.add_connection_callback(states.append)

    client._on_connect(mock_client, None, MagicMock(), 0, None)
    client._on_subscribe(mock_client, None, 1, [0], None)
    client._on_disconnect(mock_client, None, MagicMock(), 7, None)
    client._on_disconnect(mock_client, None, MagicMock(), 7, None)  # paho can repeat it

    assert states == [True, False]


def test_disconnect_does_not_hold_lock_while_joining_network_thread(patched_environment):
    """Regression: loop_stop() joins the network thread; if that thread is inside
    _handle_authentication_failure() waiting for client._lock, holding the lock
    across loop_stop() deadlocks."""
    import threading

    mock_client = patched_environment
    mock_client.is_connected.return_value = False
    device = DummyDevice()
    client = QilowattMQTTClient(
        mqtt_username="u", mqtt_password="p", device=device, max_auth_retries=0
    )
    client.connect()

    rejected = ReasonCode(PacketTypes.CONNACK, "Not authorized")
    joined = {"ok": False}
    entered = {"once": False}

    def loop_stop_joining_network_thread(*args, **kwargs):
        # First call comes from disconnect(); the auth handler calls loop_stop()
        # again from inside the network thread, which must not recurse here.
        if entered["once"]:
            return mqtt.MQTT_ERR_SUCCESS
        entered["once"] = True
        network_thread = threading.Thread(
            target=client._on_connect, args=(mock_client, None, MagicMock(), rejected, None)
        )
        network_thread.start()
        network_thread.join(timeout=2.0)
        joined["ok"] = not network_thread.is_alive()
        return mqtt.MQTT_ERR_SUCCESS

    mock_client.loop_stop.side_effect = loop_stop_joining_network_thread

    client.disconnect()

    assert joined["ok"], "network thread blocked on client._lock while loop_stop() joined it"
    assert client._shutdown is True


def test_status0_is_sent_once_connected_not_before(patched_environment):
    """STATUS0 goes out from the connected path, after SUBACK, so it is never
    attempted (and dropped) before the CONNACK has arrived."""
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    order = []
    client.add_connection_callback(lambda c: order.append(("listener", c)))
    device.on_connected = lambda: order.append(("status0", None))

    client.connect()
    assert order == []                                   # TCP up, no CONNACK yet
    client._on_connect(mock_client, None, MagicMock(), 0, None)
    assert order == []                                   # CONNACK, waiting for SUBACK
    client._on_subscribe(mock_client, None, 1, [0], None)
    assert order == [("status0", None), ("listener", True)]

    client.disconnect()


def test_status0_is_resent_on_every_reconnect(patched_environment):
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)

    client.connect()
    client._on_connect(mock_client, None, MagicMock(), 0, None)
    client._on_subscribe(mock_client, None, 1, [0], None)
    assert device.status0_sent == 1

    client._on_disconnect(mock_client, None, MagicMock(), 7, None)   # broker drop
    client._on_connect(mock_client, None, MagicMock(), 0, None)      # paho reconnected
    client._on_subscribe(mock_client, None, 1, [0], None)
    assert device.status0_sent == 2

    # A duplicate SUBACK on the same connection does not resend
    client._on_subscribe(mock_client, None, 1, [0], None)
    assert device.status0_sent == 2
    client.disconnect()


def test_status0_is_sent_when_subscribe_gives_up(patched_environment):
    """The client marks itself connected even if subscribing fails for good;
    STATUS0 must go out on that path too."""
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    client.connect()
    client._on_connect(mock_client, None, MagicMock(), 0, None)
    for _ in range(client._max_subscribe_retries):
        fire_pending_timers()                             # SUBACK never arrives
    assert client.connected is True
    assert device.status0_sent == 1
    client.disconnect()


def test_failing_on_connected_hook_does_not_block_connection(patched_environment):
    mock_client = patched_environment
    device = DummyDevice()
    device.on_connected = lambda: (_ for _ in ()).throw(RuntimeError("no network info"))
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []
    client.add_connection_callback(states.append)
    client.connect()
    client._on_connect(mock_client, None, MagicMock(), 0, None)
    client._on_subscribe(mock_client, None, 1, [0], None)
    assert client.connected is True and states == [True]
    client.disconnect()


def test_disconnect_during_on_connected_never_leaves_a_stale_connected(patched_environment):
    """Regression: the subscribe-gave-up path marks the client connected on a
    timer thread; a broker disconnect arriving while the hook runs must not
    end with listeners told True on a dead connection."""
    import threading

    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []
    client.add_connection_callback(states.append)

    hook_entered = threading.Event()
    release_hook = threading.Event()

    def slow_hook():
        hook_entered.set()
        assert release_hook.wait(timeout=2.0)

    device.on_connected = slow_hook
    client.connect()
    client._on_connect(mock_client, None, MagicMock(), 0, None)

    # SUBACK handled on another thread; the hook blocks inside _set_connected
    connector = threading.Thread(target=client._on_subscribe, args=(mock_client, None, 1, [0], None))
    connector.start()
    assert hook_entered.wait(timeout=2.0)

    # Broker drops the connection while the hook is still running
    dropper = threading.Thread(target=client._on_disconnect, args=(mock_client, None, MagicMock(), 7, None))
    dropper.start()
    time.sleep(0.05)
    release_hook.set()
    connector.join(timeout=2.0)
    dropper.join(timeout=2.0)
    assert not connector.is_alive() and not dropper.is_alive()

    assert states == [True, False]
    assert client.connected is False
    client.disconnect()


def test_late_suback_after_disconnect_is_ignored(patched_environment):
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []
    client.add_connection_callback(states.append)

    client.connect()
    client._on_connect(mock_client, None, MagicMock(), 0, None)
    client.disconnect()
    client._on_subscribe(mock_client, None, 1, [0], None)   # arrives after shutdown

    assert states == []
    assert client.connected is False
    assert device.status0_sent == 0


def test_listener_may_call_disconnect_from_the_callback(patched_environment):
    """The lock is reentrant: a listener calling back into the client must not deadlock."""
    mock_client = patched_environment
    device = DummyDevice()
    client = QilowattMQTTClient(mqtt_username="u", mqtt_password="p", device=device)
    states = []

    def listener(connected):
        states.append(connected)
        if connected:
            client.disconnect()

    client.add_connection_callback(listener)
    client.connect()
    client._on_connect(mock_client, None, MagicMock(), 0, None)
    client._on_subscribe(mock_client, None, 1, [0], None)

    assert states == [True, False]
    assert client.connected is False and client._shutdown is True
