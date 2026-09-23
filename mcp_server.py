"""
MCP Server — exposes four tools to the Agent orchestrator:
  read_sensor     — reads a sensor via the Connection Server
  list_sensors    — returns sensor identifiers with type and unit
  set_thermostat  — sends a PUT to /thermostats/{room}/target
  read_thermostat — sends a GET to /thermostats/{room}/temperature

Each tool builds an explicit API-shaped request (method, path, params) and
forwards it to connection_server.route(), which dispatches to the correct
MCU or MockThermostat. The tool never executes device logic itself.
Physical connection parameters (MCU id, address, protocol) are known only
to the Connection Server and are never included in tool return values.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
import connection_server

mcp = FastMCP("IoT MCP Server")


@mcp.tool()
def read_sensor(sensor_id: str, sensor_type: str) -> str:
    """Read the current value from an IoT sensor via the Connection Server.

    Args:
        sensor_id: Unique device identifier from the registry (e.g. TEMP-04).
        sensor_type: Measurement type to request (temperature or humidity).
    """
    api_request = {
        "method": "GET",
        "path": f"/sensors/{sensor_id}/readings",
        "params": {"type": sensor_type},
        "sensor_id": sensor_id,
        "sensor_type": sensor_type,
    }
    result = connection_server.route(api_request)
    if "error" in result:
        return f"Error: {result['error']}"
    return (
        f"Sensor: {result['sensor_id']} | "
        f"Type: {result['sensor_type']} | "
        f"Value: {result['value']}{result['unit']} | "
        f"Timestamp: {result['timestamp']}"
    )


@mcp.tool()
def list_sensors() -> str:
    """Return all sensors registered in the Connection Server's device registry."""
    return json.dumps(connection_server.list_sensors(), indent=2)


@mcp.tool()
def set_thermostat(room_id: str, target_temp: float) -> str:
    """Set the target temperature for a room's thermostat.

    Args:
        room_id: Room identifier (e.g. office).
        target_temp: Desired temperature in degrees Celsius.
    """
    api_request = {
        "method": "PUT",
        "path": f"/thermostats/{room_id}/target",
        "room_id": room_id,
        "target_temp": target_temp,
    }
    result = connection_server.route(api_request)
    if "error" in result:
        return f"Error: {result['error']}"
    return (
        f"Thermostat set | "
        f"Room: {result['room_id']} | "
        f"Target: {result['target_temp']}°C | "
        f"Current: {result['current_temp']}°C | "
        f"Status: {result['status']}"
    )


@mcp.tool()
def read_thermostat(room_id: str) -> str:
    """Read the current temperature from a room's thermostat.

    Args:
        room_id: Room identifier (e.g. office).
    """
    api_request = {
        "method": "GET",
        "path": f"/thermostats/{room_id}/temperature",
        "room_id": room_id,
    }
    result = connection_server.route(api_request)
    if "error" in result:
        return f"Error: {result['error']}"
    target_str = (
        f"{result['target_temp']}°C"
        if result["target_temp"] is not None
        else "not set"
    )
    return (
        f"Room: {result['room_id']} | "
        f"Current: {result['current_temp']}°C | "
        f"Target: {target_str}"
    )


if __name__ == "__main__":
    mcp.run()
