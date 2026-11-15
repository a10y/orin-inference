#!/usr/bin/env bash
# Start the inference server bound ONLY to this machine's Tailscale IPv4 address.
set -euo pipefail
cd "$(dirname "$0")"

# Wait for tailscaled to come up and hand out an address (e.g. right after boot).
for _ in $(seq 1 120); do
  TS_IP=$(tailscale ip -4 2>/dev/null | head -n1 || true)
  if [[ -n "$TS_IP" ]] && ip -4 addr show | grep -q "inet ${TS_IP}/"; then break; fi
  TS_IP=""
  sleep 2
done
if [[ -z "$TS_IP" ]]; then
  echo "Tailscale IPv4 address not available; refusing to start" >&2
  exit 1
fi
case "$TS_IP" in
  100.*) ;;
  *) echo "Unexpected Tailscale address '$TS_IP'; refusing to start" >&2; exit 1 ;;
esac

DNS_NAME=$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' || true)
SHORT_NAME=${DNS_NAME%%.*}
export INFER_ALLOWED_HOSTS="${TS_IP},${DNS_NAME},${SHORT_NAME}"
PORT="${INFER_PORT:-8765}"

echo "Listening on http://${TS_IP}:${PORT} (allowed hosts: ${INFER_ALLOWED_HOSTS})"
exec .venv/bin/uvicorn server.app:app --host "$TS_IP" --port "$PORT" \
  --no-server-header --timeout-keep-alive 30 --log-level info
