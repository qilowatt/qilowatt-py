from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Callable
import threading
import logging
from datetime import datetime, timezone
from .models import (
    Status0Data,
    StatusData, StatusPRMData, StatusFWRData, StatusLOGData,
    StatusNETData, StatusMQTData, StatusTIMData, VersionData
)
import platform
import socket
import getmac

_logger = logging.getLogger(__name__)

SENSOR_INTERVAL = 10  # seconds
STATE_INTERVAL = 60  # seconds
STATUS0_INTERVAL = 3600  # seconds
TIMER_JOIN_TIMEOUT = 5.0  # seconds


def _safe_local_ip() -> str:
    """Best-effort lookup of the local IP address.

    ``socket.gethostbyname(socket.gethostname())`` raises on hosts whose
    hostname does not resolve (common in containers), so fall back to a
    UDP-socket trick and finally to loopback.
    """
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # No packets are sent; this only selects the outgoing interface
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class BaseDevice(ABC):
    """Base class for all devices that can communicate via MQTT."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        self._data_initialized = False
        self._publish_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._sensor_timer_thread: Optional[threading.Thread] = None
        self._state_timer_thread: Optional[threading.Thread] = None
        self._status0_timer_thread: Optional[threading.Thread] = None

        self._sensor_timer_stop_event = threading.Event()
        self._state_timer_stop_event = threading.Event()
        self._status0_timer_stop_event = threading.Event()

        self._startup_utc = datetime.now(timezone.utc)
        self._boot_count = 1

        # Default version data - can be overridden by client
        self._version_data = VersionData()

        # Broker details reported in STATUS0; the client overrides these
        # with the real connection parameters via set_mqtt_info()
        self._mqtt_host = "mqtt.qilowatt.it"
        self._mqtt_port = 8883
        self._mqtt_user = ""

    @property
    def sensor_topic(self) -> str:
        """Get the sensor topic for this device."""
        return f"Q/{self.device_id}/SENSOR"

    @property
    def state_topic(self) -> str:
        """Get the state topic for this device."""
        return f"Q/{self.device_id}/STATE"

    @property
    def power_topic(self) -> str:
        """Send POWER1 state."""
        return f"Q/{self.device_id}/POWER1"

    @property
    def status0_topic(self) -> str:
        """Get the status topic for this device."""
        return f"Q/{self.device_id}/STATUS0"

    @property
    def command_topic(self) -> str:
        """Get the command topic for this device."""
        return f"Q/{self.device_id}/cmnd/backlog"

    @abstractmethod
    def handle_command(self, payload: bytes) -> None:
        """Handle incoming command messages."""
        pass

    @abstractmethod
    def get_sensor_data(self) -> Dict[str, Any]:
        """Get current sensor data."""
        pass

    @abstractmethod
    def get_state_data(self) -> Dict[str, Any]:
        """Get current state data."""
        pass

    def get_version_data(self) -> Dict[str, Any]:
        """Get version data for the device."""
        return self._version_data.to_dict()

    def set_version_data(self, version_data: Dict[str, Any]):
        """Set version data for the device."""
        # Update individual fields from the dictionary
        if "API" in version_data:
            self._version_data.API = version_data["API"]
        if "HA" in version_data:
            self._version_data.HA = version_data["HA"]
        if "qilowatt-ha" in version_data:
            self._version_data.qilowatt_ha = version_data["qilowatt-ha"]
        if "qilowatt-py" in version_data:
            self._version_data.qilowatt_py = version_data["qilowatt-py"]

    def set_mqtt_info(self, host: str, port: int, username: str = ""):
        """Set the broker details reported in STATUS0."""
        self._mqtt_host = host
        self._mqtt_port = port
        self._mqtt_user = username

    def start_timers(self):
        """Start all data publishing timers."""
        self._start_sensor_timer()
        self._start_state_timer()
        self._start_status0_timer()

    def stop_timers(self):
        """Stop all data publishing timers."""
        for event in [self._sensor_timer_stop_event,
                     self._state_timer_stop_event,
                     self._status0_timer_stop_event]:
            event.set()

        current = threading.current_thread()
        for thread in [self._sensor_timer_thread,
                      self._state_timer_thread,
                      self._status0_timer_thread]:
            if thread and thread is not current:
                thread.join(timeout=TIMER_JOIN_TIMEOUT)
                if thread.is_alive():
                    _logger.warning("Timer thread %s did not stop in time", thread.name)

        # Reset events and threads
        self._sensor_timer_stop_event.clear()
        self._state_timer_stop_event.clear()
        self._status0_timer_stop_event.clear()
        self._sensor_timer_thread = None
        self._state_timer_thread = None
        self._status0_timer_thread = None

    def _publish(self, topic: str, data: Dict[str, Any]):
        """Publish via the client callback, if one is attached."""
        if self._publish_callback is not None:
            self._publish_callback(topic, data)

    def publish_sensor_data(self):
        sensor_data = self.get_sensor_data()
        # Ensure VERSION is always present even if device doesn't provide it
        if not isinstance(sensor_data, dict):
            sensor_data = {}
        if "VERSION" not in sensor_data:
            sensor_data["VERSION"] = self.get_version_data()
        self._publish(self.sensor_topic, sensor_data)

    def _run_timer_loop(self, stop_event: threading.Event, interval: float,
                       publish: Callable[[], None], initial_publish: bool = False):
        """Run a publish loop that survives publish errors.

        An uncaught exception would otherwise kill the timer thread
        permanently and silently stop all future publishes.
        """
        if initial_publish and not stop_event.is_set():
            try:
                publish()
            except Exception:
                _logger.exception("Error publishing initial data")
        while not stop_event.wait(interval):
            try:
                publish()
            except Exception:
                _logger.exception("Error publishing periodic data")

    def _start_sensor_timer(self):
        """Start timer for sending sensor data."""
        self._sensor_timer_thread = threading.Thread(
            target=self._run_timer_loop,
            args=(self._sensor_timer_stop_event, SENSOR_INTERVAL, self.publish_sensor_data),
            name=f"{self.__class__.__name__}SensorTimer"
        )
        self._sensor_timer_thread.daemon = True
        self._sensor_timer_thread.start()

    def publish_state_data(self):
        state_data = self.get_state_data()
        self._publish(self.state_topic, state_data)

    def _start_state_timer(self):
        """Start timer for sending state data."""
        self._state_timer_thread = threading.Thread(
            target=self._run_timer_loop,
            args=(self._state_timer_stop_event, STATE_INTERVAL, self.publish_state_data),
            name=f"{self.__class__.__name__}StateTimer"
        )
        self._state_timer_thread.daemon = True
        self._state_timer_thread.start()

    def publish_status0_data(self):
        self._publish(self.status0_topic, self.get_status0_data().to_dict())

    def _start_status0_timer(self):
        """Start timer for sending status data (at startup, then hourly)."""
        self._status0_timer_thread = threading.Thread(
            target=self._run_timer_loop,
            args=(self._status0_timer_stop_event, STATUS0_INTERVAL, self.publish_status0_data),
            kwargs={"initial_publish": True},
            name=f"{self.__class__.__name__}Status0Timer"
        )
        self._status0_timer_thread.daemon = True
        self._status0_timer_thread.start()

    def get_status0_data(self) -> Status0Data:
        """Get current status data."""
        status = StatusData(
            DeviceName="Qilowatt Inverter",
            FriendlyName=["Home Assistant", "", ""],
            Topic=self.device_id
        )

        status_prm = StatusPRMData(
            StartupUTC=self._startup_utc.isoformat(),
            BootCount=self._boot_count
        )

        try:
            hostname = socket.gethostname()
        except OSError:
            hostname = "unknown"
        mac = getmac.get_mac_address() or ""

        return Status0Data(
            Status=status,
            StatusPRM=status_prm,
            StatusFWR=StatusFWRData(Version="1.0.0", Hardware=platform.machine()),
            StatusLOG=StatusLOGData(TelePeriod=SENSOR_INTERVAL),
            StatusNET=StatusNETData(
                Hostname=hostname,
                IPAddress=_safe_local_ip(),
                Gateway="192.168.1.1",
                Subnetmask="255.255.255.0",
                Mac=mac
            ),
            StatusMQT=StatusMQTData(
                MqttHost=self._mqtt_host,
                MqttPort=self._mqtt_port,
                MqttClient="",
                MqttUser=self._mqtt_user,
                MqttClientMask="QWAPI_%06X"
            ),
            StatusTIM=StatusTIMData(
                UTC=datetime.now(timezone.utc).isoformat(),
                Local=datetime.now().isoformat()
            )
        )

    def set_publish_callback(self, callback: Callable[[str, Dict[str, Any]], None]):
        """Set callback for publishing data."""
        self._publish_callback = callback
