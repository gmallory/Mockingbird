// @ts-check
/**
 * WebSocket worker: owns the connection off the main thread.
 *
 * Outbound: Float32 frames from the capture path -> Int16 PCM over the socket.
 * Inbound:  Int16 PCM frames from the server -> Float32 back to the main thread.
 * Reconnects with exponential backoff unless the disconnect was intentional.
 *
 * Runs as a module worker, so it can import the shared conversion helpers.
 */

import { floatTo16, int16ToFloat } from "./utils/audio-utils.js";

/** @type {WebSocket | null} */
let ws = null;
let url = "";
let token = null;
let modelId = null;
let callId = null;
let sampleRate = 48000;
let intentionalClose = false;
let backoffMs = 1000;
const MAX_BACKOFF = 30000;

// App-defined close codes the gateway uses to reject a socket (M6b). These are
// terminal: reconnecting with the same (missing/expired) token or over the same
// limit would just loop, so surface the reason and stop instead of backing off.
const WS_CLOSE_UNAUTHORIZED = 4001;
const WS_CLOSE_RATE_LIMITED = 4029;

self.onmessage = (e) => {
  const msg = e.data;
  switch (msg.type) {
    case "connect":
      url = msg.url;
      token = msg.token ?? null;
      modelId = msg.modelId ?? null;
      sampleRate = msg.sampleRate ?? 48000;
      intentionalClose = false;
      connect();
      break;
    case "disconnect":
      intentionalClose = true;
      if (ws) ws.close();
      ws = null;
      break;
    case "audio":
      sendAudio(msg.data);
      break;
    case "switch_model":
      modelId = msg.modelId;
      send({ type: "switch_model", modelId });
      break;
    case "join_call":
      // Remember the call so a mid-call reconnect re-joins its media bridge.
      callId = msg.callId;
      send({ type: "join_call", callId });
      break;
    case "leave_call":
      callId = null; // gateway side ends with the call; nothing to send
      break;
  }
};

// Append the auth token as a query param when present (the browser can't set a
// header on a WebSocket). No token -> anonymous echo-only session, which the
// gateway still accepts by default.
function wsUrl() {
  if (!token) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}

function connect() {
  ws = new WebSocket(wsUrl());
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    backoffMs = 1000;
    send({ type: "start", modelId, sampleRate });
    if (callId) send({ type: "join_call", callId });
    self.postMessage({ type: "connected" });
  };

  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      const float32 = int16ToFloat(new Int16Array(event.data));
      // Notify main thread a frame returned (for latency), then hand off audio.
      self.postMessage({ type: "audio", data: float32 }, [float32.buffer]);
    } else {
      try {
        self.postMessage({ type: "control", data: JSON.parse(event.data) });
      } catch {
        /* ignore malformed control frame */
      }
    }
  };

  ws.onerror = () => self.postMessage({ type: "error", message: "websocket error" });

  ws.onclose = (event) => {
    self.postMessage({ type: "disconnected" });
    if (intentionalClose) return;
    // Auth / rate-limit rejections are terminal — don't reconnect-storm. Report
    // the reason up so the UI can prompt a re-login or back off.
    if (event.code === WS_CLOSE_UNAUTHORIZED || event.code === WS_CLOSE_RATE_LIMITED) {
      const kind = event.code === WS_CLOSE_UNAUTHORIZED ? "unauthorized" : "rate_limited";
      self.postMessage({ type: "control", data: { type: kind, message: event.reason || "" } });
      return;
    }
    setTimeout(connect, backoffMs);
    backoffMs = Math.min(MAX_BACKOFF, backoffMs * 2);
  };
}

/** @param {Float32Array} float32 */
function sendAudio(float32) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const int16 = floatTo16(float32);
  ws.send(int16.buffer);
}

/** @param {object} obj */
function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
