from dataclasses import dataclass, field
from typing import List, Optional
from datetime import datetime
import uuid

@dataclass
class PingData:
    Reachable: bool = True

@dataclass
class ESP32Data:
    Temperature: float = 64.4  # Default value

@dataclass
class EnergyData:
    Power: List[float]
    Today: float
    Total: float
    Current: List[float]
    Voltage: List[float]
    Frequency: float

@dataclass
class MetricsData:
    PvPower: List[float]
    PvVoltage: List[float]
    PvCurrent: List[float]
    LoadPower: List[float]
    BatterySOC: int
    LoadCurrent: List[float]
    BatteryPower: List[float]
    BatteryCurrent: List[float]
    BatteryVoltage: List[float]
    GridExportLimit: float
    BatteryTemperature: List[float]
    InverterTemperature: float
    AlarmCodes: List[int] = field(default_factory=lambda: [0, 0, 0, 0, 0, 0])
    InverterStatus: int = 2  # Default value

@dataclass
class VersionData:
    fs: str = "24.7.1"
    led: str = "24.3.1"
    inverter: str = "24.10.2"
    qilowatt: str = "24.8.1"
    registers: float = 2.5

@dataclass
class WorkModeCommand:
    Mode: str = "normal"
    _source: Optional[str] = None
    BatterySoc: Optional[int] = None
    PowerLimit: Optional[int] = None
    PeakShaving: Optional[int] = None
    ChargeCurrent: Optional[int] = None
    DischargeCurrent: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'WorkModeCommand':
        return cls(
            Mode=data.get("Mode"),
            _source=data.get("_source"),
            BatterySoc=data.get("BatterySoc"),
            PowerLimit=data.get("PowerLimit"),
            PeakShaving=data.get("PeakShaving"),
            ChargeCurrent=data.get("ChargeCurrent"),
            DischargeCurrent=data.get("DischargeCurrent"),
        )

@dataclass
class SensorData:
    ENERGY: EnergyData
    METRICS: MetricsData
    PING: PingData = field(default_factory=PingData)
    Time: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    ESP32: ESP32Data = field(default_factory=ESP32Data)
    VERSION: VersionData = field(default_factory=VersionData)
    TempUnit: str = "C"
    WORKMODE: Optional[WorkModeCommand] = None  # Will be set internally

    def to_dict(self) -> dict:
        """Convert the dataclass to a dictionary."""
        data = {
            "PING": self.PING.__dict__,
            "Time": self.Time,
            "ESP32": self.ESP32.__dict__,
            "ENERGY": self.ENERGY.__dict__,
            "METRICS": self.METRICS.__dict__,
            "VERSION": self.VERSION.__dict__,
            "TempUnit": self.TempUnit,
            "WORKMODE": self.WORKMODE.__dict__ if self.WORKMODE else {},
        }
        return data

@dataclass
class StatusData:
    DeviceName: str
    FriendlyName: List[str]
    Topic: str

@dataclass
class StatusPRMData:
    StartupUTC: str
    BootCount: int

@dataclass
class StatusFWRData:
    Version: str
    Hardware: str

@dataclass
class StatusLOGData:
    TelePeriod: int

@dataclass
class StatusNETData:
    Hostname: str
    IPAddress: str
    Gateway: str
    Subnetmask: str
    Mac: str
    DNSServer1: Optional[str] = None
    DNSServer2: Optional[str] = None

@dataclass
class StatusMQTData:
    MqttHost: str
    MqttPort: int
    MqttClient: str
    MqttUser: str
    MqttCount: Optional[int] = None
    MqttClientMask: Optional[str] = None

@dataclass
class StatusTIMData:
    UTC: str
    Local: str
    StartDST: Optional[str] = None
    EndDST: Optional[str] = None
    Timezone: Optional[int] = None

@dataclass
class Status0Data:
    Status: StatusData
    StatusPRM: StatusPRMData
    StatusFWR: StatusFWRData
    StatusLOG: StatusLOGData
    StatusNET: StatusNETData
    StatusMQT: StatusMQTData
    StatusTIM: StatusTIMData

    def to_dict(self) -> dict:
        return {
            "Status": self.Status.__dict__,
            "StatusPRM": self.StatusPRM.__dict__,
            "StatusFWR": self.StatusFWR.__dict__,
            "StatusLOG": self.StatusLOG.__dict__,
            "StatusNET": self.StatusNET.__dict__,
            "StatusMQT": self.StatusMQT.__dict__,
            "StatusTIM": self.StatusTIM.__dict__,
        }