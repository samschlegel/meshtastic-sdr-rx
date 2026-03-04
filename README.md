# Meshtastic SDR RX

A Meshtastic LoRa packet receiver using GNU Radio and an SDRplay RSPdx R2 software-defined radio. Captures, decodes, and decrypts Meshtastic packets with a real-time web dashboard featuring spectrogram visualization.

Designed to run on a Raspberry Pi 5 (ARM64) via Docker.

## Features

- Real-time LoRa packet capture and demodulation via GNU Radio
- Meshtastic protobuf decryption and parsing (Position, Text, NodeInfo, Telemetry, etc.)
- Energy-based RF event detection with spectrogram visualization
- Web dashboard with WebSocket updates on port 5000
- Docker deployment optimized for Raspberry Pi 5 + SDRplay RSPdx R2

## Hardware

- **SDR:** SDRplay RSPdx R2
- **Target platform:** Raspberry Pi 5 (RPi OS Lite 64-bit / Debian Trixie, aarch64)
- **Dev machine:** Any machine with Docker and SSH access to the Pi

## Quick Start

### Prerequisites (dev machine)

- Docker
- [just](https://github.com/casey/just) command runner:
  ```bash
  cargo install just
  ```
- SSH access to your Pi (`sam@rpi5.local`)

### First-time Pi setup

```bash
just setup
```

This installs Docker on the Pi, then builds and starts the container.

### Dev iteration

```bash
just dev
```

Builds the image on the Pi's Docker daemon (via `DOCKER_HOST` over SSH) using your local source as build context, then starts the container.

### Pull a release from GHCR

```bash
just deploy
```

Pulls the latest image from `ghcr.io/samschlegel/meshtastic-sdr-rx` on the Pi and restarts.

## Justfile Recipes

Run all recipes from the repo root with `just <recipe>`.

| Recipe | Description |
|--------|-------------|
| **Setup** | |
| `provision` | Install Docker (official repo) and git on the Pi |
| `setup` | Full first-time setup: provision + dev |
| **Dev iteration** | |
| `build` | Build image on Pi via `DOCKER_HOST` over SSH |
| `up` | Start containers on the Pi |
| `dev` | Full dev cycle: build + up |
| **Release** | |
| `pull` | Pull latest image from GHCR on the Pi |
| `deploy` | Pull + up |
| **Management** | |
| `down` | Stop containers |
| `restart` | Restart containers |
| `status` | Show container status |
| `logs` | Show recent logs (last 100 lines) |
| `logs-follow` | Follow logs in real-time |
| `check-usb` | Verify SDRplay is visible on USB |
| `ssh` | Interactive SSH shell to the Pi |
| **Cleanup** | |
| `uninstall` | Stop containers and prune images |

## Configuration

Environment variables are set in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `CENTER_FREQ` | `913.125e6` | LoRa center frequency (Hz) |
| `SAMP_RATE` | `1000000` | Sample rate (1 MS/s) |
| `BW` | `250000` | LoRa bandwidth (250 kHz) |
| `SF` | `9` | Spreading factor (7-12) |
| `CR` | `1` | Coding rate (0-4) |
| `IF_GAIN` | `40` | IF gain |
| `LNA_STATE` | `10` | LNA state (0-30) |
| `ANTENNA` | `Antenna A` | Physical antenna selection |
| `MESHTASTIC_AES_KEY` | *(unset)* | Base64 AES-128 key for packet decryption |

## Architecture

```
┌─────────────────────────────────────────────────────┐
│ Docker Container                                    │
│                                                     │
│  SDRplay API Service                                │
│       │                                             │
│       ▼                                             │
│  GNU Radio Flowgraph (lora_rx_sdrplay_headless.py)  │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ SDRplay  │→ │ LoRa     │→ │ ZMQ Publishers   │  │
│  │ Source   │  │ Demod    │  │ :20002 (packets) │  │
│  │          │  │ Chain    │  │ :20003 (IQ)      │  │
│  └──────────┘  └──────────┘  └────────┬─────────┘  │
│                                       │             │
│                                       ▼             │
│                              Dashboard (Flask)      │
│                              :5000 (HTTP/WS)        │
└─────────────────────────────────────────────────────┘
```

- **SDRplay RSPdx R2** captures raw IQ samples at the configured frequency
- **GNU Radio** demodulates LoRa: frame sync → FFT demod → gray mapping → deinterleaver → Hamming decode → header decode → dewhitening → CRC verify
- **ZeroMQ** bridges the flowgraph to the dashboard (IQ on port 20003, decoded packets on port 20002)
- **Dashboard** provides real-time spectrogram visualization, packet decryption, and WebSocket-based live updates

## CI/CD

GitHub Actions builds and pushes the ARM64 Docker image to GHCR on:
- Push to `main`
- Version tags (`v*`)

The workflow uses QEMU + buildx for cross-compilation with GHA caching.

## Local Development (GUI)

For local development with a GUI (requires display + PyQt5):

```bash
./run.sh rx         # Start flowgraph only
./run.sh dashboard  # Start dashboard only
./run.sh all        # Start both
```

The GUI version (`lora_rx_sdrplay.py`) provides interactive controls and FFT/waterfall displays via Qt.
