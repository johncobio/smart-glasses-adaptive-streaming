#!/usr/bin/env python3
"""
Custom MentraOS WebSocket Server — Bypasses MentraCloud

Replaces api.mentraglass.com for research purposes.
Uses aiohttp so REST (POST/GET) and WebSocket connections share one port.

Requires: aiohttp, websockets (for reference only)
"""

import asyncio
import csv
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from aiohttp import web, WSMsgType

# ── .env loader ───────────────────────────────────────────────────────────────
def _load_env():
    env_file = Path(__file__).parent / ".env"
    if not env_file.exists():
        return
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key and key not in os.environ:
                os.environ[key] = val

_load_env()

# ── Config ────────────────────────────────────────────────────────────────────
RTMP_URL            = os.environ.get("RTMP_URL",       "rtmp://136.116.71.233:1935/live/stream")
MEDIAMTX_HOST       = os.environ.get("MEDIAMTX_HOST",  "136.116.71.233")
MEDIAMTX_PORT       = int(os.environ.get("MEDIAMTX_PORT", "9997"))
MEDIAMTX_PATH       = os.environ.get("MEDIAMTX_PATH",  "live/stream")
SERVER_HOST         = os.environ.get("SERVER_HOST",    "0.0.0.0")
SERVER_PORT         = int(os.environ.get("SERVER_PORT", "8766"))
LOG_DIR             = Path(os.environ.get("LOG_DIR",   "../backend/logs"))
KEEP_ALIVE_INTERVAL = 10

LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Session log files ─────────────────────────────────────────────────────────
SESSION_TS = datetime.now().strftime("%Y%m%d_%H%M%S")
PERF_LOG    = LOG_DIR / f"{SESSION_TS}_bypass_performance.csv"
STREAM_LOG  = LOG_DIR / f"{SESSION_TS}_bypass_stream_events.csv"
LATENCY_LOG = LOG_DIR / f"{SESSION_TS}_bypass_latency.csv"

PERF_FIELDS = [
    "timestamp_iso", "timestamp_ms",
    "glasses_connected", "stream_status",
    "glasses_battery_level", "glasses_battery_charging",
    "head_position",
    "mediamtx_ready", "mediamtx_encoded_bitrate_bps",
    "mediamtx_delivered_bitrate_bps", "mediamtx_bytes_received",
    "mediamtx_bytes_sent", "mediamtx_active_readers",
    "startup_delay_ms", "mediamtx_ingest_latency_ms",
    "server_processing_ms",
]
LATENCY_FIELDS = ["timestamp_iso", "event", "elapsed_from_start_ms", "elapsed_from_prev_ms", "notes"]
STREAM_FIELDS  = ["timestamp_iso", "timestamp_ms", "status", "stream_id", "rtmp_url", "error"]


def init_csv(path, fields):
    with open(path, "w", newline="") as f:
        csv.DictWriter(f, fieldnames=fields).writeheader()


def append_csv(path, fields, row):
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({k: row.get(k, "") for k in fields})


init_csv(PERF_LOG, PERF_FIELDS)
init_csv(STREAM_LOG, STREAM_FIELDS)
init_csv(LATENCY_LOG, LATENCY_FIELDS)


