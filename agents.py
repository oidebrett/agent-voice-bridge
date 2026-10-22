"""Which agents a call can reach, and how to ask them something.

Every agent is reached through its own CLI, never through a network API, so no agent port or
key is exposed and nothing inside a sandbox changes:

- OpenClaw: `openclaw agent --session-id <call> --message ... --json`
- Hermes:   `hermes chat -q ... -Q`

On an AgentGateway the CLI runs inside the sandbox via `openshell sandbox exec`; on a standalone
OpenClaw or Hermes box it runs on the host.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
from dataclasses import dataclass

MODE = os.environ.get("VOICE_MODE", "agentgateway").strip()  # agentgateway | openclaw | hermes
AGENT_TIMEOUT_S = int(os.environ.get("VOICE_AGENT_TIMEOUT_S", "900"))
NEMOCLAW_REGISTRY = os.environ.get("NEMOCLAW_REGISTRY", "/root/.nemoclaw/sandboxes.json")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CLI_PATH = "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"


@dataclass(frozen=True)
class Agent:
    tool: str            # Dialt tool name, e.g. ask_ivos_openclaw
    name: str            # what the caller says / hears
    kind: str            # openclaw | hermes
    sandbox: str | None  # None on a standalone box


def _tool_name(name: str) -> str:
    return "ask_" + (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "agent")[:48]


def _which(binary: str, *fallbacks: str) -> str:
    return shutil.which(binary, path=CLI_PATH) or next((f for f in fallbacks if os.path.exists(f)), binary)


async def _run(argv: list[str], env: dict[str, str] | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env={**os.environ, "HOME": "/root", "PATH": CLI_PATH, **(env or {})})
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=AGENT_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return out.decode(errors="replace")


async def _ready_sandboxes() -> set[str]:
    out = await _run([_which("openshell", "/usr/bin/openshell"), "sandbox", "list"])
    ready = set()
    for line in ANSI.sub("", out).splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 2 and cols[-1] == "Ready":
            ready.add(cols[0])
    return ready


async def discover() -> list[Agent]:
    """Agents reachable right now. Re-run per call so new sandboxes appear without a restart."""
    label = os.environ.get("VOICE_AGENT_NAME", "").strip()
    if MODE == "openclaw":
        name = label or "OpenClaw"
        return [Agent(_tool_name(name), name, "openclaw", None)]
    if MODE == "hermes":
        name = label or "Hermes"
        return [Agent(_tool_name(name), name, "hermes", None)]
    try:
        with open(NEMOCLAW_REGISTRY) as f:
            registry = json.load(f)
    except (OSError, ValueError):
        return []
    entries = registry.get("sandboxes", registry) if isinstance(registry, dict) else {}
    ready = await _ready_sandboxes()  # the registry can hold sandboxes that were deleted
    agents = []
    for sandbox, entry in sorted(entries.items()):
        kind = (entry or {}).get("agent") if isinstance(entry, dict) else None
        if sandbox in ready and kind in ("openclaw", "hermes"):
            agents.append(Agent(_tool_name(sandbox), sandbox, kind, sandbox))
    return agents


def _openclaw_cmd(request: str, session: str) -> str:
    return ("openclaw agent --agent main --session-id " + shlex.quote(session)
            + " --message " + shlex.quote(request) + " --json 2>/dev/null")


def _hermes_cmd(request: str) -> str:
    return "hermes chat -Q -q " + shlex.quote(request) + " 2>/dev/null"


def _openclaw_reply(raw: str) -> str:
    data = json.loads(raw[raw.index("{"):])
    payloads = (data.get("result") or {}).get("payloads") or []
    return "\n".join(p.get("text") or "" for p in payloads).strip()


def _hermes_reply(raw: str) -> str:
    # -Q still prints the session id and config warnings (e.g. "Warning: Unknown toolsets: ...").
    lines = [l for l in raw.strip().splitlines() if not l.startswith(("session_id:", "Warning:"))]
    return "\n".join(lines).strip()


async def ask(agent: Agent, request: str, session: str) -> str:
    """Send one request to an agent and return its text reply."""
    if agent.kind == "openclaw":
        inner, parse = _openclaw_cmd(request, session), _openclaw_reply
    else:
        inner, parse = _hermes_cmd(request), _hermes_reply
    if agent.sandbox:
        argv = [_which("openshell", "/usr/bin/openshell"), "sandbox", "exec", "-n", agent.sandbox, "--",
                "sh", "-c", "cd /sandbox && HOME=/sandbox " + inner]
    else:
        argv = ["sh", "-c", "cd /root && " + inner]
    return parse(await _run(argv)) or "(the agent returned no text)"
