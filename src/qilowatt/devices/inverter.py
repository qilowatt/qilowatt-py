from ..base_device import BaseDevice
from ..models import (
    EnergyData, MetricsData, WorkModeCommand
)
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, Callable, Tuple

_logger = logging.getLogger(__name__)

# Absolute maximum power limit - failsafe that cannot be exceeded
ABSOLUTE_MAX_POWER = 100000.0

class InverterDevice(BaseDevice):
    """Implementation of an inverter device."""
    
    def __init__(self, device_id: str, **timer_intervals):
        super().__init__(device_id, **timer_intervals)
        self._energy_data: Optional[EnergyData] = None
        self._metrics_data: Optional[MetricsData] = None
        self._workmode_command = WorkModeCommand.from_dict({"Mode": "normal"})
        self._on_command_callback: Optional[Callable[[WorkModeCommand], None]] = None
        self._data_provider: Optional[Callable[[], Tuple[EnergyData, MetricsData]]] = None
        self._provider_has_data = True
        
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

    def set_data_provider(
        self, provider: Optional[Callable[[], Tuple[EnergyData, MetricsData]]]
    ):
        """Pull ENERGY and METRICS from `provider` right before each SENSOR publish.

        This keeps the publish cadence on the device's own fixed 10 s grid while
        making every payload carry data read milliseconds before it is sent.
        Callers using a provider do not need set_energy_data / set_metrics_data.

        The provider is called on the sensor timer thread and must return an
        (EnergyData, MetricsData) tuple. It may return (None, None), or raise,
        when it has no trustworthy data (source entities unavailable): that
        SENSOR slot is then skipped. Stale data is never re-published and
        nothing is invented. Setting a provider starts the publishing
        timers; pass None to stop pulling (the last data is kept).
        """
        self._data_provider = provider
        if provider is not None and not self._data_initialized:
            self._data_initialized = True
            self.start_timers()

    def _refresh_from_provider(self) -> bool:
        """Pull data from the provider; return whether this slot may publish."""
        if self._data_provider is None:
            return True
        energy_data = metrics_data = None
        try:
            energy_data, metrics_data = self._data_provider()
        except Exception:
            if self._provider_has_data:
                _logger.exception("Data provider failed")
        if energy_data is None or metrics_data is None:
            if self._provider_has_data:
                self._provider_has_data = False
                _logger.warning("Data provider has no data; skipping SENSOR publishes until it recovers")
            return False
        if not self._provider_has_data:
            self._provider_has_data = True
            _logger.info("Data provider delivers data again")
        self._energy_data = energy_data
        self._metrics_data = metrics_data
        return True
            
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
        if not self._refresh_from_provider():
            return {}
        if not self._data_initialized or self._energy_data is None or self._metrics_data is None:
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