# ── Session state ─────────────────────────────────────────────────────────────
class SessionState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.session_start_ms       = None
        self.stream_request_sent_ms = None
        self.stream_active_ms       = None
        self.mediamtx_ready_ms      = None
        self.glasses_connected      = False
        self.stream_status          = "idle"
        self.battery_level          = None
        self.battery_charging       = None
        self.last_head_position     = None
        self.latency_events         = []

    def log_latency_event(self, event: str, notes: str = ""):
        now_ms = time.time() * 1000
        elapsed_from_start = (now_ms - self.session_start_ms) if self.session_start_ms else 0
        prev_ts = self.latency_events[-1]["ts_ms"] if self.latency_events else (self.session_start_ms or now_ms)
        elapsed_from_prev = now_ms - prev_ts
        self.latency_events.append({"ts_ms": now_ms, "event": event})
        append_csv(LATENCY_LOG, LATENCY_FIELDS, {
            "timestamp_iso": datetime.now().isoformat(),
            "event": event,
            "elapsed_from_start_ms": f"{elapsed_from_start:.1f}",
            "elapsed_from_prev_ms":  f"{elapsed_from_prev:.1f}",
            "notes": notes,
        })
        print(f"[LATENCY] {event}: +{elapsed_from_start:.0f}ms from start, "
              f"+{elapsed_from_prev:.0f}ms from prev — {notes}")

    def session_summary(self):
        print(f"\n{'─'*55}\nSESSION SUMMARY\n{'─'*55}")
        startup_ms = (self.stream_active_ms - self.stream_request_sent_ms) \
            if self.stream_active_ms and self.stream_request_sent_ms else None
        ingest_ms = (self.mediamtx_ready_ms - self.stream_request_sent_ms) \
            if self.mediamtx_ready_ms and self.stream_request_sent_ms else None
        bt_event = next((e for e in self.latency_events if e["event"] == "glasses_connected"), None)
        bt_ms = (bt_event["ts_ms"] - self.session_start_ms) \
            if bt_event and self.session_start_ms else None
        BASELINE_MS = 3208

        def row(label, val, baseline=None):
            val_str  = f"{val:.0f}ms" if val is not None else "n/a"
            base_str = f"{baseline:.0f}ms" if baseline is not None else ""
            diff_str = ""
            if val is not None and baseline is not None:
                diff = val - baseline
                diff_str = f"  ({'+' if diff > 0 else ''}{diff:.0f}ms)"
            print(f"  {label:<40} {val_str:>12}  {base_str}{diff_str}")

        print(f"  {'Metric':<40} {'Value':>12}  Baseline\n  {'─'*65}")
        row("Bluetooth handshake (phone→glasses)", bt_ms)
        row("Stream startup delay (→SDK active)",  startup_ms, BASELINE_MS)
        row("MediaMTX ingest latency (→VM ready)", ingest_ms,  4025)
        print(f"  {'─'*65}")
        print(f"  Stream status at end: {self.stream_status}")
        print(f"  Battery:              {self.battery_level}%"
              f"  {'(charging)' if self.battery_charging else ''}")
        print(f"  Head position:        {self.last_head_position}")
        if startup_ms is not None:
            saved = BASELINE_MS - startup_ms
            pct   = 100 * saved / BASELINE_MS
            if saved > 0:
                print(f"\n  RESULT: {saved:.0f}ms saved vs baseline ({pct:.0f}% reduction)")
            else:
                print(f"\n  RESULT: {abs(saved):.0f}ms slower than baseline (investigate)")
        print(f"\n  Logs: {PERF_LOG.name}, {LATENCY_LOG.name}\n{'─'*55}\n")


state = SessionState()


# ── MediaMTX polling ──────────────────────────────────────────────────────────
def poll_mediamtx() -> dict:
    try:
        url = f"http://{MEDIAMTX_HOST}:{MEDIAMTX_PORT}/v3/paths/list"
        with urllib.request.urlopen(url, timeout=1.5) as resp:
            data = json.loads(resp.read())
        items = data.get("items", [])
        path  = next((i for i in items if i.get("name") == MEDIAMTX_PATH), None)
        if not path:
            return {"ready": False}
        now_ms = time.time() * 1000
        ready  = path.get("ready", False)
        if ready and state.mediamtx_ready_ms is None and state.stream_request_sent_ms:
            state.mediamtx_ready_ms = now_ms
            ingest_ms = now_ms - state.stream_request_sent_ms
            state.log_latency_event("mediamtx_ready",
                                    f"MediaMTX ready=true, ingest latency={ingest_ms:.0f}ms")
        return {
            "ready": ready,
            "bytesReceived": path.get("bytesReceived", 0),
            "bytesSent":     path.get("bytesSent", 0),
            "activeReaders": len(path.get("readers", [])),
        }
    except Exception:
        return {"ready": False}


