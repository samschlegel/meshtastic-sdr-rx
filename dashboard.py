#!/usr/bin/env python3
"""Meshtastic LoRa packet spectrogram web dashboard.

Subscribes to:
  - ZMQ port 20003: raw IQ samples (complex float32) from the SDRplay source
  - ZMQ port 20002: decoded packet bytes from gr-lora_sdr crc_verif output

The IQ thread detects RF events via energy thresholding and stores captured IQ
segments.  Decoded packets are matched to captures for spectrogram generation.

Real-time updates via WebSocket.  Debug panel shows live power level, noise
floor, threshold, and tuning controls.

Usage:
    C:\\ProgramData\\radioconda\\python.exe dashboard.py
    # then open http://localhost:5000
"""

import io
import json
import os
import queue
import threading
import time
import uuid
from base64 import b64decode
from collections import deque
from datetime import datetime

import matplotlib
import numpy as np
from flask import Flask, Response, render_template_string
from flask_sock import Sock
from scipy.signal import spectrogram as scipy_spectrogram

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import zmq
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

try:
    from meshtastic import mesh_pb2, telemetry_pb2
except ImportError:
    mesh_pb2 = None
    telemetry_pb2 = None

# ---------------------------------------------------------------------------
# Configuration (runtime-tunable values in `config` dict below)
# ---------------------------------------------------------------------------
SAMP_RATE = 1_000_000
CENTER_FREQ = 913.125e6
BW = 250_000
SF = 9
IQ_ZMQ_ADDR = os.environ.get("IQ_ZMQ_ADDR", "tcp://127.0.0.1:20003")
PKT_ZMQ_ADDR = os.environ.get("PKT_ZMQ_ADDR", "tcp://127.0.0.1:20002")
AES_KEY_B64 = os.environ.get("MESHTASTIC_AES_KEY", "1PG7OiApB1nwvP+rz05pAQ==")
MAX_PACKETS = 50
BLOCK = 1024  # ~1 ms at 1 MS/s
MAX_CAPTURE_SAMPLES = int(2 * SAMP_RATE)
MAX_CAPTURES = 20

# Runtime-tunable config (read by IQ thread, written by WS handler)
config = {
    "threshold_db": 20.0,
    "lookback_ms": 60,
    "quiet_ms": 40,
}
config_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
captures_lock = threading.Lock()
iq_captures = deque(maxlen=MAX_CAPTURES)

packets_lock = threading.Lock()
packets = []

# Power telemetry for debug chart (IQ thread writes, broadcast reads)
power_log = deque(maxlen=2000)

# Event bus for WS broadcast
event_queue = deque(maxlen=500)
event_queue_lock = threading.Lock()

# Per-client message queues (broadcast thread writes, WS handler reads+sends)
ws_queues = []
ws_queues_lock = threading.Lock()

# Capture stats
capture_stats = {"total": 0, "unmatched": 0}
capture_stats_lock = threading.Lock()


def push_event(evt: dict):
    with event_queue_lock:
        event_queue.append(evt)


# ---------------------------------------------------------------------------
# Meshtastic helpers
# ---------------------------------------------------------------------------
AES_KEY = b64decode(AES_KEY_B64.encode("ascii"))


def msb2lsb(hex8: str) -> str:
    return hex8[6:8] + hex8[4:6] + hex8[2:4] + hex8[0:2]


def extract_packet(raw_hex: str) -> dict:
    return {
        "dest": msb2lsb(raw_hex[0:8]),
        "sender": msb2lsb(raw_hex[8:16]),
        "packet_id": msb2lsb(raw_hex[16:24]),
        "flags": raw_hex[24:26],
        "channel_hash": raw_hex[26:28],
        "data_hex": raw_hex[32:],
        "data_bytes": bytes.fromhex(raw_hex[32:]),
        "sender_bytes": bytes.fromhex(raw_hex[8:16]),
        "packet_id_bytes": bytes.fromhex(raw_hex[16:24]),
    }


def decrypt_packet(pkt: dict) -> bytes:
    nonce = (
        pkt["packet_id_bytes"]
        + b"\x00\x00\x00\x00"
        + pkt["sender_bytes"]
        + b"\x00\x00\x00\x00"
    )
    cipher = Cipher(
        algorithms.AES(AES_KEY), modes.CTR(nonce), backend=default_backend()
    )
    dec = cipher.decryptor()
    return dec.update(pkt["data_bytes"]) + dec.finalize()


