#!/bin/bash
set -e

# Container entrypoint: starts SDRplay API service, flowgraph, and dashboard.

PIDS=()

cleanup() {
    echo "[entrypoint] Shutting down..."
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    # Stop sdrplay_apiService
    killall sdrplay_apiService 2>/dev/null || true
    wait
    echo "[entrypoint] Done."
    exit 0
}

trap cleanup SIGTERM SIGINT

# 1. Start SDRplay API service
echo "[entrypoint] Starting sdrplay_apiService..."
/usr/local/bin/sdrplay_apiService &
PIDS+=($!)
sleep 2

# 2. Start headless flowgraph
echo "[entrypoint] Starting headless flowgraph..."
python3 /app/lora_rx_sdrplay_headless.py &
PIDS+=($!)
sleep 3

# 3. Start dashboard
echo "[entrypoint] Starting dashboard..."
python3 /app/dashboard.py &
PIDS+=($!)

echo "[entrypoint] All services started. Dashboard at http://0.0.0.0:5000"

# Wait for any child to exit
wait -n
echo "[entrypoint] A process exited, shutting down..."
cleanup