# ── Perf snapshot loop ────────────────────────────────────────────────────────
async def perf_loop():
    prev_bytes_rx = prev_bytes_tx = prev_poll_ms = None
    while True:
        await asyncio.sleep(2)
        t0   = time.perf_counter()
        mx   = poll_mediamtx()
        proc = (time.perf_counter() - t0) * 1000
        now_ms = time.time() * 1000

        encoded_bps = delivered_bps = None
        if prev_poll_ms and prev_bytes_rx is not None:
            interval_sec = (now_ms - prev_poll_ms) / 1000
            if interval_sec > 0:
                encoded_bps  = max(0, (mx.get("bytesReceived", 0) - prev_bytes_rx) * 8 / interval_sec)
                delivered_bps = max(0, (mx.get("bytesSent",     0) - prev_bytes_tx) * 8 / interval_sec)

        prev_bytes_rx = mx.get("bytesReceived", 0)
        prev_bytes_tx = mx.get("bytesSent",     0)
        prev_poll_ms  = now_ms

        startup_ms = (state.stream_active_ms - state.stream_request_sent_ms) \
            if state.stream_active_ms and state.stream_request_sent_ms else None
        ingest_ms  = (state.mediamtx_ready_ms - state.stream_request_sent_ms) \
            if state.mediamtx_ready_ms and state.stream_request_sent_ms else None

        append_csv(PERF_LOG, PERF_FIELDS, {
            "timestamp_iso": datetime.now().isoformat(),
            "timestamp_ms":  int(now_ms),
            "glasses_connected":   state.glasses_connected,
            "stream_status":       state.stream_status,
            "glasses_battery_level":    state.battery_level   if state.battery_level   is not None else "",
            "glasses_battery_charging": state.battery_charging if state.battery_charging is not None else "",
            "head_position": state.last_head_position or "",
            "mediamtx_ready":               mx.get("ready", False),
            "mediamtx_encoded_bitrate_bps": f"{encoded_bps:.0f}"  if encoded_bps  is not None else "",
            "mediamtx_delivered_bitrate_bps": f"{delivered_bps:.0f}" if delivered_bps is not None else "",
            "mediamtx_bytes_received": mx.get("bytesReceived", ""),
            "mediamtx_bytes_sent":     mx.get("bytesSent",     ""),
            "mediamtx_active_readers": mx.get("activeReaders", ""),
            "startup_delay_ms":        f"{startup_ms:.0f}" if startup_ms is not None else "",
            "mediamtx_ingest_latency_ms": f"{ingest_ms:.0f}" if ingest_ms is not None else "",
            "server_processing_ms":    f"{proc:.3f}",
        })


# ── Keep-alive loop ───────────────────────────────────────────────────────────
async def keep_alive_loop(ws):
    while True:
        await asyncio.sleep(KEEP_ALIVE_INTERVAL)
        if state.stream_status in ("streaming", "active"):
            try:
                await ws.send_str(json.dumps({
                    "type": "keep_stream_alive",
                    "timestamp": int(time.time() * 1000),
                }))
            except Exception:
                break


async def _send_start_stream(ws, reason: str):
    """Fire start_stream and record latency events."""
    if state.glasses_connected:
        return  # already sent
    state.glasses_connected      = True
    state.stream_request_sent_ms = time.time() * 1000
    state.log_latency_event("glasses_connected", reason)
    await ws.send_str(json.dumps({"type": "start_stream", "streamUrl": RTMP_URL}))
    state.log_latency_event("start_stream_sent", f"rtmpUrl={RTMP_URL}")
    print(f"[SERVER] Sent start_stream ({reason}) → {RTMP_URL}")


async def _start_stream_fallback(ws):
    """Wait 3s for a battery update; if none arrives, fire start_stream anyway."""
    await asyncio.sleep(3)
    if not state.glasses_connected:
        await _send_start_stream(ws, "fallback — no battery update in 3s")


