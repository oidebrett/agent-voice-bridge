#!/usr/bin/env bash
# Install (or reconfigure) the agent voice bridge: phone calls via Dialt + Telnyx or Twilio,
# answered by the OpenClaw / Hermes agents on this box. Run as root. Idempotent.
#
# Required environment:
#   VOICE_MODE          agentgateway | openclaw | hermes | oce, or a comma-separated list
#                       (one number for several backends on the box, e.g. agentgateway,oce)
#   VOICE_PROVIDER      telnyx | twilio
#   VOICE_PUBLIC_HOST   the box's existing public hostname (voice uses /telnyx/* or /twilio/* on it)
#   VOICE_PHONE_NUMBER  E.164, e.g. +35319646444 (must be on the provider account)
#   DIALT_API_KEY
#   TELNYX_API_KEY                              (provider=telnyx)
#   TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN      (provider=twilio)
# Optional:
#   VOICE_GREETING, VOICE_ALLOWED_CALLERS (comma-separated E.164; blank = anyone may call),
#   VOICE_AGENT_NAME (standalone boxes), VOICE_MAX_CALLS, VOICE_MAX_CALL_SECONDS,
#   VOICE_SILENCE_NUDGE_SECONDS (8), VOICE_SILENCE_END_SECONDS (30),
#   OCE_URL, OCE_SERVICE_KEY_FILE, OCE_KUBECONFIG (mode oce; files must already exist, root-only),
#   VOICE_SRC (directory holding these sources; default: fetch VOICE_REPO@VOICE_REF)
#
# Hosts without systemd or root (e.g. an Upstash Box container): set VOICE_SERVICE=none and
# VOICE_DIR to a directory the user owns, and run as that user. The bridge then listens on
# VOICE_BIND (default 0.0.0.0) for the host's own public URL — no Traefik route — and
# $VOICE_DIR/start.sh (re)starts it; run that on every container start. VOICE_AGENT_HOME is the
# agent user's home (default /root), where the openclaw / hermes CLIs and their config live.
#
# Afterwards: `agent-voice-ring +353...` places a test call; logs: `journalctl -u agent-voice -f`.
set -euo pipefail

DIR=${VOICE_DIR:-/opt/agent-voice}
SERVICE=${VOICE_SERVICE:-systemd}
case "$SERVICE" in systemd|none) ;; *) echo "agent-voice: bad VOICE_SERVICE $SERVICE (systemd|none)" >&2; exit 1 ;; esac
need() { [ -n "${!1:-}" ] || { echo "agent-voice: $1 is required" >&2; exit 1; }; }
for v in VOICE_MODE VOICE_PROVIDER VOICE_PUBLIC_HOST VOICE_PHONE_NUMBER DIALT_API_KEY; do need "$v"; done
for m in ${VOICE_MODE//,/ }; do
  case "$m" in agentgateway|openclaw|hermes|oce) ;; *) echo "agent-voice: bad VOICE_MODE $VOICE_MODE" >&2; exit 1 ;; esac
done
case "$VOICE_PROVIDER" in
  telnyx) need TELNYX_API_KEY ;;
  twilio) need TWILIO_ACCOUNT_SID; need TWILIO_AUTH_TOKEN ;;
  *) echo "agent-voice: bad VOICE_PROVIDER $VOICE_PROVIDER" >&2; exit 1 ;;
esac

echo "agent-voice: installing ($VOICE_MODE, $VOICE_PROVIDER, $VOICE_PUBLIC_HOST)"
install -d -m 700 "$DIR" "$DIR/app"

# 1. Sources: a local checkout (VOICE_SRC) or this repo fetched at VOICE_REF (a tag or commit SHA;
# pin it so a new install never silently picks up an unreviewed change).
SRC=${VOICE_SRC:-}
if [ -z "$SRC" ]; then
  rm -rf "$DIR/src"
  git init --quiet "$DIR/src"
  git -C "$DIR/src" fetch --quiet --depth 1 "${VOICE_REPO:-https://github.com/oidebrett/agent-voice-bridge.git}" \
    "${VOICE_REF:-main}"
  git -C "$DIR/src" checkout --quiet FETCH_HEAD
  SRC=$DIR/src
