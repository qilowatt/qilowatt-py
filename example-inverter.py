# example.py

import logging
from qilowatt import QilowattMQTTClient
from qilowatt import EnergyData, MetricsData
from qilowatt import InverterDevice
import time

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# User-provided MQTT credentials and inverter ID
mqtt_username = ''
mqtt_password = ''
device = InverterDevice(device_id='')

# Create the Qilowatt MQTT client
client = QilowattMQTTClient(
    mqtt_username=mqtt_username,
    mqtt_password=mqtt_password,
    device=device,
    host='mqtt.qilowatt.it',
    port=8883,
    tls=True
)
def on_command_received(command):
    print(f"Received WorkMode Command: {command}")

# Set the command callback
device.set_command_callback(on_command_received)

# Connect to the MQTT broker
client.connect()

# Wait a moment to ensure connection is established
time.sleep(2)

# Prepare ENERGY and METRICS data
energy_data = EnergyData(
    Power=[100.0, 100.0, 100.0],
    Today=5.0,
    Total=1000.0,
    Current=[5.0, 5.0, 5.0],
    Voltage=[230.0, 230.0, 230.0],
    Frequency=50.0
)

metrics_data = MetricsData(
    PvPower=[200.0, 200.0],
    PvVoltage=[300.0, 300.0],
    PvCurrent=[8.0, 8.0],
    LoadPower=[500.0, 500.0, 500.0],
    BatterySOC=80,
    LoadCurrent=[10.0, 10.0, 10.0],
    BatteryPower=[400.0],
    BatteryCurrent=[15.0],
    BatteryVoltage=[48.0],
    GridExportLimit=5000.0,
    BatteryTemperature=[30.0],
    InverterTemperature=40.0
)

# Set the data
device.set_energy_data(energy_data)
device.set_metrics_data(metrics_data)

# Now the module will automatically start sending data at the specified intervals

# Keep the script running to receive commands and send data
try:
    print("Running. Press Ctrl+C to exit.")
    while True:
        # Simulate updating data periodically
        time.sleep(30)
        # Update ENERGY data if needed
        energy_data.Today += 0.1
        device.set_energy_data(energy_data)
except KeyboardInterrupt:
    print("Exiting...")
finally:
    client.disconnect()
