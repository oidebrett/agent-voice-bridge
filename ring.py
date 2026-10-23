"""Ring a phone number from the configured provider and connect it to the voice agent.

Usage: agent-voice-ring +353861234567

On Telnyx starter accounts and Twilio trial accounts the destination must be a verified number.
"""

from __future__ import annotations

import os
import sys

import httpx

import envfile


def ring(to: str) -> None:
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    wss = base.replace("https://", "wss://", 1)
    number = os.environ["VOICE_PHONE_NUMBER"].replace(" ", "")
    if os.environ.get("VOICE_PROVIDER") == "twilio":
        sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
        twiml = (f'<Response><Connect><Stream url="{wss}/twilio/media" statusCallback="{base}/twilio/stream-status" '
                 'statusCallbackMethod="POST" /></Connect><Hangup /></Response>')
        r = httpx.post(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls.json", auth=(sid, tok), timeout=15,
                       data={"From": number, "To": to, "Twiml": twiml})
    else:
        r = httpx.post("https://api.telnyx.com/v2/calls", timeout=15,
                       headers={"Authorization": "Bearer " + os.environ["TELNYX_API_KEY"]},
                       json={"connection_id": os.environ["TELNYX_CONNECTION_ID"], "to": to, "from": number,
                             # No stream here: the bridge starts it on call.answered, so the
                             # greeting isn't spoken while the phone is still ringing.
                             "webhook_url": base + "/telnyx/voice"})
    if r.status_code >= 300:
        raise SystemExit(f"ring failed ({r.status_code}): {r.text[:400]}")
    print(f"ringing {to} from {number}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    envfile.load()
    ring(sys.argv[1].replace(" ", ""))
