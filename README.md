# IoT MCP — Illustrative Implementation

Minimal single-machine implementation that illustrates the architecture proposed
in the paper. All hardware is mocked; no external devices are required. This is
not an evaluation of performance or robustness.

## Files

| File | Paper component | Responsibility |
|---|---|---|
| `connection_server.py` | Connection Server | Device registry; resolves device identifiers; routes requests to the correct mock MCU. Implemented as an in-process module imported by `mcp_server.py`. |
| `mock_hardware.py` | IoT Devices (MCU / Sensors) | `MockMCU`, `MockSensor`, `MockThermostat`. Thermostat heating state is shared between subprocesses via a temp file. |
| `mcp_server.py` | MCP Server / Tool (FastMCP) | Exposes four tools: `read_sensor`, `list_sensors`, `set_thermostat`, `read_thermostat`. |
| `agent_client.py` | Agent Orchestrator + MCP Client + LLM wrapper | `AgentOrchestrator`, `AgentSession`, `MonitorSession`, `LLM`. |

## Setup

```bash
cd proof_of_concept
python3 -m venv .venv
source .venv/bin/activate
pip install "anthropic" "mcp<2.0"
export ANTHROPIC_API_KEY=sk-ant-...
```

## Running

```bash
python agent_client.py
```

To capture output for the paper (Tables 2/3):

```bash
python agent_client.py 2>&1 | tee run_output.txt
```

`agent_client.py` is the single entry point — it spawns `mcp_server.py` as a
stdio subprocess per agent session. Do not start the other files directly.

## Implementation notes

- **Tool discovery**: the orchestrator calls `list_tools` once on a temporary
  connection at startup to obtain tool schemas, which are then passed to all
  agent sessions. Additionally, the MCP SDK (`mcp/client/session.py`,
  `_validate_tool_result`, lines 417–421) issues an automatic `list_tools` on
  the first `call_tool` for each tool name in a new session, in order to
  populate an internal output-schema cache. Since the tools in this
  implementation define no `outputSchema`, the actual schema validation is
  skipped, but the `list_tools` call still fires — once per new client session.
- **Per-session MCP clients**: each `AgentSession` and the `MonitorSession` get
  their own `stdio_client` connection, which spawns a separate `mcp_server.py`
  subprocess. This is why thermostat state is shared via a temp file — the
  controller and monitor live in different processes. The server subprocess
  receives the full parent environment (`env=dict(os.environ)`) so that
  `tempfile.gettempdir()` resolves to the same directory in all processes;
  without this, the MCP SDK's `get_default_environment()` omits `TMPDIR` on
  macOS, causing the client cleanup and the server writes to target different
  paths.
- **Non-blocking LLM calls**: LLM calls inside `AgentSession.run` run via
  `asyncio.to_thread` so concurrent sessions do not block each other on the
  event loop.
- **Tools never return connection parameters**: MCU id, IP address, and protocol
  are known only to the Connection Server and never appear in tool return values.

## Scenario 1 — Environmental check

> "Check all sensors. Normal ranges: temperature 18–24 °C, humidity 30–60 %.
> Report which sensors, if any, are outside their normal range."

Decomposition is code-driven: one sub-task is created per entry in the sensor
registry. Three `AgentSession`s run concurrently, each scoped to `read_sensor`
only. Aggregation is deterministic code — not the LLM: each reading is checked
against the stated normal range, and same-type sensors that differ by more than
2 °C are flagged for manual verification. Sensor values are random within the
simulated ranges, so each run produces different numbers.

| Sensor | MCU | Type | Simulated range | Stated normal |
|---|---|---|---|---|
| TEMP-04 | ESP32-A | temperature | 16–22 °C | 18–24 °C |
| TEMP-05 | ESP32-A | temperature | 22–27 °C | 18–24 °C |
| HUM-01 | Arduino-B | humidity | 55–75 % | 30–60 % |

## Scenario 2 — Thermostat control

> "Set the office room temperature to 28°C."

The LLM selects and parameterises roles from a predefined set (`controller`,
`monitor`), with a hard-coded fallback if its JSON output cannot be parsed.

- **Controller**: one `AgentSession` with only `set_thermostat`; issues a single
  command and exits.
- **Monitor**: deterministic polling task (no LLM); calls `read_thermostat` every
  3 s until the current temperature reaches the target or 120 s elapses.

The mocked thermostat heats from 20 °C at 0.35 °C/s, so reaching 28 °C takes
roughly 23 s. The orchestrator cancels the controller only if it is still active
when the monitor finishes; the output explicitly states which case occurred. The
LLM then writes a 1–2 sentence summary of the outcome.

## What the output shows

```
[Discovery]    Tools + sensor registry queried via one temporary MCP connection.

--- Scenario 1 ---
[Orchestrator] Request decomposed into 3 sub-tasks (code-driven, from registry).
[Orchestrator] 3 AgentSessions spawned; each gets its own MCP Client/subprocess.
[Agents]       Sessions run concurrently via asyncio.gather().
[Concurrency]  Session timing table + overlap confirmation.
[Concurrency]  First LLM call window per session + overlap line:
               "→ LLM calls overlapped for ≥X.XXXs (non-blocking)"
[Agents]       Raw MCP result strings per sensor.
[Orchestrator] Aggregated report: per-sensor range status + conflict detection.

--- Scenario 2 ---
[Orchestrator] LLM-driven decomposition: JSON role list printed.
[Orchestrator] Controller + monitor spawned concurrently.
[Monitor]      Poll lines every 3 s showing rising temperature.
[Monitor]      Final status: "reached" or "not reached (timeout)", with temp + elapsed.
[Orchestrator] Cancellation outcome: CANCELLED or NOT NEEDED (with timing).
[LLM]          1–2 sentence natural-language summary.
```

## Connecting to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "iot-server": {
      "command": "/path/to/proof_of_concept/.venv/bin/python",
      "args": ["/path/to/proof_of_concept/mcp_server.py"]
    }
  }
}
```

Replace `/path/to/` with the absolute path on your machine. Restart Claude
Desktop and ask: *"What is the current temperature from sensor TEMP-04?"*
