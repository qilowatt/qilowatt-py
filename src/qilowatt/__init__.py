# qilowatt/__init__.py

from .client import QilowattMQTTClient
from .models import (
    EnergyData, MetricsData, WorkModeCommand,
    Status0Data, StatusData, StatusPRMData, StatusFWRData,
    StatusLOGData, StatusNETData, StatusMQTData, StatusTIMData
)
from .exceptions import (
    QilowattException,
    ConnectionError,
    AuthenticationError,
)
from .devices.inverter import InverterDevice
from .devices.switch import SwitchDevice

try:
    from ._version import version as __version__
except ImportError:
    __version__ = "unknown"

__all__ = [
    "QilowattMQTTClient",
    "InverterDevice",
    "SwitchDevice",
    "EnergyData",
    "MetricsData",
    "WorkModeCommand",
    "Status0Data",
    "StatusData",
    "StatusPRMData",
    "StatusFWRData",
    "StatusLOGData",
    "StatusNETData",
    "StatusMQTData",
    "StatusTIMData",
    "QilowattException",
    "ConnectionError",
    "AuthenticationError",
]
