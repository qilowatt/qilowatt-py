# qilowatt/client.py

import ssl
import json
import threading
import logging
import paho.mqtt.client as mqtt
from typing import Dict, Any, Callable, List, Optional
from .exceptions import ConnectionError, AuthenticationError
from .base_device import BaseDevice

_logger = logging.getLogger(__name__)

class QilowattMQTTClient:
    """Client to handle MQTT communication with Qilowatt server."""

    def __init__(
        self,
        mqtt_username: str,
        mqtt_password: str,
        device: BaseDevice,
        host: str = "mqtt.qilowatt.it",
        port: int = 8883,
        tls: bool = True,
        max_auth_retries: int = 5,
        auth_retry_delay: float = 5.0,
        max_auth_retry_delay: float = 60.0,
    ):
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.device = device

        self.host = host
        self.port = port
        self.tls = tls

        self._max_auth_retries = max(0, max_auth_retries)
        self._auth_retry_delay = max(0.0, auth_retry_delay)
        self._max_auth_retry_delay = max(self._auth_retry_delay, max_auth_retry_delay)

        self._client = mqtt.Client()
        self._connected = False
        self._lock = threading.Lock()
        self._connection_callbacks: List[Callable[[bool], None]] = []
        self._auth_failures = 0
        self._retry_timer: Optional[threading.Timer] = None
        self._last_error: Optional[Exception] = None
        self._shutdown = False
        
        # Enable automatic reconnection
        self._client.reconnect_delay_set(min_delay=10, max_delay=60)

        self._setup_client()

        # Set up device callback
        def publish_callback(topic: str, data: Dict[str, Any]):
            if self._client.is_connected():
                payload = json.dumps(data)
                result = self._client.publish(topic, payload)
                if result.rc == mqtt.MQTT_ERR_SUCCESS:
                    _logger.debug(f"Published data to {topic}")
                else:
                    _logger.warning(f"Failed to publish to {topic}: {result.rc}")
            else:
                _logger.warning(f"Cannot publish to {topic}: not connected")
                # Update our internal state if Paho detected disconnection
                if self._connected:
                    self._connected = False
                    self._notify_connection_change(False)
        
        self.device.set_publish_callback(publish_callback)

    @property
    def connected(self) -> bool:
        """Get the current connection state using Paho's built-in method."""
        is_connected = self._client.is_connected()
        # Sync our internal state with Paho's state
        if self._connected != is_connected:
            self._connected = is_connected
            self._notify_connection_change(is_connected)
        return is_connected

    def add_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Add a callback to be called when connection state changes.
        
        Args:
            callback: A function that takes a boolean parameter (connected state)
        """
        self._connection_callbacks.append(callback)

    def remove_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Remove a connection state callback."""
        if callback in self._connection_callbacks:
            self._connection_callbacks.remove(callback)

    def _notify_connection_change(self, connected: bool):
        """Notify all registered callbacks of connection state change."""
        for callback in self._connection_callbacks:
            try:
                callback(connected)
            except Exception as e:
                _logger.error(f"Error in connection callback: {e}")

    def _setup_client(self):
        if self.tls:
            self._client.tls_set(cert_reqs=ssl.CERT_NONE)
            self._client.tls_insecure_set(True)

        self._client.username_pw_set(self.mqtt_username, self.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        
        # Set keep-alive to detect connection issues faster
        self._client.keepalive = 30

    def _on_connect(self, client, userdata, flags, rc):
        _logger.debug(f"Connected with result code {rc}")
        if rc == 0:
            self._cancel_retry_timer()
            self._connected = True
            self._auth_failures = 0
            self._last_error = None
            # Subscribe to command topic
            client.subscribe(self.device.command_topic)
            self._notify_connection_change(True)
        elif rc == 5:
            self._handle_authentication_failure()
        else:
            error = ConnectionError(f"Connection failed with result code {rc}")
            self._last_error = error
            self._connected = False
            self._notify_connection_change(False)
            _logger.error(str(error))

    def _on_disconnect(self, client, userdata, rc):
        _logger.debug(f"Disconnected with result code {rc}")
        self._connected = False
        self._notify_connection_change(False)

    def _on_message(self, client, userdata, msg):
        _logger.debug(f"Message received on {msg.topic}: {msg.payload}")
        if msg.topic == self.device.command_topic:
            self.device.handle_command(msg.payload)

    def connect(self):
        """Connect to the MQTT broker and start the loop."""
        with self._lock:
            if not self._client.is_connected():
                self._shutdown = False
                self._auth_failures = 0
                self._last_error = None
                self._client.connect(self.host, self.port, keepalive=30)
                self._client.loop_start()

    def disconnect(self):
        """Disconnect from the MQTT broker and stop the loop."""
        with self._lock:
            self._shutdown = True
            self._cancel_retry_timer()
            if self._connected or self._client.is_connected():
                self._client.loop_stop()
                self._client.disconnect()
                self._connected = False
                self._notify_connection_change(False)
        self.device.stop_timers()

    def last_error(self) -> Optional[Exception]:
        """Return the most recent connection error, if any."""
        return self._last_error

    def _handle_authentication_failure(self):
        self._connected = False
        self._notify_connection_change(False)
        self._auth_failures += 1

        if self._auth_failures > self._max_auth_retries:
            message = (
                "Authentication failed after "
                f"{self._max_auth_retries} retries"
                if self._max_auth_retries
                else "Authentication failed"
            )
            error = AuthenticationError(message)
            self._last_error = error
            _logger.error(message)
            with self._lock:
                self._shutdown = True
                self._cancel_retry_timer()
                try:
                    self._client.loop_stop()
                except Exception:
                    pass
                try:
                    self._client.disconnect()
                except Exception:
                    pass
            self.device.stop_timers()
            return

        delay = self._calculate_retry_delay(self._auth_failures)
        _logger.warning(
            "Authentication failed (attempt %s/%s). Retrying in %.1f seconds",
            self._auth_failures,
            self._max_auth_retries if self._max_auth_retries else "∞",
            delay,
        )
        self._schedule_retry(delay)

    def _calculate_retry_delay(self, attempt: int) -> float:
        if attempt <= 0:
            return self._auth_retry_delay
        base_delay = self._auth_retry_delay or 0.0
        delay = base_delay * (2 ** (attempt - 1)) if base_delay else 0.0
        return min(delay if delay else base_delay, self._max_auth_retry_delay)

    def _schedule_retry(self, delay: float):
        with self._lock:
            if self._shutdown:
                return
            self._cancel_retry_timer()
            self._retry_timer = threading.Timer(delay, self._attempt_reauth)
            self._retry_timer.daemon = True
            self._retry_timer.start()

    def _cancel_retry_timer(self):
        if self._retry_timer:
            try:
                self._retry_timer.cancel()
            except Exception:
                pass
            finally:
                self._retry_timer = None

    def _attempt_reauth(self):
        with self._lock:
            self._retry_timer = None
            if self._shutdown or self._client.is_connected():
                return

            try:
                self._client.reconnect()
            except Exception as exc:
                _logger.error(f"Reconnect attempt failed: {exc}")
                if self._auth_failures > self._max_auth_retries:
                    self._last_error = AuthenticationError("Authentication failed")
                    self._shutdown = True
                    self._cancel_retry_timer()
                    self.device.stop_timers()
