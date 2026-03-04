#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"

# Auto-detect environment
if [ -f /.dockerenv ]; then
    ENV_TYPE="docker"
    PYTHON="python3"
elif [ -x "C:/ProgramData/radioconda/python.exe" ]; then
    ENV_TYPE="radioconda"
    PYTHON="C:/ProgramData/radioconda/python.exe"
elif command -v python3 &>/dev/null; then
    ENV_TYPE="linux"
    PYTHON="python3"
else
    echo "Error: No suitable Python found."
    exit 1
fi

# Choose flowgraph based on environment
if [ "$ENV_TYPE" = "docker" ] || [ "$ENV_TYPE" = "linux" ]; then
    FLOWGRAPH="$DIR/lora_rx_sdrplay_headless.py"
else
    FLOWGRAPH="$DIR/lora_rx_sdrplay.py"
fi

echo "Environment: $ENV_TYPE (python: $PYTHON)"
echo "Flowgraph:   $FLOWGRAPH"

case "${1:-help}" in
  rx)
    "$PYTHON" "$FLOWGRAPH"
    ;;
  dashboard)
    "$PYTHON" "$DIR/dashboard.py"
    ;;
  all)
    "$PYTHON" "$FLOWGRAPH" &
    sleep 3
    "$PYTHON" "$DIR/dashboard.py"
    ;;
  *)
    echo "Usage: ./run.sh {rx|dashboard|all}"
    echo "  rx        - start the SDRplay flowgraph"
    echo "  dashboard - start the web dashboard"
    echo "  all       - start both (flowgraph + dashboard)"
    ;;
esac