PORTNUM_NAMES = {
    0: "UNKNOWN_APP",
    1: "TEXT_MESSAGE_APP",
    3: "POSITION_APP",
    4: "NODEINFO_APP",
    5: "ROUTING_APP",
    6: "ADMIN_APP",
    32: "REPLY_APP",
    34: "PAXCOUNTER_APP",
    65: "STORE_FORWARD_APP",
    67: "TELEMETRY_APP",
    70: "TRACEROUTE_APP",
    71: "NEIGHBORINFO_APP",
    73: "MAP_REPORT_APP",
}


def decode_protobuf(decrypted: bytes, sender: str, dest: str) -> tuple:
    if mesh_pb2 is None:
        return ("UNKNOWN", decrypted.hex())
    data = mesh_pb2.Data()
    try:
        data.ParseFromString(decrypted)
    except Exception:
        return ("INVALID_PROTOBUF", decrypted.hex())
    portnum = data.portnum
    name = PORTNUM_NAMES.get(portnum, f"PORT_{portnum}")
    try:
        if portnum == 1:
            text = data.payload.decode("utf-8", errors="replace")
            summary = text if dest == "ffffffff" else "(direct message)"
        elif portnum == 3:
            pos = mesh_pb2.Position()
            pos.ParseFromString(data.payload)
            summary = f"{pos.latitude_i * 1e-7:.5f}, {pos.longitude_i * 1e-7:.5f}"
        elif portnum == 4:
            info = mesh_pb2.User()
            info.ParseFromString(data.payload)
            summary = f"{info.long_name} ({info.short_name})"
        elif portnum == 67 and telemetry_pb2:
            env = telemetry_pb2.Telemetry()
            env.ParseFromString(data.payload)
            summary = str(env).replace("\n", " ")[:120]
        elif portnum == 71:
            ninfo = mesh_pb2.NeighborInfo()
            ninfo.ParseFromString(data.payload)
            summary = str(ninfo).replace("\n", " ")[:120]
        elif portnum == 70:
            tr = mesh_pb2.RouteDiscovery()
            tr.ParseFromString(data.payload)
            summary = str(tr).replace("\n", " ")[:120]
        else:
            summary = data.payload.hex()[:80]
    except Exception as e:
        summary = f"decode error: {e}"
    return (name, summary)


