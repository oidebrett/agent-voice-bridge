#!/usr/bin/env bash
# Keep the agent-voice public route and listen address correct. Run by agent-voice-route.timer.
#
# Traefik on these boxes is a Komodo-managed container that may appear AFTER the voice install
# (cloud boxes run user_data before Komodo deploys the stack), and a stack redeploy can wipe the
# rules folder. So this re-checks every few minutes and is idempotent.
#
# Only /telnyx/* and /twilio/* on the box's existing public host go to the bridge; every request
# on those paths must carry a valid Telnyx or Twilio signature (checked in bridge.py).
set -u
DIR=/opt/agent-voice
envval() { grep -m1 "^$1=" "$DIR/.env" 2>/dev/null | cut -d= -f2-; }
HOST=$(envval VOICE_PUBLIC_HOST)
PORT=$(envval VOICE_PORT); PORT=${PORT:-8095}
[ -n "$HOST" ] || { echo "agent-voice: VOICE_PUBLIC_HOST missing in $DIR/.env"; exit 1; }

RULES=$(ls -d /etc/komodo/stacks/*/config/traefik/rules 2>/dev/null | head -1)
TRAEFIK=$(docker ps --format '{{.Names}}' 2>/dev/null | grep -i -m1 traefik)
if [ -z "$RULES" ] || [ -z "$TRAEFIK" ]; then
  echo "agent-voice: Traefik not up yet; will retry"
  exit 0
fi

# Where can Traefik reach a host service? Host networking -> loopback; otherwise the gateway of
# the Docker network it lives in (AgentGateway's Traefik shares Gerbil's network namespace).
NETMODE=$(docker inspect -f '{{.HostConfig.NetworkMode}}' "$TRAEFIK")
case "$NETMODE" in
  host) BIND=127.0.0.1 ;;
  container:*) NETC=${NETMODE#container:} ;;
  *) NETC=$TRAEFIK ;;
esac
if [ -z "${BIND:-}" ]; then
  BIND=$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.Gateway}} {{end}}' "$NETC" | awk '{print $1}')
fi
[ -n "$BIND" ] || { echo "agent-voice: cannot work out Traefik's route to the host"; exit 1; }

if [ "$(cat "$DIR/bind.env" 2>/dev/null)" != "VOICE_BIND=$BIND" ]; then
  echo "VOICE_BIND=$BIND" > "$DIR/bind.env"
  systemctl restart agent-voice
  echo "agent-voice: listening on $BIND:$PORT"
fi

RULE="$RULES/agent-voice.yml"
WANT=$(cat <<YML
# Managed by /opt/agent-voice/ensure-route.sh (openshell_controller voice-bridge) — do not edit.
# Phone-provider webhooks + media streams -> agent voice bridge. Signatures are verified there.
http:
  routers:
    agent-voice:
      entryPoints: [websecure]
      priority: 1000
      rule: "Host(\`$HOST\`) && (PathPrefix(\`/telnyx/\`) || PathPrefix(\`/twilio/\`))"
      service: agent-voice-svc
      tls: { certResolver: letsencrypt }
  services:
    agent-voice-svc:
      loadBalancer:
        servers:
          - url: "http://$BIND:$PORT"
YML
)
if [ "$(cat "$RULE" 2>/dev/null)" != "$WANT" ]; then
  printf '%s\n' "$WANT" > "$RULE"
  echo "agent-voice: route written to $RULE"
fi
