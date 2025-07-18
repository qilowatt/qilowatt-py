# qilowatt/client.py

import ssl
import json
import threading
import logging
import paho.mqtt.client as mqtt
from typing import Dict, Any, Callable, List
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
    ):
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.device = device

        self.host = host
        self.port = port
        self.tls = tls

        self._client = mqtt.Client()
        self._connected = False
        self._lock = threading.Lock()
        self._connection_callbacks: List[Callable[[bool], None]] = []
        
        # Enable automatic reconnection
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

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
            self._connected = True
            # Subscribe to command topic
            client.subscribe(self.device.command_topic)
            self._notify_connection_change(True)
        elif rc == 5:
            raise AuthenticationError("Authentication failed")
        else:
            raise ConnectionError(f"Connection failed with result code {rc}")

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
                self._client.connect(self.host, self.port, keepalive=30)
                self._client.loop_start()

    def disconnect(self):
        """Disconnect from the MQTT broker and stop the loop."""
        with self._lock:
            if self._connected:
                self._client.loop_stop()
                self._client.disconnect()
                self._connected = False
                self._notify_connection_change(False)
        self.device._stop_timers()
