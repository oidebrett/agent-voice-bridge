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
import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from dialt import DEFAULT_REALTIME_URL, DialtMode
from dialt_recipes.telephony_audio import decode_mulaw, encode_mulaw
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
GREETING_FILE = Path(os.environ.get("VOICE_GREETING_FILE",
                                    os.path.join(os.environ.get("VOICE_DIR", "/opt/agent-voice"), "greeting.txt")))
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
    silence_nudge_s: float   # caller silent this long -> the voice checks in ("Are you still there?")
    silence_end_s: float     # still silent this long after the nudge -> sign off and hang up

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
                  max_call_seconds=int(_env("VOICE_MAX_CALL_SECONDS", "1200")),
                  silence_nudge_s=float(_env("VOICE_SILENCE_NUDGE_SECONDS", "8")),
                  silence_end_s=float(_env("VOICE_SILENCE_END_SECONDS", "30")))


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


# Longest the caller's audio is held back waiting for Dialt's greeting (stream start -> welcome
# turn was ~3 s on a real Telnyx call, 2026-10-23).
GREETING_HOLD_SECONDS = 6.0


class HoldCallerAudio:
    """Drop the caller's audio until Dialt starts the greeting.

    People answer with "Hello?". If Dialt hears that before its welcome turn starts, it skips
    the greeting and just replies "Hello! How can I help you today?" (real call 2026-10-23), so
    the caller never hears the greeting set on the dashboard. Once the greeting is under way
    (or after GREETING_HOLD_SECONDS), audio flows normally and the caller can interrupt.
    """

    def __init__(self, socket, released: asyncio.Event, dump=None, codec=None) -> None:
        self._socket, self._released = socket, released
        self.frames = 0   # caller media frames received from the phone provider (diagnostics)
        self._dump = dump  # VOICE_DEBUG_AUDIO: raw payload bytes, as received
        self._codec = codec  # CallerCodec, or None to pass payloads through untouched

    def __getattr__(self, name):
        return getattr(self._socket, name)

    async def iter_text(self):
        deadline = asyncio.get_running_loop().time() + GREETING_HOLD_SECONDS
        async for raw in self._socket.iter_text():
            if '"media"' in raw:
                self.frames += 1
                if self._dump is not None or self._codec is not None:
                    message = json.loads(raw)
                    payload = message.get("media", {}).get("payload")
                    if isinstance(payload, str):
                        data = base64.b64decode(payload)
                        if self._dump is not None:
                            self._dump.write(data)
                        if self._codec is not None:
                            fixed = self._codec.to_mulaw(data)
                            if fixed is not data:
                                message["media"]["payload"] = base64.b64encode(fixed).decode()
                                raw = json.dumps(message)
            if not self._released.is_set():
                if asyncio.get_running_loop().time() >= deadline:
                    self._released.set()
                elif json.loads(raw).get("event") == "media":
                    continue
            yield raw


def _alaw_to_mulaw_table() -> bytes:
    """G.711 A-law byte -> mu-law byte, via linear PCM (ITU-T G.711 A-law expansion)."""
    a = np.arange(256, dtype=np.int32) ^ 0x55
    exponent, mantissa = (a >> 4) & 0x07, a & 0x0F
    magnitude = np.where(exponent == 0, (mantissa << 4) + 8, ((mantissa << 4) + 0x108) << (exponent - 1))
    return encode_mulaw(np.where(a & 0x80, magnitude, -magnitude))


ALAW_TO_MULAW = _alaw_to_mulaw_table()


