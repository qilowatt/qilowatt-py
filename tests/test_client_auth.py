import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.client import QilowattMQTTClient
from qilowatt.exceptions import AuthenticationError, ConnectionError
from qilowatt.base_device import BaseDevice


SUCCESS = ReasonCode(PacketTypes.CONNACK, "Success")                      # 0
BAD_CREDENTIALS = ReasonCode(PacketTypes.CONNACK, "Bad user name or password")  # 134
NOT_AUTHORIZED = ReasonCode(PacketTypes.CONNACK, "Not authorized")        # 135
SERVER_UNAVAILABLE = ReasonCode(PacketTypes.CONNACK, "Server unavailable")  # 136


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


@pytest.fixture
def mock_client():
    client = MagicMock()
    client.is_connected.return_value = False
    client.subscribe.return_value = (0, 1)
    with patch("qilowatt.client.mqtt.Client", return_value=client):
        yield client


def make_client(device, **kwargs):
    return QilowattMQTTClient(
        mqtt_username="user", mqtt_password="pass", device=device, **kwargs
    )


def connack(client, mock_client, reason_code):
    """Deliver a CONNACK the way paho 2.x (CallbackAPIVersion.VERSION2) does."""
    client._on_connect(mock_client, None, MagicMock(), reason_code, None)


@pytest.mark.parametrize("reason_code", [BAD_CREDENTIALS, NOT_AUTHORIZED, 4, 5])
def test_rejected_credentials_are_counted_but_not_fatal_before_limit(mock_client, reason_code):
    """paho reconnects on its own after a refused CONNACK; the client must only count."""
    device = DummyDevice()
    client = make_client(device, max_auth_retries=2)
    states = []
    client.add_connection_callback(states.append)
    errors = []
    client.add_error_callback(errors.append)

    client.connect()
    connack(client, mock_client, reason_code)

    assert states == [False]
    assert client.last_error() is None
    assert errors == []
    assert client._shutdown is False
    assert device.stop_called is False
    mock_client.loop_stop.assert_not_called()


def test_rejected_credentials_stop_client_after_max_attempts(mock_client):
    device = DummyDevice()
    client = make_client(device, max_auth_retries=1)
    errors = []
    client.add_error_callback(errors.append)

    client.connect()
    connack(client, mock_client, BAD_CREDENTIALS)   # attempt 1: tolerated
    assert client.last_error() is None

    connack(client, mock_client, BAD_CREDENTIALS)   # attempt 2: over the limit
    assert isinstance(client.last_error(), AuthenticationError)
    assert client._shutdown is True
    assert device.stop_called is True
    mock_client.loop_stop.assert_called_once()
    mock_client.disconnect.assert_called_once()
    assert len(errors) == 1 and isinstance(errors[0], AuthenticationError)


def test_zero_retries_makes_first_rejection_fatal(mock_client):
    device = DummyDevice()
    client = make_client(device, max_auth_retries=0)

    client.connect()
    connack(client, mock_client, NOT_AUTHORIZED)

    assert isinstance(client.last_error(), AuthenticationError)
    assert device.stop_called is True


def test_successful_connect_resets_auth_failure_count(mock_client):
    device = DummyDevice()
    client = make_client(device, max_auth_retries=1)

    client.connect()
    connack(client, mock_client, BAD_CREDENTIALS)
    assert client._auth_failures == 1

    mock_client.is_connected.return_value = True
    connack(client, mock_client, SUCCESS)
    assert client._auth_failures == 0
    assert client.last_error() is None
    mock_client.subscribe.assert_called_once_with(device.command_topic)

    # A later rejection starts counting from zero again
    connack(client, mock_client, BAD_CREDENTIALS)
    assert client.last_error() is None


def test_non_auth_failure_is_reported_as_connection_error(mock_client):
    device = DummyDevice()
    client = make_client(device, max_auth_retries=0)

    client.connect()
    connack(client, mock_client, SERVER_UNAVAILABLE)

    assert isinstance(client.last_error(), ConnectionError)
    assert client._auth_failures == 0
    assert client._shutdown is False
    assert device.stop_called is False


def test_reason_code_helpers_accept_reasoncode_and_int():
    from qilowatt.client import _is_auth_failure, _is_success

    assert _is_success(SUCCESS) and _is_success(0)
    assert not _is_success(BAD_CREDENTIALS)
    assert _is_auth_failure(BAD_CREDENTIALS) and _is_auth_failure(NOT_AUTHORIZED)
    assert _is_auth_failure(4) and _is_auth_failure(5)
    assert not _is_auth_failure(SERVER_UNAVAILABLE) and not _is_auth_failure(0)
    assert not _is_auth_failure(None) and not _is_success(None)
