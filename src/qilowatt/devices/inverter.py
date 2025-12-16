from ..base_device import BaseDevice
from ..models import (
    EnergyData, MetricsData, WorkModeCommand
)
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable

_logger = logging.getLogger(__name__)

# Absolute maximum power limit - failsafe that cannot be exceeded
ABSOLUTE_MAX_POWER = 50000.0

class InverterDevice(BaseDevice):
    """Implementation of an inverter device."""
    
    def __init__(self, device_id: str):
        super().__init__(device_id)
        self._energy_data: Optional[EnergyData] = None
        self._metrics_data: Optional[MetricsData] = None
        self._workmode_command = WorkModeCommand.from_dict({"Mode": "normal"})
        self._on_command_callback: Optional[Callable[[WorkModeCommand], None]] = None
        
        # Max value limits - values exceeding these will be reported as 0
        self._max_energy_power: Optional[float] = None
        self._max_battery_power: Optional[float] = None
    
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

    def set_max_energy_power(self, max_value: Optional[float]):
        """Set the maximum allowed value for Energy Power.
        
        Values exceeding this limit will be reported as 0.
        Set to None to disable the limit.
        """
        self._max_energy_power = max_value

    def set_max_battery_power(self, max_value: Optional[float]):
        """Set the maximum allowed value for Battery Power.
        
        Values exceeding this limit (absolute value) will be reported as 0.
        Set to None to disable the limit.
        """
        self._max_battery_power = max_value

    def _apply_power_limits(self, values: list, max_value: Optional[float]) -> list:
        """Apply max value limit to a list of power values.
        
        Returns 0 for any value that exceeds the max (by absolute value).
        The absolute failsafe limit (ABSOLUTE_MAX_POWER) is always enforced.
        """
        effective_max = ABSOLUTE_MAX_POWER
        if max_value is not None:
            effective_max = min(max_value, ABSOLUTE_MAX_POWER)
        return [0.0 if abs(v) > effective_max else v for v in values]

    def get_sensor_data(self) -> Dict[str, Any]:
        """Get current sensor data."""
        if not self._data_initialized:
            return {}
        
        # Apply power limits to energy data
        energy_dict = self._energy_data.__dict__.copy()
        energy_dict["Power"] = self._apply_power_limits(
            self._energy_data.Power, self._max_energy_power
        )
        
        # Apply power limits to metrics data
        metrics_dict = self._metrics_data.__dict__.copy()
        metrics_dict["BatteryPower"] = self._apply_power_limits(
            self._metrics_data.BatteryPower, self._max_battery_power
        )
            
        sensor_data = {
            "Time": datetime.utcnow().isoformat(),
            "POWER1": 0,
            "VERSION": self.get_version_data(),
            "ENERGY": energy_dict,
            "METRICS": metrics_dict,
            "WORKMODE": self._workmode_command.to_dict()
        }
        return sensor_data

    def get_state_data(self) -> Dict[str, Any]:
        """Get current state data."""
        return {
            "Time": datetime.utcnow().isoformat(),
            "Uptime": int((datetime.utcnow() - self._startup_utc).total_seconds()),
        }