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
let modelId = null;
let sampleRate = 48000;
let intentionalClose = false;
let backoffMs = 1000;
const MAX_BACKOFF = 30000;

self.onmessage = (e) => {
  const msg = e.data;
  switch (msg.type) {
    case "connect":
      url = msg.url;
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
  }
};

function connect() {
  ws = new WebSocket(url);
  ws.binaryType = "arraybuffer";

  ws.onopen = () => {
    backoffMs = 1000;
    send({ type: "start", modelId, sampleRate });
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

  ws.onclose = () => {
    self.postMessage({ type: "disconnected" });
    if (intentionalClose) return;
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
