"""
Agent orchestrator + Agent sessions + MCP clients + LLM wrapper.

Two scenarios run back-to-back:

  Scenario 1 — Environmental check
    Decomposition: code-driven (one sub-task per registry entry)
    Agents: 3 concurrent reader AgentSessions, each with its own MCP Client
    Aggregation: deterministic numeric range check + conflict detection

  Scenario 2 — Thermostat control
    Decomposition: LLM-driven (orchestrator calls LLM, parses JSON role list)
    Agents: controller AgentSession (sets target once) + MonitorSession (polls)
    Both agents run concurrently; orchestrator cancels controller when monitor
    signals target reached. LLM writes the final human-readable summary.

Class map (matches Fig. 1 components):
  LLM               — thin Anthropic API wrapper
  AgentSession      — system prompt + scoped tools + conversation history
  MonitorSession    — polling loop, no LLM; deterministic stop condition
  AgentOrchestrator — decompose / spawn / aggregate / summarize
"""

import asyncio
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Optional

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

MODEL = "claude-sonnet-4-5"
POLL_INTERVAL = 3.0     # seconds between thermostat reads in MonitorSession
HEATING_TIMEOUT = 120.0  # give up after this many seconds

NORMAL_RANGES: dict[str, tuple[float, float]] = {
    "temperature": (18.0, 24.0),
    "humidity": (30.0, 60.0),
}
UNITS: dict[str, str] = {"temperature": "°C", "humidity": "%"}

SENSOR_REQUEST = (
    "Check all sensors. Normal ranges: temperature 18–24 °C, humidity 30–60 %. "
    "Report which sensors, if any, are outside their normal range."
)
THERMOSTAT_REQUEST = "Set the office room temperature to 28°C."


# ---------------------------------------------------------------------------
# LLM — thin wrapper around the Anthropic API
# ---------------------------------------------------------------------------

class LLM:
    """Stateless: accepts system prompt, messages, optional tools; returns a Message."""

    def __init__(self) -> None:
        self._client = anthropic.Anthropic()

    def call(
        self,
        system: str,
        messages: list,
        tools: Optional[list] = None,
        max_tokens: int = 512,
    ) -> anthropic.types.Message:
        kwargs: dict = dict(
            model=MODEL, max_tokens=max_tokens, system=system, messages=messages
        )
        if tools:
            kwargs["tools"] = tools
        return self._client.messages.create(**kwargs)


# ---------------------------------------------------------------------------
# AgentSession — one active sub-task, scoped to one sensor or device
# ---------------------------------------------------------------------------

@dataclass
class AgentSession:
    """
    Stateful wrapper for one LLM task. Holds:
      label       : human-readable id used in log output
      system_prompt: scopes the LLM to its assigned sub-task and sensor/device
      tools       : subset of MCP tools this session may use
      sensor_id/type: metadata for sensor reader sessions (empty for others)
      history     : conversation turns accumulated during run()
      result      : populated by run(), consumed by the orchestrator
    """

    label: str
    system_prompt: str
    tools: list
    sensor_id: str = ""
    sensor_type: str = ""
    history: list = field(default_factory=list)
    llm_windows: list = field(default_factory=list)
    result: Optional[dict] = None

    async def run(
        self, llm: LLM, mcp_client: ClientSession, user_message: str
    ) -> dict:
        self.history.append({"role": "user", "content": user_message})
        t0 = time.monotonic()
        response = await asyncio.to_thread(llm.call, self.system_prompt, self.history, self.tools)
        self.llm_windows.append((t0, time.monotonic()))

        raw_result = "(no tool call)"
        summary = ""

        if response.stop_reason == "tool_use":
            tool_block = next(b for b in response.content if b.type == "tool_use")
            mcp_result = await mcp_client.call_tool(tool_block.name, tool_block.input)
            raw_result = mcp_result.content[0].text if mcp_result.content else "(empty)"

            self.history.append({"role": "assistant", "content": response.content})
            self.history.append({"role": "user", "content": [{
                "type": "tool_result",
                "tool_use_id": tool_block.id,
                "content": raw_result,
            }]})
            t1 = time.monotonic()
            final = await asyncio.to_thread(llm.call, self.system_prompt, self.history, self.tools)
            self.llm_windows.append((t1, time.monotonic()))
            summary = next((b.text for b in final.content if b.type == "text"), "")
        else:
            summary = next((b.text for b in response.content if b.type == "text"), "")

        value = self._parse_sensor_value(raw_result) if self.sensor_id else None
        self.result = {
            "label": self.label,
            "sensor_id": self.sensor_id,
            "sensor_type": self.sensor_type,
            "raw": raw_result,
            "value": value,
            "summary": summary,
            "llm_windows": self.llm_windows,
        }
        return self.result

    @staticmethod
    def _parse_sensor_value(text: str) -> Optional[float]:
        m = re.search(r"Value:\s*([\d.]+)", text)
        return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# MonitorSession — polls a thermostat until target is reached; no LLM in loop
