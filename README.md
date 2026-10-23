# voice-bridge — phone calls to OpenClaw / Hermes agents

User guide: [SETUP-GUIDE.md](SETUP-GUIDE.md). Plan and decisions:
`manidae-cloud/docs/voice-calling-plan.md`.

```
Phone ⇄ Telnyx | Twilio ──webhook + media stream──▶ bridge.py (host, /opt/agent-voice)
                                                       ├─ outbound wss ─▶ Dialt (voice agent)
                                                       └─ CLI ─▶ OpenClaw / Hermes (sandboxed or standalone)
```

- **Only the bridge is public**, and only `/telnyx/*` + `/twilio/*` on the box's existing host
  (`ensure-route.sh` writes the Traefik rule). Every request must carry a valid provider signature.
- **Agents are never exposed.** `agents.py` runs `openclaw agent` / `hermes chat -q` — inside the
  sandbox via `openshell sandbox exec` on an AgentGateway, on the host for standalone boxes. No
  agent API, port, key or config change is needed (NemoClaw disables OpenClaw config hot-reload, so
  enabling `/v1/responses` would need a gateway restart — avoided).
- **AgentGateway:** one shared number; agents are discovered per call from the NemoClaw registry
  ∩ Ready sandboxes, one Dialt tool per sandbox; the caller picks by name.
- **Agents are Dialt background tools** (`deferred`): the voice agent keeps talking while the agent
  works, and speaks the result when it arrives.
- **Providers** are enabled by credentials. Both share `dialt_recipes.twilio.run_call_bridge`.
- **Access defaults to open** (user decision 2026-10-22). Lockdown switches exist:
  `VOICE_ALLOWED_CALLERS`, `VOICE_MAX_CALLS` (3), `VOICE_MAX_CALL_SECONDS` (1200).
- **Greeting:** the bridge always sends an inline Dialt mode with its own opener, so a greeting
  saved on dialt.com (which belongs to a saved *design*) never applies. The opener is read per call
  from `/opt/agent-voice/greeting.txt` (written by the manidae dashboard over SSH; no restart),
  then `VOICE_GREETING`, then a generated one naming the agents. Max 300 characters (Dialt's cap).

## Install / reconfigure

manidae-cloud runs this automatically when the Phone calls (voice) card is filled in. By hand:

```bash
VOICE_MODE=agentgateway VOICE_PROVIDER=telnyx VOICE_PUBLIC_HOST=openshell-controller.example.com \
VOICE_PHONE_NUMBER=+35319646444 DIALT_API_KEY=dk_... TELNYX_API_KEY=KEY... \
VOICE_SRC=/opt/openshell-controller/voice-bridge bash /opt/openshell-controller/voice-bridge/install.sh
```

Then `agent-voice-ring +353...` to test; logs: `journalctl -u agent-voice -f`.
Turn off: `systemctl disable --now agent-voice agent-voice-route.timer && rm /etc/komodo/stacks/*/config/traefik/rules/agent-voice.yml`.

## Files

| File | Role |
|---|---|
| `bridge.py` | FastAPI app: Telnyx + Twilio webhooks/media, Dialt session, caps, transcript logs |
| `agents.py` | Agent discovery + CLI adapters |
| `provision.py` | Points the number at this box via the provider API (Telnyx app/webhook/public key; Twilio VoiceUrl) |
| `ring.py` | Outbound test call (`agent-voice-ring`) |
| `ensure-route.sh` | Traefik rule + listen address, re-checked by `agent-voice-route.timer` |
| `install.sh` | Idempotent installer used by manidae cloud + BYOVPS scripts |