# ── WebSocket connection handler ──────────────────────────────────────────────
async def handle_websocket(request: web.Request) -> web.WebSocketResponse:
    ws     = web.WebSocketResponse(heartbeat=5)
    await ws.prepare(request)

    client = request.remote
    print(f"\n[SERVER] Phone app connected: {client}  path={request.path}")

    state.reset()
    state.session_start_ms = time.time() * 1000
    state.log_latency_event("phone_connected", f"client={client}")

    await ws.send_str(json.dumps({"type": "connection_ack"}))
    state.log_latency_event("connection_ack_sent", "")
    print("[SERVER] Sent connection_ack")

    # Start a fallback task — if no battery update arrives within 3s,
    # fire start_stream anyway (glasses already connected before session).
    asyncio.create_task(_start_stream_fallback(ws))

    keep_alive_task = asyncio.create_task(keep_alive_loop(ws))

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data     = json.loads(msg.data)
                    msg_type = data.get("type", "unknown")

                    if msg_type == "glasses_connection_state":
                        status = data.get("status", "")
                        model  = data.get("deviceModel", "unknown")
                        if status == "CONNECTED":
                            state.glasses_connected      = True
                            state.stream_request_sent_ms = time.time() * 1000
                            state.log_latency_event("glasses_connected", f"model={model}")
                            await ws.send_str(json.dumps({"type": "start_stream", "streamUrl": RTMP_URL}))
                            state.log_latency_event("start_stream_sent", f"rtmpUrl={RTMP_URL}")
                            print(f"[SERVER] Glasses connected ({model}) — sent start_stream → {RTMP_URL}")
                        elif status == "DISCONNECTED":
                            state.glasses_connected = False
                            state.log_latency_event("glasses_disconnected", f"model={model}")
                            print("[SERVER] Glasses disconnected")

                    elif msg_type in ("rtmp_stream_status", "stream_status"):
                        status    = data.get("status", "unknown")
                        now_ms    = time.time() * 1000
                        prev      = state.stream_status
                        state.stream_status = status
                        if status in ("streaming", "active", "reconnected") and prev not in ("streaming", "active"):
                            state.stream_active_ms = now_ms
                            delay_ms = (now_ms - state.stream_request_sent_ms) if state.stream_request_sent_ms else None
                            state.log_latency_event("stream_active",
                                f"startup_delay={delay_ms:.0f}ms" if delay_ms else "startup_delay=unknown")
                        append_csv(STREAM_LOG, STREAM_FIELDS, {
                            "timestamp_iso": datetime.now().isoformat(),
                            "timestamp_ms":  int(now_ms),
                            "status":        status,
                            "stream_id":     data.get("streamId", ""),
                            "rtmp_url":      RTMP_URL,
                            "error":         data.get("error", ""),
                        })
                        print(f"[STREAM] Status: {status}")

                    elif msg_type == "glasses_battery_update":
                        state.battery_level    = data.get("level")
                        state.battery_charging = data.get("charging")
                        print(f"[BATTERY] {state.battery_level}% {'(charging)' if state.battery_charging else ''}")
                        if state.stream_status == "idle":
                            # Either first battery (glasses just connected) or fallback fired
                            # before glasses were ready — retry start_stream either way
                            state.glasses_connected = False  # reset so _send_start_stream fires
                            await _send_start_stream(ws, f"battery confirmed glasses ready ({state.battery_level}%)")

                    elif msg_type == "head_position":
                        state.last_head_position = data.get("position", "unknown")
                        print(f"[HEAD] Position: {state.last_head_position}")

                    elif msg_type == "ping":
                        await ws.send_str(json.dumps({"type": "pong"}))

                    elif msg_type == "VAD":
                        pass

                    else:
                        print(f"[SERVER] Unhandled message type: {msg_type} — {data}")

                except json.JSONDecodeError as e:
                    print(f"[SERVER] JSON error: {e}")

            elif msg.type == WSMsgType.ERROR:
                print(f"[SERVER] WebSocket error: {ws.exception()}")
                break

    finally:
        keep_alive_task.cancel()
        state.log_latency_event("phone_disconnected", "")
        state.session_summary()

    return ws


# ── REST API handler ──────────────────────────────────────────────────────────
async def handle_rest(request: web.Request) -> web.Response:
    path = request.path.split("?")[0]

    def ok(body: str) -> web.Response:
        return web.Response(text=body, content_type="application/json")

    if path == "/health":
        return ok('{"status":"ok"}')
    if path in ("/api/auth/generate-webview-token", "/api/auth/generate-webview-signed-user-token"):
        return ok('{"success":true,"data":{"token":"bypass-token"}}')
    if path == "/auth/exchange-token":
        # App sends Supabase token, expects back a coreToken for WebSocket auth.
        # Our server doesn't validate auth — return a dummy token to unblock the app.
        return ok('{"coreToken":"bypass-token"}')
    if path == "/api/client/min-version":
        return ok('{"success":true,"data":{"required":"0.0.0","recommended":"0.0.0"}}')
    if path == "/api/client/apps":
        return ok('{"success":true,"data":[]}')
    if path in ("/api/client/audio/configure", "/api/client/device/state", "/api/client/goodbye"):
        return ok('{"success":true}')
    if path == "/api/client/livekit/token":
        return ok('{"success":false,"data":null}')
    if path == "/api/client/user/settings":
        return ok('{"success":true,"data":{}}')
    if path.startswith("/api/client/notifications"):
        return ok('{"success":true,"data":[]}')

    print(f"[HTTP] {request.method} {path}")
    return ok('{"success":true,"data":null}')


# ── Entry point ───────────────────────────────────────────────────────────────
async def main():
    print(f"\n{'='*60}")
    print(f"Custom MentraOS WebSocket Server")
    print(f"{'='*60}")
    print(f"Listening on:  ws://{SERVER_HOST}:{SERVER_PORT}/glasses-ws")
    print(f"RTMP target:   {RTMP_URL}")
    print(f"MediaMTX API:  http://{MEDIAMTX_HOST}:{MEDIAMTX_PORT}")
    print(f"Logs:          {LOG_DIR.resolve()}")
    print(f"{'='*60}\nWaiting for phone app connection...\n")

    asyncio.create_task(perf_loop())

    app = web.Application()
    app.router.add_route("GET",  "/glasses-ws", handle_websocket)
    app.router.add_route("*",   "/{path_info:.*}", handle_rest)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, SERVER_HOST, SERVER_PORT)
    await site.start()
    await asyncio.Future()  # run forever


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[SERVER] Stopped")
