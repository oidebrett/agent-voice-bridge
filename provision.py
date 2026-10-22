"""Point the user's phone number at this bridge, using only the API key they gave us.

Telnyx: fetch the webhook public key, make sure a Voice API (Call Control) application sends its
webhooks to https://<host>/telnyx/voice, and assign the number to it.
Twilio: set the number's incoming Voice URL to https://<host>/twilio/voice.

Writes the discovered values (TELNYX_PUBLIC_KEY, TELNYX_CONNECTION_ID) back into the env file.
Idempotent: safe to re-run after changing the number or host.

Usage: python provision.py /opt/agent-voice/.env
"""

from __future__ import annotations

import os
import sys

import httpx

import envfile

TELNYX = "https://api.telnyx.com/v2"


def update_env(path: str, values: dict[str, str]) -> None:
    lines = [l for l in open(path).read().splitlines() if l.split("=", 1)[0] not in values]
    lines += [f"{k}={v}" for k, v in values.items()]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)


def provision_telnyx(base: str, number: str) -> dict[str, str]:
    h = {"Authorization": "Bearer " + os.environ["TELNYX_API_KEY"]}
    webhook = base + "/telnyx/voice"
    with httpx.Client(headers=h, timeout=20) as c:
        pub = c.get(f"{TELNYX}/public_key")
        pub.raise_for_status()
        out = {"TELNYX_PUBLIC_KEY": pub.json()["data"]["public"]}
        nums = c.get(f"{TELNYX}/phone_numbers", params={"filter[phone_number]": number}).json().get("data", [])
        if not nums:
            raise SystemExit(f"Telnyx: {number} is not on this account (check the API key's account)")
        num = nums[0]
        app_id = None
        if num.get("connection_id"):
            r = c.get(f"{TELNYX}/call_control_applications/{num['connection_id']}")
            if r.status_code == 200:
                app_id = num["connection_id"]  # already a Voice API app: reuse it
        if app_id:
            c.patch(f"{TELNYX}/call_control_applications/{app_id}",
                    json={"webhook_event_url": webhook, "webhook_api_version": "2"}).raise_for_status()
            print(f"Telnyx: Voice API app {app_id} now sends webhooks to {webhook}")
        else:
            body = {"application_name": f"Agent voice ({base.split('://', 1)[1]})"[:255],
                    "webhook_event_url": webhook, "webhook_api_version": "2"}
            profiles = c.get(f"{TELNYX}/outbound_voice_profiles").json().get("data", [])
            if profiles:
                body["outbound"] = {"outbound_voice_profile_id": profiles[0]["id"]}
            r = c.post(f"{TELNYX}/call_control_applications", json=body)
            r.raise_for_status()
            app_id = r.json()["data"]["id"]
            c.patch(f"{TELNYX}/phone_numbers/{num['id']}", json={"connection_id": app_id}).raise_for_status()
            print(f"Telnyx: created Voice API app {app_id} and assigned {number} to it")
        out["TELNYX_CONNECTION_ID"] = str(app_id)
        return out


def provision_twilio(base: str, number: str) -> dict[str, str]:
    sid, tok = os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"]
    api = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    with httpx.Client(auth=(sid, tok), timeout=20) as c:
        r = c.get(f"{api}/IncomingPhoneNumbers.json", params={"PhoneNumber": number})
        r.raise_for_status()
        nums = r.json().get("incoming_phone_numbers", [])
        if not nums:
            raise SystemExit(f"Twilio: {number} is not on this account")
        c.post(f"{api}/IncomingPhoneNumbers/{nums[0]['sid']}.json",
               data={"VoiceUrl": base + "/twilio/voice", "VoiceMethod": "POST"}).raise_for_status()
        print(f"Twilio: {number} now sends incoming calls to {base}/twilio/voice")
    return {}


def main(env_path: str) -> None:
    envfile.load(env_path)
    base = os.environ["PUBLIC_BASE_URL"].rstrip("/")
    number = os.environ["VOICE_PHONE_NUMBER"].replace(" ", "")
    provider = os.environ["VOICE_PROVIDER"]
    values = provision_telnyx(base, number) if provider == "telnyx" else provision_twilio(base, number)
    if values:
        update_env(env_path, values)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/opt/agent-voice/.env")
