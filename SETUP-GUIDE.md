# Phone calls with your agents — setup guide

Call your OpenClaw or Hermes agents on a normal phone number, or have them ring you. A voice
assistant (Dialt) holds the conversation and hands real work to your agents in the background, so
you can keep talking while they work and hear the answer when it's ready.

You need three things: a **Dialt API key**, an account with a **phone provider** (Telnyx *or*
Twilio) and **a phone number** on that account. You bring your own accounts and pay them directly.
Paste the details into the **Phone calls (voice)** card when you create your AgentGateway,
OpenClaw or Hermes deployment. Everything else (webhooks, routing the number to your box) is set
up for you.

## 1. Dialt (about 2 minutes)

1. Sign up at [dialt.com](https://dialt.com) and add billing.
2. Open **Account → API keys**, create a key and copy it (it starts with `dk_`).

## 2a. Telnyx (about 10 minutes, plus any number paperwork)

1. Sign up at [telnyx.com](https://telnyx.com) and **upgrade your account level** (Account →
   upgrade). New accounts can only call one verified number and can't receive calls from most
   phones until upgraded.
2. Add credit.
3. **Buy a phone number** (Numbers → Search & Buy). Some countries, including Ireland and the UK,
   ask for an address or ID document first — that can take a day or two.
4. Create an **API key** (Account Settings → Keys & Credentials → API Keys) and copy it.
5. Optional: if you also want to *place* calls, create an **outbound voice profile** (Voice →
   Outbound Voice Profiles) allowing the countries you'll call. We attach it automatically.

You don't need to create a Voice API application or set any webhook: we do that with your key.

## 2b. Twilio (instead of Telnyx)

1. Sign up at [twilio.com](https://twilio.com) and **upgrade from the trial** (trial accounts can
   only call verified numbers and play a "trial account" message first).
2. **Buy a phone number** with Voice capability (Phone Numbers → Buy a number). Regulatory
   bundles may be needed for some countries.
3. From the Console home page copy your **Account SID** and **Auth Token** (the live ones, not
   the test credentials).

We set the number's incoming-call webhook for you.

## 3. Fill in the Phone calls (voice) card

| Field | What to enter |
|---|---|
| Phone provider | Telnyx or Twilio |
| Dialt API key | from step 1 |
| Telnyx API key *or* Twilio Account SID + Auth Token | from step 2 |
| Phone number | the number you bought, in international format, e.g. `+35319646444` |
| Allowed callers (optional) | comma-separated numbers allowed to call in. **Leave blank and anyone who calls the number can talk to your agents.** |

## 4. Try it

- **Call your number.** On an AgentGateway with several agents you'll be asked which one you want
  (say its name); you can switch agents mid-call.
- **Have it ring you:** in the VPS terminal run `agent-voice-ring +353...`.
- Ask for something only your agent knows ("ask my agent what its name is"), then something it
  has to do ("ask it to save a note saying hello from the phone"). You'll hear "I've asked", you
  can keep chatting, and the answer is read out when it arrives.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "The number dialled is not available" from your mobile | Some free mobile plans can't dial VoIP number ranges — try another phone. |
| Telnyx error D61 / "only 1 verified number" | Your Telnyx account is still on the starter level — upgrade it. |
| Twilio plays a trial message, or won't call you | Trial account — upgrade it, or verify your mobile in Twilio. |
| The call connects but the agent never answers | Check the agent is running (dashboard), then `journalctl -u agent-voice -f` in the VPS terminal. |
| You changed your number or keys | Re-run the voice installer (see the controller's voice-bridge README). |

**Privacy and cost:** calls are handled by Dialt and your phone provider under your own accounts.
Transcripts are logged on your server (`journalctl -u agent-voice`). Dialt charges per minute of
conversation, and your provider charges for the number and call minutes.
