# Meshtastic SDR RX — RPi5 deployment orchestration
# Usage: just <recipe>

host := "sam@rpi5.local"

# ── One-time Pi setup ──────────────────────────────────────────────

# Install Docker and dependencies on the Pi
provision:
    ssh {{host}} 'sudo apt-get update && sudo apt-get install -y ca-certificates curl git && sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc && sudo chmod a+r /etc/apt/keyrings/docker.asc && echo "deb [arch=arm64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian trixie stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null && sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin && sudo usermod -aG docker sam'
    @echo "Provisioned. If docker group was just added, the Pi needs a reboot or re-login."

# Full first-time setup: provision + first build + start
setup: provision dev

# ── Dev iteration (build on Pi's Docker daemon over SSH) ─────────

# Build image on Pi using local source as build context
build:
    DOCKER_HOST=ssh://{{host}} docker compose build

# Start containers on the Pi
up:
    DOCKER_HOST=ssh://{{host}} docker compose up -d

# Full dev cycle: build + up
dev: build up

# ── Release (pull from GHCR) ────────────────────────────────────

# Pull latest image from GHCR on the Pi
pull:
    DOCKER_HOST=ssh://{{host}} docker compose pull

# Pull + restart
deploy: pull up

# ── Management ───────────────────────────────────────────────────

# Stop containers
down:
    DOCKER_HOST=ssh://{{host}} docker compose down

# Restart containers
restart:
    DOCKER_HOST=ssh://{{host}} docker compose restart

# Show container status
status:
    DOCKER_HOST=ssh://{{host}} docker compose ps

# Show recent logs
logs:
    DOCKER_HOST=ssh://{{host}} docker compose logs --tail=100

# Follow logs (Ctrl-C to stop)
logs-follow:
    DOCKER_HOST=ssh://{{host}} docker compose logs -f

# Verify SDRplay is visible on USB
check-usb:
    ssh {{host}} 'lsusb | grep -i sdrplay || echo "SDRplay not found on USB"'

# Interactive SSH shell
ssh:
    ssh {{host}}

# ── Cleanup ──────────────────────────────────────────────────────

# Stop containers and prune images
uninstall:
    DOCKER_HOST=ssh://{{host}} docker compose down || true
    ssh {{host}} 'docker image prune -af'
