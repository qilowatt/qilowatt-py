# qilowatt/client.py

import ssl
import json
import threading
import logging
import paho.mqtt.client as mqtt
from typing import Callable, Optional
from .models import SensorData, WorkModeCommand, EnergyData, MetricsData, Status0Data, StatusData, StatusPRMData, StatusFWRData, StatusLOGData, StatusNETData, StatusMQTData, StatusTIMData
from .exceptions import ConnectionError, AuthenticationError
from datetime import datetime, timezone
import platform
import socket
import getmac

_logger = logging.getLogger(__name__)

class QilowattMQTTClient:
    """Client to handle MQTT communication with Qilowatt server."""

    def __init__(
        self,
        mqtt_username: str,
        mqtt_password: str,
        inverter_id: str,
        host: str = "test-mqtt.qilowatt.it",
        port: int = 8883,
        tls: bool = True,
    ):
        self.mqtt_username = mqtt_username
        self.mqtt_password = mqtt_password
        self.inverter_id = inverter_id

        self.host = host
        self.port = port
        self.tls = tls

        self._client = mqtt.Client()
        self._connected = False
        self._lock = threading.Lock()

        self._sensor_topic = f"Q/{self.inverter_id}/SENSOR"
        self._state_topic = f"Q/{self.inverter_id}/STATE"
        self._status0_topic = f"Q/{self.inverter_id}/STATUS0"
        self._command_topic = f"Q/{self.inverter_id}/cmnd/backlog"

        self._workmode_command: Optional[WorkModeCommand] = None  # Store the latest command
        self._workmode_command = WorkModeCommand.from_dict({"Mode": "normal"})  # Default to normal mode

        self._on_command_callback: Optional[Callable[[WorkModeCommand], None]] = None

        # Data storage
        self._energy_data: Optional[EnergyData] = None
        self._metrics_data: Optional[MetricsData] = None

        # Timers
        self._sensor_timer_thread: Optional[threading.Thread] = None
        self._state_timer_thread: Optional[threading.Thread] = None
        self._status0_timer_thread: Optional[threading.Thread] = None

        self._sensor_timer_stop_event = threading.Event()
        self._state_timer_stop_event = threading.Event()
        self._status0_timer_stop_event = threading.Event()

        # Flags to indicate whether data has been initialized
        self._data_initialized = False

        # Initialize startup time and boot count
        self._startup_utc = datetime.utcnow()
        self._boot_count = 1  # Increment as appropriate

        self._setup_client()

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
            client.subscribe(self._command_topic)
        elif rc == 5:
            raise AuthenticationError("Authentication failed")
        else:
            raise ConnectionError(f"Connection failed with result code {rc}")

    def _on_disconnect(self, client, userdata, rc):
        _logger.debug(f"Disconnected with result code {rc}")
        self._connected = False

    def _on_message(self, client, userdata, msg):
        _logger.debug(f"Message received on {msg.topic}: {msg.payload}")
        if msg.topic == self._command_topic:
            self._handle_command_message(msg.payload)

    def _handle_command_message(self, payload):
        try:
            message = payload.decode('utf-8')
            if message.startswith("WORKMODE"):
                json_part = message[len("WORKMODE "):]
                data = json.loads(json_part)
                command = WorkModeCommand.from_dict(data)
                self._workmode_command = command  # Store internally
                if self._on_command_callback:
                    self._on_command_callback(command)
        except Exception as e:
            _logger.error(f"Error processing command message: {e}")

    def set_command_callback(self, callback: Callable[[WorkModeCommand], None]):
        """Set the callback function to be called when a command is received."""
        self._on_command_callback = callback

    def set_energy_data(self, energy_data: EnergyData):
        """Set the ENERGY data."""
        self._energy_data = energy_data
        self._check_data_initialized()

    def set_metrics_data(self, metrics_data: MetricsData):
        """Set the METRICS data."""
        self._metrics_data = metrics_data
        self._check_data_initialized()

    def _check_data_initialized(self):
        if self._energy_data and self._metrics_data and not self._data_initialized:
            self._data_initialized = True
            self._start_timers()

    def _start_timers(self):
        # Start the timers for sending data
        self._start_sensor_timer()
        self._start_state_timer()
        self._start_status0_timer()

    def _start_sensor_timer(self):
        def sensor_timer():
            while not self._sensor_timer_stop_event.wait(10):
                self.publish_sensor_data()

        self._sensor_timer_thread = threading.Thread(target=sensor_timer, name="SensorTimer")
        self._sensor_timer_thread.daemon = True
        self._sensor_timer_thread.start()
        _logger.debug("Started sensor data timer.")

    def _start_state_timer(self):
        def state_timer():
            while not self._state_timer_stop_event.wait(60):
                self.publish_state_data()

        self._state_timer_thread = threading.Thread(target=state_timer, name="StateTimer")
        self._state_timer_thread.daemon = True
        self._state_timer_thread.start()
        _logger.debug("Started state data timer.")

    def _start_status0_timer(self):
        def status0_timer():
            # Send at startup
            self.publish_status0_data()
            # Then every 60 minutes
            while not self._status0_timer_stop_event.wait(3600):
                self.publish_status0_data()

        self._status0_timer_thread = threading.Thread(target=status0_timer, name="Status0Timer")
        self._status0_timer_thread.daemon = True
        self._status0_timer_thread.start()
        _logger.debug("Started STATUS0 data timer.")

    def publish_sensor_data(self):
        """Publish sensor data to the MQTT broker."""
        if self._connected and self._data_initialized:
            # Build the sensor data
            sensor_data = SensorData(
                ENERGY=self._energy_data,
                METRICS=self._metrics_data,
                WORKMODE=self._workmode_command  # Use the latest WORKMODE command
            )
            # Update the time
            sensor_data.Time = datetime.utcnow().isoformat()
            payload = json.dumps(sensor_data.to_dict())
            self._client.publish(self._sensor_topic, payload)
            _logger.debug(f"Published sensor data to {self._sensor_topic}")
        else:
            _logger.warning("Cannot publish sensor data: not connected or data not initialized.")

    def publish_state_data(self):
        """Publish state data to the MQTT broker."""
        if self._connected:
            state_data = self.generate_state_data()
            payload = json.dumps(state_data)
            self._client.publish(self._state_topic, payload)
            _logger.debug(f"Published state data to {self._state_topic}")
        else:
            _logger.warning("Cannot publish state data: not connected.")

    def generate_state_data(self):
        """Generate the STATE data."""
        state_data = {
            "Time": datetime.utcnow().isoformat(),
            # Add other state fields as necessary
            # For example:
            "Uptime": int((datetime.utcnow() - self._startup_utc).total_seconds()),
        }
        return state_data

    def publish_status0_data(self):
        """Publish STATUS0 data to the MQTT broker."""
        if self._connected:
            status0_data = self.generate_status0_data()
            payload = json.dumps(status0_data.to_dict())
            self._client.publish(self._status0_topic, payload)
            _logger.debug(f"Published STATUS0 data to {self._status0_topic}")
        else:
            _logger.warning("Cannot publish STATUS0 data: not connected.")

    def generate_status0_data(self) -> Status0Data:
        """Generate the STATUS0 data."""
        # Automatically determine some values
        device_name = "Qilowatt Device"
        friendly_name = ["Home Assistant", "", ""]
        topic = self.inverter_id  # Assuming inverter_id is unique

        startup_utc = self._startup_utc.replace(tzinfo=timezone.utc).isoformat()
        boot_count = self._boot_count

        version = "QW-MQTT-API-24.10.01"  # Example version
        hardware = platform.machine()

        tele_period = 10  # Example telemetry period

        hostname = socket.gethostname()
        ip_address = self._get_ip_address()
        gateway = self._get_default_gateway()
        subnet_mask = self._get_subnet_mask()
        mac_address = getmac.get_mac_address()

        mqtt_host = self.host
        mqtt_port = self.port
        mqtt_client = self._client._client_id.decode('utf-8') if self._client._client_id else ''
        mqtt_user = self.mqtt_username

        utc_now = datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()
        local_now = datetime.now().isoformat()

        status = StatusData(
            DeviceName=device_name,
            FriendlyName=friendly_name,
            Topic=topic,
        )

        status_prm = StatusPRMData(
            StartupUTC=startup_utc,
            BootCount=boot_count,
        )

        status_fwr = StatusFWRData(
            Version=version,
            Hardware=hardware,
        )

        status_log = StatusLOGData(
            TelePeriod=tele_period,
        )

        status_net = StatusNETData(
            Hostname=hostname,
            IPAddress=ip_address,
            Gateway=gateway,
            Subnetmask=subnet_mask,
            Mac=mac_address,
        )

        mqtt_client_mask = "QWAPI_%06X"  # Example mask

        status_mqt = StatusMQTData(
            MqttHost=mqtt_host,
            MqttPort=mqtt_port,
            MqttClient=mqtt_client,
            MqttUser=mqtt_user,
            MqttClientMask=mqtt_client_mask,
        )

        status_tim = StatusTIMData(
            UTC=utc_now,
            Local=local_now,
        )

        status0_data = Status0Data(
            Status=status,
            StatusPRM=status_prm,
            StatusFWR=status_fwr,
            StatusLOG=status_log,
            StatusNET=status_net,
            StatusMQT=status_mqt,
            StatusTIM=status_tim,
        )

        return status0_data

    # Helper methods to retrieve network information
    def _get_ip_address(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            # This IP is not used; it's just to get the appropriate interface
            s.connect(("8.8.8.8", 80))
            ip_address = s.getsockname()[0]
            s.close()
            return ip_address
        except Exception as e:
            _logger.error(f"Error getting IP address: {e}")
            return "0.0.0.0"

    def _get_default_gateway(self):
        # Implement logic to retrieve default gateway
        # Placeholder implementation
        return "192.168.1.1"

    def _get_subnet_mask(self):
        # Implement logic to retrieve subnet mask
        # Placeholder implementation
        return "255.255.255.0"

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
        self._stop_timers()

    def _stop_timers(self):
        """Stop all running timers."""
        if self._sensor_timer_thread:
            self._sensor_timer_stop_event.set()
            self._sensor_timer_thread.join()
            _logger.debug("Stopped sensor data timer.")

        if self._state_timer_thread:
            self._state_timer_stop_event.set()
            self._state_timer_thread.join()
            _logger.debug("Stopped state data timer.")

        if self._status0_timer_thread:
            self._status0_timer_stop_event.set()
            self._status0_timer_thread.join()
            _logger.debug("Stopped STATUS0 data timer.")

        # Reset events and threads
        self._sensor_timer_stop_event.clear()
        self._state_timer_stop_event.clear()
        self._status0_timer_stop_event.clear()
        self._sensor_timer_thread = None
        self._state_timer_thread = None
        self._status0_timer_thread = None

    def get_workmode_command(self) -> Optional[WorkModeCommand]:
        """Get the latest WORKMODE command received."""
        return self._workmode_command
