// @ts-check
/**
 * AudioEngine — main-thread orchestrator for the echo loop.
 *
 *   mic -> MediaStreamSource -> CaptureWorklet -> WebSocket worker -> gateway
 *   gateway (echo) -> WebSocket worker -> PlaybackWorklet -> speakers
 *
 * Emits: 'connection' (status string), 'latency' ({p50,p95}), 'level'
 * ({input,output}), 'error' (Error|string), 'degraded' (message string when the
 * inference hop falls back to passthrough).
 *
 * Echo-slice scope: postMessage transport (no SharedArrayBuffer yet), no auth.
 * The public method surface matches agents/audio-engine.agent.md so later
 * milestones can fill in model switching and controls without API churn.
 */

import { LatencyTracker, LevelSmoother } from "./utils/metrics.js";

const BASE = "/static/js/audio-engine";

/**
 * @typedef {Object} AudioEngineConfig
 * @property {string} websocketUrl
 * @property {number} [sampleRate]
 * @property {number} [chunkSizeMs]
 * @property {boolean} [enableNoiseSuppression]
 * @property {boolean} [enableEchoCancellation]
 * @property {boolean} [enableAutoGainControl]
 */

export class AudioEngine {
  /** @param {AudioEngineConfig} config */
  constructor(config) {
    this.config = {
      sampleRate: 48000,
      chunkSizeMs: 20,
      enableNoiseSuppression: true,
      enableEchoCancellation: true,
      enableAutoGainControl: true,
      ...config,
    };

    /** @type {AudioContext | null} */
    this.audioContext = null;
    /** @type {MediaStream | null} */
    this.mediaStream = null;
    this.captureNode = null;
    this.playbackNode = null;
    this.sourceNode = null;
    /** @type {Worker | null} */
    this.wsWorker = null;

    this.transformEnabled = true;
    this.latency = new LatencyTracker();
    this.inputLevel = new LevelSmoother();
    this.outputLevel = new LevelSmoother();

    // Utterance latency (cloned-voice / walkie-talkie mode): end of speech (last
    // loud input frame) -> first output frame of the converted burst. The FIFO
    // RTT above is only meaningful for the 1:1 echo loop; under utterance
    // conversion, receives are bursty and unaligned to sends.
    this._lastLoudAt = 0;
    this._lastRecvAt = 0;
    /** @type {number[]} */
    this._uttSamples = [];

    /** @type {Map<string, Set<Function>>} */
    this._handlers = new Map();
    this._latencyTimer = null;
    this._levelTimer = null;
  }

  // ----- events -----------------------------------------------------------

  /** @param {string} event @param {Function} handler */
  on(event, handler) {
    if (!this._handlers.has(event)) this._handlers.set(event, new Set());
    this._handlers.get(event).add(handler);
  }

  /** @param {string} event @param {Function} handler */
  off(event, handler) {
    this._handlers.get(event)?.delete(handler);
  }

  /** @param {string} event @param {*} payload */
  _emit(event, payload) {
    this._handlers.get(event)?.forEach((h) => h(payload));
  }

  // ----- lifecycle --------------------------------------------------------

