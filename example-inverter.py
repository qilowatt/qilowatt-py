# example-inverter.py
#
# Minimal inverter client. Credentials come from environment variables so the
# same script can be used to try a wrong password:
#
#   QW_USERNAME=... QW_PASSWORD=... QW_DEVICE_ID=... python example-inverter.py
#   QW_USERNAME=... QW_PASSWORD=wrong QW_DEVICE_ID=... QW_MAX_AUTH_RETRIES=1 python example-inverter.py
#
# With a wrong password the broker refuses each CONNACK, paho reconnects on its
# own (10 s, then 20 s, ... up to 60 s), and after QW_MAX_AUTH_RETRIES rejected
# attempts the client stops and the error callback below ends the script.

import logging
import os
import threading
import time

from qilowatt import AuthenticationError, EnergyData, InverterDevice, MetricsData, QilowattMQTTClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

mqtt_username = os.environ.get("QW_USERNAME", "")
mqtt_password = os.environ.get("QW_PASSWORD", "")
device_id = os.environ.get("QW_DEVICE_ID", "")
max_auth_retries = int(os.environ.get("QW_MAX_AUTH_RETRIES", "5"))

if not (mqtt_username and mqtt_password and device_id):
    raise SystemExit("Set QW_USERNAME, QW_PASSWORD and QW_DEVICE_ID")

device = InverterDevice(device_id=device_id)

client = QilowattMQTTClient(
    mqtt_username=mqtt_username,
    mqtt_password=mqtt_password,
    device=device,
    host="mqtt.qilowatt.it",
    port=8883,
    tls=True,
    max_auth_retries=max_auth_retries,
)

# Set when the client has given up for good (e.g. credentials rejected too often).
fatal_error = threading.Event()


def on_command_received(command):
    print(f"Received WorkMode Command: {command}")


def on_connection_changed(connected: bool):
    print("Connected and subscribed" if connected else "Disconnected")


def on_client_error(error: Exception):
    # Called from the MQTT network thread. The client has already stopped;
    # a fresh client is needed after fixing the problem.
    if isinstance(error, AuthenticationError):
        print(f"Credentials rejected, giving up: {error}")
    else:
        print(f"Client stopped with error: {error}")
    fatal_error.set()


device.set_command_callback(on_command_received)
client.add_connection_callback(on_connection_changed)
client.add_error_callback(on_client_error)

# Connect to the MQTT broker. This raises if the broker cannot be reached at
# all (no network, DNS failure); a refused login does not raise here, it shows
# up through on_client_error.
client.connect()

# Wait a moment to ensure connection is established
time.sleep(2)

energy_data = EnergyData(
    Power=[100.0, 100.0, 100.0],
    Today=5.0,
    Total=1000.0,
    Current=[5.0, 5.0, 5.0],
    Voltage=[230.0, 230.0, 230.0],
    Frequency=50.0,
)

metrics_data = MetricsData(
    PvPower=[200.0, 200.0],
    PvVoltage=[300.0, 300.0],
    PvCurrent=[8.0, 8.0],
    LoadPower=[500.0, 500.0, 500.0],
    BatterySOC=[80],
    LoadCurrent=[10.0, 10.0, 10.0],
    BatteryPower=[400.0],
    BatteryCurrent=[15.0],
    BatteryVoltage=[48.0],
    GridExportLimit=5000.0,
    BatteryTemperature=[30.0],
    InverterTemperature=40.0,
    # GenVoltage / GenPower / GenCurrent default to zeros when there is no generator
)

# Setting both starts the periodic publishing (SENSOR every 10 s, STATE every 60 s)
device.set_energy_data(energy_data)
device.set_metrics_data(metrics_data)

try:
    print("Running. Press Ctrl+C to exit.")
    while not fatal_error.wait(30):
        # Simulate updating data periodically
        energy_data.Today += 0.1
        device.set_energy_data(energy_data)
    print(f"Stopped: {client.last_error()}")
    raise SystemExit(1)
except KeyboardInterrupt:
    print("Exiting...")
finally:
    client.disconnect()
