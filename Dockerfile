# ==============================================================================
# Stage 1: Builder — compile OOT modules for GNU Radio
# ==============================================================================
FROM debian:bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
    ca-certificates \
    curl \
    gnuradio-dev \
    libspdlog-dev \
    libfmt-dev \
    pybind11-dev \
    python3-dev \
    python3-numpy \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# --- SDRplay API 3.15 (non-interactive install) ---
# Download the Linux .run installer (contains amd64, arm64, armhf), extract, copy ARM64 libs/headers/service
RUN curl -fSL "https://www.sdrplay.com/software/SDRplay_RSP_API-Linux-3.15.2.run" \
        -o /tmp/sdrplay_api.run \
    && chmod +x /tmp/sdrplay_api.run \
    && /tmp/sdrplay_api.run --noexec --target /tmp/sdrplay_extract \
    && cp /tmp/sdrplay_extract/arm64/libsdrplay_api.so.3.15 /usr/local/lib/ \
    && ln -s libsdrplay_api.so.3.15 /usr/local/lib/libsdrplay_api.so.3 \
    && ln -s libsdrplay_api.so.3 /usr/local/lib/libsdrplay_api.so \
    && cp /tmp/sdrplay_extract/inc/* /usr/local/include/ \
    && cp /tmp/sdrplay_extract/arm64/sdrplay_apiService /usr/local/bin/ \
    && chmod +x /usr/local/bin/sdrplay_apiService \
    && ldconfig \
    && rm -rf /tmp/sdrplay_api.run /tmp/sdrplay_extract

# --- gr-sdrplay3 ---
RUN git clone --depth 1 --branch v3.11.0.9 \
        https://github.com/fventuri/gr-sdrplay3.git /build/gr-sdrplay3 \
    && cd /build/gr-sdrplay3 \
    && mkdir build && cd build \
    && cmake -DCMAKE_INSTALL_PREFIX=/usr/local .. \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig

# --- gr-lora_sdr ---
RUN git clone --depth 1 https://github.com/tapparelj/gr-lora_sdr.git /build/gr-lora_sdr \
    && cd /build/gr-lora_sdr \
    && mkdir build && cd build \
    && cmake -DCMAKE_INSTALL_PREFIX=/usr/local .. \
    && make -j"$(nproc)" \
    && make install \
    && ldconfig


# ==============================================================================
# Stage 2: Runtime — slim image with only what's needed to run
# ==============================================================================
FROM debian:bookworm-slim AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnuradio \
    python3 \
    python3-pip \
    python3-zmq \
    python3-numpy \
    python3-scipy \
    python3-matplotlib \
    libusb-1.0-0 \
    udev \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Copy SDRplay API libs + service from builder
COPY --from=builder /usr/local/lib/libsdrplay_api.so* /usr/local/lib/
COPY --from=builder /usr/local/include/sdrplay_api*.h /usr/local/include/
COPY --from=builder /usr/local/bin/sdrplay_apiService /usr/local/bin/

# Copy compiled gr-sdrplay3 module from builder
COPY --from=builder /usr/local/lib/python3.11/dist-packages/gnuradio/sdrplay3/ /usr/local/lib/python3.11/dist-packages/gnuradio/sdrplay3/
COPY --from=builder /usr/local/lib/aarch64-linux-gnu/libgnuradio-sdrplay3* /usr/local/lib/aarch64-linux-gnu/

# Copy compiled gr-lora_sdr module from builder
COPY --from=builder /usr/local/lib/python3.11/dist-packages/gnuradio/lora_sdr/ /usr/local/lib/python3.11/dist-packages/gnuradio/lora_sdr/
COPY --from=builder /usr/local/lib/aarch64-linux-gnu/libgnuradio-lora_sdr* /usr/local/lib/aarch64-linux-gnu/

RUN ldconfig

# Install Python deps (ones not available as system packages)
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
    flask flask-sock cryptography meshtastic protobuf

# Application files
WORKDIR /app
COPY lora_rx_sdrplay_headless.py dashboard.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/app/entrypoint.sh"]
