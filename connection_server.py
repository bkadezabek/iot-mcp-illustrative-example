"""
Connection Server — device registry and request router.

Holds a list of MockMCU objects (imported from mock_hardware.py) and two
registry dicts that map device identifiers to the MCU that owns them:
  _sensor_registry  : sensor_id  → MockMCU
  _thermo_registry  : room_id    → MockMCU

route() dispatches an API-shaped request to the correct MCU based on the
path prefix. The MCU then delegates to the correct attached device.
Nothing here is MCP-aware.
"""

from mock_hardware import MockMCU, MockSensor, MockThermostat


class ConnectionServer:
    def __init__(self) -> None:
        self.mcus: list[MockMCU] = []
        self._sensor_registry: dict[str, MockMCU] = {}   # sensor_id  → MCU
        self._sensor_meta: dict[str, dict] = {}           # sensor_id  → {type, unit, ...}
        self._thermo_registry: dict[str, MockMCU] = {}   # room_id    → MCU
        self._setup()

    def _setup(self) -> None:
        # --- MCU A: ESP32 hosting two temperature sensors ---
        mcu_a = MockMCU("ESP32-A", "192.168.1.104", "MQTT")
        # Ranges straddle the stated normal (18–24 °C) so Scenario 1 has
        # meaningful out-of-range and conflict cases to evaluate.
        mcu_a.attach_sensor("TEMP-04", MockSensor("temperature", "°C", (16.0, 22.0)))
        mcu_a.attach_sensor("TEMP-05", MockSensor("temperature", "°C", (22.0, 27.0)))

        # --- MCU B: Arduino hosting one humidity sensor ---
        mcu_b = MockMCU("Arduino-B", "192.168.1.105", "I2C")
        # Range straddles the stated normal (30–60 %) on the high side.
        mcu_b.attach_sensor("HUM-01", MockSensor("humidity", "%", (55.0, 75.0)))

        # --- MCU C: ESP32 hosting the office thermostat ---
        # 20 → 28 °C at 0.35 °C/s takes ~23 real seconds — fast enough for a demo.
        mcu_c = MockMCU("ESP32-C", "192.168.1.106", "MQTT")
        mcu_c.attach_thermostat("office", MockThermostat("office", ambient_temp=20.0, rate_per_sec=0.35))

        self.mcus = [mcu_a, mcu_b, mcu_c]

        # Build sensor registry from attached sensors
        for mcu in self.mcus:
            for sid, sensor in mcu.sensors.items():
                self._sensor_registry[sid] = mcu
                self._sensor_meta[sid] = {
                    "sensor_type": sensor.sensor_type,
                    "unit": sensor.unit,
                    "mcu": mcu.mcu_id,
                    "address": mcu.address,
                }
            for room_id in mcu.thermostats:
                self._thermo_registry[room_id] = mcu

    def route(self, api_request: dict) -> dict:
        """Route an API-shaped request to the correct MCU based on path prefix."""
        path = api_request.get("path", "")
        if path.startswith("/sensors/"):
            sensor_id = api_request.get("sensor_id", "")
            if sensor_id not in self._sensor_registry:
                return {"error": f"Sensor '{sensor_id}' not found in registry"}
            return self._sensor_registry[sensor_id].handle_sensor(api_request)
        if path.startswith("/thermostats/"):
            room_id = api_request.get("room_id", "")
            if room_id not in self._thermo_registry:
                return {"error": f"Room '{room_id}' not found in registry"}
            return self._thermo_registry[room_id].handle_thermostat(api_request)
        return {"error": f"Unknown resource path: '{path}'"}

    def list_sensors(self) -> dict:
        """Return sensor identifiers with type and unit only.
        Connection parameters (MCU id, address, protocol) stay internal."""
        return {
            sid: {"sensor_type": meta["sensor_type"], "unit": meta["unit"]}
            for sid, meta in self._sensor_meta.items()
        }


# Module-level singleton used by mcp_server.py
_server = ConnectionServer()


def route(api_request: dict) -> dict:
    return _server.route(api_request)


def list_sensors() -> dict:
    return _server.list_sensors()
