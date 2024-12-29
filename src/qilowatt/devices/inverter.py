from ..base_device import BaseDevice
from ..models import (
    EnergyData, MetricsData, WorkModeCommand
)
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable

_logger = logging.getLogger(__name__)

class InverterDevice(BaseDevice):
    """Implementation of an inverter device."""
    
    def __init__(self, device_id: str):
        super().__init__(device_id)
        self._energy_data: Optional[EnergyData] = None
        self._metrics_data: Optional[MetricsData] = None
        self._workmode_command = WorkModeCommand.from_dict({"Mode": "normal"})
        self._on_command_callback: Optional[Callable[[WorkModeCommand], None]] = None
    
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
            self.start_timers()
            
    def handle_command(self, payload: bytes):
        """Handle WORKMODE commands."""
        try:
            message = payload.decode('utf-8')
            if message.startswith("WORKMODE"):
                json_part = message[len("WORKMODE "):]
                data = json.loads(json_part)
                command = WorkModeCommand.from_dict(data)
                self._workmode_command = command
                if self._on_command_callback:
                    self._on_command_callback(command)
        except Exception as e:
            _logger.error(f"Error processing command message: {e}")

    def set_command_callback(self, callback: Callable[[WorkModeCommand], None]):
        """Set callback for command handling."""
        self._on_command_callback = callback

    def get_sensor_data(self) -> Dict[str, Any]:
        """Get current sensor data."""
        if not self._data_initialized:
            return {}
            
        sensor_data = {
            "Time": datetime.utcnow().isoformat(),
            "POWER1": 0,
            "ENERGY": self._energy_data.__dict__,
            "METRICS": self._metrics_data.__dict__,
            "WORKMODE": self._workmode_command.__dict__
        }
        return sensor_data

    def get_state_data(self) -> Dict[str, Any]:
        """Get current state data."""
        return {
            "Time": datetime.utcnow().isoformat(),
            "Uptime": int((datetime.utcnow() - self._startup_utc).total_seconds()),
        }