class CallerCodec:
    """Telnyx says PCMU but can send the caller leg's A-law untouched (2026-11-01, Irish mobile).

    Decoded as mu-law, A-law sounds mumbled and clipped, with a -34 dB hiss over every pause, so
    Dialt's speech detection rarely hears the caller stop and the call seems to go deaf. Decide
    from the bytes themselves: the right decoding has the quieter pauses (A-law silence is
    0xD5/0x55, which mu-law reads as about +/-700). Frames before the decision pass through.
    """

    DECIDE_AFTER = 25  # frames (~0.5 s; the greeting hold drops these anyway)

    def __init__(self, provider: str) -> None:
        self.provider, self.alaw, self._seen = provider, None, []

    def to_mulaw(self, data: bytes) -> bytes:
        if self.alaw is None:
            self._seen.append(data)
            if len(self._seen) < self.DECIDE_AFTER:
                return data
            self.alaw = self._looks_like_alaw(b"".join(self._seen))
            self._seen = []
            logger.info("[%s] caller audio is %s", self.provider,
                        "A-law (converting to mu-law)" if self.alaw else "mu-law")
        return data.translate(ALAW_TO_MULAW) if self.alaw else data

    @staticmethod
    def _looks_like_alaw(data: bytes) -> bool:
        def quiet_floor(pcm: np.ndarray) -> float:
            frames = pcm[: len(pcm) // 160 * 160].astype(np.float64).reshape(-1, 160)
            return float(np.percentile(np.sqrt((frames ** 2).mean(axis=1)), 20))
        as_mulaw = decode_mulaw(data)
        as_alaw = decode_mulaw(data.translate(ALAW_TO_MULAW))
        return quiet_floor(as_alaw) < quiet_floor(as_mulaw) / 4


# Diagnostics for bad caller audio (2026-11-01: Dialt's own copy of the caller audio was loud,
# clipped and had a steady tone). With VOICE_DEBUG_AUDIO=1 each call keeps the raw media-stream
# payload exactly as the phone provider sent it, and Telnyx records the call itself (dual channel),
# so the two can be compared. Recordings hold caller audio: off by default, root-only, for tests.
DEBUG_AUDIO = os.environ.get("VOICE_DEBUG_AUDIO", "").strip().lower() in ("1", "true", "yes")
DEBUG_DIR = Path(os.environ.get("VOICE_DEBUG_DIR", "/var/lib/agent-voice/debug"))


# Seconds between spoken "still working" updates while an agent runs.
PROGRESS_EVERY_S = float(os.environ.get("VOICE_PROGRESS_SECONDS", "20"))
# Seconds Dialt gets to start reading an agent's answer before the bridge asks it to reply.
ANSWER_GRACE_S = float(os.environ.get("VOICE_ANSWER_GRACE_SECONDS", "5"))

# Phone audio from Dialt is 8 kHz (the call engine connects with output_sample_rate=8000).
TELEPHONY_SR = 8000
# Never written to the log: ready / reconnected events carry a short-lived resume credential.
SECRET_EVENT_FIELDS = frozenset({"resume_token"})


def _loggable(data):
    if isinstance(data, dict):
        return {k: ("[redacted]" if k in SECRET_EVENT_FIELDS else _loggable(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_loggable(v) for v in data]
    return data


# Dialt events too frequent or already logged as CALLER / VOICE lines.
QUIET_EVENTS = frozenset({"audio", "text_delta", "asr", "conversation_item", "utterance"})


async def bridge_call(provider: str, socket, stream_id: str, call_id: str, settings) -> None:
    """Run one call through the shared engine with agent tools, deferral, caps and transcript logs."""
    session_id = hashlib.sha256(f"{provider}:{call_id}".encode()).hexdigest()
    agent_session = f"voice-{session_id[:16]}"
    found = await agents.discover()
    by_tool = {a.tool: a for a in found}
    live: dict = {}
    end_call = asyncio.Event()
    greeting_started = asyncio.Event()

    pending_jobs: dict[str, list[str]] = {}   # tool name -> Dialt tool ids, oldest first

    async def keep_caller_posted(tool_id: str, agent: agents.Agent):
        """While the agent works, ask the voice for a short spoken update every PROGRESS_EVERY_S.

        Without it the caller hears nothing between "I'll see to that now" and the answer: on
        2026-10-31 that was 45 s of dead air and the caller hung up. reply=True speaks only when
        the conversation is free, so it never talks over the caller.
        """
        started = asyncio.get_running_loop().time()
        while True:
            await asyncio.sleep(PROGRESS_EVERY_S)
            waited = int(asyncio.get_running_loop().time() - started)
            try:
                await live["session"].send_tool_partial_result(
                    tool_id, {"status": f"{agent.name} is still working on it ({waited} s so far); "
                                        "no answer yet. Briefly tell the caller you're still on it."},
                    reply=True)
            except Exception:  # noqa: BLE001 - an update is optional; the answer still arrives
                logger.exception("[%s] progress update failed", provider)
                return

    async def run_tool(name: str, args: dict):
        agent = by_tool.get(name)
        if agent is None:
            raise ToolError("that agent is not available")
        request = str(args.get("request", ""))[:4000]
        logger.info("[%s] -> %s: %s", provider, agent.name, request[:200])
        ids = pending_jobs.get(name) or []
        tool_id = ids.pop(0) if ids else None
        posted = asyncio.create_task(keep_caller_posted(tool_id, agent)) if tool_id and "session" in live else None
        started = asyncio.get_running_loop().time()
        try:
            reply = await agents.ask(agent, request, agent_session)
        except asyncio.CancelledError:
            logger.info("[%s] call ended while %s was still working (%.0f s)", provider, agent.name,
                        asyncio.get_running_loop().time() - started)
            raise
        except Exception:
            logger.warning("[%s] %s failed after %.0f s", provider, agent.name,
                           asyncio.get_running_loop().time() - started)
            raise
        finally:
            if posted:
                posted.cancel()
        logger.info("[%s] <- %s (%.0f s): %s", provider, agent.name,
                    asyncio.get_running_loop().time() - started, reply[:300])
        asyncio.create_task(make_sure_answer_is_spoken(agent, asyncio.get_running_loop().time()))
        return {"reply": reply[:3000]}

    async def make_sure_answer_is_spoken(agent: agents.Agent, answered_at: float):
        """Dialt normally reads a finished job out within a second. On a real call (2026-10-31) it
        got the answer and then said nothing for 42 s until the caller hung up; if no turn starts
        within ANSWER_GRACE_S of the answer, ask Dialt to reply now."""
        await asyncio.sleep(ANSWER_GRACE_S)
        if live.get("last_turn", 0) > answered_at or "session" not in live:
            return
        logger.warning("[%s] Dialt did not speak %s's answer within %.0f s; requesting a reply",
                       provider, agent.name, ANSWER_GRACE_S)
        try:
            ack = await live["session"].request_reply(
                instructions=f"{agent.name} has answered. Tell the caller what it found, briefly.")
            logger.info("[%s] request_reply ack %s", provider, json.dumps(ack, default=str)[:200])
        except Exception:  # noqa: BLE001 - best effort; the call goes on
            logger.exception("[%s] request_reply failed", provider)

    async def on_connected(session):
        live["session"] = session
        live["sent"] = 0
        send_audio = session.send_audio

        async def counted_send_audio(chunk, **kw):  # diagnostics: frames actually handed to Dialt
            live["sent"] += 1
            return await send_audio(chunk, **kw)
        session.send_audio = counted_send_audio

    async def on_event(event):
        d = event.data
        if event.type in ("turn", "audio"):
            greeting_started.set()
        if event.type == "turn":
            live["last_turn"] = asyncio.get_running_loop().time()
        if event.type in ("ready", "reconnected"):
            # Look a call up on Dialt's Sessions page or GET /v1/sessions/{uuid}: what the server
            # heard and decided. Unchanged across resumes; new for a new dialogue.
            uuid = d.get("session_uuid") or getattr(live.get("session"), "session_uuid", None)
            live["session_uuid"] = uuid
            logger.info("[%s] dialt session_uuid=%s (%s)", provider, uuid, event.type)
        if event.type == "audio" and event.audio is not None:
            track_playback(len(event.audio))
        elif event.type == "interrupted" and d.get("response_id"):
            playback["reported"].add(d["response_id"])  # the call engine reports this one itself
        elif event.type == "canceled":
            await report_cancelled_playback(d.get("response_id"))
        if event.type == "conversation_item" and d.get("item", {}).get("kind") in ("tool_call", "tool_result"):
            item = d["item"]  # confirms Dialt received the agent's answer (not otherwise visible)
            logger.info("[%s] dialt %s %s", provider, item["kind"], json.dumps(item, default=str)[:200])
        if event.type not in QUIET_EVENTS:
            # One line per Dialt event (speech start/stop, turns, tools, interruptions), so a call
            # that goes quiet can be explained: 2026-10-31 a caller's sentence was cut off and the
            # line went silent for 22 s with nothing in the log to say why.
            logger.info("[%s] dialt %.0fms %s %s", provider, event.t_ms, event.type,
                        json.dumps(_loggable(d), default=str)[:240])
        if event.type == "asr":
            logger.info("[%s] CALLER: %s", provider, str(d.get("text", ""))[:200])
        elif event.type == "conversation_item" and d.get("item", {}).get("role") == "assistant":
            logger.info("[%s] VOICE: %s", provider, str(d["item"].get("text", ""))[:200])
        elif event.type == "tool_call" and d.get("name") in by_tool:
            tool_id = str(d["id"])  # detach the agent job so the caller can keep talking
            pending_jobs.setdefault(d["name"], []).append(tool_id)
            await live["session"].send_tool_deferred(tool_id, f"{session_id[:16]}-{tool_id}",
                                                     status_label=f"Asking {by_tool[d['name']].name}")
        elif event.type in ("error", "tool_job_delivery_failed"):
            logger.warning("[%s] %s %s", provider, event.type, str(d)[:200])

    # Audio Dialt sent for the reply now playing: on a Dialt-side cancel the call engine clears
    # it from the phone line but, unlike on an interruption, tells Dialt nothing. Dialt's docs ask
    # for playback_stopped after clearing buffered playback; the 18:01 hang (2026-10-31) began
    # with such a cancel, so report it (one-shot per response, as the docs require).
    playback: dict = {"response": None, "first": 0.0, "ms": 0.0, "reported": set()}

    def track_playback(samples: int) -> None:
        response = getattr(live.get("session"), "_output_response_id", None)
        now = asyncio.get_running_loop().time()
        if response != playback["response"]:
            playback.update(response=response, first=now, ms=0.0)
        playback["ms"] += samples * 1000 / TELEPHONY_SR

    async def report_cancelled_playback(response_id: str | None) -> None:
        response_id = response_id or playback["response"]
        session = live.get("session")
        if not response_id or session is None or response_id in playback["reported"]:
            return
        playback["reported"].add(response_id)
        discarded = 0.0
        if response_id == playback["response"]:  # sent in real time: what hasn't had time to play
            played = (asyncio.get_running_loop().time() - playback["first"]) * 1000
            discarded = max(0.0, playback["ms"] - played)
        try:
            await session.playback_stopped(response_id, round(discarded))
            logger.info("[%s] reported playback_stopped for cancelled %s (~%.0f ms unplayed)",
                        provider, response_id, discarded)
        except Exception:  # noqa: BLE001 - diagnostic experiment; never break the call
            logger.exception("[%s] playback_stopped report failed", provider)

    async def call_timer():
        await asyncio.sleep(CONFIG.max_call_seconds)
        logger.info("[%s] max call length reached; ending call", provider)
        end_call.set()

    dump = None
    if DEBUG_AUDIO:
        DEBUG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        dump_path = DEBUG_DIR / f"{provider}-{session_id[:16]}.ulaw"
        dump = open(os.open(dump_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb")
        logger.info("[%s] debug: raw caller payload -> %s (8 kHz mu-law)", provider, dump_path)
    caller_audio = HoldCallerAudio(socket, greeting_started, dump, CallerCodec(provider))

    async def audio_meter():
        """Every 10 s: how much caller audio arrived. Distinguishes "Dialt stopped answering"
        from "the caller's audio stopped arriving" (both look like silence in the transcript)."""
        last, last_sent = 0, 0
        while True:
            await asyncio.sleep(10)
            sent = live.get("sent", 0)
            logger.info("[%s] caller audio: %d frames in 10 s, %d sent to Dialt (session %s)", provider,
                        caller_audio.frames - last, sent - last_sent,
                        getattr(live.get("session"), "_state", "?"))
            last, last_sent = caller_audio.frames, sent

    timer = asyncio.create_task(call_timer())
    meter = asyncio.create_task(audio_meter())
    ACTIVE_CALLS.add(session_id)
    try:
        mode = DialtMode(instructions=instructions(found), voice=CONFIG.voice, greeting=greeting(found),
                         end_call=True, tools=tool_manifest(found) or None,
                         silence_nudge_s=CONFIG.silence_nudge_s, silence_end_s=CONFIG.silence_end_s)
        await run_call_bridge(caller_audio, stream_id, session_id[:64],
                              settings=settings, mode=mode,
                              hooks=BridgeHooks(execute_tool=run_tool, on_connected=on_connected,
                                                on_event=on_event, end_call=end_call))
    finally:
        timer.cancel()
        meter.cancel()
        ACTIVE_CALLS.discard(session_id)
        if dump is not None:
            dump.close()
        logger.info("[%s] call ended; dialt session_uuid=%s", provider, live.get("session_uuid"))


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
        elif event["event_type"] == "call.answered" and payload.get("direction") != "incoming":
            # Outbound (agent-voice-ring): start the media stream only now. Streaming from the
            # dial itself connects Dialt while the phone is still ringing, so the greeting is
            # spoken into the ring and the person answering never hears it (2026-10-22 call).
            await telnyx_command(payload["call_control_id"], "streaming_start", **telnyx_stream_params())
        elif event["event_type"] == "streaming.failed":
            await telnyx_command(payload["call_control_id"], "hangup")
        elif event["event_type"] == "call.recording.saved" and DEBUG_AUDIO:
            asyncio.create_task(save_telnyx_recording(payload))
        if event["event_type"] == "call.answered" and DEBUG_AUDIO:
            # Telnyx's own recording of the call, before any media streaming (both directions).
            await telnyx_command(payload["call_control_id"], "record_start", format="wav", channels="dual")
    except httpx.HTTPError:
        logger.exception("Telnyx call command failed")
        raise HTTPException(503) from None
    return Response(status_code=200)


async def save_telnyx_recording(payload: dict) -> None:
    """VOICE_DEBUG_AUDIO: keep Telnyx's recording next to the raw stream dump (its URL expires)."""
    url = (payload.get("recording_urls") or {}).get("wav") or (payload.get("public_recording_urls") or {}).get("wav")
    if not url:
        logger.warning("[telnyx] debug: recording saved without a wav URL")
        return
    path = DEBUG_DIR / f"telnyx-recording-{str(payload.get('call_control_id', 'call'))[-16:]}.wav"
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
        DEBUG_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as f:
            f.write(r.content)
        logger.info("[telnyx] debug: Telnyx recording -> %s (%d bytes)", path, len(r.content))
    except (httpx.HTTPError, OSError):
        logger.exception("[telnyx] debug: could not save the Telnyx recording")


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