fi
for f in bridge.py agents.py provision.py ring.py envfile.py instructions.md requirements.txt ensure-route.sh; do
  install -m 600 "$SRC/$f" "$DIR/app/$f"
done
chmod 700 "$DIR/app/ensure-route.sh"

# 2. Python environment (uv manages the interpreter, so the host Python version doesn't matter).
if [ "$(id -u)" = 0 ]; then BIN=/usr/local/bin; else BIN=$HOME/.local/bin; mkdir -p "$BIN"; fi
export PATH=$BIN:/usr/local/bin:/root/.local/bin:$PATH
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$BIN" INSTALLER_NO_MODIFY_PATH=1 sh
uv venv --quiet --allow-existing --python 3.12 "$DIR/.venv"
uv pip install --quiet --python "$DIR/.venv/bin/python" -r "$DIR/app/requirements.txt"

# 3. Configuration (root-only). Keys stay on the host; nothing is written into any sandbox.
umask 077
{
  echo "VOICE_MODE=$VOICE_MODE"
  echo "VOICE_PROVIDER=$VOICE_PROVIDER"
  echo "VOICE_PUBLIC_HOST=$VOICE_PUBLIC_HOST"
  echo "PUBLIC_BASE_URL=https://$VOICE_PUBLIC_HOST"
  echo "VOICE_PHONE_NUMBER=${VOICE_PHONE_NUMBER// /}"
  echo "VOICE_PORT=${VOICE_PORT:-8095}"
  echo "DIALT_API_KEY=$DIALT_API_KEY"
  if [ "$VOICE_PROVIDER" = telnyx ]; then
    echo "TELNYX_API_KEY=$TELNYX_API_KEY"
  else
    echo "TWILIO_ACCOUNT_SID=$TWILIO_ACCOUNT_SID"
    echo "TWILIO_AUTH_TOKEN=$TWILIO_AUTH_TOKEN"
  fi
  [ -z "${VOICE_GREETING:-}" ] || echo "VOICE_GREETING=$VOICE_GREETING"
  [ -z "${VOICE_ALLOWED_CALLERS:-}" ] || echo "VOICE_ALLOWED_CALLERS=${VOICE_ALLOWED_CALLERS// /}"
  [ -z "${VOICE_AGENT_NAME:-}" ] || echo "VOICE_AGENT_NAME=$VOICE_AGENT_NAME"
  [ -z "${VOICE_MAX_CALLS:-}" ] || echo "VOICE_MAX_CALLS=$VOICE_MAX_CALLS"
  [ -z "${VOICE_SILENCE_NUDGE_SECONDS:-}" ] || echo "VOICE_SILENCE_NUDGE_SECONDS=$VOICE_SILENCE_NUDGE_SECONDS"
  [ -z "${VOICE_SILENCE_END_SECONDS:-}" ] || echo "VOICE_SILENCE_END_SECONDS=$VOICE_SILENCE_END_SECONDS"
  [ -z "${VOICE_MAX_CALL_SECONDS:-}" ] || echo "VOICE_MAX_CALL_SECONDS=$VOICE_MAX_CALL_SECONDS"
  for v in OCE_URL OCE_SERVICE_KEY_FILE OCE_KUBECONFIG; do [ -z "${!v:-}" ] || echo "$v=${!v}"; done
  [ "$DIR" = /opt/agent-voice ] || echo "VOICE_DIR=$DIR"
  [ -z "${VOICE_AGENT_HOME:-}" ] || echo "VOICE_AGENT_HOME=$VOICE_AGENT_HOME"
  # The systemd unit sets VOICE_BIND itself (loopback, behind Traefik); never override it there.
  [ "$SERVICE" = systemd ] || echo "VOICE_BIND=${VOICE_BIND:-0.0.0.0}"
} > "$DIR/.env"
chmod 600 "$DIR/.env"

