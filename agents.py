"""Which agents a call can reach, and how to ask them something.

Every agent is reached through its own CLI, never through a network API, so no agent port or
key is exposed and nothing inside a sandbox changes:

- OpenClaw: `openclaw agent --session-id <call> --message ... --json`
- Hermes:   `hermes chat -q ... -Q`

Where the CLI runs depends on the backend (VOICE_MODE, a comma-separated list, so one number can
reach agents on several backends of the same box, e.g. `agentgateway,oce`):

- agentgateway: inside the sandbox via `openshell sandbox exec`;
- openclaw / hermes: on the host of a standalone box;
- oce (OpenClaw Enterprise): inside the Agent's gateway pod via `kubectl exec`. Agents are listed
  from the OCE API with a read-only service key; only embedded OpenClaw Agents that are deployed
  and running are offered.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import urllib.request
from dataclasses import dataclass

MODES = [m.strip() for m in os.environ.get("VOICE_MODE", "agentgateway").split(",") if m.strip()]
MODE = ",".join(MODES)  # agentgateway | openclaw | hermes | oce, comma-separated
AGENT_TIMEOUT_S = int(os.environ.get("VOICE_AGENT_TIMEOUT_S", "900"))
NEMOCLAW_REGISTRY = os.environ.get("NEMOCLAW_REGISTRY", "/root/.nemoclaw/sandboxes.json")
ANSI = re.compile(r"\x1b\[[0-9;]*m")
CLI_PATH = "/root/.local/bin:/usr/local/bin:/usr/bin:/bin"

# OpenClaw Enterprise: the controller API, a read-only service key, and the cluster's kubeconfig.
OCE_URL = os.environ.get("OCE_URL", "").rstrip("/")  # the OCE controller API (control plane, not an agent)
OCE_SERVICE_KEY_FILE = os.environ.get("OCE_SERVICE_KEY_FILE", "/opt/agent-voice/oce-service-key.json")
OCE_KUBECONFIG = os.environ.get("OCE_KUBECONFIG", "/opt/agent-voice/oce-kubeconfig")
OCE_GATEWAY_CONTAINER = os.environ.get("OCE_GATEWAY_CONTAINER", "gateway")


@dataclass(frozen=True)
class Agent:
    tool: str                 # Dialt tool name, e.g. ask_ivos_openclaw
    name: str                 # what the caller says / hears
    kind: str                 # openclaw | hermes
    sandbox: str | None       # AgentGateway sandbox; None elsewhere
    oce_agent: str | None = None  # OpenClaw Enterprise Agent ID (agt_...)


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


async def _agentgateway_agents() -> list[Agent]:
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


def _oce_get(path: str) -> dict:
    if not OCE_URL:
        raise OSError("OCE_URL is not set")
    with open(OCE_SERVICE_KEY_FILE) as f:
        key = json.load(f)["data"]["key"]
    req = urllib.request.Request(OCE_URL + path, headers={"x-api-key": key, "accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


async def _oce_agents() -> list[Agent]:
    """Embedded OpenClaw Agents with a deployed version that are meant to be running."""
    def fetch() -> list[Agent]:
        found = []
        for ns in _oce_get("/namespaces").get("data", []):
            if ns.get("status") != "ready":
                continue
            for a in _oce_get(f"/namespaces/{ns['id']}/agents").get("data", []):
                if (a.get("status") == "active" and a.get("activeRevisionId")
                        and a.get("desiredRuntimeState") == "running" and a.get("executionMode") == "embedded"):
                    found.append(Agent(_tool_name(a["name"]), a["name"], "openclaw", None, a["id"]))
        return found
    try:
        return await asyncio.to_thread(fetch)
    except (OSError, ValueError, KeyError):  # OCE down, key missing or expired: offer no OCE agents
        return []


def _unique(agents: list[Agent]) -> list[Agent]:
    """Dialt tool names must be unique; two agents can sanitise to the same name."""
    seen: set[str] = set()
    out = []
    for a in agents:
        tool, n = a.tool, 2
        while tool in seen:
            tool, n = f"{a.tool[:45]}_{n}", n + 1
        seen.add(tool)
        out.append(a if tool == a.tool else Agent(tool, a.name, a.kind, a.sandbox, a.oce_agent))
    return out


async def discover() -> list[Agent]:
    """Agents reachable right now. Re-run per call so new sandboxes appear without a restart."""
    label = os.environ.get("VOICE_AGENT_NAME", "").strip()
    found: list[Agent] = []
    for mode in MODES:
        if mode == "openclaw":
            name = label or "OpenClaw"
            found.append(Agent(_tool_name(name), name, "openclaw", None))
        elif mode == "hermes":
            name = label or "Hermes"
            found.append(Agent(_tool_name(name), name, "hermes", None))
        elif mode == "agentgateway":
            found += await _agentgateway_agents()
        elif mode == "oce":
            found += await _oce_agents()
    return _unique(found)


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


def _kubectl(*args: str) -> list[str]:
    return [_which("kubectl", "/opt/oce/tools/bin/kubectl"), "--kubeconfig", OCE_KUBECONFIG, *args]


async def _oce_gateway_pod(agent_id: str) -> tuple[str, str] | None:
    """(Kubernetes namespace, pod) of the Agent's running gateway; looked up per request because
    every deployment of a new version replaces the pod."""
    out = await _run(_kubectl("get", "pods", "-A", "-l", f"openclaw.dev/agent={agent_id},openclaw.dev/workload-role=gateway",
                              "--field-selector", "status.phase=Running", "-o", "json"))
    try:
        items = json.loads(out).get("items", [])
    except ValueError:
        return None
    for pod in items:
        names = [c.get("name") for c in pod.get("spec", {}).get("containers", [])]
        if OCE_GATEWAY_CONTAINER in names and not pod["metadata"].get("deletionTimestamp"):
            return pod["metadata"]["namespace"], pod["metadata"]["name"]
    return None


async def ask(agent: Agent, request: str, session: str) -> str:
    """Send one request to an agent and return its text reply."""
    if agent.kind == "openclaw":
        inner, parse = _openclaw_cmd(request, session), _openclaw_reply
    else:
        inner, parse = _hermes_cmd(request), _hermes_reply
    if agent.oce_agent:
        target = await _oce_gateway_pod(agent.oce_agent)
        if target is None:
            return "(that agent is not running right now)"
        argv = _kubectl("exec", "-n", target[0], target[1], "-c", OCE_GATEWAY_CONTAINER, "--",
                        "sh", "-c", inner)
    elif agent.sandbox:
        argv = [_which("openshell", "/usr/bin/openshell"), "sandbox", "exec", "-n", agent.sandbox, "--",
                "sh", "-c", "cd /sandbox && HOME=/sandbox " + inner]
    else:
        argv = ["sh", "-c", "cd /root && " + inner]
    return parse(await _run(argv)) or "(the agent returned no text)"
