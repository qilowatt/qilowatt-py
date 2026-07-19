# [qilowatt](<https://qilowatt.eu>) Python Module

Python library for communicating with Qilowatt inverters and switches via MQTT.

- Connects to the Qilowatt MQTT server to send SENSOR, STATE and STATUS0 data
- Receives BACKLOG commands (WORKMODE for inverters, POWER1 for switches)
- See `example-inverter.py` and `example-switch.py` for usage

## Installation

```bash
pip install qilowatt
```

## Quick start

```python
from qilowatt import QilowattMQTTClient, InverterDevice

device = InverterDevice(device_id="YOUR_DEVICE_ID")
client = QilowattMQTTClient(
    mqtt_username="...",
    mqtt_password="...",
    device=device,
)
client.connect()
```

## TLS

By default the client verifies the broker's TLS certificate against the
system CA store. If you need to connect to a broker with a self-signed
certificate, pass `tls_insecure=True` (not recommended — the connection
becomes vulnerable to man-in-the-middle attacks):

```python
client = QilowattMQTTClient(..., tls_insecure=True)
```

## Development

```bash
pip install -e ".[dev]"
pytest
```
