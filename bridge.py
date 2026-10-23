"""Agent voice bridge: phone calls (Telnyx or Twilio) -> Dialt voice agent -> OpenClaw / Hermes.

Built on dialt-recipes: both phone providers share `dialt_recipes.twilio.run_call_bridge` (audio
pacing, barge-in, tool dispatch). Only webhook verification, answering and hangup differ per
provider. A provider is enabled when its credentials are in the environment (/opt/agent-voice/.env).

Agents are Dialt *background* tools: the voice agent tells the caller it is on it and keeps
talking; the agent's answer is spoken when it arrives. See agents.py for how agents are reached.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import NAMESPACE_URL, uuid5

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dialt import DEFAULT_REALTIME_URL, DialtMode
from dialt_recipes.telnyx import TelnyxMediaSocket
from dialt_recipes.telnyx import receive_stream_start as telnyx_stream_start
from dialt_recipes.telnyx import signature_is_valid as telnyx_signature_is_valid
from dialt_recipes.twilio import (
    BridgeHooks, ToolError, TwilioBridgeSettings, connect_stream_twiml, run_call_bridge,
    twilio_signature_is_valid,
)
from dialt_recipes.twilio import receive_stream_start as twilio_stream_start
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

import agents

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("agent_voice")

DEFAULT_INSTRUCTIONS = (Path(__file__).parent / "instructions.md").read_text().strip()
# The opener, set and reset from the hostmyagents dashboard (over SSH). Read on every call, so a
# change needs no restart and never drops a call in progress. Absent or empty = the default.
GREETING_FILE = Path(os.environ.get("VOICE_GREETING_FILE", "/opt/agent-voice/greeting.txt"))
GREETING_MAX_CHARS = 300  # Dialt's cap on a session greeting


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    dialt_api_key: str
    public_base_url: str
    dialt_url: str
    voice: str | None
    greeting: str | None          # None = generated from the agent list
    allowed_callers: frozenset[str]  # empty = anyone may call (allowlist off)
    max_calls: int
    max_call_seconds: int

    def http_url(self, path: str) -> str:
        return self.public_base_url + path

    def ws_url(self, path: str) -> str:
        return self.public_base_url.replace("https://", "wss://", 1) + path


def load_config() -> Config:
    base = _env("PUBLIC_BASE_URL").rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.path or parsed.query:
        raise RuntimeError("PUBLIC_BASE_URL must be an HTTPS origin, e.g. https://agents.example.com")
    if not _env("DIALT_API_KEY"):
        raise RuntimeError("DIALT_API_KEY is required")
    callers = frozenset(n.strip().replace(" ", "") for n in _env("VOICE_ALLOWED_CALLERS").split(",") if n.strip())
    return Config(dialt_api_key=_env("DIALT_API_KEY"), public_base_url=base,
                  dialt_url=_env("DIALT_URL") or DEFAULT_REALTIME_URL, voice=_env("DIALT_VOICE") or None,
                  greeting=_env("VOICE_GREETING") or None, allowed_callers=callers,
                  max_calls=int(_env("VOICE_MAX_CALLS", "3")),
                  max_call_seconds=int(_env("VOICE_MAX_CALL_SECONDS", "1200")))


CONFIG = load_config()
TELNYX_ON = bool(_env("TELNYX_API_KEY") and _env("TELNYX_PUBLIC_KEY"))
TWILIO_ON = bool(_env("TWILIO_AUTH_TOKEN"))
if not (TELNYX_ON or TWILIO_ON):
    raise RuntimeError("configure a phone provider: TELNYX_API_KEY + TELNYX_PUBLIC_KEY, or TWILIO_AUTH_TOKEN")
if TELNYX_ON:
    Ed25519PublicKey.from_public_bytes(base64.b64decode(_env("TELNYX_PUBLIC_KEY"), validate=True))
logger.info("providers: telnyx=%s twilio=%s mode=%s allowlist=%s", TELNYX_ON, TWILIO_ON, agents.MODE,
            "on" if CONFIG.allowed_callers else "off (anyone may call)")

ACTIVE_CALLS: set[str] = set()


def caller_allowed(number: str | None) -> bool:
    return not CONFIG.allowed_callers or (number or "").replace(" ", "") in CONFIG.allowed_callers


# --- the conversation: Dialt mode built from the agents reachable right now ------------------

def tool_manifest(found: list[agents.Agent]) -> list[dict]:
    return [{
        "name": a.tool, "status_label": f"Asking {a.name}",
        "description": f"Send a task or question to the caller's {a.kind.capitalize()} agent '{a.name}'. "
                       "It can browse, use tools and take minutes, so it runs in the background "
                       "while you keep talking.",
        "parameters": {"type": "object", "required": ["request"], "properties": {
            "request": {"type": "string", "description": "Full, self-contained instruction for the agent."}}},
        "deferred": True, "deferred_timeout": 1800, "notify_on_complete": True, "timeout": 10,
    } for a in found]


def instructions(found: list[agents.Agent]) -> str:
    if not found:
        roster = "No agents are reachable right now: apologise and suggest calling back later."
    else:
        roster = "The caller's agents (use the matching tool):\n" + "\n".join(
            f"- {a.name} ({a.kind.capitalize()}): {a.tool}" for a in found)
        if len(found) > 1:
            roster += ("\nIf it is not clear which agent the caller wants, ask them to choose. "
                       "They may switch agents at any time during the call.")
    return f"{_env('VOICE_INSTRUCTIONS') or DEFAULT_INSTRUCTIONS}\n\n{roster}"


def custom_greeting() -> str | None:
    try:
        text = " ".join(GREETING_FILE.read_text().split())
    except OSError:
        text = ""
    return (text or CONFIG.greeting or "")[:GREETING_MAX_CHARS] or None


def greeting(found: list[agents.Agent]) -> str:
    # The bridge always sends its own opener (an inline mode), so a greeting saved on dialt.com
    # never applies to these calls; this is the place to change it.
    if custom := custom_greeting():
        return custom
    if len(found) > 1:
        names = ", ".join(a.name for a in found[:-1]) + f" or {found[-1].name}"
        return f"Hi, you've reached your agents. Would you like {names}?"
    if found:
        return f"Hi, you're through to {found[0].name}. What can I do for you?"
    return "Hi. Sorry, none of your agents are available right now."


async def bridge_call(provider: str, socket, stream_id: str, call_id: str, settings) -> None:
    """Run one call through the shared engine with agent tools, deferral, caps and transcript logs."""
    session_id = hashlib.sha256(f"{provider}:{call_id}".encode()).hexdigest()
    agent_session = f"voice-{session_id[:16]}"
    found = await agents.discover()
    by_tool = {a.tool: a for a in found}
    live: dict = {}
    end_call = asyncio.Event()

    async def run_tool(name: str, args: dict):
        agent = by_tool.get(name)
        if agent is None:
            raise ToolError("that agent is not available")
        request = str(args.get("request", ""))[:4000]
        logger.info("[%s] -> %s: %s", provider, agent.name, request[:200])
        reply = await agents.ask(agent, request, agent_session)
        logger.info("[%s] <- %s: %s", provider, agent.name, reply[:300])
        return {"reply": reply[:3000]}

    async def on_connected(session):
        live["session"] = session

    async def on_event(event):
        d = event.data
        if event.type == "asr":
            logger.info("[%s] CALLER: %s", provider, str(d.get("text", ""))[:200])
        elif event.type == "conversation_item" and d.get("item", {}).get("role") == "assistant":
            logger.info("[%s] VOICE: %s", provider, str(d["item"].get("text", ""))[:200])
        elif event.type == "tool_call" and d.get("name") in by_tool:
            tool_id = str(d["id"])  # detach the agent job so the caller can keep talking
            await live["session"].send_tool_deferred(tool_id, f"{session_id[:16]}-{tool_id}",
                                                     status_label=f"Asking {by_tool[d['name']].name}")
        elif event.type in ("error", "tool_job_delivery_failed"):
            logger.warning("[%s] %s %s", provider, event.type, str(d)[:200])

    async def call_timer():
        await asyncio.sleep(CONFIG.max_call_seconds)
        logger.info("[%s] max call length reached; ending call", provider)
        end_call.set()

    timer = asyncio.create_task(call_timer())
    ACTIVE_CALLS.add(session_id)
    try:
        mode = DialtMode(instructions=instructions(found), voice=CONFIG.voice, greeting=greeting(found),
                         end_call=True, tools=tool_manifest(found) or None)
        await run_call_bridge(socket, stream_id, session_id[:64], settings=settings, mode=mode,
                              hooks=BridgeHooks(execute_tool=run_tool, on_connected=on_connected,
                                                on_event=on_event, end_call=end_call))
    finally:
        timer.cancel()
        ACTIVE_CALLS.discard(session_id)


app = FastAPI(title="Agent voice bridge")


@app.get("/healthz")
async def health() -> dict:
    return {"status": "ok", "telnyx": TELNYX_ON, "twilio": TWILIO_ON, "active_calls": len(ACTIVE_CALLS)}


# --- Telnyx: Call Control + bidirectional media streaming --------------------------------------

@dataclass(frozen=True)
class TelnyxSettings:
    dialt_api_key: str
    dialt_url: str


async def telnyx_command(call_id: str, action: str, **params) -> None:
    command_id = str(uuid5(NAMESPACE_URL, f"dialt:{call_id}:{action}"))  # dedupes webhook retries
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"https://api.telnyx.com/v2/calls/{quote(call_id, safe='')}/actions/{action}",
                              headers={"Authorization": f"Bearer {_env('TELNYX_API_KEY')}"},
                              json={"command_id": command_id, **params})
        if action in ("hangup", "reject") and r.status_code == 422:
            return  # the call already ended
        r.raise_for_status()


def telnyx_stream_params() -> dict:
    return {"stream_url": CONFIG.ws_url("/telnyx/media"), "stream_track": "inbound_track",
            "stream_codec": "PCMU", "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU", "stream_bidirectional_target_legs": "self"}


@app.post("/telnyx/voice")
async def telnyx_voice(request: Request) -> Response:
    if not TELNYX_ON:
        raise HTTPException(404)
    raw = await request.body()
    if not telnyx_signature_is_valid(_env("TELNYX_PUBLIC_KEY"), raw, request.headers.get("telnyx-timestamp"),
                                     request.headers.get("telnyx-signature-ed25519")):
        raise HTTPException(403, "invalid Telnyx signature")
    event = json.loads(raw)["data"]
    payload = event["payload"]
    try:
        if event["event_type"] == "call.initiated" and payload.get("direction") == "incoming":
            caller = payload.get("from")
            if not caller_allowed(caller) or len(ACTIVE_CALLS) >= CONFIG.max_calls:
                logger.info("[telnyx] rejecting call from %s (allowlist or call cap)", caller)
                await telnyx_command(payload["call_control_id"], "reject", cause="CALL_REJECTED")
            else:
                logger.info("[telnyx] answering call from %s", caller)
                await telnyx_command(payload["call_control_id"], "answer", **telnyx_stream_params())
        elif event["event_type"] == "streaming.failed":
            await telnyx_command(payload["call_control_id"], "hangup")
    except httpx.HTTPError:
        logger.exception("Telnyx call command failed")
        raise HTTPException(503) from None
    return Response(status_code=200)


@app.websocket("/telnyx/media")
async def telnyx_media(websocket: WebSocket) -> None:
    if not TELNYX_ON or not telnyx_signature_is_valid(
            _env("TELNYX_PUBLIC_KEY"), CONFIG.ws_url("/telnyx/media").encode(),
            websocket.headers.get("telnyx-timestamp"), websocket.headers.get("telnyx-signature-ed25519")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    call_id = None
    try:
        async with asyncio.timeout(10):
            stream_id, call_id, media_format = await telnyx_stream_start(websocket)
        if media_format != {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}:
            raise ValueError("Telnyx stream requires mono PCMU at 8000 Hz")
        await bridge_call("telnyx", TelnyxMediaSocket(websocket), stream_id, call_id,
                          TelnyxSettings(CONFIG.dialt_api_key, CONFIG.dialt_url))
    except (WebSocketDisconnect, ConnectionError):
        pass
    except Exception:
        logger.exception("Telnyx bridge failed")
    finally:
        if call_id:  # closing the media stream alone leaves the phone call up
            try:
                await telnyx_command(call_id, "hangup")
            except httpx.HTTPError:
                logger.exception("Telnyx hangup failed")
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


# --- Twilio: TwiML <Connect><Stream> + Media Streams -------------------------------------------

def twilio_settings() -> TwilioBridgeSettings:
    return TwilioBridgeSettings(dialt_api_key=CONFIG.dialt_api_key, twilio_auth_token=_env("TWILIO_AUTH_TOKEN"),
                                public_base_url=CONFIG.public_base_url, dialt_url=CONFIG.dialt_url)


def twilio_twiml() -> str:
    return connect_stream_twiml(twilio_settings(), media_path="/twilio/media", status_path="/twilio/stream-status")


async def _twilio_form(request: Request, path: str) -> dict:
    form = dict(await request.form())
    if not twilio_signature_is_valid(_env("TWILIO_AUTH_TOKEN"), CONFIG.http_url(path), form,
                                     request.headers.get("x-twilio-signature")):
        raise HTTPException(403, "invalid Twilio signature")
    return form


@app.post("/twilio/voice")
async def twilio_voice(request: Request) -> Response:
    if not TWILIO_ON:
        raise HTTPException(404)
    form = await _twilio_form(request, "/twilio/voice")
    caller = form.get("From")
    if form.get("Direction", "inbound") == "inbound" and (
            not caller_allowed(caller) or len(ACTIVE_CALLS) >= CONFIG.max_calls):
        logger.info("[twilio] rejecting call from %s (allowlist or call cap)", caller)
        return Response("<Response><Reject /></Response>", media_type="application/xml")
    logger.info("[twilio] connecting call %s from %s", form.get("CallSid"), caller)
    return Response(twilio_twiml(), media_type="application/xml")


@app.post("/twilio/stream-status", status_code=204)
async def twilio_stream_status(request: Request) -> Response:
    if not TWILIO_ON:
        raise HTTPException(404)
    form = await _twilio_form(request, "/twilio/stream-status")
    logger.info("[twilio] stream %s %s %s", form.get("StreamEvent"), form.get("CallSid"), form.get("StreamError") or "")
    return Response(status_code=204)


@app.websocket("/twilio/media")
async def twilio_media(websocket: WebSocket) -> None:
    if not TWILIO_ON or not twilio_signature_is_valid(
            _env("TWILIO_AUTH_TOKEN"), CONFIG.ws_url("/twilio/media"), {},
            websocket.headers.get("x-twilio-signature")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        start = await twilio_stream_start(websocket)
        stream_sid = str(start["streamSid"])
        await bridge_call("twilio", websocket, stream_sid, str(start.get("callSid") or stream_sid), twilio_settings())
    except (WebSocketDisconnect, ConnectionError):
        pass
    except Exception:
        logger.exception("Twilio bridge failed")
    finally:  # Twilio ends the call itself: <Hangup/> follows the <Connect>
        try:
            await websocket.close()
        except (RuntimeError, WebSocketDisconnect):
            pass