# ---------------------------------------------------------------------------
# Spectrogram generation
# ---------------------------------------------------------------------------
def make_spectrogram_png(iq_samples: np.ndarray) -> bytes:
    if len(iq_samples) < 1024:
        return b""
    nperseg = 1024
    noverlap = 768
    f, t, Sxx = scipy_spectrogram(
        iq_samples,
        fs=SAMP_RATE,
        nperseg=nperseg,
        noverlap=noverlap,
        return_onesided=False,
        window="hann",
    )
    f = np.fft.fftshift(f)
    Sxx = np.fft.fftshift(Sxx, axes=0)
    f_mhz = (f + CENTER_FREQ) / 1e6
    Sxx_dB = 10 * np.log10(Sxx + 1e-12)
    duration_ms = len(iq_samples) / SAMP_RATE * 1000

    fig, ax = plt.subplots(figsize=(8, 3), dpi=100)
    ax.pcolormesh(t * 1000, f_mhz, Sxx_dB, shading="gouraud", cmap="viridis")
    ax.set_ylabel("Frequency (MHz)")
    ax.set_xlabel("Time (ms)")
    ax.set_title(f"{CENTER_FREQ / 1e6:.3f} MHz  |  {duration_ms:.0f} ms capture")
    bw_mhz = BW / 1e6
    center_mhz = CENTER_FREQ / 1e6
    ax.set_ylim(center_mhz - bw_mhz * 0.8, center_mhz + bw_mhz * 0.8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# IQ thread — energy-gated capture
# ---------------------------------------------------------------------------
def iq_thread_func():
    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(IQ_ZMQ_ADDR)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    print(f"[IQ] Subscribed to {IQ_ZMQ_ADDR}")

    state = "idle"
    residual = np.array([], dtype=np.complex64)
    lookback = deque(maxlen=60)
    capture_blocks = []
    capture_start = 0.0
    noise_powers = deque(maxlen=500)
    noise_floor = -60.0
    quiet_count = 0
    capture_sample_count = 0
    block_counter = 0

    while True:
        try:
            data = sock.recv()
            samples = np.frombuffer(data, dtype=np.complex64)
            if len(residual) > 0:
                samples = np.concatenate([residual, samples])

            n_full = len(samples) // BLOCK
            residual = samples[n_full * BLOCK :].copy()

            # Read current config
            with config_lock:
                threshold_db = config["threshold_db"]
                lookback_blocks = max(1, int(config["lookback_ms"]))
                quiet_blocks = max(1, int(config["quiet_ms"]))

            if lookback.maxlen != lookback_blocks:
                lookback = deque(lookback, maxlen=lookback_blocks)

            for i in range(n_full):
                block = samples[i * BLOCK : (i + 1) * BLOCK]
                power_db = 10.0 * np.log10(
                    np.mean(block.real**2 + block.imag**2) + 1e-12
                )

                # Log power for debug chart (~5/sec at every 200 blocks)
                block_counter += 1
                if block_counter % 200 == 0:
                    power_log.append(
                        {
                            "ts": time.time(),
                            "power_db": round(float(power_db), 2),
                            "noise_floor": round(float(noise_floor), 2),
                            "threshold": round(float(noise_floor + threshold_db), 2),
                            "state": state,
                        }
                    )

                if state == "idle":
                    lookback.append(block.copy())
                    noise_powers.append(power_db)
                    if len(noise_powers) >= 30:
                        noise_floor = float(np.median(list(noise_powers)))

                    if power_db > noise_floor + threshold_db:
                        capture_blocks = list(lookback)
                        capture_blocks.append(block.copy())
                        capture_sample_count = len(capture_blocks) * BLOCK
                        capture_start = time.time()
                        state = "capturing"
                        quiet_count = 0
                        push_event(
                            {
                                "type": "capture",
                                "event": "start",
                                "power_db": round(float(power_db), 2),
                                "noise_floor": round(float(noise_floor), 2),
                            }
                        )
                        print(
                            f"[IQ] Trigger at {power_db:.1f} dB "
                            f"(floor {noise_floor:.1f} dB)"
                        )

                elif state == "capturing":
                    capture_blocks.append(block.copy())
                    capture_sample_count += BLOCK

                    if power_db < noise_floor + threshold_db * 0.5:
                        quiet_count += 1
                    else:
                        quiet_count = 0

                    if (
                        quiet_count >= quiet_blocks
                        or capture_sample_count > MAX_CAPTURE_SAMPLES
                    ):
                        arr = np.concatenate(capture_blocks)
                        duration_ms = len(arr) / SAMP_RATE * 1000
                        with captures_lock:
                            iq_captures.append(
                                {
                                    "time": capture_start,
                                    "samples": arr,
                                    "matched": False,
                                }
                            )
                        with capture_stats_lock:
                            capture_stats["total"] += 1
                            capture_stats["unmatched"] += 1
                        push_event(
                            {
                                "type": "capture",
                                "event": "end",
                                "duration_ms": round(duration_ms, 1),
                            }
                        )
                        print(f"[IQ] Capture done: {duration_ms:.0f} ms")
                        capture_blocks = []
                        capture_sample_count = 0
                        state = "idle"
                        lookback.clear()
                        noise_powers.clear()
                        quiet_count = 0

        except Exception as e:
            print(f"[IQ] Error: {e}")
            time.sleep(1)


# ---------------------------------------------------------------------------
# Packet thread — decode + match to IQ capture
# ---------------------------------------------------------------------------
def packet_thread_func():
    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(PKT_ZMQ_ADDR)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    print(f"[PKT] Subscribed to {PKT_ZMQ_ADDR}")

    while True:
        try:
            msg = sock.recv()
            ts_now = time.time()
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            raw_hex = msg.hex()
            if len(raw_hex) < 32:
                continue

            try:
                pkt = extract_packet(raw_hex)
                decrypted = decrypt_packet(pkt)
                portnum_name, summary = decode_protobuf(
                    decrypted, pkt["sender"], pkt["dest"]
                )
            except Exception as e:
                pkt = {"sender": "????????", "dest": "????????"}
                portnum_name = "ERROR"
                summary = str(e)

            iq_snap = np.array([], dtype=np.complex64)
            matched = False
            with captures_lock:
                for cap in reversed(iq_captures):
                    if not cap["matched"] and (ts_now - cap["time"]) < 15:
                        cap["matched"] = True
                        iq_snap = cap["samples"]
                        matched = True
                        break
            if matched:
                with capture_stats_lock:
                    capture_stats["unmatched"] = max(
                        0, capture_stats["unmatched"] - 1
                    )

            png_bytes = make_spectrogram_png(iq_snap)

            pkt_id = str(uuid.uuid4())[:8]
            iq_dur = len(iq_snap) / SAMP_RATE * 1000
            entry = {
                "id": pkt_id,
                "timestamp": ts_str,
                "sender": pkt.get("sender", "?"),
                "dest": pkt.get("dest", "?"),
                "portnum": portnum_name,
                "summary": summary,
                "iq_duration_ms": iq_dur,
                "spectrogram_png": png_bytes,
            }

            with packets_lock:
                packets.insert(0, entry)
                if len(packets) > MAX_PACKETS:
                    packets.pop()

            push_event(
                {
                    "type": "packet",
                    "id": pkt_id,
                    "timestamp": ts_str,
                    "sender": pkt.get("sender", "?"),
                    "dest": pkt.get("dest", "?"),
                    "portnum": portnum_name,
                    "summary": summary,
                    "iq_duration_ms": round(iq_dur, 1),
                    "has_spectrogram": len(png_bytes) > 0,
                }
            )

            print(
                f"[PKT] {ts_str} {pkt.get('sender', '?')} -> "
                f"{pkt.get('dest', '?')} {portnum_name}: "
                f"{summary[:50]}  [{iq_dur:.0f} ms]"
            )

        except Exception as e:
            print(f"[PKT] Error: {e}")
            time.sleep(1)


# ---------------------------------------------------------------------------
# WebSocket broadcast thread
# ---------------------------------------------------------------------------
def broadcast_thread_func():
    """Drain power_log and event_queue every 200 ms, enqueue for all WS clients."""
    while True:
        time.sleep(0.2)
        messages = []

        # Drain power log
        batch = []
        while power_log:
            try:
                batch.append(power_log.popleft())
            except IndexError:
                break
        if batch:
            messages.append(json.dumps({"type": "power", "readings": batch}))

        # Drain event queue
        while True:
            try:
                with event_queue_lock:
                    if not event_queue:
                        break
                    evt = event_queue.popleft()
                messages.append(json.dumps(evt))
            except IndexError:
                break

        # Stats
        with capture_stats_lock:
            stats = dict(capture_stats)
        messages.append(
            json.dumps(
                {
                    "type": "stats",
                    "captures_total": stats["total"],
                    "captures_unmatched": stats["unmatched"],
                }
            )
        )

        if not messages:
            continue

        # Put messages into each client's queue (client thread does the send)
        with ws_queues_lock:
            for q in ws_queues:
                for msg in messages:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass  # drop if client can't keep up


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)
sock_ext = Sock(app)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meshtastic LoRa Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', system-ui, sans-serif; background: #0d1117; color: #c9d1d9; }
  header { background: #161b22; padding: 16px 24px; border-bottom: 1px solid #30363d; }
  header h1 { font-size: 20px; font-weight: 600; }
  header .subtitle { font-size: 13px; color: #8b949e; margin-top: 2px; }
  #status { float: right; font-size: 13px; color: #8b949e; line-height: 28px; }
  #status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                 margin-right: 6px; vertical-align: middle; }
  #status .dot.connected { background: #3fb950; }
  #status .dot.disconnected { background: #f85149; }

  /* Debug panel */
  #debug-panel { background: #161b22; border-bottom: 1px solid #30363d; }
  #debug-toggle { display: block; width: 100%; padding: 8px 24px; background: none;
                  border: none; color: #8b949e; font-size: 13px; cursor: pointer;
                  text-align: left; }
  #debug-toggle:hover { color: #c9d1d9; }
  #debug-content { padding: 0 24px 16px; display: none; }
  #debug-content.open { display: block; }
  #debug-top { display: flex; gap: 24px; align-items: flex-start; flex-wrap: wrap; }
  #chart-wrap { flex: 1; min-width: 400px; }
  #power-chart { width: 100%; height: 150px; background: #0d1117; border: 1px solid #30363d;
                 border-radius: 4px; }
  #debug-stats { min-width: 180px; font-size: 13px; line-height: 2; }
  #debug-stats .label { color: #8b949e; }
  #debug-stats .val { color: #e6edf3; font-family: 'Cascadia Code','Consolas',monospace; }
  #debug-stats .val.capturing { color: #3fb950; font-weight: 600; }
  .sliders { display: flex; gap: 24px; margin-top: 12px; flex-wrap: wrap; }
  .slider-group { flex: 1; min-width: 200px; }
  .slider-group label { display: block; font-size: 12px; color: #8b949e; margin-bottom: 4px; }
  .slider-group .slider-row { display: flex; align-items: center; gap: 8px; }
  .slider-group input[type=range] { flex: 1; accent-color: #58a6ff; }
  .slider-group .slider-val { font-family: 'Cascadia Code','Consolas',monospace;
                               font-size: 13px; min-width: 50px; text-align: right; }
  .chart-legend { display: flex; gap: 16px; margin-top: 4px; font-size: 11px; color: #8b949e; }
  .chart-legend span::before { content: ''; display: inline-block; width: 16px; height: 2px;
                                margin-right: 4px; vertical-align: middle; }
  .legend-power::before { background: #58a6ff; }
  .legend-floor::before { background: #f0883e; }
  .legend-thresh::before { background: #f85149; }

  /* Packets */
  main { max-width: 1100px; margin: 24px auto; padding: 0 16px; }
  .empty { text-align: center; color: #8b949e; padding: 80px 0; font-size: 15px; }
  .packet { background: #161b22; border: 1px solid #30363d; border-radius: 8px;
            margin-bottom: 16px; overflow: hidden; }
  .packet-header { display: flex; flex-wrap: wrap; gap: 16px; padding: 12px 16px;
                   align-items: center; border-bottom: 1px solid #21262d; }
  .packet-header .time { color: #8b949e; font-size: 13px; min-width: 140px; }
  .packet-header .addr { font-family: 'Cascadia Code','Consolas',monospace; font-size: 13px; }
  .packet-header .sender { color: #58a6ff; }
  .packet-header .arrow { color: #484f58; margin: 0 4px; }
  .packet-header .dest { color: #bc8cff; }
  .packet-header .duration { color: #8b949e; font-size: 12px; }
  .tag { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px;
         font-weight: 600; background: #1f6feb33; color: #58a6ff; border: 1px solid #1f6feb55; }
  .tag.text { background: #3fb95033; color: #3fb950; border-color: #3fb95055; }
  .tag.position { background: #d2992033; color: #d29922; border-color: #d2992255; }
  .tag.telemetry { background: #bc8cff33; color: #bc8cff; border-color: #bc8cff55; }
  .tag.nodeinfo { background: #f7814833; color: #f78148; border-color: #f7814855; }
  .summary { padding: 8px 16px; font-size: 14px; color: #e6edf3;
             font-family: 'Cascadia Code','Consolas',monospace; word-break: break-all; }
  .spectrogram { padding: 8px; text-align: center; background: #0d1117; }
  .spectrogram img { max-width: 100%; border-radius: 4px; }
  .no-spec { padding: 12px 16px; color: #484f58; font-size: 12px; font-style: italic; }
</style>
</head>
<body>
<header>
  <div id="status">
    <span class="dot disconnected" id="ws-dot"></span>
    <span id="count">0</span> packets
  </div>
  <h1>Meshtastic LoRa Dashboard</h1>
  <div class="subtitle">913.125 MHz &middot; SF9 BW250k &middot; energy-gated spectrogram</div>
</header>

<div id="debug-panel">
  <button id="debug-toggle">Debug &#9660;</button>
  <div id="debug-content">
    <div id="debug-top">
      <div id="chart-wrap">
        <canvas id="power-chart"></canvas>
        <div class="chart-legend">
          <span class="legend-power">Power</span>
          <span class="legend-floor">Noise floor</span>
          <span class="legend-thresh">Threshold</span>
        </div>
      </div>
      <div id="debug-stats">
        <div><span class="label">State: </span><span class="val" id="st-state">--</span></div>
        <div><span class="label">Power: </span><span class="val" id="st-power">-- dB</span></div>
        <div><span class="label">Floor: </span><span class="val" id="st-floor">-- dB</span></div>
        <div><span class="label">Captures: </span><span class="val" id="st-captures">0</span></div>
        <div><span class="label">Unmatched: </span><span class="val" id="st-unmatched">0</span></div>
      </div>
    </div>
    <div class="sliders">
      <div class="slider-group">
        <label>Threshold (dB above noise)</label>
        <div class="slider-row">
          <input type="range" id="sl-threshold" min="0.5" max="30" step="0.5" value="20.0">
          <span class="slider-val" id="sv-threshold">20.0</span>
        </div>
      </div>
      <div class="slider-group">
        <label>Lookback (ms before trigger)</label>
        <div class="slider-row">
          <input type="range" id="sl-lookback" min="10" max="200" step="5" value="60">
          <span class="slider-val" id="sv-lookback">60</span>
        </div>
      </div>
      <div class="slider-group">
        <label>Quiet end (ms to end capture)</label>
        <div class="slider-row">
          <input type="range" id="sl-quiet" min="10" max="200" step="5" value="40">
          <span class="slider-val" id="sv-quiet">40</span>
        </div>
      </div>
    </div>
  </div>
</div>

<main id="packets">
  <div class="empty" id="empty-msg">Waiting for packets&hellip;</div>
</main>

<script>
// ---- WebSocket ----
let ws = null;
let reconnectTimer = null;
const TAG_CLASSES = {
  TEXT_MESSAGE_APP: 'text', POSITION_APP: 'position',
  TELEMETRY_APP: 'telemetry', NODEINFO_APP: 'nodeinfo'
};
let knownIds = new Set();
let packetCount = 0;

function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onopen = () => {
    document.getElementById('ws-dot').className = 'dot connected';
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  };
  ws.onclose = () => {
    document.getElementById('ws-dot').className = 'dot disconnected';
    reconnectTimer = setTimeout(connectWS, 2000);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'power') handlePower(msg.readings);
    else if (msg.type === 'packet') handlePacket(msg);
    else if (msg.type === 'stats') handleStats(msg);
    else if (msg.type === 'capture') handleCapture(msg);
  };
}

function sendConfig() {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({
    type: 'config',
    threshold_db: parseFloat(document.getElementById('sl-threshold').value),
    lookback_ms: parseInt(document.getElementById('sl-lookback').value),
    quiet_ms: parseInt(document.getElementById('sl-quiet').value),
  }));
}

// ---- Debug panel toggle ----
document.getElementById('debug-toggle').addEventListener('click', () => {
  const c = document.getElementById('debug-content');
  const open = c.classList.toggle('open');
  document.getElementById('debug-toggle').innerHTML = 'Debug ' + (open ? '&#9650;' : '&#9660;');
});

// ---- Sliders ----
['threshold', 'lookback', 'quiet'].forEach(name => {
  const sl = document.getElementById('sl-' + name);
  const sv = document.getElementById('sv-' + name);
  sl.addEventListener('input', () => {
    sv.textContent = sl.value;
    sendConfig();
  });
});

// ---- Power chart ----
const chartCanvas = document.getElementById('power-chart');
const chartCtx = chartCanvas.getContext('2d');
const CHART_SECONDS = 10;
const CHART_MAX_POINTS = 200;
let chartData = [];

function handlePower(readings) {
  for (const r of readings) chartData.push(r);
  const cutoff = Date.now() / 1000 - CHART_SECONDS;
  while (chartData.length > 0 && chartData[0].ts < cutoff) chartData.shift();
  if (chartData.length > CHART_MAX_POINTS) chartData = chartData.slice(-CHART_MAX_POINTS);
  if (readings.length > 0) {
    const last = readings[readings.length - 1];
    document.getElementById('st-power').textContent = last.power_db.toFixed(1) + ' dB';
    document.getElementById('st-floor').textContent = last.noise_floor.toFixed(1) + ' dB';
    const stEl = document.getElementById('st-state');
    stEl.textContent = last.state.toUpperCase();
    stEl.className = last.state === 'capturing' ? 'val capturing' : 'val';
  }
  drawChart();
}

function drawChart() {
  const W = chartCanvas.clientWidth;
  const H = chartCanvas.clientHeight;
  if (W === 0 || H === 0) return;
  const dpr = window.devicePixelRatio || 1;
  chartCanvas.width = W * dpr;
  chartCanvas.height = H * dpr;
  chartCtx.setTransform(dpr, 0, 0, dpr, 0, 0);

  if (chartData.length < 2) {
    chartCtx.fillStyle = '#0d1117';
    chartCtx.fillRect(0, 0, W, H);
    chartCtx.fillStyle = '#484f58';
    chartCtx.font = '12px system-ui';
    chartCtx.textAlign = 'center';
    chartCtx.fillText('Waiting for IQ data...', W / 2, H / 2);
    return;
  }

  let allVals = [];
  for (const d of chartData) allVals.push(d.power_db, d.noise_floor, d.threshold);
  let yMin = Math.min(...allVals) - 2;
  let yMax = Math.max(...allVals) + 2;
  if (yMax - yMin < 10) { const mid = (yMax + yMin) / 2; yMin = mid - 5; yMax = mid + 5; }

  const pad = { l: 45, r: 10, t: 8, b: 20 };
  const cw = W - pad.l - pad.r;
  const ch = H - pad.t - pad.b;
  const tMin = chartData[0].ts;
  const tMax = chartData[chartData.length - 1].ts;
  const tRange = Math.max(tMax - tMin, 1);
  const toX = t => pad.l + (t - tMin) / tRange * cw;
  const toY = v => pad.t + (1 - (v - yMin) / (yMax - yMin)) * ch;

  chartCtx.fillStyle = '#0d1117';
  chartCtx.fillRect(0, 0, W, H);

  // Highlight capturing regions
  chartCtx.fillStyle = 'rgba(63, 185, 80, 0.08)';
  let inCap = false, capX = 0;
  for (const d of chartData) {
    if (d.state === 'capturing' && !inCap) { capX = toX(d.ts); inCap = true; }
    if (d.state !== 'capturing' && inCap) {
      chartCtx.fillRect(capX, pad.t, toX(d.ts) - capX, ch);
      inCap = false;
    }
  }
  if (inCap) chartCtx.fillRect(capX, pad.t, toX(tMax) - capX, ch);

  // Grid
  chartCtx.strokeStyle = '#21262d';
  chartCtx.lineWidth = 1;
  const yStep = Math.max(1, Math.ceil((yMax - yMin) / 5));
  chartCtx.font = '10px system-ui';
  chartCtx.fillStyle = '#484f58';
  chartCtx.textAlign = 'right';
  for (let v = Math.ceil(yMin); v <= yMax; v += yStep) {
    const y = toY(v);
    chartCtx.beginPath(); chartCtx.moveTo(pad.l, y); chartCtx.lineTo(W - pad.r, y); chartCtx.stroke();
    chartCtx.fillText(v + ' dB', pad.l - 4, y + 3);
  }

  function drawLine(key, color, lw, dash) {
    chartCtx.strokeStyle = color;
    chartCtx.lineWidth = lw;
    chartCtx.setLineDash(dash || []);
    chartCtx.beginPath();
    let first = true;
    for (const d of chartData) {
      const x = toX(d.ts), y = toY(d[key]);
      if (first) { chartCtx.moveTo(x, y); first = false; }
      else chartCtx.lineTo(x, y);
    }
    chartCtx.stroke();
    chartCtx.setLineDash([]);
  }

  drawLine('threshold', '#f85149', 1, [4, 4]);
  drawLine('noise_floor', '#f0883e', 1);
  drawLine('power_db', '#58a6ff', 1.5);
}

// ---- Stats ----
function handleStats(msg) {
  document.getElementById('st-captures').textContent = msg.captures_total;
  document.getElementById('st-unmatched').textContent = msg.captures_unmatched;
}

// ---- Captures ----
function handleCapture(msg) {
  if (msg.event === 'start') {
    document.getElementById('st-state').textContent = 'CAPTURING';
    document.getElementById('st-state').className = 'val capturing';
  }
}

// ---- Packets ----
function handlePacket(p) {
  packetCount++;
  document.getElementById('count').textContent = packetCount;
  const el = document.getElementById('empty-msg');
  if (el) el.remove();
  const container = document.getElementById('packets');
  container.insertAdjacentHTML('afterbegin', renderPacket(p));
  while (container.children.length > 50) container.lastElementChild.remove();
}

function renderPacket(p) {
  const tagCls = TAG_CLASSES[p.portnum] || '';
  const durStr = p.iq_duration_ms > 0 ? p.iq_duration_ms.toFixed(0) + ' ms' : '';
  let specHtml;
  if (p.has_spectrogram) {
    specHtml = `<div class="spectrogram"><img src="/api/spectrogram/${p.id}" loading="lazy"></div>`;
  } else {
    specHtml = `<div class="no-spec">No IQ capture matched</div>`;
  }
  return `<div class="packet">
    <div class="packet-header">
      <span class="time">${p.timestamp}</span>
      <span class="addr">
        <span class="sender">!${p.sender}</span>
        <span class="arrow">&rarr;</span>
        <span class="dest">!${p.dest}</span>
      </span>
      <span class="tag ${tagCls}">${p.portnum}</span>
      <span class="duration">${durStr}</span>
    </div>
    <div class="summary">${escapeHtml(p.summary)}</div>
    ${specHtml}
  </div>`;
}

function escapeHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

// ---- Init ----
connectWS();

// Load existing packets on page load
fetch('/api/packets').then(r => r.json()).then(data => {
  packetCount = data.length;
  document.getElementById('count').textContent = packetCount;
  if (data.length > 0) {
    const el = document.getElementById('empty-msg');
    if (el) el.remove();
  }
  const container = document.getElementById('packets');
  for (const p of data.slice().reverse()) {
    knownIds.add(p.id);
    container.insertAdjacentHTML('afterbegin', renderPacket(p));
  }
}).catch(() => {});
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/packets")
def api_packets():
    with packets_lock:
        return [
            {
                "id": p["id"],
                "timestamp": p["timestamp"],
                "sender": p["sender"],
                "dest": p["dest"],
                "portnum": p["portnum"],
                "summary": p["summary"],
                "iq_duration_ms": round(p["iq_duration_ms"], 1),
                "has_spectrogram": len(p["spectrogram_png"]) > 0,
            }
            for p in packets
        ]


@app.route("/api/spectrogram/<pkt_id>")
def api_spectrogram(pkt_id):
    with packets_lock:
        for p in packets:
            if p["id"] == pkt_id:
                png = p["spectrogram_png"]
                if png:
                    return Response(png, mimetype="image/png")
                break
    return Response(status=404)


@sock_ext.route("/ws")
def ws_handler(ws_conn):
    client_q = queue.Queue(maxsize=2000)
    with ws_queues_lock:
        ws_queues.append(client_q)
    print("[WS] Client connected")
    try:
        while True:
            # Send all pending messages from our queue
            while True:
                try:
                    msg = client_q.get_nowait()
                    ws_conn.send(msg)
                except queue.Empty:
                    break

            # Short blocking receive — doubles as the loop sleep
            try:
                data = ws_conn.receive(timeout=0.2)
            except Exception:
                break  # connection closed
            if data is not None:
                try:
                    msg = json.loads(data)
                    if msg.get("type") == "config":
                        with config_lock:
                            if "threshold_db" in msg:
                                config["threshold_db"] = float(msg["threshold_db"])
                            if "lookback_ms" in msg:
                                config["lookback_ms"] = int(msg["lookback_ms"])
                            if "quiet_ms" in msg:
                                config["quiet_ms"] = int(msg["quiet_ms"])
                        print(f"[WS] Config updated: {config}")
                except (json.JSONDecodeError, ValueError):
                    pass
    except Exception:
        pass
    finally:
        with ws_queues_lock:
            ws_queues.remove(client_q)
        print("[WS] Client disconnected")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    threading.Thread(target=iq_thread_func, daemon=True).start()
    threading.Thread(target=packet_thread_func, daemon=True).start()
    threading.Thread(target=broadcast_thread_func, daemon=True).start()

    print("Dashboard starting at http://localhost:5000")
    with config_lock:
        print(f"  Config: {config}")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
