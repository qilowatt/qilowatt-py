from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Callable
import math
import threading
import logging
import time
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

SENSOR_INTERVAL = 10.0   # seconds; the Qilowatt backend expects SENSOR exactly this far apart
STATE_INTERVAL = 60.0
STATUS0_INTERVAL = 3600.0

# After a publish, the next grid slot must be at least this fraction of an
# interval after the publish *finished*. Normal publishes take milliseconds, so
# this never triggers in steady state and the schedule stays exactly on the
# grid; it only skips slots after a disruption (a stalled publish, a suspended
# host), so two messages are never sent close together.
MIN_SPACING_FRACTION = 0.9


def _check_interval(name: str, value: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{name} must be a number of seconds, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number of seconds, got {value!r}")
    return float(value)


class BaseDevice(ABC):
    """Base class for all devices that can communicate via MQTT."""
    
    def __init__(
        self,
        device_id: str,
        sensor_interval: float = SENSOR_INTERVAL,
        state_interval: float = STATE_INTERVAL,
        status0_interval: float = STATUS0_INTERVAL,
    ):
        self.device_id = device_id
        self._sensor_interval = _check_interval("sensor_interval", sensor_interval)
        self._state_interval = _check_interval("state_interval", state_interval)
        self._status0_interval = _check_interval("status0_interval", status0_interval)
        self._data_initialized = False
        self._sensor_timer_thread: Optional[threading.Thread] = None
        self._state_timer_thread: Optional[threading.Thread] = None
        self._status0_timer_thread: Optional[threading.Thread] = None
        
        self._sensor_timer_stop_event = threading.Event()
        self._state_timer_stop_event = threading.Event()
        self._status0_timer_stop_event = threading.Event()

        # Set by on_connected(); the STATUS0 timer thread wakes on it and sends
        self._status0_request = threading.Event()
        
        self._startup_utc = datetime.utcnow()
        self._boot_count = 1
        
        # Default version data - can be overridden by client
        self._version_data = VersionData()
        
        
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
        self._status0_request.set()   # wake the STATUS0 loop so it sees the stop
            
        for thread in [self._sensor_timer_thread, 
                      self._state_timer_thread, 
                      self._status0_timer_thread]:
            if thread:
                thread.join()
                
        # Reset events and threads
        self._sensor_timer_stop_event.clear()
        self._state_timer_stop_event.clear()
        self._status0_timer_stop_event.clear()
        self._status0_request.clear()
        self._sensor_timer_thread = None
        self._state_timer_thread = None
        self._status0_timer_thread = None

    def publish_sensor_data(self):
        sensor_data = self.get_sensor_data()
        # Ensure VERSION is always present even if device doesn't provide it
        if not isinstance(sensor_data, dict):
            sensor_data = {}
        if not sensor_data:
            # Nothing to send yet (e.g. a data provider has not produced data)
            return
        if "VERSION" not in sensor_data:
            sensor_data["VERSION"] = self.get_version_data()
        # Callback will be set by client
        if hasattr(self, '_publish_callback'):
            self._publish_callback(self.sensor_topic, sensor_data)

    def _run_on_grid(self, stop_event: threading.Event, interval: float,
                     action: Callable[[], None], run_immediately: bool = False):
        """Run action every `interval` seconds on a fixed grid until stop_event is set.

        The schedule is anchored to a monotonic clock, so the time spent inside
        action (reading data, serialising, publishing) does not accumulate into
        drift: the n-th run is due at start + n * interval. After each run the
        next slot is chosen so that it lies at least MIN_SPACING_FRACTION of an
        interval after the run *finished*: a stalled publish or a late wake-up
        (suspended host) therefore skips the missed slots instead of being
        followed by another message a few seconds later. An exception in action
        is logged and does not stop the timer.
        """
        def _safe_action():
            try:
                action()
            except Exception:
                _logger.exception("Periodic publish failed; will retry on the next slot")

        min_spacing = interval * MIN_SPACING_FRACTION
        if run_immediately:
            _safe_action()
        next_at = time.monotonic() + interval
        while True:
            if stop_event.wait(max(0.0, next_at - time.monotonic())):
                return
            _safe_action()
            finished = time.monotonic()
            next_at += interval
            while next_at < finished + min_spacing:
                next_at += interval

    def _start_sensor_timer(self):
        """Start timer for sending sensor data on a fixed grid."""
        self._sensor_timer_thread = threading.Thread(
            target=self._run_on_grid,
            args=(self._sensor_timer_stop_event, self._sensor_interval, self.publish_sensor_data),
            name=f"{self.__class__.__name__}SensorTimer",
        )
        self._sensor_timer_thread.daemon = True
        self._sensor_timer_thread.start()

    def publish_state_data(self):
        state_data = self.get_state_data()
        if hasattr(self, '_publish_callback'):
            self._publish_callback(self.state_topic, state_data)

    def _start_state_timer(self):
        """Start timer for sending state data."""
        self._state_timer_thread = threading.Thread(
            target=self._run_on_grid,
            args=(self._state_timer_stop_event, self._state_interval, self.publish_state_data),
            name=f"{self.__class__.__name__}StateTimer",
        )
        self._state_timer_thread.daemon = True
        self._state_timer_thread.start()

    def publish_status0_data(self):
        status0_data = self.get_status0_data()
        if hasattr(self, '_publish_callback'):
            self._publish_callback(self.status0_topic, status0_data.to_dict())

    def on_connected(self):
        """Called by the client once it is connected and subscribed.

        Requests a STATUS0 send so the backend gets the device information
        (StartupUTC, BootCount, network) at every connect and reconnect. The
        send itself happens on the STATUS0 timer thread, never on the caller's
        (paho's network thread or a timer thread), because collecting host
        information can block on DNS or on getmac. Returns immediately. If the
        timers are not running yet, the request is served as soon as they
        start; if they have been stopped, it is dropped with them.
        """
        self._status0_request.set()

    def _status0_loop(self):
        """Send STATUS0 when requested (connect/reconnect) and otherwise every
        status0_interval, measured from the last send."""
        stop = self._status0_timer_stop_event
        request = self._status0_request
        next_at = time.monotonic() + self._status0_interval
        while True:
            request.wait(max(0.0, next_at - time.monotonic()))
            if stop.is_set():
                return
            request.clear()
            try:
                self.publish_status0_data()
            except Exception:
                _logger.exception("STATUS0 publish failed; will retry on the next slot")
            next_at = time.monotonic() + self._status0_interval

    def _start_status0_timer(self):
        """Start the STATUS0 thread: sends on request and hourly."""
        self._status0_timer_thread = threading.Thread(
            target=self._status0_loop,
            name=f"{self.__class__.__name__}Status0Timer",
        )
        self._status0_timer_thread.daemon = True
        self._status0_timer_thread.start()

    @staticmethod
    def _net_info() -> Dict[str, str]:
        """Hostname, IP and MAC. May block (DNS, getmac); only called from the
        STATUS0 thread. Failures fall back to placeholders."""
        hostname = socket.gethostname()
        try:
            ip_address = socket.gethostbyname(hostname)
        except OSError as exc:
            _logger.debug("Could not resolve %s: %s", hostname, exc)
            ip_address = "0.0.0.0"
        try:
            mac = getmac.get_mac_address() or ""
        except Exception as exc:  # getmac can fail in odd ways on some hosts
            _logger.debug("Could not determine MAC address: %s", exc)
            mac = ""
        return {"Hostname": hostname, "IPAddress": ip_address, "Mac": mac}

    def get_status0_data(self) -> Status0Data:
        """Get current status data."""
        # Implementation remains the same as in the original code
        # Creating all required status objects...
        status = StatusData(
            DeviceName="Qilowatt Inverter",
            FriendlyName=["Home Assistant", "", ""],
            Topic=self.device_id
        )
        
        status_prm = StatusPRMData(
            StartupUTC=self._startup_utc.replace(tzinfo=timezone.utc).isoformat(),
            BootCount=self._boot_count
        )
                
        return Status0Data(
            Status=status,
            StatusPRM=status_prm,
            StatusFWR=StatusFWRData(Version="1.0.0", Hardware=platform.machine()),
            StatusLOG=StatusLOGData(TelePeriod=10),
            StatusNET=StatusNETData(
                Gateway="192.168.1.1",
                Subnetmask="255.255.255.0",
                **self._net_info(),
            ),
            StatusMQT=StatusMQTData(
                MqttHost="mqtt-test.qilowatt.it",
                MqttPort=8883,
                MqttClient="",
                MqttUser="",
                MqttClientMask="QWAPI_%06X"
            ),
            StatusTIM=StatusTIMData(
                UTC=datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(),
                Local=datetime.now().isoformat()
            )
        )

    def set_publish_callback(self, callback: Callable[[str, Dict[str, Any]], None]):
        """Set callback for publishing data."""
        self._publish_callback = callback