# ---------------------------------------------------------------------------

@dataclass
class MonitorSession:
    """
    Polls read_thermostat at regular intervals. The stop condition
    (current >= target) is deterministic code — the LLM is not involved.
    The orchestrator cancels the controller agent once this session returns.
    """

    room_id: str
    target_temp: float
    poll_interval: float = POLL_INTERVAL
    timeout: float = HEATING_TIMEOUT
    readings: list[tuple[float, float]] = field(default_factory=list)
    result: Optional[dict] = None

    async def run(self, mcp_client: ClientSession) -> dict:
        start = time.time()
        print(
            f"  [Monitor:{self.room_id}] Polling every {self.poll_interval:.0f}s "
            f"until {self.target_temp}°C ..."
        )
        while True:
            result = await mcp_client.call_tool(
                "read_thermostat", {"room_id": self.room_id}
            )
            text = result.content[0].text if result.content else ""
            current = self._parse_temp(text)
            elapsed = round(time.time() - start, 1)

            if current is not None:
                self.readings.append((elapsed, current))
                print(f"  [Monitor:{self.room_id}] {current}°C  (+{elapsed:.0f}s)")

            if current is not None and current >= self.target_temp:
                self.result = {
                    "reached": True,
                    "final_temp": current,
                    "elapsed_seconds": elapsed,
                    "readings": self.readings,
                }
                return self.result
            if elapsed >= self.timeout:
                self.result = {
                    "reached": False,
                    "final_temp": current,
                    "elapsed_seconds": elapsed,
                    "readings": self.readings,
                }
                return self.result
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _parse_temp(text: str) -> Optional[float]:
        m = re.search(r"Current:\s*([\d.]+)", text)
        return float(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# AgentOrchestrator — decompose, spawn, aggregate, summarize
# ---------------------------------------------------------------------------

class AgentOrchestrator:
    """
    Scenario 1 (sensor check):
      decompose()      — one sub-task per registry entry (code-driven)
      spawn_session()  — reader AgentSession scoped to one sensor
      aggregate()      — numeric range check + conflict detection

    Scenario 2 (thermostat control):
      decompose_with_llm() — LLM parses request → JSON role list
      spawn_controller()   — AgentSession with only set_thermostat
      spawn_monitor()      — MonitorSession for the same room
      summarize_thermostat() — LLM writes final human-readable outcome
    """

    CONFLICT_THRESHOLD = 2.0

    def __init__(self, llm: LLM, all_tools: list) -> None:
        self.llm = llm
        self.all_tools = all_tools
        self.sessions: list[AgentSession] = []

    # --- Scenario 1 ---

    def decompose(self, sensor_registry: dict) -> list[dict]:
        """Create one sub-task per registered sensor (code-driven)."""
        return [
            {"sensor_id": sid, "sensor_type": meta["sensor_type"]}
            for sid, meta in sensor_registry.items()
        ]

    def spawn_session(self, subtask: dict) -> AgentSession:
        """Spawn a reader AgentSession scoped to one sensor."""
        sid, stype = subtask["sensor_id"], subtask["sensor_type"]
        session = AgentSession(
            label=f"reader:{sid}",
            sensor_id=sid,
            sensor_type=stype,
            system_prompt=(
                f"You are an IoT monitoring agent responsible for sensor {sid}. "
                f"Use read_sensor to retrieve its {stype} reading. "
                f"Report the exact numeric value and unit."
            ),
            tools=[t for t in self.all_tools if t["name"] == "read_sensor"],
        )
        self.sessions.append(session)
        return session

    def aggregate(self, results: list[dict]) -> str:
        """
        Compare each result against NORMAL_RANGES.
        Flag out-of-range sensors and detect conflicts between
        same-type sensors that diverge beyond CONFLICT_THRESHOLD.
        This is deterministic code — the LLM is not involved.
        """
        lines: list[str] = ["=== Aggregated Report ==="]
        out_of_range: list[str] = []
        by_type: dict[str, list[tuple[str, float]]] = {}

        for r in results:
            sid, stype, value = r["sensor_id"], r["sensor_type"], r["value"]
            unit = UNITS.get(stype, "")
            if value is None:
                lines.append(f"  {sid}: could not parse numeric value")
                continue
            lo, hi = NORMAL_RANGES.get(stype, (None, None))
            in_range = (lo <= value <= hi) if lo is not None else True
            status = "OK" if in_range else "OUT OF RANGE"
            lines.append(
                f"  {sid} ({stype}): {value}{unit}  [{status}]"
                + (f"  normal: {lo}–{hi}{unit}" if lo is not None else "")
            )
            if not in_range:
                out_of_range.append(sid)
            by_type.setdefault(stype, []).append((sid, value))

        for stype, readings in by_type.items():
            if len(readings) >= 2:
                vals = [v for _, v in readings]
                spread = max(vals) - min(vals)
                if spread > self.CONFLICT_THRESHOLD:
                    ids = [s for s, _ in readings]
                    unit = UNITS.get(stype, "")
                    lines.append(
                        f"\n  CONFLICT: {stype} sensors {ids} differ by "
                        f"{spread:.1f}{unit} (>{self.CONFLICT_THRESHOLD}{unit})"
                        f" — manual verification advised"
                    )

        lines.append("")
        lines.append(
            f"  Sensors outside normal range: {out_of_range}"
            if out_of_range
            else "  All sensors within normal range."
        )
        return "\n".join(lines)

    # --- Scenario 2 ---

    def decompose_with_llm(self, request: str) -> list[dict]:
        """
        Call the LLM with the thermostat request and available tools.
        Expect a JSON array of role specs: controller + monitor.
        The LLM decides which roles are needed; the orchestrator code
        does the actual spawning.
        """
        thermostat_tools = [
            t for t in self.all_tools
            if t["name"] in ("set_thermostat", "read_thermostat")
        ]
        tool_desc = "\n".join(
            f"  - {t['name']}: {t['description']}" for t in thermostat_tools
        )
        system = (
            "You are an IoT agent orchestrator. Given a user request and available "
            "tools, output a JSON array of agent roles to create. Each entry must have:\n"
            "  role: 'controller' (actuates the device once) or "
            "'monitor' (polls until goal is met)\n"
            "  tool: the MCP tool name this agent will use\n"
            "  room_id: room identifier in lowercase (e.g. 'office')\n"
            "  target_temp: numeric target temperature in °C\n"
            "Output ONLY a valid JSON array. No markdown, no explanation."
        )
        response = self.llm.call(
            system=system,
            messages=[{
                "role": "user",
                "content": f"Request: {request}\nAvailable tools:\n{tool_desc}",
            }],
            tools=None,
            max_tokens=256,
        )
        raw = next((b.text for b in response.content if b.type == "text"), "[]")
        raw = re.sub(r"```json?\s*|\s*```", "", raw).strip()
        try:
            roles = json.loads(raw)
            print("[Orchestrator] Roles parsed from LLM output")
            return roles
        except json.JSONDecodeError:
            print("[Orchestrator] LLM output could not be parsed — using fallback roles")
            return [
                {"role": "controller", "tool": "set_thermostat",
                 "room_id": "office", "target_temp": 28.0},
                {"role": "monitor", "tool": "read_thermostat",
                 "room_id": "office", "target_temp": 28.0},
            ]

    def spawn_controller(self, role_spec: dict) -> AgentSession:
        """Spawn a controller AgentSession scoped to set_thermostat only."""
        room_id = role_spec["room_id"]
        target = role_spec["target_temp"]
        session = AgentSession(
            label=f"controller:{room_id}",
            system_prompt=(
                f"You control the thermostat for room '{room_id}'. "
                f"Use set_thermostat to set the target temperature to {target}°C. "
                f"Confirm when done."
            ),
            tools=[t for t in self.all_tools if t["name"] == "set_thermostat"],
        )
        self.sessions.append(session)
        return session

    def spawn_monitor(self, role_spec: dict) -> MonitorSession:
        """Spawn a MonitorSession for the same room and target."""
        return MonitorSession(
            room_id=role_spec["room_id"],
            target_temp=float(role_spec["target_temp"]),
        )

    def summarize_thermostat(self, request: str, monitor_result: dict) -> str:
        """Ask the LLM to write a 1–2 sentence summary of the control outcome."""
        context = (
            f"Request: '{request}'\n"
            f"Outcome: {'target reached' if monitor_result['reached'] else 'timed out'}\n"
            f"Final temperature: {monitor_result['final_temp']}°C "
            f"after {monitor_result['elapsed_seconds']:.0f} seconds\n"
            f"Readings (elapsed_s, °C): {monitor_result['readings']}"
        )
        response = self.llm.call(
            system="Summarize this IoT thermostat control outcome in 1–2 concise sentences.",
            messages=[{"role": "user", "content": context}],
            tools=None,
            max_tokens=128,
        )
        return next((b.text for b in response.content if b.type == "text"), "")


# ---------------------------------------------------------------------------
# Session runners — each session gets its own MCP Client / subprocess
# ---------------------------------------------------------------------------

async def run_session(
    session: AgentSession,
    llm: LLM,
    server_params: StdioServerParameters,
    user_message: str,
) -> dict:
    t_start = time.monotonic()
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp_client:
                await mcp_client.initialize()
                await session.run(llm, mcp_client, user_message)
    except TimeoutError:
        # MCP SDK raises TimeoutError when a subprocess exits cleanly before
        # the SDK's own teardown completes. The result was already stored in
        # session.result by AgentSession.run(), so we can safely ignore this.
        pass
    t_end = time.monotonic()
    result = session.result or {}
    result["_t_start"] = t_start
    result["_t_end"] = t_end
    return result


async def run_monitor(
    monitor_session: MonitorSession,
    server_params: StdioServerParameters,
) -> dict:
    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as mcp_client:
                await mcp_client.initialize()
                await monitor_session.run(mcp_client)
    except TimeoutError:
        pass
    return monitor_session.result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    server_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_server.py"],
        env=dict(os.environ),
    )

    # ---- Discovery (one temporary connection, shared between both scenarios) ----
    print("=" * 60)
    print("IoT MCP Agent — Illustrative Implementation")
    print(f"Model: {MODEL}")
    print("=" * 60)

    print("\n[Discovery] Connecting to MCP server ...")
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as disc:
            await disc.initialize()
            tools_result = await disc.list_tools()
            all_tools = [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema,
                }
                for t in tools_result.tools
            ]
            print(f"             Tools  : {[t['name'] for t in all_tools]}")

            reg_result = await disc.call_tool("list_sensors", {})
            sensor_registry = json.loads(reg_result.content[0].text)
            print(f"             Sensors: {list(sensor_registry)}")

    llm = LLM()
    orchestrator = AgentOrchestrator(llm, all_tools)

    # ================================================================
    # SCENARIO 1 — Environmental check (code-driven decomposition)
    # ================================================================
    print("\n\n" + "=" * 60)
    print("Scenario 1: Environmental Check")
    print("=" * 60)
    print(f'\nRequest: "{SENSOR_REQUEST}"')

    subtasks = orchestrator.decompose(sensor_registry)
    print(f"\n[Orchestrator] Decomposed into {len(subtasks)} sub-task(s) "
          f"(code-driven, from device registry):")
    for st in subtasks:
        print(f"               · {st['sensor_id']} ({st['sensor_type']})")

    sessions_s1 = [orchestrator.spawn_session(st) for st in subtasks]
    print(f"\n[Orchestrator] Spawned {len(sessions_s1)} AgentSession(s); "
          f"each gets its own MCP Client")

    print("\n[Agents] Running concurrently ...")
    sensor_results = list(await asyncio.gather(*[
        run_session(
            s, llm, server_params,
            f"Read the current {s.sensor_type} from sensor {s.sensor_id}. "
            f"Report the exact numeric value and unit only.",
        )
        for s in sessions_s1
    ]))

    # Concurrency timing — all timestamps are time.monotonic() seconds,
    # offset relative to the earliest session start so the numbers are readable.
    t0 = min(r["_t_start"] for r in sensor_results)
    print("\n[Concurrency] Session timing (seconds from first session start):")
    print(f"  {'Session':<12} {'Start':>7}  {'End':>7}  {'Duration':>9}")
    for r in sensor_results:
        rel_start = r["_t_start"] - t0
        rel_end   = r["_t_end"]   - t0
        duration  = r["_t_end"]   - r["_t_start"]
        print(f"  {r['sensor_id']:<12} {rel_start:>7.3f}  {rel_end:>7.3f}  {duration:>8.3f}s")
    earliest_end = min(r["_t_end"] for r in sensor_results)
    latest_start = max(r["_t_start"] for r in sensor_results)
    if latest_start < earliest_end:
        overlap = earliest_end - latest_start
        print(f"  → Overlap confirmed: all sessions active simultaneously for ≥{overlap:.3f}s")
    else:
        print("  → No overlap detected: sessions appear sequential.")

    print("\n[Concurrency] First LLM call window per session (seconds from first session start):")
    print(f"  {'Session':<12} {'LLM Start':>10}  {'LLM End':>8}  {'Duration':>9}")
    llm_starts = []
    llm_ends = []
    for r in sensor_results:
        wins = r.get("llm_windows", [])
        if wins:
            w_start, w_end = wins[0]
            llm_starts.append(w_start)
            llm_ends.append(w_end)
            print(f"  {r['sensor_id']:<12} {w_start - t0:>10.3f}  {w_end - t0:>8.3f}  {w_end - w_start:>8.3f}s")
    if llm_starts and llm_ends:
        llm_latest_start = max(llm_starts)
        llm_earliest_end = min(llm_ends)
        if llm_latest_start < llm_earliest_end:
            llm_overlap = llm_earliest_end - llm_latest_start
            print(f"  → LLM calls overlapped for ≥{llm_overlap:.3f}s (non-blocking)")
        else:
            print("  → LLM calls did not overlap (event loop likely blocked)")

    print("\n[Agents] Raw MCP results:")
    for r in sensor_results:
        print(f"         {r['sensor_id']}: {r['raw']}")

    print("\n[Orchestrator] Evaluating against normal ranges ...")
    print("\n" + orchestrator.aggregate(sensor_results))

    # ================================================================
    # SCENARIO 2 — Thermostat control (LLM-driven decomposition)
    # ================================================================
    print("\n\n" + "=" * 60)
    print("Scenario 2: Thermostat Control")
    print("=" * 60)
    print(f'\nRequest: "{THERMOSTAT_REQUEST}"')

    # Clear any stale state from a previous run so heating starts fresh.
    stale = os.path.join(tempfile.gettempdir(), "iot_thermo_office.json")
    if os.path.exists(stale):
        os.remove(stale)

    # LLM-driven decomposition: orchestrator calls LLM, parses JSON role list.
    print("\n[Orchestrator] Calling LLM to decompose request into agent roles ...")
    roles = orchestrator.decompose_with_llm(THERMOSTAT_REQUEST)
    print(f"[Orchestrator] {len(roles)} role(s) returned by LLM:")
    for r in roles:
        print(f"               · {r['role']} — tool: {r['tool']}, "
              f"room: {r['room_id']}, target: {r['target_temp']}°C")

    ctrl_spec = next(r for r in roles if r["role"] == "controller")
    mon_spec = next(r for r in roles if r["role"] == "monitor")
    room_id = ctrl_spec["room_id"]

    controller_session = orchestrator.spawn_controller(ctrl_spec)
    monitor_session = orchestrator.spawn_monitor(mon_spec)
    print(f"\n[Orchestrator] Spawned controller + monitor agents for room '{room_id}'")

    # Both run concurrently; each has its own MCP Client (separate subprocess).
    # MockThermostat shares state via a temp file so the monitor can read what
    # the controller wrote, even though they are in separate processes.
    ctrl_task = asyncio.create_task(
        run_session(
            controller_session, llm, server_params,
            f"Set the {room_id} thermostat to {ctrl_spec['target_temp']}°C "
            f"using set_thermostat.",
        )
    )
    mon_task = asyncio.create_task(run_monitor(monitor_session, server_params))

    print("[Orchestrator] Controller and monitor running concurrently ...\n")

    # Wait for monitor to signal completion (target reached or timeout).
    monitor_result = await mon_task
    t_mon_end = time.monotonic()
    if monitor_result["reached"]:
        _mon_status = f"reached — final {monitor_result['final_temp']}°C after {monitor_result['elapsed_seconds']:.0f}s"
    else:
        _mon_status = f"not reached (timeout) — final {monitor_result['final_temp']}°C after {monitor_result['elapsed_seconds']:.0f}s"
    print(f"\n[Monitor] {monitor_session.target_temp}°C {_mon_status} — signalling orchestrator.")

    # Orchestrator terminates the controller agent.
    _ctrl_cancelled = False
    if not ctrl_task.done():
        ctrl_task.cancel()
        try:
            await ctrl_task
        except asyncio.CancelledError:
            _ctrl_cancelled = True
            print("[Orchestrator] Controller agent cancelled.")
    else:
        ctrl_result = ctrl_task.result()
        print(f"[Orchestrator] Controller already completed: {ctrl_result['raw']}")

    if _ctrl_cancelled:
        print("[Orchestrator] Cancellation outcome: CANCELLED (controller was still active)")
    else:
        lead = t_mon_end - ctrl_result["_t_end"]
        print(
            f"[Orchestrator] Cancellation outcome: NOT NEEDED "
            f"(controller completed {lead:.1f}s before monitor)"
        )

    # LLM writes the human-readable summary.
    print("\n[Orchestrator] Requesting final summary from LLM ...")
    summary = orchestrator.summarize_thermostat(THERMOSTAT_REQUEST, monitor_result)
    print(f"\n[LLM] {summary}")

    print("\n" + "=" * 60)
    print("Run complete.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