  async start(modelId = null, token = null) {
    const chunkSize = Math.round((this.config.sampleRate * this.config.chunkSizeMs) / 1000);

    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        sampleRate: this.config.sampleRate,
        noiseSuppression: this.config.enableNoiseSuppression,
        echoCancellation: this.config.enableEchoCancellation,
        autoGainControl: this.config.enableAutoGainControl,
      },
    });

    this.audioContext = new AudioContext({
      sampleRate: this.config.sampleRate,
      latencyHint: "interactive",
    });
    if (this.audioContext.state === "suspended") await this.audioContext.resume();

    await this.audioContext.audioWorklet.addModule(`${BASE}/processors/voice-capture.worklet.js`);
    await this.audioContext.audioWorklet.addModule(`${BASE}/processors/voice-playback.worklet.js`);

    this.sourceNode = this.audioContext.createMediaStreamSource(this.mediaStream);

    this.captureNode = new AudioWorkletNode(this.audioContext, "voice-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      channelCount: 1,
      processorOptions: { chunkSize },
    });
    this.captureNode.port.onmessage = (e) => this._onCaptureMessage(e.data);

    this.playbackNode = new AudioWorkletNode(this.audioContext, "voice-playback", {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    });
    this.playbackNode.port.onmessage = (e) => this._onPlaybackMessage(e.data);

    // Capture node writes no output (stays silent), but routing it to the
    // destination keeps it pulled by the render graph. Playback drives audio.
    this.sourceNode.connect(this.captureNode).connect(this.audioContext.destination);
    this.playbackNode.connect(this.audioContext.destination);

    this.wsWorker = new Worker(`${BASE}/websocket-worker.js`, { type: "module" });
    this.wsWorker.onmessage = (e) => this._onWorkerMessage(e.data);
    this.wsWorker.postMessage({
      type: "connect",
      url: this.config.websocketUrl,
      token,
      modelId,
      sampleRate: this.config.sampleRate,
    });

    this._startMeters();
  }

  async stop() {
    this._stopMeters();
    if (this.wsWorker) {
      this.wsWorker.postMessage({ type: "disconnect" });
      this.wsWorker.terminate();
      this.wsWorker = null;
    }
    this.captureNode?.disconnect();
    this.playbackNode?.disconnect();
    this.sourceNode?.disconnect();
    this.mediaStream?.getTracks().forEach((t) => t.stop());
    if (this.audioContext && this.audioContext.state !== "closed") {
      await this.audioContext.close();
    }
    this.audioContext = null;
    this.mediaStream = null;
    this.latency.reset();
    this._uttSamples = [];
    this._lastLoudAt = 0;
    this._lastRecvAt = 0;
    this._emit("connection", "disconnected");
  }

  async dispose() {
    await this.stop();
    this._handlers.clear();
  }

  // ----- model + controls (stubs until inference lands) -------------------

  async switchModel(modelId) {
    this.wsWorker?.postMessage({ type: "switch_model", modelId });
  }

  /**
   * Attach this session to a live call's media bridge (M8a). The gateway then
   * routes converted audio to the phone leg and streams the callee's audio
   * back; emits 'callJoined' on the gateway's ack.
   * @param {string} callId CallRecord id from POST /api/calls/outbound
   */
  joinCall(callId) {
    this.wsWorker?.postMessage({ type: "join_call", callId });
  }

  leaveCall() {
    this.wsWorker?.postMessage({ type: "leave_call" });
  }

  setTransformEnabled(enabled) {
    this.transformEnabled = enabled;
    this.captureNode?.port.postMessage({ type: enabled ? "start" : "stop" });
  }

  setPitchOffset(_semitones) {
    /* no-op until a voice model exists (Milestone 3) */
  }

  setSpeedFactor(_factor) {
    /* no-op until a voice model exists (Milestone 3) */
  }

  // ----- metrics ----------------------------------------------------------

  getLatency() {
    return this.latency.stats();
  }
  getUtteranceLatency() {
    if (this._uttSamples.length === 0) return { last: 0, p50: 0, p95: 0 };
    const sorted = [...this._uttSamples].sort((a, b) => a - b);
    const pct = (q) => sorted[Math.floor(q * (sorted.length - 1))];
    return {
      last: this._uttSamples[this._uttSamples.length - 1],
      p50: pct(0.5),
      p95: pct(0.95),
    };
  }
  getInputLevel() {
    return this.inputLevel.value;
  }
  getOutputLevel() {
    return this.outputLevel.value;
  }

  // ----- internal message handlers ----------------------------------------

  _onCaptureMessage(data) {
    if (data.type === "audio") {
      if (!this.transformEnabled) return;
      this.latency.markSent();
      this.wsWorker?.postMessage({ type: "audio", data: data.data }, [data.data.buffer]);
    } else if (data.type === "level") {
      this.inputLevel.push(data.rms);
      // Last moment the speaker was clearly talking (matches the server VAD's
      // ~0.02 energy gate) — the "end of speech" reference for utterance latency.
      if (data.rms > 0.02) this._lastLoudAt = performance.now();
    }
  }

  _onPlaybackMessage(data) {
    if (data.type === "level") this.outputLevel.push(data.rms);
  }

  _onWorkerMessage(data) {
    switch (data.type) {
      case "connected":
        // Start RTT tracking fresh: any send timestamps from before a
        // reconnect will never be matched (their echoes were dropped), so the
        // FIFO queue must not carry across the connection boundary.
        this.latency.reset();
        this._emit("connection", "connected");
        break;
      case "disconnected":
        this.latency.reset();
        this._emit("connection", "disconnected");
        break;
      case "error":
        this._emit("error", data.message || "connection error");
        break;
      case "audio": {
        const now = performance.now();
        // A gap since the last received frame marks the start of a new output
        // burst — the server just finished converting an utterance.
        if (now - this._lastRecvAt > 300 && this._lastLoudAt > 0) {
          const ms = now - this._lastLoudAt;
          if (ms > 0 && ms < 20000) {
            this._uttSamples.push(ms);
            if (this._uttSamples.length > 50) this._uttSamples.shift();
          }
        }
        this._lastRecvAt = now;
        this.latency.markReceived();
        this.playbackNode?.port.postMessage({ type: "audio", data: data.data }, [data.data.buffer]);
        break;
      }
      case "control": {
        // ready / pong / model_loaded / degraded / error / unauthorized / rate_limited.
        const ctrl = data.data;
        if (ctrl?.type === "error") this._emit("error", ctrl.message);
        else if (ctrl?.type === "degraded") this._emit("degraded", ctrl.message);
        // Terminal auth/limit rejections from the worker (WS closed, no retry).
        else if (ctrl?.type === "unauthorized")
          this._emit("authError", ctrl.message || "session expired — please log in again");
        else if (ctrl?.type === "rate_limited")
          this._emit("limited", ctrl.message || "connection limit reached");
        else if (ctrl?.type === "call_joined") this._emit("callJoined", ctrl.callId);
        break;
      }
    }
  }

  _startMeters() {
    this._latencyTimer = setInterval(() => {
      this._emit("latency", this.latency.stats());
      this._emit("utteranceLatency", this.getUtteranceLatency());
    }, 500);
    this._levelTimer = setInterval(
      () => this._emit("level", { input: this.inputLevel.value, output: this.outputLevel.value }),
      80,
    );
  }

  _stopMeters() {
    clearInterval(this._latencyTimer);
    clearInterval(this._levelTimer);
    this._latencyTimer = null;
    this._levelTimer = null;
  }
}
