from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Callable
import threading
import logging
from datetime import datetime, timezone
from .models import (
    Status0Data,
    StatusData, StatusPRMData, StatusFWRData, StatusLOGData,
    StatusNETData, StatusMQTData, StatusTIMData
)
import platform
import socket
import getmac

_logger = logging.getLogger(__name__)

class BaseDevice(ABC):
    """Base class for all devices that can communicate via MQTT."""
    
    def __init__(self, device_id: str):
        self.device_id = device_id
        self._data_initialized = False
        self._sensor_timer_thread: Optional[threading.Thread] = None
        self._state_timer_thread: Optional[threading.Thread] = None
        self._status0_timer_thread: Optional[threading.Thread] = None
        
        self._sensor_timer_stop_event = threading.Event()
        self._state_timer_stop_event = threading.Event()
        self._status0_timer_stop_event = threading.Event()
        
        self._startup_utc = datetime.utcnow()
        self._boot_count = 1
        
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
            
        for thread in [self._sensor_timer_thread, 
                      self._state_timer_thread, 
                      self._status0_timer_thread]:
            if thread:
                thread.join()
                
        # Reset events and threads
        self._sensor_timer_stop_event.clear()
        self._state_timer_stop_event.clear()
        self._status0_timer_stop_event.clear()
        self._sensor_timer_thread = None
        self._state_timer_thread = None
        self._status0_timer_thread = None

    def publish_sensor_data(self):
        sensor_data = self.get_sensor_data()
        # Callback will be set by client
        if hasattr(self, '_publish_callback'):
            self._publish_callback(self.sensor_topic, sensor_data)

    def _start_sensor_timer(self):
        """Start timer for sending sensor data."""
        def sensor_timer():
            while not self._sensor_timer_stop_event.wait(10):
                self.publish_sensor_data()
                    
        self._sensor_timer_thread = threading.Thread(
            target=sensor_timer, 
            name=f"{self.__class__.__name__}SensorTimer"
        )
        self._sensor_timer_thread.daemon = True
        self._sensor_timer_thread.start()

    def publish_state_data(self):
        state_data = self.get_state_data()
        if hasattr(self, '_publish_callback'):
            self._publish_callback(self.state_topic, state_data)

    def _start_state_timer(self):
        """Start timer for sending state data."""
        def state_timer():
            while not self._state_timer_stop_event.wait(60):
                self.publish_state_data()

        self._state_timer_thread = threading.Thread(
            target=state_timer, 
            name=f"{self.__class__.__name__}StateTimer"
        )
        self._state_timer_thread.daemon = True
        self._state_timer_thread.start()

    def _start_status0_timer(self):
        """Start timer for sending status data."""
        def status0_timer():
            # Send at startup
            status0_data = self.get_status0_data()
            if hasattr(self, '_publish_callback'):
                self._publish_callback(self.status0_topic, status0_data.to_dict())
            # Then every 60 minutes
            while not self._status0_timer_stop_event.wait(3600):
                status0_data = self.get_status0_data()
                if hasattr(self, '_publish_callback'):
                    self._publish_callback(self.status0_topic, status0_data.to_dict())
                    
        self._status0_timer_thread = threading.Thread(
            target=status0_timer, 
            name=f"{self.__class__.__name__}Status0Timer"
        )
        self._status0_timer_thread.daemon = True
        self._status0_timer_thread.start()

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
                Hostname=socket.gethostname(),
                IPAddress=socket.gethostbyname(socket.gethostname()),
                Gateway="192.168.1.1",
                Subnetmask="255.255.255.0",
                Mac=getmac.get_mac_address()
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