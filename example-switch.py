import logging
from qilowatt import QilowattMQTTClient
from qilowatt import SwitchDevice
import time

# Configure logging
logging.basicConfig(level=logging.DEBUG)

# User-provided MQTT credentials and device ID
mqtt_username = ''
mqtt_password = ''
device = SwitchDevice(device_id='')

# Create the Qilowatt MQTT client
client = QilowattMQTTClient(
    mqtt_username=mqtt_username,
    mqtt_password=mqtt_password,
    device=device,
    host='mqtt.qilowatt.it',
    port=8883,
    tls=True
)
def on_command_received(state):
    print(f"Switch status: {state}")

# Connect to the MQTT broker
client.connect()

device.set_command_callback(on_command_received)

# Wait a moment to ensure connection is established
time.sleep(2)

# Keep the script running to receive commands and send data
try:
    print("Running. Press Ctrl+C to exit.")
    while True:
        # Simulate updating data periodically
        time.sleep(30)
        # Turn the switch on or off
        device.turn_on()
        time.sleep(30)
        device.turn_off()
except KeyboardInterrupt:
    print("Exiting...")
finally:
    client.disconnect()
