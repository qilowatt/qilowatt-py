# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Python library for communicating with Qilowatt inverters and switches via MQTT. Used by Home Assistant integrations and other clients to send telemetry data (sensor readings, state, status) and receive commands from the Qilowatt cloud.

## Commands

```bash
# Install dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test
pytest tests/test_client_auth.py::test_authentication_failure_triggers_retry
```

## Architecture

### Core Components

- **QilowattMQTTClient** (`src/qilowatt/client.py`): Main MQTT client handling connection, authentication with retry logic, and message routing. Manages TLS connection to `mqtt.qilowatt.it:8883`.

- **BaseDevice** (`src/qilowatt/base_device.py`): Abstract base class for all device types. Manages:
  - MQTT topic patterns (`Q/{device_id}/SENSOR`, `Q/{device_id}/STATE`, `Q/{device_id}/STATUS0`, `Q/{device_id}/cmnd/backlog`)
  - Automatic timer-based publishing (sensor: 10s, state: 60s, status0: startup + hourly)
  - Status0 system information collection

- **Device implementations** (`src/qilowatt/devices/`):
  - `InverterDevice`: Handles ENERGY/METRICS data and WORKMODE commands. Supports power limiting via `set_max_energy_power()` and `set_max_battery_power()`.
  - `SwitchDevice`: Simple on/off switch with POWER1 commands.

### Data Flow

1. Client creates a device (InverterDevice/SwitchDevice) with a device_id
2. Client connects via QilowattMQTTClient with MQTT credentials
3. For inverters: set ENERGY and METRICS data to trigger automatic publishing
4. Device receives commands on `Q/{device_id}/cmnd/backlog` topic
5. Commands are parsed and forwarded to registered callbacks

### Models (`src/qilowatt/models.py`)

Dataclasses for structured data: `EnergyData`, `MetricsData`, `WorkModeCommand`, `Status0Data` and related status types. `WorkModeCommand` uses `extras` dict for unknown fields.
