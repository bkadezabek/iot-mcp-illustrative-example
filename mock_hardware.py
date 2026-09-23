"""
Mock hardware layer — simulates physical MCUs, sensors, and thermostats.

Nothing here is MCP-aware or Connection-Server-aware. These classes represent
the MCU / Sensors box in Fig. 1: they receive a parsed request from the
Connection Server and return a raw reading.

MockThermostat persists heating state to a temp file so separate MCP server
subprocesses (one per AgentSession) all see the same thermostat state.
"""

import json
import os
import random
import tempfile
import time


class MockSensor:
    """Simulates a read-only physical sensor attached to an MCU."""

    def __init__(self, sensor_type: str, unit: str, value_range: tuple[float, float]) -> None:
        self.sensor_type = sensor_type
        self.unit = unit
        self._range = value_range

    def read(self) -> float:
        return round(random.uniform(*self._range), 1)


class MockThermostat:
    """
    Simulates a thermostat actuator (read + write).

    set_target() writes {target, ambient, start} to a JSON file.
    read_current() loads that file and computes current temperature
    from elapsed real time × heating rate.
    """

    _STATE_DIR = tempfile.gettempdir()

    def __init__(
        self,
        room_id: str,
        ambient_temp: float = 20.0,
        rate_per_sec: float = 0.35,
    ) -> None:
        self.room_id = room_id
        self._ambient = ambient_temp
        self._rate = rate_per_sec
        self._state_file = os.path.join(self._STATE_DIR, f"iot_thermo_{room_id}.json")

    def set_target(self, target: float) -> dict:
        state = {"target": target, "ambient": self._ambient, "start": time.time()}
        with open(self._state_file, "w") as f:
            json.dump(state, f)
        return {
            "status": "heating_started",
            "room_id": self.room_id,
            "target_temp": target,
            "current_temp": round(self._ambient, 1),
        }

    def read_current(self) -> dict:
        try:
            with open(self._state_file) as f:
                state = json.load(f)
            elapsed = time.time() - state["start"]
            current = round(
                min(state["ambient"] + self._rate * elapsed, state["target"]), 1
            )
            return {"current_temp": current, "target_temp": state["target"]}
        except FileNotFoundError:
            return {"current_temp": round(self._ambient, 1), "target_temp": None}


class MockMCU:
    """
    Simulates a microcontroller. Holds one or more MockSensors and/or
    MockThermostats. Receives a parsed API request from the Connection
    Server and delegates to the correct attached device.
    """

    def __init__(self, mcu_id: str, address: str, protocol: str) -> None:
        self.mcu_id = mcu_id
        self.address = address
        self.protocol = protocol
        self.sensors: dict[str, MockSensor] = {}
        self.thermostats: dict[str, MockThermostat] = {}

    def attach_sensor(self, sensor_id: str, sensor: MockSensor) -> None:
        self.sensors[sensor_id] = sensor

    def attach_thermostat(self, room_id: str, thermostat: MockThermostat) -> None:
        self.thermostats[room_id] = thermostat

    def handle_sensor(self, api_request: dict) -> dict:
        """Handle a GET /sensors/{id}/readings request."""
        sensor_id = api_request["sensor_id"]
        sensor_type = api_request["sensor_type"]
        if sensor_id not in self.sensors:
            return {"error": f"Sensor '{sensor_id}' not attached to MCU '{self.mcu_id}'"}
        sensor = self.sensors[sensor_id]
        return {
            "sensor_id": sensor_id,
            "sensor_type": sensor_type,
            "value": sensor.read(),
            "unit": sensor.unit,
            "mcu": self.mcu_id,
            "address": self.address,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def handle_thermostat(self, api_request: dict) -> dict:
        """Handle GET or PUT /thermostats/{room}/... requests."""
        room_id = api_request["room_id"]
        if room_id not in self.thermostats:
            return {"error": f"Thermostat '{room_id}' not attached to MCU '{self.mcu_id}'"}
        thermostat = self.thermostats[room_id]
        if api_request.get("method") == "PUT":
            target = api_request.get("target_temp")
            if target is None:
                return {"error": "target_temp required for PUT"}
            return thermostat.set_target(float(target))
        reading = thermostat.read_current()
        return {"room_id": room_id, **reading}
