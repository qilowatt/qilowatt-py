# qilowatt/client.py

import ssl
import json
import threading
import logging
import paho.mqtt.client as mqtt
from typing import Dict, Any
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

        self._setup_client()

        # Set up device callback
        def publish_callback(topic: str, data: Dict[str, Any]):
            if self._connected:
                payload = json.dumps(data)
                self._client.publish(topic, payload)
                _logger.debug(f"Published data to {topic}")
            else:
                _logger.warning(f"Cannot publish to {topic}: not connected")
        
        self.device.set_publish_callback(publish_callback)

    def _setup_client(self):
        if self.tls:
            self._client.tls_set(cert_reqs=ssl.CERT_NONE)
            self._client.tls_insecure_set(True)

        self._client.username_pw_set(self.mqtt_username, self.mqtt_password)
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, rc):
        _logger.debug(f"Connected with result code {rc}")
        if rc == 0:
            self._connected = True
            # Subscribe to command topic
            client.subscribe(self.device.command_topic)
        elif rc == 5:
            raise AuthenticationError("Authentication failed")
        else:
            raise ConnectionError(f"Connection failed with result code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        _logger.debug(f"Disconnected with result code {rc}")
        self._connected = False

    def _on_message(self, client, userdata, msg):
        _logger.debug(f"Message received on {msg.topic}: {msg.payload}")
        if msg.topic == self.device.command_topic:
            self.device.handle_command(msg.payload)

    def connect(self):
        """Connect to the MQTT broker and start the loop."""
        with self._lock:
            if not self._connected:
                self._client.connect(self.host, self.port)
                self._client.loop_start()

    def disconnect(self):
        """Disconnect from the MQTT broker and stop the loop."""
        with self._lock:
            if self._connected:
                self._client.loop_stop()
                self._client.disconnect()
                self._connected = False
        self.device._stop_timers()
