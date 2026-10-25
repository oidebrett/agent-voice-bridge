// Invariants for agent-voice-bridge (phone calls -> Dialt -> OpenClaw/Hermes). See README.md.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'

const read = (f) => readFileSync(new URL(`../${f}`, import.meta.url), 'utf8')
const bridge = read('bridge.py')
const agents = read('agents.py')
const route = read('ensure-route.sh')
const install = read('install.sh')
const reqs = read('requirements.txt')

// Only the provider paths are public, on the box's existing host.
assert.match(route, /PathPrefix\(\\`\/telnyx\/\\`\) \|\| PathPrefix\(\\`\/twilio\/\\`\)/, 'route must expose only /telnyx/* and /twilio/*')
assert.doesNotMatch(route, /PathPrefix\(\\`\/\\`\)/, 'route must never take the whole host')
// ufw (INPUT DROP on AgentGateway boxes) must let Traefik's Docker networks — and only them — reach the bridge.
assert.match(route, /ufw allow proto tcp from 172\.16\.0\.0\/12 to any port "\$PORT"/, 'route keeper must open the bridge port to Docker networks when ufw is active')

// Every provider entry point verifies its signature before doing anything.
for (const [path, check] of [
  ['/telnyx/voice', 'telnyx_signature_is_valid'], ['/telnyx/media', 'telnyx_signature_is_valid'],
  ['/twilio/voice', '_twilio_form'], ['/twilio/stream-status', '_twilio_form'], ['/twilio/media', 'twilio_signature_is_valid'],
]) {
  const decorator = bridge.match(new RegExp(`@app\\.(?:post|websocket)\\("${path.replace(/\//g, '\\/')}"`))
  assert.ok(decorator, `bridge must serve ${path}`)
  const body = bridge.slice(decorator.index, decorator.index + 700)
  assert.ok(body.includes(check), `${path} must verify the provider signature (${check})`)
}
assert.match(bridge, /HTTPException\(403, "invalid Telnyx signature"\)/)
assert.match(bridge, /HTTPException\(403, "invalid Twilio signature"\)/)

// Agents are reached through their CLI (sandbox exec / host), never an exposed API or port.
assert.match(agents, /"sandbox", "exec", "-n", agent\.sandbox/, 'sandboxed agents must be reached via openshell sandbox exec')
assert.doesNotMatch(agents, /127\.0\.0\.1:\d+|\/v1\/responses|API_SERVER_KEY/, 'agents.py must not use agent network APIs or keys')
assert.match(agents, /stdin=asyncio\.subprocess\.DEVNULL/, 'agent subprocesses must not inherit stdin')
assert.match(agents, /_ready_sandboxes/, 'discovery must skip registry entries whose sandbox is gone')

// Background tools + caps + allowlist switch exist.
assert.match(bridge, /"deferred": True/)
assert.match(bridge, /send_tool_deferred/)
assert.match(bridge, /VOICE_ALLOWED_CALLERS/)
assert.match(bridge, /VOICE_MAX_CALLS/)
assert.match(bridge, /VOICE_MAX_CALL_SECONDS/)
// Outbound Telnyx calls stream only once answered, else the greeting plays into the ringing.
assert.match(bridge, /"call\.answered"[\s\S]{0,400}"streaming_start", \*\*telnyx_stream_params\(\)/, 'outbound Telnyx calls must start streaming on call.answered')
assert.doesNotMatch(read('ring.py'), /"stream_url"/, 'ring.py must not stream from the dial (greeting lost while ringing)')
// Caller audio is held until Dialt's greeting starts, else an early "Hello?" skips the greeting.
assert.match(bridge, /run_call_bridge\(HoldCallerAudio\(socket, greeting_started\)/, 'calls must hold caller audio until the greeting starts')
assert.match(bridge, /GREETING_HOLD_SECONDS = /, 'the hold must have a cut-off')
// The dashboard sets/resets the greeting by writing this file; it must be read per call (no restart).
assert.match(bridge, /\/opt\/agent-voice\/greeting\.txt/, 'bridge must read the dashboard greeting file')
assert.match(bridge, /def greeting\(found[^)]*\)[^\n]*\n(?:\s*#.*\n)*\s*if custom := custom_greeting\(\)/, 'greeting must be read on every call')

// Secrets: root-only env file on the host.
assert.match(install, /umask 077/)
assert.match(install, /chmod 600 "\$DIR\/\.env"/)

// Dependencies pinned exactly (dialt-recipes by commit).
for (const line of reqs.split('\n').filter((l) => l && !l.startsWith('#'))) {
  assert.ok(/==|@[0-9a-f]{40}$/.test(line), `unpinned requirement: ${line}`)
}
assert.match(reqs, /python-multipart==/, 'Twilio form webhooks need python-multipart')

// Python sources at least compile (skipped where python3 is missing).
const py = spawnSync('python3', ['-c', 'import ast,sys\nfor f in sys.argv[1:]: ast.parse(open(f).read(), f)',
  ...['bridge.py', 'agents.py', 'provision.py', 'ring.py', 'envfile.py'].map((f) => new URL(`../${f}`, import.meta.url).pathname)])
if (!py.error) assert.equal(py.status, 0, `python syntax: ${py.stderr}`)
const sh = spawnSync('bash', ['-n', new URL('../install.sh', import.meta.url).pathname])
if (!sh.error) assert.equal(sh.status, 0, `install.sh syntax: ${sh.stderr}`)

console.log('voice-bridge-check: ok')
