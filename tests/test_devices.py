import copy
import json
import os
import pickle
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from qilowatt.devices.inverter import InverterDevice, ABSOLUTE_MAX_POWER
from qilowatt.devices.switch import SwitchDevice
from qilowatt.models import EnergyData, MetricsData, WorkModeCommand


def make_energy_data(power=None):
    return EnergyData(
        Power=power or [100.0, 100.0, 100.0],
        Today=5.0,
        Total=1000.0,
        Current=[5.0, 5.0, 5.0],
        Voltage=[230.0, 230.0, 230.0],
        Frequency=50.0,
    )


def make_metrics_data(battery_power=None):
    return MetricsData(
        PvPower=[200.0],
        PvVoltage=[300.0],
        PvCurrent=[1.0],
        LoadPower=[100.0],
        BatterySOC=[50],
        LoadCurrent=[1.0],
        BatteryPower=battery_power or [500.0],
        BatteryCurrent=[10.0],
        BatteryVoltage=[48.0],
        GenVoltage=[0.0],
        GenPower=[0.0],
        GenCurrent=[0.0],
        GridExportLimit=0.0,
        BatteryTemperature=[25.0],
        InverterTemperature=40.0,
    )


class TestInverterCommands:
    def make_device(self):
        # No client attached: publish callback stays None, publishing is a no-op
        return InverterDevice(device_id="INV1")

    def test_workmode_command_parsed_and_forwarded(self):
        device = self.make_device()
        received = []
        device.set_command_callback(received.append)

        device.handle_command(b'WORKMODE {"Mode": "buy", "PowerLimit": 5000}')

        assert len(received) == 1
        assert received[0].Mode == "buy"
        assert received[0].PowerLimit == 5000
        assert device._workmode_command.Mode == "buy"

    def test_bare_workmode_without_payload_is_ignored(self):
        device = self.make_device()
        received = []
        device.set_command_callback(received.append)

        device.handle_command(b"WORKMODE")

        assert received == []
        assert device._workmode_command.Mode == "normal"

    def test_invalid_json_does_not_raise_or_change_state(self):
        device = self.make_device()
        device.handle_command(b"WORKMODE not-json{")
        assert device._workmode_command.Mode == "normal"

    def test_non_object_json_payload_is_ignored(self):
        device = self.make_device()
        received = []
        device.set_command_callback(received.append)

        device.handle_command(b'WORKMODE [1, 2, 3]')

        assert received == []
        assert device._workmode_command.Mode == "normal"

    def test_unknown_fields_kept_in_extras(self):
        device = self.make_device()
        device.handle_command(b'WORKMODE {"Mode": "sell", "NewField": 42}')
        command = device._workmode_command
        assert command.NewField == 42
        assert command.to_dict()["NewField"] == 42


class TestInverterPowerLimits:
    def make_initialized_device(self, **kwargs):
        device = InverterDevice(device_id="INV1")
        # Avoid starting real timer threads in tests
        device.start_timers = lambda: None
        device.set_energy_data(make_energy_data(kwargs.get("power")))
        device.set_metrics_data(make_metrics_data(kwargs.get("battery_power")))
        return device

    def test_values_within_limit_pass_through(self):
        device = self.make_initialized_device(power=[1000.0, -1000.0, 500.0])
        device.set_max_energy_power(2000.0)
        data = device.get_sensor_data()
        assert data["ENERGY"]["Power"] == [1000.0, -1000.0, 500.0]

    def test_values_over_limit_reported_as_zero(self):
        device = self.make_initialized_device(power=[3000.0, -3000.0, 500.0])
        device.set_max_energy_power(2000.0)
        data = device.get_sensor_data()
        assert data["ENERGY"]["Power"] == [0.0, 0.0, 500.0]

    def test_absolute_failsafe_applies_without_configured_limit(self):
        device = self.make_initialized_device(power=[ABSOLUTE_MAX_POWER + 1, 500.0, 500.0])
        data = device.get_sensor_data()
        assert data["ENERGY"]["Power"] == [0.0, 500.0, 500.0]

    def test_battery_power_limit(self):
        device = self.make_initialized_device(battery_power=[6000.0])
        device.set_max_battery_power(5000.0)
        data = device.get_sensor_data()
        assert data["METRICS"]["BatteryPower"] == [0.0]

    def test_sensor_data_is_json_serializable(self):
        device = self.make_initialized_device()
        json.dumps(device.get_sensor_data())


class TestWorkModeCommand:
    def test_copy_and_deepcopy(self):
        command = WorkModeCommand.from_dict({"Mode": "buy", "Extra": 1})
        assert copy.copy(command).Extra == 1
        assert copy.deepcopy(command).Extra == 1

    def test_pickle_roundtrip(self):
        command = WorkModeCommand.from_dict({"Mode": "buy", "Extra": 1})
        restored = pickle.loads(pickle.dumps(command))
        assert restored.Mode == "buy"
        assert restored.Extra == 1

    def test_missing_attribute_raises(self):
        command = WorkModeCommand.from_dict({"Mode": "buy"})
        try:
            command.DoesNotExist
        except AttributeError:
            pass
        else:
            raise AssertionError("expected AttributeError")


class TestSwitchDevice:
    def test_send_update_without_client_does_not_raise(self):
        device = SwitchDevice(device_id="SW1")
        try:
            device.turn_on()
            assert device._state is True
            device.turn_off()
            assert device._state is False
        finally:
            device.stop_timers()

    def test_power_commands(self):
        device = SwitchDevice(device_id="SW1")
        published = []
        device.set_publish_callback(lambda topic, data: published.append((topic, data)))
        try:
            device.handle_command(b"POWER1 1")
            assert device._state is True
            device.handle_command(b"POWER1 0")
            assert device._state is False
            assert any(topic.endswith("POWER1") for topic, _ in published)
        finally:
            device.stop_timers()


class TestStatus0:
    def test_status0_reflects_client_mqtt_info(self):
        device = InverterDevice(device_id="INV1")
        device.set_mqtt_info(host="mqtt.example.org", port=1234, username="alice")
        status = device.get_status0_data()
        assert status.StatusMQT.MqttHost == "mqtt.example.org"
        assert status.StatusMQT.MqttPort == 1234
        assert status.StatusMQT.MqttUser == "alice"

    def test_status0_is_json_serializable(self):
        device = InverterDevice(device_id="INV1")
        json.dumps(device.get_status0_data().to_dict())
