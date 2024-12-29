from ..base_device import BaseDevice
from typing import Dict, Any
from typing import Callable, Optional
from datetime import datetime
import logging

_logger = logging.getLogger(__name__)

class SwitchDevice(BaseDevice):
    """Implementation of a basic switch device."""
    
    def __init__(self, device_id: str):
        super().__init__(device_id)
        self._state = False
        self._data_initialized = True  # Basic switch is always ready
        self._on_switch_command_callback: Optional[Callable[[bool], None]] = None
        self.start_timers()

    def send_update(self):
        """Send an update to the MQTT broker."""
        self.publish_sensor_data()
        self.publish_state_data()
        self._publish_callback(self.power_topic, 1 if self._state else 0)
    
    def set_command_callback(self, callback: Callable[[bool], None]):
        """Set callback for command handling."""
        self._on_switch_command_callback = callback

    def handle_command(self, payload: bytes):
        """Handle on/off commands."""
        try:
            message = payload.decode('utf-8')
            if message == "POWER1 1":
                self.turn_on()
            elif message == "POWER1 0":
                self.turn_off()
        except Exception as e:
            _logger.error(f"Error processing command message: {e}")
 
    def turn_on(self):
        """Turn the switch on."""
        self._state = True
        self.send_update()
        if self._on_switch_command_callback:
            self._on_switch_command_callback(self._state)

    def turn_off(self):
        """Turn the switch off."""
        self._state = False
        self.send_update()
        if self._on_switch_command_callback:
            self._on_switch_command_callback(self._state)
   
    def get_sensor_data(self) -> Dict[str, Any]:
        """Get current sensor data."""
        return {
            "Time": datetime.utcnow().isoformat(),
            "Switch1": "ON" if self._state else "OFF"
        }
    
    def get_state_data(self) -> Dict[str, Any]:
        """Get current state data."""
        return {
            "Time": datetime.utcnow().isoformat(),
            "Uptime": int((datetime.utcnow() - self._startup_utc).total_seconds()),
            "POWER1": "ON" if self._state else "OFF",
        }