# 4. Point the number at this box via the provider API (webhook URL, Voice API app, public key).
if ! (cd "$DIR/app" && "$DIR/.venv/bin/python" provision.py "$DIR/.env"); then
  echo "agent-voice: WARNING provider setup failed; fix the key/number and re-run this installer" >&2
fi

# 5a. No systemd (VOICE_SERVICE=none): a start script the host runs on every container start.
# Restart = stop the previous bridge by its pid file, checked to really be our uvicorn (pids are
# reused after a container restart, so a stale file could name another process).
if [ "$SERVICE" = none ]; then
  cat > "$DIR/start.sh" <<START
#!/bin/sh
# (Re)start the agent voice bridge. Written by install.sh (VOICE_SERVICE=none).
DIR=$DIR
PID=\$(cat "\$DIR/bridge.pid" 2>/dev/null)
if [ -n "\$PID" ] && grep -q uvicorn "/proc/\$PID/cmdline" 2>/dev/null; then kill "\$PID"; sleep 1; fi
cd "\$DIR/app" || exit 1
VOICE_DIR="\$DIR" nohup "\$DIR/.venv/bin/python" -c '
import os, envfile
envfile.load()
os.execv(os.path.join(os.environ["VOICE_DIR"], ".venv/bin/uvicorn"), ["uvicorn", "bridge:app",
         "--host", os.environ.get("VOICE_BIND", "0.0.0.0"), "--port", os.environ.get("VOICE_PORT", "8095"),
         "--no-server-header"])
' >> "\$DIR/bridge.log" 2>&1 &
echo \$! > "\$DIR/bridge.pid"
START
  chmod 700 "$DIR/start.sh"
  cat > "$BIN/agent-voice-ring" <<SH
#!/bin/sh
# Ring a number from the configured provider and connect it to your agents: agent-voice-ring +353...
cd $DIR/app && VOICE_DIR=$DIR exec $DIR/.venv/bin/python ring.py "\$@"
SH
  chmod 755 "$BIN/agent-voice-ring"
  "$DIR/start.sh"
  echo "agent-voice: installed (no systemd). Start it on every boot with $DIR/start.sh; logs: $DIR/bridge.log"
  exit 0
fi

# 5. Services: the bridge, plus a timer that keeps its Traefik route and listen address right.
cat > /etc/systemd/system/agent-voice.service <<'UNIT'
[Unit]
Description=Agent voice bridge (Dialt + Telnyx/Twilio -> OpenClaw/Hermes)
After=network-online.target docker.service
Wants=network-online.target

[Service]
WorkingDirectory=/opt/agent-voice/app
Environment=VOICE_BIND=127.0.0.1 HOME=/root
EnvironmentFile=/opt/agent-voice/.env
EnvironmentFile=-/opt/agent-voice/bind.env
ExecStart=/opt/agent-voice/.venv/bin/uvicorn bridge:app --host ${VOICE_BIND} --port ${VOICE_PORT} --no-server-header
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/agent-voice-route.service <<'UNIT'
[Unit]
Description=Keep the agent voice Traefik route and listen address current

[Service]
Type=oneshot
ExecStart=/opt/agent-voice/app/ensure-route.sh
UNIT
cat > /etc/systemd/system/agent-voice-route.timer <<'UNIT'
[Unit]
Description=Re-check the agent voice route (Traefik may start or be redeployed later)

[Timer]
OnBootSec=1min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
UNIT
cat > /usr/local/bin/agent-voice-ring <<'SH'
#!/bin/sh
# Ring a number from the configured provider and connect it to your agents: agent-voice-ring +353...
cd /opt/agent-voice/app && exec /opt/agent-voice/.venv/bin/python ring.py "$@"
SH
chmod 755 /usr/local/bin/agent-voice-ring

systemctl daemon-reload
systemctl enable agent-voice agent-voice-route.timer
# restart, not just --now: a reinstall must run the new code and .env (2026-10-25: a running bridge
# kept the old agents.py after a reinstall, so a call offered only the old backends).
systemctl restart agent-voice
systemctl start agent-voice-route.timer
systemctl start agent-voice-route.service || true
echo "agent-voice: installed. Test with: agent-voice-ring <your verified mobile>"
