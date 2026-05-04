#!/usr/bin/env bash
# Forward minikube Prometheus (monitoring/svc/prometheus:9090) to host:9090.
# Required for: host Grafana datasource, host curl http://localhost:9090/...
#
# Run this in a dedicated terminal and keep it open. Ctrl-C to stop.
# Logs to /tmp/prometheus-tunnel.log if you prefer background:
#   nohup ./scripts/start-prometheus-tunnel.sh > /tmp/prometheus-tunnel.log 2>&1 &
set -euo pipefail

if ! kubectl -n monitoring get svc prometheus >/dev/null 2>&1; then
  echo "ERROR: Prometheus svc not found in 'monitoring' ns. Did you 'kubectl apply -f manifests/monitoring/'?" >&2
  exit 1
fi

# Kill any prior forwarder on :9090 to avoid 'address already in use'.
PIDS=$(lsof -nP -iTCP:9090 -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
  echo "Killing previous listeners on :9090: $PIDS"
  kill $PIDS 2>/dev/null || true
  sleep 1
fi

echo "Forwarding minikube://monitoring/svc/prometheus:9090 -> localhost:9090"
exec kubectl -n monitoring port-forward svc/prometheus 9090:9090 --address=127.0.0.1
